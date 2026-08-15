"""Kernel tests: the filesystem jail, the permission gate, memory ranking and
the app lifecycle.

The jail and the secret-injection tests are the important ones. Everything the
model can do to the host passes through those two boundaries, and a generated
app is code nobody reviewed.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# AIOS_HOME must be set before kernel imports, since paths binds it at import.
_TMP = tempfile.mkdtemp(prefix="aios-test-")
os.environ.setdefault("AIOS_HOME", _TMP)  # first module to import wins; all must agree
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "system"))

from kernel import apps, memory, paths, syscalls  # noqa: E402

HOME = paths.HOME  # the root that actually got bound, not necessarily _TMP


class Ctx:
    """Minimal syscall context, with a scriptable permission gate."""

    def __init__(self, allow=True):
        paths.ensure()
        self.registry = apps.Registry()
        self.memory = memory.Memory()
        self.secrets = {"OPENROUTER_API_KEY": "sk-secret", "OTHER_KEY": "other-secret"}
        self.model = "test/model"
        self._allow = allow
        self.asked = []

    def allow(self, name, args):
        self.asked.append(name)
        return self._allow

    def emit(self, *a, **k):
        pass


class TestFilesystemJail(unittest.TestCase):
    """The agent must not be able to touch anything outside the aiOS root."""

    def setUp(self):
        self.ctx = Ctx()

    def test_traversal_is_blocked(self):
        for escape in ("../../etc/passwd", "/../../../etc/passwd", "data/../../../../etc/passwd"):
            out = syscalls.dispatch("fs_read", {"path": escape}, self.ctx)
            self.assertTrue(out.startswith("denied"), f"{escape!r} was not denied: {out[:80]}")

    def test_absolute_paths_are_root_relative(self):
        """'/etc/passwd' means <root>/etc/passwd, not the host's."""
        syscalls.dispatch("fs_write", {"path": "/etc/passwd", "content": "fake"}, self.ctx)
        self.assertTrue((HOME / "etc" / "passwd").exists())
        self.assertEqual((HOME / "etc" / "passwd").read_text(), "fake")

    def test_symlink_escape_is_blocked(self):
        """A symlink pointing out of the root must not become an exit."""
        link = HOME / "escape"
        if not link.exists():
            link.symlink_to("/etc")
        out = syscalls.dispatch("fs_read", {"path": "escape/hosts"}, self.ctx)
        self.assertTrue(out.startswith("denied"), out[:80])

    def test_write_read_round_trip(self):
        syscalls.dispatch("fs_write", {"path": "data/note.txt", "content": "hello"}, self.ctx)
        self.assertEqual(syscalls.dispatch("fs_read", {"path": "data/note.txt"}, self.ctx), "hello")


class TestPermissionGate(unittest.TestCase):
    def test_mutating_calls_are_gated(self):
        ctx = Ctx(allow=False)
        out = syscalls.dispatch("fs_write", {"path": "data/x", "content": "y"}, ctx)
        self.assertTrue(out.startswith("denied"))
        self.assertFalse((HOME / "data" / "x").exists())
        self.assertEqual(ctx.asked, ["fs_write"])

    def test_read_only_calls_are_not_gated(self):
        ctx = Ctx(allow=False)
        syscalls.dispatch("fs_list", {"path": "/"}, ctx)
        self.assertEqual(ctx.asked, [], "fs_list should not require approval")

    def test_unknown_syscall(self):
        self.assertIn("no such syscall", syscalls.dispatch("rm_rf", {}, Ctx()))

    def test_syscall_exception_does_not_escape(self):
        """A broken syscall returns an error string, it does not kill the kernel."""
        out = syscalls.dispatch("fs_read", {}, Ctx())  # missing required arg
        self.assertTrue(out.startswith("error:"), out)


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.mem = memory.Memory(HOME / "memtest")

    def test_write_and_search(self):
        self.mem.write("VPS credentials", "The Hetzner box is at 10.0.0.4", ["infra"])
        self.mem.write("Coffee", "I drink oat flat whites", ["personal"])
        hits = self.mem.search("hetzner vps")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["title"], "VPS credentials")

    def test_title_outranks_body(self):
        self.mem.write("Rust", "nothing relevant here", [])
        self.mem.write("Shopping", "I should learn rust someday", [])
        self.assertEqual(self.mem.search("rust")[0]["title"], "Rust")

    def test_rewrite_updates_in_place(self):
        self.mem.write("Server", "old value")
        self.mem.write("Server", "new value")
        matches = [n for n in self.mem.all() if n["title"] == "Server"]
        self.assertEqual(len(matches), 1)
        self.assertIn("new value", matches[0]["content"])

    def test_empty_query(self):
        self.assertEqual(self.mem.search(""), [])

    def test_forget(self):
        self.mem.write("Temporary", "x")
        self.assertTrue(self.mem.forget("Temporary"))
        self.assertFalse(self.mem.forget("Temporary"))


