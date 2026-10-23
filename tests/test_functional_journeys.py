"""Complete user journeys through the real `functional` entry point, with fake boundaries.

The stand-in Seren CLI (tests/functional_standin.py) emulates the pinned `seren psql` route over
a local world and a stand-in employee that answers only by calling `seren_db_query` on the table
named in its message and by reading the previous deliverable quoted to it. Scripted employee
behaviours and planner/judge replies test orchestration, verification and safety handling; they
are not evidence about the real Seren service or a real model's choices.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import seren_support  # noqa: F401  (imports the checkout's package first)
from seren_harness import functional, functional_issues, functional_journeys as journeys, functional_tasks
from test_functional import Env, NoTransport

PROJECT = "5a1b2c3d-0000-4000-8000-000000000001"
BRANCH = "5a1b2c3d-0000-4000-8000-000000000002"
BOTH = ["data_report_revision", "document_brief_followup"]
OWNER = "seren-harness:test:0000-unit"


def journey_env(root, templates=BOTH, runtime="heuristic", tools=("system_time", "seren_db_query"), **campaign):
    env = Env(root, runtime=runtime, scope={"families": {"research_with_references": ["single_fact"]},
                                            "required_tools": []}, **campaign)
    psql = root / "bin" / "psql"
    psql.parent.mkdir()
    psql.write_text("#!/bin/sh\nexit 1\n")   # Pinned but never run: the stand-in CLI emulates it.
    psql.chmod(0o755)
    env.campaign["authorization"]["resource_writes"] = True
    env.campaign["journeys"] = {"templates": list(templates), "database": {
        "project_id": PROJECT, "branch_id": BRANCH, "database": "qa", "schema": "harness_journeys",
        "psql_executable": str(psql), "psql_sha256": hashlib.sha256(psql.read_bytes()).hexdigest(),
        "attachment": None}}
    env.scenario["deployments"][0]["tools"] = list(tools)
    env.write()
    return env


def creates(env):
    return [c for c in env.calls() if c[:3] == ["-o", "json", "psql"] and "CREATE TABLE" in c[-1]]


def journey_items(state, journey_id):
    record = next(r for r in state["journeys"] if r["id"] == journey_id)
    return record, [next(t for t in state["tasks"] if t["id"] == s["item"]) for s in record["stages"]]


def go(decision, **fields):
    return dict({"decision": decision}, **fields)


class Journeys(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        NoTransport.created = 0
        patcher = patch.object(functional_issues, "GhTransport", NoTransport)   # where submit_issue looks it up
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_journeys_with_claim_contradiction_correction_continuation_and_reproduced_failure(self):
        env = journey_env(self.root, runtime="claude_cli")
        env.scenario["replies"] = [{"behaviour": b} for b in (
            "wrong_filter", "competent", "competent",        # J1 report, correction, revision
            "competent", "ignore_prior",                    # J2 brief, revision
            "competent", "ignore_prior")]                   # J3 (reproduction of J2)
        env.write()
        cost = 0.02
        env.model(planner=[
            (go("journey", journey="data_report_revision", seed=7, purpose="coverage", follow_up_of=None), cost),
            (go("continue", journey_run="J1", action="revise"), cost),       # not permitted after a failure
            (go("continue", journey_run="J1", action="correct", hypothesis="give the user's feedback"), cost),
            (go("continue", journey_run="J1", action="revise"), cost),
            (go("journey", journey="document_brief_followup", seed=11, purpose="coverage", follow_up_of=None), cost),
            (go("continue", journey_run="J2", action="revise"), cost),
            (go("journey", journey="document_brief_followup", seed=11, purpose="reproduce", follow_up_of="J2"), cost),
            (go("continue", journey_run="J3", action="revise"), cost),
            (go("stop", hypothesis="both journeys covered and the failure reproduced"), cost)],
            judge=[({"verdict": "met", "quote": "Spend could exceed the project budget", "reason": "on task"}, 0.01)] * 2)
        code, out = env.run()
        self.assertEqual(code, 0, out)
        state = env.state()
        self.assertEqual([(r["id"], r["template"], r["status"]) for r in state["journeys"]], [
            ("J1", "data_report_revision", "accepted"), ("J2", "document_brief_followup", "failed"),
            ("J3", "document_brief_followup", "failed")])

        # J1: a plausible "verified" report contradicted by the independent read-back, an
        # adaptive correction after the failure, then a revision that depends on prior work.
        record, items = journey_items(state, "J1")
        self.assertEqual([(i["stage"], (i.get("evaluation") or {}).get("verdict") or i["phase"]) for i in items],
                         [("setup", "done"), ("report", "failed"), ("correction", "passed"),
                          ("revision", "passed"), ("cleanup", "done")])
        report, correction, revision = items[1]["evaluation"], items[2]["evaluation"], items[3]["evaluation"]
        self.assertTrue(report["claim_contradicted"])
        self.assertTrue(report["criteria"]["performed_retrieval"] and report["criteria"]["source_fidelity"])
        self.assertIn("regions_correct", report["failed_criteria"])
        self.assertIn(json.dumps(correction["deliverable"], sort_keys=True), items[3]["task"]["message"])
        self.assertTrue(revision["criteria"]["builds_on_previous"])
        self.assertEqual(revision["deliverable"]["previous_total"], correction["deliverable"]["total"])
        self.assertEqual(len(revision["deliverable"]["changed_regions"]), 2)
        rejected = [d["reason"] for d in state["decisions"] if not d["accepted"]]
        self.assertEqual(len(rejected), 1)
        self.assertIn("'revise' is not permitted for J1", rejected[0])

        # J2/J3: a verified brief, then a failure at the revision stage, reproduced on a fresh run.
        record, items = journey_items(state, "J2")
        self.assertEqual([i["stage"] for i in items], ["setup", "brief", "update_documents", "revision", "cleanup"])
        self.assertEqual(items[1]["evaluation"]["verdict"], "passed")
        self.assertEqual(items[3]["evaluation"]["verdict"], "failed")
        self.assertIn("builds_on_previous", items[3]["evaluation"]["failed_criteria"])
        self.assertTrue(items[3]["evaluation"]["criteria"]["source_fidelity"])     # it read the new record

        report_json = json.loads((env.folder / "report.json").read_text())
        drafted = [f for f in report_json["findings"] if f["draft"] == "drafted"]
        self.assertEqual([(f["category"], f["occurrences"]) for f in drafted], [("product_defect", 2)])
        self.assertIn("revision", drafted[0]["title"])
        not_drafted = [f for f in report_json["findings"] if f["draft"] != "drafted"]
        self.assertEqual([f["occurrences"] for f in not_drafted], [1])            # J1 report: recovered
        draft = (env.folder / "drafts" / (drafted[0]["key"] + ".md")).read_text()
        for section in ("## User goal", "## Setup", "## Stages attempted", "## Where the user became blocked",
                        "## Expected", "## Observed evidence", "## User impact", "## Reproduction",
                        "Stage brief message", "Stage revision message", "seren_db_query"):
            self.assertIn(section, draft)
        self.assertIn("harness setup, not under test", draft)
        self.assertNotIn(PROJECT, draft)

        # Each table was created once and dropped; the employee did every stage's work itself.
        self.assertEqual(len(creates(env)), 3)
        self.assertEqual(env.world()["tables"], {})
        self.assertTrue(all(i["evaluation"]["calls"] for r in state["journeys"]
                            for i in journey_items(state, r["id"])[1] if i["kind"] == "journey_work"))
        markdown = (env.folder / "report.md").read_text()
        self.assertIn("## Journeys", markdown)
        self.assertIn("Usable result (verified against the harness's own read-back)", markdown)
        self.assertIn("claimed success contradicted by read-back", markdown)
        prompts = [c["prompt"] for c in env.model_calls() if c["kind"] == "planner"]
        self.assertIn('"permitted_actions": ["correct", "abandon"]', prompts[1])
        self.assertEqual(NoTransport.created, 0)
        self.assertEqual(env.world()["deployments"][env.scenario["deployments"][0]["deployment_id"]]["status"], "stopped")

    def test_convincing_answer_without_performing_the_retrieval_never_passes(self):
        env = journey_env(self.root, templates=["data_report_revision"])
        spec = journeys.generate("data_report_revision", 300, "J1", "harness_journeys", "nunit", OWNER)
        truth = journeys.truth(spec, spec["rows"], "report")
        correct = json.dumps({"regions": truth["regions"], "total": truth["total"],
                              "query": "SELECT region, sum(amount) FROM %s GROUP BY region" % spec["table"]})
        env.scenario["replies"] = [{"answer": correct}, {"behaviour": "no_tool_claim"}]
        env.write()
        self.assertEqual(env.run()[0], 0)
        state = env.state()
        evaluations = [i["evaluation"] for i in journey_items(state, "J1")[1] if i["kind"] == "journey_work"]
        # Even exactly right figures do not pass without the employee's own query of the source.
        self.assertEqual(evaluations[0]["verdict"], "failed")
        self.assertEqual(evaluations[0]["criteria"]["regions_correct"], True)
        self.assertFalse(evaluations[0]["criteria"]["performed_retrieval"])
        self.assertIn("without performing the required seren_db_query retrieval", evaluations[0]["summary"])
        self.assertEqual(evaluations[1]["verdict"], "failed")
        self.assertTrue(evaluations[1]["claim_contradicted"])
        self.assertEqual(state["journeys"][0]["status"], "failed")

    def test_without_tool_access_or_with_approval_gated_tool_the_journey_is_unsupported_not_passed(self):
        for name, tools, approval in (("no-tool", ("system_time",), "none"),
                                      ("gated", ("system_time", "seren_db_query"), "always")):
            with self.subTest(name):
                root = self.root / name
                root.mkdir()
                env = journey_env(root, templates=["data_report_revision"], tools=tools)
                env.scenario["tool_approval"] = approval
                env.scenario["replies"] = [{"answer": "ANSWER: 1\nSOURCE: R1\nCONFLICT: no"}] * 3
                env.write()
                self.assertEqual(env.run()[0], 0)
                state = env.state()
                self.assertEqual(state["journeys"], [])
                self.assertFalse(state["journey_support"]["data_report_revision"]["supported"])
                self.assertIn("unsupported_journey", [o["kind"] for o in state["observations"]])
                self.assertEqual(creates(env), [])
                self.assertIn("Unsupported journeys (not run, not counted as passes)",
                              (env.folder / "report.md").read_text())

    def test_stand_in_employee_needs_the_tool_so_removing_access_fails_the_stage(self):
        """If the catalog check were bypassed, a tool-less employee still could not pass."""
        env = journey_env(self.root, templates=["data_report_revision"], tools=("system_time",))
        env.write()
        with patch.object(functional.Campaign, "journey_support", lambda self: self.ledger.data[
                "journey_support"].update({"data_report_revision": {"supported": True, "reason": None}})):
            env.scenario["replies"] = [{"behaviour": "competent"}] * 2
            env.write()
            self.assertEqual(env.run()[0], 0)
        evaluation = journey_items(env.state(), "J1")[1][1]["evaluation"]
        self.assertEqual(evaluation["verdict"], "failed")
        self.assertFalse(evaluation["criteria"]["performed_retrieval"])

    def test_irrelevant_plan_with_correct_metadata_fails_the_brief(self):
        env = journey_env(self.root, templates=["document_brief_followup"])
        env.scenario["replies"] = [{"behaviour": "irrelevant_plan"}, {"behaviour": "irrelevant_plan"}]
        env.write()
        self.assertEqual(env.run()[0], 0)
        brief = journey_items(env.state(), "J1")[1][1]["evaluation"]
        self.assertEqual(brief["verdict"], "failed")
        self.assertTrue(brief["criteria"]["owner_correct"] and brief["criteria"]["deadline_correct"])
        self.assertTrue({"work_covered", "steps_grounded", "costs_match_quotes"} <= set(brief["failed_criteria"]))

    def test_pause_and_interruption_resume_without_repeating_setup_or_assignment(self):
        env = journey_env(self.root, templates=["data_report_revision"])
        env.scenario["replies"] = [{"behaviour": "competent"}, {"behaviour": "competent"}]
        env.write()
        code, out = env.run("--max-tasks", "1")
        self.assertEqual(code, 4, out)
        state = env.state()
        self.assertEqual(state["retained"], [state["journeys"][0]["spec"]["table"]])
        self.assertEqual(state["cleanup"], "confirmed")                 # the employee was stopped
        real = functional.evaluate_journey
        calls = []

        def interrupted(*args):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt()
            return real(*args)
        with patch.object(functional, "evaluate_journey", interrupted):
            code, out = env.run()
            self.assertEqual(code, 4, out)
            self.assertEqual(env.state()["tasks"][2]["phase"], "read_back")
            code, out = env.run()
        self.assertEqual(code, 0, out)
        state = env.state()
        self.assertEqual(state["journeys"][0]["status"], "accepted")
        self.assertEqual(len(creates(env)), 1)
        journey_starts = [c for c in env.starts() if "Seren Harness journey" in c[c.index("--message") + 1]]
        self.assertEqual(len(journey_starts), 2)
        self.assertEqual(env.world()["tables"], {})

    def test_uncertain_setup_write_is_settled_by_owned_read_back_never_blindly_resent(self):
        # fail_after: the table was created (with our token) but the reply was lost; an
        # ownership probe in the same run settles it and nothing is resent.
        # fail_before: nothing was created; the next run sends the setup once more.
        for mode, first_code, expected_creates in (("fail_after", 0, 1), ("fail_before", 3, 2)):
            with self.subTest(mode):
                root = self.root / mode
                root.mkdir()
                env = journey_env(root, templates=["data_report_revision"])
                env.scenario["psql"] = {"create": mode}
                env.scenario["replies"] = [{"behaviour": "competent"}] * 2
                env.write()
                code, out = env.run()
                self.assertEqual(code, first_code, out)
                env.scenario["psql"] = {}
                env.write()
                if code:
                    code, out = env.run()
                self.assertEqual(code, 0, out)
                self.assertEqual(len(creates(env)), expected_creates)
                self.assertEqual(env.state()["journeys"][0]["status"], "accepted")
                self.assertIn("resource_reconciled", [e["kind"] for e in env.state()["events"]])

    def test_interruption_during_creation_is_reconciled_only_with_ownership_proof(self):
        env = journey_env(self.root, templates=["data_report_revision"])
        env.scenario["replies"] = [{"behaviour": "competent"}] * 2
        env.write()
        original = functional.SerenAdapter.psql

        def crash_after_create(adapter, statement, *args, **kwargs):
            result = original(adapter, statement, *args, **kwargs)
            if statement.startswith("BEGIN;"):
                raise KeyboardInterrupt()          # the intent stays unresolved in the journal
            return result
        with patch.object(functional.SerenAdapter, "psql", crash_after_create):
            self.assertEqual(env.run()[0], 4)
        self.assertEqual(env.run()[0], 0)
        self.assertEqual(len(creates(env)), 1)
        self.assertEqual(env.state()["journeys"][0]["status"], "accepted")

    def test_failed_independent_read_back_leaves_the_stage_unverified(self):
        env = journey_env(self.root, templates=["data_report_revision"])
        env.scenario["replies"] = [{"behaviour": "competent"}] * 2
        env.write()
        original = functional.SerenAdapter.psql
        reads = []

        def failing_read(adapter, statement, *args, **kwargs):
            if "'rows', (SELECT coalesce" in statement:
                reads.append(1)
                if len(reads) == 2:   # the read-back after the report, not the setup check
                    adapter.budget.request()
                    return functional.sql_probe.CliResult("error", [], exit_code=1, stderr_tail="query or CLI error")
            return original(adapter, statement, *args, **kwargs)
        with patch.object(functional.SerenAdapter, "psql", failing_read):
            self.assertEqual(env.run()[0], 0)
        report = journey_items(env.state(), "J1")[1][1]["evaluation"]
        self.assertEqual(report["verdict"], "uncertain")
        self.assertIn("not verified", report["summary"])


class JourneyIsolationAndProvenance(unittest.TestCase):
    """Second review: aggregate provenance, wrong sources and resource ownership, end to end."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_correct_aggregate_report_and_revision_pass_through_the_entry_point(self):
        env = journey_env(self.root, templates=["data_report_revision"])
        env.scenario["replies"] = [{"behaviour": "aggregate"}, {"behaviour": "aggregate"}]
        env.write()
        self.assertEqual(env.run()[0], 0)
        state = env.state()
        self.assertEqual(state["journeys"][0]["status"], "accepted")
        report = journey_items(state, "J1")[1][1]["evaluation"]
        self.assertIn("GROUP BY region", report["calls"][0]["query"])
        self.assertTrue(report["criteria"]["source_fidelity"])

    def test_wrong_source_cannot_pass(self):
        env = journey_env(self.root, templates=["data_report_revision"])
        archive = "harness_journeys.orders_archive"
        env.scenario["tables"] = {archive: {"owner": "someone-else", "rows": [
            {"order_id": "A-1", "region": "East", "quarter": "Q3", "status": "paid", "amount": 999,
             "test_account": False}]}}
        env.scenario["wrong_source"] = archive
        env.scenario["replies"] = [{"behaviour": "wrong_source"}] * 3
        env.write()
        self.assertEqual(env.run()[0], 0)
        report = journey_items(env.state(), "J1")[1][1]["evaluation"]
        self.assertEqual(report["verdict"], "failed")
        self.assertFalse(report["criteria"]["performed_retrieval"])
        self.assertEqual(report["calls"][0]["other_relations"], [archive])
        self.assertIn(archive, env.world()["tables"])          # never touched

    def campaign(self, name, root_name="shared", **extra):
        root = self.root / name
        root.mkdir()
        env = journey_env(root, templates=["data_report_revision"])
        env.campaign["id"] = name
        env.output = self.root / root_name / "reports"            # same output_dir for both
        env.config_home = self.root / root_name / "config"        # same stand-in world (one database)
        env.config_home.mkdir(parents=True, exist_ok=True)
        env.scenario["replies"] = [{"behaviour": "competent"}] * 30   # one shared stand-in world
        env.scenario.update(extra)
        world = env.config_home / "world.json"
        if world.exists():
            # A second operator file pointing at the same Seren account and database.
            env.scenario["deployments"] = []
        env.write()
        return env

    def folder(self, env, name):
        return json.loads((env.output / "functional" / name / "state.json").read_text())

    def test_campaigns_with_identical_configuration_use_distinct_resources_and_resume_their_own(self):
        a = self.campaign("campaign-a")
        self.assertEqual(a.run("--max-tasks", "1")[0], 4)         # A pauses holding its table
        state_a = self.folder(a, "campaign-a")
        table_a = state_a["journeys"][0]["spec"]["table"]
        snapshot = json.loads((a.config_home / "world.json").read_text())["tables"][table_a]
        b = self.campaign("campaign-b")
        self.assertEqual(b.run()[0], 0)
        state_b = self.folder(b, "campaign-b")
        table_b = state_b["journeys"][0]["spec"]["table"]
        self.assertNotEqual(table_a, table_b)
        self.assertNotEqual(state_a["instance"]["owner"], state_b["instance"]["owner"])
        world = json.loads((a.config_home / "world.json").read_text())["tables"]
        self.assertEqual(world[table_a], snapshot)                  # B neither adopted nor dropped it
        self.assertNotIn(table_b, world)                            # B dropped only its own
        self.assertEqual(a.run()[0], 0)                             # A resumes with its namespace
        resumed = self.folder(a, "campaign-a")
        self.assertEqual(resumed["instance"], state_a["instance"])
        self.assertEqual(resumed["journeys"][0]["spec"]["table"], table_a)
        self.assertEqual(resumed["journeys"][0]["status"], "accepted")
        self.assertNotIn(table_a, json.loads((a.config_home / "world.json").read_text())["tables"])

    def test_a_colliding_name_is_never_adopted_changed_or_dropped(self):
        a = self.campaign("campaign-a")
        self.assertEqual(a.run("--max-tasks", "1")[0], 4)
        instance_a = self.folder(a, "campaign-a")["instance"]
        table_a = self.folder(a, "campaign-a")["journeys"][0]["spec"]["table"]
        snapshot = json.loads((a.config_home / "world.json").read_text())["tables"][table_a]
        b = self.campaign("campaign-b")
        forced = dict(functional.new_instance("campaign-b"), namespace=instance_a["namespace"])
        with patch.object(functional, "new_instance", lambda _cid: forced):
            code, out = b.run()
        self.assertEqual(code, 0, out)
        journey_b = self.folder(b, "campaign-b")["journeys"][0]
        self.assertEqual(journey_b["spec"]["table"], table_a)       # the same name was forced
        self.assertEqual((journey_b["status"], journey_b["cleanup"]), ("blocked", "not_owned"))
        stages_b = journey_items(self.folder(b, "campaign-b"), "J1")[1]
        self.assertEqual([(i["stage"], i["phase"]) for i in stages_b], [("setup", "blocked"), ("report", "blocked")])
        self.assertEqual(stages_b[1]["evaluation"]["verdict"], "not_run")    # nothing was assigned
        self.assertEqual(json.loads((a.config_home / "world.json").read_text())["tables"][table_a], snapshot)
        self.assertEqual(a.run()[0], 0)
        self.assertEqual(self.folder(a, "campaign-a")["journeys"][0]["status"], "accepted")

    def test_unrelated_table_with_identical_rows_is_not_ownership(self):
        probe = self.campaign("campaign-x")
        fixed = functional.new_instance("campaign-x")
        spec = journeys.generate("data_report_revision", 300, "J1", "harness_journeys",
                                 fixed["namespace"], fixed["owner"])
        probe.scenario["tables"] = {spec["table"]: {"owner": "seren-harness:other:1234-5678",
                                                    "rows": spec["rows"]}}
        probe.write()
        with patch.object(functional, "new_instance", lambda _cid: fixed):
            self.assertEqual(probe.run()[0], 0)
        journey = self.folder(probe, "campaign-x")["journeys"][0]
        self.assertEqual((journey["status"], journey["cleanup"]), ("blocked", "not_owned"))
        self.assertEqual(json.loads((probe.config_home / "world.json").read_text())["tables"][spec["table"]]["owner"],
                         "seren-harness:other:1234-5678")
        self.assertEqual(len(creates(probe)), 1)                    # the one refused CREATE TABLE
        self.assertFalse([c for c in probe.starts() if "Seren Harness journey" in c[c.index("--message") + 1]])

    def test_saved_campaign_without_ownership_is_resumed_conservatively(self):
        env = self.campaign("campaign-old")
        self.assertEqual(env.run("--max-tasks", "1")[0], 4)
        path = env.output / "functional" / "campaign-old" / "state.json"
        state = json.loads(path.read_text())
        table = state["journeys"][0]["spec"]["table"]
        del state["instance"], state["journeys"][0]["spec"]["owner"]   # as saved before this fix
        path.write_text(json.dumps(state))
        self.assertEqual(env.run()[0], 0)
        state = json.loads(path.read_text())
        self.assertTrue(state["instance"]["legacy"])
        self.assertIn(table, json.loads((env.config_home / "world.json").read_text())["tables"])
        revision = journey_items(state, "J1")[1][-1]
        self.assertNotEqual((revision.get("evaluation") or {}).get("verdict"), "passed")


