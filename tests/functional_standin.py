#!/usr/bin/env python3
"""SYNTHETIC stand-in for the Seren CLI 0.10.0 commands the functional campaign uses.

Offline tests only; it proves nothing about the real service. World state, the scenario and a
log of every invocation live in XDG_CONFIG_HOME. Employee replies are scripted per run start
(`replies`, consumed in order): {"answer": text} | {"answer": text, "running_polls": n} |
{"start": "403" | "timeout" | "no_identity"} | {"status": "failed"} | {"behaviour": name} |
{"research": plan}. A research plan runs the stand-in researcher below: it "retrieves" the
plan's URLs through a simulated retrieval tool (content from `pages`, the employee's view of the
web) and answers with the plan's `answer`, or, for a revision, with `revise` edits applied to the
previous deliverable quoted in its own message. `production_runs` seeds runs this harness must
never touch.
A behaviour runs the stand-in employee below: it reads the journey table named in its message
through a simulated `seren_db_query` call (only if the deployment's catalog allows that tool)
and builds its reply from the rows it read and from the previous deliverable quoted to it.
`psql` emulates the pinned harness route over world tables; writes need the write session.
Other scenario keys:
`start`, `stop`, `apply` ("ok" | "timeout"), `stop_never` (status stays running),
`identity_change_after_start` (a new active revision after the first start),
`update_after_first_run` (deployment fields changed by its owner after the first run start).
A deployment's `omit_fields` drops those keys from managed-get (an owner view).
"""
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

HOME = Path(os.environ["XDG_CONFIG_HOME"])
SCENARIO = json.loads((HOME / "scenario.json").read_text())
WORLD_PATH = HOME / "world.json"
WORLD = json.loads(WORLD_PATH.read_text()) if WORLD_PATH.exists() else {
    "deployments": {}, "runs": {}, "starts": 0, "replies_used": 0, "onboardings": {}, "tables": {}}
WORLD.setdefault("tables", {})
for name, table in SCENARIO.get("tables", {}).items():   # Pre-existing tables someone else owns.
    WORLD["tables"].setdefault(name, dict(table))
for item in SCENARIO.get("deployments", []):
    WORLD["deployments"].setdefault(item["deployment_id"], dict(item))
for identifier, run in SCENARIO.get("production_runs", {}).items():   # Someone else's work.
    WORLD["runs"].setdefault(identifier, dict(run, polls=0))


def save():
    WORLD_PATH.write_text(json.dumps(WORLD))


def log(argv):
    with open(HOME / "calls.jsonl", "a") as stream:
        stream.write(json.dumps(argv) + "\n")


def out(value):
    print(json.dumps(value))
    save()
    sys.exit(0)


def fail(status, context="request"):
    save()
    sys.stderr.write("Error: %s: unexpected response %s Forbidden - denied\n" % (context, status))
    sys.exit(1)


def stall():
    save()
    time.sleep(SCENARIO.get("stall_seconds", 5))
    sys.exit(1)


def deployment(identifier):
    item = WORLD["deployments"].get(identifier)
    if item is None:
        save()
        sys.stderr.write("Error: get: unexpected response 404 Not Found - missing\n")
        sys.exit(1)
    return item


def detail(item):
    keys = ("deployment_id", "name", "status", "active_revision_id", "agent_identity_id",
            "secret_resolution_result_id", "approval_policy", "capability_policy",
            "runtime_policy", "side_effect_policy", "requirements")
    value = {k: item.get(k) for k in keys if k not in item.get("omit_fields", ())}   # owner view
    if "bundle" in item:   # Instruction documents, as managed-get returns them.
        value["bundle"] = item["bundle"]
    return value


