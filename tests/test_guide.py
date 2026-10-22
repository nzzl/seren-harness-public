"""The guided review: operator answers in, the same review out, no hand-written JSON."""
import argparse
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from seren_support import SOURCE
from test_workflow_review import CANARY, CASE, FIELDS, good_messages, paged_messages
from seren_harness import guide

COMMAND = CASE["command"]


def lines(text):
    return text.split("\n") + ["."]


def discord_copy(*messages, clock="01:17"):
    return "\n".join("Example Research Employee\nAPP\n — " + clock + "\n" + m
                     + "\nOnly you can see this • Dismiss message" for m in messages)


class Guided:
    def __init__(self, root):
        self.home = Path(root) / "reviews"

    def run(self, answers, case=CASE["id"]):
        feed = iter(answers)
        asked = []

        def answer(question):
            asked.append(question)
            try:
                return next(feed)
            except StopIteration:
                raise EOFError() from None

        out = io.StringIO()
        args = argparse.Namespace(case=case, home=str(self.home), state_dir=None,
                                  root=str(SOURCE))
        code = guide.run(args, answer, out)
        return code, out.getvalue(), asked

    def folders(self):
        base = self.home / CASE["id"]
        return sorted(p for p in base.iterdir()) if base.exists() else []

    def report(self):
        return json.loads((self.folders()[-1] / "report" / "report.json").read_text())

    def bundle(self):
        return json.loads((self.folders()[-1] / "bundle.json").read_text())

    def findings(self):
        db = sqlite3.connect(str(self.home / "findings" / "state.sqlite3"))
        try:
            return db.execute("SELECT key, occurrences FROM findings").fetchall()
        finally:
            db.close()


