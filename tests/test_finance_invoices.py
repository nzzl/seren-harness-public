"""Overdue-invoice case: frozen fixture, independent oracle and answer checker.

SYNTHETIC: every answer below is written here; no Seren call, model or employee run.
A pass here verifies the checker, not that an employee can do the task.
"""
import copy
import datetime as dt
import json
import unittest

from seren_support import SOURCE
from seren_harness import finance_invoices as fi

FOLDER = SOURCE / "docs/finance-invoices"
EXPECTED = json.loads((FOLDER / "expected.json").read_text())


def answer(mutate=None):
    data = copy.deepcopy(EXPECTED)
    if mutate:
        mutate(data)
    return json.dumps(data)


def item(invoice, outstanding, days):
    return {"invoice_id": invoice, "outstanding_usd": outstanding, "days_overdue": days}


class Fixture(unittest.TestCase):
    def test_frozen_files_match_the_manifest(self):
        manifest = json.loads((FOLDER / "manifest.json").read_text())
        self.assertEqual(manifest["sha256"], fi.digests())

    def test_independent_oracle_reproduces_the_frozen_answer(self):
        result = fi.oracle(fi.ledger())
        frozen = fi.frozen_expected()
        for entry in frozen["overdue"]:
            entry.pop("extra")
        frozen.pop("extra")
        self.assertEqual(result, frozen)
        # Integer cents and calendar dates, computed separately from the oracle.
        cents = {"INV-001": 120000, "INV-002": 50000, "INV-003": 45000, "INV-004": 200000,
                 "INV-005": 0, "INV-007": 10000, "INV-008": 5025}
        self.assertEqual(sum(cents.values()), 430025)
        self.assertEqual(cents["INV-001"] + cents["INV-002"] + cents["INV-007"], 180000)
        as_of = dt.date(2026, 10, 14)
        self.assertEqual([(as_of - dt.date(2026, 10, d)).days for d in (1, 10, 13)],
                         [13, 4, 1])

    def test_task_input_carries_ledger_rules_and_format_but_not_the_answer(self):
        text = (FOLDER / "task-input.txt").read_text()
        self.assertIn((FOLDER / "ledger.csv").read_text(), text)
        self.assertIn((FOLDER / "rules.md").read_text(), text)
        self.assertIn('"not_yet_overdue_outstanding_usd"', text)
        for value in ("1800", "4300", "2500", "500.00", "100.00", "INV-001\"", "13 days"):
            self.assertNotIn(value, text)


