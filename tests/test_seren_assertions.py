"""Critical assertion contracts: enforcement, recorded provenance, history and resume."""
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import seren_support  # noqa: F401  (imports the tested tree)
from seren_harness import assertions, cli, seren
from seren_harness.journey import JourneyRunner, Step
from seren_harness.models import PASSED, passed
from seren_harness.report import build_report, render_markdown
from seren_harness.sanitize import Sanitizer
from seren_harness.state import Store

REPLY = "discord.reply_to_probe"
GOOD_REPLY = {"reply_id": "20", "probe_id": "10", "reply_author": "900",
              "expected_author": "900", "reply_channel": "500", "expected_channel": "500",
              "matched_by": "reply_reference", "latency_ms": 40, "window_ms": 120000}
STEPS = ["verify_reply", "after"]


class AssertionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="seren-assertion-test-")
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state")
        self.addCleanup(self.store.close)
        self.run_id = self.store.create_run("operational", "emp", "simulation", "d", "0")

    def execute(self, evidence):
        steps = [Step("verify_reply", lambda ctx: passed("reply seen", **evidence)),
                 Step("after", lambda ctx: passed("dependent step"))]
        JourneyRunner(self.store, self.run_id, "operational", steps, None).execute()
        return {s["name"]: s for s in self.store.steps(self.run_id)}

    def test_critical_pass_without_required_evidence_is_blocked(self):
        for missing in ("reply_author", "probe_id", "latency_ms"):
            unproven = dict(GOOD_REPLY)
            unproven.pop(missing)
            self.store.invalidate_steps(self.run_id, STEPS, "next case")
            steps = self.execute(unproven)
            self.assertEqual(steps["verify_reply"]["outcome"], "blocked", missing)
            self.assertEqual(steps["after"]["outcome"], "not_run", missing)
            finding = self.store.finding(steps["verify_reply"]["finding_key"])
            self.assertEqual(finding["classification"], "harness_defect")
        wrong_author = dict(GOOD_REPLY, reply_author="901")
        self.store.invalidate_steps(self.run_id, STEPS, "next case")
        self.assertEqual(self.execute(wrong_author)["verify_reply"]["outcome"], "blocked")

    def test_valid_evidence_passes_with_its_contract_snapshot(self):
        steps = self.execute(GOOD_REPLY)
        self.assertEqual(steps["verify_reply"]["outcome"], PASSED)
        recorded = steps["verify_reply"]["evidence"]["assertion"]
        self.assertEqual(recorded["unmet"], [])
        self.assertEqual(recorded["snapshot"], assertions.definition(REPLY))

    def test_snapshots_survive_sanitization_unchanged(self):
        for assertion_id in ("passwords.approval_applied", REPLY, assertions.SHUTDOWN):
            snapshot = assertions.definition(assertion_id)
            self.assertEqual(Sanitizer().value(snapshot), snapshot, assertion_id)

    def test_historical_report_keeps_recorded_definition_after_registry_change(self):
        self.execute(GOOD_REPLY)
        original = assertions.definition(REPLY)
        changed = dict(assertions._CONTRACTS[REPLY], version=2, claim="A different claim.")
        with mock.patch.dict(assertions._CONTRACTS, {REPLY: changed}):
            check = build_report(self.store, self.run_id, STEPS)["checks"][0]
            self.assertTrue(assertions.stale("verify_reply", self.store.steps(
                self.run_id)[0]["evidence"]))
        self.assertEqual((check["version"], check["claim"], check["digest"]),
                         (1, original["claim"], original["digest"]))

    def test_legacy_record_is_readable_without_fabricated_provenance(self):
        legacy = types.SimpleNamespace(outcome=PASSED, summary="old pass", evidence={})
        self.store.record_step(self.run_id, 0, "verify_reply", legacy)
        report = build_report(self.store, self.run_id, STEPS)
        self.assertEqual(report["checks"], [{"result": "verify_reply", "outcome": PASSED,
                                             "provenance": "not recorded"}])
        self.assertIn("provenance not recorded", render_markdown(report))
        self.assertEqual(self.store.steps(self.run_id)[0]["outcome"], PASSED)  # Not rewritten.

    def test_resume_does_not_reuse_a_pass_under_another_contract_version(self):
        self.execute(GOOD_REPLY)
        self.assertEqual(cli.invalidate_changed_assertions(self.store, self.run_id), [])
        changed = dict(assertions._CONTRACTS[REPLY], version=2)
        with mock.patch.dict(assertions._CONTRACTS, {REPLY: changed}):
            self.assertEqual(cli.invalidate_changed_assertions(self.store, self.run_id),
                             ["verify_reply"])
        self.assertNotIn("verify_reply", [s["name"] for s in self.store.steps(self.run_id)])
        self.assertIn("steps_invalidated", [e["kind"] for e in self.store.events(self.run_id)])

    def test_shutdown_is_not_confirmed_without_a_stopped_status_read(self):
        cleanup = self.store.add_cleanup(self.run_id, "emp", "stop_runtime", "dep")
        ctx = types.SimpleNamespace(store=self.store)
        item = {"id": cleanup, "run_id": self.run_id}
        self.assertFalse(seren.record_shutdown(ctx, item, {"how": "stop confirmed",
                                                           "confirmed_status": "stopping"}))
        report = build_report(self.store, self.run_id, ["verify_reply"])
        self.assertEqual(report["cleanup"][0]["status"], "failed")
        self.assertFalse(report["completion"]["clean"])
        self.assertTrue(seren.record_shutdown(ctx, item, {
            "how": "stop confirmed", "confirmed_status": "stopped",
            "confirmed_by": "status read"}))


if __name__ == "__main__":
    unittest.main()
