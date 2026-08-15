"""Kernel loop tests, driven by a scripted fake model.

Exercises the real syscall dispatch, permission gate and app installation path
without needing an API key -- only the model is faked.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="aios-agent-test-")
os.environ.setdefault("AIOS_HOME", _TMP)  # first module to import wins; all must agree
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "system"))

from kernel import agent, apps, memory, paths  # noqa: E402

HOME = paths.HOME  # the root that actually got bound, not necessarily _TMP


def call(_name: str, **args) -> dict:
    """_name is underscored so syscalls with their own "name" argument work."""
    return {
        "id": f"call_{_name}",
        "type": "function",
        "function": {"name": _name, "arguments": json.dumps(args)},
    }


class FakeClient:
    """Replays a scripted list of model replies."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat(self, messages, tools=None, on_delta=None):
        self.seen.append(list(messages))
        reply = self.script.pop(0) if self.script else {"content": "done", "tool_calls": []}
        reply = {"content": "", "tool_calls": [], "finish_reason": "stop", "usage": {}, **reply}
        if reply["content"] and on_delta:
            on_delta(reply["content"])
        return reply


def make_kernel(script, autonomy="full"):
    paths.ensure()
    ctx = agent.Context(
        registry=apps.Registry(),
        memory=memory.Memory(),
        secrets={"OPENROUTER_API_KEY": "sk-test"},
        model="fake/model",
        autonomy=autonomy,
    )
    return agent.Kernel(FakeClient(script), ctx), ctx


