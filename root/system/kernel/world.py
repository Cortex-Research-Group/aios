"""A JEPA-style world model of the OS itself.

The kernel reasons in token space: the model writes a syscall, the kernel runs
it, the model reads the result. That is fine when running the syscall is cheap
and reversible. It is exactly wrong when it is not -- you cannot un-run
`rm -rf /apps` and read the outcome.

So this module learns to answer "what would that do?" without doing it. It
follows the Joint Embedding Predictive Architecture idea: encode the state, and
predict the *embedding* of the next state from the embedding of the current one
plus the action. It never tries to reconstruct the literal next state -- no file
listings, no output text. Only the representation.

    predict(encode(state), encode(action))  ~=  encode(next_state)

Trained on the transitions the OS already journals, the norm of the predicted
change is a usable "how much does this disturb the world" signal, which the
permission gate can surface before you approve something irreversible.

Honest simplifications, since this is a working component and not a paper:

  * The encoder is fixed and hand-designed (feature hashing over the filesystem),
    not learned. Real JEPA learns the encoder jointly, using an EMA target
    encoder and a stop-gradient to avoid representation collapse. A fixed
    encoder cannot collapse, which removes the hardest part of the problem and
    also removes the part that makes JEPA interesting on raw perceptual data.
    Here the state is already structured, so a learned encoder buys much less.
  * The predictor is ridge regression on the residual, solved in closed form.
    Linear is enough for the dynamics of a filesystem and it trains in
    milliseconds with no dependencies. Swapping in an MLP changes only _fit().

Everything is stdlib: the OS must still boot on a stick.
"""

import hashlib
import json
import math
import time
from pathlib import Path

from . import paths

D_STATE = 24
D_ACTION = 32
RIDGE = 1e-3

# Syscalls get a dedicated slot each rather than a hash bucket. Hashing 12 names
# into 12 buckets collides by the birthday bound -- fs_write, mem_write and
# app_build all landed together, making three very different actions
# indistinguishable to the model. Unknown names still hash, into the tail.
KNOWN_SYSCALLS = (
    "fs_read", "fs_write", "fs_list", "proc_run", "net_fetch", "web_search",
    "mem_write", "mem_search", "app_build", "app_run", "app_list", "app_source",
    "sched_add", "sched_list", "sched_remove",
)
# Leave headroom above the known set: at exactly len(KNOWN_SYSCALLS) every
# unknown name would share the one remaining bucket, which is the collision this
# table exists to avoid.
_SYSCALL_SLOTS = 20

# The layout of the action vector, not its length. Adding syscalls shifts where
# the argument features live while D_ACTION stays 32, so dimensions alone cannot
# tell a stale saved model from a current one -- it would load and be silently
# misinterpreted. Bump this whenever the meaning of a slot moves.
LAYOUT = 6

_PROC_BASE = _SYSCALL_SLOTS       # 20..27: what a shell command intends
_CONTENT = _PROC_BASE + 8         # 28: size of the payload being written
assert _CONTENT < D_ACTION, "action features no longer fit in D_ACTION"

# Below this many transitions the model is fitting noise and should say so
# rather than quietly emitting confident nonsense.
MIN_SAMPLES = 40


# --- encoders -----------------------------------------------------------------


def _bucket(text: str, n: int) -> int:
    return int(hashlib.blake2b(text.encode("utf-8"), digest_size=4).hexdigest(), 16) % n


def _scale(x: float, unit: float, cap: float = 4.0) -> float:
    """Linear, capped.

    The obvious choice is log1p, to keep counts bounded. It is wrong here: under
    a log, adding one file to a world of ten moves the embedding far more than
    adding one to a world of three hundred, so an action's effect depends on the
    state multiplicatively -- a relationship an additive linear predictor cannot
    represent. Measured, that encoder could not tell fs_write from fs_read.
    Linear counts make "+1 file" the same delta everywhere, which is exactly the
    structure the predictor can learn.
    """
    return min(max(0.0, x) / unit, cap)


