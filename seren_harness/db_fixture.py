"""Dedicated-project fixture for the database-read campaign: setup, cleanup and status.

The only resource created is one new SerenDB project named by the approved package, holding
one synthetic table written by the manifest's single frozen transaction. What runs is fixed
by docs/db-fixture/lifecycle-package.proposed.json, its manifest and the pinned target entry
(all bound by sha256 to one owner authorization) plus IDs this attempt recorded itself. No
SQL, ID, name or CLI argument is taken from the command line. Campaign read cases and model
sessions are not implemented here.

Durability: before an invocation runs, its worst-case requests are reserved and it is marked
in flight in state.json; a fresh process treats anything still in flight as uncertain and
never replays it. The cleanup obligation opens before the create request (without an ID) and
closes only when deletion is verified. Ordinary setup work may not use the cleanup reserve.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

from seren_harness import sql_probe
from seren_harness.contract import SUPPORTED_CLI_VERSIONS
from seren_harness.models import utc_now
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer
from seren_harness.state import private_directory

OK, FAILED, BLOCKED, INVALID = 0, 1, 4, 5

# Worst-case service requests reserved per invocation and invocation limits (source-derived:
# projects.rs delete makes a GET then a DELETE; psql.rs fetches the URI, then psql connects).
COST = {"A1": 1, "R1": 1, "A2": 1, "A3": 1, "A4": 2, "A5": 2, "C1": 2, "C2": 1}
MAX = {"A1": 1, "R1": 2, "A2": 1, "A3": 1, "A4": 3, "A5": 1, "C1": 2, "C2": 2}
MUTATING = ("A1", "A4", "C1")
CLEANUP_RESERVE = COST["C1"] * MAX["C1"] + COST["C2"] * MAX["C2"] + COST["R1"]
MAX_REQUESTS = sum(COST[a] * MAX[a] for a in COST)
CAMPAIGN_AUTHORIZATION = "db_read_campaign_authorization"
AUTHORIZATION_KINDS = ("db_fixture_authorization", CAMPAIGN_AUTHORIZATION)
CREATE_BANNER = "\u2713 Project created successfully!"
# Provenance of a project binding. Only an ID read from this attempt's own create response
# carries deletion authority; a name/time match in a listing never does.
CREATE_RESPONSE = "A1 create response"
UNPROVEN = "unproven: a project listing cannot show which attempt created a project"
NEEDED = ("resolve outside the harness by confirming in Seren which project, if any, this "
          "attempt created, and removing it there")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_INSTANT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,9})?(Z|[+-]\d\d:\d\d)")
_STATUS = re.compile(r"\bstatus:? (\d{3})\b")
_DATABASE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_NAME = re.compile(r"seren-harness-dbread-[0-9a-f]{8}")
FLAGS = ("block_public_connections", "block_vpc_connections", "hipaa",
         "protected_branches_only", "enable_logical_replication")
PROJECT_FIELDS = ("id", "name", "organization_id", "region", "default_branch_id",
                  "created_at", "deleted_at", "postgres_version", "compute_unit_min",
                  "compute_unit_max") + FLAGS


class FixtureError(Exception):
    """The inputs or authority are not acceptable; nothing was requested."""


class _Stop(Exception):
    """Setup cannot continue; the reason is recorded and cleanup follows."""


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _when(text):
    text = str(text).replace("Z", "+00:00")
    match = re.fullmatch(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)", text)
    if match and match.group(2):  # Python's parser accepts at most six fractional digits
        text = match.group(1) + match.group(2)[:7] + match.group(3)
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError("timestamp without a zone")
    return value


def _strict_equal(a, b):
    """JSON equality without bool/int/float coercion."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def _bind(name):
    return {"$bind": name}


def _psql(statement):
    return ["psql", "--project-id", _bind("project.id"), "--branch-id", _bind("branch.id"),
            "--database", _bind("database.name"), "--ssl", "require", "--", "-X", "-w", "-A",
            "-t", "--set=ON_ERROR_STOP=1", "-c", {"$statement": statement}]


def expected_actions(name):
    """The only command shapes this adapter will run; the package must match exactly."""
    create = ["projects", "create", "--name", name, "--region", "us-east-1"]
    for flag in FLAGS:
        create += ["--" + flag.replace("_", "-"), "false"]
    return {"A1": create + ["-o", "json"], "R1": ["projects", "list", "-o", "json"],
            "A2": ["branches", "--project-id", _bind("project.id"), "list", "-o", "json"],
            "A3": ["databases", "--project-id", _bind("project.id"), "--branch-id",
                   _bind("branch.id"), "list", "-o", "json"],
            "A4": _psql("fixture_write"), "A5": _psql("fixture_readback"),
            "C1": ["projects", "delete", _bind("project.id"), "-y"],
            "C2": ["projects", "get", _bind("project.id"), "-o", "json"]}


def _literal(value, kind):
    if value is None:
        return "NULL"
    if kind == "integer":
        return str(int(value))
    if kind == "date":
        return "DATE '" + value + "'"
    return "'" + value.replace("'", "''") + "'"


