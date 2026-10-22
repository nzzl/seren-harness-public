"""Confirmed finding-to-issue links: recorded through the CLI, kept locally, shown in drafts.

Synthetic stores and clearly fake confirmations. External processes and sockets are refused
for every test, so linking and listing are shown to need no GitHub or live session.
"""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE  # noqa: F401  (imports the tested tree)
from seren_harness import cli
from seren_harness.models import DEFECT, Finding
from seren_harness.report import finding_status, write_draft
from seren_harness.state import Store

CANARY = "ghp_" + "A" * 36
ISSUE = "https://github.com/example-org/example-repo/issues/7"
WHO = "synthetic test confirmer"
SUGGESTIONS = {"status": "ok", "terms": "x", "issues": [
    {"number": 99, "title": "Suggested only", "state": "OPEN",
     "url": "https://github.com/example-org/example-repo/issues/99"}]}
NO_SEARCH = {"status": "unavailable", "reason": "test"}


def _refuse(*_args, **_kwargs):
    raise AssertionError("external call attempted")


class IssueLinkTests(unittest.TestCase):
    def setUp(self):
        for target in ("subprocess.run", "subprocess.Popen", "socket.socket",
                       "socket.create_connection"):
            patcher = mock.patch(target, side_effect=_refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        store = Store(self.dir)
        self.key = self._finding(store, "F-functional01", "functional-review")
        self.lifecycle = self._finding(store, "F-lifecycle001", "emp")
        store.record_retest(self.key, "retest-run", "inconclusive", {"eligible": False})
        store.close()

    @staticmethod
    def _finding(store, key, employee):
        run = store.create_run("case-x", employee, "local", "digest", "test")
        store.record_finding(key, run, "case-x", "criterion", Finding(
            "a.criterion", DEFECT, "Synthetic finding " + key, "expected", "observed"))
        return key

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def link(self, key=None, url=ISSUE, who=WHO, state_dir=None):
        return self.cli("link-issue", key or self.key, url, "--confirmed-by", who,
                        "--state-dir", state_dir or self.dir)

    def links(self):
        store = Store(self.dir)
        try:
            return store.issue_links()
        finally:
            store.close()

    def test_cli_link_persists_after_reopening_the_store(self):
        code, out, _ = self.link()
        self.assertEqual(code, 0)
        self.assertIn("example-org/example-repo#7", out)
        self.assertIn("nothing was checked against or sent to GitHub", out)
        [link] = self.links()  # a fresh connection
        self.assertEqual((link["finding_key"], link["url"], link["confirmed_by"]),
                         (self.key, ISSUE, WHO))
        code, out, _ = self.cli("issue-links", "--state-dir", self.dir)
        self.assertEqual(code, 0)
        self.assertIn(self.key + "  example-org/example-repo#7  " + ISSUE, out)
        self.assertIn("confirmed by " + WHO + "; recorded " + link["recorded_at"], out)
        self.assertEqual(self.cli("issue-links", "--state-dir", self.dir, "--finding",
                                  self.lifecycle)[1], "No confirmed issue links.\n")

    def test_repeating_a_confirmation_is_idempotent_and_keeps_its_attribution(self):
        self.link()
        [original] = self.links()
        code, out, _ = self.link(url=ISSUE.replace("example-org", "Example-Org"))
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        code, _, err = self.link(who="someone else")
        self.assertEqual(code, cli.REFUSED)
        self.assertIn("confirmed by " + WHO, err)
        self.assertEqual(self.links(), [original])

    def test_confirmed_links_are_separate_from_suggestions_and_survive_regeneration(self):
        store = Store(self.dir)
        try:
            path, _, _ = write_draft(store, self.key, SUGGESTIONS)
            without = path.read_text()
            self.assertNotIn("Confirmed issue links", without)
            self.assertEqual(store.issue_links(), [])  # suggestions never become links
        finally:
            store.close()
        code, out, _ = self.link()
        self.assertEqual(code, 0)
        self.assertIn("Private draft refreshed: " + str(path), out)
        self.assertIn("## Confirmed issue links", path.read_text())
        store = Store(self.dir)
        try:
            for _ in range(2):
                write_draft(store, self.key, SUGGESTIONS)
            text = path.read_text()
        finally:
            store.close()
        confirmed = text.split("## Confirmed issue links\n", 1)[1].split("## Possibly", 1)[0]
        suggested = text.split("## Possibly related issues\n", 1)[1]
        self.assertIn("example-org/example-repo#7: " + ISSUE + " (confirmed by " + WHO, confirmed)
        self.assertIn("has not checked the issue's existence, contents or status", confirmed)
        self.assertNotIn("#99", confirmed)
        self.assertIn("#99", suggested)
        self.assertNotIn("#7", suggested)
        code, out, _ = self.link(key=self.lifecycle)
        self.assertEqual(code, 0)
        self.assertIn("draft-issue", out)  # lifecycle drafts keep their configured rendering

    def test_unknown_findings_and_invalid_urls_are_rejected_without_records_or_leaks(self):
        bad = [
            "https://user:" + CANARY + "@github.com/example-org/example-repo/issues/7",
            ISSUE + "?token=" + CANARY, ISSUE + "#" + CANARY, ISSUE + "/" + CANARY,
            ISSUE.replace("https", "http"), ISSUE.replace("github.com", "github.com:443"),
            ISSUE.replace("github.com", "evil.example") + CANARY,
            ISSUE.replace("issues", "pull"), ISSUE[:-1] + "0", " " + ISSUE, ISSUE + "\n",
            CANARY]
        for url in bad:
            code, out, err = self.link(url=url)
            self.assertEqual(code, cli.USAGE, url)
            self.assertIn("issue URL rejected", err)
            self.assertNotIn(CANARY, out + err)
        code, _, err = self.link(key="F-unknown")
        self.assertEqual((code, "unknown finding" in err), (cli.USAGE, True))
        self.assertEqual(self.link(who="  ")[0], cli.USAGE)
        self.assertEqual(self.links(), [])
        missing = str(Path(self.dir) / "absent")
        for argv in (("link-issue", self.key, ISSUE, "--confirmed-by", WHO), ("issue-links",)):
            code, _, err = self.cli(*argv, "--state-dir", missing)
            self.assertEqual((code, "no finding store" in err), (cli.USAGE, True))
        self.assertFalse(Path(missing).exists())

    def test_linking_changes_no_finding_status_occurrence_or_retest(self):
        def snapshot():
            store = Store(self.dir)
            try:
                retests = store.retests(self.key)
                return (store.finding(self.key), store.finding_runs(self.key), retests,
                        finding_status(retests))
            finally:
                store.close()

        before = snapshot()
        self.assertEqual(self.link()[0], 0)
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
