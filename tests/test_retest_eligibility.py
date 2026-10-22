"""Which evidence may change a finding, which finding new evidence belongs to, and the record
correction for retests classified before eligibility was checked.

Synthetic bundles from the case's own texts; they exercise the harness rules, not Example Research Employee.
"""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from seren_support import SOURCE  # noqa: F401  (imports the tested tree)
from test_collect import Collect, paged, reading
from test_workflow_review import (CANARY, CASE, CASE_PATH, approve, good_messages,
                                  paged_messages, write_bundle)
from seren_harness import cli
from seren_harness.state import Store
from seren_harness.workflow_review import candidate_findings

ARBITRUM = SOURCE / "cases/scott-arbitrum-parked-reading.json"


def main(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(list(argv))
    return code, out.getvalue()


def paged_bundle(directory, provenance, extra=""):
    pages = paged_messages()
    pages[0] += extra
    return write_bundle(directory, provenance, pages[0], pages[1:], paged=True, captured=False)


class Store_:
    """Read-only views of the finding store for assertions."""

    def __init__(self, state):
        self.path = Path(state) / "state.sqlite3"

    def rows(self, sql, *params):
        db = sqlite3.connect(str(self.path))
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def keys(self):
        return [r[0] for r in self.rows("SELECT key FROM findings ORDER BY first_seen")]

    def counts(self):
        return {t: self.rows("SELECT count(*) FROM " + t)[0][0] for t in (
            "findings", "occurrences", "retests", "drafts", "finding_relations", "runs")}

    def retests(self, key):
        return self.rows("SELECT run_id, outcome, recorded_at FROM retests WHERE finding_key=?"
                         " ORDER BY recorded_at", key)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.db = Store_(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def review(self, bundle, name, *extra, case=CASE_PATH):
        code, out = main("review-workflow", "--case", str(case), "--evidence", str(bundle),
                         "--state-dir", str(self.state), "--out", str(self.root / name), *extra)
        path = self.root / name / "report.json"
        return code, out, json.loads(path.read_text()) if path.exists() else None

    def discord_finding(self):
        code, _, report = self.review(paged_bundle(self.root / "discord", "operator_supplied"),
                                      "discord-review")
        self.assertEqual(code, 1)
        return list(report["findings"])[0]

    def draft(self, key):
        return (self.state / "drafts" / (key + ".md")).read_text()

    def resolve(self, key):
        initial, full = good_messages()
        bundle = write_bundle(self.root / "fixed", "operator_supplied", initial, full)
        assessment = self.root / "approved.json"
        assessment.write_text(json.dumps(approve(bundle)))
        code, _, report = self.review(bundle, "resolved", "--retest", key, "--assessment",
                                      str(assessment))
        self.assertEqual(report["retest"]["finding_status"], "resolved")


class EligibilityTests(Base):
    def test_local_failure_against_a_discord_finding_is_a_diagnostic_not_a_second_finding(self):
        key = self.discord_finding()
        local = paged_bundle(self.root / "local", "local", " (local render)")
        code, out, report = self.review(local, "local-review", "--retest", key)
        self.assertEqual(code, 1)  # the workflow verdict still reports the failure
        self.assertEqual(report["verdict"], "failed")
        retest = report["retest"]
        self.assertEqual(retest["outcome"], "inconclusive")
        self.assertFalse(retest["eligible"])
        self.assertIn("complete_without_page_commands failed on local evidence",
                      retest["diagnostic"])
        self.assertEqual(retest["finding_status"], "open")
        self.assertEqual(report["findings"], {})
        self.assertEqual(self.db.keys(), [key])  # no local copy of the same defect
        self.assertEqual(self.db.rows("SELECT occurrences FROM findings")[0][0], 1)
        md = (self.root / "local-review" / "report.md").read_text()
        self.assertIn("Evidence eligibility: not accepted by this scope", md)
        self.assertIn("| failed |", md)  # failed checks stay visible
        draft = self.draft(key)
        self.assertIn("(ineligible evidence)", draft)
        self.assertIn("diagnostic only: initial_summary failed on local evidence", draft)
        self.assertIn("does not change that status", draft)

    def test_local_success_cannot_resolve_a_discord_finding(self):
        key = self.discord_finding()
        initial, full = good_messages()
        code, _, report = self.review(write_bundle(self.root / "ok", "local", initial, full),
                                      "local-ok", "--retest", key)
        self.assertEqual(report["retest"]["outcome"], "inconclusive")
        self.assertFalse(report["retest"]["eligible"])
        self.assertEqual(report["retest"]["finding_status"], "open")

    def test_inconclusive_evidence_after_a_resolution_keeps_the_resolution_visible(self):
        key = self.discord_finding()
        self.resolve(key)
        _, _, local = self.review(paged_bundle(self.root / "later", "local", " later"),
                                  "later-review", "--retest", key)
        self.assertEqual(local["retest"]["outcome"], "inconclusive")
        self.assertEqual(local["retest"]["finding_status"], "resolved")
        # Accepted evidence that lacks the required human judgment decides nothing either.
        initial, full = good_messages(" again")
        _, _, pending = self.review(write_bundle(self.root / "pending", "operator_supplied",
                                                 initial, full), "pending-review", "--retest", key)
        self.assertEqual(pending["retest"]["outcome"], "inconclusive")
        self.assertIn("operator_reading_assessment unverified", pending["retest"]["reasons"])
        self.assertEqual(pending["retest"]["finding_status"], "resolved")
        draft = self.draft(key)
        self.assertIn("**Finding status:** resolved.", draft)
        self.assertIn("Latest evidence (run `" + pending["run"] + "`) is **inconclusive**", draft)
        self.assertEqual(self.db.keys(), [key])

    def test_accepted_evidence_reproduces_a_resolved_finding(self):
        key = self.discord_finding()
        self.resolve(key)
        _, _, report = self.review(paged_bundle(self.root / "regressed", "operator_supplied",
                                                " regressed"), "regressed-review", "--retest", key)
        self.assertTrue(report["retest"]["eligible"])
        self.assertEqual(report["retest"]["outcome"], "still_failing")
        self.assertEqual(report["retest"]["finding_status"], "open")
        self.assertEqual(self.db.keys(), [key])

    def test_repeated_import_keeps_counts_and_history_order(self):
        key = self.discord_finding()
        local = paged_bundle(self.root / "local", "local", " local")
        self.review(local, "a", "--retest", key)
        before, history = self.db.counts(), self.db.retests(key)
        self.review(local, "b", "--retest", key)
        self.assertEqual(self.db.counts(), before)
        self.assertEqual(self.db.retests(key), history)  # recorded_at is not refreshed


class AssociationTests(Base):
    def test_simulated_evidence_never_links_to_a_real_finding(self):
        key = self.discord_finding()
        before = self.db.counts()
        code, _, _ = self.review(paged_bundle(self.root / "sim", "simulated"), "sim-review",
                                 "--retest", key)
        self.assertEqual(code, 5)
        self.assertEqual(self.db.counts(), before)

    def test_another_case_never_links(self):
        key = self.discord_finding()
        code, _, _ = self.review(paged_bundle(self.root / "other", "operator_supplied"),
                                 "other", "--retest", key, case=ARBITRUM)
        self.assertEqual(code, 5)

    def test_local_first_then_discord_keeps_distinct_claims_explicitly_related(self):
        _, _, first = self.review(paged_bundle(self.root / "local", "local"), "local-first")
        local_key = list(first["findings"])[0]
        _, _, later = self.review(paged_bundle(self.root / "discord", "operator_supplied"),
                                  "discord-review", "--retest", local_key)
        self.assertEqual(later["retest"]["outcome"], "still_failing")
        discord_key = list(later["findings"])[0]
        self.assertNotEqual(discord_key, local_key)
        self.assertEqual(self.db.keys(), [local_key, discord_key])
        self.assertEqual(self.db.rows("SELECT finding_key, related_key, relation FROM "
                                      "finding_relations"),
                         [(discord_key, local_key, "related")])
        self.assertIn("related to `" + local_key + "`", self.draft(discord_key))

    def test_candidates_respect_mode_aliases_and_case(self):
        live = self.discord_finding()
        _, _, sim = self.review(paged_bundle(self.root / "sim", "simulated"), "sim-review")
        store = Store(self.state)
        try:
            self.assertEqual(candidate_findings(store, CASE, "local"), [live])
            self.assertEqual(candidate_findings(store, CASE, "simulation"), list(sim["findings"]))
            other = json.loads(ARBITRUM.read_text())
            self.assertEqual(candidate_findings(store, other, "local"), [])
        finally:
            store.close()


class CollectAssociationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Collect(self.tmp.name, paged(), complete=False)
        self.state = self.c.home / "findings"

    def tearDown(self):
        self.tmp.cleanup()

    def discord(self, name, extra=""):
        main("review-workflow", "--case", str(CASE_PATH), "--evidence",
             str(paged_bundle(Path(self.tmp.name) / name, "operator_supplied", extra)),
             "--state-dir", str(self.state), "--out", str(Path(self.tmp.name) / (name + "-r")))
        return Store_(self.state).keys()

    def test_local_collection_links_the_single_compatible_finding_as_a_diagnostic(self):
        key = self.discord("discord")[0]
        code, out = self.c.run()
        self.assertEqual(code, 1, out)
        self.assertIn("Linked as a retest of " + key, out)
        self.assertIn("diagnostic observation of that finding", out)
        self.assertIn("finding status stays open", out)
        self.assertEqual(Store_(self.state).keys(), [key])

    def test_several_compatible_findings_are_not_guessed(self):
        key = self.discord("discord")[0]
        local = Path(self.tmp.name) / "local"
        main("review-workflow", "--case", str(CASE_PATH), "--evidence",
             str(paged_bundle(local, "local", " other")), "--state-dir", str(self.state),
             "--out", str(local) + "-r")
        keys = Store_(self.state).keys()
        self.assertEqual(len(keys), 2)
        self.c.write(reading())
        code, out = self.c.run()
        self.assertIn("Not linked as a retest: 2 open findings could match", out)
        self.assertIn(key, out)
        self.assertIn("The evidence is saved", out)
        self.assertIsNone(self.c.report()["retest"])


def seed_legacy(test):
    """The retained Maple shape: a Discord finding, then a local run recorded under retest
    rules 1, which both retested it `still_failing` and created a local-mode finding."""
    key = test.discord_finding()
    _, _, local = test.review(paged_bundle(test.root / "local", "local", " local"), "legacy")
    alias = list(local["findings"])[0]
    store = Store(test.state)
    try:
        store.record_retest(key, local["run"], "still_failing", {
            "finding": key, "kind": "retest", "outcome": "still_failing", "provenance": "local",
            "reasons": ["complete_without_page_commands failed"], "run": local["run"],
            "case_version": local["case"]["version"], "workflow_verdict": "failed"})
    finally:
        store.close()
    return key, alias, local


class RecordCorrectionTests(Base):
    def reclassify(self, key, run, name, report=None, correction="Retest rules 2: local "
                   "evidence cannot reproduce a Discord-scoped finding."):
        return main("reclassify-retest", "--state-dir", str(self.state), "--report",
                    str(report or self.root / "legacy" / "report.json"), "--retest", key,
                    "--reanalysis-of", run, "--correction", correction,
                    "--out", str(self.root / name))

    def test_correction_supersedes_the_legacy_retest_and_aliases_the_duplicate(self):
        key, alias, local = seed_legacy(self)
        store = Store(self.state)
        try:  # before: two open findings, so automatic association would refuse to link
            self.assertEqual(len(candidate_findings(store, json.loads(CASE_PATH.read_text()),
                                                    "local")), 2)
        finally:
            store.close()
        before = self.db.counts()
        original_rows = self.db.rows("SELECT * FROM findings ORDER BY key")
        code, out = self.reclassify(key, local["run"], "c1")
        self.assertEqual(code, 0, out)
        self.assertIn("retest still_failing -> inconclusive; finding status open -> open", out)
        self.assertIn(alias + " alias_of " + key, out)
        after = self.db.counts()
        self.assertEqual(after["findings"], before["findings"])
        self.assertEqual(after["occurrences"], before["occurrences"])
        self.assertEqual(after["retests"], before["retests"] + 1)
        self.assertEqual(after["finding_relations"], 1)
        self.assertEqual(self.db.rows("SELECT * FROM findings ORDER BY key"), original_rows)
        outcomes = [r[1] for r in self.db.retests(key)]
        self.assertEqual(outcomes, ["still_failing", "inconclusive"])  # original retained
        draft = self.draft(key)
        self.assertIn("**Finding status:** open.", draft)
        self.assertIn("[superseded by a later reanalysis; result retained]", draft)
        self.assertIn("`" + alias + "` is an alias of this finding", draft)
        self.assertIn("Record correction: this is not a separate finding", self.draft(alias))
        correction = (self.root / "c1" / "correction.md").read_text()
        self.assertIn("Not a new observation", correction)
        store = Store(self.state)
        try:
            self.assertEqual(candidate_findings(store, json.loads(CASE_PATH.read_text()),
                                                "local"), [key])
        finally:
            store.close()
        # Explicitly retesting the alias is refused rather than silently redirected.
        code, _, _ = self.review(paged_bundle(self.root / "x", "local", " x"), "x",
                                 "--retest", alias)
        self.assertEqual(code, 5)

        # Repeating the correction adds nothing and leaves drafts byte-identical.
        snapshot = (self.db.counts(), self.db.retests(key), self.draft(key), self.draft(alias))
        self.assertEqual(self.reclassify(key, local["run"], "c2")[0], 0)
        self.assertEqual((self.db.counts(), self.db.retests(key), self.draft(key),
                          self.draft(alias)), snapshot)

    def test_correction_keeps_a_prior_resolution(self):
        key, _, local = seed_legacy(self)
        self.resolve(key)
        # The legacy local record is older than the resolution, so the status was resolved
        # before and stays resolved; the correction never reopens it.
        code, out = self.reclassify(key, local["run"], "c")
        self.assertEqual(code, 0, out)
        self.assertIn("finding status resolved -> resolved", out)

    def test_correction_refuses_a_report_from_another_run(self):
        key, _, local = seed_legacy(self)
        before = self.db.counts()
        other = self.root / "discord-review" / "report.json"
        self.assertEqual(self.reclassify(key, local["run"], "bad", report=other)[0], 5)
        self.assertEqual(self.reclassify(key, local["run"], "blank", correction=" ")[0], 5)
        self.assertEqual(self.db.counts(), before)

    def test_correction_output_is_private_and_sanitized(self):
        key, _, local = seed_legacy(self)
        code, _ = self.reclassify(key, local["run"], "c",
                                  correction="Rules 2 (Authorization: " + CANARY + ")")
        self.assertEqual(code, 0)
        secret = CANARY.split(" ", 1)[1].encode()
        out = self.root / "c"
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for path in [*out.iterdir(), self.state / "state.sqlite3",
                     *(self.state / "drafts").iterdir()]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
            self.assertNotIn(secret, path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()
