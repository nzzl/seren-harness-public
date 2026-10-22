"""Prospect-brief suite: independent oracle, checker variations and failures, and the full
simulated campaign through the actual executor, reviewer and cleanup (no live calls)."""
from datetime import date, timedelta
import json
from pathlib import Path
import re
import unittest
from unittest import mock

from seren_support import SOURCE  # noqa: F401  (imports this checkout)
from seren_harness import bd_brief, employee_tool, invoice_campaign as campaign
from seren_harness import invoice_catalog as catalog
from seren_harness.cli import main
import test_finance_employee as finance_tests

COUNT_WORDS = {"No": 0, "One": 1, "Two": 2, "Three": 3}


def records_from_message(key):
    """Parse the dossier from the delivered message text, not from bd_brief.CASES."""
    body = catalog.message(key).split("Records:\n", 1)[1].split("\n\nRules:", 1)[0]
    rows = []
    for line in body.splitlines()[1:]:
        ident, day, kind, status, content = [p.strip() for p in line.split(" | ", 4)]
        rows.append({"id": ident, "date": None if day == "-" else date.fromisoformat(day),
                     "kind": kind, "status": status, "content": content})
    return rows


def independent_answer(key):
    """Apply the stated rules to the parsed records (integer days, regex extraction)."""
    rows = records_from_message(key)
    available = [r for r in rows if r["status"] == "available"]
    found = {}  # field -> list of (date, value, id)
    for r in available:
        text = r["content"]
        for field, pattern, cast in (
                ("headcount", r"Employees: (\d+)", int),
                ("analytics_warehouse", r"Analytics warehouse: (\w+)", str.lower),
                ("data_team_size", r"data team has (\d+)", int)):
            m = re.search(pattern, text)
            if m:
                found.setdefault(field, []).append((r["date"], cast(m.group(1)), r["id"]))
        m = re.search(r"(Series \w) round of ([\d,]+) USD", text)
        if m:
            found.setdefault("latest_funding", []).append((r["date"], {
                "round": m.group(1).lower(), "amount_usd": int(m.group(2).replace(",", "")),
                "date": r["date"].isoformat()}, r["id"]))
        m = re.search(r"(\w+) open (?:data )?roles?", text)
        if r["kind"] == "job_postings" and m:
            found.setdefault("open_data_roles", []).append((r["date"], COUNT_WORDS[m.group(1)],
                                                            r["id"]))
        if r["kind"] == "crm_account" and "No active contract" in text:
            found.setdefault("existing_customer", []).append((r["date"], False, r["id"]))
    facts, conflicts, unknowns = {}, [], []
    for field in bd_brief.FIELDS:
        values = sorted(found.get(field, []), key=lambda x: x[0], reverse=True)
        if not values:
            facts[field] = None
            kind = {"data_team_size": "org", "existing_customer": "crm"}.get(field)
            failed = [r for r in rows if r["status"] != "available" and kind
                      and r["kind"].startswith(kind)]
            reason = ("source_unavailable" if failed and failed[0]["status"] == "unavailable"
                      else "record_not_found" if failed else "no_record")
            unknowns.append((field, reason, frozenset(r["id"] for r in failed)))
            continue
        facts[field] = values[0][1]
        if len({json.dumps(v[1]) for v in values}) > 1:
            conflicts.append((field, frozenset(v[2] for v in values), values[0][2]))
    as_of = date(2026, 10, 14)
    triggers = [r for r in available if r["kind"] == "press_release"
                or (r["kind"] == "job_postings" and COUNT_WORDS.get(r["content"].split()[0], 0))]
    recent = any(0 <= (as_of - r["date"]).days <= 183 for r in triggers)
    criteria = {
        "headcount_50_500": None if facts["headcount"] is None else 50 <= facts["headcount"] <= 500,
        "analytics_warehouse_fit": None if facts["analytics_warehouse"] is None
        else facts["analytics_warehouse"] in ("snowflake", "bigquery"),
        "data_team_3_plus": None if facts["data_team_size"] is None else facts["data_team_size"] >= 3,
        "recent_trigger": True if recent else (
            None if facts["latest_funding"] is None or facts["open_data_roles"] is None else False),
        "not_existing_customer": None if facts["existing_customer"] is None
        else not facts["existing_customer"]}
    values = criteria.values()
    rec = ("disqualify" if any(v is False for v in values)
           else "pursue" if all(v is True for v in values) else "qualify_further")
    return facts, conflicts, unknowns, criteria, rec, {r["id"] for r in triggers}


