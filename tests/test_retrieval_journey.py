"""Retrieval journey end to end with stand-ins only: handoff -> real gate -> real adapter ->
real fixed-host transport (fake opener) -> real ClockRun (fake CLI) -> rollback -> ledger ->
review -> fake private issue and Discord. No network, subprocess, credential or live call."""
import copy
import functools
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from seren_harness import (bug_notifications as notify, branch_read, claude_live, discovery,
                           employee_run, finance_invoices, grant_transport, journeys,
                           live_discovery as live, preview_diagnostic, private_issues,
                           retrieval_journey as rj)
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import CliResult, ERROR, OK
from seren_harness.state import Store
from tests.investigation_fixture import RATIONALE, comparison

RETRIEVAL = "employee-retrieval.branch-metadata"
TARGET = live.TARGET
ORIG = "99999999-9999-4999-8999-999999999999"  # the synthetic preparation chain's revision
SETUP_REV = "22222222-2222-4222-8222-222222222222"
RESTORED = "33333333-3333-4333-8333-333333333333"
RESTORED_AGAIN = "55555555-5555-4555-8555-555555555555"
KEY = "seren_" + "K" * 40
STAMP = "2026-10-17T03:00:00+00:00"
CATALOG = journeys.load()
ENTRY = journeys.entry(CATALOG, RETRIEVAL)
PACKAGE = rj.load_package(ENTRY)
DIGEST = journeys.package_digest(ENTRY)
CASE = rj.case(PACKAGE)
SPEC = CASE["employee_tool"]
BRANCH, PROJECT = SPEC["result"]["id"], SPEC["result"]["project_id"]
REAL_RUN = rj.GrantedBranchRun
BRANCH_RECEIPT = json.loads((Path(__file__).parent / "fixtures/branch-read-direct-20261017.json")
                            .read_text())["receipt"]
REAL_TRANSPORT = grant_transport.GrantTransport


def prepared_chain(test):
    """A real, hash-bound supplied-data preparation chain for the Clock Test target:
    the existing finance fixture (branch chain plus instruction-update journal)."""
    import test_employee_run
    import test_finance_employee
    case = copy.deepcopy(test_employee_run.CASE)
    case["employee_tool"]["deployment_id"] = TARGET
    with patch.object(test_employee_run, "CASE", case):
        parent = test_finance_employee.FinanceRunTests(
            "test_valid_handoff_sends_the_frozen_task_once_and_scores_the_full_answer")
        parent.setUp()
    test.addCleanup(parent.doCleanups)
    return parent


def original_config():
    return {"deployment_id": TARGET, "name": employee_run.CLOCK_NAME, "tool_presets": [],
            "approval_policy": "read_only", "max_iterations": 2, "max_tool_calls_per_run": 1,
            "max_timeout_seconds": 60, "private_output_policy": "control_plane",
            "max_tool_output_chars": 32768, "model_id": "synthetic-model",
            "bundle": {"instructions": [{"kind": "skill",
                                         "content": finance_invoices.instruction()}]}}


def baseline():
    c = original_config()
    return dict({k: c[k] for k in employee_run.BASELINE_FIELDS if k in c},
                active_revision_id=ORIG)


class FakeSeren:
    """One fake Clock Test employee; `modes` select failures. Records every request."""

    def __init__(self, modes=()):
        self.modes = set(modes)
        self.config, self.revision = original_config(), ORIG
        self.restorable = ORIG
        self.status = self.desired = "stopped"
        self.calls, self.patches, self.drifted = [], 0, False
        self.ready_reads = self.run_reads = 0
        self.preview_patch, self.readback_patch = {}, {}  # field changes for negative controls
        self.granted = dict(original_config(), tool_refs=[rj.REF],
                            bundle={"instructions": [{"kind": "skill",
                                                      "content": branch_read.prompt(SPEC)}]})

    def detail(self):
        value = dict(copy.deepcopy(self.config), active_revision_id=self.revision,
                     status=self.status)
        if "secret_requirements" in self.modes:  # managed-get only; preview views omit it
            value["managed_agent_secret_requirements"] = {"requirements_fingerprint": "f"}
        return value

    def raw_view(self, value):
        """Synthetic preview views: no revision, and
        empty/null optionals the CLI would not print."""
        value = {k: v for k, v in value.items()
                 if k not in ("active_revision_id", "managed_agent_secret_requirements")}
        return dict(value, allowed_remote_agent_origins=[], cron_schedule=None)

    # ---- pinned CLI stand-in
    def cli(self):
        fake = self

        class Cli:
            timeout = 30
            sanitizer = Sanitizer()

            def display_argv(self, args):
                return ["seren", *map(str, args)]

            def json(self, *args):
                return self._execute([*args, "-o", "json"], parse=True)

            def _execute(self, args, parse=False):
                return fake.handle([a for a in args if a not in ("-o", "json")])
        return Cli()

    def ok(self, data):
        return CliResult(OK, [], {"data": data}, 0)

    def handle(self, args):
        self.calls.append(args)
        if args[0] == "branches":
            if "missing_branch" in self.modes:
                return CliResult(ERROR, [], None, 1, "Failed to get branch: 404")
            # The pinned CLI prints the Branch object itself (real saved shape).
            return CliResult(OK, [], copy.deepcopy(BRANCH_RECEIPT["response"]), 0)
        verb = args[1]
        if verb == "managed-get":
            d = self.detail()
            if "stale_baseline" in self.modes:
                d["max_iterations"] = 3
            if self.revision == SETUP_REV and ("persistent_drift" in self.modes or (
                    "readback_drift" in self.modes and not self.drifted)):
                self.drifted = True
                d["model_id"] = "other-model"
            if self.revision == SETUP_REV:
                d.update(self.readback_patch)
            return self.ok(d)
        if verb == "managed-deployment-tools":
            tools = [{"name": n, "effective_policy": {"status": "allowed"}, "side_effecting": False,
                      "approval_type": "none", "checkpoint_required": False,
                      "input_schema": branch_read.SCHEMA if n == branch_read.TOOL else {}}
                     for n in sorted(employee_run.REVIEWED_TOOLS)]
            return self.ok({"deployment_id": TARGET, "tools": tools, "tool_presets": ["live_data"],
                            "approval_policy": "read_only",
                            "allowed_publisher_operations": sorted(employee_run.REVIEWED_OPERATIONS)})
        if verb == "cloud" and args[2] == "deployment":
            status = self.status
            if "slow_ready" in self.modes and self.status == "running" and self.ready_reads < 5:
                self.ready_reads += 1
                status = "building"
            return self.ok({"id": TARGET, "status": status,
                            "desired_lifecycle_state": self.desired})
        if verb == "managed-start":
            self.status = self.desired = "running"
            return self.ok({"status": "running"})
        if verb == "managed-stop":
            self.desired = "stopped"
            self.status = "stopping" if "stuck_stop" in self.modes else "stopped"
            return self.ok({"status": self.status})
        if verb == "cloud" and args[3] == "start":
            return CliResult(OK, [], "Run accepted for deployment %s.\nRun ID: %s\nExecution ID: e1\n"
                             % (TARGET, "44444444-4444-4444-8444-444444444444"), 0)
        if verb == "cloud" and args[3] == "get":
            record = self.run_record()
            if "slow_run" in self.modes and self.run_reads < 3:
                self.run_reads += 1
                record["status"] = "running"
            return self.ok(record)
        if verb == "managed-rollback":
            expected = args[args.index("--expected-active-revision-id") + 1]
            if ("rollback_fails" in self.modes or expected != self.revision
                    or args[3] != self.restorable):
                return CliResult(ERROR, [], None, 1, "rollback failed")
            restored = RESTORED if self.restorable == ORIG else RESTORED_AGAIN
            self.config, self.revision = original_config(), restored
            self.restorable = restored
            return self.ok(self.mutation(restored, "r"))
        raise AssertionError("unexpected CLI call " + repr(args))

    def run_record(self):
        call = {"type": "tool_call", "id": "c1", "name": branch_read.TOOL,
                "arguments": json.dumps(SPEC["arguments"])}
        record = {"id": BRANCH, "project_id": PROJECT, "name": "main"}
        result = {"type": "tool_result", "id": "c1", "is_error": False,
                  "content": json.dumps({"data": record})}
        answer = BRANCH
        if "wrong_record" in self.modes:
            result["content"] = json.dumps({"data": dict(record, id=SETUP_REV)})
        if "truncated" in self.modes:
            result["content"] = '{"truncated":true,"content":"cut'
        if "denied" in self.modes or "bare_after_denied" in self.modes:
            result = {"type": "tool_result", "id": "c1", "is_error": True,
                      "code": "tool_permission_denied",
                      "recovery": {"error_class": "permission_denied", "upstream_status": 401}}
            answer = (BRANCH if "bare_after_denied" in self.modes else
                      "Retrieval of branch %s failed (permission denied); no record was returned." % BRANCH)
        events = [call, result, {"type": "text", "text": answer},
                  {"type": "done", "status": "ok"}]
        if "no_call" in self.modes:
            events = [{"type": "text", "text": BRANCH}, {"type": "done", "status": "ok"}]
        return {"id": "44444444-4444-4444-8444-444444444444", "deployment_id": TARGET,
                "execution_id": "e1", "status": "completed",
                "completed_at": "2026-10-17T02:59:00Z",
                "invocation_payload": {"message": SPEC["message"]}, "output_events": events}

    def mutation(self, revision, request_id):
        return {"deployment": {"id": TARGET, "active_revision_id": revision},
                "reconciliation": {"request_id": request_id, "revision_id": revision,
                                   "state": "applied", "target": "next_start"}}

    # ---- fixed-host HTTPS stand-in (the real GrantTransport builds every request)
    def opener(self, request, timeout):
        assert request.full_url.startswith(grant_transport.BASE + "/deployments/" + TARGET)
        assert request.get_header("Authorization") == "Bearer " + KEY
        body = json.loads(request.data) if request.data else None
        self.calls.append(["http", request.get_method(), request.full_url])
        if request.full_url.endswith("/managed/preview"):
            proposed = copy.deepcopy(self.granted)
            if "broad_preview" in self.modes:
                proposed["external_databases"] = [{"project_id": PROJECT}]
            if "normalized_ref" in self.modes:
                proposed["tool_refs"] = [dict(rj.REF, permitted_actions=[])]
            current = self.detail()
            proposed = dict(proposed, active_revision_id=ORIG, status="stopped")
            if self.modes & {"raw_preview", "secret_requirements"}:
                current, proposed = self.raw_view(current), self.raw_view(proposed)
            current.update(self.preview_patch)
            data = {"deployment_id": TARGET, "current": current, "proposed": proposed,
                    "changed_fields": []}
        elif request.get_method() == "PATCH":
            self.patches += 1
            assert body == rj.setup_body(PACKAGE)
            if "interrupt_update" in self.modes:
                raise KeyboardInterrupt()
            if "reject_update" in self.modes:
                raise urllib.error.HTTPError(request.full_url, 409, "conflict", {}, None)
            self.config, self.revision = copy.deepcopy(self.granted), SETUP_REV
            self.request_id = request.get_header("Idempotency-key")
            if "lost_update" in self.modes:
                raise urllib.error.URLError("connection reset")
            data = self.mutation(SETUP_REV, self.request_id)
        else:
            if "lost_action" in self.modes:
                raise urllib.error.URLError("connection reset")
            data = self.mutation(SETUP_REV, self.request_id)
        return Response(request.full_url, {"data": data})


