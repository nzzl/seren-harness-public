"""The persistent discovery loop, with stand-in experiments and stand-in worker processes.

No pinned binary, network or launchd is used: the experiment, lab and isolation check are
injected, and "workers" are small local processes that hold the work lock."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from seren_harness import discovery, tier0_cases

AUTHORITY = json.loads(discovery.TEMPLATE.read_text())
AUTHORITY["executable"] = "/nonexistent/seren"
DEFECT = {"case": "C3", "part": "run-id", "disposition": "reproduced_client_defect",
          "summary": "run_id sent", "grade": "stated", "basis": ["spec"]}


def home_with_authority(**limits):
    home = discovery.Home(tempfile.mkdtemp())
    value = json.loads(json.dumps(AUTHORITY))
    value["limits"].update(limits)
    discovery.write_json(home.root / "authority.json", value)
    return home


def fake_execute(results=None, check=None):
    def execute(lab, case_id, repeats, record):
        if check:
            check(case_id)
        record({"scenario": "s", "repeat": 0})
        found = [dict(r, case=case_id) for r in (results or [DEFECT])]
        return {"results": found, "facts": {"s": {}}}
    return execute


def work(home, execute, session="s1", **kw):
    return discovery.work(home, session, execute=execute, make_lab=lambda exe, d: object(),
                          check=lambda lab, root: {"ok": True}, **kw)


def holder(home, seconds=60):
    """A stand-in worker process that holds the work lock like a real one."""
    code = ("import fcntl,os,time;fd=os.open(%r,os.O_CREAT|os.O_RDWR);"
            "fcntl.flock(fd,fcntl.LOCK_EX);time.sleep(%d)" % (str(home.root / "work.lock"),
                                                              seconds))
    child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    for _ in range(50):
        if home.worker_running():
            break
        time.sleep(0.05)
    return child


class WorkerTests(unittest.TestCase):
    def test_attempt_identity_is_durable_before_the_experiment_runs(self):
        home = home_with_authority(worker_max_cases=1)
        seen = []

        def check(case_id):
            records = [json.loads(p.read_text()) for p in (home.root / "attempts").glob("*.json")]
            seen.extend((r["case"], r["status"]) for r in records)
        self.assertEqual("turnover:case_limit", work(home, fake_execute(check=check)))
        self.assertEqual([("C1", "started")], seen)
        record = json.loads(next((home.root / "attempts").glob("*.json")).read_text())
        self.assertEqual("completed", record["status"])
        self.assertTrue((home.root / "evidence" / "C1" / record["id"] / "result.json").exists())

    def test_completed_cases_are_not_repeated_and_findings_are_deduplicated(self):
        home = home_with_authority(worker_max_cases=50)
        self.assertEqual("idle", work(home, fake_execute()))
        state = home.state()
        self.assertEqual({"done"}, {e["status"] for e in state["cases"].values()})
        self.assertEqual("idle", work(home, fake_execute(check=self.fail), session="s2"))
        discovery.run_case(home, "s3", object(), "C3", home.authority(), fake_execute())
        finding = home.state()["findings"]["T0-C3-run-id"]
        self.assertEqual(2, len(finding["occurrences"]))
        drafts = sorted(p.name for p in (home.root / "findings").glob("*.md"))
        self.assertEqual(sorted("T0-%s-run-id.md" % c for c in tier0_cases.ORDER), drafts)

    def test_new_evidence_revises_a_finding_and_keeps_its_history(self):
        home = home_with_authority()
        question = dict(DEFECT, disposition="contract_question", summary="assumed shape")
        discovery.run_case(home, "a", object(), "C3", home.authority(), fake_execute([question]))
        stronger = dict(question, summary="recorded shape", basis=["recorded provenance"])
        discovery.run_case(home, "b", object(), "C3", home.authority(), fake_execute([stronger]))
        finding = home.state()["findings"]["T0-C3-run-id"]
        self.assertEqual(("recorded shape", "contract_question"),
                         (finding["summary"], finding["disposition"]))
        self.assertEqual("assumed shape", finding["history"][-1]["summary"])
        self.assertTrue(any("revised" in e for e in home.state()["events"]))

    def test_a_fix_is_recorded_as_a_change_not_a_new_defect(self):
        home = home_with_authority()
        discovery.run_case(home, "a", object(), "C3", home.authority(), fake_execute())
        fixed = dict(DEFECT, disposition="contract_met", summary="omitted")
        discovery.run_case(home, "b", object(), "C3", home.authority(), fake_execute([fixed]))
        finding = home.state()["findings"]["T0-C3-run-id"]
        self.assertEqual(("contract_met", "reproduced_client_defect"),
                         (finding["disposition"], finding["previous"]))

    def test_repeated_harness_failures_defer_one_case_and_others_continue(self):
        home = home_with_authority(worker_max_cases=50, max_attempts_per_case=2)

        def execute(lab, case_id, repeats, record):
            if case_id == "C1":
                raise RuntimeError("synthetic harness fault")
            return fake_execute()(lab, case_id, repeats, record)
        work(home, execute)
        work(home, execute, session="s2")
        state = home.state()
        self.assertEqual("deferred", state["cases"]["C1"]["status"])
        self.assertIn("evidence kept", state["cases"]["C1"]["deferred_reason"])
        self.assertEqual("done", state["cases"]["C6"]["status"])
        errors = list((home.root / "evidence" / "C1").glob("*/error.txt"))
        self.assertEqual(2, len(errors))

    def test_inconclusive_results_are_not_counted_as_done(self):
        home = home_with_authority(worker_max_cases=1)
        bad = dict(DEFECT, disposition="blocked")
        work(home, fake_execute([bad]))
        self.assertEqual("pending", home.state()["cases"]["C1"]["status"])

    def test_a_second_worker_is_refused_while_one_holds_the_lock(self):
        home = home_with_authority()
        child = holder(home)
        try:
            self.assertEqual("busy", work(home, fake_execute()))
        finally:
            child.kill()
            child.wait()

    def test_isolation_failure_blocks_without_running_anything(self):
        home = home_with_authority()

        def check(lab, root):
            raise discovery.tier0.IsolationError("sandbox did not deny")
        outcome = discovery.work(home, "s", execute=fake_execute(check=self.fail),
                                 make_lab=lambda exe, d: object(), check=check)
        self.assertEqual("blocked:isolation", outcome)
        self.assertIn("isolation", home.state()["blockers"])


class ScopeTests(unittest.TestCase):
    def test_authority_must_be_offline_and_carry_every_prohibition(self):
        for change in ({"live": True}, {"allowed_kinds": ["tier1-live"]},
                       {"prohibited": ["push"]}, {"executable": ""}):
            value = dict(AUTHORITY, **change)
            with self.assertRaises(discovery.ScopeError):
                discovery.validate_authority(value)

    def test_out_of_scope_cases_are_refused_never_queued(self):
        live = dict(tier0_cases.CASES["C3"], kind="tier1-live")
        with mock.patch.dict(tier0_cases.CASES, {"C3": live}):
            due, refused = discovery.pending(discovery.Home(tempfile.mkdtemp()).state(),
                                             AUTHORITY)
        self.assertEqual(["C3"], refused)
        self.assertNotIn("C3", due)

    def test_missing_authority_blocks_the_supervisor(self):
        home = discovery.Home(tempfile.mkdtemp())
        self.assertEqual("blocked", discovery.supervise(home, launch=self.fail))
        self.assertIn("authority", home.state()["blockers"])


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()

    def launcher(self, home, seconds=60):
        def launch(home_, session):
            child = holder(home, seconds)
            self.children.append(child)
            return child.pid
        return launch

    def test_one_worker_at_a_time_and_quiet_while_it_runs(self):
        home = home_with_authority()
        self.assertEqual("launched", discovery.supervise(home, self.launcher(home)))
        record = json.loads((home.root / "worker.json").read_text())
        self.assertEqual("running", record["status"])
        events = len(home.state()["events"])
        self.assertEqual("worker_running", discovery.supervise(home, launch=self.fail))
        self.assertEqual(events, len(home.state()["events"]))

    def test_stop_switch_terminates_the_worker_and_prevents_launches(self):
        home = home_with_authority()
        discovery.supervise(home, self.launcher(home))
        home.stop_path.write_text("owner")
        self.assertEqual("stopped", discovery.supervise(home, launch=self.fail))
        self.children[0].wait(10)
        self.assertFalse(home.worker_running())
        record = json.loads((home.root / "worker.json").read_text())
        self.assertEqual("stopped_by_owner", record["outcome"])
        self.assertEqual("stopped", discovery.supervise(home, launch=self.fail))
        self.assertIn("stop switch on", home.state()["next_action"])

    def test_wall_time_limit_ends_the_worker_and_interrupted_attempts_are_recovered(self):
        home = home_with_authority()
        discovery.supervise(home, self.launcher(home))
        session = json.loads((home.root / "worker.json").read_text())["session"]
        discovery.write_json(home.root / "attempts" / "a1.json", {
            "id": "a1", "case": "C2", "session": session, "status": "started",
            "digest": "x"})
        (home.root / "evidence" / "C2" / "a1").mkdir(parents=True)
        record = json.loads((home.root / "worker.json").read_text())
        record["deadline_epoch"] = time.time() - 1
        discovery.write_json(home.root / "worker.json", record)
        discovery.supervise(home, launch=lambda h, s: 999999)
        self.children[0].wait(10)
        ended = json.loads((home.root / "worker.json").read_text())
        self.assertEqual("limit:wall_time", [s["outcome"] for s in home.state()["sessions"]
                                             if s["id"] == session][0])
        self.assertNotEqual(session, ended["session"])  # a replacement was launched
        attempt = json.loads((home.root / "attempts" / "a1.json").read_text())
        self.assertEqual("interrupted", attempt["status"])
        self.assertTrue((home.root / "evidence" / "C2" / "a1").exists())
        self.assertEqual(1, home.state()["cases"]["C2"]["failures"])

    def test_a_dead_worker_without_a_result_is_a_failure_with_backoff(self):
        home = home_with_authority()
        discovery.supervise(home, launch=lambda h, s: 999999)
        self.assertEqual("backoff", discovery.supervise(home, launch=self.fail))
        self.assertIn("retry after backoff", home.state()["next_action"])

    def test_idle_worker_leads_to_a_durable_wait_and_a_new_case_wakes_it(self):
        home = home_with_authority(worker_max_cases=50)
        work(home, fake_execute())
        self.assertEqual("waiting", discovery.supervise(home, launch=self.fail))
        self.assertEqual("waiting", discovery.supervise(home, launch=self.fail))
        self.assertEqual(1, sum("durable wait" in e for e in home.state()["events"]))
        extra = dict(tier0_cases.CASES["C3"])
        with mock.patch.dict(tier0_cases.CASES, {"C9": extra}), \
                mock.patch.object(tier0_cases, "ORDER", tier0_cases.ORDER + ("C9",)):
            self.assertEqual("launched", discovery.supervise(home, self.launcher(home)))

    def test_daily_session_limit(self):
        home = home_with_authority(max_sessions_per_day=1)
        discovery.supervise(home, launch=lambda h, s: 999999)
        record = json.loads((home.root / "worker.json").read_text())
        discovery.write_json(home.root / "worker.json", dict(record, status="ended"))
        self.assertEqual("limited", discovery.supervise(home, launch=self.fail))

    def test_status_reports_scope_queue_findings_and_stop(self):
        home = home_with_authority(worker_max_cases=50)
        work(home, fake_execute())
        home.stop_path.write_text("x")
        text = discovery.status_text(home)
        for needle in ("Scope: tier0-offline only", "Stop switch: ON", "T0-C3-run-id",
                       "Queue: due none", "Resources:"):
            self.assertIn(needle, text)


class OwnerCommandTests(unittest.TestCase):
    def test_init_refuses_an_unpinned_executable_and_never_overwrites(self):
        root = Path(tempfile.mkdtemp())
        fake = root / "seren"
        fake.write_text("x")
        args = mock.Mock(action="init", home=str(root / "d"), executable=str(fake))
        self.assertEqual(2, discovery.run(args))
        discovery.write_json(root / "d" / "authority.json", {"kept": True})
        self.assertEqual(0, discovery.run(args))
        self.assertEqual({"kept": True},
                         json.loads((root / "d" / "authority.json").read_text()))

    def test_agent_plist_runs_only_the_launcher_tick(self):
        home = home_with_authority()
        text = discovery.plist(home, 120)
        self.assertIn("discovery_launcher.py</string>", text)
        self.assertIn("<key>RunAtLoad</key><true/>", text)
        self.assertNotIn("KeepAlive", text)
        self.assertTrue(os.path.basename(str(home.root)))

    def test_launcher_runs_the_committed_head_not_working_tree_edits(self):
        from seren_harness import discovery_launcher
        repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "case.py").write_text("committed\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "c"], check=True)
        (repo / "case.py").write_text("half-written\n")
        home = Path(tempfile.mkdtemp())
        with mock.patch.object(discovery_launcher, "REPO", repo):
            head, target = discovery_launcher.snapshot(home)
            again = discovery_launcher.snapshot(home)
        self.assertEqual("committed\n", (target / "case.py").read_text())
        self.assertEqual((head, target), again)
        self.assertTrue((target / ".complete").exists())

    def test_cli_dispatch_imports_this_checkouts_catalog(self):
        """The supervisor reads the catalog through `discovery due` (see due_sandboxed);
        the focused commit route relies on this to catch catalog import or dispatch
        breakage that in-process tests would miss."""
        source = Path(tier0_cases.__file__).resolve().parents[1]
        home = home_with_authority()
        out = subprocess.run(
            [sys.executable, "-c", "import runpy,sys,seren_harness.tier0_cases as c;"
             "sys.stderr.write(c.__file__+'\\n');sys.argv=['seren_harness']+sys.argv[1:];"
             "runpy.run_module('seren_harness',run_name='__main__')",
             "discovery", "due", "--home", str(home.root)],
            cwd=str(source), capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "LANG": "C",
                 "PYTHONPATH": str(source)})
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(str(source / "seren_harness/tier0_cases.py"),
                         str(Path(out.stderr.splitlines()[0]).resolve()))
        catalog = json.loads(out.stdout)["catalog"]
        self.assertEqual(list(tier0_cases.ORDER), [c[0] for c in catalog])
        self.assertTrue(all(kind == "tier0-offline" for _, _, kind in catalog))


if __name__ == "__main__":
    unittest.main()
