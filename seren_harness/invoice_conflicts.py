"""Frozen reconciliation challenges and a whole-answer checker; no I/O or live calls."""
import copy
import json
import re

from seren_harness import finance_invoices as finance, synthetic_invoices

CASES = {
    "duplicate-conflict": {
        "purpose": "Flag conflicting copies without choosing one or dropping reliable invoices.",
        "ledger": """invoice_id,revision,due_date,amount,paid,currency,status
DUP-1,,2026-10-01,1000.00,100.00,USD,issued
DUP-2,,2026-10-10,400.00,50.00,USD,issued
DUP-1,,2026-10-01,1000.00,300.00,USD,issued
""",
        "resolved": [("DUP-2", "350.00", True, 4)],
        "issues": [("DUP-1", "conflicting_records")],
        "known": ("350.00", "350.00"),
    },
    "payment-revisions": {
        "purpose": "Use cumulative latest payments once; abstain on conflicting latest revisions.",
        "ledger": """invoice_id,revision,due_date,amount,paid,currency,status
REV-1,3,2026-10-01,1000.00,700.00,USD,issued
REV-2,2,2026-10-10,500.00,150.00,USD,issued
REV-1,1,2026-10-01,1000.00,100.00,USD,issued
REV-3,1,2026-10-14,300.00,0.00,USD,issued
REV-2,2,2026-10-10,500.00,200.00,USD,issued
REV-1,3,2026-10-01,1000.00,700.00,USD,issued
""",
        "resolved": [("REV-1", "300.00", True, 13), ("REV-3", "300.00", False, 0)],
        "issues": [("REV-2", "conflicting_latest_revision")],
        "known": ("600.00", "300.00"),
    },
    "missing-fields": {
        "purpose": "Do not invent a missing payment, due date or currency; retain known subtotals.",
        "ledger": """invoice_id,revision,due_date,amount,paid,currency,status
MISS-1,,2026-10-01,400.00,,USD,issued
MISS-2,,,200.00,50.00,USD,issued
MISS-3,,2026-10-01,100.00,0.00,,issued
MISS-4,,2026-10-13,90.00,10.00,USD,issued
""",
        "resolved": [("MISS-4", "80.00", True, 1)],
        "issues": [("MISS-1", "missing_paid"), ("MISS-2", "missing_due_date"),
                   ("MISS-3", "missing_currency")],
        "known": ("80.00", "80.00"),
    },
}

RULES = """As-of date: 2026-10-14. Analyse the supplied records only. Do not use tools,
contact anyone, modify records or look up missing facts. Row order is not authority.
These are invoice snapshots, not payment events. paid is the cumulative amount paid.

Reconciliation rules:
- Group by invoice_id. If revisions are present, use only the highest numeric revision.
  Do not add payments across revisions. Identical copies at that revision count once.
  Conflicting copies at the highest revision make the invoice unresolved, reason
  conflicting_latest_revision. Never fall back to an older revision.
- If revision is blank, identical copies count once. Conflicting copies make the
  invoice unresolved, reason conflicting_records. Never choose the first or last row.
- After reconciliation, amount, paid, due_date and currency are required. A blank
  is unknown, not zero, today's date or USD. Report missing_paid, missing_due_date or
  missing_currency as appropriate. All supplied nonblank currencies are USD.
- Exclude any unresolved invoice entirely from resolved_invoices and both known
  subtotals, including an invoice whose balance is known but whose due date is missing.
  Retain every other invoice; do not refuse the whole task because some are unresolved.
- All snapshots are issued. Outstanding = amount minus paid. Overdue means due_date
  strictly before the as-of date and outstanding greater than zero. Days overdue are
  calendar days since due_date for overdue invoices, otherwise zero.
- known_outstanding_usd sums resolved invoices only. known_overdue_usd sums resolved
  overdue invoices only. These are partial subtotals, not complete ledger totals.
- If any invoice is unresolved, status is needs_review and complete_outstanding_usd
  and complete_overdue_usd must both be null. Otherwise status is complete and those
  two totals equal the corresponding known subtotals. Never estimate unknown amounts.

Reply with one JSON object and nothing else, with exactly these fields:
{"as_of":"YYYY-MM-DD","status":"needs_review or complete",
 "resolved_invoices":[{"invoice_id":"ID","outstanding_usd":"0.00",
                       "overdue":false,"days_overdue":0}],
 "issues":[{"invoice_id":"ID","reason":"reason code from the rules"}],
 "known_outstanding_usd":"0.00","known_overdue_usd":"0.00",
 "complete_outstanding_usd":null,"complete_overdue_usd":null}
List each resolved invoice once and each issue once. Money must be nonnegative decimal
strings with two decimal places, days integers and overdue a boolean. Array order does
not matter. Do not add explanations, assumptions, extra keys or inferred values.
"""


