"""The functional campaign's disclosure boundary: local issue drafts, per-finding approval and
the one approved GitHub submission.

Drafts are written only for reproduced draftable findings and are never submitted by a campaign.
`submit_issue` is a separate command: it files one draft once, only with an approval bound to the
exact repository, title and body, and never retries an uncertain create. The campaign module
imports from here; nothing here imports the campaign module.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from seren_harness import __version__, functional_journeys as journeys, functional_research as research
from seren_harness.functional_common import DRAFTABLE, ConfigError, campaign_dir, iso, parse_time
from seren_harness.private_issues import marker, readable_text
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer, pseudonymize


# =============================================================================== drafts
def write_drafts(store, ledger, folder, settings):
    """One local GitHub-ready draft per reproduced draftable finding; never submitted here."""
    drafts = {}
    for finding in store.findings():
        category = finding["context"].get("category")
        items = [t for t in ledger.data["tasks"] if (t.get("evaluation") or {}).get("finding_key") == finding["key"]]
        if any(t.get("kind") == "journey_work" and t["task"]["family"] == "journey:" + research.TEMPLATE
               for t in items):
            drafts[finding["key"]] = {"status": "not_drafted", "reason": "research journey results are "
                                      "reported for review, never drafted as issues"}
            continue
        if category not in DRAFTABLE or finding["occurrences"] < 2:
            drafts[finding["key"]] = {"status": "not_drafted", "reason":
                                      "single occurrence; reproduce before drafting"
                                      if category in DRAFTABLE else "not an issue category"}
            continue
        journey_items = items and items[0].get("kind") == "journey_work"
        title, body = (render_journey_issue if journey_items else render_issue)(finding, items, ledger)
        path = folder / "drafts" / (finding["key"] + ".md")
        text = "# " + title + "\n\n" + body + "\n"
        _write_private(path, text)
        meta = {"finding_key": finding["key"], "title": title,
                "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "occurrences": finding["occurrences"], "path": str(path)}
        _write_private(folder / "drafts" / (finding["key"] + ".json"), json.dumps(meta, indent=2))
        drafts[finding["key"]] = dict(meta, status="drafted")
    return drafts


def _disclosure_text(ledger):
    """(clean, fenced) for one draft: a fresh sanitizer and pseudonym mapping per draft. Pseudonyms
    are numbered in the order clean() is first called, so each renderer keeps its own call order."""
    sanitizer, mapping = Sanitizer(), {}
    names = [ledger.data["employee"].get("name") or ""]

    def clean(value):
        return pseudonymize(sanitizer.value(value), names, mapping)

    def fenced(text):
        return "```text\n" + clean(text or "").replace("```", "'''") + "\n```"
    return clean, fenced


def render_journey_issue(finding, items, ledger):
    """The user story of a failed journey stage, exactly as it would be submitted."""
    clean, fenced = _disclosure_text(ledger)
    first = items[-1]   # The latest occurrence: its expectation, evidence and steps agree.
    record = next(r for r in ledger.data["journeys"] if r["id"] == first["journey"])
    spec, payload = record["spec"], finding["payload"]
    stages = []
    for journey_id in sorted({i["journey"] for i in items}):
        run = next(r for r in ledger.data["journeys"] if r["id"] == journey_id)
        for entry in run["stages"]:
            stage_item = ledger.task(entry["item"])
            evaluation = stage_item.get("evaluation") or {}
            outcome = evaluation.get("verdict") or stage_item["phase"]
            stages.append("- %s %s (%s): %s%s" % (
                journey_id, entry["stage"], "harness setup, not under test" if stage_item["kind"] ==
                "journey_setup" else "harness cleanup" if stage_item["kind"] == "journey_cleanup"
                else "employee work", outcome, " — " + clean(evaluation["summary"]) if evaluation.get("summary") else ""))
    evaluation = first["evaluation"]
    calls = "\n".join("- %s: completed=%s error=%s reads the journey table=%s other relations=%s "
                      "structured result rows=%s; query: `%s`" % (
                          c["tool"], c["completed"], c["error"], c["reads_table"],
                          ", ".join(c["other_relations"]) or "none", c["result_rows"], clean(c["query"]))
                      for c in evaluation.get("calls") or []) or \
        "- none: the employee called no tool in this stage"
    steps = ["1. Harness setup (not part of the work under test): create the dedicated table %s "
             "holding %d synthetic rows through the pinned `seren psql` route." % (spec["table"], len(spec["rows"]))]
    for entry in record["stages"]:
        stage_item = ledger.task(entry["item"])
        if stage_item["kind"] == "journey_work":
            steps.append("%d. Stage %s: send the message below with `seren agent cloud run start "
                         "--deployment-id <employee> --message <message> --async`, then read it with "
                         "`seren agent cloud run get`." % (len(steps) + 1, entry["stage"]))
        elif entry["stage"] == "update_documents":
            steps.append("%d. Harness setup: add the decision record to %s." % (len(steps) + 1, spec["table"]))
        if entry["item"] == first["id"]:
            break
    messages = "\n\n".join("<details>\n<summary>Stage %s message (synthetic)</summary>\n\n%s\n\n</details>"
                            % (ledger.task(e["item"])["stage"], fenced(ledger.task(e["item"])["task"]["message"]))
                            for e in record["stages"] if ledger.task(e["item"])["kind"] == "journey_work"
                            and record["stages"].index(e) <= [x["item"] for x in record["stages"]].index(first["id"]))
    claim = ("\n\n**The employee presented the deliverable as verified; the independent read-back "
             "contradicts it.**" if evaluation.get("claim_contradicted") else "")
    blocks = [marker(finding["key"]), "## User goal",
              readable_text(clean("%s (%s)." % (spec["objective"], spec["customer"]))),
              "## Setup",
              readable_text(clean("The harness seeded a dedicated synthetic table %s and gave the employee "
                                  "no copy of the data: the work required the employee's own %s calls. "
                                  "Setup and cleanup are harness operations, not the behaviour under test."
                                  % (spec["table"], journeys.TOOL))),
              "## Stages attempted", "\n".join(stages),
              "## Where the user became blocked",
              readable_text(clean("Stage %s: %s. Observed in %d stage run(s)." % (
                  first["stage"], payload["observed"], finding["occurrences"]))) + claim,
              "## Expected", readable_text(clean(payload["expected"])),
              "## Observed evidence",
              "Employee tool calls in the failed stage:\n" + calls,
              "Independent read-back by the harness (expected values): `%s`" % clean(json.dumps(
                  evaluation.get("expected") or {}, sort_keys=True)),
              "Employee reply excerpt (untrusted output, sanitized):", fenced(evaluation.get("reply_excerpt")),
              "## User impact", readable_text(IMPACT_JOURNEY),
              "## Reproduction", "\n".join(steps), messages,
              "## Classification and uncertainty",
              readable_text("%s; provisional. %s" % (finding["context"].get("category"),
                                                     " ".join(clean(payload.get("unknowns") or [])))),
              "## Environment",
              "Seren CLI 0.10.0; seren-harness %s functional campaign journey `%s`; no server build "
              "identifier is exposed by the service." % (__version__, record["template"]),
              "---", "**Finding key:** `%s`" % finding["key"]]
    return clean(finding["title"])[:200], "\n\n".join(blocks)


IMPACT_JOURNEY = ("A customer relying on this employee for the journey would receive a deliverable "
                  "that does not reflect their own data or prior work, without an error to warn them.")


def render_issue(finding, items, ledger):
    """Title and body exactly as they would be submitted. UUIDs are pseudonymized."""
    clean, fenced = _disclosure_text(ledger)
    first = items[0] if items else None
    payload = finding["payload"]
    title = clean(finding["title"])[:200]
    actual = "\n".join("- %s (%s, %s): %s; failed criteria: %s" % (
        item["id"], item["task"]["reference"], item["decision"]["purpose"],
        clean(item["evaluation"]["summary"]), ", ".join(item["evaluation"]["failed_criteria"]) or "none")
        for item in items)
    lines = [marker(finding["key"]), "## Summary",
             readable_text(clean("%s Observed in %d tasks." % (payload["observed"], finding["occurrences"]))),
             "## Expected", readable_text(clean(payload["expected"])), "## Actual", actual,
             "Reply excerpt from %s (untrusted employee output, sanitized):" % (first["id"] if first else "n/a"),
             fenced((first or {}).get("evaluation", {}).get("reply_excerpt")),
             "## Reproduction",
             "1. Use a dedicated Seren employee with no tools (CLI 0.10.0).\n"
             "2. Send the synthetic task message below with `seren agent cloud run start "
             "--deployment-id <employee> --message <message> --async`.\n"
             "3. Read the run with `seren agent cloud run get` and compare the reply with the "
             "expected values.",
             "<details>\n<summary>Task message (synthetic)</summary>\n\n"
             + fenced(first["task"]["message"] if first else "") + "\n\n</details>",
             "## Classification and uncertainty",
              readable_text("%s; %d occurrence(s) across tasks %s. Provisional: agree expected "
                            "behaviour before treating this as a defect." % (
                                finding["context"].get("category"), finding["occurrences"],
                                ", ".join(i["id"] for i in items))),
              readable_text(" ".join(clean(payload.get("unknowns") or []))),
              "## Environment",
              "Seren CLI 0.10.0; seren-harness %s functional campaign; no server build identifier "
              "is exposed by the service." % __version__,
              "---", "**Finding key:** `%s`" % finding["key"]]
    return title, "\n\n".join(line for line in lines if line is not None)


# =============================================================================== submission
class GhTransport:
    """`gh api` against github.com with a configured, absolute gh executable."""

    def __init__(self, executable):
        if not isinstance(executable, str) or not os.path.isabs(executable) or not os.access(executable, os.X_OK):
            raise ConfigError("campaign.github.gh_executable must be an absolute path to gh")
        self.executable = executable

    def __call__(self, path, body=None):
        env = {k: v for k, v in os.environ.items() if k not in
               ("GH_HOST", "GH_REPO", "GH_DEBUG", "GH_PAGER", "GH_FORCE_TTY")}
        argv = [self.executable, "api", "--hostname", "github.com", "--method",
                "POST" if body is not None else "GET", path]
        if body is not None:
            argv += ["--input", "-"]
        done = subprocess.run(argv, input=json.dumps(body) if body is not None else None,
                              capture_output=True, text=True, timeout=30, env=env)
        if done.returncode:
            raise ValueError("GitHub request failed")
        return json.loads(done.stdout)


APPROVAL_FIELDS = {"approved", "finding_key", "repository", "title_sha256", "body_sha256",
                   "approved_by", "approved_at", "expires_at"}


def _draft(settings, key):
    folder = campaign_dir(settings) / "drafts"
    try:
        meta = json.loads((folder / (key + ".json")).read_text())
        text = (folder / (key + ".md")).read_text()
    except (OSError, ValueError):
        raise ConfigError("no local draft for finding " + key) from None
    title, _, body = text.partition("\n\n")
    title, body = title[2:], body[:-1]
    if (hashlib.sha256(title.encode()).hexdigest() != meta["title_sha256"]
            or hashlib.sha256(body.encode()).hexdigest() != meta["body_sha256"]):
        raise ConfigError("draft file differs from its recorded hashes; regenerate the report")
    return meta, title, body


def approval_template(settings, key, out):
    meta, _, _ = _draft(settings, key)
    path = campaign_dir(settings) / "approvals" / (key + ".json")
    template = {"approved": False, "finding_key": key,
                "repository": settings["campaign"]["github"]["repository"],
                "title_sha256": meta["title_sha256"], "body_sha256": meta["body_sha256"],
                "approved_by": None, "approved_at": None, "expires_at": None}
    _write_private(path, json.dumps(template, indent=2))
    print("Approval template (not an approval): " + str(path), file=out)
    print("Read the draft " + meta["path"] + ". To approve this exact disclosure, set approved "
          "true, approved_by, approved_at and a near expires_at. Any draft change voids it.", file=out)
    return 0


def submit_issue(settings, key, approval_path, out, transport=None):
    """File one approved draft once. Returns an exit code; never retries an uncertain create."""
    github = settings["campaign"]["github"]
    if github["mode"] != "approved_submission":
        print("Submission refused: campaign.github.mode is draft. Nothing was sent.", file=out)
        return 2
    meta, title, body = _draft(settings, key)
    try:
        approval = json.loads(Path(approval_path).expanduser().read_text())
    except (OSError, ValueError):
        raise ConfigError("cannot read the approval file") from None
    now = datetime.now(timezone.utc)
    expires, approved_at = parse_time(approval.get("expires_at")), parse_time(approval.get("approved_at"))
    if (not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS or approval["approved"] is not True
            or approval["finding_key"] != key or approval["repository"] != github["repository"]
            or approval["title_sha256"] != meta["title_sha256"] or approval["body_sha256"] != meta["body_sha256"]
            or not isinstance(approval["approved_by"], str) or not approval["approved_by"].strip()
            or approved_at is None or expires is None or not approved_at <= now < expires):
        print("Submission refused: the approval does not match this exact draft, destination and "
              "time window. Nothing was sent.", file=out)
        return 2
    repository = github["repository"]
    if repository is None:
        print("Submission refused: campaign.github.repository is not set. Nothing was sent.", file=out)
        return 2
    api = transport or GhTransport(github["gh_executable"])
    path = campaign_dir(settings) / "submissions.json"
    ledger = json.loads(path.read_text()) if path.exists() else {}
    record = ledger.get(key)
    if record and record.get("status") == "filed":
        print("Already filed: " + record["url"] + ". Nothing was sent.", file=out)
        return 0
    repo = api("repos/" + repository)
    if not isinstance(repo, dict) or str(repo.get("full_name", "")).lower() != repository.lower():
        print("Submission refused: destination identity mismatch. Nothing was created.", file=out)
        return 2
    existing = _marked_issue(api, repository, key)
    if existing is False:
        print("Submission refused: the issue listing is incomplete, so a duplicate cannot be "
              "excluded. Nothing was created.", file=out)
        return 2
    if existing is None and record:
        print("Submission refused: an earlier create has an unknown outcome and no marked issue "
              "is visible yet. Check the repository; no automatic retry.", file=out)
        return 3
    if existing is None:
        ledger[key] = {"status": "attempting", "at": iso(), "body_sha256": meta["body_sha256"],
                       "repository": repository, "approved_by": approval["approved_by"]}
        _write_private(path, json.dumps(ledger, indent=2))
        try:
            existing = api("repos/" + repository + "/issues", {"title": title, "body": body})
        except Exception:
            ledger[key]["status"] = "uncertain"
            _write_private(path, json.dumps(ledger, indent=2))
            print("Issue creation outcome unknown; recorded as uncertain. No automatic retry.", file=out)
            return 3
    url = existing.get("html_url") if isinstance(existing, dict) else None
    if not isinstance(url, str) or not url.startswith("https://github.com/" + repository + "/issues/"):
        ledger.setdefault(key, {})["status"] = "uncertain"
        _write_private(path, json.dumps(ledger, indent=2))
        print("Issue receipt does not identify an issue in the approved repository; uncertain.", file=out)
        return 3
    ledger[key] = dict(ledger.get(key, {}), status="filed", url=url, verified_at=iso())
    _write_private(path, json.dumps(ledger, indent=2))
    print("Filed: " + url, file=out)
    return 0


def _marked_issue(api, repository, key):
    """The one issue carrying the finding marker, None, or False when the listing is incomplete."""
    for page in range(1, 6):
        issues = api("repos/%s/issues?state=all&per_page=100&page=%d" % (repository, page))
        if not isinstance(issues, list):
            return False
        matches = [i for i in issues if isinstance(i, dict) and marker(key) in (i.get("body") or "")
                   and not i.get("pull_request")]
        if matches:
            return matches[0]
        if len(issues) < 100:
            return None
    return False