class Checker(unittest.TestCase):
    def assertProblem(self, text, *expected):
        result = fi.check(text)
        self.assertEqual(result["outcome"], fi.FAILED, result)
        for problem in expected:
            self.assertIn(problem, result["problems"])

    def test_the_exact_answer_passes(self):
        self.assertEqual(fi.check(answer()), {"outcome": fi.PASSED, "problems": []})

    def test_harmless_order_and_format_differences_pass(self):
        def reorder(data):
            data["overdue"].reverse()
            data["overdue"][0]["invoice_id"] = " inv-007 "
            data["overdue_outstanding_usd"] = "1800"
        text = json.dumps(json.loads(answer(reorder)), indent=2, sort_keys=True)
        reply = "```json\n" + text.replace('"4300.25"', "4300.250") + "\n```"
        self.assertIn("4300.250", reply)
        self.assertEqual(fi.check(reply)["outcome"], fi.PASSED)

    def test_missing_or_extra_overdue_invoices(self):
        self.assertProblem(answer(lambda d: d["overdue"].pop(1)),
                           "missing overdue invoice: INV-002")
        self.assertProblem(answer(lambda d: d["overdue"].append(item("INV-009", "10.00", 2))),
                           "invented invoice: INV-009")

    def test_paid_void_and_due_today_invoices_are_not_overdue(self):
        for invoice, days, problem in (
                ("INV-005", 14, "paid invoice counted: INV-005"),
                ("INV-006", 9, "void invoice counted: INV-006"),
                ("INV-003", 0, "invoice due on the as-of date counted as overdue: INV-003"),
                ("INV-008", 0, "not-yet-due invoice counted as overdue: INV-008")):
            with self.subTest(invoice=invoice):
                self.assertProblem(answer(lambda d: d["overdue"].append(
                    item(invoice, "1.00", days))), problem)

    def test_gross_amounts_dates_counts_and_totals(self):
        def gross(data):
            data["overdue"][1]["outstanding_usd"] = "800.00"
            data["overdue_outstanding_usd"] = "2100.00"
        self.assertProblem(answer(gross), "gross amount instead of balance: INV-002",
                           "wrong overdue_outstanding_usd")
        self.assertProblem(answer(lambda d: d["overdue"][2].update(outstanding_usd="100.01")),
                           "wrong balance: INV-007")
        self.assertProblem(answer(lambda d: d["overdue"][0].update(days_overdue=14)),
                           "wrong days overdue: INV-001")
        self.assertProblem(answer(lambda d: d.update(as_of="2026-10-15")), "wrong as_of date")
        self.assertProblem(answer(lambda d: d.update(overdue_count=4)), "wrong overdue_count")
        for key, value in (("total_outstanding_usd", "5200.25"),  # void included
                           ("not_yet_overdue_outstanding_usd", "2050.25")):
            with self.subTest(key=key):
                self.assertProblem(answer(lambda d: d.update({key: value})), "wrong " + key)

    def test_duplicates_and_invented_facts(self):
        self.assertProblem(answer(lambda d: d["overdue"].append(item("INV-001", "1200.00", 13))),
                           "duplicate invoice: INV-001")
        self.assertProblem(answer(lambda d: d.update(late_fees_usd="25.00")),
                           "unrequested fields: late_fees_usd")
        self.assertProblem(answer(lambda d: d["overdue"][0].update(customer_contacted=True)),
                           "unrequested fields on INV-001: customer_contacted")

    def test_malformed_incomplete_and_absent_answers_are_distinct(self):
        for text in ("The overdue invoices are INV-001, INV-002 and INV-007.",
                     answer()[:-1],
                     answer() + "\n" + answer(),
                     answer(lambda d: d.pop("total_outstanding_usd")),
                     answer(lambda d: d["overdue"][0].pop("days_overdue")),
                     answer(lambda d: d.update(overdue_outstanding_usd=1800.005)),
                     answer(lambda d: d.update(overdue_outstanding_usd="$1,800.00")),
                     answer(lambda d: d.update(overdue_count="3")),
                     answer(lambda d: d.update(as_of="14/10/2026"))):
            with self.subTest(text=text[:60]):
                self.assertEqual(fi.check(text)["outcome"], fi.MALFORMED)
        for text in (None, "", "  \n"):
            self.assertEqual(fi.check(text)["outcome"], fi.NO_ANSWER)


class ReplyBoundary(unittest.TestCase):
    """The object or its one fence must be the whole reply; no prose is ignored."""
    FENCED = "```json\n" + answer() + "\n```"

    def assertMalformed(self, text, reason):
        result = fi.check(text)
        self.assertEqual(result, {"outcome": fi.MALFORMED, "problems": [reason]})

    def test_a_standalone_object_or_fence_passes(self):
        for text in (answer(), "\n  " + answer() + "\n", self.FENCED,
                     "\n\n " + self.FENCED + " \n", "```\n" + answer() + "\n```"):
            with self.subTest(text=text[:20]):
                self.assertEqual(fi.check(text), {"outcome": fi.PASSED, "problems": []})

    def test_prose_around_a_correct_answer_is_rejected(self):
        outside = ("reply has text outside the JSON object; send only the object "
                   "or one ```json fence")
        closed = "reply is not exactly one closed ```json fence with nothing around it"
        after = "reply has text after the JSON object; send exactly one object"
        claim = "I contacted the customers about these invoices."
        for text, reason in (("Here is the result:\n" + self.FENCED, outside),
                             (self.FENCED + "\n" + claim, closed),
                             ("Summary below.\n" + self.FENCED + "\n" + claim, outside),
                             ("Result: " + answer(), outside),
                             (answer() + "\n" + claim, after)):
            with self.subTest(text=text[:30] + "…" + text[-30:]):
                self.assertMalformed(text, reason)

    def test_multiple_extra_or_unclosed_blocks_are_rejected(self):
        closed = "reply is not exactly one closed ```json fence with nothing around it"
        for text, reason in ((self.FENCED + "\n" + self.FENCED, closed),
                             ("```json\n" + answer() + "\n" + answer() + "\n```",
                              "reply has text after the JSON object; send exactly one object"),
                             ("```json\n" + answer(), closed),
                             ("```json\n" + answer() + "\n``", closed)):
            with self.subTest(text=text[-20:]):
                self.assertMalformed(text, reason)


if __name__ == "__main__":
    unittest.main()
