"""The database-read campaign: fixture setup, immutable case preparation, reads, cleanup.

SYNTHETIC: the pinned-CLI stand-in (tests/fixture_standin.py) serves the fixture commands and
the three frozen read statements computed from the rows it stored; a stand-in Claude CLI
(tests/test_sessions.py) runs only the exact commands each session allows. This is synthetic
coordinator verification, not a live-Seren or real-Claude demonstration.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import cli, db_fixture, dbread
from test_db_fixture import CANARY, DELETE_CALLS, Env, PACKAGE_ID, SETUP_CALLS, _sha
from test_sessions import FAKE_CLAUDE

CAMPAIGN = "seren.db-read-campaign"
CASES = ["seren.db-read.open-invoices", "seren.db-read.first-page-by-due-date",
         "seren.db-read.missing-values"]
READ = ["GET /projects/{id}/connection_uri", "DB session"]


class DbEnv(Env):
    def __init__(self, path, scenario, case_timeout=2, **limits):
        super().__init__(path, scenario, **limits)
        package = json.loads(self.package.read_text())
        package["campaign"]["psql_invocation_timeout_seconds"] = case_timeout
        self.package.write_text(json.dumps(package, indent=2))
        self.claude = self.root / "claude"
        self.claude.write_text(standin(FAKE_CLAUDE.replace("{python}", sys.executable)))
        self.claude.chmod(0o755)
        prerun(self.claude)  # first-run assessment outside the timed case runs
        self.log = self.root / "claude-log.jsonl"

    def authorize(self, name="auth.json", **override):
        now = datetime.now(timezone.utc)
        auth = {"kind": "db_read_campaign_authorization", "package": PACKAGE_ID,
                "package_sha256": _sha(self.package), "manifest_sha256": _sha(self.manifest),
                "target_sha256": _sha(self.target), "approved_by": "test owner (synthetic)",
                "approved_at": now.isoformat(), "max_service_requests": 25,
                "budgets": {"fixture": 19, "reads": 6},
                "cases": ["case_open_invoices", "case_first_page_by_due_date",
                          "case_missing_values"], "max_sessions": 2,
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(minutes=30)).isoformat(),
                "cleanup_expires_at": (now + timedelta(minutes=60)).isoformat()}
        auth.update(override)
        auth = {k: v for k, v in auth.items() if v is not None}
        path = self.root / name
        path.write_text(json.dumps(auth))
        return path

    def db(self, action, auth=None, *extra):
        argv = ["dbread", action, "--package", str(self.package), "--manifest",
                str(self.manifest), "--home", str(self.home), "--root", str(self.root), *extra]
        if auth:
            argv += ["--authorization", str(auth)]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}), \
                contextlib.redirect_stdout(out):
            code = cli.main(argv)
        self.output = getattr(self, "output", "") + out.getvalue()
        return code

    def campaign_state(self):
        return json.loads((self.home / "campaigns" / CAMPAIGN / "state.json").read_text())

    def results(self):
        return {k: e.get("result", e["status"]) for k, e in
                self.campaign_state()["cases"].items()}

    def coordinate(self, mode="", popen=None):
        out = io.StringIO()
        env = {"SEREN_API_KEY": CANARY, "ANTHROPIC_API_KEY": CANARY,
               "FAKE_CLAUDE_LOG": str(self.log), "FAKE_CLAUDE_MODE": mode,
               "FAKE_AUTH": "claude.ai"}
        kwargs = {"popen": popen} if popen else {}
        with mock.patch.dict(os.environ, env):
            code = dbread.DbReadCoordinator(self.package, self.manifest, self.root / "auth.json",
                                            self.home, self.root, self.claude, **kwargs).run(out)
        self.output = getattr(self, "output", "") + out.getvalue()
        return code

    def sessions(self):
        return json.loads((self.home / "campaigns" / CAMPAIGN / "sessions.json").read_text())

    def workspace(self):
        return self.home / "campaigns" / CAMPAIGN / "workspace"


class DbReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def env(self, scenario=None, **kwargs):
        self.n += 1
        return DbEnv(Path(self.tmp.name) / str(self.n), scenario or {}, **kwargs)

    def assertClean(self, e):
        saved = b"".join(p.read_bytes() for p in e.home.rglob("*") if p.is_file())
        self.assertNotIn(CANARY.encode(), saved)
        self.assertNotIn(CANARY, e.output)

    def ready(self, scenario=None, **kwargs):
        e = self.env(scenario, **kwargs)
        auth = e.authorize()
        self.assertEqual(e.db("setup", auth), dbread.OK, e.output)
        self.assertEqual(e.db("prepare", auth), dbread.OK, e.output)
        return e, auth

    def test_full_flow_reads_exact_results_and_cleans_up(self):
        e, auth = self.ready()
        prepared = e.campaign_state()["prepared"]["cases"]
        self.assertEqual(list(prepared), CASES)
        self.assertTrue(all(r["customer_task"] and r["failure_hypothesis"]
                            for r in prepared.values()))
        self.assertEqual(e.db("run", auth), dbread.OK, e.output)
        self.assertEqual(e.results(), {k: "pass" for k in CASES})
        self.assertEqual(e.calls(), SETUP_CALLS + READ * 3)
        self.assertEqual(e.campaign_state()["budget"]["reserved"], 6)
        self.assertEqual(e.db("verify-handoff"), dbread.OK)
        self.assertIn("handoff verified", e.output)
        self.assertEqual(e.db("run", auth), dbread.OK)  # closed: nothing repeated
        self.assertEqual(e.db("cleanup", auth), db_fixture.OK)
        self.assertEqual(e.calls(), SETUP_CALLS + READ * 3 + DELETE_CALLS)
        self.assertEqual(e.campaign_state()["fixture"]["obligation"], "closed")
        self.assertEqual(e.db("prepare", auth), dbread.REFUSED)  # closed campaign
        self.assertEqual(len(e.calls()), 16)
        self.assertClean(e)

    def test_contradictory_results_fail_with_private_findings(self):
        e, auth = self.ready({"cases": {"case_open_invoices": "wrong_type",
                                        "case_first_page_by_due_date": "reorder",
                                        "case_missing_values": "null_as_empty"}})
        self.assertEqual(e.db("run", auth), dbread.OK)
        self.assertEqual(e.results(), {k: "supported_failure" for k in CASES})
        state = e.campaign_state()
        self.assertTrue(all(e_["findings"] == {"read_result": "new"} or
                            list(e_["findings"].values()) == ["new"]
                            for e_ in state["cases"].values()))
        self.assertTrue(list((e.home / "findings" / "drafts").glob("*.md")))
        for change in ("missing_field", "extra_field", "canary"):
            f, auth = self.ready({"cases": {"case_open_invoices": change}})
            f.db("run", auth)
            self.assertEqual(f.results()[CASES[0]], "supported_failure", change)
            self.assertClean(f)

    def test_incomplete_or_uncertain_results_stop_testing_and_never_pass(self):
        for change, result in (("not_json", "malformed_response"), ("huge", "malformed_response"),
                               ("error", "inconclusive")):
            e, auth = self.ready({"cases": {"case_open_invoices": change}})
            self.assertEqual(e.db("run", auth), dbread.STOPPED, change)
            self.assertEqual(e.results(), {CASES[0]: result, CASES[1]: "pending",
                                           CASES[2]: "pending"})
            self.assertEqual(e.calls(), SETUP_CALLS + READ)
            self.assertEqual(e.db("cleanup", auth), db_fixture.OK)
            self.assertClean(e)
        e, auth = self.ready({"case": "hang_before", "stall_seconds": 10}, case_timeout=1)
        self.assertEqual(e.db("run", auth), dbread.STOPPED)
        self.assertEqual(e.results()[CASES[0]], "uncertain_timeout")
        self.assertEqual(e.db("run", auth), dbread.STOPPED)  # never replayed
        self.assertEqual(e.calls(), SETUP_CALLS + READ[:1])
        self.assertEqual(e.db("cleanup", auth), db_fixture.OK)

    def test_changed_artifacts_or_authority_are_refused_before_a_read(self):
        def case_file(e):
            return next((e.workspace() / "cases").glob("*open-invoices*"))

        def edit(path, change):
            value = json.loads(path.read_text())
            change(value)
            path.write_text(json.dumps(value))
        tamperings = {
            "altered SQL": lambda e: edit(case_file(e), lambda c: c["execution_args"].__setitem__(
                16, c["execution_args"][16].replace("'acme'", "'globex'"))),
            "altered expectation": lambda e: edit(case_file(e), lambda c: c["api"][
                "expected_result"].__setitem__("total_cents", 1)),
            "substituted target": lambda e: edit(e.workspace() / "targets" / "seren.json",
                                                 lambda t: t["collectors"][CASES[0]]["args"]
                                                 .__setitem__(2, "1" * 8 + "-1111-4111-8111-"
                                                              + "1" * 12)),
            "extra case": lambda e: (e.workspace() / "cases" / "extra.json").write_text("{}"),
            "edited registration": lambda e: edit(
                e.home / "fixtures" / PACKAGE_ID / "registration.json",
                lambda r: r.__setitem__("database", "other")),
            "changed evidence": lambda e: edit(Path(json.loads(
                (e.home / "fixtures" / PACKAGE_ID / "registration.json").read_text())
                ["readback_evidence"]), lambda v: v["retained"]["observed"]["rows"].pop()),
        }
        for label, tamper in tamperings.items():
            e, auth = self.ready()
            tamper(e)
            self.assertEqual(e.db("run", auth), dbread.STOPPED, label)
            self.assertIn("invariant failed", e.campaign_state()["stopped"]["reason"], label)
            self.assertEqual(e.calls(), SETUP_CALLS, label)  # no read was sent
            self.assertEqual(e.db("verify-handoff"), dbread.STOPPED, label)
        e = self.env()
        refused = [{"cases": ["case_unknown"]}, {"budgets": {"fixture": 19, "reads": 9},
                                                 "max_service_requests": 28},
                   {"max_sessions": 3}, {"cleanup_expires_at": None},
                   {"kind": "db_fixture_authorization"}, {"cases": ["case_missing_values",
                                                                    "case_open_invoices"]}]
        for override in refused:
            self.assertEqual(e.db("setup", e.authorize("x.json", **override)),
                             dbread.INVALID, override)
        self.assertEqual(e.calls(), [])
        auth = e.authorize()
        self.assertEqual(e.db("setup", auth), dbread.OK)
        self.assertEqual(e.db("prepare", e.authorize("other.json", approved_by="someone")),
                         dbread.INVALID)
        manifest = json.loads(e.manifest.read_text())
        manifest["statements"]["case_open_invoices"] += " "
        e.manifest.write_text(json.dumps(manifest))
        self.assertEqual(e.db("prepare", auth), dbread.INVALID)  # altered family
        self.assertEqual(e.calls(), SETUP_CALLS)

    def test_stale_registration_and_unproven_ownership_never_authorize_reads(self):
        e = self.env()
        auth = e.authorize()
        self.assertEqual(e.db("setup", auth), dbread.OK)
        self.assertEqual(e.db("cleanup", auth), db_fixture.OK)
        self.assertTrue((e.home / "fixtures" / PACKAGE_ID / "registration.json").exists())
        self.assertEqual(e.db("prepare", auth), dbread.REFUSED)  # campaign closed by cleanup
        f = self.env()
        auth = f.authorize()
        self.assertEqual(f.db("setup", auth), dbread.OK)
        path = f.home / "fixtures" / PACKAGE_ID / "state.json"
        state = json.loads(path.read_text())
        state["bindings"]["project.source"] = "recovery listing"
        path.write_text(json.dumps(state))
        self.assertEqual(f.db("prepare", auth), dbread.REFUSED)
        self.assertIn("ownership", f.campaign_state()["stopped"]["reason"])
        # A lost create response: unresolved, nothing bound, read, registered or deleted.
        g = self.env({"create": "timeout_after", "stall_seconds": 5})
        auth = g.authorize()
        self.assertEqual(g.db("setup", auth), dbread.STOPPED)
        self.assertEqual(g.db("prepare", auth), dbread.REFUSED)
        self.assertEqual(g.db("cleanup", auth), db_fixture.BLOCKED)
        self.assertEqual(g.calls(), ["POST /projects", "GET /projects", "GET /projects"])
        self.assertEqual(g.campaign_state()["fixture"]["obligation"], "unresolved")
        self.assertIsNone(g.world()["projects"][0]["deleted_at"])
        self.assertClean(g)

    def test_exhausted_read_budget_and_expiry_keep_cleanup_available(self):
        e, auth = self.ready()
        path = e.home / "campaigns" / CAMPAIGN / "state.json"
        state = json.loads(path.read_text())
        state["budget"]["reserved"] = 6  # exhausted by earlier reads (simulated)
        path.write_text(json.dumps(state))
        self.assertEqual(e.db("run", auth), dbread.STOPPED)
        self.assertEqual(e.campaign_state()["stopped"]["reason"], "read budget exhausted")
        self.assertEqual(e.calls(), SETUP_CALLS)
        self.assertEqual(e.db("cleanup", auth), db_fixture.OK)
        self.assertEqual(e.calls()[-3:], DELETE_CALLS)
        f = self.env()
        soon = datetime.now(timezone.utc) + timedelta(minutes=5)
        auth = f.authorize(expires_at=soon.isoformat())
        self.assertEqual(f.db("setup", auth), dbread.OK)
        self.assertEqual(f.db("prepare", auth), dbread.OK)

        class Later(datetime):  # the clock just after the main window, without waiting
            @classmethod
            def now(cls, tz=None):
                return soon + timedelta(seconds=1)
        with mock.patch.object(db_fixture, "datetime", Later):
            self.assertEqual(f.db("run", auth), dbread.INVALID)  # main window over
            self.assertEqual(f.db("cleanup", auth), db_fixture.OK)  # explicit cleanup window
        self.assertEqual(f.calls(), SETUP_CALLS + DELETE_CALLS)

    def test_interrupted_read_is_uncertain_and_never_replayed(self):
        e, auth = self.ready({"case": "hang_before", "stall_seconds": 30}, case_timeout=25)
        argv = [sys.executable, "-m", "seren_harness", "dbread", "run", "--package",
                str(e.package), "--manifest", str(e.manifest), "--authorization", str(auth),
                "--home", str(e.home), "--root", str(e.root)]
        process = subprocess.Popen(argv, cwd=str(SOURCE), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   env=dict(os.environ, PYTHONPATH=str(SOURCE)))
        try:
            for _ in range(200):
                if e.calls().count(READ[0]) == 3:  # A4, A5, then the first read
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            process.send_signal(signal.SIGINT)
            process.wait(15)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(e.campaign_state()["cases"][CASES[0]]["status"], "in_flight")
        e.scenario({})
        self.assertEqual(e.db("run", auth), dbread.STOPPED)
        self.assertEqual(e.results()[CASES[0]], "uncertain_interrupted")
        self.assertEqual(e.campaign_state()["budget"], {"max_service_requests": 6,
                         "reserved": 2, "commands_issued": 0, "unknown": 2})
        self.assertEqual(e.calls().count(READ[0]), 3)
        self.assertEqual(e.db("cleanup", auth), db_fixture.OK)

    def test_two_sessions_complete_the_flow_from_verified_state(self):
        e = self.env()
        e.authorize()
        self.assertEqual(e.coordinate(), dbread.OK, e.output)
        record = e.sessions()
        self.assertEqual([s["status"] for s in record["sessions"]], ["exited", "exited"])
        first, second = (s["allowed_commands"] for s in record["sessions"])
        self.assertEqual([c.split()[4] for c in first], ["setup", "prepare", "run", "status"])
        self.assertEqual([c.split()[4] for c in second],
                         ["verify-handoff", "run", "cleanup", "status"])
        self.assertEqual(e.calls(), SETUP_CALLS + READ * 3 + DELETE_CALLS)
        self.assertEqual(e.results(), {k: "pass" for k in CASES})
        self.assertEqual(record["coordinator_cleanup"]["action"],
                         "none needed (obligation closed)")
        launches = [json.loads(line) for line in e.log.read_text().splitlines()]
        self.assertEqual([x["event"] for x in launches], ["start", "end", "start", "end"])
        self.assertTrue(all(x["seren_env"] == [] and not x["api_key"] for x in launches
                            if x["event"] == "start"))
        self.assertEqual(e.coordinate(), dbread.REFUSED)  # no third session
        self.assertEqual(len(e.calls()), 16)
        self.assertClean(e)

    def test_session_two_refused_on_changed_state_then_coordinator_cleans_up(self):
        e = self.env()
        e.authorize()
        state = {"n": 0}

        def popen(*args, **kwargs):
            state["n"] += 1
            if state["n"] == 2:
                raise AssertionError("session 2 must not launch")
            process = subprocess.Popen(*args, **kwargs)
            original = process.communicate

            def communicate(*a, **k):
                result = original(*a, **k)
                path = e.workspace() / "targets" / "seren.json"
                path.write_text(path.read_text().replace('"timeout_seconds": 2',
                                                         '"timeout_seconds": 3'))
                return result
            process.communicate = communicate
            return process
        self.assertEqual(e.coordinate(popen=popen), dbread.STOPPED)
        record = e.sessions()
        self.assertEqual(record["used"], 1)
        self.assertIn("handoff not verified", record["stopped"]["reason"])
        self.assertIn("coordinator ran dbread cleanup", record["coordinator_cleanup"]["action"])
        self.assertEqual(e.calls(), SETUP_CALLS + READ + DELETE_CALLS)
        self.assertIsNotNone(e.world()["projects"][0]["deleted_at"])

    def test_session_timeout_and_lost_create_leave_explicit_obligations(self):
        e = self.env()
        e.authorize()
        with mock.patch.object(dbread.DbReadCoordinator, "SESSION_SECONDS", 1):
            self.assertEqual(e.coordinate(mode="hang"), dbread.STOPPED)
        record = e.sessions()
        self.assertEqual((record["used"], record["sessions"][0]["status"]), (1, "timed_out"))
        self.assertEqual(record["coordinator_cleanup"]["action"], "none needed (obligation None)")
        self.assertEqual(e.calls(), [])
        f = self.env({"create": "timeout_after", "stall_seconds": 5})
        f.authorize()
        self.assertEqual(f.coordinate(), dbread.STOPPED)
        record = f.sessions()
        self.assertEqual(record["used"], 1)
        self.assertEqual(record["coordinator_cleanup"]["action"],
                         "none needed (obligation unresolved)")
        self.assertNotIn("DELETE /projects/{id}", f.calls())
        self.assertEqual(f.campaign_state()["fixture"]["obligation"], "unresolved")
        self.assertClean(f)


if __name__ == "__main__":
    unittest.main()
