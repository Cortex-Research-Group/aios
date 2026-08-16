"""The JEPA-style world model.

Two things have to be true for this to be worth having rather than decorative:
the linear algebra must be correct, and the model must actually learn the OS's
dynamics from recorded transitions -- meaning it beats the trivial baseline of
"assume nothing ever changes". Both are tested here.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="aios-world-test-")
os.environ.setdefault("AIOS_HOME", _TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "system"))

from kernel import world  # noqa: E402


class TestLinearAlgebra(unittest.TestCase):
    def test_solve_matches_known_solution(self):
        # 2x + y = 5 ; x + 3y = 10  ->  x = 1, y = 3
        a = [[2.0, 1.0], [1.0, 3.0]]
        b = [[5.0], [10.0]]
        x = world._solve(a, b)
        self.assertAlmostEqual(x[0][0], 1.0, places=6)
        self.assertAlmostEqual(x[1][0], 3.0, places=6)

    def test_solve_identity(self):
        ident = [[1.0, 0.0], [0.0, 1.0]]
        b = [[7.0, 2.0], [-3.0, 4.0]]
        self.assertEqual(world._solve(ident, b), b)

    def test_solve_handles_multiple_columns(self):
        a = [[4.0, 0.0], [0.0, 2.0]]
        b = [[8.0, 4.0], [6.0, 2.0]]
        x = world._solve(a, b)
        self.assertAlmostEqual(x[0][0], 2.0, places=6)
        self.assertAlmostEqual(x[1][1], 1.0, places=6)


class TestEncoders(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="aios-enc-"))
        for d in ("apps", "memory", "data", "logs"):
            (self.root / d).mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_dimensions(self):
        self.assertEqual(len(world.encode_state(self.root)), world.D_STATE)
        self.assertEqual(len(world.encode_action("fs_read", {"path": "/x"})), world.D_ACTION)

    def test_state_is_deterministic(self):
        self.assertEqual(world.encode_state(self.root), world.encode_state(self.root))

    def test_state_moves_when_the_world_changes(self):
        before = world.encode_state(self.root)
        (self.root / "data" / "new.txt").write_text("hello" * 100)
        after = world.encode_state(self.root)
        self.assertNotEqual(before, after)
        self.assertGreater(after[3], before[3], "total file count should rise")
        self.assertGreater(after[4], before[4], "total bytes should rise")

    def test_apps_are_counted(self):
        before = world.encode_state(self.root)
        (self.root / "apps" / "thing").mkdir()
        (self.root / "apps" / "thing" / "main.py").write_text("print(1)")
        after = world.encode_state(self.root)
        self.assertGreater(after[0], before[0], "app count should rise")

    def test_vault_presence_is_flagged(self):
        self.assertEqual(world.encode_state(self.root)[7], 0.0)
        (self.root / "vault").mkdir()
        (self.root / "vault" / "keys.enc").write_bytes(b"sealed")
        self.assertEqual(world.encode_state(self.root)[7], 1.0)

    def test_action_distinguishes_arguments(self):
        """Same syscall, different intent: rm -rf must not embed like ls."""
        rm = world.encode_action("proc_run", {"command": "rm -rf /apps"})
        ls = world.encode_action("proc_run", {"command": "ls"})
        self.assertNotEqual(rm, ls)

    def test_action_is_deterministic(self):
        a = world.encode_action("fs_write", {"path": "/data/x", "content": "y"})
        b = world.encode_action("fs_write", {"path": "/data/x", "content": "y"})
        self.assertEqual(a, b)

    def test_unknown_syscall_still_encodes(self):
        """Hashing, not a fixed table: a syscall added later must not crash."""
        v = world.encode_action("some_future_syscall", {"x": 1})
        self.assertEqual(len(v), world.D_ACTION)
        self.assertTrue(any(v))


class TestLearning(unittest.TestCase):
    """The model must beat 'assume nothing changes', or it is not a model."""

    def _synthetic(self, n=200):
        """Dynamics with real structure: one action grows the world, one shrinks
        it, one does nothing at all."""
        import random

        rng = random.Random(7)
        grow = world.encode_action("app_build", {"name": "x", "code": "y"})
        shrink = world.encode_action("proc_run", {"command": "rm -rf apps"})
        noop = world.encode_action("fs_read", {"path": "/aios.json"})

        samples = []
        for _ in range(n):
            state = [rng.uniform(0, 1) for _ in range(world.D_STATE)]
            action, effect = rng.choice([(grow, 0.10), (shrink, -0.10), (noop, 0.0)])
            nxt = list(state)
            for i in (0, 1, 3, 4):
                nxt[i] += effect
            samples.append((state, action, nxt))
        return samples, grow, shrink, noop

    def test_fit_learns_the_dynamics(self):
        samples, *_ = self._synthetic()
        wm = world.WorldModel()
        stats = wm.fit(samples)
        self.assertEqual(stats["samples"], 200)
        self.assertLess(
            stats["vs_static"], 0.25,
            f"model barely beat the do-nothing baseline: {stats['vs_static']:.3f}",
        )

    def test_predictions_have_the_right_sign(self):
        samples, grow, shrink, noop = self._synthetic()
        wm = world.WorldModel()
        wm.fit(samples)

        state = [0.5] * world.D_STATE
        self.assertGreater(wm.predict(state, grow)[0], state[0], "growth not predicted")
        self.assertLess(wm.predict(state, shrink)[0], state[0], "shrinkage not predicted")

    def test_destructive_action_scores_higher_disturbance_than_a_read(self):
        """The whole point: rank actions by predicted impact before running them."""
        samples, grow, shrink, noop = self._synthetic()
        wm = world.WorldModel()
        wm.fit(samples)

        state = [0.5] * world.D_STATE
        self.assertGreater(wm.disturbance(state, shrink), wm.disturbance(state, noop))
        self.assertGreater(wm.disturbance(state, grow), wm.disturbance(state, noop))

    def test_explain_names_the_changed_dimensions(self):
        samples, grow, *_ = self._synthetic()
        wm = world.WorldModel()
        wm.fit(samples)
        report = wm.explain([0.5] * world.D_STATE, grow)
        self.assertIn("app count", report["predicted_changes"])
        self.assertGreater(report["disturbance"], 0)
        self.assertTrue(report["reliable"])

    def test_untrained_model_predicts_no_change(self):
        wm = world.WorldModel()
        state = [0.3] * world.D_STATE
        self.assertEqual(wm.predict(state, world.encode_action("fs_write", {})), state)
        self.assertEqual(wm.disturbance(state, world.encode_action("fs_write", {})), 0.0)
        self.assertFalse(wm.is_reliable)

    def test_too_few_samples_is_flagged_unreliable(self):
        samples, *_ = self._synthetic(n=5)
        wm = world.WorldModel()
        wm.fit(samples)
        self.assertTrue(wm.is_trained)
        self.assertFalse(wm.is_reliable, "5 samples must not be presented as trustworthy")

    def test_empty_training_set_refused(self):
        with self.assertRaises(ValueError):
            world.WorldModel().fit([])

    def test_destructive_does_not_depend_on_scale(self):
        """destructive must be direction alone, not direction gated by magnitude.

        Built so most training removals are large (effect -0.10) and `scale`
        -- the median disturbance -- lands close to that. A small removal
        (-0.002, twenty times smaller) still needs to be flagged: losing one
        file is destructive regardless of whether it is typical for this
        machine. This is what regressed before: the gate used to require
        d >= scale, so anything below the *typical* removal size passed
        silently, and that included ordinary single-file deletions.
        """
        import random

        rng = random.Random(11)
        grow = world.encode_action("app_build", {"name": "x", "code": "y"})
        big_shrink = world.encode_action("proc_run", {"command": "rm -rf apps"})
        small_shrink = world.encode_action("proc_run", {"command": "rm data/f1.txt"})
        noop = world.encode_action("fs_read", {"path": "/aios.json"})

        samples = []
        for _ in range(300):
            state = [rng.uniform(0, 1) for _ in range(world.D_STATE)]
            action, effect = rng.choice(
                [(grow, 0.10), (big_shrink, -0.10), (small_shrink, -0.005), (noop, 0.0)]
            )
            nxt = list(state)
            for i in (0, 1, 3, 4):
                nxt[i] += effect
            samples.append((state, action, nxt))

        wm = world.WorldModel()
        wm.fit(samples)
        state = [0.5] * world.D_STATE

        big = wm.explain(state, big_shrink)
        small = wm.explain(state, small_shrink)
        self.assertLess(small["disturbance"], wm.scale, "test setup: small removal should be sub-median")
        self.assertTrue(big["destructive"])
        self.assertTrue(small["destructive"], "a below-median removal must still be flagged")


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="aios-world-save-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_round_trip(self):
        samples = [
            ([0.1] * world.D_STATE, world.encode_action("fs_write", {"path": "a"}), [0.2] * world.D_STATE)
            for _ in range(50)
        ]
        wm = world.WorldModel()
        wm.fit(samples)
        path = wm.save(self.dir / "world.json")

        loaded = world.WorldModel.load(path)
        self.assertTrue(loaded.is_trained)
        self.assertEqual(loaded.trained_on, 50)
        state = [0.1] * world.D_STATE
        action = world.encode_action("fs_write", {"path": "a"})
        for a, b in zip(wm.predict(state, action), loaded.predict(state, action)):
            self.assertAlmostEqual(a, b, places=9)

    def test_missing_file_yields_untrained_model(self):
        self.assertFalse(world.WorldModel.load(self.dir / "nope.json").is_trained)

    def test_corrupt_file_yields_untrained_model(self):
        p = self.dir / "world.json"
        p.write_text("{not json")
        self.assertFalse(world.WorldModel.load(p).is_trained)

    def test_dimension_change_invalidates_the_model(self):
        """Weights trained under a different encoder are meaningless, not merely stale."""
        p = self.dir / "world.json"
        p.write_text(json.dumps({"weights": [[1.0]], "trained_on": 99, "d_state": 4, "d_action": 4}))
        self.assertFalse(world.WorldModel.load(p).is_trained)

    def test_layout_change_invalidates_the_model(self):
        """The dimensions are not enough to tell a stale model from a current one.

        Adding syscalls moves where the argument features sit inside the action
        vector while D_ACTION stays put, so a model trained before the move would
        load cleanly and mean something different in every slot. Retraining is
        cheap; a silently misread predictor gating deletions is not.
        """
        p = self.dir / "world.json"
        p.write_text(json.dumps({
            "weights": [[0.0] * (world.D_STATE + world.D_ACTION)] * world.D_STATE,
            "trained_on": 99,
            "d_state": world.D_STATE,
            "d_action": world.D_ACTION,
            "layout": world.LAYOUT - 1,
        }))
        self.assertFalse(world.WorldModel.load(p).is_trained)

    def test_a_model_saved_now_declares_the_current_layout(self):
        wm = world.WorldModel()
        wm.fit([([0.1] * world.D_STATE, world.encode_action("fs_write", {"path": "a"}),
                 [0.2] * world.D_STATE)] * 50)
        blob = json.loads(wm.save(self.dir / "world.json").read_text())
        self.assertEqual(blob["layout"], world.LAYOUT)


class TestTransitionLog(unittest.TestCase):
    def setUp(self):
        self.logs = Path(tempfile.mkdtemp(prefix="aios-world-logs-"))

    def tearDown(self):
        shutil.rmtree(self.logs, ignore_errors=True)

    def _write(self, entries):
        with (self.logs / "session-20260815-1.jsonl").open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_reads_transitions_and_ignores_chatter(self):
        s = [0.0] * world.D_STATE
        a = [0.0] * world.D_ACTION
        self._write([
            {"role": "user", "content": "hi"},
            {"role": "transition", "action": "fs_write", "s": s, "a": a, "s2": s},
            {"role": "assistant", "content": "ok"},
            {"role": "transition", "action": "fs_read", "s": s, "a": a, "s2": s},
        ])
        self.assertEqual(len(world.load_transitions(self.logs)), 2)

    def test_malformed_lines_are_skipped(self):
        (self.logs / "session-bad.jsonl").write_text("{not json\n")
        self.assertEqual(world.load_transitions(self.logs), [])

    def test_wrong_dimensions_are_rejected(self):
        self._write([{"role": "transition", "action": "x", "s": [0.0], "a": [0.0], "s2": [0.0]}])
        self.assertEqual(world.load_transitions(self.logs), [])

    def test_missing_directory(self):
        self.assertEqual(world.load_transitions(self.logs / "nope"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRealDynamics(unittest.TestCase):
    """Synthetic data proves the maths. This proves the premise: that the model
    can learn what aiOS syscalls actually do, from watching them happen."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="aios-real-"))
        for d in ("apps", "memory", "data", "logs"):
            (self.root / d).mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _collect(self):
        """Drive the filesystem the way the syscalls do, recording transitions."""
        samples = []

        def step(action_name, action_args, mutate):
            before = world.encode_state(self.root)
            mutate()
            after = world.encode_state(self.root)
            samples.append((before, world.encode_action(action_name, action_args), after))

        for i in range(40):
            # writing a file grows the world
            step("fs_write", {"path": f"/data/f{i}.txt", "content": "x"},
                 lambda i=i: (self.root / "data" / f"f{i}.txt").write_text("x" * 500))
            # reading changes nothing
            step("fs_read", {"path": f"/data/f{i}.txt"}, lambda: None)
            # installing an app grows it more
            def install(i=i):
                d = self.root / "apps" / f"app{i}"
                d.mkdir(exist_ok=True)
                (d / "main.py").write_text("print(1)\n" * 20)
                (d / "manifest.json").write_text("{}")
            step("app_build", {"name": f"app{i}", "code": "print(1)"}, install)
            # listing changes nothing
            step("fs_list", {"path": "/"}, lambda: None)

        return samples

    def test_model_learns_which_syscalls_change_the_world(self):
        samples = self._collect()
        wm = world.WorldModel()
        stats = wm.fit(samples)

        self.assertGreaterEqual(stats["samples"], 160)
        self.assertLess(stats["vs_static"], 1.0,
                        "model did not beat assuming the world never changes")

        state = world.encode_state(self.root)
        write = wm.disturbance(state, world.encode_action("fs_write", {"path": "/data/z.txt", "content": "x"}))
        build = wm.disturbance(state, world.encode_action("app_build", {"name": "z", "code": "print(1)"}))
        read = wm.disturbance(state, world.encode_action("fs_read", {"path": "/data/z.txt"}))
        listing = wm.disturbance(state, world.encode_action("fs_list", {"path": "/"}))

        # Learned purely from observation: mutating calls disturb the world,
        # read-only calls do not.
        # Not merely greater: usefully separated, or the signal is not actionable.
        self.assertGreater(write, read * 3, f"write {write:.4f} vs read {read:.4f}")
        self.assertGreater(build, listing * 3, f"build {build:.4f} vs list {listing:.4f}")

    def test_readonly_syscalls_predict_near_zero_disturbance(self):
        wm = world.WorldModel()
        wm.fit(self._collect())
        state = world.encode_state(self.root)
        for name, args in (("fs_read", {"path": "/x"}), ("fs_list", {"path": "/"})):
            self.assertLess(wm.disturbance(state, world.encode_action(name, args)), 0.05,
                            f"{name} should be predicted as near-harmless")


