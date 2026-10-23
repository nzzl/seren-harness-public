"""Persistent, unattended offline bug discovery (the default operating path).

Roles, all local and user-owned:
- supervisor: `discovery supervise`, one short tick started by a per-user launchd agent.
  It never runs experiments. It reconciles the previous worker, enforces limits and the
  owner's stop switch, and launches at most one worker when authorized work exists.
- worker: `discovery work`, one bounded session. It proves isolation, then runs pending
  cases from the catalog (tier0_cases) with attempt records written before execution,
  classifies results, deduplicates findings and checkpoints after every case.
- owner: `discovery status`, `discovery stop`, `discovery resume`.

Durable state lives in a private directory (default ~/seren-harness-live/discovery):
authority.json (the owner's standing scope; never written by the loop after init),
state.json, attempts/, evidence/, findings/, log/, STOP. See docs/ADVANCED.md.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

from seren_harness import model_worker, tier0

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = "~/seren-harness-live/discovery"
TEMPLATE = ROOT / "docs" / "discovery" / "standing-authority.json"
LABEL = "local.seren-harness.discovery"
FINDING_DISPOSITIONS = ("reproduced_client_defect", "contract_question", "capability_gap",
                        "observed_behavior")
LIMIT_KEYS = ("worker_wall_seconds", "worker_max_cases", "worker_stall_seconds", "repeats",
              "max_attempts_per_case", "worker_log_bytes", "max_sessions_per_day",
              "backoff_seconds", "tick_seconds")
PROHIBITED = ("live_seren_calls", "real_employee_tasks", "production_credentials",
              "real_oauth_refresh", "publication", "push", "messages_to_others")


class ScopeError(RuntimeError):
    pass


def _catalog():
    """The case catalog is model-worker-authored code. Only sandboxed processes import it
    (workers, the `due` helper, run-case); the supervisor process never does."""
    from seren_harness import tier0_cases
    return tier0_cases


def now():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or now()).isoformat(timespec="seconds")


def parse(value):
    return datetime.fromisoformat(value) if value else None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


class Home:
    """The private discovery directory and its locks."""

    def __init__(self, root=None):
        self.root = Path(os.path.expanduser(str(root or DEFAULT_HOME)))
        for sub in ("", "attempts", "evidence", "findings", "log"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
            os.chmod(self.root / sub, 0o700)
        self.state_path = self.root / "state.json"
        self.stop_path = self.root / "STOP"

    @contextmanager
    def lock(self, name, blocking=True):
        fd = os.open(self.root / (name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(fd)

    def worker_running(self):
        """True while a worker holds the work lock (a pid alone could be reused)."""
        with self.lock("work", blocking=False) as free:
            return not free

    def state(self):
        return read_json(self.state_path, {"version": 1, "cases": {}, "findings": {},
                                           "sessions": [], "events": [], "wait": None,
                                           "blockers": {}})

    @contextmanager
    def edit(self):
        with self.lock("state"):
            state = self.state()
            yield state
            write_json(self.state_path, state)

    def event(self, state, text):
        """Only state changes are logged, so an idle loop stays quiet."""
        line = iso() + " " + text
        state["events"] = (state.get("events", []) + [line])[-80:]
        with open(self.root / "log" / "events.log", "a") as stream:
            stream.write(line + "\n")

    def authority(self):
        value = read_json(self.root / "authority.json")
        if value is None:
            raise ScopeError("no standing authority recorded; run `discovery init`")
        validate_authority(value)
        return value


def validate_authority(value):
    if value.get("live") is not False or set(value.get("allowed_kinds", [])) - {"tier0-offline"}:
        raise ScopeError("authority grants something this loop cannot do offline")
    if not set(PROHIBITED) <= set(value.get("prohibited", [])):
        raise ScopeError("authority omits a required prohibition")
    if set(LIMIT_KEYS) - set(value.get("limits", {})):
        raise ScopeError("authority is missing a limit")
    if not value.get("executable"):
        raise ScopeError("authority names no pinned executable")


# ---------------------------------------------------------------- catalog

def _scenario_shape(scenario):
    auth = scenario.auth
    if isinstance(auth, dict):  # saved sign-in: volatile expiry excluded from identity
        auth = {k: v for k, v in auth.items() if k != "expires_at"}
    return {"name": scenario.name, "role": scenario.role, "args": scenario.args,
            "routes": {"%s %s" % k: v for k, v in scenario.routes.items()}, "auth": auth,
            "env": scenario.extra_env, "deadline": scenario.deadline, "files": scenario.files}


def case_digest(case_id, catalog=None):
    """What a result depends on: scenario shapes, fact/judge code, lab code, binary pin.
    A changed digest makes a completed case due again (re-verification, not repetition)."""
    cases = _catalog()
    case = (catalog or cases.CASES)[case_id]
    parts = {"case": case_id, "kind": case["kind"], "pin": tier0.PINNED_SHA256,
             "lab": [inspect.getsource(tier0), inspect.getsource(cases.execute)] +
                    [inspect.getsource(f) for f in cases.SHARED],
             "code": [inspect.getsource(case[k]) for k in ("scenarios", "facts", "judge")],
             "scenarios": [_scenario_shape(s) for s in case["scenarios"]()]}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def pending(state, authority, catalog=None, order=None):
    """Due cases in catalog order. Out-of-scope kinds are refused, never queued."""
    catalog = catalog or _catalog().CASES
    due, refused = [], []
    for case_id in order or _catalog().ORDER:
        case = catalog[case_id]
        if case["kind"] not in authority["allowed_kinds"]:
            refused.append(case_id)
            continue
        entry = state["cases"].get(case_id, {})
        if entry.get("status") == "deferred" and entry.get("digest") == case_digest(case_id,
                                                                                   catalog):
            continue
        if entry.get("status") == "done" and entry.get("digest") == case_digest(case_id,
                                                                               catalog):
            continue
        due.append(case_id)
    return due, refused


# ---------------------------------------------------------------- findings

def record_findings(home, state, attempt, results):
    changes = []
    for item in results:
        key = "T0-%s-%s" % (item["case"], item["part"])
        known = state["findings"].get(key)
        if item["disposition"] not in FINDING_DISPOSITIONS and not known:
            continue
        snapshot = ({k: known.get(k) for k in ("disposition", "summary", "grade", "basis",
                                                "impact", "question")}
                    if known else None)
        if known and known["disposition"] == item["disposition"]:
            known["occurrences"].append({"attempt": attempt, "at": iso()})
            known["occurrences"] = known["occurrences"][-20:]
            revised = {k: item.get(k) for k in ("summary", "grade", "basis", "impact",
                                                "question")}
            if any(revised[k] != known.get(k) for k in revised):
                # Same classification, new evidence or wording: revise, keep history.
                known.setdefault("history", []).append(dict(snapshot, until=iso(),
                                                            attempt=attempt))
                known.update(revised)
                write_draft(home, known)
                changes.append("%s revised (still %s)" % (key, item["disposition"]))
            continue
        entry = {"key": key, "case": item["case"], "part": item["part"],
                 "disposition": item["disposition"], "summary": item["summary"],
                 "grade": item.get("grade"), "basis": item.get("basis", []),
                 "impact": item.get("impact"), "question": item.get("question"),
                 "first_seen": known["first_seen"] if known else iso(),
                 "previous": known["disposition"] if known else None,
                 "history": (known.get("history", []) + [dict(snapshot, until=iso(),
                                                              attempt=attempt)])
                 if known else [],
                 "occurrences": [{"attempt": attempt, "at": iso()}]}
        state["findings"][key] = entry
        write_draft(home, entry)
        changes.append("%s %s%s" % (key, item["disposition"],
                                    " (was %s)" % known["disposition"] if known else ""))
    return changes


def write_draft(home, entry):
    """Sanitized local draft; never published. Synthetic identifiers only."""
    kind = {"reproduced_client_defect": "Client defect (reproduced against a local fake API)",
            "capability_gap": "Capability gap", "contract_question": "Contract question",
            "observed_behavior": "Observed behaviour (change detector)"}.get(
        entry["disposition"], "Status change: " + entry["disposition"])
    lines = ["# %s: %s" % (entry["key"], kind), "",
             "Seren CLI 0.10.0 (commit 2317949, sha256 %s...)." % tier0.PINNED_SHA256[:12],
             "Status: DRAFT, not published. Not to be shared with Seren unless the owner "
             "explicitly instructs and approves.", "", "## Observed", "", entry["summary"], "",
             "## Basis (%s)" % entry.get("grade"), ""]
    lines += ["- " + b for b in entry.get("basis") or []]
    if entry.get("impact"):
        lines += ["", "## Possible customer impact", "", entry["impact"]]
    if entry.get("question"):
        lines += ["", "## Question for maintainers", "", entry["question"]]
    lines += ["", "## Limits", "", "Established against a loopback fake API with synthetic "
              "data: client behaviour only, not production server behaviour."]
    path = home.root / "findings" / (entry["key"] + ".md")
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- worker

def _heartbeat(home, session, **fields):
    record = read_json(home.root / "worker.json", {})
    if record.get("session") == session:
        record.update(fields, heartbeat_at=iso())
        write_json(home.root / "worker.json", record)


def run_case(home, session, lab, case_id, authority, execute=None):
    """Attempt identity is durable before the first scenario runs."""
    execute = execute or _catalog().execute
    attempt = str(uuid.uuid4())
    folder = home.root / "evidence" / case_id / attempt
    folder.mkdir(parents=True)
    record = {"id": attempt, "case": case_id, "session": session, "status": "started",
              "started_at": iso(), "digest": case_digest(case_id)}
    write_json(home.root / "attempts" / (attempt + ".json"), record)
    with home.edit() as state:
        entry = state["cases"].setdefault(case_id, {"attempts": [], "failures": 0})
        entry["attempts"] = (entry["attempts"] + [attempt])[-20:]
        entry["status"] = "running"
        home.event(state, "attempt %s started for %s" % (attempt[:8], case_id))

    def observed(item):
        with open(folder / "observations.jsonl", "a") as stream:
            stream.write(json.dumps(item, default=str) + "\n")
        _heartbeat(home, session, current={"case": case_id, "scenario": item["scenario"],
                                           "repeat": item["repeat"]})

    try:
        outcome = execute(lab, case_id, authority["limits"]["repeats"], observed)
    except Exception:  # noqa: BLE001 - a harness failure is recorded, never a pass
        (folder / "error.txt").write_text(traceback.format_exc())
        return finish(home, record, "failed", [], authority)
    write_json(folder / "result.json", {"results": outcome["results"],
                                        "facts": outcome["facts"]})
    bad = [r for r in outcome["results"] if r["disposition"] in ("blocked", "inconclusive")]
    return finish(home, record, "inconclusive" if bad else "completed", outcome["results"],
                  authority)


def finish(home, record, status, results, authority):
    record.update(status=status, finished_at=iso())
    write_json(home.root / "attempts" / (record["id"] + ".json"), record)
    with home.edit() as state:
        entry = state["cases"][record["case"]]
        entry["last_attempt"] = record["id"]
        if status == "completed":
            entry.update(status="done", digest=record["digest"], failures=0,
                         results=[{k: r[k] for k in ("part", "disposition", "summary")}
                                  for r in results], completed_at=iso())
            changes = record_findings(home, state, record["id"], results)
            home.event(state, "%s completed: %s" % (record["case"], "; ".join(
                "%s=%s" % (r["part"], r["disposition"]) for r in results)))
            for change in changes:
                home.event(state, "finding " + change)
        else:
            entry["failures"] = entry.get("failures", 0) + 1
            entry["status"] = "pending"
            if entry["failures"] >= authority["limits"]["max_attempts_per_case"]:
                entry.update(status="deferred", digest=record["digest"],
                             deferred_reason="%d unsuccessful attempts; last %s, evidence "
                             "kept under evidence/%s/%s" % (entry["failures"], status,
                                                           record["case"], record["id"]))
            home.event(state, "%s attempt %s %s (%d/%d)%s" % (
                record["case"], record["id"][:8], status, entry["failures"],
                authority["limits"]["max_attempts_per_case"],
                "; deferred" if entry["status"] == "deferred" else ""))
    return status


def recover(home, state, alive_sessions=()):
    """Mark attempts left 'started' by a dead session as interrupted. Evidence is kept;
    the case stays due, and the interruption counts toward its attempt limit."""
    authority = home.authority()
    for path in sorted((home.root / "attempts").glob("*.json")):
        record = read_json(path)
        if record.get("status") != "started" or record.get("session") in alive_sessions:
            continue
        record.update(status="interrupted", finished_at=iso())
        write_json(path, record)
        entry = state["cases"].setdefault(record["case"], {"attempts": [], "failures": 0})
        entry["failures"] = entry.get("failures", 0) + 1
        entry["status"] = "pending"
        if entry["failures"] >= authority["limits"]["max_attempts_per_case"]:
            entry.update(status="deferred", digest=record["digest"],
                         deferred_reason="interrupted repeatedly; evidence kept")
        home.event(state, "attempt %s for %s recovered as interrupted (%d/%d); evidence "
                   "kept, retry decided by the attempt limit" % (
                       record["id"][:8], record["case"], entry["failures"],
                       authority["limits"]["max_attempts_per_case"]))


def work(home, session, deadline_at=None, execute=None, make_lab=None, check=None):
    """One worker session. Returns its outcome string."""
    authority = home.authority()
    limits = authority["limits"]
    deadline_at = deadline_at or (time.time() + limits["worker_wall_seconds"])
    stop = {"flag": False}

    def terminate(*_):
        stop["flag"] = True
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    with home.lock("work", blocking=False) as mine:
        if not mine:
            return "busy"
        workdir = Path(tempfile.mkdtemp(prefix="seren-tier0-"))
        try:
            client = None if make_lab else tier0.lab_from_environment()
            if client:  # sandboxed worker: the trusted host owns the lab and its preflight
                lab, isolation = client, client.isolation()
            else:
                lab = (make_lab or tier0.Lab)(authority["executable"], workdir)
                isolation = (check or tier0.preflight)(lab, home.root)
            write_json(home.root / "evidence" / ("isolation-%s.json" % session), isolation)
            done = 0
            while not home.stop_path.exists():
                if done >= limits["worker_max_cases"]:
                    return "turnover:case_limit"
                if time.time() + 120 > deadline_at:
                    return "turnover:wall_limit"
                with home.edit() as state:
                    due, _ = pending(state, authority)
                if not due:
                    return "idle"
                _heartbeat(home, session, current={"case": due[0]})
                run_case(home, session, lab, due[0], authority, execute)
                done += 1
            return "stopped"
        except tier0.IsolationError as error:
            with home.edit() as state:
                state["blockers"]["isolation"] = "%s (%s)" % (error, iso())
                home.event(state, "isolation check failed: %s; no scenario ran" % error)
            return "blocked:isolation"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------- supervisor

def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, TypeError):
        return False


def _terminate(pid, grace=5):
    """End a worker's whole process group (its own session, created at launch)."""
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpgrp():
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.time() + grace
    while time.time() < end and _alive(pid):
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def default_worker_argv(home, session):
    return [sys.executable, "-m", "seren_harness", "discovery", "work", "--home",
            str(home.root), "--session", session]


