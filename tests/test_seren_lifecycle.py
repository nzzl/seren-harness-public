"""Lifecycle decisions with v0.10.0 detail and cloud-summary response shapes.

Synthetic minimal projections of the published schemas. The saved live readback
only establishes stopped status and absence of the lifecycle field, not a full
captured fixture or live verification of the cloud status endpoint.
"""
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import seren_support  # noqa: F401
from seren_harness import seren
from seren_harness.seren_cli import CliResult, OK, TIMEOUT
from seren_harness.state import Store


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="seren-lifecycle-")
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        self.addCleanup(self.store.close)
        self.run = self.store.create_run("operational", "emp", "simulation", "d", "0")
        self.detail = {"deployment_id": "dep", "status": "stopped",
                       "conditions": [{"type": "Ready", "status": "False"}]}
        self.summary = {"id": "dep", "status": "stopped",
                        "desired_lifecycle_state": "stopped", "control_generation": 4}
        self.stop_status = OK
        self.client = mock.Mock(history=[])
        self.client.json.side_effect = self.command
        self.client.display_argv.side_effect = lambda args: list(args)
        ticks = iter(range(100))
        config = types.SimpleNamespace(deployment_id="dep", shutdown_timeout=1, poll_interval=0)
        self.ctx = seren.Context(config, self.store, self.run, self.client,
                                 clock=lambda: next(ticks), sleep=lambda _: None)

    def command(self, *args):
        self.client.history.append(list(args))
        if args[1] == "managed-get":
            data = dict(self.detail)
        elif args[1:4] == ("cloud", "deployment", "status"):
            data = dict(self.summary)
        elif args[1] == "managed-deployment-activity":
            data = {"runs": []}
        elif args[1] == "managed-stop":
            return CliResult(self.stop_status, list(args), {"data": {"status": "accepted"}})
        else:
            self.fail("unexpected command " + str(args))
        return CliResult(OK, list(args), {"data": data})

    def initial(self):
        result = seren.initial_state(self.ctx)
        self.store.record_step(self.run, 0, "initial_state", result)
        return result

    def cleanup_item(self):
        self.store.add_cleanup(self.run, "emp", "stop_runtime", "dep")
        return self.store.open_cleanup(run_id=self.run)[0]

    def test_stopped_detail_without_intent_uses_authoritative_summary(self):
        result = self.initial()
        self.assertEqual(result.outcome, "passed")
        self.assertEqual(result.evidence["control_generation"], 4)
        self.assertIn(["agent", "cloud", "deployment", "status", "dep"],
                      result.evidence["commands"])
        self.assertFalse(any(c[1] in ("managed-start", "managed-stop")
                             for c in self.client.history))

    def test_stopped_compute_with_running_intent_does_not_pass_initial_state(self):
        self.summary["desired_lifecycle_state"] = "running"
        self.assertEqual(self.initial().outcome, "blocked")

    def test_missing_invalid_or_wrong_target_summary_stays_unproven(self):
        original = dict(self.summary)
        for change in ({"id": "other"}, {"control_generation": None},
                       {"control_generation": True}, {"status": None},
                       {"desired_lifecycle_state": None}):
            self.summary = dict(original, **change)
            self.assertEqual(self.initial().outcome, "blocked", change)
            self.assertIsNone(seren._reconcile_start(self.ctx), change)
            self.assertIsNone(seren._reconcile_stop(self.ctx), change)

    def test_ambiguous_start_reconciles_from_generation_without_retry(self):
        self.initial()
        self.summary.update(status="building", desired_lifecycle_state="running",
                            control_generation=5)
        self.assertTrue(seren._reconcile_start(self.ctx))
        self.summary.update(desired_lifecycle_state="stopped", control_generation=4)
        self.assertFalse(seren._reconcile_start(self.ctx))
        self.summary["control_generation"] = 5
        self.assertIsNone(seren._reconcile_start(self.ctx))
        self.assertFalse(any(c[1] == "managed-start" for c in self.client.history))

    def test_already_stopped_summary_closes_cleanup_without_stop(self):
        self.assertTrue(seren.stop_and_confirm(self.ctx, self.cleanup_item()))
        self.assertFalse(any(c[1] == "managed-stop" for c in self.client.history))

    def test_accepted_stop_requires_observed_stopped_status(self):
        self.summary.update(status="running", desired_lifecycle_state="running")
        for observed, expected in (("stopped", True), ("running", False)):
            self.detail["status"] = observed
            self.assertEqual(seren.stop_and_confirm(self.ctx, self.cleanup_item()), expected)

    def test_unanswered_stop_reconciles_intent_then_confirms_observed_stop(self):
        self.summary.update(status="stopped", desired_lifecycle_state="stopping")
        self.stop_status = TIMEOUT
        self.assertTrue(seren.stop_and_confirm(self.ctx, self.cleanup_item()))
        self.assertEqual(sum(c[1] == "managed-stop" for c in self.client.history), 1)
        self.assertEqual(self.store.actions(self.run)[0]["status"], "reconciled_applied")