class TestSyscallIdentity(unittest.TestCase):
    """The action encoder must keep giving every syscall its own slot.

    Hashing 12 names into 12 buckets collided (fs_write / mem_write / app_build
    shared one), which made those actions indistinguishable to the model. This
    guards against both drift and a return of the collision.
    """

    def test_known_syscalls_covers_the_registry(self):
        from kernel import syscalls

        missing = set(syscalls.REGISTRY) - set(world.KNOWN_SYSCALLS)
        self.assertFalse(missing, f"syscalls absent from world.KNOWN_SYSCALLS: {sorted(missing)}")

    def test_no_stale_entries(self):
        from kernel import syscalls

        stale = set(world.KNOWN_SYSCALLS) - set(syscalls.REGISTRY)
        self.assertFalse(stale, f"world.KNOWN_SYSCALLS lists syscalls that no longer exist: {sorted(stale)}")

    def test_every_syscall_gets_a_distinct_slot(self):
        from kernel import syscalls

        slots = [world.encode_action(n).index(1.0) for n in syscalls.REGISTRY]
        self.assertEqual(len(set(slots)), len(slots), "two syscalls share an identity slot")

    def test_unknown_syscall_does_not_steal_a_known_slot(self):
        known = {world.encode_action(n).index(1.0) for n in world.KNOWN_SYSCALLS}
        for name in ("future_call", "gpu_alloc", "zzz"):
            self.assertNotIn(world.encode_action(name).index(1.0), known)


