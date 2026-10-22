"""Multi-stage user journeys: the employee does the work with its own tool; the harness checks it.

A journey is a customer objective carried through stages on one dedicated synthetic resource:

  setup (harness, not under test) -> work (employee, through `seren_db_query`) ->
  independent read-back and verification -> continuation chosen from the observed result
  (revise after a user change, or correct after user feedback) -> acceptance -> cleanup.

The data the employee needs is never in its message: it lives in a dedicated table the
harness seeds through the pinned `seren psql` route, and the employee must query it itself.
Verification uses the run's tool trace (a completed `seren_db_query` call on that table whose
result carries this journey's unique keys) together with the harness's own read-back of the
table, so neither a fluent answer nor a success claim can pass. Continuation stages quote the
employee's actual previous deliverable, and their criteria depend on it.

Templates (both use the database tool path established by the handover's own contracts; see
TASK.md for the routes that are not established):

- data_report_revision: finance asks for Q3 paid revenue by region, then a revised report
  that excludes test accounts and states what changed against the previous report.
- document_brief_followup: project documents stored as rows; the employee produces a cited,
  costed delivery plan, then revises it after a new decision record appears.

Criteria are generated with the journey and fixed by digest before any stage runs.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import random
import re

from seren_harness.functional_tasks import ACTIONS, PEOPLE, TOPICS, _business_days, _grounded

TOOL = "seren_db_query"
REGIONS = ("East", "North", "South", "West")
FOLLOW_UPS = ("revise", "correct", "abandon")
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,40}")
OWNER = re.compile(r"seren-harness:[a-z0-9:-]{8,120}")
RESULT_ROWS = 500   # rows of one tool result used as provenance evidence
CLAIM = re.compile(r"(?i)\b(verified|confirmed|double[- ]checked|checked against|validated)\b")

TEMPLATES = {
    "data_report_revision": {
        "objective": "Finance needs Q3 paid revenue by region from the orders table, then a "
                     "revised report that excludes test accounts and says what changed.",
        "customer": "a finance analyst who keeps order data in a SerenDB table",
        "work_stage": "report", "revision_stage": "revision", "prefix": "orders"},
    "document_brief_followup": {
        "objective": "Turn the project documents into a cited, costed delivery plan, then "
                     "revise it when a new decision record is added.",
        "customer": "a programme lead whose project records live in a SerenDB table",
        "work_stage": "brief", "revision_stage": "revision", "prefix": "docs"},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _ident(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("unsafe SQL identifier")
    return value


def _literal(value):
    return "'" + json.dumps(value, sort_keys=True).replace("'", "''") + "'"


# ============================================================================= generation
def generate(template, seed, journey_id, schema, campaign_tag, owner):
    """The whole journey, including later-stage material and every stage's criteria.

    `campaign_tag` is the campaign instance's durable namespace and `owner` its ownership
    token: the table name is unique to this campaign instance, and the token is written into
    the table in the transaction that creates it, so later steps can prove the table is ours."""
    if template not in TEMPLATES:
        raise ValueError("unknown journey template")
    if not isinstance(owner, str) or not OWNER.fullmatch(owner):
        raise ValueError("journey owner token required")
    meta = TEMPLATES[template]
    suffix = "%s_%s" % (campaign_tag, journey_id.lower())
    table = "%s.%s" % (_ident(schema), _ident("%s_%s" % (meta["prefix"], suffix)))
    rng = random.Random("%s:%d" % (template, seed))
    body = (_data if template == "data_report_revision" else _documents)(rng, suffix.upper())
    journey = {"template": template, "seed": seed, "objective": meta["objective"],
               "customer": meta["customer"], "table": table, "tool": TOOL, "owner": owner, **body}
    journey["criteria"] = criteria(journey)
    journey["criteria_sha256"] = digest({"criteria": journey["criteria"], "rows": journey["rows"],
                                         "later_rows": journey.get("later_rows", [])})
    return journey


def _data(rng, tag):
    rows = []
    for index in range(16):
        rows.append({"order_id": "ORD-%s-%02d" % (tag, index + 1), "region": REGIONS[index % 4],
                     "quarter": "Q3" if index % 5 else "Q2",
                     "status": ("paid", "paid", "refunded", "pending")[rng.randrange(4)],
                     "amount": rng.randint(50, 900), "test_account": False})
    # Paid Q3 test-account orders in two regions, so the revision changes exactly those.
    for region in rng.sample(REGIONS, 2):
        rows.append({"order_id": "ORD-%s-T%s" % (tag, region[0]), "region": region,
                     "quarter": "Q3", "status": "paid", "amount": rng.randint(100, 400),
                     "test_account": True})
    columns = ("order_id text PRIMARY KEY, region text NOT NULL, quarter text NOT NULL, "
               "status text NOT NULL, amount integer NOT NULL, test_account boolean NOT NULL")
    return {"rows": rows, "key": "order_id", "columns": columns,
            "stages": ["setup", "report", "revision", "cleanup"]}


def _documents(rng, tag):
    project, other = rng.sample(TOPICS, 2)
    owner = rng.choice(PEOPLE)
    agreed = rng.sample(ACTIONS, 3)
    extra = [a for a in ACTIONS if a not in agreed][:2]
    quotes = {key: rng.choice((300, 450, 600, 800, 1200)) for key, _ in ACTIONS}
    days = rng.randint(3, 8)
    meeting = date(2026, 4, 6) + timedelta(days=rng.randint(0, 60))
    deadline = _business_days(meeting, days)
    budget = sum(quotes[k] for k, _ in agreed) + rng.choice((200, 500))
    dropped = rng.choice([k for k, _ in agreed])
    new_budget = sum(quotes[k] for k, _ in agreed if k != dropped) + 100

    def doc(number, project_name, title, text):
        return {"doc_id": "DOC-%s-%d" % (tag, number), "project": project_name, "title": title,
                "body": text}
    rows = [
        doc(1, project, "Kick-off meeting notes", "Meeting on %s (%s). Decision: pilot the %s in "
            "two neighbourhoods. Owner: %s. Agreed workstreams: %s." % (
                meeting.isoformat(), meeting.strftime("%A"), project, owner,
                "; ".join(wording for _, wording in agreed))),
        doc(2, project, "Delivery policy", "The owner's first deliverable is due %d business "
            "days after the meeting date. Business days are Monday to Friday." % days),
        doc(3, project, "Quoted costs", "Quoted costs for the %s: %s." % (
            project, "; ".join("%s %d credits" % (key, quotes[key]) for key, _ in agreed))),
        doc(4, project, "Budget", "Total spend for the %s must not exceed %d credits." % (project, budget)),
        doc(5, other, "Other project notes", "Notes for the %s, a different project. Agreed "
            "workstreams: %s. Quoted costs: %s." % (other, "; ".join(w for _, w in extra),
                                                     "; ".join("%s %d credits" % (k, quotes[k]) for k, _ in extra))),
    ]
    later = [doc(6, project, "Decision update", "Decision update for the %s: drop the %s "
                 "workstream. The budget is now %d credits." % (project, dropped, new_budget))]
    return {"rows": rows, "later_rows": later, "key": "doc_id",
            "columns": "doc_id text PRIMARY KEY, project text NOT NULL, title text NOT NULL, body text NOT NULL",
            "stages": ["setup", "brief", "update_documents", "revision", "cleanup"],
            "facts": {"project": project, "owner": owner, "deadline": deadline.isoformat(),
                      "agreed": [k for k, _ in agreed], "quotes": {k: quotes[k] for k, _ in agreed},
                      "budget": budget, "dropped": dropped, "new_budget": new_budget,
                      "notes_doc": rows[0]["doc_id"], "quotes_doc": rows[2]["doc_id"],
                      "project_docs": [r["doc_id"] for r in rows if r["project"] == project],
                      "decision_doc": later[0]["doc_id"]}}


def criteria(journey):
    """Immutable criteria per work stage. Descriptions are user-facing expectations."""
    table = journey["table"]
    retrieval = [
        {"id": "performed_retrieval", "kind": "deterministic",
         "description": "The employee ran a completed %s call that reads %s." % (TOOL, table)},
        {"id": "source_fidelity", "kind": "deterministic",
         "description": "A tool result carries this journey's own records, so the work used the "
                        "dedicated source rather than invented data."}]
    if journey["template"] == "data_report_revision":
        report = retrieval + [
            {"id": "reply_format", "kind": "deterministic", "format": True,
             "description": "Reply is only the requested JSON report."},
            {"id": "regions_correct", "kind": "deterministic",
             "description": "Each region's Q3 paid total equals the harness's independent read-back."},
            {"id": "total_correct", "kind": "deterministic",
             "description": "The total equals the independent read-back."}]
        revision = retrieval + [
            {"id": "reply_format", "kind": "deterministic", "format": True,
             "description": "Reply is only the requested JSON report with previous_total and changed_regions."},
            {"id": "regions_correct", "kind": "deterministic",
             "description": "Each region's total excludes test accounts and equals the read-back."},
            {"id": "total_correct", "kind": "deterministic",
             "description": "The revised total equals the read-back without test accounts."},
            {"id": "builds_on_previous", "kind": "deterministic",
             "description": "previous_total and changed_regions match the employee's own previous "
                            "report, as quoted to it."}]
        return {"report": report, "revision": revision}
    facts = journey["facts"]
    brief = retrieval + [
        {"id": "reply_format", "kind": "deterministic", "format": True,
         "description": "Reply is only the requested JSON plan with cited, costed step objects."},
        {"id": "owner_correct", "kind": "deterministic", "description": "owner is %s." % facts["owner"]},
        {"id": "deadline_correct", "kind": "deterministic",
         "description": "deadline is %s per the delivery policy." % facts["deadline"]},
        {"id": "work_covered", "kind": "deterministic",
         "description": "Each agreed workstream (%s) has a step that names it and cites the notes "
                        "or quotes document." % ", ".join(facts["agreed"])},
        {"id": "steps_grounded", "kind": "deterministic",
         "description": "Every step cites a document of this project whose text supports it; no "
                        "step uses the other project's document."},
        {"id": "costs_match_quotes", "kind": "deterministic",
         "description": "Each step costs exactly the quote for the workstream it names, and total_cost "
                        "equals the quoted cost of the agreed work (%d credits), within %d credits."
                        % (sum(facts["quotes"].values()), facts["budget"])},
        {"id": "steps_relevant", "kind": "judged",
         "description": "Every step is a concrete action toward piloting the %s; none is "
                        "unrelated." % facts["project"]}]
    revision = retrieval + [
        {"id": "reply_format", "kind": "deterministic", "format": True,
         "description": "Reply is only the requested JSON plan with removed_steps and decision_doc."},
        {"id": "decision_followed", "kind": "deterministic",
         "description": "decision_doc is %s and no step keeps the dropped %s workstream." % (
             facts["decision_doc"], facts["dropped"])},
        {"id": "builds_on_previous", "kind": "deterministic",
         "description": "removed_steps are exactly the previous plan's %s steps and every other "
                        "previous step is kept whole: same action, cited document and cost."
                        % facts["dropped"]},
        {"id": "steps_grounded", "kind": "deterministic",
         "description": "Every kept step still cites an existing document of this project whose "
                        "text supports it."},
        {"id": "costs_match_quotes", "kind": "deterministic",
         "description": "Each kept step keeps its quoted cost and total_cost equals the quoted cost of "
                        "the remaining work, within the new budget of %d credits." % facts["new_budget"]},
        {"id": "owner_correct", "kind": "deterministic", "description": "owner is still %s." % facts["owner"]},
        {"id": "deadline_correct", "kind": "deterministic",
         "description": "deadline is still %s." % facts["deadline"]}]
    return {"brief": brief, "revision": revision}


def check_criteria(journey):
    if journey["criteria_sha256"] != digest({"criteria": journey["criteria"], "rows": journey["rows"],
                                             "later_rows": journey.get("later_rows", [])}):
        raise ValueError("journey criteria changed after they were recorded")


# ============================================================================= SQL (harness side)
# Every statement after creation proves ownership first: the creating transaction stores the
# campaign instance's owner token as the table comment, and updates and drops run inside a DO
# block that acts only when that comment matches. Row content is never treated as ownership.
def _owned(journey):
    table = journey["table"]
    return "obj_description(to_regclass('%s'), 'pg_class')" % table


def _guarded(journey, statement):
    owner = journey["owner"]
    if not OWNER.fullmatch(owner) or "$harness$" in statement:
        raise ValueError("unsafe guarded statement")
    return ("DO $harness$ BEGIN IF %s = '%s' THEN %s; END IF; END $harness$;"
            % (_owned(journey), owner, statement))


def setup_sql(journey):
    table = journey["table"]
    schema = table.split(".")[0]
    return ("BEGIN; CREATE SCHEMA IF NOT EXISTS %s; CREATE TABLE %s (%s); COMMENT ON TABLE %s IS "
            "'%s'; INSERT INTO %s SELECT * FROM json_populate_recordset(NULL::%s, %s); COMMIT; "
            "SELECT json_build_object('rows', (SELECT count(*) FROM %s), 'owner', %s);"
            % (schema, table, journey["columns"], table, journey["owner"], table, table,
               _literal(journey["rows"]), table, _owned(journey)))


def insert_sql(journey, rows):
    table = journey["table"]
    return (_guarded(journey, "INSERT INTO %s SELECT * FROM json_populate_recordset(NULL::%s, %s)"
                     % (table, table, _literal(rows)))
            + " SELECT json_build_object('rows', (SELECT count(*) FROM %s), 'owner', %s);"
            % (table, _owned(journey)))


def read_sql(journey):
    """Owner and rows together, so verification never reads a table it cannot prove is ours."""
    table = journey["table"]
    return ("SELECT json_build_object('owner', %s, 'rows', (SELECT coalesce(json_agg(row_to_json(r) "
            "ORDER BY r.%s), '[]'::json) FROM %s r));" % (_owned(journey), _ident(journey["key"]), table))


def drop_sql(journey):
    table = journey["table"]
    return (_guarded(journey, "DROP TABLE %s" % table)
            + " SELECT json_build_object('exists', to_regclass('%s') IS NOT NULL, 'owner', %s);"
            % (table, _owned(journey)))


def probe_sql(journey):
    """Existence and owner of the table; never fails just because the table is absent."""
    return ("SELECT json_build_object('exists', to_regclass('%s') IS NOT NULL, 'owner', %s);"
            % (journey["table"], _owned(journey)))


def owned_rows(journey, data):
    """The read-back rows when the table carries this campaign instance's token, else None."""
    if isinstance(data, dict) and data.get("owner") == journey["owner"] and isinstance(data.get("rows"), list):
        return data["rows"]
    return None


