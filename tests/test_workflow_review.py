"""Scoped functional verdicts, finding/retest linkage and evidence boundaries.

Synthetic bundles are built from the case's own saved texts; they exercise the review rules,
not Example Research Employee's behavior.
"""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from seren_support import SOURCE
from seren_harness import cli
from seren_harness.workflow_review import evaluate, load_bundle

CASE_PATH = SOURCE / "cases/scott-saved-research-reading.json"
CASE = json.loads(CASE_PATH.read_text())
FIELDS = {f["id"]: f["text"] for f in CASE["complete_fields"]}
ELEMENTS = {e["id"]: e["any_of"][0] for e in CASE["initial_elements"]}
CANARY = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"


def paged_messages():
    """The incident's shape: page commands, a split rationale, a stranded heading."""
    rationale = FIELDS["rationale"]
    cut = len(rationale) // 2
    footer = "\nFollow up\n• /bd research entity:Cedar Cooperative page:{} — next page"
    return [
        "Detail 1/16 · Saved research\nExample Research Employee — Cedar Cooperative\n"
        "Recommendation\ninvestigate\nhttps://example.invalid/cedar" + footer.format(2),
        "Detail 2/16\nAssessment\n" + rationale[:cut] + footer.format(3),
        "Detail 3/16\n" + rationale[cut:] + "\nSourced facts\n" + FIELDS["fact_1"] + "\n"
        + FIELDS["fact_2"] + "\nHypotheses, not customer statements:" + footer.format(4),
        "Detail 4/16\n" + "\n".join(FIELDS["hypothesis_" + n] for n in "123") + footer.format(5),
        "Detail 5/16\n" + FIELDS["next_question"] + "\n" + FIELDS["next_action"]
        + footer.format(6),
        "Detail 6/16\n" + "; ".join(FIELDS["unknown_" + n] for n in "123456") + footer.format(7),
    ]


def good_messages(extra=""):
    initial = ("**[Cedar Cooperative](<https://example.invalid/cedar>)** · Saved research · "
               "assessment dated 2026-09-29\nRecommendation: investigate\n"
               + ELEMENTS["reason"] + "\n• " + ELEMENTS["key_uncertainty"] + "\n"
               + ELEMENTS["next_action"] + extra)
    full = [
        "**Assessment**\n" + FIELDS["rationale"] + "\nSourced facts\n- " + FIELDS["fact_1"]
        + "\n- " + FIELDS["fact_2"] + "\nSource: https://example.invalid/cedar · reported"
        " · observed 2026-09-29T07:36:00Z",
        "Hypotheses, not customer statements:\n" + "\n".join(
            "- " + FIELDS["hypothesis_" + n] for n in "123"),
        FIELDS["next_question"] + "\n" + FIELDS["next_action"] + "\nUnanswered questions\n"
        + "; ".join(FIELDS["unknown_" + n] for n in "123456"),
    ]
    return initial, full


