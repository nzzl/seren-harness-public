"""Offline reconciliation evidence: independent oracle, hostile answers, full executor."""
import copy
import csv
from datetime import date, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from seren_harness import employee_tool, invoice_campaign as campaign, invoice_catalog as catalog
from seren_harness import invoice_conflicts as conflicts
from seren_harness.cli import main
import test_finance_employee as finance_tests
import test_invoice_campaign as campaign_tests


def independent_answer(key):
    """Resolve the CSV using integer cents, without the production checker/expected data."""
    groups = {}
    for row in csv.DictReader(io.StringIO(conflicts.CASES[key]["ledger"])):
        groups.setdefault(row["invoice_id"], []).append(row)
    resolved, issues = [], []
    total = overdue_total = 0
    for ident, records in sorted(groups.items()):
        revisioned = all(r["revision"] for r in records)
        if revisioned:
            latest = max(int(r["revision"]) for r in records)
            records = [r for r in records if int(r["revision"]) == latest]
        if len({tuple(sorted(r.items())) for r in records}) > 1:
            issues.append({"invoice_id": ident, "reason": "conflicting_latest_revision"
                           if revisioned else "conflicting_records"})
            continue
        row = records[0]
        missing = [f for f in ("paid", "due_date", "currency") if not row[f]]
        if missing:
            issues.extend({"invoice_id": ident, "reason": "missing_" + f} for f in missing)
            continue
        cents = int(Decimal(row["amount"]) * 100) - int(Decimal(row["paid"]) * 100)
        delta = (date(2026, 10, 14) - date.fromisoformat(row["due_date"])).days
        overdue = delta > 0 and cents > 0
        total += cents
        overdue_total += cents if overdue else 0
        resolved.append({"invoice_id": ident, "outstanding_usd": f"{cents // 100}.{cents % 100:02}",
                         "overdue": overdue, "days_overdue": delta if overdue else 0})
    return {"as_of": "2026-10-14", "status": "needs_review" if issues else "complete",
            "resolved_invoices": resolved, "issues": issues,
            "known_outstanding_usd": f"{total // 100}.{total % 100:02}",
            "known_overdue_usd": f"{overdue_total // 100}.{overdue_total % 100:02}",
            "complete_outstanding_usd": None, "complete_overdue_usd": None}


class ReconciliationChecks(unittest.TestCase):
    def test_review_exports_match_executable_inputs_expectations_and_manifest(self):
        root = campaign.ROOT / "docs/finance-invoices/conflicts-v1"
        for key in conflicts.CASES:
            self.assertEqual((root / (key + ".input.txt")).read_text().strip(), catalog.message(key))
            self.assertEqual(json.loads((root / (key + ".expected.json")).read_text()), catalog.expected(key))
        self.assertEqual(json.loads((root / "manifest.json").read_text()), catalog.manifest("conflicts-v1"))

    def test_frozen_expectations_follow_snapshot_and_missing_data_rules(self):
        for key in conflicts.CASES:
            self.assertEqual(conflicts.expected(key), independent_answer(key))
            self.assertEqual(conflicts.check(key, json.dumps(independent_answer(key)))["outcome"], "passed")
            employee_tool.validate(catalog.case(key))

    def test_order_and_whitespace_are_not_customer_failures(self):
        for key in conflicts.CASES:
            answer = conflicts.expected(key)
            answer["issues"].reverse()
            answer["resolved_invoices"].reverse()
            text = "```json\n" + json.dumps(answer, indent=2) + "\n```"
            self.assertEqual(conflicts.check(key, text)["outcome"], "passed")

    def test_whole_answer_is_checked_including_prose_extra_keys_and_duplicates(self):
        key = "missing-fields"
        text = json.dumps(conflicts.expected(key))
        for bad in ("Assume USD. " + text, text + " Invoice MISS-3 is overdue.",
                    text + text, '{"status":"complete",' + text[1:],
                    text.replace('"days_overdue": 1', '"days_overdue": 1, "days_overdue": 1')):
            self.assertEqual(conflicts.check(key, bad)["outcome"], "malformed")
        answer = conflicts.expected(key)
        answer["assumption"] = "Missing payments are zero"
        self.assertEqual(conflicts.check(key, json.dumps(answer))["outcome"], "malformed")

    def test_unsupported_complete_totals_and_blanket_abstention_fail(self):
        for key in conflicts.CASES:
            for change in ({"complete_outstanding_usd": "0.00"}, {"complete_overdue_usd": "0.00"},
                           {"status": "complete"}, {"issues": []}, {"resolved_invoices": []},
                           {"known_outstanding_usd": "0.00"}, {"as_of": "2026-10-15"}):
                answer = conflicts.expected(key)
                answer.update(change)
                self.assertEqual(conflicts.check(key, json.dumps(answer))["outcome"], "failed", change)

    def test_first_last_or_summed_payment_choices_do_not_pass(self):
        key = "payment-revisions"
        for amount in ("900.00", "200.00", "0.00", "600.00"):
            answer = conflicts.expected(key)
            answer["resolved_invoices"][0]["outstanding_usd"] = amount
            self.assertEqual(conflicts.check(key, json.dumps(answer))["outcome"], "failed")
        for key, ident in (("duplicate-conflict", "DUP-1"), ("payment-revisions", "REV-2"),
                           ("missing-fields", "MISS-3")):
            answer = conflicts.expected(key)
            answer["resolved_invoices"].append(dict(invoice_id=ident, outstanding_usd="100.00",
                                                   overdue=True, days_overdue=13))
            self.assertEqual(conflicts.check(key, json.dumps(answer))["outcome"], "failed")

    def test_exact_issue_identity_reason_and_scalar_types_required(self):
        key = "missing-fields"
        for field, value in (("invoice_id", "MISS-99"), ("reason", "missing_paid")):
            answer = conflicts.expected(key)
            answer["issues"][2][field] = value
            self.assertEqual(conflicts.check(key, json.dumps(answer))["outcome"], "failed")
        for field, value in (("overdue", 1), ("days_overdue", True), ("outstanding_usd", 80)):
            answer = conflicts.expected(key)
            answer["resolved_invoices"][0][field] = value
            self.assertEqual(conflicts.check(key, json.dumps(answer))["outcome"], "malformed")
        for field in ("issues", "resolved_invoices"):
            answer = conflicts.expected(key)
            answer[field].append(copy.deepcopy(answer[field][0]))
            self.assertEqual(conflicts.check(key, json.dumps(answer))["outcome"], "malformed")
        self.assertEqual(conflicts.check(key, "")["outcome"], "no_answer")


