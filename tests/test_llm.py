"""OpenRouter wire-format handling.

llm.py parses a server-sent-event stream by hand and reassembles tool calls that
arrive in fragments. That logic is the most fragile part of the kernel and the
hardest to reach from a terminal, so it is exercised here against synthetic
streams shaped like the real ones.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "system"))

from kernel import llm  # noqa: E402


class FakeResponse:
    """Stands in for the urlopen result: a context manager yielding byte lines."""

    def __init__(self, lines):
        self.lines = [l.encode("utf-8") if isinstance(l, str) else l for l in lines]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.lines)


def sse(**delta) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


class StreamTest(unittest.TestCase):
    """Runs a chat() against a scripted stream and returns the parsed reply."""

    def run_stream(self, lines, capture_payload=None):
        original = llm._request

        def fake_request(url, key, payload=None, stream=False):
            if capture_payload is not None and payload:
                capture_payload.update(payload)
            return FakeResponse(lines)

        llm._request = fake_request
        try:
            return llm.OpenRouter("sk-test").chat([{"role": "user", "content": "hi"}])
        finally:
            llm._request = original


class TestRequestShape(StreamTest):
    def test_max_tokens_is_capped(self):
        """Unset, OpenRouter reserves the whole context and a limited key 402s."""
        payload = {}
        self.run_stream([sse(content="ok"), "data: [DONE]"], capture_payload=payload)
        self.assertEqual(payload["max_tokens"], llm.DEFAULT_MAX_TOKENS)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["model"], llm.DEFAULT_MODEL)

    def test_max_tokens_is_overridable(self):
        payload = {}
        original = llm._request
        llm._request = lambda *a, **k: (payload.update(k.get("payload") or a[2] or {}),
                                        FakeResponse([sse(content="x"), "data: [DONE]"]))[1]
        try:
            llm.OpenRouter("sk-test", max_tokens=256).chat([{"role": "user", "content": "hi"}])
        finally:
            llm._request = original
        self.assertEqual(payload["max_tokens"], 256)


class TestStreamParsing(StreamTest):
    def test_content_is_assembled_in_order(self):
        reply = self.run_stream([
            sse(content="Hello"),
            sse(content=", "),
            sse(content="world"),
            "data: [DONE]",
        ])
        self.assertEqual(reply["content"], "Hello, world")
        self.assertEqual(reply["tool_calls"], [])

    def test_keepalive_comments_are_skipped(self):
        """OpenRouter emits ': OPENROUTER PROCESSING' between chunks."""
        reply = self.run_stream([
            ": OPENROUTER PROCESSING",
            "",
            sse(content="a"),
            ": OPENROUTER PROCESSING",
            sse(content="b"),
            "data: [DONE]",
        ])
        self.assertEqual(reply["content"], "ab")

    def test_malformed_chunk_does_not_abort_the_stream(self):
        reply = self.run_stream([
            sse(content="good"),
            "data: {not valid json",
            sse(content=" end"),
            "data: [DONE]",
        ])
        self.assertEqual(reply["content"], "good end")

    def test_on_delta_receives_each_token(self):
        seen = []
        original = llm._request
        llm._request = lambda *a, **k: FakeResponse([sse(content="x"), sse(content="y"), "data: [DONE]"])
        try:
            llm.OpenRouter("sk-test").chat([{"role": "user", "content": "hi"}], on_delta=seen.append)
        finally:
            llm._request = original
        self.assertEqual(seen, ["x", "y"])

    def test_usage_and_finish_reason_are_captured(self):
        reply = self.run_stream([
            sse(content="hi"),
            "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            "data: " + json.dumps({"choices": [], "usage": {"total_tokens": 42, "cost": 0.001}}),
            "data: [DONE]",
        ])
        self.assertEqual(reply["finish_reason"], "stop")
        self.assertEqual(reply["usage"]["total_tokens"], 42)


class TestToolCallAssembly(StreamTest):
    """Tool calls arrive in pieces: the name first, then arguments byte by byte."""

    def test_fragmented_arguments_are_joined(self):
        reply = self.run_stream([
            sse(tool_calls=[{"index": 0, "id": "call_1", "function": {"name": "fs_read", "arguments": ""}}]),
            sse(tool_calls=[{"index": 0, "function": {"arguments": '{"pa'}}]),
            sse(tool_calls=[{"index": 0, "function": {"arguments": 'th": "/a'}}]),
            sse(tool_calls=[{"index": 0, "function": {"arguments": 'ios.json"}'}}]),
            "data: [DONE]",
        ])
        self.assertEqual(len(reply["tool_calls"]), 1)
        call = reply["tool_calls"][0]
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["function"]["name"], "fs_read")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "/aios.json"})

    def test_parallel_calls_stay_separate_and_ordered(self):
        reply = self.run_stream([
            sse(tool_calls=[{"index": 0, "id": "a", "function": {"name": "fs_read", "arguments": "{}"}}]),
            sse(tool_calls=[{"index": 1, "id": "b", "function": {"name": "fs_list", "arguments": "{}"}}]),
            sse(tool_calls=[{"index": 1, "function": {"arguments": ""}}]),
            "data: [DONE]",
        ])
        names = [c["function"]["name"] for c in reply["tool_calls"]]
        self.assertEqual(names, ["fs_read", "fs_list"])
        self.assertEqual([c["id"] for c in reply["tool_calls"]], ["a", "b"])

    def test_content_and_tool_calls_can_coexist(self):
        reply = self.run_stream([
            sse(content="checking that"),
            sse(tool_calls=[{"index": 0, "id": "z", "function": {"name": "web_search", "arguments": '{"query":"x"}'}}]),
            "data: [DONE]",
        ])
        self.assertEqual(reply["content"], "checking that")
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "web_search")

    def test_missing_index_defaults_to_zero(self):
        reply = self.run_stream([
            sse(tool_calls=[{"id": "solo", "function": {"name": "app_list", "arguments": "{}"}}]),
            "data: [DONE]",
        ])
        self.assertEqual(len(reply["tool_calls"]), 1)
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "app_list")


if __name__ == "__main__":
    unittest.main(verbosity=2)