def _limits(authority, record):
    """(log path, log byte cap, stall seconds) for this worker kind."""
    if record.get("kind") == "model":
        limits = authority["model_worker"]["limits"]
        return ("model-%s.jsonl" % record["session"], limits["log_bytes"],
                limits["stall_seconds"])
    limits = authority["limits"]
    return ("worker-%s.log" % record["session"], limits["worker_log_bytes"],
            limits["worker_stall_seconds"])


def _reconcile_worker(home, state, authority):
    """Returns True while a healthy worker is still running."""
    record = read_json(home.root / "worker.json")
    if not record or record.get("status") not in ("launching", "running"):
        return False
    name, log_cap, stall = _limits(authority, record)
    log = home.root / "log" / name
    running = home.worker_running() or (_alive(record.get("pid")) and
                                        record.get("status") == "launching")
    reason = None
    if running:
        beat = parse(record.get("heartbeat_at") or record["started_at"])
        if home.stop_path.exists():
            reason = "stopped_by_owner"
        elif time.time() > record["deadline_epoch"]:
            reason = "limit:wall_time"
        elif log.exists() and log.stat().st_size > log_cap:
            reason = "limit:output"
        elif (now() - beat).total_seconds() > stall:
            reason = "stalled"
        if reason is None:
            return True
        _terminate(record["pid"])
    outcome = reason or _exit_outcome(home, record)
    if record.get("kind") == "model" and outcome == "model:success":
        # A failed checked commit must not disappear behind a successful session exit.
        commits = [c for c in state.get("model", {}).get("commits", [])
                   if c.get("session") == record["session"]]
        if commits and commits[-1].get("exit") != 0:
            outcome = "failed:commit:%s" % (commits[-1].get("cause") or "unknown")
    record.update(status="ended", ended_at=iso(), outcome=outcome)
    write_json(home.root / "worker.json", record)
    recover(home, state)
    for session in state["sessions"]:
        if session["id"] == record["session"]:
            session.update(ended_at=record["ended_at"], outcome=outcome)
    if record.get("kind") == "model":
        _model_ended(home, state, authority, record, outcome)
    else:
        failed = outcome.startswith(("failed", "stalled", "blocked"))
        state["consecutive_failures"] = (state.get("consecutive_failures", 0) + 1
                                         if failed else 0)
    home.event(state, "%s worker %s ended: %s" % (record.get("kind", "deterministic"),
                                                  record["session"][:8], outcome))
    return False


