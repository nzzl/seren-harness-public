"""Synthetic reply-content checks; no captured private message is included."""
import hashlib
from pathlib import Path
import tempfile
import types
import unittest

from test_seren_journeys import SYNTHETIC_EXPECTATION, SimulatedJourney
from seren_harness import cli, discord, seren
from seren_harness.models import PASSED, Finding
from seren_harness.report import build_report, render_markdown
from seren_harness.sanitize import Sanitizer
from seren_harness.state import Store

MARKER = "SH-0123456789AB"  # SYNTHETIC
SYNTHETIC_REFUSAL = ("Cannot answer this synthetic probe until the test setup is ready.")
SECRET = "sk-" + "Q" * 32  # Key-shaped test material the sanitizer must remove.
CONTENT = "verify_reply_content"


class ComparisonTests(unittest.TestCase):
    def test_only_ack_plus_the_exact_marker_matches_after_whitespace_normalization(self):
        expected = "ACK " + MARKER
        for text in (expected, "  ACK   " + MARKER + "\n", "\tACK\n" + MARKER):
            self.assertTrue(discord.content_matches(text, expected), repr(text))
        for text in (SYNTHETIC_REFUSAL, "ACK", "ACK SH-000000000000",
                     "ack " + MARKER, "ACK " + MARKER.lower(), "ACK " + MARKER + " thanks",
                     "Sure. ACK " + MARKER, "ACK " + MARKER + ".", "ACK" + MARKER, "", None):
            self.assertFalse(discord.content_matches(text, expected), repr(text))


class Context:
    """A minimal step context: verify_reply's stored evidence and the in-memory reply."""

    def __init__(self, reply, expectation=SYNTHETIC_EXPECTATION):
        self.reply = reply
        self.config = types.SimpleNamespace(expected_reply=expectation)
        self.store = types.SimpleNamespace(sanitizer=Sanitizer())
        self._evidence = {"send_probe": {"marker": MARKER},
                          "verify_reply": {"reply_id": "20"}}

    def evidence(self, step):
        return self._evidence[step]


class StepTests(unittest.TestCase):
    def test_verdict_uses_the_complete_reply_never_a_retained_prefix(self):
        long_reply = "ACK " + MARKER + " " + "x" * 600
        result = seren.verify_reply_content(Context({"id": "20", "content": long_reply}))
        self.assertEqual(result.outcome, "failed")
        self.assertTrue(result.evidence["content"]["truncated"])
        # The reply was not received in this process: no verdict, whatever was retained.
        result = seren.verify_reply_content(Context(None))
        self.assertEqual((result.outcome, result.finding), ("blocked", None))
        self.assertNotIn("content_matches", result.evidence)
        other = seren.verify_reply_content(Context({"id": "21", "content": "ACK " + MARKER}))
        self.assertEqual(other.outcome, "blocked")

    def test_retained_content_is_sanitized_capped_and_hashes_what_it_keeps(self):
        text = "ACK " + MARKER + " " + SECRET + " " + "y" * 600
        kept = seren.retained_content(Sanitizer(), text)
        self.assertNotIn(SECRET, kept["text"])
        self.assertEqual((kept["redacted"], kept["truncated"], kept["retained_chars"]),
                         (True, True, 500))
        self.assertEqual(kept["sha256"], hashlib.sha256(kept["text"].encode()).hexdigest())
        self.assertNotEqual(kept["sha256"], hashlib.sha256(text.encode()).hexdigest())
        plain = seren.retained_content(Sanitizer(), "ACK " + MARKER)
        self.assertEqual((plain["redacted"], plain["truncated"]), (False, False))