class TestAppLifecycle(unittest.TestCase):
    def setUp(self):
        self.ctx = Ctx()
        self.reg = self.ctx.registry

    def test_build_run_round_trip(self):
        out = syscalls.dispatch(
            "app_build",
            {
                "name": "greet",
                "description": "says hello",
                "spec": "Print a greeting for the name given in argv.",
                "code": "import sys\nprint('hello', sys.argv[1] if len(sys.argv)>1 else 'world')\n",
                "caps": [],
            },
            self.ctx,
        )
        self.assertTrue(out.startswith("installed"), out)

        result = self.reg.run("greet", ["aios"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"].strip(), "hello aios")

    def test_only_declared_secrets_are_injected(self):
        """An app that asked for one key must not see the others."""
        code = (
            "import os\n"
            "print('GOT_OPENROUTER=' + os.environ.get('OPENROUTER_API_KEY',''))\n"
            "print('GOT_OTHER=' + os.environ.get('OTHER_KEY',''))\n"
        )
        self.reg.install(
            "peek", "tries to read keys", "spec", code, caps=["secrets"], secrets=["OPENROUTER_API_KEY"]
        )
        out = self.reg.run("peek", secrets=self.ctx.secrets)["stdout"]
        self.assertIn("GOT_OPENROUTER=sk-secret", out)
        self.assertIn("GOT_OTHER=\n", out, "undeclared secret leaked into the app environment")

    def test_no_secrets_by_default(self):
        self.reg.install("blind", "d", "s", "import os\nprint(os.environ.get('OPENROUTER_API_KEY',''))\n")
        self.assertEqual(self.reg.run("blind", secrets=self.ctx.secrets)["stdout"].strip(), "")

    def test_invalid_names_rejected(self):
        for bad in ("../evil", "Evil", "9lives", "has space", ""):
            with self.assertRaises(apps.AppError, msg=f"{bad!r} was accepted"):
                self.reg.install(bad, "d", "s", "print(1)")

    def test_unknown_capability_rejected(self):
        with self.assertRaises(apps.AppError):
            self.reg.install("x", "d", "s", "print(1)", caps=["root"])

    def test_rebuild_bumps_version_and_keeps_data(self):
        app = self.reg.install("counter", "v1", "spec", "print(1)")
        (app.path / "data" / "state.txt").write_text("keep me")
        self.reg.install("counter", "v2", "spec", "print(2)")
        import json

        manifest = json.loads((app.path / "manifest.json").read_text())
        self.assertEqual(manifest["version"], 2)
        self.assertEqual((app.path / "data" / "state.txt").read_text(), "keep me")

    def test_failing_app_reports_error(self):
        self.reg.install("boom", "fails", "spec", "import sys\nsys.exit(3)\n")
        r = self.reg.run("boom")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], 3)

    def test_timeout(self):
        self.reg.install("hang", "hangs", "spec", "import time\ntime.sleep(30)\n")
        r = self.reg.run("hang", timeout=1)
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["stderr"])

    def test_app_runs_in_its_own_data_dir(self):
        self.reg.install("cwd", "d", "s", "import os\nprint(os.getcwd())\n")
        out = self.reg.run("cwd")["stdout"].strip()
        self.assertTrue(out.endswith("apps/cwd/data"), out)


