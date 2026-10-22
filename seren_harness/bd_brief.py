"""Business-development prospect briefs from a supplied synthetic dossier (no I/O).

Three frozen variants for the existing supplied-answer route on Seren Harness Clock Test:
correct synthesis with attribution, conflicting and missing evidence, and an unavailable
source next to a record that does not exist. The dossier is supplied in the message: this
tests synthesis, source attribution and uncertainty handling, not retrieval (no supported
tool can read controlled records for this employee today; see docs/bd-brief/README.md).

The checker is deterministic for facts, sources, conflicts, unknowns, qualification
criteria and the recommendation category. Free-text quality (next step, summary) is
judged afterwards with the rubric in docs/bd-brief/README.md, not by exact wording.
All companies, records and amounts are synthetic.
"""
import copy
from datetime import date
import json

from seren_harness import finance_invoices as finance
from seren_harness.invoice_conflicts import _unique

AS_OF = "2026-10-14"
FIELDS = ("headcount", "analytics_warehouse", "data_team_size", "latest_funding",
          "open_data_roles", "existing_customer")
CRITERIA = ("headcount_50_500", "analytics_warehouse_fit", "data_team_3_plus",
            "recent_trigger", "not_existing_customer")
REASONS = ("no_record", "source_unavailable", "record_not_found")
RECOMMENDATIONS = ("pursue", "qualify_further", "disqualify")
KEYS = {"prospect", "facts", "conflicts", "unknowns", "criteria", "recommendation",
        "next_step", "next_step_sources", "summary"}

# Each record: (id, date or "", kind, status, content). status is available, unavailable
# (retrieval failed; content absent) or not_found (no such record exists).
CASES = {
    "bd-attribution": {
        "purpose": "Brief a qualified prospect with every fact attributed to its record.",
        "prospect": "Halvorsen Freight Systems",
        "records": [
            ("S1", "2026-09-30", "company_profile", "available",
             "Halvorsen Freight Systems builds freight brokerage software. Headquarters: "
             "Rotterdam. Employees: 240."),
            ("S2", "2026-10-05", "job_postings", "available",
             "Two open roles: Senior Data Engineer and Data Engineer. Both postings mention "
             "Snowflake and Airflow."),
            ("S3", "2026-08-12", "press_release", "available",
             "Halvorsen Freight Systems announced a Series B round of 18,000,000 USD."),
            ("S4", "2026-07-01", "tech_stack_note", "available",
             "Operational database: PostgreSQL. Analytics warehouse: Snowflake."),
            ("S5", "2026-10-02", "org_note", "available",
             "The data team has 4 people and reports to a Head of Data."),
            ("S6", "2026-10-10", "crm_account", "available",
             "No active contract and no open opportunity with us."),
        ],
        "facts": {
            "headcount": (240, {"S1"}),
            "analytics_warehouse": ("snowflake", {"S4", "S2"}),
            "data_team_size": (4, {"S5"}),
            "latest_funding": ({"round": "series b", "amount_usd": 18000000,
                                "date": "2026-08-12"}, {"S3"}),
            "open_data_roles": (2, {"S2"}),
            "existing_customer": (False, {"S6"}),
        },
        "conflicts": [],
        "unknowns": [],
        "triggers": {"S2", "S3"},
    },
    "bd-conflicting-missing": {
        "purpose": "Resolve conflicting headcounts by recency and keep missing facts unknown.",
        "prospect": "Marrow & Pine Logistics",
        "records": [
            ("S1", "2026-09-28", "company_profile", "available",
             "Marrow & Pine Logistics runs regional cold-storage distribution. "
             "Employees: 240."),
            ("S2", "2026-10-06", "job_postings", "available",
             "One open role: Data Engineer (BigQuery, dbt)."),
            ("S3", "2026-03-15", "directory_listing", "available",
             "Marrow & Pine Logistics. Employees: 620."),
            ("S4", "2026-08-20", "tech_stack_note", "available",
             "Analytics warehouse: BigQuery. Operational database: MySQL."),
            ("S5", "2026-10-01", "crm_account", "available",
             "No active contract and no open opportunity with us."),
        ],
        "facts": {
            "headcount": (240, {"S1"}),
            "analytics_warehouse": ("bigquery", {"S4", "S2"}),
            "data_team_size": (None, set()),
            "latest_funding": (None, set()),
            "open_data_roles": (1, {"S2"}),
            "existing_customer": (False, {"S5"}),
        },
        "conflicts": [("headcount", {"S1", "S3"}, "S1")],
        "unknowns": [("data_team_size", "no_record", set()),
                     ("latest_funding", "no_record", set())],
        "triggers": {"S2"},
    },
    "bd-unavailable-source": {
        "purpose": "Keep an unavailable CRM export and a missing org chart unknown, not guessed.",
        "prospect": "Ostrava Cold Chain",
        "records": [
            ("S1", "2026-09-15", "company_profile", "available",
             "Ostrava Cold Chain operates temperature-controlled warehousing. Employees: 180."),
            ("S2", "2026-09-29", "press_release", "available",
             "Ostrava Cold Chain closed a Series A round of 9,500,000 USD."),
            ("S3", "2026-08-02", "tech_stack_note", "available",
             "Analytics warehouse: Snowflake. Operational database: SQL Server."),
            ("S4", "", "org_chart_export", "not_found",
             "No org chart export exists for this account."),
            ("S5", "", "crm_account", "unavailable",
             "Export failed: the CRM connector timed out; no content was retrieved."),
            ("S6", "2026-10-08", "job_postings", "available", "No open data roles."),
        ],
        "facts": {
            "headcount": (180, {"S1"}),
            "analytics_warehouse": ("snowflake", {"S3"}),
            "data_team_size": (None, set()),
            "latest_funding": ({"round": "series a", "amount_usd": 9500000,
                                "date": "2026-09-29"}, {"S2"}),
            "open_data_roles": (0, {"S6"}),
            "existing_customer": (None, set()),
        },
        "conflicts": [],
        "unknowns": [("data_team_size", "record_not_found", {"S4"}),
                     ("existing_customer", "source_unavailable", {"S5"})],
        "triggers": {"S2"},
    },
}

