"""Offline: frozen financial challenges and unattended sequencing, no service access."""
import copy
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_harness import employee_tool, finance_invoices, invoice_campaign as campaign
from seren_harness import invoice_catalog as catalog
import test_finance_employee as finance_tests


class CatalogTests(unittest.TestCase):
    def test_frozen_answers_independently_calculated_in_integer_cents(self):
        for key, spec in catalog.CASES.items():
            expected = catalog.expected(key)
            total = overdue = later = 0
            items = []
            for row in catalog.rows(key):
                if row["status"] == "void":
                    continue
                cents = int(row["amount"] * 100) - int(row["paid"] * 100)
                total += cents
                days = (date.fromisoformat(spec["as_of"]) - row["due_date"]).days
                if days > 0 and cents > 0:
                    overdue += cents
                    items.append((row["invoice_id"], cents, days))
                else:
                    later += cents
            self.assertEqual([int(Decimal(expected[k]) * 100) for k in finance_invoices.MONEY],
                             [overdue, total, later])
            self.assertEqual([(r["invoice_id"], int(Decimal(r["outstanding_usd"]) * 100),
                               r["days_overdue"]) for r in expected["overdue"]], items)
            self.assertEqual(expected["overdue_count"], len(items))
            self.assertEqual(catalog.check(key, json.dumps(expected))["outcome"], "passed")
            self.assertIn("As-of date: " + spec["as_of"], catalog.message(key))

    def test_wrong_balance_prose_and_other_case_rejected(self):
        for key in catalog.CASES:
            answer = catalog.expected(key)
            answer["total_outstanding_usd"] = "99999.00"
            self.assertEqual(catalog.check(key, json.dumps(answer))["outcome"], "failed")
            self.assertEqual(catalog.check(key, "Contact customers now. " +
                                          json.dumps(catalog.expected(key)))["outcome"], "malformed")
        employee_tool.validate(catalog.case("calendar-boundaries"))
        case = catalog.case("calendar-boundaries")
        case["employee_tool"]["catalog_case"] = "no-overdue"
        with self.assertRaises(ValueError):
            employee_tool.validate(case)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = employee_tool._utc("2026-10-14T12:00:00Z")
        self.proposal = campaign.package({"state_dir": self.tmp.name})
        self.approval = {"approved": True,
                         "authorization_id": "a1111111-1111-4111-8111-111111111111",
                         "package_sha256": campaign.digest(self.proposal),
                         "issued_at": self.now.isoformat(),
                         "expires_at": (self.now + timedelta(seconds=1800)).isoformat()}
        self.calls = []
        patcher = mock.patch.object(campaign.employee_run, "validate")
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self, session, case):
        self.calls.append((session, case))
        return {"cleanup_confirmed": True, "service_invocations": 14,
                "checks": {k: "passed" for k in
                           ("run_identity", "answer", "no_tool_activity", "shutdown")}}

    def run_campaign(self, runner=None):
        return campaign.execute(self.proposal, self.approval, runner or self.runner,
                                wall=lambda: self.now.isoformat())

    def test_runs_three_different_tasks_without_handoff_and_never_replays(self):
        result = self.run_campaign()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({s["authorization_id"] for s, _ in self.calls}), 3)
        self.assertEqual(len({c["employee_tool"]["message"] for _, c in self.calls}), 3)
        self.assertEqual(self.run_campaign(), result)
        self.assertEqual(len(self.calls), 3)

    def test_wrong_answer_or_unclean_shutdown_stops_remaining_cases(self):
        for kind in ("answer", "cleanup"):
            with self.subTest(kind=kind):
                self.approval["authorization_id"] = (
                    "b1111111-1111-4111-8111-111111111111" if kind == "answer" else
                    "c1111111-1111-4111-8111-111111111111")
                self.calls.clear()
                def failure(session, case):
                    result = self.runner(session, case)
                    if kind == "answer":
                        result["checks"]["answer"] = "failed"
                    else:
                        result["cleanup_confirmed"] = False
                    return result
                self.assertEqual(self.run_campaign(failure)["status"], "stopped")
                self.assertEqual(self.run_campaign()["status"], "stopped")
                self.assertEqual(len(self.calls), 1)

    def test_interruption_never_replays_uncertain_attempt(self):
        def interrupted(session, case):
            self.calls.append((session, case))
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_campaign(interrupted)
        self.assertEqual(self.run_campaign()["status"], "attempting")
        self.assertEqual(len(self.calls), 1)

    def test_completed_case_resumes_without_repeating_it(self):
        actual = campaign.write
        def crash_after_result(path, state):
            actual(path, state)
            if state["status"] == "ready" and len(state["results"]) == 1:
                raise KeyboardInterrupt
        with mock.patch.object(campaign, "write", side_effect=crash_after_result):
            with self.assertRaises(KeyboardInterrupt):
                self.run_campaign()
        self.assertEqual(self.run_campaign()["status"], "complete")
        self.assertEqual(len(self.calls), 3)

    def test_changed_scope_expired_and_unapproved_make_no_calls(self):
        for field, value in (("approved", False), ("package_sha256", "0" * 64),
                             ("expires_at", self.now.isoformat())):
            approval = copy.deepcopy(self.approval)
            approval[field] = value
            with self.assertRaises(ValueError):
                campaign.execute(self.proposal, approval, self.runner,
                                 wall=lambda: self.now.isoformat())
        changed = copy.deepcopy(self.proposal)
        changed["limits"]["retries"] = 1
        self.approval["package_sha256"] = campaign.digest(changed)
        with self.assertRaises(ValueError):
            campaign.execute(changed, self.approval, self.runner, wall=lambda: self.now.isoformat())
        self.assertFalse(self.calls)

    def test_insufficient_time_reserves_whole_case_and_shutdown(self):
        self.now += timedelta(seconds=1381)
        self.assertEqual(self.run_campaign()["status"], "budget_exhausted")
        self.assertFalse(self.calls)

    def test_unknown_or_corrupt_resume_state_never_dispatches(self):
        path = Path(self.tmp.name) / "invoice-campaigns" / (self.approval["authorization_id"] + ".json")
        binding = campaign.digest({"package": self.proposal, "approval": self.approval})
        for state in ({"status": "unknown", "results": []},
                      {"status": "ready", "results": [{"case": "no-overdue"}]}):
            campaign.write(path, dict(state, binding=binding))
            with self.assertRaises(ValueError):
                self.run_campaign()
        self.assertFalse(self.calls)


