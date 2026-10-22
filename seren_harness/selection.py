"""Explainable selection of up to three cases from a finite, owner-approved catalog.

The catalog (catalogs/<id>.json) lists existing, offline-verified cases with their pinned
version, contract digest, exact request, expectation sources, prerequisites and bounds.
Membership is not live authority. Selection reads only saved local evidence: live review
reports, campaign records and the finding store. It never calls a service. Every catalog
entry receives one recorded decision; missing or ambiguous history is never read as
"untested", and nothing is invented, reset or relaxed to fill the queue.
"""
from __future__ import annotations

import json
from pathlib import Path

from seren_harness import assertions, collect, sql_probe
from seren_harness.guide import workflow_cases
from seren_harness.models import utc_now
from seren_harness.report import finding_status
from seren_harness.state import Store

RULES_VERSION = 1
LIMIT = 3
# Campaign results whose request may have reached the service with an unknown outcome.
UNCERTAIN = ("uncertain_timeout", "uncertain_interrupted")
RULES = (
    "1. The entry must still match the current case version, contract digest and target "
    "request; a changed case needs a newly approved catalog.",
    "2. The exact request must be covered by the authorization (when one is supplied).",
    "3. An unresolved case target or any prerequisite not recorded as met blocks the case.",
    "4. A request whose earlier outcome is uncertain is never replayed automatically.",
    "5. An open finding is retested only with a recorded repeat reason and evidence.",
    "6. A live result for the same case version and digest means the test is completed; a "
    "repeat needs a recorded reason. Live results only for other versions are ambiguous.",
    "7. With no saved live result, the entry must record that the case never ran live; "
    "otherwise the history is ambiguous and the case is skipped.",
    "8. Eligible cases are taken in catalog order, untested behaviour before recorded "
    "repeats, up to three and the authorized budget; the rest are skipped for the limit.")


class CatalogError(ValueError):
    """The catalog itself is malformed; no selection is made."""


def _allowed(args):
    return collect.read_only_args(args) or sql_probe.allowed(args)


def load_catalog(raw, root):
    """(catalog, current cases by id, target) after validating the catalog's structure."""
    entries = raw.get("entries")
    if (raw.get("kind") != "catalog" or not isinstance(raw.get("id"), str)
            or not isinstance(raw.get("request_timeout_seconds"), int)
            or not isinstance(entries, list) or not entries
            or len({e.get("case") for e in entries}) != len(entries)):
        raise CatalogError("catalog needs an id, a timeout and unique entries")
    for e in entries:
        if (not isinstance(e.get("case"), str) or not isinstance(e.get("version"), int)
                or not isinstance(e.get("digest"), str) or not isinstance(e.get("args"), list)
                or not e.get("expectation_sources") or e.get("max_requests") != 1
                or not isinstance(e.get("prerequisites"), list)
                or any(not {"id", "status", "source"} <= set(p) for p in e["prerequisites"])):
            raise CatalogError("catalog entry " + str(e.get("case")) + " is incomplete")
    cases = {c["id"]: c for _, c in workflow_cases(Path(root))}
    target = collect.load_target(Path(root) / raw["target"])
    return raw, cases, target


def entry_problem(entry, cases, target, timeout):
    """Why a catalog entry no longer matches the current case or target, or None."""
    case = cases.get(entry["case"])
    collector = target["collectors"].get(entry["case"], {})
    if case is None:
        return "the case no longer exists"
    if case["version"] != entry["version"] or assertions.snapshot(case)["digest"] != \
            entry["digest"]:
        return "the case changed since the catalog was approved"
    if collector.get("args") != entry["args"] or not _allowed(entry["args"]):
        return "the target request differs from the catalog's exact request"
    if collector.get("timeout_seconds") != timeout:
        return "the target timeout differs from the catalog"
    return None


def live_reports(home, case_id):
    """Saved live review reports for a case: the harness's own coverage record."""
    found = []
    for path in sorted((Path(home) / case_id).glob("*/report/report.json")):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            found.append({"path": str(path), "unreadable": True})
            continue
        if report.get("mode") == "live" and report.get("case", {}).get("id") == case_id:
            found.append({"path": str(path.parent.parent), "version": report["case"]["version"],
                          "digest": report["case"]["digest"], "verdict": report["verdict"],
                          "processed_at": report.get("processed_at")})
    return found


def uncertain_requests(home, case_id):
    out = []
    for path in sorted((Path(home) / "campaigns").glob("*/state.json")):
        try:
            entry = json.loads(path.read_text())["cases"].get(case_id)
        except (OSError, ValueError, KeyError, AttributeError):
            out.append({"campaign_state": str(path), "result": "unreadable record"})
            continue
        if entry and (entry.get("result") in UNCERTAIN or entry.get("status") == "in_flight"):
            out.append({"campaign_state": str(path),
                        "result": entry.get("result", entry["status"])})
    return out


