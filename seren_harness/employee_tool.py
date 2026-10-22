"""Bounded employee-tool evidence checks and exact approval preparation; no I/O.

Trace shapes: Seren v0.10.0 CloudDeploymentRunEvent/CloudRunOutputEvent.
The execution driver is deliberately not enabled before isolated fixture setup.
"""

from datetime import datetime
import math
import re
import json
from uuid import UUID

from seren_harness.models import BLOCKED, FAILED, PASSED


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _read(items, role):
    selected = [
        i for i in items if i.get("kind") == "api_response" and i.get("role") == role
    ]
    return _object(selected[0]["text"]) if len(selected) == 1 else None


SUPPLIED = "supplied_answer"
SUPPLIED_CHECKS = ("employee_run_identity", "employee_supplied_answer",
                   "employee_no_tool_activity", "employee_tool_shutdown")
# CloudRunOutputEvent kinds (pinned v0.10.0) that show tool or action activity.
ACTIVITY = ("tool_call", "tool_result", "tool_audit", "approval_wait", "approval_decision",
            "handoff", "artifact")


def _validate_supplied(case, spec):
    from seren_harness import finance_invoices, invoice_catalog
    keys = {"deployment_id", "result_type", "message", "message_sha256"}
    expected_message = finance_invoices.message()
    if "catalog_case" in spec:
        keys.add("catalog_case")
        if not invoice_catalog.known(spec["catalog_case"]):
            raise ValueError("unknown frozen invoice challenge")
        expected_message = invoice_catalog.message(spec["catalog_case"])
    checks = [c.get("check") for c in case.get("criteria", [])]
    # A frozen catalog case may name any fixture; employee_run.validate then requires that
    # deployment to be the session baseline its setup evidence establishes.
    target = (_uuid(spec.get("deployment_id")) if "catalog_case" in spec
              else spec.get("deployment_id") == finance_invoices.TARGET)
    if (set(spec) != keys
            or not target
            or spec["message"] != expected_message
            or spec["message_sha256"] != finance_invoices._sha(spec["message"])
            or sorted(checks) != sorted(SUPPLIED_CHECKS)):
        raise ValueError("supplied-answer case needs the dedicated target, the frozen "
                         "task input and exactly its four checks")


def validate(case):
    spec = case.get("employee_tool")
    if isinstance(spec, dict) and spec.get("result_type") == SUPPLIED:
        return _validate_supplied(case, spec)
    if any(c.get("check") in ("employee_supplied_answer", "employee_no_tool_activity")
           for c in case.get("criteria", [])):
        raise ValueError("supplied-answer checks belong only to a supplied-answer case")
    if (
        not isinstance(spec, dict)
        or not _uuid(spec.get("deployment_id"))
        or any(
            not isinstance(spec.get(k), str) or not spec[k] for k in ("tool", "message")
        )
        or not isinstance(spec.get("arguments"), dict)
        or spec.get("result_type", "database") not in ("database", "utc_time", "publisher_metadata", "branch_metadata")
    ):
        raise ValueError(
            "employee tool checks need explicit identity, invocation and result expectations"
        )

    if spec.get("result_type", "database") == "utc_time":
        if (
            spec["tool"] != "system_time"
            or spec["arguments"] != {}
            or type(spec.get("clock_tolerance_seconds")) is not int
            or not 0 <= spec["clock_tolerance_seconds"] <= 5
            or spec.get("max_observation_seconds") != 120
        ):
            raise ValueError(
                "system_time needs empty arguments and bounded clock expectations"
            )
    elif spec.get("result_type") == "publisher_metadata":
        if (spec["tool"] != "seren_publishers_get" or spec["arguments"] != {"slug": "seren-db"}
                or spec.get("result") != {"slug": "seren-db"} or spec.get("answer") != "seren-db"):
            raise ValueError("publisher lookup requires the fixed read-only Seren DB identity case")
    elif spec.get("result_type") == "branch_metadata":
        from seren_harness import branch_read
        expected = spec.get("result", {})
        if (not isinstance(expected, dict) or set(expected) != {"id", "project_id"}
                or not all(_uuid(v) for v in expected.values())
                or spec["tool"] != branch_read.TOOL
                or spec["arguments"] not in branch_arguments(expected)
                or spec.get("answer") != expected["id"]
                or spec["message"] != branch_read.message(spec)):
            raise ValueError("branch case needs one exact read and independent branch/project IDs")
    elif (
        not spec["arguments"]
        or not isinstance(spec.get("result"), dict)
        or not spec["result"]
        or not isinstance(spec.get("answer"), str)
        or not spec["answer"]
    ):
        raise ValueError("database case needs fixed result and answer expectations")


