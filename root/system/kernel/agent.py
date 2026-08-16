"""The kernel: perceive, plan, syscall, observe, repeat.

Holds the conversation, drives the model, dispatches syscalls and enforces the
permission gate. Deliberately UI-agnostic -- it emits events through a callback
so the TUI today and any other shell later are just clients.
"""

import json
import os
import time
from pathlib import Path

from . import paths
from . import schedule as _schedule
from . import syscalls, world

MAX_STEPS = 25  # a runaway tool loop should stall, not bill you forever

_CONTROL = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def repair_json(raw: str) -> str:
    """Escape raw control characters that appear inside JSON string literals.

    Smaller models routinely emit tool arguments containing a literal newline
    inside a quoted string -- which is invalid JSON -- and it happens most often
    on exactly the call that matters here, app_build, because that one carries a
    whole program as a string. Rejecting those outright makes weak models
    unusable for the OS's central feature, so repair the common case instead.
    """
    out = []
    in_string = False
    escaped = False
    for ch in raw:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        out.append(_CONTROL[ch] if (in_string and ch in _CONTROL) else ch)
    return "".join(out)


def parse_args(raw: str) -> tuple[dict, str | None]:
    """Parse tool-call arguments, repairing them if needed.

    Returns (args, repaired_raw). repaired_raw is None when no repair was
    needed; when it is set the caller must store it back on the assistant
    message, or the next request replays the malformed JSON and the server
    rejects the whole conversation.
    """
    try:
        return json.loads(raw or "{}"), None
    except json.JSONDecodeError:
        fixed = repair_json(raw)
        return json.loads(fixed), fixed  # may raise; caller handles

SYSTEM_PROMPT = """You are aiOS, an operating system whose userland is written by you, on demand.

You run from a portable root directory that may live on a USB stick, a VM disk or a VPS.
Everything you keep -- apps, memory, logs -- lives under that root and travels with it.

THE CORE IDEA
When the user wants a capability you do not have, you do not simulate it and you do not
just explain how they could build it. You build it, with app_build, and it becomes a
permanent command in this OS. The user's prompts are how this OS grows. Treat every
"can you..." as a candidate for a real, installed app.

WRITING APPS
- Standard library only. There is no pip at boot; an app with an import that is not in
  the stdlib is a broken app.
- Complete and runnable. No TODOs, no placeholder functions, no "..." bodies.
- Read arguments from sys.argv. Print human-readable output to stdout. Exit non-zero on error.
- Write persistent state to os.environ["AIOS_APP_DATA"], which always exists when an app runs.
- Declare capabilities honestly in caps: net, fs, proc, secrets. The user sees them before install.
- For API keys, declare the vault key name in secrets and read it from os.environ.
- After building, run it with app_run to prove it works. If it fails, read the error, fix
  the code and rebuild. Do not hand the user a broken app.

SCHEDULING
An app that only runs when the user types its name is half a capability. When what they
asked for is recurring -- "every morning", "keep an eye on", "check hourly", "remind me" --
build the app, then schedule it with sched_add. Only installed apps can be scheduled; there
is no way to schedule a shell command, and that is deliberate. Scheduled jobs run unattended
with the app's declared capabilities, so say so plainly when you create one. Jobs only fire
while the scheduler daemon is running, which the user starts with /sched start.

MEMORY
Use mem_write for durable facts about the user, their machines, their preferences and their
projects -- things worth knowing on a future boot. Use mem_search before asking the user
something you may already have been told. Do not record trivia or things only relevant to
the current conversation.

STYLE
You are a systems tool, not a chat assistant. Be terse. Report what you did, not what you
are about to do. No preamble, no filler, no restating the request. When a syscall's output
answers the question, say the answer -- do not narrate the syscall.
"""


