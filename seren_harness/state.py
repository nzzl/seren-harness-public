"""SQLite state: runs, steps, action journal, cleanup obligations, locks, findings.

Every text or JSON value passes through the sanitizer before it is written, so
the database never needs to be treated as a secret store.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import uuid

from seren_harness.models import RUN_ACTIVE, utc_now
from seren_harness.sanitize import Sanitizer

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, journey TEXT NOT NULL, employee_key TEXT NOT NULL,
    mode TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, config_digest TEXT NOT NULL, harness_version TEXT NOT NULL,
    cli_version TEXT, server_build TEXT NOT NULL, fixture_json TEXT,
    owner_pid INTEGER, owner_host TEXT);
CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL, name TEXT NOT NULL, seq INTEGER NOT NULL, outcome TEXT NOT NULL,
    summary TEXT NOT NULL, evidence_json TEXT NOT NULL, finished_at TEXT NOT NULL,
    finding_key TEXT, PRIMARY KEY (run_id, name));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, at TEXT NOT NULL,
    kind TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step TEXT NOT NULL,
    kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
    detail_json TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS cleanup (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, employee_key TEXT NOT NULL,
    kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
    detail_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locks (
    employee_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, pid INTEGER NOT NULL,
    host TEXT NOT NULL, acquired_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS findings (
    key TEXT PRIMARY KEY, journey TEXT NOT NULL, step TEXT NOT NULL, assertion TEXT NOT NULL,
    classification TEXT NOT NULL, title TEXT NOT NULL, context_json TEXT NOT NULL,
    payload_json TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL, latest_run TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS occurrences (
    finding_key TEXT NOT NULL, run_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    PRIMARY KEY (finding_key, run_id));
CREATE TABLE IF NOT EXISTS drafts (
    finding_key TEXT PRIMARY KEY, path TEXT NOT NULL, rendered_occurrences INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fixtures (
    employee_key TEXT PRIMARY KEY, identity_json TEXT NOT NULL, run_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS retests (
    finding_key TEXT NOT NULL, run_id TEXT NOT NULL, outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY (finding_key, run_id));
CREATE TABLE IF NOT EXISTS finding_relations (
    finding_key TEXT NOT NULL, related_key TEXT NOT NULL, relation TEXT NOT NULL,
    detail_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY (finding_key, related_key, relation));
CREATE TABLE IF NOT EXISTS issue_links (
    finding_key TEXT NOT NULL, repository TEXT NOT NULL, number INTEGER NOT NULL,
    url TEXT NOT NULL, confirmed_by TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY (finding_key, repository, number));
"""
# A person's confirmed association between a finding and an existing GitHub issue. Only this
# exact form is stored; search suggestions are never written here.
_ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/"
                        r"([1-9][0-9]{0,9})")
ISSUE_URL_FORM = "https://github.com/<owner>/<repo>/issues/<number>"
# `alias_of`: a record correction; the finding is the same observation as `related_key` and is
# kept only as history. `related`: distinct claims (for example local and Discord evidence of
# one case criterion) that a reader should see together. Neither deletes or merges records.
RELATIONS = ("alias_of", "related")

# Journal states for mutations whose outcome must be known before any retry.
INTENT = "intent"
SUCCEEDED = "succeeded"
REJECTED = "rejected"
AMBIGUOUS = "ambiguous"
RECONCILED_APPLIED = "reconciled_applied"
RECONCILED_NOT_APPLIED = "reconciled_not_applied"
# The outcome stays unknown, but a later confirmed action settled the resource's state
# (for example a confirmed stop after an unanswered start).
SUPERSEDED = "superseded"
UNRESOLVED = (INTENT, AMBIGUOUS)
APPLIED = (SUCCEEDED, RECONCILED_APPLIED)


class StateError(Exception):
    pass


