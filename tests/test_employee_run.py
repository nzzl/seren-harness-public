"""Synthetic executor tests: actual journal/reviewer, no live subprocess or sockets."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE
from seren_harness.employee_run import (ClockRun, Halt, pinned_cli, validate,
                                        EXECUTION_SCOPE, REVIEWED_TOOLS, REVIEWED_OPERATIONS,
                                        fixture_baseline, fixture_refs_evidence)
from seren_harness.seren_cli import CliResult
from seren_harness.state import Store
from test_employee_time import CASE, fixture


class FakeCli:
    def __init__(self, first, close, baseline, mode="ok"):
        self.first, self.close, self.baseline, self.mode = first, close, baseline, mode
        self.calls = []
        self.running = False
        self.timeout = 30

    def display_argv(self, args):
        return ["seren", *args]

    def _execute(self, args):
        self.calls.append(list(args))
        if self.mode == "invoke-timeout":
            return CliResult("timeout", args)
        r = self.first["request"]
        return CliResult("ok", args, "✓ Run accepted for deployment " + r["deployment_id"]
                         + ".\n  Run ID: " + r["run_id"] + "\n  Execution ID: "
                         + r["execution_id"] + "\n", 0)

    def json(self, *args):
        self.calls.append(list(args))
        if "managed-start" in args:
            self.running = True
            if self.mode == "start-timeout":
                return CliResult("timeout", list(args))
            data = {}
        elif "managed-stop" in args:
            if self.mode != "stop-fails":
                self.running = False
            data = {}
        elif "managed-get" in args:
            data = dict(self.baseline, status="running" if self.running else "stopped")
            if self.mode == "refs-added":
                data["tool_refs"] = [{"type": "connector"}]
            if self.mode == "empty-refs":
                data["tool_refs"] = []
            if self.mode == "missing-required":
                del data["max_tool_calls_per_run"]
            if self.mode == "cap-changed":
                data["max_tool_calls_per_run"] = 2
            if self.mode == "changed":
                data["active_revision_id"] = "changed"
        elif "managed-deployment-tools" in args:
            tool = {"name": "system_time", "effective_policy": {"status": "allowed"},
                    "side_effecting": False, "approval_type": "none",
                    "checkpoint_required": False, "input_schema": {
                        "type": "object", "properties": {}, "additionalProperties": False}}
            data = {"deployment_id": self.baseline["deployment_id"],
                    "tool_presets": ["live_data"], "approval_policy": "read_only",
                    "allowed_publisher_operations": sorted(REVIEWED_OPERATIONS),
                    "tools": [dict(tool, name=n) for n in sorted(REVIEWED_TOOLS)]}
            if self.mode == "policy-changed":
                data["approval_policy"] = "allow_mutations"
            if self.mode == "operations-changed":
                data["allowed_publisher_operations"].append("post")
            if self.mode == "clock-blocked":
                next(t for t in data["tools"] if t["name"] == "system_time")[
                    "effective_policy"] = {"status": "blocked"}
            if self.mode == "external-tool":
                data["tools"].append(dict(tool, name="database_query"))
        elif "status" in args:
            state = "running" if self.running else "stopped"
            data = {"id": self.baseline["deployment_id"], "status": state,
                    "desired_lifecycle_state": state}
        else:
            data = copy.deepcopy(self.first["response"]["data"])
            if self.mode == "wrong-run":
                data["id"] = "other"
            if self.mode == "unknown-result":
                data["output_events"][1]["content"] = "the time is today"
            if self.mode == "wrong-tool":
                data["output_events"][0]["name"] = "seren_publisher_request"
            if self.mode == "extra-call":
                data["output_events"].insert(1, copy.deepcopy(data["output_events"][0]))
            if self.mode == "missing-trace":
                data.pop("output_events")
            if self.mode == "secret":
                data["output_events"][2]["text"] = "sk-" + "Z" * 40
        return CliResult("ok", list(args), {"data": data}, 0)


class ClockExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = copy.deepcopy(CASE)
        self.case["target_status"] = "resolved"
        self.first, self.close = fixture()
        self.baseline = {"deployment_id": self.case["employee_tool"]["deployment_id"],
                         "active_revision_id": "11111111-1111-4111-8111-111111111111",
                         "name": "Seren Harness Clock Test",
                         "tool_presets": [], "approval_policy": "read_only",
                         "max_iterations": 2, "max_tool_calls_per_run": 1,
                         "max_timeout_seconds": 60, "private_output_policy": "control_plane"}
        self.session = {"baseline": self.baseline, "authorization_id":
                        "22222222-2222-4222-8222-222222222222", "profile": "default",
                        "execution_scope": EXECUTION_SCOPE,
                        "other_tools_available_acknowledged": True,
                        "runtime_remaining_seconds": 300, "expires_at": "2026-10-11T00:20:00Z",
                        "home": str(self.root), "state_dir": str(self.root / "state")}
        self.basis = {"deployment_id": self.baseline["deployment_id"],
                      "active_revision_id": self.baseline["active_revision_id"],
                      "execution_scope": EXECUTION_SCOPE, "unbound_fixture_verified": True,
                      "creation_receipt": "synthetic-creation", "preview_receipt": "synthetic-preview",
                      "effective_policy": {"resolved_tools": sorted(REVIEWED_TOOLS),
                                           "tool_presets": ["live_data"],
                                           "approval_policy": "read_only",
                                           "allowed_publisher_operations": sorted(REVIEWED_OPERATIONS)}}
        # Synthetic receipts mirror the saved live response shape (tool_refs absent).
        oid = "33333333-3333-4333-8333-333333333333"
        sources = {
            "creation": {"id": oid, "application": {"source_revision": 3,
                         "deployment_outcome": "confirmed"},
                         "operations": {"deployment_id": self.baseline["deployment_id"]},
                         "draft": {"deployment": {"tool_refs": []}}},
            "preview": {"onboarding_id": oid, "source_revision": 3,
                        "desired_spec": {"tool_refs": []}},
            "detail": dict(self.baseline, status="stopped"),
        }
        self.sources = sources
        self.write_sources()
        self.write_basis()
        self.store = Store(self.session["state_dir"])
        self.addCleanup(self.store.close)
        self.ticks = 0

    def write_sources(self):
        for name, data in self.sources.items():
            (self.root / (name + ".json")).write_text(json.dumps(
                {"status": "ok", "exit_code": 0,
                 "response": data if name == "creation" else {"data": data}}))
        self.basis["tool_refs_evidence"] = fixture_refs_evidence(
            *(self.root / (name + ".json") for name in ("creation", "preview", "detail")))

    def write_basis(self):
        receipt = self.root / "synthetic-setup.json"
        receipt.write_text(json.dumps(self.basis))
        self.session["setup_receipt"] = str(receipt)
        self.session["setup_receipt_sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()

    def clock(self):
        return self.ticks

    def wall(self):
        return (datetime(2026, 10, 11, tzinfo=timezone.utc) + timedelta(seconds=self.ticks)).isoformat()

    def sleep(self, seconds):
        self.ticks += seconds

    def driver(self, mode="ok"):
        cli = FakeCli(self.first, self.close, self.baseline, mode)
        # Advance the observation clock for every actual fake call, not model output.
        for method in ("json", "_execute"):
            original = getattr(cli, method)
            def timed(*args, original=original):
                self.ticks += 0.1
                result = original(*args)
                if "get" in args and "run" in args and result.ok:
                    data = result.data["data"]
                    stamp = self.wall()
                    data["completed_at"] = stamp
                    if mode in ("ok", "empty-refs"):
                        data["output_events"][1]["content"] = stamp
                        data["output_events"][2]["text"] = stamp
                return result
            setattr(cli, method, timed)
        return ClockRun(self.session, self.case, cli, self.store,
                        self.clock, self.sleep, self.wall), cli

    def execute(self, driver):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("network")), \
                mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            return driver.run()

    def test_one_invocation_actual_journal_report_and_confirmed_cleanup(self):
        validate(self.session, self.case)
        driver, cli = self.driver()
        self.assertEqual(self.execute(driver), 0)
        self.assertEqual(sum("--async" in a for a in cli.calls), 1)
        self.assertFalse(self.store.open_cleanup())
        self.assertFalse(self.store.actions(driver.run_id, True))
        with self.assertRaises(Halt):
            self.execute(self.driver()[0])
        report = (self.root / driver.run_id / "report/report.md").read_text()
        self.assertIn("Other tools remain available", report)
        self.assertIn("not a tool-isolation test", report)

    def test_offline_cli_interface_with_synthetic_receipts(self):
        from seren_harness.cli import main
        case_path = self.root / "synthetic-case.json"
        case_path.write_text(json.dumps(self.case))
        self.session["case"] = str(case_path)
        session_path = self.root / "synthetic-session.json"
        session_path.write_text(json.dumps(self.session))
        _, fake_cli = self.driver()
        def offline_driver(session, case, cli, store):
            return ClockRun(session, case, cli, store, self.clock, self.sleep, self.wall)
        with mock.patch("seren_harness.employee_run.pinned_cli", return_value=fake_cli), \
                mock.patch("seren_harness.employee_run.ClockRun", side_effect=offline_driver), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("network")), \
                mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["employee-time", "--session", str(session_path)]), 0)
        self.assertIn("Workflow passed", out.getvalue())
        self.assertIn("Shutdown: confirmed", out.getvalue())
        self.assertEqual(sum("--async" in a for a in fake_cli.calls), 1)

    def readiness_cli(self, driver, cli, ready_after=3, delay=0, late=False):
        """Synthetic building prefix based on saved observations; later states invented."""
        original = cli.json
        observed = {"polls": [], "stop_remaining": None}

        def json_call(*args):
            readiness = "status" in args and cli.running and not any(
                "managed-stop" in a for a in cli.calls)
            if "managed-start" in args:
                observed["start"] = self.ticks
            if "managed-stop" in args:
                observed["stop_remaining"] = driver.deadline - self.ticks
            if readiness:
                observed["polls"].append((self.ticks, cli.timeout))
                self.ticks += delay if late else min(delay, cli.timeout)
            result = original(*args)
            if readiness and len(observed["polls"]) <= ready_after:
                result.data["data"].update(status="building", desired_lifecycle_state="running")
            if "managed-start" in args:
                observed["ready_start"] = self.ticks
            return result

        cli.json = json_call
        return observed

    def test_ready_after_three_building_reads_invokes_once(self):
        driver, cli = self.driver()
        observed = self.readiness_cli(driver, cli)
        self.assertEqual(self.execute(driver), 0)
        self.assertEqual(len(observed["polls"]), 4)
        self.assertEqual(sum("--async" in a for a in cli.calls), 1)
        self.assertFalse(self.store.open_cleanup())
        self.assertLess(self.ticks - observed["start"], 300)

    def test_building_uses_readiness_window_and_reserves_cleanup_reads(self):
        for mode in ("ok", "stop-fails"):
            with self.subTest(mode=mode):
                self.ticks = 0
                self.session["authorization_id"] = mode
                driver, cli = self.driver(mode)
                observed = self.readiness_cli(driver, cli, ready_after=100)
                self.assertEqual(self.execute(driver), 2)
                self.assertEqual(len(observed["polls"]), 6)
                stop = next(r for r in driver.receipts if r["kind"] == "managed_stop")
                stop_tick = (datetime.fromisoformat(stop["started_at"]) -
                             datetime(2026, 10, 11, tzinfo=timezone.utc)).total_seconds()
                self.assertAlmostEqual(stop_tick - observed["ready_start"], 60)
                self.assertGreaterEqual(observed["stop_remaining"], 180)
                self.assertFalse(any("--async" in a for a in cli.calls))
                self.assertLessEqual(driver.counts["status"], 12)
                if mode == "stop-fails":
                    self.assertEqual(driver.counts["status"], 12)
                    self.assertTrue(self.store.open_cleanup())
                else:
                    self.assertFalse(self.store.open_cleanup())

    def test_slow_readiness_reply_cannot_cross_deadline_then_invoke(self):
        driver, cli = self.driver()
        observed = self.readiness_cli(driver, cli, ready_after=0, delay=61, late=True)
        self.assertEqual(self.execute(driver), 2)
        self.assertLessEqual(observed["polls"][0][1], 30)
        self.assertFalse(any("--async" in a for a in cli.calls))
        self.assertFalse(self.store.open_cleanup())

    def test_readiness_timeout_and_sleep_shrink_to_remaining_work_budget(self):
        self.session["runtime_remaining_seconds"] = 210
        driver, cli = self.driver()
        observed = self.readiness_cli(driver, cli, ready_after=100, delay=18)
        self.assertEqual(self.execute(driver), 2)
        for started, timeout in observed["polls"]:
            self.assertLessEqual(timeout, driver.work_deadline - started + 1e-9)
        self.assertLess(observed["polls"][-1][1], 18)
        # The fake adds 0.1s per call even when its timeout expires.
        self.assertGreaterEqual(observed["stop_remaining"], 179.9)
        self.assertFalse(any("--async" in a for a in cli.calls))
        self.assertFalse(self.store.open_cleanup())
        self.assertLess(self.ticks - observed["start"], 210)

    def test_unknown_invocation_never_retried_and_cleanup_still_runs(self):
        driver, cli = self.driver("invoke-timeout")
        self.assertEqual(self.execute(driver), 2)
        self.assertEqual(sum("--async" in a for a in cli.calls), 1)
        self.assertIn("employee_invoke", [a["kind"] for a in self.store.actions(driver.run_id, True)])
        self.assertFalse(self.store.open_cleanup())
        self.assertIn("managed-stop", [a[1] for a in cli.calls])

    def test_unknown_start_is_stopped_without_invocation(self):
        driver, cli = self.driver("start-timeout")
        self.assertEqual(self.execute(driver), 2)
        self.assertFalse(any("--async" in a for a in cli.calls))
        self.assertFalse(self.store.open_cleanup())
        self.assertFalse(self.store.actions(driver.run_id, True))

    def test_changed_fixture_or_extra_tool_never_starts(self):
        for mode in ("changed", "external-tool", "cap-changed", "policy-changed",
                     "operations-changed", "clock-blocked", "refs-added", "missing-required"):
            with self.subTest(mode=mode):
                self.session["authorization_id"] = mode
                driver, cli = self.driver(mode)
                self.assertEqual(self.execute(driver), 2)
                self.assertFalse(any("managed-start" in a for a in cli.calls))

    def test_wrong_or_multiple_tool_calls_fail_and_shutdown_without_retry(self):
        for mode in ("wrong-tool", "extra-call"):
            with self.subTest(mode=mode):
                self.session["authorization_id"] = mode
                driver, cli = self.driver(mode)
                self.assertEqual(self.execute(driver), 1)
                self.assertEqual(sum("--async" in a for a in cli.calls), 1)
                self.assertEqual(sum("managed-stop" in a for a in cli.calls), 1)
                self.assertFalse(self.store.open_cleanup())

    def test_missing_trace_is_incomplete_despite_completed_run_and_cleanup(self):
        driver, cli = self.driver("missing-trace")
        self.assertEqual(self.execute(driver), 2)
        self.assertFalse(self.store.open_cleanup())
        self.assertEqual(sum("--async" in a for a in cli.calls), 1)

    def test_old_isolation_receipt_or_missing_acknowledgment_is_rejected(self):
        validate(self.session, self.case)
        for key in ("execution_scope", "other_tools_available_acknowledged"):
            session = copy.deepcopy(self.session)
            session.pop(key)
            with self.assertRaises(ValueError):
                validate(session, self.case)
        self.basis.pop("execution_scope")
        self.basis["isolation_verified"] = True
        self.write_basis()
        with self.assertRaises(ValueError):
            validate(self.session, self.case)

    def test_preview_exposure_and_one_call_cap_cannot_be_relaxed(self):
        for key, value in (("approval_policy", "allow_mutations"),
                           ("resolved_tools", ["system_time"]),
                           ("allowed_publisher_operations", ["post"])):
            basis = copy.deepcopy(self.basis)
            self.basis["effective_policy"][key] = value
            self.write_basis()
            with self.assertRaises(ValueError):
                validate(self.session, self.case)
            self.basis = basis
        self.write_basis()
        self.session["baseline"]["max_tool_calls_per_run"] = 2
        with self.assertRaises(ValueError):
            validate(self.session, self.case)

    def test_optional_refs_absence_never_fabricates_empty_list(self):
        detail = self.sources["detail"]
        self.assertNotIn("tool_refs", fixture_baseline(detail))
        self.assertEqual(fixture_baseline(dict(detail, tool_refs=[])), self.baseline)
        with self.assertRaises(ValueError):
            fixture_baseline(dict(detail, tool_refs=None))
        validate(self.session, self.case)
        driver, cli = self.driver("empty-refs")
        self.assertEqual(self.execute(driver), 0)
        self.assertTrue(any("managed-start" in a for a in cli.calls))

    def test_authored_refs_source_identity_revision_and_success_required(self):
        changes = [
            ("creation", lambda x: x["draft"]["deployment"].pop("tool_refs")),
            ("preview", lambda x: x["desired_spec"].update(tool_refs=[{"type": "connector"}])),
            ("creation", lambda x: x["operations"].update(deployment_id="other")),
            ("creation", lambda x: x["application"].update(source_revision=4)),
            ("preview", lambda x: x.update(onboarding_id="other")),
            ("detail", lambda x: x.update(active_revision_id="other")),
            ("creation", lambda x: x["application"].update(deployment_outcome="unknown")),
        ]
        original = copy.deepcopy(self.sources)
        for label, change in changes:
            self.sources = copy.deepcopy(original)
            change(self.sources[label])
            self.write_sources()
            self.write_basis()
            with self.assertRaises(ValueError):
                validate(self.session, self.case)

    def test_source_hash_or_evidence_missing_stops_before_any_call(self):
        with (self.root / "creation.json").open("a") as f:
            f.write(" ")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no call")):
            with self.assertRaises(ValueError):
                validate(self.session, self.case)
        self.basis.pop("tool_refs_evidence")
        self.write_basis()
        with self.assertRaises(KeyError):
            validate(self.session, self.case)

    def test_wrong_run_or_unknown_result_never_pass(self):
        for mode in ("wrong-run", "unknown-result"):
            self.session["authorization_id"] = mode
            driver, cli = self.driver(mode)
            self.assertEqual(self.execute(driver), 2)
            self.assertFalse(self.store.open_cleanup())

    def test_failed_stop_keeps_cleanup_open_and_blocks_next_run(self):
        driver, cli = self.driver("stop-fails")
        self.assertNotEqual(self.execute(driver), 0)
        self.assertTrue(self.store.open_cleanup())
        self.assertEqual(sum("managed-stop" in a for a in cli.calls), 1)
        self.assertLessEqual(sum("status" in a for a in cli.calls), 12)

    def test_expired_authority_no_calls_and_output_sanitized(self):
        self.session["expires_at"] = "2026-10-10T00:00:00Z"
        driver, cli = self.driver()
        with self.assertRaises(Halt):
            self.execute(driver)
        self.assertEqual(cli.calls, [])
        self.session["expires_at"] = "2026-10-11T00:20:00Z"
        driver, cli = self.driver("secret")
        self.execute(driver)
        for p in self.root.rglob("*"):
            if p.is_file():
                self.assertNotIn(("sk-" + "Z" * 40).encode(), p.read_bytes())

    def test_pin_and_session_validation_precede_service_calls(self):
        with self.assertRaises(ValueError):
            validate(self.session, CASE)
        with mock.patch("seren_harness.employee_run._sha256", return_value="wrong"), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no call")):
            with self.assertRaises(ValueError):
                pinned_cli({"executable": "fake", "executable_sha256": "expected"}, self.store.sanitizer)
        text = (SOURCE / "seren_harness/cli.py").read_text()
        self.assertIn('"employee-time"', text)
