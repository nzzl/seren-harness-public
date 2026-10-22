"""Overdue-invoice case: frozen synthetic ledger, independent oracle and answer checker.

Deterministic, offline and model-free. Money is compared as Decimal, dates as calendar
dates. The checker scores one employee answer; it does not run the employee, and a
synthetic pass is not evidence that an employee can do the task.
"""
from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re

FIXTURE = Path(__file__).resolve().parents[1] / "docs" / "finance-invoices"
FROZEN = ("ledger.csv", "rules.md", "task-input.txt", "expected.json")
AS_OF = dt.date(2026, 10, 14)
CENTS = Decimal("0.01")

# Outcomes. Execution or access failures and cleanup are reported separately by the
# run, never by this answer check.
PASSED, FAILED, MALFORMED, NO_ANSWER = "passed", "failed", "malformed", "no_answer"

TOP = ("as_of", "overdue", "overdue_count", "overdue_outstanding_usd",
       "total_outstanding_usd", "not_yet_overdue_outstanding_usd")
ITEM = ("invoice_id", "outstanding_usd", "days_overdue")
MONEY = ("overdue_outstanding_usd", "total_outstanding_usd", "not_yet_overdue_outstanding_usd")
_MONEY_TEXT = re.compile(r"\d+(\.\d+)?")
_FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*)\n[ \t]*```", re.S)


def digests(folder=FIXTURE):
    return {name: hashlib.sha256((Path(folder) / name).read_bytes()).hexdigest()
            for name in FROZEN}


def ledger(folder=FIXTURE):
    text = (Path(folder) / "ledger.csv").read_text()
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        rows.append({"invoice_id": row["invoice_id"], "customer": row["customer"],
                     "due_date": dt.date.fromisoformat(row["due_date"]),
                     "amount": Decimal(row["amount_usd"]), "paid": Decimal(row["paid_usd"]),
                     "status": row["status"]})
    return rows


def oracle(rows, as_of=AS_OF):
    """The expected answer from the written rules, in Decimal and calendar dates."""
    overdue, total, later = [], Decimal(0), Decimal(0)
    for row in rows:
        if row["status"] != "issued":
            continue  # void and any other status are excluded entirely
        balance = row["amount"] - row["paid"]
        total += balance
        if row["due_date"] < as_of and balance > 0:
            overdue.append({"invoice_id": row["invoice_id"], "outstanding_usd": balance,
                            "days_overdue": (as_of - row["due_date"]).days})
        else:
            later += balance
    overdue.sort(key=lambda item: item["invoice_id"])
    return {"as_of": as_of, "overdue": overdue, "overdue_count": len(overdue),
            "overdue_outstanding_usd": sum((i["outstanding_usd"] for i in overdue), Decimal(0)),
            "total_outstanding_usd": total, "not_yet_overdue_outstanding_usd": later}


def frozen_expected(folder=FIXTURE):
    return _normalize(json.loads((Path(folder) / "expected.json").read_text(),
                                 parse_float=Decimal))


def _money(value):
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str):
        if not _MONEY_TEXT.fullmatch(value.strip()):
            raise ValueError
        value = Decimal(value.strip())
    elif isinstance(value, int):
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite() or value != value.quantize(CENTS):
        raise ValueError
    return value


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _normalize(data):
    """Typed answer, or ValueError naming the first structural problem."""
    if not isinstance(data, dict):
        raise ValueError("answer is not a JSON object")
    missing = [k for k in TOP if k not in data]
    if missing:
        raise ValueError("missing field: " + missing[0])
    if not isinstance(data["overdue"], list):
        raise ValueError("overdue is not a list")
    try:
        as_of = dt.date.fromisoformat(data["as_of"]) if isinstance(data["as_of"], str) else None
    except ValueError:
        as_of = None
    if as_of is None:
        raise ValueError("as_of is not a calendar date")
    items = []
    for item in data["overdue"]:
        if not isinstance(item, dict) or any(k not in item for k in ITEM):
            raise ValueError("overdue item lacks invoice_id, outstanding_usd or days_overdue")
        if not isinstance(item["invoice_id"], str):
            raise ValueError("invoice_id is not a string")
        try:
            items.append({"invoice_id": item["invoice_id"].strip().upper(),
                          "outstanding_usd": _money(item["outstanding_usd"]),
                          "days_overdue": _integer(item["days_overdue"]),
                          "extra": sorted(set(item) - set(ITEM))})
        except (ValueError, InvalidOperation):
            raise ValueError("overdue item money or days not exact") from None
    try:
        values = {k: _money(data[k]) for k in MONEY}
        count = _integer(data["overdue_count"])
    except (ValueError, InvalidOperation):
        raise ValueError("totals or count not exact") from None
    return dict(values, as_of=as_of, overdue=items, overdue_count=count,
                extra=sorted(set(data) - set(TOP)))


def parse(text):
    """One JSON object filling the reply, or one ```json fence filling it; no other text."""
    stripped = text.strip()
    fence = _FENCE.fullmatch(stripped)
    if fence is not None and "```" not in fence.group(1):
        candidate = fence.group(1)
    elif stripped.startswith("```"):
        raise ValueError("reply is not exactly one closed ```json fence with nothing around it")
    elif stripped.startswith("{"):
        candidate = stripped
    else:
        raise ValueError("reply has text outside the JSON object; send only the object "
                         "or one ```json fence")
    candidate = candidate.strip()
    try:
        data, end = json.JSONDecoder(parse_float=Decimal).raw_decode(candidate)
    except ValueError:
        raise ValueError("reply is not valid JSON") from None
    if candidate[end:].strip():
        raise ValueError("reply has text after the JSON object; send exactly one object")
    return _normalize(data)


def check(text, rows=None, expected=None):
    """{"outcome", "problems"}; problems name each detected semantic error."""
    if text is None or not str(text).strip():
        return {"outcome": NO_ANSWER, "problems": ["no final answer text"]}
    try:
        answer = parse(str(text))
    except ValueError as error:
        return {"outcome": MALFORMED, "problems": [str(error)]}
    rows = ledger() if rows is None else rows
    expected = frozen_expected() if expected is None else expected
    known = {r["invoice_id"]: r for r in rows}
    want = {i["invoice_id"]: i for i in expected["overdue"]}
    problems = []
    if answer["as_of"] != expected["as_of"]:
        problems.append("wrong as_of date")
    seen = set()
    for item in answer["overdue"]:
        invoice = item["invoice_id"]
        if invoice in seen:
            problems.append("duplicate invoice: " + invoice)
            continue
        seen.add(invoice)
        if item["extra"]:
            problems.append("unrequested fields on " + invoice + ": " + ", ".join(item["extra"]))
        row = known.get(invoice)
        if row is None:
            problems.append("invented invoice: " + invoice)
        elif invoice not in want:
            problems.append(_why_not_overdue(row, expected["as_of"]))
        else:
            if item["outstanding_usd"] != want[invoice]["outstanding_usd"]:
                problems.append(("gross amount instead of balance: " if item["outstanding_usd"]
                                 == row["amount"] else "wrong balance: ") + invoice)
            if item["days_overdue"] != want[invoice]["days_overdue"]:
                problems.append("wrong days overdue: " + invoice)
    problems += ["missing overdue invoice: " + i for i in sorted(set(want) - seen)]
    if answer["overdue_count"] != expected["overdue_count"]:
        problems.append("wrong overdue_count")
    for key in MONEY:
        if answer[key] != expected[key]:
            problems.append("wrong " + key)
    if answer["extra"]:
        problems.append("unrequested fields: " + ", ".join(answer["extra"]))
    return {"outcome": FAILED if problems else PASSED, "problems": problems}


## Clock Test fixture binding for the `employee-data` supplied_answer case ------------------
# The instruction is installed by `instruction-update` with this package; the executor
# accepts it only through that journal's verified read-back, never a boolean claim.
TARGET = "7a000021-0000-4000-8000-000000000022"
SCOPE = "supplied-answer-finance-v1"
PACKAGE = FIXTURE / "instruction-update.proposed.json"
PACKAGE_ID = "seren.finance.clock-instruction"
BRANCH_CASE = FIXTURE.parents[1] / "cases" / "seren-employee-branch-read.json"


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def message():
    """task-input.txt as the pinned CLI sends it: `cloud run start --message` trims it."""
    return (FIXTURE / "task-input.txt").read_text().strip()


def instruction():
    """SKILL.finance.md as stored: the CLI trims `--prompt` before the update."""
    return (FIXTURE / "SKILL.finance.md").read_text().strip()


def verify_detail(detail):
    bundle = detail.get("bundle")
    instructions = bundle.get("instructions") if isinstance(bundle, dict) else None
    if not isinstance(instructions, list) or not any(
            isinstance(i, dict) and i.get("kind") == "skill" and i.get("content") == instruction()
            for i in instructions):
        raise ValueError("finance instruction not installed")


def verify_update(basis, baseline):
    """The digest-bound instruction-update journal must show this prompt-only update applied
    and read back, stopped, on the session's revision, from the verified branch baseline."""
    previous = basis["branch_baseline"]
    if any(previous[k] != baseline[k] for k in baseline if k != "active_revision_id"):
        raise ValueError("finance update changed existing fixture controls")
    ref = basis["instruction_update"]["journal"]
    raw = Path(ref["path"]).read_bytes()
    if _sha(raw) != ref["sha256"]:
        raise ValueError("instruction-update journal changed")
    journal = json.loads(raw)
    package = json.loads(PACKAGE.read_text())
    binding = journal.get("binding") or {}
    seen = journal.get("observations") or {}
    before, after = seen.get("prerequisite") or {}, seen.get("read_back") or {}
    trimmed = _sha(instruction())
    target = baseline["deployment_id"]
    if (package["id"] != PACKAGE_ID or journal.get("package") != PACKAGE_ID
            or package["target"]["deployment_id"] != target
            or journal.get("deployment") != target or binding.get("deployment_id") != target
            or binding.get("package_sha256") != _sha(PACKAGE.read_bytes())
            or package["correction"]["corrected_trimmed_sha256"] != trimmed
            or binding.get("corrected_trimmed_sha256") != trimmed
            or journal.get("result") != "proceed"
            or (journal.get("update") or {}).get("outcome") != "applied_verified"
            or package["baseline"]["active_revision_id"] != previous["active_revision_id"]
            or before.get("active_revision_id") != previous["active_revision_id"]
            or baseline["active_revision_id"] == previous["active_revision_id"]
            or after.get("deployment_id") != target
            or after.get("active_revision_id") != baseline["active_revision_id"]
            or after.get("status") != "stopped"
            or after.get("instruction_trimmed_sha256") != trimmed):
        raise ValueError("instruction-update journal does not establish the finance setup")


def _why_not_overdue(row, as_of):
    if row["status"] != "issued":
        return "void invoice counted: " + row["invoice_id"]
    if row["amount"] - row["paid"] <= 0:
        return "paid invoice counted: " + row["invoice_id"]
    if row["due_date"] == as_of:
        return "invoice due on the as-of date counted as overdue: " + row["invoice_id"]
    return "not-yet-due invoice counted as overdue: " + row["invoice_id"]