class RevenueSmokeConfiguration(unittest.TestCase):
    """The prepared live configuration's shape: journey-only, heuristic, two assignments."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def env(self, tools=("system_time", "seren_db_query")):
        env = journey_env(self.root, templates=["data_report_revision"], tools=tools,
                          limits={"max_tasks": 2, "max_seren_requests": 100, "max_model_calls": 0,
                                  "max_cost_usd": 0, "max_wall_seconds": 900, "max_reproductions": 0,
                                  "task_seconds": 180})
        env.campaign["scope"] = {"families": {}, "required_tools": ["seren_db_query"]}
        env.campaign["corrections"] = []
        env.write()
        return env

    def assignments(self, env):
        return [c for c in env.starts()]

    def test_journey_only_campaign_completes_report_and_dependent_revision_in_two_assignments(self):
        env = self.env()
        env.scenario["replies"] = [{"behaviour": "competent"}] * 4
        env.write()
        code, out = env.run()
        self.assertEqual(code, 0, out)
        state = env.state()
        self.assertEqual(len(self.assignments(env)), 2)
        record, items = journey_items(state, "J1")
        self.assertEqual(record["status"], "accepted")
        self.assertEqual([(i["stage"], (i.get("evaluation") or {}).get("verdict") or i["phase"]) for i in items],
                         [("setup", "done"), ("report", "passed"), ("revision", "passed"), ("cleanup", "done")])
        self.assertTrue(items[2]["evaluation"]["criteria"]["builds_on_previous"])
        self.assertEqual(state["cleanup"], "confirmed")
        self.assertEqual(env.world()["tables"], {})
        self.assertEqual([t for t in state["tasks"] if t.get("kind", "task") == "task"], [])
        self.assertLessEqual(state["counters"]["seren_requests"], 100)

    def test_unsupported_journey_sends_zero_assignments_and_no_text_fallback(self):
        env = self.env(tools=("system_time",))
        code, out = env.run()
        self.assertEqual(code, 2, out)
        state = env.state()
        self.assertEqual(self.assignments(env), [])
        self.assertEqual(state["tasks"], [])
        self.assertIn("no supported work in scope", state["stop"]["reason"])
        self.assertEqual(creates(env), [])

    def test_failed_report_gets_one_correction_and_never_a_third_task(self):
        for name, second, status in (("recovers", "competent", "incomplete"), ("fails again", "wrong_filter", "failed")):
            with self.subTest(name):
                self.root = self.root / name.replace(" ", "-")
                self.root.mkdir(parents=True)
                env = self.env()
                env.scenario["replies"] = [{"behaviour": "wrong_filter"}, {"behaviour": second},
                                           {"behaviour": "competent"}]
                env.write()
                code, out = env.run()
                self.assertEqual(code, 0, out)
                state = env.state()
                self.assertEqual(len(self.assignments(env)), 2)
                record, items = journey_items(state, "J1")
                self.assertEqual([i["stage"] for i in items if i["kind"] == "journey_work"], ["report", "correction"])
                self.assertEqual(record["status"], status)          # never accepted without the revision
                self.assertEqual(env.world()["tables"], {})
                self.root = self.root.parent

    def test_saved_disabled_example_validates_once_filled_and_stays_disabled(self):
        example = json.loads((Path(functional.ROOT) / "docs" / "live-validation" /
                              "Seren-Revenue-Journey-Live-Test-DISABLED.example.json").read_text())
        authorization = example["campaign"]["authorization"]
        self.assertEqual({k: v for k, v in authorization.items()}, {
            "live_seren": False, "create_employee": False, "model_calls": False,
            "resource_writes": False, "approved_by": None, "expires_at": None})
        self.assertEqual(example["campaign"]["github"], {"mode": "draft", "repository": None, "gh_executable": None})
        env = self.env()      # synthetic stand-in bindings only
        filled = json.loads(env.path.read_text())
        example["seren"], example["deployment_id"] = filled["seren"], filled["deployment_id"]
        example["output_dir"] = filled["output_dir"]
        example["campaign"]["journeys"]["database"] = dict(
            filled["campaign"]["journeys"]["database"], schema="harness_revenue_smoke")
        env.path.write_text(json.dumps(example))
        code, out = env.run(action="check")
        self.assertEqual(code, 2, out)
        self.assertIn("MISSING  live Seren authorization", out)
        self.assertIn("MISSING  journey resources: campaign.authorization.resource_writes is false", out)
        self.assertNotIn("MISSING  planner", out)
        self.assertEqual([c for c in env.calls() if c != ["--version"]], [])

    def test_empty_work_scope_is_rejected(self):
        env = self.env()
        env.campaign["journeys"]["templates"] = []
        env.write()
        code, out = env.run(action="check")
        self.assertEqual(code, 5, out)
        self.assertIn("the campaign has no work", out)


class JourneyVerification(unittest.TestCase):
    """Criteria checks that need no campaign: prior-context dependence and immutability."""

    def spec(self, template="data_report_revision"):
        return journeys.generate(template, 5, "J1", "harness_journeys", "c123456", OWNER)

    def events(self, spec, rows):
        content = json.dumps({"rows": [[r[spec["key"]]] for r in rows]})
        return [{"type": "tool_call", "id": "a", "name": "seren_db_query", "status": "completed",
                 "arguments": json.dumps({"query": "SELECT * FROM " + spec["table"]})},
                {"type": "tool_result", "id": "a", "is_error": False, "content": content}]

    def test_revision_is_judged_against_the_actual_previous_deliverable(self):
        spec = self.spec()
        rows = spec["rows"]
        first = journeys.truth(spec, rows, "report")
        revised = journeys.truth(spec, rows, "revision")
        changed = sorted(r for r in journeys.REGIONS if first["regions"][r] != revised["regions"][r])
        reply = json.dumps({"regions": revised["regions"], "total": revised["total"], "query": "q",
                            "previous_total": first["total"], "changed_regions": changed})
        previous = {"regions": first["regions"], "total": first["total"], "query": "q"}
        ok, _, _ = journeys.verify(spec, "revision", "revision", self.events(spec, rows), reply, rows, previous)
        self.assertTrue(ok["builds_on_previous"])
        for prior in (None, dict(previous, total=first["total"] + 1)):
            result, _, _ = journeys.verify(spec, "revision", "revision", self.events(spec, rows), reply, rows, prior)
            self.assertFalse(result["builds_on_previous"], prior)
        message = journeys.message(spec, "revision", "x", previous)
        self.assertIn(json.dumps(previous, sort_keys=True), message)
        self.assertNotIn(str(rows[0]["amount"]) + ",", message.split("PREVIOUS")[0])   # data not inlined

    def test_metadata_read_back_is_not_verification_of_the_work(self):
        spec = self.spec()
        unrelated = [{"type": "tool_call", "id": "a", "name": "seren_db_query", "status": "completed",
                      "arguments": json.dumps({"query": "SELECT count(*) FROM information_schema.tables"})},
                     {"type": "tool_result", "id": "a", "is_error": False, "content": '{"rows": [[12]]}'}]
        result, calls, _ = journeys.verify(spec, "report", "report", unrelated, "{}", spec["rows"], None)
        self.assertFalse(result["performed_retrieval"])
        self.assertEqual(calls[0]["reads_table"], False)

    def aggregate_call(self, sql, rows, columns=("region", "total")):
        return [{"type": "tool_call", "id": "a", "name": "seren_db_query", "status": "completed",
                 "arguments": json.dumps({"query": sql})},
                {"type": "tool_result", "id": "a", "is_error": False,
                 "content": json.dumps({"columns": list(columns), "rows": rows, "row_count": len(rows)})}]

    def test_aggregate_projection_and_source_rules(self):
        spec = journeys.generate("data_report_revision", 7, "J1", "harness_journeys", "review", OWNER)
        truth = journeys.truth(spec, spec["rows"], "report")
        grouped = [[r, v] for r, v in truth["regions"].items() if v]
        good = json.dumps({"regions": truth["regions"], "total": truth["total"], "query": "q"})

        def check(sql, rows, reply=good, columns=("region", "total")):
            result, calls, _ = journeys.verify(spec, "report", "report", self.aggregate_call(sql, rows, columns),
                                               reply, spec["rows"], None)
            return result
        aggregate = ("SELECT region, sum(amount) AS total FROM %s WHERE quarter='Q3' AND status='paid' "
                     "GROUP BY region" % spec["table"])
        self.assertTrue(all(check(aggregate, grouped).values()))                     # review reproduction
        self.assertTrue(check(aggregate.replace(spec["table"], spec["table"].split(".")[1]), grouped)["source_fidelity"])
        other = check(aggregate.replace(spec["table"], "harness_journeys.orders_archive"), grouped)
        self.assertFalse(other["performed_retrieval"])                              # wrong source
        mixed = check(aggregate.replace("WHERE", "JOIN audit.orders_copy c ON true WHERE"), grouped)
        self.assertFalse(mixed["source_fidelity"])                                  # mixed source
        commented = check("SELECT 1 -- FROM %s" % spec["table"], grouped)
        self.assertFalse(commented["performed_retrieval"])                          # name only in a comment
        literal = check("SELECT 'FROM %s'" % spec["table"], grouped)
        self.assertFalse(literal["performed_retrieval"])
        wrong = {r: v + 10 for r, v in truth["regions"].items()}
        bad = check(aggregate, [[r, v] for r, v in wrong.items()],
                    json.dumps({"regions": wrong, "total": sum(wrong.values()), "query": "q"}))
        self.assertFalse(bad["regions_correct"])                                    # incorrect aggregate
        ignored = check(aggregate, grouped, json.dumps({"regions": wrong, "total": sum(wrong.values()), "query": "q"}))
        self.assertFalse(ignored["source_fidelity"])                                # not derived from its result
        columns = ("amount", "order_id", "quarter", "region", "status")
        raw = [[r[c] for c in columns] for r in spec["rows"]]
        projected = check("SELECT order_id, region, quarter, status, amount FROM %s" % spec["table"], raw, columns=columns)
        self.assertTrue(projected["source_fidelity"])                               # legitimate projection
        fabricated = [[r[0] + 1] + r[1:] for r in raw]
        self.assertFalse(check("SELECT order_id, region, quarter, status, amount FROM %s" % spec["table"],
                               fabricated, columns=columns)["source_fidelity"])
        region_only = check("SELECT region FROM %s" % spec["table"], [["East"]], columns=("region",))
        self.assertFalse(region_only["source_fidelity"])                            # too little to support figures

    def test_missing_result_evidence_is_unverified_not_a_product_defect(self):
        spec = journeys.generate("data_report_revision", 7, "J1", "harness_journeys", "review", OWNER)
        truth = journeys.truth(spec, spec["rows"], "report")
        sql = "SELECT region, sum(amount) FROM %s GROUP BY region" % spec["table"]
        events = [{"type": "tool_call", "id": "a", "name": "seren_db_query", "status": "completed",
                   "arguments": json.dumps({"query": sql})},
                  {"type": "tool_result", "id": "a", "is_error": False, "content": '{"columns": ["region", "to'},
                  {"type": "text", "text": json.dumps({"regions": truth["regions"], "total": truth["total"], "query": sql})},
                  {"type": "done", "status": "ok"}]
        record = {"spec": spec}
        item = {"result": {"status": "completed", "output_events": events}, "criterion_stage": "report",
                "stage": "report", "task": {"message": "m", "criteria": spec["criteria"]["report"]},
                "readback": {"ok": True, "rows": spec["rows"]}}
        evaluation = functional.evaluate_journey(record, item, None, None, None)
        self.assertIsNone(evaluation["criteria"]["source_fidelity"])
        self.assertEqual((evaluation["verdict"], evaluation["category"]), ("uncertain", "uncertain"))

    def document_case(self, replace=None):
        spec = journeys.generate("document_brief_followup", 11, "J1", "harness_journeys", "review", OWNER)
        facts = spec["facts"]
        wording = dict(functional_tasks.ACTIONS)
        steps = [{"action": wording[k][0].upper() + wording[k][1:], "doc": facts["notes_doc"],
                  "cost": facts["quotes"][k]} for k in facts["agreed"]]
        previous = {"owner": facts["owner"], "deadline": facts["deadline"], "steps": steps, "risks": ["r"],
                    "total_cost": sum(s["cost"] for s in steps)}
        kept = [dict(s) for s in steps if facts["dropped"] not in s["action"].lower()]
        for step in kept:
            step.update(replace or {})
        revision = {"owner": facts["owner"], "deadline": facts["deadline"], "steps": kept, "risks": ["r"],
                    "total_cost": sum(s["cost"] for s in kept), "decision_doc": facts["decision_doc"],
                    "removed_steps": [s["action"] for s in steps if facts["dropped"] in s["action"].lower()]}
        rows = sorted(spec["rows"] + spec["later_rows"], key=lambda r: r["doc_id"])
        return spec, previous, revision, rows

    def document_events(self, spec, rows):
        columns = ("body", "doc_id", "project", "title")
        return self.aggregate_call("SELECT * FROM " + spec["table"], [[r[c] for c in columns] for r in rows], columns)

    def test_document_revision_keeps_whole_steps_and_checks_their_citations(self):
        spec, previous, revision, rows = self.document_case()
        result, _, _ = journeys.verify(spec, "revision", "revision", self.document_events(spec, rows),
                                       json.dumps(revision), rows, previous)
        self.assertTrue(all(result.values()), result)                               # valid continuation
        other_project = next(r["doc_id"] for r in rows if r["project"] != spec["facts"]["project"])
        for name, change in (("nonexistent", {"doc": "NONEXISTENT-SOURCE"}), ("other project", {"doc": other_project}),
                             ("changed cost", {"cost": 1})):
            spec, previous, changed, rows = self.document_case(change)
            result, _, _ = journeys.verify(spec, "revision", "revision", self.document_events(spec, rows),
                                           json.dumps(changed), rows, previous)
            self.assertFalse(result["builds_on_previous"], name)
            if "doc" in change:
                self.assertFalse(result["steps_grounded"], name)
        spec, previous, revision, rows = self.document_case()
        before = [r for r in rows if r["doc_id"] != spec["facts"]["decision_doc"]]
        result, _, _ = journeys.verify(spec, "revision", "revision", self.document_events(spec, before),
                                       json.dumps(revision), rows, previous)
        self.assertFalse(result["source_fidelity"])                                 # never read the decision

    def test_criteria_cannot_change_after_generation(self):
        spec = self.spec("document_brief_followup")
        spec["criteria"]["brief"] = spec["criteria"]["brief"][:2]
        with self.assertRaises(ValueError):
            journeys.verify(spec, "brief", "brief", [], "{}", spec["rows"], None)

    def test_harness_sql_is_identifier_checked_and_reads_are_read_only(self):
        with self.assertRaises(ValueError):
            journeys.generate("data_report_revision", 1, "J1", "public; drop", "c1", OWNER)
        with self.assertRaises(ValueError):
            journeys.generate("data_report_revision", 1, "J1", "harness_journeys", "c1", "x'; DROP")
        spec = self.spec()
        self.assertTrue(journeys.read_sql(spec).startswith("SELECT"))
        self.assertIn("json_populate_recordset", journeys.setup_sql(spec))


if __name__ == "__main__":
    unittest.main()