def expected_rows(journey, stage_index):
    """Rows the table must hold once the setup stages up to this point have been applied."""
    rows = list(journey["rows"])
    if "update_documents" in journey["stages"][:stage_index + 1]:
        rows += journey["later_rows"]
    return sorted(rows, key=lambda r: r[journey["key"]])


# ============================================================================= messages
def message(journey, stage, reference, previous=None, feedback=None):
    """The user's request for a work stage; continuation stages quote the prior deliverable."""
    head = ["Seren Harness journey %s, stage %s. Synthetic test data; you are working for %s."
            % (reference, stage, journey["customer"]),
            "Objective: " + journey["objective"]]
    table = journey["table"]
    if journey["template"] == "data_report_revision":
        source = ("The order data is in the database table %s (columns order_id, region, quarter, "
                  "status, amount, test_account). It is not in this message: read it with %s "
                  "(read-only SELECT statements only)." % (table, TOOL))
        shape = ('{"regions": {"East": int, "North": int, "South": int, "West": int}, "total": int, '
                 '"query": "the SQL you ran"')
        if stage == "revision":
            body = [source, _quoted(previous),
                    "Change request from the finance lead: exclude orders from test accounts "
                    "(test_account = true). Query the table again and revise the report.",
                    "Reply with only a JSON object: " + shape + ', "previous_total": the total '
                    'from your previous report, "changed_regions": the regions whose figure '
                    'differs from your previous report, in alphabetical order}.']
        else:
            body = [source, "Task: for each region, total the amount of orders with status 'paid' "
                    "in quarter 'Q3'.", "Reply with only a JSON object: " + shape + "}."]
            if feedback:
                body[1:1] = [_quoted(previous), feedback]
    else:
        facts = journey["facts"]
        source = ("The project documents are rows of the database table %s (columns doc_id, "
                  "project, title, body). They are not in this message: read them with %s "
                  "(read-only SELECT statements only). Use only documents for project '%s'."
                  % (table, TOOL, facts["project"]))
        shape = ('{"owner": string, "deadline": "YYYY-MM-DD", "steps": [{"action": string, "doc": '
                 'doc_id, "cost": int}], "total_cost": int, "risks": [string]')
        if stage == "revision":
            body = [source, _quoted(previous),
                    "A new decision record has been added to the project documents. Read the "
                    "documents again and revise your plan: follow the decision, keep every "
                    "unaffected step exactly as it was.",
                    "Reply with only a JSON object: " + shape + ', "removed_steps": [the exact '
                    'action texts you removed], "decision_doc": the doc_id you followed}.']
        else:
            body = [source, "Task: produce the delivery plan. Cover every agreed workstream with "
                    "one step citing the document that states it, take costs from the quoted "
                    "costs document, and keep the total within the budget.",
                    "Reply with only a JSON object: " + shape + "}."]
            if feedback:
                body[1:1] = [_quoted(previous), feedback]
    return "\n\n".join(head + body)


