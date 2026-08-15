"""Chat client for any OpenAI-compatible endpoint.

stdlib urllib only -- no requests, no openai package. Defaults to OpenRouter
(one key, ~300 models, swap brains at runtime with /model), but the same wire
format is spoken by llama.cpp's server, vLLM, Ollama and friends, so pointing
base_url at a locally hosted model needs no new code path -- see kernel/brain.py.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-opus-5"
TIMEOUT = 180

# OpenRouter reserves max_tokens against your credit balance up front. Left
# unset it reserves the model's entire context (65k+), which a credit-limited
# key cannot afford -- the request fails with a 402 before a single token is
# generated. Cap it at something that still fits a generated app comfortably.
DEFAULT_MAX_TOKENS = 8192


class LLMError(Exception):
    pass


class AuthError(LLMError):
    pass


def _request(url: str, key: str, payload: dict | None = None, stream: bool = False):
    headers = {
        # Local servers ignore auth; sending a placeholder keeps one code path.
        "Authorization": f"Bearer {key or 'none'}",
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
        raise LLMError(f"HTTP {e.code} from {_host(url)}: {msg}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"cannot reach {_host(url)} ({e.reason}) -- is this machine online?") from None


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc or url


class Client:
    """One turn of chat against any OpenAI-compatible /chat/completions."""

    def __init__(self, key: str = "", model: str = DEFAULT_MODEL,
                 max_tokens: int = DEFAULT_MAX_TOKENS, base_url: str = ""):
        self.key = key
        self.model = model
        self.max_tokens = max_tokens
        # Resolved at call time, not bound as a default argument: a default
        # would freeze the module constant at import and silently ignore any
        # later override of llm.API.
        self.base_url = (base_url or API).rstrip("/")

    @property
    def is_local(self) -> bool:
        return "openrouter.ai" not in self.base_url

    # --- account -------------------------------------------------------------

    def check(self) -> dict:
        """Validate the key. OpenRouter-only; local servers have no accounts."""
        if self.is_local:
            return {"local": True, "base_url": self.base_url}
        with _request(f"{self.base_url}/key", self.key) as r:
            return json.loads(r.read()).get("data", {})

    def models(self) -> list:
        with _request(f"{self.base_url}/models", self.key) as r:
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
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools

        content = []
        calls: dict[int, dict] = {}
        finish_reason = None
        usage = {}

        with _request(f"{self.base_url}/chat/completions", self.key, payload, stream=True) as resp:
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


# The class was called OpenRouter before it learned to talk to local servers.
OpenRouter = Client