# --------------------------------------------------------------------------- psql
def psql(args):
    """The harness statements, with PostgreSQL's relevant semantics: CREATE TABLE fails on an
    existing name, the table comment holds the owner, guarded blocks act only for that owner,
    and writes need a write session."""
    sql = args[-1]
    writable = "default_transaction_read_only=off" in os.environ.get("PGOPTIONS", "")
    mode = SCENARIO.get("psql", {})
    tables = WORLD["tables"]

    def rows_literal():
        match = re.search(r"NULL::[a-z0-9_.]+, '((?:[^']|'')*)'\)", sql)
        return json.loads(match.group(1).replace("''", "'"))

    def owner_of(name):
        return tables[name].get("owner") if name in tables else None
    if sql.startswith(("DO ", "BEGIN;")) and not writable:
        psql_fail("cannot execute in a read-only transaction")
    if sql.startswith("DO $harness$"):
        token = re.search(r"pg_class'\) = '([^']*)' THEN", sql).group(1)
        table = re.search(r"to_regclass\('([a-z0-9_.]+)'\)", sql).group(1)
        kind = "drop" if " THEN DROP TABLE" in sql else "insert"
        if mode.get(kind) == "fail_before":
            psql_fail("connection reset")
        if table in tables and owner_of(table) == token:
            if kind == "drop":
                tables.pop(table)
            else:
                tables[table]["rows"].extend(rows_literal())
        if mode.get(kind) == "fail_after":
            psql_fail("server closed the connection unexpectedly")
        if kind == "drop":
            out_raw({"exists": table in tables, "owner": owner_of(table)})
        if table not in tables:
            psql_fail('relation "%s" does not exist' % table)
        out_raw({"rows": len(tables[table]["rows"]), "owner": owner_of(table)})
    if sql.startswith("BEGIN;"):
        table = re.search(r"CREATE TABLE ([a-z0-9_.]+) ", sql).group(1)
        if mode.get("create") == "fail_before":
            psql_fail("connection reset")
        if table in tables:
            psql_fail('relation "%s" already exists' % table)
        tables[table] = {"rows": rows_literal(),
                         "owner": re.search(r"COMMENT ON TABLE [a-z0-9_.]+ IS '([^']*)'", sql).group(1)}
        if mode.get("create") == "fail_after":
            psql_fail("server closed the connection unexpectedly")
        out_raw({"rows": len(tables[table]["rows"]), "owner": owner_of(table)})
    if sql.startswith("SELECT json_build_object('database', current_database()"):
        out_raw(dict({"database": args[args.index("--database") + 1],
                      "read_only": "off" if writable else "on", "schema_exists": False,
                      "database_create": True, "schema_create": None, "comment_function": True},
                     **SCENARIO.get("db_readiness", {})))
    if "'exists', to_regclass(" in sql:
        table = re.search(r"to_regclass\('([a-z0-9_.]+)'\)", sql).group(1)
        out_raw({"exists": table in tables, "owner": owner_of(table)})
    read = re.search(r"ORDER BY r\.([a-z_]+)\), '\[\]'::json\) FROM ([a-z0-9_.]+) r\)", sql)
    if read:
        if mode.get("read") == "fail":
            psql_fail("connection timed out")
        key, table = read.groups()
        if table not in tables:
            psql_fail('relation "%s" does not exist' % table)
        out_raw({"owner": owner_of(table), "rows": sorted(tables[table]["rows"], key=lambda r: r[key])})
    psql_fail("stand-in does not understand this statement")


def out_raw(value):
    print(json.dumps(value))
    save()
    sys.exit(0)


def psql_fail(message):
    save()
    sys.stderr.write("psql: error: %s\nError: psql exited with non-zero status\n" % message)
    sys.exit(1)


# --------------------------------------------------------------------------- stand-in employee
REGIONS = ("East", "North", "South", "West")
KEYS = ("permit", "volunteer", "kit", "schedule", "training")


def business_days(start, count):
    day = start
    while count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count -= 1
    return day


