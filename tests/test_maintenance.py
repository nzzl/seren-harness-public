"""Supervised maintenance sessions with stand-ins only: disposable repositories, fake
`claude`, fake usage readings and the real sandbox profiles. No model, network or live call."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from seren_harness import claude_budget, claude_live, discovery, maintenance, model_worker
from test_model_worker import AUTHORITY, INIT, MACOS_SANDBOX, OK, fake_claude

SOURCE = Path(__file__).resolve().parents[1]
RUNNER = SOURCE / ".tiny-harness/run.py"
PLAN = "docs/maintenance/plan.md"
GOAL = "consolidated-goal"
FRESH = "2099-01-01T00:00:00+00:00"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], check=True, capture_output=True, text=True).stdout


def repo_with_plan():
    repo = Path(tempfile.mkdtemp())
    git(repo, "init", "-q")
    for path in (PLAN, "tests/run.py", ".tiny-harness/run.py", ".tiny-harness/checks.json",
                 "seren_harness/model_worker.py", "other.txt"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def home_for(repo, **limits):
    home = discovery.Home(tempfile.mkdtemp())
    value = json.loads(json.dumps(AUTHORITY))
    value["model_worker"] = {"enabled": True, "claude": "/bin/echo", "model": "m",
                             "repo": str(repo), "pinned_source": "/p", "contract_map": "/c",
                             "limits": dict(discovery.MODEL_LIMITS, **limits),
                             "subscription_budget": dict(claude_budget.POLICY)}
    discovery.write_json(home.root / "authority.json", value)
    return home


def usage(percent=10, extra=False):
    return {"five_hour": {"utilization": percent, "resets_at": FRESH},
            "seven_day": {"utilization": percent, "resets_at": FRESH},
            "extra_usage": {"is_enabled": extra}}


class PathTests(unittest.TestCase):
    def test_only_the_approved_goal_files_are_maintenance_paths(self):
        for path in ("tests/run.py", ".tiny-harness/run.py", ".tiny-harness/NARRATIVE.md",
                     "seren_harness/offline_evals.py", "tests/test_offline_evals.py",
                     "tests/fixtures/offline-evals/a.json", "docs/release/MANIFEST.md",
                     "docs/maintenance/plan.md", "requirements-dev.txt"):
            self.assertTrue(maintenance.owned(path), path)
        for path in (".tiny-harness/checks.json", ".tiny-harness/CONTRACT.md", "AGENTS.md",
                     ".tiny-harness/TASK.md", ".tiny-harness/NOTES.md",
                     "CLAUDE.md", "docs/MODEL-WORKER.md", "ruff.toml",
                     "seren_harness/maintenance.py", "seren_harness/model_worker.py",
                     "seren_harness/discovery.py", "seren_harness/discovery_launcher.py",
                     "seren_harness/claude_budget.py", "seren_harness/live_discovery.py",
                     "seren_harness/claude_live.py", "tests/test_maintenance.py",
                     "tests/fixtures/branch-read-direct-20261017.json", "catalogs/x.json"):
            self.assertFalse(maintenance.owned(path), path)

    def test_the_narrative_file_is_not_referenced_by_any_instruction_or_prompt(self):
        name = Path(maintenance.NARRATIVE).name
        for path in ("AGENTS.md", "CLAUDE.md", ".tiny-harness/CONTRACT.md",
                     "docs/MODEL-WORKER.md", "docs/AUTONOMOUS-DISCOVERY.md"):
            if path.startswith("docs/") and not (SOURCE / path).exists():
                continue   # Not in this release; the checked runner skips absent files the same way.
            self.assertNotIn(name, (SOURCE / path).read_text(), path)
        self.assertNotIn(name, maintenance.PROMPT + model_worker.PROMPT)

    def test_discovery_worker_restrictions_are_unchanged(self):
        for path in (".tiny-harness/run.py", ".tiny-harness/checks.json", "tests/run.py",
                     "seren_harness/offline_evals.py", "docs/release/x.md"):
            self.assertFalse(model_worker.owned(path), path)
        self.assertEqual(model_worker.VALIDATION_PATHS, (".tiny-harness/", "tests/run.py"))

    def test_launch_argv_grants_exactly_those_paths_and_no_other_workspace(self):
        cfg = {"claude": "/x/claude", "model": "m", "repo": "/r"}
        argv = maintenance.claude_argv(cfg, "s", "t", {"goal": GOAL, "plan": PLAN})
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Write,Glob,Grep,Bash")
        rules = [a for a in argv if a.startswith(("Edit(", "Write("))]
        granted = {r.split("(//r/", 1)[1].rstrip(")").rstrip("*") for r in rules}
        self.assertEqual(granted, set(maintenance.PATHS) | set(maintenance.DIRS))
        bash = [a for a in argv if a.startswith("Bash(")]
        reads = ("Bash(git status", "Bash(git diff", "Bash(git log", "Bash(git show")
        self.assertTrue(all("seren_harness worker" in b or b.startswith(reads) for b in bash))
        for flag in ("dontAsk", "--strict-mcp-config", "WebFetch", "WebSearch"):
            self.assertIn(flag, argv)
        self.assertNotIn("bypassPermissions", " ".join(argv))


class RunnerChoiceTests(unittest.TestCase):
    """`worker test --runner unittest|pytest`: fixed commands, validated targets only."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "tests").mkdir()
        shutil.copyfile(SOURCE / "tests/run.py", self.repo / "tests/run.py")
        (self.repo / "seren_harness").mkdir()
        (self.repo / "seren_harness/__init__.py").write_text("")
        (self.repo / "tests/test_a.py").write_text(
            "import unittest\nclass A(unittest.TestCase):\n    def test_1(self): pass\n"
            "    def test_2(self):\n        for c in ('ok', 'bad'):\n"
            "            with self.subTest(c=c):\n                self.assertEqual(c, 'ok')\n")

    def test_only_fixed_runners_and_existing_targets_build_a_command(self):
        python = sys.executable
        self.assertEqual(maintenance.test_argv(self.repo, "unittest", ["test_a.A.test_1"]),
                         [python, "-B", "tests/run.py", "--runner", "unittest", "test_a.A.test_1"])
        self.assertEqual(maintenance.test_argv(self.repo, "pytest", ["test_a", "test_a.A.test_1"]),
                         [python, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q",
                          "tests/test_a.py", "tests/test_a.py::A::test_1"])
        for runner, names in (("nose", ["test_a"]), ("/bin/sh", ["test_a"]), ("pytest", []),
                              ("pytest", ["test_missing"]), ("pytest", ["tests/test_a.py"]),
                              ("pytest", ["-k", "test_a"]), ("pytest", ["test_a;rm"]),
                              ("pytest", ["test_a.A.b.c"]), ("unittest", ["../test_a"]),
                              ("pytest", ["test_a"] * 21)):
            with self.subTest(runner=runner, names=names):
                self.assertIsNone(maintenance.test_argv(self.repo, runner, names))

    @MACOS_SANDBOX
    def test_both_runners_run_confined_and_report_the_failing_subtest(self):
        home = discovery.Home(tempfile.mkdtemp())
        for runner in ("unittest", "pytest"):
            with self.subTest(runner):
                out = []
                with mock.patch.object(model_worker, "_bounded",
                                       lambda argv, repo, timeout: out.append(subprocess.run(
                                           argv, cwd=repo, capture_output=True, text=True,
                                           timeout=timeout)) or out[-1].returncode):
                    code = maintenance.verb_test(home, self.repo, "s",
                                                 args("test", runner=runner, modules=["test_a"]), {})
                text = out[-1].stdout + out[-1].stderr
                self.assertEqual(1, code, text)
                self.assertIn("bad", text)
                self.assertEqual(2, maintenance.verb_test(home, self.repo, "s", args(
                    "test", runner="sh", modules=["test_a"]), {}))

    def test_discovery_workers_cannot_select_a_runner(self):
        self.assertEqual(2, model_worker.verb_test(None, self.repo, "s", mock.Mock(
            modules=["tests.test_a"], runner="pytest")))