class Response(io.BytesIO):
    def __init__(self, url, value, status=200):
        super().__init__(json.dumps(value).encode())
        self.url, self.status = url, status

    def geturl(self):
        return self.url


class Attempt:
    """A fresh discovery home, private stand-in state and an owner grant for the package."""

    def __init__(self, test, modes=(), grant=True, runs=90):
        policy = patch.dict(private_issues.POLICY, enabled=True)
        policy.start()
        test.addCleanup(policy.stop)
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = discovery.Home(root / "discovery")
        self.service = FakeSeren(modes)
        self.chain = prepared_chain(test).f
        test.assertEqual(self.chain.baseline, baseline())
        self.session = {k: v for k, v in self.chain.session.items()
                        if k not in ("authorization_id", "expires_at", "home", "state_dir")}
        self.session.update(state_dir=str(root / "state"), home=str(root / "runs"))
        self.key_reads, self.transports = [], []
        self.authority = {"approved": True, "target": TARGET, "runs_per_day": 200,
                          "calls_per_day": 3100, "timezone": "Etc/GMT-7",
                          "no_monetary_cap": True, "session": self.session}
        if grant:
            self.authority["journeys"] = {RETRIEVAL: {"active": True, "approved": True,
                                                      "package_sha256": DIGEST}}
        for name, value in (("live-authority.json", self.authority),
                            ("live-ledger.json", {"version": 1, "attempts": []}),
                            ("claude-live-status.json", {"runs_remaining": runs}),
                            ("bug-notifications.json", {
                                "enabled": True, "guild_id": notify.GUILD,
                                "channel_id": notify.CHANNEL,
                                "private_issues": dict(private_issues.POLICY)}),
                            ("bug-notification-ledger.json", {"alerts": {}}),
                            ("private-issue-ledger.json", {"issues": {}})):
            discovery.write_json(self.home.root / name, value)
        self.clock = [1000.0]
        self.posts, self.issues = [], []

    def write_authority(self):
        discovery.write_json(self.home.root / "live-authority.json", self.authority)

    def rebasis(self, **changes):
        """Rewrite the preparation receipt and rebind its hash, like a re-made receipt."""
        self.chain.basis.update(changes)
        self.chain.write_basis()
        self.session["setup_receipt_sha256"] = self.chain.session["setup_receipt_sha256"]
        self.write_authority()

    def read_key(self):
        self.key_reads.append(1)
        return KEY

    def make_transport(self, *args, **kwargs):
        self.transports.append(1)
        return REAL_TRANSPORT(*args, **kwargs)

    def tick(self, seconds):
        self.clock[0] += max(0, seconds)

    def patches(self):
        driver = functools.partial(REAL_RUN, clock=lambda: self.clock[0], sleep=self.tick,
                                   wall=lambda: STAMP)
        return [patch.object(live, "validate_source", return_value="checked"),
                patch.object(live, "utc_now", return_value=STAMP),
                patch.object(employee_run, "pinned_cli", return_value=self.service.cli()),
                patch.object(grant_transport, "keychain_api_key", side_effect=self.read_key),
                patch.object(grant_transport, "GrantTransport", side_effect=self.make_transport),
                patch.object(grant_transport, "_urlopen", self.service.opener),
                patch.object(rj, "GrantedBranchRun", driver),
                patch("subprocess.Popen", side_effect=AssertionError("subprocess")),
                patch("socket.socket", side_effect=AssertionError("network"))]

    def handoff(self):
        """Queue through worker live-request, dispatch through the trusted wrapper."""
        claude_live.request(self.home, "s", SimpleNamespace(
            case=None, rationale=RATIONALE, journey=RETRIEVAL,
            comparison=comparison(self.home, RETRIEVAL)))

        def wrapper(home, case, rationale, digest, journey=None):
            return live.execute(home, case, rationale, digest, journey=journey)
        active = self.patches()
        for p in active:
            p.start()
        try:
            with patch.object(claude_live, "execute", side_effect=wrapper), \
                    patch.object(claude_live.live_discovery, "status",
                                 return_value={"runs_remaining": 89}):
                claude_live.dispatch(self.home, time.time() + 2000)
                claude_live.dispatch(self.home, time.time() + 2000)  # never replays
        finally:
            for p in reversed(active):
                p.stop()
        return json.loads((self.home.root / "claude-live-request.json").read_text())

    def ledger(self):
        return live.read(self.home.root / "live-ledger.json")["attempts"]

    def store(self):
        return Store(self.session["state_dir"])

    def github(self, path, body=None):
        if path == "repos/example-owner/seren-harness":
            return {"full_name": private_issues.REPO, "id": private_issues.REPO_ID, "private": True}
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
        notify.deliver(self.home, self.home.state(), get_token=lambda: "fake-bot",
                       api=self.discord, issue_api=self.github)


