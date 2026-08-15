# aiOS — session handoff

Written so a fresh session can resume with no prior context. Read this, then
`README.md` for the user-facing description.

**State:** working, 123 tests green, everything committed (HEAD `7e1e022`).
4,853 lines. Nothing in flight, nothing half-finished.

---

## What this is

An agent OS whose userland it writes itself. You ask for a capability it lacks;
it writes a Python program, installs it into `apps/`, and that program is a
permanent command on every future boot. Two halves:

- **Token-space half** — an LLM agent loop over 12 syscalls (the conventional part).
- **Embedding-space half** — a JEPA-style world model that predicts what a syscall
  will do *before* it runs, so the permission gate can warn about irreversible
  actions (`kernel/world.py`).

The `root/` tree **is** the OS. It never writes outside itself, so it can be
copied to a stick, a VM, a VPS or a Modal Volume unchanged.

**Hard constraint: zero pip dependencies.** Kernel and generated apps are stdlib
only. `python3` is the entire requirement. `kernel/sandbox.py` imports `modal`
lazily *only* when that backend is selected — do not break this.

---

## How to run it

```sh
./root/aios                                     # local; prompts for OpenRouter key + passphrase
modal run build/modal/aios_modal.py             # on Modal, interactive
modal run build/modal/aios_modal.py::status     # what the volume holds
modal run build/modal/aios_modal.py::selftest   # prove sandbox capability enforcement
modal run build/modal/aios_modal.py::smoke      # one real agent turn, no TTY needed
modal run build/modal/aios_modal.py::local_brain  # GPU, self-hosted Qwen2.5-7B
python3 -m unittest discover -s tests           # 123 tests
```

**The interactive shell needs a TTY, which an agent session cannot drive.** Use
`::smoke` to exercise the real agent loop non-interactively — that is how every
live-API claim in this repo was verified.

---

## Environment facts (specific to this user)

- Modal workspace `nftmansa`, authenticated, CLI 1.5.3.
- Modal secret **`openrouter-api-key`** exposes `OPENROUTER_API_KEY`.
- Volumes: `aios-root` (the OS), `aios-app-data` (sandboxed app scratch),
  `aios-hf-cache` (model weights).
- The OpenRouter key has a **$1 limit** — this is why `max_tokens` is capped (see below).
- Host is a 2017 Intel MacBook, 2 cores / 8 GB, macOS 13. **qemu cannot be built
  here** (Homebrew deprioritises Intel+Ventura; the source build fails). The local
  VM target is written but *untested* — do not claim otherwise.
- The Mac is pre-T2, so a bootable USB would not hit Secure Boot problems.

---

## Architecture

```
root/system/kernel/
  paths.py      the OS root + the jail check (AIOS_HOME binds at import time)
  vault.py      ChaCha20 + scrypt secret storage, verified against RFC 8439 vectors
  llm.py        any OpenAI-compatible endpoint (OpenRouter by default)
  syscalls.py   THE SECURITY BOUNDARY. 12 primitives, all model access goes here
  apps.py       userland registry + static validation of generated code
  memory.py     markdown facts + TF-IDF search
  sandbox.py    optional Modal-confined app execution (lazy import)
  world.py      JEPA-style predictor: what will this syscall do?
  agent.py      the loop; also JSON repair and transition journaling
root/system/shell/tui.py    terminal shell, permission prompts, slash commands
build/          provision.sh (VPS/VM), deploy.sh (over SSH), modal/, vm/
```

---

## Verified vs unverified — be precise about this

**Verified by execution:**
- Vault: RFC 8439 cipher vectors byte-exact; sealed file provably contains no key; tampering detected.
- Filesystem jail: `../` traversal, absolute paths, and symlink escapes all denied.
- Secret isolation: an app receives only the keys it declared.
- Sandbox capability enforcement **on Modal**: same program, different manifest —
  `NETWORK_REACHABLE` vs `NETWORK_BLOCKED`. Not asserted, measured.
- Live OpenRouter turn: built and ran a `clock` app end to end (~$0.095, 40s).
- Local GPU brain: Qwen2.5-7B via vLLM built and self-corrected a `dice` app in 20.6s.
- World model: beats do-nothing baseline; flags deletions destructive; read-only
  syscalls predict exactly 0.0000.
- Modal volume persistence across separate invocations.

**Written but NOT verified:**
- **Local VM** (`build/vm/run-vm.sh`) — blocked on qemu. Syntax-checked only.
- **VPS deploy** (`build/deploy.sh`) — no box to try it on. The provisioner logic
  was tested locally against a temp prefix.
- **USB bare-metal boot** — never attempted.

---

## Hard-won lessons — do not reintroduce these