class LockHeld(StateError):
    pass


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class Store:
    def __init__(self, state_dir, sanitizer=None):
        self.directory = private_directory(Path(state_dir))
        self.path = self.directory / "state.sqlite3"
        self.sanitizer = sanitizer or Sanitizer()
        created = not self.path.exists()
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        if created:
            os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._check_schema()

    def close(self):
        self.db.close()

    def _check_schema(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                            (str(SCHEMA_VERSION),))
        elif int(row["value"]) != SCHEMA_VERSION:
            raise StateError("state schema version " + row["value"] + " is not supported by "
                             "this harness (expects " + str(SCHEMA_VERSION) + ")")

    def _json(self, value) -> str:
        return json.dumps(self.sanitizer.value(value), sort_keys=True)

    def _text(self, value: str) -> str:
        return self.sanitizer.text(value)

    # Runs ---------------------------------------------------------------
    def create_run(self, journey, employee_key, mode, config_digest, harness_version):
        run_id = utc_now()[:10].replace("-", "") + "-" + uuid.uuid4().hex[:10]
        now = utc_now()
        self.db.execute(
            "INSERT INTO runs (id, journey, employee_key, mode, status, created_at, updated_at,"
            " config_digest, harness_version, server_build) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, journey, employee_key, mode, RUN_ACTIVE, now, now, config_digest,
             harness_version, "unknown"))
        self.event(run_id, "run_created", {"journey": journey, "mode": mode})
        return run_id

    def ensure_run(self, run_id, journey, employee_key, mode, config_digest, harness_version):
        """Create a run with a caller-chosen, evidence-derived ID; repeating it is a no-op."""
        now = utc_now()
        created = self.db.execute(
            "INSERT OR IGNORE INTO runs (id, journey, employee_key, mode, status, created_at,"
            " updated_at, config_digest, harness_version, server_build)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, journey, employee_key, mode, RUN_ACTIVE, now, now, config_digest,
             harness_version, "unknown")).rowcount
        if created:
            self.event(run_id, "run_created", {"journey": journey, "mode": mode})

    def run(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise StateError("unknown run: " + run_id)
        return dict(row)

    def runs(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY created_at")]

    def update_run(self, run_id, **fields):
        allowed = {"status", "cli_version", "server_build", "fixture_json", "owner_pid",
                   "owner_host"}
        if not set(fields) <= allowed:
            raise StateError("unsupported run fields: " + ", ".join(sorted(set(fields) - allowed)))
        if "fixture_json" in fields and fields["fixture_json"] is not None:
            fields["fixture_json"] = self._json(fields["fixture_json"])
        for key in ("cli_version", "server_build"):
            if fields.get(key) is not None:
                fields[key] = self._text(fields[key])
        assignments = ", ".join(key + "=?" for key in fields)
        self.db.execute("UPDATE runs SET " + assignments + ", updated_at=? WHERE id=?",
                        (*fields.values(), utc_now(), run_id))

    def event(self, run_id, kind, detail=None):
        self.db.execute("INSERT INTO events (run_id, at, kind, detail_json) VALUES (?,?,?,?)",
                        (run_id, utc_now(), kind, self._json(detail or {})))

    def events(self, run_id):
        return [dict(r, detail=json.loads(r["detail_json"])) for r in self.db.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))]

    # Steps --------------------------------------------------------------
    def record_step(self, run_id, seq, name, result, finding_key=None):
        self.db.execute(
            "INSERT OR REPLACE INTO steps VALUES (?,?,?,?,?,?,?,?)",
            (run_id, name, seq, result.outcome, self._text(result.summary),
             self._json(result.evidence), utc_now(), finding_key))
        self.event(run_id, "step", {"step": name, "outcome": result.outcome})

    def steps(self, run_id):
        return [dict(r, evidence=json.loads(r["evidence_json"])) for r in self.db.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,))]

    def invalidate_steps(self, run_id, names, reason):
        for name in names:
            self.db.execute("DELETE FROM steps WHERE run_id=? AND name=?", (run_id, name))
        self.event(run_id, "steps_invalidated", {"steps": list(names), "reason": reason})

    # Action journal -----------------------------------------------------
    def record_intent(self, run_id, step, kind, target, detail=None):
        """Persist intent before a mutation so a crash leaves a visible record."""
        cursor = self.db.execute(
            "INSERT INTO actions (run_id, step, kind, target, status, detail_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, step, kind, target, INTENT, self._json(detail or {}), utc_now()))
        return cursor.lastrowid

    def resolve_action(self, action_id, status, detail=None):
        resolved = None if status in UNRESOLVED else utc_now()
        self.db.execute(
            "UPDATE actions SET status=?, detail_json=?, resolved_at=? WHERE id=?",
            (status, self._json(detail or {}), resolved, action_id))

    def actions(self, run_id=None, unresolved_only=False):
        query, params = "SELECT * FROM actions", []
        clauses = []
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if unresolved_only:
            clauses.append("status IN (?, ?)")
            params.extend(UNRESOLVED)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return [dict(r, detail=json.loads(r["detail_json"]))
                for r in self.db.execute(query + " ORDER BY id", params)]

    def supersede_actions(self, run_id, kind, reason):
        for action in self.actions(run_id, unresolved_only=True):
            if action["kind"] == kind:
                self.resolve_action(action["id"], SUPERSEDED,
                                    dict(action["detail"], superseded_by=reason))
                self.event(run_id, "action_superseded", {"action": action["id"],
                                                         "reason": reason})

    def operator_reconcile(self, run_id, action_id, applied, note):
        """Record a human's verified answer for a mutation the harness cannot reconcile."""
        row = self.db.execute("SELECT * FROM actions WHERE id=? AND run_id=?",
                              (action_id, run_id)).fetchone()
        if row is None:
            raise StateError("run " + run_id + " has no action " + str(action_id))
        if row["status"] not in UNRESOLVED:
            raise StateError("action " + str(action_id) + " is already " + row["status"])
        status = RECONCILED_APPLIED if applied else RECONCILED_NOT_APPLIED
        self.resolve_action(action_id, status, dict(json.loads(row["detail_json"]),
                                                    reconciled_by="operator", note=note))
        self.event(run_id, "operator_reconciled", {"action": action_id, "status": status,
                                                   "note": note})
        return status

    # Cleanup obligations ------------------------------------------------
    def add_cleanup(self, run_id, employee_key, kind, target):
        now = utc_now()
        cursor = self.db.execute(
            "INSERT INTO cleanup (run_id, employee_key, kind, target, status, detail_json,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, employee_key, kind, target, "pending", "{}", now, now))
        return cursor.lastrowid

    def update_cleanup(self, cleanup_id, status, detail=None):
        if status not in ("pending", "done", "failed"):
            raise StateError("unknown cleanup status: " + status)
        self.db.execute("UPDATE cleanup SET status=?, detail_json=?, updated_at=? WHERE id=?",
                        (status, self._json(detail or {}), utc_now(), cleanup_id))

    def open_cleanup(self, employee_key=None, run_id=None):
        query = "SELECT * FROM cleanup WHERE status != 'done'"
        params = []
        if employee_key is not None:
            query += " AND employee_key=?"
            params.append(employee_key)
        if run_id is not None:
            query += " AND run_id=?"
            params.append(run_id)
        return [dict(r, detail=json.loads(r["detail_json"]))
                for r in self.db.execute(query + " ORDER BY id", params)]

    def cleanup_items(self, run_id):
        return [dict(r, detail=json.loads(r["detail_json"])) for r in self.db.execute(
            "SELECT * FROM cleanup WHERE run_id=? ORDER BY id", (run_id,))]

    # Per-employee lock --------------------------------------------------
    def acquire_lock(self, employee_key, run_id):
        """One run per employee. Only the owning run may take over its own lock."""
        host, pid = socket.gethostname(), os.getpid()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM locks WHERE employee_key=?",
                                  (employee_key,)).fetchone()
            if row is not None and row["run_id"] != run_id:
                raise LockHeld("employee " + employee_key + " is locked by run " + row["run_id"]
                               + " (pid " + str(row["pid"]) + " on " + row["host"] + ")"
                               + _staleness(row))
            if row is not None and row["pid"] != pid and _alive(row):
                raise LockHeld("run " + run_id + " is already active in pid " + str(row["pid"]))
            self.db.execute("INSERT OR REPLACE INTO locks VALUES (?,?,?,?,?)",
                            (employee_key, run_id, pid, host, utc_now()))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.update_run(run_id, owner_pid=pid, owner_host=host)
        self.event(run_id, "lock_acquired", {"employee": employee_key})

    def release_lock(self, employee_key, run_id):
        self.db.execute("DELETE FROM locks WHERE employee_key=? AND run_id=?",
                        (employee_key, run_id))
        self.event(run_id, "lock_released", {"employee": employee_key})

    def lock(self, employee_key):
        row = self.db.execute("SELECT * FROM locks WHERE employee_key=?",
                              (employee_key,)).fetchone()
        return dict(row) if row else None

    # Findings -----------------------------------------------------------
    def record_finding(self, key, run_id, journey, step, finding, details=None):
        """Count one occurrence per run; never create a second record for the same key."""
        now = utc_now()
        payload = self._json(dict(details or {}, **{
            "expected": finding.expected, "observed": finding.observed,
            "impact": finding.impact, "unknowns": finding.unknowns}))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            new_occurrence = self.db.execute(
                "INSERT OR IGNORE INTO occurrences VALUES (?,?,?)", (key, run_id, now)).rowcount
            exists = self.db.execute("SELECT 1 FROM findings WHERE key=?", (key,)).fetchone()
            if exists is None:
                self.db.execute(
                    "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, journey, step, finding.assertion, finding.classification,
                     self._text(finding.title), self._json(finding.context), payload, now, now,
                     1, run_id))
            elif new_occurrence:
                self.db.execute(
                    "UPDATE findings SET occurrences=occurrences+1, last_seen=?, latest_run=?,"
                    " payload_json=? WHERE key=?", (now, run_id, payload, key))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def finding(self, key):
        row = self.db.execute("SELECT * FROM findings WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return dict(row, context=json.loads(row["context_json"]),
                    payload=json.loads(row["payload_json"]))

    def findings(self):
        return [self.finding(r["key"]) for r in
                self.db.execute("SELECT key FROM findings ORDER BY first_seen")]

    def finding_runs(self, key):
        return [r["run_id"] for r in self.db.execute(
            "SELECT run_id FROM occurrences WHERE finding_key=? ORDER BY observed_at", (key,))]

    def draft(self, key):
        row = self.db.execute("SELECT * FROM drafts WHERE finding_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def save_draft(self, key, path, occurrences):
        now = utc_now()
        existing = self.draft(key)
        self.db.execute("INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?)",
                        (key, str(path), occurrences,
                         existing["created_at"] if existing else now, now))

    def record_retest(self, key, run_id, outcome, detail):
        """Link a later evaluation to its finding; the same evidence replaces, never repeats.
        A repeat keeps the original recording time, so history order never changes."""
        updated = self.db.execute(
            "UPDATE retests SET outcome=?, detail_json=? WHERE finding_key=? AND run_id=?",
            (outcome, self._json(detail), key, run_id)).rowcount
        if not updated:
            self.db.execute("INSERT INTO retests VALUES (?,?,?,?,?)",
                            (key, run_id, outcome, self._json(detail), utc_now()))

    def retests(self, key):
        return [dict(r, detail=json.loads(r["detail_json"])) for r in self.db.execute(
            "SELECT * FROM retests WHERE finding_key=? ORDER BY recorded_at", (key,))]

    def record_relation(self, key, related_key, relation, detail):
        """Record an explicit relation once; repeating it changes nothing."""
        if relation not in RELATIONS or key == related_key:
            raise StateError("unknown or self-referencing finding relation")
        self.db.execute("INSERT OR IGNORE INTO finding_relations VALUES (?,?,?,?,?)",
                        (key, related_key, relation, self._json(detail), utc_now()))

    def relations(self, key):
        """Relations in either direction, each as seen from `key`."""
        rows = self.db.execute(
            "SELECT * FROM finding_relations WHERE finding_key=? OR related_key=?"
            " ORDER BY recorded_at", (key, key))
        return [dict(r, detail=json.loads(r["detail_json"]),
                     other=r["related_key"] if r["finding_key"] == key else r["finding_key"],
                     outgoing=r["finding_key"] == key) for r in rows]

    def alias_target(self, key):
        row = self.db.execute("SELECT related_key FROM finding_relations WHERE finding_key=?"
                              " AND relation='alias_of'", (key,)).fetchone()
        return row["related_key"] if row else None

    def finding_mode(self, key):
        """Evidence mode of a finding's first occurrence (part of its identity)."""
        runs = self.finding_runs(key)
        return self.run(runs[0])["mode"] if runs else None

    def drafts(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM drafts ORDER BY created_at")]

    def record_issue_link(self, key, url, confirmed_by):
        """Record a person's confirmed association once. Returns (link, created); an existing
        link, including its attribution and time, is never changed. The issue itself is not
        checked: this is the person's assertion, recorded locally."""
        if self.finding(key) is None:
            raise StateError("unknown finding: " + key)
        owner, repo, number = parse_issue_url(url)
        if not isinstance(confirmed_by, str) or not confirmed_by.strip() \
                or "\n" in confirmed_by or "\r" in confirmed_by:
            raise StateError("--confirmed-by must name who confirmed the association "
                             "(one line)")
        created = self.db.execute(
            "INSERT OR IGNORE INTO issue_links VALUES (?,?,?,?,?,?)",
            (key, (owner + "/" + repo).lower(), number,
             "https://github.com/" + owner + "/" + repo + "/issues/" + str(number),
             self._text(confirmed_by.strip()), utc_now())).rowcount
        link = self.db.execute(
            "SELECT * FROM issue_links WHERE finding_key=? AND repository=? AND number=?",
            (key, (owner + "/" + repo).lower(), number)).fetchone()
        return dict(link), bool(created)

    def issue_links(self, key=None):
        query, params = "SELECT * FROM issue_links", ()
        if key is not None:
            query, params = query + " WHERE finding_key=?", (key,)
        return [dict(r) for r in self.db.execute(query + " ORDER BY recorded_at, finding_key",
                                                 params)]

    # Provisioned fixture baseline ---------------------------------------
    def save_fixture(self, employee_key, identity, run_id):
        self.db.execute("INSERT OR REPLACE INTO fixtures VALUES (?,?,?,?)",
                        (employee_key, self._json(identity), run_id, utc_now()))

    def fixture(self, employee_key):
        row = self.db.execute("SELECT * FROM fixtures WHERE employee_key=?",
                              (employee_key,)).fetchone()
        return dict(row, identity=json.loads(row["identity_json"])) if row else None

    def employee_unresolved_actions(self, employee_key):
        return [dict(r) for r in self.db.execute(
            "SELECT actions.* FROM actions JOIN runs ON runs.id = actions.run_id"
            " WHERE runs.employee_key=? AND actions.status IN (?, ?) ORDER BY actions.id",
            (employee_key, *UNRESOLVED))]


def parse_issue_url(url):
    """(owner, repo, number) of a plain GitHub issue URL. The input is never echoed, since a
    rejected value may carry credentials."""
    match = _ISSUE_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        raise StateError("issue URL rejected: expected exactly " + ISSUE_URL_FORM
                         + " (https, no credentials, port, query or fragment)")
    return match.group(1), match.group(2), int(match.group(3))


def issue_reference(link):
    """`owner/repo#n` from the stored URL (keeps the owner's spelling)."""
    owner, repo, number = parse_issue_url(link["url"])
    return owner + "/" + repo + "#" + str(number)


def _alive(row):
    if row["host"] != socket.gethostname():
        return True  # Cannot inspect another host; treat as alive.
    try:
        os.kill(row["pid"], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _staleness(row):
    if _alive(row):
        return ""
    return "; that process has exited, so resume that run to reconcile its " \
           "cleanup and action journal"