def run_gate(attempt, **kw):
    """The real gate with only the external edges replaced."""
    patches = attempt.patches()
    for p in patches:
        p.start()
    try:
        with patch.object(journeys, "load", return_value=kw.pop("catalog", CATALOG)):
            return live.execute(attempt.home, None, RATIONALE, kw.pop("digest", DIGEST),
                                journey=RETRIEVAL)
    finally:
        for p in reversed(patches):
            p.stop()


class EndToEndTests(unittest.TestCase):
    def test_exact_setup_retrieval_shutdown_restore_and_rebinding(self):
        a = Attempt(self)
        item = a.handoff()
        result = item["result"]
        self.assertEqual((item["status"], result["status"], result["classification"]),
                         ("finished", "finished", "passed"))
        self.assertEqual(a.service.patches, 1)
        self.assertEqual((a.service.revision, a.service.config), (RESTORED, original_config()))
        self.assertEqual(result["restore"]["status"], "restored")
        [entry] = a.ledger()
        sent = len(a.service.calls)
        self.assertEqual(entry["calls"], sent)  # every CLI and HTTPS call journaled
        self.assertLessEqual(sent, live.CALLS)
        self.assertEqual(live.usage(self.session_of(a), {"attempts": a.ledger()}, STAMP)["calls_used"], sent)
        receipts = (a.home.root / "live-attempts" / entry["id"] / "transport-receipts.json").read_text()
        self.assertNotIn(KEY, receipts)
        self.assertEqual([r["kind"] for r in json.loads(receipts)], ["preview", "update"])
        # Invoice lane: the revision moves forward only through the verified record.
        session = rj.current_session(a.session, {"attempts": a.ledger()})
        self.assertEqual(session["baseline"]["active_revision_id"], RESTORED)
        self.assertEqual(rj.original_baseline(session), baseline())
        seen = []

        def spy(session, case):
            seen.append((session["baseline"]["active_revision_id"],
                         rj.original_baseline(session)["active_revision_id"]))
            raise ValueError("stop after validation")
        with patch.object(live, "validate_source", return_value="checked"), \
                patch.object(live, "utc_now", return_value=STAMP), \
                patch.object(employee_run, "validate", side_effect=spy), \
                patch.object(employee_run, "pinned_cli") as cli:
            with self.assertRaises(ValueError):
                live.execute(a.home, "partial-payments", RATIONALE)
            cli.assert_not_called()
        self.assertEqual(seen, [(RESTORED, ORIG)])
        self.assertEqual(len(a.ledger()), 1)  # no reservation for the refused invoice run
        Path(entry["rebinding"]["path"]).write_text("{}")
        with self.assertRaises(ValueError):
            rj.current_session(a.session, {"attempts": a.ledger()})
        store = a.store()
        self.addCleanup(store.close)
        self.assertFalse(store.open_cleanup() or store.actions(entry["run_id"], True))
        claude_live.review(a.home, "r", SimpleNamespace(text="Correlated retrieval matched.",
                                                       status=None, classification=None))
        saved = json.loads(next((a.home.root / "live-reviews").glob("*.json")).read_text())
        self.assertEqual(saved["classification"]["automatic"], "passed")
        a.deliver()
        self.assertEqual((a.posts, a.issues), ([], []))

    def session_of(self, a):
        return a.session

    def test_denied_with_honest_reply_is_functional_failure_reported_once(self):
        a = Attempt(self, {"denied"})
        item = a.handoff()
        detail = item["result"]["classification_detail"]
        self.assertEqual((detail["classification"], detail["inability_report"]),
                         ("seren_functional_failure", "needs_review"))
        self.assertEqual(item["result"]["restore"]["status"], "restored")
        claude_live.review(a.home, "r", SimpleNamespace(
            text="Grant verified by preview and read-back; the exact request was denied (401).",
            status="confirmed_bug", finding=["journey-branch-denied"], workaround=None,
            classification="seren_functional_failure",
            summary="Expected the branch record with the grant in place; observed permission denied.",
            customer_impact="An owner's employee cannot read its own branch record; exposure unknown.",
            evidence_limits="One bounded live run; grant scope and component cause not established."))
        a.deliver()
        a.deliver()
        self.assertEqual((len(a.issues), len(a.posts)), (1, 1))
        self.assertIn(journeys.LABELS["seren_functional_failure"], a.issues[0]["body"])
        self.assertIn("frozen journey package inputs", a.issues[0]["body"])
        self.assertIn("**" + journeys.SHORT["seren_functional_failure"]
                      + "** · package input · live Seren run", a.posts[0])
        self.assertIn("component cause not established", a.posts[0])

    def test_result_variants_never_pass_without_correlated_retrieval(self):
        expected = {"wrong_record": "seren_functional_failure", "truncated": "incomplete",
                    "no_call": "model_answer_failure", "bare_after_denied": "model_answer_failure"}
        for mode, kind in expected.items():
            with self.subTest(mode):
                a = Attempt(self, {mode})
                entry = run_gate(a)
                self.assertEqual(entry["classification"], kind)
                self.assertNotEqual(entry["verdict"], "passed")
                self.assertEqual(entry["restore"]["status"], "restored")


