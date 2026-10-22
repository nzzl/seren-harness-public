"""Explicitly linked lifecycle retests, through the real CLI against the simulated Seren CLI.

SIMULATION throughout: the original findings here are simulated, so only simulated runs may
retest them (a simulated run never resolves a live finding).
"""
import json
import unittest

from test_seren_journeys import CANARIES, SimulatedJourney
from seren_harness import cli
from seren_harness.models import Finding
from seren_harness.report import finding_status


class LifecycleRetestTests(SimulatedJourney):
    def original_failure(self):
        """A simulated #232-shaped finding: start accepted, connector binding failed."""
        self.provision()
        self.set_scenario(readiness="binding_failure")
        code, output = self.harness("run")
        self.assertEqual(code, 1, output)
        [finding] = self.store().findings()
        self.assertEqual(finding["step"], "runtime_ready")
        return finding["key"], self.latest_run()["id"]

    def retest(self, key):
        code, output = self.harness("run", "--retest", key)
        return code, output, self.latest_run()["id"]

    def retests(self, key):
        return self.store().retests(key)

    def test_compatible_passing_retest_resolves_and_keeps_history_and_links(self):
        key, original = self.original_failure()
        store = self.store()
        store.record_issue_link(key, "https://github.com/example-org/example-repo/issues/232",
                                "synthetic test confirmer")
        self.set_scenario(readiness="ready")
        code, output, run = self.retest(key)
        self.assertEqual(code, 0, output)
        self.assertIn("Finding " + key + " retest: resolved (finding status resolved)", output)
        [retest] = self.retests(key)
        self.assertEqual((retest["run_id"], retest["outcome"]), (run, "resolved"))
        self.assertEqual(retest["detail"]["recovery_condition"],
                         "step `runtime_ready` and every later step pass")
        finding = self.store().finding(key)
        self.assertEqual((finding["occurrences"], finding["latest_run"]), (1, original))
        self.assertEqual(len(self.store().issue_links(key)), 1)
        report = self.report(run)
        self.assertEqual((report["outcome"], report["retest"]["outcome"],
                          report["retest"]["finding_status"]), ("passed", "resolved", "resolved"))
        code, output = self.harness("draft-issue", run)
        self.assertEqual(code, 0, output)
        draft = (self.root / "state" / "drafts" / (key + ".md")).read_text()
        self.assertIn("**Finding status:** resolved.", draft)
        self.assertIn("## Confirmed issue links", draft)
        self.assertIn("1 run(s)", draft)  # The original occurrence is unchanged.

    def test_reproduced_failure_is_still_failing_not_another_occurrence(self):
        key, _ = self.original_failure()
        code, output, run = self.retest(key)  # Scenario still binding_failure.
        self.assertEqual(code, 1, output)
        self.assertEqual([r["outcome"] for r in self.retests(key)], ["still_failing"])
        self.assertEqual(self.store().finding(key)["occurrences"], 1)
        self.assertEqual(len(self.store().findings()), 1)
        report = self.report(run)
        self.assertEqual(report["retest"]["finding_status"], "open")
        self.assertTrue(report["retest"]["cleanup_confirmed"])
        self.assertEqual(report["cleanup"][0]["status"], "done")

    def test_unrelated_or_missing_evidence_is_inconclusive(self):
        key, _ = self.original_failure()
        # Readiness never arrives: a different failure of the same step.
        self.set_scenario(readiness="never")
        self.retest(key)
        # Ready, but no reply: the step under retest passed, a later one failed.
        self.set_scenario(readiness="ready", reply="none")
        self.retest(key)
        outcomes = [r["outcome"] for r in self.retests(key)]
        self.assertEqual(outcomes, ["inconclusive", "inconclusive"])
        reasons = [r["detail"]["reasons"][0] for r in self.retests(key)]
        self.assertIn("with a different failure", reasons[0])
        self.assertIn("passed, but later step `verify_reply` was failed", reasons[1])
        self.assertEqual(finding_status(self.retests(key)), "open")
        # The unrelated failures are their own findings, shown as diagnostics.
        self.assertEqual(len(self.store().findings()), 3)
        self.assertTrue(all(r["detail"]["diagnostic"] for r in self.retests(key)))

    def test_incompatible_or_unknown_finding_is_rejected_before_anything_runs(self):
        key, _ = self.original_failure()
        runs, log = len(self.store().runs()), len(self.sim_state()["log"])
        for args in (("run", "--retest", "F-000000000000"),
                     ("control", "--retest", key)):  # wrong journey
            code, output = self.harness(*args)
            self.assertEqual(code, cli.USAGE, output)
            self.assertIn("cannot link this run as a retest", output)
        # Another test employee cannot retest it either.
        config = json.loads(self.config.read_text())
        config["employee"]["key"] = "sim-employee-2"
        self.config.write_text(json.dumps(config))
        code, output = self.harness("run", "--retest", key)
        self.assertEqual(code, cli.USAGE, output)
        self.assertIn("another test employee", output)
        self.assertEqual((len(self.store().runs()), len(self.sim_state()["log"])), (runs, log))

    def test_simulated_run_cannot_retest_a_live_finding(self):
        store = self.store()
        live = store.create_run("operational", "sim-employee-1", "live", "digest", "test")
        store.record_finding("F-live00000001", live, "operational", "runtime_ready", Finding(
            "runtime.ready", "defect", "Synthetic live finding", "ready", "failed"))
        runs = len(store.runs())
        code, output = self.harness("run", "--retest", "F-live00000001")
        self.assertEqual(code, cli.USAGE, output)
        self.assertIn("recorded on live evidence; this run is simulation", output)
        self.assertEqual(len(self.store().runs()), runs)
        self.assertEqual(self.store().retests("F-live00000001"), [])

    def test_reprocessing_a_saved_run_is_idempotent(self):
        key, _ = self.original_failure()
        self.set_scenario(readiness="ready")
        _, _, run = self.retest(key)
        before = self.retests(key)
        for _ in range(2):
            code, output = self.harness("report", run)
            self.assertEqual(code, 0, output)
        self.assertEqual(self.retests(key), before)
        self.assertFalse(any(c.encode() in self.all_persisted_bytes() for c in CANARIES))


if __name__ == "__main__":
    unittest.main()
