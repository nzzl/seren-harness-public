"""The offline evaluation reports mismatches as failure and derives observations from runs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from seren_support import SOURCE

sys.path.insert(0, str(SOURCE / "simulation"))
import evaluate  # noqa: E402
import fake_seren  # noqa: E402

MANIFEST = json.loads((SOURCE / "simulation" / "evaluation.json").read_text())
CANARIES = (fake_seren.SECRET_CANARY, "c2ltdWxhdGVkY2FwYWJpbGl0eXRva2Vu")


def case(case_id):
    return next(c for c in MANIFEST["cases"] if c["id"] == case_id)


class JudgementTests(unittest.TestCase):
    def observation(self, outcome, clean, step_outcome, baseline="accept"):
        return {"verdict": evaluate.verdict(outcome, clean), "outcome": outcome,
                "clean_completion": clean, "managed_start_calls": 1,
                "steps": {"runtime_ready": {"outcome": step_outcome}},
                "baseline_verdict": baseline}

    def test_mismatch_is_unsuccessful_and_named(self):
        wrong = evaluate.judge(case("healthy_connector_failure"),
                               self.observation("passed", True, "passed"))
        self.assertFalse(wrong["match"])
        self.assertEqual(wrong["classification"], "false pass")
        self.assertFalse(evaluate.summarize(MANIFEST, [wrong])["successful"])
        right = evaluate.judge(case("healthy_connector_failure"),
                               self.observation("failed", False, "failed"))
        self.assertTrue(right["match"])
        self.assertEqual(right["baseline"]["classification"], "false pass")

    def test_unconfirmed_shutdown_is_rejected_and_baseline_not_applicable(self):
        self.assertEqual(evaluate.verdict("passed", False), "reject")
        self.assertEqual(evaluate.verdict("blocked", False), "inconclusive")
        result = evaluate.judge(case("unconfirmed_shutdown"),
                                dict(self.observation("passed", False, "passed"),
                                     steps={"verify_reply": {"outcome": "passed"}}))
        self.assertTrue(result["match"])
        self.assertEqual(result["baseline"]["verdict"], "not applicable")
        self.assertIsNone(evaluate.summarize(MANIFEST, [result])["operator_minutes"])


class EndToEndTests(unittest.TestCase):
    def evaluate(self, expected):
        directory = tempfile.TemporaryDirectory(prefix="seren-evaluation-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        manifest = dict(MANIFEST, cases=[dict(case("valid_reply"),
                                              expected=dict(case("valid_reply")["expected"],
                                                            **expected))])
        (root / "manifest.json").write_text(json.dumps(manifest))
        code = evaluate.main(["--manifest", str(root / "manifest.json"),
                              "--out", str(root / "out")])
        summary = json.loads((root / "out" / "summary.json").read_text())
        persisted = b"".join(p.read_bytes() for p in (root / "out").rglob("*") if p.is_file())
        for canary in CANARIES:
            self.assertNotIn(canary.encode(), persisted)
        return code, summary

    def test_observed_results_come_from_the_run_and_mismatch_fails(self):
        code, summary = self.evaluate({})
        self.assertEqual((code, summary["successful"]), (0, True))
        self.assertEqual(summary["cases"][0]["observed"]["step_outcome"], "passed")
        self.assertEqual(summary["evidence_mode"], "synthetic simulation")
        code, summary = self.evaluate({"verdict": "reject", "outcome": "failed"})
        self.assertEqual((code, summary["successful"]), (1, False))
        self.assertEqual({m["field"] for m in summary["cases"][0]["mismatches"]},
                         {"verdict", "outcome"})


if __name__ == "__main__":
    unittest.main()