class SelectionTests(unittest.TestCase):
    def test_absent_valid_changed_misplaced_and_finished_selections(self):
        repo = repo_with_plan()
        home = home_for(repo)
        self.assertEqual(maintenance.selection(home, repo), (None, None))  # disabled by default
        chosen = maintenance.select(home, repo, GOAL, PLAN, "owner")
        self.assertEqual(maintenance.selection(home, repo), (chosen, None))
        (repo / PLAN).write_text("v2\n")
        git(repo, "commit", "-qam", "change plan")
        self.assertEqual(maintenance.selection(home, repo)[1],
                         "maintenance plan at HEAD differs from the selected digest")
        with self.assertRaises(maintenance.Refused):
            maintenance.select(home, repo, GOAL, "other.txt", "owner")
        self.assertFalse((home.root / maintenance.SELECTION).exists())
        maintenance.select(home, repo, GOAL, PLAN, "owner")
        maintenance.record(home, GOAL, "s", "complete", "all steps done")
        self.assertIn("is complete", maintenance.selection(home, repo)[1])


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.launched = []
        self.repo = repo_with_plan()

    def launch(self, home, session, deadline):
        self.launched.append(session)
        return 999999

    def tick(self, home, dirty=None, reading=None):
        reading = reading or (lambda: usage())
        with mock.patch.object(claude_budget, "fetch_usage", reading):
            return discovery.supervise(home, due=lambda h, s, a: {"due": [], "refused": []},
                                       model_launch=self.launch, launch=lambda h, s: 1,
                                       repo_dirty=lambda repo: dict(dirty or {}))

    def end(self, home, outcome="model:success"):
        record = json.loads((home.root / "worker.json").read_text())
        discovery.write_json(home.root / "log" / ("model-%s.summary.json" % record["session"]),
                             {"outcome": outcome, "dirty_after": {}})

    def test_without_a_selection_sessions_stay_discovery_sessions(self):
        home = home_for(self.repo)
        self.assertEqual("launched", self.tick(home))
        record = json.loads((home.root / "worker.json").read_text())
        self.assertNotIn("mode", record)
        self.assertNotIn("mode", home.state()["sessions"][-1])

    def test_a_selected_goal_launches_counted_visible_maintenance_sessions(self):
        home = home_for(self.repo, sessions_per_day=2)
        maintenance.select(home, self.repo, GOAL, PLAN, "owner")
        self.assertEqual("launched", self.tick(home))
        record = json.loads((home.root / "worker.json").read_text())
        self.assertEqual((record["mode"], record["goal"]), ("maintenance", GOAL))
        self.assertEqual(home.state()["sessions"][-1]["mode"], "maintenance")
        with home.lock("work"):  # the running wrapper holds it
            self.assertEqual("worker_running", self.tick(home))  # never a second worker
        self.assertEqual(1, len(self.launched))
        self.end(home)
        self.assertEqual("launched", self.tick(home))
        self.end(home)
        self.assertEqual("limited", self.tick(home))  # shares the daily model-session limit
        self.assertEqual(2, discovery._today(home.state(), "model"))

    def test_budget_exhaustion_unavailable_usage_or_paid_overage_launch_nothing(self):
        def unavailable():
            raise claude_budget.Unavailable("usage service unavailable")
        for name, reading in (("45% cutoff", lambda: usage(45)),
                              ("unavailable", unavailable),
                              ("paid overage", lambda: usage(10, extra=True))):
            with self.subTest(name):
                home = home_for(self.repo)
                maintenance.select(home, self.repo, GOAL, PLAN, "owner")
                self.assertEqual("limited", self.tick(home, reading=reading))
                self.assertEqual([], self.launched)
                self.assertEqual([], home.state()["sessions"])  # no session consumed

    def test_an_orphaned_check_or_commit_blocks_launches_and_a_second_one(self):
        home = home_for(self.repo)
        goal = maintenance.select(home, self.repo, GOAL, PLAN, "owner")
        with home.lock(maintenance.VERB_LOCK):  # a commit outliving its ended session
            self.assertEqual("waiting", self.tick(home))
            self.assertIn("still running", home.state()["model_status"])
            for verb in ("check", "commit"):
                self.assertEqual(2, maintenance.run_verb(home, self.repo, "s",
                                                         args(verb, message="m"), goal))
        self.assertEqual([], self.launched)
        self.assertEqual("launched", self.tick(home))

    def test_invalid_or_finished_selections_and_foreign_changes_launch_nothing(self):
        home = home_for(self.repo)
        maintenance.select(home, self.repo, GOAL, PLAN, "owner")
        self.assertEqual("waiting", self.tick(home, dirty={".tiny-harness/checks.json": "h"}))
        self.assertEqual("waiting", self.tick(home, dirty={"seren_harness/model_worker.py": "h"}))
        maintenance.record(home, GOAL, "s", "blocked", "needs an owner decision")
        self.assertEqual("waiting", self.tick(home))  # never falls back to discovery
        self.assertIn("is blocked", home.state()["model_status"])
        self.assertEqual([], self.launched)


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.repo = repo_with_plan()
        self.home = home_for(self.repo)
        self.goal = maintenance.select(self.home, self.repo, GOAL, PLAN, "owner")
        self.cfg = model_worker.config(self.home.authority())

    def run_session(self, lines, sleep=0, deadline=60, reading=None):
        seen = []

        def popen(argv, **kw):
            seen.append(argv)
            self.env = kw["env"]
            return fake_claude(lines, sleep)(argv, **kw)
        with mock.patch.object(claude_budget, "fetch_usage", reading or (lambda: usage())), \
                mock.patch.object(claude_live, "dispatch", side_effect=AssertionError("live")), \
                mock.patch.object(claude_live, "snapshot", side_effect=AssertionError("live")):
            result = model_worker.run_session(self.home, "sess", time.time() + deadline, self.cfg,
                                              popen=popen, goal=self.goal)
        return result, seen

    def test_maintenance_bash_calls_outlast_a_checked_commit_but_not_the_session(self):
        self.run_session([INIT, OK])
        for name in ("BASH_MAX_TIMEOUT_MS", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"):
            self.assertEqual(1800000, int(self.env[name]))
            self.assertLess(1800000, self.cfg["limits"]["wall_seconds"] * 1000)
            self.assertNotIn(name, model_worker.claude_env(self.cfg, "s", self.home.root))

    def test_success_never_touches_live_status_or_dispatch(self):
        (outcome, summary), seen = self.run_session([INIT, OK])
        self.assertEqual(("model:success", GOAL), (outcome, summary["maintenance_goal"]))
        self.assertNotIn("--add-dir", seen[0])
        self.assertIn("Goal %s" % GOAL, seen[0][2])

    def test_wall_time_ends_the_session_and_recorded_progress_survives(self):
        maintenance.record(self.home, GOAL, "sess", "in_progress", "step A2 done; next A3")
        (outcome, _), _ = self.run_session([INIT], sleep=60, deadline=2)
        self.assertEqual("limit:wall_time", outcome)
        self.assertEqual(maintenance.progress(self.home, GOAL)["entries"][-1]["text"],
                         "step A2 done; next A3")
        self.assertEqual(maintenance.selection(self.home, self.repo)[0], self.goal)

    def test_usage_is_checked_before_launch_and_while_running(self):
        (outcome, _), seen = self.run_session([INIT, OK], reading=lambda: usage(45))
        self.assertEqual(("blocked:subscription_budget", []), (outcome, seen))
        readings = iter([usage(10)] + [usage(50)] * 100)
        with mock.patch.dict(claude_budget.POLICY, poll_seconds=0):
            (outcome, _), _ = self.run_session([INIT], sleep=60, reading=lambda: next(readings))
        self.assertEqual("blocked:subscription_budget", outcome)

    def test_a_second_session_is_refused_while_the_work_lock_is_held(self):
        with self.home.lock("work"):
            (outcome, _), seen = self.run_session([INIT, OK])
        self.assertEqual(("busy", []), (outcome, seen))


def args(verb, **kw):
    base = dict(verb=verb, modules=[], message=None, status=None, text=None)
    base.update(kw)
    return mock.Mock(**base)


class VerbTests(unittest.TestCase):
    def setUp(self):
        self.repo = repo_with_plan()
        self.home = home_for(self.repo)
        self.goal = maintenance.select(self.home, self.repo, GOAL, PLAN, "owner")

    def test_live_disclosure_case_and_discovery_verbs_are_unavailable(self):
        for verb in ("live-request", "live-review", "run-case", "synthetic-check", "candidate",
                     "followup", "note", "wait", "validate", "discard"):
            with self.subTest(verb):
                self.assertEqual(2, maintenance.run_verb(self.home, self.repo, "s", args(verb),
                                                         self.goal))

    def test_the_launch_record_not_the_caller_selects_maintenance_verbs(self):
        record = {"session": "sess", "kind": "model", "status": "running"}
        discovery.write_json(self.home.root / "worker.json", record)
        self.assertIsNone(maintenance.current(self.home, "sess"))  # a discovery session
        discovery.write_json(self.home.root / "worker.json", dict(record, mode="maintenance",
                                                                  goal=GOAL))
        self.assertIsNotNone(maintenance.current(self.home, "sess"))
        self.assertIsNone(maintenance.current(self.home, "other"))
        env = {"SEREN_DISCOVERY_SESSION": "sess", "SEREN_DISCOVERY_HOME": str(self.home.root)}
        with mock.patch.dict(os.environ, env), mock.patch.object(Path, "cwd",
                                                                 return_value=self.repo):
            self.assertEqual(2, model_worker.run(args("live-request", home=None)))

    def test_progress_checkpoints_and_completion_stops_further_launches(self):
        self.assertEqual(2, maintenance.run_verb(self.home, self.repo, "s",
                                                 args("progress", status="done", text="x"), self.goal))
        self.assertEqual(0, maintenance.run_verb(self.home, self.repo, "s", args(
            "progress", status="complete", text="all four deliverables committed"), self.goal))
        self.assertEqual(maintenance.selection(self.home, self.repo)[0], None)

    def test_changes_outside_the_paths_are_refused_before_any_check(self):
        calls = []
        for path in (".tiny-harness/checks.json", "seren_harness/model_worker.py", "other.txt"):
            with self.subTest(path):
                git(self.repo, "checkout", "--", ".")
                (self.repo / "tests/run.py").write_text("changed\n")
                (self.repo / path).write_text("tampered\n")
                code = maintenance.trusted_validation(
                    self.home, self.repo, "m", run=lambda *a, **k: calls.append(a))
                self.assertEqual((2, []), (code, calls))

    def test_validation_uses_the_head_runner_confined_and_a_runner_change_needs_the_floor(self):
        (self.repo / "tests/run.py").write_text("print('OK')  # a runner that reports success\n")
        seen = []

        def run(argv, **kw):
            seen.append((argv, kw))
            if argv[:2] == ["python3", argv[1]] and argv[1].endswith("run.py"):
                seen.append(("runner", Path(argv[1]).read_text(), kw["env"]))
            return subprocess.run(argv, **kw) if argv[0] == "/usr/bin/git" else \
                mock.Mock(returncode=0, stdout='{"tests": 5, "ok": true, "skipped": 0}\n')
        with mock.patch.object(maintenance, "independent_floor", return_value="fewer tests"):
            self.assertEqual(3, maintenance.trusted_validation(self.home, self.repo, "m", run=run))
        self.assertEqual("", git(self.repo, "diff", "--cached", "--name-only"))  # unstaged again
        with mock.patch.object(maintenance, "independent_floor", return_value=None):
            self.assertEqual(0, maintenance.trusted_validation(self.home, self.repo, None, run=run))
        [(_, content, env)] = [s for s in seen if s[0] == "runner"]
        self.assertEqual(content, "v1\n")  # HEAD's runner, never the working-tree copy
        prefix = json.loads(env[maintenance.CHECK_PREFIX])
        self.assertTrue(prefix == [] and model_worker.already_sandboxed()
                        or prefix[:2] == ["/usr/bin/sandbox-exec", "-f"], prefix)

    @MACOS_SANDBOX
    def test_independent_floor_counts_and_runs_plain_unittest(self):
        for name, extra, expected in (("passing, more tests", "pass", None),
                                      ("failing", "self.fail('x')", "failed"),
                                      ("fewer", None, "fewer than HEAD")):
            with self.subTest(name):
                repo = Path(tempfile.mkdtemp())
                git(repo, "init", "-q")
                (repo / "tests").mkdir()
                (repo / "tests/test_a.py").write_text(
                    "import unittest\nclass A(unittest.TestCase):\n"
                    "    def test_1(self): pass\n    def test_2(self): pass\n")
                git(repo, "add", "-A")
                git(repo, "commit", "-qm", "init")
                body = ("    def test_1(self): pass\n    def test_2(self): pass\n"
                        "    def test_3(self): %s\n" % extra if extra else "    def test_1(self): pass\n")
                (repo / "tests/test_a.py").write_text(
                    "import unittest\nclass A(unittest.TestCase):\n" + body)
                git(repo, "add", "-A")
                result = maintenance.independent_floor(self.home, repo)
                if expected is None:
                    self.assertIsNone(result)
                else:
                    self.assertIn(expected, result)


class ConfinementTests(unittest.TestCase):
    """The real profiles (macOS sandbox-exec), applied as the maintenance verbs apply them."""

    def confined(self, profile, command):
        disc = Path(tempfile.mkdtemp())
        argv = maintenance.confined(disc, "p.sb", profile, ["/bin/sh", "-c", command])
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    @MACOS_SANDBOX
    def test_checks_cannot_write_the_repository_discovery_home_or_reach_the_network(self):
        if model_worker.already_sandboxed():
            self.skipTest("an outer sandbox already confines this process")
        target = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".maintenance-test-"))
        self.addCleanup(shutil.rmtree, target, True)
        (target / ".git").mkdir()
        for profile in (maintenance.CHECK_PROFILE, maintenance.VERB_PROFILE):
            out = self.confined(profile, (
                "touch %s/file 2>/dev/null && echo HOME-WRITABLE || echo home-denied; "
                "touch %s/.git/HEAD 2>/dev/null && echo GIT-WRITABLE || echo git-denied; "
                "touch \"$TMPDIR/ok\" && echo temp-writable; "
                "ls ~/seren-harness-live >/dev/null 2>&1 && echo LIVE-READABLE || echo live-denied; "
                "/usr/bin/nc -v -z -G 2 192.0.2.1 443 2>&1 | tail -1" % (target, target))).stdout
            for expected in ("home-denied", "git-denied", "temp-writable", "live-denied",
                             "Operation not permitted"):
                self.assertIn(expected, out)

    @MACOS_SANDBOX
    def test_the_trusted_runner_confines_checks_and_alone_writes_the_commit(self):
        if model_worker.already_sandboxed():
            self.skipTest("an outer sandbox already confines this process")
        repo = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".maintenance-repo-"))
        self.addCleanup(shutil.rmtree, repo, True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for command in (["git", "init", "-q"], ["git", "config", "user.name", "t"],
                        ["git", "config", "user.email", "t@t"],
                        [sys.executable, str(RUNNER), "install", str(repo)]):
            subprocess.run(command, cwd=repo, env=env, check=True, capture_output=True)
        # A confined check must not see the prefix: nested runners would re-apply it.
        probe = ("import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                 "sys.exit(0 if not p.exists() and %r not in os.environ else 1)"
                 % maintenance.CHECK_PREFIX)

        def checks(code):
            (repo / ".tiny-harness/checks.json").write_text(json.dumps({"checks": [
                {"name": "probe", "timeout_seconds": 30,
                 "argv": [sys.executable, "-c", code, str(repo / "escaped")]}]}))
        checks(probe)
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
        subprocess.run([sys.executable, str(RUNNER), "commit", "-m", "seed"], cwd=repo, env=env,
                       check=True, capture_output=True)
        prefix = maintenance.confined(repo / ".git", "check.sb", maintenance.CHECK_PROFILE, [])
        confined_env = dict(env, **{maintenance.CHECK_PREFIX: json.dumps(prefix)})
        checks("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('x')")  # escape attempt
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
        out = subprocess.run([sys.executable, str(RUNNER), "commit", "-m", "escape"], cwd=repo,
                             env=confined_env, capture_output=True, text=True)
        self.assertNotEqual(0, out.returncode)
        self.assertFalse((repo / "escaped").exists())
        self.assertIn("(confined by", out.stdout)
        checks(probe + "  # harmless")
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
        out = subprocess.run([sys.executable, str(RUNNER), "commit", "-m", "ok"], cwd=repo,
                             env=confined_env, capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stdout + out.stderr)
        self.assertIn("ok", git(repo, "log", "-1", "--format=%s"))

    def test_a_malformed_check_prefix_is_rejected(self):
        out = subprocess.run([sys.executable, "-c",
                              "import importlib.util,sys; s=importlib.util.spec_from_file_location"
                              "('r', %r); m=importlib.util.module_from_spec(s); "
                              "s.loader.exec_module(m); m.check_prefix()" % str(RUNNER)],
                             env=dict(os.environ, **{maintenance.CHECK_PREFIX: '"sandbox"'}),
                             capture_output=True, text=True)
        self.assertIn("must be a JSON list", out.stderr)


if __name__ == "__main__":
    unittest.main()