class ConflictCampaignBoundaries(campaign_tests.CampaignTests):
    """Re-run existing replay, interruption, time and failure-stop tests on the new suite."""
    def setUp(self):
        super().setUp()
        self.proposal = campaign.package({"state_dir": self.tmp.name}, "conflicts-v1")
        self.approval["package_sha256"] = campaign.digest(self.proposal)

    def test_suite_case_order_and_expected_digest_cannot_change_after_authorization(self):
        for change in ("suite", "cases", "catalog"):
            proposal = copy.deepcopy(self.proposal)
            if change == "suite":
                proposal["suite"] = "arithmetic-v1"
            elif change == "cases":
                proposal["cases"].reverse()
            else:
                proposal["catalog"]["missing-fields"]["expected_sha256"] = "0" * 64
            approval = dict(self.approval, package_sha256=campaign.digest(proposal))
            with self.assertRaises(ValueError):
                campaign.execute(proposal, approval, self.runner, wall=lambda: self.now.isoformat())
        self.assertFalse(self.calls)
        self.assertEqual(campaign.package({}, None)["cases"], list(catalog.CASES))


class Integration(unittest.TestCase):
    def fixture(self):
        f = finance_tests.FinanceRunTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_prepare_is_offline_and_pins_only_the_new_suite(self):
        f = self.fixture()
        path, output = f.f.root / "session.json", f.f.root / "proposal.json"
        path.write_text(json.dumps(f.f.session))
        with mock.patch.object(campaign.employee_run, "pinned_cli", side_effect=AssertionError("live entry")):
            self.assertEqual(main(["invoice-campaign", "prepare", "--suite", "conflicts-v1",
                                   "--session", str(path), "--output", str(output)]), 0)
        proposal = json.loads(output.read_text())
        self.assertEqual(proposal["cases"], list(conflicts.CASES))
        self.assertEqual(proposal["catalog"], catalog.manifest("conflicts-v1"))
        self.assertNotIn("approved", proposal)

    def exercise(self, incorrect=False):
        f = self.fixture()
        proposal = campaign.package(f.f.session, "conflicts-v1")
        now = employee_tool._utc(f.f.driver()[0].wall())
        approval = {"approved": True, "authorization_id": "d1111111-1111-4111-8111-111111111111",
                    "package_sha256": campaign.digest(proposal), "issued_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=1800)).isoformat()}
        calls, reports = [], []
        def run(session, case):
            f.f.session, f.f.case = session, case
            key = case["employee_tool"]["catalog_case"]
            f.f.first["request"]["message"] = catalog.message(key)
            run = f.f.first["response"]["data"]
            run["invocation_payload"]["message"] = catalog.message(key)
            answer = independent_answer(key)
            if incorrect:
                answer["complete_outstanding_usd"] = answer["known_outstanding_usd"]
            run["output_events"] = finance_tests.events(json.dumps(answer))
            driver, cli = f.driver()
            campaign.employee_run.validate(session, case)
            self.assertEqual(f.f.execute(driver), 1 if incorrect else 0)
            calls.extend(cli.calls)
            reports.append(f.report(driver))
            saved = json.loads((Path(session["home"]) / driver.run_id / "invocation.json").read_text())
            self.assertEqual(saved["response"]["data"]["output_events"], run["output_events"])
            return {"checks": f.verdicts(driver), "service_invocations": sum(driver.counts.values()),
                    "cleanup_confirmed": not f.f.store.open_cleanup()}
        result = campaign.execute(proposal, approval, runner=run, wall=lambda: now.isoformat())
        self.assertEqual(result["status"], "stopped" if incorrect else "complete")
        self.assertEqual(sum("--async" in a for a in calls), 1 if incorrect else 3)
        self.assertEqual(sum("managed-stop" in a for a in calls), 1 if incorrect else 3)
        self.assertFalse(f.f.store.open_cleanup())
        if incorrect:
            self.assertEqual(reports[0]["verdict"], "failed")
            self.assertTrue(reports[0]["findings"])

    def test_three_reconciliation_answers_execute_score_and_stop_without_handoff(self):
        self.exercise()

    def test_false_complete_total_preserves_failure_and_stops_campaign_after_cleanup(self):
        self.exercise(incorrect=True)
