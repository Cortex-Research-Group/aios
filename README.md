# aiOS

An operating system whose userland is written by the operating system, on demand.

Ask it for a capability it does not have. It writes the program, installs it, and
that program is a permanent command — on the next boot, on another machine, on a
stick in your pocket. The OS grows from your prompts and keeps what it grows.

```
aios ∎ track the SOL price and warn me if it moves 5% in an hour

  · app_build(name=solwatch, caps=['net','fs'])
    ✓ installed app 'solwatch'
  · app_run(name=solwatch)
    ✓ SOL $203.11  1h +0.4%  — quiet

installed as `solwatch`. run it any time, or ask me to schedule it.

aios ∎ solwatch
  SOL $203.11  1h +0.4%  — quiet
  [solwatch exit 0 in 0.4s]
```

## Quickstart

You need an [OpenRouter key](https://openrouter.ai/keys) and Python 3.11+.

```sh
./root/aios
```

First boot asks for the key and a passphrase to seal it with. That is the whole
setup. `/help` lists the commands.

## What it runs on

The `root/` tree *is* the OS. Copy it anywhere; it never writes outside itself.

| Target | How | Why |
|---|---|---|
| **This machine** | `./root/aios` | Fastest way to use it. |
| **Modal** | `modal run build/modal/aios_modal.py` | Persistent state on a Volume, and **real confinement** for generated apps. |
| **VPS** | `./build/deploy.sh root@your-host` | Always-on. Your OS lives at an IP and keeps running when the laptop sleeps. |
| **Local VM** | `./build/vm/run-vm.sh -d` then `./build/deploy.sh` | Disposable Alpine boot. *Untested — needs a qemu that builds on your host.* |
| **USB stick** | copy `root/` to the drive, run `./aios` | Portable. Bare-metal boot is the next milestone. |

### Modal

```sh
modal secret create openrouter-api-key OPENROUTER_API_KEY=sk-or-...   # once
modal run build/modal/aios_modal.py            # interactive session
modal run build/modal/aios_modal.py::status    # what the volume holds
modal run build/modal/aios_modal.py::selftest  # prove caps are enforced
```

State lives on the `aios-root` Volume, so the OS you grow in one session is the
one you return to. The key comes from a Modal Secret, so there is no passphrase
prompt — aiOS bypasses the vault whenever `OPENROUTER_API_KEY` is already in the
environment.

The tradeoff is honest: Modal is serverless, so this is **not** an always-on box
at an IP. You get a container when you ask and it stops when you leave. What you
get in return is free compute, persistent state, and the sandbox below.

### Local VM

```sh
brew install qemu
./build/vm/run-vm.sh -d                                    # boot Alpine, ~15s
./build/deploy.sh root@localhost "-i build/vm/.cache/id_ed25519 -p 2222"
./build/vm/run-vm.sh ssh -t aios                           # log into the OS
```

`run-vm.sh reset` throws the VM away. The disk is a copy-on-write overlay on the
downloaded image, so resetting costs nothing and you can break things freely.

### VPS

Any Alpine or Debian box with SSH:

```sh
./build/deploy.sh root@203.0.113.9
ssh root@203.0.113.9 -t aios
```

Re-run `deploy.sh` any time to push code changes — apps, memory and the vault on
the target are left alone. On the physical console the machine boots *into* aiOS;
SSH gives you a normal shell, so a wedged agent never locks you out of your own box.

## Architecture

```
root/                     the entire OS — this tree is the deliverable
├── aios                  launcher; resolves the root from its own location
├── system/
│   ├── kernel/
│   │   ├── agent.py      the loop: perceive → plan → syscall → observe
│   │   ├── syscalls.py   every primitive the model may invoke. the security boundary.
│   │   ├── apps.py       userland registry — install, version, run
│   │   ├── memory.py     markdown facts + TF-IDF search
│   │   ├── sandbox.py    optional confined execution (Modal), lazily imported
│   │   ├── world.py      JEPA-style predictor: what will this syscall do?
│   │   ├── vault.py      ChaCha20 + scrypt secret storage
│   │   ├── llm.py        OpenRouter client (streaming, tool calls)
│   │   └── paths.py      the root, and the jail check
│   └── shell/tui.py      terminal shell
├── apps/                 ← the OS writes this
├── memory/               ← and this
├── vault/keys.enc        sealed secrets
└── logs/                 every syscall, journaled
```

**Zero pip dependencies.** Standard library only, kernel and generated apps alike.
Every dependency is a thing that can fail to install on a machine with no network,
and this OS is supposed to boot on a stick. `python3` is the entire requirement.

### Apps

An app is a directory the kernel wrote:

```
apps/solwatch/
├── manifest.json   name, description, capabilities, model that built it, version
├── spec.md         the prompt it was built from — the real source
├── main.py         the generated program
└── data/           its own writable scratch space
```

`spec.md` is why this is more than codegen. The userland is *regenerable*: point a
better model at the same specs and the OS rebuilds itself.

### Security model

The agent writes code nobody reviewed, so the boundaries are explicit:

- **Filesystem jail** — every `fs_*` syscall resolves inside `root/` or is denied.
  Traversal, absolute paths and symlink escapes are all tested (`tests/test_kernel.py`).
- **Permission gate** — mutating syscalls (`fs_write`, `proc_run`, `app_build`,
  `app_run`) prompt before running, showing exactly what was requested.
  `/autonomy full` disables prompting; `/autonomy readonly` denies everything.
- **Capability manifests** — an app declares `net`, `fs`, `proc`, `secrets` and you
  see them before it installs.
- **Secret isolation** — an app receives only the vault keys it declared. One that
  never asked for a key cannot read one. Tested.
- **Vault** — scrypt-derived key, ChaCha20, encrypt-then-MAC with HMAC-SHA256.
  Verified against the RFC 8439 test vectors in `tests/test_vault.py`.

**Where apps actually run** decides whether that manifest is enforcement or just
paperwork:

| `/sandbox` | Backend | What a capability means |
|---|---|---|
| `off` (default) | local subprocess | Disclosure + key isolation. An app with `proc` can do whatever your user account can. |
| `modal` | `modal.Sandbox` | Enforcement. No `net` → `block_network=True` and the container has no route out. No `fs` → no volume mounted. Plus hard cpu/memory/timeout ceilings. |

`kernel/sandbox.py` imports `modal` lazily and only when selected, so the stick,
the VPS and the VM keep their zero-dependency guarantee.

This is verified, not asserted. `modal run build/modal/aios_modal.py::selftest`
installs the *same* network-probing program twice, differing only in its manifest,
and runs both through the sandbox:

```
app declaring   net : NETWORK_REACHABLE
app declaring  none : NETWORK_BLOCKED URLError
RESULT: capability enforcement is REAL
```

So: run adventurous prompts with `/sandbox modal`, not on your laptop. The local
default is fine for code you'd have written yourself, and honest about being a
disclosure mechanism rather than a jail.

## The world model (the JEPA half)

The kernel reasons in token space: write a syscall, run it, read the result. That
is fine when a syscall is cheap and reversible, and exactly wrong when it is not —
you cannot un-run `rm -rf /apps` and read the outcome.

So `kernel/world.py` learns to answer *"what would that do?"* without doing it,
following the Joint Embedding Predictive Architecture idea: encode the state,
predict the **embedding** of the next state from the current embedding plus the
action. It never reconstructs the literal next state — only the representation.

    predict(encode(state), encode(action))  ≈  encode(next_state)

Training data costs nothing: every syscall already journals a
`(state, action, next_state)` triple. `/world train` fits on them.

```
proposed syscall                     magnitude   direction   destructive
fs_list                              negligible  none        -
proc_run(ls -la)                     ordinary    removes     -
fs_write                             ordinary    creates     -
app_build                            extreme     creates     -
proc_run(rm data/f1.txt)             ordinary    removes     *** YES ***
proc_run(rm -rf apps)                extreme     removes     *** YES ***
```

Magnitude and direction are reported separately on purpose: installing an app
moves the world further than deleting one note, but only one of them destroys
your work.

### Bootstrapping

Journalled usage only covers what you happened to do, and nobody spends a session
deleting things — so a model trained purely on real usage has never seen
destruction. Measured, such a model predicted `rm -rf /apps` would **increase**
the app count. `/world bootstrap` fixes that: the OS practises in a throwaway
root, destruction included, and records the same triples. Nothing touches the
real root.

### What it is and is not

- The encoder is **fixed** (hand-designed features over the filesystem), not
  learned. Real JEPA learns it jointly with an EMA target encoder and a
  stop-gradient to avoid representation collapse. A fixed encoder cannot
  collapse, which removes both the hardest part and the part that makes JEPA
  interesting on raw perceptual data.
- The predictor is **ridge regression on the residual**, closed-form, and uses
  the action only. Including the state measured worse on every count: it gave
  the predictor a drift term that fired regardless of action, so a read scored
  as disturbing as a write. The state is still recorded in every transition, so
  a nonlinear predictor can use it later without re-gathering data.
- With few transitions it says so (`under-trained`) rather than emitting
  confident nonsense.

## Syscalls

| | |
|---|---|
| `fs_read` `fs_write` `fs_list` | filesystem, jailed to the root |
| `proc_run` | shell command |
| `net_fetch` | HTTP(S), HTML reduced to text |
| `web_search` | DuckDuckGo, no API key needed |
| `mem_write` `mem_search` | durable memory |
| `app_build` `app_run` `app_list` `app_source` | the userland |

Every syscall is journaled with a before/after state embedding, which is what the
world model trains on.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

123 tests: RFC 8439 cipher vectors, vault round-trip and tamper detection, the
filesystem jail, the permission gate, memory ranking, the app lifecycle,
build-time smoke checks, the kernel loop against a scripted model, executor
delegation, OpenRouter wire-format parsing, offline degradation, and the world
model — including that it beats the do-nothing baseline, flags deletions as
destructive, and predicts exactly zero for read-only syscalls.
