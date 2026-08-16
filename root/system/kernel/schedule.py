"""Scheduling: making an installed app run on its own.

The OS could already write a tool. It could not run one every five minutes, so
every capability it built needed a human at the keyboard to be worth anything.
This closes that gap.

THE SECURITY SHAPE -- read this before extending it

A scheduled job runs with nobody watching. That breaks the assumption the
permission gate rests on, so the reach of a job is deliberately narrower than
the reach of the agent:

  * A job may run **an installed app, and nothing else.** Not a syscall, not a
    shell command, not generated code. An app is an artifact you already saw the
    capabilities of and already approved installing.
  * It runs through the same Registry.run() as app_run -- same declared caps,
    same sandbox backend, same "only the secrets it asked for" rule. Scheduling
    grants no authority that running the app by hand would not.
  * So the only new power is *repetition without asking*. That is real, and it
    is what the permission prompt discloses when a job is created.

Nothing is written outside the aiOS root: the job list lives in data/, the run
history in logs/, the pidfile in data/. The host's cron and launchd are
deliberately not used -- they would pin the OS to one machine and leave state
behind when the stick is pulled.

Timekeeping notes:

  * `every` fires on an interval; `at` fires daily at a local wall-clock time.
  * The next run is always computed forward from *now*, never from the missed
    slot. A daemon that was off for a day therefore resumes with one run, not
    with 288 queued ones. Catching up on a schedule is almost never what the
    user meant and is an excellent way to spend a credit balance.

Stdlib only, like everything else here.
"""

import json
import os
import re
import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import paths

# A job that fires faster than this is a busy loop wearing a schedule's clothes.
MIN_INTERVAL = 30
TICK = 5  # seconds the daemon sleeps between checks for due work
MAX_OUTPUT = 2000  # chars of a run's output kept in the history

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.I)
_CLOCK = re.compile(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 1}


class ScheduleError(Exception):
    pass


def parse_every(text) -> int:
    """'5m' -> 300. Bare numbers are seconds."""
    m = _DURATION.match(str(text))
    if not m:
        raise ScheduleError(
            f"cannot read interval {text!r} -- use a number and a unit, e.g. 30s, 5m, 2h, 1d"
        )
    seconds = int(m.group(1)) * _UNITS[m.group(2).lower()]
    if seconds < MIN_INTERVAL:
        raise ScheduleError(f"interval too short: minimum is {MIN_INTERVAL}s")
    return seconds


def parse_at(text) -> str:
    """'9:05' -> '09:05'. Local wall-clock time, because that is what a user means."""
    m = _CLOCK.match(str(text))
    if not m:
        raise ScheduleError(f"cannot read time {text!r} -- use HH:MM, e.g. 09:30")
    h, minute = int(m.group(1)), int(m.group(2))
    if h > 23 or minute > 59:
        raise ScheduleError(f"not a real time: {text}")
    return f"{h:02d}:{minute:02d}"


def describe(job: "Job") -> str:
    if job.at:
        return f"daily at {job.at}"
    return f"every {_pretty(job.every)}"


