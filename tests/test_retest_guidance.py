"""Draft retest guidance follows the failed journey and the recorded recovery state.

Small synthetic stores; rendering a draft reads local state only.
"""
import re
import shlex
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE  # noqa: F401  (imports the tested tree)
from seren_harness import cli
from seren_harness.models import DEFECT, Finding
from seren_harness.report import write_draft
from seren_harness.state import AMBIGUOUS, SUCCEEDED, Store

CANARY = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
NO_SEARCH = {"status": "unavailable", "reason": "test"}
COMMAND = re.compile(r"`(python -m seren_harness [^`]+)`")
PLACEHOLDERS = {"<config>": "harness.json", "<applied|not-applied>": "not-applied",
                "<case-file>": "case.json", "<bundle.json>": "bundle.json",
                "<finding-store>": "state", "<new-folder>": "out"}


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def finding(self, journey, step="verify_reply", status="complete", employee="emp",
                details=None):
        run = self.store.create_run(journey, employee, "simulation", "digest", "test")
        self.store.update_run(run, status=status)
        key = "F-" + journey[:6] + step[:6]
        self.store.record_finding(key, run, journey, step, Finding(
            "a." + step, DEFECT, "Synthetic " + journey + " failure", "expected",
            "observed " + CANARY), details=details)
        return key, run

    def guidance(self, key):
        path, _, _ = write_draft(self.store, key, NO_SEARCH)
        text = path.read_text()
        self.assertNotIn(CANARY.split(" ", 1)[1], text)
        section = text.split("## Retest\n\n", 1)[1].split("\n\n", 1)[0]
        for command in COMMAND.findall(section):
            for placeholder, value in PLACEHOLDERS.items():
                command = command.replace(placeholder, value)
            self.assertNotIn("<", command.replace('"<what was checked, and where>"', "x"))
            cli.parser().parse_args(shlex.split(command)[3:])  # SystemExit if invalid
        return section

    def test_each_journey_gets_its_own_command_when_state_is_clean(self):
        for journey, command in (("operational", "run"), ("provisioning", "provision"),
                                 ("control", "control"), ("preflight", "preflight")):
            key, _ = self.finding(journey, employee=journey)
            text = self.guidance(key)
            self.assertIn("`python -m seren_harness " + command + " --config <config> --retest "
                          + key + "`", text)
            self.assertIn("step `verify_reply` and every later step pass", text)
        self.assertNotIn("seren_harness run ", self.guidance(self.finding("provisioning",
                                                                          "x", "complete",
                                                                          "p2")[0]))

    def test_unfinished_provisioning_is_continued_not_restarted(self):
        key, run = self.finding("provisioning", "request_approval", status="awaiting_human")
        text = self.guidance(key)
        self.assertIn("resume " + run + " --config <config>` after the owner approves", text)
        self.assertNotIn("provision --config", text)

    def test_unknown_outcome_comes_first_and_never_asks_for_a_guess(self):
        key, run = self.finding("operational", status="unresolved")
        self.store.add_cleanup(run, "emp", "stop_runtime", "target")
        action = self.store.record_intent(run, "start_runtime", "managed_start", "target",
                                          {"note": CANARY})
        self.store.resolve_action(action, AMBIGUOUS, {"note": CANARY})
        text = self.guidance(key)
        self.assertIn("settle action " + str(action) + " (managed_start)", text)
        self.assertIn("`python -m seren_harness resume " + run + " --config <config>`", text)
        self.assertIn("reconcile " + run + " --action " + str(action), text)
        self.assertIn("verified independently", text)
        self.assertIn("Never guess", text)
        self.assertNotIn("seren_harness run --config", text)

    def test_unknown_outcome_in_another_run_of_the_employee_blocks_a_retest(self):
        key, _ = self.finding("operational")
        other = self.store.create_run("provisioning", "emp", "simulation", "digest", "test")
        self.store.update_run(other, status="unresolved")
        self.store.record_intent(other, "apply", "managed_passwords_apply", "target")
        text = self.guidance(key)
        self.assertIn("of run `" + other + "`", text)
        self.assertNotIn("seren_harness run --config", text)

    def test_unconfirmed_shutdown_blocks_a_fresh_run(self):
        key, run = self.finding("operational")
        cleanup = self.store.add_cleanup(run, "emp", "stop_runtime", "target")
        self.store.update_cleanup(cleanup, "failed", {"reason": "not confirmed"})
        text = self.guidance(key)
        self.assertIn("Shutdown from run `" + run + "` is unconfirmed", text)
        self.assertIn("Do not start a fresh run", text)
        self.assertNotIn("seren_harness run --config", text)
        self.assertNotIn("resume", text)  # a complete run cannot be resumed

    def test_interrupted_run_with_open_cleanup_is_resumed(self):
        key, run = self.finding("operational", status="interrupted")
        self.store.add_cleanup(run, "emp", "stop_runtime", "target")
        text = self.guidance(key)
        self.assertIn("Resume that run before any retest", text)
        self.assertIn("resume " + run + " --config <config>", text)

    def test_confirmed_cleanup_and_settled_actions_allow_a_new_run(self):
        key, run = self.finding("operational")
        cleanup = self.store.add_cleanup(run, "emp", "stop_runtime", "target")
        self.store.update_cleanup(cleanup, "done")
        action = self.store.record_intent(run, "start_runtime", "managed_start", "target")
        self.store.resolve_action(action, SUCCEEDED)
        self.assertIn("`python -m seren_harness run --config <config> --retest " + key + "`",
                      self.guidance(key))

    def test_functional_and_unknown_journeys(self):
        key, _ = self.finding("scott.example-case", "initial_summary",
                              employee="functional-review")
        text = self.guidance(key)
        self.assertIn("review-workflow --case <case-file>", text)
        self.assertIn("--retest " + key, text)
        key, _ = self.finding("mystery", employee="other")
        text = self.guidance(key)
        self.assertIn("Limitation: no supported retest command", text)
        self.assertNotIn("`python -m seren_harness", text)

    def test_case_specific_instructions_are_kept_verbatim(self):
        explicit = "After the fix, run `/bd research entity:X` and copy every message."
        key, run = self.finding("operational", status="unresolved",
                                details={"retest": explicit})
        self.store.record_intent(run, "start_runtime", "managed_start", "target")
        self.assertEqual(self.guidance(key), explicit)

    def test_rendering_guidance_performs_no_external_action(self):
        key, run = self.finding("operational", status="unresolved")
        self.store.record_intent(run, "start_runtime", "managed_start", "target")
        refuse = mock.Mock(side_effect=AssertionError("external action attempted"))
        with mock.patch.object(subprocess, "run", refuse), \
                mock.patch.object(subprocess, "Popen", refuse), \
                mock.patch.object(socket, "create_connection", refuse):
            self.assertIn("resume", self.guidance(key))
        refuse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
