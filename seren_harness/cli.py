"""Command-line interface. Exit codes: 0 passed, 1 failed, 2 blocked or not run,
3 awaiting a human checkpoint, 4 refused (lock, pending cleanup or changed config),
5 configuration or usage error."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from seren_harness import __version__, assertions, config as config_module, discord, seren
from seren_harness.github import suggest_related
from seren_harness.journey import JourneyRunner, Step
from seren_harness.models import (AWAITING_HUMAN, BLOCKED, FAILED, NOT_RUN, PASSED, RUN_AWAITING,
                                  RUN_COMPLETE, RUN_UNRESOLVED, blocked, passed)
from seren_harness.journey import applicable_steps, overall_outcome
from seren_harness.report import retest_link, write_draft, write_report
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import SerenCli
from seren_harness.state import (RECONCILED_NOT_APPLIED, REJECTED, LockHeld, StateError,
                                 Store, issue_reference)

# Fields the v0.10.0 apply update may change: it sets agent_identity_id and
# secret_resolution_result_id; whether the update also creates a revision is unverified.
APPLY_CHANGES = ("secret_resolution_result_id", "agent_identity_id", "active_revision_id")

EXIT = {PASSED: 0, FAILED: 1, BLOCKED: 2, NOT_RUN: 2, AWAITING_HUMAN: 3}
REFUSED, USAGE = 4, 5
RECONCILERS = {"managed_start": seren._reconcile_start, "managed_stop": seren._reconcile_stop,
               "managed_passwords_apply": seren._reconcile_apply}


def integrations(ctx):
    config = ctx.config
    evidence = {"sender": config.sender, "observer": config.observer,
                "observer_problem": ctx.observer_problem,
                "related_issue_search": bool(config.github.get("related_search"))}
    if ctx.observer is None or config.sender == "unavailable":
        return blocked("Discord sender or observer unavailable; the operational journey would "
                       "block at send_probe", unsupported=seren.contract.GAPS["automated_sender"]
                       if config.sender == "unavailable" else (ctx.observer_problem or
                       seren.contract.GAPS["reply_observation"]), **evidence)
    try:  # Read-only: proves the reader can see the channel; nothing is posted.
        evidence["channel_messages_readable"] = len(ctx.observer.messages())
    except discord.ObserverUnavailable as error:
        return blocked("Discord observer cannot read the test channel: " + str(error),
                       unsupported=str(error), **evidence)
    return passed("sender " + config.sender + " configured; observer " + config.observer
                  + " read the test channel", **evidence)


PREFLIGHT = [Step("preflight", seren.preflight), Step("inspect_target", seren.inspect_target),
             Step("integrations", integrations)]
# Read-only checks repeated on every resume: CLI identity and checksum, then approval,
# policy and revision. Earlier passes of these cannot carry across a pause.
RECHECKED_ON_RESUME = ("preflight", "verify_fixture")
JOURNEYS = dict(seren.JOURNEYS, preflight=PREFLIGHT)


class Session:
    def __init__(self, config):
        self.config = config
        self.sanitizer = Sanitizer(config.secret_values())
        self.store = Store(config.state_dir, self.sanitizer)
        # Explicit seren.config_home / seren.profile reach the CLI; without them the CLI
        # inherits the shell's session location (documented fallback).
        self.cli = SerenCli(config.executable, config.command_timeout, self.sanitizer,
                            config.cli_env())

    def context(self, run_id):
        ctx = seren.Context(self.config, self.store, run_id, self.cli,
                            output=lambda text: print(text, flush=True))
        try:
            if self.config.observer == "simulation":
                ctx.observer = discord.SimulatedChannel(self.config.simulation_channel)
            elif self.config.observer == "discord_rest":
                ctx.observer = discord.RestObserver(self.config.discord["channel_id"],
                                                    self.config.discord.get("reader_token_env"))
        except discord.ObserverUnavailable as error:
            ctx.observer_problem = str(error)
        return ctx

    def finish(self, run_id, journey):
        steps = [step.name for step in JOURNEYS[journey]]
        key = retest_link(self.store, run_id)
        if key and self.store.run(run_id)["status"] not in ("active", "awaiting_human"):
            classify_retest(self.store, run_id, key, steps)
        report, directory = write_report(self.store, run_id, steps)
        print("Run " + run_id + " (" + report["run"]["mode"] + "): " + report["outcome"]
              + " [run status " + report["run"]["status"] + "]")
        for step in report["steps"]:
            print("  " + step["outcome"].ljust(15) + step["name"] + ": " + step["summary"])
        for item in report["cleanup"]:
            print("  cleanup " + item["kind"] + ": " + item["status"])
        for limitation in report["limitations"]:
            print("  limitation: " + limitation)
        if report.get("retest"):
            r = report["retest"]
            print("Finding " + r["finding"] + " retest: " + r["outcome"] + " (finding status "
                  + r["finding_status"] + "); workflow " + report["outcome"]
                  + ("; shutdown or cleanup not confirmed" if not r["cleanup_confirmed"]
                     else ""))
        print("Report: " + str(directory / "report.md"))
        return EXIT.get(report["outcome"], 2)


def _refuse(session, run_id, message):
    session.store.event(run_id, "refused", {"reason": message})
    session.store.update_run(run_id, status="refused")
    print("REFUSED: " + message, file=sys.stderr)
    return REFUSED


def retest_problem(store, config, journey, key):
    """Why `key` cannot be retested by this run, or None. Checked before any run record or
    external action: same journey, evidence mode, employee and deployment; not an alias."""
    finding = store.finding(key)
    if finding is None:
        return "unknown finding: " + key
    if store.alias_target(key):
        return key + " is an alias of " + store.alias_target(key) + "; retest that finding"
    if finding["journey"] != journey:
        return key + " belongs to journey " + finding["journey"] + ", not " + journey
    if store.finding_mode(key) != config.environment:
        return (key + " was recorded on " + str(store.finding_mode(key)) + " evidence; this run "
                "is " + config.environment + " and cannot retest it")
    original = store.run(store.finding_runs(key)[0])
    if original["employee_key"] != config.employee_key:
        return key + " was recorded for another test employee"
    fixture = json.loads(original["fixture_json"]) if original["fixture_json"] else {}
    if fixture.get("deployment_id") not in (None, config.deployment_id):
        return key + " was recorded against another deployment"
    return None


def classify_retest(store, run_id, key, expected_steps):
    """The finding's own retest result from this run's saved steps. The recovery condition is
    the one the draft states for lifecycle findings: the failed step and every later step
    pass. Start acceptance or health alone never satisfies it. Supplementary criteria (reply
    content) are not part of that chain unless the finding is their own."""
    finding, run = store.finding(key), store.run(run_id)
    step = finding["step"]
    rows = {r["name"]: r for r in store.steps(run_id)}
    row = rows.get(step)
    workflow_steps = applicable_steps(expected_steps, list(rows.values()), run["status"])
    expected_steps = [s for s in expected_steps
                      if s == step or s not in assertions.SUPPLEMENTARY_STEPS]
    diagnostic = [r["name"] + ": " + r["outcome"]
                  + (" (finding " + r["finding_key"] + ")" if r["finding_key"] else "")
                  for r in rows.values() if r["outcome"] not in (PASSED, NOT_RUN)
                  and r["finding_key"] != key]
    if step not in expected_steps:
        outcome, reasons = "inconclusive", ["step `" + step + "` is not part of this journey"]
    elif row and row["outcome"] == FAILED and row["finding_key"] == key:
        outcome, reasons = "still_failing", ["step `" + step + "` failed with the same failure"]
    elif all(rows.get(s, {}).get("outcome") == PASSED
             for s in expected_steps[expected_steps.index(step):]):
        outcome, reasons = "resolved", ["step `" + step + "` and every later step passed"]
    else:
        later = expected_steps[expected_steps.index(step):]
        first = next(s for s in later if rows.get(s, {}).get("outcome") != PASSED)
        reached = rows.get(first, {}).get("outcome", NOT_RUN)
        if first != step:
            reason = ("step `" + step + "` passed, but later step `" + first + "` was "
                      + reached + "; recovery is not verified")
        elif reached == NOT_RUN:
            reason = "step `" + step + "` was not reached; an earlier step did not pass"
        else:
            reason = ("step `" + step + "` was " + reached + " with a different failure; the "
                      "original failure was not reproduced")
        outcome, reasons = "inconclusive", [reason]
    cleanup = store.cleanup_items(run_id)
    unresolved = store.actions(run_id, unresolved_only=True)
    detail = {"kind": "retest", "finding": key, "run": run_id,
              "workflow_verdict": overall_outcome(list(rows.values()), workflow_steps),
              "provenance": run["mode"], "revision": run["server_build"], "eligible": True,
              "recovery_condition": "step `" + step + "` and every later step pass",
              "reasons": reasons, "diagnostic": diagnostic,
              "cleanup_confirmed": all(c["status"] == "done" for c in cleanup),
              "unresolved_actions": len(unresolved)}
    store.record_retest(key, run_id, outcome, detail)
    return outcome


def start(session, journey, retest=None):
    config, store = session.config, session.store
    if retest:
        problem = retest_problem(store, config, journey, retest)
        if problem:
            print("ERROR: cannot link this run as a retest: " + problem, file=sys.stderr)
            return USAGE
    run_id = store.create_run(journey, config.employee_key, config.environment, config.digest,
                              __version__)
    if retest:
        store.event(run_id, "retest_link", {"finding": retest})
    ctx = session.context(run_id)
    if journey != "preflight":
        try:
            store.acquire_lock(config.employee_key, run_id)
        except LockHeld as error:
            return _refuse(session, run_id, str(error))
        refusal = seren.reconcile_employee(ctx)
        if refusal:
            store.release_lock(config.employee_key, run_id)
            return _refuse(session, run_id, refusal)
    return _execute(session, ctx, journey)


def _execute(session, ctx, journey):
    runner = JourneyRunner(session.store, ctx.run_id, journey, JOURNEYS[journey], ctx,
                           cleanup=seren.cleanup if journey != "preflight" else None,
                           retest_key=retest_link(session.store, ctx.run_id))
    status = runner.execute()
    if status == RUN_COMPLETE and session.store.actions(ctx.run_id, unresolved_only=True):
        session.store.update_run(ctx.run_id, status=RUN_UNRESOLVED)
    if journey != "preflight" and status != RUN_AWAITING:
        session.store.release_lock(session.config.employee_key, ctx.run_id)
    return session.finish(ctx.run_id, journey)


def resume(session, run_id):
    config, store = session.config, session.store
    run = store.run(run_id)
    if run["status"] in (RUN_COMPLETE, "refused"):
        print("Run " + run_id + " is " + run["status"] + "; start a new run instead.")
        return session.finish(run_id, run["journey"])
    if run["config_digest"] != config.digest or run["employee_key"] != config.employee_key:
        print("REFUSED: configuration differs from the one this run started with",
              file=sys.stderr)
        return REFUSED
    try:
        store.acquire_lock(config.employee_key, run_id)
    except LockHeld as error:
        print("REFUSED: " + str(error), file=sys.stderr)
        return REFUSED
    store.update_run(run_id, status="active")
    ctx = session.context(run_id)
    for action in store.actions(run_id, unresolved_only=True):
        reconcile = RECONCILERS.get(action["kind"], lambda _ctx: None)
        seren.reconcile_action(ctx, action["id"], reconcile)
    steps = [step.name for step in JOURNEYS[run["journey"]]]
    store.invalidate_steps(run_id, [s for s in RECHECKED_ON_RESUME if s in steps],
                           "rechecked on resume")
    invalidate_changed_assertions(store, run_id)
    if run["fixture_json"]:
        _check_identity(ctx, run, steps)
    if run["journey"] == "operational" and any(
            c["status"] == "done" for c in store.cleanup_items(run_id)):
        # The runtime this run started was already stopped; its evidence no longer holds.
        store.invalidate_steps(run_id, steps[steps.index("initial_state"):],
                               "runtime was stopped by earlier cleanup")
    return _execute(session, ctx, run["journey"])


def invalidate_changed_assertions(store, run_id):
    """A critical pass recorded under another (or no) contract version is not reused.

    The step re-runs under the current contract; cleanup is unaffected.
    """
    changed = [s["name"] for s in store.steps(run_id)
               if s["outcome"] == PASSED and assertions.stale(s["name"], s["evidence"])]
    if changed:
        store.invalidate_steps(run_id, changed, "assertion contract changed since the pass")
    return changed


def _check_identity(ctx, run, steps):
    import json
    recorded = json.loads(run["fixture_json"])
    detail, problem = seren._read_detail(ctx, "resume")
    current = seren.contract.identity(detail) if detail else None
    changes = ["identity_unreadable"] if current is None else \
        seren.contract.identity_changes(recorded, current)
    if run["journey"] == "provisioning" and any(
            a["kind"] == "managed_passwords_apply" and a["status"] not in
            (REJECTED, RECONCILED_NOT_APPLIED) for a in ctx.store.actions(run["id"])):
        # An apply that may have taken effect legitimately binds a new identity and result
        # (and possibly a new revision); read_back records the resulting baseline.
        changes = [c for c in changes if c not in APPLY_CHANGES]
    if changes:
        ctx.resume_changes = changes
        ctx.store.invalidate_steps(run["id"], steps[1:], "identity changed: " + ", ".join(changes))


def show_status(session):
    store = session.store
    for run in store.runs():
        print(run["id"] + "  " + run["journey"].ljust(12) + " " + run["mode"].ljust(10) + " "
              + run["status"])
    for item in store.open_cleanup():
        print("OPEN CLEANUP: run " + item["run_id"] + " " + item["kind"] + " " + item["status"]
              + " " + str(item["detail"].get("reason", "")))
    for action in store.actions(unresolved_only=True):
        print("UNRESOLVED MUTATION: run " + action["run_id"] + " action " + str(action["id"])
              + " " + action["kind"] + " " + action["status"])
    for finding in store.findings():
        print("FINDING " + finding["key"] + " [" + finding["classification"] + "] x"
              + str(finding["occurrences"]) + " " + finding["title"])
    return 0


def operator_reconcile(session, run_id, action_id, outcome, note):
    """Record what a human verified about a mutation the harness cannot reconcile itself."""
    status = session.store.operator_reconcile(run_id, action_id, outcome == "applied", note)
    print("Recorded action " + str(action_id) + " as " + status + " (operator: " + note + ")")
    return 0


def draft_issues(session, run_id):
    store, config = session.store, session.config
    keys = sorted({row["finding_key"] for row in store.steps(run_id) if row["finding_key"]}
                  | ({retest_link(store, run_id)} - {None}))
    if not keys:
        print("Run " + run_id + " recorded no findings; no draft needed.")
        return 0
    for key in keys:
        related = suggest_related(config.github, store.finding(key))
        path, created, changed = write_draft(store, key, related, config.include_identifiers,
                                             config.employee_names)
        state = "created" if created else ("updated" if changed else "unchanged")
        print("Draft " + state + " for " + key + " (" + str(store.finding(key)["occurrences"])
              + " occurrence(s)): " + str(path))
    print("Drafts are private files; nothing was published.")
    return 0


LINK_NOTE = ("Recorded as the person's asserted association; nothing was checked against or "
             "sent to GitHub.")


def _existing_store(state_dir):
    """Open a finding store that already exists; never create one by mistake."""
    if not (Path(state_dir).expanduser() / "state.sqlite3").is_file():
        raise StateError("no finding store at " + str(state_dir))
    return Store(Path(state_dir).expanduser())


def link_issue(args):
    """Record a person's confirmed finding-to-issue association. Local only."""
    store = _existing_store(args.state_dir)
    try:
        link, created = store.record_issue_link(args.finding, args.url, args.confirmed_by)
        reference = issue_reference(link)
        if not created:
            if link["confirmed_by"] != store.sanitizer.text(args.confirmed_by.strip()):
                print("REFUSED: " + args.finding + " is already linked to " + reference
                      + ", confirmed by " + link["confirmed_by"] + " (recorded "
                      + link["recorded_at"] + "); the original attribution is kept and "
                      "nothing was changed.", file=sys.stderr)
                return REFUSED
            print("Already recorded: " + args.finding + " -> " + reference + " (confirmed by "
                  + link["confirmed_by"] + "; recorded " + link["recorded_at"]
                  + "); unchanged.")
            return 0
        print("Confirmed link recorded: " + args.finding + " -> " + reference + " (confirmed by "
              + link["confirmed_by"] + "; recorded " + link["recorded_at"] + ").")
        print(LINK_NOTE)
        run = store.run(store.finding(args.finding)["latest_run"])
        if run["employee_key"] == "functional-review":
            from seren_harness.workflow_review import NO_RELATED
            path, _, _ = write_draft(store, args.finding, NO_RELATED)
            print("Private draft refreshed: " + str(path))
        else:
            print("Refresh its private draft with: python -m seren_harness draft-issue "
                  + run["id"] + " --config <config>")
        return 0
    finally:
        store.close()


