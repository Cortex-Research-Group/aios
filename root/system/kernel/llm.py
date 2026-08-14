"""OpenRouter client.

stdlib urllib only -- no requests, no openai package. One key, ~300 models, and
the brain is swappable at runtime with /model.
"""

import json
import urllib.error
import urllib.request

API = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-opus-4.5"
TIMEOUT = 180


class LLMError(Exception):
    pass


class AuthError(LLMError):
    pass


def _request(url: str, key: str, payload: dict | None = None, stream: bool = False):
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # OpenRouter attributes traffic with these; they are optional but polite.
        "HTTP-Referer": "https://github.com/aios",
        "X-Title": "aiOS",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body[:400] or e.reason
        if e.code in (401, 403):
            raise AuthError(f"OpenRouter rejected the key: {msg}") from None
        raise LLMError(f"OpenRouter {e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"cannot reach OpenRouter ({e.reason}) -- is this machine online?") from None


class OpenRouter:
    def __init__(self, key: str, model: str = DEFAULT_MODEL):
        self.key = key
        self.model = model

    # --- account -------------------------------------------------------------

    def check(self) -> dict:
        """Validate the key. Returns OpenRouter's account info."""
        with _request(f"{API}/key", self.key) as r:
            return json.loads(r.read()).get("data", {})

    def models(self) -> list:
        with _request(f"{API}/models", self.key) as r:
            return json.loads(r.read()).get("data", [])

    # --- inference -----------------------------------------------------------

    def chat(self, messages: list, tools: list | None = None, on_delta=None) -> dict:
        """One turn. Streams tokens to on_delta as they arrive.

        Returns {"content", "tool_calls", "finish_reason", "usage"}. tool_calls
        come back in OpenAI wire format so they can be appended to messages
        verbatim on the next turn.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = tools

        content = []
        calls: dict[int, dict] = {}
        finish_reason = None
        usage = {}

        with _request(f"{API}/chat/completions", self.key, payload, stream=True) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                # OpenRouter emits ": OPENROUTER PROCESSING" keepalive comments.
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    continue
                blob = line[6:]
                if blob == "[DONE]":
                    break

                try:
                    chunk = json.loads(blob)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    usage = chunk["usage"]

                for choice in chunk.get("choices") or []:
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}

                    text = delta.get("content")
                    if text:
                        content.append(text)
                        if on_delta:
                            on_delta(text)

                    # Tool calls arrive fragmented: name in one chunk, arguments
                    # dribbled across many. Accumulate by index.
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = calls.setdefault(
                            idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]

        return {
            "content": "".join(content),
            "tool_calls": [calls[i] for i in sorted(calls)],
            "finish_reason": finish_reason,
            "usage": usage,
        }
