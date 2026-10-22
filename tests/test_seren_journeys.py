"""End-to-end behavior through the real CLI, state store and adapter, against the
simulated Seren CLI in simulation/fake_seren.py (labelled simulation throughout)."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from seren_support import SOURCE, prerun, standin
from seren_harness import cli
from seren_harness.sanitize import Sanitizer
from seren_harness.state import Store

DIAGNOSTICS_ENV = "SEREN_HARNESS_TEST_DIAGNOSTICS"

sys.path.insert(0, str(SOURCE / "simulation"))
import fake_seren  # noqa: E402

CANARIES = (fake_seren.SECRET_CANARY, "c2ltdWxhdGVkY2FwYWJpbGl0eXRva2Vu",
            "Zm9vYmFyYmF6cXV4cXV1eGNhcGFiaWxpdHk")
SYNTHETIC_EXPECTATION = {"template": "ACK {marker}",
                         "source": "SYNTHETIC: simulator expectation for tests"}


class SimulatedJourney(unittest.TestCase):
    scenario = {}

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="seren-journey-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.exe, self.state, self.channel, self.deployment = fake_seren.install(
            self.root, self.scenario)
        self.addCleanup(self.stop_simulated_employee)
        self.config = self.root / "config.json"
        self.write_config()
        self.calls_made = []
        if os.environ.get(DIAGNOSTICS_ENV):  # Runs before the directory is removed.
            self.addCleanup(self.retain_diagnostics, Path(os.environ[DIAGNOSTICS_ENV]))

    def retain_diagnostics(self, directory):
        """Bounded, sanitized evidence for investigating intermittent failures.

        Written outside the repository only when SEREN_HARNESS_TEST_DIAGNOSTICS names a
        directory. Output passes through the harness sanitizer; no environment is saved.
        """
        def tail(path, limit=6000):
            return path.read_text(errors="replace")[-limit:] if path.exists() else None

        store = self.store() if (self.root / "state" / "state.sqlite3").exists() else None
        runs = []
        for run in store.runs() if store else []:
            runs.append({"id": run["id"], "journey": run["journey"], "status": run["status"],
                         "steps": [{k: s[k] for k in ("name", "outcome", "summary",
                                                      "finished_at")}
                                   for s in store.steps(run["id"])],
                         "actions": [{k: a[k] for k in ("kind", "status", "created_at",
                                                        "resolved_at")}
                                     for a in store.actions(run["id"])]})
        sim = fake_seren.read_state(self.state)
        bundle = {"test": self.id(), "harness_calls": self.calls_made, "runs": runs,
                  "sim_timeline": sim.get("timeline", [])[-200:],
                  "sim_deployment": {k: sim["deployment"][k] for k in (
                      "status", "desired_lifecycle_state", "control_generation")},
                  "employee_log": tail(self.root / "sim-employee.log"),
                  "channel": tail(self.channel)}
        directory.mkdir(parents=True, exist_ok=True)
        name = self.id().rsplit(".", 1)[-1] + "-" + str(os.getpid()) + "-" \
            + str(int(time.time() * 1000)) + ".json"
        (directory / name).write_text(json.dumps(bundle, indent=1, sort_keys=True))

    def write_config(self, **overrides):
        config = {
            "environment": "simulation",
            "seren": {"executable": str(self.exe), "command_timeout_seconds": 2,
                      "poll_interval_seconds": 0.05, "readiness_timeout_seconds": 2,
                      "shutdown_timeout_seconds": 2},
            "employee": {"key": "sim-employee-1", "deployment_id": self.deployment,
                         "names": ["Simulated Test Employee"]},
            "discord": {"sender": "simulation", "observer": "simulation",
                        "channel_id": "sim-channel", "employee_user_id": "sim-employee",
                        "reply_timeout_seconds": 1.5,
                        "simulation_channel_file": "sim-channel.jsonl",
                        "expected_reply": SYNTHETIC_EXPECTATION},
            "state_dir": "state"}
        config.update(overrides)
        self.config.write_text(json.dumps(config))

    def stop_simulated_employee(self):
        with fake_seren.locked_state(self.state) as state:
            state["deployment"]["desired_lifecycle_state"] = "stopped"
        fake_seren.wait_for_employees(self.state)  # Before the directory is removed.

    def harness(self, *args):
        output = io.StringIO()
        started = time.time()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main([*args, "--config", str(self.config)])
        self.calls_made.append({"args": list(args), "exit": code, "started": round(started, 3),
                                "seconds": round(time.time() - started, 3),
                                "output": Sanitizer().text(output.getvalue())[-3000:]})
        return code, output.getvalue()

    def sim(self, *args):
        subprocess.run([str(self.exe), *args], check=True)

    def set_scenario(self, **values):
        self.sim("sim-set", json.dumps(values))

    def sim_state(self):
        return fake_seren.read_state(self.state)

    def calls(self, command):
        return [entry for entry in self.sim_state()["log"] if entry[1:2] == [command]]

    def store(self):
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        return store

    def latest_run(self):
        return self.store().runs()[-1]

    def report(self, run_id=None):
        run_id = run_id or self.latest_run()["id"]
        path = self.root / "state" / "runs" / run_id / "report.json"
        return json.loads(path.read_text())

    def steps(self, run_id=None):
        return {s["name"]: s for s in self.report(run_id)["steps"]}

    def provision(self):
        code, output = self.harness("provision")
        self.assertEqual(code, 3, output)
        setup_id = next(iter(self.sim_state()["setups"]))
        self.sim("sim-approve", setup_id)
        code, output = self.harness("resume", self.latest_run()["id"])
        self.assertEqual(code, 0, output)

    def all_persisted_bytes(self):
        return b"".join(p.read_bytes() for p in (self.root / "state").rglob("*") if p.is_file())


class ReadinessTests(SimulatedJourney):
    def test_successful_simulated_journey(self):
        self.provision()
        code, output = self.harness("run")
        self.assertEqual(code, 0, output)
        report = self.report()
        self.assertEqual(report["outcome"], "passed")
        self.assertTrue(report["label"].startswith("SIMULATION"))
        self.assertEqual(self.steps()["verify_reply"]["evidence"]["matched_by"],
                         "reply_reference")
        self.assertEqual(report["steps"][-1]["name"], "verify_reply_content")
        self.assertEqual(report["steps"][-1]["outcome"], "passed")
        self.assertEqual([c["status"] for c in report["cleanup"]], ["done"])
        self.assertEqual(self.sim_state()["deployment"]["status"], "stopped")

    def test_accepted_start_and_healthy_flag_cannot_hide_failed_connector(self):
        self.provision()
        self.set_scenario(readiness="binding_failure")
        code, output = self.harness("run")
        self.assertEqual(code, 1, output)
        steps = self.steps()
        self.assertEqual(steps["start_runtime"]["outcome"], "passed")  # Start was accepted.
        ready = steps["runtime_ready"]
        self.assertEqual(ready["outcome"], "failed")
        self.assertEqual(ready["evidence"]["observation"]["health"], "ok")
        self.assertIn("channel_binding_failure", ready["summary"] + json.dumps(ready["evidence"]))
        self.assertEqual(steps["send_probe"]["outcome"], "not_run")
        self.assertEqual(steps["verify_reply"]["outcome"], "not_run")
        self.assertEqual(self.report()["cleanup"][0]["status"], "done")

    def test_ready_and_healthy_without_a_reply_fails(self):
        self.provision()
        self.set_scenario(reply="none")
        code, output = self.harness("run")
        self.assertEqual(code, 1, output)
        steps = self.steps()
        self.assertEqual(steps["runtime_ready"]["outcome"], "passed")
        self.assertEqual(steps["verify_reply"]["outcome"], "failed")


    def test_simulated_employee_sees_the_saved_start_even_when_saving_is_delayed(self):
        # Test infrastructure: a start command descheduled between deciding to start and
        # saving must not leave the simulated employee reading the old, stopped state.
        self.provision()
        self.set_scenario(start_save_delay_seconds=0.5)
        code, output = self.harness("run")
        self.assertEqual(code, 0, output)
        log = [json.loads(line) for line in
               (self.root / "sim-employee.log").read_text().splitlines()]
        self.assertEqual([e["desired"] for e in log if e["event"] == "first_read"], ["running"])

    def test_simulated_reply_in_the_probes_millisecond_keeps_a_distinct_id(self):
        # Test infrastructure: observed in a full-suite run, a sub-millisecond simulated
        # reply received the probe's own id and was skipped as the probe.
        self.provision()
        self.set_scenario(reply_clock="probe_millisecond")
        code, output = self.harness("run")
        self.assertEqual(code, 0, output)
        reply = self.steps()["verify_reply"]
        self.assertEqual(reply["evidence"]["matched_by"], "reply_reference")
        self.assertEqual(reply["evidence"]["latency_ms"], 1)

    def test_teardown_waits_for_simulated_employees_to_exit(self):
        # Test infrastructure: an employee writing its exit line while the test
        # directory was being removed made cleanup fail with "Directory not empty".
        self.sim("agent", "managed-start", self.deployment)
        self.stop_simulated_employee()
        log = [json.loads(line) for line in
               (self.root / "sim-employee.log").read_text().splitlines()]
        spawned = [e["child"] for e in log if e["event"] == "spawned"]
        self.assertEqual(len(spawned), 1)
        self.assertIn(spawned[0], [e["pid"] for e in log if e["event"] == "exit"])

    def test_controlled_condition_rejects_the_stopped_employee_read_only(self):
        self.provision()
        code, output = self.harness("control")
        self.assertEqual(code, 0, output)
        step = self.steps()["stopped_rejected"]
        self.assertEqual(step["outcome"], "passed")
        self.assertEqual(step["evidence"]["observation"]["ready"], "False")
        self.assertEqual(self.calls("managed-start") + self.calls("managed-stop"), [])
        self.assertTrue(self.report()["label"].startswith("SIMULATION"))


class OwnershipTests(SimulatedJourney):
    def test_runtime_already_running_is_neither_started_nor_stopped(self):
        self.provision()
        with fake_seren.locked_state(self.state) as state:
            state["deployment"].update(status="running", desired_lifecycle_state="running")
        code, output = self.harness("run")
        self.assertEqual(code, 2, output)
        self.assertEqual(self.steps()["initial_state"]["outcome"], "blocked")
        self.assertEqual(self.calls("managed-start"), [])
        self.assertEqual(self.calls("managed-stop"), [])
        self.assertEqual(self.report()["cleanup"], [])


class ApprovalTests(SimulatedJourney):
    def test_missing_approval_blocks_dependent_steps_without_starting(self):
        code, output = self.harness("run")  # Never provisioned.
        self.assertEqual(code, 2, output)
        steps = self.steps()
        self.assertEqual(steps["verify_fixture"]["outcome"], "blocked")
        for name in ("initial_state", "start_runtime", "runtime_ready", "send_probe",
                     "verify_reply"):
            self.assertEqual(steps[name]["outcome"], "not_run", name)
        self.assertEqual(self.calls("managed-start"), [])

    def test_revoked_approval_blocks_after_provisioning(self):
        self.provision()
        with fake_seren.locked_state(self.state) as state:
            fake_seren.set_condition(state["deployment"], "AwaitingApproval", "True")
        code, output = self.harness("run")
        self.assertEqual(code, 2, output)
        self.assertIn("approval", self.steps()["verify_fixture"]["summary"])
        self.assertEqual(self.calls("managed-start"), [])

    def test_rejected_setup_fails_with_a_finding_and_no_apply(self):
        self.set_scenario(setup="rejected")
        self.assertEqual(self.harness("provision")[0], 3)
        self.sim("sim-approve", next(iter(self.sim_state()["setups"])))
        code, output = self.harness("resume", self.latest_run()["id"])
        self.assertEqual(code, 1, output)
        self.assertEqual(self.steps()["owner_approval"]["outcome"], "failed")
        self.assertEqual(self.calls("managed-passwords-apply"), [])


class ObservationAccessTests(SimulatedJourney):
    def test_missing_reader_access_blocks_without_an_employee_defect(self):
        self.provision()
        os.environ.pop("SEREN_HARNESS_TEST_UNSET_READER", None)
        self.write_config(discord={
            "sender": "simulation", "observer": "discord_rest", "channel_id": "sim-channel",
            "employee_user_id": "sim-employee", "reply_timeout_seconds": 1.5,
            "simulation_channel_file": "sim-channel.jsonl",
            "reader_token_env": "SEREN_HARNESS_TEST_UNSET_READER"})
        code, output = self.harness("run")
        self.assertEqual(code, 2, output)
        report = self.report()
        steps = {s["name"]: s for s in report["steps"]}
        self.assertEqual(steps["send_probe"]["outcome"], "blocked")
        self.assertEqual(steps["verify_reply"]["outcome"], "not_run")
        self.assertNotIn("defect", [f["classification"] for f in report["findings"]])
        self.assertEqual(report["cleanup"][0]["status"], "done")
        self.assertFalse(report["completion"]["clean"])


class CorrelationTests(SimulatedJourney):
    def test_wrong_author_and_unrelated_replies_cannot_pass(self):
        self.provision()
        for mode, reason in (("wrong_author", "wrong_author"),
                             ("unrelated", "unrelated_to_probe")):
            # Without the marker: an unreferenced echo of the marker would be bound.
            self.set_scenario(reply=mode, reply_text="ACK")
            code, output = self.harness("run")
            self.assertEqual(code, 1, output)
            reply = self.steps()["verify_reply"]
            self.assertEqual(reply["outcome"], "failed", mode)
            self.assertIn(reason, reply["evidence"]["rejected_candidates"])


class MutationTests(SimulatedJourney):
    def test_unanswered_start_that_applied_is_reconciled_not_retried(self):
        self.provision()
        self.set_scenario(start="timeout_applied")
        code, output = self.harness("run")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.calls("managed-start")), 1)
        self.assertIn("read-back shows it applied", self.steps()["start_runtime"]["summary"])
        actions = self.store().actions(self.latest_run()["id"])
        self.assertEqual(actions[0]["status"], "reconciled_applied")

    def test_unanswered_start_that_did_not_apply_is_not_retried(self):
        self.provision()
        self.set_scenario(start="timeout_not_applied")
        code, output = self.harness("run")
        self.assertEqual(code, 1, output)
        self.assertEqual(len(self.calls("managed-start")), 1)
        self.assertEqual(self.steps()["start_runtime"]["outcome"], "failed")
        self.assertEqual(self.store().actions(self.latest_run()["id"])[0]["status"],
                         "reconciled_not_applied")
        # Nothing was started, so cleanup confirms the stopped state without a stop call.
        self.assertEqual(self.calls("managed-stop"), [])
        self.assertEqual(self.report()["cleanup"][0]["status"], "done")

    def test_rejected_start_is_classified_as_access_problem(self):
        self.provision()
        self.set_scenario(start="reject_403")
        code, output = self.harness("run")
        self.assertEqual(code, 2, output)
        self.assertEqual(self.steps()["start_runtime"]["evidence"]["cause"], "access_denied")
        self.assertEqual(self.store().actions(self.latest_run()["id"])[0]["status"], "rejected")

    def test_failed_exit_that_applied_is_reconciled_not_journaled_as_rejected(self):
        # The CLI treats lost answers and 5xx as ambiguous; so must the journal.
        self.provision()
        self.set_scenario(start="error_applied")
        code, output = self.harness("run")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.calls("managed-start")), 1)
        start = self.store().actions(self.latest_run()["id"])[0]
        self.assertEqual(start["status"], "reconciled_applied")
        self.assertIn("error sending request", start["detail"]["stderr"])

    def test_server_error_is_settled_by_state_not_assumed_rejected(self):
        self.provision()
        self.set_scenario(start="error_503_not_applied")
        code, output = self.harness("run")
        self.assertEqual(code, 1, output)
        self.assertEqual(len(self.calls("managed-start")), 1)
        self.assertEqual(self.store().actions(self.latest_run()["id"])[0]["status"],
                         "reconciled_not_applied")


class UnknownOutcomeTests(SimulatedJourney):
    def test_unanswered_setup_is_never_requested_again_until_a_human_answers(self):
        self.set_scenario(setup="timeout")
        code, output = self.harness("provision")
        self.assertEqual(code, 2, output)
        run_id = self.latest_run()["id"]
        self.assertIn("UNRESOLVED MUTATION: run " + run_id, self.harness("status")[1])
        self.assertEqual(self.harness("provision")[0], 4)  # Other runs refused.
        self.set_scenario(setup="normal")
        code, output = self.harness("resume", run_id)
        self.assertEqual(code, 2, output)
        self.assertIn("not issued again", self.steps(run_id)["request_approval"]["summary"])
        self.assertEqual(len(self.calls("managed-passwords-setup")), 1)
        action = self.store().actions(run_id, unresolved_only=True)[0]["id"]
        code, output = self.harness("reconcile", run_id, "--action", str(action), "--outcome",
                                    "not-applied", "--note", "cancelled in Passwords UI")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.harness("resume", run_id)[0], 3)  # Asks the owner again.
        self.assertEqual(len(self.calls("managed-passwords-setup")), 2)
        events = [e["kind"] for e in self.store().events(run_id)]
        self.assertIn("operator_reconciled", events)

    def test_unanswered_apply_is_settled_by_the_idempotent_apply(self):
        self.assertEqual(self.harness("provision")[0], 3)
        run_id = self.latest_run()["id"]
        self.sim("sim-approve", next(iter(self.sim_state()["setups"])))
        self.set_scenario(setup="apply_timeout_applied")
        code, output = self.harness("resume", run_id)
        self.assertEqual(code, 2, output)  # Setup still says approved: outcome unknown.
        self.assertIsNone(self.store().fixture("sim-employee-1"))
        code, output = self.harness("resume", run_id)
        self.assertEqual(code, 0, output)
        self.assertTrue(self.steps(run_id)["apply_approval"]["evidence"]["already_applied"])
        self.assertEqual(len(self.calls("managed-passwords-apply")), 2)
        statuses = [a["status"] for a in self.store().actions(run_id)
                    if a["kind"] == "managed_passwords_apply"]
        self.assertEqual(statuses, ["superseded", "succeeded"])
        self.assertIsNotNone(self.store().fixture("sim-employee-1"))

    def test_binding_that_differs_from_the_applied_result_is_not_recorded(self):
        self.assertEqual(self.harness("provision")[0], 3)
        self.sim("sim-approve", next(iter(self.sim_state()["setups"])))
        self.set_scenario(setup="rebound_after_apply")
        code, output = self.harness("resume", self.latest_run()["id"])
        self.assertEqual(code, 2, output)
        self.assertEqual(self.steps()["read_back"]["outcome"], "blocked")
        self.assertIsNone(self.store().fixture("sim-employee-1"))

    def test_unexpected_response_shape_is_a_contract_finding_not_a_harness_error(self):
        self.provision()
        self.set_scenario(activity_output="unexpected_shape")
        code, output = self.harness("run")
        self.assertEqual(code, 2, output)
        step = self.steps()["initial_state"]
        self.assertIn("did not match the supported contract", step["summary"])
        self.assertNotIn("harness error", output)
        self.assertEqual(self.calls("managed-start"), [])


class LockAndResumeTests(SimulatedJourney):
    def test_run_refused_while_another_run_holds_the_employee(self):
        self.provision()
        store = self.store()
        other = store.create_run("operational", "sim-employee-1", "simulation", "x", "0")
        store.acquire_lock("sim-employee-1", other)
        code, output = self.harness("run")
        self.assertEqual(code, 4, output)
        self.assertIn("locked by run " + other, output)
        self.assertEqual(self.calls("managed-start"), [])

    def test_resume_detects_changed_revision_and_invalidates_evidence(self):
        self.assertEqual(self.harness("provision")[0], 3)
        run_id = self.latest_run()["id"]
        self.sim("sim-change-revision")
        self.sim("sim-approve", next(iter(self.sim_state()["setups"])))
        code, output = self.harness("resume", run_id)
        self.assertEqual(code, 2, output)
        steps = self.steps(run_id)
        self.assertEqual(steps["inspect_employee"]["outcome"], "blocked")
        self.assertIn("active_revision_id", steps["inspect_employee"]["summary"])
        self.assertEqual(steps["request_approval"]["outcome"], "not_run")
        self.assertEqual(self.calls("managed-passwords-apply"), [])
        self.assertIsNone(self.store().fixture("sim-employee-1"))

    def test_resume_refuses_changed_configuration(self):
        self.assertEqual(self.harness("provision")[0], 3)
        self.write_config(state_dir="state", disclosure={"include_identifiers": True})
        self.assertEqual(self.harness("resume", self.latest_run()["id"])[0], 4)


class CleanupTests(SimulatedJourney):
    def test_failed_cleanup_stays_visible_and_blocks_the_next_run(self):
        self.provision()
        self.set_scenario(stop="reject")
        code, output = self.harness("run")
        self.assertEqual(code, 0, output)  # The journey passed; cleanup is reported apart.
        first = self.latest_run()["id"]
        report = self.report(first)
        self.assertEqual(report["cleanup"][0]["status"], "failed")
        self.assertFalse(report["completion"]["clean"])  # A passed journey is not clean.
        self.assertIn("Cleanup is not confirmed", " ".join(report["limitations"]))
        self.assertIn("OPEN CLEANUP: run " + first, self.harness("status")[1])
        code, output = self.harness("run")
        self.assertEqual(code, 4, output)
        self.assertIn("cleanup from run " + first, output)
        self.set_scenario(stop="ok")
        code, output = self.harness("run")
        self.assertEqual(code, 0, output)  # Prior cleanup reconciled first.
        self.assertNotIn("OPEN CLEANUP", self.harness("status")[1])

    def test_interrupted_run_stops_the_runtime(self):
        self.provision()
        self.set_scenario(readiness="never")
        self.write_config(seren={"executable": str(self.exe), "command_timeout_seconds": 2,
                                 "poll_interval_seconds": 0.05, "readiness_timeout_seconds": 30,
                                 "shutdown_timeout_seconds": 2})
        process = self.spawn_harness("run")
        self.wait_for(lambda: self.calls("managed-start"))
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=20)
        run = self.latest_run()
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(self.report(run["id"])["cleanup"][0]["status"], "done")
        self.assertEqual(self.sim_state()["deployment"]["status"], "stopped")

    def test_killed_run_leaves_visible_obligation_that_resume_reconciles(self):
        self.provision()
        self.set_scenario(readiness="never")
        self.write_config(seren={"executable": str(self.exe), "command_timeout_seconds": 2,
                                 "poll_interval_seconds": 0.05, "readiness_timeout_seconds": 30,
                                 "shutdown_timeout_seconds": 2})
        process = self.spawn_harness("run")
        self.wait_for(lambda: self.calls("managed-start"))
        time.sleep(0.3)
        process.kill()  # No chance to clean up.
        process.communicate(timeout=20)
        killed = self.latest_run()["id"]
        status = self.harness("status")[1]
        self.assertIn("OPEN CLEANUP: run " + killed, status)
        code, output = self.harness("run")
        self.assertEqual(code, 4, output)
        self.assertIn("resume that run", output)
        self.set_scenario(readiness="ready")
        code, output = self.harness("resume", killed)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.report(killed)["cleanup"][0]["status"], "done")

    def test_resume_rechecks_approval_revoked_while_interrupted(self):
        self.provision()
        self.set_scenario(readiness="never")
        self.write_config(seren={"executable": str(self.exe), "command_timeout_seconds": 2,
                                 "poll_interval_seconds": 0.05, "readiness_timeout_seconds": 30,
                                 "shutdown_timeout_seconds": 2})
        process = self.spawn_harness("run")
        self.wait_for(lambda: self.calls("managed-start"))
        time.sleep(0.3)
        process.kill()
        process.communicate(timeout=20)
        killed = self.latest_run()["id"]
        with fake_seren.locked_state(self.state) as state:  # Revoked; result id unchanged.
            fake_seren.set_condition(state["deployment"], "AwaitingApproval", "True")
        self.set_scenario(readiness="ready")
        code, output = self.harness("resume", killed)
        self.assertEqual(code, 2, output)
        steps = self.steps(killed)
        self.assertEqual(steps["verify_fixture"]["outcome"], "blocked")
        for name in ("initial_state", "start_runtime", "runtime_ready", "send_probe",
                     "verify_reply"):
            self.assertEqual(steps[name]["outcome"], "not_run", name)
        self.assertEqual(len(self.calls("managed-start")), 1)
        self.assertEqual(self.report(killed)["cleanup"][0]["status"], "done")
        self.assertEqual(self.sim_state()["deployment"]["status"], "stopped")

    def spawn_harness(self, *args):
        env = dict(os.environ, PYTHONPATH=str(SOURCE))
        return subprocess.Popen([sys.executable, "-m", "seren_harness", *args, "--config",
                                 str(self.config)], cwd=str(self.root), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_for(self, condition, timeout=15):
        deadline = time.time() + timeout
        while not condition():
            if time.time() > deadline:
                self.fail("condition not reached")
            time.sleep(0.05)


class HonestyTests(SimulatedJourney):
    def test_unknown_server_build_is_reported(self):
        self.provision()
        self.harness("run")
        report = self.report()
        self.assertEqual(report["run"]["server_build"], "unknown")
        self.assertIn("Server build is unknown", " ".join(report["limitations"]))

    def test_unsupported_cli_version_blocks_without_touching_the_deployment(self):
        with fake_seren.locked_state(self.state) as state:
            state["version_text"] = "seren 0.11.0 (simulation)"
        code, output = self.harness("run")
        self.assertEqual(code, 2, output)
        self.assertIn("no established contract", " ".join(self.report()["limitations"]))
        self.assertEqual([e for e in self.sim_state()["log"] if e != ["--version"]], [])

    def test_undecodable_output_is_a_contract_finding(self):
        self.provision()
        self.set_scenario(managed_get_output="garbage")
        code, output = self.harness("run")
        self.assertEqual(code, 2, output)
        verify = self.steps()["verify_fixture"]
        self.assertIn("did not match the supported contract", verify["summary"])
        self.assertEqual(self.calls("managed-start"), [])

    def test_live_configuration_refuses_the_simulator(self):
        self.write_config(environment="live", discord={})
        self.assertEqual(self.harness("preflight")[0], 2)
        self.assertIn("refusing to mix", self.steps()["preflight"]["summary"])

    def test_live_runs_require_the_recorded_executable_checksum(self):
        # A non-simulator version string makes this fixture look live to preflight.
        with fake_seren.locked_state(self.state) as state:
            state["version_text"] = "seren 0.10.0"
        seren = {"executable": str(self.exe), "command_timeout_seconds": 2}
        self.write_config(environment="live", discord={}, seren=seren)
        self.assertEqual(self.harness("preflight")[0], 2)
        self.assertIn("executable_sha256 is required", self.steps()["preflight"]["summary"])
        self.write_config(environment="live", discord={},
                          seren=dict(seren, executable_sha256="0" * 64))
        self.harness("preflight")
        self.assertIn("checksum differs", self.steps()["preflight"]["summary"])
        digest = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        self.write_config(environment="live", discord={},
                          seren=dict(seren, executable_sha256=digest))
        self.harness("preflight")
        self.assertEqual(self.steps()["preflight"]["outcome"], "passed")
        self.assertEqual(self.steps()["preflight"]["evidence"]["executable_sha256"], digest)

    def test_preflight_reads_only(self):
        code, output = self.harness("preflight")
        self.assertEqual(code, 0, output)
        self.assertEqual({e[1] for e in self.sim_state()["log"] if e[0] == "agent"},
                         {"managed-get"})
        self.assertEqual(self.steps()["inspect_target"]["evidence"]["status"], "stopped")
        self.assertIn("channel_messages_readable", self.steps()["integrations"]["evidence"])


class FindingAndDraftTests(SimulatedJourney):
    def test_identical_failures_accumulate_without_duplicate_drafts(self):
        self.provision()
        self.set_scenario(readiness="binding_failure")
        first_run = None
        for index in range(2):
            self.harness("run")
            run_id = self.latest_run()["id"]
            first_run = first_run or run_id
            code, output = self.harness("draft-issue", run_id)
            self.assertEqual(code, 0, output)
            self.assertIn("created" if index == 0 else "updated", output)
        findings = self.store().findings()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["occurrences"], 2)
        drafts = list((self.root / "state" / "drafts").iterdir())
        self.assertEqual(len(drafts), 1)
        self.assertIn("2 run(s)", drafts[0].read_text())
        self.assertIn("unchanged", self.harness("draft-issue", first_run)[1])

    def test_distinct_failures_are_separate_findings(self):
        self.provision()
        self.set_scenario(readiness="binding_failure")
        self.harness("run")
        self.set_scenario(readiness="never")
        self.harness("run")
        self.assertEqual(len(self.store().findings()), 2)

    def test_draft_is_useful_and_pseudonymized(self):
        self.provision()
        self.set_scenario(readiness="binding_failure")
        self.harness("run")
        self.harness("draft-issue", self.latest_run()["id"])
        draft = next((self.root / "state" / "drafts").iterdir()).read_text()
        for section in ("## Customer impact", "## Environment", "## Reproduction",
                        "## Expected", "## Observed", "## Unknowns", "## Retest",
                        "Server build: unknown", "SIMULATION", "provisional",
                        "managed-start", "Duplicate search unavailable"):
            self.assertIn(section, draft)
        self.assertNotIn(self.deployment, draft)
        self.assertNotIn("Simulated Test Employee", draft)

    def test_related_issue_search_is_read_only_suggestion(self):
        gh = self.root / "fake-gh"
        log = self.root / "gh-args.json"
        gh.write_text(standin(
            "#!" + sys.executable + "\nimport json, sys\n"
            "open(" + repr(str(log)) + ", 'w').write(json.dumps(sys.argv[1:]))\n"
            "print(json.dumps([{'number': 232, 'title': 'Managed Discord connector "
            "fails', 'state': 'OPEN', 'url': 'https://github.invalid/i/232'}]))\n"))
        gh.chmod(0o755)
        prerun(gh)  # first-run assessment outside the timed search
        self.provision()
        self.write_config(github={"related_search": True, "gh_executable": str(gh),
                                  "repository": "example/repo"})
        self.set_scenario(readiness="binding_failure")
        self.harness("run")
        self.harness("draft-issue", self.latest_run()["id"])
        self.assertEqual(json.loads(log.read_text())[:2], ["issue", "list"])
        draft = next((self.root / "state" / "drafts").iterdir()).read_text()
        self.assertIn("#232 (open)", draft)
        self.assertIn("a human confirms", draft)


class SecretTests(SimulatedJourney):
    def test_secrets_and_capability_urls_never_persist(self):
        os.environ["SEREN_HARNESS_TEST_READER"] = "reader-secret-canary-value"
        self.addCleanup(os.environ.pop, "SEREN_HARNESS_TEST_READER")
        self.write_config(discord={
            "sender": "simulation", "observer": "simulation", "channel_id": "sim-channel",
            "employee_user_id": "sim-employee", "reply_timeout_seconds": 1.5,
            "simulation_channel_file": "sim-channel.jsonl",
            "reader_token_env": "SEREN_HARNESS_TEST_READER"})
        code, output = self.harness("provision")
        self.assertIn(fake_seren.SECRET_CANARY, output)  # Shown once to the operator only.
        self.sim("sim-approve", next(iter(self.sim_state()["setups"])))
        self.harness("resume", self.latest_run()["id"])
        self.set_scenario(readiness="binding_failure")
        self.harness("run")
        self.harness("draft-issue", self.latest_run()["id"])
        persisted = self.all_persisted_bytes()
        for canary in CANARIES:
            self.assertNotIn(canary.encode(), persisted)
        self.assertIn(b"passwords.example.invalid", persisted)  # Host kept for diagnosis.

    def test_config_rejects_inline_secret_values(self):
        self.write_config(discord={"bot_token": "abc"})
        code, output = self.harness("preflight")
        self.assertEqual(code, 5)
        self.assertIn("looks like a secret value", output)


if __name__ == "__main__":
    unittest.main()
