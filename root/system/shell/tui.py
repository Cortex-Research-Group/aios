"""The terminal shell.

A plain stdin REPL with ANSI rendering rather than curses: it works on a bare
Linux console, over serial, and over SSH without a terminfo database, and
scrollback keeps working. That matters when this is booting a VM or a stick.
"""

import getpass
import json
import os
import sys
import time
from pathlib import Path

from kernel import agent, apps, llm, memory, paths, sandbox, syscalls, vault, world

try:
    import readline  # noqa: F401  -- line editing and history, if available
except ImportError:
    readline = None

# --- rendering ----------------------------------------------------------------

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def dim(t):
    return c("2", t)


def bold(t):
    return c("1", t)


def cyan(t):
    return c("36", t)


def green(t):
    return c("32", t)


def yellow(t):
    return c("33", t)


def red(t):
    return c("31", t)


BANNER = r"""
       _  ___  ___
  __ _(_)/ _ \/ __|   an operating system that
 / _` | | (_) \__ \   writes its own userland
 \__,_|_|\___/|___/
"""


class Shell:
    def __init__(self):
        paths.ensure()
        self.config = self._load_config()
        self.registry = apps.Registry(executor=self._build_executor())
        self.memory = memory.Memory()
        self.world = world.WorldModel.load()
        self.secrets: dict = {}
        self.kernel = None
        self.hosted = False
        self._streaming = False
        self._session_allow_all = False

    # --- config --------------------------------------------------------------

    def _load_config(self) -> dict:
        config = {"model": llm.DEFAULT_MODEL, "autonomy": "ask", "sandbox": "off"}
        if paths.CONFIG.exists():
            try:
                config.update(json.loads(paths.CONFIG.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        # A hosted deployment picks the sandbox backend; it should not depend on
        # a config file that lives in a volume the deployment may not have yet.
        if os.environ.get("AIOS_SANDBOX"):
            config["sandbox"] = os.environ["AIOS_SANDBOX"]
        if os.environ.get("AIOS_SANDBOX_VOLUME"):
            config["sandbox_volume"] = os.environ["AIOS_SANDBOX_VOLUME"]
        return config

    def _build_executor(self):
        """The app execution backend. Falls back to subprocesses if unavailable."""
        try:
            return sandbox.from_config(self.config)
        except sandbox.SandboxError as e:
            print(red(f"  sandbox unavailable: {e}"))
            print(dim("  falling back to local subprocesses"))
            self.config["sandbox"] = "off"
            return None

    def _save_config(self) -> None:
        paths.CONFIG.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    # --- boot ----------------------------------------------------------------

    def boot(self) -> bool:
        print(cyan(BANNER))
        print(dim(f"  root   {paths.HOME}"))

        # Hosted deployments (Modal secrets, VPS env, CI) inject the key directly.
        # There is nobody at a keyboard to type a passphrase, so the vault is
        # bypassed and the environment becomes the secret pool. Apps still only
        # receive the keys they declared.
        env_key = os.environ.get("OPENROUTER_API_KEY")
        if env_key:
            self.secrets = dict(os.environ)
            self.hosted = True
        else:
            v = vault.Vault()
            if not v.exists():
                if not self._first_run(v):
                    return False
            else:
                if not self._unlock(v):
                    return False

        model = self.config["model"]
        self.client = llm.OpenRouter(self.secrets["OPENROUTER_API_KEY"], model)

        ctx = agent.Context(
            registry=self.registry,
            memory=self.memory,
            secrets=self.secrets,
            model=model,
            autonomy=self.config["autonomy"],
            confirm=self._confirm,
            emit=self._on_event,
            world_model=self.world,
        )
        self.ctx = ctx
        self.kernel = agent.Kernel(self.client, ctx)

        n_apps = len(self.registry.all())
        n_mem = len(self.memory.all())
        print(dim(f"  brain  {model}" + ("   key from environment" if self.hosted else "")))
        confine = self.registry.executor.name if self.registry.executor else "subprocess"
        print(dim(f"  apps   {n_apps}    memory {n_mem} notes    autonomy {self.config['autonomy']}    apps run in: {confine}"))
        if self.world.is_trained:
            note = "" if self.world.is_reliable else "  (under-trained)"
            print(dim(f"  world  predictor trained on {self.world.trained_on} transitions{note}"))
        print(dim("  type /help for commands, or just say what you want\n"))
        return True

    def _first_run(self, v: vault.Vault) -> bool:
        print(bold("\n  First boot. This OS needs a brain and a vault.\n"))
        print("  Get an OpenRouter key at " + cyan("https://openrouter.ai/keys"))
        key = getpass.getpass("  OpenRouter API key (hidden): ").strip()
        if not key:
            print(red("  no key, no OS. aborting."))
            return False

        print(dim("\n  The key is sealed with a passphrase you type at every boot."))
        print(dim("  Lose it and the vault is unrecoverable -- that is the point.\n"))
        while True:
            p1 = getpass.getpass("  New vault passphrase: ")
            if len(p1) < 6:
                print(red("  too short, use at least 6 characters"))
                continue
            p2 = getpass.getpass("  Confirm passphrase: ")
            if p1 != p2:
                print(red("  passphrases differ, try again"))
                continue
            break

        print(dim("\n  verifying key..."))
        try:
            info = llm.OpenRouter(key, self.config["model"]).check()
        except llm.LLMError as e:
            print(red(f"  {e}"))
            return False

        limit = info.get("limit")
        print(green(f"  key ok") + dim(f"  (credit limit: {limit if limit is not None else 'unlimited'})"))
        v.seal({"OPENROUTER_API_KEY": key}, p1)
        self.secrets = {"OPENROUTER_API_KEY": key}
        print(green(f"  vault sealed at {paths.rel(v.path)}\n"))
        return True

    def _unlock(self, v: vault.Vault) -> bool:
        for attempt in range(3):
            try:
                self.secrets = v.unseal(getpass.getpass("  vault passphrase: "))
                return True
            except vault.BadPassphrase:
                print(red(f"  wrong passphrase ({2 - attempt} attempts left)"))
            except vault.VaultError as e:
                print(red(f"  {e}"))
                return False
            except (KeyboardInterrupt, EOFError):
                return False
        return False

    # --- kernel events -------------------------------------------------------

    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "token":
            self._streaming = True
            sys.stdout.write(data["text"])
            sys.stdout.flush()
        elif kind == "syscall":
            self._break_stream()
            print(dim(f"  · {data['name']}({_brief(data['args'])})"))
        elif kind == "result":
            out = data["output"]
            first = out.strip().split("\n")[0][:100] if out.strip() else ""
            mark = red("✗") if out.startswith(("error", "denied")) else green("✓")
            print(dim(f"    {mark} {first}"))
        elif kind == "usage":
            cost = data.get("cost")
            if cost:
                print(dim(f"    ${cost:.4f}"))

    def _break_stream(self) -> None:
        if self._streaming:
            print()
            self._streaming = False

    def _confirm(self, name: str, args: dict) -> bool:
        if self._session_allow_all:
            return True
        self._break_stream()
        print(yellow(f"\n  {name} wants to run:"))
        foresight = self.ctx.foresee(name, args)
        if foresight:
            caveat = "" if foresight["reliable"] else " (under-trained -- a hint, not a verdict)"
            if foresight["destructive"]:
                print(red(f"    ⚠ predicted to REMOVE things ({foresight['magnitude']} impact){caveat}"))
            else:
                print(dim(f"    predicted impact: {foresight['magnitude']}, {foresight['direction']}{caveat}"))
            changes = ", ".join(f"{k} {v:+.3f}" for k, v in foresight["predicted_changes"].items())
            if changes:
                print(dim(f"    predicted change: {changes}"))
        for k, v in args.items():
            text = str(v)
            if len(text) > 400:
                text = text[:400] + f" … (+{len(text) - 400} chars)"
            print(dim(f"    {k}: {text}"))
        try:
            answer = input(bold("  allow? [y/N/a=always] ")).strip().lower()
        except (KeyboardInterrupt, EOFError):
            return False
        if answer == "a":
            self._session_allow_all = True
            print(dim("  allowing everything for this session"))
            return True
        return answer in ("y", "yes")

    # --- repl ----------------------------------------------------------------

    def run(self) -> int:
        if not self.boot():
            return 1
        while True:
            try:
                line = input(bold(cyan("aios") + " ∎ ")).strip()
            except (KeyboardInterrupt, EOFError):
                print("\n" + dim("  halt"))
                return 0
            if not line:
                continue
            try:
                if self._dispatch(line) is False:
                    return 0
            except KeyboardInterrupt:
                self._break_stream()
                print(dim("\n  interrupted"))
            except llm.LLMError as e:
                self._break_stream()
                print(red(f"  {e}"))

    def _dispatch(self, line: str):
        if line.startswith("/"):
            return self._command(line)
        if line.startswith("!"):
            os.system(line[1:])
            return None

        # A bare installed app name runs the app -- it is a real command.
        word, _, rest = line.partition(" ")
        if self.registry.get(word):
            self._run_app(word, rest.split() if rest else [])
            return None

        self._streaming = False
        self.kernel.turn(line)
        self._break_stream()
        print()
        return None

    def _run_app(self, name: str, args: list) -> None:
        app = self.registry.get(name)
        t0 = time.time()
        r = self.registry.run(name, args, secrets=self.secrets)
        if r["stdout"].strip():
            print(r["stdout"].rstrip())
        if r["stderr"].strip():
            print(red(r["stderr"].rstrip()))
        print(dim(f"  [{name} exit {r['code']} in {time.time() - t0:.1f}s]\n"))

    # --- slash commands ------------------------------------------------------

    def _command(self, line: str):
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()

        if cmd in ("exit", "quit", "halt"):
            print(dim("  halt"))
            return False

        elif cmd == "help":
            print(f"""
  {bold('talk')}              just type -- the OS builds what you ask for
  {bold('<app> [args]')}      run an installed app
  {bold('!<command>')}        run a shell command on the host

  {bold('/apps')}             list installed apps
  {bold('/rm <app>')}         uninstall an app
  {bold('/spec <app>')}       show the prompt an app was built from
  {bold('/mem [query]')}      list or search memory
  {bold('/forget <title>')}   delete a memory
  {bold('/model [id]')}       show or change the brain
  {bold('/models [filter]')}  browse available models
  {bold('/autonomy [mode]')}  ask | full | readonly
  {bold('/sandbox [mode]')}    off | modal -- where generated apps run
  {bold('/world [bootstrap|train]')}  predict a syscall's effect before running it
  {bold('/syscalls')}         list kernel syscalls
  {bold('/reset')}            clear the conversation, keep apps and memory
  {bold('/exit')}             halt
""")

        elif cmd == "apps":
            apps_ = self.registry.all()
            if not apps_:
                print(dim("  no apps yet -- ask for something and the OS will build it\n"))
            else:
                for a in apps_:
                    caps = f" [{', '.join(a.caps)}]" if a.caps else ""
                    print(f"  {bold(a.name):<24} {a.description}{dim(caps)}")
                print()

        elif cmd == "rm":
            print(green(f"  removed {arg}\n") if self.registry.remove(arg) else red(f"  no such app: {arg}\n"))
            self.kernel.refresh_boot_context()

        elif cmd == "spec":
            app = self.registry.get(arg)
            print(f"\n{app.spec}\n" if app else red(f"  no such app: {arg}\n"))

        elif cmd == "mem":
            notes = self.memory.search(arg) if arg else self.memory.all()
            if not notes:
                print(dim("  nothing remembered\n"))
            for n in notes:
                print(f"  {bold(n['title'])} {dim(n['updated'])}")
                print(f"    {n['content'][:200]}")
            print()

        elif cmd == "forget":
            print(green(f"  forgot {arg}\n") if self.memory.forget(arg) else red("  no such memory\n"))

        elif cmd == "model":
            if arg:
                self.config["model"] = arg
                self._save_config()
                self.client.model = arg
                self.ctx.model = arg
                print(green(f"  brain is now {arg}\n"))
            else:
                print(f"  {self.config['model']}\n")

        elif cmd == "models":
            try:
                found = self.client.models()
            except llm.LLMError as e:
                print(red(f"  {e}\n"))
                return None
            rows = [m for m in found if not arg or arg.lower() in m["id"].lower()]
            for m in sorted(rows, key=lambda m: m["id"])[:60]:
                price = m.get("pricing", {}).get("prompt", "?")
                try:
                    per_m = f"${float(price) * 1_000_000:.2f}/M"
                except (TypeError, ValueError):
                    per_m = ""
                print(f"  {m['id']:<50} {dim(per_m)}")
            print(dim(f"  {len(rows)} models\n"))

        elif cmd == "autonomy":
            if arg in ("ask", "full", "readonly"):
                self.config["autonomy"] = arg
                self.ctx.autonomy = arg
                self._save_config()
                print(green(f"  autonomy: {arg}\n"))
            else:
                print(f"  {self.config['autonomy']} " + dim("(ask | full | readonly)\n"))

        elif cmd == "sandbox":
            if arg in ("off", "modal"):
                self.config["sandbox"] = arg
                self._save_config()
                self.registry.executor = self._build_executor()
                where = self.registry.executor.name if self.registry.executor else "local subprocesses"
                print(green(f"  apps now run in: {where}\n"))
            else:
                where = self.registry.executor.name if self.registry.executor else "subprocess"
                print(f"  {where} " + dim("(off | modal)\n"))

        elif cmd == "world":
            if arg == "train":
                samples = world.load_transitions()
                if not samples:
                    print(dim("  no transitions journaled yet -- use the OS a while first\n"))
                    return None
                stats = self.world.fit(samples)
                self.world.save()
                self.ctx.world_model = self.world
                verdict = ("learned something" if stats["vs_static"] < 0.9
                           else "no better than assuming nothing ever changes")
                print(green(f"  trained on {stats['samples']} transitions"))
                print(dim(f"  rmse {stats['rmse']:.4f}   vs do-nothing baseline {stats['vs_static']:.3f} -- {verdict}"))
                if not self.world.is_reliable:
                    print(yellow(f"  under {world.MIN_SAMPLES} samples: treat predictions as a hint\n"))
                else:
                    print()
            elif arg == "bootstrap":
                print(dim("  practising in a throwaway root (creation and deletion)..."))
                samples = world.bootstrap()
                samples += world.load_transitions()  # plus whatever you actually did
                stats = self.world.fit(samples)
                self.world.save()
                self.ctx.world_model = self.world
                print(green(f"  trained on {stats['samples']} transitions"))
                print(dim(f"  rmse {stats['rmse']:.4f}   vs do-nothing baseline {stats['vs_static']:.3f}\n"))

            elif arg == "forget":
                self.world = world.WorldModel()
                self.world.save()
                self.ctx.world_model = self.world
                print(dim("  world model discarded\n"))
            else:
                pending = len(world.load_transitions())
                if self.world.is_trained:
                    print(f"  trained on {bold(str(self.world.trained_on))} transitions "
                          + dim(f"({'reliable' if self.world.is_reliable else 'under-trained'})"))
                else:
                    print(dim("  no world model yet"))
                print(dim(f"  {pending} transitions journaled"))
                print(dim("  /world bootstrap  learn by practising, deletions included"))
                print(dim("  /world train      learn from journalled usage only"))
                print(dim("  /world forget     discard the model\n"))

        elif cmd == "syscalls":
            for s in syscalls.REGISTRY.values():
                mark = yellow(" mutating") if s["mutating"] else ""
                print(f"  {bold(s['name']):<24}{mark}")
                print(dim(f"    {s['description'][:110]}"))
            print()

        elif cmd == "reset":
            self.kernel = agent.Kernel(self.client, self.ctx)
            print(dim("  conversation cleared\n"))

        else:
            print(red(f"  unknown command: /{cmd}") + dim(" -- try /help\n"))

        return None


def _brief(args: dict) -> str:
    """Compact one-line rendering of syscall arguments."""
    parts = []
    for k, v in args.items():
        text = str(v).replace("\n", " ")
        if len(text) > 48:
            text = text[:48] + "…"
        parts.append(f"{k}={text}")
    return ", ".join(parts)


def main() -> int:
    return Shell().run()
