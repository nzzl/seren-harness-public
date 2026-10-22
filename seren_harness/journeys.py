"""Customer-journey catalog: explicit candidates, cross-journey comparison, package binding.

Data and decisions only; no service access. Catalog membership grants no live authority.
A journey runs only through an allowlisted existing executor, under an active owner grant
bound to its frozen package digest, inside the shared Clock Test ledger and budgets.
Invoice variations are one supporting journey; they need a distinct customer reason.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalogs" / "customer-journeys.json"
PRIORITY = ("employee_retrieval", "saved_research", "lifecycle", "supporting")
GROUNDING = ("observed_workflow", "product_promise", "hypothetical_use")
AUTHORITY = ("standing_clock_test", "journey_grant", "offline")
SUPPORTING = "supplied-data.invoice-reconciliation"
# Existing executors only, never a command string. An adapter that cannot meet the
# shared ledger, durable-intent, no-replay and cleanup requirements is unavailable.
EXECUTORS = {
    "clock_supplied_answer": {
        "live": True, "available": True,
        "route": "seren_harness.live_discovery: standing Clock Test gate, tool-free answers"},
    "saved_research_review": {
        "live": False, "available": True,
        "route": "python -m seren_harness review-workflow over a saved operator capture"},
    "employee_branch_read": {
        "live": True, "available": True,
        "route": "seren_harness.retrieval_journey inside the live gate: fixed-host grant "
                 "transport, exact-grant check, ClockRun, CLI rollback and rebinding"},
    "publisher_lookup": {
        "live": True, "available": False,
        "reason": "Recipient must configure the lookup instruction; the route sits "
                  "outside the shared live ledger"},
    "lifecycle_journey": {
        "live": True, "available": False,
        "reason": "the lifecycle journey has no adapter to the shared live ledger or "
                  "standing budgets"},
    "dbread_campaign": {
        "live": True, "available": False,
        "reason": "recipient fixture and fresh authorization have not been configured"},
}
FIELDS = ("id", "priority", "title", "customer_task", "grounding", "uncertainty",
          "expected_result", "decision_if_pass", "decision_if_fail", "executor", "bindings",
          "evidence_required", "authority", "cleanup", "prior_evidence",
          "not_redundant_because", "prerequisites", "status")


class Refused(ValueError):
    pass


def _text(value, low=20):
    return isinstance(value, str) and len(value.strip()) >= low


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path=CATALOG):
    raw = json.loads(Path(path).read_text())
    entries = raw.get("entries")
    if raw.get("kind") != "journey_catalog" or not isinstance(entries, list) or not entries:
        raise ValueError("journey catalog needs kind and entries")
    if len({e.get("id") for e in entries}) != len(entries):
        raise ValueError("journey ids must be unique")
    for e in entries:
        problem = _problem(e)
        if problem:
            raise ValueError("journey " + str(e.get("id")) + ": " + problem)
    return raw


def _problem(e):
    if set(e) - set(FIELDS) - {"package", "closed_questions"} or any(k not in e for k in FIELDS):
        return "fields differ from the catalog schema"
    if e["priority"] not in PRIORITY or e["executor"] not in EXECUTORS:
        return "unknown priority or executor (only allowlisted existing executors)"
    if e["status"] not in ("candidate", "completed"):
        return "status must be candidate or completed"
    if (not isinstance(e["grounding"], dict) or e["grounding"].get("kind") not in GROUNDING
            or not _text(e["grounding"].get("source"))):
        return "grounding needs kind and source"
    expected = e["expected_result"]
    if not isinstance(expected, dict) or not _text(expected.get("statement")) \
            or not _text(expected.get("established_by")):
        return "expected result needs a statement and an independent source"
    if not all(_text(e[k]) for k in ("customer_task", "uncertainty", "decision_if_pass",
                                     "decision_if_fail", "not_redundant_because")):
        return "customer task, uncertainty, decisions and non-redundancy need concrete text"
    if not isinstance(e["authority"], dict) or e["authority"].get("kind") not in AUTHORITY:
        return "authority kind required"
    if EXECUTORS[e["executor"]]["live"] != (e["authority"]["kind"] != "offline"):
        return "offline executors need offline authority and live ones live authority"
    if not isinstance(e["bindings"], dict) or not e["bindings"]:
        return "exact resource bindings required"
    for key in ("evidence_required", "cleanup", "prior_evidence", "prerequisites"):
        if not isinstance(e[key], list):
            return key + " must be a list"
    if not e["evidence_required"]:
        return "evidence requirements required"
    if any(not {"id", "status", "need", "source"} <= set(p)
           or p["status"] not in ("met", "unmet") for p in e["prerequisites"]):
        return "prerequisites need id, met/unmet status, need and source"
    if any(not {"date", "result", "source"} <= set(p) for p in e["prior_evidence"]):
        return "prior evidence needs date, result and source"
    if "package" in e:
        package = e["package"] if isinstance(e["package"], dict) else {}
        if not isinstance(package.get("path"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", str(package.get("sha256"))):
            return "a package binds one frozen path and sha256"
    return None


def entry(catalog, journey_id):
    found = [e for e in catalog["entries"] if e["id"] == journey_id]
    if len(found) != 1:
        raise Refused("unknown journey")
    return found[0]


def package_digest(e, root=ROOT):
    package = e.get("package")
    if not package:
        return None
    actual = sha256(Path(root) / package["path"])
    if actual != package["sha256"]:
        raise Refused("journey package changed since the catalog froze it")
    return actual


def grant(authority, e, root=ROOT):
    """True only for an active owner grant naming this journey and its frozen package."""
    kind = e["authority"]["kind"]
    if kind == "offline":
        return True
    if not isinstance(authority, dict) or authority.get("approved") is not True:
        return False
    if kind == "standing_clock_test":
        return authority.get("session", {}).get("execution_scope") == e["authority"].get("scope")
    item = (authority.get("journeys") or {}).get(e["id"])
    return (isinstance(item, dict) and item.get("active") is True
            and item.get("approved") is True
            and item.get("package_sha256") == package_digest(e, root))


def blockers(e, authority, live_status, root=ROOT):
    if e["status"] == "completed":
        return ["completed: a repeat needs a new customer question, not the same journey"]
    found = []
    executor = EXECUTORS[e["executor"]]
    if not executor["available"]:
        found.append("executor " + e["executor"] + " unavailable: " + executor["reason"])
    found += ["prerequisite " + p["id"] + " unmet: " + p["need"]
              for p in e["prerequisites"] if p["status"] != "met"]
    if e["authority"]["kind"] == "journey_grant" and "package" not in e:
        found.append("no frozen package prepared for owner review")
    try:
        covered = grant(authority, e, root)
    except Refused as error:
        covered, found = False, found + [str(error)]
    digest = (e.get("package") or {}).get("sha256")
    if digest and any(a.get("journey_digest") == digest
                      for a in (live_status or {}).get("attempts", [])):
        found.append("package already attempted once; consumed, never replayed")
    if not covered:
        found.append("no active authority covers this journey (" + e["authority"]["kind"] + ")")
    if executor["live"] and covered and (live_status or {}).get("runs_remaining", 0) < 1:
        found.append("shared live budget exhausted or unknown; wait for the next UTC+07:00 day")
    return found


def compare(catalog, authority, live_status, root=ROOT):
    """One decision per journey, priority journeys first; blockers are fingerprinted."""
    rows = []
    for e in catalog["entries"]:
        found = blockers(e, authority, live_status, root)
        state = "completed" if e["status"] == "completed" else "blocked" if found else "eligible"
        rows.append({"id": e["id"], "priority": e["priority"], "title": e["title"],
                     "executor": e["executor"], "state": state, "blockers": found,
                     "fingerprint": hashlib.sha256(json.dumps(found).encode()).hexdigest()[:16]})
    rows.sort(key=lambda r: PRIORITY.index(r["priority"]))  # stable: catalog order within
    return rows


def remember(rows, previous, now):
    """Keep the first date of an unchanged blocker, so it is recorded once, not re-derived."""
    old = {r["id"]: r for r in (previous or {}).get("journeys", [])}
    for row in rows:
        before = old.get(row["id"], {})
        if row["state"] == "blocked":
            same = before.get("fingerprint") == row["fingerprint"] and before.get("blocked_since")
            row["blocked_since"] = before["blocked_since"] if same else now
    return rows


def next_action(rows):
    eligible = [r for r in rows if r["state"] == "eligible"]
    priority = [r for r in eligible if r["priority"] != "supporting"]
    if priority:
        return "investigate priority journey " + priority[0]["id"]
    if eligible:
        return ("only supporting journeys are eligible: request one only with a structured "
                "comparison and a distinct customer justification; otherwise wait quietly")
    return "no eligible journey: record the gap once and use the durable wait"


COMPARED = ("journey", "state", "fingerprint", "evidence", "customer_consequence",
            "new_information", "decision_changed")
REASONING = COMPARED[3:]


def validate_comparison(catalog, rows, comparison, selected):
    """Check a request's cross-journey comparison against the catalog and current state.

    Objective facts (which journeys, their states and blocker fingerprints, the cited
    evidence) must match; reasoning must be concrete and specific to each journey. This
    cannot prove value; it stops a citation or a copied paragraph standing in for a
    comparison. No scores: an eligible priority journey simply outranks supporting work.
    """
    if not isinstance(comparison, dict) or set(comparison) != {"considered",
                                                                "customer_justification"}:
        raise ValueError("comparison needs exactly considered and customer_justification")
    by_id = {r["id"]: r for r in rows}
    if selected not in by_id or by_id[selected]["state"] != "eligible":
        raise ValueError("selected journey is not eligible now")
    if by_id[selected]["priority"] == "supporting" and any(
            r["state"] == "eligible" and r["priority"] != "supporting" for r in rows):
        raise ValueError("an eligible priority journey outranks supporting work")
    required = {r["id"] for r in rows if r["priority"] != "supporting"
                and r["state"] != "completed"} | {selected}
    considered = comparison["considered"]
    if (not isinstance(considered, list) or any(not isinstance(c, dict) or set(c) != set(COMPARED)
                                                for c in considered)
            or sorted(c["journey"] for c in considered) != sorted(required)):
        raise ValueError("compare every open priority journey and the selected one, once each, "
                         "with " + ", ".join(COMPARED))
    for c in considered:
        row, e = by_id[c["journey"]], entry(catalog, c["journey"])
        if c["state"] != row["state"] or c["fingerprint"] != row["fingerprint"]:
            raise ValueError(c["journey"] + ": state or blocker fingerprint differs from "
                             "worker status; re-read it")
        if any(not isinstance(c[k], str) or not 30 <= len(c[k].strip()) <= 800
               for k in REASONING):
            raise ValueError(c["journey"] + ": reasoning fields need 30-800 characters each")
        refs = [p["source"] for p in e["prior_evidence"]] + [p["id"] for p in e["prerequisites"]]
        if not any(ref in c["evidence"] for ref in refs):
            raise ValueError(c["journey"] + ": evidence must cite one of its catalog prior-"
                             "evidence sources or prerequisite ids")
    for key in REASONING[1:]:
        texts = [" ".join(c[key].lower().split()) for c in considered]
        if len(set(texts)) != len(texts):
            raise ValueError(key + " must be specific to each compared journey")
    justification = comparison["customer_justification"]
    if not isinstance(justification, str) or not 40 <= len(justification.strip()) <= 800:
        raise ValueError("a distinct 40-800 character customer justification is required")
    if any(" ".join(justification.lower().split()) == " ".join(c[k].lower().split())
           for c in considered for k in REASONING):
        raise ValueError("customer justification must be distinct from the comparison text")
    return {"selected": selected, "considered": considered,
            "customer_justification": justification}


def authorize(catalog, authority, journey_id, digest, ledger, root=ROOT):
    """Trusted gate check before any CLI, reservation or service call."""
    e = entry(catalog, journey_id)
    if e["authority"]["kind"] != "journey_grant" or e["status"] != "candidate":
        raise Refused("journey is not a grantable package")
    if not digest or package_digest(e, root) != digest:
        raise Refused("queued journey package differs from the frozen package")
    if any(p["status"] != "met" for p in e["prerequisites"]):
        raise Refused("journey prerequisites are unmet")
    if not grant(authority, e, root):
        raise Refused("no active owner grant for this journey package")
    if any(a.get("journey_digest") == digest for a in ledger.get("attempts", [])):
        raise Refused("journey package already attempted; never replayed")
    return e


GATE_ADAPTERS = {"employee_branch_read": "seren_harness.retrieval_journey"}


def adapter(e):
    executor = EXECUTORS[e["executor"]]
    if not executor["available"]:
        raise Refused("executor unavailable: " + executor["reason"])
    if e["executor"] not in GATE_ADAPTERS:
        raise Refused("no gate adapter for journey executor " + e["executor"])
    return GATE_ADAPTERS[e["executor"]]


# ---------------------------------------------------------------- retrieval outcomes

EXECUTION = {"synthetic": "none (stand-in)", "saved_evidence": "recorded past run, no new call",
             "live": "new bounded run"}
DENIED = ("permission_denied", "tool_permission_denied", "unauthorized", "forbidden")


def observe(events):
    """Normalize saved output events (the shape the executor captures) for classification."""
    calls, results, answer = [], {}, None
    for event in events:
        if event.get("type") == "tool_call":
            calls.append(event)
        elif event.get("type") == "tool_result":
            results[event.get("id")] = event
        elif event.get("type") == "text":
            answer = event.get("text")
    observed = []
    for call in calls:
        try:
            arguments = json.loads(call.get("arguments") or "null")
        except ValueError:
            arguments = None
        result = results.get(call.get("id"))
        observed.append({"tool": call.get("name"), "arguments": arguments,
                         "correlated": result is not None, "result": _result(result)})
    return {"invocations": observed, "answer": answer}


def _result(event):
    if event is None:
        return {"kind": "absent"}
    recovery = event.get("recovery") or {}
    if event.get("is_error"):
        codes = {str(event.get("code", "")).lower(), str(recovery.get("error_class", "")).lower(),
                 str(recovery.get("reason_code", "")).lower()}
        kind = "denied" if codes & set(DENIED) or recovery.get("upstream_status") in (401, 403) \
            else "error"
        return {"kind": kind, "code": event.get("code"),
                "upstream_status": recovery.get("upstream_status")}
    try:
        content = json.loads(event.get("content") or "")
    except ValueError:
        return {"kind": "truncated"}
    if isinstance(content, dict) and content.get("truncated"):
        return {"kind": "truncated"}
    record = content.get("data") if isinstance(content, dict) and set(content) == {"data"} \
        else content
    if not isinstance(record, dict):
        return {"kind": "unsupported"}
    return {"kind": "success", "record": record}


def classify(observation, expected, *, provenance, run_identity, shutdown, restore,
             grant_verified):
    """Separate Seren functional failures, model-answer failures and unresolved questions.

    provenance: synthetic (stand-in, never executed), saved_evidence (a recorded past run,
    reclassified offline) or live (a new run). Only live input can become a confirmed bug.
    """
    if provenance not in EXECUTION:
        raise ValueError("unknown provenance")
    reasons, inability = [], "not_applicable"
    calls = observation.get("invocations") or []
    answer = (observation.get("answer") or "").strip()
    if run_identity != "match":
        kind = "incomplete"
        reasons.append("run identity " + run_identity + ": evidence is not attributed to the "
                       "requested employee run and is never scored")
    elif not calls:
        if answer == expected["answer"]:
            kind = "model_answer_failure"
            reasons.append("instructed success reply given without any retrieval call")
        else:
            kind, inability = "incomplete", "needs_review" if answer else "no_answer"
            reasons.append("no retrieval call observed; cause unattributed")
    elif len(calls) != 1 or calls[0]["tool"] != expected["tool"] \
            or calls[0]["arguments"] != expected["arguments"]:
        kind = "model_answer_failure"
        reasons.append("invocation differs from the one exact instructed request")
    elif not calls[0]["correlated"] or calls[0]["result"]["kind"] in ("absent", "truncated",
                                                                      "unsupported"):
        kind = "incomplete"
        reasons.append("result " + calls[0]["result"]["kind"] + " or uncorrelated; never scored")
    elif calls[0]["result"]["kind"] in ("denied", "error"):
        result = calls[0]["result"]
        detail = "%s (upstream %s)" % (result.get("code"), result.get("upstream_status"))
        if result["kind"] == "denied" and not grant_verified:
            kind = "unresolved_contract_or_attribution"
            reasons.append("access denied " + detail + " while the required grant was not "
                           "verified in place")
        else:
            kind = "seren_functional_failure"
            reasons.append("documented request failed " + detail)
        # Structure only: the bare instructed success reply presents a value never
        # retrieved. Any other text, even one quoting the requested id while saying the
        # call failed, needs a reviewer's reading; keywords and ids decide nothing.
        if answer == expected["answer"]:
            inability = "presented_unretrieved_value"
            kind = "model_answer_failure"
            reasons.append("instructed success reply given after the call failed")
        else:
            inability = "needs_review" if answer else "no_answer"
    else:
        record = calls[0]["result"]["record"]
        if not record.get("id") or not record.get("project_id") or not record.get("name"):
            kind = "incomplete"
            reasons.append("record identity fields missing; never scored")
        elif record["id"] != expected["result"]["id"] \
                or record["project_id"] != expected["result"]["project_id"]:
            kind = "seren_functional_failure"
            reasons.append("service returned a different record than the requested path")
        elif answer != expected["answer"]:
            kind = "model_answer_failure"
            reasons.append("answer differs from the correlated returned identifier")
        else:
            kind = "passed"
            reasons.append("correlated record matched and the answer repeated its id")
    # "not_required" means execution never started and nothing was changed, so no stop or
    # restore was needed; it is never a confirmed shutdown and never a passed retrieval.
    cleanup_open = (shutdown not in ("confirmed", "not_required")
                    or restore not in ("confirmed", "not_needed"))
    if shutdown == "not_required" and not cleanup_open:
        reasons.append("no cleanup required: execution never started and nothing changed; "
                       "shutdown was not observed")
        if kind == "passed":
            kind = "incomplete"
    if cleanup_open:
        reasons.append("cleanup unconfirmed (shutdown %s, restore %s): obligation stays open"
                       % (shutdown, restore))
        if kind == "passed":
            kind = "incomplete"
    outcome = {"passed": "passed", "incomplete": "incomplete"}.get(kind, "failed")
    return {"outcome": outcome, "classification": kind, "inability_report": inability,
            "reasons": reasons, "provenance": provenance,
            "service_execution": EXECUTION[provenance],
            "customer_impact": ("hypothetical: synthetic input" if provenance == "synthetic"
                                else "unknown exposure; one bounded test run"),
            "cleanup_open": cleanup_open,
            "confirmable": (provenance == "live" and not cleanup_open and kind in
                            ("seren_functional_failure", "model_answer_failure")),
            "finding_key": "journey-retrieval-" + kind + "-" + hashlib.sha256(
                reasons[0].encode()).hexdigest()[:12]}


LABELS = {
    "passed": "Passed",
    "seren_functional_failure": "Observed functional failure through Seren; component-level "
                                "cause not established",
    "model_answer_failure": "Model-answer failure: the answer broke the stated rules; model "
                            "versus Seren runtime attribution not established",
    "unresolved_contract_or_attribution": "Unresolved contract or attribution question",
    "incomplete": "Incomplete evidence",
}
CONFIRMABLE = ("seren_functional_failure", "model_answer_failure")
# Discord's 2,000-character limit leaves room for one compact line; issues keep LABELS.
SHORT = {"passed": "Passed", "seren_functional_failure": "Seren functional failure; cause unknown", "model_answer_failure": "Model-answer failure; cause unattributed",
         "unresolved_contract_or_attribution": "Unresolved contract/attribution",
         "incomplete": "Incomplete evidence"}


def classify_attempt(item):
    """Automatic classification of a handoff outcome from structured gate evidence only."""
    result = item.get("result") or {}
    if item.get("status") != "finished" or result.get("operational_complete") is not True:
        return "incomplete"
    if result.get("classification") in LABELS:  # a journey adapter's own classification
        return result["classification"]
    return {"passed": "passed", "failed": "model_answer_failure"}.get(result.get("verdict"),
                                                                       "incomplete")


def provenance(item):
    """What the evidence is: input kind and whether a service execution happened."""
    executed = item.get("status") == "finished" and bool(item.get("result"))
    package = bool(item.get("journey"))
    return {"input": ("frozen journey package inputs" if package
                      else "synthetic supplied records (no real customer data)"),
            "execution": ("one bounded live run through Seren" if executed
                          else "no service execution"),
            "short": ("package input" if package else "synthetic input") + " · "
                     + ("live Seren run" if executed else "no live run")}


def describe(result):
    lines = ["**Outcome:** %s (%s)" % (result["outcome"], result["classification"]),
             "**Input:** %s; service execution: %s" % (result["provenance"],
                                                      result["service_execution"]),
             "**Customer impact:** " + result["customer_impact"],
             "**Inability report:** " + result["inability_report"]]
    lines += ["- " + r for r in result["reasons"]]
    return "\n".join(lines)