class TestEncodingIsLearnable(unittest.TestCase):
    """A regression guard on the encoder choice itself.

    log1p scaling made an action's effect depend on the state multiplicatively,
    which an additive linear predictor cannot represent -- measured, it could not
    separate a write from a read at all. Linear counts fixed it. If someone
    reintroduces a log here, this fails.
    """

    def test_equal_increments_produce_equal_deltas(self):
        root = Path(tempfile.mkdtemp(prefix="aios-lin-"))
        try:
            for d in ("apps", "memory", "data", "logs"):
                (root / d).mkdir(parents=True)

            def add_file(i):
                (root / "data" / f"f{i}.txt").write_text("x" * 100)

            add_file(0)
            early = world.encode_state(root)[3]
            add_file(1)
            early_delta = world.encode_state(root)[3] - early

            for i in range(2, 60):
                add_file(i)
            late = world.encode_state(root)[3]
            add_file(60)
            late_delta = world.encode_state(root)[3] - late

            self.assertAlmostEqual(
                early_delta, late_delta, places=6,
                msg="adding one file must move the embedding the same amount "
                    "regardless of how full the world already is",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestBootstrapAndDestruction(unittest.TestCase):
    """The world model exists to warn before an irreversible syscall runs.

    A model trained only on journalled usage never sees a deletion -- measured,
    it predicted `rm -rf /apps` would *increase* the app count. bootstrap()
    fixes that by practising in a throwaway root, destruction included.
    """

    @classmethod
    def setUpClass(cls):
        cls.wm = world.WorldModel()
        cls.stats = cls.wm.fit(world.bootstrap(20))
        cls.root = Path(tempfile.mkdtemp(prefix="aios-destr-"))
        for d in ("apps", "memory", "data", "logs"):
            (cls.root / d).mkdir(parents=True)
        for i in range(15):
            (cls.root / "data" / f"f{i}.txt").write_text("x" * 400)
            a = cls.root / "apps" / f"app{i}"
            a.mkdir()
            (a / "main.py").write_text("print(1)\n" * 10)
            (a / "manifest.json").write_text("{}")
        cls.state = world.encode_state(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def explain(self, name, args):
        return self.wm.explain(self.state, world.encode_action(name, args))

    def test_bootstrap_beats_the_static_baseline(self):
        self.assertLess(self.stats["vs_static"], 0.7)
        self.assertGreater(self.stats["samples"], 100)

    def test_deletion_predicts_removal(self):
        for cmd in ("rm -rf apps", "rm -rf memory", "rm data/f1.txt"):
            r = self.explain("proc_run", {"command": cmd})
            self.assertEqual(r["direction"], "removes", f"{cmd!r} not seen as removal")
            self.assertTrue(r["destructive"], f"{cmd!r} not flagged destructive")

    def test_deleting_apps_predicts_fewer_apps(self):
        r = self.explain("proc_run", {"command": "rm -rf apps"})
        self.assertLess(r["predicted_changes"].get("app count", 0), 0)

    def test_unseen_benign_commands_are_not_flagged_destructive(self):
        """A read-only command the model never saw verbatim must not inherit a
        deletion's score just for being short.

        Regression: the action encoder used to feature the raw command length.
        Only 'ls -la' was ever in the training set, and destructive commands
        (rm -rf apps/bootN, rm memory/note-N.md) happened to be longer, so ridge
        regression leaned on length as a proxy for danger. Bare `ls` -- never
        seen in training, and shorter than every trained example -- came out
        predicted disturbance 0.0194 against a scale of 0.0145: flagged
        destructive for listing a directory. None of these commands touch the
        filesystem at all; they must predict exactly zero disturbance.
        """
        for cmd in ("ls", "ls -l", "pwd", "whoami", "date", "git status",
                   "python3 --version", "df -h", "curl -O https://example.com",
                   "tar -xvf x.tar", "grep -r foo .", "npm --force-color"):
            r = self.explain("proc_run", {"command": cmd})
            self.assertFalse(r["destructive"], f"{cmd!r} wrongly flagged destructive")
            self.assertEqual(r["disturbance"], 0.0, f"{cmd!r} should predict zero disturbance")

    def test_single_file_deletion_is_flagged_destructive(self):
        """The magnitude gate that used to sit on top of direction ('removes')
        was the *median* disturbance across every effectful training action --
        which, by construction, misses half of them. An ordinary single-file
        `rm` landed almost exactly on that median and was silently let through
        without a warning. Losing one file to an unattended `rm` is exactly the
        case a permission prompt exists for.
        """
        r = self.explain("proc_run", {"command": "rm data/f1.txt"})
        self.assertEqual(r["direction"], "removes")
        self.assertTrue(r["destructive"])

    def test_creation_is_not_flagged_destructive(self):
        for name, args in (
            ("fs_write", {"path": "/data/n.txt", "content": "x" * 500}),
            ("mem_write", {"title": "t", "content": "c"}),
            ("app_build", {"name": "t", "description": "d", "spec": "s", "code": "print(1)"}),
        ):
            r = self.explain(name, args)
            self.assertFalse(r["destructive"], f"{name} wrongly flagged destructive")
            self.assertEqual(r["direction"], "creates")

    def test_scheduling_is_learned_as_quiet(self):
        """Scheduling touches one small file in data/ and nothing else.

        The model should say so rather than never having seen these syscalls.
        What makes a job worth a second look -- that it then runs unattended,
        forever -- is not visible in the filesystem at all, so the permission
        prompt states it in words instead of leaning on this prediction.
        """
        for name, args in (
            ("sched_add", {"app": "app1", "every": "5m"}),
            ("sched_remove", {"id": "app1"}),
            ("sched_list", {}),
        ):
            r = self.explain(name, args)
            self.assertFalse(r["destructive"], f"{name} wrongly flagged destructive")
            self.assertLess(r["disturbance"], self.stats["scale"],
                            f"{name} should disturb less than an ordinary action")

    def test_scheduling_is_distinguishable_from_deleting_apps(self):
        """sched_remove unschedules a job; it does not uninstall anything. If the
        two ever encode alike, the prompt would warn about the wrong thing."""
        quiet = self.explain("sched_remove", {"id": "app1"})
        loud = self.explain("proc_run", {"command": "rm -rf apps"})
        self.assertLess(quiet["disturbance"], loud["disturbance"] / 10)
        self.assertFalse(quiet["destructive"])
        self.assertTrue(loud["destructive"])

    def test_app_build_predicts_more_apps(self):
        r = self.explain("app_build", {"name": "t", "description": "d", "spec": "s", "code": "print(1)"})
        self.assertGreater(r["predicted_changes"].get("app count", 0), 0)

    def test_readonly_syscalls_predict_exactly_nothing(self):
        """Including ones absent from the training set: an unseen syscall must
        not inherit another's predicted effect."""
        for name, args in (
            ("fs_list", {"path": "/"}),
            ("fs_read", {"path": "/data/f1.txt"}),
            ("app_list", {}),
            ("mem_search", {"query": "x"}),
            ("app_source", {"name": "app1"}),
            ("web_search", {"query": "anything"}),
        ):
            r = self.explain(name, args)
            self.assertEqual(r["disturbance"], 0.0, f"{name} predicted a nonzero effect")
            self.assertEqual(r["direction"], "none")

    def test_destruction_outranks_a_harmless_shell_command(self):
        harmful = self.explain("proc_run", {"command": "rm -rf apps"})["disturbance"]
        harmless = self.explain("proc_run", {"command": "ls -la"})["disturbance"]
        self.assertGreater(harmful, harmless * 3,
                           f"rm {harmful:.4f} barely above ls {harmless:.4f}")

    def test_harmless_shell_command_is_not_flagged(self):
        for cmd in ("ls -la", "echo hello", "cat aios.json"):
            self.assertFalse(self.explain("proc_run", {"command": cmd})["destructive"],
                             f"{cmd!r} wrongly flagged destructive")