def answer_for(key, **change):
    """A well-formed answer built from the independent oracle."""
    facts, conflicts, unknowns, criteria, rec, triggers = independent_answer(key)
    support = {f: sorted(s) for f, (_, s) in bd_brief.SPECS[key]["facts"].items()}
    answer = {
        "prospect": bd_brief.SPECS[key]["prospect"],
        "facts": {f: {"value": v, "sources": support[f] if v is not None else
                      sorted(next((s for uf, _, s in unknowns if uf == f), []))}
                  for f, v in facts.items()},
        "conflicts": [{"field": f, "sources": sorted(s), "used": u} for f, s, u in conflicts],
        "unknowns": [{"field": f, "reason": r, "sources": sorted(s)} for f, r, s in unknowns],
        "criteria": criteria, "recommendation": rec,
        "next_step": "Email the Head of Data this week referencing the recent trigger "
                     "and offer a 30-minute pipeline-monitoring review.",
        "next_step_sources": sorted(triggers)[:1],
        "summary": "Qualified by the stated rules; see cited records."}
    answer.update(change)
    return answer


class Oracle(unittest.TestCase):
    def test_frozen_expectations_match_the_independent_oracle(self):
        for key in bd_brief.SPECS:
            facts, conflicts, unknowns, criteria, rec, triggers = independent_answer(key)
            want = bd_brief.expected(key)
            self.assertEqual({f: v["value"] for f, v in want["facts"].items()}, facts, key)
            self.assertEqual({(c["field"], frozenset(c["sources"]), c["used"])
                              for c in want["conflicts"]}, set(conflicts), key)
            self.assertEqual({(u["field"], u["reason"], frozenset(u["sources"]))
                              for u in want["unknowns"]}, set(unknowns), key)
            self.assertEqual((want["criteria"], want["recommendation"]), (criteria, rec), key)
            self.assertEqual(set(want["trigger_sources"]), triggers, key)

    def test_the_three_variants_exercise_distinct_outcomes(self):
        self.assertEqual(["pursue", "qualify_further", "qualify_further"],
                         [bd_brief.recommendation(k) for k in bd_brief.CASES])
        reasons = {u[1] for k in bd_brief.CASES for u in bd_brief.CASES[k]["unknowns"]}
        self.assertEqual({"no_record", "source_unavailable", "record_not_found"}, reasons)