# bd-reliability-v1 (2026-10-15): the bd-unavailable-source task again with its message and
# expectations unchanged, an identical repeat under its own case ID (the campaign derives a
# per-case authorization from the ID), and a matched complete-evidence control: the same
# prospect, records, rules and output contract, except that the org chart (S4) and the CRM
# export (S5) are available. Kept out of CASES so the bd-brief-v1 suite is unchanged.
RELIABILITY = {
    "bd-unavailable-source-repeat": dict(
        CASES["bd-unavailable-source"],
        purpose="Identical repeat of bd-unavailable-source (same message and expectations)."),
    "bd-complete-control": {
        "purpose": "Matched control: the same Ostrava Cold Chain brief with complete evidence.",
        "prospect": "Ostrava Cold Chain",
        "records": [
            CASES["bd-unavailable-source"]["records"][0],
            CASES["bd-unavailable-source"]["records"][1],
            CASES["bd-unavailable-source"]["records"][2],
            ("S4", "2026-10-03", "org_chart_export", "available",
             "The data team has 4 people and reports to a Head of Data."),
            ("S5", "2026-10-09", "crm_account", "available",
             "No active contract and no open opportunity with us."),
            CASES["bd-unavailable-source"]["records"][5],
        ],
        "facts": {
            "headcount": (180, {"S1"}),
            "analytics_warehouse": ("snowflake", {"S3"}),
            "data_team_size": (4, {"S4"}),
            "latest_funding": ({"round": "series a", "amount_usd": 9500000,
                                "date": "2026-09-29"}, {"S2"}),
            "open_data_roles": (0, {"S6"}),
            "existing_customer": (False, {"S5"}),
        },
        "conflicts": [],
        "unknowns": [],
        "triggers": {"S2"},
    },
}
SPECS = {**CASES, **RELIABILITY}
SUITES = {"bd-brief-v1": tuple(CASES),
          "bd-reliability-v1": ("bd-unavailable-source", "bd-unavailable-source-repeat",
                                "bd-complete-control")}