def _quoted(previous):
    return ("Here is what you delivered in the previous stage:\n=== YOUR PREVIOUS DELIVERABLE ===\n"
            + json.dumps(previous, sort_keys=True) + "\n=== END PREVIOUS DELIVERABLE ===")


def feedback(evaluation, criteria_list):
    failed = [c["description"] for c in criteria_list if c["id"] in evaluation["failed_criteria"]]
    return ("The reviewer says this deliverable is not usable yet. Problems: " + " ".join(failed)
            + " Recheck the source with %s and send a corrected deliverable in the same JSON format."
            % TOOL)


# ============================================================================= verification
def relations(sql):
    """Relations a query reads (after FROM/JOIN), ignoring comments, string literals and
    function calls. Not a SQL parser: enough to tell which table a query targeted."""
    text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S)
    text = re.sub(r"'(?:[^']|'')*'", "''", text).replace('"', "").lower()
    names = re.findall(r"\b(?:from|join)\s+(?!lateral\b)([a-z_][a-z0-9_$]*(?:\.[a-z_][a-z0-9_$]*)?)(?!\s*\()", text)
    return sorted(set(names))


def _result_rows(content):
    """A tool result as a list of row dicts, or None when it is not a complete structured result."""
    try:
        value = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        return None
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        rows, columns = value["rows"], value.get("columns")
        if rows and all(isinstance(r, list) for r in rows):
            if not isinstance(columns, list) or any(len(r) != len(columns) for r in rows):
                return None
            rows = [dict(zip((str(c) for c in columns), r)) for r in rows]
        value = rows
    if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
        return None
    return value[:RESULT_ROWS]


