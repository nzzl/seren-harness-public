"""Model-worker supervision with stand-in `claude` processes (no model, no network)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from seren_harness import discovery, model_worker

# Real confinement needs macOS sandbox-exec; Linux CI has none (the supervisor is macOS-only).
MACOS_SANDBOX = unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(),
                                    "needs the macOS sandbox (/usr/bin/sandbox-exec)")

AUTHORITY =json.loads(discovery.TEMPLATE.read_text())
AUTHORITY["executable"] = "/nonexistent/seren"


def git_repo():
    repo = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for path in model_worker.EDITABLE + ("other.txt",):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("v1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    return repo


def enabled_home(repo=None, **limits):
    home = discovery.Home(tempfile.mkdtemp())
    value = json.loads(json.dumps(AUTHORITY))
    value["model_worker"] = {"enabled": True, "claude": "/bin/echo", "model": "m",
                             "repo": str(repo or git_repo()), "pinned_source": "/p",
                             "contract_map": "/c", "limits": dict(discovery.MODEL_LIMITS)}
    value["model_worker"]["limits"].update(limits)
    discovery.write_json(home.root / "authority.json", value)
    return home


def fake_claude(lines, sleep=0):
    script = "import sys,time\nfor l in %r:\n    print(l, flush=True)\ntime.sleep(%s)\n" % (
        [json.dumps(x) for x in lines], sleep)
    return lambda argv, **kw: subprocess.Popen([sys.executable, "-c", script],
                                               **{k: v for k, v in kw.items() if k != "env"})


INIT = {"type": "system", "subtype": "init", "apiKeySource": "none", "model": "m",
        "permissionMode": "dontAsk", "tools": ["Read"], "mcp_servers": []}
OK = {"type": "result", "subtype": "success", "is_error": False, "result": "done",
      "num_turns": 3, "duration_ms": 10, "total_cost_usd": 0.1, "permission_denials": []}


class LaunchRouteTests(unittest.TestCase):
    def test_argv_is_narrow_noninteractive_and_never_bypasses_permissions(self):
        cfg = {"claude": "/x/claude", "model": "m", "repo": "/r", "pinned_source": "/p",
               "contract_map": "/c"}
        argv = model_worker.claude_argv(cfg, "s", "t")
        for flag in ("--permission-mode", "dontAsk", "--permission-prompts", "none",
                     "--strict-mcp-config", "--setting-sources", "project"):
            self.assertIn(flag, argv)
        joined = " ".join(argv)
        self.assertNotIn("bypassPermissions", joined)
        self.assertNotIn("dangerously", joined)
        self.assertNotIn("--bare", joined)  # bare would drop the subscription login
        edits = [a for a in argv if a.startswith("Edit(")]
        self.assertEqual(["Edit(//r/seren_harness/tier0_cases.py)",
                          "Edit(//r/tests/test_tier0.py)", "Edit(//r/docs/tier0/**)",
                          "Edit(//r/docs/synthetic-cases/**)"], edits)
        self.assertEqual(["Write(//r/docs/synthetic-cases/*.json)"],
                         [a for a in argv if a.startswith("Write(")])
        bash = [a for a in argv if a.startswith("Bash(")]
        self.assertTrue(all("seren_harness worker" in b or b.startswith("Bash(git ") or
                            "discovery status" in b for b in bash))
        self.assertFalse(any("commit" in b or "push" in b for b in bash
                             if b.startswith("Bash(git ")))

    def test_saved_evidence_is_readable_but_not_editable(self):
        cfg = {"claude": "/x/claude", "model": "m", "repo": "/r", "pinned_source": "/p",
               "contract_map": "/c"}
        argv = model_worker.claude_argv(cfg, "s", "t", "/d")
        added = argv[argv.index("--add-dir") + 1:argv.index("--session-id")]
        self.assertEqual(["/p", "/c", "/d/evidence", "/d/findings"], added)
        self.assertFalse([a for a in argv if a.startswith("Edit(//d")])

    def test_environment_never_carries_provider_credentials(self):
        cfg = {"claude": "/x/claude", "repo": "/r"}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret",
                                          "CLAUDE_CODE_USE_BEDROCK": "1"}):
            env = model_worker.claude_env(cfg, "s", "/d")
        self.assertFalse([k for k in env if k.startswith(("ANTHROPIC", "CLAUDE_CODE_USE"))])
        self.assertTrue(env["PATH"].startswith("/r/.venv/bin:/x:"))

    def test_owned_paths(self):
        self.assertTrue(model_worker.owned("seren_harness/tier0_cases.py"))
        self.assertTrue(model_worker.owned("docs/tier0/notes.md"))
        for path in ("seren_harness/tier0.py", "seren_harness/discovery.py", "AGENTS.md",
                     "docs/MODEL-WORKER.md", ".tiny-harness/checks.json"):
            self.assertFalse(model_worker.owned(path), path)


class SessionTests(unittest.TestCase):
    def run_with(self, lines, sleep=0, deadline=60, **limits):
        home = enabled_home(**limits)
        cfg = model_worker.config(home.authority())
        return model_worker.run_session(home, "sess", time.time() + deadline, cfg,
                                        popen=fake_claude(lines, sleep)), home

    def test_success_on_the_subscription_route(self):
        (outcome, summary), home = self.run_with([INIT, OK])
        self.assertEqual("model:success", outcome)
        self.assertEqual("none", summary["init"]["apiKeySource"])
        self.assertTrue((home.root / "log" / "model-sess.jsonl").exists())

    def test_a_non_subscription_session_is_ended_and_blocked(self):
        init = dict(INIT, apiKeySource="ANTHROPIC_API_KEY")
        (outcome, _), _ = self.run_with([init], sleep=30)
        self.assertEqual("blocked:not_subscription", outcome)

    def test_rate_limit_and_missing_result(self):
        limited = dict(OK, is_error=True, subtype="error_during_execution",
                       result="API Error: 429 rate limit reached")
        self.assertEqual("rate_limited", self.run_with([INIT, limited])[0][0])
        self.assertEqual("failed:no_result", self.run_with([INIT])[0][0])

    def test_wall_time_and_output_caps_end_the_session(self):
        started = time.time()
        (outcome, _), _ = self.run_with([INIT], sleep=60, deadline=2)
        self.assertEqual("limit:wall_time", outcome)
        self.assertLess(time.time() - started, 20)
        (outcome, _), _ = self.run_with([INIT, {"type": "assistant", "x": "y" * 500}],
                                        sleep=60, log_bytes=100)
        self.assertEqual("limit:output", outcome)

    def test_a_second_session_is_refused_while_the_work_lock_is_held(self):
        home = enabled_home()
        cfg = model_worker.config(home.authority())
        with home.lock("work"):
            outcome, _ = model_worker.run_session(home, "s2", time.time() + 30, cfg,
                                                  popen=fake_claude([INIT, OK]))
        self.assertEqual("busy", outcome)


class FollowupTests(unittest.TestCase):
    def args(self, **kw):
        base = {k: None for k in model_worker.FOLLOWUP_FIELDS}
        base.update(id=None, status=None, needs=None, finding=None, evidence=None, text=None)
        base.update(kw)
        return mock.Mock(**base)

    def test_value_follow_ups_are_durable_and_gated_by_authority(self):
        home = enabled_home()
        add = model_worker.verb_followup
        self.assertEqual(2, add(home, None, "s", self.args(id="F1", problem="p")))
        self.assertEqual(0, add(home, None, "s", self.args(
            id="F1", problem="cloud errors drop the server message",
            consequence="operators cannot see why a command failed", needs="tier0",
            next_step="survey other cloud-* commands", cost="low",
            finding=["T0-C5-cloud-error-detail"])))
        self.assertEqual(0, add(home, None, "s", self.args(
            id="F2", problem="stream may stop at first tool call",
            consequence="answers hidden, success reported", needs="live_authority",
            next_step="one read-only replay of an existing run")))
        eligible, gated = model_worker.followup_groups(home.state())
        self.assertEqual(["F1"], [i["id"] for i in eligible])  # contract question eligible
        self.assertEqual(["F2"], [i["id"] for i in gated])  # live step never picked
        item = home.state()["followups"]["F1"]
        self.assertEqual("unknown", item["exposure"])  # unsupported fields stay unknown
        text = discovery.status_text(home)
        self.assertIn("1 eligible for Tier 0 work, 1 awaiting your authority", text)

    def test_completed_work_reopens_only_with_new_evidence(self):
        home = enabled_home()
        add = model_worker.verb_followup
        add(home, None, "s", self.args(id="F1", problem="p", consequence="c", needs="tier0",
                                       next_step="n"))
        add(home, None, "s", self.args(id="F1", status="done"))
        self.assertEqual(2, add(home, None, "s", self.args(id="F1", status="open")))
        self.assertEqual(0, add(home, None, "s", self.args(
            id="F1", status="open", evidence="new pinned-source finding at agent.rs 100")))
        self.assertEqual("open", home.state()["followups"]["F1"]["status"])


class SupervisorModelTests(unittest.TestCase):
    def setUp(self):
        self.launched = []

    def launch(self, home, session, deadline):
        self.launched.append(session)
        return 999999

    def tick(self, home, due=(), dirty=None):
        dirty = dirty or {}
        return discovery.supervise(home, due=lambda h, s, a: {"due": list(due), "refused": []},
                                   model_launch=self.launch, launch=lambda h, s: 999998,
                                   repo_dirty=lambda repo: dict(dirty))

    def end(self, home, outcome):
        record = json.loads((home.root / "worker.json").read_text())
        discovery.write_json(home.root / "log" / ("model-%s.summary.json" % record["session"]),
                             {"outcome": outcome, "dirty_after": []})

    def test_model_worker_starts_only_after_the_deterministic_queue_drains(self):
        home = enabled_home()
        self.assertEqual("launched", self.tick(home, due=["C1"]))
        self.assertEqual([], self.launched)
        record = json.loads((home.root / "worker.json").read_text())
        discovery.write_json(home.root / "worker.json", dict(record, status="ended"))
        self.assertEqual("launched", self.tick(home))
        self.assertEqual(1, len(self.launched))
        self.assertEqual("model", json.loads((home.root / "worker.json").read_text())["kind"])

    def test_uncommitted_non_worker_changes_hold_model_launches(self):
        home = enabled_home()
        self.assertEqual("waiting", self.tick(home, dirty={"seren_harness/tier0.py": "h"}))
        # Another writer's edit under a worker path is foreign too (no recorded leftover).
        self.assertEqual("waiting", self.tick(home, dirty={"docs/tier0/README.md": "h1"}))
        with home.edit() as state:
            state.setdefault("model", {})["owned_dirty"] = {"docs/tier0/README.md": "h0"}
        self.assertEqual("waiting", self.tick(home, dirty={"docs/tier0/README.md": "h1"}))
        self.assertEqual([], self.launched)
        with home.edit() as state:
            state["model"]["owned_dirty"] = {"seren_harness/tier0_cases.py": "h2"}
        self.assertEqual("launched", self.tick(
            home, dirty={"seren_harness/tier0_cases.py": "h2"}))

    def test_a_session_claims_only_worker_path_changes_it_made(self):
        repo = git_repo()
        (repo / "docs" / "tier0").mkdir(parents=True)
        (repo / "docs" / "tier0" / "owner.md").write_text("owner\n")
        home = enabled_home(repo)
        cfg = model_worker.config(home.authority())
        script = ("import pathlib\npathlib.Path(%r).write_text('worker edit\\n')\n"
                  "print(%r, flush=True)" % (str(repo / model_worker.EDITABLE[0]),
                                             json.dumps(OK)))
        def popen(argv, **kw):
            return subprocess.Popen([sys.executable, "-c", script],
                                    **{k: v for k, v in kw.items() if k != "env"})
        _, summary = model_worker.run_session(home, "s", time.time() + 60, cfg, popen=popen)
        self.assertEqual([model_worker.EDITABLE[0]], sorted(summary["dirty_after"]))

    def test_a_killed_worker_leftovers_are_recovered_by_hash_and_resumed(self):
        repo = git_repo()
        home = enabled_home(repo)

        def tick():
            return discovery.supervise(
                home, due=lambda h, s, a: {"due": [], "refused": []},
                model_launch=self.launch, repo_dirty=model_worker.dirty_hashes)
        self.assertEqual("launched", tick())
        session = json.loads((home.root / "worker.json").read_text())["session"]
        discovery.write_json(home.root / "log" / ("model-%s.start.json" % session),
                             {"dirty_before": {}, "inherited": {}})
        (repo / model_worker.EDITABLE[0]).write_text("partial edit\n")  # then killed
        self.assertEqual("backoff", tick())  # no summary: failure, but leftovers attributed
        self.assertIn(model_worker.EDITABLE[0], home.state()["model"]["owned_dirty"])
        with home.edit() as state:
            state["model"]["backoff_until"] = discovery.iso(discovery.now())
        self.assertEqual("launched", tick())  # resumable, not held as a foreign change
        self.assertEqual(2, len(self.launched))

    def test_session_turnover_then_requested_wait(self):
        home = enabled_home()
        self.tick(home)
        self.end(home, "model:success")
        self.assertEqual("launched", self.tick(home))  # automatic next session
        self.assertEqual(2, len(self.launched))
        self.end(home, "model:success")
        with home.edit() as state:
            state.setdefault("model", {})["wait"] = {
                "until": discovery.iso(discovery.now() + discovery.timedelta(hours=5)),
                "reason": "nothing justified", "head": discovery._head()}
        self.assertEqual("waiting", self.tick(home))
        self.assertEqual(2, len(self.launched))
        self.assertIn("asked to wait", home.state()["model_status"])

    def test_rate_limit_backs_off_and_resumes(self):
        home = enabled_home()
        self.tick(home)
        self.end(home, "rate_limited")
        self.assertEqual("backoff", self.tick(home))
        with home.edit() as state:
            state["model"]["backoff_until"] = discovery.iso(discovery.now())
        self.assertEqual("launched", self.tick(home))

    def test_a_non_subscription_session_blocks_further_model_launches(self):
        home = enabled_home()
        self.tick(home)
        self.end(home, "blocked:not_subscription")
        self.assertEqual("waiting", self.tick(home))
        self.assertIn("model_auth", home.state()["blockers"])
        self.assertEqual(1, len(self.launched))

    def test_a_failed_final_commit_is_a_session_failure_with_backoff(self):
        home = enabled_home()
        self.tick(home)
        session = json.loads((home.root / "worker.json").read_text())["session"]
        with home.edit() as state:
            state.setdefault("model", {})["commits"] = [
                {"session": session, "exit": 1, "cause": "harness"}]
        self.end(home, "model:success")
        self.assertEqual("backoff", self.tick(home))
        sessions = home.state()["model"]["sessions"]
        self.assertEqual("failed:commit:harness", sessions[-1]["outcome"])

    def test_daily_model_session_limit(self):
        home = enabled_home(sessions_per_day=1)
        self.tick(home)
        self.end(home, "model:success")
        self.assertEqual("limited", self.tick(home))

    def test_disabled_model_workers_mean_a_durable_wait(self):
        home = discovery.Home(tempfile.mkdtemp())
        discovery.write_json(home.root / "authority.json", AUTHORITY)
        self.assertEqual("waiting", self.tick(home))
        self.assertIn("not enabled", home.state()["next_action"])


class VerbTests(unittest.TestCase):
    def test_commit_refuses_staged_files_outside_worker_paths(self):
        repo = git_repo()
        (repo / "other.txt").write_text("v2\n")
        subprocess.run(["git", "-C", str(repo), "add", "other.txt"], check=True)
        args = mock.Mock(message="x")
        self.assertEqual(2, model_worker.verb_commit(enabled_home(repo), repo, "s", args))

    def test_commit_message_is_one_subject_line_without_trailers(self):
        repo = git_repo()
        (repo / model_worker.EDITABLE[0]).write_text("v2\n")
        seen = []

        def bounded(argv, cwd, timeout, capture=None):
            seen.append(argv)
            return 1
        args = mock.Mock(message="Add case\n\nBody\nCo-Authored-By: someone <x@y>",
                         candidate="C99")
        with mock.patch.object(model_worker, "_bounded", bounded):
            model_worker.verb_commit(enabled_home(repo), repo, "abcdef123", args)
        self.assertEqual("[model-worker abcdef12] Add case", seen[0][-1])

    def test_commit_refuses_an_altered_validation_runner_or_policy(self):
        for path in (".tiny-harness/run.py", ".tiny-harness/checks.json", "tests/run.py"):
            with self.subTest(path=path):
                repo = git_repo()
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                (repo / path).write_text("select everything as focused\n")
                (repo / model_worker.EDITABLE[0]).write_text("v2\n")
                calls = []
                with mock.patch.object(model_worker, "_bounded",
                                       lambda *a, **k: calls.append(a) or 0):
                    code = model_worker.verb_commit(enabled_home(repo), repo, "s",
                                                    mock.Mock(message="x", candidate="C99"))
                self.assertEqual((2, []), (code, calls))

    def test_commit_records_the_validation_route_and_tree(self):
        repo = git_repo()
        home = enabled_home(repo)
        (repo / model_worker.EDITABLE[0]).write_text("v2\n")

        def bounded(argv, cwd, timeout, capture=None):
            capture.append("ROUTE focused for staged tree 0a1b2c: every staged change ...\n")
            return 0
        with mock.patch.object(model_worker, "_bounded", bounded):
            model_worker.verb_commit(home, repo, "s", mock.Mock(message="x", candidate="C99"))
        self.assertEqual("focused 0a1b2c", home.state()["model"]["commits"][-1]["route"])

    def test_check_failures_are_classified_by_whose_code_failed(self):
        classify = model_worker.classify_check_failure
        self.assertEqual("worker_change", classify(
            "FAIL: test_x (test_tier0.JudgeTests.test_x)\nFAILED (failures=1)")[0])
        self.assertEqual(("harness", ["test_sql_probe.SqlProbeTests.test_y"]), classify(
            "FAIL: test_y (test_sql_probe.SqlProbeTests.test_y)\nFAILED (failures=1)"))
        self.assertEqual("worker_change", classify(
            "seren_harness/tier0_cases.py:3:1: F401 unused\nFAIL basic Python lint (exit 1)")[0])
        self.assertEqual("unknown", classify("ERROR: Required validation failed")[0])

    def test_the_repair_budget_is_durable_and_refuses_a_fourth_attempt(self):
        repo = git_repo()
        home = enabled_home(repo)
        calls = []

        def bounded(argv, cwd, timeout, capture=None):
            calls.append(argv)
            capture.append("FAIL: test_y (test_sql_probe.SqlProbeTests.test_y)\n")
            return 1
        args = mock.Mock(message="Add case C99", candidate="C99")
        with mock.patch.object(model_worker, "_bounded", bounded):
            for session in ("s1", "s2", "s3"):  # failures survive session turnover
                (repo / model_worker.EDITABLE[0]).write_text("change %s\n" % session)
                self.assertEqual(1, model_worker.verb_commit(home, repo, session, args))
            self.assertEqual(3, model_worker.verb_commit(home, repo, "s4", args))
        self.assertEqual(3, len(calls))  # the fourth expensive check never ran
        state = home.state()
        self.assertEqual("deferred", state["model"]["candidates"]["C99"]["status"])
        self.assertEqual(3, state["harness_failures"][
            "test_sql_probe.SqlProbeTests.test_y"]["count"])
        self.assertEqual(2, model_worker.verb_commit(home, repo, "s5",
                                                     mock.Mock(message="x", candidate="")))

    def test_discard_restores_only_worker_paths(self):
        repo = git_repo()
        (repo / "other.txt").write_text("interactive\n")
        (repo / model_worker.EDITABLE[0]).write_text("partial\n")
        (repo / "docs" / "tier0").mkdir(parents=True)
        (repo / "docs" / "tier0" / "new.md").write_text("x\n")
        model_worker.verb_discard(enabled_home(repo), repo, "s", None)
        self.assertEqual("v1\n", (repo / model_worker.EDITABLE[0]).read_text())
        self.assertFalse((repo / "docs" / "tier0" / "new.md").exists())
        self.assertEqual("interactive\n", (repo / "other.txt").read_text())

    @MACOS_SANDBOX
    def test_an_already_sandboxed_caller_does_not_nest(self):
        disc = Path(tempfile.mkdtemp())
        code = ("from seren_harness import model_worker as m; "
                "print(m.already_sandboxed(), m.sandboxed(%r, %r, ['x']) == ['x'])"
                % (str(disc), str(disc)))
        argv = model_worker.sandboxed(disc, disc, [sys.executable, "-c", code])
        root = str(Path(model_worker.__file__).resolve().parents[1])
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                             env={"PATH": "/usr/bin:/bin", "PYTHONPATH": root})
        self.assertEqual("True True", out.stdout.strip(), out.stderr[-400:])

    @MACOS_SANDBOX
    def test_worker_sandbox_denies_network_and_credential_reads(self):
        disc = Path(tempfile.mkdtemp())
        argv = model_worker.sandboxed(disc, disc, [
            "/bin/sh", "-c", "/usr/bin/nc -v -z -G 2 192.0.2.1 443 2>&1 | tail -1; "
            "ls ~/.ssh >/dev/null 2>&1 && echo SSH-READABLE || echo ssh-denied"])
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout
        self.assertIn("Operation not permitted", out)
        self.assertIn("ssh-denied", out)


if __name__ == "__main__":
    unittest.main()
