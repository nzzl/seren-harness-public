"""Offline functional review of one customer workflow from a bundle of observed responses.

The case declares scope, required criteria and evidence requirements before evaluation;
business expectations live there, never here. Scoped success needs every required criterion
passed on evidence whose provenance the scope accepts. A failure stands whatever else passed,
and missing evidence stays unverified. Text checks establish textual consistency only; meaning
needs an attributed operator assessment. Failures become deduplicated findings with private
drafts, and a retest links to its finding while keeping its own evidence under a separate run.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from seren_harness import __version__, assertions, employee_tool
from seren_harness.findings import finding_key
from seren_harness.models import (BLOCKED, FAILED, FINDING_CLASSES, LIVE, PASSED, RUN_COMPLETE,
                                  SIMULATION, Finding, StepResult, utc_now)
from seren_harness.report import _write_private, finding_status, write_draft
from seren_harness.response_review import read_json, read_text
from seren_harness.sanitize import Sanitizer
from seren_harness.state import StateError, Store

LOCAL = "local"
MODES = {"operator_supplied": LIVE, "independently_observed": LIVE, "local": LOCAL,
         "simulated": SIMULATION}
KINDS = ("discord_response", "button_response", "attachment", "web_view", "api_response")
READING = ("initial", "continuation", "complete_view")
ROLES = READING + ("alternative",)
CATEGORIES = ("usability", "textual_consistency", "human_assessment", "source_verification",
              "service_outcome")
VERDICTS = {PASSED: "passed", FAILED: "failed", BLOCKED: "unverified"}
NO_RELATED = {"status": "unavailable", "reason": "offline functional review; no search run"}


def _norm(text):
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_bundle(path):
    bundle = read_json(path)
    base = Path(path).parent
    items = bundle.get("items")
    if bundle.get("provenance") not in MODES or not isinstance(items, list) or not items:
        raise ValueError("bundle needs a known provenance and observed items")
    if len({i.get("id") for i in items}) != len(items):
        raise ValueError("bundle item IDs must be unique")
    loaded = []
    for item in items:
        if (not isinstance(item.get("id"), str) or item.get("kind") not in KINDS
                or item.get("role") not in ROLES or not isinstance(item.get("path"), str)
                or not isinstance(item.get("invocation", ""), (str, type(None)))):
            raise ValueError("bundle items need id, kind, role, path and optional invocation")
        loaded.append(dict(item, text=read_text(base / item["path"])))
    return bundle, loaded


def _validate(case):
    required = ("id", "version", "command", "claim", "scope", "basis", "expectation_date",
                "criteria", "findings", "unverified_always")
    if any(k not in case for k in required):
        raise ValueError("case is missing its scope, criteria or findings")
    scope = case["scope"]
    if (not isinstance(scope.get("statement"), str) or not scope.get("accepted_provenance")
            or any(p not in MODES for p in scope["accepted_provenance"])):
        raise ValueError("case scope needs a statement and accepted provenance")
    ids = [c.get("id") for c in case["criteria"]]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("case criteria need unique IDs")
    for c in case["criteria"]:
        if (c.get("category") not in CATEGORIES or c.get("check") not in CHECKS
                or not isinstance(c.get("required"), bool) or not c.get("claim")
                or not c.get("evidence")):
            raise ValueError("criterion " + str(c.get("id")) + " is incomplete")
        # Every criterion states its finding, or why it belongs to none; an omitted
        # association would otherwise silently drop that criterion from finding retests.
        if "finding" not in c:
            raise ValueError("criterion " + str(c["id"]) + " must declare its finding")
        if c["finding"] is None:
            if not isinstance(c.get("finding_exclusion"), str) or not c["finding_exclusion"]:
                raise ValueError("criterion " + str(c["id"]) + " has no finding and no "
                                 "finding_exclusion reason")
        elif c["finding"] not in case["findings"]:
            raise ValueError("criterion " + str(c["id"]) + " names an unknown finding")
    _validate_api(case)
    if any(c["check"].startswith("employee_") for c in case["criteria"]):
        employee_tool.validate(case)
    if case.get("target_status", "resolved") not in ("resolved", "unresolved"):
        raise ValueError("case target_status must be resolved or unresolved")
    for fid, f in case["findings"].items():
        if f.get("classification") not in FINDING_CLASSES or not f.get("title"):
            raise ValueError("finding templates need a classification and title")
        if not any(c["finding"] == fid and c["required"] for c in case["criteria"]):
            raise ValueError("finding " + fid + " has no required criterion")


def _reading(items):
    return [i for i in items if i["role"] in READING]


def _missing(case, key, text):
    return [x["id"] for x in case[key] if _norm(x["text"]) not in text]


def _split(field, texts):
    """True when the longest prefix found in one message is completed by another message,
    whatever page chrome sits between them."""
    for i, text in enumerate(texts):
        cut = next((n for n in range(len(field) - 1, 19, -1) if field[:n] in text), 0)
        if cut and any(field[cut:].strip() in other for j, other in enumerate(texts) if j != i):
            return True
    return False


def _placement(case, items):
    """Per saved field: whole in one message, split across messages, or not observed."""
    texts = [_norm(i["text"]) for i in _reading(items)]
    whole, split, unseen = [], [], []
    for field in case["complete_fields"]:
        text = _norm(field["text"])
        bucket = whole if any(text in t for t in texts) else split if _split(text, texts) \
            else unseen
        bucket.append(field["id"])
    return whole, split, unseen


def _initial(case, items, captured):
    initial = [i for i in items if i["role"] == "initial"]
    if len(initial) != 1:
        return BLOCKED, "exactly one initial response is required", {"initial": len(initial)}
    text = _norm(initial[0]["text"]).casefold()
    missing = [e["id"] for e in case["initial_elements"]
               if not any(_norm(a).casefold() in text for a in e["any_of"])]
    return (FAILED if missing else PASSED), "elements checked in the first response only", {
        "item": initial[0]["id"], "missing": missing,
        "present": [e["id"] for e in case["initial_elements"] if e["id"] not in missing]}


def _complete(case, items, captured):
    reading = _reading(items)
    if not reading:
        return BLOCKED, "no reading-path evidence", {}
    pattern = re.compile(case["scope"]["page_command_pattern"])
    paged = [i["id"] for i in reading if pattern.search(i.get("invocation") or "")]
    missing = _placement(case, items)[2]
    evidence = {"page_command_items": paged, "missing_fields": missing,
                "responses": sum(i["kind"] in ("discord_response", "button_response")
                                 for i in reading),
                "files_or_views": sum(i["kind"] in ("attachment", "web_view") for i in reading)}
    if paged:
        return FAILED, str(len(paged)) + " reading step(s) needed manual page commands", evidence
    if missing:
        return (FAILED if captured else BLOCKED), ("saved fields absent from the captured "
                "complete reading" if captured else "complete reading not captured"), evidence
    return PASSED, "all saved fields reached without page commands", evidence


def _whole(case, items, captured):
    _, split, unseen = _placement(case, items)
    evidence = {"split_across_messages": split, "not_observed": unseen}
    if split:
        return FAILED, "saved text split across messages", evidence
    if unseen:
        return (FAILED if captured else BLOCKED), "saved text not observed", evidence
    return PASSED, "every saved field appears whole in one response or file", evidence


def _headings(case, items, captured):
    texts = [_norm(i["text"]) for i in _reading(items)]
    separated, unseen = [], []
    for pair in case["heading_pairs"]:
        heading, content = _norm(pair["heading"]), _norm(pair["content"])
        if any(heading in t and content in t for t in texts):
            continue
        both = any(heading in t for t in texts) and any(content in t for t in texts)
        (separated if both else unseen).append(pair["id"])
    evidence = {"separated": separated, "not_observed": unseen}
    if separated:
        return FAILED, "a heading was separated from its first item", evidence
    if unseen:
        return (FAILED if captured else BLOCKED), "heading or item not observed", evidence
    return PASSED, "headings appear with their first item", evidence


def _no_json(case, items, captured):
    pattern = re.compile(case["scope"]["raw_json_pattern"])
    found = [i["id"] for i in _reading(items) if pattern.search(i["text"])]
    if found:
        return FAILED, "raw JSON on the reading path", {"items": found}
    if not captured:
        return BLOCKED, "complete reading not captured", {"items": []}
    return PASSED, "no raw JSON on the captured reading path", {"items": []}


def _sources(case, items, captured):
    missing = _missing(case, "source_texts", _norm(" ".join(i["text"] for i in _reading(items))))
    if missing:
        return (FAILED if captured else BLOCKED), "source reference or date not observed", {
            "missing": missing}
    return PASSED, "source reference and observation date visible", {"missing": []}


def _not_evaluated(case, items, captured):
    return BLOCKED, "outside the required scope; not evaluated", {}


# Service answers: one `api_response` item holding the harness's envelope of a supported
# interface call ({"status", "exit_code", "stderr_tail", "response"}). The case's `api` block
# declares the target and the expectations; nothing here is specific to one service.
def _answer(items):
    answers = [i for i in items if i["kind"] == "api_response" and i["role"] == "initial"]
    if len(answers) != 1:
        return None
    try:
        envelope = json.loads(answers[0]["text"])
    except ValueError:
        return None
    return envelope if isinstance(envelope, dict) else None


def _payload(case, items):
    """The documented response object, or None when the call returned none."""
    envelope = _answer(items)
    if envelope is None or envelope.get("status") != "ok":
        return None
    payload = envelope.get("response")
    for key in case["api"].get("data_path", []):
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload if isinstance(payload, dict) else None


def _api_answered(case, items, captured):
    envelope = _answer(items)
    if envelope is None:
        return BLOCKED, "no service answer was captured", {}
    status = envelope.get("status")
    evidence = {"status": status, "exit_code": envelope.get("exit_code")}
    if status == "ok":
        if _payload(case, items) is None:
            return FAILED, "the answer lacks the documented response object", evidence
        return PASSED, "the service returned the documented response object", evidence
    # Only an answer the case declares as a service-side refusal of this target is a failure;
    # authentication, permission or unclassified errors leave the outcome unverified.
    tail = str(envelope.get("stderr_tail", ""))
    matched = [p for p in case["api"]["failure_patterns"] if re.search(p, tail, re.I)]
    evidence["matched_failure_patterns"] = matched
    if matched:
        return FAILED, "the service refused the target", evidence
    return BLOCKED, "the call did not return a usable answer (access or unclassified error)", \
        evidence


def _api_equals(case, items, captured):
    payload = _payload(case, items)
    if payload is None:
        return BLOCKED, "no response object to check", {}
    expected = case["api"]["equals"]
    different = {k: payload.get(k) for k, v in expected.items() if payload.get(k) != v}
    return ((FAILED, "the response describes a different target", {"different": different})
            if different else (PASSED, "the response describes the requested target",
                               {"checked": sorted(expected)}))


def _api_result_equals(case, items, captured):
    payload = _payload(case, items)
    if payload is None:
        return BLOCKED, "no result object to check", {}
    expected = case["api"]["expected_result"]
    # Compare JSON values including types: true is not the numeric answer 1.
    passed = json.dumps(payload, sort_keys=True) == json.dumps(expected, sort_keys=True)
    return (PASSED if passed else FAILED, "query result matches" if passed else
            "query result differs from the independently specified expectation",
            {"expected": expected, "observed": payload})


def _entries(case, payload):
    values = payload.get(case["api"]["list_field"])
    return values if isinstance(values, list) else None


def _matches(entry, tokens):
    # The exact entry format is not documented; an entry matches a declared item when every
    # token of that item appears in it (case-insensitive). Recorded as a limitation.
    text = (entry if isinstance(entry, str) else json.dumps(entry, sort_keys=True)).casefold()
    return all(t.casefold() in text for t in tokens)


def _api_declared_present(case, items, captured):
    payload = _payload(case, items)
    entries = _entries(case, payload) if payload is not None else None
    if entries is None:
        return BLOCKED, "no " + case["api"]["list_field"] + " list to check", {}
    missing = [d["id"] for d in case["api"]["declared"]
               if not any(_matches(e, d["tokens"]) for e in entries)]
    return ((FAILED, "declared items absent from the response", {"missing": missing})
            if missing else (PASSED, "every declared item is present", {"missing": []}))


def _api_none_prohibited(case, items, captured):
    """Only items matching a documented prohibition fail. Items that are neither declared nor
    recognisably prohibited are listed as unclassified rather than judged."""
    payload = _payload(case, items)
    entries = _entries(case, payload) if payload is not None else None
    if entries is None:
        return BLOCKED, "no " + case["api"]["list_field"] + " list to check", {}
    prohibited = [{"entry": e, "prohibition": p["id"]} for e in entries
                  for p in case["api"]["prohibited"] if _matches(e, p["tokens"])]
    unclassified = [e for e in entries if not any(_matches(e, d["tokens"])
                                                  for d in case["api"].get("declared", []))
                    and not any(_matches(e, p["tokens"]) for p in case["api"]["prohibited"])]
    evidence = {"prohibited": prohibited, "unclassified": unclassified}
    if prohibited:
        return FAILED, "the response grants a prohibited item", evidence
    return PASSED, ("no prohibited item" + ("; " + str(len(unclassified)) + " item(s) not "
                    "classifiable from the documented terms, listed, not judged"
                    if unclassified else "")), evidence


def _typed(value, kind):
    if kind == "present":
        return True
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, {"string": str, "object": dict}[kind])


def _api_fields_in(case, items, captured):
    """Check independently declared scalar enums without equating different state fields."""
    payload = _payload(case, items)
    if payload is None:
        return BLOCKED, "no response object to check", {}
    fields = case["api"]["field_values"]
    observed = {name: payload.get(name) for name in fields}
    invalid = [name for name, allowed in fields.items()
               if not isinstance(observed[name], str) or observed[name] not in allowed]
    evidence = {"observed": observed, "invalid_fields": invalid}
    if invalid:
        return FAILED, "required fields are missing or outside their documented values", evidence
    return PASSED, "required fields each contain a documented value", evidence


# Answer classes for `api_answer_class`. A case maps EVERY class to pass, fail or unverified
# before execution, so access, fixture, service and unclassified errors stay distinct and an
# unknown error never becomes a pass or a failure. Error text is matched in this order.
ANSWER_CLASSES = ("record", "missing_object", "deployment_missing", "auth_rejected",
                  "forbidden", "service_error", "not_found", "unclassified")
_ERROR_PATTERNS = (
    ("deployment_missing", r"deployment not found"),
    ("auth_rejected", r"\b401\b|unauthori[sz]ed|not authenticated|session (has )?expired"),
    ("forbidden", r"\b403\b|forbidden|permission denied"),
    ("service_error", r"\b5\d\d\b|internal server error|bad gateway|service unavailable|"
                      r"gateway time-?out"),
    ("not_found", r"\b404\b|not found"))
_OUTCOMES = {"pass": PASSED, "fail": FAILED, "unverified": BLOCKED}


def answer_class(case, items):
    """(class, evidence) for the single captured service answer, or (None, {}) if none."""
    envelope = _answer(items)
    if envelope is None:
        return None, {}
    evidence = {"status": envelope.get("status"), "exit_code": envelope.get("exit_code")}
    if envelope.get("status") == "ok":
        payload = _payload(case, items)
        if payload is None:
            return "missing_object", evidence
        evidence["observed"] = {k: payload.get(k) for k in ("id", "deployment_id")}
        return "record", evidence
    tail = str(envelope.get("stderr_tail", ""))
    for name, pattern in _ERROR_PATTERNS:
        if re.search(pattern, tail, re.I):
            return name, evidence
    return "unclassified", evidence


def _api_answer_class(case, items, captured):
    name, evidence = answer_class(case, items)
    if name is None:
        return BLOCKED, "no service answer was captured", {}
    evidence["answer_class"] = name
    outcome = case["api"]["answer_outcomes"][name]
    return _OUTCOMES[outcome], "answer class " + name + " (declared " + outcome + ")", evidence


def _api_required_fields(case, items, captured):
    """Documented required fields with their documented JSON types. A successful exit
    without the documented object is itself a contract failure."""
    envelope = _answer(items)
    if envelope is None or envelope.get("status") != "ok":
        return BLOCKED, "no successful answer to check against the contract", {}
    payload = _payload(case, items)
    if payload is None:
        return FAILED, "a successful answer lacks the documented response object", {}
    bad = [name for name, kind in case["api"]["required_fields"].items()
           if name not in payload or payload[name] is None or not _typed(payload[name], kind)]
    return ((FAILED, "required fields are missing or of the wrong type", {"invalid": bad})
            if bad else (PASSED, "every documented required field is present and typed",
                         {"fields": len(case["api"]["required_fields"])}))


def _instant(value):
    match = isinstance(value, str) and re.fullmatch(
        r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        return None
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    whole = datetime.fromisoformat(match.group(1) + zone).astimezone(timezone.utc)
    return whole.isoformat(), int((match.group(2) or "0").ljust(9, "0"))


def _api_baseline(case, items, captured):
    """Consistency with a dated earlier observation. A difference is evidence of change or
    inconsistency, not a contract failure: the contract does not make history immutable."""
    payload = _payload(case, items)
    if payload is None:
        return BLOCKED, "no response object to compare with the dated baseline", {}
    baseline = case["api"]["baseline"]
    different = {}
    for name, expected in baseline["fields"].items():
        observed = payload.get(name)
        same = (_instant(observed) == _instant(expected) and _instant(expected) is not None
                if name in baseline.get("instants", ()) else observed == expected)
        if not same:
            different[name] = {"baseline": expected, "observed": observed}
    evidence = {"baseline": baseline["source"], "dated": baseline["date"],
                "checked": sorted(baseline["fields"]), "different": different}
    return ((FAILED, "differs from the dated baseline", evidence) if different else
            (PASSED, "consistent with the dated baseline", evidence))


def _api_entries_have(case, items, captured):
    payload = _payload(case, items)
    rule = case["api"]["entries"]
    entries = payload.get(rule["field"]) if payload is not None else None
    if not isinstance(entries, list):
        return BLOCKED, "no " + rule["field"] + " list to check", {}
    incomplete = []
    for n, entry in enumerate(entries):
        bad = [name for name, kind in rule["fields"].items()
               if not isinstance(entry, dict) or name not in entry
               or not _typed(entry[name], kind)]
        if bad:
            incomplete.append({"entry": n, "name": entry.get("name") if isinstance(entry, dict)
                               else None, "fields": bad})
    return ((FAILED, "entries lack documented fields", {"incomplete": incomplete})
            if incomplete else (PASSED, "every entry carries the documented fields",
                                {"entries": len(entries)}))


CHECKS = {"initial_elements": _initial, "complete_without_pages": _complete,
          "fields_whole": _whole, "heading_pairs": _headings, "no_raw_json": _no_json,
          "source_texts": _sources, "not_evaluated": _not_evaluated, "operator": None,
          "api_answered": _api_answered, "api_equals": _api_equals,
          "api_declared_present": _api_declared_present,
          "api_none_prohibited": _api_none_prohibited,
          "api_entries_have": _api_entries_have, "api_fields_in": _api_fields_in,
          "api_result_equals": _api_result_equals,
          "api_answer_class": _api_answer_class,
          "api_required_fields": _api_required_fields, "api_baseline": _api_baseline,
          "employee_run_identity": employee_tool.run_identity,
          "employee_tool_execution": employee_tool.tool_execution,
          "employee_tool_answer": employee_tool.employee_answer,
          "employee_supplied_answer": employee_tool.supplied_answer,
          "employee_no_tool_activity": employee_tool.no_tool_activity,
          "employee_tool_shutdown": employee_tool.shutdown}

# What each service-answer check needs from the case's `api` block.
_API_NEEDS = {"api_answered": ("failure_patterns",), "api_equals": ("equals",),
              "api_declared_present": ("list_field", "declared"),
              "api_none_prohibited": ("list_field", "prohibited"),
              "api_entries_have": ("entries",), "api_fields_in": ("field_values",),
              "api_result_equals": ("expected_result",),
              "api_answer_class": ("answer_outcomes",),
              "api_required_fields": ("required_fields",), "api_baseline": ("baseline",)}


def _token_items(value):
    return isinstance(value, list) and value and all(
        isinstance(d, dict) and isinstance(d.get("id"), str) and isinstance(d.get("tokens"), list)
        and d["tokens"] and all(isinstance(t, str) and t for t in d["tokens"]) for d in value)


def _validate_api(case):
    needs = {n for c in case["criteria"] for n in _API_NEEDS.get(c["check"], ())}
    if not needs:
        return
    api = case.get("api")
    shapes = {"failure_patterns": lambda v: isinstance(v, list),
              "equals": lambda v: isinstance(v, dict) and v,
              "expected_result": lambda v: isinstance(v, dict) and v,
              "field_values": lambda v: isinstance(v, dict) and v and all(
                  isinstance(k, str) and k and isinstance(values, list) and values
                  and all(isinstance(x, str) and x for x in values)
                  for k, values in v.items()),
              "list_field": lambda v: isinstance(v, str) and v,
              "declared": _token_items, "prohibited": _token_items,
              "entries": lambda v: isinstance(v, dict) and isinstance(v.get("field"), str)
              and isinstance(v.get("fields"), dict) and v["fields"],
              # The full table, decided before execution: every class, known outcomes only.
              "answer_outcomes": lambda v: isinstance(v, dict) and set(v) == set(ANSWER_CLASSES)
              and all(o in _OUTCOMES for o in v.values()),
              "required_fields": lambda v: isinstance(v, dict) and v and all(
                  k in ("present", "boolean", "integer", "string", "object")
                  for k in v.values()),
              "baseline": lambda v: isinstance(v, dict) and isinstance(v.get("source"), str)
              and isinstance(v.get("date"), str) and isinstance(v.get("fields"), dict)
              and v["fields"] and set(v.get("instants", ())) <= set(v["fields"])}
    if not isinstance(api, dict) or any(not shapes[n](api.get(n)) for n in needs):
        raise ValueError("service-answer criteria need a complete case `api` block")


def _operator(criterion, review, assessor, clean_texts):
    if not isinstance(review, dict) or review.get("verdict", "unverified") not in (
            "passed", "failed", "unverified"):
        raise ValueError("invalid operator assessment")
    verdict = review.get("verdict", "unverified")
    excerpt, note = review.get("excerpt", ""), review.get("note", "")
    if not isinstance(excerpt, str) or not isinstance(note, str):
        raise ValueError("assessment excerpts and notes must be text")
    if verdict != "unverified":
        if not isinstance(assessor, str) or not assessor.strip():
            raise ValueError("a decided assessment requires a named assessor")
        if (not note.strip() or (verdict == "passed" and not excerpt.strip())
                or (excerpt and not any(excerpt in t for t in clean_texts))):
            raise ValueError("a decision needs a reason, and a pass an exact excerpt from "
                             "the sanitized evidence")
    outcome = {"passed": PASSED, "failed": FAILED, "unverified": BLOCKED}[verdict]
    return outcome, ("operator assessment" if verdict != "unverified" else
                     "operator assessment required"), {
        "expected": criterion.get("expected", ""), "excerpt": excerpt, "note": note,
        "assessor": assessor if verdict != "unverified" else None}


def evaluate(case, bundle, items, assessment=None):
    """Return (report, raw step results). Checks read raw text in memory; the report is
    sanitized before anything is persisted."""
    _validate(case)
    sanitizer = Sanitizer()
    contract = assertions.snapshot(case)
    captured = bool(bundle.get("complete_reading_captured"))
    bundle_digest = _digest({"provenance": bundle["provenance"],
                             "revision": bundle.get("revision"), "captured": captured,
                             "items": [{k: i.get(k) for k in ("id", "kind", "role", "invocation")}
                                       | {"sha256": hashlib.sha256(i["text"].encode()).hexdigest()}
                                       for i in items]})
    assessment = assessment or {}
    reviews = assessment.get("checks", {})
    operator_ids = [c["id"] for c in case["criteria"] if c["check"] == "operator"]
    if not isinstance(reviews, dict) or any(k not in operator_ids for k in reviews):
        raise ValueError("assessment contains unknown criteria")
    if assessment and (assessment.get("bundle_sha256") != bundle_digest
                       or assessment.get("case_digest") != contract["digest"]):
        raise ValueError("assessment does not belong to this evidence and expectation")
    clean_texts = [sanitizer.text(i["text"]) for i in items]
    accepted = bundle["provenance"] in case["scope"]["accepted_provenance"]
    criteria, results = [], {}
    for criterion in case["criteria"]:
        if criterion["check"] == "operator":
            outcome, summary, evidence = _operator(
                criterion, reviews.get(criterion["id"], {}), assessment.get("assessor"),
                clean_texts)
        else:
            outcome, summary, evidence = CHECKS[criterion["check"]](case, items, captured)
        results[criterion["id"]] = StepResult(outcome, summary, evidence)
        criteria.append({"id": criterion["id"], "category": criterion["category"],
                         "required": criterion["required"], "claim": criterion["claim"],
                         "evidence_required": criterion["evidence"],
                         "finding": criterion.get("finding"), "verdict": VERDICTS[outcome],
                         "summary": summary, "evidence": evidence})
    unverified = [{"id": u["id"], "claim": u["claim"], "verdict": "unverified",
                   "follow_up": u["follow_up"]} for u in case["unverified_always"]]
    required = [c for c in criteria if c["required"]]
    # A case whose live target is not established to match its expectation never passes,
    # whatever the evidence shows.
    resolved = case.get("target_status", "resolved") == "resolved"
    if any(c["verdict"] == "failed" for c in criteria):
        verdict = "failed"
    elif accepted and resolved and all(c["verdict"] == "passed" for c in required):
        verdict = "passed"
    else:
        verdict = "incomplete"
    reasons = []
    if not accepted:
        reasons.append("evidence provenance " + bundle["provenance"] + " is not accepted by "
                       "this scope")
    if not resolved:
        reasons.append("the case's live target and expectation baseline are not established "
                       "to match")
    reasons += [c["id"] + " " + c["verdict"] for c in required if c["verdict"] != "passed"]
    report = {
        "schema_version": 1, "harness_version": __version__, "processed_at": utc_now(),
        "case": {"id": case["id"], "version": case["version"], "digest": contract["digest"]},
        "expectation": contract, "scope": case["scope"]["statement"],
        "employee_certification": False, "provenance": bundle["provenance"],
        "provenance_accepted": accepted, "mode": MODES[bundle["provenance"]],
        "revision": bundle.get("revision"), "environment": bundle.get("environment"),
        "observed": bundle.get("observed"), "notes": bundle.get("notes"),
        "complete_reading_captured": captured, "bundle_sha256": bundle_digest,
        "items": [{"id": i["id"], "kind": i["kind"], "role": i["role"],
                   "invocation": i.get("invocation"), "note": i.get("note"),
                   "sha256": hashlib.sha256(i["text"].encode()).hexdigest(), "text": t}
                  for i, t in zip(items, clean_texts)],
        "criteria": criteria, "unverified": unverified,
        "categories": {k: [c["id"] + ": " + c["verdict"] for c in criteria if c["category"] == k]
                       for k in CATEGORIES},
        "verdict": verdict, "scoped_success": verdict == "passed",
        "verdict_reasons": reasons if verdict != "passed" else [],
        "limitations": _limitations(bundle, items),
    }
    return sanitizer.value(report), results


def _limitations(bundle, items):
    """Limits of the recorded collection method. A harness-made service call is neither a copy
    nor a local rendering; every other bundle keeps the established wording."""
    direct = (bundle["provenance"] == "independently_observed" and items
              and all(i["kind"] == "api_response" for i in items))
    if direct:
        # A request ID is stated only when the captured answer itself carries one (the CLI
        # prints response headers for some error answers); otherwise it stays unknown.
        ids = sorted({m for i in items for m in re.findall(
            r'x-request-id\\?":\s*\\?"([0-9A-Za-z-]{8,64})', i["text"])})
        identity = ("Request ID from the captured response headers: " + ", ".join(ids)
                    + "; no server build is exposed" if ids else
                    "This answer captured no request ID or server build")
        method = [
            "Checks compare the structured service answer with the case's declared identity and "
            "documented response contract; they do not judge what the listed values mean.",
            "Direct harness-executed CLI request: one answer within the recorded harness-clock "
            "window. " + identity + ", so the answering build, server-side latency and any "
            "server-side effects are not established."]
    else:
        method = [
            "Text checks establish textual consistency with the dated case, not semantics.",
            "Copied or locally captured responses do not prove message identity, latency, "
            "absence of side effects or current database agreement."]
    return method + ["A scoped pass covers only the declared workflow; it does not certify "
                     "the employee."]


RETEST_RULES = 2  # 2: provenance eligibility is decided before any linked criterion counts


def classify_retest(report, finding):
    """Finding-specific result from the criteria associated with that finding only.
    The workflow verdict is reported beside it; criteria of other scopes never decide it.
    Evidence the case scope does not accept is ineligible: it can neither reproduce nor
    resolve the finding, so its linked failures are kept as diagnostics and the result is
    inconclusive."""
    linked = [c for c in report["criteria"] if c["finding"] == finding]
    failed = [c["id"] for c in linked if c["verdict"] == "failed"]
    open_ = [c["id"] for c in linked if c["verdict"] != "passed"]
    eligible = report["provenance_accepted"]
    diagnostic = []
    if not eligible:
        outcome = "inconclusive"
        reasons = ["evidence provenance " + report["provenance"] + " is not accepted by this "
                   "scope, so it cannot reproduce or resolve this finding"]
        diagnostic = [c + " failed on " + report["provenance"] + " evidence" for c in failed]
    elif failed:
        outcome, reasons = "still_failing", [c + " failed" for c in failed]
    elif open_:
        outcome, reasons = "inconclusive", [c + " unverified" for c in open_]
    else:
        outcome, reasons = "resolved", []
    unrelated = [c["id"] + " " + c["verdict"] for c in report["criteria"]
                 if c["finding"] != finding and c["required"] and c["verdict"] != "passed"]
    return {"outcome": outcome, "reasons": reasons, "linked_criteria": [c["id"] for c in linked],
            "eligible": eligible, "diagnostic": diagnostic, "retest_rules": RETEST_RULES,
            "workflow_verdict": report["verdict"],
            "difference": ("workflow " + report["verdict"] + " because of criteria outside this "
                           "finding: " + "; ".join(unrelated)) if unrelated else ""}


def _compatible(store, key, case, mode):
    """Why evidence of `mode` for `case` cannot be linked to finding `key`, or None."""
    original = store.finding(key)
    if original is None or original["journey"] != case["id"]:
        return "the finding does not belong to this case"
    if original["context"].get("finding") not in case["findings"]:
        return "the finding's criterion group is not declared by this case version"
    alias = store.alias_target(key)
    if alias:
        return "the finding is an alias of " + alias
    if (mode == SIMULATION) != (store.finding_mode(key) == SIMULATION):
        return "simulated and non-simulated evidence are never linked"
    return None


def candidate_findings(store, case, mode, open_only=True):
    """Findings this case's new evidence could retest: same case, a criterion group the case
    still declares, not an alias, and on the same side of the simulation boundary."""
    return [f["key"] for f in store.findings()
            if _compatible(store, f["key"], case, mode) is None
            and (not open_only or finding_status(store.retests(f["key"])) == "open")]


def _run_id(store, report):
    """One run per evidence bundle and case version. Runs recorded before the case digest
    entered the ID keep their original ID, so re-imports stay deduplicated."""
    legacy = "review-" + report["bundle_sha256"][:12]
    try:
        if store.run(legacy)["config_digest"] == report["case"]["digest"]:
            return legacy
    except StateError:
        pass
    return "review-" + _digest([report["bundle_sha256"], report["case"]["digest"]])[:12]


def _bundle_of(store, run_id):
    run = store.run(run_id)
    fixture = json.loads(run["fixture_json"]) if run["fixture_json"] else {}
    return fixture.get("bundle_sha256") or run_id[len("review-"):]


def record(store, case, report, results, retest_key=None, reanalysis_of=None, correction=None):
    """Persist one evaluation as a deterministic run; identical evidence never duplicates.
    A reanalysis re-evaluates the evidence of an earlier retest under a corrected case and
    is recorded alongside it; the earlier result is kept."""
    run_id = _run_id(store, report)
    if bool(reanalysis_of) != bool(correction) or (reanalysis_of and not retest_key):
        raise ValueError("a reanalysis needs --retest, --reanalysis-of and --correction")
    original = None
    if retest_key:
        problem = _compatible(store, retest_key, case, report["mode"])
        if problem:
            raise ValueError("cannot link this retest: " + problem)
        original = store.finding(retest_key)
        retests = {r["run_id"]: r for r in store.retests(retest_key)}
        if run_id in store.finding_runs(retest_key) and run_id not in retests:
            raise ValueError("retest evidence is identical to an original occurrence")
        if reanalysis_of:
            prior = retests.get(reanalysis_of)
            if prior is None or reanalysis_of == run_id:
                raise ValueError("reanalysis must name an earlier retest run of this finding")
            if not report["bundle_sha256"].startswith(_bundle_of(store, reanalysis_of)):
                raise ValueError("reanalysis evidence differs from the earlier retest")
    store.ensure_run(run_id, case["id"], "functional-review", report["mode"],
                     report["case"]["digest"], __version__)
    store.update_run(run_id, status=RUN_COMPLETE, server_build=report["revision"] or "unknown",
                     fixture_json={"bundle_sha256": report["bundle_sha256"],
                                   "case_version": report["case"]["version"]})
    failed_by_finding = {}
    for c in report["criteria"]:
        if c["verdict"] == "failed" and c["finding"]:
            failed_by_finding.setdefault(c["finding"], []).append(c)
    findings, keys = {}, {}
    for fid, failed in failed_by_finding.items():
        template = case["findings"][fid]
        findings[fid] = Finding(
            "workflow." + fid, template["classification"], template["title"],
            template.get("expected", ""),
            "\n".join("- " + c["id"] + ": " + c["summary"] + " "
                      + json.dumps(c["evidence"], sort_keys=True) for c in failed),
            context={"case": case["id"], "finding": fid}, impact=template.get("impact", ""),
            unknowns=template.get("unknowns", []))
        keys[fid] = finding_key(case["id"], fid, findings[fid], report["mode"])
    # Failures of the linked finding's own criterion group belong to that finding's retest,
    # not to a new occurrence, so the original observation stays intact. That includes evidence
    # in another mode the scope does not accept (local rendering against a Discord finding):
    # it is a diagnostic of the same finding, not a second defect. Accepted evidence in another
    # mode makes a distinct claim and keeps its own finding, explicitly related below.
    group = original["context"]["finding"] if original else None
    attributed = {fid for fid in keys if fid == group
                  and (keys[fid] == retest_key or not report["provenance_accepted"])}
    for seq, (name, result) in enumerate(results.items()):
        criterion = next(c for c in report["criteria"] if c["id"] == name)
        fid = criterion["finding"] if criterion["verdict"] == "failed" else None
        key = retest_key if fid in attributed else keys.get(fid)
        store.record_step(run_id, seq, name, StepResult(result.outcome, result.summary,
                          criterion["evidence"]), key)
    for fid, key in keys.items():
        if fid in attributed:
            continue
        template = case["findings"][fid]
        store.record_finding(key, run_id, case["id"], failed_by_finding[fid][0]["id"],
                             findings[fid], details={
            "reproduction": template.get("reproduction", []), "retest": template.get("retest", ""),
            "source_explanation": template.get("source_explanation", ""),
            "provenance": [report["provenance"] + (" (accepted)" if report["provenance_accepted"]
                           else " (not accepted for scoped success)"),
                           "Revision: " + str(report["revision"]),
                           "Observed: " + str(report["observed"]),
                           "Evidence bundle: " + report["bundle_sha256"][:12]]})
        if fid == group:
            store.record_relation(key, retest_key, "related", {
                "kind": "association", "run": run_id,
                "reason": "same case criterion group; this finding rests on accepted "
                          + report["mode"] + " evidence, the linked finding on "
                          + str(store.finding_mode(retest_key)) + " evidence"})
    retest = _record_retest(store, report, original, retest_key, run_id, reanalysis_of,
                            correction) if retest_key else None
    keys = {fid: key for fid, key in keys.items() if fid not in attributed}
    drafts = {}
    for key in sorted(set(keys.values()) | ({retest_key} if retest_key else set())):
        path, created, changed = write_draft(store, key, NO_RELATED)
        drafts[key] = {"path": str(path), "created": created, "changed": changed}
    return run_id, keys, retest, drafts


def _record_retest(store, report, original, retest_key, run_id, reanalysis_of, correction):
    retest = classify_retest(report, original["context"]["finding"])
    retest.update({"finding": retest_key, "run": run_id, "provenance": report["provenance"],
                   "revision": report["revision"], "case_version": report["case"]["version"],
                   "kind": "reanalysis" if reanalysis_of else "retest"})
    if reanalysis_of:
        retest.update({"supersedes": reanalysis_of, "correction": correction})
    store.record_retest(retest_key, run_id, retest["outcome"], retest)
    retest["finding_status"] = finding_status(store.retests(retest_key))
    return retest


def reclassify(state_dir, report, retest_key, reanalysis_of, correction):
    """Correct a recorded retest after an evaluator-rule change, from the criteria verdicts
    already saved in that run's report. The evidence, case and criteria are unchanged and are
    checked against the store; nothing is re-evaluated. The earlier result is kept and shown
    as superseded. A finding the corrected rules would not have created (its only occurrence is
    that same run, in the linked finding's criterion group, on evidence the scope does not
    accept) is marked an alias of the linked finding; any other is only marked related.
    Repeating the correction adds nothing."""
    if not isinstance(correction, str) or not correction.strip():
        raise ValueError("a reclassification needs a correction reason")
    store = Store(state_dir)
    try:
        prior = store.run(reanalysis_of)
        original = store.finding(retest_key)
        retests = {r["run_id"]: r for r in store.retests(retest_key)}
        if original is None or reanalysis_of not in retests:
            raise ValueError("the run is not a recorded retest of this finding")
        if (report.get("run") != reanalysis_of or report["case"]["id"] != original["journey"]
                or report["case"]["digest"] != prior["config_digest"]
                or report["mode"] != prior["mode"]
                or not report["bundle_sha256"].startswith(_bundle_of(store, reanalysis_of))):
            raise ValueError("the saved report does not belong to that run")
        before = {"finding_status": finding_status(store.retests(retest_key)),
                  "retest_outcome": retests[reanalysis_of]["outcome"]}
        run_id = "review-" + _digest(["reclassify", reanalysis_of, retest_key,
                                      RETEST_RULES])[:12]
        store.ensure_run(run_id, original["journey"], "functional-review", report["mode"],
                         report["case"]["digest"], __version__)
        store.update_run(run_id, status=RUN_COMPLETE, server_build=report["revision"] or "unknown",
                         fixture_json={"bundle_sha256": report["bundle_sha256"],
                                       "case_version": report["case"]["version"],
                                       "reclassifies": reanalysis_of,
                                       "retest_rules": RETEST_RULES})
        retest = _record_retest(store, report, original, retest_key, run_id, reanalysis_of,
                                correction)
        group = original["context"]["finding"]
        relations = {}
        for step in store.steps(reanalysis_of):
            other = store.finding(step["finding_key"]) if step["finding_key"] else None
            if (other is None or other["key"] == retest_key or other["key"] in relations
                    or other["context"].get("finding") != group):
                continue
            alias = (store.finding_runs(other["key"]) == [reanalysis_of]
                     and not report["provenance_accepted"])
            relation = "alias_of" if alias else "related"
            store.record_relation(other["key"], retest_key, relation, {
                "kind": "record_correction", "run": reanalysis_of, "correction": correction,
                "reason": ("created only by run " + reanalysis_of + ", whose evidence is linked "
                           "to " + retest_key + " as a retest; under retest rules "
                           + str(RETEST_RULES) + " that evidence is attributed to "
                           + retest_key + " and creates no separate finding") if alias else
                          ("same case criterion group, observed in run " + reanalysis_of)})
            relations[other["key"]] = relation
        drafts = {}
        for key in [retest_key, *relations]:
            path, created, changed = write_draft(store, key, NO_RELATED)
            drafts[key] = {"path": str(path), "changed": changed}
        return {"kind": "record_correction", "finding": retest_key, "run": run_id,
                "reclassifies": reanalysis_of, "case": report["case"],
                "provenance": report["provenance"], "correction": correction,
                "before": before, "retest": retest,
                "after": {"finding_status": retest["finding_status"]},
                "relations": relations, "drafts": drafts}
    finally:
        store.close()


def render(report):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["# Functional workflow review", "",
             "Scope: " + report["scope"], "",
             "Verdict: **" + report["verdict"] + "**; scoped success: **"
             + ("yes" if report["scoped_success"] else "no") + "**; employee certification: **no**.",
             "Evidence: " + report["provenance"] + (" (accepted)" if report["provenance_accepted"]
                                                    else " (not accepted for scoped success)")
             + "; revision: " + str(report["revision"]) + "; observed: " + str(report["observed"]),
             "Complete reading captured: " + ("yes" if report["complete_reading_captured"] else "no"),
             ""]
    if report["verdict_reasons"]:
        lines += ["Not passed because: " + "; ".join(report["verdict_reasons"]) + ".", ""]
    lines += ["## Criteria", "", "| Criterion | Category | Required | Verdict | Basis |",
              "|---|---|---|---|---|"]
    for c in report["criteria"]:
        lines.append("| " + cell(c["claim"]) + " | " + c["category"] + " | "
                     + ("yes" if c["required"] else "no") + " | " + c["verdict"] + " | "
                     + cell(c["summary"] + " " + json.dumps(c["evidence"], sort_keys=True))
                     + " |")
    lines += ["", "## Not established", ""]
    lines += ["- " + u["claim"] + ": unverified. " + u["follow_up"] for u in report["unverified"]]
    lines += ["", "## Evidence items", ""]
    lines += ["- " + i["id"] + " (" + i["kind"] + ", " + i["role"] + "): "
              + str(i["invocation"] or "no invocation") + "; sha256 " + i["sha256"][:12]
              for i in report["items"]]
    if report.get("findings"):
        lines += ["", "## Findings", ""]
        lines += ["- " + k + " → private draft " + v for k, v in report["findings"].items()]
    if report.get("retest"):
        r = report["retest"]
        lines += ["", "## Finding " + ("reanalysis" if r["kind"] == "reanalysis" else "retest"),
                  "",
                  "- Workflow verdict (whole declared scope): **" + r["workflow_verdict"] + "**",
                  "- Finding " + r["finding"] + " retest (criteria " + ", ".join(
                      r["linked_criteria"]) + "): **" + r["outcome"] + "**"
                  + ("; " + "; ".join(r["reasons"]) if r["reasons"] else ""),
                  "- Finding status: **" + r["finding_status"] + "**"]
        if not r.get("eligible", True):
            lines.append("- Evidence eligibility: not accepted by this scope; recorded as a "
                         "diagnostic observation that leaves the finding status unchanged"
                         + (" (diagnostic only: " + "; ".join(r["diagnostic"]) + ")"
                            if r.get("diagnostic") else ""))
        if r["difference"]:
            lines.append("- Difference: " + r["difference"])
        if r["kind"] == "reanalysis":
            lines.append("- Reanalysis of run `" + r["supersedes"] + "` (same evidence, case v"
                         + str(r["case_version"]) + "): " + r["correction"]
                         + " The earlier result is retained.")
    lines += ["", "## Limits", "", *["- " + x for x in report["limitations"]], ""]
    return "\n".join(lines)


def review(case, bundle, items, state_dir, destination, assessment=None, retest_key=None,
           reanalysis_of=None, correction=None):
    """Evaluate, record in the finding store and write the private report. Shared by the
    `review-workflow` and `guide` commands; returns the sanitized report."""
    destination = Path(destination)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("output directory must be new or empty")
    report, results = evaluate(case, bundle, items, assessment)
    store = Store(state_dir)
    try:
        run_id, keys, retest, drafts = record(store, case, report, results, retest_key,
                                              reanalysis_of, correction)
    finally:
        store.close()
    report["run"] = run_id
    # New or repeated occurrences only; a retested finding is reported under its retest.
    report["findings"] = {k: drafts[k]["path"] for k in keys.values() if k != retest_key}
    report["retest"] = retest
    template = {"bundle_sha256": report["bundle_sha256"],
                "case_digest": report["case"]["digest"], "assessor": "",
                "checks": {c["id"]: {"verdict": "unverified", "excerpt": "", "note": ""}
                           for c in case["criteria"] if c["check"] == "operator"}}
    report = Sanitizer().value(report)
    _write_private(destination / "report.json", json.dumps(report, indent=2) + "\n")
    _write_private(destination / "report.md", render(report))
    _write_private(destination / "assessment-template.json",
                   json.dumps(template, indent=2) + "\n")
    return report


def summary_line(report):
    return ("Workflow " + report["verdict"] + "; scoped success: "
            + ("yes" if report["scoped_success"] else "no") + "; findings: "
            + (", ".join(report["findings"]) or "none")
            + ("; finding " + report["retest"]["kind"] + " " + report["retest"]["outcome"]
               + ", finding " + report["retest"]["finding_status"] if report["retest"] else ""))


def run(args):
    try:
        bundle, items = load_bundle(args.evidence)
        report = review(read_json(args.case), bundle, items, args.state_dir, args.out,
                        read_json(args.assessment) if args.assessment else None,
                        args.retest, args.reanalysis_of, args.correction)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        # Generalized regression scenario; private investigation history omitted.
        print("Cannot review workflow: check readable UTF-8/JSON case, bundle and assessment, "
              "matching digests, retest key and an empty output directory.", file=sys.stderr)
        return 5
    print(summary_line(report) + ". Private report written to the requested output directory.")
    return {"passed": 0, "failed": 1}.get(report["verdict"], 2)


def render_correction(result):
    r = result["retest"]
    lines = ["# Record correction: finding retest reclassification", "",
             "Not a new observation. Run `" + result["reclassifies"] + "` (" + result["provenance"]
             + " evidence, case " + result["case"]["id"] + " v" + str(result["case"]["version"])
             + ") is reclassified from its saved criteria verdicts under retest rules "
             + str(r["retest_rules"]) + "; the evidence, case and criteria are unchanged.", "",
             "Correction: " + result["correction"], "",
             "- Finding " + result["finding"] + " retest: **" + result["before"]["retest_outcome"]
             + "** (retained, now superseded) → **" + r["outcome"] + "** in run `"
             + result["run"] + "`" + ("; " + "; ".join(r["reasons"]) if r["reasons"] else ""),
             "- Diagnostic failures kept visible: " + ("; ".join(r["diagnostic"]) or "none"),
             "- Finding status: **" + result["before"]["finding_status"] + "** → **"
             + result["after"]["finding_status"] + "**"]
    lines += ["- " + key + ": marked `" + relation + "` " + result["finding"]
              for key, relation in result["relations"].items()]
    lines += ["", "Original findings, occurrences, retests, evidence and drafts are retained.", ""]
    return "\n".join(lines)


def reclassify_run(args):
    destination = Path(args.out)
    try:
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise ValueError("output directory must be new or empty")
        result = reclassify(args.state_dir, read_json(args.report), args.retest,
                            args.reanalysis_of, args.correction)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, StateError):
        print("Cannot reclassify: check the finding store, the run's saved report.json, the "
              "finding key, the earlier retest run, a correction reason and an empty output "
              "directory.", file=sys.stderr)
        return 5
    result = Sanitizer().value(result)
    _write_private(destination / "correction.json", json.dumps(result, indent=2) + "\n")
    _write_private(destination / "correction.md", render_correction(result))
    print("Record correction for " + result["finding"] + ": retest "
          + result["before"]["retest_outcome"] + " -> " + result["retest"]["outcome"]
          + "; finding status " + result["before"]["finding_status"] + " -> "
          + result["after"]["finding_status"]
          + "".join("; " + k + " " + v + " " + result["finding"]
                    for k, v in result["relations"].items())
          + ". Private correction written to the requested output directory.")
    return 0
