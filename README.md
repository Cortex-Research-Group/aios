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
| **Local VM** | `./build/vm/run-vm.sh -d` then `./build/deploy.sh` | Real Alpine boot, disposable. The dev loop for the bootable target. |
| **VPS** | `./build/deploy.sh root@your-host` | Always-on. Your OS lives at an IP and keeps running when the laptop sleeps. |
| **USB stick** | copy `root/` to the drive, run `./aios` | Portable. Bare-metal boot is the next milestone. |

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

Honest limits: a generated app runs as a normal subprocess. The capability manifest
is *disclosure and key isolation*, not a sandbox — an app with `proc` can do what
your user account can do. Real confinement needs the Linux targets (`unshare`,
seccomp), which is why the VM and VPS paths matter beyond convenience. Run untrusted
prompts in the VM, not on your laptop.

## Syscalls

| | |
|---|---|
| `fs_read` `fs_write` `fs_list` | filesystem, jailed to the root |
| `proc_run` | shell command |
| `net_fetch` | HTTP(S), HTML reduced to text |
| `web_search` | DuckDuckGo, no API key needed |
| `mem_write` `mem_search` | durable memory |
| `app_build` `app_run` `app_list` `app_source` | the userland |

## Tests

```sh
python3 -m unittest discover -s tests -v
```

31 tests: RFC 8439 cipher vectors, vault round-trip and tamper detection, the
filesystem jail, the permission gate, memory ranking, and the app lifecycle.