class Checker(unittest.TestCase):
    def outcome(self, key, answer, raw=None):
        return bd_brief.check(key, raw if raw is not None else json.dumps(answer))

    def test_oracle_answers_and_harmless_variations_pass(self):
        for key in bd_brief.SPECS:
            answer = answer_for(key)
            self.assertEqual("passed", self.outcome(key, answer)["outcome"], key)
            fenced = "```json\n" + json.dumps(answer, indent=2) + "\n```"
            self.assertEqual("passed", self.outcome(key, None, fenced)["outcome"])
        varied = answer_for("bd-attribution")
        varied["facts"]["analytics_warehouse"] = {"value": "SNOWFLAKE", "sources": ["S2", "S4"]}
        varied["facts"]["latest_funding"]["value"] = {"round": "Series  B", "amount_usd": 18000000,
                                                      "date": "2026-08-12"}
        varied["prospect"] = "halvorsen freight systems"
        varied["next_step_sources"] = ["S3", "S2"]
        self.assertEqual("passed", self.outcome("bd-attribution", varied)["outcome"])

    def test_documented_failure_examples(self):
        cases = {
            # Guessing a missing fact.
            ("bd-conflicting-missing", "wrong value: data_team_size"):
                lambda a: a["facts"].update(data_team_size={"value": 5, "sources": ["S1"]}),
            # Using the older conflicting record.
            ("bd-conflicting-missing", "wrong value: headcount"):
                lambda a: a["facts"].update(headcount={"value": 620, "sources": ["S3"]}),
            # Treating an unavailable CRM export as "not a customer".
            ("bd-unavailable-source", "wrong value: existing_customer"):
                lambda a: a["facts"].update(existing_customer={"value": False, "sources": ["S5"]}),
            # Pursuing although a criterion is unknown.
            ("bd-conflicting-missing", "wrong recommendation"):
                lambda a: a.update(recommendation="pursue"),
            # Citing a record that does not exist.
            ("bd-attribution", "cited a record that is not in the dossier"):
                lambda a: a["facts"]["headcount"].update(sources=["S1", "S9"]),
            # Missing record reported as a retrieval failure.
            ("bd-unavailable-source", "wrong unknowns (field, reason or sources)"):
                lambda a: a.update(unknowns=[
                    {"field": "data_team_size", "reason": "source_unavailable", "sources": ["S4"]},
                    {"field": "existing_customer", "reason": "source_unavailable", "sources": ["S5"]}]),
            # Next step not tied to a trigger.
            ("bd-attribution", "next_step not tied to an available trigger record"):
                lambda a: a.update(next_step_sources=["S1"]),
            # Missed conflict.
            ("bd-conflicting-missing", "wrong conflicts"): lambda a: a.update(conflicts=[]),
        }
        for (key, problem), mutate in cases.items():
            answer = answer_for(key)
            mutate(answer)
            result = self.outcome(key, answer)
            self.assertEqual("failed", result["outcome"], problem)
            self.assertIn(problem, result["problems"])

    def test_format_violations_are_malformed_not_failed(self):
        answer = answer_for("bd-attribution")
        prose = "Here is the brief:\n" + json.dumps(answer)
        self.assertEqual("malformed", self.outcome("bd-attribution", None, prose)["outcome"])
        self.assertEqual("malformed", self.outcome("bd-attribution", dict(answer, extra=1))["outcome"])
        answer["facts"]["headcount"]["value"] = "240"
        self.assertEqual("malformed", self.outcome("bd-attribution", answer)["outcome"])
        self.assertEqual("no_answer", bd_brief.check("bd-attribution", "  ")["outcome"])