def _model_ended(home, state, authority, record, outcome):
    model = state.setdefault("model", {})
    summary = read_json(home.root / "log" / ("model-%s.summary.json" % record["session"]),
                        {})
    if "dirty_after" not in summary:  # wrapper killed: attribute from its start record
        start = read_json(home.root / "log" / ("model-%s.start.json" % record["session"]))
        if start is not None:
            from seren_harness import maintenance
            summary["dirty_after"] = model_worker.claimable(
                authority["model_worker"]["repo"], start,
                maintenance.owned if record.get("mode") == "maintenance" else model_worker.owned)
            home.event(state, "recovered model worker %s leftovers by content hash: %s" % (
                record["session"][:8], sorted(summary["dirty_after"]) or "none"))
    model["owned_dirty"] = summary.get("dirty_after", model.get("owned_dirty") or {})
    sessions = model.setdefault("sessions", [])
    sessions.append({"session": record["session"], "outcome": outcome,
                     "started_at": record["started_at"], "ended_at": record["ended_at"],
                     "init": summary.get("init"), "result": summary.get("result")})
    model["sessions"] = sessions[-100:]
    limits = authority["model_worker"]["limits"]
    if outcome == "rate_limited":
        model["backoff_until"] = iso(now() + timedelta(
            seconds=limits["rate_limit_backoff_seconds"]))
    elif outcome == "blocked:not_subscription":
        state["blockers"]["model_auth"] = ("a model worker did not report the subscription "
                                           "login (apiKeySource none); model launches stop "
                                           "until the owner clears this blocker")
    elif outcome.startswith(("failed", "stalled", "limit")):
        model["failures"] = model.get("failures", 0) + 1
        backoff = limits["backoff_seconds"]
        model["backoff_until"] = iso(now() + timedelta(
            seconds=backoff[min(model["failures"], len(backoff)) - 1]))
    else:
        model["failures"] = 0


