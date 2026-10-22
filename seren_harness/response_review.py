"""Offline copied-response review. No provider calls, model judge or lifecycle actions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from seren_harness import __version__, assertions
from seren_harness.models import BLOCKED, FAILED, PASSED, StepResult, utc_now
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer

METADATA = ("command", "message_id", "request_message_id", "channel_id", "guild_id",
            "author_id", "operator", "response_timestamp", "request_timestamp", "notes")
VERDICTS = {PASSED: "passed", FAILED: "failed", BLOCKED: "unverified"}
LIMIT = 262144
UNOBSERVABLE = {
    "no_new_research": ("No new research run", "Separately authorized before/after run evidence "
                        "is needed. The response's own assurance does not establish this."),
    "no_database_mutation": ("No duplicate entity or other database mutation",
                             "Separately authorized database evidence is needed; none is read "
                             "by this command. Static handler inspection is insufficient."),
    "correlation": ("Exact message correlation", "Retain the original command and response "
                    "identifiers plus verified interaction evidence. Supplied IDs alone are "
                    "not independently verified."),
    "latency": ("Response latency", "Obtain verified request/response timestamps from the "
                "same interaction; do not use file modification or processing time."),
    "current_record": ("Agreement with the current authoritative database",
                       "Compare against a separately authorized current record snapshot. "
                       "This review uses only the dated expectation supplied in the case."),
}


def read_text(path):
    data = Path(path).read_bytes()
    if len(data) > LIMIT:
        raise ValueError("input exceeds 256 KiB")
    return data.decode("utf-8")


def read_json(path):
    value = json.loads(read_text(path))
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def reference_urls(text):
    urls = []
    for item in re.findall(r"https?://[^\s<>\]\)]+", text):
        item = item.rstrip(".,;:")
        try:
            url = urlsplit(item)
            if (url.hostname and "." in url.hostname and not url.username and not url.password
                    and "[redacted]" not in item and item not in urls):
                urls.append(item)
        except ValueError:
            continue
    return urls


def evaluate(case, response, metadata=None, assessment=None, synthetic=False):
    sanitizer = Sanitizer()
    clean = sanitizer.text(response)
    case = sanitizer.value(case)
    required = ("id", "version", "command", "claim", "basis", "expectation_date", "review")
    if any(k not in case for k in required) or not isinstance(case["review"], list):
        raise ValueError("case is missing its versioned expectation or review criteria")
    ids = [r.get("id") for r in case["review"] if isinstance(r, dict)]
    if (len(ids) != len(case["review"]) or not ids or len(set(ids)) != len(ids)
            or any(not isinstance(i, str) or not i for i in ids)
            or any(not isinstance(r.get("claim"), str) or not isinstance(r.get("expected"), str)
                   for r in case["review"])):
        raise ValueError("case review criteria must have unique IDs, claims and expectations")
    contract = assertions.snapshot(case)
    digest = hashlib.sha256(clean.encode()).hexdigest()
    metadata = metadata or {}
    if any(k not in METADATA or (v is not None and not isinstance(v, str))
           for k, v in metadata.items()):
        raise ValueError("metadata must contain supported string fields only")
    metadata = sanitizer.value({k: metadata.get(k) for k in METADATA})
    assessment = assessment or {}
    reviews = assessment.get("checks", {})
    if not isinstance(reviews, dict) or any(k not in ids for k in reviews):
        raise ValueError("assessment contains unknown criteria")
    if assessment and (assessment.get("response_sha256") != digest
                       or assessment.get("case_digest") != contract["digest"]):
        raise ValueError("assessment does not belong to this response and expectation")
    criteria = []

    def add(key, claim, result, method, follow_up):
        criteria.append({"id": key, "claim": claim, "verdict": VERDICTS[result.outcome],
                         "method": method, "summary": result.summary,
                         "evidence": result.evidence, "follow_up": follow_up})

    add("response_present", "A nonempty response was supplied",
        StepResult(PASSED if clean.strip() else FAILED, "supplied text length",
                   {"characters": len(clean)}), "objective", "" if clean.strip() else
        "Copy the complete ephemeral response; an empty file cannot be evaluated.")
    urls = reference_urls(clean)
    add("reference_syntax", "The copied text includes explicit HTTP(S) source references",
        StepResult(PASSED if urls else FAILED, "URL syntax only; relevance and reachability "
                   "are not verified", {"urls": urls}), "objective",
        "Review links under sources below." if urls else "Copy the actual link destinations "
        "if Discord's copy operation omitted them. Missing copied links alone are not a "
        "proven employee defect.")
    command = metadata["command"]
    add("command_context", "The operator identifies the requested command",
        StepResult(BLOCKED if command is None else PASSED if command == case["command"] else FAILED,
                   "operator declaration only", {"supplied": command, "expected": case["command"]}),
        "operator-supplied metadata", "Supply the exact command in metadata; this does not "
        "verify the Discord interaction." if command is None else "")
    for criterion in case["review"]:
        review = reviews.get(criterion["id"], {})
        if not isinstance(review, dict) or review.get("verdict", "unverified") not in (
                "passed", "failed", "unverified"):
            raise ValueError("invalid operator assessment")
        verdict = review.get("verdict", "unverified")
        excerpt, note = review.get("excerpt", ""), review.get("note", "")
        if not isinstance(excerpt, str) or not isinstance(note, str):
            raise ValueError("assessment excerpts and notes must be text")
        if verdict != "unverified":
            if not isinstance(assessment.get("assessor"), str) or not assessment["assessor"].strip():
                raise ValueError("a decided assessment requires a named assessor")
            if not note.strip() or (excerpt and excerpt not in clean) or (verdict == "passed" and not excerpt.strip()):
                raise ValueError("a pass needs an exact excerpt and a reason; failure without "
                                 "an excerpt must explain the missing content")
        outcome = {"passed": PASSED, "failed": FAILED, "unverified": BLOCKED}[verdict]
        add(criterion["id"], criterion["claim"], StepResult(outcome,
            "operator assessment" if verdict != "unverified" else "operator review required",
            {"expected": criterion["expected"], "excerpt": excerpt, "note": note,
             "assessor": assessment.get("assessor")}), "operator assessment; not automated semantics",
            "Assess against: " + criterion["expected"] + " Record an excerpt and brief reason "
            "in assessment-template.json; leave unverified if the baseline or page is insufficient."
            if verdict == "unverified" else "A reported discrepancy needs source/current-record "
            "confirmation before attributing a product defect." if verdict == "failed" else "")
    for key, (claim, follow_up) in UNOBSERVABLE.items():
        add(key, claim, StepResult(BLOCKED, "not established by a copied response"),
            "no supporting evidence", follow_up)
    failed = any(c["verdict"] == "failed" for c in criteria)
    content = [c for c in criteria if c["id"] in ids]
    report = {"schema_version": 1, "harness_version": __version__, "processed_at": utc_now(),
              "evidence_origin": "synthetic demonstration response" if synthetic else
              "operator-supplied Discord response", "live_capture_verified": False,
              "metadata": metadata, "latency_ms": None, "response_sha256": digest,
              "sanitization_changed": clean != response, "response": clean,
              "expectation": contract, "criteria": criteria,
              "verdict": "discrepancy_in_supplied_evidence" if failed else "partial_evidence",
              "functional_content": "failed_operator_review" if any(c["verdict"] == "failed"
                for c in content) else "passed_operator_review" if all(c["verdict"] == "passed"
                for c in content) else "review_pending", "complete_pass": False,
              "limitations": ["No live calls, database reads or mutations were performed.",
                "Human assessments are attributed judgments, not independently certified "
                "semantic correctness. Objective checks certify only the stated properties.",
                "Neither a copy nor supplied metadata proves original message identity, "
                "runtime behavior, absence of side effects or current database accuracy."]}
    return sanitizer.value(report)


def render(report):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["# Saved-research response review", "", report["evidence_origin"], "",
             "Verdict: **" + report["verdict"] + "**; complete pass: **no**.",
             "Content review: " + report["functional_content"], "",
             "Command: `" + report["expectation"]["command"] + "`",
             "Expectation date: " + report["expectation"]["expectation_date"],
             "Processing time (not message time): " + report["processed_at"], "",
             "## Criteria", "", "| Check | Verdict | Evidence / basis |",
             "|---|---|---|"]
    for c in report["criteria"]:
        e = c["evidence"]
        detail = (e.get("excerpt") or e.get("note") or
                  ", ".join(e.get("urls", [])) or c["summary"])
        lines.append("| " + cell(c["claim"]) + " | " + c["verdict"] + " | "
                     + cell(detail) + " |")
    lines += ["", "## Follow-up", ""]
    for c in report["criteria"]:
        if c["follow_up"]:
            lines.append("- **" + c["id"] + ":** " + c["follow_up"])
    lines += ["", "## Supplied metadata", "", "```json",
              json.dumps(report["metadata"], indent=2), "```", "Latency: unknown.", "",
              "## Expectation provenance", ""]
    lines += ["- " + b["kind"] + ": " + b["reference"]
              for b in report["expectation"]["basis"]]
    lines += ["- " + x for x in report["expectation"].get("limitations", [])]
    lines += ["", "## Limits", "", *["- " + x for x in report["limitations"]], "",
              "The full sanitized response, expected content, assessments and versioned "
              "expectation snapshot are retained in report.json.", ""]
    return "\n".join(lines)


def run(args):
    try:
        destination = Path(args.out)
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise ValueError("output directory must be new or empty")
        report = evaluate(read_json(args.case), read_text(args.response),
                          read_json(args.metadata) if args.metadata else None,
                          read_json(args.assessment) if args.assessment else None, args.synthetic)
        template = {"response_sha256": report["response_sha256"],
                    "case_digest": report["expectation"]["digest"], "assessor": "",
                    "checks": {c["id"]: {"verdict": "unverified", "excerpt": "", "note": ""}
                               for c in report["expectation"]["review"]}}
        _write_private(destination / "report.json", json.dumps(report, indent=2) + "\n")
        _write_private(destination / "report.md", render(report))
        _write_private(destination / "assessment-template.json", json.dumps(template, indent=2) + "\n")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        # Generalized regression scenario; private investigation history omitted.
        print("Cannot review input: check readable UTF-8/JSON files, case and assessment fields, "
              "matching assessment digests, and an empty output directory.", file=sys.stderr)
        return 5
    print(report["verdict"] + "; content " + report["functional_content"]
          + "; complete pass: no. Private report written to the requested output directory.")
    return 1 if report["verdict"] == "discrepancy_in_supplied_evidence" else 2