def employee(message, behaviour, tools):
    """Events for one run: tool calls on the table named in the message, then the reply."""
    events = []
    found = re.search(r"database table ([a-z_][a-z0-9_]*\.[a-z0-9_]+)", message)
    table = found.group(1) if found else None
    previous = re.search(r"=== YOUR PREVIOUS DELIVERABLE ===\n(.*?)\n=== END", message, re.S)
    previous = json.loads(previous.group(1)) if previous else None

    def query(sql):
        events.append({"type": "tool_call", "id": "call-%d" % len(events), "name": "seren_db_query",
                       "arguments": json.dumps({"query": sql}), "status": "completed"})
        if behaviour == "tool_error":
            events.append({"type": "tool_result", "id": events[-1]["id"], "is_error": True,
                           "content": "permission denied for table " + str(table)})
            return None
        source = re.search(r"FROM ([a-z0-9_.]+)", sql).group(1)
        rows = WORLD["tables"].get(source, {}).get("rows", [])
        if " GROUP BY region" in sql:   # The aggregate the analyst would actually write.
            chosen = [r for r in rows if r["quarter"] == "Q3" and r["status"] == "paid"
                      and not (" NOT test_account" in sql and r["test_account"])]
            groups = sorted({r["region"] for r in chosen})
            result = {"columns": ["region", "total"], "row_count": len(groups),
                      "rows": [[g, sum(r["amount"] for r in chosen if r["region"] == g)] for g in groups]}
            events.append({"type": "tool_result", "id": events[-1]["id"], "is_error": False,
                           "content": json.dumps(result)})
            return [{"region": g, "total": t} for g, t in result["rows"]]
        columns = sorted(rows[0]) if rows else []
        events.append({"type": "tool_result", "id": events[-1]["id"], "is_error": False,
                       "content": json.dumps({"columns": columns, "row_count": len(rows),
                                              "rows": [[r[c] for c in columns] for r in rows]})})
        return rows

    def done(value):
        text = value if isinstance(value, str) else json.dumps(value)
        return events + [{"type": "text", "text": text}, {"type": "done", "status": "ok"}]
    if behaviour == "no_tool_claim":
        if "orders" in (table or ""):
            return done({"regions": {"East": 1240, "North": 980, "South": 1515, "West": 760},
                         "total": 4495, "query": "SELECT region, sum(amount) FROM %s GROUP BY region" % table,
                         "note": "All figures verified against the database."})
        return done({"owner": "Project owner", "deadline": "2026-05-01", "steps": [], "total_cost": 0,
                     "risks": ["Budget"], "note": "Plan confirmed against the documents."})
    if "seren_db_query" not in tools or table is None:
        return done("I cannot read the database: no database tool is available to me.")
    revision = "test_account = true" in message
    if behaviour == "aggregate" and "orders" in table:
        groups = query("SELECT region, sum(amount) AS total FROM %s WHERE quarter = 'Q3' AND "
                       "status = 'paid'%s GROUP BY region" % (table, " AND NOT test_account" if revision else ""))
        regions = {g: next((r["total"] for r in groups if r["region"] == g), 0) for g in REGIONS}
        value = {"regions": regions, "total": sum(regions.values()), "query": json.loads(events[-2]["arguments"])["query"]}
        if revision:
            value.update(previous_total=previous["total"], changed_regions=sorted(
                g for g in REGIONS if previous["regions"].get(g) != regions[g]))
        return done(value)
    source = SCENARIO.get("wrong_source", table) if behaviour == "wrong_source" else table
    rows = query("SELECT * FROM %s" % source)
    if rows is None:
        return done("I could not read the data because the query was refused.")
    if "orders" in table:
        return done(report(rows, previous, behaviour, revision, table))
    return done(plan(rows, previous, behaviour, message))


def report(rows, previous, behaviour, revision, table):
    statuses = ("paid", "refunded") if behaviour == "wrong_filter" else ("paid",)
    chosen = [r for r in rows if r["quarter"] == "Q3" and r["status"] in statuses
              and not (revision and r["test_account"])]
    regions = {g: sum(r["amount"] for r in chosen if r["region"] == g) for g in REGIONS}
    value = {"regions": regions, "total": sum(regions.values()),
             "query": "SELECT region, sum(amount) FROM %s WHERE quarter = 'Q3' AND status = 'paid'%s "
                      "GROUP BY region" % (table, " AND NOT test_account" if revision else "")}
    if behaviour == "wrong_filter":
        value["note"] = "All figures verified against the database."
    if revision:
        if behaviour == "ignore_prior" or previous is None:
            value.update(previous_total=value["total"], changed_regions=[])
        else:
            value.update(previous_total=previous["total"], changed_regions=sorted(
                g for g in REGIONS if previous["regions"].get(g) != regions[g]))
    return value


def plan(rows, previous, behaviour, message):
    project = re.search(r"documents for project '([^']+)'", message).group(1)
    docs = [r for r in rows if r["project"] == project]
    notes = next(d for d in docs if "Agreed workstreams" in d["body"])
    quotes_doc = next(d for d in docs if "Quoted costs" in d["body"])
    owner = re.search(r"Owner: ([^.]+)\.", notes["body"]).group(1)
    meeting = date.fromisoformat(re.search(r"Meeting on (\d{4}-\d{2}-\d{2})", notes["body"]).group(1))
    days = int(re.search(r"due (\d+) business days", " ".join(d["body"] for d in docs)).group(1))
    quotes = {k: int(v) for k, v in re.findall(r"(\w+) (\d+) credits", quotes_doc["body"])}
    wording = re.search(r"Agreed workstreams: (.*)\.$", notes["body"]).group(1).split("; ")
    steps = [{"action": w[0].upper() + w[1:], "doc": notes["doc_id"],
              "cost": quotes[next(k for k in KEYS if k in w)]} for w in wording]
    if behaviour == "irrelevant_plan":
        steps = [{"action": a, "doc": notes["doc_id"], "cost": 0}
                 for a in ("Eat breakfast", "Take a nap", "Watch television")]
    value = {"owner": owner, "deadline": business_days(meeting, days).isoformat(), "steps": steps,
             "total_cost": sum(s["cost"] for s in steps),
             "risks": ["Spend could exceed the project budget"]}
    decision = next((d for d in docs if "Decision update" in d["title"]), None)
    if previous is not None and decision is not None:
        dropped = re.search(r"drop the (\w+) workstream", decision["body"]).group(1)
        kept = [s for s in previous["steps"] if dropped not in s["action"].lower()]
        value.update(steps=kept, total_cost=sum(s["cost"] for s in kept),
                     removed_steps=[s["action"] for s in previous["steps"] if dropped in s["action"].lower()],
                     decision_doc=decision["doc_id"])
        if behaviour == "ignore_prior":
            value.update(steps=steps, removed_steps=[], total_cost=sum(s["cost"] for s in steps))
    return value