def list_issue_links(args):
    """Show stored confirmed links; reads local state only."""
    store = _existing_store(args.state_dir)
    try:
        links = store.issue_links(args.finding)
        if not links:
            print("No confirmed issue links.")
            return 0
        for link in links:
            print(link["finding_key"] + "  " + issue_reference(link) + "  " + link["url"]
                  + "  confirmed by " + link["confirmed_by"] + "; recorded "
                  + link["recorded_at"])
        print(LINK_NOTE)
        return 0
    finally:
        store.close()


def parser():
    root = argparse.ArgumentParser(prog="python -m seren_harness", description=__doc__)
    root.add_argument("--version", action="version", version="seren-harness " + __version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name, description in (("check-setup", "check one operator configuration locally; no service request"),
                              ("test-live", "read one employee tool catalog; no start, update or retry")):
        command = commands.add_parser(name, help=description)
        command.add_argument("--config", required=True, help="filled-in copy of operator.example.json")
    command = commands.add_parser("functional", help="autonomous functional campaign: the "
                                  "operating agent chooses, assigns and judges work for a dedicated "
                                  "employee (see docs/OPERATOR.md)")
    command.add_argument("action", choices=("check", "readiness", "run", "status", "report",
                                            "reconcile", "approval-template", "submit-issue"),
                         help="check: local prerequisites only; readiness: read-only live checks of "
                         "the journey's assumptions (no changes, no model prompt); run: start or "
                         "resume the campaign; submit-issue: file one approved draft")
    command.add_argument("--config", required=True, help="operator configuration with a campaign section")
    command.add_argument("--max-tasks", type=int, help="run: pause at a task boundary after this "
                         "many tasks in this invocation")
    command.add_argument("--action-id", type=int, help="reconcile: action id from the report")
    command.add_argument("--outcome", choices=("applied", "not-applied"), help="reconcile")
    command.add_argument("--note", help="reconcile: what you checked, and where")
    command.add_argument("--finding", help="approval-template/submit-issue: finding key")
    command.add_argument("--approval", help="submit-issue: the completed approval file")
    for name, text in (("preflight", "read-only CLI contract and integration checks"),
                       ("provision", "human-assisted approval of the test employee"),
                       ("control", "read-only controlled condition: readiness rejects the "
                                   "stopped test employee"),
                       ("run", "bounded operational journey"),
                       ("status", "runs, open cleanup, unresolved mutations, findings")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--config", required=True)
        if name != "status":
            command.add_argument("--retest", help="existing finding of this journey that "
                                 "this run retests (checked before anything runs)")
    for name, text in (("resume", "reconcile, then continue a run"),
                       ("report", "rebuild a run's saved report; no live work"),
                       ("draft-issue", "write private issue drafts for a run's findings")):
        command = commands.add_parser(name, help=text)
        command.add_argument("run_id")
        command.add_argument("--config", required=True)
    command = commands.add_parser("review-response", help="offline review of an operator-copied "
                                  "Discord response; no credentials or live calls")
    command.add_argument("--case", required=True)
    command.add_argument("--response", required=True)
    command.add_argument("--out", required=True, help="new or empty output directory")
    command.add_argument("--metadata", help="optional JSON containing operator-supplied metadata")
    command.add_argument("--assessment", help="completed assessment-template.json from a prior review")
    command.add_argument("--synthetic", action="store_true", help="label demonstration input, not live evidence")
    command = commands.add_parser("review-workflow", help="offline scoped review of one "
                                  "workflow's observed responses; records findings and retests")
    command.add_argument("--case", required=True)
    command.add_argument("--evidence", required=True, help="evidence bundle JSON")
    command.add_argument("--state-dir", required=True, help="private finding store directory")
    command.add_argument("--out", required=True, help="new or empty output directory")
    command.add_argument("--assessment", help="completed assessment-template.json")
    command.add_argument("--retest", help="finding key this evidence retests")
    command.add_argument("--reanalysis-of", help="earlier retest run whose same evidence is "
                         "re-evaluated under a corrected case; the earlier result is kept")
    command.add_argument("--correction", help="why the reanalysis was needed (required with "
                         "--reanalysis-of)")
    command = commands.add_parser("reclassify-retest", help="record correction: re-derive a "
                                  "recorded finding retest from its saved criteria under the "
                                  "current retest rules; not a new observation")
    command.add_argument("--state-dir", required=True, help="private finding store directory")
    command.add_argument("--report", required=True, help="the earlier run's saved report.json")
    command.add_argument("--retest", required=True, help="finding key the run retested")
    command.add_argument("--reanalysis-of", required=True, help="the earlier retest run")
    command.add_argument("--correction", required=True, help="why the correction is needed")
    command.add_argument("--out", required=True, help="new or empty private output directory")
    command = commands.add_parser("collect", help="collect a case's evidence automatically "
                                  "through the target's configured interface and review it")
    command.add_argument("case", help="case id or file name")
    command.add_argument("--target", default=str(Path(__file__).resolve().parents[1]
                                                 / "targets" / "seren.json"),
                         help="target configuration (default: targets/seren.json)")
    command.add_argument("--target-root", help="override the target checkout location")
    command.add_argument("--from-copy", help="import a retained Discord copy instead of "
                         "running the collector")
    command.add_argument("--retest", help="finding key to link; by default the case's single "
                         "open finding is linked")
    command.add_argument("--no-retry", action="store_true",
                         help="make at most one collector attempt, even after a timeout")
    command.add_argument("--home", default="~/seren-harness-reviews",
                         help="private folder for reviews and the finding store")
    command.add_argument("--state-dir", help="use an existing finding store")
    command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                         help=argparse.SUPPRESS)
    command = commands.add_parser("campaign", help="run or inspect one bounded campaign: a fixed "
                                  "queue of read-only cases, each requested at most once")
    command.add_argument("action", choices=("run", "status", "select", "verify-handoff",
                                            "sessions"),
                         help="select: preview a catalog selection from saved evidence only; "
                         "sessions: run the campaign across at most two Claude sessions")
    command.add_argument("definition", help="campaign definition or catalog, e.g. "
                         "campaigns/<id>.json or catalogs/<id>.json")
    command.add_argument("--authorization", help="owner-approved authorization record (run)")
    command.add_argument("--max-cases", type=int, help="run: end this invocation at a case "
                         "boundary after this many cases; the campaign stays open")
    command.add_argument("--claude", help="sessions: absolute path of the Claude CLI")
    command.add_argument("--home", default="~/seren-harness-reviews",
                         help="private folder for reviews, findings and campaign state")
    command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                         help=argparse.SUPPRESS)
    command = commands.add_parser("fixture", help="set up, clean up or inspect the approved "
                                  "dedicated-project database fixture (no campaign reads)")
    command.add_argument("action", choices=("setup", "cleanup", "status"))
    command.add_argument("--package", default="docs/db-fixture/lifecycle-package.proposed.json",
                         help="approved fixture lifecycle package")
    command.add_argument("--manifest", default="docs/db-fixture/fixture-manifest.json",
                         help="frozen fixture manifest")
    command.add_argument("--authorization", help="owner-approved fixture authorization "
                         "(setup and cleanup)")
    command.add_argument("--home", default="~/seren-harness-reviews",
                         help="private folder for fixture state and evidence")
    command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                         help=argparse.SUPPRESS)
    command = commands.add_parser("instruction-update", help="the response-correctness "
                                  "package's bounded update phase (prerequisite reads, preview, "
                                  "one update, read-back)")
    command.add_argument("--config", required=True, help="harness configuration (pinned)")
    command.add_argument("--package", required=True, help="response-correctness package JSON")
    command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                         help=argparse.SUPPRESS)
    command = commands.add_parser("preview-diagnostic", help="one bounded diagnostic preview "
                                  "of the corrected instruction (reads, one preview, reads; "
                                  "never an update)")
    command.add_argument("--config", required=True, help="harness configuration (pinned)")
    command.add_argument("--package", required=True, help="preview diagnostic package JSON")
    command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                         help=argparse.SUPPRESS)
    command = commands.add_parser("prerequisite-read", help="one bounded managed-get of the "
                                  "dedicated fixture, with a fixed failure classification")
    command.add_argument("--config", required=True, help="harness configuration (pinned)")
    command.add_argument("--package", required=True, help="prerequisite read package JSON")
    command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                         help=argparse.SUPPRESS)
    command = commands.add_parser("limits", help="local check: the response-correctness "
                                  "package's pinned settings and derived execution ceilings")
    command.add_argument("--config", required=True, help="harness configuration to check")
    command = commands.add_parser("dbread", help="the bounded database-read campaign over the "
                                  "dedicated-project fixture (setup, prepare, run, cleanup)")
    command.add_argument("action", choices=("setup", "prepare", "run", "cleanup", "status",
                                            "verify-handoff", "sessions"))
    command.add_argument("--package", help="approved lifecycle package (default: "
                         "docs/db-fixture/lifecycle-package.proposed.json under the root)")
    command.add_argument("--manifest", help="frozen fixture manifest (default: "
                         "docs/db-fixture/fixture-manifest.json under the root)")
    command.add_argument("--authorization", help="the one owner-approved campaign authorization")
    command.add_argument("--max-cases", type=int, help="run: stop at a case boundary after "
                         "this many reads; the campaign stays open")
    command.add_argument("--claude", help="sessions: absolute path of the Claude CLI")
    command.add_argument("--home", default="~/seren-harness-reviews",
                         help="private folder for state, evidence and findings")
    command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                         help=argparse.SUPPRESS)
    command = commands.add_parser("discovery", help="persistent offline bug discovery (the "
                                  "default path): status, stop, resume, supervise, work, "
                                  "run-case, init, agent-plist; see docs/ADVANCED.md")
    command.add_argument("action", choices=("status", "stop", "resume", "supervise", "work",
                                            "run-case", "init", "agent-plist", "due",
                                            "model-work", "enable-model-worker", "work-host",
                                            "select-maintenance", "clear-maintenance"))
    command.add_argument("--goal", help="select-maintenance: goal id from the plan")
    command.add_argument("--plan", help="select-maintenance: committed docs/maintenance/ plan")
    command.add_argument("--owner", help="select-maintenance: who selected it")
    command.add_argument("--home", default="~/seren-harness-live/discovery",
                         help="private discovery directory (default: %(default)s)")
    command.add_argument("--session", help="work: session id assigned by the supervisor")
    command.add_argument("--deadline", help="model-work: wall-clock deadline (epoch seconds)")
    command.add_argument("--executable", help="init: the pinned Seren CLI 0.10.0 binary")
    command.add_argument("--case", help="run-case: catalog id, e.g. C1")
    command.add_argument("--repeats", type=int, default=3, help="run-case: repeats per scenario")
    command.add_argument("--claude", help="enable-model-worker: Claude Code executable")
    command.add_argument("--model", default="claude-opus-5-5", help="enable-model-worker")
    command.add_argument("--contract-map", help="enable-model-worker: read-only map directory")
    command.add_argument("--pinned-source-repo", help="enable-model-worker: Seren source repo")
    command.add_argument("--pinned-commit", default="2317949", help="enable-model-worker")
    command = commands.add_parser("worker", help="model-worker verbs, each run inside the "
                                  "worker sandbox; see docs/ADVANCED.md")
    command.add_argument("verb", choices=("status", "test", "validate", "run-case", "commit",
                                          "discard", "candidate", "note", "wait", "followup",
                                          "live-request", "live-review", "synthetic-check",
                                          "check", "progress"))
    command.add_argument("--candidate", help="commit: candidate or follow-up id (repair budget)")
    command.add_argument("--finding", action="append", help="followup: linked finding key")
    for name in ("problem", "consequence", "confidence", "uncertainty", "exposure",
                 "workaround", "cost"):
        command.add_argument("--" + name, help="followup: %s (say 'unknown' if unsupported)"
                             % name)
    command.add_argument("--next-step", dest="next_step", help="followup: next evidence step")
    command.add_argument("--needs", help="followup: tier0|live_authority|owner_approval")
    command.add_argument("--evidence", help="followup: new evidence when reopening")
    command.add_argument("modules", nargs="*", help="test: tests.test_* modules")
    command.add_argument("--runner", choices=("unittest", "pytest"),
                         help="test (maintenance sessions only): focused diagnostic runner")
    command.add_argument("--home", default="~/seren-harness-live/discovery")
    command.add_argument("--case", help="run-case: catalog id")
    command.add_argument("--repeats", type=int, default=1)
    command.add_argument("-m", "--message", help="commit: subject")
    command.add_argument("--id", help="candidate: id, e.g. C11")
    command.add_argument("--status", help="candidate: proposed|implemented|committed|"
                         "executed|deferred|rejected")
    command.add_argument("--rationale", help="candidate: why it is useful")
    command.add_argument("--basis", help="candidate: source/spec basis and grade")
    command.add_argument("--text", help="note: progress text")
    command.add_argument("--batch", help="live-request: JSON array of 2-4 independent cases")
    command.add_argument("--journey", help="live-request: an eligible granted journey package "
                         "id from worker status (instead of --case)")
    command.add_argument("--comparison", help="live-request: JSON cross-journey comparison "
                         "(see docs/ADVANCED.md)")
    command.add_argument("--classification", help="live-review: passed|seren_functional_failure|"
                         "model_answer_failure|unresolved_contract_or_attribution|incomplete")
    command.add_argument("--summary", help="live-review: concise expected versus observed, 20-300 chars")
    command.add_argument("--evidence-limits", help="live-review: concise recurrence and attribution limits, 20-300 chars")
    command.add_argument("--customer-impact", help="live-review: evidence-bounded customer consequence")
    command.add_argument("--wake-on", choices=("change", "time"), default="change",
                         help="wait: actionable change (default), or justified timed eligibility")
    command.add_argument("--hours", default="24", help="timed wait: hours (1-168)")
    command.add_argument("--reason", help="wait: why no justified work remains")
    campaign = commands.add_parser("invoice-campaign", help="bounded supplied-record campaign")
    actions = campaign.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare", help="offline package only")
    prepare.add_argument("--session", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--suite", choices=("arithmetic-v1", "conflicts-v1", "bd-brief-v1",
                                                  "bd-reliability-v1"),
                         help="frozen case suite; default is the original arithmetic campaign")
    execute = actions.add_parser("run", help="execute only an exactly authorized package")
    execute.add_argument("--package", required=True)
    execute.add_argument("--authorization", required=True)
    fixture = commands.add_parser("supplied-fixture", help="create one supplied-data fixture, "
                                  "then run the supplied-record campaign on it")
    actions = fixture.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare", help="offline package only")
    prepare.add_argument("--template", required=True,
                         help="session template: pinned CLI and private state/home paths")
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--suite", default="bd-reliability-v1",
                         choices=("arithmetic-v1", "conflicts-v1", "bd-brief-v1",
                                  "bd-reliability-v1"))
    execute = actions.add_parser("run", help="execute only an exactly authorized package")
    execute.add_argument("--package", required=True)
    execute.add_argument("--authorization", required=True)
    replay = commands.add_parser("stream-replay", help="one read-only replay of a completed "
                                 "Clock Test run through `cloud run stream`")
    actions = replay.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare", help="offline package from a saved run record")
    prepare.add_argument("--record", required=True)
    prepare.add_argument("--output", required=True)
    execute = actions.add_parser("run", help="execute only an exactly authorized package")
    execute.add_argument("--package", required=True)
    execute.add_argument("--session", required=True)
    execute.add_argument("--authorization", required=True)
    command = commands.add_parser("employee-time", aliases=["employee-lookup", "employee-data"],
                                  help="one authorized isolated clock, publisher lookup or branch read")
    command.add_argument("--session", required=True, help="private approved session configuration")
    command = commands.add_parser("guide", help="guided functional review: paste observed "
                                  "responses, answer a few questions, get a report")
    command.add_argument("--case", help="case id or file name; omit to choose from a list")
    command.add_argument("--home", default="~/seren-harness-reviews",
                         help="private folder for reviews and the finding store "
                         "(default: %(default)s)")
    command.add_argument("--state-dir", help="use an existing finding store instead of "
                         "<home>/findings")
    command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                         help=argparse.SUPPRESS)
    command = commands.add_parser("reconcile", help="record a human-verified outcome for a "
                                  "mutation the harness cannot reconcile; changes nothing live")
    command.add_argument("run_id")
    command.add_argument("--action", type=int, required=True, help="action id from status")
    command.add_argument("--outcome", choices=("applied", "not-applied"), required=True)
    command.add_argument("--note", required=True, help="what was checked, and where")
    command.add_argument("--config", required=True)
    command = commands.add_parser("link-issue", help="record a person's confirmed association "
                                  "between a finding and an existing GitHub issue; local only")
    command.add_argument("finding", help="finding key")
    command.add_argument("url", help="https://github.com/<owner>/<repo>/issues/<number>")
    command.add_argument("--confirmed-by", required=True, help="who confirmed the association")
    command.add_argument("--state-dir", required=True, help="existing finding store directory")
    command = commands.add_parser("issue-links", help="list confirmed issue links; local only")
    command.add_argument("--state-dir", required=True, help="existing finding store directory")
    command.add_argument("--finding", help="only this finding key")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "functional":
        from seren_harness import functional
        return functional.run(args)
    if args.command in ("check-setup", "test-live"):
        from seren_harness import operator
        return operator.run(args)
    if args.command == "discovery":
        from seren_harness import discovery
        return discovery.run(args)
    if args.command == "worker":
        from seren_harness import model_worker
        return model_worker.run(args)
    if args.command == "invoice-campaign":
        from seren_harness import invoice_campaign
        return invoice_campaign.run(args)
    if args.command == "supplied-fixture":
        from seren_harness import supplied_fixture
        return supplied_fixture.run(args)
    if args.command == "stream-replay":
        from seren_harness import stream_replay
        return stream_replay.run(args)
    if args.command == "review-response":
        from seren_harness.response_review import run
        return run(args)
    if args.command in ("employee-time", "employee-lookup", "employee-data"):
        from seren_harness import employee_run
        return employee_run.run(args)
    if args.command == "collect":
        from seren_harness import collect
        return collect.run(args)
    if args.command == "campaign":
        from seren_harness import campaign
        return campaign.run(args)
    if args.command == "instruction-update":
        from seren_harness import instruction_update
        return instruction_update.run(args)
    if args.command == "preview-diagnostic":
        from seren_harness import preview_diagnostic
        return preview_diagnostic.run(args)
    if args.command == "prerequisite-read":
        from seren_harness import prerequisite_read
        return prerequisite_read.run(args)
    if args.command == "limits":
        from seren_harness import execution_limits
        return execution_limits.run(args)
    if args.command == "dbread":
        from seren_harness import dbread
        return dbread.run(args)
    if args.command == "fixture":
        from seren_harness import db_fixture
        return db_fixture.run(args)
    if args.command == "guide":
        from seren_harness import guide
        return guide.run(args)
    if args.command == "review-workflow":
        from seren_harness import workflow_review
        return workflow_review.run(args)
    if args.command == "reclassify-retest":
        from seren_harness import workflow_review
        return workflow_review.reclassify_run(args)
    if args.command in ("link-issue", "issue-links"):
        try:
            return (link_issue if args.command == "link-issue" else list_issue_links)(args)
        except StateError as error:
            print("ERROR: " + str(error), file=sys.stderr)
            return USAGE
    try:
        session = Session(config_module.load(args.config))
    except (config_module.ConfigError, StateError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return USAGE
    try:
        if args.command in ("preflight", "run", "provision", "control"):
            journey = {"run": "operational", "provision": "provisioning"}.get(args.command,
                                                                                args.command)
            return start(session, journey, args.retest)
        if args.command == "resume":
            return resume(session, args.run_id)
        if args.command == "status":
            return show_status(session)
        if args.command == "report":
            return session.finish(args.run_id, session.store.run(args.run_id)["journey"])
        if args.command == "reconcile":
            return operator_reconcile(session, args.run_id, args.action, args.outcome,
                                      args.note)
        return draft_issues(session, args.run_id)
    except StateError as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return USAGE
    finally:
        session.store.close()