def trace(events, table):
    """(summary, results): every tool call with the relations it read, and the parsed result rows
    of each call (None when the result is missing, failed, unstructured or truncated)."""
    calls, results = {}, {}
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_call":
            arguments = event.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            calls[event.get("id")] = {"name": event.get("name"), "status": event.get("status"),
                                      "query": (arguments or {}).get("query") if isinstance(arguments, dict) else None}
        elif event.get("type") == "tool_result":
            content = event.get("content")
            results[event.get("id")] = {"is_error": event.get("is_error"), "content": content}
    bare = table.split(".")[-1]
    summary, rows = [], []
    for identifier, call in calls.items():
        result = results.get(identifier, {})
        read = relations(call["query"]) if isinstance(call["query"], str) else []
        error = bool(result.get("is_error"))
        parsed = None if error or identifier not in results else _result_rows(result.get("content"))
        content = result.get("content")
        summary.append({"tool": call["name"], "query": (call["query"] or "")[:300],
                        "completed": call["status"] in (None, "completed") and identifier in results,
                        "error": error, "relations": read,
                        "reads_table": table in read or bare in read,
                        "other_relations": [r for r in read if r not in (table, bare)],
                        "result_rows": None if parsed is None else len(parsed),
                        "error_text": str(content)[:160] if error else None})
        rows.append(parsed)
    return summary, rows


