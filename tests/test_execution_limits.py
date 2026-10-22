"""Execution ceilings of the response-correctness package, checked against real step code.

SIMULATION: a fake Seren CLI, Discord observer and clock drive the existing step functions
and journey runner; time advances only through the steps' own sleeps (reads cost nothing,
the worst case for read counts). Traces are counted in the harness's own unit (Seren CLI
service invocations) and compared with execution_limits, not with declared constants.
"""
from collections import Counter
from pathlib import Path
import tempfile
import time
import unittest

from seren_support import SOURCE  # noqa: F401  (imports this checkout)
from seren_harness import contract, execution_limits as limits, seren
from seren_harness.config import Config
from seren_harness.journey import JourneyRunner
from seren_harness.models import PASSED
from seren_harness.seren_cli import ERROR, OK, TIMEOUT, CliResult
from seren_harness.state import AMBIGUOUS, RECONCILED_APPLIED, Store
from test_seren_journeys import SimulatedJourney

DEPLOYMENT = "7a00003a-0000-4000-8000-00000000003b"
CHANNEL, EMPLOYEE, HUMAN = "100000000000000001", "100000000000000002", "100000000000000003"


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeCli:
    """Answers like the pinned CLI for the commands the lifecycle steps use."""

    def __init__(self, world):
        self.world = world
        self.history = []

    @staticmethod
    def key(args):
        return " ".join(args[1:4]) if args[1] == "cloud" else args[1]

    def display_argv(self, args):
        return ["seren", *args]

    def version(self):
        return CliResult(OK, ["seren", "--version"], "seren 0.10.0 (simulation)")

    def json(self, *args):
        args = list(args)
        self.history.append(args)
        w, key = self.world, self.key(args)
        if key == "managed-get":
            conditions = [{"type": "Ready", "status": "True" if w["ready"] else "False"},
                          {"type": "AwaitingApproval", "status": "False"}]
            return CliResult(OK, args, {"data": dict(w["detail"], status=w["status"],
                                                     conditions=conditions)})
        if key == "cloud deployment status":
            return CliResult(OK, args, {"data": {
                "id": DEPLOYMENT, "status": w["status"], "desired_lifecycle_state": w["desired"],
                "control_generation": w["generation"]}})
        if key == "managed-deployment-activity":
            return CliResult(OK, args, {"data": {"runs": []}})
        if key == "managed-deployment-health":
            return CliResult(OK, args, {"data": {"status": "ok", "findings": []}})
        if key in ("managed-start", "managed-stop"):
            w["desired"] = "running" if key == "managed-start" else "stopped"
            w["generation"] += 1
            if key == "managed-start":
                w["status"] = "running"
            elif w["stops_confirm"]:
                w["status"] = "stopped"
            if w.get(key + "_unknown"):
                return CliResult(TIMEOUT, args)
            return CliResult(OK, args, {"data": {"status": w["status"],
                                                 "desired_lifecycle_state": w["desired"]}})
        if key == "managed-passwords-status":
            return CliResult(OK, args, {"status": w["setup_status"]})
        if key == "managed-passwords-apply":
            return CliResult(TIMEOUT, args)
        return CliResult(ERROR, args, stderr_tail="unexpected command")


class FakeObserver:
    def __init__(self):
        self.reads = 0

    def messages(self):
        self.reads += 1
        return []


def world(**changes):
    detail = {"deployment_id": DEPLOYMENT, "agent_identity_id": "a", "active_revision_id": "r",
              "secret_resolution_result_id": "s", "approval_policy": "read_only"}
    base = {"detail": detail, "status": "stopped", "desired": "stopped", "generation": 7,
            "ready": False, "stops_confirm": True, "setup_status": "approved"}
    base.update(changes)
    return base


class Harness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        executable = self.root / "seren"
        executable.write_text("stand-in")
        self.config = Config({
            "environment": "simulation",
            "seren": {"executable": str(executable),
                      **{k: v for k, v in limits.PINNED.items()
                         if k not in ("reply_timeout_seconds",)}},
            "employee": {"key": "sim-1", "deployment_id": DEPLOYMENT},
            "discord": {"sender": "human", "observer": "discord_rest", "channel_id": CHANNEL,
                        "employee_user_id": EMPLOYEE, "human_sender_user_id": HUMAN,
                        "reply_timeout_seconds": limits.PINNED["reply_timeout_seconds"],
                        "expected_reply": {"template": "ACK {marker}", "source": "SYNTHETIC"}},
            "state_dir": "state"}, source=str(self.root / "config.json"))
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.ceilings = limits.ceilings()

    def context(self, w, journey="operational"):
        run = self.store.create_run(journey, "sim-1", "simulation", self.config.digest, "test")
        self.clock, self.cli, self.observer = Clock(), FakeCli(w), FakeObserver()
        self.store.save_fixture("sim-1", contract.identity(dict(
            w["detail"], conditions=[{"type": "Ready", "status": "False"}])), run)
        return seren.Context(self.config, self.store, run, self.cli, observer=self.observer,
                             output=lambda *a: None, sleep=self.clock.sleep,
                             clock=self.clock.now)

    def counts(self):
        return Counter(FakeCli.key(args) for args in self.cli.history)


