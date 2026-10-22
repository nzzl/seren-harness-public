"""Finite, resumable invoice discovery using the existing employee executor.

Preparation is offline. Execution requires a separate, exact authorization. An
interrupted attempt is never replayed, including interruption before the first call.
"""
from __future__ import annotations

from datetime import timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

from seren_harness import employee_run, employee_tool, invoice_catalog
from seren_harness.models import utc_now
from seren_harness.sanitize import Sanitizer
from seren_harness.state import Store, StateError, private_directory

ROOT = Path(__file__).resolve().parents[1]
WINDOW = 1800
CASE_WINDOW = 420


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_digest():
    paths = sorted((ROOT / "seren_harness").glob("*.py"))
    paths += [ROOT / "cases/seren-employee-finance-invoices.json"]
    paths += sorted((ROOT / "docs/finance-invoices").glob("SKILL*.md"))
    paths += [ROOT / "docs/finance-invoices/task-input.txt"]
    return digest({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in paths})


def package(session, suite=None):
    return {"version": 1 if suite is None else 2, **({"suite": suite} if suite is not None else {}),
            "session": session, "catalog": invoice_catalog.manifest(suite),
            "source_sha256": source_digest(), "cases": list(invoice_catalog.case_ids(suite)),
            "limits": {"window_seconds": WINDOW, "case_window_seconds": CASE_WINDOW,
                       "service_invocations": 93, "invocations_per_case": 31,
                       "employee_runs": 3, "retries": 0}}


def write(path, value):
    """Atomic and durable transition before any employee executor is entered."""
    private_directory(path.parent)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def authorize(proposal, approval, now):
    if proposal != package(proposal["session"], proposal.get("suite")):
        raise ValueError("campaign source, cases or limits changed")
    if (set(approval) != {"approved", "authorization_id", "package_sha256", "issued_at", "expires_at"}
            or approval["approved"] is not True
            or not employee_tool._uuid(approval["authorization_id"])
            or approval["package_sha256"] != digest(proposal)):
        raise ValueError("exact separate campaign authorization required")
    issued = employee_tool._utc(approval["issued_at"])
    expires = employee_tool._utc(approval["expires_at"])
    if issued is None or expires is None or not issued <= now < expires:
        raise ValueError("authorization window invalid or expired")
    if not 0 < (expires - issued).total_seconds() <= WINDOW:
        raise ValueError("authorization exceeds the campaign window")
    return expires


def execute_case(session, case):
    """Only live entry point: retain all existing receipt, policy and cleanup gates."""
    employee_run.validate(session, case)
    sanitizer = Sanitizer()
    cli = employee_run.pinned_cli(session, sanitizer)
    store = Store(session["state_dir"], sanitizer)
    try:
        driver = employee_run.ClockRun(session, case, cli, store)
        driver.run()
        folder = Path(session["home"]) / driver.run_id
        report = json.loads((folder / "report/report.json").read_text())
        checks = {c["id"]: c["verdict"] for c in report["criteria"]}
        clean = (driver.cleanup_id is not None and
                 not store.open_cleanup(run_id=driver.run_id) and
                 not store.employee_unresolved_actions(driver.key))
        return {"run_id": driver.run_id, "report": str(folder / "report/report.md"),
                "checks": checks, "cleanup_confirmed": clean,
                "service_invocations": sum(driver.counts.values())}
    finally:
        store.close()


def execute(proposal, approval, runner=execute_case, wall=utc_now):
    now = employee_tool._utc(wall())
    expires = authorize(proposal, approval, now)
    # Validate every saved setup reference offline before even the local CLI version call.
    target = (proposal["session"].get("baseline") or {}).get("deployment_id")
    for key in proposal["cases"]:
        employee_run.validate(proposal["session"], invoice_catalog.case(key, target))
    home = private_directory(Path(proposal["session"]["state_dir"]) / "invoice-campaigns")
    path = home / (approval["authorization_id"] + ".json")
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binding = digest({"package": proposal, "approval": approval})
        state = json.loads(path.read_text()) if path.exists() else {
            "binding": binding, "status": "ready", "results": []}
        if state["binding"] != binding:
            raise ValueError("campaign binding changed")
        if state["status"] in ("attempting", "stopped", "complete", "budget_exhausted"):
            return state  # Never duplicate even an uncertain attempt.
        if (state["status"] != "ready" or not isinstance(state["results"], list)
                or len(state["results"]) > len(proposal["cases"])):
            raise ValueError("unsupported campaign journal state")
        for index, result in enumerate(state["results"]):
            if (result.get("case") != proposal["cases"][index] or not successful(result)):
                raise ValueError("completed case prefix is not established")
        for index, key in enumerate(proposal["cases"]):
            if index < len(state["results"]):
                continue
            now = employee_tool._utc(wall())
            if (expires - now).total_seconds() < CASE_WINDOW:
                state["status"] = "budget_exhausted"
                write(path, state)
                return state
            session = dict(proposal["session"])
            session.update(authorization_id=str(uuid5(UUID(approval["authorization_id"]), key)),
                           expires_at=(now + timedelta(seconds=CASE_WINDOW)).isoformat(),
                           runtime_remaining_seconds=300)
            state.update(status="attempting", current_case=key)
            write(path, state)
            result = runner(session, invoice_catalog.case(key, target))
            state["results"].append(dict(result, case=key))
            passed = successful(result)
            state["status"] = "ready" if passed else "stopped"
            write(path, state)
            if not passed:
                return state
        state["status"] = "complete"
        write(path, state)
        return state


def successful(result):
    required = {"run_identity", "answer", "no_tool_activity", "shutdown"}
    return (result.get("cleanup_confirmed") is True and
            isinstance(result.get("checks"), dict) and set(result["checks"]) == required and
            all(v == "passed" for v in result["checks"].values()) and
            type(result.get("service_invocations")) is int and
            0 < result["service_invocations"] <= 31)


def run(args):
    try:
        if args.action == "prepare":
            session = json.loads(Path(args.session).expanduser().read_text())
            for key in invoice_catalog.case_ids(args.suite):
                employee_run.validate(session, invoice_catalog.case(
                    key, session["baseline"]["deployment_id"]))
            proposal = package(session, args.suite)
            write(Path(args.output).expanduser(), proposal)
            print("Offline package prepared. Authorization must bind SHA256: " + digest(proposal))
            return 0
        proposal = json.loads(Path(args.package).expanduser().read_text())
        approval = json.loads(Path(args.authorization).expanduser().read_text())
        state = execute(proposal, approval)
        print(json.dumps(state, indent=2))
        return 0 if state["status"] == "complete" else 2
    except (ValueError, KeyError, OSError, StateError, employee_run.Halt):
        print("Campaign stopped. Inspect its durable journal and employee cleanup; no replay.")
        return 3
