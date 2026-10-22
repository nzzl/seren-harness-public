"""SYNTHETIC stand-in for the pinned Seren CLI 0.10.0 fixture commands, for offline tests.

It follows the pinned source's observable behaviour: `projects create` prints a banner, an
empty line and the top-level project object; list commands print the data wrapper; `projects
delete -y` reads then deletes and prints a text line; `psql` fetches a connection URI before it
starts psql, and errors print as `Error: <message>` with exit 1. The write statement's VALUES
are parsed and applied, so the read-back reflects what the adapter actually sent. Every service
request is appended to calls.jsonl. Scenario and world state live in XDG_CONFIG_HOME.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

HOME = Path(os.environ["XDG_CONFIG_HOME"])
SCENARIO = json.loads((HOME / "scenario.json").read_text())
WORLD_PATH = HOME / "world.json"
WORLD = json.loads(WORLD_PATH.read_text()) if WORLD_PATH.exists() else {
    "projects": [], "tables": {}, "counters": {}}
CANARY = SCENARIO.get("canary", "")
FLAGS = ("block-public-connections", "block-vpc-connections", "hipaa",
         "protected-branches-only", "enable-logical-replication")
COLUMNS = [["invoice_key", "text", "NO"], ["customer", "text", "NO"], ["status", "text", "NO"],
           ["amount_cents", "integer", "NO"], ["due_date", "date", "YES"], ["note", "text", "YES"]]
DDL = ("CREATE TABLE harness_fixture.invoices (invoice_key text PRIMARY KEY, customer text NOT "
       "NULL, status text NOT NULL CHECK (status IN ('open', 'paid', 'void')), amount_cents "
       "integer NOT NULL, due_date date, note text)")


def save():
    WORLD_PATH.write_text(json.dumps(WORLD))


def log(request):
    with open(HOME / "calls.jsonl", "a") as stream:
        stream.write(json.dumps({"request": request}) + "\n")


def step(name, default="ok"):
    """The scenario behaviour for this invocation of `name` (a list is consumed in order)."""
    value = SCENARIO.get(name, default)
    n = WORLD["counters"].get(name, 0)
    WORLD["counters"][name] = n + 1
    save()
    return value[min(n, len(value) - 1)] if isinstance(value, list) else value


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fail(message, status=None):
    sys.stderr.write("Error: " + message + ("" if status is None else
                     ": Unexpected Response: Response { url: \"https://api.example/x\", "
                     "status: " + str(status) + ", headers: {} }") + "\n")
    sys.exit(1)


def stall():
    time.sleep(SCENARIO.get("stall_seconds", 20))


def find(project_id):
    return next((p for p in WORLD["projects"] if p["id"] == project_id), None)


def create(args):
    expected = ["--name", args[1], "--region", "us-east-1"]
    for flag in FLAGS:
        expected += ["--" + flag, "false"]
    if args[:1] != ["--name"] or args != expected + ["-o", "json"]:
        fail("stand-in: unexpected create arguments")
    behaviour = step("create")
    log("POST /projects")
    if behaviour == "timeout_before":
        stall()
        return
    if behaviour == "reject_400":
        sys.stderr.write("Error: Failed to create project: Error Response: status: 400 Bad "
                         "Request; headers: {}; value: quota\n")
        sys.exit(1)
    project = {"id": str(uuid.uuid4()), "name": args[1], "organization_id": str(uuid.uuid4()),
               "region": "us-east-1", "tenant_id": str(uuid.uuid4()), "created_at": now(),
               "block_public_connections": False, "block_vpc_connections": False,
               "hipaa": behaviour == "config_mismatch", "protected_branches_only": False,
               "compute_unit_min": 1, "compute_unit_max": 4, "branch_count": 1,
               "default_branch_id": str(uuid.uuid4()), "enable_logical_replication": False,
               "is_service_project": False, "postgres_version": "17", "storage_bytes": 0,
               "deleted_at": None}
    WORLD["projects"].append(project)
    save()
    shown = {k: v for k, v in project.items() if k != "deleted_at"}
    if CANARY:
        shown["connection_hint"] = "postgres://owner:" + CANARY + "@ep.example/serendb"
    if behaviour == "error_503":
        fail("Failed to create project", 503)
    banner = "✓ Project created successfully!"
    if behaviour == "timeout_after":
        stall()
    elif behaviour == "wrapped":
        print(banner + "\n\n" + json.dumps({"data": shown}, indent=2))
    elif behaviour == "malformed":
        print(banner + "\n\n{\"id\": ")
    elif behaviour == "banner_mismatch":
        print("Project created\n\n" + json.dumps(shown, indent=2))
    elif behaviour == "huge":
        print(banner + "\n\n" + json.dumps(dict(shown, padding="x" * 50000), indent=2))
    else:
        print(banner + "\n\n" + json.dumps(shown, indent=2))


def listing():
    log("GET /projects")
    hidden = SCENARIO.get("list_hide", False)
    data = [] if hidden else [p for p in WORLD["projects"]]
    data += SCENARIO.get("list_extra", [])
    pagination = ({"total": len(data) + 5, "count": len(data), "limit": len(data), "offset": 0,
                   "has_more": True} if SCENARIO.get("list_has_more") else None)
    print(json.dumps({"data": data, "pagination": pagination}, indent=2))


def branches(args):
    if len(args) != 5 or args[0] != "--project-id" or args[2:] != ["list", "-o", "json"]:
        fail("stand-in: unexpected branches arguments")
    log("GET /projects/{id}/branches")
    project = find(args[1])
    if project is None:
        fail("Failed to list branches", 404)
    branch = {"id": project["default_branch_id"], "name": "main", "project_id": project["id"],
              "timeline_id": str(uuid.uuid4()), "created_by": str(uuid.uuid4()),
              "created_at": project["created_at"], "updated_at": project["created_at"],
              "protected": False, "archived": False, "is_default": True,
              "parent_branch_id": None, "logical_size_bytes": 0}
    data = [branch]
    if SCENARIO.get("branches") == "two":
        data.append(dict(branch, id=str(uuid.uuid4()), is_default=False,
                         parent_branch_id=branch["id"]))
    print(json.dumps({"data": data}, indent=2))


def databases(args):
    if (len(args) != 7 or args[0] != "--project-id" or args[2] != "--branch-id"
            or args[4:] != ["list", "-o", "json"]):
        fail("stand-in: unexpected databases arguments")
    log("GET /projects/{id}/branches/{bid}/databases")
    entry = {"id": str(uuid.uuid4()), "name": "serendb", "branch_id": args[3],
             "owner_name": "serendb_owner", "created_at": now(), "updated_at": now()}
    data = [entry]
    if SCENARIO.get("databases") == "two":
        data.append(dict(entry, id=str(uuid.uuid4()), name="other"))
    elif SCENARIO.get("databases") == "service":
        data = [dict(entry, name="seren_internal")]
    print(json.dumps({"data": data}, indent=2))


def literal(text, i):
    if text.startswith("NULL", i):
        return None, i + 4
    if text.startswith("DATE '", i):
        end = text.index("'", i + 6)
        return text[i + 6:end], end + 1
    if text[i] == "'":
        out, j = [], i + 1
        while True:
            if text[j] == "'" and text[j + 1:j + 2] == "'":
                out.append("'")
                j += 2
            elif text[j] == "'":
                return "".join(out), j + 1
            else:
                out.append(text[j])
                j += 1
    match = re.compile(r"-?\d+").match(text, i)
    return int(match.group(0)), match.end()


def apply_write(sql, project_id):
    """Parse and apply the frozen write; fail like PostgreSQL on violations."""
    if not sql.startswith("BEGIN; CREATE SCHEMA harness_fixture; " + DDL + "; INSERT INTO "
                          "harness_fixture.invoices (") or not sql.endswith("; COMMIT;"):
        return "ERROR:  syntax error (stand-in: unexpected write statement)"
    if WORLD["tables"].get(project_id) is not None:
        return 'ERROR:  schema "harness_fixture" already exists'
    head, _, values = sql.partition(") VALUES ")
    columns = head.rsplit("(", 1)[1].split(", ")
    values, rows, i = values[:-len("; COMMIT;")], [], 0
    while i < len(values):
        assert values[i] == "("
        i, row = i + 1, []
        while values[i] != ")":
            item, i = literal(values, i)
            row.append(item)
            if values.startswith(", ", i):
                i += 2
        i += 1
        if values.startswith(", ", i):
            i += 2
        rows.append(dict(zip(columns, row)))
    keys = [r["invoice_key"] for r in rows]
    if len(set(keys)) != len(keys) or any(r["status"] not in ("open", "paid", "void")
                                          for r in rows):
        return "ERROR:  constraint violation"
    WORLD["tables"][project_id] = rows
    save()
    return None


def readback(project_id, database, read_only):
    rows = WORLD["tables"].get(project_id)
    value = {"database": database, "read_only": read_only,
             "columns": COLUMNS if rows is not None else None,
             "relations": ["invoices", "invoices_pkey"] if rows is not None else None,
             "rows": sorted(rows, key=lambda r: r["invoice_key"]) if rows else None}
    change = SCENARIO.get("readback", "ok")
    if rows and change == "wrong_value":
        value["rows"][0] = dict(value["rows"][0], amount_cents=12001)
    elif rows and change == "wrong_type":
        value["rows"][0] = dict(value["rows"][0], amount_cents="12000")
    elif rows and change == "bool_for_int":
        value["rows"][1] = dict(value["rows"][1], amount_cents=True)
    elif rows and change == "reorder":
        value["rows"][0], value["rows"][1] = value["rows"][1], value["rows"][0]
    elif rows and change == "missing_row":
        value["rows"] = value["rows"][:-1]
    elif rows and change == "null_as_empty":
        value["rows"] = [dict(r, note="" if r["note"] is None else r["note"])
                         for r in value["rows"]]
    elif rows and change == "extra_relation":
        value["relations"] = value["relations"] + ["leftover"]
    elif rows and change == "canary":
        value["rows"][0] = dict(value["rows"][0], note=CANARY)
    return json.dumps(value, separators=(", ", " : "))


def case_result(sql, project_id, read_only):
    """The frozen read cases, computed from the rows the write actually stored."""
    rows = WORLD["tables"].get(project_id) or []
    chosen = [r for r in rows if r["customer"] == "acme" and r["status"] == "open"]
    if "'keys'" in sql and "'total_cents'" in sql:
        key, value = "case_open_invoices", {
            "keys": sorted(r["invoice_key"] for r in chosen) or None,
            "total_cents": sum(r["amount_cents"] for r in chosen) if chosen else None}
    elif "'page'" in sql:
        page = sorted(chosen, key=lambda r: (r["due_date"] is None, r["due_date"] or "",
                                             r["invoice_key"]))[:3]
        key, value = "case_first_page_by_due_date", {"page": [
            {"invoice_key": r["invoice_key"], "due_date": r["due_date"]} for r in page] or None}
    elif "'note_is_null'" in sql:
        picked = sorted((r for r in rows if r["invoice_key"] in ("INV-03", "INV-04", "INV-09")),
                        key=lambda r: r["invoice_key"])
        key, value = "case_missing_values", {"rows": [
            {"invoice_key": r["invoice_key"], "due_date": r["due_date"], "note": r["note"],
             "note_is_null": r["note"] is None} for r in picked] or None}
    else:
        sys.stderr.write("ERROR:  stand-in: statement outside the frozen family\n"
                         "Error: psql exited with non-zero status\n")
        sys.exit(1)
    value = dict({"read_only": read_only}, **value)
    change = SCENARIO.get("cases", {}).get(key, "ok")
    if change == "reorder":
        listed = next(v for v in value.values() if isinstance(v, list))
        listed[0], listed[1] = listed[1], listed[0]
    elif change == "wrong_type":
        value["total_cents"] = str(value["total_cents"])
    elif change == "null_as_empty":
        value["rows"] = [dict(r, note="" if r["note"] is None else r["note"])
                         for r in value["rows"]]
    elif change == "missing_field":
        value.pop(next(k for k in value if k != "read_only"))
    elif change == "extra_field":
        value["extra"] = True
    elif change == "canary":
        value["leak"] = CANARY
    elif change == "error":
        sys.stderr.write("ERROR:  relation does not exist" + (" " + CANARY if CANARY else "")
                         + "\nError: psql exited with non-zero status\n")
        sys.exit(1)
    elif change == "not_json":
        return "(1 row)"
    elif change == "huge":
        value["padding"] = "x" * 70000
    return json.dumps(value, separators=(", ", " : "))


def psql(args):
    if (len(args) != 16 or args[0] != "--project-id" or args[2] != "--branch-id"
            or args[4] != "--database" or args[6:15] != ["--ssl", "require", "--", "-X", "-w",
                                                        "-A", "-t", "--set=ON_ERROR_STOP=1",
                                                        "-c"]):
        fail("stand-in: unexpected psql arguments")
    options = os.environ.get("PGOPTIONS", "")
    read_only = "on" if "default_transaction_read_only=on" in options else "off"
    if not (os.environ.get("PGPASSFILE") == "/dev/null" and "statement_timeout=" in options):
        fail("stand-in: psql environment not isolated")
    sql, project_id = args[15], args[1]
    write = sql.startswith("BEGIN;")
    readback_sql = sql.startswith("SELECT pg_catalog.json_build_object('database', ")
    behaviour = step("write" if write else "read" if readback_sql else "case")
    log("GET /projects/{id}/connection_uri")
    if behaviour == "uri_fail":
        fail("Failed to fetch connection URI", 503)
    if behaviour == "hang_before":
        stall()
        return
    log("DB session")
    if behaviour == "conn_fail":
        sys.stderr.write('psql: error: connection to server at "ep.example" (10.0.0.1), port '
                         '5432 failed: FATAL:  password authentication failed for user "owner"'
                         + (" password=" + CANARY if CANARY else "")
                         + "\nError: psql exited with non-zero status\n")
        sys.exit(1)
    if write:
        if read_only == "on":
            error = "ERROR:  cannot execute CREATE SCHEMA in a read-only transaction"
        elif behaviour == "lost_before_commit":
            error = "server closed the connection unexpectedly"
        else:
            error = apply_write(sql, project_id)
        if behaviour == "hang_after_commit" and error is None:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            (HOME / "child.pid").write_text(str(child.pid))
            stall()
            return
        if behaviour == "lost_after_commit" and error is None:
            error = "server closed the connection unexpectedly"
        if error:
            sys.stderr.write(error + "\nError: psql exited with non-zero status\n")
            sys.exit(1)
        print("COMMIT")
        return
    if sql.startswith("SELECT pg_catalog.json_build_object('database', "):
        print(readback(project_id, args[5], read_only))
        return
    print(case_result(sql, project_id, read_only))


def delete(args):
    if len(args) != 2 or args[1] != "-y":
        fail("stand-in: unexpected delete arguments")
    log("GET /projects/{id}")
    project = find(args[0])
    if project is None or project.get("deleted_at"):
        sys.stderr.write("Error: Failed to get project: Error Response: status: 404 Not Found; "
                         "headers: {}; value: none\n")
        sys.exit(1)
    behaviour = step("delete")
    log("DELETE /projects/{id}")
    if behaviour == "error_500":
        fail("Failed to delete project", 500)
    if behaviour != "noop_ok":
        if SCENARIO.get("delete_mode", "soft") == "soft":
            project["deleted_at"] = now()
        else:
            WORLD["projects"].remove(project)
        save()
    if behaviour == "hang_after":
        stall()
        return
    print("✓ Project '" + project["name"] + "' deleted successfully!")


def get(args):
    if len(args) != 3 or args[1:] != ["-o", "json"]:
        fail("stand-in: unexpected get arguments")
    behaviour = step("get")
    log("GET /projects/{id}")
    if behaviour == "hang":
        stall()
        return
    project = find(args[0])
    if project is None:
        sys.stderr.write("Error: Failed to get project: Error Response: status: 404 Not Found; "
                         "headers: {}; value: none\n")
        sys.exit(1)
    print(json.dumps(dict(project, created_by=str(uuid.uuid4()), updated_at=now()), indent=2))


def main():
    args = sys.argv[1:]
    collector = args[:2] == ["-o", "json"] or args == ["--version"]  # the collector's path
    if args[:2] == ["-o", "json"]:
        args = args[2:]
    if os.environ.get("SEREN_API_KEY") or (not collector and os.environ.get("NO_COLOR") != "1"):
        fail("stand-in: unexpected environment")
    if args == ["--version"]:
        print("seren 0.10.0")
        return
    if args[:2] == ["projects", "create"]:
        return create(args[2:])
    if args == ["projects", "list", "-o", "json"]:
        return listing()
    if args[:1] == ["branches"]:
        return branches(args[1:])
    if args[:1] == ["databases"]:
        return databases(args[1:])
    if args[:1] == ["psql"]:
        return psql(args[1:])
    if args[:2] == ["projects", "delete"]:
        return delete(args[2:])
    if args[:2] == ["projects", "get"]:
        return get(args[2:])
    fail("stand-in: unsupported command " + " ".join(args[:2]))


main()
