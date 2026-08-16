# aiOS — session handoff

Written so a fresh session can resume with no prior context. Read this, then
`README.md` for the user-facing description.

**State:** working, 174 tests green, everything committed.
Nothing in flight, nothing half-finished.

---

## What this is

An agent OS whose userland it writes itself. You ask for a capability it lacks;
it writes a Python program, installs it into `apps/`, and that program is a
permanent command on every future boot — optionally on a schedule. Two halves:

- **Token-space half** — an LLM agent loop over 15 syscalls (the conventional part).
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
./root/aios schedd                              # the scheduling daemon, foreground
modal run build/modal/aios_modal.py             # on Modal, interactive
modal run build/modal/aios_modal.py::status     # what the volume holds
modal run build/modal/aios_modal.py::selftest   # prove sandbox capability enforcement
modal run build/modal/aios_modal.py::smoke      # one real agent turn, no TTY needed
modal run build/modal/aios_modal.py::local_brain  # GPU, self-hosted Qwen2.5-7B
./build/usb/make-usb.sh --image-only aios-usb.img  # build + verify a boot image, write nothing
python3 -m unittest discover -s tests           # 174 tests
```

**The interactive shell needs a TTY, which an agent session cannot drive.** Use
`::smoke` to exercise the real agent loop non-interactively — that is how every
live-API claim in this repo was verified.

---

## Environment facts (specific to this user)

- Modal workspace `<your-modal-workspace>`, authenticated, CLI 1.5.3.
- Modal secret **`openrouter-api-key`** exposes `OPENROUTER_API_KEY`.
- Volumes: `aios-root` (the OS), `aios-app-data` (sandboxed app scratch),
  `aios-hf-cache` (model weights).
- The OpenRouter key has a **$1 limit** — this is why `max_tokens` is capped (see below).
- Host is a 2017 Intel MacBook, 2 cores / 8 GB, macOS 13. **qemu cannot be built
  here** — confirmed again this session with a concrete cause, not just repeated
  folklore: `p11-kit`'s `meson test` hangs on `test-transport`/`test-transport3`
  (60s timeout each, `SIGTERM`-killed) during `brew install qemu`, almost
  certainly because this sandboxed shell has no D-Bus session bus. Homebrew
  treats a test failure as fatal on this unsupported tier (macOS 13 + Intel) and
  will not skip it. The local VM target is written but *untested* — do not claim
  otherwise.
- **Docker is not installed.** `colima`/`lima` (Go binaries, bottle-installed, no
  compile trap) work fine and give a real Linux VM via macOS's Virtualization
  framework — but that framework's Linux boot path loads a kernel+initrd
  directly and skips BIOS/MBR emulation entirely, so it cannot test the one
  thing that actually needed testing this session (does firmware find and boot
  from a patched MBR). `vfkit` has the same limitation. Only qemu's full-system
  emulation does that, and it isn't available here.
- The Mac is pre-T2, so a bootable USB would not hit Secure Boot problems.
- `mtools`, `dosfstools` are now installed (`brew`, bottled) — used by
  `build/usb/make-usb.sh` to build a FAT32 image without mounting or sudo.

---

## Architecture

```
root/system/kernel/
  paths.py      the OS root + the jail check (AIOS_HOME binds at import time)
  vault.py      ChaCha20 + scrypt secret storage, verified against RFC 8439 vectors
  llm.py        any OpenAI-compatible endpoint (OpenRouter by default)
  syscalls.py   THE SECURITY BOUNDARY. 15 primitives, all model access goes here
  apps.py       userland registry + static validation of generated code
  memory.py     markdown facts + TF-IDF search
  sandbox.py    optional Modal-confined app execution (lazy import)
  schedule.py   job store, runner and daemon: apps that run unattended
  world.py      JEPA-style predictor: what will this syscall do?
  agent.py      the loop; also JSON repair and transition journaling
