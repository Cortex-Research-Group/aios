"""Scheduling tests.

Two things here actually matter and the rest is bookkeeping:

  * the confinement -- a job can only run an installed app, through the same
    path and the same secret rules as running it by hand, and
  * the catch-up rule -- a daemon that was off must not fire a 5m job three
    hundred times when it comes back.

Everything else (parsing, persistence, counters) is tested because it is cheap,
not because it is subtle.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="aios-test-")
os.environ.setdefault("AIOS_HOME", _TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "system"))

from kernel import apps, paths, schedule, syscalls  # noqa: E402


class Ctx:
    """Syscall context with a scriptable gate, scoped to a fresh root."""

    def __init__(self, root: Path, allow=True):
        self.registry = apps.Registry(root=root / "apps")
        self.schedule = schedule.Schedule(root / "schedule.json")
        self.secrets = {"OPENROUTER_API_KEY": "sk-secret", "OTHER_KEY": "other"}
        self.model = "test/model"
        self._allow = allow
        self.asked = []

    def allow(self, name, args):
        self.asked.append(name)
        return self._allow

    def emit(self, *a, **k):
        pass


def _root() -> Path:
    d = Path(tempfile.mkdtemp(prefix="aios-sched-"))
    (d / "apps").mkdir()
    return d


def _install(registry, name="hello", code="print('hi')\n", caps=None, secrets=None):
    return registry.install(name=name, description="d", spec="s", code=code,
                            caps=caps or [], secrets=secrets or [])


# --- parsing ------------------------------------------------------------------


class TestParsing(unittest.TestCase):
    def test_durations(self):
        for text, seconds in (("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400),
                              (" 90 ", 90), ("10M", 600)):
            self.assertEqual(schedule.parse_every(text), seconds, text)

    def test_interval_floor_is_enforced(self):
        """A 1s job is a busy loop, not a schedule."""
        with self.assertRaises(schedule.ScheduleError):
            schedule.parse_every("1s")

    def test_garbage_intervals_rejected(self):
        for text in ("soon", "", "5 minutes", "-5m", "m"):
            with self.assertRaises(schedule.ScheduleError):
                schedule.parse_every(text)

    def test_clock_times(self):
        self.assertEqual(schedule.parse_at("9:05"), "09:05")
        self.assertEqual(schedule.parse_at("23:59"), "23:59")

    def test_impossible_times_rejected(self):
        for text in ("24:00", "12:60", "noon", "9"):
            with self.assertRaises(schedule.ScheduleError):
                schedule.parse_at(text)

    def test_describe_is_readable(self):
        s = schedule.Schedule(_root() / "s.json")
        self.assertEqual(schedule.describe(s.add("a", every="5m")), "every 5m")
        self.assertEqual(schedule.describe(s.add("b", at="09:30")), "daily at 09:30")


# --- the clock ----------------------------------------------------------------


class TestNextRun(unittest.TestCase):
    def setUp(self):
        self.store = schedule.Schedule(_root() / "s.json")

    def test_interval_counts_forward_from_now(self):
        now = time.time()
        job = self.store.add("a", every="5m", now=now)
        self.assertAlmostEqual(job.next_run, now + 300, delta=1)

    def test_a_long_outage_does_not_stampede(self):
        """The whole point of computing forward from now.

        A 5m job whose daemon was down for a day should resume with one run, not
        with the 288 it slept through.
        """
        now = time.time()
        job = self.store.add("a", every="5m", now=now)
        job.next_run = now - 86400  # daemon was off for a day

        self.assertEqual(len(self.store.due(now)), 1)
        self.store.record(job, {"code": 0, "stdout": "ok"}, now)
        self.assertEqual(self.store.due(now), [], "a missed schedule was replayed")
        self.assertAlmostEqual(job.next_run, now + 300, delta=1)

    def test_daily_time_lands_on_that_time(self):
        job = self.store.add("a", at="09:30")
        t = time.localtime(job.next_run)
        self.assertEqual((t.tm_hour, t.tm_min), (9, 30))

    def test_daily_time_is_always_in_the_future(self):
        for hh in range(0, 24, 3):
            job = self.store.add(f"a{hh}", at=f"{hh:02d}:00")
            self.assertGreater(job.next_run, time.time())

    def test_daily_job_advances_by_a_day(self):
        job = self.store.add("a", at="09:30")
        first = job.next_run
        self.store.record(job, {"code": 0}, first)
        self.assertAlmostEqual(job.next_run - first, 86400, delta=3601)  # DST

    def test_exactly_one_of_every_or_at(self):
        for kwargs in ({}, {"every": "5m", "at": "09:00"}):
            with self.assertRaises(schedule.ScheduleError):
                self.store.add("a", **kwargs)


# --- the store ----------------------------------------------------------------


class TestStore(unittest.TestCase):
    def setUp(self):
        self.path = _root() / "s.json"
        self.store = schedule.Schedule(self.path)

    def test_round_trip(self):
        self.store.add("hello", ["--now"], every="5m")
        reloaded = schedule.Schedule(self.path)
        self.assertEqual(len(reloaded.all()), 1)
        self.assertEqual(reloaded.all()[0].args, ["--now"])
        self.assertEqual(reloaded.all()[0].every, 300)

    def test_ids_do_not_collide(self):
        ids = [self.store.add("hello", every="5m").id for _ in range(3)]
        self.assertEqual(ids, ["hello", "hello-2", "hello-3"])
        self.assertEqual(len(set(ids)), 3)

    def test_remove(self):
        job = self.store.add("hello", every="5m")
        self.assertTrue(self.store.remove(job.id))
        self.assertFalse(self.store.remove(job.id))
        self.assertEqual(schedule.Schedule(self.path).all(), [])

    def test_disabled_jobs_are_not_due(self):
        job = self.store.add("hello", every="5m")
        job.next_run = time.time() - 1
        self.assertEqual(len(self.store.due()), 1)
        self.store.set_enabled(job.id, False)
        self.assertEqual(self.store.due(), [])

    def test_reenabling_does_not_fire_immediately(self):
        """Otherwise turning a job back on replays every interval it missed."""
        job = self.store.add("hello", every="5m")
        self.store.set_enabled(job.id, False)
        job.next_run = time.time() - 86400
        self.store.set_enabled(job.id, True)
        self.assertEqual(self.store.due(), [])

    def test_corrupt_file_does_not_break_boot(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(schedule.Schedule(self.path).all(), [])

    def test_unknown_fields_are_ignored(self):
        """A schedule written by a future version must still load."""
        self.path.write_text(json.dumps({"jobs": [
            {"id": "x", "app": "hello", "every": 300, "invented_later": True}
        ]}), encoding="utf-8")
        self.assertEqual(len(schedule.Schedule(self.path).all()), 1)

    def test_save_leaves_no_temp_file(self):
        self.store.add("hello", every="5m")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


# --- running ------------------------------------------------------------------


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.root = _root()
        self.registry = apps.Registry(root=self.root / "apps")
        self.store = schedule.Schedule(self.root / "s.json")
        self.log = self.root / "schedule.jsonl"

    def _runner(self, secrets=None):
        return schedule.Runner(self.registry, self.store, secrets=secrets or {},
                               log_path=self.log)

    def test_a_due_job_runs_and_records(self):
        _install(self.registry, "hello", "print('hi')\n")
        job = self.store.add("hello", every="5m")
        job.next_run = time.time() - 1

        ran = self._runner().tick()
        self.assertEqual(len(ran), 1)
        self.assertEqual(job.runs, 1)
        self.assertEqual(job.failures, 0)
        self.assertEqual(job.last_code, 0)
        self.assertEqual(job.last_output, "hi")

    def test_a_job_that_is_not_due_does_not_run(self):
        _install(self.registry, "hello")
        self.store.add("hello", every="5m")
        self.assertEqual(self._runner().tick(), [])

    def test_failures_are_counted_not_raised(self):
        _install(self.registry, "boom", "import sys\nsys.exit(3)\n")
        job = self.store.add("boom", every="5m")
        job.next_run = time.time() - 1

        self._runner().tick()
        self.assertEqual(job.last_code, 3)
        self.assertEqual(job.failures, 1)

    def test_a_job_whose_app_was_uninstalled_does_not_kill_the_daemon(self):
        _install(self.registry, "hello")
        job = self.store.add("hello", every="5m")
        job.next_run = time.time() - 1
        self.registry.remove("hello")

        ran = self._runner().tick()  # must not raise
        self.assertEqual(len(ran), 1)
        self.assertEqual(job.failures, 1)
        self.assertIn("no such app", job.last_output)

    def test_one_bad_job_does_not_block_the_next(self):
        _install(self.registry, "hello")
        gone = self.store.add("ghost", every="5m")
        good = self.store.add("hello", every="5m")
        gone.next_run = good.next_run = time.time() - 1

        self._runner().tick()
        self.assertEqual(good.runs, 1)
        self.assertEqual(good.last_code, 0)

    def test_args_reach_the_app(self):
        _install(self.registry, "echo", "import sys\nprint(' '.join(sys.argv[1:]))\n")
        job = self.store.add("echo", ["a", "b"], every="5m")
        job.next_run = time.time() - 1

        self._runner().tick()
        self.assertEqual(job.last_output, "a b")

    def test_only_declared_secrets_reach_a_scheduled_app(self):
        """Scheduling must not widen what an app can read.

        The same rule as app_run: an app that never declared a key does not get
        it, whether a human triggered the run or the clock did.
        """
        code = "import os\nprint(os.environ.get('OPENROUTER_API_KEY', 'ABSENT'), os.environ.get('OTHER_KEY', 'ABSENT'))\n"
        _install(self.registry, "peek", code, secrets=["OPENROUTER_API_KEY"])
        job = self.store.add("peek", every="5m")
        job.next_run = time.time() - 1

        self._runner({"OPENROUTER_API_KEY": "sk-secret", "OTHER_KEY": "other"}).tick()
        self.assertEqual(job.last_output, "sk-secret ABSENT")

    def test_history_is_written(self):
        _install(self.registry, "hello")
        job = self.store.add("hello", every="5m")
        job.next_run = time.time() - 1
        self._runner().tick()

        rows = schedule.history(log_path=self.log)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job"], "hello")
        self.assertEqual(rows[0]["code"], 0)

    def test_history_survives_a_truncated_line(self):
        self.log.write_text('{"job": "a", "code": 0}\n{"job": "b", trunc', encoding="utf-8")
        self.assertEqual(len(schedule.history(log_path=self.log)), 1)


class TestDaemonLoop(unittest.TestCase):
    """The loop itself, bounded so it does not sleep in the test suite."""

    def test_serve_runs_due_work_and_cleans_up_its_pidfile(self):
        root = _root()
        registry = apps.Registry(root=root / "apps")
        _install(registry, "hello")

        store = schedule.Schedule()  # the real DATA path -- what serve() reads
        for j in store.all():
            store.remove(j.id)
        job = store.add("hello", every="5m")
        job.next_run = time.time() - 1
        store.save()

        seen = []
        schedule.serve(registry=registry, secrets={}, tick=0, max_ticks=1,
                       on_run=lambda j, r: seen.append((j.id, r["code"])))

        self.assertEqual(seen, [("hello", 0)])
        self.assertIsNone(schedule.running_pid(), "pidfile outlived the daemon")
        store.remove(job.id)

    def test_a_stale_pidfile_does_not_read_as_running(self):
        p = schedule.pidfile()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("999999", encoding="utf-8")  # a pid that does not exist
        try:
            self.assertIsNone(schedule.running_pid())
            self.assertFalse(schedule.stop())
        finally:
            p.unlink(missing_ok=True)

    def test_a_garbage_pidfile_does_not_read_as_running(self):
        p = schedule.pidfile()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not a pid", encoding="utf-8")
        try:
            self.assertIsNone(schedule.running_pid())
        finally:
            p.unlink(missing_ok=True)


# --- the syscalls -------------------------------------------------------------


class TestSchedSyscalls(unittest.TestCase):
    def setUp(self):
        self.root = _root()
        self.ctx = Ctx(self.root)
        _install(self.ctx.registry, "hello")

    def test_add_and_list(self):
        out = syscalls.dispatch("sched_add", {"app": "hello", "every": "5m"}, self.ctx)
        self.assertIn("scheduled 'hello' every 5m", out)
        self.assertIn("unattended", out)
        self.assertIn("hello", syscalls.dispatch("sched_list", {}, self.ctx))

    def test_scheduling_is_gated(self):
        """It is mutating: it must go through the permission gate."""
        ctx = Ctx(self.root, allow=False)
        _install(ctx.registry, "hello")
        self.assertTrue(syscalls.dispatch("sched_add", {"app": "hello", "every": "5m"}, ctx)
                        .startswith("denied"))
        self.assertEqual(ctx.schedule.all(), [])

        ctx._allow = True
        syscalls.dispatch("sched_add", {"app": "hello", "every": "5m"}, ctx)
        syscalls.dispatch("sched_remove", {"id": "hello"}, ctx)
        self.assertEqual(ctx.asked, ["sched_add", "sched_add", "sched_remove"])

    def test_listing_is_not_gated(self):
        syscalls.dispatch("sched_list", {}, self.ctx)
        self.assertEqual(self.ctx.asked, [])

    def test_cannot_schedule_an_app_that_does_not_exist(self):
        """The confinement, stated as a test: a job runs an installed app or
        nothing. There is no syscall that schedules a command."""
        out = syscalls.dispatch("sched_add", {"app": "not-installed", "every": "5m"}, self.ctx)
        self.assertTrue(out.startswith("error"))
        self.assertEqual(self.ctx.schedule.all(), [])

    def test_no_syscall_schedules_a_raw_command(self):
        for name, entry in syscalls.REGISTRY.items():
            if name.startswith("sched_"):
                props = set(entry["schema"].get("properties", {}))
                self.assertNotIn("command", props, f"{name} accepts a raw command")
                self.assertNotIn("code", props, f"{name} accepts raw code")

    def test_bad_interval_is_an_error_not_a_traceback(self):
        out = syscalls.dispatch("sched_add", {"app": "hello", "every": "whenever"}, self.ctx)
        self.assertTrue(out.startswith("error"), out)
        self.assertIn("interval", out)

    def test_missing_schedule_spec_is_rejected(self):
        out = syscalls.dispatch("sched_add", {"app": "hello"}, self.ctx)
        self.assertTrue(out.startswith("error"), out)

    def test_remove_reports_a_missing_job(self):
        self.assertTrue(
            syscalls.dispatch("sched_remove", {"id": "nope"}, self.ctx).startswith("error")
        )

    def test_removing_a_job_keeps_the_app(self):
        syscalls.dispatch("sched_add", {"app": "hello", "every": "5m"}, self.ctx)
        syscalls.dispatch("sched_remove", {"id": "hello"}, self.ctx)
        self.assertIsNotNone(self.ctx.registry.get("hello"))

    def test_empty_list(self):
        self.assertEqual(syscalls.dispatch("sched_list", {}, self.ctx), "no jobs scheduled")


if __name__ == "__main__":
    unittest.main()
