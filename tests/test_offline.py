"""What survives with no network.

aiOS thinks by calling OpenRouter, so no network means no reasoning -- that part
is unavoidable with a hosted brain. But an OS that becomes a brick the moment
wifi drops is not an OS, so the question worth testing is what still works:
installed apps, memory, the app registry, and whether the failure is legible
rather than a traceback.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="aios-offline-test-")
os.environ.setdefault("AIOS_HOME", _TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "system"))

from kernel import apps, llm, memory, paths, syscalls  # noqa: E402

HOME = paths.HOME

# Refused immediately rather than hanging on a timeout.
UNREACHABLE = "http://127.0.0.1:1"


class Ctx:
    def __init__(self):
        paths.ensure()
        self.registry = apps.Registry()
        self.memory = memory.Memory()
        self.secrets = {}
        self.model = "test/model"

    def allow(self, name, args):
        return True

    def emit(self, *a, **k):
        pass


class TestBrainOffline(unittest.TestCase):
    """The kernel cannot think offline, but it must say so clearly."""

    def setUp(self):
        self._api = llm.API
        llm.API = UNREACHABLE

    def tearDown(self):
        llm.API = self._api

    def test_chat_raises_legible_error(self):
        # Explicit base_url: this suite must never touch the real network.
        client = llm.Client("sk-test", "anthropic/claude-opus-5", base_url=UNREACHABLE)
        with self.assertRaises(llm.LLMError) as cm:
            client.chat([{"role": "user", "content": "hi"}])
        msg = str(cm.exception)
        self.assertIn("cannot reach", msg)
        self.assertIn("127.0.0.1:1", msg, "the error should name the endpoint it tried")
        self.assertIn("online", msg, "the error should hint at the actual cause")

    def test_check_skips_the_network_for_local_endpoints(self):
        """Self-hosted servers have no /key account endpoint to validate against."""
        info = llm.Client("", base_url=UNREACHABLE).check()
        self.assertTrue(info["local"])
        self.assertEqual(info["base_url"], UNREACHABLE)

    def test_module_constant_override_is_honoured(self):
        """llm.API must not be frozen into a default argument."""
        self.assertEqual(llm.Client("k").base_url, UNREACHABLE)

    def test_error_is_catchable_as_one_type(self):
        """The shell catches llm.LLMError; AuthError must be a subclass."""
        self.assertTrue(issubclass(llm.AuthError, llm.LLMError))


class TestUserlandOffline(unittest.TestCase):
    """Everything that does not need the model must keep working."""

    def setUp(self):
        self.ctx = Ctx()
        self.reg = self.ctx.registry

    def test_installed_apps_still_run(self):
        self.reg.install("offline-calc", "adds numbers", "spec",
                         "import sys\nprint(sum(int(a) for a in sys.argv[1:]))\n")
        r = self.reg.run("offline-calc", ["21", "21"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["stdout"].strip(), "42")

    def test_app_listing_works(self):
        self.reg.install("offline-note", "d", "s", "print('x')")
        self.assertIn("offline-note", [a.name for a in self.reg.all()])

    def test_memory_read_and_search_work(self):
        self.ctx.memory.write("Wifi password", "the cafe one is 'hunter2'", ["places"])
        hits = self.ctx.memory.search("wifi")
        self.assertTrue(hits)
        self.assertIn("hunter2", hits[0]["content"])

    def test_local_syscalls_work(self):
        syscalls.dispatch("fs_write", {"path": "data/offline.txt", "content": "still here"}, self.ctx)
        self.assertEqual(syscalls.dispatch("fs_read", {"path": "data/offline.txt"}, self.ctx), "still here")
        self.assertIn("data", syscalls.dispatch("fs_list", {"path": "/"}, self.ctx))

    def test_networked_syscalls_fail_without_crashing(self):
        """A dead network must produce an error string, never an exception."""
        out = syscalls.dispatch("net_fetch", {"url": UNREACHABLE}, self.ctx)
        self.assertTrue(out.startswith("error:"), out[:120])
        self.assertIn("cannot reach", out)

    def test_an_app_needing_net_fails_cleanly(self):
        self.reg.install(
            "offline-fetch", "needs the net", "spec",
            "import urllib.request\n"
            f"urllib.request.urlopen('{UNREACHABLE}', timeout=5)\n",
            caps=["net"],
        )
        r = self.reg.run("offline-fetch", timeout=30)
        self.assertFalse(r["ok"])
        self.assertTrue(r["stderr"].strip(), "a failing app should explain itself on stderr")


if __name__ == "__main__":
    unittest.main(verbosity=2)