class RefusalTests(unittest.TestCase):
    def refused(self, a, contains, **kw):
        before = (a.home.root / "live-ledger.json").read_bytes()
        with self.assertRaises(live.Refused) as caught:
            run_gate(a, **kw)
        self.assertIn(contains, str(caught.exception))
        self.assertEqual((a.home.root / "live-ledger.json").read_bytes(), before)
        self.assertEqual(a.service.calls, [])

    def test_clock_grant_inactive_mismatched_or_changed_package_refuse_before_calls(self):
        self.refused(Attempt(self, grant=False), "no active owner grant")
        a = Attempt(self)
        a.authority["journeys"][RETRIEVAL]["active"] = False
        discovery.write_json(a.home.root / "live-authority.json", a.authority)
        self.refused(a, "no active owner grant")
        self.refused(Attempt(self), "differs from the frozen package", digest="f" * 64)
        moved = copy.deepcopy(CATALOG)
        journeys.entry(moved, RETRIEVAL)["package"]["sha256"] = "e" * 64
        self.refused(Attempt(self), "package changed", catalog=moved)
        wrong = copy.deepcopy(CATALOG)
        journeys.entry(wrong, RETRIEVAL)["bindings"]["path"] = "/projects/x/branches/y"
        self.refused(Attempt(self), "differs from the executable adapter", catalog=wrong)

    def test_exhausted_budget_and_unfinished_attempt_refuse(self):
        a = Attempt(self)
        discovery.write_json(a.home.root / "live-ledger.json", {"version": 1, "attempts": [
            {"id": str(i), "day": live.day(STAMP), "status": "finished"} for i in range(200)]})
        self.refused(a, "budget exhausted")
        a = Attempt(self)
        discovery.write_json(a.home.root / "live-ledger.json", {"version": 1, "attempts": [
            {"id": "x", "day": live.day(STAMP), "status": "attempting"}]})
        self.refused(a, "never replay")


class SafetyPathTests(unittest.TestCase):
    def test_stale_baseline_or_broad_preview_stop_before_any_mutation(self):
        for mode, idle in (("stale_baseline", True), ("missing_branch", True),
                           ("broad_preview", False), ("normalized_ref", False)):
            with self.subTest(mode):
                a = Attempt(self, {mode})
                entry = run_gate(a)
                self.assertEqual(a.service.patches, 0)
                self.assertEqual(a.service.config, original_config())
                self.assertEqual(entry["restore"]["status"], "not_needed")
                self.assertEqual(entry["classification"], "incomplete")
                self.assertFalse(entry["cleanup_confirmed"])  # shutdown never observed
                if idle:  # only reads were sent: nothing to clean up, nothing confirmed
                    self.assertEqual((entry["status"], entry["cleanup"]["status"]),
                                     ("finished", "not_required"))
                    self.assertFalse(entry["cleanup"]["shutdown_observed"])
                    self.assertFalse(entry["operational_complete"])
                else:  # a preview was sent: a possible side effect keeps the lane blocked
                    self.assertEqual(entry["status"], "blocked")
                    self.assertNotIn("cleanup", entry)
                with self.assertRaises(live.Refused):  # the package is consumed either way
                    run_gate(a)

    def test_drifted_readback_never_starts_and_restores(self):
        a = Attempt(self, {"readback_drift"})
        entry = run_gate(a)
        self.assertNotIn(["agent", "managed-start", TARGET], a.service.calls)
        self.assertEqual((entry["restore"]["status"], a.service.config),
                         ("restored", original_config()))
        a = Attempt(self, {"persistent_drift"})
        entry = run_gate(a)  # neither original nor reviewed: never rolled back blindly
        self.assertEqual((entry["status"], entry["restore"]["status"]), ("blocked", "open"))
        self.assertFalse(any(c[:2] == ["agent", "managed-rollback"] for c in a.service.calls))

    def test_lost_update_response_is_recovered_by_key_and_never_resent(self):
        a = Attempt(self, {"lost_update"})
        entry = run_gate(a)
        self.assertEqual((a.service.patches, entry["classification"]), (1, "passed"))
        a = Attempt(self, {"lost_update", "lost_action"})
        entry = run_gate(a)
        self.assertEqual(a.service.patches, 1)
        self.assertNotIn(["agent", "managed-start", TARGET], a.service.calls)
        self.assertEqual((entry["restore"]["status"], a.service.config),
                         ("restored", original_config()))

    def test_rejected_or_interrupted_update_is_confirmed_unapplied_by_readback(self):
        for mode in ("reject_update", "interrupt_update"):
            with self.subTest(mode):
                a = Attempt(self, {mode})
                entry = run_gate(a)
                self.assertEqual(entry["restore"]["status"], "not_applied")
                self.assertEqual((a.service.revision, a.service.config), (ORIG, original_config()))
                self.assertNotIn("rebinding", entry)

    def test_shutdown_failure_keeps_configuration_and_blocks_lane(self):
        a = Attempt(self, {"stuck_stop"})
        entry = run_gate(a)
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(entry["restore"]["reason"], "not attempted: shutdown unconfirmed")
        self.assertFalse(any(c[:2] == ["agent", "managed-rollback"] for c in a.service.calls))
        store = a.store()
        self.addCleanup(store.close)
        self.assertEqual({c["kind"] for c in store.open_cleanup()},
                         {"stop_runtime", "restore_configuration"})

    def test_restore_failure_keeps_obligation_open(self):
        a = Attempt(self, {"rollback_fails"})
        entry = run_gate(a)
        self.assertEqual((entry["status"], entry["restore"]["status"]), ("blocked", "open"))
        store = a.store()
        self.addCleanup(store.close)
        self.assertEqual([c["kind"] for c in store.open_cleanup()], ["restore_configuration"])
        self.assertNotIn("rebinding", entry)

    def test_call_reserve_and_deadline_keep_stop_and_restore_possible(self):
        a = Attempt(self)
        with patch.object(live, "CALLS", 16):
            entry = run_gate(a)
        self.assertLessEqual(entry["calls"], 16)
        self.assertNotIn(["agent", "managed-start", TARGET], a.service.calls)
        self.assertEqual(entry["restore"]["status"], "restored")
        a = Attempt(self)
        original = a.service.handle

        def slow(args):
            if args[0] == "branches":
                a.tick(400)  # the whole work window passes during preflight
            return original(args)
        a.service.handle = slow
        entry = run_gate(a)
        self.assertEqual(a.service.patches, 0)
        self.assertEqual(entry["restore"]["status"], "not_needed")


