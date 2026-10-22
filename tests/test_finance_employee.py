"""Supplied-answer invoice case through the existing employee executor.

SYNTHETIC: the branch-read fixture chain (fake CLI, fake clock, actual journal, store and
reviewer) plus a synthetic instruction-update journal. Sockets and subprocesses are
blocked; no Seren call, model or real lifecycle wait. A pass here verifies the harness
path, not the employee.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import branch_read, employee_tool, execution_limits, finance_invoices as fi
from seren_harness.cli import main
from seren_harness.config import load
from seren_harness.employee_run import Halt, validate
import test_branch_read as branch_tests
import test_instruction_update as iu

CASE = json.loads((SOURCE / "cases/seren-employee-finance-invoices.json").read_text())
EXPECTED = (SOURCE / "docs/finance-invoices/expected.json").read_text()
NEW_REVISION = "99999999-9999-4999-8999-999999999999"


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def events(text=EXPECTED, done="ok", extra=()):
    return [*extra, {"type": "text", "text": text}, {"type": "done", "status": done}]


class FinanceRunTests(unittest.TestCase):
    def setUp(self):
        self.parent = branch_tests.BranchReadTests(
            "test_data_operation_matches_both_supported_shapes_and_exact_answer")
        self.parent.setUp()
        self.addCleanup(self.parent.doCleanups)
        self.f = f = self.parent.f
        target = f.baseline["deployment_id"]
        # The verified branch chain ends at the recorded pre-update revision.
        branch_revision = json.loads(fi.PACKAGE.read_text())["baseline"]["active_revision_id"]
        f.baseline["active_revision_id"] = branch_revision
        self.parent.detail["active_revision_id"] = branch_revision
        p = f.root / "branch-post-update.json"
        p.write_text(json.dumps({"status": "ok", "exit_code": 0,
                                 "response": {"data": self.parent.detail}}))
        f.basis["branch_read_update"]["post_update_detail"]["sha256"] = sha(p.read_bytes())
        f.basis["branch_baseline"] = copy.deepcopy(f.baseline)
        # Synthetic fixtures use another deployment ID: the package copy differs only there.
        package = json.loads(fi.PACKAGE.read_text())
        package["target"]["deployment_id"] = target
        self.package = f.root / "instruction-update.proposed.json"
        self.package.write_text(json.dumps(package))
        for name, value in (("TARGET", target), ("PACKAGE", self.package)):
            patcher = mock.patch.object(fi, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        f.baseline["active_revision_id"] = f.basis["active_revision_id"] = NEW_REVISION
        f.session["execution_scope"] = f.basis["execution_scope"] = fi.SCOPE
        self.journal = {
            "package": fi.PACKAGE_ID, "deployment": target, "result": "proceed",
            "binding": {"deployment_id": target, "package_sha256": sha(self.package.read_bytes()),
                        "corrected_trimmed_sha256": sha(fi.instruction())},
            "observations": {"prerequisite": {"active_revision_id": branch_revision},
                             "read_back": {"deployment_id": target,
                                           "active_revision_id": NEW_REVISION,
                                           "status": "stopped",
                                           "instruction_trimmed_sha256": sha(fi.instruction())}},
            "update": {"status": "ok", "outcome": "applied_verified"}}
        self.write_journal()
        f.case = copy.deepcopy(CASE)
        f.case["target_status"] = "resolved"
        f.case["employee_tool"]["deployment_id"] = target
        f.first["request"]["message"] = fi.message()
        run = f.first["response"]["data"]
        run["invocation_payload"]["message"] = fi.message()
        run["output_events"] = events()

    def write_journal(self):
        j = self.f.root / "instruction-update-journal.json"
        j.write_text(json.dumps(self.journal))
        self.f.basis["instruction_update"] = {"journal": {"path": str(j), "sha256": sha(j.read_bytes())}}
        self.f.write_basis()

    def driver(self, mode="finance", trace=None, prompt=None, run_status="completed"):
        driver, cli = self.f.driver(mode)
        original = cli.json

        def response(*args):
            result = original(*args)
            if not result.ok:
                return result
            data = result.data["data"]
            if "managed-get" in args:
                data["bundle"] = {"instructions": [
                    {"kind": "skill", "content": fi.instruction() if prompt is None else prompt}]}
            elif "run" in args and "get" in args:
                data["status"] = run_status
                if trace is not None:
                    data["output_events"] = trace
            return result

        cli.json = response
        return driver, cli

    def report(self, driver):
        return json.loads((self.f.root / driver.run_id / "report/report.json").read_text())

    def verdicts(self, driver):
        return {c["id"]: c["verdict"] for c in self.report(driver)["criteria"]}

    def fresh(self, name):
        self.f.session["authorization_id"] = {
            "a": "a1111111-1111-4111-8111-111111111111",
            "b": "b1111111-1111-4111-8111-111111111111",
            "c": "c1111111-1111-4111-8111-111111111111",
            "d": "d1111111-1111-4111-8111-111111111111",
            "e": "e1111111-1111-4111-8111-111111111111"}[name]

    # Handoff and answer collection ------------------------------------------------------
    def test_valid_handoff_sends_the_frozen_task_once_and_scores_the_full_answer(self):
        validate(self.f.session, self.f.case)
        driver, cli = self.driver()
        self.assertEqual(self.f.execute(driver), 0)
        invocations = [a for a in cli.calls if "--async" in a]
        self.assertEqual(len(invocations), 1)
        sent = invocations[0][invocations[0].index("--message") + 1]
        self.assertEqual(sent, (SOURCE / "docs/finance-invoices/task-input.txt").read_text().strip())
        for value in ("1800.00", "4300.25", "2500.25", "500.00", "100.00"):
            self.assertNotIn(value, sent)
            self.assertNotIn(value, fi.instruction())
        self.assertEqual(sum("managed-stop" in a for a in cli.calls), 1)
        self.assertFalse(self.f.store.open_cleanup())
        self.assertEqual(self.verdicts(driver), {"run_identity": "passed", "answer": "passed",
                                                 "no_tool_activity": "passed",
                                                 "shutdown": "passed"})
        answer = next(c for c in self.report(driver)["criteria"] if c["id"] == "answer")
        self.assertEqual(answer["evidence"]["outcome"], "passed")
        self.assertLessEqual(self.f.ticks, 300)
        # A consumed session is never reused for a fresh run.
        with self.assertRaises(Halt):
            self.f.execute(self.driver()[0])

    def test_end_to_end_setup_route_handoff_run_answer_and_cleanup(self):
        """Actual setup route (clock config shape, stand-in CLI) -> its real journal bound
        into the receipt chain -> one executor run -> scored answer -> confirmed stop."""
        f = self.f
        mock.patch.stopall()  # the branch fixture blocks subprocesses; the stand-in needs them
        for name, value in (("TARGET", f.baseline["deployment_id"]), ("PACKAGE", self.package)):
            patcher = mock.patch.object(fi, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        previous = f.basis["branch_baseline"]["active_revision_id"]
        code, journal, _, output = clock_setup(f.root / "setup", fi.TARGET, previous, fi.PACKAGE)
        self.assertEqual(code, iu.instruction_update.PROCEED, output)
        new = json.loads(journal.read_text())["observations"]["read_back"]["active_revision_id"]
        self.assertNotEqual(new, previous)
        f.baseline["active_revision_id"] = f.basis["active_revision_id"] = new
        f.basis["instruction_update"] = {"journal": {"path": str(journal),
                                                     "sha256": sha(journal.read_bytes())}}
        f.write_basis()
        validate(f.session, f.case)
        driver, cli = self.driver()
        self.assertEqual(f.execute(driver), 0)
        self.assertEqual(sum("--async" in a for a in cli.calls), 1)
        self.assertEqual(sum("managed-stop" in a for a in cli.calls), 1)
        self.assertFalse(f.store.open_cleanup())
        self.assertEqual(set(self.verdicts(driver).values()), {"passed"})
        self.assertLessEqual(sum(driver.counts.values()), 31)

    def test_answer_split_across_text_events_is_joined_not_trimmed_to_fit(self):
        head, tail = EXPECTED[:40], EXPECTED[40:]
        trace = [{"type": "text", "text": head}, {"type": "thinking", "text": "planning"},
                 {"type": "text", "text": tail}, {"type": "done", "status": "ok"}]
        driver, _ = self.driver(trace=trace)
        self.assertEqual(self.f.execute(driver), 0)
        self.fresh("a")
        trace = [{"type": "text", "text": "Here you go:\n"},
                 {"type": "text", "text": EXPECTED}, {"type": "done", "status": "ok"}]
        driver, _ = self.driver(trace=trace)
        self.assertEqual(self.f.execute(driver), 1)
        answer = next(c for c in self.report(driver)["criteria"] if c["id"] == "answer")
        self.assertEqual(answer["evidence"]["outcome"], "malformed")

    def test_wrong_malformed_and_empty_answers_fail_unfinished_run_is_incomplete(self):
        wrong = json.loads(EXPECTED)
        wrong["overdue_count"] = 4
        for name, trace, code, outcome in (
                ("a", events(json.dumps(wrong)), 1, "failed"),
                ("b", events("The overdue invoices are INV-001, INV-002 and INV-007."), 1,
                 "malformed"),
                ("c", [{"type": "done", "status": "ok"}], 1, "no_answer"),
                ("d", events(done="error"), 2, None)):
            with self.subTest(name=name):
                self.fresh(name)
                driver, cli = self.driver(trace=trace)
                self.assertEqual(self.f.execute(driver), code)
                answer = next(c for c in self.report(driver)["criteria"] if c["id"] == "answer")
                self.assertEqual(answer["evidence"].get("outcome"), outcome)
                self.assertEqual(sum("--async" in a for a in cli.calls), 1)
                self.assertFalse(self.f.store.open_cleanup())

    def test_tool_activity_fails_separately_even_with_a_correct_answer(self):
        call = {"type": "tool_call", "id": "c1", "name": "seren_schedule_run", "arguments": "{}"}
        result = {"type": "tool_result", "id": "c1", "is_error": False, "content": "scheduled"}
        driver, _ = self.driver(trace=events(extra=(call, result)))
        self.assertEqual(self.f.execute(driver), 1)
        self.assertEqual(self.verdicts(driver)["answer"], "passed")
        self.assertEqual(self.verdicts(driver)["no_tool_activity"], "failed")
        activity = next(c for c in self.report(driver)["criteria"]
                        if c["id"] == "no_tool_activity")
        # Saved evidence is sanitized: `seren_*` names are redacted, counts are kept.
        self.assertEqual(activity["evidence"]["events"], {"tool_call": 1, "tool_result": 1})
        self.assertEqual(len(activity["evidence"]["tools"]), 1)
        self.assertEqual(self.report(driver)["verdict"], "failed")

    # Setup evidence and binding -----------------------------------------------------------
    def test_missing_altered_wrong_target_or_revision_setup_is_refused_before_start(self):
        f = self.f
        validate(f.session, f.case)
        changes = {
            "missing": lambda j, b: b.pop("instruction_update"),
            "stopped": lambda j, b: j.update(result="stopped"),
            "unknown-update": lambda j, b: j["update"].update(outcome="unknown"),
            "wrong-target": lambda j, b: j.update(deployment="88888888-8888-4888-8888-888888888888"),
            "wrong-revision": lambda j, b: j["observations"]["read_back"].update(
                active_revision_id="88888888-8888-4888-8888-888888888888"),
            "running": lambda j, b: j["observations"]["read_back"].update(status="running"),
            "other-instruction": lambda j, b: j["observations"]["read_back"].update(
                instruction_trimmed_sha256=sha(branch_read.prompt(
                    branch_tests.CASE["employee_tool"]))),
            "other-package": lambda j, b: j["binding"].update(package_sha256="0" * 64),
            "wrong-prior": lambda j, b: j["observations"]["prerequisite"].update(
                active_revision_id=NEW_REVISION),
            "controls-changed": lambda j, b: b["branch_baseline"].update(max_iterations=3),
        }
        original_journal, original_basis = copy.deepcopy(self.journal), copy.deepcopy(f.basis)
        for name, change in changes.items():
            with self.subTest(name=name):
                self.journal = copy.deepcopy(original_journal)
                f.basis = copy.deepcopy(original_basis)
                change(self.journal, f.basis)
                if name == "missing":
                    f.write_basis()
                else:
                    self.write_journal()
                with self.assertRaises((KeyError, ValueError)):
                    validate(f.session, f.case)
        self.journal, f.basis = original_journal, original_basis
        self.write_journal()
        validate(f.session, f.case)
        # A tampered journal after binding changes its digest.
        path = f.root / "instruction-update-journal.json"
        path.write_text(path.read_text().replace("applied_verified", "applied_verified "))
        with self.assertRaises(ValueError):
            validate(f.session, f.case)

    def test_refused_setup_makes_no_service_call_through_the_command(self):
        f = self.f
        self.journal["result"] = "stopped"
        self.write_journal()
        cp, sp = f.root / "case.json", f.root / "session.json"
        cp.write_text(json.dumps(f.case))
        f.session["case"] = str(cp)
        sp.write_text(json.dumps(f.session))
        _, cli = self.driver()
        with mock.patch("seren_harness.employee_run.pinned_cli", return_value=cli), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["employee-data", "--session", str(sp)]), 3)
        self.assertEqual(cli.calls, [])
        self.assertIn("No replay", out.getvalue())

    def test_wrong_instruction_session_binding_or_case_is_refused(self):
        f = self.f
        driver, cli = self.driver(prompt=branch_read.prompt(branch_tests.CASE["employee_tool"]))
        self.assertEqual(f.execute(driver), 2)
        self.assertFalse(any("managed-start" in a for a in cli.calls))
        self.fresh("a")
        for change in (lambda s, c: s.update(execution_scope=branch_read.SCOPE),
                       lambda s, c: s.update(runtime_remaining_seconds=600),
                       lambda s, c: s["baseline"].update(max_tool_calls_per_run=2),
                       lambda s, c: c["employee_tool"].update(result_type="text_answer"),
                       lambda s, c: c["employee_tool"].update(message=fi.message() + " Hint: 3."),
                       lambda s, c: c["employee_tool"].update(answer="1800.00"),
                       lambda s, c: c["criteria"].pop(2)):
            session, case = copy.deepcopy(f.session), copy.deepcopy(f.case)
            change(session, case)
            with self.assertRaises((KeyError, ValueError)):
                validate(session, case)
        tool_case = copy.deepcopy(branch_tests.CASE)
        tool_case["criteria"][2]["check"] = "employee_supplied_answer"
        with self.assertRaises(ValueError):
            employee_tool.validate(tool_case)

    # Runtime and cleanup -------------------------------------------------------------------
    def uncertain(self, mode, invokes):
        driver, cli = self.driver(mode)
        self.assertEqual(self.f.execute(driver), 2)
        self.assertEqual(sum("--async" in a for a in cli.calls), invokes)
        self.assertEqual(sum("managed-stop" in a for a in cli.calls), 1)
        self.assertFalse(self.f.store.open_cleanup())
        with self.assertRaises(Halt):  # the consumed session is never reused
            self.f.execute(self.driver()[0])

    def test_uncertain_invocation_is_not_replayed_and_cleanup_runs(self):
        self.uncertain("invoke-timeout", 1)
        self.assertTrue(self.f.store.employee_unresolved_actions("clock:" + fi.TARGET))
        # The ambiguous invocation also refuses a fresh authorization for this employee.
        self.fresh("a")
        with self.assertRaises(Halt):
            self.f.execute(self.driver()[0])

    def test_uncertain_start_never_invokes_and_cleanup_runs(self):
        self.uncertain("start-timeout", 0)
        # A confirmed stop settles the uncertain start; nothing was invoked.
        self.assertFalse(self.f.store.employee_unresolved_actions("clock:" + fi.TARGET))

    def test_interruption_during_polling_still_stops_the_employee(self):
        driver, cli = self.driver()
        original = cli.json

        def interrupted(*args):
            if "run" in args and "get" in args:
                raise KeyboardInterrupt()
            return original(*args)

        cli.json = interrupted
        self.assertEqual(self.f.execute(driver), 2)
        self.assertEqual(driver.problem, "execution interrupted")
        self.assertEqual(sum("managed-stop" in a for a in cli.calls), 1)
        self.assertFalse(self.f.store.open_cleanup())

    def test_slowest_allowed_polling_cannot_consume_the_shutdown_reserve(self):
        self.f.session["runtime_remaining_seconds"] = 210
        driver, cli = self.driver(run_status="running")
        original, seen = cli.json, {}

        def slow(*args):
            if "run" in args and "get" in args:
                self.f.ticks += cli.timeout  # each read uses its whole allowed timeout
            if "managed-stop" in args:
                seen["stop_remaining"] = driver.deadline - self.f.ticks
            return original(*args)

        cli.json = slow
        self.assertEqual(self.f.execute(driver), 2)
        self.assertLessEqual(driver.counts["run_get"], 12)
        self.assertGreaterEqual(seen["stop_remaining"], 179.9)
        self.assertEqual(sum("--async" in a for a in cli.calls), 1)
        self.assertFalse(self.f.store.open_cleanup())

    def test_unconfirmed_shutdown_stays_open_and_never_passes(self):
        driver, cli = self.driver("stop-fails")
        self.assertNotEqual(self.f.execute(driver), 0)
        self.assertTrue(self.f.store.open_cleanup())
        self.assertNotEqual(self.verdicts(driver)["shutdown"], "passed")
        # Open cleanup marks the reading incomplete, so no criterion can certify it.
        self.assertNotEqual(self.report(driver)["verdict"], "passed")
        for criterion in ("answer", "no_tool_activity", "shutdown"):
            self.assertNotEqual(self.verdicts(driver)[criterion], "passed")
        saved = (self.f.root / driver.run_id / "invocation.json").read_text()
        self.assertIn("4300.25", saved)  # the answer is kept as evidence


def clock_setup(root, deployment, revision, package, **extra):
    """The actual instruction-update route under the proposed Clock Test config shape (live,
    explicit session, pinned executable, Discord unavailable), against the existing stand-in
    CLI. Returns (exit code, journal path, stand-in state path, output)."""
    folder = root / "docs/finance-invoices"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("SKILL.clock-current.md", "SKILL.finance.md"):
        shutil.copy(SOURCE / "docs/finance-invoices" / name, folder / name)
    exe = root / "seren"
    exe.write_text(standin(iu.STANDIN.replace('"7a000004-0000-4000-8000-000000000005"',
                                              'state.get("identity")')
                           .replace("{python}", sys.executable)))
    exe.chmod(0o755)
    prerun(exe)  # first-run assessment outside the timed commands
    pins = {k: v for k, v in execution_limits.PINNED.items() if k != "reply_timeout_seconds"}
    config = root / "clock-config.json"
    config.write_text(json.dumps({
        "environment": "live",
        "seren": dict(pins, executable=str(exe), executable_sha256=sha(exe.read_bytes()),
                      config_home=str(root / "never-created"), profile="default"),
        "employee": {"key": "clock-test", "deployment_id": deployment},
        "discord": {"sender": "unavailable", "observer": "unavailable"},
        "state_dir": "clock-state"}))
    state = root / "standin.json"
    state.write_text(json.dumps(dict({
        "deployment": deployment, "revision": revision, "identity": None,
        "content": (SOURCE / "docs/finance-invoices/SKILL.clock-current.md").read_text(),
        "tool_refs": [], "update": "ok"}, **extra)))
    out = io.StringIO()
    with mock.patch.dict(os.environ, {"STANDIN_STATE": str(state)}), \
            contextlib.redirect_stdout(out):
        code = main(["instruction-update", "--config", str(config), "--package",
                     str(package), "--root", str(root)])
    journal = root / "clock-state/instruction-update" / fi.PACKAGE_ID / "journal.json"
    return code, journal, state, out.getvalue()


class InstructionUpdateHandoff(unittest.TestCase):
    """The actual `instruction-update` route with this package and the real config shape;
    its journal is what the executor's setup check consumes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.package = json.loads(fi.PACKAGE.read_text())
        self.previous = self.package["baseline"]["active_revision_id"]

    def run_setup(self, package=None, **extra):
        code, self.journal, self.state, self.output = clock_setup(
            self.root, fi.TARGET, self.previous, package or fi.PACKAGE, **extra)
        return code

    def calls(self):
        path = Path(str(self.state) + ".calls")
        return [" ".join(json.loads(line)) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def journal_path(self, _package=None):
        return self.journal

    def test_discord_pins_apply_unless_the_package_declares_discord_unused(self):
        # The finance package passes under Discord-unavailable settings ...
        self.assertEqual(self.run_setup(), iu.instruction_update.PROCEED, self.output)
        # ... the same package without its declaration is refused before any service call,
        undeclared = dict(self.package)
        undeclared.pop("discord")
        copy_path = self.root / "undeclared.json"
        copy_path.write_text(json.dumps(undeclared))
        root = self.root / "second"
        root.mkdir()
        code, _, state, output = clock_setup(root, fi.TARGET, self.previous, copy_path)
        self.assertEqual(code, iu.instruction_update.INVALID)
        self.assertIn("discord sender must be human", output)
        self.assertFalse(Path(str(state) + ".calls").exists())
        # ... and every other package kind keeps the Discord pins under that config.
        config = load(str(self.root / "clock-config.json"))
        for path in (SOURCE / "docs/lifecycle-setup/response-correctness.proposed.json",
                     SOURCE / "docs/lifecycle-setup/preview-diagnostic-2.proposed.json"):
            other = json.loads(path.read_text())
            self.assertFalse(iu.instruction_update.discord_unused(other, config))
        self.assertTrue(iu.instruction_update.discord_unused(self.package, config))
        self.assertIn("discord sender must be human and observer discord_rest",
                      execution_limits.check_pins(config))
        self.assertEqual(execution_limits.check_pins(config, discord=False), [])

    def test_a_clean_setup_journal_is_accepted_and_binds_the_new_revision(self):
        self.assertEqual(self.run_setup(), iu.instruction_update.PROCEED, self.output)
        self.assertEqual(self.calls(), ["agent managed-get", "agent cloud deployment status",
                                        "agent managed-preview", "agent managed-get",
                                        "agent managed-update", "agent managed-get"])
        journal = json.loads(self.journal_path(fi.PACKAGE).read_text())
        self.assertTrue(all(e["timeout_seconds"] == 60 for e in journal["invocations"]))
        new = journal["observations"]["read_back"]["active_revision_id"]
        received = json.loads(self.state.read_text())["received_prompts"]
        self.assertEqual(received, [(SOURCE / "docs/finance-invoices/SKILL.finance.md")
                                    .read_text()] * 2)  # preview and update, exact
        previous = {"deployment_id": fi.TARGET, "active_revision_id": self.previous,
                    "name": "Seren Harness Clock Test", "tool_presets": ["live_data"],
                    "approval_policy": "read_only", "max_iterations": 2,
                    "max_tool_calls_per_run": 1, "max_timeout_seconds": 60,
                    "private_output_policy": "control_plane"}
        basis = {"branch_baseline": previous, "instruction_update": {"journal": {
            "path": str(self.journal_path(fi.PACKAGE)),
            "sha256": sha(self.journal_path(fi.PACKAGE).read_bytes())}}}
        fi.verify_update(basis, dict(previous, active_revision_id=new))
        with self.assertRaises(ValueError):
            fi.verify_update(basis, previous)  # the old revision is never accepted

    def test_an_unexpected_preview_difference_stops_before_the_update(self):
        self.assertEqual(self.run_setup(preview_extra=True), iu.instruction_update.STOP)
        self.assertNotIn("agent managed-update", self.calls())
        journal = json.loads(self.journal_path(fi.PACKAGE).read_text())
        self.assertEqual(journal["result"], "stopped")
        basis = {"branch_baseline": {"deployment_id": fi.TARGET,
                                     "active_revision_id": self.previous},
                 "instruction_update": {"journal": {
                     "path": str(self.journal_path(fi.PACKAGE)),
                     "sha256": sha(self.journal_path(fi.PACKAGE).read_bytes())}}}
        with self.assertRaises(ValueError):
            fi.verify_update(basis, {"deployment_id": fi.TARGET,
                                     "active_revision_id": NEW_REVISION})


class ShippedArtifacts(unittest.TestCase):
    def test_case_package_and_instruction_bind_the_dedicated_target(self):
        employee_tool.validate(CASE)
        spec = CASE["employee_tool"]
        self.assertEqual(spec["deployment_id"], "7a000021-0000-4000-8000-000000000022")
        self.assertEqual(CASE["target_status"], "unresolved")
        package = json.loads(fi.PACKAGE.read_text())
        self.assertEqual(package["target"]["deployment_id"], fi.TARGET)
        self.assertEqual(package["correction"]["corrected_trimmed_sha256"], sha(fi.instruction()))
        current = (SOURCE / package["correction"]["current_file"]).read_text()
        self.assertEqual(current, branch_read.prompt(json.loads(
            fi.BRANCH_CASE.read_text())["employee_tool"]))
        self.assertEqual(package["correction"]["current_sha256"], sha(current))
        for value in ("1800", "4300", "2500"):
            self.assertNotIn(value, json.dumps(CASE))
        other = copy.deepcopy(CASE)
        other["employee_tool"]["deployment_id"] = "7a00003a-0000-4000-8000-00000000003b"
        with self.assertRaises(ValueError):
            employee_tool.validate(other)


if __name__ == "__main__":
    unittest.main()