RULES = """As-of date: 2026-10-14. Use only the records below. Do not use tools, browse,
contact anyone, schedule anything or change any record. Cite record IDs for every fact.

Qualification (all must hold to pursue):
- headcount_50_500: headcount between 50 and 500 inclusive.
- analytics_warehouse_fit: the analytics warehouse is Snowflake or BigQuery (an
  operational database does not count).
- data_team_3_plus: the data team has at least 3 people.
- recent_trigger: a funding round or at least one open data-engineering role dated within
  183 days before the as-of date.
- not_existing_customer: the CRM account shows no active contract.

Evidence rules:
- If two available records disagree on a fact, use the most recently dated record and list
  the conflict with both record IDs and the ID you used.
- A fact no available record supports is unknown: value null, listed in unknowns with
  reason no_record and no sources.
- A record with status unavailable could not be retrieved: do not use or guess its content.
  List the fact it would support in unknowns with reason source_unavailable and its ID.
  Unavailable means the information was not retrieved, not that it does not exist.
- A record with status not_found does not exist: reason record_not_found and its ID.
- A criterion is true, false, or null when its fact is unknown.

Recommendation: pursue if all five criteria are true; disqualify if any is false;
qualify_further if none is false and at least one is null. next_step is one concrete
action for the account executive; next_step_sources cite at least one trigger record.

Reply with one JSON object and nothing else, with exactly these fields:
{"prospect":"company name",
 "facts":{"headcount":{"value":240,"sources":["S1"]},
          "analytics_warehouse":{"value":"name or null","sources":[]},
          "data_team_size":{"value":0,"sources":[]},
          "latest_funding":{"value":{"round":"Series X","amount_usd":0,"date":"YYYY-MM-DD"},
                            "sources":[]},
          "open_data_roles":{"value":0,"sources":[]},
          "existing_customer":{"value":false,"sources":[]}},
 "conflicts":[{"field":"fact name","sources":["ID","ID"],"used":"ID"}],
 "unknowns":[{"field":"fact name","reason":"no_record or source_unavailable or record_not_found",
              "sources":[]}],
 "criteria":{"headcount_50_500":true,"analytics_warehouse_fit":true,"data_team_3_plus":true,
             "recent_trigger":true,"not_existing_customer":true},
 "recommendation":"pursue or qualify_further or disqualify",
 "next_step":"one concrete action","next_step_sources":["ID"],
 "summary":"at most 400 characters"}
Use null for unknown values (latest_funding null if there is no funding record). Integers
for counts and amounts. Array order does not matter. No extra fields.
"""


def dossier(key):
    lines = ["id | date | kind | status | content"]
    for ident, day, kind, status, content in SPECS[key]["records"]:
        lines.append(" | ".join((ident, day or "-", kind, status, content)))
    return "\n".join(lines)


def message(key):
    return ("Prepare a short business-development prospect brief on "
            + SPECS[key]["prospect"] + " for an account executive selling a data-pipeline "
            "monitoring product.\n\nRecords:\n" + dossier(key) + "\n\nRules:\n" + RULES).strip()


def criteria(key):
    """Qualification from the expected facts by the stated rules (also re-derived
    independently in tests)."""
    facts = {f: v for f, (v, _) in SPECS[key]["facts"].items()}

    def known(value, test):
        return None if value is None else test(value)
    as_of = date.fromisoformat(AS_OF)
    dated = [date.fromisoformat(r[1]) for r in SPECS[key]["records"]
             if r[0] in SPECS[key]["triggers"]]
    if any(0 <= (as_of - d).days <= 183 for d in dated):
        recent = True
    elif facts["latest_funding"] is None or facts["open_data_roles"] is None:
        recent = None  # a trigger could exist in the unknown fact
    else:
        recent = False
    return {"headcount_50_500": known(facts["headcount"], lambda v: 50 <= v <= 500),
            "analytics_warehouse_fit": known(facts["analytics_warehouse"],
                                             lambda v: v in ("snowflake", "bigquery")),
            "data_team_3_plus": known(facts["data_team_size"], lambda v: v >= 3),
            "recent_trigger": recent,
            "not_existing_customer": known(facts["existing_customer"], lambda v: not v)}


