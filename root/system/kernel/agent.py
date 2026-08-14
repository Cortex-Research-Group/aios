"""The kernel: perceive, plan, syscall, observe, repeat.

Holds the conversation, drives the model, dispatches syscalls and enforces the
permission gate. Deliberately UI-agnostic -- it emits events through a callback
so the TUI today and any other shell later are just clients.
"""

import json
import os
import time
from pathlib import Path

from . import paths, syscalls

MAX_STEPS = 25  # a runaway tool loop should stall, not bill you forever

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

    def __init__(self, registry, memory, secrets, model, autonomy="ask", confirm=None, emit=None):
        self.registry = registry
        self.memory = memory
        self.secrets = secrets
        self.model = model
        self.autonomy = autonomy  # 'ask' | 'full' | 'readonly'
        self._confirm = confirm
        self._emit = emit

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
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    args, out = {}, f"error: arguments were not valid JSON ({e})"
                else:
                    self.ctx.emit("syscall", name=name, args=args)
                    out = syscalls.dispatch(name, args, self.ctx)

                if name == "app_build" and out.startswith("installed"):
                    built = True

                self.ctx.emit("result", name=name, output=out)
                self._log({"role": "tool", "name": name, "args": args, "output": out})
                self.messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": out}
                )

        return f"(stopped after {MAX_STEPS} syscall rounds -- the task may be looping)"