def _same_value(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _projection(rows, source, needed):
    """Every result row is a projection of some source row and carries the needed columns."""
    columns = set(source[0]) if source else set()
    if not rows:
        return False
    for row in rows:
        shared = set(row) & columns
        if not needed <= shared or not any(all(_same_value(row[c], s[c]) for c in shared) for s in source):
            return False
    return True


def _aggregate(rows, delivered):
    """A grouped result that carries every nonzero delivered regional figure."""
    if not isinstance(delivered, dict) or not rows:
        return False
    if not all(any(v in REGIONS for v in row.values()) for row in rows):
        return False
    return all(any(region in row.values() and any(_same_value(v, figure) for k, v in row.items()
                                                  if not isinstance(v, (str, bool))) for row in rows)
               for region, figure in delivered.items() if figure)


def provenance(journey, stage, calls, rows, source, value):
    """True: the delivered work rests on this journey's source; False: a wrong or mixed source,
    or results that do not match the source; None: the evidence needed is missing."""
    useful = [i for i, c in enumerate(calls) if c["tool"] == TOOL and c["completed"] and not c["error"]
              and c["reads_table"]]
    if any(calls[i]["other_relations"] for i in useful):
        return False
    evidence = [rows[i] for i in useful if rows[i] is not None]
    if not useful:
        return False
    if not evidence:
        return None
    if journey["template"] == "data_report_revision":
        delivered = _ints(value.get("regions")) or {}
        return any(_projection(r, source, {"region", "amount"}) or _aggregate(r, delivered) for r in evidence)
    read = [r for rs in evidence if _projection(rs, source, {"doc_id", "body"}) for r in rs]
    if not read:
        return False
    seen = {r["doc_id"] for r in read}
    cited = {s["doc"] for s in _steps(value)} & set(journey["facts"]["project_docs"])
    needed = cited | ({journey["facts"]["decision_doc"]} if stage == "revision" else set())
    return needed <= seen


def _parse(reply):
    try:
        strict = json.loads(reply.strip())
    except ValueError:
        strict = None
    value = strict
    if not isinstance(value, dict):
        match = re.search(r"\{.*\}", reply, re.S)
        try:
            value = json.loads(match.group(0)) if match else None
        except ValueError:
            value = None
    return (strict if isinstance(strict, dict) else None), (value if isinstance(value, dict) else {})


def _ints(mapping):
    if not isinstance(mapping, dict):
        return None
    try:
        return {str(k): int(v) for k, v in mapping.items() if type(v) in (int, float) or str(v).isdigit()}
    except (TypeError, ValueError):
        return None


def truth(journey, rows, stage):
    """Ground truth computed by the harness from its own read-back of the table."""
    if journey["template"] == "data_report_revision":
        selected = [r for r in rows if r.get("quarter") == "Q3" and r.get("status") == "paid"
                    and (stage != "revision" or not r.get("test_account"))]
        regions = {region: sum(int(r["amount"]) for r in selected if r["region"] == region)
                   for region in REGIONS}
        return {"regions": regions, "total": sum(regions.values())}
    return dict(journey["facts"])


def verify(journey, stage, criterion_stage, events, reply, rows, previous):
    """({criterion: True/False/None}, calls, expected) for a work stage. `rows` is the harness's
    independent read-back of a table it has proved it owns."""
    check_criteria(journey)
    calls, results = trace(events, journey["table"])
    strict, value = _parse(reply or "")
    useful = [c for c in calls if c["tool"] == TOOL and c["completed"] and not c["error"] and c["reads_table"]]
    checked = {"performed_retrieval": bool(useful),
               "source_fidelity": provenance(journey, stage, calls, results, rows, value)}
    expected = truth(journey, rows, stage)
    if journey["template"] == "data_report_revision":
        checked.update(_check_report(stage, strict, value, expected, previous))
    else:
        checked.update(_check_plan(criterion_stage, strict, value, expected, previous, rows))
    wanted = [c["id"] for c in journey["criteria"][criterion_stage]]
    return {k: checked.get(k) for k in wanted}, calls, expected


def _check_report(stage, strict, value, expected, previous):
    regions = _ints(value.get("regions"))
    keys = {"regions", "total", "query"} | ({"previous_total", "changed_regions"} if stage == "revision" else set())
    results = {"reply_format": strict is not None and set(strict) == keys,
               "regions_correct": regions == expected["regions"],
               "total_correct": value.get("total") == expected["total"]}
    if stage == "revision":
        prior = _ints((previous or {}).get("regions")) or {}
        changed = sorted(r for r in REGIONS if prior.get(r) != expected["regions"][r])
        results["builds_on_previous"] = (bool(prior) and value.get("previous_total") == (previous or {}).get("total")
                                         and value.get("changed_regions") == changed)
    return results


def _steps(value):
    steps = value.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        return []
    return [{"action": str(s.get("action") or ""), "doc": str(s.get("doc") or ""), "cost": s.get("cost")}
            for s in steps]


def _check_plan(stage, strict, value, facts, previous, rows):
    steps = _steps(value)
    bodies = {r["doc_id"]: r["body"] for r in rows if r.get("project") == facts["project"]}
    cost_sum = sum(s["cost"] for s in steps if type(s["cost"]) is int) if steps else None
    total = value.get("total_cost")

    def quoted(step):  # The quote of the one agreed workstream a step names, else None.
        named = [k for k in facts["agreed"] if k in step["action"].lower()]
        return facts["quotes"][named[0]] if len(named) == 1 else None

    def costed(keys, limit):
        return (bool(steps) and all(quoted(s) is not None and s["cost"] == quoted(s) for s in steps)
                and type(total) is int and total == cost_sum
                and total == sum(facts["quotes"][k] for k in keys) and total <= limit)
    results = {"owner_correct": value.get("owner") == facts["owner"],
               "deadline_correct": value.get("deadline") == facts["deadline"]}
    if stage == "brief":
        keys = {"owner", "deadline", "steps", "total_cost", "risks"}
        results.update(
            reply_format=strict is not None and set(strict) == keys and bool(steps),
            work_covered=bool(steps) and all(any(key in s["action"].lower() and s["doc"] in (
                facts["notes_doc"], facts["quotes_doc"]) for s in steps) for key in facts["agreed"]),
            steps_grounded=bool(steps) and all(s["doc"] in bodies and _grounded(s["action"], bodies[s["doc"]])
                                               for s in steps),
            costs_match_quotes=costed(facts["agreed"], facts["budget"]),
            steps_relevant=None)
        return results
    prior = _steps(previous or {})
    dropped = facts["dropped"]
    removed = [s["action"] for s in prior if dropped in s["action"].lower()]
    kept = [s for s in prior if dropped not in s["action"].lower()]   # whole steps, citation included
    keys = {"owner", "deadline", "steps", "total_cost", "risks", "removed_steps", "decision_doc"}
    results.update(
        reply_format=strict is not None and set(strict) == keys,
        decision_followed=value.get("decision_doc") == facts["decision_doc"]
        and not any(dropped in s["action"].lower() for s in steps),
        builds_on_previous=bool(prior) and value.get("removed_steps") == removed and steps == kept,
        steps_grounded=bool(steps) and all(s["doc"] in bodies and _grounded(s["action"], bodies[s["doc"]])
                                           for s in steps),
        costs_match_quotes=costed([k for k in facts["agreed"] if k != dropped], facts["new_budget"]))
    return results


def claims_success(reply):
    return bool(CLAIM.search(reply or ""))


# ============================================================================= follow-ups
def permitted(journey_record):
    """Follow-up actions the planner may choose for a journey awaiting a decision."""
    if journey_record["status"] != "awaiting_decision":
        return []
    last = journey_record["last_verdict"]
    # An undecided judged criterion does not stop the user from continuing; the journey then
    # ends as partially verified rather than accepted.
    if last in ("passed", "uncertain") and not journey_record.get("revised"):
        return ["revise", "abandon"]
    if last in ("failed", "format_only") and not journey_record.get("corrected"):
        return ["correct", "abandon"]
    return ["abandon"]
