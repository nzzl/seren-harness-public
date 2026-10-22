"""One bounded campaign: a fixed queue of existing cases run once each through `collect`.

The definition (campaigns/<id>.json) names the cases, target and limits. An owner-approved
authorization record binds that exact definition, the exact requests, a request budget and
a time window. Progress lives in a private state file, saved atomically before each request
(reservation) and after it (result), so a fresh process or agent context resumes from the
record: completed or uncertain cases are never repeated, budgets are never replenished, and
a different authorization cannot extend or reset the campaign. The generated handoff is a
summary for the next context, not authority. Nothing here publishes, schedules or retries.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys

from seren_harness import assertions, collect, selection
from seren_harness.guide import workflow_cases
from seren_harness.models import utc_now
from seren_harness.report import _write_private
from seren_harness.state import Store, private_directory

OK, STOPPED, NOTHING_ELIGIBLE, REFUSED, INVALID, PAUSED = 0, 2, 3, 4, 5, 6
# Per-case results. Only `pass` is a scoped success; uncertain results are never replayed.
RESULTS = ("pass", "supported_failure", "access_problem", "fixture_problem", "service_error",
           "inconclusive", "malformed_response", "uncertain_timeout", "uncertain_interrupted",
           "local_precheck_failed")
# Results that end the campaign: a failed local invariant, or rejected authentication.
STOPPING = {"local_precheck_failed": "a local pre-request check failed (checksum, version, "
                                     "command or target)",
            "auth_rejected": "the service rejected authentication"}


class CampaignError(Exception):
    """The campaign cannot run as recorded; nothing was requested."""


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _when(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def load(definition_path, root):
    """(definition, cases by id, target) after checking that every case belongs to this
    campaign, has a fixed request and a matching single-shape read-only collector."""
    definition = json.loads(Path(definition_path).read_text())
    if (not isinstance(definition.get("id"), str) or not isinstance(definition.get("cases"), list)
            or not definition["cases"] or len(set(definition["cases"])) != len(definition["cases"])
            or definition.get("attempts_per_case") != 1 or definition.get("retries") != 0
            or not isinstance(definition.get("max_service_requests"), int)
            or definition["max_service_requests"] > len(definition["cases"])):
        raise CampaignError("campaign definition needs unique cases, one attempt each, no "
                            "retries and at most one request per case")
    known = {c["id"]: c for _, c in workflow_cases(Path(root))}
    target = collect.load_target(Path(root) / definition["target"])
    cases = {}
    for case_id in definition["cases"]:
        case = known.get(case_id)
        collector = target["collectors"].get(case_id, {})
        if (case is None or case.get("campaign") != definition["id"]
                or not case.get("execution_args")
                or collector.get("args") != case["execution_args"]
                or not collect.read_only_args(case["execution_args"])
                or collector.get("timeout_seconds") != definition["request_timeout_seconds"]):
            raise CampaignError(case_id + " is not a fixed read-only request of this campaign")
        cases[case_id] = case
    return definition, cases, target


def check_authorization(auth, definition, definition_sha, cases, now=None):
    """Problems with an owner authorization record for this exact campaign, or []."""
    now = now or datetime.now(timezone.utc)
    problems = []
    requests = {k: c["execution_args"] for k, c in cases.items()}
    if auth.get("campaign") != definition["id"]:
        problems.append("authorization is for another campaign")
    if auth.get("definition_sha256") != definition_sha:
        problems.append("authorization does not match this campaign definition")
    if auth.get("requests") != requests:
        problems.append("authorized requests differ from the campaign's fixed requests")
    if (not isinstance(auth.get("max_service_requests"), int)
            or not 0 < auth["max_service_requests"] <= definition["max_service_requests"]):
        problems.append("authorized request budget is missing or exceeds the definition")
    if auth.get("retries") != 0 or auth.get("request_timeout_seconds") != \
            definition["request_timeout_seconds"]:
        problems.append("authorization must allow no retries at the definition's timeout")
    if not isinstance(auth.get("approved_by"), str) or not auth["approved_by"].strip():
        problems.append("authorization does not name who approved it")
    try:
        if not _when(auth["not_before"]) <= now < _when(auth["expires_at"]):
            problems.append("authorization is not currently valid (window " + auth["not_before"]
                            + " to " + auth["expires_at"] + ")")
    except (KeyError, ValueError, TypeError):
        problems.append("authorization needs a not_before/expires_at window")
    return problems


def check_catalog_authorization(auth, catalog, catalog_sha, queue=(), now=None):
    """Problems with an authorization to select from and run this exact catalog, or []."""
    now = now or datetime.now(timezone.utc)
    problems = []
    entries = {e["case"]: e["args"] for e in catalog["entries"]}
    candidates = auth.get("candidates")
    if auth.get("catalog") != catalog["id"] or auth.get("catalog_sha256") != catalog_sha:
        problems.append("authorization does not match this catalog")
    if auth.get("allow_subset_selection") is not True:
        problems.append("authorization does not allow selecting a subset")
    if (not isinstance(candidates, dict) or not candidates
            or any(entries.get(k) != v for k, v in candidates.items())):
        problems.append("authorized candidates are not exact requests of this catalog")
    elif any(k not in candidates for k in queue):
        problems.append("the selected queue is not covered by the authorized candidates")
    if (not isinstance(auth.get("max_service_requests"), int)
            or not 0 < auth["max_service_requests"] <= selection.LIMIT):
        problems.append("authorized request budget is missing or exceeds "
                        + str(selection.LIMIT))
    if auth.get("retries") != 0 or auth.get("request_timeout_seconds") != \
            catalog["request_timeout_seconds"]:
        problems.append("authorization must allow no retries at the catalog's timeout")
    if not isinstance(auth.get("approved_by"), str) or not auth["approved_by"].strip():
        problems.append("authorization does not name who approved it")
    try:
        if not _when(auth["not_before"]) <= now < _when(auth["expires_at"]):
            problems.append("authorization is not currently valid (window " + auth["not_before"]
                            + " to " + auth["expires_at"] + ")")
    except (KeyError, ValueError, TypeError):
        problems.append("authorization needs a not_before/expires_at window")
    return problems


def classify(record):
    """(result, answer class, report) from what collect recorded; never a pass without
    a recorded review whose workflow verdict passed."""
    error = record.get("collection_error")
    if error is not None:
        if not record.get("service_requests"):
            return "local_precheck_failed", None, None
        if "timed out" in error:
            return "uncertain_timeout", None, None
        if "not the documented JSON" in error:
            return "malformed_response", None, None
        return "inconclusive", None, None
    if record.get("review_error") or not record.get("folder"):
        return "inconclusive", None, None
    report = json.loads((Path(record["folder"]) / "report" / "report.json").read_text())
    answer = next((c["evidence"].get("answer_class") for c in report["criteria"]
                   if isinstance(c.get("evidence"), dict)
                   and c["evidence"].get("answer_class")), None)
    if report["verdict"] == "passed":
        return "pass", answer, report
    if report["verdict"] == "failed":
        return "supported_failure", answer, report
    return ({"auth_rejected": "access_problem", "forbidden": "access_problem",
             "deployment_missing": "fixture_problem",
             "service_error": "service_error"}.get(answer, "inconclusive"), answer, report)


class Campaign:
    def __init__(self, definition_path, home, root):
        self.definition_path = Path(definition_path)
        self.root = Path(root)
        raw = json.loads(self.definition_path.read_text())
        self.catalog = None
        if raw.get("kind") == "catalog":
            # A catalog campaign runs the queue selected once and recorded in its state.
            self.catalog, self.all_cases, self.target = selection.load_catalog(raw, root)
            self.definition = {"id": raw["id"], "version": raw.get("version"),
                               "target": raw["target"], "cases": [],
                               "request_timeout_seconds": raw["request_timeout_seconds"]}
            self.cases = {}
        else:
            self.definition, self.cases, self.target = load(definition_path, root)
        self.definition_sha = _sha256(definition_path)
        self.home = Path(home).expanduser()
        # Created only when a run starts, so `status` and `select` write nothing.
        self.directory = self.home / "campaigns" / self.definition["id"]
        self.state_path = self.directory / "state.json"
        self.state = (json.loads(self.state_path.read_text()) if self.state_path.exists()
                      else None)
        self.notes = []
        if self.catalog and self.state:
            self._bind(self.state["selection"]["queue"])

    def _bind(self, queue):
        missing = [k for k in queue if k not in self.all_cases]
        if missing:
            raise CampaignError("selected cases no longer exist: " + ", ".join(missing))
        self.definition["cases"] = list(queue)
        self.cases = {k: self.all_cases[k] for k in queue}

    def _auth_problems(self, auth, now):
        if self.catalog:
            return check_catalog_authorization(auth, self.catalog, self.definition_sha,
                                               self.definition["cases"], now)
        return check_authorization(auth, self.definition, self.definition_sha, self.cases, now)

    # Durable record ------------------------------------------------------------------
    def save(self):
        self.state["updated_at"] = utc_now()
        self.state["next_action"] = self.next_action()
        text = json.dumps(self.state, indent=2) + "\n"
        temporary = self.state_path.with_suffix(".tmp")
        _write_private(temporary, text)
        os.replace(temporary, self.state_path)  # atomic: a crash leaves old or new, never half
        # The compact handoff names the exact state it summarizes; a stale or edited handoff
        # therefore fails verification instead of standing in for the state.
        record = self.handoff_record(hashlib.sha256(text.encode()).hexdigest())
        _write_private(self.directory / "handoff.json", json.dumps(record, indent=2) + "\n")
        _write_private(self.directory / "handoff.md", self.handoff())
        _write_private(self.directory / "summary.json",
                       json.dumps(self.summary(), indent=2) + "\n")
        _write_private(self.directory / "summary.md", self.summary_markdown())

    def start(self, auth, auth_sha, auth_path, chosen=None):
        digests = {k: assertions.snapshot(c)["digest"] for k, c in self.cases.items()}
        if self.state is None:
            budget = auth["max_service_requests"]
            if chosen is not None:
                budget = min(budget, len(chosen["queue"]))
            self.state = {
                "campaign": self.definition["id"], "definition_version":
                self.definition.get("version"), "definition_sha256": self.definition_sha,
                "definition_path": str(self.definition_path),
                "authorization_sha256": auth_sha, "authorization_path": str(auth_path),
                "authorization": {k: auth.get(k) for k in (
                    "approved_by", "approved_at", "not_before", "expires_at",
                    "max_service_requests", "retries", "request_timeout_seconds", "profile")},
                "created_at": utc_now(), "invocations": [], "stopped": None,
                "budget": {"max_service_requests": budget,
                           "reserved": 0, "issued": 0, "issued_unknown": 0},
                "cases": {k: {"status": "pending", "version": self.cases[k]["version"],
                              "case_digest": digests[k],
                              "request": " ".join(self.target["collectors"][k]["args"])}
                          for k in self.definition["cases"]}}
            if chosen is not None:
                self.state["selection"] = chosen
            return
        if self.state["definition_sha256"] != self.definition_sha:
            raise CampaignError("the campaign definition changed after it started")
        if self.state["authorization_sha256"] != auth_sha:
            raise CampaignError("a different authorization cannot extend or reset this "
                                "campaign; its record stays as it is")
        changed = [k for k, d in digests.items() if self.state["cases"][k]["case_digest"] != d]
        if changed:
            raise CampaignError("case expectations changed after the campaign started: "
                                + ", ".join(changed))

    # Execution -----------------------------------------------------------------------
    def run(self, auth_path, out=None, execute=collect.run, now=datetime.now, max_cases=None):
        """`max_cases` ends this invocation at a safe case boundary (nothing in flight) with
        the campaign still open, so a later session can continue from the durable record."""
        out = out or sys.stdout
        auth_path = Path(auth_path).expanduser()
        auth = json.loads(auth_path.read_text())
        problems = self._auth_problems(auth, now(timezone.utc))
        if problems and self.state is None:
            raise CampaignError("; ".join(problems))
        private_directory(private_directory(private_directory(self.home) / "campaigns")
                          / self.definition["id"])
        lock = self.directory / "lock"
        self._lock(lock)
        try:
            return self._run_locked(auth, auth_path, out, execute, now, max_cases)
        finally:
            lock.unlink(missing_ok=True)

    def _run_locked(self, auth, auth_path, out, execute, now, max_cases=None):
        chosen = None
        if self.catalog and self.state is None:
            # Select once, from saved evidence only, and bind the queue before any request.
            chosen = selection.select(self.catalog, self.definition_sha, self.all_cases,
                                      self.target, self.home, auth["candidates"],
                                      auth["max_service_requests"])
            self._bind(chosen["queue"])
        self.start(auth, _sha256(auth_path), auth_path, chosen)
        self.state.setdefault("interruptions", []).extend(self.notes)
        if chosen is not None and not chosen["queue"]:
            self._stop("no eligible cases")
            self.save()
            print(selection.describe(chosen) + "\n\nNo eligible cases: nothing was requested.",
                  file=out)
            return NOTHING_ELIGIBLE
        invocation = {"started_at": utc_now(), "pid": os.getpid()}
        self.state["invocations"].append(invocation)
        try:
            for case_id, entry in self.state["cases"].items():
                if entry["status"] == "in_flight":
                    # The previous process stopped mid-request: the outcome is unknown, so
                    # the request is never replayed and its reservation stays consumed.
                    entry.update(status="done", result="uncertain_interrupted",
                                 finished_at=utc_now(), service_requests="unknown")
                    self.state["budget"]["issued_unknown"] += 1
            self.save()
            ran = 0
            while self.state["stopped"] is None:
                pending = [k for k, e in self.state["cases"].items() if e["status"] == "pending"]
                if not pending:
                    self._stop("complete")
                    break
                if max_cases is not None and ran >= max_cases:
                    invocation["outcome"] = "paused at a case boundary"
                    break
                if self._auth_problems(auth, now(timezone.utc)):
                    self._stop("authorization no longer valid; " + str(len(pending))
                               + " case(s) not run")
                    break
                budget = self.state["budget"]
                if budget["reserved"] >= budget["max_service_requests"]:
                    self._stop("request budget exhausted; " + str(len(pending))
                               + " case(s) not run")
                    break
                self._run_case(pending[0], execute)
                ran += 1
            if self.state["stopped"] is not None:
                invocation["outcome"] = self.state["stopped"]["reason"]
        finally:
            invocation["ended_at"] = utc_now()
            self.save()
        print(self.summary_markdown(), file=out)
        if self.state["stopped"] is None:
            return PAUSED
        return OK if self.state["stopped"]["reason"] == "complete" else STOPPED

    def _lock(self, lock):
        try:
            descriptor = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip())
                os.kill(pid, 0)
            except (ValueError, OSError):
                # The holder has exited: that was an interruption, recorded below (any
                # in-flight case becomes uncertain). The lock is never taken from a live one.
                self.notes.append(
                    {"stale_lock_pid": lock.read_text().strip(), "observed_at": utc_now()})
                lock.unlink()
                return self._lock(lock)
            raise CampaignError("another process (pid " + str(pid) + ") is running this "
                                "campaign") from None
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))

    def _run_case(self, case_id, execute):
        entry = self.state["cases"][case_id]
        entry.update(status="in_flight", attempt=1, reserved_at=utc_now())
        self.state["budget"]["reserved"] += 1
        self.save()  # the reservation is durable before any request can be issued
        before = self._finding_keys()
        record, buffer = {}, io.StringIO()
        args = argparse.Namespace(
            case=case_id, target=str(self.root / self.definition["target"]), target_root=None,
            from_copy=None, retest=None, home=str(self.home), state_dir=None,
            root=str(self.root), no_retry=True)
        execute(args, buffer, record)
        issued = record.get("service_requests", 0)
        self.state["budget"]["issued"] += issued
        result, answer, report = self._classify(record)
        after = self._finding_keys()
        findings = {k: ("known" if k in before else "new")
                    for k in ((report or {}).get("findings") or {})}
        entry.update(status="done", result=result, answer_class=answer,
                     service_requests=issued, finished_at=utc_now(),
                     folder=record.get("folder"), verdict=record.get("verdict"),
                     collection_error=record.get("collection_error"), findings=findings,
                     criteria={c["id"]: c["verdict"] for c in (report or {}).get("criteria", [])},
                     new_finding_keys=sorted(set(after) - set(before)))
        if result == "local_precheck_failed":
            self._stop(STOPPING[result])
        elif answer == "auth_rejected":
            self._stop(STOPPING["auth_rejected"])
        self.save()

    def _finding_keys(self):
        directory = self.home / "findings"
        if not (directory / "state.sqlite3").exists():
            return []
        store = Store(directory)
        try:
            return [f["key"] for f in store.findings()]
        finally:
            store.close()

    def _classify(self, record):
        return classify(record)

    def _stop(self, reason):
        self.state["stopped"] = {"reason": reason, "at": utc_now()}

    # Reporting -----------------------------------------------------------------------
    def next_action(self):
        state = self.state
        if state["stopped"] is not None:
            return ("None. The campaign is closed (" + state["stopped"]["reason"] + "). Any "
                    "further request needs a new scope and a new owner authorization.")
        pending = [k for k, e in state["cases"].items() if e["status"] == "pending"]
        return ("Continue within the recorded authorization: python -m seren_harness campaign "
                "run " + str(self.definition_path) + " --authorization "
                + state["authorization_path"] + " (pending: " + ", ".join(pending) + "). "
                "Completed and uncertain cases are never repeated.")

    def summary(self):
        cases = self.state["cases"]
        counts = {r: sum(1 for e in cases.values() if e.get("result") == r) for r in RESULTS}
        return {"campaign": self.state["campaign"],
                "definition_version": self.state["definition_version"],
                "complete": (self.state["stopped"] or {}).get("reason") == "complete",
                "stopped": self.state["stopped"], "budget": self.state["budget"],
                "cases_total": len(cases),
                "cases_attempted": sum(1 for e in cases.values() if e["status"] == "done"),
                "results": {r: n for r, n in counts.items() if n},
                "cases": cases, "authorization": self.state["authorization"],
                "invocations": self.state["invocations"],
                "selection": self.state.get("selection")}

    def summary_markdown(self):
        s = self.summary()
        lines = ["# Campaign " + s["campaign"] + " (definition v" + str(s["definition_version"])
                 + ")", "",
                 "Status: " + ("complete" if s["complete"] else
                               "stopped: " + s["stopped"]["reason"] if s["stopped"] else
                               "in progress"),
                 "Cases attempted: " + str(s["cases_attempted"]) + " of " + str(s["cases_total"])
                 + "; service requests issued " + str(s["budget"]["issued"])
                 + (" plus " + str(s["budget"]["issued_unknown"]) + " unknown"
                    if s["budget"]["issued_unknown"] else "")
                 + " of " + str(s["budget"]["max_service_requests"]) + " allowed for this "
                 "queue; retries 0.", "",
                 "| Case | v | Result | Answer | Verdict | Criteria | Findings | Evidence |",
                 "|---|---|---|---|---|---|---|---|"]
        for case_id, e in s["cases"].items():
            lines.append("| " + " | ".join([
                case_id, str(e["version"]), e.get("result", e["status"]),
                str(e.get("answer_class") or "-"), str(e.get("verdict") or "-"),
                ", ".join(k + " " + v for k, v in (e.get("criteria") or {}).items()) or "-",
                ", ".join(k + " (" + v + ")" for k, v in (e.get("findings") or {}).items())
                or "none", e.get("folder") or e.get("collection_error") or "-"]) + " |")
        lines += ["", "Results: " + (", ".join(k + " " + str(v) for k, v in s["results"].items())
                                     or "none yet") + ".",
                  "A pass is scoped to its case. Uncertain and inconclusive results are not "
                  "passes and are never replayed automatically. Findings and drafts are "
                  "private; nothing was published.", ""]
        if s["selection"]:
            lines += ["## Selection (made once, before any request)", "",
                      selection.describe(s["selection"]), ""]
        return "\n".join(lines)

    def sessions(self):
        """The session coordinator's record for this campaign, if one exists (read-only)."""
        path = self.directory / "sessions.json"
        try:
            return json.loads(path.read_text()) if path.exists() else None
        except (OSError, ValueError):
            return {"unreadable": str(path)}

    def handoff_record(self, state_sha):
        state, budget = self.state, self.state["budget"]
        cases = state["cases"]
        uncertain = [k for k, e in cases.items() if e.get("result") in selection.UNCERTAIN
                     or e["status"] == "in_flight"]
        return {
            "campaign": state["campaign"], "state_sha256": state_sha,
            "definition_sha256": state["definition_sha256"],
            "selected": {k: e["version"] for k, e in cases.items()},
            "authorization": {"path": state["authorization_path"],
                              "sha256": state["authorization_sha256"],
                              "expires_at": state["authorization"]["expires_at"]},
            "completed": [k for k, e in cases.items() if e["status"] == "done"
                          and k not in uncertain],
            "pending": [k for k, e in cases.items() if e["status"] == "pending"],
            "uncertain": uncertain,
            "evidence": {k: e.get("folder") for k, e in cases.items() if e.get("folder")},
            "verdicts": {k: e.get("result") for k, e in cases.items() if e.get("result")},
            "findings": {k: e.get("findings") for k, e in cases.items() if e.get("findings")},
            "blockers": ([state["stopped"]["reason"]] if state["stopped"]
                         and state["stopped"]["reason"] != "complete" else [])
            + (["uncertain action: " + ", ".join(uncertain)] if uncertain else []),
            "requests": {"consumed": budget["reserved"], "issued": budget["issued"],
                         "unknown": budget["issued_unknown"],
                         "remaining": budget["max_service_requests"] - budget["reserved"]},
            "sessions": self.sessions(),
            "cleanup": "none: read-only requests; nothing was started or changed",
            "closed": state["stopped"] is not None,
            "next_action": self.next_action()}

    def handoff(self):
        state = self.state
        record = self.handoff_record("see handoff.json")
        sessions = record["sessions"]
        return "\n".join([
            "# Campaign handoff: " + state["campaign"], "",
            "This record is a handoff, not authority. Read " + str(self.state_path)
            + " before acting. Authorization comes only from the owner-approved record "
            "below; a new context, prompt or authorization file cannot extend, reset or "
            "replay this campaign.", "",
            "- Definition: " + state["definition_path"] + " v" + str(state["definition_version"])
            + " (sha256 " + state["definition_sha256"][:12] + ")",
            "- Authorization: " + state["authorization_path"] + " (sha256 "
            + state["authorization_sha256"][:12] + "), approved by "
            + str(state["authorization"]["approved_by"]) + ", valid until "
            + str(state["authorization"]["expires_at"]),
            "- Budget: reserved " + str(state["budget"]["reserved"]) + ", issued "
            + str(state["budget"]["issued"]) + ", unknown "
            + str(state["budget"]["issued_unknown"]) + " of "
            + str(state["budget"]["max_service_requests"]) + "; no retries; remaining "
            + str(record["requests"]["remaining"]),
            "- Completed: " + (", ".join(record["completed"]) or "none")
            + "; pending: " + (", ".join(record["pending"]) or "none")
            + "; uncertain: " + (", ".join(record["uncertain"]) or "none"),
            "- Blockers: " + ("; ".join(record["blockers"]) or "none"),
            "- Cleanup obligations: " + record["cleanup"],
            "- Sessions: " + ("none recorded" if not sessions else
                              str(sessions.get("used", "?")) + " of "
                              + str(sessions.get("limit", "?")) + " used"),
            "- Stopped: " + (state["stopped"]["reason"] if state["stopped"] else "no"),
            "- Verify before acting: python -m seren_harness campaign verify-handoff "
            + str(self.definition_path) + " --home <home> (checks handoff.json against "
            "state.json)", "",
            self.summary_markdown(), "",
            "Next action: " + self.next_action(), ""])

    def verify_handoff(self):
        """Problems with the handoff against the durable state, or []. Never repairs."""
        try:
            text = self.state_path.read_text()
            state = json.loads(text)
            record = json.loads((self.directory / "handoff.json").read_text())
        except (OSError, ValueError):
            return ["state or handoff is missing or unreadable"]
        problems = []
        if record.get("state_sha256") != hashlib.sha256(text.encode()).hexdigest():
            problems.append("the handoff does not describe the current state")
        if (record.get("campaign") != state.get("campaign")
                or record.get("definition_sha256") != self.definition_sha
                or state.get("definition_sha256") != self.definition_sha):
            problems.append("the handoff, state and definition disagree")
        if record.get("authorization", {}).get("sha256") != state.get("authorization_sha256"):
            problems.append("the handoff names a different authorization")
        if set(record.get("selected", {})) != set(state.get("cases", {})):
            problems.append("the handoff names a different selection")
        return problems