def _exit_outcome(home, record):
    name = ("model-%s.summary.json" if record.get("kind") == "model"
            else "worker-%s.result.json") % record["session"]
    result = read_json(home.root / "log" / name)
    if result:
        return result["outcome"]
    return "failed:no_result (exit unknown; see worker log)"


WAKE_ON = ("a committed case is added or its scenarios, facts/judge code, the lab code or "
           "the pinned executable digest change", "new actionable model-worker evidence or owner "
           "direction, or an explicitly timed wait expires", "the stop switch is cleared")


def due_inprocess(home, state, authority):
    due, refused = pending(state, authority)
    cases = _catalog()
    return {"due": due, "refused": refused,
            "catalog": [[c, cases.CASES[c]["title"], cases.CASES[c]["kind"]]
                        for c in cases.ORDER]}


def due_sandboxed(home, state, authority):
    """Catalog questions are answered by a sandboxed child: the catalog is worker code."""
    argv = model_worker.sandboxed(home.root, ROOT, [
        sys.executable, "-m", "seren_harness", "discovery", "due", "--home", str(home.root)])
    out = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=120,
                         env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""),
                              "LANG": "C", "PYTHONPATH": str(ROOT)})
    if out.returncode != 0:
        raise ScopeError("catalog could not be read: " + out.stderr.strip()[-300:])
    return json.loads(out.stdout)


