"""The offline Pydantic Evals path: same results as the classifier, deterministic, no models."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from seren_harness import journeys, offline_evals

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"MatchesExpected", "KeepsProvenance"}
# pydantic_evals loads pydantic_ai's abstract model and provider packages; a concrete
# provider or its SDK must not load.
PROVIDERS = tuple("pydantic_ai.models." + p for p in (
    "openai", "anthropic", "google", "gemini", "groq", "mistral", "cohere", "bedrock",
    "huggingface", "function", "test", "fallback")) + (
    "pydantic_ai.providers.", "openai", "anthropic", "google.genai", "groq", "mistralai",
    "cohere", "boto3", "logfire._internal")


class OfflineEvalTests(unittest.TestCase):
    def test_results_equal_the_classifier_on_every_fixture(self):
        fixture = json.loads(offline_evals.FIXTURES.read_text())
        report = offline_evals.run()
        self.assertEqual((report.name, report.failures), (offline_evals.NAME, []))
        self.assertEqual([c.name for c in report.cases], [c["name"] for c in fixture["cases"]])
        for case, spec in zip(report.cases, fixture["cases"]):
            with self.subTest(case.name):
                direct = journeys.classify(
                    journeys.observe(offline_evals.events(fixture, spec)), offline_evals.SPEC,
                    provenance="synthetic", run_identity=spec.get("run_identity", "match"),
                    shutdown=spec.get("shutdown", "confirmed"),
                    restore=spec.get("restore", "confirmed"),
                    grant_verified=spec.get("grant_verified", False))
                self.assertEqual(case.output, direct)
                self.assertEqual({k: r.value for k, r in case.assertions.items()},
                                 dict.fromkeys(ALLOWED, True))
                self.assertEqual(case.metadata["provenance"], "synthetic")
        verdicts = {c.metadata["verdict"] for c in report.cases}
        self.assertEqual(verdicts, {"pass", "fail", "incomplete", "unknown"})

    def test_a_wrong_expectation_is_reported_not_hidden(self):
        fixture = json.loads(offline_evals.FIXTURES.read_text())
        fixture["cases"][0]["expected"]["classification"] = "incomplete"
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "c.json"
        path.write_text(json.dumps(fixture))
        case = offline_evals.run(path).cases[0]
        self.assertEqual((case.assertions["MatchesExpected"].value,
                          case.assertions["KeepsProvenance"].value), (False, True))

    def test_evaluators_come_from_the_allowlist(self):
        dataset = offline_evals.dataset()
        self.assertEqual({type(e).__name__ for e in dataset.evaluators}, ALLOWED)
        self.assertEqual({e.__name__ for e in offline_evals.EVALUATORS}, ALLOWED)
        self.assertEqual(dataset.report_evaluators, [])
        self.assertTrue(all(c.evaluators == [] for c in dataset.cases))

    def test_no_model_provider_module_is_imported(self):
        # A fresh interpreter, so other tests' imports cannot mask or cause a match.
        code = ("import sys; from seren_harness import offline_evals; r = offline_evals.run(); "
                "print(len(r.cases), sorted(m for m in sys.modules if m.startswith(%r)))"
                % (PROVIDERS,))
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "11 []\n", ""))


if __name__ == "__main__":
    unittest.main()