class TestExecutorDelegation(unittest.TestCase):
    """When a confining executor is configured, apps must not run locally."""

    class FakeExecutor:
        name = "fake"

        def __init__(self):
            self.calls = []

        def run(self, app, args=None, secrets=None, timeout=120):
            self.calls.append({"app": app.name, "caps": app.caps, "args": args, "timeout": timeout})
            return {"ok": True, "code": 0, "stdout": "from sandbox", "stderr": "", "sandboxed": True}

    def test_executor_is_used_instead_of_subprocess(self):
        ex = self.FakeExecutor()
        reg = apps.Registry(executor=ex)
        # Code that would prove local execution by writing to the host.
        canary = HOME / "canary.txt"
        reg.install("escapee", "d", "s", f"open({str(canary)!r},'w').write('ran locally')\n", caps=["net"])

        result = reg.run("escapee", ["x"])
        self.assertEqual(result["stdout"], "from sandbox")
        self.assertTrue(result["sandboxed"])
        self.assertFalse(canary.exists(), "app executed locally despite a configured executor")
        self.assertEqual(ex.calls[0]["app"], "escapee")
        self.assertEqual(ex.calls[0]["caps"], ["net"])

    def test_missing_app_still_raises_before_executor(self):
        ex = self.FakeExecutor()
        with self.assertRaises(apps.AppError):
            apps.Registry(executor=ex).run("nonexistent")
        self.assertEqual(ex.calls, [])

    def test_off_config_yields_no_executor(self):
        from kernel import sandbox

        self.assertIsNone(sandbox.from_config({"sandbox": "off"}))
        self.assertIsNone(sandbox.from_config({}))

    def test_unknown_backend_rejected(self):
        from kernel import sandbox

        with self.assertRaises(sandbox.SandboxError):
            sandbox.from_config({"sandbox": "chroot-please"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBuildTimeSmokeCheck(unittest.TestCase):
    """app_build must catch programs that install cleanly and then do nothing."""

    def setUp(self):
        self.ctx = Ctx()

    def build(self, name, code):
        return syscalls.dispatch(
            "app_build",
            {"name": name, "description": "d", "spec": "s", "code": code},
            self.ctx,
        )

    def test_uncalled_main_is_caught(self):
        """The exact bug a 7B model shipped: main() defined, never called."""
        out = self.build("smoke-dice", "import random\n\ndef main():\n    print(random.randint(1,6))\n")
        self.assertIn("never calls it", out)
        self.assertIn("printed nothing", out)
        self.assertIn("FIX THE CODE", out)
        self.assertNotIn("The user can now run it", out)

    def test_working_app_reports_its_output(self):
        out = self.build("smoke-good", "print('hello from the app')\n")
        self.assertIn("smoke run ok: hello from the app", out)
        self.assertIn("The user can now run it", out)
        self.assertNotIn("PROBLEM", out)

    def test_non_stdlib_import_is_caught(self):
        out = self.build("smoke-deps", "import requests\nprint(requests)\n")
        self.assertIn("not in the standard library", out)
        self.assertIn("FIX THE CODE", out)

    def test_syntax_error_is_caught(self):
        out = self.build("smoke-broken", "def oops(:\n    pass\n")
        self.assertIn("syntax error", out)
        self.assertIn("FIX THE CODE", out)

    def test_app_requiring_arguments_is_not_condemned(self):
        """Exiting non-zero with no args can be legitimate; report, don't accuse."""
        code = (
            "import sys\n"
            "if len(sys.argv) < 2:\n"
            "    sys.exit('usage: needs an argument')\n"
            "print(sys.argv[1])\n"
        )
        out = self.build("smoke-args", code)
        self.assertIn("may be expected", out)
        self.assertNotIn("FIX THE CODE", out)

    def test_app_is_still_installed_when_suspect(self):
        """Flagging a problem must not lose the code -- the model has to read it."""
        self.build("smoke-keep", "def main():\n    print(1)\n")
        app = self.ctx.registry.get("smoke-keep")
        self.assertIsNotNone(app)
        self.assertIn("def main", app.code)