def _head():
    head = os.environ.get("SEREN_DISCOVERY_HEAD")
    if head:
        return head
    out = subprocess.run(["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip() or None


def model_wake_signals(home, state, authority):
    """Local facts only: commits and generic checkpoint updates are not work signals.

    Values are fingerprints, so unchanged gated items cannot consume model sessions.
    Removing work does not wake a worker; only a new or changed current signal does.
    """
    signals = {}

    def add(key, value):
        signals[key] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    checkpoint = read_json(home.root / "investigation-checkpoint.json", {})
    if checkpoint.get("owner_direction"):
        add("owner_direction", checkpoint["owner_direction"])
    owner_notes = [n["text"] for n in state.get("model", {}).get("notes", [])
                   if n.get("session") == "owner-direction"]
    if owner_notes:
        add("owner_note", owner_notes[-1])
    for key, item in state.get("followups", {}).items():
        if model_worker.followup_eligible(item):
            add("followup:" + key, {k: item.get(k) for k in
                                  (*model_worker.FOLLOWUP_FIELDS, "status", "needs")})
    for key, item in state.get("model", {}).get("candidates", {}).items():
        if item.get("status") in ("proposed", "implemented"):
            add("candidate:" + key, {k: item.get(k) for k in ("status", "rationale", "basis")})
    for key, item in state.get("cases", {}).items():
        if item.get("status") in ("done", "deferred"):
            add("result:" + key, {k: item.get(k) for k in
                                ("status", "last_attempt", "results", "deferred_reason")})
    executable = Path(authority["executable"])
    if executable.exists():
        info = executable.stat()
        add("pinned_cli", [str(executable), info.st_size, info.st_mtime_ns])
    live = read_json(home.root / "live-authority.json", {})
    if live.get("approved") is True:
        add("live_authority", {k: live.get(k) for k in
                              ("target", "runs_per_day", "calls_per_day", "timezone",
                               "no_monetary_cap", "session")})
    handoff = read_json(home.root / "claude-live-request.json", {})
    if handoff:
        add("live_handoff", handoff)
    selected = read_json(home.root / "maintenance-goal.json", None)
    if selected:
        add("maintenance_selection", selected)
    return signals


def _today(state, kind):
    today = now().date().isoformat()
    return sum(1 for s in state["sessions"] if s["started_at"][:10] == today
               and s.get("kind", "deterministic") == kind)


def _record_launch(home, state, kind, deadline, extra):
    session = str(uuid.uuid4())
    record = dict({"session": session, "kind": kind, "status": "launching",
                   "started_at": iso(), "deadline_epoch": deadline}, **extra)
    write_json(home.root / "worker.json", record)  # identity before launch
    entry = {"id": session, "kind": kind, "started_at": record["started_at"]}
    if extra.get("mode"):  # counted like any model session, visibly identified
        entry["mode"] = extra["mode"]
    state["sessions"] = (state["sessions"] + [entry])[-300:]
    return session, record


def _set_wait(home, state, reason):
    if state.get("wait_reason") != reason:
        state["wait_reason"] = reason
        home.event(state, "durable wait: " + reason)
    state["wait"] = {"since": state.get("wait", {}) and state["wait"].get("since") or iso(),
                     "reason": reason, "wake_on": list(WAKE_ON)}
    state["next_action"] = "durable wait: %s (wakes when %s)" % (reason, "; ".join(WAKE_ON))
    return "waiting"


def supervise(home, launch=None, due=None, model_launch=None, repo_dirty=None):
    """One tick. Returns a short status word; quiet unless something changed."""
    with home.lock("supervisor", blocking=False) as mine, home.lock("live-handoff", False) as handoff:
        if not mine or not handoff:
            return "busy"
        try:
            authority = home.authority()
        except ScopeError as error:
            with home.edit() as state:
                if state["blockers"].get("authority") != str(error):
                    state["blockers"]["authority"] = str(error)
                    home.event(state, "blocked: " + str(error))
            return "blocked"
        with home.edit() as state:
            state["blockers"].pop("authority", None)
            state["last_tick"] = iso()
            if _reconcile_worker(home, state, authority):
                return "worker_running"
            if home.stop_path.exists():
                state["next_action"] = "stop switch on: nothing launches until `discovery resume`"
                return "stopped"
            try:
                from seren_harness import bug_notifications
                bug_notifications.deliver(home, state)
            except (OSError, ValueError, KeyError) as error:
                state["bug_notification_status"] = {"status": "blocked", "error": type(error).__name__}
            try:
                info = (due or due_inprocess)(home, state, authority)
            except (ScopeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                if state["blockers"].get("catalog") != str(error)[:300]:
                    state["blockers"]["catalog"] = str(error)[:300]
                    home.event(state, "catalog unreadable; nothing launched: %s" % error)
                return "blocked"
            state["blockers"].pop("catalog", None)
            if info["due"]:
                return _deterministic(home, state, authority, info["due"], launch)
            word = _model(home, state, authority, model_launch,
                          repo_dirty or model_worker.dirty_hashes)
            if word:
                return word
            return _set_wait(home, state, "no deterministic work is due and no model worker "
                             "is eligible (%s)" % state.get("model_status", "not enabled"))


def _deterministic(home, state, authority, due, launch):
    state["wait"], state["wait_reason"] = None, None
    failures = state.get("consecutive_failures", 0)
    if failures:
        backoff = authority["limits"]["backoff_seconds"]
        delay = backoff[min(failures, len(backoff)) - 1]
        ended = [parse(s["ended_at"]) for s in state["sessions"]
                 if s.get("kind", "deterministic") == "deterministic" and s.get("ended_at")]
        if ended and (now() - ended[-1]).total_seconds() < delay:
            state["next_action"] = "retry after backoff at %s" % iso(
                ended[-1] + timedelta(seconds=delay))
            return "backoff"
    if _today(state, "deterministic") >= authority["limits"]["max_sessions_per_day"]:
        state["next_action"] = "daily session limit reached; next session tomorrow"
        return "limited"
    session, record = _record_launch(
        home, state, "deterministic",
        time.time() + authority["limits"]["worker_wall_seconds"], {"due": due})
    pid = (launch or _launch)(home, session)
    record.update(pid=pid, status="running")
    write_json(home.root / "worker.json", record)
    state["next_action"] = "worker %s running %s" % (session[:8], due[0])
    home.event(state, "launched worker %s (pid %s) for %s" % (session[:8], pid, ", ".join(due)))
    return "launched"


def _model(home, state, authority, model_launch, repo_dirty):
    """Launch a model worker when the deterministic queue is empty, or return None and
    leave the reason in state["model_status"]."""
    try:
        cfg = model_worker.config(authority)
    except model_worker.ConfigError as error:
        state["model_status"] = str(error)
        return None
    if not cfg:
        state["model_status"] = "model workers not enabled in the standing authority"
        return None
    model = state.setdefault("model", {})
    if state["blockers"].get("model_auth"):
        state["model_status"] = "blocked: " + state["blockers"]["model_auth"]
        return None
    wait = model.get("wait")
    if wait:
        current = model_wake_signals(home, state, authority)
        # Upgrade an old HEAD-based wait without spending a session on migration.
        baseline = wait.setdefault("signals", current)
        changed = sorted(k for k, value in current.items() if baseline.get(k) != value)
        timed = wait.get("wake_on") == "time" and now() >= parse(wait["until"])
        if not changed and not timed:
            state["model_status"] = "a model worker asked to wait for actionable work: %s" % (
                wait["reason"])
            return None
        state["model_wake_reason"] = ("changed: " + ", ".join(changed) if changed
                                      else "explicit timed wait expired: " + wait["reason"])
    until = parse(model.get("backoff_until"))
    if until and now() < until:
        state["model_status"] = "model worker backoff until %s" % model["backoff_until"]
        state["next_action"] = state["model_status"]
        return "backoff"
    from seren_harness import maintenance
    if maintenance.verb_running(home):
        state["model_status"] = "an ended session's maintenance check or commit is still running"
        state["next_action"] = state["model_status"]
        return None
    if _today(state, "model") >= cfg["limits"]["sessions_per_day"]:
        state["model_status"] = "daily model-session limit reached"
        state["next_action"] = state["model_status"] + "; next session tomorrow"
        return "limited"
    # An owner-selected maintenance goal replaces discovery sessions; an invalid or finished
    # selection launches nothing (it never silently falls back to discovery).
    goal, problem = maintenance.selection(home, cfg["repo"])
    if problem:
        state["model_status"] = problem
        state["next_action"] = problem
        return None
    mine = maintenance.owned if goal else model_worker.owned
    foreign = model_worker.foreign_changes(repo_dirty(cfg["repo"]), model.get("owned_dirty"),
                                           mine)
    if foreign:
        reason = "model worker waits: the repository has uncommitted non-worker changes %s" \
            % foreign[:6]
        if state.get("model_status") != reason:
            home.event(state, reason)
        state["model_status"] = reason
        state["next_action"] = reason
        return "waiting"
    from seren_harness import claude_budget
    budget = claude_budget.check(home, cfg)
    if not budget["allowed"]:
        state["model_status"] = "Claude subscription budget: " + budget["reason"]
        state["next_action"] = state["model_status"]
        return "limited"
    state["wait"], state["wait_reason"] = None, None
    model.pop("wait", None)
    extra = {"mode": "maintenance", "goal": goal["goal"]} if goal else {}
    session, record = _record_launch(home, state, "model",
                                     time.time() + cfg["limits"]["wall_seconds"], extra)
    pid = (model_launch or _launch_model)(home, session, record["deadline_epoch"])
    record.update(pid=pid, status="running")
    write_json(home.root / "worker.json", record)
    label = "maintenance worker" if goal else "model worker"
    state["model_status"] = "%s %s running" % (label, session[:8])
    state["next_action"] = state["model_status"]
    home.event(state, "launched %s %s (pid %s)%s" % (label, session[:8], pid,
                                                    " for goal " + goal["goal"] if goal else ""))
    return "launched"


def _launch(home, session):
    """Deterministic worker: a trusted host (committed snapshot code) owns the lab; the
    worker itself, which runs catalog code, runs inside the worker sandbox."""
    log = open(home.root / "log" / ("worker-%s.log" % session), "ab")
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "LANG": "C",
           "PYTHONPATH": str(ROOT)}
    argv = [sys.executable, "-m", "seren_harness", "discovery", "work-host", "--home",
            str(home.root), "--session", session]
    child = subprocess.Popen(argv, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    return child.pid


def lab_host(home, authority, child_argv, env, cwd, timeout):
    """Trusted side: build the lab, prove isolation, serve it to one sandboxed child.
    Returns (child exit code or None if isolation failed, isolation evidence or error)."""
    workdir = Path(tempfile.mkdtemp(prefix="seren-tier0-"))
    try:
        lab = tier0.Lab(authority["executable"], workdir)
        isolation = tier0.preflight(lab, home.root)
        with tier0.LabServer(lab, isolation) as server:
            child = subprocess.Popen(model_worker.sandboxed(home.root, cwd, child_argv),
                                     cwd=str(cwd), env=dict(env, **server.env()))
            try:
                return child.wait(timeout), isolation
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                return 124, isolation
    except tier0.IsolationError as error:
        return None, str(error)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _launch_model(home, session, deadline_epoch):
    """Model-worker wrapper (committed snapshot code, not sandboxed: it runs `claude`)."""
    log = open(home.root / "log" / ("model-%s.wrapper.log" % session), "ab")
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "LANG": "C",
           "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", ""),
           "PYTHONPATH": str(ROOT)}
    argv = [sys.executable, "-m", "seren_harness", "discovery", "model-work", "--home",
            str(home.root), "--session", session, "--deadline", str(deadline_epoch)]
    child = subprocess.Popen(argv, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    return child.pid


# ---------------------------------------------------------------- owner interface

def status_text(home, due=None):
    state = home.state()
    lines = []
    info = None
    try:
        authority = home.authority()
        lines.append("Scope: %s only; live Seren calls, real tasks, production credentials, "
                     "publication and pushes prohibited. Granted %s by %s." % (
                         ", ".join(authority["allowed_kinds"]), authority.get("granted_at"),
                         authority.get("granted_by")))
    except ScopeError as error:
        authority = None
        lines.append("Scope: BLOCKED (%s)" % error)
    lines.append("Stop switch: %s" % ("ON (no new work; `discovery resume` to continue)"
                                      if home.stop_path.exists() else "off"))
    agent = _agent_loaded()
    lines.append("Supervisor: launchd agent %s; last tick %s" % (
        agent, state.get("last_tick", "never")))
    record = read_json(home.root / "worker.json", {})
    if record.get("status") in ("launching", "running"):
        lines.append("Worker: %s since %s, current %s, heartbeat %s" % (
            record["session"][:8], record["started_at"], record.get("current"),
            record.get("heartbeat_at")))
    else:
        lines.append("Worker: none running (last: %s %s)" % (
            record.get("session", "-")[:8], record.get("outcome", "")))
    lines.append("Next: %s" % state.get("next_action", "next supervisor tick"))
    if authority:
        try:
            info = (due or due_inprocess)(home, state, authority)
            lines.append("Queue: due %s; refused (out of scope) %s" % (
                info["due"] or "none", info["refused"] or "none"))
        except (ScopeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            lines.append("Queue: catalog unreadable (%s)" % error)
    for case_id, title, _ in (info or {}).get("catalog", []):
        entry = state["cases"].get(case_id)
        if entry:
            lines.append("  %s %s: %s%s" % (case_id, title, entry.get("status"),
                                            " (%s)" % entry["deferred_reason"]
                                            if entry.get("deferred_reason") else ""))
        else:
            lines.append("  %s %s: not yet run" % (case_id, title))
    lines.append("Findings (%d):" % len(state["findings"]))
    for key, item in sorted(state["findings"].items()):
        lines.append("  %s [%s, %s] seen %dx: %s" % (
            key, item["disposition"], item.get("grade"), len(item["occurrences"]),
            item["summary"][:150]))
    today = now().date().isoformat()
    sessions = state["sessions"]
    lines.append("Resources: %d worker sessions today (limit %s), %d total; evidence %.1f MB" % (
        sum(1 for s in sessions if s["started_at"][:10] == today),
        authority["limits"]["max_sessions_per_day"] if authority else "?", len(sessions),
        sum(p.stat().st_size for p in (home.root / "evidence").rglob("*") if p.is_file())
        / 1e6))
    model = state.get("model", {})
    if authority and authority.get("model_worker", {}).get("enabled"):
        last = (model.get("sessions") or [{}])[-1]
        lines.append("Model workers: enabled (%s, subscription login only); %s; %d sessions "
                     "today (limit %s); last %s %s" % (
                         authority["model_worker"].get("model"),
                         state.get("model_status", "-"), _today(state, "model"),
                         authority["model_worker"]["limits"]["sessions_per_day"],
                         last.get("session", "-")[:8], last.get("outcome", "")))
        for key, item in sorted(model.get("candidates", {}).items()):
            lines.append("  candidate %s [%s]: %s" % (key, item["status"],
                                                      item["rationale"][:120]))
        commits = [c for c in model.get("commits", []) if c["exit"] == 0]
        lines.append("  worker commits: %s" % (", ".join(c["head"] for c in commits) or
                                               "none"))
    else:
        lines.append("Model workers: not enabled")
    eligible, gated = model_worker.followup_groups(state)
    lines.append("Follow-ups: %d eligible for Tier 0 work, %d awaiting your authority" % (
        len(eligible), len(gated)))
    for item in eligible:
        lines.append("  %s [cost %s] %s | next: %s" % (
            item["id"], item.get("cost"), item["consequence"][:110], item["next_step"][:110]))
    for item in gated:
        lines.append("  %s [needs %s] %s | next: %s" % (
            item["id"], item["needs"], item["consequence"][:110], item["next_step"][:110]))
    if state.get("harness_failures"):
        lines.append("Harness test failures in worker commits: %s" % {
            k: v["count"] for k, v in state["harness_failures"].items()})
    lines.append("Blockers: %s" % (state.get("blockers") or "none"))
    lines.append("Recent events:")
    lines += ["  " + e for e in state.get("events", [])[-8:]]
    return "\n".join(lines)


def _agent_loaded():
    try:
        out = subprocess.run(["/bin/launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if out.returncode != 0:
        return "NOT loaded"
    interval = next((x.split("=")[1].strip() for x in out.stdout.splitlines()
                     if "run interval" in x), "?")
    return "loaded (interval %s)" % interval


def plist(home, tick_seconds, repo=None):
    """The agent runs the launcher, which executes the committed HEAD snapshot."""
    repo = Path(repo or ROOT)
    python = str(repo / ".venv" / "bin" / "python")
    args = "".join("<string>%s</string>" % a for a in
                   (python, str(repo / "seren_harness" / "discovery_launcher.py"), "--home",
                    str(home.root)))
    log = home.root / "log" / "supervisor.log"
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>%s</string>
<key>ProgramArguments</key><array>%s</array>
<key>WorkingDirectory</key><string>%s</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin</string></dict>
<key>StartInterval</key><integer>%d</integer>
<key>RunAtLoad</key><true/>
<key>ProcessType</key><string>Standard</string>
<key>StandardOutPath</key><string>%s</string>
<key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (LABEL, args, repo, tick_seconds, log, log))


MODEL_LIMITS = {"wall_seconds": 2700, "log_bytes": 8000000, "stall_seconds": 900,
                "sessions_per_day": 24, "backoff_seconds": [900, 3600, 10800],
                "rate_limit_backoff_seconds": 3600}
PINNED_PATHS = ("cli/src", "cli/README.md", "sdk/src", "sdk/openapi", "sdk/build.rs")


def enable_model_worker(home, args):
    """Owner-invoked: record the owner's grant of model workers in the private authority
    and export the pinned source (read-only reference for workers)."""
    authority = home.authority()
    claude = Path(os.path.expanduser(args.claude))
    if not os.access(claude, os.X_OK):
        raise ScopeError("claude executable not found")
    target = home.root / "pinned-source"
    if not (target / ".complete").exists():
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(mode=0o700)
        archive = subprocess.Popen(["/usr/bin/git", "-C", os.path.expanduser(
            args.pinned_source_repo), "archive", args.pinned_commit, *PINNED_PATHS],
            stdout=subprocess.PIPE)
        subprocess.run(["/usr/bin/tar", "-x", "-C", str(target)], stdin=archive.stdout,
                       check=True)
        if archive.wait() != 0:
            raise ScopeError("pinned source export failed")
        (target / ".complete").write_text(args.pinned_commit + "\n")
    authority["model_worker"] = {
        "enabled": True, "granted_by": "project owner", "granted_at": iso()[:10],
        "grant": "Supervisor may launch bounded Claude Code model workers through the "
                 "existing subscription account for local Tier 0 discovery and routine "
                 "harness development under explicit recipient authorization.",
        "claude": str(claude), "model": args.model, "repo": str(ROOT),
        "pinned_source": str(target), "contract_map": str(Path(os.path.expanduser(
            args.contract_map)).resolve()), "limits": dict(MODEL_LIMITS)}
    model_worker.config(authority)
    validate_authority(authority)
    write_json(home.root / "authority.json", authority)
    return authority["model_worker"]


def run(args):
    home = Home(args.home)
    if args.action == "init":
        target = home.root / "authority.json"
        if target.exists():
            print("Standing authority already recorded; not overwritten: %s" % target)
            return 0
        value = json.loads(TEMPLATE.read_text())
        source = Path(os.path.expanduser(args.executable))
        if tier0.sha256_file(source) != tier0.PINNED_SHA256:
            print("Refused: executable is not the pinned Seren CLI 0.10.0 build.")
            return 2
        local = home.root / "bin" / "seren"  # outside protected folders for launchd
        local.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(source, local)
        os.chmod(local, 0o755)
        value["executable"] = str(local)
        validate_authority(value)
        write_json(target, value)
        print("Recorded standing authority and pinned executable copy in %s" % home.root)
        return 0
    if args.action == "status":
        print(status_text(home, due_sandboxed))
        return 0
    if args.action == "stop":
        home.stop_path.write_text("stopped by owner at %s\n" % iso())
        print("Stop switch on. Supervisor: %s" % supervise(home, due=due_sandboxed))
        return 0
    if args.action == "due":  # runs inside the worker sandbox (see due_sandboxed)
        print(json.dumps(due_inprocess(home, home.state(), home.authority())))
        return 0
    if args.action == "model-work":
        from seren_harness import maintenance
        cfg = model_worker.config(home.authority())
        outcome, summary = "failed:exception", {}
        try:
            goal = None
            if maintenance.current(home, args.session) is not None:
                goal, problem = maintenance.selection(home, cfg["repo"])
                if goal is None or goal["goal"] != maintenance.current(
                        home, args.session).get("goal"):
                    outcome = "blocked:maintenance_selection"
                    raise ScopeError(problem or "maintenance selection changed")
            outcome, summary = model_worker.run_session(home, args.session,
                                                        float(args.deadline), cfg, goal=goal)
        except ScopeError:
            pass
        finally:
            summary["outcome"] = outcome
            write_json(home.root / "log" / ("model-%s.summary.json" % args.session), summary)
            print(iso(), "model worker", args.session[:8], outcome, flush=True)
        return 0
    if args.action in ("select-maintenance", "clear-maintenance"):  # owner commands only
        from seren_harness import maintenance
        if args.action == "clear-maintenance":
            (home.root / maintenance.SELECTION).unlink(missing_ok=True)
            print("Maintenance selection cleared; discovery sessions resume when eligible.")
            return 0
        try:
            chosen = maintenance.select(home, home.authority()["model_worker"]["repo"],
                                        args.goal or "", args.plan or "", args.owner or "")
        except maintenance.Refused as error:
            print("Refused:", error)
            return 2
        print("Selected maintenance goal %s (%s, sha256 %s). The supervisor launches it at "
              "the next eligible tick under the existing session, budget and time limits."
              % (chosen["goal"], chosen["plan"], chosen["plan_sha256"]))
        return 0
    if args.action == "enable-model-worker":
        value = enable_model_worker(home, args)
        print("Model workers enabled: %s via %s; limits %s" % (value["model"], value["claude"],
                                                               value["limits"]))
        return 0
    if args.action == "resume":
        if home.stop_path.exists():
            home.stop_path.unlink()
        with home.edit() as state:
            state["next_action"] = "next supervisor tick"
            home.event(state, "stop switch cleared by owner")
        print("Stop switch off; the next supervisor tick may launch work.")
        return 0
    if args.action == "supervise":
        word = supervise(home, due=due_sandboxed)
        if word == "launched":  # other states repeat; they live in `status` and events
            print(iso(), word, flush=True)
        return 0
    if args.action == "work-host":
        authority = home.authority()
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "LANG": "C",
               "PYTHONPATH": str(ROOT)}
        code, isolation = lab_host(home, authority, default_worker_argv(home, args.session),
                                   env, ROOT, authority["limits"]["worker_wall_seconds"] + 60)
        if code is None:
            with home.edit() as state:
                state["blockers"]["isolation"] = "%s (%s)" % (isolation, iso())
                home.event(state, "isolation check failed: %s; no scenario ran" % isolation)
            write_json(home.root / "log" / ("worker-%s.result.json" % args.session),
                       {"outcome": "blocked:isolation", "at": iso()})
        else:
            with home.edit() as state:
                state["blockers"].pop("isolation", None)
        print(iso(), "work-host", args.session[:8], "child exit", code, flush=True)
        return 0
    if args.action == "work":
        outcome = "failed:exception"
        try:
            outcome = work(home, args.session)
        finally:
            write_json(home.root / "log" / ("worker-%s.result.json" % args.session),
                       {"outcome": outcome, "at": iso()})
            print(iso(), "worker", args.session[:8], outcome, flush=True)
        return 0
    if args.action == "run-case":  # one-shot development/reproduction path
        authority = home.authority()
        cases = _catalog()
        if args.case not in cases.CASES:
            print("Unknown case %s; catalog: %s" % (args.case, ", ".join(cases.ORDER)))
            return 2
        if cases.CASES[args.case]["kind"] not in authority["allowed_kinds"]:
            print("Refused: case is outside the standing authority.")
            return 2
        workdir = Path(tempfile.mkdtemp(prefix="seren-tier0-"))
        try:
            lab = tier0.lab_from_environment()
            if lab is None:  # direct (interactive) mode
                lab = tier0.Lab(authority["executable"], workdir)
                tier0.preflight(lab, home.root)
            outcome = cases.execute(lab, args.case, args.repeats)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        for name, facts in outcome["facts"].items():
            print("facts %s: %s" % (name, json.dumps(facts, default=str)[:600]))
        for item in outcome["results"]:
            print("%s %s: %s\n  %s" % (item["case"], item["part"], item["disposition"],
                                       item["summary"]))
        return 0
    if args.action == "agent-plist":
        print(plist(home, home.authority()["limits"]["tick_seconds"]))
        return 0
    return 2