def run(args, out=None):
    out = out or sys.stdout
    if args.action == "sessions":
        from seren_harness import sessions
        return sessions.run(args, out)
    try:
        campaign = Campaign(args.definition, args.home, args.root)
        if args.action == "status":
            if campaign.state is None:
                print("Campaign " + campaign.definition["id"] + " has not started.", file=out)
            else:
                print((campaign.directory / "handoff.md").read_text(), file=out)
            return OK
        if args.action == "verify-handoff":
            if campaign.state is None:
                raise CampaignError("there is no campaign state to verify")
            problems = campaign.verify_handoff()
            if problems:
                raise CampaignError("handoff not verified: " + "; ".join(problems))
            record = json.loads((campaign.directory / "handoff.json").read_text())
            print("handoff verified against state (sha256 " + record["state_sha256"][:12]
                  + ").\nPending: " + (", ".join(record["pending"]) or "none")
                  + "; uncertain: " + (", ".join(record["uncertain"]) or "none")
                  + "; requests remaining: " + str(record["requests"]["remaining"])
                  + ".\nNext action: " + record["next_action"], file=out)
            return OK
        if args.action == "select":
            # A preview from saved evidence only: nothing is written, bound or requested.
            if campaign.catalog is None:
                raise CampaignError("selection needs a catalog")
            if campaign.state is not None:
                print("Selection already bound for this catalog (not repeated):\n\n"
                      + selection.describe(campaign.state["selection"]), file=out)
                return OK
            chosen = selection.select(campaign.catalog, campaign.definition_sha,
                                      campaign.all_cases, campaign.target, campaign.home)
            print(selection.describe(chosen) + "\n\nPreview only: no authorization is bound "
                  "and nothing was requested.", file=out)
            return OK if chosen["queue"] else NOTHING_ELIGIBLE
        if not args.authorization:
            raise CampaignError("campaign run needs --authorization")
        return campaign.run(args.authorization, out,
                            max_cases=getattr(args, "max_cases", None))
    except CampaignError as error:
        print("Campaign refused: " + str(error) + ". Nothing was requested.", file=out)
        return REFUSED
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Campaign cannot start: " + type(error).__name__ + ".", file=out)
        return INVALID
