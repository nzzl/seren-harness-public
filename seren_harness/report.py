"""Sanitized run reports and private GitHub issue drafts (never published)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from seren_harness.assertions import ASSUMPTION, CRITICAL_STEPS
from seren_harness.journey import applicable_steps, overall_outcome
from seren_harness.models import NOT_RUN, PASSED, SIMULATION, UNKNOWN_SERVER_BUILD
from seren_harness.sanitize import pseudonymize
from seren_harness.state import UNRESOLVED, StateError, issue_reference, private_directory

SIMULATION_LABEL = ("SIMULATION: produced by a simulated Seren CLI and Discord channel. "
                    "It exercises the harness only and is not evidence about the Seren service.")


def _write_private(path: Path, text: str):
    private_directory(path.parent)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def retest_link(store, run_id):
    """The finding a lifecycle run was explicitly linked to retest (`--retest`), or None."""
    links = [e["detail"].get("finding") for e in store.events(run_id) if e["kind"] == "retest_link"]
    return links[0] if links else None


def build_report(store, run_id, expected_steps):
    run = store.run(run_id)
    rows = store.steps(run_id)
    recorded = {row["name"]: row for row in rows}
    expected_steps = applicable_steps(expected_steps, rows, run["status"])
    steps = []
    for name in expected_steps:
        row = recorded.get(name)
        steps.append({"name": name, "outcome": row["outcome"] if row else NOT_RUN,
                      "summary": row["summary"] if row else "not reached in this run",
                      "evidence": row["evidence"] if row else {},
                      "finding": row["finding_key"] if row else None})
    keys = sorted({s["finding"] for s in steps if s["finding"]})
    findings = []
    for key in keys:
        item = store.finding(key)
        findings.append({"key": key, "classification": item["classification"],
                         "classification_status": "provisional", "title": item["title"],
                         "step": item["step"], "occurrences": item["occurrences"],
                         "first_seen": item["first_seen"], "last_seen": item["last_seen"]})
    cleanup = [{"kind": c["kind"], "target": c["target"], "status": c["status"],
                "detail": c["detail"]} for c in store.cleanup_items(run_id)]
    unresolved = [{"action": a["id"], "step": a["step"], "kind": a["kind"],
                   "status": a["status"]}
                  for a in store.actions(run_id) if a["status"] in UNRESOLVED]
    limitations = []
    if run["server_build"] == UNKNOWN_SERVER_BUILD:
        limitations.append("Server build is unknown: no trustworthy deployed-build identifier "
                           "was available, so results cannot be attributed to a server commit.")
    for step in steps:
        unsupported = step["evidence"].get("unsupported")
        if unsupported:
            limitations.append(step["name"] + ": " + unsupported)
    if any(c["status"] != "done" for c in cleanup):
        limitations.append("Cleanup is not confirmed; see the cleanup section before running "
                           "again.")
    if unresolved:
        limitations.append("Mutations with unknown outcome remain; resume reconciles them "
                           "from state where possible, and `reconcile` records a human-"
                           "verified outcome otherwise. Only apply, which the CLI answers "
                           "with already_applied, is issued again while unknown.")
    fixture = json.loads(run["fixture_json"]) if run["fixture_json"] else None
    outcome = overall_outcome(rows, expected_steps)
    checks = recorded_checks(steps, cleanup)
    for finding in findings:
        check = next((c for c in checks if c["result"] == finding["step"]), None)
        finding["assertion"] = check.get("id") if check else None
        finding["rests_on_unverified_assumption"] = bool(check and ASSUMPTION in check.get(
            "basis_kinds", []))
    retest = None
    key = retest_link(store, run_id)
    linked = [r for r in store.retests(key) if r["run_id"] == run_id] if key else []
    if linked:
        d = linked[0]["detail"]
        retest = {"finding": key, "outcome": linked[0]["outcome"],
                  "finding_status": finding_status(store.retests(key)),
                  "recovery_condition": d.get("recovery_condition"),
                  "reasons": d.get("reasons", []), "diagnostic": d.get("diagnostic", []),
                  "cleanup_confirmed": d.get("cleanup_confirmed", False)}
    reasons = ([] if outcome == PASSED else ["journey outcome is " + outcome]) \
        + (["shutdown or cleanup not confirmed"]
           if any(c["status"] != "done" for c in cleanup) else []) \
        + (["mutations with unknown outcome remain"] if unresolved else [])
    return {
        "label": SIMULATION_LABEL if run["mode"] == SIMULATION else "LIVE",
        "run": {key: run[key] for key in ("id", "journey", "mode", "status", "created_at",
                                          "updated_at", "harness_version", "cli_version",
                                          "server_build")},
        "employee": run["employee_key"],
        "fixture": fixture,
        "outcome": outcome,
        # Clean only when the journey passed, every cleanup is confirmed and no mutation
        # outcome is unknown. A passing journey with unconfirmed shutdown is not clean.
        "completion": {"clean": not reasons, "reasons": reasons},
        "checks": checks,
        "steps": steps,
        "cleanup": cleanup,
        "unresolved_actions": unresolved,
        "findings": findings,
        "retest": retest,
        "limitations": limitations,
    }


def _check_entry(result, outcome, provenance):
    snapshot = provenance.get("snapshot") or {}
    return {"result": result, "outcome": outcome, "id": snapshot.get("id"),
            "version": snapshot.get("version"), "digest": snapshot.get("digest"),
            "claim": snapshot.get("claim"), "basis": snapshot.get("basis", []),
            "basis_kinds": sorted({b.get("kind") for b in snapshot.get("basis", [])}),
            "observed": provenance.get("observed", []),
            "unmet": provenance.get("unmet", []),
            "insufficient_evidence": snapshot.get("insufficient_evidence", []),
            "limitations": snapshot.get("limitations", [])}


def recorded_checks(steps, cleanup):
    """Critical checks as recorded with each result; never filled in from today's registry.

    The current registry only names which results are critical, so that a result
    recorded before provenance existed is reported as such rather than certified.
    """
    checks = []
    for step in steps:
        provenance = step["evidence"].get("assertion")
        if provenance:
            checks.append(_check_entry(step["name"], step["outcome"], provenance))
        elif step["name"] in CRITICAL_STEPS and step["outcome"] != NOT_RUN:
            checks.append({"result": step["name"], "outcome": step["outcome"],
                           "provenance": "not recorded"})
    for item in cleanup:
        if item["kind"] != "stop_runtime":
            continue
        provenance = item["detail"].get("assertion")
        if provenance:
            checks.append(_check_entry("cleanup " + item["kind"], item["status"], provenance))
        else:
            checks.append({"result": "cleanup " + item["kind"], "outcome": item["status"],
                           "provenance": "not recorded" if item["status"] != "pending"
                           else "not attempted"})
    return checks


def render_checks(checks, simulated):
    lines = ["## What was checked", ""]
    if not checks:
        return lines + ["- No critical checks were reached.", ""]
    if simulated:
        lines += ["Simulated results show harness behavior only; the basis entries below "
                  "keep their recorded kind and are not upgraded by a simulated pass.", ""]
    for check in checks:
        if "provenance" in check:
            lines += ["- `" + check["result"] + "` (" + check["outcome"] + "): assertion "
                      "provenance " + check["provenance"] + " for this result."]
            continue
        lines.append("- **" + str(check["id"]) + " v" + str(check["version"]) + "** (`"
                     + check["result"] + "`: " + check["outcome"] + "): " + str(check["claim"]))
        lines += ["  - Basis: " + b.get("kind", "") + ": " + b.get("reference", "")
                  for b in check["basis"]]
        lines += ["  - Evidence " + ("met" if o.get("met") else "NOT met") + ": "
                  + o.get("requirement", "") for o in check["observed"]]
        lines.append("  - Not sufficient alone: " + "; ".join(check["insufficient_evidence"]))
        lines += ["  - Limitation: " + item for item in check["limitations"]]
    return lines + [""]


def render_markdown(report):
    run = report["run"]
    lines = ["# Seren Harness run " + run["id"], "", "> " + report["label"], "",
             "- Journey: " + run["journey"],
             "- Outcome: **" + report["outcome"] + "** (run status: " + run["status"] + ")",
             "- Clean completion: **" + ("yes" if report["completion"]["clean"] else "no")
             + "**" + ("" if report["completion"]["clean"]
                       else " (" + "; ".join(report["completion"]["reasons"]) + ")"),
             "- Employee fixture: " + report["employee"],
             "- Harness " + run["harness_version"] + "; Seren CLI "
             + (run["cli_version"] or "not recorded") + "; server build " + run["server_build"],
             "- Started " + run["created_at"] + "; updated " + run["updated_at"], "",
             "## Steps", "", "| Step | Outcome | Summary |", "|---|---|---|"]
    for step in report["steps"]:
        summary = step["summary"].replace("|", "\\|")
        if step["finding"]:
            summary += " (finding " + step["finding"] + ")"
        lines.append("| " + step["name"] + " | " + step["outcome"] + " | " + summary + " |")
    lines += [""] + render_checks(report["checks"], run["mode"] == SIMULATION)
    lines += ["## Cleanup", ""]
    lines += ["- " + c["kind"] + " " + c["target"] + ": **" + c["status"] + "**"
              for c in report["cleanup"]] or ["- No cleanup obligations were recorded."]
    if report["unresolved_actions"]:
        lines += ["", "## Unresolved mutations", ""]
        lines += ["- action " + str(a["action"]) + ": " + a["step"] + " " + a["kind"] + ": "
                  + a["status"] for a in report["unresolved_actions"]]
    lines += ["", "## Findings", ""]
    lines += ["- " + f["key"] + " [" + f["classification"] + ", provisional] " + f["title"]
              + " (" + str(f["occurrences"]) + " occurrence(s))"
              + ("; expectation rests partly on an unverified assumption"
                 if f["rests_on_unverified_assumption"] else "") for f in report["findings"]] \
        or ["- None."]
    if report.get("retest"):
        r = report["retest"]
        lines += ["", "## Finding retest", "",
                  "- Workflow outcome (whole journey): **" + report["outcome"] + "**",
                  "- Finding " + r["finding"] + " retest: **" + r["outcome"] + "** ("
                  + "; ".join(r["reasons"]) + "). Recovery condition: "
                  + str(r["recovery_condition"]) + ".",
                  "- Finding status: **" + r["finding_status"] + "**",
                  "- Shutdown and cleanup: " + ("confirmed" if r["cleanup_confirmed"]
                                                else "**not confirmed**; see Cleanup")]
        lines += ["- Other failures (diagnostic, not this finding): " + "; ".join(
            r["diagnostic"])] if r["diagnostic"] else []
    lines += ["", "## Limitations", ""]
    lines += ["- " + item for item in report["limitations"]] or ["- None recorded."]
    return "\n".join(lines) + "\n"


def write_report(store, run_id, expected_steps):
    report = build_report(store, run_id, expected_steps)
    directory = store.directory / "runs" / run_id
    _write_private(directory / "report.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write_private(directory / "report.md", render_markdown(report))
    return report, directory


def render_draft(store, key, related, include_identifiers=False, names=()):
    """Deterministic Markdown; UUIDs and employee names are pseudonymized by default."""
    item = store.finding(key)
    latest = store.run(item["latest_run"])
    steps = store.steps(item["latest_run"])
    payload = item["payload"]
    title = item["title"]
    commands = []
    reached = []
    for step in steps:
        reached.append(step["name"] + ": " + step["outcome"])
        commands.extend(step["evidence"].get("commands", []))
        if step["name"] == item["step"]:
            failed_evidence = step["evidence"]
            break
    else:
        failed_evidence = {}
    evidence = {k: v for k, v in failed_evidence.items() if k not in ("commands", "assertion")}
    failed_step = next((s for s in steps if s["name"] == item["step"]), None)
    checked = recorded_checks([failed_step], []) if failed_step else []
    fixture = json.loads(latest["fixture_json"]) if latest["fixture_json"] else {}
    lines = [
        "<!-- Seren Harness private draft " + key + ". Not published; review before filing. -->",
        "# " + title, ""]
    alias = store.alias_target(key)
    if alias:
        lines += ["> Record correction: this is not a separate finding. It is an alias of `"
                  + alias + "`, which tracks the observation; this record and its evidence "
                  "are retained as history. Do not file it.", ""]
    if latest["mode"] == SIMULATION:
        lines += ["> " + SIMULATION_LABEL, ""]
    elif latest["mode"] == "local":
        lines += ["> Local evidence only: not observed in the live service.", ""]
    lines += [
        "**Classification (provisional):** " + item["classification"]
        + ". Agree the expected behavior before treating this as a defect.", "",
        "## Customer impact", "", payload.get("impact") or "Not yet assessed.", "",
        "## Environment", "",
        "- Seren CLI: " + (latest["cli_version"] or "not recorded"),
        "- Server build: " + latest["server_build"]
        + (" (no trustworthy deployed-build identifier was available)"
           if latest["server_build"] == UNKNOWN_SERVER_BUILD else ""),
        "- Harness: seren-harness " + latest["harness_version"] + ", journey `"
        + item["journey"] + "`",
        "- Evidence mode: " + latest["mode"], ""]
    if payload.get("provenance"):
        # Offline functional review of observed responses: no fixture or CLI journey.
        lines += ["## Evidence provenance", ""]
        lines += ["- " + p for p in payload["provenance"]]
        if payload.get("source_explanation"):
            lines += ["", "## Source-based explanation (not observed)", "",
                      payload["source_explanation"]]
    else:
        lines += ["## Prerequisites verified", ""]
        lines += ["- " + k + ": " + json.dumps(v) for k, v in sorted(fixture.items())] \
            or ["- No fixture identity was recorded before the failure."]
    lines += ["", "## Reproduction", ""]
    lines += [str(i + 1) + ". " + text
              for i, text in enumerate(payload.get("reproduction") or reached)]
    if commands:
        lines += ["", "Seren CLI commands issued (in order):", "", "```sh"]
        lines += [" ".join(c) for c in commands]
        lines += ["```"]
    lines += ["", "## Expected", "", payload.get("expected", ""), "",
              "## Observed", "", payload.get("observed", ""), "",
              *(render_checks(checked, latest["mode"] == SIMULATION) if checked else []),
              "## Sanitized evidence", "", "```json",
              json.dumps(evidence, indent=2, sort_keys=True), "```", "",
              "## Unknowns", ""]
    lines += ["- " + u for u in payload.get("unknowns", [])] or ["- None recorded."]
    lines += ["", "## Retest", "", payload.get("retest") or retest_guidance(store, item), ""]
    retests = store.retests(key)
    if retests:
        superseded = {r["detail"].get("supersedes") for r in retests}
        lines += ["## Retest history", "",
                  "**Finding status:** " + finding_status(retests) + ".", ""]
        uncertain = latest_uncertainty(retests)
        if uncertain:
            lines += ["Latest evidence (run `" + uncertain["run_id"] + "`) is **"
                      + uncertain["outcome"] + "** and does not change that status"
                      + (": it is a diagnostic observation outside the finding's accepted "
                         "evidence" if not _eligible(uncertain) else "") + ".", ""]
        for r in retests:
            d = r["detail"]
            workflow = d.get("workflow_verdict") or d.get("scoped_verdict")
            lines.append(
                "- " + r["recorded_at"] + " run `" + r["run_id"] + "`"
                + (" (reanalysis of `" + d["supersedes"] + "`)" if d.get("supersedes") else "")
                + ": finding retest **" + r["outcome"] + "**"
                + (" (ineligible evidence)" if not _eligible(r) else "")
                + "; workflow " + str(workflow)
                + " (" + d.get("provenance", "") + ", revision " + str(d.get("revision"))
                + (", case v" + str(d["case_version"]) if d.get("case_version") else "") + ")"
                + ("; " + "; ".join(d["reasons"]) if d.get("reasons") else "")
                + ("; diagnostic only: " + "; ".join(d["diagnostic"])
                   if d.get("diagnostic") else "")
                + ("; " + d["difference"] if d.get("difference") else "")
                + ("; shutdown or cleanup not confirmed" if d.get("cleanup_confirmed") is False
                   else "")
                + ("; correction: " + d["correction"] if d.get("correction") else "")
                + (" [superseded by a later reanalysis; result retained]"
                   if r["run_id"] in superseded else ""))
        lines += ["", "Each run keeps its own evidence; the original occurrence is unchanged.",
                  ""]
    relations = store.relations(key)
    if relations:
        lines += ["## Related findings", ""]
        for rel in relations:
            if rel["relation"] == "alias_of":
                text = ("this record is an alias of `" + rel["other"] + "`" if rel["outgoing"]
                        else "`" + rel["other"] + "` is an alias of this finding")
            else:
                text = "related to `" + rel["other"] + "`"
            lines.append("- " + rel["recorded_at"] + " " + text + " ("
                         + rel["detail"].get("kind", "") + "): "
                         + rel["detail"].get("reason", ""))
        lines.append("")
    lines += ["## Occurrences", "",
              str(item["occurrences"]) + " run(s); first " + item["first_seen"] + ", latest "
              + item["last_seen"] + ".", ""]
    links = store.issue_links(key)
    if links:
        lines += ["## Confirmed issue links", ""]
        lines += ["- " + issue_reference(link) + ": " + link["url"] + " (confirmed by "
                  + link["confirmed_by"] + "; recorded " + link["recorded_at"] + ")"
                  for link in links]
        lines += ["", "A person's asserted association, recorded locally; the harness has not "
                  "checked the issue's existence, contents or status.", ""]
    lines += ["## Possibly related issues", ""]
    if related.get("status") == "ok":
        lines += ["- " + "#" + str(i["number"]) + " (" + i["state"].lower() + ") " + i["title"]
                  for i in related["issues"]] or ["- Search returned no candidates."]
        lines += ["", "Suggestions only; a human confirms any association."]
    else:
        lines += ["- Duplicate search unavailable: " + related.get("reason", "not configured")]
    text = "\n".join(lines) + "\n"
    text = store.sanitizer.text(text)
    if not include_identifiers:
        text = pseudonymize(text, names=names)
    return text


H = "python -m seren_harness "
UNFINISHED = ("active", "awaiting_human", "interrupted", "unresolved")
# What a later run of the same journey re-checks: (command, what it does).
JOURNEY_RETESTS = {
    "operational": ("run", "the bounded operational journey against the same fixture"),
    "provisioning": ("provision", "human-assisted provisioning of the same test employee"),
    "control": ("control", "the read-only controlled condition against the stopped employee"),
    "preflight": ("preflight", "the read-only CLI contract and integration checks"),
}


def retest_guidance(store, item):
    """The next authorized step for a finding without case-specific retest text, from recorded
    state only. Recovery comes before any new mutation; nothing is run or authorized here."""
    key, journey = item["key"], item["journey"]
    try:
        run = store.run(item["latest_run"])
    except StateError:
        return ("Limitation: the run that recorded this finding is not in the store, so no "
                "retest command is suggested. Establish the retest procedure first.")
    state = " (recorded state when this draft was rendered; check `" + H \
        + "status --config <config>` first)"
    if run["employee_key"] == "functional-review":
        return ("Collect new evidence for case `" + journey + "`, then link it: `" + H
                + "review-workflow --case <case-file> --evidence <bundle.json> --state-dir "
                "<finding-store> --out <new-folder> --retest " + key + "`. Only evidence the "
                "case scope accepts can reproduce or resolve it.")
    unresolved = store.employee_unresolved_actions(run["employee_key"])
    if unresolved:
        action = unresolved[0]
        return ("Before any retest or other mutation, settle action " + str(action["id"]) + " ("
                + action["kind"] + ") of run `" + action["run_id"] + "`, whose outcome is "
                "unknown" + state + ". `" + H + "resume " + action["run_id"]
                + " --config <config>` reconciles it from service state. Only if that cannot "
                "settle it, record an outcome someone verified independently: `" + H
                + "reconcile " + action["run_id"] + " --action " + str(action["id"])
                + " --outcome <applied|not-applied> --note \"<what was checked, and where>\" "
                "--config <config>`. Never guess the outcome.")
    for cleanup in store.open_cleanup(employee_key=run["employee_key"]):
        owner = store.run(cleanup["run_id"])
        if owner["status"] in UNFINISHED:
            return ("Cleanup (" + cleanup["kind"] + ") of run `" + cleanup["run_id"] + "` is "
                    + cleanup["status"] + state + ". Resume that run before any retest: `" + H
                    + "resume " + cleanup["run_id"] + " --config <config>` stops and confirms "
                    "the runtime it started.")
        return ("Shutdown from run `" + cleanup["run_id"] + "` is unconfirmed (cleanup "
                + cleanup["kind"] + " " + cleanup["status"] + ")" + state + ". Do not start "
                "a fresh run: confirm the test runtime is stopped first (status and desired "
                "state `stopped` on a read-only lifecycle read). The next authorized run "
                "retries this stop and refuses further work until shutdown is confirmed.")
    if run["status"] in UNFINISHED:
        return ("Run `" + run["id"] + "` is " + run["status"] + state + ". Continue it rather "
                "than starting another: `" + H + "resume " + run["id"] + " --config <config>`"
                + (" after the owner approves" if run["status"] == "awaiting_human" else "")
                + ". Resume repeats preflight and the fixture check and reconciles the journal.")
    if journey not in JOURNEY_RETESTS:
        return ("Limitation: no supported retest command is recorded for journey `" + journey
                + "`, so none is suggested. Establish the retest procedure first.")
    command, what = JOURNEY_RETESTS[journey]
    return ("No unknown mutation outcome or open cleanup is recorded" + state + ". When "
            "authorized, run " + what + ": `" + H + command + " --config <config> --retest "
            + key + "`, which records the result as this finding's retest. The fix is "
            "verified only when step `" + item["step"] + "` and every later step pass; record "
            "the CLI version and observation time with the result.")


DEFINITIVE = ("resolved", "still_failing")


def _eligible(retest):
    # Retests recorded before eligibility was stored keep the result they were recorded with;
    # a wrongly classified one is corrected by an explicit reanalysis, never reinterpreted here.
    return retest["detail"].get("eligible", True)


def current_retests(retests):
    superseded = {r["detail"].get("supersedes") for r in retests}
    return [r for r in retests if r["run_id"] not in superseded]


def finding_status(retests):
    """Lifecycle status from the latest current retest that is eligible and definitive.
    Only such a `resolved` closes the finding and only such a `still_failing` reopens it;
    inconclusive or ineligible evidence leaves the last justified status unchanged."""
    decisive = [r for r in current_retests(retests)
                if r["outcome"] in DEFINITIVE and _eligible(r)]
    return "resolved" if decisive and decisive[-1]["outcome"] == "resolved" else "open"


def latest_uncertainty(retests):
    """The newest current retest when it did not decide the status, else None."""
    current = current_retests(retests)
    if current and (current[-1]["outcome"] not in DEFINITIVE or not _eligible(current[-1])):
        return current[-1]
    return None


def write_draft(store, key, related, include_identifiers=False, names=()):
    """Create or update the single draft for a finding. Returns (path, created, changed)."""
    item = store.finding(key)
    existing = store.draft(key)
    path = store.directory / "drafts" / (key + ".md")
    text = render_draft(store, key, related, include_identifiers, names)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    _write_private(path, text)
    store.save_draft(key, path, item["occurrences"])
    return path, existing is None, previous != text