class Context:
    """Everything a syscall needs, plus the permission gate."""

    def __init__(self, registry, memory, secrets, model, autonomy="ask", confirm=None,
                 emit=None, world_model=None, record=True, schedule=None):
        self.registry = registry
        self.memory = memory
        self.schedule = schedule if schedule is not None else _schedule.Schedule()
        self.secrets = secrets
        self.model = model
        self.autonomy = autonomy  # 'ask' | 'full' | 'readonly'
        self._confirm = confirm
        self._emit = emit
        # The world model predicts what a syscall would do. Recording the
        # before/after embeddings is what gives it something to learn from.
        self.world_model = world_model
        self.record = record

    def observe(self):
        """Embed the current OS state, or None if recording is off."""
        if not self.record:
            return None
        try:
            return world.encode_state()
        except OSError:
            return None  # a world model is never worth failing a syscall over

    def foresee(self, name: str, args: dict) -> dict | None:
        """What does the world model think this syscall will do?"""
        wm = self.world_model
        if wm is None or not wm.is_trained:
            return None
        state = self.observe()
        if state is None:
            return None
        return wm.explain(state, world.encode_action(name, args))

    def emit(self, kind: str, **data) -> None:
        if self._emit:
            self._emit(kind, data)

    def allow(self, name: str, args: dict) -> bool:
        if self.autonomy == "full":
            return True
        if self.autonomy == "readonly":
            return False
        return self._confirm(name, args) if self._confirm else True


class Kernel:
    def __init__(self, client, ctx: Context):
        self.client = client
        self.ctx = ctx
        self.messages: list[dict] = [{"role": "system", "content": self._boot_context()}]
        self.log_path = paths.LOGS / f"session-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl"

    def _boot_context(self) -> str:
        """System prompt plus a snapshot of what this OS currently is."""
        apps = self.ctx.registry.all()
        installed = (
            "\n".join(f"- {a.name}: {a.description}" for a in apps)
            if apps
            else "- (none yet -- the userland is empty)"
        )
        notes = self.ctx.memory.all()
        known = (
            "\n".join(f"- {n['title']}" for n in notes[:40])
            if notes
            else "- (nothing remembered yet)"
        )
        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"CURRENTLY INSTALLED APPS\n{installed}\n\n"
            f"MEMORY INDEX (titles only; use mem_search to read one)\n{known}\n\n"
            f"Root directory: {paths.HOME}\nBrain: {self.ctx.model}\n"
            f"Date: {time.strftime('%Y-%m-%d')}"
        )

    def refresh_boot_context(self) -> None:
        """Rebuild the system message after the OS changes shape."""
        self.messages[0] = {"role": "system", "content": self._boot_context()}

    def _log(self, entry: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), **entry}) + "\n")
        except OSError:
            pass  # logging must never break the OS

    def turn(self, user_input: str) -> str:
        """Run one user turn to completion, including any syscall rounds."""
        self.messages.append({"role": "user", "content": user_input})
        self._log({"role": "user", "content": user_input})

        built = False
        for step in range(MAX_STEPS):
            self.ctx.emit("thinking")
            reply = self.client.chat(
                self.messages,
                tools=syscalls.tools(),
                on_delta=lambda t: self.ctx.emit("token", text=t),
            )

            msg = {"role": "assistant", "content": reply["content"] or None}
            if reply["tool_calls"]:
                msg["tool_calls"] = reply["tool_calls"]
            self.messages.append(msg)
            self._log({"role": "assistant", **reply})

            if reply["usage"]:
                self.ctx.emit("usage", **reply["usage"])

            if not reply["tool_calls"]:
                if built:
                    self.refresh_boot_context()
                return reply["content"]

            for call in reply["tool_calls"]:
                name = call["function"]["name"]
                try:
                    args, repaired = parse_args(call["function"]["arguments"])
                    if repaired is not None:
                        # Store the repair, so the next request does not replay
                        # invalid JSON and get the whole conversation rejected.
                        call["function"]["arguments"] = repaired
                        self.ctx.emit("repaired", name=name)
                except json.JSONDecodeError as e:
                    args, out = {}, f"error: arguments were not valid JSON ({e})"
                    # Unsalvageable: replace it so the malformed JSON is not
                    # replayed on the next request. The model sees the error in
                    # the tool result and can retry properly.
                    call["function"]["arguments"] = "{}"
                else:
                    self.ctx.emit("syscall", name=name, args=args)
                    before = self.ctx.observe()
                    out = syscalls.dispatch(name, args, self.ctx)
                    after = self.ctx.observe()
                    if before is not None and after is not None:
                        # Training data for the world model, gathered simply by
                        # using the OS.
                        self._log({
                            "role": "transition",
                            "action": name,
                            "s": before,
                            "a": world.encode_action(name, args),
                            "s2": after,
                        })

                if name == "app_build" and out.startswith("installed"):
                    built = True

                self.ctx.emit("result", name=name, output=out)
                self._log({"role": "tool", "name": name, "args": args, "output": out})
                self.messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": out}
                )

        return f"(stopped after {MAX_STEPS} syscall rounds -- the task may be looping)"
