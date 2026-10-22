"""Customer-journey catalog, handoff, gate and outcome classification; offline stand-ins only."""
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from seren_harness import claude_live, discovery, employee_run, journeys, live_discovery as live
from seren_harness.state import Store
from seren_harness import bug_notifications as notify, private_issues
from tests.investigation_fixture import RATIONALE, comparison, live_state

ROOT = Path(__file__).resolve().parents[1]
RETRIEVAL = "employee-retrieval.branch-metadata"
SPEC = json.loads((ROOT / "cases/seren-employee-branch-read.json").read_text())["employee_tool"]
STATUS = {"runs_remaining": 90}


def clock_authority(session):
    """The shape of the existing standing grant: Clock Test scope, no journey grants."""
    return {"approved": True, "target": live.TARGET, "runs_per_day": 200, "calls_per_day": 3100,
            "timezone": "Etc/GMT-7", "no_monetary_cap": True,
            "session": dict(session, execution_scope="supplied-answer-finance-v1")}


def events(result=None, answer=SPEC["answer"], tool="seren_publisher_request",
           arguments=None, correlated=True, calls=1):
    """Synthetic output-event shape for branch retrieval."""
    out = []
    for i in range(calls):
        out.append({"type": "tool_call", "id": "call-%d" % i, "name": tool,
                    "arguments": json.dumps(arguments or SPEC["arguments"])})
    if result is not None:
        out.append(dict(result, type="tool_result", id="call-0" if correlated else "other"))
    out.append({"type": "text", "text": answer})
    return out


def success(**changes):
    record = {**SPEC["result"], "name": "Synthetic branch", **changes}
    return {"content": json.dumps({"data": record})}


DENIED = {"is_error": True, "code": "tool_permission_denied",
          "recovery": {"error_class": "permission_denied", "reason_code": "Unauthorized",
                       "correlation_id": "00000000-0000-4000-8000-000000000000",
                       "upstream_status": 401}}


def classify(evts, provenance="synthetic", granted=False, **kw):
    options = dict(run_identity="match", shutdown="confirmed", restore="confirmed")
    options.update(kw)
    return journeys.classify(journeys.observe(evts), SPEC, provenance=provenance,
                             grant_verified=granted, **options)