class PreparationTests(unittest.TestCase):
    """The original supplied-data fixture preparation is a separate authority from the
    journey grant; an invalid chain must refuse before any reservation, key or client."""

    def refused(self, a, contains, gate="fixture preparation or journey package not established"):
        ledger = (a.home.root / "live-ledger.json").read_bytes()
        state = Path(a.session["state_dir"]) / "state.sqlite3"
        with patch.object(employee_run, "verify_preparation",
                          wraps=employee_run.verify_preparation) as checked:
            with self.assertRaises(live.Refused) as caught:
                run_gate(a)
        self.assertIn(gate, str(caught.exception))
        self.assertIn(contains, str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual((a.home.root / "live-ledger.json").read_bytes(), ledger)
        self.assertFalse((a.home.root / "live-attempts").exists())
        self.assertEqual((a.key_reads, a.transports, a.service.calls), ([], [], []))
        self.assertEqual((a.service.revision, a.service.config), (ORIG, original_config()))
        self.assertFalse(state.exists())  # no store, so no run, reservation or obligation
        return checked

    def test_valid_preparation_is_checked_by_the_real_validator_then_runs(self):
        a = Attempt(self)
        with patch.object(employee_run, "verify_preparation",
                          wraps=employee_run.verify_preparation) as checked:
            entry = run_gate(a)
        self.assertEqual(entry["status"], "finished")
        self.assertEqual([c.args[1:] for c in checked.call_args_list], [(TARGET, "supplied")])

    def test_missing_or_altered_receipt(self):
        a = Attempt(self)
        a.session.pop("setup_receipt")
        a.write_authority()
        self.refused(a, "receipt missing")
        a = Attempt(self)
        Path(a.session["setup_receipt"]).unlink()
        self.refused(a, "receipt changed")
        a = Attempt(self)
        with open(a.session["setup_receipt"], "a") as receipt:
            receipt.write(" ")
        self.refused(a, "receipt changed")
        a = Attempt(self)
        a.session["setup_receipt_sha256"] = "0" * 64
        a.write_authority()
        self.refused(a, "receipt changed")

    def test_wrong_deployment_scope_or_original_revision(self):
        for changes in ({"deployment_id": "7a00003a-0000-4000-8000-00000000003b"},
                        {"execution_scope": "one-call-trace-v2"},
                        {"active_revision_id": SETUP_REV}):
            with self.subTest(changes):
                a = Attempt(self)
                a.rebasis(**changes)
                self.refused(a, "creation, unbound fixture")
        a = Attempt(self)
        a.session["execution_scope"] = "one-call-branch-read-v1"
        a.write_authority()
        self.refused(a, "approved clock limits")

    def test_missing_altered_or_inconsistent_referenced_evidence(self):
        a = Attempt(self)
        (a.chain.root / "creation.json").unlink()
        self.refused(a, "FileNotFoundError")
        a = Attempt(self)
        (a.chain.root / "preview.json").write_text("{}")
        self.refused(a, "fixture source receipt changed")
        a = Attempt(self)
        journal = a.chain.root / "instruction-update-journal.json"
        journal.write_text(journal.read_text().replace("applied_verified", "applied"))
        self.refused(a, "instruction-update journal changed")
        a = Attempt(self)
        record = json.loads((a.chain.root / "instruction-update-journal.json").read_text())
        record["observations"]["read_back"]["active_revision_id"] = SETUP_REV
        path = a.chain.root / "instruction-update-journal.json"
        path.write_text(json.dumps(record))
        a.rebasis(instruction_update={"journal": {"path": str(path), "sha256": hashlib.sha256(
            path.read_bytes()).hexdigest()}})
        self.refused(a, "journal does not establish the finance setup")

    def test_invalid_baseline_controls_or_target(self):
        for field, value in (("max_iterations", 3), ("approval_policy", "allow_mutations")):
            with self.subTest(field):
                a = Attempt(self)
                a.session["baseline"] = dict(a.session["baseline"], **{field: value})
                a.write_authority()
                self.refused(a, "approved clock limits")
        a = Attempt(self)  # the existing authority check refuses a non-Clock Test session first
        a.session["baseline"] = dict(a.session["baseline"], name="Seren Harness Supplied Data")
        a.write_authority()
        self.refused(a, "", gate="authority does not bind the existing Clock Test session")

    def test_broken_tampered_or_mismatched_rebinding_chain(self):
        a = Attempt(self)
        record = {"journey_attempt": "x", "from_revision": ORIG, "to_revision": RESTORED,
                  "setup_revision": SETUP_REV, "original_projection": original_config(),
                  "projection_sha256": rj.sha(original_config())}
        path = Path(self.tmp_dir(a)) / "rebinding.json"

        def ledger(rec, sha=None):
            path.write_text(json.dumps(rec))
            digest = sha or hashlib.sha256(path.read_bytes()).hexdigest()
            discovery.write_json(a.home.root / "live-ledger.json", {"version": 1, "attempts": [
                {"id": "earlier", "day": "2026-10-16", "status": "finished", "calls": 20,
                 "cleanup_confirmed": True, "ended_at": STAMP, "report": "r",
                 "rebinding": {"path": str(path), "sha256": digest}}]})
        ledger(record, sha="0" * 64)
        self.refused(a, "rebinding record changed")
        ledger(dict(record, from_revision=SETUP_REV))
        self.refused(a, "rebinding chain broken")
        ledger(dict(record, projection_sha256="1" * 64))
        self.refused(a, "not an exact restore")
        ledger(dict(record, original_projection=dict(original_config(), max_iterations=3),
                    projection_sha256=rj.sha(dict(original_config(), max_iterations=3))))
        self.refused(a, "rebinding changed a reviewed baseline field")

    def tmp_dir(self, a):
        return a.home.root

    def test_valid_preparation_after_verified_rebinding_uses_restored_revision(self):
        a = Attempt(self)
        first = run_gate(a)
        self.assertEqual((first["status"], a.service.revision), ("finished", RESTORED))
        # A second, separately frozen package (new digest) on the same fixture.
        package = json.loads((journeys.ROOT / ENTRY["package"]["path"]).read_text())
        package["prepared_at"] = "2026-10-18"
        path = a.home.root / "package-second.json"
        path.write_text(json.dumps(package))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        catalog = copy.deepcopy(CATALOG)
        journeys.entry(catalog, RETRIEVAL)["package"] = {"path": str(path), "sha256": digest}
        a.authority["journeys"][RETRIEVAL]["package_sha256"] = digest
        a.write_authority()
        with patch.object(employee_run, "verify_preparation",
                          wraps=employee_run.verify_preparation) as checked:
            second = run_gate(a, catalog=catalog, digest=digest)
        session = checked.call_args.args[0]
        self.assertEqual(session["baseline"]["active_revision_id"], RESTORED)
        self.assertEqual(rj.original_baseline(session)["active_revision_id"], ORIG)
        self.assertEqual((second["status"], second["restore"]["status"]), ("finished", "restored"))
        self.assertEqual(second["restore"]["original_revision"], RESTORED)
        chain = rj.current_session(a.session, {"attempts": a.ledger()})
        self.assertEqual((chain["baseline"]["active_revision_id"], len(chain["rebinding"])),
                         (RESTORED_AGAIN, 2))
        self.assertEqual(rj.original_baseline(chain)["active_revision_id"], ORIG)


class BranchPreflightTests(unittest.TestCase):
    """The preflight parser follows the pinned CLI's direct-object output contract."""

    def result(self, data, status=OK, code=0):
        return CliResult(status, BRANCH_RECEIPT["argv"], data, code)

    def test_saved_real_response_shape_is_accepted(self):
        self.assertEqual((BRANCH_RECEIPT["status"], BRANCH_RECEIPT["exit_code"]), ("ok", 0))
        record = rj.branch_record(self.result(copy.deepcopy(BRANCH_RECEIPT["response"])))
        self.assertEqual((record["id"], record["project_id"], record["name"]),
                         (BRANCH, PROJECT, "development"))
        # The shared helper this parser replaces rejects the same real response.
        self.assertEqual(employee_run.body(self.result(BRANCH_RECEIPT["response"])), {})

    def test_malformed_enveloped_or_unsuccessful_responses_are_rejected(self):
        real = BRANCH_RECEIPT["response"]
        for name, result in (
                ("envelope", self.result({"data": real})),
                ("missing field", self.result({k: v for k, v in real.items() if k != "timeline_id"})),
                ("not an object", self.result([real])),
                ("failed call", self.result(real, status=ERROR, code=1)),
                ("nonzero exit", self.result(real, code=2))):
            with self.subTest(name):
                self.assertIsNone(rj.branch_record(result))

    def test_wrong_identity_or_empty_name_blocks_before_any_mutation(self):
        for changes in ({"id": SETUP_REV}, {"project_id": SETUP_REV}, {"name": "  "}):
            with self.subTest(changes):
                a = Attempt(self)
                original = a.service.handle

                def handle(args, changes=changes, original=original):
                    result = original(args)
                    if args[0] == "branches":
                        result.data.update(changes)
                    return result
                a.service.handle = handle
                entry = run_gate(a)
                self.assertEqual((a.service.patches, entry["calls"]), (0, 1))
                self.assertEqual(entry["cleanup"]["status"], "not_required")
                self.assertEqual(entry["classification"], "incomplete")


SAVED = json.loads((Path(__file__).parent / "fixtures/managed-preview-v4-20261017.json").read_text())
SAVED_BASE = SAVED["baseline_receipt"]["response"]["data"]
SAVED_CURRENT = SAVED["preview_receipt"]["response"]["current"]


class PreviewRepresentationTests(unittest.TestCase):
    """managed-get (pinned CLI, typed re-serialization) against the raw preview views."""

    def test_saved_v4_pair_still_differs_where_saved_evidence_cannot_decide(self):
        raw = {k for k in set(SAVED_BASE) | set(SAVED_CURRENT)
               if SAVED_BASE.get(k, "absent") != SAVED_CURRENT.get(k, "absent")}
        self.assertEqual(raw, {"active_revision_id", "allowed_remote_agent_origins",
                               "capability_policy", "config", "context_budget_tokens",
                               "cron_schedule", "managed_agent_secret_requirements",
                               "secret_keys"})
        # Explained: the preview carries no revision; the CLI omits [] and null optionals.
        self.assertNotIn("active_revision_id", SAVED_CURRENT)
        self.assertEqual((SAVED_CURRENT["allowed_remote_agent_origins"],
                          SAVED_CURRENT["cron_schedule"]), ([], None))
        # Unresolved: redacted only in the CLI receipt (non-empty values, never compared from
        # placeholders), and secret requirements present in managed-get only.
        self.assertEqual(rj.differing(rj.projection(SAVED_CURRENT), rj.projection(SAVED_BASE)),
                         "capability_policy, config, context_budget_tokens, "
                         "managed_agent_secret_requirements, secret_keys")
        self.assertEqual(SAVED_BASE["managed_agent_secret_requirements"], "[redacted]")

    def test_equivalent_absent_empty_and_null_optionals_compare_equal(self):
        # What the CLI prints for a raw view: skipped empty array and null option.
        cli = {k: v for k, v in SAVED_CURRENT.items()
               if k not in ("allowed_remote_agent_origins", "cron_schedule")}
        cli["active_revision_id"] = "7a000031-0000-4000-8000-000000000032"
        self.assertEqual(rj.projection(cli), rj.projection(SAVED_CURRENT))
        for name, change in (("non-empty origins", {"allowed_remote_agent_origins": ["https://x"]}),
                             ("schedule set", {"cron_schedule": "0 * * * *"}),
                             ("empty string is not null", {"cron_schedule": ""}),
                             ("null is not []", {"allowed_remote_agent_origins": None}),
                             ("required array not skipped", {"secret_keys": []}),
                             ("present in one view only", {"managed_agent_secret_requirements": {}}),
                             ("unknown key", {"surprise": []}),
                             ("policy", {"approval_policy": "full"}),
                             ("nested shape", {"capability_policy": dict(
                                 SAVED_CURRENT["capability_policy"], browser=None)})):
            with self.subTest(name):
                self.assertNotEqual(rj.projection(dict(SAVED_CURRENT, **change)),
                                    rj.projection(cli))
        self.assertTrue(set(rj.SKIPPED_WHEN_EMPTY + rj.SKIPPED_WHEN_NULL)
                        <= preview_diagnostic.DETAIL_FIELDS)
        self.assertFalse(set(rj.SKIPPED_WHEN_EMPTY) & set(rj.SKIPPED_WHEN_NULL))

    def test_raw_preview_views_pass_setup_readback_and_restore(self):
        a = Attempt(self, {"raw_preview"})
        entry = run_gate(a)
        self.assertEqual((entry["classification"], entry["restore"]["status"]),
                         ("passed", "restored"))
        self.assertEqual((a.service.patches, a.service.config), (1, original_config()))
        self.assertTrue(entry["cleanup_confirmed"])

    def test_meaningful_or_unexplained_preview_drift_stops_before_mutation(self):
        for name, modes, change in (
                ("secret requirements only in managed-get", {"secret_requirements"}, {}),
                ("reported revision differs", {"raw_preview"}, {"active_revision_id": SETUP_REV}),
                ("schedule", {"raw_preview"}, {"cron_schedule": "0 * * * *"}),
                ("origins", {"raw_preview"}, {"allowed_remote_agent_origins": ["https://x"]}),
                ("identity", {"raw_preview"}, {"deployment_id": SETUP_REV}),
                ("model", {"raw_preview"}, {"model_id": "other-model"}),
                ("permissions", {"raw_preview"}, {"tool_refs": [rj.REF]}),
                ("policy", {"raw_preview"}, {"approval_policy": "full"}),
                ("instruction", {"raw_preview"}, {"bundle": {"instructions": []}})):
            with self.subTest(name):
                a = Attempt(self, modes)
                a.service.preview_patch = change
                entry = run_gate(a)
                self.assertEqual((a.service.patches, a.service.config), (0, original_config()))
                self.assertNotIn(["agent", "managed-start", TARGET], a.service.calls)
                self.assertEqual((entry["status"], entry["restore"]["status"]),
                                 ("blocked", "not_needed"))  # a preview was sent
                self.assertEqual(entry["classification"], "incomplete")
                notes = json.loads(Path(entry["report"]).read_text())["notes"]
                self.assertIn("preview current", notes)
                if name == "secret requirements only in managed-get":
                    self.assertTrue(notes.endswith(": managed_agent_secret_requirements"), notes)

    def test_readback_drift_after_update_still_blocks_start_and_restores(self):
        for change in ({"cron_schedule": "0 * * * *"}, {"allowed_remote_agent_origins": ["https://x"]}):
            with self.subTest(change):
                a = Attempt(self, {"raw_preview"})
                a.service.readback_patch = change
                entry = run_gate(a)
                self.assertNotIn(["agent", "managed-start", TARGET], a.service.calls)
                # Neither original nor the reviewed change: never rolled back blindly.
                self.assertEqual((entry["status"], entry["restore"]["status"]), ("blocked", "open"))


class ReconciliationTests(unittest.TestCase):
    """Correcting a pre-fix blocked attempt that only ever sent its preflight read."""

    def legacy_blocked(self, modes=("missing_branch",)):
        a = Attempt(self, set(modes))
        with patch.object(rj, "never_started", return_value=False):  # the pre-fix outcome
            entry = run_gate(a)
        self.assertEqual(entry["status"], "blocked")
        discovery.write_json(a.home.root / "claude-live-request.json", {
            "id": "h", "journey": RETRIEVAL, "case": RETRIEVAL, "status": "blocked",
            "result": entry})
        return a, entry

    def test_never_started_attempt_is_reconciled_without_claiming_shutdown(self):
        a, entry = self.legacy_blocked()
        outcome = (a.home.root / "live-attempts" / entry["id"] / "outcome.json").read_bytes()
        before = live.usage(a.session, {"attempts": a.ledger()}, STAMP)
        result = live.reconcile_not_started(a.home, entry["id"])
        [after] = a.ledger()
        self.assertEqual((after["status"], after["cleanup"]["status"]), ("finished", "not_required"))
        self.assertFalse(after["cleanup_confirmed"])
        self.assertFalse(after["cleanup"]["shutdown_observed"])
        self.assertEqual(after["classification"], "incomplete")
        self.assertEqual(after["reconciliation"]["previous"]["status"], "blocked")
        changed = {k for k in set(after) | set(entry) if after.get(k) != entry.get(k)}
        self.assertEqual(changed, {"status", "cleanup", "classification_detail", "reconciliation"})
        # Usage keeps the consumed attempt and settles its exact journaled call.
        usage = live.usage(a.session, {"attempts": a.ledger()}, STAMP)
        self.assertEqual((before["runs_used"], usage["runs_used"]), (1, 1))
        self.assertEqual((usage["calls_used"], usage["calls_reserved"]), (entry["calls"], 0))
        self.assertEqual(result["usage_after"]["calls_used"], entry["calls"])
        self.assertTrue(Path(result["backups"]["ledger"]).exists())
        self.assertEqual((a.home.root / "live-attempts" / entry["id"] / "outcome.json").read_bytes(),
                         outcome)
        handoff = json.loads((a.home.root / "claude-live-request.json").read_text())
        self.assertEqual((handoff["status"], handoff["result"]["cleanup"]["status"]),
                         ("finished", "not_required"))
        claude_live.review(a.home, "r", SimpleNamespace(text="Preflight parser rejected a valid "
                           "branch read; retrieval untested.", status=None, classification=None))
        with self.assertRaisesRegex(live.Refused, "already attempted"):
            run_gate(a)  # the consumed package can never run again
        rows = journeys.compare(CATALOG, a.authority, {"runs_remaining": 90, "attempts": a.ledger()})
        row = next(r for r in rows if r["id"] == RETRIEVAL)
        self.assertEqual(row["state"], "blocked")
        self.assertIn("package already attempted once; consumed, never replayed", row["blockers"])
        with self.assertRaises(live.Refused):
            live.reconcile_not_started(a.home, entry["id"])  # not repeatable

    def preview_blocked(self):
        """Attempt 7a00000c's shape: reads, one successful preview, stop before the update."""
        a = Attempt(self, {"secret_requirements"})
        entry = run_gate(a)
        self.assertEqual((entry["status"], entry["calls"], a.service.patches), ("blocked", 3, 0))
        discovery.write_json(a.home.root / "claude-live-request.json", {
            "id": "h", "journey": RETRIEVAL, "case": RETRIEVAL, "status": "blocked",
            "result": entry})
        return a, entry

    def test_preview_only_attempt_reconciles_with_its_limits_recorded(self):
        a, entry = self.preview_blocked()
        folder = a.home.root / "live-attempts" / entry["id"]
        saved = {p.name: p.read_bytes() for p in folder.iterdir()}
        result = live.reconcile_not_started(a.home, entry["id"])
        [after] = a.ledger()
        cleanup = after["cleanup"]
        self.assertEqual((after["status"], cleanup["status"]), ("finished", "not_required"))
        self.assertEqual((cleanup["preview_sent"], cleanup["post_attempt_readback"],
                          cleanup["shutdown_observed"], after["cleanup_confirmed"]),
                         (True, False, False, False))
        self.assertEqual(after["classification"], "incomplete")  # retrieval never attempted
        self.assertIn("one preview was sent and no read-back followed it",
                      after["classification_detail"]["reasons"][-1])
        changed = {k for k in set(after) | set(entry) if after.get(k) != entry.get(k)}
        self.assertEqual(changed, {"status", "cleanup", "classification_detail", "reconciliation"})
        # All three journaled calls stay charged; only the unused reservation is released.
        self.assertEqual((result["usage_before"]["calls_charged"], result["usage_after"]["calls_used"],
                          result["usage_after"]["calls_reserved"], result["usage_after"]["runs_used"]),
                         (live.CALLS, 3, 0, 1))
        for p in folder.iterdir():  # original evidence untouched; one record added
            self.assertEqual(p.read_bytes(), saved.get(p.name, p.read_bytes()))
        self.assertEqual(set(saved) | {"reconciliation.json"}, {p.name for p in folder.iterdir()})
        with self.assertRaisesRegex(live.Refused, "already attempted"):
            run_gate(a)
        with self.assertRaises(live.Refused):
            live.reconcile_not_started(a.home, entry["id"])

    def test_the_saved_v4_preview_receipt_meets_the_evidence_check(self):
        self.assertTrue(rj.preview_only([SAVED["preview_receipt"]], TARGET))

    def test_starts_mutations_or_doubtful_previews_are_never_reconciled(self):
        def tamper(change):
            def apply(a, entry):
                path = a.home.root / "live-attempts" / entry["id"] / "transport-receipts.json"
                receipts = json.loads(path.read_text())
                change(receipts)
                path.write_text(json.dumps(receipts))
            return apply

        def action(a, entry):
            store = a.store()
            store.record_intent(entry["run_id"], "grant_update", "grant_update", TARGET, {})
            store.close()
        cases = {
            "update sent": tamper(lambda r: r.append(dict(r[0], kind="update", method="PATCH"))),
            "two previews": tamper(lambda r: r.append(copy.deepcopy(r[0]))),
            "uncertain": tamper(lambda r: r[0].update(outcome="uncertain")),
            "accepted, not 200": tamper(lambda r: r[0].update(http_status=202)),
            "keyed": tamper(lambda r: r[0].update(request_id=SETUP_REV)),
            "other endpoint": tamper(lambda r: r[0].update(url=r[0]["url"] + "/x")),
            "not stopped": tamper(lambda r: r[0]["response"]["current"].update(status="running")),
            "other deployment": tamper(lambda r: r[0]["response"].update(deployment_id=SETUP_REV)),
            "intent journaled": action,
            "calls disagree": lambda a, entry: discovery.write_json(
                a.home.root / "live-ledger.json", {"version": 1, "attempts": [dict(entry, calls=4)]}),
        }
        for name, change in cases.items():
            with self.subTest(name):
                a, entry = self.preview_blocked()
                change(a, entry)
                before = (a.home.root / "live-ledger.json").read_bytes()
                with self.assertRaises(live.Refused):
                    live.reconcile_not_started(a.home, entry["id"])
                self.assertEqual((a.home.root / "live-ledger.json").read_bytes(), before)
        a = Attempt(self, {"stuck_stop"})  # started: stop and restore obligations stay
        entry = run_gate(a)
        with self.assertRaises(live.Refused):
            live.reconcile_not_started(a.home, entry["id"])


PREDECESSOR = "6818e21a32c83d80a0616dc9c8493ca555a4b6cef3fd7b6b3010a752659f5b62"


class SuccessorPackageTests(unittest.TestCase):
    """Package v4 supersedes the synthetic predecessor v3 without relabelling it."""

    def test_successor_keeps_the_reviewed_operations_and_names_its_predecessor(self):
        root = journeys.ROOT / "docs/journeys/employee-retrieval"
        v3 = json.loads((root / "package.json").read_text())
        v4 = json.loads((root / "package-v4.json").read_text())
        self.assertEqual(hashlib.sha256((root / "package.json").read_bytes()).hexdigest(), PREDECESSOR)
        self.assertNotEqual(DIGEST, PREDECESSOR)
        self.assertEqual((v4["supersedes"]["package_sha256"], v4["version"]), (PREDECESSOR, 4))
        changed = {k for k in set(v3) | set(v4) if v3.get(k) != v4.get(k)}
        self.assertEqual(changed, {k for k in ("version", "assessment", "supersedes", "authorization")
                                   if v3.get(k) != v4.get(k)})
        self.assertEqual(sum(v4["bounds"]["call_allocation"].values()), live.CALLS)
        self.assertEqual(v4["bounds"]["timing"]["window_seconds"], live.WINDOW)
        proposed = json.loads((root / "authorization-v4.proposed.json").read_text())
        self.assertEqual((proposed["status"], proposed["active"], proposed["approved"],
                          proposed["package_sha256"]), ("proposed", False, False, DIGEST))
        self.assertIn("No paid Claude overage", proposed["spending_and_sessions"])

    def predecessor_state(self, a):
        """Synthetic regression shape: an active grant for the synthetic predecessor v3 and its attempt."""
        a.authority["journeys"] = {RETRIEVAL: {"active": True, "approved": True,
                                               "package_sha256": PREDECESSOR}}
        a.write_authority()
        discovery.write_json(a.home.root / "live-ledger.json", {"version": 1, "attempts": [
            {"id": "7a00000d-0000-4000-8000-00000000000e", "day": "2026-10-17",
             "status": "finished", "calls": 1, "journey": RETRIEVAL, "journey_digest": PREDECESSOR,
             "cleanup": {"status": "not_required"}, "cleanup_confirmed": False,
             "ended_at": STAMP, "report": "saved"}]})

    def test_successor_needs_a_fresh_grant_and_the_predecessor_stays_consumed(self):
        a = Attempt(self)
        self.predecessor_state(a)
        before = (a.home.root / "live-ledger.json").read_bytes()
        with self.assertRaisesRegex(live.Refused, "no active owner grant"):
            run_gate(a)
        self.assertEqual(((a.home.root / "live-ledger.json").read_bytes(), a.service.calls,
                          a.key_reads, a.transports), (before, [], [], []))
        old = copy.deepcopy(CATALOG)
        journeys.entry(old, RETRIEVAL)["package"] = {"path": "docs/journeys/employee-retrieval/package.json",
                                                    "sha256": PREDECESSOR}
        with self.assertRaisesRegex(live.Refused, "already attempted"):
            run_gate(a, catalog=old, digest=PREDECESSOR)
        rows = journeys.compare(old, a.authority, {"runs_remaining": 90, "attempts": a.ledger()})
        self.assertIn("package already attempted once; consumed, never replayed",
                      next(r for r in rows if r["id"] == RETRIEVAL)["blockers"])
        # A fresh grant for v4 permits exactly one attempt.
        a.authority["journeys"][RETRIEVAL]["package_sha256"] = DIGEST
        a.write_authority()
        entry = run_gate(a)
        self.assertEqual((entry["status"], entry["classification"], entry["journey_digest"]),
                         ("finished", "passed", DIGEST))
        with self.assertRaisesRegex(live.Refused, "already attempted"):
            run_gate(a)

    def test_slow_worst_case_fits_31_calls_and_420_seconds_with_stop_and_restore(self):
        a = Attempt(self, {"slow_ready", "slow_run"})
        entry = run_gate(a)
        self.assertLessEqual(entry["calls"], live.CALLS)
        self.assertEqual(entry["calls"], len(a.service.calls))
        self.assertLessEqual(a.clock[0] - 1000.0, live.WINDOW)
        self.assertEqual((entry["status"], entry["restore"]["status"], entry["cleanup_confirmed"]),
                         ("finished", "restored", True))
        self.assertEqual(entry["classification"], "passed")


class GrantCheckTests(unittest.TestCase):
    def driver(self):
        d = rj.GrantedBranchRun.__new__(rj.GrantedBranchRun)
        d.setup_revision = SETUP_REV
        d.granted = rj.projection(FakeSeren().granted)
        return d

    def test_exact_grant_only(self):
        d = self.driver()
        good = dict(FakeSeren().granted, active_revision_id=SETUP_REV, status="stopped")
        d.check_baseline(good)
        bad = [dict(good, tool_refs=[rj.REF, dict(rj.REF, operation_id="seren_db_list_branches")]),
               dict(good, tool_refs=[dict(rj.REF, execution_surface="script")]),
               dict(good, tool_refs=[dict(rj.REF, publisher_slug="seren-cloud")]),
               dict(good, tool_refs=[dict(rj.REF, require_approval=True)]),
               dict(good, external_databases=[{"project_id": PROJECT}]),
               dict(good, active_revision_id=ORIG), dict(good, status="running")]
        for detail in bad:
            with self.subTest(detail=str(detail)[:60]), self.assertRaises(employee_run.Halt):
                d.check_baseline(detail)
        with self.assertRaises(ValueError):  # the invoice lane still refuses every ref
            employee_run.fixture_baseline(good)


class TransportTests(unittest.TestCase):
    def transport(self, opener, key=KEY):
        sent = []
        return grant_transport.GrantTransport(TARGET, rj.setup_body(PACKAGE), lambda: key,
                                              Sanitizer(), sent.append, opener), sent

    def test_fixed_host_frozen_body_and_redaction(self):
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 500, "boom " + KEY, {}, None)
        t, sent = self.transport(opener)
        with patch.dict("os.environ", {"SEREN_API_BASE": "https://evil.example",
                                       "HTTPS_PROXY": "http://proxy.example"}):
            with self.assertRaises(grant_transport.TransportError) as caught:
                t.preview(rj.setup_body(PACKAGE), 10)
        self.assertEqual(caught.exception.kind, "uncertain")
        self.assertEqual(seen, [grant_transport.BASE + "/deployments/" + TARGET + "/managed/preview"])
        self.assertEqual(len(sent), 1)  # journaled before sending
        self.assertNotIn(KEY, json.dumps(t.receipts) + str(caught.exception))
        with self.assertRaises(grant_transport.TransportError):
            t.update(dict(rj.setup_body(PACKAGE), tool_refs=[]), "11111111-1111-4111-8111-111111111111", 10)
        self.assertEqual(len(sent), 1)
        handlers = [type(h) for h in grant_transport.urllib.request.build_opener(
            grant_transport.urllib.request.ProxyHandler({}), grant_transport._NoRedirect()).handlers]
        self.assertIn(grant_transport._NoRedirect, handlers)
        with self.assertRaises(grant_transport.TransportError):
            grant_transport._NoRedirect().redirect_request(None, None, 302, "", {}, "https://x")

    def test_definite_rejection_redirected_or_odd_responses(self):
        def rejecting(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 409, "conflict", {}, None)
        t, _ = self.transport(rejecting)
        with self.assertRaises(grant_transport.TransportError) as caught:
            t.update(rj.setup_body(PACKAGE), "11111111-1111-4111-8111-111111111111", 10)
        self.assertEqual(caught.exception.kind, "rejected")
        for response in (Response("https://elsewhere.example/x", {"data": {}}),
                         Response(None, ["not", "an", "envelope"]),
                         Response(None, {"data": {}}, status=301)):
            def opener(request, timeout, r=response):
                r.url = r.url or request.full_url
                return r
            t, _ = self.transport(opener)
            with self.subTest(url=response.url), self.assertRaises(grant_transport.TransportError) as caught:
                t.preview(rj.setup_body(PACKAGE), 10)
            self.assertEqual(caught.exception.kind, "uncertain")


if __name__ == "__main__":
    unittest.main()
