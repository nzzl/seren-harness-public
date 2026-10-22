"""Supplied-data fixture creation and campaign handoff, offline.

SYNTHETIC: a fake onboarding/lifecycle service and fake time drive the real journal, store,
executor, reviewer and campaign runner. Sockets and subprocesses are blocked; no Seren
call. A pass verifies the harness path, not the service or the employee.
"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_harness import (employee_run, finance_invoices as fi, invoice_campaign as campaign,
                           invoice_catalog as catalog, supplied_fixture as sf)
from seren_harness.cli import main
from seren_harness.employee_run import REVIEWED_OPERATIONS, REVIEWED_TOOLS
from seren_harness.seren_cli import CliResult
from seren_harness.state import Store
from test_bd_brief import answer_for
import test_finance_employee as finance_tests
from test_employee_run import FakeCli
from test_employee_tool import fixture as run_fixture

OID = "7a00001d-0000-4000-8000-000000000001"
DEP = "7a00001d-0000-4000-8000-000000000002"
REV = "7a00001d-0000-4000-8000-000000000003"
OTHER = "7a00001d-0000-4000-8000-000000000009"
AUTH = "7a00001d-0000-4000-8000-00000000001e"
CLOCK = "7a000021-0000-4000-8000-000000000022"


class Interrupt(BaseException):
    """A process interruption at an exact point, not a returned timeout."""


class Service:
    """One onboarding and the deployment it creates. Apply bootstraps a running runtime."""

    def __init__(self, modes=()):
        self.modes, self.calls, self.timeout = set(modes), [], 30
        self.revision, self.status, self.stops = 0, None, 0
        self.name, self.record = sf.NAME, None  # record: replaces the onboarding get reply
        self.stop_timeouts = []

    def display_argv(self, args):
        return ["seren", *map(str, args)]

    def json(self, *args):
        args = [str(a) for a in args]
        self.calls.append(args)
        argv = ["seren", *args, "-o", "json"]
        ok = lambda data: CliResult("ok", argv, data, 0)  # noqa: E731
        verb = " ".join(args[:3])
        if verb == "agent onboarding create":
            if "create-timeout" in self.modes:
                return CliResult("timeout", argv)
            self.revision = 1
            return ok(self.onboarding("identity"))
        if verb == "agent onboarding update":
            self.revision += 1
            return ok(self.onboarding("review"))
        if verb == "agent onboarding preview":
            return ok({"data": self.preview()})
        if verb == "agent onboarding apply":
            if "apply-lost" in self.modes:
                raise Interrupt("process ended before apply reached the service")
            self.status, self.applied_revision = "running", self.revision
            if "apply-interrupt" in self.modes:
                raise Interrupt("process ended after apply was processed")
            if "apply-timeout" in self.modes:
                return CliResult("timeout", argv)
            return ok(self.applied())
        if verb == "agent onboarding get":
            if self.record is not None:
                return ok(self.record(self))
            return ok(self.applied() if self.status else self.onboarding("review"))
        if "managed-stop" in args:
            self.stops += 1
            self.stop_timeouts.append(self.timeout)
            if "stop-lost" in self.modes and self.stops == 1:
                raise Interrupt("process ended while the stop was in flight; not processed")
            if "never-stops" not in self.modes and not ("sticky" in self.modes
                                                         and self.stops == 1):
                self.status = "stopped"
            if "stop-interrupt" in self.modes and self.stops == 1:
                raise Interrupt("process ended after the stop was processed")
            return ok({"data": {"status": "stopping", "desired_lifecycle_state": "stopping",
                                "desired_egress_state": "muted", "control_generation": 2,
                                "confirmation": None}})
        if "status" in args:
            desired = "stopped" if self.status == "stopped" else (
                "stopping" if self.stops else "running")
            return ok({"data": {"id": DEP, "status": self.status, "name": self.name,
                                "desired_lifecycle_state": desired}})
        if "managed-get" in args:
            return ok({"data": self.detail()})
        if "managed-deployment-tools" in args:
            names = sorted(REVIEWED_TOOLS) + (["database_query"] if "extra-tool" in self.modes
                                             else [])
            return ok({"data": {"deployment_id": DEP, "tool_presets": ["live_data"],
                                "approval_policy": "read_only",
                                "allowed_publisher_operations": sorted(REVIEWED_OPERATIONS),
                                "tools": [{"name": n, "effective_policy": {"status": "allowed"}}
                                          for n in names]}})
        raise AssertionError("unexpected call: " + " ".join(args))

    def onboarding(self, step):
        return {"id": OID, "revision": self.revision, "step": step, "status": "draft",
                "entry_mode": "additional_employee", "draft": sf.draft(),
                "operations": {"deployment_id": None}}

    def preview(self):
        spec = copy.deepcopy(sf.draft()["deployment"])
        for memory in spec["memory_policy"].values():  # the service expands defaults
            memory.update(read_policy="explicit_tool", store="seren_managed")
        if "preview-config" in self.modes:
            spec["workload"]["config"] = {"seren_cloud_runtime": {}}
        tools = sorted(REVIEWED_TOOLS) + (["database_query"] if "preview-tool" in self.modes
                                          else [])
        return {"onboarding_id": OID, "source_revision": self.revision, "desired_spec": spec,
                "effective_policy": {"resolved_tools": tools, "tool_presets": ["live_data"],
                                     "approval_policy": "read_only",
                                     "allowed_publisher_operations": sorted(REVIEWED_OPERATIONS)}}

    def applied(self):
        return {"id": OID, "revision": self.revision + 1, "status": "waiting_for_provider",
                "application": {"deployment_outcome": "confirmed", "attempt": 1,
                                "stage": "readiness",
                                "source_revision": getattr(self, "applied_revision",
                                                           self.revision)},
                "operations": {"deployment_id": DEP}, "draft": sf.draft()}

    def detail(self):
        secrets = ["SEREN_INTERNAL_CLOUD_RUNTIME_DATABASE_URL"] + (
            ["OTHER_SECRET"] if "extra-secret" in self.modes else [])
        content = "other" if "old-instruction" in self.modes else fi.instruction()
        return {"deployment_id": DEP, "active_revision_id": REV, "name": sf.NAME,
                "tool_presets": ["live_data"], "approval_policy": "read_only",
                "max_iterations": 2, "max_tool_calls_per_run": 1, "max_timeout_seconds": 60,
                "private_output_policy": "control_plane", "status": self.status,
                "bundle": {"instructions": [{"kind": "skill", "content": content}]},
                "config": {"seren_cloud_runtime": {}}, "secret_keys": secrets}


class SuppliedFixtureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ticks = 0.0
        self.template = {"executable": "/nonexistent/seren", "executable_sha256": "0" * 64,
                         "config_home": str(self.root / "config"), "profile": "default",
                         "state_dir": str(self.root / "state"), "home": str(self.root / "runs"),
                         "type": "seren_cli", "timeout_seconds": 30,
                         "other_tools_available_acknowledged": True}
        self.proposal = sf.package(self.template)
        now = datetime.fromisoformat(self.wall())
        self.approval = {"approved": True, "authorization_id": AUTH,
                         "package_sha256": campaign.digest(self.proposal),
                         "issued_at": now.isoformat(),
                         "expires_at": (now + timedelta(seconds=sf.WINDOW)).isoformat()}
        self.campaign_calls = []

    # Fake time and the real executor --------------------------------------------------------
    def clock(self):
        return self.ticks

    def sleep(self, seconds):
        self.ticks += seconds

    def wall(self):
        return (datetime(2026, 10, 16, tzinfo=timezone.utc)
                + timedelta(seconds=self.ticks)).isoformat()

    def executor(self, wrong=()):
        """The real ClockRun and reviewer for each case, on the created deployment."""
        def run(session, case):
            employee_run.validate(session, case)
            key = case["employee_tool"]["catalog_case"]
            first, close = run_fixture()
            first["request"].update(deployment_id=DEP, message=catalog.message(key))
            data = first["response"]["data"]
            data["deployment_id"] = DEP
            data["invocation_payload"]["message"] = catalog.message(key)
            answer = answer_for(key)
            if key in wrong:
                answer["recommendation"] = "disqualify"
            data["output_events"] = finance_tests.events(json.dumps(answer))
            cli = FakeCli(first, close, session["baseline"])
            original_json, original_execute = cli.json, cli._execute

            def response(*args):
                self.ticks += 0.1
                result = original_json(*args)
                if result.ok and "managed-get" in args:
                    result.data["data"]["bundle"] = {"instructions": [
                        {"kind": "skill", "content": fi.instruction()}]}
                return result

            def execute(args):
                self.ticks += 0.1
                return original_execute(args)
            cli.json, cli._execute = response, execute
            store = Store(session["state_dir"])
            try:
                driver = employee_run.ClockRun(session, case, cli, store, self.clock,
                                               self.sleep, self.wall)
                driver.run()
                self.campaign_calls.extend(cli.calls)
                report = json.loads((Path(session["home"]) / driver.run_id /
                                     "report/report.json").read_text())
                return {"run_id": driver.run_id,
                        "checks": {c["id"]: c["verdict"] for c in report["criteria"]},
                        "cleanup_confirmed": driver.cleanup_id is not None
                        and not store.open_cleanup(run_id=driver.run_id)
                        and not store.employee_unresolved_actions(driver.key),
                        "service_invocations": sum(driver.counts.values())}
            finally:
                store.close()
        return run

    def no_campaign(self, session, case):
        raise AssertionError("no campaign case may run")

    def execute(self, *modes, runner=None, service=None):
        service = service or Service(modes)
        original = service.json

        def timed(*args):
            self.ticks += 0.1
            return original(*args)
        service.json = timed
        with mock.patch("subprocess.Popen", side_effect=AssertionError("network")), \
                mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            state = sf.execute(self.proposal, self.approval,
                               cli_factory=lambda _session, _sanitizer: service,
                               clock=self.clock, sleep=self.sleep, wall=self.wall,
                               runner=runner or self.no_campaign)
        return state, service

    def store(self):
        store = Store(self.template["state_dir"])
        self.addCleanup(store.close)
        return store

    def verbs(self, service):
        return [" ".join(c[:3]) if c[1] == "onboarding" else c[1] for c in service.calls]

    # Successful creation and campaign -------------------------------------------------------
    def test_created_fixture_runs_the_unchanged_reliability_campaign(self):
        state, service = self.execute(runner=self.executor())
        self.assertEqual("complete", state["status"], state.get("reason"))
        self.assertEqual(["agent onboarding create", "agent onboarding update",
                          "agent onboarding preview", "agent onboarding apply",
                          "managed-stop", "cloud", "managed-get", "managed-deployment-tools"],
                         self.verbs(service))
        self.assertEqual(list(catalog.case_ids("bd-reliability-v1")),
                         [r["case"] for r in state["campaign"]["results"]])
        self.assertEqual(3, sum("--async" in a for a in self.campaign_calls))
        self.assertEqual(3, sum("managed-stop" in a for a in self.campaign_calls))
        sent = [a[a.index("--message") + 1] for a in self.campaign_calls if "--async" in a]
        self.assertEqual([catalog.message(k) for k in catalog.case_ids("bd-reliability-v1")], sent)
        self.assertTrue(all(DEP in a for a in self.campaign_calls if len(a) > 2))
        self.assertFalse(self.store().open_cleanup())
        session = state["session"]
        self.assertEqual((DEP, sf.NAME), (session["baseline"]["deployment_id"],
                                          session["baseline"]["name"]))
        # Keys derive from this authorization; a rerun never repeats creation or a case.
        create = next(c for c in service.calls if c[:3] == ["agent", "onboarding", "create"])
        self.assertEqual(sf.keys(AUTH)["create"], create[create.index("--idempotency-key") + 1])
        again, second = self.execute(runner=self.executor())
        self.assertEqual("complete", again["status"])
        self.assertEqual([], second.calls)

    def test_first_case_failure_stops_later_cases_after_shutdown(self):
        state, _ = self.execute(runner=self.executor(wrong=("bd-unavailable-source",)))
        self.assertEqual("stopped", state["status"])
        self.assertEqual("stopped", state["campaign"]["status"])
        self.assertEqual(["bd-unavailable-source"], [r["case"] for r in state["campaign"]["results"]])
        self.assertEqual(1, sum("--async" in a for a in self.campaign_calls))
        self.assertEqual(1, sum("managed-stop" in a for a in self.campaign_calls))
        self.assertFalse(self.store().open_cleanup())

    def test_bootstrap_gets_one_recovery_stop_only_after_two_running_reads(self):
        state, service = self.execute("sticky", runner=self.executor())
        self.assertEqual("complete", state["status"], state.get("reason"))
        verbs = self.verbs(service)
        self.assertEqual(2, verbs.count("managed-stop"))
        between = verbs[verbs.index("managed-stop") + 1:len(verbs) - verbs[::-1].index("managed-stop") - 1]
        self.assertEqual(["cloud", "cloud"], between)

    # Creation uncertainty and gates ---------------------------------------------------------
    def test_preview_differences_stop_before_apply(self):
        for mode in ("preview-config", "preview-tool"):
            with self.subTest(mode=mode):
                self.setUp()
                state, service = self.execute(mode)
                self.assertEqual("stopped", state["status"])
                self.assertNotIn("agent onboarding apply", self.verbs(service))
                self.assertNotIn("deployment_id", state)

    def test_uncertain_create_is_never_repeated_or_handed_off(self):
        state, service = self.execute("create-timeout")
        self.assertEqual("stopped", state["status"])
        self.assertIn("outcome unknown", state["reason"])
        self.assertEqual(["agent onboarding create"], self.verbs(service))
        again, second = self.execute()
        self.assertEqual("stopped", again["status"])
        self.assertEqual([], second.calls)

    def test_unknown_apply_recovers_the_id_stops_it_and_never_hands_off(self):
        state, service = self.execute("apply-timeout")
        self.assertEqual("stopped", state["status"])
        verbs = self.verbs(service)
        self.assertEqual(1, verbs.count("agent onboarding apply"))
        self.assertEqual(1, verbs.count("agent onboarding get"))
        self.assertEqual(1, verbs.count("managed-stop"))
        self.assertEqual("done", state["cleanup"]["status"])
        self.assertNotIn("session", state)
        self.assertEqual([], self.execute()[1].calls)

    def test_unconfirmed_bootstrap_shutdown_keeps_the_new_fixture_blocked(self):
        store = self.store()
        clock_run = store.create_run("employee_supplied_answer", "clock:" + CLOCK, "live",
                                     "c0000000-0000-4000-8000-000000000000", "test")
        store.add_cleanup(clock_run, "clock:" + CLOCK, "stop_runtime", CLOCK)
        state, service = self.execute("never-stops")
        self.assertEqual("unresolved", state["status"])
        self.assertIn("charges may continue", state["next_action"])
        self.assertEqual(2, self.verbs(service).count("managed-stop"))
        self.assertEqual(["pending"], [c["status"] for c in store.open_cleanup("clock:" + DEP)])
        # Clock Test's own obligation is untouched and authorizes nothing here.
        self.assertEqual(["pending"], [c["status"] for c in store.open_cleanup("clock:" + CLOCK)])

    def test_unexpected_settings_or_instruction_stop_handoff_after_shutdown(self):
        for mode in ("extra-secret", "old-instruction", "extra-tool"):
            with self.subTest(mode=mode):
                self.setUp()
                state, service = self.execute(mode)
                self.assertEqual("stopped", state["status"])
                self.assertEqual("done", state["cleanup"]["status"])
                self.assertNotIn("session", state)

    def test_authorization_must_bind_this_package_and_window(self):
        for change in ({"package_sha256": "0" * 64}, {"approved": False},
                       {"expires_at": (datetime.fromisoformat(self.approval["issued_at"])
                                       + timedelta(seconds=sf.WINDOW + 1)).isoformat()}):
            with self.subTest(change=change):
                approval = dict(self.approval, **change)
                with self.assertRaises(ValueError):
                    sf.execute(self.proposal, approval, cli_factory=self.no_campaign,
                               wall=self.wall)
        changed = dict(self.proposal, cases=self.proposal["cases"][:1])
        with self.assertRaises(ValueError):
            sf.authorize(changed, self.approval, datetime.fromisoformat(self.wall()))

    # Process interruption and resume -------------------------------------------------------
    def journal(self):
        return json.loads((Path(self.template["state_dir"]) / "fixture-creations" /
                           (AUTH + ".json")).read_text())

    def obligations(self):
        store = self.store()
        return [c["status"] for r in store.runs() if r["journey"] == sf.JOURNEY
                for c in store.cleanup_items(r["id"])]

    def interrupt(self, *modes, at=None):
        """Run until the process is interrupted at an exact point; the journal must still
        say `creating` (nothing recorded the interruption)."""
        service = Service(modes)
        fired = []
        if at is None:
            with self.assertRaises(Interrupt):
                self.execute(service=service)
        else:
            name, kind = at
            original = getattr(sf.Creation, name)

            def once(creation, *args):
                if not fired and (kind is None or args[0] == kind):
                    fired.append(name)
                    raise Interrupt(name)
                return original(creation, *args)
            with mock.patch.object(sf.Creation, name, once), self.assertRaises(Interrupt):
                self.execute(service=service)
        self.assertEqual("creating", self.journal()["status"])
        return service

    def resume(self, service, *keep):
        service.modes &= set(keep)
        before = len(service.calls)
        state, _ = self.execute(service=service)
        return state, self.verbs(service)[before:]

    def test_apply_processed_then_interrupted_recovers_only_this_attempts_deployment(self):
        service = self.interrupt("apply-interrupt")
        journal = self.journal()
        self.assertEqual("intent", journal["apply"])
        self.assertIsNone(journal["intents"][-1]["outcome"])  # sent, result never recorded
        self.assertEqual([], self.obligations())
        state, calls = self.resume(service)
        self.assertEqual("stopped", state["status"])
        self.assertIn("never handed", state["reason"])
        self.assertEqual(["agent onboarding get", "cloud", "managed-stop", "cloud", "managed-get"],
                         calls)
        self.assertEqual(1, self.verbs(service).count("agent onboarding create"))
        self.assertEqual(1, self.verbs(service).count("agent onboarding apply"))
        self.assertEqual(["done"], self.obligations())
        self.assertNotIn("session", state)
        self.assertEqual([], self.resume(service)[1])  # finished: nothing more, ever

    def test_apply_lost_in_flight_leaves_an_explicit_unresolved_record(self):
        service = self.interrupt("apply-lost")
        state, calls = self.resume(service)
        self.assertEqual("unresolved", state["status"])
        self.assertEqual(["agent onboarding get"] * 4, calls)
        self.assertIn("a runtime may exist", state["next_action"])
        self.assertEqual([], self.obligations())
        again, calls = self.resume(service)  # counts are not reset on resume
        self.assertEqual(("unresolved", []), (again["status"], calls))

    def test_interrupted_before_cleanup_registration_registers_it_before_any_stop(self):
        service = self.interrupt(at=("own", None))
        self.assertEqual(("confirmed", DEP), (self.journal()["apply"],
                                              self.journal()["deployment_id"]))
        self.assertEqual([], self.obligations())
        state, calls = self.resume(service)
        self.assertEqual(["cloud", "managed-stop", "cloud", "managed-get"], calls)
        self.assertEqual(("stopped", ["done"]), (state["status"], self.obligations()))

    def test_interrupted_after_registration_reuses_the_one_obligation(self):
        service = self.interrupt(at=("mutate", "managed_stop"))
        self.assertEqual(["pending"], self.obligations())
        self.assertFalse([i for i in self.journal()["intents"] if i["kind"] == "managed_stop"])
        state, calls = self.resume(service)
        self.assertEqual(["cloud", "managed-stop", "cloud", "managed-get"], calls)
        self.assertEqual(("stopped", ["done"]), (state["status"], self.obligations()))

    def test_stop_processed_then_interrupted_is_observed_never_resent(self):
        service = self.interrupt("stop-interrupt")
        state, calls = self.resume(service)
        self.assertEqual(["cloud", "managed-get"], calls)
        self.assertEqual(1, self.verbs(service).count("managed-stop"))
        self.assertEqual(("stopped", ["done"]), (state["status"], self.obligations()))

    def test_stop_lost_in_flight_is_never_resent_and_keeps_the_fixture_blocked(self):
        service = self.interrupt("stop-lost")
        state, calls = self.resume(service)
        self.assertEqual("unresolved", state["status"])
        self.assertNotIn("managed-stop", calls)
        self.assertEqual(["cloud"] * 8, calls)  # the original status budget, not a fresh one
        self.assertEqual(["pending"], self.obligations())
        store = self.store()
        self.assertTrue(store.employee_unresolved_actions("clock:" + DEP))
        self.assertEqual([], self.resume(service)[1])

    def at_bootstrap_offset(self, seconds):
        """Move fake time to the persisted bootstrap deadline plus `seconds`."""
        deadline = datetime.fromisoformat(self.journal()["bootstrap_deadline_at"])
        self.ticks = (deadline - datetime(2026, 10, 16, tzinfo=timezone.utc)).total_seconds() \
            + seconds
        return deadline

    def stop_budgets(self, deadline):
        """(timeout given to each stop, bootstrap seconds left minus the read-back reserve)."""
        stops = [i for i in self.journal()["intents"] if i["kind"] == "managed_stop"]
        return [(t, (deadline - datetime.fromisoformat(i["at"])).total_seconds() - 30)
                for t, i in zip(self.service_timeouts, stops)]

    def test_resume_after_the_bootstrap_budget_sends_no_stop(self):
        service = self.interrupt(at=("mutate", "managed_stop"))  # deployment may exist
        self.at_bootstrap_offset(10)
        journal = self.journal()
        self.assertLess(self.wall(), journal["deadline_at"])                  # creation open
        self.assertLess(self.wall(), self.approval["expires_at"])            # authority open
        counts = dict(journal["counts"])
        state, calls = self.resume(service)
        self.assertEqual("unresolved", state["status"])
        self.assertIn("no stop sent", state["reason"])
        self.assertIn("runtime and charges may continue", state["next_action"])
        self.assertNotIn("managed-stop", self.verbs(service))
        self.assertEqual(["pending"], self.obligations())
        self.assertNotIn("session", state)
        self.assertEqual([], self.campaign_calls)
        self.assertTrue(all(state["counts"][k] >= v for k, v in counts.items()))
        again, calls = self.resume(service)  # still no stop and no fresh budget
        self.assertEqual("unresolved", again["status"])
        self.assertNotIn("managed-stop", self.verbs(service))

    def test_stops_are_bounded_by_the_remaining_bootstrap_budget(self):
        # Late first stop, then the conditional recovery stop, both inside the budget.
        service = self.interrupt("sticky", at=("mutate", "managed_stop"))
        deadline = self.at_bootstrap_offset(-80)
        state, calls = self.resume(service, "sticky")
        self.service_timeouts = service.stop_timeouts
        budgets = self.stop_budgets(deadline)
        self.assertEqual(2, len(budgets))
        # Intent times are stored to the microsecond.
        self.assertTrue(all(0 < t <= left + 1e-6 for t, left in budgets), budgets)
        self.assertLess(budgets[1][0], 30)  # the recovery stop got only what was left
        self.assertEqual(("stopped", ["done"]), (state["status"], self.obligations()))
        # Too close to the deadline for a recovery stop: none is sent; unresolved.
        self.setUp()
        service = self.interrupt("sticky", at=("mutate", "managed_stop"))
        deadline = self.at_bootstrap_offset(-50)
        state, _ = self.resume(service, "sticky")
        self.service_timeouts = service.stop_timeouts
        budgets = self.stop_budgets(deadline)
        self.assertEqual(1, len(budgets))
        self.assertTrue(0 < budgets[0][0] <= budgets[0][1] + 1e-6 < 30, budgets)
        self.assertEqual(("unresolved", ["pending"]), (state["status"], self.obligations()))

    def test_recovery_refuses_unowned_or_contradictory_identity_without_any_stop(self):
        def record(change):
            def build(service):
                value = service.applied()
                change(value)
                return value
            return build
        variants = {
            "other onboarding": lambda r: r.update(id=OTHER),
            "no deployment": lambda r: r["operations"].pop("deployment_id"),
            "malformed deployment": lambda r: r["operations"].update(deployment_id="dep-1"),
            "other attempt's revision": lambda r: r["application"].update(source_revision=99),
            "no application": lambda r: r.pop("application"),
            "other fixture": lambda r: r["draft"]["deployment"].update(name="Other employee"),
        }
        for label, change in variants.items():
            with self.subTest(label):
                self.setUp()
                service = self.interrupt("apply-interrupt")
                service.record = record(change)
                state, calls = self.resume(service)
                self.assertEqual("unresolved", state["status"])
                self.assertEqual(["agent onboarding get"] * 4, calls)
                self.assertEqual([], self.obligations())
        self.setUp()
        service = self.interrupt("apply-interrupt")
        service.name = "Other employee"  # linkage valid, but the deployment is not this fixture
        state, calls = self.resume(service)
        self.assertEqual(("unresolved", ["agent onboarding get", "cloud"]),
                         (state["status"], calls))
        self.assertEqual([], self.obligations())

    def test_resume_bindings_deadlines_and_authority_are_never_reset(self):
        service = self.interrupt("apply-interrupt")
        path = (Path(self.template["state_dir"]) / "fixture-creations" / (AUTH + ".json"))
        saved = path.read_text()
        shorter = dict(self.approval, expires_at=(datetime.fromisoformat(
            self.approval["expires_at"]) - timedelta(seconds=1)).isoformat())
        before = len(service.calls)
        with self.assertRaises(ValueError):
            sf.execute(self.proposal, shorter, cli_factory=lambda *_: service, wall=self.wall)
        tampered = json.loads(saved)
        tampered["binding"] = "0" * 64
        path.write_text(json.dumps(tampered))
        with self.assertRaises(ValueError):
            self.execute(service=service)
        self.assertEqual(before, len(service.calls))
        path.write_text(saved)
        self.ticks += sf.CREATION_WINDOW  # past the creation deadline, inside the window
        state, calls = self.resume(service)
        self.assertEqual(("unresolved", []), (state["status"], calls))
        self.assertIn("deadline passed", state["reason"])
        path.write_text(saved)
        self.ticks += sf.WINDOW  # past the authorization itself
        state, calls = self.resume(service)
        self.assertEqual(("unresolved", []), (state["status"], calls))
        self.assertIn("authorization expired", state["reason"])

    # Binding: substitutions and mismatches are refused --------------------------------------
    def created(self):
        state, _ = self.execute(runner=lambda s, c: {"checks": {}})
        session = state["session"]
        return session, json.loads(Path(session["setup_receipt"]).read_text())

    def rebind(self, session, basis):
        path = Path(session["setup_receipt"])
        path.write_text(json.dumps(basis))
        session["setup_receipt_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

    def test_created_binding_accepts_its_cases_and_refuses_substitutions(self):
        session, basis = self.created()
        for key in catalog.case_ids("bd-reliability-v1"):
            employee_run.validate(session, catalog.case(key, DEP))

        def refused(session=session, case=None):
            with self.assertRaises((ValueError, KeyError)):
                employee_run.validate(session, case or catalog.case("bd-unavailable-source", DEP))
        refused(case=catalog.case("bd-unavailable-source"))                 # Clock target
        refused(case=catalog.case("bd-unavailable-source", OTHER))          # other target
        refused(dict(session, creation_authorization_id=OTHER))              # other attempt
        refused(dict(session, state_dir=str(self.root / "other-state")))     # other store
        refused(dict(session, setup_receipt_sha256="0" * 64))               # receipt changed
        for field, value in (("active_revision_id", OTHER), ("max_tool_calls_per_run", 2),
                             ("name", employee_run.CLOCK_NAME), ("deployment_id", OTHER)):
            with self.subTest(field=field):
                baseline = dict(session["baseline"], **{field: value})
                case = catalog.case("bd-unavailable-source", baseline["deployment_id"])
                refused(dict(session, baseline=baseline), case)

    def test_receipt_contents_bind_policy_instruction_and_this_creation(self):
        session, basis = self.created()
        changes = {
            "policy": lambda b: b["effective_policy"]["resolved_tools"].append("database_query"),
            "instruction": lambda b: b["creation"].update(instruction_sha256="0" * 64),
            "draft": lambda b: b["creation"].update(draft_sha256="0" * 64),
            "route": lambda b: b.update(setup_route="created-other"),
            "legacy-chain": lambda b: b.pop("setup_route"),
            "creation-receipt": lambda b: b["creation"]["apply_receipt"].update(sha256="0" * 64),
            "catalog": lambda b: b["creation"]["catalog_receipt"].update(
                path=b["creation"]["apply_receipt"]["path"],
                sha256=b["creation"]["apply_receipt"]["sha256"]),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                tampered = copy.deepcopy(basis)
                change(tampered)
                bound = dict(session)
                self.rebind(bound, tampered)
                with self.assertRaises((ValueError, KeyError)):
                    employee_run.validate(bound, catalog.case("bd-unavailable-source", DEP))
                self.rebind(bound, basis)

    def test_same_name_employee_without_this_creation_is_refused(self):
        session, basis = self.created()
        # Another deployment with the approved name and identical fields, but not the one
        # this authorization's apply returned.
        detail = json.loads(Path(basis["tool_refs_evidence"]["detail"]["path"]).read_text())
        detail["response"]["data"]["deployment_id"] = OTHER
        path = self.root / "other-detail.json"
        path.write_text(json.dumps(detail))
        tampered = copy.deepcopy(basis)
        tampered.update(deployment_id=OTHER)
        tampered["tool_refs_evidence"]["detail"] = {
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        baseline = dict(session["baseline"], deployment_id=OTHER)
        bound = dict(session, baseline=baseline)
        self.rebind(bound, tampered)
        with self.assertRaises(ValueError):
            employee_run.validate(bound, catalog.case("bd-unavailable-source", OTHER))

    def test_open_cleanup_on_the_fixture_blocks_its_campaign_without_any_start(self):
        session, _ = self.created()
        store = self.store()
        run = store.create_run("employee_supplied_answer", "clock:" + DEP, "live",
                               "c1111111-1111-4111-8111-111111111111", "test")
        store.add_cleanup(run, "clock:" + DEP, "stop_runtime", DEP)
        bound = dict(session, authorization_id=OTHER, runtime_remaining_seconds=300,
                     expires_at=(datetime.fromisoformat(self.wall())
                                 + timedelta(seconds=420)).isoformat())
        with self.assertRaisesRegex(employee_run.Halt, "prior session/cleanup"):
            self.executor()(bound, catalog.case("bd-unavailable-source", DEP))
        self.assertEqual([], self.campaign_calls)
        self.assertEqual(["pending"], [c["status"] for c in store.open_cleanup("clock:" + DEP)])

    def test_legacy_names_and_routes_cannot_be_mixed(self):
        session, basis = self.created()
        legacy = dict(session, baseline=dict(session["baseline"], name=employee_run.CLOCK_NAME))
        with self.assertRaises(ValueError):
            employee_run.validate(legacy, catalog.case("bd-unavailable-source", DEP))
        lookup = copy.deepcopy(catalog.case("bd-unavailable-source", DEP))
        lookup["employee_tool"]["result_type"] = "utc_time"
        with self.assertRaises((ValueError, KeyError)):
            employee_run.validate(session, lookup)

    # Shipped inputs and offline preparation -------------------------------------------------
    def test_draft_installs_the_supplied_data_instruction_and_no_tools(self):
        value = sf.verify_draft()
        self.assertEqual(fi.instruction(),
                         value["deployment"]["workload"]["execution"]["bundle"]["instructions"][0]
                         ["content"])
        unbound = json.loads((fi.FIXTURE.parent / "system-time" /
                              "unbound-onboarding-draft.json").read_text())
        for key in ("mode", "template", "approval_policy", "model_policy", "memory_policy",
                    "capability_policy", "private_output_policy"):
            self.assertEqual(unbound["deployment"][key], value["deployment"][key], key)
        for change in (lambda d: d["deployment"]["tool_presets"].append("live_data"),
                       lambda d: d["deployment"]["workload"]["config"].update(x=1),
                       lambda d: d["deployment"]["workload"]["execution"]["bundle"]
                       ["instructions"][0].update(content="other")):
            tampered = copy.deepcopy(value)
            change(tampered)
            with self.assertRaises(ValueError):
                sf.verify_draft(tampered)

    def test_prepare_is_offline_and_binds_the_unchanged_campaign(self):
        path, output = self.root / "template.json", self.root / "package.json"
        path.write_text(json.dumps(self.template))
        with mock.patch.object(employee_run, "pinned_cli", side_effect=AssertionError("live")):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(0, main(["supplied-fixture", "prepare", "--template", str(path),
                                          "--output", str(output)]))
        proposal = json.loads(output.read_text())
        self.assertEqual(self.proposal, proposal)
        self.assertEqual(catalog.manifest("bd-reliability-v1"), proposal["catalog"])
        self.assertEqual(campaign.package({}, "bd-reliability-v1")["limits"],
                         proposal["limits"]["campaign"])
        with self.assertRaises(ValueError):
            sf.package(dict(self.template, baseline={}))


if __name__ == "__main__":
    unittest.main()