### World model (`world.py`) — four bugs found only by measuring
1. **Never use `log1p` for the state encoding.** Under a log, "+1 file" moves the
   embedding differently depending on how full the world is, making an action's
   effect *multiplicative* in the state — which an additive linear predictor
   cannot represent. Measured: could not distinguish `fs_write` from `fs_read`.
   Linear capped counts fixed it. There is a regression test.
2. **Every syscall needs its own identity slot.** Hashing 12 names into 12 buckets
   collided by the birthday bound (`fs_write`/`mem_write`/`app_build` shared one).
   `KNOWN_SYSCALLS` + a drift-guard test now prevent this.
3. **Never pool hashed argument tokens across syscalls.** `fs_read /data/x` and
   `proc_run rm /data/x` share tokens, so a harmless read inherited the deletion's
   predicted damage. Argument features are now scoped per-syscall.
4. **The predictor uses the action only, not the state.** Including the state gave
   it a drift term that fired regardless of action, so read-only calls could never
   predict zero. State is still recorded in every transition, so a nonlinear
   predictor can use it later without re-gathering data.

**Training data must contain destruction.** A model trained only on journalled
usage predicted `rm -rf /apps` would *increase* the app count — it had never seen
a deletion. `world.bootstrap()` practises in a throwaway root. It must also
include *harmless* `proc_run` commands, or the model learns `proc_run` itself
means destruction and flags `ls`.

### Modal
- **Never resolve local paths at module scope.** Modal re-imports the module inside
  the container where `__file__` is `/root/aios_modal.py`; `parents[2]` raised
  `IndexError` and crash-looped every container. Guard with `modal.is_local()`.
- `Sandbox.mkdir()` / `Sandbox.open()` are **retired** (ConflictError). The executor
  passes code via `python -c` and needs no sandbox filesystem at all.
- `modal run file::func` **streams stdout but discards return values.** Print results.
- Volume changes need an explicit `.commit()` or a session's work is lost.

### LLM
- **Always send `max_tokens`.** Unset, OpenRouter reserves the model's entire
  context against your credit balance and a limited key 402s before generating a
  single token. Capped at 8192.
- **Weaker models emit invalid JSON tool arguments** (raw newlines inside strings,
  most often on `app_build` because it carries a whole program). `agent.repair_json()`
  salvages it; unsalvageable arguments are replaced with `{}` so the malformed JSON
  is never replayed — otherwise one bad call poisons the whole conversation with a 400.
- `base_url` is resolved **at call time**, not as a default argument; a default
  froze `llm.API` at import and made it unoverridable (and silently sent the
  "offline" test suite to the real network).

### Shell / deploy
- The `aios` launcher **must resolve symlinks itself** (BSD `readlink` has no `-f`).
  Provisioning puts a link at `/usr/local/bin/aios`, and `dirname $0` there points
  at the link, not the install. This broke every VM/VPS install.
- Never pipe long-running commands through `tail` — it buffers everything until
  exit, so a hang is invisible, and it masks non-zero exit codes. This cost a
  30-minute undiagnosed hang and a falsely-reported successful qemu install.

---

## Known limitations

- **Local `/sandbox off` is disclosure, not a jail.** An app with `proc` can do
  whatever your user account can. Real confinement requires `/sandbox modal`.
- **`app_build` now executes what it installs** (the smoke run). Approving a build
  implies approving one run. Deliberate, disclosed, but it is a widening.
- **Offline: the agent cannot think.** OpenRouter is remote. Installed apps,
  memory, `fs_*` and the shell all keep working; failures are legible, not tracebacks.
- **`proc_run: ls` predicts a small spurious −0.007** — below the destructive
  threshold so it is not escalated, but it is not zero either.
- World model reports `under-trained` below 40 transitions rather than bluffing.
- Qwen2.5-7B local brain works but is meaningfully worse than opus-5 at writing
  correct programs; it needed the build-time smoke check to self-correct.

---

## Sensible next steps

1. **VPS deploy** — the only untested path that is cheap to verify and gives the
   always-on box Modal deliberately does not.
2. **Scheduling** — the OS can build a tool but cannot run it every 5 minutes.
   Probably the biggest usability gap.
3. **Bare-metal USB** — the original goal; the Mac being pre-T2 helps.
4. **Nonlinear world-model predictor** — the transitions already record state,
   so an MLP could exploit it. Only `WorldModel.fit()` changes.
5. **Learned encoder** — would make the JEPA half faithful to the paper (needs an
   EMA target encoder + stop-gradient to avoid representation collapse).

---

## Working agreements from this session

- Verify claims by running them. Say "verified" only with output to show, and mark
  untested things untested.
- Report failures verbatim, including one's own mistakes.
- Prefer measurement over argument when a design choice is in question — every
  world-model fix above came from an experiment, and several contradicted a
  confident prediction.