class PinTests(Harness):
    def test_the_pinned_configuration_passes_and_defaults_do_not(self):
        self.assertEqual(limits.check_pins(self.config), [])
        default = Config(dict(self.config.data, seren={"executable": self.config.executable}),
                         source=self.config.source)
        problems = " ".join(limits.check_pins(default))
        for key in ("readiness_timeout_seconds", "shutdown_timeout_seconds",
                    "max_apply_attempts"):
            self.assertIn(key, problems)


class LoopBoundaryTests(Harness):
    def test_readiness_reads_up_to_and_at_its_deadline(self):
        ctx = self.context(world(status="running"))
        result = seren.runtime_ready(ctx)
        self.assertEqual(result.outcome, "failed")
        self.assertIn("not ready within 180", result.summary)
        polls = self.ceilings["loop_reads"]["readiness"]
        self.assertEqual((polls, self.clock.t), (37, 180.0))
        self.assertEqual(self.counts(), Counter({"managed-get": polls,
                         "managed-deployment-health": polls,
                         "managed-deployment-activity": polls}))
        # Ready exactly at the deadline poll still passes; one poll later never happens.
        w = world(status="running")
        ctx = self.context(w)
        original = ctx.sleep

        def sleep(seconds):
            original(seconds)
            w["ready"] = self.clock.t >= 180
        ctx.sleep = sleep
        self.assertEqual(seren.runtime_ready(ctx).outcome, PASSED)
        self.assertEqual(self.counts()["managed-get"], polls)

    def test_probe_and_reply_observation_stop_at_their_deadlines(self):
        ctx = self.context(world())
        result = seren.send_probe(ctx)
        self.assertIn("not observed within 120", result.summary)
        self.assertEqual((self.observer.reads, self.clock.t),
                         (self.ceilings["loop_reads"]["probe"], 120.0))
        self.store.record_step(ctx.run_id, 5, "send_probe", seren.passed(
            "probe", probe_id="100000000000000004", marker="SH-0123456789AB",
            input_mode="human-assisted"), None)
        self.observer.reads, self.clock.t = 0, 0.0
        result = seren.verify_reply(ctx)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(self.observer.reads, self.ceilings["loop_reads"]["reply"])
        self.assertEqual(self.ceilings["totals"]["discord_reads"], 50)
        self.assertEqual(self.counts(), Counter())  # no Seren command while observing

    def test_unconfirmed_shutdown_stays_unresolved_within_its_bound(self):
        w = world(status="running", desired="running", stops_confirm=False,
                  **{"managed-stop_unknown": True})
        ctx = self.context(w)
        self.store.add_cleanup(ctx.run_id, "sim-1", "stop_runtime", DEPLOYMENT)
        seren.cleanup(ctx)
        self.assertEqual(dict(self.counts()), self.ceilings["phases"]["cleanup"]["by_command"])
        item = self.store.cleanup_items(ctx.run_id)[0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("not confirmed within 120", item["detail"]["reason"])
        self.assertEqual(len(self.store.open_cleanup(run_id=ctx.run_id)), 1)


class JourneyTraceTests(Harness):
    def test_worst_case_operational_trace_equals_the_ceilings(self):
        # Start outcome unknown (reconciled from state), never ready, stop outcome unknown
        # and never confirmed: every bounded path runs to its limit.
        w = world(stops_confirm=False, **{"managed-start_unknown": True,
                                          "managed-stop_unknown": True})
        ctx = self.context(w)
        JourneyRunner(self.store, ctx.run_id, "operational", seren.OPERATIONAL, ctx,
                      cleanup=seren.cleanup).execute()
        steps = {s["name"]: s["outcome"] for s in self.store.steps(ctx.run_id)}
        self.assertEqual(steps["runtime_ready"], "failed")
        for later in ("send_probe", "verify_reply", "verify_reply_content"):
            self.assertEqual(steps[later], "not_run")  # ordinary work stops at the limit
        expected = Counter(self.ceilings["phases"]["operational"]["by_command"])
        expected.update(self.ceilings["phases"]["cleanup"]["by_command"])
        self.assertEqual(self.counts(), expected)
        self.assertEqual(sum(self.counts().values()),
                         self.ceilings["phases"]["operational"]["service_invocations"]
                         + self.ceilings["phases"]["cleanup"]["service_invocations"])
        self.assertEqual(len(self.store.open_cleanup(run_id=ctx.run_id)), 1)  # unresolved

    def test_stop_recovery_through_the_control_journey_is_bounded(self):
        w = world(status="running", desired="running", stops_confirm=False)
        first = self.context(w)
        self.store.add_cleanup(first.run_id, "sim-1", "stop_runtime", DEPLOYMENT)
        w["stops_confirm"] = True
        ctx = self.context(w, journey="control")
        self.cli = ctx.cli
        self.assertIsNone(seren.reconcile_employee(ctx))
        JourneyRunner(self.store, ctx.run_id, "control", seren.CONTROL, ctx,
                      cleanup=seren.cleanup).execute()
        counts = self.counts()
        bound = self.ceilings["phases"]["stop_recovery"]
        self.assertLessEqual(sum(counts.values()), bound["service_invocations"])
        self.assertEqual(counts["managed-stop"], 1)
        self.assertEqual(self.store.open_cleanup(run_id=first.run_id), [])


class ApplyPolicyTests(Harness):
    def apply_context(self, status):
        ctx = self.context(world(), journey="provisioning")
        self.store.record_step(ctx.run_id, 2, "request_approval",
                               seren.passed("setup", setup_id="00000000-0000-4000-8000-0000000"
                                            "00001"), None)
        action = self.store.record_intent(ctx.run_id, "apply_approval", "managed_passwords_apply",
                                          DEPLOYMENT, {"argv": ["seren"]})
        self.store.resolve_action(action, status, {})
        return ctx

    def test_one_attempt_then_reads_only(self):
        ctx = self.apply_context(AMBIGUOUS)
        result = seren.apply_approval(ctx)
        self.assertEqual(result.outcome, "blocked")
        self.assertIn("not issued again", result.summary)
        self.assertEqual(self.counts()["managed-passwords-apply"], 0)
        ctx = self.apply_context(RECONCILED_APPLIED)
        result = seren.apply_approval(ctx)
        self.assertEqual(result.outcome, PASSED)
        self.assertEqual(self.counts()["managed-passwords-apply"], 0)

    def test_first_attempt_is_issued_once_and_never_again(self):
        ctx = self.context(world(), journey="provisioning")
        self.store.record_step(ctx.run_id, 2, "request_approval",
                               seren.passed("setup", setup_id="00000000-0000-4000-8000-0000000"
                                            "00001"), None)
        first = seren.apply_approval(ctx)  # unknown outcome; status still "approved"
        self.assertEqual(first.outcome, "blocked")
        second = seren.apply_approval(ctx)  # as a resume would re-run the step
        self.assertIn("not issued again", second.summary)
        self.assertEqual(self.counts()["managed-passwords-apply"], 1)
        self.assertEqual(self.counts()["managed-passwords-status"], 1)  # bounded reconcile


class ProvisioningResumeTests(SimulatedJourney):
    """SIMULATION through the real CLI: provision, resumes while waiting, approval, apply."""

    def pinned(self):
        seren_settings = {k: v for k, v in limits.PINNED.items() if k != "reply_timeout_seconds"}
        self.write_config(seren=dict(executable=str(self.exe), **dict(
            seren_settings, poll_interval_seconds=0.05, readiness_timeout_seconds=2,
            shutdown_timeout_seconds=2, command_timeout_seconds=2)))

    def service_calls(self):
        return [e for e in self.sim_state()["log"] if e[:1] == ["agent"]]

    def test_resumes_stay_within_the_provisioning_ceiling(self):
        self.pinned()
        self.assertEqual(self.harness("provision")[0], 3)
        run_id = self.latest_run()["id"]
        for _ in range(limits.PROVISIONING_RESUMES - 1):  # owner has not approved yet
            self.assertEqual(self.harness("resume", run_id)[0], 3)
        self.sim("sim-approve", next(iter(self.sim_state()["setups"])))
        self.assertEqual(self.harness("resume", run_id)[0], 0)
        bound = limits.ceilings()["phases"]["provisioning"]
        calls = Counter(e[1] for e in self.service_calls())
        self.assertLessEqual(sum(calls.values()), bound["service_invocations"])
        for command, count in calls.items():
            self.assertLessEqual(count, bound["by_command"][command], command)
        self.assertEqual((calls["managed-passwords-setup"], calls["managed-passwords-apply"]),
                         (1, 1))

    def test_a_pinned_single_apply_is_not_reissued_by_resume(self):
        self.pinned()
        self.assertEqual(self.harness("provision")[0], 3)
        run_id = self.latest_run()["id"]
        self.sim("sim-approve", next(iter(self.sim_state()["setups"])))
        self.set_scenario(setup="apply_timeout_applied")
        self.assertEqual(self.harness("resume", run_id)[0], 2)  # outcome unknown
        code, output = self.harness("resume", run_id)
        self.assertEqual(code, 2, output)
        self.assertIn("not issued again", self.steps(run_id)["apply_approval"]["summary"])
        self.assertEqual(len(self.calls("managed-passwords-apply")), 1)
        self.assertIsNone(self.store().fixture("sim-employee-1"))  # no baseline recorded


class TimedCli(FakeCli):
    """Each command takes `duration` seconds of fake time. Readiness turns true at the
    readiness loop's last poll; a stop confirms `confirm_after` seconds after it returns."""

    def __init__(self, world, clock, duration):
        super().__init__(world)
        self.clock, self.duration, self.times = clock, duration, {}

    def json(self, *args):
        key, w, now = self.key(list(args)), self.world, self.clock.now()
        self.times.setdefault(key, []).append(now)
        if key == "managed-get" and w["status"] == "running" and w["desired"] == "running":
            w.setdefault("first_poll", now)  # the readiness loop's deadline starts here
            # Ready on the poll after which the readiness deadline has passed (its last).
            w["ready"] = now + 3 * self.duration - w["first_poll"] >= 180
        if key == "managed-get" and w.get("stopped_at") is not None:
            w["status"] = "stopped" if now >= w["stopped_at"] else w["status"]
        result = super().json(*args)
        self.clock.sleep(self.duration)
        if key == "managed-stop" and w.get("confirm_after") is not None:
            w["stopped_at"] = self.clock.now() + w["confirm_after"]
        return result


class TimedObserver:
    """Each read takes `duration`; the probe appears on the read after which the probe loop's
    deadline has passed, i.e. its last read."""

    def __init__(self, clock, duration, outputs):
        self.clock, self.duration, self.outputs, self.first = clock, duration, outputs, None
        self.reads = 0

    def messages(self):
        self.reads += 1
        now = self.clock.now()
        self.first = now if self.first is None else self.first
        self.clock.sleep(self.duration)
        marker = next((t.split()[-1] for t in self.outputs if "SH-" in t), None)
        if marker and now + self.duration - self.first >= 120:  # the loop's last read
            snowflake = (int(time.time() * 1000) - 1420070400000) << 22
            return [{"id": str(snowflake), "channel_id": CHANNEL, "content": "probe " + marker,
                     "author": {"id": HUMAN}}]
        return []


class TimingTests(Harness):
    """Controlled command durations against the declared time model (no local overhead)."""

    def timed(self, w, duration, read_duration, journey="operational"):
        ctx = self.context(w, journey)
        self.cli = TimedCli(w, self.clock, duration)
        ctx.cli, outputs = self.cli, []
        ctx.output = outputs.append
        ctx.observer = TimedObserver(self.clock, read_duration, outputs)
        return ctx

    def test_runtime_through_initial_shutdown_and_recovery_stays_within_bounds(self):
        model = self.ceilings["time_model"]
        for duration in (0, 1, 13, 60):
            for read_duration in (0, 20):
                w = world(stops_confirm=False, confirm_after=None,
                          **{"managed-stop_unknown": True})
                ctx = self.timed(w, duration, read_duration)
                JourneyRunner(self.store, ctx.run_id, "operational", seren.OPERATIONAL, ctx,
                              cleanup=seren.cleanup).execute()
                steps = {s["name"]: s["outcome"] for s in self.store.steps(ctx.run_id)}
                self.assertEqual((steps["runtime_ready"], steps["send_probe"],
                                  steps["verify_reply"]), ("passed", "passed", "failed"))
                started = self.cli.times["managed-start"][0]
                ordinary_end = self.cli.times["managed-stop"][0]  # cleanup's stop follows
                self.assertLessEqual(ordinary_end, model["ordinary_execution_seconds"]
                                     + model["initial_shutdown_seconds"])
                self.assertLessEqual(self.clock.now() - started,
                                     limits.runtime_bound("initial"), (duration, read_duration))
                self.assertEqual(len(self.store.open_cleanup(run_id=ctx.run_id)), 1)
                # The one recovery, started at the latest allowed delay, confirms at its
                # last confirmation poll.
                self.clock.sleep(limits.RECOVERY_DELAY)
                w["confirm_after"] = limits.PINNED["shutdown_timeout_seconds"]
                control = self.store.create_run("control", "sim-1", "simulation",
                                                self.config.digest, "test")
                recovery = seren.Context(self.config, self.store, control, self.cli,
                                         sleep=self.clock.sleep, clock=self.clock.now,
                                         output=lambda *a: None)
                self.assertIsNone(seren.reconcile_employee(recovery))
                confirmed = self.clock.now()
                self.assertEqual(w["status"], "stopped")
                self.assertLessEqual(confirmed - started, limits.runtime_bound("recovery"),
                                     (duration, read_duration))
                self.assertEqual(self.store.open_cleanup(run_id=ctx.run_id), [])

    def test_failed_recovery_leaves_shutdown_unresolved_with_no_runtime_bound(self):
        w = world(status="running", desired="running", stops_confirm=False,
                  confirm_after=None)
        first = self.timed(w, 60, 20)
        self.store.add_cleanup(first.run_id, "sim-1", "stop_runtime", DEPLOYMENT)
        seren.cleanup(first)
        self.clock.sleep(limits.RECOVERY_DELAY)
        control = self.store.create_run("control", "sim-1", "simulation", self.config.digest,
                                        "test")
        recovery = seren.Context(self.config, self.store, control, self.cli,
                                 sleep=self.clock.sleep, clock=self.clock.now,
                                 output=lambda *a: None)
        refusal = seren.reconcile_employee(recovery)
        self.assertIn("still unconfirmed", refusal)
        self.assertEqual(len(self.store.open_cleanup(run_id=first.run_id)), 1)
        self.assertEqual(self.counts()["managed-stop"], 2)  # initial and the one recovery
        self.assertIsNone(limits.runtime_bound("unconfirmed"))

    def test_window_gates_keep_cleanup_and_recovery_capacity(self):
        model = self.ceilings["time_model"]
        gate = model["operational_start_gate_seconds"]
        self.assertTrue(limits.operational_may_start(gate))
        self.assertFalse(limits.operational_may_start(gate - 1))
        self.assertEqual(gate, model["ordinary_execution_seconds"]
                         + model["initial_shutdown_seconds"] + limits.RECOVERY_DELAY
                         + model["stop_recovery_seconds"])
        budget = model["window_budget_seconds"]
        self.assertLessEqual(budget["update"] + budget["provisioning"] + gate,
                             model["window_minutes"] * 60)


class CeilingTests(unittest.TestCase):
    def test_totals_are_derived_and_consistent(self):
        c = limits.ceilings()
        phases = c["phases"]
        self.assertEqual(c["loop_reads"], {"readiness": 37, "probe": 25, "reply": 25,
                                           "shutdown_confirmation": 25})
        self.assertEqual(c["totals"]["service_invocations"],
                         {"ordinary": sum(phases[p]["service_invocations"]
                                          for p in ("update", "provisioning", "operational")),
                          "cleanup_reserve": phases["cleanup"]["service_invocations"]
                          + phases["stop_recovery"]["service_invocations"]})
        self.assertEqual(phases["provisioning"]["mutations"],
                         {"managed-passwords-setup": 1, "managed-passwords-apply": 1})
        self.assertEqual(phases["update"]["mutations"], {"managed-update": 1})
        model = c["time_model"]["runtime_until_confirmed_stop_seconds"]
        self.assertIsNone(model["unconfirmed"])
        self.assertIsNone(limits.runtime_bound("unconfirmed"))
        self.assertLess(model["confirmed_by_initial_cleanup"], model["confirmed_by_stop_recovery"])
        self.assertNotIn("employee_runtime_seconds_upper", c["totals"])
        # Longer limits raise the ceilings through the same formulas.
        wider = limits.ceilings({"readiness_timeout_seconds": 300})
        self.assertEqual(wider["loop_reads"]["readiness"], 61)


if __name__ == "__main__":
    unittest.main()