def recommendation(key):
    values = criteria(key).values()
    if any(v is False for v in values):
        return "disqualify"
    return "pursue" if all(v is True for v in values) else "qualify_further"


def expected(key):
    """Frozen expectation (normalized forms; the checker accepts equivalent variants)."""
    spec = SPECS[key]
    return {"prospect": spec["prospect"],
            "facts": {f: {"value": v, "sources": sorted(s)} for f, (v, s) in spec["facts"].items()},
            "conflicts": [{"field": f, "sources": sorted(s), "used": u}
                          for f, s, u in spec["conflicts"]],
            "unknowns": [{"field": f, "reason": r, "sources": sorted(s)}
                         for f, r, s in spec["unknowns"]],
            "criteria": criteria(key), "recommendation": recommendation(key),
            "trigger_sources": sorted(spec["triggers"])}


def _ids(value, valid, where):
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise ValueError("sources must be a list of record IDs: " + where)
    if len(set(value)) != len(value):
        raise ValueError("duplicate source: " + where)
    return set(value)


def _fact(field, value):
    """Normalize equivalent forms; type errors are malformed answers."""
    if value is None:
        return None
    if field in ("headcount", "data_team_size", "open_data_roles"):
        if type(value) is not int or value < 0:
            raise ValueError("count must be a nonnegative integer: " + field)
        return value
    if field == "analytics_warehouse":
        if not isinstance(value, str):
            raise ValueError("warehouse must be a string")
        return value.strip().lower()
    if field == "existing_customer":
        if type(value) is not bool:
            raise ValueError("existing_customer must be a boolean")
        return value
    if (not isinstance(value, dict) or set(value) != {"round", "amount_usd", "date"}
            or not isinstance(value["round"], str) or type(value["amount_usd"]) is not int
            or not isinstance(value["date"], str)):
        raise ValueError("latest_funding must be null or {round, amount_usd, date}")
    return {"round": " ".join(value["round"].lower().split()),
            "amount_usd": value["amount_usd"], "date": value["date"]}


def _parse(text):
    raw = str(text).strip()
    fence = finance._FENCE.fullmatch(raw)
    if fence is not None:
        raw = fence.group(1)
    data = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(data, dict) or set(data) != KEYS:
        raise ValueError("answer must contain exactly the requested fields")
    if not isinstance(data["facts"], dict) or set(data["facts"]) != set(FIELDS):
        raise ValueError("facts must contain exactly the six requested facts")
    if not isinstance(data["criteria"], dict) or set(data["criteria"]) != set(CRITERIA):
        raise ValueError("criteria must contain exactly the five criteria")
    return data