def good_answers(decision=("pass", "Clear and complete.", "Saved research", "Test operator"),
                 captured="yes", retest=()):
    initial, full = good_messages()
    answers = lines(initial)
    for text in full:
        answers += ["yes"] + lines(text)
    answers += ["no", "yes", "yes", "Read full assessment", captured, "", "", "", *retest]
    return answers + list(decision)


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.g = Guided(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_review_completes_without_authored_json(self):
        code, out, _ = self.g.run(good_answers())
        self.assertEqual(code, 0, out)
        folder = self.g.folders()[0]
        for name in ("bundle.json", "assessment.json", "report/report.md",
                     "evidence/response-1.txt"):
            self.assertTrue((folder / name).exists(), name)
            self.assertEqual(oct((folder / name).stat().st_mode)[-3:], "600")
        roles = [(i["role"], i["kind"]) for i in self.g.bundle()["items"]]
        self.assertEqual(roles[0], ("initial", "discord_response"))
        self.assertTrue(all(r == ("complete_view", "button_response") for r in roles[1:]))
        self.assertEqual(self.g.bundle()["provenance"], "operator_supplied")
        self.assertIn("Report: " + str(folder / "report" / "report.md"), out)
        self.assertIn("never certifies the employee", out)

    def test_combined_discord_copy_stays_separate_messages(self):
        initial, full = good_messages()
        paste = discord_copy(initial, *full)
        answers = lines(paste) + ["yes", "no", "yes", "yes", "Read full assessment", "yes",
                                  "", "", "", "pass", "Readable.", "Saved research", "op"]
        code, out, asked = self.g.run(answers)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.g.bundle()["items"]), 4)
        self.assertTrue(any("contains 4 Discord messages" in q for q in asked))
        # Declining the split asks for separate pastes rather than merging.
        g2 = Guided(Path(self.tmp.name) / "second")
        code, out, _ = g2.run(lines(paste) + ["no"] + good_answers())
        self.assertIn("paste each message separately", out)
        self.assertEqual(len(g2.bundle()["items"]), 4)

    def test_attachment_and_continuation_choices(self):
        pages = paged_messages()
        initial, full = good_messages()
        answers = (lines(initial) + ["yes"] + lines(full[0]) + ["yes"] + lines(pages[1])
                   + ["no", "yes", "no", "3", "2", COMMAND + " page:2", "yes", "", "", "",
                      "skip"])
        code, out, _ = self.g.run(answers)
        items = self.g.bundle()["items"]
        self.assertEqual([(i["role"], i["kind"]) for i in items],
                         [("initial", "discord_response"), ("complete_view", "attachment"),
                          ("continuation", "discord_response")])
        verdicts = {c["id"]: c["verdict"] for c in self.g.report()["criteria"]}
        self.assertEqual(verdicts["complete_without_page_commands"], "failed")
        self.assertEqual(code, 1)

    def test_partial_capture_and_skipped_assessment_stay_incomplete(self):
        initial, _ = good_messages()
        code, out, _ = self.g.run(lines(initial) + ["no", "yes", "no", "", "", "", "skip"])
        self.assertEqual(code, 2, out)
        report = self.g.report()
        self.assertEqual(report["verdict"], "incomplete")
        self.assertFalse(report["complete_reading_captured"])
        self.assertFalse((self.g.folders()[0] / "assessment.json").read_text().count('"passed"'))

    def test_human_failure_stands_and_attribution_is_verbatim(self):
        reason = "Useful, but redundant and hard to read."
        code, out, asked = self.g.run(good_answers(
            ("fail", reason, "not in the messages", "Saved research", "Reviewer A")))
        self.assertEqual(code, 1, out)
        self.assertTrue(any("not in the messages exactly" in line for line in out.split("\n")))
        saved = json.loads((self.g.folders()[0] / "assessment.json").read_text())
        check = saved["checks"]["operator_reading_assessment"]
        self.assertEqual((saved["assessor"], check["verdict"], check["note"], check["excerpt"]),
                         ("Reviewer A", "failed", reason, "Saved research"))
        key = self.g.findings()[0][0]
        self.assertIn("Private draft for " + key, out)
        self.assertTrue((self.g.home / "findings" / "drafts" / (key + ".md")).exists())

    def test_duplicates_deduplicate_and_retests_link(self):
        failing = ("fail", "Hard to read.", "", "Reviewer A")
        self.g.run(good_answers(failing))
        self.g.run(good_answers(failing, retest=("no",)))  # finding now exists: not a retest
        self.assertEqual(len(self.g.findings()), 1)
        self.assertEqual(self.g.findings()[0][1], 1)
        self.assertEqual(len(list((self.g.home / "findings" / "drafts").iterdir())), 1)
        self.assertEqual(len(self.g.folders()), 2)  # each review keeps its own folder
        initial, full = good_messages(" after the fix")
        answers = lines(initial)
        for text in full:
            answers += ["yes"] + lines(text)
        answers += ["no", "yes", "yes", "Read full assessment", "yes", "", "", "",
                    "yes", "1", "pass", "Now readable.", "Saved research", "Reviewer A"]
        code, out, _ = self.g.run(answers)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.g.report()["retest"]["outcome"], "resolved")
        self.assertEqual(self.g.findings()[0][1], 1)
        draft = (self.g.home / "findings" / "drafts" / (self.g.findings()[0][0] + ".md"))
        self.assertIn("**Finding status:** resolved.", draft.read_text())

    def test_invalid_answers_reprompt_and_cancellation_saves_nothing(self):
        initial, _ = good_messages()
        code, out, _ = self.g.run(["", "."] + lines(initial) + ["maybe", "no", "cancel"])
        self.assertEqual(code, 130)
        self.assertIn("Nothing was pasted", out)
        self.assertIn("Please answer one of", out)
        self.assertIn("Cancelled. Nothing was saved.", out)
        self.assertEqual(self.g.folders(), [])
        code, _, _ = self.g.run(lines(initial)[:2])  # input ends mid-paste
        self.assertEqual(code, 130)
        self.assertEqual(self.g.folders(), [])

    def test_unknown_case_and_unreadable_import_are_reported(self):
        code, out, _ = self.g.run([], case="no-such-case")
        self.assertEqual(code, 5)
        initial, _ = good_messages()
        code, out, _ = self.g.run(["@" + str(Path(self.tmp.name) / "missing.txt")]
                                  + lines(initial) + ["no", "yes", "no", "", "", "", "skip"])
        self.assertIn("could not be read", out)
        self.assertEqual(code, 2)

    def test_imported_file_and_sensitive_text_stay_out_of_saved_artifacts(self):
        initial, full = good_messages("\nAuthorization: " + CANARY)
        source = Path(self.tmp.name) / "first.txt"
        source.write_text(initial)
        answers = ["@" + str(source), "yes"] + lines(full[0]) + ["yes"] + lines(full[1]) \
            + ["yes"] + lines(full[2]) + ["no", "yes", "yes", "Read full assessment", "yes",
                                          "", "", "", "skip"]
        code, out, _ = self.g.run(answers)
        self.assertEqual(code, 2, out)
        secret = CANARY.split(" ", 1)[1]
        self.assertNotIn(secret, out)
        for path in self.g.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), path.name)
        self.assertIn(FIELDS["rationale"][:40], (self.g.folders()[0] / "evidence"
                                                 / "response-2.txt").read_text())


if __name__ == "__main__":
    unittest.main()


class GuideOutputTests(unittest.TestCase):
    def test_printed_paths_are_real_and_folders_private(self):
        # Store names like this one look key-shaped to the sanitizer; printed paths must not.
        with tempfile.TemporaryDirectory() as tmp:
            g = Guided(tmp)
            store = Path(tmp) / "seren-harness-findings"
            feed = iter(good_answers(("fail", "Hard to read.", "", "Reviewer A")))
            out = io.StringIO()
            args = argparse.Namespace(case=CASE["id"], home=str(g.home), state_dir=str(store),
                                      root=str(SOURCE))
            self.assertEqual(guide.run(args, lambda _: next(feed), out), 1)
            drafts = [line.split(": ", 1)[1] for line in out.getvalue().split("\n")
                      if line.startswith("Private draft for ")]
            self.assertEqual(len(drafts), 1)
            self.assertTrue(Path(drafts[0]).is_file(), drafts[0])
            for folder in (g.home, g.home / CASE["id"], g.folders()[0]):
                self.assertEqual(oct(folder.stat().st_mode)[-3:], "700", folder)