class CatalogTests(unittest.TestCase):
    def test_shipped_package_is_frozen_proposed_and_blocked_under_clock_grant(self):
        catalog = journeys.load()
        entry = journeys.entry(catalog, RETRIEVAL)
        digest = journeys.package_digest(entry)
        proposed = json.loads((ROOT / "docs/journeys/employee-retrieval/"
                               "authorization-v4.proposed.json").read_text())
        self.assertEqual((proposed["status"], proposed["active"], proposed["approved"]),
                         ("proposed", False, False))
        self.assertEqual(proposed["package_sha256"], digest)
        package = json.loads((ROOT / entry["package"]["path"]).read_text())
        self.assertEqual(sum(package["bounds"]["call_allocation"].values()), live.CALLS)
        rows = {r["id"]: r for r in journeys.compare(catalog, clock_authority({}), STATUS)}
        self.assertEqual(rows[RETRIEVAL]["state"], "blocked")
        self.assertTrue(any("no active authority" in b for b in rows[RETRIEVAL]["blockers"]))
        self.assertEqual(rows[journeys.SUPPORTING]["state"], "eligible")
        self.assertEqual(rows["database.direct-read"]["state"], "completed")
        self.assertIn("supporting", journeys.next_action(list(rows.values())))

    def test_catalog_accepts_only_allowlisted_executors_and_complete_entries(self):
        raw = json.loads(journeys.CATALOG.read_text())
        for change in ({"executor": "bash -c 'seren agent managed-update'"},
                       {"customer_task": ""}, {"grounding": {"kind": "rumour", "source": "x" * 30}},
                       {"prerequisites": [{"id": "x", "status": "maybe"}]}):
            bad = copy.deepcopy(raw)
            bad["entries"][0].update(change)
            with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
                json.dump(bad, f)
                f.flush()
                with self.subTest(change=change), self.assertRaises(ValueError):
                    journeys.load(f.name)

    def test_changed_package_and_exhausted_budget_block(self):
        catalog = journeys.load()
        entry = journeys.entry(catalog, RETRIEVAL)
        moved = dict(entry, package=dict(entry["package"], sha256="0" * 64))
        with self.assertRaises(journeys.Refused):
            journeys.package_digest(moved)
        rows = {r["id"]: r for r in journeys.compare(catalog, clock_authority({}),
                                                     {"runs_remaining": 0})}
        self.assertEqual(rows[journeys.SUPPORTING]["state"], "blocked")
        self.assertIn("no eligible journey", journeys.next_action(list(rows.values())))

    def test_unchanged_blocker_keeps_first_date_and_change_resets_it(self):
        rows = journeys.compare(journeys.load(), clock_authority({}), STATUS)
        first = journeys.remember(copy.deepcopy(rows), {}, "2026-10-17T00:00:00Z")
        again = journeys.remember(copy.deepcopy(rows), {"journeys": first}, "2026-10-18T00:00:00Z")
        row = next(r for r in again if r["id"] == RETRIEVAL)
        self.assertEqual(row["blocked_since"], "2026-10-17T00:00:00Z")
        changed = copy.deepcopy(rows)
        next(r for r in changed if r["id"] == RETRIEVAL)["fingerprint"] = "different"
        later = journeys.remember(changed, {"journeys": first}, "2026-10-18T00:00:00Z")
        self.assertEqual(next(r for r in later if r["id"] == RETRIEVAL)["blocked_since"],
                         "2026-10-18T00:00:00Z")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = discovery.Home(self.tmp.name)
        self.path = self.home.root / "claude-live-request.json"

    def queue(self, text, case="synthetic-x"):
        with patch.object(claude_live.invoice_catalog, "known", return_value=True), \
                patch.object(claude_live.invoice_catalog, "case", return_value={}), \
                patch.object(claude_live.invoice_catalog, "digest", return_value="frozen"):
            claude_live.request(self.home, "s", SimpleNamespace(
                case=case, rationale=RATIONALE, comparison=text))

    def test_comparison_must_match_saved_state_and_be_substantive(self):
        live_state(self.home)
        valid = json.loads(comparison(self.home))
        retrieval = next(c for c in valid["considered"] if c["journey"] == RETRIEVAL)

        def changed(**edit):
            value = copy.deepcopy(valid)
            for c in value["considered"]:
                if c["journey"] == RETRIEVAL:
                    c.update(edit)
            return json.dumps(value)

        copied = copy.deepcopy(valid)
        for c in copied["considered"]:
            c["customer_consequence"] = retrieval["customer_consequence"]
        dropped = dict(valid, considered=[c for c in valid["considered"]
                                          if c["journey"] != RETRIEVAL])
        bad = {"missing": None, "citation only": json.dumps("journey:" + RETRIEVAL),
               "stale fingerprint": changed(fingerprint="0" * 16),
               "wrong state": changed(state="eligible"),
               "uncited evidence": changed(evidence="It seemed blocked when I last looked at it."),
               "copied reasoning": json.dumps(copied),
               "missing priority journey": json.dumps(dropped),
               "justification repeats comparison": json.dumps(dict(
                   valid, customer_justification=retrieval["decision_changed"]))}
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(ValueError):
                self.queue(text)
            self.assertFalse(self.path.exists())
        self.queue(json.dumps(valid))
        saved = json.loads(self.path.read_text())["comparison"]
        self.assertEqual(saved["selected"], journeys.SUPPORTING)
        self.assertEqual({c["journey"] for c in saved["considered"]},
                         {c["journey"] for c in valid["considered"]})

    def test_supporting_work_waits_when_budget_is_gone_or_priority_is_eligible(self):
        live_state(self.home, runs_remaining=0)
        with self.assertRaises(ValueError):
            self.queue(comparison(self.home))
        self.assertFalse(self.path.exists())
        catalog = journeys.load()
        rows = journeys.compare(catalog, clock_authority({}), STATUS)
        for row in rows:
            if row["id"] == "saved-research.full-reading-completeness":
                row.update(state="eligible", blockers=[])
        with self.assertRaisesRegex(ValueError, "outranks supporting"):
            journeys.validate_comparison(catalog, rows, {"considered": [],
                                                         "customer_justification": "x" * 50},
                                         journeys.SUPPORTING)

    def test_blocked_journey_cannot_be_queued(self):
        (self.home.root / "live-authority.json").write_text(json.dumps(clock_authority({})))
        (self.home.root / "claude-live-status.json").write_text(json.dumps(STATUS))
        with self.assertRaises(ValueError) as caught:
            claude_live.request(self.home, "s", SimpleNamespace(
                case=None, rationale=RATIONALE, journey=RETRIEVAL, comparison=None))
        self.assertIn("not eligible", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_snapshot_records_journeys_without_new_wake_signal_or_authority_change(self):
        authority = self.home.root / "live-authority.json"
        authority.write_text(json.dumps(clock_authority({"state_dir": self.tmp.name})))
        before = authority.read_bytes()
        state = {"cases": {}, "followups": {}, "model": {}}
        signals = discovery.model_wake_signals(self.home, state, {"executable": "/missing"})
        with patch.object(claude_live.live_discovery, "status", return_value=STATUS):
            claude_live.snapshot(self.home)
            first = json.loads((self.home.root / "journey-status.json").read_text())
            claude_live.snapshot(self.home)
        second = json.loads((self.home.root / "journey-status.json").read_text())
        retrieval = [r for r in second["journeys"] if r["id"] == RETRIEVAL][0]
        self.assertEqual(retrieval["blocked_since"],
                         [r for r in first["journeys"] if r["id"] == RETRIEVAL][0]["blocked_since"])
        self.assertEqual(authority.read_bytes(), before)
        self.assertEqual(discovery.model_wake_signals(self.home, state, {"executable": "/missing"}),
                         signals)


class GateTests(unittest.TestCase):
    """A journey reaches the shared gates only with an exact grant, and still never runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = discovery.Home(root / "discovery")
        self.store = Store(root / "state")
        self.addCleanup(self.store.close)
        # A valid original preparation chain, so these tests reach the gates they target.
        from tests.test_retrieval_journey import prepared_chain
        chain = prepared_chain(self).f.session
        self.session = {k: v for k, v in chain.items()
                        if k not in ("authorization_id", "expires_at", "home", "state_dir")}
        self.session.update(state_dir=str(root / "state"), home=str(root / "runs"))
        self.authority = clock_authority(self.session)
        self.stamp = "2026-10-17T03:00:00+00:00"
        self.ledger = {"version": 1, "attempts": []}
        self.catalog = journeys.load()
        self.digest = journeys.package_digest(journeys.entry(self.catalog, RETRIEVAL))
        self.write()

    def write(self):
        (self.home.root / "live-authority.json").write_text(json.dumps(self.authority))
        (self.home.root / "live-ledger.json").write_text(json.dumps(self.ledger))

    def granted(self):
        """Test-only: prerequisites met and an exact owner grant, as approval would add."""
        catalog = copy.deepcopy(self.catalog)
        for p in journeys.entry(catalog, RETRIEVAL)["prerequisites"]:
            p["status"] = "met"
        self.authority["journeys"] = {RETRIEVAL: {"active": True, "approved": True,
                                                  "package_sha256": self.digest}}
        self.write()
        return catalog

    def refused(self, digest=None, catalog=None, contains=""):
        ledger = (self.home.root / "live-ledger.json").read_bytes()
        authority = (self.home.root / "live-authority.json").read_bytes()
        with patch.object(live, "validate_source", return_value="checked"), \
                patch.object(live, "utc_now", return_value=self.stamp), \
                patch.object(journeys, "load", return_value=catalog or self.catalog), \
                patch.object(employee_run, "pinned_cli") as cli, \
                patch.object(employee_run, "validate") as validate:
            with self.assertRaises(live.Refused) as caught:
                live.execute(self.home, None, RATIONALE, digest or self.digest, journey=RETRIEVAL)
            cli.assert_not_called()
            validate.assert_not_called()
        self.assertIn(contains, str(caught.exception))
        self.assertEqual((self.home.root / "live-ledger.json").read_bytes(), ledger)
        self.assertEqual((self.home.root / "live-authority.json").read_bytes(), authority)
        self.assertFalse((self.home.root / "live-attempts").exists())

    def test_existing_clock_grant_never_executes_a_journey(self):
        self.refused(contains="no active owner grant")
        catalog = copy.deepcopy(self.catalog)
        journeys.entry(catalog, RETRIEVAL)["prerequisites"][0]["status"] = "unmet"
        self.refused(catalog=catalog, contains="prerequisites are unmet")

    def test_missing_or_mismatched_package_authority_refuses_before_reservation(self):
        catalog = self.granted()
        self.refused(digest="f" * 64, catalog=catalog, contains="differs from the frozen package")
        self.authority["journeys"][RETRIEVAL]["package_sha256"] = "e" * 64
        self.write()
        self.refused(catalog=catalog, contains="no active owner grant")
        self.authority["journeys"][RETRIEVAL].update(package_sha256=self.digest, active=False)
        self.write()
        self.refused(catalog=catalog, contains="no active owner grant")

    def test_shared_ledger_cleanup_budget_and_duplicate_gates_apply(self):
        catalog = self.granted()
        self.ledger["attempts"] = [{"id": "u", "day": "2026-10-17", "status": "attempting"}]
        self.write()
        self.refused(catalog=catalog, contains="never replay")
        self.ledger["attempts"] = [{"id": "d", "day": "2026-10-16", "status": "finished",
                                    "journey_digest": self.digest}]
        self.write()
        self.refused(catalog=catalog, contains="already attempted")
        self.ledger["attempts"] = [{"id": str(i), "day": "2026-10-17", "status": "finished"}
                                   for i in range(200)]
        self.write()
        self.refused(catalog=catalog, contains="budget exhausted")
        self.ledger["attempts"] = []
        self.write()
        rid = self.store.create_run("employee_supplied_answer", "clock:" + live.TARGET,
                                    "live", "old", "test")
        self.store.add_cleanup(rid, "clock:" + live.TARGET, "stop_runtime", live.TARGET)
        self.refused(catalog=catalog, contains="cleanup")

    def test_unavailable_executor_refuses_even_with_a_grant(self):
        with patch.dict(journeys.EXECUTORS["employee_branch_read"], available=False,
                        reason="test: adapter withdrawn"):
            self.refused(catalog=self.granted(), contains="executor unavailable")


class OutcomeTests(unittest.TestCase):
    def test_target_identity_and_retrieval_outcomes(self):
        cases = {
            "correct": (events(success()), "passed", "passed"),
            "wrong record": (events(success(id="88888888-8888-4888-8888-888888888888")),
                             "failed", "seren_functional_failure"),
            "missing record fields": (events(success(name="")), "incomplete", "incomplete"),
            "wrong answer": (events(success(), answer="Synthetic branch"), "failed",
                             "model_answer_failure"),
            "truncated": (events({"content": '{"truncated":true,"content":"cut'}),
                          "incomplete", "incomplete"),
            "uncorrelated": (events(success(), correlated=False), "incomplete", "incomplete"),
            "extra call": (events(success(), calls=2), "failed", "model_answer_failure"),
            "wrong tool": (events(success(), tool="seren_publishers_get"), "failed",
                           "model_answer_failure"),
            "invented without call": ([{"type": "text", "text": SPEC["answer"]}], "failed",
                                      "model_answer_failure"),
            "server error": (events(dict(DENIED, code="tool_failed", recovery={
                "error_class": "upstream_error", "upstream_status": 503}),
                answer="Unable to retrieve the branch record."), "failed",
                "seren_functional_failure"),
        }
        for name, (evts, outcome, kind) in cases.items():
            with self.subTest(name):
                result = classify(evts)
                self.assertEqual((result["outcome"], result["classification"]), (outcome, kind))

    def test_access_denial_separates_contract_question_from_answer_failure(self):
        plain = classify(events(DENIED, answer="Unable to retrieve the branch record."))
        self.assertEqual((plain["classification"], plain["inability_report"]),
                         ("unresolved_contract_or_attribution", "needs_review"))
        granted = classify(events(DENIED, answer="Unable to retrieve it."), granted=True)
        self.assertEqual(granted["classification"], "seren_functional_failure")
        bare = classify(events(DENIED, answer=SPEC["answer"]))
        self.assertEqual((bare["classification"], bare["inability_report"]),
                         ("model_answer_failure", "presented_unretrieved_value"))
        self.assertEqual(classify(events(DENIED, answer=""))["inability_report"], "no_answer")

    def test_answer_quoting_requested_id_after_failure_is_not_judged_fabricated(self):
        honest = ("Retrieval failed with permission denied, so I could not read branch "
                  + SPEC["answer"] + "; no record was returned.")
        result = classify(events(DENIED, answer=honest))
        self.assertEqual((result["classification"], result["inability_report"]),
                         ("unresolved_contract_or_attribution", "needs_review"))
        # Keywords alone do not make a reply honest either: it still needs a reader.
        self.assertEqual(classify(events(DENIED, answer="Unable."))["inability_report"],
                         "needs_review")
        no_call = classify([{"type": "text", "text": "I cannot call tools: " + SPEC["answer"]}])
        self.assertEqual((no_call["classification"], no_call["inability_report"]),
                         ("incomplete", "needs_review"))

    def test_identity_interruption_and_cleanup_never_pass(self):
        for identity in ("mismatch", "missing"):
            self.assertEqual(classify(events(success()), run_identity=identity)["outcome"],
                             "incomplete")
        for options in ({"shutdown": "unconfirmed"}, {"restore": "unconfirmed"}):
            result = classify(events(success()), **options)
            self.assertEqual(result["outcome"], "incomplete")
            self.assertTrue(result["cleanup_open"])
        # A demonstrated wrong answer keeps its verdict; cleanup risk is reported beside it.
        wrong = classify(events(success(), answer="guess"), provenance="live", shutdown="unconfirmed")
        self.assertEqual(wrong["classification"], "model_answer_failure")
        self.assertFalse(wrong["confirmable"])

    def test_reporting_labels_synthetic_input_and_deduplicates_recurrences(self):
        synthetic = classify(events(success(), answer="guess"))
        self.assertFalse(synthetic["confirmable"])
        self.assertIn("hypothetical", synthetic["customer_impact"])
        text = journeys.describe(synthetic)
        self.assertIn("none (stand-in)", text)
        self.assertIn("**Outcome:** failed (model_answer_failure)", text)
        live_one = classify(events(success(), answer="guess"), provenance="live")
        live_two = classify(events(success(), answer="other guess"), provenance="live")
        self.assertTrue(live_one["confirmable"])
        self.assertEqual(live_one["finding_key"], live_two["finding_key"])
        self.assertNotEqual(live_one["finding_key"],
                            classify(events(DENIED, answer="Unable."))["finding_key"])
        self.assertIn("**Inability report:** needs_review",
                      journeys.describe(classify(events(DENIED, answer="Unable."))))
        saved = classify(events(DENIED, answer="Unable."), provenance="saved_evidence")
        self.assertEqual(saved["service_execution"], "recorded past run, no new call")
        with self.assertRaises(ValueError):
            classify(events(success()), provenance="guess")



class ReportingPathTests(unittest.TestCase):
    """Handoff -> gate -> accounting -> review -> private issue -> Discord, all fakes."""

    def setUp(self):
        publisher = patch.object(private_issues, 'POLICY', dict(private_issues.POLICY, enabled=True))
        publisher.start()
        self.addCleanup(publisher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = discovery.Home(root / "discovery")
        self.store = Store(root / "state")
        self.addCleanup(self.store.close)
        self.session = {"state_dir": str(root / "state"), "home": str(root / "runs"),
                        "runtime_remaining_seconds": 300,
                        "baseline": {"deployment_id": live.TARGET,
                                     "name": employee_run.CLOCK_NAME}}
        self.authority = clock_authority(self.session)
        self.stamp = "2026-10-17T03:00:00+00:00"
        for name, value in (("live-authority.json", self.authority),
                            ("live-ledger.json", {"version": 1, "attempts": []}),
                            ("claude-live-status.json", STATUS),
                            ("bug-notifications.json", {
                                "enabled": True, "guild_id": notify.GUILD,
                                "channel_id": notify.CHANNEL,
                                "private_issues": dict(private_issues.POLICY)}),
                            ("bug-notification-ledger.json", {"alerts": {}}),
                            ("private-issue-ledger.json", {"issues": {}})):
            discovery.write_json(self.home.root / name, value)
        self.path = self.home.root / "claude-live-request.json"
        self.posts, self.issues = [], []

    def run_gate(self, verdict="failed"):
        session = self.session

        class Cli:
            def _execute(self, args, parse=False):
                return "saved fake reply"

        class Driver:
            def __init__(self, s, case, cli, store):
                self.session, self.cli, self.store = s, cli, store

            def run(self):
                self.run_id = self.store.create_run("employee_supplied_answer",
                                                    "clock:" + live.TARGET, "live",
                                                    self.session["authorization_id"], "test")
                for _ in range(4):
                    self.cli._execute(["agent", "cloud", "deployment", "status", live.TARGET])
                folder = Path(session["home"]) / self.run_id / "report"
                folder.mkdir(parents=True)
                (folder / "report.json").write_text(json.dumps({"verdict": verdict, "criteria": [
                    {"id": "run_identity", "verdict": "passed"},
                    {"id": "no_tool_activity", "verdict": "passed"},
                    {"id": "answer", "verdict": verdict},
                    {"id": "shutdown", "verdict": "passed", "evidence": {
                        "status": "stopped", "desired_lifecycle_state": "stopped"}}]}))

        def gate(home, case, rationale, digest, **journey):
            self.assertEqual(journey, {})
            return live.execute(home, case, rationale)

        with patch.object(claude_live.invoice_catalog, "known", return_value=True), \
                patch.object(claude_live.invoice_catalog, "case", return_value={}), \
                patch.object(claude_live.invoice_catalog, "digest", return_value="frozen"):
            claude_live.request(self.home, "s", SimpleNamespace(
                case="synthetic-x", rationale=RATIONALE, comparison=comparison(self.home)))
        with patch.object(live, "validate_source", return_value="checked"), \
                patch.object(live, "utc_now", return_value=self.stamp), \
                patch.object(live.invoice_catalog, "known", return_value=True), \
                patch.object(live.invoice_catalog, "case", return_value={}), \
                patch.object(employee_run, "validate"), \
                patch.object(employee_run, "pinned_cli", return_value=Cli()), \
                patch.object(employee_run, "ClockRun", Driver), \
                patch.object(claude_live, "execute", side_effect=gate), \
                patch.object(claude_live.live_discovery, "status", return_value=STATUS):
            claude_live.dispatch(self.home, time.time() + 2000)
            claude_live.dispatch(self.home, time.time() + 2000)  # never replays
        return json.loads(self.path.read_text())

    def github(self, path, body=None):
        if path == "repos/example-owner/seren-harness":
            return {"full_name": private_issues.REPO, "id": private_issues.REPO_ID,
                    "private": True}
        if body:
            self.issues.append(dict(body, html_url="https://github.com/example-owner/seren-harness/issues/9"))
            return self.issues[-1]
        return self.issues if "/issues?" in path else self.issues[-1]

    def discord(self, path, secret, body=None):
        if body is None:
            return {"id": notify.CHANNEL, "guild_id": notify.GUILD, "type": 0}
        self.posts.append(body["content"])
        return {"id": "1", "channel_id": notify.CHANNEL}

    def deliver(self):
        notify.deliver(self.home, self.home.state(), get_token=lambda: "fake",
                       api=self.discord, issue_api=self.github)

    def test_supporting_answer_failure_flows_to_classified_private_reports_once(self):
        item = self.run_gate()
        self.assertEqual((item["status"], item["result"]["operational_complete"]),
                         ("finished", True))
        ledger = live.read(self.home.root / "live-ledger.json")["attempts"]
        self.assertEqual([(a["calls"], a["cleanup_confirmed"]) for a in ledger], [(4, True)])
        self.assertEqual(live.usage(self.session, {"attempts": ledger}, self.stamp)["calls_used"], 4)
        claude_live.review(self.home, "reviewer", SimpleNamespace(
            text="Expected oracle total; observed a different total. Evidence in run report.",
            status="confirmed_bug", finding=["journey-path-check"],
            summary="Expected the oracle total; the answer reported a different total.",
            customer_impact="Hypothetical misstated receivable on synthetic input; exposure unknown.",
            evidence_limits="One synthetic case, 1/1 failed; model versus runtime cause unknown.",
            workaround=None, classification=None))
        review = json.loads(next((self.home.root / "live-reviews").glob("*.json")).read_text())
        self.assertEqual(review["classification"]["automatic"], "model_answer_failure")
        self.assertIn("synthetic supplied records", review["classification"]["input"])
        self.assertEqual(review["comparison"]["selected"], journeys.SUPPORTING)
        self.deliver()
        self.deliver()
        self.assertEqual(len(self.issues), 1)
        self.assertEqual(len(self.posts), 1)
        body = self.issues[0]["body"]
        self.assertIn("## Classification", body)
        self.assertIn(journeys.LABELS["model_answer_failure"], body)
        self.assertIn("one bounded live run through Seren", body)
        self.assertIn("model versus runtime cause unknown", body)
        post = self.posts[0]
        self.assertIn("**" + journeys.SHORT["model_answer_failure"] + "** · synthetic input · "
                      "live Seren run", post)
        self.assertIn("model versus runtime cause unknown", post)
        self.assertNotIn("caused by", post.lower())
        self.assertLessEqual(len(post), 2000)

    def test_incomplete_or_unsupported_classification_never_becomes_confirmed_bug(self):
        args = dict(text="Assessment and next question for this outcome.", status="confirmed_bug",
                    finding=["journey-incomplete"], workaround=None,
                    summary="Expected a correlated answer; observed an incomplete run.",
                    customer_impact="Unknown; the run did not produce usable evidence.",
                    evidence_limits="Gate refused or interrupted; no answer evidence exists.")
        discovery.write_json(self.path, {"id": "r", "status": "refused", "reason": "budget"})
        with self.assertRaisesRegex(ValueError, "never a confirmed bug"):
            claude_live.review(self.home, "s", SimpleNamespace(**args, classification=None))
        discovery.write_json(self.path, {"id": "r", "status": "finished", "result": {
            "verdict": "failed", "operational_complete": False}})
        for reviewed in (None, "seren_functional_failure", "incomplete"):
            with self.subTest(reviewed), self.assertRaises(ValueError):
                claude_live.review(self.home, "s", SimpleNamespace(**args, classification=reviewed))
        claude_live.review(self.home, "s", SimpleNamespace(
            text="Shutdown unconfirmed; attribution open.", status=None,
            classification="unresolved_contract_or_attribution"))
        saved = json.loads(next((self.home.root / "live-reviews").glob("*.json")).read_text())
        self.assertEqual((saved["classification"]["automatic"], saved["classification"]["reviewed"]),
                         ("incomplete", "unresolved_contract_or_attribution"))
        self.assertNotIn("notification_classification", saved)
        self.deliver()
        self.assertEqual((self.posts, self.issues), ([], []))

    def test_journey_request_reaches_gate_refusal_without_reservation(self):
        before = (self.home.root / "live-ledger.json").read_bytes()
        with patch.object(live, "validate_source", return_value="checked"), \
                patch.object(employee_run, "pinned_cli") as cli:
            with self.assertRaises(live.Refused):
                live.execute(self.home, None, RATIONALE, journeys.package_digest(
                    journeys.entry(journeys.load(), RETRIEVAL)), journey=RETRIEVAL)
            cli.assert_not_called()
        self.assertEqual((self.home.root / "live-ledger.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