root/system/shell/tui.py    terminal shell, permission prompts, slash commands
build/          provision.sh (VPS/VM), deploy.sh (over SSH), modal/, vm/
build/usb/
  make-usb.sh     builds + (optionally) writes a bootable aiOS USB image
  mkusb-mbr.py    the one precise binary edit that makes it work -- see below
  provision-usb.sh  first-boot script, ships on the image's data partition
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
- World model: beats do-nothing baseline; flags deletions destructive regardless
  of size; read-only syscalls predict exactly 0.0000, including ones never seen
  verbatim in training and ones that merely mention `apps`/`memory` in a path
  without deleting anything. Swept 33 representative commands (23 benign, 10
  destructive) against a freshly bootstrapped model: 0 misclassifications.
- Modal volume persistence across separate invocations.
- **Scheduling, end to end on a real clock.** Built a `ticker` app, scheduled it
  `every 30s`, started the detached daemon: fired at 19:03:05 and 19:03:35, exit 0
  both times, app data accumulated across runs, everything confined to the root.
  `stop()` reaped the pidfile. Then backdated the job three days and restarted —
  **one** run on resume, not the 8,640 it slept through.
- **The bootable-USB image, everything short of actually booting it:**
  - The Alpine Standard ISO's sha256 verified against Alpine's own published
    manifest (fetched fresh, not pinned — see lessons below).
  - The MBR patch (`mkusb-mbr.py`) changes exactly 10 bytes, all inside the one
    16-byte slot it targets, on the real downloaded ISO — checked with `cmp -l`,
    not assumed. Every other byte of the 283MB ISO is untouched.
  - The composite image (ISO + patched MBR + FAT32 data partition) attaches
    cleanly with `hdiutil` as a two-partition disk; the data partition mounts
    and its contents match `git archive HEAD root` byte-for-byte. Checked twice
    — once from `--image-only`, once by re-attaching the image `make-usb.sh`
    itself wrote in the device-write test below.
  - **The full `--device` write path, run for real** against an `hdiutil`-attached
    virtual disk (a plain file, zero physical risk — a disk image genuinely
    reports `Device Location: External` / `Removable Media: Removable` to
    `diskutil`, which is what makes this safe to test at all): confirmed the
    confirmation-mismatch path refuses and writes nothing, then confirmed the
    real path writes correctly, ejects, and the result re-verifies clean on a
    fresh attach.
  - This surfaced two real bugs before either shipped — see lessons below.

**Written but NOT verified:**
- **Local VM** (`build/vm/run-vm.sh`) — blocked on qemu. Syntax-checked only.
- **VPS deploy** (`build/deploy.sh`) — no box to try it on. The provisioner logic
  was tested locally against a temp prefix.
- **USB bare-metal boot — the one thing all of the above cannot prove.** Does a
  real machine's firmware actually find and boot from the patched MBR? Needs
  qemu (unavailable, confirmed above with a concrete cause) or a real stick in
  a real machine (out of scope without someone physically present). Everything
  upstream of "does it boot" is now verified; that one step is not, and no
  amount of image-inspection substitutes for it. If you try it: report back
  either way, and if it fails, `mkusb-mbr.py`'s slot-3 assumption is the first
  thing to question, not the safety gates in `make-usb.sh`.