class ContentJourneyTests(SimulatedJourney):
    def run_with(self, reply_text, expected_code):
        self.set_scenario(reply_text=reply_text)
        code, output = self.harness("run")
        self.assertEqual(code, expected_code, output)
        return self.steps(), self.report()

    def assert_delivery_and_cleanup_intact(self, steps, report):
        self.assertEqual(steps["verify_reply"]["outcome"], "passed")
        self.assertEqual(steps["verify_reply"]["evidence"]["matched_by"], "reply_reference")
        self.assertNotIn("content", steps["verify_reply"]["evidence"])
        self.assertEqual([c["status"] for c in report["cleanup"]], ["done"])
        self.assertEqual(self.sim_state()["deployment"]["status"], "stopped")

    def test_exact_response_passes_with_its_expectation_source(self):
        self.provision()
        steps, report = self.run_with("  ACK   {marker}\n", 0)
        self.assert_delivery_and_cleanup_intact(steps, report)
        content = steps[CONTENT]
        marker = steps["send_probe"]["evidence"]["marker"]
        self.assertEqual(content["outcome"], "passed")
        self.assertEqual((content["evidence"]["expected"], content["evidence"]["compared"]),
                         ("ACK " + marker, "complete reply"))
        self.assertEqual(content["evidence"]["expectation_source"],
                         SYNTHETIC_EXPECTATION["source"])
        probe = [line for line in self.channel.read_text().splitlines() if marker in line][0]
        self.assertIn("ACK followed by this marker, unchanged: " + marker, probe)
        check = next(c for c in report["checks"] if c["result"] == CONTENT)
        self.assertEqual((check["id"], check["unmet"]), ("discord.reply_content", []))

    def test_any_other_captured_response_fails_content_only(self):
        self.provision()
        texts = (SYNTHETIC_REFUSAL, "ACK", "ACK SH-000000000000", "ack {marker}",
                 "ACK {marker} and more")
        for text in texts:
            steps, report = self.run_with(text, 1)
            self.assert_delivery_and_cleanup_intact(steps, report)
            content = steps[CONTENT]
            self.assertEqual((content["outcome"], content["evidence"]["content_matches"]),
                             ("failed", False), text)
            marker = steps["send_probe"]["evidence"]["marker"]
            self.assertEqual(content["evidence"]["content"]["text"],
                             text.replace("{marker}", marker))
            self.assertEqual(report["outcome"], "failed")
        [finding] = self.store().findings()
        self.assertEqual((finding["step"], finding["occurrences"]), (CONTENT, len(texts)))
        self.assertEqual(finding["classification"], "uncertain")

    def test_missing_content_is_unverified_not_failed(self):
        self.provision()
        for text in (False, "", "  \n "):
            steps, report = self.run_with(text, 2)
            self.assert_delivery_and_cleanup_intact(steps, report)
            content = steps[CONTENT]
            self.assertEqual((content["outcome"], content["finding"]), ("blocked", None))
            self.assertIn("unverified", content["summary"])
            self.assertNotIn("content_matches", content["evidence"])
            self.assertTrue(any(item.startswith(CONTENT + ": reply content was empty")
                                for item in report["limitations"]))
        self.assertEqual(self.store().findings(), [])

    def test_without_an_expectation_content_is_retained_but_not_judged(self):
        self.write_config(discord={"sender": "simulation", "observer": "simulation",
                                   "channel_id": "sim-channel", "employee_user_id": "sim-employee",
                                   "reply_timeout_seconds": 1.5,
                                   "simulation_channel_file": "sim-channel.jsonl"})
        self.provision()
        steps, report = self.run_with(SYNTHETIC_REFUSAL, 2)
        self.assert_delivery_and_cleanup_intact(steps, report)
        content = steps[CONTENT]
        self.assertEqual(content["outcome"], "blocked")
        self.assertEqual(content["evidence"]["content"]["text"], SYNTHETIC_REFUSAL)
        self.assertNotIn("expected", content["evidence"])
        self.assertEqual(self.store().findings(), [])

    def test_sensitive_reply_material_never_persists_and_truncation_is_reported(self):
        self.provision()
        steps, _ = self.run_with("ACK {marker} " + SECRET + " " + "z" * 700, 1)
        kept = steps[CONTENT]["evidence"]["content"]
        self.assertEqual((kept["redacted"], kept["truncated"], kept["retained_chars"]),
                         (True, True, 500))
        self.assertEqual(kept["sha256"], hashlib.sha256(kept["text"].encode()).hexdigest())
        code, output = self.harness("draft-issue", self.latest_run()["id"])
        self.assertEqual(code, 0, output)
        state = self.root / "state"
        persisted = b"".join(p.read_bytes() for p in state.rglob("*") if p.is_file())
        self.assertNotIn(SECRET.encode(), persisted)
        self.assertTrue(list((state / "drafts").iterdir()))

    def test_content_failure_does_not_change_lifecycle_retest_recovery(self):
        self.provision()
        self.set_scenario(readiness="binding_failure")
        self.harness("run")
        [lifecycle] = self.store().findings()
        self.set_scenario(readiness="ready", reply_text=SYNTHETIC_REFUSAL)
        code, output = self.harness("run", "--retest", lifecycle["key"])
        self.assertEqual(code, 1, output)
        [retest] = self.store().retests(lifecycle["key"])
        self.assertEqual(retest["outcome"], "resolved")
        self.assertEqual(retest["detail"]["workflow_verdict"], "failed")
        self.assertEqual(retest["detail"]["recovery_condition"],
                         "step `runtime_ready` and every later step pass")
        self.assertTrue(any(d.startswith(CONTENT + ": failed")
                            for d in retest["detail"]["diagnostic"]))
        report = self.report()
        self.assertEqual((report["outcome"], report["retest"]["finding_status"]),
                         ("failed", "resolved"))
        self.assertTrue(report["retest"]["cleanup_confirmed"])


class HistoricalRecordTests(unittest.TestCase):
    """Runs recorded before the criterion existed keep their verdicts and get no content
    result."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="seren-content-history-")
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state")
        self.addCleanup(self.store.close)
        self.steps = [s.name for s in cli.JOURNEYS["operational"]]

    def legacy_run(self, status="complete"):
        run_id = self.store.create_run("operational", "emp", "simulation", "d", "0")
        for seq, name in enumerate(self.steps[:-1]):  # Everything up to verify_reply.
            self.store.record_step(run_id, seq, name, types.SimpleNamespace(
                outcome=PASSED, summary="old pass", evidence={}))
        self.store.update_run(run_id, status=status)
        return run_id

    def test_legacy_run_report_has_no_content_result_and_keeps_its_outcome(self):
        run_id = self.legacy_run()
        report = build_report(self.store, run_id, self.steps)
        self.assertEqual(report["outcome"], "passed")
        self.assertNotIn(CONTENT, [s["name"] for s in report["steps"]])
        self.assertNotIn(CONTENT, [c["result"] for c in report["checks"]])
        self.assertNotIn(CONTENT, render_markdown(report))

    def test_legacy_retest_keeps_its_recovery_and_an_interrupted_run_does_not_pass(self):
        original = self.store.create_run("operational", "emp", "simulation", "d", "0")
        self.store.record_finding("F-legacy", original, "operational", "runtime_ready",
                                  Finding("runtime_ready_in_time", "defect", "t", "e", "o"))
        run_id = self.legacy_run()
        self.assertEqual(cli.classify_retest(self.store, run_id, "F-legacy", self.steps),
                         "resolved")
        self.assertEqual(self.store.retests("F-legacy")[0]["detail"]["workflow_verdict"],
                         "passed")
        interrupted = self.legacy_run(status="interrupted")
        self.assertEqual(build_report(self.store, interrupted, self.steps)["outcome"],
                         "not_run")


if __name__ == "__main__":
    unittest.main()
