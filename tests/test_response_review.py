"""Offline input/evidence boundaries, not a semantic oracle for employee answers."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE
from seren_harness import cli
from seren_harness.response_review import evaluate

CASE = json.loads((SOURCE / "cases/scott-compound-research.json").read_text())
TEXT = (SOURCE / "tests/fixtures/compound-page1-synthetic.txt").read_text()


class ResponseReviewTests(unittest.TestCase):
    def criteria(self, report):
        return {c["id"]: c for c in report["criteria"]}

    def assessment(self, text=TEXT):
        report = evaluate(CASE, text)
        return {"response_sha256": report["response_sha256"],
                "case_digest": report["expectation"]["digest"], "assessor": "Test operator",
                "checks": {c["id"]: {"verdict": "passed", "excerpt": "Cedar Cooperative",
                                       "note": "Synthetic test of attributed review handling, "
                                               "not a semantic evaluation."}
                           for c in CASE["review"]}}

    def test_valid_copy_records_objective_evidence_without_semantic_certification(self):
        report = evaluate(CASE, TEXT, {"command": CASE["command"]}, synthetic=True)
        checks = self.criteria(report)
        self.assertEqual(checks["reference_syntax"]["verdict"], "passed")
        self.assertEqual(checks["command_context"]["verdict"], "passed")
        self.assertEqual(checks["qualification"]["verdict"], "unverified")
        self.assertEqual(report["verdict"], "partial_evidence")
        self.assertEqual(report["functional_content"], "review_pending")
        self.assertIn("synthetic", report["evidence_origin"])
        self.assertFalse(report["live_capture_verified"])

    def test_missing_content_or_links_is_a_copy_discrepancy_not_product_defect(self):
        for text in ("", "Cedar Cooperative saved research without copied links"):
            report = evaluate(CASE, text)
            self.assertEqual(self.criteria(report)["reference_syntax"]["verdict"], "failed")
            self.assertEqual(report["verdict"], "discrepancy_in_supplied_evidence")
            self.assertFalse(report["complete_pass"])
        self.assertEqual(self.criteria(evaluate(CASE, ""))["response_present"]["verdict"], "failed")

    def test_contradiction_needs_explicit_review_not_keyword_approval(self):
        text = "Cedar Cooperative is fully qualified. Newly researched. https://example.invalid/cedar"
        report = evaluate(CASE, text)
        self.assertEqual(self.criteria(report)["qualification"]["verdict"], "unverified")
        review = self.assessment(text)
        review["checks"] = {"qualification": {"verdict": "failed",
            "excerpt": "fully qualified", "note": "Contradicts the dated unqualified baseline; "
            "current database state has not been checked."}}
        report = evaluate(CASE, text, assessment=review)
        self.assertEqual(report["functional_content"], "failed_operator_review")
        self.assertEqual(self.criteria(report)["qualification"]["evidence"]["assessor"],
                         "Test operator")

    def test_missing_metadata_stays_unknown_and_supplied_metadata_is_only_attributed(self):
        report = evaluate(CASE, TEXT)
        self.assertTrue(all(v is None for v in report["metadata"].values()))
        self.assertIsNone(report["latency_ms"])
        report = evaluate(CASE, TEXT, {"message_id": "123", "response_timestamp": "22:19"})
        self.assertEqual(report["metadata"]["response_timestamp"], "22:19")
        self.assertEqual(self.criteria(report)["correlation"]["verdict"], "unverified")
        self.assertIsNone(report["latency_ms"])

    def test_even_all_operator_content_passes_cannot_become_complete_pass(self):
        report = evaluate(CASE, TEXT, {"command": CASE["command"]}, self.assessment())
        self.assertEqual(report["functional_content"], "passed_operator_review")
        self.assertEqual(report["verdict"], "partial_evidence")
        self.assertFalse(report["complete_pass"])
        for key in ("no_new_research", "no_database_mutation", "current_record", "correlation", "latency"):
            self.assertEqual(self.criteria(report)[key]["verdict"], "unverified")

    def test_reviews_are_bound_to_input_and_require_real_excerpts_and_reasons(self):
        review = self.assessment()
        with self.assertRaises(ValueError):
            evaluate(CASE, TEXT + " altered", assessment=review)
        review["checks"]["entity"]["excerpt"] = "not in input"
        with self.assertRaises(ValueError):
            evaluate(CASE, TEXT, assessment=review)
        review["checks"]["entity"].update(excerpt="Cedar Cooperative", note="")
        with self.assertRaises(ValueError):
            evaluate(CASE, TEXT, assessment=review)

    def test_offline_cli_sanitizes_every_saved_artifact_and_never_constructs_live_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canaries = ("ghp_abcdefghijklmnopqrstuvwx", "super-private-value-123456")
            response = root / "input.txt"
            response.write_text(TEXT + "\npassword=" + canaries[1] + "\n" + canaries[0]
                                + "\nhttps://example.org/setup?token=private-query-value")
            metadata = root / "metadata.json"
            metadata.write_text(json.dumps({"notes": "Bearer " + canaries[1]}))
            args = ["review-response", "--case", str(SOURCE / "cases/scott-compound-research.json"),
                    "--response", str(response), "--metadata", str(metadata),
                    "--out", str(root / "result"), "--synthetic"]
            output = io.StringIO()
            with mock.patch("seren_harness.cli.Session", side_effect=AssertionError("live session")), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(args), 2)
            saved = "".join(p.read_text() for p in (root / "result").iterdir()) + output.getvalue()
            for canary in (*canaries, "private-query-value"):
                self.assertNotIn(canary, saved)
            report = json.loads((root / "result/report.json").read_text())
            self.assertTrue(report["sanitization_changed"])
            for p in (root / "result").iterdir():
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 5)  # Does not overwrite prior evidence.

    def test_real_copy_origin_and_wrong_command_are_not_silently_upgraded(self):
        report = evaluate(CASE, TEXT, {"command": "@Example Research Employee mention lookup"})
        self.assertEqual(report["evidence_origin"], "operator-supplied Discord response")
        self.assertEqual(self.criteria(report)["command_context"]["verdict"], "failed")
        self.assertFalse(report["complete_pass"])


if __name__ == "__main__":
    unittest.main()