- **`provision-usb.sh`'s Alpine mechanics, specifically.** The commands
  (`blkid -L`, `lbu commit` with `LBU_BACKUPDIR`, the `/etc/local.d` hook,
  `setup-apkcache`'s actual effect) are grounded in the real `alpine-conf`
  package source, not memory or blog posts — extracted and read directly this
  session (see lessons below). What is NOT verified is the boot-time apkovl
  auto-restore this whole persistence story depends on: every source describes
  it consistently ("scans all available filesystems for `*.apkovl.tar.gz`"),
  but that logic lives in the initramfs/mkinitfs boot scripts, which were not
  pulled and read the way `lbu` itself was. Second-most-likely failure point
  after the MBR slot, if a real boot doesn't come back clean on reboot.
- **Scheduling anywhere but this Mac.** The daemon has not run on Modal or a VPS.
  Modal in particular is serverless — a container stops when you leave, so a
  long-lived `schedd` there is not obviously meaningful. Untried, not designed for.
- **`/sched start` from the interactive shell.** The daemon itself is verified,
  but the spawn path lives in `tui.py`, which needs a TTY an agent cannot drive.
  The daemon was launched directly (`aios.py schedd`) in every test above.

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

5. **A feature that *can* be true for a benign command will eventually be true
   for one.** The `proc_run` action encoding had four features doing substring
   or word matching without enough context, and every one of them let something
   harmless through as destructive:
   - `command length`, scaled 0–4 — correlated with the destructive indicators
     purely by accident of the training set (deletions in `bootstrap()` happened
     to be longer than `ls -la`). Ridge regression leaned on it as a danger
     proxy, so any short *unseen* command — literally `ls` typed alone, never in
     the training set — predicted destructive regardless of content. Fix:
     deleted the feature; slot kept reserved rather than reused.
   - `w.startswith("-") and ("r" in w or "f" in w)`, meant to catch `-rf` — also
     matched `--version`, `--force-color`, `--verbose`, anything with r or f
     anywhere in a long flag name. `python3 --version` predicted destructive.
   - `"apps"`/`"memory"`/`"vault"`/`"data"` in `words` — meant to say *which*
     directory a deletion targets, but fired on the word alone. `words` comes
     from `cmd.replace("/", " ").split()`, so any path mentioning the directory
     turns it into a standalone word: `mkdir apps/newapp`, `cat memory/note.md`,
     `curl .../apps/list` all predicted destructive despite not deleting
     anything. Found by deliberately auditing the sibling features after fixing
     the `-rf` one, on the hypothesis that the same mistake was probably made
     more than once — it was.
   Fix for all three word/flag features, the same shape each time: gate on an
   actual `rm`-family word (`rm`, `rmdir`, `unlink`, `shred`, `truncate`, `dd`)
   being present in the *same* command. `_is_force_recursive_flag()` also
   tightened to match only `-r`/`-f`/`-rf`/`-fr`/`--recursive`/`--force`
   exactly, not any flag containing those letters. The directory word alone, or
   the flag alone, is not informative — only in the context of an actual
   deletion does either one mean anything.
   All four were **already wrong at the commit that introduced the world
   model**, not something scheduling broke — removing the length feature just
   stopped it from masking the others on some inputs. A 33-command sweep
   (23 benign, 10 destructive) now returns 0 misclassifications; see
   `TestBootstrapAndDestruction`. **If another word/substring feature like this
   gets added to `encode_action`, assume it has the same bug until swept against
   a comparably wide command sample — three of four were broken, not one.**

6. **The destructive gate should be direction alone, not direction gated by
   typical size.** `explain()` required `d >= self.scale`, where `scale` is the
   *median* disturbance across every effectful training action — which, by
   construction, is below half of them. An ordinary single-file `rm` landed
   almost exactly on that median and silently passed the gate. Losing one file
   to an unattended job is exactly the case a permission prompt exists for,
   regardless of whether it's a "typical-sized" action for this machine. Fixed:
   `destructive = direction == "removes"`, full stop — the `-1e-4` noise floor
   already lives inside `direction`, so a second, coarser threshold on top of it
   protected nothing. `scale` still has a job: `risk()` uses it to rank magnitude
   for display, which is a different question from "is this reversible."

### Scheduling
- **A job may run an installed app and nothing else.** No syscall schedules a raw
  command or code, deliberately: a job runs with nobody watching, so it is limited
  to artifacts whose capabilities the user already approved. It reuses
  `Registry.run()`, so caps, sandbox and secret isolation are unchanged. There is
  a test asserting no `sched_*` schema accepts `command` or `code` — keep it.
- **Compute the next run forward from now, never from the missed slot.** Otherwise
  a daemon that was off overnight wakes up and fires a 5m job hundreds of times,
  each one possibly billing an API. Same rule when re-enabling a paused job.
- **Adding a syscall shifts the world model's action layout.** `D_ACTION` stayed
  32, so dimensions alone could not tell a stale `world.json` from a current one —
  it would load cleanly and mean something different in every slot. Hence
  `world.LAYOUT`; bump it whenever a slot's meaning moves.
- **The world model cannot see what makes a job risky.** `sched_add` writes one
  small file, so it correctly predicts ~0.0007, "low". That the app then runs
  forever, unattended, is not a filesystem fact. The permission prompt states it
  in words; do not try to make the embedding carry it.
- Don't auto-start the daemon at boot. A job firing because someone opened a shell
  is exactly the surprise this OS should not spring.

### Bootable USB
- **When you can't test the real thing, find the largest piece of it you
  actually can, and test that for real instead of reasoning about all of it.**
  Booting real firmware was never testable this session. Everything upstream
  of that — download integrity, the exact bytes a binary patch changes, whether
  an assembled image mounts and matches its source, whether the destructive
  device-write path's safety gates actually gate — all was. Doing that work
  found two real bugs that pure code review had already missed once:
  1. `IS_INTERNAL="$(... awk '/Internal/{print $2}')"` matched the *value*
     "Internal" under macOS's `Device Location:` field, not a field literally
     named `Internal:` (which doesn't exist). `$2` was therefore always
     "Internal" or "External", never "Yes"/"No" — so
     `[ "$IS_INTERNAL" = "No" ]` was **never true, for any device**, and the
     script would have refused to write to a legitimate USB stick every single
     time. Caught by running the real refusal logic against real `diskutil
     info` output for an actual disk, not by reading the code again.
  2. The FAT32 data-partition features (`apps`/`memory`/`vault`/`data` word
     matching in `world.py`'s `proc_run` encoding — see the world-model
     section above) is the same *shape* of bug as #1: code that looks locally
     correct and is wrong about what a field or pattern actually contains.
     Different subsystem, same lesson, same session. Worth remembering as a
     class, not a one-off.
  A destructive-write tool is exactly the wrong place to discover a safety
  gate doesn't gate. It was caught here because a virtual disk backed by a
  plain file reports `Device Location: External` / `Removable Media:
  Removable` to `diskutil` just like real removable media does — meaning the
  *entire* `--device` code path, including the interactive confirmation
  prompt, is safely testable without any physical device at all. Do this
  before ever pointing a similar tool at something real.
- **Don't guess a CLI's flags from blog posts when the source is one `apk
  fetch` away.** `lbu commit -d <dir>` looked, from multiple independent
  write-ups, like "write the apkovl to this directory." It means "delete old
  apk overlay files" — an entirely different flag. Downloaded the real
  `alpine-conf` package (`curl .../alpine-conf-*.apk`, which is just a
  tar.gz) and read `usr/sbin/lbu_commit` directly: the actual mechanism is
  `LBU_BACKUPDIR` in `/etc/lbu/lbu.conf`, read at the top of every `lbu`
  invocation, which bypasses Alpine's `/media/<usb|floppy>` convention
  entirely. Would have shipped a config line that silently did the wrong
  thing, discovered only when someone's "persistent" stick lost its state on
  reboot.
- **`/root/.profile` is not what you think it is for `lbu`.** By default `lbu
  commit` only backs up `/etc/` (plus a `setup-alpine`-created user's home,
  which this flow never runs). `provision.sh` (the VPS/VM target) edits
  `/root/.profile` for console autostart and that's fine there — the VPS disk
  is always-on, nothing needs to survive a wipe-and-restore cycle. On a
  diskless Alpine stick it would silently not persist. `provision-usb.sh`
  edits `/etc/profile` instead — functionally identical for a root-only
  console appliance, and actually covered by `lbu`'s default scope.
- **A foreign disk image's MBR is not something to hand to a real partitioning
  tool.** `parted`/`sfdisk`/`diskutil` risk reinterpreting or rewriting the
  two entries Alpine's own bootloader depends on. Inspecting the real,
  downloaded ISO showed only 2 of 4 MBR slots used and the other two
  provably all-zero (`xxd -s 446 -l 66`), and the ISO's size landed on an
  exact 1 MiB boundary (270 MiB, not a round number by luck alone — Alpine
  builds it that way). That made "write one precise, asserted, previously-
  unused 16-byte entry" both correct and mechanically checkable — verify
  `bytes(mbr[:slot]) == iso[:slot]` and same for after the slot — a smaller
  claim than "used a real tool correctly," which would need trusting the tool
  understood a hybrid MBR it didn't create.
- **Fetch the release manifest, don't pin a version.** `run-vm.sh` hardcodes
  `ALPINE_VER="3.22.4"`; by the time this session ran, Alpine had shipped
  3.22.5. `make-usb.sh` fetches `latest-releases.yaml` fresh every run and
  takes both the filename and the sha256 from it, so there is nothing to go
  stale.

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
- **A scheduled run is not gated.** Approving `sched_add` once approves every run
  after it — that is what scheduling *is*, and the prompt says so, but it is the
  widest grant in the system. Jobs are confined to installed apps for this reason.
- **The daemon holds the unsealed vault in memory** for its lifetime, so scheduled
  apps get the secrets they declared. Nothing writes them to disk. But a
  long-running `schedd` is a process holding your keys with no passphrase prompt
  in front of it; `/sched stop` when you care.
- **Offline: the agent cannot think.** OpenRouter is remote. Installed apps,
  memory, `fs_*` and the shell all keep working; failures are legible, not tracebacks.
- **`proc_run: ls` was wrongly flagged destructive — fixed this session.** An
  earlier version of this file claimed it predicted −0.007 and was "not
  escalated"; that was wrong, and was wrong at `a1cc896` too. Root causes were
  four unguarded word/substring features and the destructive gate itself, all
  described under "World model" lessons 5–6 above. Every `proc_run` action
  feature that did word or substring matching was audited, not just the one
  that produced the `ls` symptom — three of four were broken. All fixed and
  covered by regression tests (`test_unseen_benign_commands_are_not_flagged_destructive`,
  `test_single_file_deletion_is_flagged_destructive`,
  `test_mentioning_a_directory_without_deleting_is_not_destructive`,
  `test_deleting_named_directories_is_still_flagged`,
  `test_destructive_does_not_depend_on_scale`). `world.LAYOUT` is now `6` —
  any `world.json` saved before this session retrains cleanly rather than being
  silently misread.
- World model reports `under-trained` below 40 transitions rather than bluffing.
- Qwen2.5-7B local brain works but is meaningfully worse than opus-5 at writing
  correct programs; it needed the build-time smoke check to self-correct.
- **The USB image's persistence has never survived a real reboot.** Everything
  in `provision-usb.sh` is grounded in the actual `alpine-conf` source (not
  guessed), but the boot-time apkovl auto-restore it depends on was not itself
  read or tested — see "Written but NOT verified" above. First real boot should
  specifically check: does the stick come back with no login prompt on the
  *second* boot, and is `/etc/profile`'s aios hook present after the reboot.

---

## Sensible next steps

1. **Boot the USB image on real hardware, or find a working qemu.** This is now
   the single highest-value next step: `build/usb/make-usb.sh` is written,
   safety-tested, and structurally verified end to end, but nobody has watched
   it actually boot. The Mac being pre-T2 helps if it's the test machine.
   Failing that, a Linux box with a working qemu install would let this be
   verified without any physical stick at all.
2. **VPS deploy** — still the only untested deploy path that is cheap to verify,
   and now more valuable: it is the always-on box that makes scheduling worth
   having. Nothing about scheduling has been tried on a remote host.
3. **Nonlinear world-model predictor** — the transitions already record state,
   so an MLP could exploit it. Only `WorldModel.fit()` changes.
4. **Learned encoder** — would make the JEPA half faithful to the paper (needs an
   EMA target encoder + stop-gradient to avoid representation collapse).

All `proc_run` action features have now been audited for the unguarded-word
bug class (lesson 5) — done this session, not a remaining item. The `mkdir`/
`touch`/`cp`/`mv`/`tee` creation feature (`b+6`) was checked too and is fine
standing alone: it is a positive "this creates something" signal, not a danger
signal, so it is correctly *not* gated on `is_rm`.

---

## Working agreements from this session

- Verify claims by running them. Say "verified" only with output to show, and mark
  untested things untested.
- Report failures verbatim, including one's own mistakes.
- Prefer measurement over argument when a design choice is in question — every
  world-model fix above came from an experiment, and several contradicted a
  confident prediction.