def _pretty(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@dataclass
class Job:
    id: str
    app: str
    args: list = field(default_factory=list)
    every: int = 0          # seconds; 0 when this is a daily 'at' job
    at: str = ""            # "HH:MM" local; empty when this is an interval job
    enabled: bool = True
    created: str = ""
    next_run: float = 0.0
    last_run: float = 0.0
    last_code: int | None = None
    last_output: str = ""
    runs: int = 0
    failures: int = 0

    def due(self, now: float) -> bool:
        return self.enabled and self.next_run <= now


def next_run_after(job: Job, now: float) -> float:
    """When this job should next fire, counted forward from now.

    Never from the slot it missed -- see the module docstring on catch-up.
    """
    if job.at:
        h, m = (int(x) for x in job.at.split(":"))
        t = datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
        if t.timestamp() <= now:
            t += timedelta(days=1)
        return t.timestamp()
    return now + max(job.every, MIN_INTERVAL)


class Schedule:
    """The job list, persisted as one JSON file inside the root."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.DATA / "schedule.json"
        self.jobs: list[Job] = []
        self.load()

    # --- persistence ---------------------------------------------------------

    def load(self) -> None:
        self.jobs = []
        if not self.path.exists():
            return
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # a corrupt schedule must not stop the OS from booting
        for row in blob.get("jobs", []):
            try:
                self.jobs.append(Job(**{k: v for k, v in row.items() if k in Job.__annotations__}))
            except TypeError:
                continue

    def save(self) -> None:
        """Write atomically: the daemon and the shell both hold this file, and a
        half-written schedule read at boot would silently lose every job."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"jobs": [asdict(j) for j in self.jobs]}, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # --- reading -------------------------------------------------------------

    def all(self) -> list[Job]:
        return list(self.jobs)

    def get(self, job_id: str) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def due(self, now: float | None = None) -> list[Job]:
        now = time.time() if now is None else now
        return [j for j in self.jobs if j.due(now)]

    # --- writing -------------------------------------------------------------

    def add(self, app: str, args: list | None = None, every=None, at=None,
            now: float | None = None) -> Job:
        now = time.time() if now is None else now
        if (every is None) == (at is None):
            raise ScheduleError("give exactly one of every (an interval) or at (a daily time)")

        job = Job(
            id=self._next_id(app),
            app=app,
            args=[str(a) for a in (args or [])],
            every=parse_every(every) if every is not None else 0,
            at=parse_at(at) if at is not None else "",
            created=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        )
        job.next_run = next_run_after(job, now)
        self.jobs.append(job)
        self.save()
        return job

    def _next_id(self, app: str) -> str:
        taken = {j.id for j in self.jobs}
        if app not in taken:
            return app
        n = 2
        while f"{app}-{n}" in taken:
            n += 1
        return f"{app}-{n}"

    def remove(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j.id != job_id]
        if len(self.jobs) == before:
            return False
        self.save()
        return True

    def set_enabled(self, job_id: str, enabled: bool, now: float | None = None) -> Job | None:
        job = self.get(job_id)
        if not job:
            return None
        job.enabled = enabled
        if enabled:
            # Re-enabling starts the clock again rather than firing immediately
            # for every interval that elapsed while it was off.
            job.next_run = next_run_after(job, time.time() if now is None else now)
        self.save()
        return job

    def record(self, job: Job, result: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        job.last_run = now
        job.last_code = result.get("code")
        out = (result.get("stdout") or "").strip() or (result.get("stderr") or "").strip()
        job.last_output = out[:MAX_OUTPUT]
        job.runs += 1
        if result.get("code") != 0:
            job.failures += 1
        job.next_run = next_run_after(job, now)
        self.save()


# --- running ------------------------------------------------------------------


class Runner:
    """Runs due jobs. Separated from the daemon loop so it can be tested without
    sleeping, and so a shell can trigger a job by hand through the same path."""

    def __init__(self, registry, schedule: Schedule, secrets: dict | None = None,
                 log_path: Path | None = None, timeout: int = 120):
        self.registry = registry
        self.schedule = schedule
        self.secrets = secrets or {}
        self.timeout = timeout
        self.log_path = Path(log_path) if log_path else paths.LOGS / "schedule.jsonl"

    def run_job(self, job: Job, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        try:
            result = self.registry.run(
                job.app, job.args, secrets=self.secrets, timeout=self.timeout
            )
        except Exception as e:
            # A job whose app was uninstalled must not take the daemon down with
            # it; it fails, it is counted, and the next one still runs.
            result = {"ok": False, "code": -1, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}
        self.schedule.record(job, result, now)
        self._log(job, result, now)
        return result

    def tick(self, now: float | None = None) -> list[tuple[Job, dict]]:
        """Run everything currently due. Returns what ran."""
        now = time.time() if now is None else now
        return [(job, self.run_job(job, now)) for job in self.schedule.due(now)]

    def _log(self, job: Job, result: dict, now: float) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "t": now,
                    "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                    "job": job.id,
                    "app": job.app,
                    "args": job.args,
                    "code": result.get("code"),
                    "stdout": (result.get("stdout") or "")[:MAX_OUTPUT],
                    "stderr": (result.get("stderr") or "")[:MAX_OUTPUT],
                }) + "\n")
        except OSError:
            pass  # history is useful, not load-bearing


def history(limit: int = 20, log_path: Path | None = None) -> list[dict]:
    """The most recent scheduled runs, newest last."""
    path = Path(log_path) if log_path else paths.LOGS / "schedule.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-limit * 2:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


# --- the daemon ---------------------------------------------------------------


def pidfile(path: Path | None = None) -> Path:
    return Path(path) if path else paths.DATA / "schedd.pid"


def running_pid(path: Path | None = None) -> int | None:
    """The daemon's pid, or None. Verifies the process actually exists, so a
    pidfile left behind by a hard kill does not read as a running daemon."""
    p = pidfile(path)
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def stop(path: Path | None = None) -> bool:
    pid = running_pid(path)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def serve(registry=None, secrets: dict | None = None, tick: int = TICK,
          max_ticks: int | None = None, on_run=None) -> int:
    """The scheduling daemon.

    Long-lived and dull on purpose: wake, run what is due, sleep. The schedule is
    re-read every tick so jobs added from a running shell are picked up without a
    restart -- the two processes share only that one file.

    max_ticks exists so the loop is testable without waiting on wall-clock time.
    """
    from . import apps  # local import: the daemon is optional, the kernel is not

    paths.ensure()
    registry = registry if registry is not None else apps.Registry()
    schedule = Schedule()
    runner = Runner(registry, schedule, secrets=secrets or dict(os.environ))

    pid_path = pidfile()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    stopping = {"now": False}

    def handle(signum, frame):
        stopping["now"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle)
        except (ValueError, OSError):
            pass  # not the main thread, or a platform without it

    ticks = 0
    try:
        while not stopping["now"]:
            schedule.load()  # pick up jobs added by a running shell
            for job, result in runner.tick():
                if on_run:
                    on_run(job, result)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            for _ in range(tick):  # sleep in one-second slices so SIGTERM is prompt
                if stopping["now"]:
                    break
                time.sleep(1)
    finally:
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()

        except OSError:
            pass
    return 0