def encode_state(root: Path | None = None) -> list[float]:
    """Embed the OS's current state.

    Deliberately cheap: this runs before and after every mutating syscall.
    """
    root = Path(root) if root else paths.HOME
    v = [0.0] * D_STATE

    n_files = n_bytes = 0
    per_dir = {"apps": 0, "memory": 0, "data": 0, "logs": 0}

    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        n_files += 1
        n_bytes += size
        try:
            top = path.relative_to(root).parts[0]
        except (ValueError, IndexError):
            continue
        if top in per_dir:
            per_dir[top] += 1
        # Hashed bag of paths: gives the model a coarse sense of *what* exists,
        # not just how much.
        v[8 + _bucket(top + "/" + path.name, 16)] += 0.01

    apps_dir = root / "apps"
    app_names = [d.name for d in apps_dir.iterdir() if d.is_dir()] if apps_dir.is_dir() else []

    v[0] = _scale(len(app_names), 20)
    v[1] = _scale(per_dir["apps"], 100)
    v[2] = _scale(per_dir["memory"], 50)
    v[3] = _scale(n_files, 100)
    v[4] = _scale(n_bytes, 1e6)
    v[5] = _scale(per_dir["data"], 100)
    v[6] = _scale(per_dir["logs"], 50)
    v[7] = 1.0 if (root / "vault" / "keys.enc").exists() else 0.0
    for i in range(8, D_STATE):
        v[i] = min(v[i], 4.0)
    return v


def _is_force_recursive_flag(word: str) -> bool:
    """True for -r, -f, -rf, -fr, --recursive, --force -- the flags that turn
    `rm` from one file into a subtree.

    Not simple substring matching: `w.startswith("-") and ("r" in w or "f" in w)`
    also matched `--version`, `--force` unrelated to deletion, `--verbose`, and
    any other long flag that merely contains the letter r or f somewhere in its
    name. Measured, that made `python3 --version` predict as destructive.
    """
    body = word.lstrip("-")
    if not body:
        return False
    if word.startswith("--"):
        return body in ("recursive", "force")
    return bool(body) and set(body) <= {"r", "f"}


def encode_action(name: str, args: dict | None = None) -> list[float]:
    """Embed a proposed syscall.

    Hashed rather than one-hot over a fixed table, so a syscall added later
    still lands somewhere sensible instead of falling off the end.
    """
    args = args or {}
    v = [0.0] * D_ACTION

    # 0.._SYSCALL_SLOTS-1 -- identity. One slot per known syscall; unknown names
    # hash into the tail so a syscall added later cannot steal a trained slot.
    if name in KNOWN_SYSCALLS:
        v[KNOWN_SYSCALLS.index(name)] = 1.0
    else:
        v[len(KNOWN_SYSCALLS) + _bucket(name, _SYSCALL_SLOTS - len(KNOWN_SYSCALLS))] = 1.0

    # _PROC_BASE.. -- what a shell command intends. Scoped to proc_run on purpose:
    # features shared across syscalls leak learned effects between them. Hashed
    # argument tokens were worse still -- an unseen path landed in a bucket
    # trained on deletions, and a harmless read inherited its predicted damage.
    if name == "proc_run":
        b = _PROC_BASE
        cmd = str(args.get("command", "")).lower()
        words = cmd.replace("/", " ").split()
        is_rm = any(w in ("rm", "rmdir", "unlink", "shred", "truncate", "dd") for w in words)
        v[b + 0] = 1.0 if is_rm else 0.0
        # Tied to an rm-family word being present, not judged on its own: -r and
        # -f are ordinary flags for grep, tar, curl and a dozen other commands
        # that do not delete anything. Only in the context of rm does "recursive,
        # force" mean "no confirmation, no going back".
        v[b + 1] = 1.0 if is_rm and any(_is_force_recursive_flag(w) for w in words) else 0.0
        # Same reasoning as the flag above, and found the same way: these four
        # used to fire on the word alone, so `curl .../apps/list`, `mkdir
        # apps/x` and `cat memory/note.md` all mentioned a directory name and
        # picked up part of rm's learned effect on that directory, despite not
        # deleting anything. Which directory is only informative in the context
        # of an actual deletion; gate on is_rm like the flag feature.
        v[b + 2] = 1.0 if is_rm and "apps" in words else 0.0
        v[b + 3] = 1.0 if is_rm and "memory" in words else 0.0
        v[b + 4] = 1.0 if is_rm and "vault" in words else 0.0
        v[b + 5] = 1.0 if is_rm and "data" in words else 0.0
        v[b + 6] = 1.0 if any(w in ("mkdir", "touch", "cp", "mv", "tee") for w in words) else 0.0
        # b+7 was command length. Removed: it correlated with the destructive
        # indicators by accident of the training set (deletions happened to be
        # longer commands than `ls`), so ridge regression leaned on it as a proxy
        # for danger. Measured, that made short *unseen* commands -- anything not
        # literally in bootstrap()'s training set, starting with plain `ls` --
        # predict as destructive purely for being short, regardless of content.
        # Slot kept reserved (always 0) rather than reused, so D_ACTION and every
        # slot after it do not shift again.

    # How much content is being written, for the calls that write content.
    if name in ("fs_write", "app_build", "mem_write"):
        payload = args.get("content") or args.get("code") or ""
        v[_CONTENT] = _scale(len(str(payload)), 4000)

    return v