def researcher(message, plan, tools):
    """Retrieval tool calls, then the plan's reply. A revision is built from the previous
    deliverable in the message, so a missing or different quote changes the answer."""
    context = json.loads(message)["task"]["context"]
    events = []
    tool = plan.get("tool", "browser_navigate")
    for url in plan.get("retrieve", []):
        events.append({"type": "tool_call", "id": "call-%d" % len(events), "name": tool,
                       "arguments": json.dumps({"url": url}), "status": "completed"})
        page = SCENARIO.get("pages", {}).get(url) if tool in tools else None
        events.append({"type": "tool_result", "id": events[-1]["id"], "is_error": page is None,
                       "content": page if page is not None else "error: not retrieved"})
    if "answer" in plan:
        value = plan["answer"]
    else:
        value = json.loads(json.dumps(context["previousDeliverable"]))
        edits = plan["revise"]
        value["claims"] = [edits.get("replace", {}).get(c["id"], c) for c in value["claims"]
                           if c["id"] not in edits.get("remove", [])] + edits.get("add", [])
        value["unknowns"] += edits.get("unknowns", [])
        for key in ("priority", "changes"):
            value[key] = edits.get(key, [])
    text = value if isinstance(value, str) else json.dumps(value)
    if plan.get("no_trace"):   # Only the managed-runtime envelope, no event trace.
        return None, json.dumps({"response": text, "inference_cost_atomic": 1, "model_calls": []})
    return events + [{"type": "text", "text": text}, {"type": "done", "status": "ok"}], None