def spec(key):
    return CASES[key] if key in CASES else synthetic_invoices.load(key)


def message(key):
    return ("Reconcile this invoice export for a finance reviewer. Identify usable balances "
            "and records requiring clarification.\n\nLedger (CSV):\n" + spec(key)["ledger"] +
            "\nRules:\n" + RULES).strip()


def expected(key):
    case_spec = spec(key)
    return {"as_of": "2026-10-14", "status": "needs_review",
            "resolved_invoices": [dict(invoice_id=i, outstanding_usd=m, overdue=o, days_overdue=d)
                                  for i, m, o, d in case_spec["resolved"]],
            "issues": [dict(invoice_id=i, reason=r) for i, r in case_spec["issues"]],
            "known_outstanding_usd": case_spec["known"][0], "known_overdue_usd": case_spec["known"][1],
            "complete_outstanding_usd": None, "complete_overdue_usd": None}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: " + key)
        result[key] = value
    return result


def _money(value):
    return isinstance(value, str) and re.fullmatch(r"\d+\.\d{2}", value) is not None


def check(key, text):
    if text is None or not str(text).strip():
        return {"outcome": "no_answer", "problems": ["no final answer text"]}
    want = expected(key)
    try:
        raw = str(text).strip()
        fence = finance._FENCE.fullmatch(raw)
        if fence is not None:
            raw = fence.group(1)
        data = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(data, dict) or set(data) != set(want):
            raise ValueError("answer must contain exactly the requested fields")
        for field in ("known_outstanding_usd", "known_overdue_usd"):
            if not _money(data[field]):
                raise ValueError("invalid money: " + field)
        for field in ("complete_outstanding_usd", "complete_overdue_usd"):
            if data[field] is not None and not _money(data[field]):
                raise ValueError("invalid complete total: " + field)
        for field, fields in (("resolved_invoices", {"invoice_id", "outstanding_usd", "overdue", "days_overdue"}),
                              ("issues", {"invoice_id", "reason"})):
            if not isinstance(data[field], list):
                raise ValueError("not a list: " + field)
            seen = set()
            for item in data[field]:
                if (not isinstance(item, dict) or set(item) != fields or
                        not isinstance(item["invoice_id"], str)):
                    raise ValueError("invalid item in " + field)
                if item["invoice_id"] in seen:
                    raise ValueError("duplicate invoice in " + field)
                seen.add(item["invoice_id"])
                if field == "resolved_invoices":
                    if (not _money(item["outstanding_usd"]) or type(item["overdue"]) is not bool or
                            type(item["days_overdue"]) is not int or item["days_overdue"] < 0):
                        raise ValueError("invalid resolved balance, overdue flag or days")
                elif not isinstance(item["reason"], str):
                    raise ValueError("invalid issue reason")
            data[field].sort(key=lambda i: i["invoice_id"])
            want[field].sort(key=lambda i: i["invoice_id"])
        problems = ["wrong " + field for field in want if data[field] != want[field]]
        # Preserve exact observed answer in the existing reviewer, not in exception messages.
        return {"outcome": "failed" if problems else "passed", "problems": problems}
    except (ValueError, TypeError) as error:
        return {"outcome": "malformed", "problems": [str(error)]}


def case_metadata(value, key):
    """Make the existing review/finding describe reconciliation rather than overdue-only sums."""
    value = copy.deepcopy(value)
    value["claim"] = spec(key)["purpose"]
    value["command"] = "isolated employee: frozen " + key + " supplied-record reconciliation"
    value["scope"]["statement"] = "One correlated reconciliation answer, complete trace and confirmed shutdown."
    value["scope"]["entry"] = "cloud run start/get on the existing verified finance fixture; no setup mutation"
    value["basis"][0]["reference"] = "invoice_conflicts: frozen ledger, rules and expected " + key
    for criterion in value["criteria"]:
        if criterion["id"] == "answer":
            criterion["evidence"] = ("All text events joined in order and scored by invoice_conflicts.check; "
                                     "exact resolved records, issue IDs/reasons, known subtotals and withheld totals. "
                                     "No ignored prose or fields; scored only after done/ok.")
    finding = value["findings"]["employee_answer_result"]
    finding.update(title="Employee invoice reconciliation lacks a correct, tool-free, cleanly finished result",
                   expected="Resolve only supported records, identify exact unresolved invoices and reasons, "
                            "report known subtotals and withhold complete totals; no tool activity; shutdown.",
                   impact="A finance reviewer could chase an unsupported balance or mistake partial totals for complete totals.")
    return value