def check(key, text):
    """passed / failed (named problems) / malformed / no_answer."""
    if text is None or not str(text).strip():
        return {"outcome": finance.NO_ANSWER, "problems": ["no final answer text"]}
    spec = SPECS[key]
    valid = {r[0] for r in spec["records"]}
    usable = {r[0] for r in spec["records"] if r[3] == "available"}
    try:
        data = _parse(text)
        problems = []
        if not isinstance(data["prospect"], str) or \
                data["prospect"].strip().lower() != spec["prospect"].lower():
            problems.append("wrong prospect")
        cited = set()
        for field in FIELDS:
            item = data["facts"][field]
            if not isinstance(item, dict) or set(item) != {"value", "sources"}:
                raise ValueError("fact must be {value, sources}: " + field)
            sources = _ids(item["sources"], valid, field)
            cited |= sources
            want, support = spec["facts"][field]
            got = _fact(field, item["value"])
            if got != want:
                problems.append("wrong value: " + field)
            if want is None:
                if sources - {i for f, _, s in spec["unknowns"] if f == field for i in s}:
                    problems.append("unsupported sources for unknown fact: " + field)
            elif not sources or not sources <= support:
                problems.append("unsupported or missing sources: " + field)
            if sources - usable and want is not None:
                problems.append("used an unavailable or nonexistent record: " + field)
        for name, kind in (("conflicts", ("field", "sources", "used")),
                           ("unknowns", ("field", "reason", "sources"))):
            if not isinstance(data[name], list) or not all(
                    isinstance(i, dict) and set(i) == set(kind) for i in data[name]):
                raise ValueError(name + " must be a list of " + "/".join(kind) + " objects")
        conflicts = set()
        for c in data["conflicts"]:
            ids = _ids(c["sources"], valid, "conflict")
            cited |= ids
            conflicts.add((c["field"], frozenset(ids), c["used"]))
        if conflicts != {(f, frozenset(s), u) for f, s, u in spec["conflicts"]}:
            problems.append("wrong conflicts")
        unknowns = set()
        for u in data["unknowns"]:
            ids = _ids(u["sources"], valid, "unknown")
            cited |= ids
            unknowns.add((u["field"], u["reason"], frozenset(ids)))
        if unknowns != {(f, r, frozenset(s)) for f, r, s in spec["unknowns"]}:
            problems.append("wrong unknowns (field, reason or sources)")
        if any(data["criteria"][c] is not None and type(data["criteria"][c]) is not bool
               for c in CRITERIA):
            raise ValueError("criteria must be true, false or null")
        if data["criteria"] != criteria(key):
            problems.append("wrong criteria: " + ", ".join(
                c for c in CRITERIA if data["criteria"][c] != criteria(key)[c]))
        if data["recommendation"] not in RECOMMENDATIONS:
            raise ValueError("unknown recommendation")
        if data["recommendation"] != recommendation(key):
            problems.append("wrong recommendation")
        if not isinstance(data["next_step"], str) or len(data["next_step"].strip()) < 15:
            problems.append("next_step missing or not concrete")
        steps = _ids(data["next_step_sources"], valid, "next_step")
        cited |= steps
        if not steps & spec["triggers"] or steps - usable:
            problems.append("next_step not tied to an available trigger record")
        if not isinstance(data["summary"], str) or not 0 < len(data["summary"]) <= 400:
            problems.append("summary missing or over 400 characters")
        if cited - valid:
            problems.append("cited a record that is not in the dossier")
        return {"outcome": finance.FAILED if problems else finance.PASSED,
                "problems": problems}
    except (ValueError, TypeError) as error:
        return {"outcome": finance.MALFORMED, "problems": [str(error)]}


def case_metadata(value, key):
    """Describe the brief task in the existing review and finding records."""
    value = copy.deepcopy(value)
    value["claim"] = SPECS[key]["purpose"]
    value["command"] = "isolated employee: frozen " + key + " supplied-dossier prospect brief"
    value["scope"]["statement"] = ("One correlated prospect-brief answer, complete trace and "
                                   "confirmed shutdown.")
    value["scope"]["entry"] = ("cloud run start/get on the existing verified supplied-data "
                               "fixture; no setup mutation")
    value["basis"][0]["reference"] = "bd_brief: frozen dossier, rules and expected " + key
    for criterion in value["criteria"]:
        if criterion["id"] == "answer":
            criterion["evidence"] = (
                "All text events joined in order and scored by bd_brief.check: exact facts "
                "with supporting record IDs, conflicts, unknowns with reasons, criteria and "
                "recommendation category; next step tied to a trigger record. Free-text "
                "usefulness is reviewed separately with the documented rubric.")
    finding = value["findings"]["employee_answer_result"]
    finding.update(
        title="Employee prospect brief lacks correct, attributed, tool-free, cleanly finished result",
        expected="Attribute every fact to a supporting record, resolve conflicts by recency, "
                 "keep missing or unavailable facts unknown, qualify by the stated rules; "
                 "no tool activity; shutdown.",
        impact="An account executive could act on a fabricated or unsupported fact or misjudge "
               "whether to pursue a prospect (customer exposure unknown).")
    return value