def write_bundle(directory, provenance, initial, rest, paged=False, captured=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    items = []
    for index, text in enumerate([initial, *rest]):
        name = "m" + str(index) + ".txt"
        (directory / name).write_text(text)
        items.append({"id": "m" + str(index), "kind": "discord_response" if paged or index == 0
                      else "button_response", "role": "initial" if index == 0 else
                      "continuation" if paged else "complete_view", "path": name,
                      "invocation": ("/bd research entity:Cedar Cooperative page:" + str(index + 1))
                      if paged else "/bd research entity:Cedar Cooperative" if index == 0
                      else "button: Read full assessment"})
    bundle = {"provenance": provenance, "revision": "test-revision", "observed": "synthetic",
              "complete_reading_captured": captured, "items": items}
    (directory / "bundle.json").write_text(json.dumps(bundle))
    return directory / "bundle.json"


def review(bundle_path, assessment=None):
    bundle, items = load_bundle(bundle_path)
    return evaluate(CASE, bundle, items, assessment)[0]


def approve(bundle_path, verdict="passed"):
    report = review(bundle_path)
    return {"bundle_sha256": report["bundle_sha256"], "case_digest": report["case"]["digest"],
            "assessor": "Test operator", "checks": {"operator_reading_assessment": {
                "verdict": verdict, "excerpt": "Saved research",
                "note": "Synthetic attributed judgment for rule testing."}}}


def criteria(report):
    return {c["id"]: c["verdict"] for c in report["criteria"]}


def cli_review(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(["review-workflow", "--case", str(CASE_PATH), *args])
    return code, out.getvalue()


class WorkflowVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_original_paged_experience_fails_the_reading_criteria(self):
        pages = paged_messages()
        report = review(write_bundle(self.root / "b", "operator_supplied", pages[0], pages[1:],
                                     paged=True, captured=False))
        verdicts = criteria(report)
        self.assertEqual(report["verdict"], "failed")
        self.assertEqual(verdicts["initial_summary"], "failed")
        self.assertEqual(verdicts["complete_without_page_commands"], "failed")
        self.assertEqual(verdicts["fields_whole"], "failed")
        self.assertEqual(verdicts["headings_with_content"], "failed")
        whole = next(c for c in report["criteria"] if c["id"] == "fields_whole")
        self.assertEqual(whole["evidence"]["split_across_messages"], ["rationale"])

    def test_missing_required_evidence_prevents_scoped_success(self):
        initial, full = good_messages()
        bundle = write_bundle(self.root / "b", "operator_supplied", initial, full)
        report = review(bundle)
        self.assertEqual(criteria(report)["operator_reading_assessment"], "unverified")
        self.assertEqual(report["verdict"], "incomplete")
        self.assertFalse(report["scoped_success"])
        uncaptured = review(write_bundle(self.root / "c", "operator_supplied", initial, [],
                                         captured=False))
        self.assertEqual(criteria(uncaptured)["complete_without_page_commands"], "unverified")
        self.assertNotEqual(uncaptured["verdict"], "passed")
        passed = review(bundle, approve(bundle))
        self.assertEqual(passed["verdict"], "passed")
        self.assertTrue(passed["scoped_success"])
        self.assertFalse(passed["employee_certification"])
        self.assertEqual(passed["categories"]["source_verification"], ["source_support: unverified"])
        self.assertTrue(all(u["verdict"] == "unverified" for u in passed["unverified"]))

    def test_contradictory_evidence_keeps_the_failure(self):
        initial, full = good_messages()
        bundle = write_bundle(self.root / "b", "operator_supplied", initial, full)
        self.assertEqual(review(bundle, approve(bundle, "failed"))["verdict"], "failed")
        pages = paged_messages()
        paged = write_bundle(self.root / "p", "operator_supplied", pages[0], pages[1:],
                             paged=True, captured=False)
        positive = approve(paged)
        positive["checks"]["operator_reading_assessment"]["excerpt"] = "Saved research"
        self.assertEqual(review(paged, positive)["verdict"], "failed")

    def test_local_evidence_cannot_produce_scoped_success(self):
        initial, full = good_messages()
        bundle = write_bundle(self.root / "b", "local", initial, full)
        report = review(bundle, approve(bundle))
        self.assertTrue(all(v == "passed" for k, v in criteria(report).items()
                            if k != "source_support"))
        self.assertEqual(report["verdict"], "incomplete")
        self.assertIn("evidence provenance local is not accepted by this scope",
                      report["verdict_reasons"])

    def test_raw_json_on_the_reading_path_fails(self):
        initial, full = good_messages()
        report = review(write_bundle(self.root / "b", "operator_supplied", initial,
                                     full + ['{"fit":"hold","kind":"qualify"}']))
        self.assertEqual(criteria(report)["no_raw_json"], "failed")
        self.assertEqual(report["verdict"], "failed")

    def test_assessment_must_match_evidence_and_name_an_assessor(self):
        initial, full = good_messages()
        bundle = write_bundle(self.root / "b", "operator_supplied", initial, full)
        other = write_bundle(self.root / "o", "operator_supplied", initial + " changed", full)
        with self.assertRaises(ValueError):
            review(other, approve(bundle))
        anonymous = approve(bundle)
        anonymous["assessor"] = ""
        with self.assertRaises(ValueError):
            review(bundle, anonymous)


class FindingAndRetestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        pages = paged_messages()
        pages[2] += "\nAuthorization: " + CANARY
        self.original = write_bundle(self.root / "original", "operator_supplied", pages[0],
                                     pages[1:], paged=True, captured=False)

    def tearDown(self):
        self.tmp.cleanup()

    def run_review(self, bundle, name, *extra):
        return cli_review("--evidence", str(bundle), "--state-dir", str(self.state),
                          "--out", str(self.root / name), *extra)

    def finding_key(self):
        db = sqlite3.connect(str(self.state / "state.sqlite3"))
        try:
            return [r[0] for r in db.execute("SELECT key FROM findings")]
        finally:
            db.close()

    def test_repeated_imports_deduplicate_and_retests_link_without_overwriting(self):
        code, _ = self.run_review(self.original, "r1")
        self.assertEqual(code, 1)
        code, _ = self.run_review(self.original, "r2")
        self.assertEqual(code, 1)
        keys = self.finding_key()
        self.assertEqual(len(keys), 1)
        key = keys[0]
        drafts = list((self.state / "drafts").iterdir())
        self.assertEqual([d.name for d in drafts], [key + ".md"])
        db = sqlite3.connect(str(self.state / "state.sqlite3"))
        self.assertEqual(db.execute("SELECT occurrences FROM findings").fetchone()[0], 1)
        original_steps = db.execute("SELECT run_id, name, outcome FROM steps ORDER BY name"
                                    ).fetchall()
        db.close()
        # Identical evidence is not a retest.
        self.assertEqual(self.run_review(self.original, "r3", "--retest", key)[0], 5)

        initial, full = good_messages()
        local = write_bundle(self.root / "local", "local", initial, full)
        code, output = self.run_review(local, "r4", "--retest", key)
        self.assertEqual(code, 2)
        self.assertIn("retest inconclusive", output)
        operator = write_bundle(self.root / "after", "operator_supplied", initial, full)
        assessment = self.root / "assessment.json"
        assessment.write_text(json.dumps(approve(operator)))
        code, output = self.run_review(operator, "r5", "--retest", key, "--assessment",
                                       str(assessment))
        self.assertEqual(code, 0)
        self.assertIn("retest resolved", output)
        self.run_review(operator, "r6", "--retest", key, "--assessment", str(assessment))

        db = sqlite3.connect(str(self.state / "state.sqlite3"))
        try:
            outcomes = [r[0] for r in db.execute("SELECT outcome FROM retests ORDER BY outcome")]
            self.assertEqual(outcomes, ["inconclusive", "resolved"])
            self.assertEqual(len(self.finding_key()), 1)
            original_run = original_steps[0][0]
            self.assertEqual(db.execute(
                "SELECT run_id, name, outcome FROM steps WHERE run_id=? ORDER BY name",
                (original_run,)).fetchall(), original_steps)
        finally:
            db.close()
        draft = (self.state / "drafts" / (key + ".md")).read_text()
        self.assertIn("## Retest history", draft)
        self.assertIn("**resolved**", draft)
        self.assertIn("**inconclusive**", draft)
        self.assertIn("not accepted by this scope", draft)

    def test_sensitive_evidence_stays_out_of_persisted_artifacts(self):
        self.run_review(self.original, "r1")
        persisted = [p for p in self.root.rglob("*") if p.is_file()
                     and not str(p).startswith(str(self.root / "original"))]
        self.assertTrue(persisted)
        secret = CANARY.split(" ", 1)[1]
        for path in persisted:
            self.assertNotIn(secret.encode(), path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()


def case_variant(path, **changes):
    """A copy of the case with per-criterion overrides: {criterion_id: {field: value}}."""
    case = json.loads(CASE_PATH.read_text())
    case["version"] = changes.pop("version", case["version"])
    for criterion in case["criteria"]:
        criterion.update(changes.get(criterion["id"], {}))
    path.write_text(json.dumps(case))
    return path


def operator_review(bundle_path, case_path, verdict, excerpt="Saved research"):
    bundle, items = load_bundle(bundle_path)
    report = evaluate(json.loads(Path(case_path).read_text()), bundle, items)[0]
    return {"bundle_sha256": report["bundle_sha256"], "case_digest": report["case"]["digest"],
            "assessor": "Test operator", "checks": {"operator_reading_assessment": {
                "verdict": verdict, "excerpt": excerpt if verdict == "passed" else "",
                "note": "Synthetic attributed judgment: hard to read."}}}


class FindingScopeTests(unittest.TestCase):
    """Finding retests follow the criteria associated with that finding, not the whole workflow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        pages = paged_messages()
        cli_review("--evidence", str(write_bundle(self.root / "original", "operator_supplied",
                   pages[0], pages[1:], paged=True, captured=False)),
                   "--state-dir", str(self.state), "--out", str(self.root / "o"))
        db = sqlite3.connect(str(self.state / "state.sqlite3"))
        self.key, self.observed = db.execute("SELECT key, payload_json FROM findings").fetchone()
        db.close()
        initial, full = good_messages()
        self.after = write_bundle(self.root / "after", "operator_supplied", initial, full)

    def tearDown(self):
        self.tmp.cleanup()

    def retest(self, name, case_path=CASE_PATH, verdict=None, *extra, bundle=None):
        bundle = bundle or self.after
        args = ["--evidence", str(bundle), "--state-dir", str(self.state),
                "--out", str(self.root / name), "--retest", self.key, *extra]
        if verdict:
            path = self.root / (name + "-assessment.json")
            path.write_text(json.dumps(operator_review(bundle, case_path, verdict)))
            args += ["--assessment", str(path)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["review-workflow", "--case", str(case_path), *args])
        report = json.loads((self.root / name / "report.json").read_text()) if code != 5 else None
        return code, report

    def test_relevant_human_failure_is_not_hidden_by_passing_automated_checks(self):
        code, report = self.retest("failed-reading", CASE_PATH, "failed")
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"], "failed")
        self.assertEqual(report["retest"]["outcome"], "still_failing")
        self.assertEqual(report["retest"]["reasons"], ["operator_reading_assessment failed"])
        self.assertEqual(report["retest"]["finding_status"], "open")

    def test_missing_human_assessment_stays_inconclusive(self):
        code, report = self.retest("pending", CASE_PATH)
        self.assertEqual(code, 2)
        self.assertEqual(report["retest"]["outcome"], "inconclusive")
        self.assertIn("operator_reading_assessment unverified", report["retest"]["reasons"])
        self.assertEqual(report["retest"]["finding_status"], "open")

    def test_unrelated_failure_does_not_reproduce_the_finding(self):
        # Make the raw-JSON check belong to no finding, then present raw JSON on the path.
        case = case_variant(self.root / "case.json", no_raw_json={
            "finding": None, "finding_exclusion": "Separate presentation concern for this test."})
        initial, full = good_messages()
        bundle = write_bundle(self.root / "json", "operator_supplied", initial,
                              full + ['{"fit":"hold"}'])
        code, report = self.retest("unrelated", case, "passed", bundle=bundle)
        self.assertEqual(report["verdict"], "failed")
        self.assertEqual(report["retest"]["outcome"], "resolved")
        self.assertIn("no_raw_json failed", report["retest"]["difference"])
        self.assertNotIn("no_raw_json", report["retest"]["linked_criteria"])

    def test_adequate_evidence_resolves_the_finding(self):
        code, report = self.retest("resolved", CASE_PATH, "passed")
        self.assertEqual(code, 0)
        self.assertEqual(report["retest"]["outcome"], "resolved")
        self.assertEqual(report["retest"]["finding_status"], "resolved")
        self.assertIn("**resolved**", (self.state / "drafts" / (self.key + ".md")).read_text())

    def test_associations_must_be_declared(self):
        initial, full = good_messages()
        bundle, items = load_bundle(write_bundle(self.root / "v", "operator_supplied",
                                                 initial, full))
        for change in ({"finding": None}, {"finding": "no-such-finding"}):
            case = json.loads(CASE_PATH.read_text())
            case["criteria"][-2].update(change)
            with self.assertRaises(ValueError):
                evaluate(case, bundle, items)
        case = json.loads(CASE_PATH.read_text())
        del case["criteria"][-2]["finding"]
        with self.assertRaises(ValueError):
            evaluate(case, bundle, items)
        case = json.loads(CASE_PATH.read_text())
        for criterion in case["criteria"]:
            criterion.update({"finding": None, "finding_exclusion": "test"})
        with self.assertRaises(ValueError):
            evaluate(case, bundle, items)

    def test_versioned_reanalysis_keeps_the_original_result_and_deduplicates(self):
        v1 = case_variant(self.root / "v1.json", version=1, operator_reading_assessment={
            "finding": None, "finding_exclusion": "Version 1 omission, reproduced for the test."})
        code, first = self.retest("v1", v1, "failed")
        # Explicitly scoped without the human criterion, that version resolves the finding.
        self.assertEqual(first["retest"]["outcome"], "resolved")
        self.assertEqual(first["retest"]["finding_status"], "resolved")
        prior = first["run"]
        # A reanalysis needs a reason and must reuse the same evidence.
        self.assertEqual(self.retest("no-reason", CASE_PATH, "failed",
                                     "--reanalysis-of", prior)[0], 5)
        initial, full = good_messages(" changed")
        other = write_bundle(self.root / "other", "operator_supplied", initial, full)
        self.assertEqual(self.retest("other", CASE_PATH, "failed", "--reanalysis-of", prior,
                                     "--correction", "x", bundle=other)[0], 5)
        args = ("--reanalysis-of", prior, "--correction", "Associate the operator criterion.")
        code, second = self.retest("v2", CASE_PATH, "failed", *args)
        self.assertEqual(second["retest"]["outcome"], "still_failing")
        self.assertEqual(second["retest"]["supersedes"], prior)
        self.retest("v2-again", CASE_PATH, "failed", *args)
        db = sqlite3.connect(str(self.state / "state.sqlite3"))
        try:
            rows = db.execute("SELECT run_id, outcome FROM retests ORDER BY recorded_at").fetchall()
            self.assertEqual(rows, [(prior, "resolved"), (second["run"], "still_failing")])
            self.assertEqual(db.execute("SELECT occurrences, payload_json FROM findings")
                             .fetchone(), (1, self.observed))
        finally:
            db.close()
        draft = (self.state / "drafts" / (self.key + ".md")).read_text()
        self.assertIn("**Finding status:** open.", draft)
        self.assertIn("superseded by a later reanalysis; result retained", draft)
        self.assertIn("reanalysis of `" + prior + "`", draft)
        self.assertEqual(second["criteria"][-2]["evidence"]["assessor"], "Test operator")


class CaseFileTests(unittest.TestCase):
    """Every shipped case runs through the same engine without case-specific code."""

    def test_each_case_validates_and_missing_evidence_never_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in sorted((SOURCE / "cases").glob("*.json")):
                case = json.loads(path.read_text())
                if "criteria" not in case:
                    continue  # response-review cases use the older single-response format
                bundle = write_bundle(Path(tmp) / path.stem, "operator_supplied",
                                      "Saved research", [], captured=False)
                loaded, items = load_bundle(bundle)
                report = evaluate(case, loaded, items)[0]
                self.assertNotEqual(report["verdict"], "passed", path.name)
                self.assertFalse(report["employee_certification"])
                self.assertTrue(all(u["verdict"] == "unverified" for u in report["unverified"]))
                review = {"bundle_sha256": report["bundle_sha256"],
                          "case_digest": report["case"]["digest"], "assessor": "Test operator",
                          "checks": {c["id"]: {"verdict": "passed", "excerpt": "Saved research",
                                               "note": "Synthetic contradiction test."}
                                     for c in case["criteria"] if c["check"] == "operator"}}
                # A positive human judgment cannot override failed objective criteria.
                if review["checks"]:
                    self.assertEqual(evaluate(case, loaded, items, review)[0]["verdict"],
                                     "failed", path.name)