class ExecutorIntegrationTests(unittest.TestCase):
    def test_three_cases_reuse_real_executor_reviewer_and_cleanup_without_handoff(self):
        fixture = finance_tests.FinanceRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        proposal = campaign.package(fixture.f.session)
        now = employee_tool._utc(fixture.f.driver()[0].wall())
        approval = {"approved": True, "authorization_id": "f1111111-1111-4111-8111-111111111111",
                    "package_sha256": campaign.digest(proposal), "issued_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=1800)).isoformat()}
        calls = []
        def execute(session, case):
            fixture.f.session = session
            fixture.f.case = case
            key = case["employee_tool"]["catalog_case"]
            fixture.f.first["request"]["message"] = catalog.message(key)
            run = fixture.f.first["response"]["data"]
            run["invocation_payload"]["message"] = catalog.message(key)
            run["output_events"] = finance_tests.events(json.dumps(catalog.expected(key)))
            driver, cli = fixture.driver()
            campaign.employee_run.validate(session, case)
            self.assertEqual(fixture.f.execute(driver), 0)
            calls.extend(cli.calls)
            return {"checks": fixture.verdicts(driver), "service_invocations": sum(driver.counts.values()),
                    "cleanup_confirmed": not fixture.f.store.open_cleanup()}
        result = campaign.execute(proposal, approval, runner=execute, wall=lambda: now.isoformat())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(sum("--async" in a for a in calls), 3)
        self.assertEqual(sum("managed-stop" in a for a in calls), 3)
        self.assertFalse(fixture.f.store.open_cleanup())

    def test_catalog_message_answer_and_shutdown_through_existing_executor(self):
        fixture = finance_tests.FinanceRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        key = "calendar-boundaries"
        fixture.f.case = catalog.case(key)
        fixture.f.case["employee_tool"]["deployment_id"] = finance_invoices.TARGET
        fixture.f.first["request"]["message"] = catalog.message(key)
        fixture.f.first["response"]["data"]["invocation_payload"]["message"] = catalog.message(key)
        fixture.f.first["response"]["data"]["output_events"] = finance_tests.events(
            json.dumps(catalog.expected(key)))
        campaign.employee_run.validate(fixture.f.session, fixture.f.case)
        driver, cli = fixture.driver()
        self.assertEqual(fixture.f.execute(driver), 0)
        self.assertTrue(all(v == "passed" for v in fixture.verdicts(driver).values()))
        self.assertEqual(sum("managed-stop" in a for a in cli.calls), 1)
        self.assertFalse(fixture.f.store.open_cleanup())
        report = Path(fixture.f.root) / driver.run_id / "report/report.json"
        self.assertTrue(report.exists())