class Integration(unittest.TestCase):
    def fixture(self):
        f = finance_tests.FinanceRunTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_prepare_is_offline_and_pins_only_the_brief_suite(self):
        f = self.fixture()
        path, output = f.f.root / "session.json", f.f.root / "proposal.json"
        path.write_text(json.dumps(f.f.session))
        with mock.patch.object(campaign.employee_run, "pinned_cli",
                               side_effect=AssertionError("live entry")):
            self.assertEqual(main(["invoice-campaign", "prepare", "--suite", "bd-brief-v1",
                                   "--session", str(path), "--output", str(output)]), 0)
        proposal = json.loads(output.read_text())
        self.assertEqual(proposal["cases"], list(bd_brief.CASES))
        self.assertEqual(proposal["catalog"], catalog.manifest("bd-brief-v1"))
        self.assertEqual(proposal["limits"]["employee_runs"], 3)

    def test_cases_validate_on_the_existing_supplied_answer_route(self):
        for key in bd_brief.SPECS:
            case = catalog.case(key)
            employee_tool.validate(case)
            self.assertEqual(case["employee_tool"]["message"], bd_brief.message(key))
            self.assertIn("prospect brief", case["findings"]["employee_answer_result"]["title"])

    def test_reliability_suite_replays_the_v1_task_unchanged_and_leaves_v1_intact(self):
        f = self.fixture()
        path, output = f.f.root / "session.json", f.f.root / "proposal.json"
        path.write_text(json.dumps(f.f.session))
        with mock.patch.object(campaign.employee_run, "pinned_cli",
                               side_effect=AssertionError("live entry")):
            self.assertEqual(main(["invoice-campaign", "prepare", "--suite",
                                   "bd-reliability-v1", "--session", str(path), "--output",
                                   str(output)]), 0)
        proposal = json.loads(output.read_text())
        self.assertEqual(["bd-unavailable-source", "bd-unavailable-source-repeat",
                          "bd-complete-control"], proposal["cases"])
        manifest = catalog.manifest("bd-reliability-v1")
        v1 = catalog.manifest("bd-brief-v1")["bd-unavailable-source"]
        for key in ("bd-unavailable-source", "bd-unavailable-source-repeat"):
            self.assertEqual((v1["message_sha256"], v1["expected_sha256"]),
                             (manifest[key]["message_sha256"], manifest[key]["expected_sha256"]))
        self.assertEqual(list(bd_brief.CASES), list(catalog.case_ids("bd-brief-v1")))
        control = bd_brief.message("bd-complete-control")
        original = bd_brief.message("bd-unavailable-source")
        self.assertEqual(original.split("Records:")[0], control.split("Records:")[0])
        self.assertEqual(original.split("Rules:")[1], control.split("Rules:")[1])
        self.assertEqual("pursue", bd_brief.recommendation("bd-complete-control"))

    def exercise(self, wrong=False, suite="bd-brief-v1"):
        f = self.fixture()
        proposal = campaign.package(f.f.session, suite)
        now = employee_tool._utc(f.f.driver()[0].wall())
        approval = {"approved": True, "authorization_id": "b1111111-1111-4111-8111-111111111111",
                    "package_sha256": campaign.digest(proposal), "issued_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=1800)).isoformat()}
        calls, reports = [], []

        def run(session, case):
            f.f.session, f.f.case = session, case
            key = case["employee_tool"]["catalog_case"]
            f.f.first["request"]["message"] = catalog.message(key)
            data = f.f.first["response"]["data"]
            data["invocation_payload"]["message"] = catalog.message(key)
            answer = answer_for(key)
            if wrong:
                answer["recommendation"] = "disqualify"
            data["output_events"] = finance_tests.events(json.dumps(answer))
            driver, cli = f.driver()
            campaign.employee_run.validate(session, case)
            self.assertEqual(f.f.execute(driver), 1 if wrong else 0)
            calls.extend(cli.calls)
            reports.append(f.report(driver))
            saved = json.loads((Path(session["home"]) / driver.run_id /
                                "invocation.json").read_text())
            self.assertEqual(saved["response"]["data"]["output_events"], data["output_events"])
            return {"checks": f.verdicts(driver), "service_invocations": sum(driver.counts.values()),
                    "cleanup_confirmed": not f.f.store.open_cleanup()}
        result = campaign.execute(proposal, approval, runner=run, wall=lambda: now.isoformat())
        self.assertEqual(result["status"], "stopped" if wrong else "complete")
        self.assertEqual(sum("--async" in a for a in calls), 1 if wrong else 3)
        self.assertEqual(sum("managed-stop" in a for a in calls), 1 if wrong else 3)
        self.assertFalse(f.f.store.open_cleanup())
        if wrong:
            self.assertEqual(reports[0]["verdict"], "failed")
            self.assertTrue(reports[0]["findings"])

    def test_three_briefs_execute_score_and_stop_without_handoff(self):
        self.exercise()

    def test_a_wrong_recommendation_is_preserved_and_stops_after_cleanup(self):
        self.exercise(wrong=True)

    def test_reliability_suite_runs_three_cases_and_a_first_failure_stops_the_rest(self):
        self.exercise(suite="bd-reliability-v1")
        self.exercise(wrong=True, suite="bd-reliability-v1")


if __name__ == "__main__":
    unittest.main()
