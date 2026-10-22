"""Finite supplied-record challenges; frozen before a campaign can be authorized.

No model generates the oracle. Each case stresses a different customer error while
reusing the installed supplied-data instruction and the existing finance checker.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import copy
import hashlib
import io
import json

from seren_harness import bd_brief
from seren_harness import finance_invoices as finance
from seren_harness import invoice_conflicts, synthetic_invoices


CASES = {
    "partial-payments": {
        "as_of": "2026-10-14",
        "purpose": "Do not chase paid or void invoices; subtract payments exactly.",
        "ledger": """invoice_id,customer,due_date,amount_usd,paid_usd,status
PAY-1,Aster,2026-10-01,999.99,999.98,issued
PAY-2,Birch,2026-10-13,1200.50,200.25,issued
PAY-3,Cedar,2026-10-02,700.00,700.00,issued
PAY-4,Dune,2026-09-01,5000.00,0.00,void
PAY-5,Elm,2026-10-15,10.10,0.10,issued
""",
        "expected": {"overdue": [("PAY-1", "0.01", 13), ("PAY-2", "1000.25", 1)],
                     "totals": ["1000.26", "1010.26", "10.00"]},
    },
    "calendar-boundaries": {
        "as_of": "2028-03-01",
        "purpose": "Handle leap day and due-today boundaries without using today's date.",
        "ledger": """invoice_id,customer,due_date,amount_usd,paid_usd,status
DAY-1,Fern,2028-02-28,100.00,0.00,issued
DAY-2,Grove,2028-02-29,200.00,50.00,issued
DAY-3,Harbor,2028-03-01,300.00,0.00,issued
DAY-4,Iris,2028-03-02,400.00,100.00,issued
DAY-5,Juniper,2027-12-31,50.00,0.00,issued
""",
        "expected": {"overdue": [("DAY-1", "100.00", 2), ("DAY-2", "150.00", 1),
                                  ("DAY-5", "50.00", 61)],
                     "totals": ["300.00", "900.00", "600.00"]},
    },
    "no-overdue": {
        "as_of": "2026-10-14",
        "purpose": "Return an empty overdue set rather than inventing collections work.",
        "ledger": """invoice_id,customer,due_date,amount_usd,paid_usd,status
ZERO-1,Kestrel,2026-10-14,80.00,20.00,issued
ZERO-2,Larch,2026-10-15,40.00,0.00,issued
ZERO-3,Maple,2026-10-01,100.00,100.00,issued
ZERO-4,Nettle,2026-09-01,900.00,0.00,void
""",
        "expected": {"overdue": [], "totals": ["0.00", "100.00", "100.00"]},
    },
}


def rows(case_id):
    return [{"invoice_id": r["invoice_id"], "customer": r["customer"],
             "due_date": date.fromisoformat(r["due_date"]),
             "amount": Decimal(r["amount_usd"]), "paid": Decimal(r["paid_usd"]),
             "status": r["status"]}
            for r in csv.DictReader(io.StringIO(CASES[case_id]["ledger"]))]


def expected(case_id):
    if case_id in bd_brief.SPECS:
        return bd_brief.expected(case_id)
    if case_id in invoice_conflicts.CASES or synthetic_invoices.known(case_id):
        return invoice_conflicts.expected(case_id)
    spec = CASES[case_id]
    answer = {"as_of": spec["as_of"], "overdue": [
        {"invoice_id": ident, "outstanding_usd": money, "days_overdue": days}
        for ident, money, days in spec["expected"]["overdue"]],
        "overdue_count": len(spec["expected"]["overdue"])}
    answer.update(zip(finance.MONEY, spec["expected"]["totals"]))
    return answer


def message(case_id):
    if case_id in bd_brief.SPECS:
        return bd_brief.message(case_id)
    if case_id in invoice_conflicts.CASES or synthetic_invoices.known(case_id):
        return invoice_conflicts.message(case_id)
    # Reuse the exact business rules and reply format already exercised live.
    original = finance.message()
    prefix, tail = original.split("Ledger (CSV):\n", 1)
    _, rules = tail.split("\nRules:\n", 1)
    rules = rules.replace("As-of date: 2026-10-14.",
                          "As-of date: " + CASES[case_id]["as_of"] + ".", 1)
    return prefix + "Ledger (CSV):\n" + CASES[case_id]["ledger"] + "\nRules:\n" + rules


def check(case_id, answer):
    if case_id in bd_brief.SPECS:
        return bd_brief.check(case_id, answer)
    if case_id in invoice_conflicts.CASES or synthetic_invoices.known(case_id):
        return invoice_conflicts.check(case_id, answer)
    return finance.check(answer, rows=rows(case_id),
                         expected=finance._normalize(expected(case_id)))


def case_ids(suite=None):
    if suite is None or suite == "arithmetic-v1":
        return tuple(CASES)
    if suite == "conflicts-v1":
        return tuple(invoice_conflicts.CASES)
    if suite in bd_brief.SUITES:
        return bd_brief.SUITES[suite]
    raise ValueError("unknown invoice suite")


def known(case_id):
    return isinstance(case_id, str) and (case_id in CASES or case_id in invoice_conflicts.CASES
                                         or case_id in bd_brief.SPECS or synthetic_invoices.known(case_id))


def manifest(suite=None):
    """Bind inputs AND expectations, not just the requested prose."""
    specs = {**CASES, **invoice_conflicts.CASES, **bd_brief.SPECS}
    return {key: {"purpose": specs[key]["purpose"],
                  "message_sha256": hashlib.sha256(message(key).encode()).hexdigest(),
                  "expected_sha256": hashlib.sha256(json.dumps(
                      expected(key), sort_keys=True).encode()).hexdigest()}
            for key in case_ids(suite)}


def case(case_id, deployment_id=None):
    """The frozen case bound to a fixture; employee_run.validate decides whether the
    session's setup evidence establishes that deployment (Clock Test by default)."""
    value = copy.deepcopy(json.loads((finance.FIXTURE.parents[1] /
                                     "cases/seren-employee-finance-invoices.json").read_text()))
    value["id"] = "seren.employee.invoice-" + case_id
    value["target_status"] = "resolved"
    value["claim"] = (synthetic_invoices.load(case_id)["purpose"] if synthetic_invoices.known(case_id)
                      else ({**CASES, **invoice_conflicts.CASES, **bd_brief.SPECS})[case_id]["purpose"])
    value["basis"][0]["reference"] = "invoice_catalog: frozen " + case_id
    spec = value["employee_tool"]
    spec.update(deployment_id=deployment_id or finance.TARGET, catalog_case=case_id,
                message=message(case_id),
                message_sha256=finance._sha(message(case_id)))
    if case_id in bd_brief.SPECS:
        return bd_brief.case_metadata(value, case_id)
    return invoice_conflicts.case_metadata(value, case_id) if case_id in invoice_conflicts.CASES or synthetic_invoices.known(case_id) else value


def digest(case_id, deployment_id=None):
    """Pin the full case and independent expectation across the queued handoff."""
    return hashlib.sha256(json.dumps({'case': case(case_id, deployment_id),
                                     'expected': expected(case_id)}, sort_keys=True).encode()).hexdigest()