def fixture_write_statement(manifest):
    """The frozen write rebuilt from the manifest rows; the manifest's text must equal it."""
    table = manifest["table"]
    kinds = {name: kind for name, kind, _ in table["columns"]}
    qualified = table["schema"] + "." + table["name"]
    values = ", ".join("(" + ", ".join(_literal(r[c], kinds[c]) for c in kinds) + ")"
                       for r in manifest["rows"])
    return ("BEGIN; CREATE SCHEMA " + table["schema"] + "; CREATE TABLE " + qualified
            + " (invoice_key text PRIMARY KEY, customer text NOT NULL, status text NOT NULL "
            "CHECK (status IN ('open', 'paid', 'void')), amount_cents integer NOT NULL, "
            "due_date date, note text); INSERT INTO " + qualified + " ("
            + ", ".join(kinds) + ") VALUES " + values + "; COMMIT;")


def expected_readback(manifest, database):
    """The read-back expectation, materialized only from the frozen manifest."""
    table = manifest["table"]
    return {"database": database, "read_only": "on",
            "columns": [list(c) for c in table["columns"]],
            "relations": list(table["relations"]),
            "rows": sorted((dict(r) for r in manifest["rows"]),
                           key=lambda r: r["invoice_key"])}


def check_package(package, manifest):
    """Problems that make the package or manifest unusable (empty when acceptable)."""
    problems = []
    name = package.get("project", {}).get("name")
    if package.get("kind") != "db_fixture_lifecycle_package" or not isinstance(
            package.get("id"), str) or not isinstance(name, str) or not _NAME.fullmatch(name):
        return ["the package needs its kind, id and a seren-harness-dbread-<8 hex> name"]
    actions = {a.get("id"): a for a in package.get("actions", [])}
    for key, argv in expected_actions(name).items():
        action = actions.get(key, {})
        if action.get("argv") != argv:
            problems.append("action " + key + " is not the supported command")
        if action.get("max_invocations") != MAX[key]:
            problems.append("action " + key + " has an unsupported invocation limit")
        requests = action.get("requests", {})
        if requests.get("api", 0) + requests.get("db_connections", 0) != COST[key]:
            problems.append("action " + key + " declares a different request cost")
    accounting = package.get("accounting", {}).get("fixture_adapter", {})
    if (accounting.get("max_service_requests") != MAX_REQUESTS
            or accounting.get("cleanup_reserve") != CLEANUP_RESERVE
            or accounting.get("ordinary_limit") != MAX_REQUESTS - CLEANUP_RESERVE):
        problems.append("the package budget differs from the adapter's action limits")
    limits = package.get("limits", {})
    for key, low, high in (("api_invocation_timeout_seconds", 1, 120),
                           ("psql_invocation_timeout_seconds", 1, 60),
                           ("psql_connect_timeout_seconds", 1, 30),
                           ("statement_timeout_ms", 1, 30000),
                           ("reattempt_delay_seconds", 0, 300),
                           ("recovery_list_spacing_seconds", 0, 3600),
                           ("output_cap_bytes", 1024, 1 << 20),
                           ("setup_deadline_minutes", 1, 120)):
        if not isinstance(limits.get(key), int) or not low <= limits[key] <= high:
            problems.append("limit " + key + " is missing or out of range")
    statements = manifest.get("statements", {})
    rows = manifest.get("rows", [])
    if (manifest.get("kind") != "db_fixture_manifest" or not 0 < len(rows) <= 12
            or len({r.get("invoice_key") for r in rows}) != len(rows)):
        problems.append("the manifest needs at most 12 uniquely keyed rows")
    elif statements.get("fixture_write") != fixture_write_statement(manifest):
        problems.append("the manifest's write statement is not exactly its rows")
    if not isinstance(statements.get("fixture_readback"), str):
        problems.append("the manifest has no read-back statement")
    return problems


def check_authorization(auth, shas, purpose, now=None):
    """Problems with the authority for this purpose ('setup' or 'cleanup')."""
    now = now or datetime.now(timezone.utc)
    problems = []
    if auth.get("kind") not in AUTHORIZATION_KINDS or auth.get("package") != shas["id"]:
        problems.append("authorization is not for this fixture package")
    for key in ("package_sha256", "manifest_sha256", "target_sha256"):
        if auth.get(key) != shas[key]:
            problems.append("authorization " + key + " does not match")
    if not isinstance(auth.get("approved_by"), str) or not auth["approved_by"].strip():
        problems.append("authorization does not name who approved it")
    if auth.get("kind") == CAMPAIGN_AUTHORIZATION:
        # One owner approval for the whole campaign: the fixture's share is explicit and the
        # shares add up to the total, so no scope can silently exceed it.
        budgets = auth.get("budgets")
        if (not isinstance(budgets, dict) or budgets.get("fixture") != MAX_REQUESTS
                or any(type(v) is not int or v < 0 for v in budgets.values())
                or auth.get("max_service_requests") != sum(budgets.values())):
            problems.append("authorization budgets must give the fixture exactly "
                            + str(MAX_REQUESTS) + " and sum to max_service_requests")
    elif auth.get("max_service_requests") != MAX_REQUESTS:
        problems.append("authorization must allow exactly " + str(MAX_REQUESTS)
                        + " service requests")
    try:
        start, end = _when(auth["not_before"]), _when(auth["expires_at"])
        cleanup_end = _when(auth["cleanup_expires_at"]) if auth.get("cleanup_expires_at") \
            else end
        if cleanup_end < end:
            problems.append("the cleanup window must not end before the main window")
        stop = end if purpose == "setup" else cleanup_end
        if not start <= now < stop:
            problems.append("authorization is not valid now for " + purpose)
    except (KeyError, TypeError, ValueError):
        problems.append("authorization needs not_before/expires_at timestamps")
    return problems


