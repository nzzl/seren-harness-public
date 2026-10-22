"""Automatic collection to report: no operator files, JSON or questions.

The adapter here is a fixture standing in for a target interface; it verifies the
harness path, not live collection.
"""
import argparse
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from seren_support import SOURCE
from test_guide import discord_copy
from test_workflow_review import CANARY, CASE, good_messages, paged_messages
from seren_harness import collect

ADAPTER = r'''
import json, sys
from pathlib import Path
mode, data, counter = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
calls = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(calls))
if mode == "fail" or (mode == "flaky" and calls == 1):
    sys.stderr.write("secret detail " + data.read_text()[:40])
    sys.exit(3)
if mode == "sleep":
    import time; time.sleep(5)
if mode == "garbage":
    print("not json"); sys.exit(0)
print(data.read_text())
'''


class Collect:
    def __init__(self, root, messages, complete=True, mode="ok", provenance="local"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.home = self.root / "reviews"
        self.data = self.root / "adapter-output.json"
        self.counter = self.root / "calls"
        (self.root / "adapter.py").write_text(ADAPTER)
        self.write(messages, complete)
        self.target = self.root / "target.json"
        self.target.write_text(json.dumps({"employee": "Fixture", "root": str(self.root),
            "collectors": {CASE["id"]: {
                "type": "command", "provenance": provenance, "timeout_seconds": 2,
                "environment": "fixture adapter (test only)",
                "argv": [sys.executable, "{root}/adapter.py", mode, str(self.data),
                         str(self.counter)]}},
            "reading_paths": {CASE["id"]: {"follow_up": "button: Read full assessment"}}}))

    def write(self, messages, complete=True):
        self.data.write_text(json.dumps({"observed": "2026-10-10T00:00:00Z",
                                         "complete": complete, "messages": messages}))

    def run(self, *extra, **options):
        args = argparse.Namespace(case=CASE["id"], target=str(self.target), target_root=None,
                                  from_copy=None, retest=None, home=str(self.home),
                                  state_dir=None, root=str(SOURCE))
        for key, value in options.items():
            setattr(args, key, value)
        out = io.StringIO()
        code = collect.run(args, out)
        return code, out.getvalue()

    def calls(self):
        return int(self.counter.read_text()) if self.counter.exists() else 0

    def folders(self):
        base = self.home / CASE["id"]
        return sorted(base.iterdir()) if base.exists() else []

    def report(self):
        return json.loads((self.folders()[-1] / "report" / "report.json").read_text())

    def findings(self):
        db = sqlite3.connect(str(self.home / "findings" / "state.sqlite3"))
        try:
            return db.execute("SELECT key, occurrences FROM findings").fetchall()
        finally:
            db.close()


def reading(extra=""):
    initial, full = good_messages(extra)
    return [{"role": "initial", "kind": "discord_response", "invocation": CASE["command"],
             "text": initial}] + [{"role": "complete_view", "kind": "button_response",
                                   "invocation": "button: Read full assessment", "text": t}
                                  for t in full]


def paged():
    pages = paged_messages()
    return [{"role": "initial", "kind": "discord_response",
             "invocation": CASE["command"] + " page:1", "text": pages[0]}] + [
        {"role": "continuation", "kind": "discord_response",
         "invocation": CASE["command"] + " page:" + str(n), "text": p}
        for n, p in enumerate(pages[1:], 2)]


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_collection_to_report_needs_no_files_json_or_questions(self):
        c = Collect(self.tmp.name, reading())
        code, out = c.run()
        self.assertEqual(code, collect.INCOMPLETE, out)
        self.assertIn("Harness run: completed (4 message(s), local evidence)", out)
        self.assertIn("cannot produce a scoped pass", out)
        self.assertIn("Awaiting a person's judgment (left unverified): "
                      "operator_reading_assessment", out)
        report = c.report()
        self.assertEqual(report["provenance"], "local")
        self.assertTrue(report["complete_reading_captured"])
        verdicts = {x["id"]: x["verdict"] for x in report["criteria"]}
        self.assertEqual(verdicts["operator_reading_assessment"], "unverified")
        self.assertTrue(all(v == "passed" for k, v in verdicts.items()
                            if k not in ("operator_reading_assessment", "source_support")))
        bundle = json.loads((c.folders()[0] / "bundle.json").read_text())
        self.assertIsNone(bundle["revision"])  # fixture root is not a git checkout: unknown
        self.assertEqual([i["role"] for i in bundle["items"]],
                         ["initial"] + ["complete_view"] * 3)

    def test_failures_and_partial_capture_never_pass(self):
        c = Collect(self.tmp.name, paged(), complete=False)
        code, out = c.run()
        self.assertEqual(code, collect.FAILED_VERDICT, out)
        self.assertIn("Completeness of the reading was not established", out)
        self.assertFalse(c.report()["complete_reading_captured"])
        self.assertEqual(len(c.findings()), 1)
        self.assertIn("Private draft for", out)

    def test_adapter_cannot_claim_live_observation(self):
        c = Collect(self.tmp.name, reading(), provenance="operator_supplied")
        code, out = c.run()
        self.assertEqual(code, collect.COLLECTION_FAILED)
        self.assertEqual(c.folders(), [])

    def test_transient_failure_retries_once_then_stops(self):
        c = Collect(self.tmp.name, reading(), mode="flaky")
        code, out = c.run()
        self.assertEqual(code, collect.INCOMPLETE, out)
        self.assertEqual(c.calls(), 2)
        self.assertIn("after a retry", out)
        failing = Collect(Path(self.tmp.name) / "f", reading(CANARY), mode="fail")
        code, out = failing.run()
        self.assertEqual(code, collect.COLLECTION_FAILED)
        self.assertEqual(failing.calls(), collect.ATTEMPTS)
        self.assertIn("No review was recorded", out)
        self.assertNotIn("secret detail", out)
        self.assertEqual(failing.folders(), [])
        once = Collect(Path(self.tmp.name) / "n", reading(), mode="fail")
        code, out = once.run(no_retry=True)
        self.assertEqual((code, once.calls()), (collect.COLLECTION_FAILED, 1), out)

    def test_timeouts_and_malformed_output_are_bounded(self):
        slow = Collect(self.tmp.name, reading(), mode="sleep")
        code, out = slow.run()
        self.assertEqual(code, collect.COLLECTION_FAILED)
        self.assertIn("timed out", out)
        self.assertEqual(slow.calls(), collect.ATTEMPTS)
        garbage = Collect(Path(self.tmp.name) / "g", reading(), mode="garbage")
        code, out = garbage.run()
        self.assertEqual(code, collect.COLLECTION_FAILED)
        self.assertEqual(garbage.calls(), 1)  # malformed output is not retried blindly
        bad = Collect(Path(self.tmp.name) / "b", [{"role": "complete_view",
                                                  "kind": "button_response", "text": "x"}])
        self.assertEqual(bad.run()[0], collect.COLLECTION_FAILED)

    def test_repeated_collection_deduplicates_and_links_the_open_finding(self):
        c = Collect(self.tmp.name, paged(), complete=False)
        c.run()
        c.run()
        self.assertEqual(c.findings()[0][1], 1)
        self.assertEqual(len(list((c.home / "findings" / "drafts").iterdir())), 1)
        c.write(reading(" after"))
        code, out = c.run()
        key = c.findings()[0][0]
        self.assertIn("Linked as a retest of " + key, out)
        self.assertEqual(c.report()["retest"]["outcome"], "inconclusive")  # local evidence
        self.assertEqual(c.findings()[0][1], 1)

    def test_sensitive_output_stays_out_of_saved_artifacts(self):
        c = Collect(self.tmp.name, reading("\nAuthorization: " + CANARY))
        code, out = c.run()
        secret = CANARY.split(" ", 1)[1]
        self.assertNotIn(secret, out)
        for path in c.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), path.name)

    def test_retained_copy_imports_without_questions(self):
        initial, full = good_messages()
        copy = Path(self.tmp.name) / "copy.txt"
        copy.write_text(discord_copy(initial, *full))
        c = Collect(self.tmp.name, reading())
        code, out = c.run(from_copy=str(copy))
        self.assertEqual(c.calls(), 0)  # the collector is not run
        report = c.report()
        self.assertEqual(report["provenance"], "operator_supplied")
        self.assertEqual(len(report["items"]), 4)
        self.assertFalse(report["complete_reading_captured"])  # never assumed
        self.assertEqual(code, collect.INCOMPLETE, out)


if __name__ == "__main__":
    unittest.main()


class NoQuestionTests(unittest.TestCase):
    def test_configured_collection_never_prompts(self):
        import builtins
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                builtins, "input", side_effect=AssertionError("collect must not prompt")):
            code, out = Collect(tmp, reading()).run()
        self.assertEqual(code, collect.INCOMPLETE, out)