class TestKernelLoop(unittest.TestCase):
    def test_plain_reply(self):
        k, _ = make_kernel([{"content": "hello"}])
        self.assertEqual(k.turn("hi"), "hello")

    def test_tool_call_then_answer(self):
        k, _ = make_kernel(
            [
                {"tool_calls": [call("fs_write", path="data/t.txt", content="abc")]},
                {"content": "wrote it"},
            ]
        )
        self.assertEqual(k.turn("write a file"), "wrote it")
        self.assertEqual((HOME / "data" / "t.txt").read_text(), "abc")

    def test_tool_result_is_fed_back(self):
        """The model must see the syscall output on the next turn."""
        k, _ = make_kernel(
            [{"tool_calls": [call("fs_list", path="/")]}, {"content": "ok"}]
        )
        k.turn("list root")
        last = k.client.seen[-1]
        tool_msgs = [m for m in last if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("apps", tool_msgs[0]["content"])

    def test_denied_syscall_reports_back(self):
        k, _ = make_kernel(
            [{"tool_calls": [call("proc_run", command="echo nope")]}, {"content": "understood"}],
            autonomy="readonly",
        )
        k.turn("run something")
        tool_msg = [m for m in k.client.seen[-1] if m.get("role") == "tool"][0]
        self.assertTrue(tool_msg["content"].startswith("denied"))

    def test_parallel_tool_calls(self):
        k, _ = make_kernel(
            [
                {
                    "tool_calls": [
                        call("fs_write", path="data/a", content="1"),
                        call("fs_write", path="data/b", content="2"),
                    ]
                },
                {"content": "both done"},
            ]
        )
        k.turn("write two files")
        self.assertEqual((HOME / "data" / "a").read_text(), "1")
        self.assertEqual((HOME / "data" / "b").read_text(), "2")

    def test_malformed_arguments_do_not_crash(self):
        k, _ = make_kernel([{"content": "recovered"}])
        k.client.script.insert(
            0,
            {
                "tool_calls": [
                    {"id": "x", "type": "function", "function": {"name": "fs_read", "arguments": "{not json"}}
                ]
            },
        )
        self.assertEqual(k.turn("break it"), "recovered")

    def test_runaway_loop_is_stopped(self):
        """A model that only ever calls tools must stall, not bill forever."""
        script = [{"tool_calls": [call("fs_list", path="/")]}] * (agent.MAX_STEPS + 5)
        k, _ = make_kernel(script)
        self.assertIn("stopped after", k.turn("loop forever"))


class TestSelfBuilding(unittest.TestCase):
    def test_app_build_installs_and_runs(self):
        """The headline behaviour: a prompt becomes a permanent command."""
        code = "import sys\nprint('sum', sum(int(a) for a in sys.argv[1:]))\n"
        k, ctx = make_kernel(
            [
                {
                    "tool_calls": [
                        call(
                            "app_build",
                            name="adder",
                            description="adds numbers",
                            spec="Add the integers given as arguments.",
                            code=code,
                            caps=[],
                        )
                    ]
                },
                {"tool_calls": [call("app_run", name="adder", args=["2", "3"])]},
                {"content": "installed adder"},
            ]
        )
        k.turn("build me a calculator")

        app = ctx.registry.get("adder")
        self.assertIsNotNone(app, "app was not installed")
        self.assertEqual(app.description, "adds numbers")
        self.assertIn("Add the integers", app.spec)
        self.assertEqual(app.model, "fake/model")

        result = ctx.registry.run("adder", ["2", "3"])
        self.assertEqual(result["stdout"].strip(), "sum 5")

    def test_boot_context_lists_installed_apps(self):
        """A new session must know what the OS already is."""
        k, ctx = make_kernel([{"content": "ok"}])
        ctx.registry.install("weather", "shows the forecast", "spec", "print('sunny')")
        k.refresh_boot_context()
        self.assertIn("weather: shows the forecast", k.messages[0]["content"])

    def test_boot_context_lists_memory(self):
        k, ctx = make_kernel([{"content": "ok"}])
        ctx.memory.write("Preferred editor", "helix")
        k.refresh_boot_context()
        self.assertIn("Preferred editor", k.messages[0]["content"])

    def test_session_is_journaled(self):
        k, _ = make_kernel([{"tool_calls": [call("fs_list", path="/")]}, {"content": "ok"}])
        k.turn("list things")
        lines = [json.loads(l) for l in k.log_path.read_text().splitlines()]
        roles = [e.get("role") for e in lines][-5:]
        # A transition is journaled around every syscall: it is the world
        # model's training data.
        self.assertEqual(roles, ["user", "assistant", "transition", "tool", "assistant"])

    def test_transitions_are_journaled_for_the_world_model(self):
        from kernel import world

        k, _ = make_kernel([{"tool_calls": [call("fs_write", path="data/w.txt", content="x")]},
                            {"content": "done"}])
        k.turn("write a file")

        entries = [json.loads(l) for l in k.log_path.read_text().splitlines()]
        transitions = [e for e in entries if e.get("role") == "transition"]
        self.assertTrue(transitions, "no transition journaled")

        t = transitions[-1]
        self.assertEqual(t["action"], "fs_write")
        self.assertEqual(len(t["s"]), world.D_STATE)
        self.assertEqual(len(t["a"]), world.D_ACTION)
        self.assertEqual(len(t["s2"]), world.D_STATE)
        self.assertNotEqual(t["s"], t["s2"], "writing a file should move the state embedding")

    def test_transitions_are_loadable_as_training_data(self):
        from kernel import world

        k, _ = make_kernel([{"tool_calls": [call("fs_write", path="data/z.txt", content="y")]},
                            {"content": "done"}])
        k.turn("write another")
        samples = world.load_transitions(k.log_path.parent)
        self.assertTrue(samples)
        state, action, nxt = samples[-1]
        self.assertEqual((len(state), len(action), len(nxt)),
                         (world.D_STATE, world.D_ACTION, world.D_STATE))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMalformedToolArguments(unittest.TestCase):
    """Smaller models emit raw newlines inside JSON strings, most often on
    app_build because it carries a whole program as a string."""

    def test_repair_escapes_control_chars_in_strings(self):
        broken = '{"code": "import sys\nprint(1)\n", "name": "x"}'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(broken)
        parsed = json.loads(agent.repair_json(broken))
        self.assertEqual(parsed["code"], "import sys\nprint(1)\n")
        self.assertEqual(parsed["name"], "x")

    def test_repair_leaves_valid_json_untouched(self):
        good = '{"code": "print(1)\\n", "n": 3, "deep": {"a": [1, 2]}}'
        self.assertEqual(json.loads(agent.repair_json(good)), json.loads(good))

    def test_repair_preserves_existing_escapes(self):
        raw = '{"s": "a \\"quoted\\" word\nand a newline"}'
        self.assertEqual(json.loads(agent.repair_json(raw))["s"], 'a "quoted" word\nand a newline')

    def test_control_chars_outside_strings_are_untouched(self):
        raw = '{\n  "a": 1\n}'
        self.assertEqual(json.loads(agent.repair_json(raw)), {"a": 1})

    def test_parse_args_reports_whether_it_repaired(self):
        args, repaired = agent.parse_args('{"a": 1}')
        self.assertIsNone(repaired)
        args, repaired = agent.parse_args('{"a": "x\ny"}')
        self.assertEqual(args["a"], "x\ny")
        self.assertIsNotNone(repaired)

    def test_kernel_repairs_and_rewrites_the_message(self):
        """The stored message must be valid, or the next request is rejected."""
        code = "import sys\nprint('hi')\n"
        bad = '{"name": "fixme", "description": "d", "spec": "s", "code": "%s"}' % code
        k, ctx = make_kernel([
            {"tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "app_build", "arguments": bad}}]},
            {"content": "built it"},
        ])
        self.assertEqual(k.turn("build something"), "built it")

        self.assertIsNotNone(ctx.registry.get("fixme"), "repaired call should have installed")
        stored = [m for m in k.messages if m.get("tool_calls")][0]["tool_calls"][0]
        json.loads(stored["function"]["arguments"])  # must not raise

    def test_unsalvageable_arguments_are_neutralised(self):
        k, _ = make_kernel([
            {"tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "fs_read", "arguments": "{definitely not json"}}]},
            {"content": "recovered"},
        ])
        self.assertEqual(k.turn("go"), "recovered")
        stored = [m for m in k.messages if m.get("tool_calls")][0]["tool_calls"][0]
        self.assertEqual(json.loads(stored["function"]["arguments"]), {})