def _utc(value):
    # A deliberately narrow observation format, not a claimed Seren result schema.
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value
    ):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc_nanoseconds(value):
    """Exact comparison value; never truncate a tool's nanoseconds to microseconds."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(?:Z|\+00:00)", value
    )
    if match is None:
        return None
    base = _utc(match[1] + "Z")
    if base is None:
        return None
    delta = base - _utc("1970-01-01T00:00:00Z")
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int(
        (match[2] or "").ljust(9, "0")
    )


def _clock_result(content):
    """Recognize legacy bare UTC or the six-field JSON observed in the saved run.

    This is an observed encoding, not a claim about all Seren tool versions.
    Redundant metadata must agree; unsupported/ambiguous content stays unverified.
    """
    stamp = content
    if isinstance(content, str) and content.lstrip().startswith("{"):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate field")
                result[key] = value
            return result
        try:
            data = json.loads(content, object_pairs_hook=unique)
        except ValueError:
            return None
        if set(data) != {"utc", "unix_epoch", "timezone", "utc_offset", "date", "time"}:
            return None
        stamp = data["utc"]
        ns = _utc_nanoseconds(stamp)
        if (ns is None or type(data["unix_epoch"]) is not int
                or data["unix_epoch"] != ns // 1_000_000_000
                or data["timezone"] != "UTC" or data["utc_offset"] != "+00:00"
                or data["date"] != stamp[:10]
                or data["time"] != re.sub(r"(?:Z|\+00:00)$", "", stamp[11:])):
            return None
    ns = _utc_nanoseconds(stamp)
    return (ns, stamp) if ns is not None else None


def _time_result(case, items, content):
    parsed = _clock_result(content)
    if parsed is None:
        return (
            BLOCKED,
            "tool result is not a supported UTC timestamp; preserve its format for review",
            {},
        )
    timestamp, result_text = parsed
    window = (_read(items, "initial") or {}).get("observation_window", {})
    if not isinstance(window, dict):
        window = {}
    start, end = _utc(window.get("started_at")), _utc(window.get("finished_at"))
    elapsed = window.get("elapsed_seconds")
    if (
        start is None
        or end is None
        or end < start
        or (end - start).total_seconds()
        > case["employee_tool"]["max_observation_seconds"]
        or type(elapsed) not in (int, float)
        or not math.isfinite(elapsed)
        or elapsed < 0
        or elapsed > case["employee_tool"]["max_observation_seconds"]
        or abs((end - start).total_seconds() - elapsed) > 1
    ):
        return (
            BLOCKED,
            "independent bounded harness observation window unavailable or clock changed",
            {},
        )
    tolerance = case["employee_tool"]["clock_tolerance_seconds"]
    start_ns = _utc_nanoseconds(window["started_at"])
    end_ns = _utc_nanoseconds(window["finished_at"])
    ok = start_ns - tolerance * 1_000_000_000 <= timestamp <= end_ns + tolerance * 1_000_000_000
    return (
        PASSED if ok else FAILED,
        "tool time falls inside the harness observation window and clock tolerance"
        if ok
        else "tool time falls outside the harness observation window and clock tolerance",
        {
            "result": result_text,
            "window": window,
            "clock_tolerance_seconds": tolerance,
        },
    )


def _run(case, items):
    envelope = _read(items, "initial") or {}
    request = envelope.get("request")
    response = envelope.get("response")
    run = response.get("data") if isinstance(response, dict) else None
    spec = case["employee_tool"]
    if not isinstance(request, dict) or not isinstance(run, dict):
        return None
    payload = run.get("invocation_payload")
    if (
        envelope.get("status") != "ok"
        or envelope.get("exit_code") != 0
        or not _uuid(request.get("run_id"))
        or request.get("deployment_id") != spec["deployment_id"]
        or run.get("deployment_id") != request["deployment_id"]
        or run.get("id") != request["run_id"]
        or not isinstance(request.get("execution_id"), str)
        or not request["execution_id"]
        or run.get("execution_id") != request["execution_id"]
        or request.get("message") != spec["message"]
        or not isinstance(payload, dict)
        or payload.get("message") != spec["message"]
    ):
        return None
    return run


def branch_arguments(expected):
    """The one exact branch read, optionally naming its operation for a typed grant."""
    plain = {"publisher": "seren-db", "operation": "get",
             "path": "/projects/" + expected["project_id"] + "/branches/" + expected["id"]}
    return [plain, dict(plain, operation_id="seren_db_get_branch")]


def run_identity(case, items, captured):
    run = _run(case, items)
    if run is None:
        return BLOCKED, "run identity or invocation is not established", {}
    if run.get("status") != "completed":
        return (
            BLOCKED,
            "the requested run did not complete",
            {"status": run.get("status")},
        )
    return (
        PASSED,
        "completed run matches its recorded invocation",
        {
            "run_id": run["id"],
            "execution_id": run["execution_id"],
            "deployment_id": run["deployment_id"],
        },
    )


def _publisher_result(content, expected_slug):
    """Check complete publisher identity; never reconstruct a cut result."""
    data = _object(content)
    # The saved live trace has a truncated wrapper that is itself cut off.
    # Never repair it, unwrap partial content, or certify a marked result.
    if ((isinstance(data, dict) and data.get("truncated") is True)
            or (isinstance(content, str) and re.match(
                r'^\s*\{\s*"truncated"\s*:\s*true\b', content))):
        return BLOCKED, "publisher tool result explicitly truncated", {
            "truncated": True,
            "captured_characters": len(content) if isinstance(content, str) else None,
        }
    if isinstance(data, dict) and "data" in data:
        if "slug" in data:
            return BLOCKED, "ambiguous publisher response envelope", {}
        data = data["data"]
    if isinstance(data, dict) and data.get("truncated") is True:
        return BLOCKED, "publisher metadata explicitly truncated", {"truncated": True}
    if (not isinstance(data, dict) or not _uuid(data.get("id"))
            or not isinstance(data.get("name"), str) or not data["name"].strip()
            or not isinstance(data.get("slug"), str)):
        return BLOCKED, "publisher identity metadata is incomplete or unsupported", {}
    projected = {k: data[k] for k in ("id", "name", "slug")}
    if data["slug"] != expected_slug:
        return FAILED, "publisher slug differs from the independently expected record", projected
    return PASSED, "complete publisher identity metadata", {"result": projected}


def _branch_result(content, expected):
    data = _object(content)
    if ((isinstance(data, dict) and data.get("truncated") is True)
            or (isinstance(content, str) and re.match(r'^\s*\{\s*"truncated"\s*:\s*true\b', content))):
        return BLOCKED, "branch result explicitly truncated", {"truncated": True}
    if isinstance(data, dict) and "data" in data:
        if "id" in data or "project_id" in data:
            return BLOCKED, "ambiguous branch response envelope", {}
        data = data["data"]
    if (not isinstance(data, dict) or data.get("truncated") is True
            or not _uuid(data.get("id")) or not _uuid(data.get("project_id"))
            or not isinstance(data.get("name"), str) or not data["name"].strip()):
        return BLOCKED, "branch identity metadata incomplete or unsupported", {}
    projected = {k: data[k] for k in ("id", "project_id", "name")}
    if any(data[k] != v for k, v in expected.items()):
        return FAILED, "branch/project differs from the independent expectation", projected
    return PASSED, "branch identity matches the recorded target", {"result": projected}


def tool_execution(case, items, captured):
    run = _run(case, items)
    if run is None or run.get("status") != "completed":
        return BLOCKED, "no completed correlated run", {}
    events = run.get("output_events")
    if (
        not captured
        or not isinstance(events, list)
        or not events
        or not all(isinstance(e, dict) for e in events)
    ):
        return (
            BLOCKED,
            "complete structured event trace unavailable; model text is insufficient",
            {},
        )
    calls = [(i, e) for i, e in enumerate(events) if e.get("type") == "tool_call"]
    results = [(i, e) for i, e in enumerate(events) if e.get("type") == "tool_result"]
    if len(calls) != 1 or len(results) != 1:
        return (
            FAILED,
            "expected exactly one tool call and one result",
            {"calls": len(calls), "results": len(results)},
        )
    ci, call = calls[0]
    ri, result = results[0]
    spec = case["employee_tool"]
    if (
        call.get("name") != spec["tool"]
        or _object(call.get("arguments")) != spec["arguments"]
        or not isinstance(call.get("id"), str)
        or not call["id"]
    ):
        return FAILED, "tool identity or arguments differ from the fixed request", {}
    if ri <= ci or result.get("id") != call["id"]:
        return BLOCKED, "tool result is not linked after its call", {}
    if result.get("is_error") is not False:
        return FAILED, "tool result is not explicitly successful", {}
    if spec.get("result_type", "database") == "utc_time":
        checked = _time_result(case, items, result.get("content"))
        if checked[0] != PASSED:
            return checked
        projected = checked[2]["result"]
    elif spec.get("result_type") == "branch_metadata":
        checked = _branch_result(result.get("content"), spec["result"])
        if checked[0] != PASSED:
            return checked
        projected = checked[2]["result"]
    elif spec.get("result_type") == "publisher_metadata":
        checked = _publisher_result(result.get("content"), spec["result"]["slug"])
        if checked[0] != PASSED:
            return checked
        projected = checked[2]["result"]
    else:
        data = _object(result.get("content"))
        if data is None or not isinstance(data.get("data"), dict):
            return (
                BLOCKED,
                "tool result has no supported structured database result",
                {},
            )
        data = data["data"]
        projected = {k: data.get(k) for k in ("columns", "row_count", "rows")}
        if json.dumps(projected, sort_keys=True) != json.dumps(
            spec["result"], sort_keys=True
        ):
            return (
                FAILED,
                "tool result differs from the independent expectation",
                {"observed": projected},
            )
    if any(e.get("type") in ("error", "guardrail_fail") for e in events):
        return FAILED, "trace contains an execution error", {}
    done = [e for e in events if e.get("type") == "done"]
    if len(done) != 1 or events[-1] is not done[0] or done[0].get("status") != "ok":
        return BLOCKED, "successful terminal event is missing or contradictory", {}
    return (
        PASSED,
        "one exact tool invocation has a linked successful structured result",
        {
            "tool": call["name"],
            "call_id": call["id"],
            "arguments": spec["arguments"],
            "result": projected,
            **(
                {
                    "observation_window": (_read(items, "initial") or {}).get(
                        "observation_window"
                    ),
                    "clock_tolerance_seconds": spec["clock_tolerance_seconds"],
                }
                if spec.get("result_type") == "utc_time"
                else {}
            ),
        },
    )


def employee_answer(case, items, captured):
    execution = tool_execution(case, items, captured)
    if execution[0] != PASSED:
        return (
            BLOCKED,
            "answer cannot establish execution without the linked tool result",
            {},
        )
    events = _run(case, items)["output_events"]
    result_index = next(
        i for i, e in enumerate(events) if e.get("type") == "tool_result"
    )
    texts = [
        e.get("text") for e in events[result_index + 1 :] if e.get("type") == "text"
    ]
    if not texts or not all(isinstance(t, str) for t in texts):
        return BLOCKED, "no complete employee answer after the result", {}
    answer = "".join(texts).strip()
    expected = (
        execution[2]["result"]
        if case["employee_tool"].get("result_type") == "utc_time"
        else case["employee_tool"]["answer"]
    )
    return (
        PASSED if answer == expected else FAILED,
        "employee answer agrees with the result"
        if answer == expected
        else "employee answer differs from the requested exact answer",
        {"answer": answer},
    )


def _trace(case, items, captured):
    """(events, None) for a completed, correlated run with a complete trace, else (None, why)."""
    run = _run(case, items)
    if run is None:
        return None, (BLOCKED, "run identity or invocation is not established", {})
    if run.get("status") != "completed":
        return None, (BLOCKED, "the requested run did not complete", {"status": run.get("status")})
    events = run.get("output_events")
    if (not captured or not isinstance(events, list) or not events
            or not all(isinstance(e, dict) for e in events)):
        return None, (BLOCKED, "complete structured event trace unavailable", {})
    done = [e for e in events if e.get("type") == "done"]
    if len(done) != 1 or events[-1] is not done[0]:
        return None, (BLOCKED, "terminal event is missing or contradictory", {})
    return events, None


def supplied_answer(case, items, captured):
    """Every `text` output event, in order, is the answer; scored by the frozen finance checker."""
    from seren_harness import finance_invoices
    events, problem = _trace(case, items, captured)
    if problem:
        return problem
    if events[-1].get("status") != "ok" or any(
            e.get("type") in ("error", "guardrail_fail") for e in events):
        return BLOCKED, "the run did not end successfully; its answer is not scored", {
            "done_status": events[-1].get("status")}
    texts = [e.get("text") for e in events if e.get("type") == "text"]
    if not all(isinstance(t, str) for t in texts):
        return BLOCKED, "a text event has no text", {}
    answer = "".join(texts)
    if "catalog_case" in case["employee_tool"]:
        from seren_harness import invoice_catalog
        result = invoice_catalog.check(case["employee_tool"]["catalog_case"], answer)
    else:
        result = finance_invoices.check(answer)
    return (PASSED if result["outcome"] == finance_invoices.PASSED else FAILED,
            "answer " + result["outcome"] + " against the frozen expected answer",
            {"outcome": result["outcome"], "problems": result["problems"],
             "answer": answer.strip()})


def no_tool_activity(case, items, captured):
    """Tool or action events are a separate failure; a correct answer cannot hide them."""
    events, problem = _trace(case, items, captured)
    if problem:
        return problem
    activity = [e for e in events if e.get("type") in ACTIVITY]
    if activity:
        counts = {}
        for e in activity:
            counts[e["type"]] = counts.get(e["type"], 0) + 1
        names = sorted({str(e.get("name") or e.get("tool")) for e in activity
                        if e.get("type") in ("tool_call", "tool_audit")})
        return FAILED, "the trace shows tool or action activity despite the no-tool instruction", {
            "events": counts, "tools": names}
    return PASSED, "no tool or action activity in the complete trace", {"events": len(events)}


def shutdown(case, items, captured):
    envelope = _read(items, "complete_view") or {}
    response = envelope.get("response")
    data = response.get("data") if isinstance(response, dict) else None
    if (
        envelope.get("status") != "ok"
        or envelope.get("exit_code") != 0
        or not isinstance(data, dict)
        or data.get("id") != case["employee_tool"]["deployment_id"]
    ):
        return BLOCKED, "authoritative shutdown read-back unavailable", {}
    # The driver must capture this after the run; timestamps are not inferred.
    run = _run(case, items)
    try:
        observed = datetime.fromisoformat(
            envelope["observed_at"].replace("Z", "+00:00")
        )
        completed = datetime.fromisoformat(run["completed_at"].replace("Z", "+00:00"))
        after = (
            observed.tzinfo is not None
            and completed.tzinfo is not None
            and observed >= completed
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        after = False
    if not after:
        return (
            BLOCKED,
            "shutdown observation is missing a usable time after completion",
            {},
        )
    ok = (
        data.get("status") == "stopped"
        and data.get("desired_lifecycle_state") == "stopped"
    )
    return (
        PASSED if ok else BLOCKED,
        "shutdown confirmed" if ok else "shutdown unconfirmed",
        {
            "status": data.get("status"),
            "desired_lifecycle_state": data.get("desired_lifecycle_state"),
        },
    )


def exact_approval(spec, pending, run_id):
    """Prepare a selective CloudDeploymentRunResumeRequest, never approve-all.

    Caller must already hold fresh, deployment-scoped run identity and live authority.
    This is only a pure request builder; it sends nothing and grants no authority.
    """
    data = pending.get("data") if isinstance(pending, dict) else None
    if not _uuid(run_id) or not isinstance(data, dict) or data.get("run_id") != run_id:
        raise ValueError("approval run identity is not established")
    approvals = data.get("pending_approvals")
    checkpoint = data.get("checkpoint_id")
    if (
        data.get("status") != "awaiting_approval"
        or not isinstance(checkpoint, str)
        or not checkpoint
        or not isinstance(approvals, list)
        or len(approvals) != 1
    ):
        raise ValueError("exactly one approval on an explicit checkpoint is required")
    call = approvals[0]
    if (
        not isinstance(call, dict)
        or call.get("tool") != spec["tool"]
        or call.get("args") != spec["arguments"]
        or not isinstance(call.get("id"), str)
        or not call["id"]
        or call.get("script_publisher_approval") is not None
    ):
        raise ValueError("pending call differs from the fixed approved scope")
    return {
        "resume_checkpoint_id": checkpoint,
        "approval_decisions": [{"id": call["id"], "decision": "approve"}],
    }