def _run(argv, env, timeout, cap):
    """Run one process in its own session. Returns (kind, exit code, stdout, stderr) where
    kind is exit, timeout, cap or unavailable. The process group is killed on timeout, cap
    breach or interruption, and after exit if a descendant still holds the output pipes."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError:
        return "unavailable", None, "", ""
    buffers, over = {"out": bytearray(), "err": bytearray()}, threading.Event()

    def pump(stream, key):
        while True:
            try:
                block = os.read(stream.fileno(), 65536)
            except (OSError, ValueError):
                return
            if not block:
                return
            if len(buffers[key]) + len(block) > cap:
                over.set()
            else:
                buffers[key].extend(block)

    readers = [threading.Thread(target=pump, args=(process.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(process.stderr, "err"), daemon=True)]
    for reader in readers:
        reader.start()

    def kill():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    kind, deadline = "exit", time.monotonic() + timeout
    try:
        while process.poll() is None:
            if over.is_set() or time.monotonic() > deadline:
                kind = "cap" if over.is_set() else "timeout"
                kill()
                process.wait()
                break
            time.sleep(0.02)
        for reader in readers:
            reader.join(2)
        if any(reader.is_alive() for reader in readers):  # a descendant kept the pipes
            kill()
            for reader in readers:
                reader.join(2)
        if over.is_set():
            kind = "cap"
    except BaseException:
        kill()
        process.wait()
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
    return (kind, process.returncode, buffers["out"].decode("utf-8", "replace"),
            buffers["err"].decode("utf-8", "replace"))


def _category(stderr):
    """A fixed category and HTTP status from stderr; the text itself is never kept."""
    found = _STATUS.search(stderr)
    status = int(found.group(1)) if found else None
    if re.search(r"^Error: Failed to fetch connection URI:", stderr, re.M) \
            and "psql exited with non-zero status" not in stderr:
        return "uri_fetch_failed", status
    if "psql exited with non-zero status" in stderr:
        return "psql_failed", status
    for category, marker in (("create_failed", "Failed to create project:"),
                             ("delete_failed", "Failed to delete project:"),
                             ("get_failed", "Failed to get project:"),
                             ("list_failed", "Failed to list ")):
        if marker in stderr:
            return category, status
    if re.search(r"authentication token|unauthori[sz]ed|\b401\b", stderr, re.I):
        return "authentication", status
    return "unclassified", status


class Fixture:
    def __init__(self, package_path, manifest_path, home, root):
        self.root = Path(root)
        try:
            self.package = json.loads(Path(package_path).read_text())
            self.manifest = json.loads(Path(manifest_path).read_text())
            target_path = self.root / self.package["target"]["path"]
            target = json.loads(target_path.read_text())
            self.entry = target["fixtures"][self.package["target"]["entry"]]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise FixtureError("cannot read the package, manifest or target ("
                               + type(error).__name__ + ")") from None
        problems = check_package(self.package, self.manifest)
        if problems:
            raise FixtureError("; ".join(problems))
        self.target_root = str(Path(target.get("root") or ".").expanduser())
        self.shas = {"id": self.package["id"], "package_sha256": _sha256(package_path),
                     "manifest_sha256": _sha256(manifest_path),
                     "target_sha256": _sha256(target_path)}
        self.name = self.package["project"]["name"]
        self.limits = self.package["limits"]
        self.directory = Path(home).expanduser() / "fixtures" / self.package["id"]
        self.state_path = self.directory / "state.json"
        self.sanitizer = Sanitizer()
        self.state = None
        self.auth = {}
        self.out = sys.stdout
        self._checked = False

    # Durable record ------------------------------------------------------------------
    def say(self, text):
        print(text, file=self.out)

    def save(self):
        self.state["updated_at"] = utc_now()
        temporary = self.state_path.with_suffix(".tmp")
        _write_private(temporary, json.dumps(self.state, indent=2) + "\n")
        os.replace(temporary, self.state_path)

    def _load_state(self, auth_sha):
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            for key in ("package_sha256", "manifest_sha256", "target_sha256"):
                if state[key] != self.shas[key]:
                    raise FixtureError("the recorded attempt used a different " + key)
            if auth_sha is not None and state["authorization_sha256"] != auth_sha:
                raise FixtureError("the recorded attempt used a different authorization")
            for entry in state["actions"] if auth_sha is not None else ():
                # Only a lock holder sees this: the process that marked it has ended.
                if entry["status"] == "in_flight":
                    entry.update(status="uncertain", outcome="interrupted",
                                 finished_at=utc_now())
            self.state = state
            return
        if auth_sha is None:
            raise FixtureError("no recorded fixture attempt")
        self.state = {"package": self.package["id"], **{k: self.shas[k] for k in (
            "package_sha256", "manifest_sha256", "target_sha256")},
            "authorization_sha256": auth_sha, "created_at": utc_now(), "phase": "new",
            "setup_started_at": None, "actions": [], "invocations": {k: 0 for k in COST},
            "reserved": {"ordinary": 0, "cleanup": 0},
            "bindings": {}, "obligation": {"status": "none", "project_id": None},
            "failure": None, "blocker": None, "registration": None}

    def _lock(self):
        lock = self.directory / "lock"
        try:
            descriptor = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip())
                os.kill(pid, 0)
            except (ValueError, OSError):
                lock.unlink()  # the holder exited; its in-flight work becomes uncertain
                return self._lock()
            raise FixtureError("another process (pid " + str(pid) + ") holds this fixture")
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        return lock

    # Execution -----------------------------------------------------------------------
    def _environment(self, mode):
        expand = lambda value: value.replace("{root}", self.target_root)  # noqa: E731
        env = {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin",
               "XDG_CONFIG_HOME": expand(self.entry["config_home"]),
               "SEREN_PROFILE": self.entry["profile"], "NO_COLOR": "1"}
        if mode.startswith("psql"):
            env = sql_probe.environment(env, self.entry["psql_executable"])
            env["PGCONNECT_TIMEOUT"] = str(self.limits["psql_connect_timeout_seconds"])
            env["PGOPTIONS"] = ("-c default_transaction_read_only="
                                + ("off" if mode == "psql_write" else "on")
                                + " -c statement_timeout="
                                + str(self.limits["statement_timeout_ms"]))
        return env

    def _preflight(self):
        """Pinned executables and supported CLI version; local only, no service request."""
        if self._checked:
            return
        executable = self.entry["executable"].replace("{root}", self.target_root)
        psql = self.entry["psql_executable"]
        try:
            if _sha256(executable) != self.entry["executable_sha256"]:
                raise _Stop("the Seren CLI does not match its pinned checksum")
            if (not Path(psql).is_absolute() or Path(psql).name != "psql"
                    or not os.access(psql, os.X_OK) or _sha256(psql) != self.entry["psql_sha256"]):
                raise _Stop("psql does not match its pinned checksum")
        except OSError:
            raise _Stop("a pinned executable is missing") from None
        kind, code, stdout, _ = _run([executable, "--version"], self._environment("api"), 10, 4096)
        found = re.findall(r"\d+\.\d+\.\d+", stdout)
        if kind != "exit" or code != 0 or not found or found[0] not in SUPPORTED_CLI_VERSIONS:
            raise _Stop("the Seren CLI version is not a supported contract")
        self.executable, self._checked = executable, True

    def _argv(self, key):
        argv = []
        for token in expected_actions(self.name)[key]:
            if isinstance(token, dict) and "$bind" in token:
                value = self.state["bindings"].get(token["$bind"])
                if not isinstance(value, str):
                    raise _Stop("binding " + token["$bind"] + " is not established")
                argv.append(value)
            elif isinstance(token, dict):
                argv.append(self.manifest["statements"][token["$statement"]])
            else:
                argv.append(token)
        return argv

    def _display(self, key, argv):
        statement = next((t["$statement"] for t in expected_actions(self.name)[key]
                          if isinstance(t, dict) and "$statement" in t), None)
        if statement is None:
            return ["seren", *argv]
        text = self.manifest["statements"][statement]
        return ["seren", *argv[:-1], "<statement " + statement + " sha256 "
                + hashlib.sha256(text.encode()).hexdigest()[:16] + ">"]

    def _window(self, role):
        now = datetime.now(timezone.utc)
        auth = self.auth
        if role == "cleanup":
            end = _when(auth.get("cleanup_expires_at") or auth["expires_at"])
            if now >= end:
                raise _Stop("the cleanup window has ended")
            return
        if now >= _when(auth["expires_at"]):
            raise _Stop("the authorization window has ended")
        started = _when(self.state["setup_started_at"])
        if now >= started + timedelta(minutes=self.limits["setup_deadline_minutes"]):
            raise _Stop("the setup deadline has passed")

    def _invoke(self, key, role, prepare=None):
        """Reserve, mark in flight (durably), run once and record. Returns the entry plus the
        raw process result, which is never saved. `prepare` changes state only after every
        check passed, in the same durable save as the reservation."""
        self._window(role)
        counts, reserved = self.state["invocations"], self.state["reserved"]
        if counts[key] >= MAX[key]:
            raise _Stop("action " + key + " reached its invocation limit")
        if key == "R1" and role == "ordinary" and counts["R1"] >= 1:
            raise _Stop("the second recovery listing is reserved for cleanup")
        if role == "ordinary" and reserved["ordinary"] + COST[key] > MAX_REQUESTS - CLEANUP_RESERVE:
            raise _Stop("ordinary work would use the cleanup reserve")
        if reserved["ordinary"] + reserved["cleanup"] + COST[key] > MAX_REQUESTS:
            raise _Stop("the service-request budget is exhausted")
        self._preflight()
        argv = self._argv(key)
        counts[key] += 1
        reserved[role] += COST[key]
        entry = {"action": key, "attempt": counts[key], "role": role, "status": "in_flight",
                 "reserved_requests": COST[key], "reserved_at": utc_now(),
                 "argv": self._display(key, argv)}
        self.state["actions"].append(entry)
        if prepare:
            prepare()
        self.save()  # durable before the request can be issued
        mode = next(a["mode"] for a in self.package["actions"] if a["id"] == key)
        timeout = self.limits["psql_invocation_timeout_seconds" if mode.startswith("psql")
                              else "api_invocation_timeout_seconds"]
        try:
            result = _run([self.executable, *argv], self._environment(mode), timeout,
                          self.limits["output_cap_bytes"])
        except BaseException:
            entry.update(status="uncertain", outcome="interrupted", finished_at=utc_now())
            self.save()
            raise
        kind, code, _, stderr = result
        category, http = _category(stderr) if kind == "exit" and code else (None, None)
        entry.update(finished_at=utc_now(), process=kind, exit_code=code, category=category,
                     http_status=http)
        return entry, result

    def _record(self, entry, status, outcome, retained=None):
        entry.update(status=status, outcome=outcome)
        if retained is not None:
            name = "%02d-%s-%d.json" % (len(self.state["actions"]), entry["action"],
                                        entry["attempt"])
            path = private_directory(self.directory / "evidence") / name
            _write_private(path, json.dumps(dict(entry, retained=retained), indent=2) + "\n")
            entry["evidence"] = str(path)
            entry["evidence_sha256"] = _sha256(path)
        self.save()
        return entry

    # Parsing -------------------------------------------------------------------------
    def _project(self, value):
        """Validated identity/configuration fields of a top-level project object."""
        if not isinstance(value, dict):
            return None
        kept = {}
        for key in PROJECT_FIELDS:
            item = value.get(key)
            if item is None:
                continue
            ok = ((key in ("id", "organization_id", "default_branch_id")
                   and isinstance(item, str) and _UUID.fullmatch(item))
                  or (key in ("created_at", "deleted_at") and isinstance(item, str)
                      and _INSTANT.fullmatch(item))
                  or (key == "name" and item == self.name)
                  or (key == "region" and item == "us-east-1")
                  or (key in FLAGS and isinstance(item, bool))
                  or (key.startswith("compute_unit") and type(item) is int)
                  or (key == "postgres_version" and isinstance(item, str)
                      and re.fullmatch(r"[0-9.]{1,12}", item)))
            if ok:
                kept[key] = item
        return kept if "id" in kept else None

    @staticmethod
    def _list(stdout):
        try:
            payload = json.loads(stdout)
        except ValueError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return None
        pagination = payload.get("pagination")
        if pagination is not None and (not isinstance(pagination, dict)
                                       or pagination.get("has_more") is not False):
            return None
        return payload["data"]

    def _parse_create(self, stdout):
        """(project fields, problem). Exact pinned format: banner, empty line, then the
        top-level ProjectCreated object (projects.rs create; print_json(&project.data))."""
        lines = _ANSI.sub("", stdout).split("\n")
        if len(lines) < 3 or lines[0] != CREATE_BANNER or lines[1] != "":
            return None, "banner mismatch"
        try:
            value = json.loads("\n".join(lines[2:]))
        except ValueError:
            return None, "not one JSON object"
        project = self._project(value)
        if project is None or not isinstance(value, dict) or value.get("name") != self.name:
            return None, "no project identity for this package"
        expected = dict(self.package["project"]["explicit_flags"], name=self.name,
                        region="us-east-1")
        if any(project.get(k) != v for k, v in expected.items()) or \
                "organization_id" not in project or "created_at" not in project:
            return project, "configuration differs from the request"
        return project, None

    # Setup ---------------------------------------------------------------------------
    def _entries(self, key):
        return [e for e in self.state["actions"] if e["action"] == key]

    def _create(self):
        def obligation():
            self.state["obligation"] = {"status": "open", "project_id": None,
                                        "name": self.name, "since": utc_now(),
                                        "reason": "creation requested; ID not yet known"}
        entry, (kind, code, stdout, _) = self._invoke("A1", "ordinary", obligation)
        if kind == "exit" and code == 0:
            project, problem = self._parse_create(stdout)
            if project is not None:
                self._bind_project(project)
                self._record(entry, "done" if problem is None else "failed",
                             problem or "created", project)
                if problem:
                    raise _Stop("create returned " + problem)
                return
            self._record(entry, "uncertain", problem, {})
            return
        if (kind == "exit" and entry["category"] == "create_failed"
                and entry["http_status"] is not None and 400 <= entry["http_status"] < 500):
            self.state["obligation"].update(status="closed", closed_at=utc_now(),
                                            reason="the service refused creation (HTTP "
                                            + str(entry["http_status"]) + ")")
            self._record(entry, "rejected", "refused by the service", {})
            return
        self._record(entry, "uncertain", kind if kind != "exit" else "error: "
                     + str(entry["category"]), {})

    def _bind_project(self, project):
        """Bind the project whose identity the create response itself established. This
        is the only source of deletion authority."""
        self.state["bindings"].update({"project.id": project["id"],
                                       "project.organization_id":
                                       project.get("organization_id"),
                                       "project.default_branch_id":
                                       project.get("default_branch_id"),
                                       "project.source": CREATE_RESPONSE})
        self.state["obligation"].update(project_id=project["id"], provenance=CREATE_RESPONSE,
                                        reason="project " + project["id"]
                                        + " identified by the create response")

    def _recover(self, role):
        """One evidence-only recovery listing after an uncertain create. A matching name and
        creation time cannot show which attempt created a project, so nothing is bound,
        written, registered or deleted; candidates are recorded and the obligation stays
        unresolved without an ID."""
        earlier = self._entries("R1")
        spacing = self.limits["recovery_list_spacing_seconds"]
        if earlier and datetime.now(timezone.utc) < _when(
                earlier[-1].get("finished_at") or earlier[-1]["reserved_at"]) + timedelta(
                seconds=spacing):
            raise _Stop("the next recovery listing must wait " + str(spacing) + " s")
        create = self._entries("A1")[0]
        entry, (kind, code, stdout, _) = self._invoke("R1", role)
        listed, complete = None, False
        if kind == "exit" and code == 0:
            try:
                payload = json.loads(stdout)
            except ValueError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                listed = payload["data"]
                complete = self._list(stdout) is not None
        if listed is None:
            self._record(entry, "uncertain", "no project list", {"ownership": UNPROVEN})
            self._unresolved("creation outcome unknown for " + self.name + ": the "
                             "recovery list was unavailable; " + NEEDED)
            return
        low = _when(create["reserved_at"]) - timedelta(minutes=5)
        high = _when(entry["reserved_at"]) + timedelta(minutes=1)
        candidates = []
        for item in listed:
            project = self._project(item) if isinstance(item, dict) and \
                item.get("name") == self.name else None
            if item is not None and isinstance(item, dict) and item.get("name") == self.name:
                try:
                    inside = bool(project) and low <= _when(project.get("created_at", "")) <= high
                except ValueError:
                    inside = False
                candidates.append({"id": project["id"] if project else None,
                                   "created_at": project.get("created_at") if project else None,
                                   "deleted": bool(item.get("deleted_at")),
                                   "near_attempt_time": inside})
        retained = {"list_complete": complete, "same_name_candidates": candidates,
                    "ownership": UNPROVEN}
        self._record(entry, "done", "evidence only; ownership unproven", retained)
        self.state["obligation"]["candidates"] = candidates
        listed_text = ("no same-name project listed" if not candidates else
                       str(len(candidates)) + " same-name project(s) listed ("
                       + ", ".join(str(c["id"]) + " created " + str(c["created_at"])
                                   for c in candidates) + ")")
        self._unresolved("creation outcome unknown for " + self.name + ": "
                         + listed_text + (" in a complete list" if complete else
                                          " in an incomplete list")
                         + "; a name and time match does not show this attempt "
                         "created it, so nothing was bound or deleted; " + NEEDED)

    def _unresolved(self, reason):
        self.state["obligation"].update(status="unresolved", reason=reason)
        self.state["blocker"] = reason
        self.save()
        return False

    def _bind_branch(self):
        entry, (kind, code, stdout, _) = self._invoke("A2", "ordinary")
        listed = self._list(stdout) if kind == "exit" and code == 0 else None
        b = self.state["bindings"]
        ok = (listed is not None and len(listed) == 1 and isinstance(listed[0], dict)
              and isinstance(listed[0].get("id"), str) and _UUID.fullmatch(listed[0]["id"])
              and listed[0].get("project_id") == b["project.id"]
              and listed[0].get("is_default") is True
              and listed[0].get("parent_branch_id") is None
              and b.get("project.default_branch_id") in (None, listed[0]["id"]))
        if not ok:
            self._record(entry, "failed", "branch binding rule unmet",
                         {"branches": len(listed) if listed is not None else None})
            raise _Stop("the new project does not have exactly one default branch")
        b["branch.id"] = listed[0]["id"]
        self._record(entry, "done", "branch bound", {"branch_id": listed[0]["id"]})

    def _bind_database(self):
        entry, (kind, code, stdout, _) = self._invoke("A3", "ordinary")
        listed = self._list(stdout) if kind == "exit" and code == 0 else None
        b = self.state["bindings"]
        ok = (listed is not None and len(listed) == 1 and isinstance(listed[0], dict)
              and listed[0].get("branch_id") == b["branch.id"]
              and isinstance(listed[0].get("name"), str)
              and _DATABASE.fullmatch(listed[0]["name"])
              and not listed[0]["name"].startswith("seren_"))
        if not ok:
            self._record(entry, "failed", "database binding rule unmet",
                         {"databases": len(listed) if listed is not None else None})
            raise _Stop("the new branch does not have exactly one usable database")
        b["database.name"] = listed[0]["name"]
        self._record(entry, "done", "database bound", {"database": listed[0]["name"]})

    def _write(self):
        """At most one attempt can reach psql: re-attempts follow only the CLI's own
        connection-URI failure, which psql.rs reports before it spawns psql."""
        while True:
            entries = self._entries("A4")
            if entries and entries[-1]["status"] != "pre_send_failure":
                return entries[-1]["status"] == "done"
            if entries:
                if len(entries) >= MAX["A4"]:
                    raise _Stop("the fixture write was never sent (connection URI unavailable)")
                wait = (_when(entries[-1]["finished_at"])
                        + timedelta(seconds=self.limits["reattempt_delay_seconds"])
                        - datetime.now(timezone.utc)).total_seconds()
                if wait > 0:
                    time.sleep(wait)
            entry, (kind, code, stdout, _) = self._invoke("A4", "ordinary")
            if kind == "exit" and code == 0 and stdout.strip() in ("", "COMMIT"):
                self._record(entry, "done", "transaction committed", {})
            elif kind == "exit" and entry["category"] == "uri_fetch_failed" and not stdout:
                self._record(entry, "pre_send_failure",
                             "connection URI not obtained; psql was not started", {})
            elif kind == "exit" and code:
                self._record(entry, "failed", "psql may have run; outcome left to read-back",
                             {})
            else:
                self._record(entry, "uncertain", kind, {})

    def _readback(self):
        entry, (kind, code, stdout, _) = self._invoke("A5", "ordinary")
        expected = expected_readback(self.manifest, self.state["bindings"]["database.name"])
        observed = None
        if kind == "exit" and code == 0:
            try:
                observed = json.loads(stdout.strip())
            except ValueError:
                observed = None
        if observed is None or not _strict_equal(observed, expected):
            differences = sorted(k for k in expected if not isinstance(observed, dict)
                                 or not _strict_equal(observed.get(k), expected[k]))
            extra = sorted(set(observed) - set(expected)) if isinstance(observed, dict) else []
            self._record(entry, "failed", "read-back differs from the manifest",
                         {"differing": differences, "unexpected_keys": extra,
                          "observed": self.sanitizer.value(observed)})
            raise _Stop("the read-back does not match the frozen manifest")
        self._record(entry, "done", "exact match", {"observed": observed})
        return entry

    def _register(self, readback):
        b = self.state["bindings"]
        record = {"status": "registered", "registered_at": utc_now(),
                  "package": self.package["id"], **{k: self.shas[k] for k in (
                      "package_sha256", "manifest_sha256", "target_sha256")},
                  "authorization_sha256": self.state["authorization_sha256"],
                  "project": {"id": b["project.id"], "name": self.name,
                              "organization_id": b.get("project.organization_id")},
                  "branch_id": b["branch.id"], "database": b["database.name"],
                  "table": self.manifest["table"]["schema"] + "."
                  + self.manifest["table"]["name"],
                  "readback_evidence": readback["evidence"],
                  "readback_sha256": readback["evidence_sha256"],
                  "cleanup": "obligation open; run fixture cleanup after the campaign",
                  "consumers": "future campaign read cases (not implemented)"}
        _write_private(self.directory / "registration.json", json.dumps(record, indent=2) + "\n")
        self.state.update(phase="registered", registration=record)
        self.save()

    def _setup_steps(self):
        if not self._entries("A1"):
            self._create()
        create = self._entries("A1")[0]
        if create["status"] == "rejected":
            self.state["phase"] = "setup_failed"
            self.state["failure"] = "creation refused by the service"
            self.save()
            return FAILED
        if "project.id" not in self.state["bindings"]:
            if self.state["obligation"]["status"] != "unresolved":
                self._recover("ordinary")
            self.state.update(phase="setup_failed", failure="creation outcome unknown")
            self.save()
            return BLOCKED
        if self.state["bindings"].get("project.source") != CREATE_RESPONSE:
            raise _Stop("the bound project was not identified by the create response")
        if create["status"] != "done":
            raise _Stop("create returned " + str(create.get("outcome")))
        for key, step in (("A2", self._bind_branch), ("A3", self._bind_database)):
            entries = self._entries(key)
            if not entries:
                step()
            elif entries[-1]["status"] != "done":
                raise _Stop("action " + key + " did not establish its binding")
        if not self._entries("A5"):
            self._write()  # failed or uncertain writes are decided by the read-back
            self._register(self._readback())
            return OK
        raise _Stop("the read-back already ran without registering the fixture")

    # Cleanup -------------------------------------------------------------------------
    def _delete(self):
        entry, (kind, code, stdout, _) = self._invoke("C1", "cleanup")
        line = "\u2713 Project '" + self.name + "' deleted successfully!"
        if kind == "exit" and code == 0 and _ANSI.sub("", stdout).strip() == line:
            self._record(entry, "done", "delete reported success", {})
        elif kind == "exit" and entry["category"] == "get_failed" and entry["http_status"] == 404:
            self._record(entry, "done", "already absent (404 before delete)", {})
        else:
            self._record(entry, "uncertain", kind if kind != "exit" else "error: "
                         + str(entry["category"]), {})

    def _verify(self):
        """'deleted', 'present' or 'uncertain' from one project read."""
        entry, (kind, code, stdout, _) = self._invoke("C2", "cleanup")
        if kind == "exit" and code and entry["category"] == "get_failed" \
                and entry["http_status"] == 404:
            self._record(entry, "done", "deleted (404)", {})
            return "deleted"
        if kind == "exit" and code == 0:
            try:
                value = json.loads(stdout)
            except ValueError:
                value = None
            project = self._project(value) if isinstance(value, dict) else None
            if project and project["id"] == self.state["obligation"]["project_id"]:
                if project.get("deleted_at"):
                    self._record(entry, "done", "deleted (deleted_at set)", project)
                    return "deleted"
                if value.get("deleted_at") is None:
                    self._record(entry, "done", "still present", project)
                    return "present"
        self._record(entry, "uncertain", "deletion not established", {})
        return "uncertain"

    def _cleanup_steps(self):
        obligation = self.state["obligation"]
        if obligation["status"] in ("none", "closed"):
            return OK
        if obligation["project_id"] is None:
            self._recover("cleanup")  # evidence only: never binds
            return BLOCKED
        if self.state["bindings"].get("project.source") != CREATE_RESPONSE:
            # A legacy binding inferred from a listing: kept as recorded, never deleted.
            self._unresolved(
                "project " + obligation["project_id"] + " was bound from "
                + str(self.state["bindings"].get("project.source")) + ", not from this "
                "attempt's create response; ownership is unproven, so it was not deleted; "
                + NEEDED)
            return BLOCKED
        while True:
            c1, c2 = self._entries("C1"), self._entries("C2")
            if not c1:
                self._delete()
                continue
            if self.state["actions"].index(c2[-1] if c2 else c1[-1]) < \
                    self.state["actions"].index(c1[-1]) or not c2:
                self._verify()  # every delete attempt is followed by a verification
                continue
            state = {"deleted (404)": "deleted", "deleted (deleted_at set)": "deleted",
                     "still present": "present"}.get(c2[-1].get("outcome"), "uncertain")
            if state == "deleted":
                obligation.update(status="closed", closed_at=utc_now(),
                                  reason="deletion of " + obligation["project_id"] + " verified")
                self.state["blocker"] = None
                if self.state["phase"] == "registered":
                    self.state["phase"] = "cleaned"
                self.save()
                return OK
            if state == "present" and len(c1) < MAX["C1"]:
                self._delete()
            elif state == "uncertain" and len(c2) < MAX["C2"]:
                self._verify()
            else:
                return self._unresolved_cleanup()

    def _unresolved_cleanup(self):
        self._unresolved("deletion of project " + str(self.state["obligation"]["project_id"])
                         + " is not verified; remove it in Seren and record the result")
        return BLOCKED

    # Commands ------------------------------------------------------------------------
    def _begin(self, authorization_path):
        try:
            self.auth = json.loads(Path(authorization_path).read_text())
        except (OSError, ValueError, TypeError):
            raise FixtureError("cannot read the authorization") from None
        private_directory(self.directory)
        return _sha256(authorization_path)

    def setup(self, authorization_path, out):
        self.out = out
        auth_sha = self._begin(authorization_path)
        problems = check_authorization(self.auth, self.shas, "setup")
        if problems:
            raise FixtureError("; ".join(problems))
        self._checked_preflight()
        lock = self._lock()
        try:
            self._load_state(auth_sha)
            if self.state["phase"] == "registered":
                self.say("Fixture already registered; nothing requested.")
                return OK
            if self.state["phase"] != "new" and self.state["phase"] != "setting_up":
                self.say("Setup was already attempted (" + self.state["phase"] + "); use "
                         "fixture cleanup or status.")
                return INVALID
            if self.state["setup_started_at"] is None:
                self.state.update(setup_started_at=utc_now(), phase="setting_up")
                self.save()
            try:
                code = self._setup_steps()
            except _Stop as stop:
                self.state.update(phase="setup_failed", failure=str(stop))
                self.save()
                code = self._safe_cleanup()
                code = FAILED if code == OK else code
            self.say(self.summary())
            return code
        finally:
            lock.unlink()

    def _checked_preflight(self):
        try:
            self._preflight()
        except _Stop as stop:
            raise FixtureError(str(stop)) from None

    def _safe_cleanup(self):
        try:
            return self._cleanup_steps()
        except _Stop as stop:
            obligation = self.state["obligation"]
            if obligation["status"] == "unresolved":  # keep what is already known
                self._unresolved(obligation["reason"].split("; cleanup stopped:")[0]
                                 + "; cleanup stopped: " + str(stop))
            elif obligation["status"] == "open":
                self._unresolved("cleanup stopped: " + str(stop))
            return BLOCKED

    def cleanup(self, authorization_path, out):
        self.out = out
        auth_sha = self._begin(authorization_path)
        problems = check_authorization(self.auth, self.shas, "cleanup")
        if problems:
            raise FixtureError("; ".join(problems))
        self._checked_preflight()
        lock = self._lock()
        try:
            self._load_state(auth_sha)
            code = self._safe_cleanup()
            self.say(self.summary())
            return code
        finally:
            lock.unlink()

    def status(self, out):
        self.out = out
        self._load_state(None)
        self.say(self.summary())
        return OK if self.state["obligation"]["status"] in ("none", "closed") or \
            self.state["phase"] == "registered" else BLOCKED

    def summary(self):
        s, o = self.state, self.state["obligation"]
        reserved = s["reserved"]["ordinary"] + s["reserved"]["cleanup"]
        lines = ["Fixture " + s["package"] + ": " + s["phase"],
                 "Cleanup obligation: " + o["status"] + (" (project " + o["project_id"] + ")"
                                                         if o.get("project_id") else "")
                 + (": " + o["reason"] if o.get("reason") else ""),
                 "Reserved service requests: " + str(reserved) + " of " + str(MAX_REQUESTS)
                 + " (cleanup reserve " + str(CLEANUP_RESERVE) + ")"]
        lines += ["  " + e["action"] + "#" + str(e["attempt"]) + " " + e["status"] + ": "
                  + str(e.get("outcome")) for e in s["actions"]]
        if s.get("failure"):
            lines.append("Setup failure: " + s["failure"])
        if s.get("blocker"):
            lines.append("Blocker: " + s["blocker"])
        if s.get("registration"):
            lines.append("Registered: " + str(self.directory / "registration.json"))
        return "\n".join(lines)


def run(args, out=None):
    out = out or sys.stdout
    try:
        fixture = Fixture(args.package, args.manifest, args.home, args.root)
        if args.action == "status":
            return fixture.status(out)
        if not args.authorization:
            raise FixtureError("setup and cleanup need --authorization")
        return getattr(fixture, args.action)(args.authorization, out)
    except FixtureError as error:
        print("Refused (nothing requested): " + str(error), file=out)
        return INVALID
