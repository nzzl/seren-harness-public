"""One authorized unbound clock, publisher lookup, branch read or supplied-data answer.
No setup or retry path.

Uses the existing CLI, journal, per-employee lock, sanitizer and workflow review.
The setup receipt and immutable revision bind a dedicated, unbound fixture. Other
tools remain available: a one-call cap and the actual trace bound this test, not
tool isolation. Fresh policy/catalog drift prevents starting.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import time

from seren_harness import (__version__, contract, employee_tool, publisher_lookup, branch_read,
                           finance_invoices)
from seren_harness.collect import _sha256
from seren_harness.models import LIVE, utc_now
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import SerenCli
from seren_harness.state import (AMBIGUOUS, REJECTED, SUCCEEDED, StateError, Store,
                                 private_directory)
from seren_harness.workflow_review import _validate, review, summary_line


# Maintainer contract: serenorg/seren#234, comment 6110748897. This is the
# reviewed exposure for this one experiment, not a generic tool allowlist.
EXECUTION_SCOPE = "one-call-trace-v2"
REVIEWED_TOOLS = frozenset({
    "system_time", "seren_mcp_call_tool", "seren_schedule_run",
    "seren_publishers_suggest", "seren_publishers_get", "seren_mcp_list_tools",
    "seren_mcp_read_resource", "seren_publisher_request",
})
REVIEWED_OPERATIONS = frozenset({"get", "estimate", "mcp_list_tools", "mcp_read_resource"})


def reviewed_policy(data, names):
    """Match the reviewed exposure; never treat it as system_time-only access."""
    operations = data.get("allowed_publisher_operations")
    return (isinstance(names, list) and all(isinstance(n, str) for n in names)
            and len(names) == len(REVIEWED_TOOLS) and set(names) == REVIEWED_TOOLS
            and data.get("tool_presets") == ["live_data"]
            and data.get("approval_policy") == "read_only"
            and isinstance(operations, list)
            and all(isinstance(op, str) for op in operations)
            and len(operations) == len(REVIEWED_OPERATIONS)
            and set(operations) == REVIEWED_OPERATIONS)


class Halt(Exception):
    pass


def body(result):
    value = contract.unwrap(result.data) if result.ok else None
    return value if isinstance(value, dict) else {}


CLOCK_NAME = "Seren Harness Clock Test"


def validate(session, case):
    _validate(case)
    spec = case.get("employee_tool", {})
    kind = {employee_tool.SUPPLIED: "supplied", "branch_metadata": "branch",
            "publisher_metadata": "lookup", "utc_time": "clock"}.get(spec.get("result_type"))
    if case.get("target_status") != "resolved" or kind is None:
        raise ValueError("unbound clock case, explicit baseline and bounded session required")
    if kind == "lookup" and spec.get("message") != publisher_lookup.MESSAGE:
        raise ValueError("lookup invocation differs from the fixed request")
    verify_preparation(session, spec.get("deployment_id"), kind, spec)


def verify_preparation(session, deployment_id, kind, spec=None):
    """The session's fixture against its hash-bound preparation receipt chain.

    kind selects the one setup route that prepared the fixture: supplied (the invoice
    lane's Clock Test instruction-update chain or a created supplied-data fixture), branch,
    lookup or clock. Raises ValueError with a sanitized reason; reads local files only.
    """
    from seren_harness import supplied_fixture  # imports this module
    # The receipt chain binds the original revision; verified journey restores (exact
    # read-back records) carry only the revision id forward. Every hop is re-verified.
    from seren_harness import retrieval_journey
    baseline = retrieval_journey.original_baseline(session)
    if session.get("rebinding") and baseline.get("name") != CLOCK_NAME:
        raise ValueError("only the Clock Test receipt can be rebound")
    supplied, branch, lookup = kind == "supplied", kind == "branch", kind == "lookup"
    scope = (finance_invoices.SCOPE if supplied else branch_read.SCOPE if branch
             else publisher_lookup.SCOPE if lookup else EXECUTION_SCOPE)
    if (baseline.get("deployment_id") != deployment_id
            or baseline.get("name") not in (CLOCK_NAME, supplied_fixture.NAME)
            or not employee_tool._uuid(baseline.get("active_revision_id"))
            or not employee_tool._uuid(session.get("authorization_id"))
            or session.get("profile") != "default"
            or not 210 <= session.get("runtime_remaining_seconds", 0) <= 300
            or employee_tool._utc(session.get("expires_at")) is None):
        raise ValueError("unbound clock case, explicit baseline and bounded session required")
    expected = {"approval_policy": "read_only",
                "max_iterations": 2, "max_tool_calls_per_run": 1,
                "max_timeout_seconds": 60, "private_output_policy": "control_plane"}
    if (set(baseline) != set(BASELINE_FIELDS)
            or any(baseline.get(k) != v for k, v in expected.items())
            or baseline.get("tool_presets") not in ([], ["live_data"])
            or session.get("execution_scope") != scope
            or session.get("other_tools_available_acknowledged") is not True):
        raise ValueError("fixture baseline differs from the approved clock limits")
    # Receipt hash ties the configuration to preparation evidence, not a boolean claim.
    if not isinstance(session.get("setup_receipt"), str):
        raise ValueError("fixture preparation receipt missing")
    receipt = Path(session["setup_receipt"])
    if not receipt.is_file() or _sha256(receipt) != session.get("setup_receipt_sha256"):
        raise ValueError("fixture preparation receipt changed")
    basis = json.loads(receipt.read_text())
    if (basis.get("deployment_id") != deployment_id
            or basis.get("active_revision_id") != baseline["active_revision_id"]
            or basis.get("execution_scope") != scope
            or basis.get("unbound_fixture_verified") is not True
            or not reviewed_policy(basis.get("effective_policy", {}),
                                   basis.get("effective_policy", {}).get("resolved_tools"))
            or not basis.get("creation_receipt") or not basis.get("preview_receipt")):
        raise ValueError("creation, unbound fixture and reviewed tool exposure not established")
    # Each fixture name has exactly one setup route: Clock Test's recorded update chain, or a
    # supplied-data fixture created under its own authorization (supplied_fixture).
    created = basis.get("setup_route") is not None
    if created != (baseline["name"] == supplied_fixture.NAME) or (created and not supplied):
        raise ValueError("fixture name does not match its setup route")
    if created:
        supplied_fixture.verify_setup(basis, baseline, session)
    elif supplied:
        # The whole branch chain, then the instruction-update journal on top of it.
        verify_fixture_refs(basis.get("tool_refs_evidence", {}), basis["prior_baseline"])
        branch_spec = json.loads(finance_invoices.BRANCH_CASE.read_text())["employee_tool"]
        branch_read.verify_update(basis, basis["branch_baseline"], fixture_baseline, branch_spec)
        finance_invoices.verify_update(basis, baseline)
    elif branch:
        verify_fixture_refs(basis.get("tool_refs_evidence", {}), basis["prior_baseline"])
        branch_read.verify_update(basis, baseline, fixture_baseline, spec)
    elif lookup:
        verify_fixture_refs(basis.get("tool_refs_evidence", {}), basis["prior_baseline"])
        publisher_lookup.verify_update(basis, baseline, fixture_baseline)
    else:
        verify_fixture_refs(basis.get("tool_refs_evidence", {}), baseline)


BASELINE_FIELDS = ("deployment_id", "active_revision_id", "name", "tool_presets",
                   "approval_policy", "max_iterations", "max_tool_calls_per_run",
                   "max_timeout_seconds", "private_output_policy")


def fixture_baseline(detail):
    """Response fields only. Absent optional tool_refs is NOT replaced by []."""
    if (detail.get("status") != "stopped"
            or any(k not in detail for k in BASELINE_FIELDS)
            or ("tool_refs" in detail and detail["tool_refs"] != [])):
        raise ValueError("stopped fixture fields missing or declared refs contradict setup")
    return {k: detail[k] for k in BASELINE_FIELDS}


def fixture_refs_evidence(creation, preview, detail):
    """Pin the saved authored-declaration -> created ID -> observed revision chain."""
    return {label: {"path": str(path), "sha256": _sha256(path)}
            for label, path in (("creation", creation), ("preview", preview), ("detail", detail))}


def verify_fixture_refs(evidence, baseline):
    """Supported optional response shape plus explicit, hash-bound source evidence.

    Cloud onboarding application.source_revision binds the applied draft/preview.
    Its returned deployment ID is bound to the saved stopped detail's active revision;
    prepare() requires that same revision again before starting. No tool_refs default.
    """
    def read(label):
        ref = evidence[label]
        path = Path(ref["path"])
        if _sha256(path) != ref["sha256"]:
            raise ValueError("fixture source receipt changed")
        receipt = json.loads(path.read_text())
        if receipt.get("status") != "ok" or receipt.get("exit_code") != 0:
            raise ValueError("successful fixture source receipt required")
        value = (receipt.get("response") if label == "creation"
                 else contract.unwrap(receipt.get("response")))
        if not isinstance(value, dict):
            raise ValueError("fixture source response missing")
        return value
    created, preview, detail = read("creation"), read("preview"), read("detail")
    application = created.get("application", {})
    revision = preview.get("source_revision")
    if (not employee_tool._uuid(created.get("id"))
            or created["id"] != preview.get("onboarding_id")
            or type(revision) is not int or revision < 1
            or application.get("source_revision") != revision
            or application.get("deployment_outcome") != "confirmed"
            or created.get("operations", {}).get("deployment_id") != baseline["deployment_id"]
            or created.get("draft", {}).get("deployment", {}).get("tool_refs") != []
            or preview.get("desired_spec", {}).get("tool_refs") != []
            or fixture_baseline(detail) != baseline):
        raise ValueError("authored empty refs and created fixture revision not established")


def pinned_cli(session, sanitizer):
    if _sha256(session["executable"]) != session["executable_sha256"]:
        raise ValueError("CLI checksum differs")
    cli = SerenCli(session["executable"], 30, sanitizer,
                   {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin",
                    "XDG_CONFIG_HOME": session["config_home"], "SEREN_PROFILE": "default"})
    version = cli.version()
    if not version.ok or contract.parse_version(version.data) != "0.10.0":
        raise ValueError("CLI contract differs")
    return cli


class ClockRun:
    def __init__(self, session, case, cli, store, clock=time.monotonic,
                 sleep=time.sleep, wall=utc_now):
        self.session, self.case, self.cli, self.store = session, case, cli, store
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.target = case["employee_tool"]["deployment_id"]
        self.key = "clock:" + self.target
        self.counts = {}
        self.receipts = []
        self.run_id = None
        self.initial, self.close = {}, {}
        self.cleanup_id = None
        self.problem = None
        self.complete = False
        self.deadline = 0
        self.work_deadline = 0

    def call(self, kind, args, maximum, mutation=False, plain=False, cleanup=False,
             deadline=None):
        limit = self.deadline if cleanup else self.work_deadline
        if deadline is not None:
            limit = min(limit, deadline)
        remaining = limit - self.clock()
        if self.counts.get(kind, 0) >= maximum or remaining <= 0:
            raise Halt("call or time budget exhausted: " + kind)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.cli.timeout = min(30, remaining)
        action = None
        if mutation:
            action = self.store.record_intent(self.run_id, kind, kind, self.target,
                                              {"argv": self.cli.display_argv(args)})
        started = self.wall()
        result = self.cli._execute(args) if plain else self.cli.json(*args)
        receipt = {"kind": kind, "started_at": started, "finished_at": self.wall(),
                   "status": result.status, "exit_code": result.exit_code,
                   "argv": result.argv, "response": result.data,
                   "stderr": result.stderr_tail}
        self.receipts.append(receipt)
        self.store.event(self.run_id, "cli_receipt", receipt)
        if action is not None:
            state = SUCCEEDED if result.ok else (REJECTED if
                    contract.definitely_not_applied(result.stderr_tail) else AMBIGUOUS)
            self.store.resolve_action(action, state, receipt)
        return result

    def status(self, cleanup=False, deadline=None):
        result = self.call("status", ["agent", "cloud", "deployment", "status", self.target],
                           12, cleanup=cleanup, deadline=deadline)
        data = body(result)
        if data.get("id") != self.target:
            raise Halt("lifecycle read does not establish target identity")
        return result, data

    def check_baseline(self, detail):
        """Unbound fixtures: no declared refs and exactly the reviewed baseline."""
        try:
            baseline = fixture_baseline(detail)
        except ValueError:
            raise Halt("stopped fixture fields or declared refs not established") from None
        if baseline != self.session["baseline"]:
            raise Halt("stopped fixture differs from its reviewed baseline")

    def prepare(self):
        result = self.call("detail", ["agent", "managed-get", self.target], 3)
        detail = body(result)
        self.check_baseline(detail)
        if self.case["employee_tool"].get("result_type") == "branch_metadata":
            try:
                branch_read.verify_detail(detail, self.case["employee_tool"])
            except ValueError:
                raise Halt("branch fixture instruction/output limit changed") from None
        elif self.case["employee_tool"].get("result_type") == "publisher_metadata":
            try:
                publisher_lookup.verify_detail(detail)
            except ValueError:
                raise Halt("lookup fixture instructions/output limit changed") from None
        elif self.case["employee_tool"].get("result_type") == employee_tool.SUPPLIED:
            try:
                finance_invoices.verify_detail(detail)
            except ValueError:
                raise Halt("finance instruction is not installed") from None
        result = self.call("catalog", ["agent", "managed-deployment-tools", self.target], 1)
        data = body(result)
        if data.get("deployment_id") != self.target or not isinstance(data.get("tools"), list):
            raise Halt("tool catalog identity/shape unavailable")
        tools = data["tools"]
        if any(not isinstance(t, dict) or not isinstance(t.get("effective_policy"), dict)
               or t["effective_policy"].get("status") not in ("allowed", "blocked") for t in tools):
            raise Halt("tool availability is not established")
        if not reviewed_policy(data, [t.get("name") for t in tools]):
            raise Halt("tool catalog differs from the explicitly reviewed exposure")
        if self.case["employee_tool"].get("result_type") == employee_tool.SUPPLIED:
            # No tool is requested: the reviewed exposure above is the whole tool check.
            self.require_stopped()
            return
        lookup = self.case["employee_tool"].get("result_type") == "publisher_metadata"
        branch = self.case["employee_tool"].get("result_type") == "branch_metadata"
        tool_name = branch_read.TOOL if branch else "seren_publishers_get" if lookup else "system_time"
        tool = next(t for t in tools if t["name"] == tool_name)
        if tool["effective_policy"]["status"] != "allowed":
            raise Halt("requested read-only tool unavailable")
        if (tool.get("side_effecting") is not False or tool.get("approval_type") != "none"
                or tool.get("checkpoint_required") is not False
                or tool.get("input_schema") != (branch_read.SCHEMA if branch else publisher_lookup.SCHEMA if lookup else
                    {"type": "object", "properties": {}, "additionalProperties": False})):
            raise Halt("tool contract differs from the approved read-only call")
        self.require_stopped()

    def require_stopped(self):
        _, data = self.status()
        if data.get("status") != "stopped" or data.get("desired_lifecycle_state") != "stopped":
            raise Halt("initial stopped state and intent not established")

    def execute(self):
        self.prepare()
        # Runtime budget starts at the start attempt, not when the answer arrives.
        self.deadline = min(self.deadline, self.clock() + self.session["runtime_remaining_seconds"])
        self.work_deadline = self.deadline - 180
        if self.work_deadline - self.clock() < 30:
            raise Halt("insufficient runtime allowance with shutdown reserve")
        self.cleanup_id = self.store.add_cleanup(self.run_id, self.key, "stop_runtime", self.target)
        started = self.call("managed_start", ["agent", "managed-start", self.target], 1, True)
        if not started.ok:
            raise Halt("start outcome unsuccessful or unknown; no invocation")
        ready_deadline = min(self.work_deadline, self.clock() + 60)
        # Twelve status reads total: one preflight, at most six readiness,
        # and five reserved for confirmed shutdown. Spread readiness reads over
        # up to 60s inside the work budget; never borrow the 180s shutdown reserve.
        while self.clock() < ready_deadline and self.counts.get("status", 0) < 7:
            _, state = self.status(deadline=ready_deadline)
            if self.clock() >= ready_deadline:
                raise Halt("employee did not become running within readiness budget")
            if state.get("status") == "running" and state.get("desired_lifecycle_state") == "running":
                break
            if state.get("status") == "failed":
                raise Halt("employee did not become running within readiness budget")
            self.sleep(min(10, max(0, ready_deadline - self.clock())))
        else:
            raise Halt("employee did not become running within readiness budget")
        message = self.case["employee_tool"]["message"]
        start_wall, start_clock = self.wall(), self.clock()
        result = self.call("employee_invoke", ["agent", "cloud", "run", "start",
                           "--deployment-id", self.target, "--message", message, "--async"],
                           1, True, True)
        text = re.sub(r"\x1b\[[0-9;]*m", "", str(result.data or ""))
        rid = re.findall(r"(?m)^\s*Run ID: ([0-9a-f-]+)\s*$", text)
        eid = re.findall(r"(?m)^\s*Execution ID: (\S+)\s*$", text)
        if (not result.ok or "Run accepted for deployment " + self.target + "." not in text
                or len(rid) != 1 or not employee_tool._uuid(rid[0]) or len(eid) != 1):
            self.store.resolve_action(self.store.actions(self.run_id)[-1]["id"], AMBIGUOUS,
                                      {"reason": "invocation identity unavailable"})
            raise Halt("invocation identity unknown; it will not be submitted again")
        request = {"deployment_id": self.target, "run_id": rid[0],
                   "execution_id": eid[0], "message": message}
        self.initial = {"request": request}
        for attempt in range(12):
            if self.clock() >= min(self.work_deadline, start_clock + 120):
                raise Halt("run observation budget exhausted")
            result = self.call("run_get", ["agent", "cloud", "run", "get",
                               "--deployment-id", self.target, rid[0]], 12)
            self.initial.update(status=result.status, exit_code=result.exit_code,
                                response=result.data, observation_window={
                                    "started_at": start_wall, "finished_at": self.wall(),
                                    "elapsed_seconds": self.clock() - start_clock})
            data = body(result)
            if (not result.ok or data.get("id") != rid[0]
                    or data.get("deployment_id") != self.target
                    or data.get("execution_id") != eid[0]):
                raise Halt("run read failed or returned a different identity")
            if data.get("status") == "completed":
                self.complete = isinstance(data.get("output_events"), list)
                return
            if data.get("status") not in ("pending", "queued", "running"):
                raise Halt("run ended or requires an unapproved action")
            if attempt < 11:
                self.sleep(min(5, max(0, self.work_deadline - self.clock())))
        raise Halt("run observation poll limit exhausted")

    def cleanup(self):
        if self.cleanup_id is None:
            return
        close_deadline = min(self.deadline, self.clock() + 180)
        self.call("managed_stop", ["agent", "managed-stop", self.target], 1,
                  mutation=True, cleanup=True, deadline=close_deadline)
        # Keep 30s for the final managed read-back. Five lifecycle reads span
        # the available observation window instead of exhausting it in ~25s.
        # Acceptance alone is insufficient. No stop retry in this executor.
        poll_deadline = close_deadline - 30
        poll_start = self.clock()
        last_poll = max(poll_start, poll_deadline - 30)
        for attempt in range(5):
            due = poll_start + (last_poll - poll_start) * attempt / 4
            self.sleep(max(0, min(due, poll_deadline) - self.clock()))
            result, state = self.status(cleanup=True, deadline=poll_deadline)
            if self.clock() >= poll_deadline:
                raise Halt("shutdown observation deadline exhausted")
            self.close = {"status": result.status, "exit_code": result.exit_code,
                          "observed_at": self.wall(), "response": result.data}
            if state.get("status") == "stopped" and state.get("desired_lifecycle_state") == "stopped":
                detail = self.call("detail", ["agent", "managed-get", self.target], 3,
                                   cleanup=True, deadline=close_deadline)
                d = body(detail)
                if (self.clock() >= close_deadline or d.get("deployment_id") != self.target
                        or d.get("status") != "stopped"):
                    raise Halt("managed stop read-back is unconfirmed")
                self.store.update_cleanup(self.cleanup_id, "done", self.close)
                self.store.supersede_actions(self.run_id, "managed_start", "subsequent confirmed stop")
                self.store.supersede_actions(self.run_id, "managed_stop", "confirmed stopped read-back")
                return
        raise Halt("shutdown not confirmed")

    def run(self):
        expires = employee_tool._utc(self.session["expires_at"])
        remaining = (expires - employee_tool._utc(self.wall())).total_seconds()
        if not 210 <= remaining <= 1200:
            raise Halt("authorization expired or insufficient session/shutdown time")
        if (self.store.open_cleanup(self.key) or self.store.employee_unresolved_actions(self.key)
                or any(r["config_digest"] == self.session["authorization_id"] for r in self.store.runs())):
            raise Halt("prior session/cleanup exists; no automatic replay")
        self.deadline = self.clock() + remaining
        self.work_deadline = self.deadline - 180
        self.run_id = self.store.create_run(
            "employee_supplied_answer" if self.case["employee_tool"].get("result_type") ==
            employee_tool.SUPPLIED else
            "employee_branch_read" if self.case["employee_tool"].get("result_type") == "branch_metadata" else
            "employee_publisher_lookup" if self.case["employee_tool"].get("result_type") ==
            "publisher_metadata" else "employee_clock", self.key, LIVE,
                                           self.session["authorization_id"], __version__)
        self.store.acquire_lock(self.key, self.run_id)
        previous = {}
        def interrupt(_sig, _frame):
            raise KeyboardInterrupt()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, interrupt)
            try:
                self.execute()
            except (Halt, KeyboardInterrupt) as error:
                self.problem = str(error) if isinstance(error, Halt) else "execution interrupted"
            finally:
                # Avoid a second signal interrupting the bounded cleanup once underway.
                for sig in previous:
                    signal.signal(sig, signal.SIG_IGN)
                try:
                    self.cleanup()
                except (Halt, OSError, ValueError):
                    self.problem = "shutdown unconfirmed; cleanup remains open"
        finally:
            unresolved = bool(self.store.actions(self.run_id, True) or
                              self.store.open_cleanup(run_id=self.run_id))
            self.complete = self.complete and not unresolved
            self.store.update_run(self.run_id, status="unresolved" if unresolved else "complete")
            self.store.release_lock(self.key, self.run_id)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return self.save()

    def save(self):
        folder = private_directory(Path(self.session["home"]) / self.run_id)
        sanitizer = self.store.sanitizer
        items = [{"id": "invocation", "role": "initial", "kind": "api_response",
                  "text": json.dumps(self.initial)},
                 {"id": "shutdown", "role": "complete_view", "kind": "api_response",
                  "text": json.dumps(self.close)}]
        bundle = {"provenance": "independently_observed", "complete_reading_captured": self.complete,
                  "environment": "Pinned Seren CLI 0.10.0; actual one-shot employee invocation",
                  "notes": self.problem, "observed": self.wall()}
        for item in items:
            item["path"] = item["id"] + ".json"
            _write_private(folder / item["path"], sanitizer.text(item["text"]))
        bundle["items"] = [{k: v for k, v in i.items() if k != "text"} for i in items]
        for name, value in (("bundle.json", bundle), ("case.json", self.case),
                            ("receipts.json", self.receipts)):
            _write_private(folder / name, json.dumps(sanitizer.value(value), indent=2))
        report = review(self.case, bundle, items, self.session["state_dir"], folder / "report", None, None)
        print(summary_line(report))
        print("Report: " + str(folder / "report/report.md"))
        print("Shutdown: " + ("confirmed" if self.cleanup_id is not None and not
              self.store.open_cleanup(run_id=self.run_id) else "not established by this run"))
        return {"passed": 0, "failed": 1}.get(report["verdict"], 2)


def run(args):
    store = None
    try:
        session = json.loads(Path(args.session).expanduser().read_text())
        case = json.loads(Path(session["case"]).read_text())
        validate(session, case)
        sanitizer = Sanitizer()
        cli = pinned_cli(session, sanitizer)
        store = Store(session["state_dir"], sanitizer)
        return ClockRun(session, case, cli, store).run()
    except (ValueError, KeyError, OSError, StateError, Halt):
        print("Clock test stopped: invalid/expired setup or unresolved prior session. No replay.")
        return 3
    finally:
        if store is not None:
            store.close()