# --- linear algebra (stdlib only) ---------------------------------------------


def _solve(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Solve A X = B by Gauss-Jordan with partial pivoting."""
    n = len(a)
    m = len(b[0])
    aug = [list(a[i]) + list(b[i]) for i in range(n)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            continue  # ridge term should prevent this; skip rather than divide by ~0
        aug[col], aug[pivot] = aug[pivot], aug[col]
        div = aug[col][col]
        aug[col] = [x / div for x in aug[col]]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor:
                aug[r] = [x - factor * y for x, y in zip(aug[r], aug[col])]

    return [row[n : n + m] for row in aug]


# --- the model ----------------------------------------------------------------


class WorldModel:
    """Predicts encode(next_state) from encode(state) and encode(action).

    Predicts the *residual* -- the change -- because most of the next state is
    the current state, and asking a linear map to relearn the identity wastes
    all of its capacity on the boring part.
    """

    def __init__(self, weights: list[list[float]] | None = None, trained_on: int = 0,
                 scale: float = 0.0):
        self.weights = weights  # D_STATE rows x (D_STATE + D_ACTION) cols
        self.trained_on = trained_on
        # Median action-effect seen in training, so a raw embedding distance can
        # be reported as "ordinary" or "extreme" for this particular machine.
        self.scale = scale

    @property
    def is_trained(self) -> bool:
        return self.weights is not None

    @property
    def is_reliable(self) -> bool:
        return self.is_trained and self.trained_on >= MIN_SAMPLES

    # --- training ---

    def fit(self, samples: list[tuple[list[float], list[float], list[float]]]) -> dict:
        """samples: (state, action, next_state). Returns training stats."""
        if not samples:
            raise ValueError("no transitions to learn from")

        # The design matrix carries the action only. Including the state was
        # measured as worse on every count: it gave the predictor a drift term
        # that fired regardless of action, so a read scored as disturbing as a
        # write, and read-only syscalls could never predict exactly zero.
        # Under the linear state encoding a delta is action-determined anyway.
        # The state is still recorded in every transition, so a nonlinear
        # predictor can exploit it later without re-gathering data.
        x = [[0.0] * D_STATE + a for _, a, _ in samples]
        y = [[n[i] - s[i] for i in range(D_STATE)] for s, _, n in samples]

        d = D_STATE + D_ACTION
        xtx = [[sum(row[i] * row[j] for row in x) + (RIDGE if i == j else 0.0)
                for j in range(d)] for i in range(d)]
        xty = [[sum(x[k][i] * y[k][j] for k in range(len(x))) for j in range(D_STATE)]
               for i in range(d)]

        w = _solve(xtx, xty)  # d x D_STATE
        self.weights = [[w[i][j] for i in range(d)] for j in range(D_STATE)]
        self.trained_on = len(samples)

        errors = []
        for s, a, n in samples:
            pred = self.predict(s, a)
            errors.append(math.sqrt(sum((pred[i] - n[i]) ** 2 for i in range(D_STATE))))
        baseline = [
            math.sqrt(sum((s[i] - n[i]) ** 2 for i in range(D_STATE))) for s, _, n in samples
        ]
        # Calibrate against actions that actually do something. Most syscalls
        # are read-only and predict exactly zero, so a median over all of them
        # sits near zero and makes every ordinary write look catastrophic.
        effects = sorted(d for d in (self.disturbance(s, a) for s, a, _ in samples) if d > 1e-6)
        self.scale = effects[len(effects) // 2] if effects else 0.0

        return {
            "samples": len(samples),
            "scale": self.scale,
            "rmse": sum(errors) / len(errors),
            # How much better than assuming nothing ever changes. Below 1.0 the
            # model has learned something; at 1.0 it has learned nothing.
            "vs_static": (sum(errors) / len(errors)) / (sum(baseline) / len(baseline) or 1e-9),
        }

    # --- inference ---

    def predict(self, state: list[float], action: list[float]) -> list[float]:
        if not self.is_trained:
            return list(state)  # untrained: assume the world does not move
        x = [0.0] * D_STATE + action  # action-only; see fit()
        return [state[i] + sum(self.weights[i][j] * x[j] for j in range(len(x)))
                for i in range(D_STATE)]

    def disturbance(self, state: list[float], action: list[float]) -> float:
        """Predicted change attributable to *this action*, in embedding space.

        Measured counterfactually, against taking no action from the same state.
        The naive version -- distance from the current state to the predicted one
        -- also picks up the model's state-dependent drift term, which made a
        read score as high as a write. What matters is the action's own effect.
        """
        if not self.is_trained:
            return 0.0
        acted = self.predict(state, action)
        idle = self.predict(state, [0.0] * D_ACTION)
        return math.sqrt(sum((acted[i] - idle[i]) ** 2 for i in range(D_STATE)))

    def explain(self, state: list[float], action: list[float]) -> dict:
        """Predicted change, broken out along the interpretable dimensions.

        Like disturbance(), reported against the do-nothing counterfactual.
        """
        pred = self.predict(state, action)
        idle = self.predict(state, [0.0] * D_ACTION)
        labels = {
            0: "app count", 1: "app files", 2: "memories", 3: "total files",
            4: "total bytes", 5: "data files", 6: "log files", 7: "vault",
        }
        deltas = {
            label: round(pred[i] - idle[i], 4)
            for i, label in labels.items()
            if abs(pred[i] - idle[i]) > 1e-4
        }
        d = self.disturbance(state, action)
        # Magnitude is not danger. Installing an app moves the world further
        # than deleting one note, but only one of them destroys your work, and
        # that is what a permission prompt exists to warn about. The sign of the
        # predicted change on the counting dimensions carries that.
        net = sum(pred[i] - idle[i] for i in (0, 1, 2, 3, 5))
        direction = "removes" if net < -1e-4 else ("creates" if net > 1e-4 else "none")
        # destructive is direction alone -- any real removal, however small.
        # It used to also require d >= self.scale, gating on the *median*
        # disturbance across every effectful training action. That excludes half
        # of all real deletions by construction: an ordinary single-file `rm`
        # landed right at that median and silently failed to warn. The -1e-4
        # threshold inside `direction` above is already the noise floor; a
        # second, coarser one on top of it protected nothing and hid real risk.
        return {
            "disturbance": round(d, 4),
            "magnitude": self.risk(d),
            "direction": direction,
            "destructive": direction == "removes",
            "predicted_changes": deltas,
            "reliable": self.is_reliable,
            "trained_on": self.trained_on,
        }

    def risk(self, disturbance: float) -> str:
        """Rank a disturbance against what this OS normally does.

        The raw number is in embedding units and means nothing on its own; what
        is useful is whether an action is unusually disruptive for this machine.
        """
        if not self.scale:
            return "unknown"
        if disturbance < 1e-6:
            return "negligible"
        ratio = disturbance / self.scale
        if ratio < 0.5:
            return "low"
        if ratio < 1.5:
            return "ordinary"
        if ratio < 3.0:
            return "high"
        return "extreme"

    # --- persistence ---

    def save(self, path: Path | None = None) -> Path:
        path = Path(path) if path else paths.DATA / "world.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "weights": self.weights,
            "trained_on": self.trained_on,
            "scale": self.scale,
            "d_state": D_STATE,
            "d_action": D_ACTION,
            "layout": LAYOUT,
            "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        }), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "WorldModel":
        path = Path(path) if path else paths.DATA / "world.json"
        if not path.exists():
            return cls()
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        # A model trained under different dimensions cannot be interpreted --
        # and neither can one trained under a different slot layout, which the
        # dimensions alone would not catch. Retrain rather than mislead.
        if blob.get("d_state") != D_STATE or blob.get("d_action") != D_ACTION:
            return cls()
        if blob.get("layout") != LAYOUT:
            return cls()
        return cls(blob.get("weights"), blob.get("trained_on", 0), blob.get("scale", 0.0))


# --- training data ------------------------------------------------------------


def load_transitions(logs: Path | None = None) -> list[tuple]:
    """Recover (state, action, next_state) triples from the session journals."""
    logs = Path(logs) if logs else paths.LOGS
    if not logs.is_dir():
        return []

    samples = []
    for path in sorted(logs.glob("session-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("role") != "transition":
                continue
            s, a, n = entry.get("s"), entry.get("a"), entry.get("s2")
            if (isinstance(s, list) and isinstance(a, list) and isinstance(n, list)
                    and len(s) == D_STATE and len(a) == D_ACTION and len(n) == D_STATE):
                samples.append((s, a, n))
    return samples


def bootstrap(rounds: int = 25) -> list[tuple]:
    """Learn the OS's dynamics by acting in a throwaway copy of it.

    Journalled transitions only cover what you happen to have done, and nobody
    spends a session deleting things -- so a model trained purely on real usage
    has never seen destruction and cannot predict it. Measured, such a model
    predicted `rm -rf /apps` would *increase* the app count, which is worse than
    useless for the one job it has.

    So the OS practises. This runs a curated set of syscalls, creation and
    destruction alike, against a scratch root that is deleted afterwards,
    recording the same (state, action, next_state) triples. Nothing here touches
    the real root.
    """
    import shutil
    import tempfile

    samples = []
    scratch = Path(tempfile.mkdtemp(prefix="aios-bootstrap-"))
    try:
        for d in ("apps", "memory", "data", "logs"):
            (scratch / d).mkdir(parents=True, exist_ok=True)

        def act(name: str, args: dict, mutate) -> None:
            before = encode_state(scratch)
            try:
                mutate()
            except OSError:
                return
            samples.append((before, encode_action(name, args), encode_state(scratch)))

        for i in range(rounds):
            # --- creation ---
            act("fs_write", {"path": f"/data/b{i}.txt", "content": "x"},
                lambda i=i: (scratch / "data" / f"b{i}.txt").write_text("x" * 400))

            def build(i=i):
                d = scratch / "apps" / f"boot{i}"
                d.mkdir(exist_ok=True)
                (d / "main.py").write_text("print('ok')\n" * 10)
                (d / "manifest.json").write_text("{}")
                (d / "spec.md").write_text("spec\n")
            act("app_build", {"name": f"boot{i}", "description": "d", "spec": "s",
                              "code": "print('ok')"}, build)

            act("mem_write", {"title": f"note {i}", "content": "something"},
                lambda i=i: (scratch / "memory" / f"note-{i}.md").write_text("# note\nbody\n"))

            # --- observation: must be learned as harmless ---
            # Including benign shell commands matters: if every proc_run in the
            # training set is a deletion, the model learns that proc_run *means*
            # deletion and flags `ls` as destructive.
            act("proc_run", {"command": "ls -la"}, lambda: None)
            act("proc_run", {"command": "cat aios.json"}, lambda: None)
            act("proc_run", {"command": "echo hello"}, lambda: None)
            act("fs_read", {"path": f"/data/b{i}.txt"}, lambda: None)
            act("fs_list", {"path": "/"}, lambda: None)
            act("app_list", {}, lambda: None)
            act("mem_search", {"query": "note"}, lambda: None)

            # --- scheduling: rewrites one small file in data/, and that is all.
            # Worth practising anyway, so the model learns these are quiet rather
            # than never having seen them. Adding grows that file and removing
            # shrinks it, so the two are not taught as the same event. The part
            # the filesystem cannot show -- that the job then runs unattended,
            # repeatedly -- is carried by the permission prompt, in words.
            jobs_file = scratch / "data" / "schedule.json"

            def write_jobs(n):
                jobs_file.write_text('{"jobs": [' + '{"a": 1},' * n + "{}]}")

            act("sched_add", {"app": f"boot{i}", "every": "5m"},
                lambda i=i: write_jobs(i + 1))
            act("sched_list", {}, lambda: None)
            act("sched_remove", {"id": f"boot{i}"}, lambda i=i: write_jobs(i))

            # --- destruction: the whole reason this function exists ---
            if i >= 3:
                victim = scratch / "apps" / f"boot{i - 3}"
                act("proc_run", {"command": f"rm -rf apps/boot{i - 3}"},
                    lambda v=victim: shutil.rmtree(v, ignore_errors=True))

                doomed = scratch / "data" / f"b{i - 3}.txt"
                act("proc_run", {"command": f"rm data/b{i - 3}.txt"},
                    lambda d=doomed: d.unlink(missing_ok=True))

                note = scratch / "memory" / f"note-{i - 3}.md"
                act("proc_run", {"command": f"rm memory/note-{i - 3}.md"},
                    lambda n=note: n.unlink(missing_ok=True))

            if i and i % 10 == 0:  # occasionally, something drastic
                act("proc_run", {"command": "rm -rf apps"},
                    lambda: shutil.rmtree(scratch / "apps", ignore_errors=True))
                (scratch / "apps").mkdir(exist_ok=True)

        return samples
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