def main(args):
    log(args)
    if args == ["--version"]:
        print("seren 0.10.0")
        return
    if args[:2] == ["-o", "json"]:
        args = args[2:]
    if args[:1] == ["psql"]:
        psql(args)
    if args[-2:] == ["-o", "json"]:
        args = args[:-2]
    if args[:2] == ["agent", "managed-get"]:
        out({"data": detail(deployment(args[2]))})
    if args[:2] == ["agent", "managed-deployment-tools"]:
        item = deployment(args[2])
        approval = SCENARIO.get("tool_approval", "none")
        tools = SCENARIO.get("catalog_tools")   # Raw entries in catalog order (duplicates allowed).
        if tools is None:
            tools = [{"name": name, "effective_policy": {"status": "allowed"}, "approval_type": approval,
                      "checkpoint_required": False, "side_effecting": False} for name in item.get("tools", [])]
        out({"data": {"deployment_id": SCENARIO.get("catalog_deployment_id", item["deployment_id"]),
                      "tools": tools, "tool_presets": [], "approval_policy": "read_only"}})
    if args[:4] == ["agent", "cloud", "deployment", "status"]:
        item = deployment(args[4])
        out({"data": {"id": item["deployment_id"], "name": item["name"], "status": item["status"],
                      "desired_lifecycle_state": item.get("desired", item["status"])}})
    if args[:2] == ["agent", "managed-start"]:
        if SCENARIO.get("start") == "timeout":
            stall()
        item = deployment(args[2])
        item.update(status="running", desired="running")
        WORLD["starts"] += 1
        if SCENARIO.get("identity_change_after_start") and WORLD["starts"] == 1:
            item["active_revision_id"] = str(uuid.uuid4())
        out({"data": {"id": item["deployment_id"], "status": "running"}})
    if args[:2] == ["agent", "managed-stop"]:
        item = deployment(args[2])
        if SCENARIO.get("stop") == "timeout":
            stall()
        if not SCENARIO.get("stop_never"):
            item.update(status="stopped", desired="stopped")
        out({"data": {"id": item["deployment_id"], "status": item["status"]}})
    if args[:3] == ["agent", "onboarding", "create"]:
        draft = json.loads(Path(args[args.index("--draft") + 1]).read_text())
        key = args[args.index("--idempotency-key") + 1]
        record = WORLD["onboardings"].setdefault(key, {"id": str(uuid.uuid4()), "revision": 1,
                                                       "status": "draft", "step": "basics",
                                                       "draft": draft})
        out(record)
    if args[:3] == ["agent", "onboarding", "update"]:
        record = next(r for r in WORLD["onboardings"].values() if r["id"] == args[3])
        record.update(step="review", revision=record["revision"] + 1)
        out(record)
    if args[:3] == ["agent", "onboarding", "preview"]:
        record = next(r for r in WORLD["onboardings"].values() if r["id"] == args[3])
        spec = dict(record["draft"]["deployment"])
        out({"data": {"onboarding_id": record["id"], "source_revision": record["revision"],
                      "desired_spec": spec}})
    if args[:3] == ["agent", "onboarding", "apply"]:
        if SCENARIO.get("apply") == "timeout":
            stall()
        record = next(r for r in WORLD["onboardings"].values() if r["id"] == args[3])
        identifier = record.setdefault("deployment_id", str(uuid.uuid4()))
        WORLD["deployments"].setdefault(identifier, {
            "deployment_id": identifier, "name": record["draft"]["deployment"]["name"],
            "status": "running", "desired": "running", "active_revision_id": str(uuid.uuid4()),
            "agent_identity_id": str(uuid.uuid4()), "secret_resolution_result_id": None,
            "approval_policy": "read_only", "tools": []})
        out(dict(record, application={"deployment_outcome": "confirmed",
                                      "source_revision": record["revision"]},
                 operations={"deployment_id": identifier}))
    if args[:4] == ["agent", "cloud", "run", "start"]:
        target = args[args.index("--deployment-id") + 1]
        message = args[args.index("--message") + 1]
        replies = SCENARIO.get("replies", [])
        index = WORLD["replies_used"]
        WORLD["replies_used"] += 1
        reply = replies[index] if index < len(replies) else {"answer": "no scripted reply"}
        if reply.get("start") == "403":
            fail(403, "run start")
        if reply.get("start") == "timeout":
            stall()
        if deployment(target)["status"] != "running":
            fail(409, "run start (employee not running)")
        run_id = str(uuid.uuid4())
        WORLD["runs"][run_id] = {"deployment_id": target, "reply": reply, "polls": 0,
                                 "message": message, "execution_id": "exec-" + run_id[:8]}
        if reply.get("behaviour"):
            WORLD["runs"][run_id]["events"] = employee(message, reply["behaviour"],
                                                       deployment(target).get("tools", []))
        if SCENARIO.get("bundle_after_first_run") and len(WORLD["runs"]) == 1 + len(SCENARIO.get("production_runs", {})):
            deployment(target)["bundle"] = SCENARIO["bundle_after_first_run"]   # changed by its owner
        if SCENARIO.get("update_after_first_run") and len(WORLD["runs"]) == 1 + len(SCENARIO.get("production_runs", {})):
            deployment(target).update(SCENARIO["update_after_first_run"])
        if reply.get("research"):
            WORLD["runs"][run_id]["events"], WORLD["runs"][run_id]["output"] = researcher(
                message, reply["research"], deployment(target).get("tools", []))
        save()
        if reply.get("start") == "no_identity":
            print("Run accepted for deployment %s." % target)
            return
        print("Run accepted for deployment %s.\nRun ID: %s\nExecution ID: %s"
              % (target, run_id, WORLD["runs"][run_id]["execution_id"]))
        return
    if args[:4] == ["agent", "cloud", "run", "get"]:
        run_id = args[-1]
        run = WORLD["runs"][run_id]
        run["polls"] += 1
        reply = run["reply"]
        status = "running" if run["polls"] <= reply.get("running_polls", 0) else reply.get("status", "completed")
        events = None
        if status == "completed" and not run.get("output"):
            events = run.get("events") or [
                {"type": "text", "text": reply.get("answer", ""), "sequence_number": 1},
                {"type": "done", "status": "ok", "sequence_number": 2}]
        out({"data": {"id": run_id, "deployment_id": run["deployment_id"],
                      "execution_id": run["execution_id"], "status": status,
                      "output_events": events, "output": run.get("output") if status == "completed" else None}})
    save()
    sys.stderr.write("Error: stand-in does not support: %s\n" % " ".join(args))
    sys.exit(2)


main(sys.argv[1:])