def open_findings(home, case_id):
    directory = Path(home) / "findings"
    if not (directory / "state.sqlite3").exists():
        return []
    store = Store(directory)
    try:
        return [f["key"] for f in store.findings()
                if f["context"].get("case") == case_id and not store.alias_target(f["key"])
                and finding_status(store.retests(f["key"])) == "open"]
    finally:
        store.close()


def _decide(entry, case, home, authorized):
    """(eligible kind or None, reason, evidence)."""
    case_id = entry["case"]
    if authorized is not None and authorized.get(case_id) != entry["args"]:
        return None, "not covered by the authorization's exact requests", []
    if case.get("target_status") == "unresolved":
        return None, "blocked: the case's live target and expectation baseline are " \
                     "unresolved", []
    unmet = [p for p in entry["prerequisites"] if p["status"] != "met"]
    if unmet:
        return None, "blocked: prerequisite " + ", ".join(
            p["id"] + " is " + p["status"] for p in unmet), [p["source"] for p in unmet]
    uncertain = uncertain_requests(home, case_id)
    if uncertain:
        return None, "an earlier request's outcome is uncertain; never replayed " \
                     "automatically", uncertain
    repeat = entry.get("repeat_reason")
    findings = open_findings(home, case_id)
    if findings and not repeat:
        return None, "open finding " + ", ".join(findings) + "; a retest needs a recorded " \
                     "repeat reason with new evidence", findings
    reports = live_reports(home, case_id)
    same = [r for r in reports if r.get("digest") == entry["digest"]
            and r.get("version") == entry["version"]]
    if same:
        if repeat:
            return "repeat", "recorded repeat reason: " + repeat["reason"], \
                [repeat["evidence"]] + [r["path"] for r in same]
        return None, "completed unchanged live test (" + ", ".join(
            r["verdict"] for r in same) + "); a repeat needs a recorded reason", \
            [r["path"] for r in same]
    if reports:
        if repeat:
            return "repeat", "recorded repeat reason: " + repeat["reason"], \
                [repeat["evidence"]] + [r["path"] for r in reports]
        return None, "ambiguous history: live results exist only for other case versions " \
                     "or are unreadable", [r["path"] for r in reports]
    history = entry.get("history") or {}
    if history.get("never_run_live_as_of") and history.get("source"):
        return "untested", "behaviour not yet demonstrated live (recorded " + \
            history["never_run_live_as_of"] + ")", [history["source"]]
    return None, "ambiguous history: no saved live result here and no recorded statement " \
                 "that it never ran live", []


def select(catalog, catalog_sha, cases, target, home, authorized=None, budget=LIMIT):
    """One recorded decision per catalog entry and a queue of at most min(3, budget)."""
    decisions, eligible = [], []
    for entry in catalog["entries"]:
        problem = entry_problem(entry, cases, target, catalog["request_timeout_seconds"])
        if problem:
            kind, reason, evidence = None, "catalog entry invalid: " + problem, []
        else:
            kind, reason, evidence = _decide(entry, cases[entry["case"]], home, authorized)
        decision = {"case": entry["case"], "version": entry["version"],
                    "decision": "eligible" if kind else "skipped", "kind": kind,
                    "reason": reason, "evidence": evidence}
        decisions.append(decision)
        if kind:
            eligible.append(decision)
    eligible.sort(key=lambda d: 0 if d["kind"] == "untested" else 1)  # stable: catalog order
    limit = max(0, min(LIMIT, budget))
    queue = [d["case"] for d in eligible[:limit]]
    for d in decisions:
        if d["decision"] == "eligible":
            if d["case"] in queue:
                d["decision"] = "selected"
            else:
                d.update(decision="skipped", reason="eligible, but the selection limit of "
                         + str(limit) + " was reached")
    return {"rules_version": RULES_VERSION, "rules": list(RULES), "selected_at": utc_now(),
            "catalog": catalog["id"], "catalog_version": catalog.get("version"),
            "catalog_sha256": catalog_sha, "limit": limit, "queue": queue,
            "decisions": decisions}


def describe(selection):
    lines = ["Selection from catalog " + selection["catalog"] + " (rules v"
             + str(selection["rules_version"]) + "): "
             + (", ".join(selection["queue"]) if selection["queue"] else "no eligible cases")
             + ".", "", "| Case | v | Decision | Reason | Evidence |", "|---|---|---|---|---|"]
    for d in selection["decisions"]:
        evidence = "; ".join(e if isinstance(e, str) else json.dumps(e, sort_keys=True)
                             for e in d["evidence"]) or "-"
        lines.append("| " + " | ".join([d["case"], str(d["version"]), d["decision"],
                                        d["reason"], evidence]) + " |")
    return "\n".join(lines)
