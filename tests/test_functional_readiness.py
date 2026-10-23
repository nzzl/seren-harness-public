"""`functional readiness`: read-only live checks, through the real entry point with fake boundaries.

The stand-in Seren CLI and Claude CLI record every invocation, so these tests can prove the
command only reads: no lifecycle change, assignment, onboarding, psql write or model prompt.
"""
import json
from pathlib import Path
import tempfile
import unittest

import seren_support  # noqa: F401  (imports the checkout's package first)
from test_functional_journeys import journey_env

READS = {("--version",), ("agent", "managed-get"), ("agent", "cloud", "deployment", "status"),
         ("agent", "managed-deployment-tools"), ("-o", "json", "psql")}


def shape(call):
    for prefix in sorted(READS, key=len, reverse=True):
        if tuple(call[:len(prefix)]) == prefix:
            return prefix
    return tuple(call[:4])


class Readiness(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def env(self, **scenario):
        env = journey_env(self.root, runtime="claude_cli")
        env.scenario.update(scenario)
        env.write()
        return env

    def local(self, env, **values):
        (env.model_home / "local.json").write_text(json.dumps(values))

    def model_prompts(self, env):
        return env.model_calls()   # calls.jsonl records prompts only; local commands are separate

    def check(self, env):
        code, out = env.run(action="readiness")
        report = sorted((env.folder / "readiness").glob("*.json"))
        return code, out, json.loads(report[-1].read_text()) if report else None

    def test_ready_configuration_reads_only_and_reports_what_stays_unverified(self):
        env = self.env()
        code, out, report = self.check(env)
        self.assertEqual(code, 0, out)
        self.assertEqual(report["verdict"], "ready")
        self.assertEqual({shape(c) for c in env.calls()}, READS)
        self.assertEqual(report["seren_requests"], 4)
        psql = [c for c in env.calls() if c[:3] == ["-o", "json", "psql"]]
        self.assertEqual(len(psql), 1)
        self.assertTrue(psql[0][-1].startswith("SELECT "))
        checks = {r["check"]: r for r in report["checks"]}
        self.assertEqual(checks["read-only reads"]["status"], "ok")
        self.assertEqual(checks["journey tool seren_db_query"]["status"], "ok")
        self.assertEqual(self.model_prompts(env), [])                      # no model session
        self.assertFalse((env.folder / "state.json").exists())             # no campaign started
        self.assertIn("Only a live journey can show", (sorted((env.folder / "readiness").glob("*.md"))[-1]).read_text())
        self.assertIn("output_events", out)

    def test_unauthorized_readiness_sends_nothing(self):
        env = self.env()
        env.campaign["authorization"]["live_seren"] = False
        env.write()
        code, out, report = self.check(env)
        self.assertEqual(code, 2, out)
        self.assertIsNone(report)
        self.assertEqual([c for c in env.calls() if c != ["--version"]], [])

    def test_contradicted_assumptions_are_reported_as_mismatches(self):
        cases = {
            "approval-gated tool": ({"tool_approval": "always"}, {}, "journey tool seren_db_query"),
            "write-capable reads": ({"db_readiness": {"read_only": "off"}}, {}, "read-only reads"),
            "no CREATE privilege": ({"db_readiness": {"database_create": False}}, {}, "journey table creation privilege"),
            "missing Claude flags": ({}, {"missing_flags": ["--tools", "--max-budget-usd"]}, "Claude CLI flags"),
        }
        for name, (scenario, local, check) in cases.items():
            with self.subTest(name):
                self.root = self.root / name.replace(" ", "-")
                self.root.mkdir(parents=True)
                env = self.env(**scenario)
                self.local(env, **local)
                code, out, report = self.check(env)
                self.assertEqual(code, 1, out)
                self.assertEqual(report["verdict"], "mismatch")
                self.assertEqual({r["check"]: r["status"] for r in report["checks"]}[check], "mismatch")
                self.root = self.root.parent

    def test_running_employee_and_missing_login(self):
        env = self.env()
        env.scenario["deployments"][0].update(status="running", desired="running")
        env.write()
        self.local(env, logged_in=False)
        code, out, report = self.check(env)
        statuses = {r["check"]: r["status"] for r in report["checks"]}
        self.assertEqual(statuses["employee status"], "mismatch")
        self.assertEqual(statuses["Claude login"], "blocked")
        self.assertEqual(code, 2, out)

    def test_access_error_is_blocked_with_no_retry(self):
        env = self.env()
        env.scenario["deployments"] = []          # the stand-in answers 404 for an unknown employee
        env.write()
        code, out, report = self.check(env)
        self.assertEqual(code, 2, out)
        self.assertEqual(report["checks"][0]["status"], "blocked")
        self.assertEqual(len([c for c in env.calls() if c[:2] == ["agent", "managed-get"]]), 1)

    def test_configured_journey_without_database_blocks(self):
        env = journey_env(self.root, runtime="heuristic")
        env.campaign["journeys"]["database"] = None
        env.write()
        code, out = env.run(action="readiness")
        report = json.loads(sorted((env.folder / "readiness").glob("*.json"))[-1].read_text())
        self.assertEqual(code, 2, out)
        self.assertEqual(report["verdict"], "blocked")
        self.assertEqual({r["check"]: r["status"] for r in report["checks"]}["dedicated database"], "blocked")
        self.assertFalse([c for c in env.calls() if c[:3] == ["-o", "json", "psql"]])

    def test_catalog_for_another_deployment_cannot_establish_the_tool(self):
        for name, value in (("different", "00000000-0000-4000-8000-000000000999"), ("missing", None)):
            with self.subTest(name):
                root = self.root / name
                root.mkdir()
                env = journey_env(root, runtime="heuristic")
                env.scenario["catalog_deployment_id"] = value
                env.write()
                code, out = env.run(action="readiness")
                report = json.loads(sorted((env.folder / "readiness").glob("*.json"))[-1].read_text())
                statuses = {r["check"]: r["status"] for r in report["checks"]}
                self.assertEqual(code, 1, out)
                self.assertEqual(statuses["tool catalog identity"], "mismatch")
                self.assertEqual(statuses["journey tool seren_db_query"], "mismatch")

    def test_heuristic_readiness_stays_read_only_within_four_requests(self):
        env = journey_env(self.root, runtime="heuristic")
        code, out = env.run(action="readiness")
        self.assertEqual(code, 0, out)
        service = [c for c in env.calls() if c != ["--version"]]
        self.assertEqual(len(service), 4)
        self.assertEqual({shape(c) for c in service}, READS - {("--version",)})
        self.assertFalse([c for c in service if c[:3] == ["-o", "json", "psql"]
                          and not c[-1].startswith("SELECT ")])
        self.assertEqual(env.model_calls(), [])
        self.assertIn("does not show which database the employee is attached to", out + json.dumps(
            json.loads(sorted((env.folder / "readiness").glob("*.json"))[-1].read_text())))

    def test_all_false_authorization_sends_no_service_request(self):
        env = journey_env(self.root, runtime="heuristic")
        env.campaign["authorization"].update(live_seren=False, resource_writes=False, model_calls=False,
                                             create_employee=False, approved_by=None, expires_at=None)
        env.write()
        for action in ("readiness", "run"):
            self.assertEqual(env.run(action=action)[0], 2)
        self.assertEqual([c for c in env.calls() if c != ["--version"]], [])

    def test_create_mode_skips_employee_reads(self):
        env = journey_env(self.root, runtime="heuristic")
        env.campaign["employee"] = {"mode": "create"}
        env.campaign["authorization"]["create_employee"] = True
        env.campaign["journeys"]["database"]["attachment"] = {"kind": "operator-supplied"}
        env.write()
        code, out = env.run(action="readiness")
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in env.calls() if c[:1] == ["agent"]])
        self.assertIn("SKIPPED   employee", out)


def catalog_entry(name, **fields):
    return dict({"name": name, "effective_policy": {"status": "allowed"}, "approval_type": "none",
                 "checkpoint_required": False, "side_effecting": False}, **fields)


class JourneyToolCatalogEntries(unittest.TestCase):
    """Readiness reports the first catalog entry for the journey tool; the campaign uses its
    name-to-contract map, where a later duplicate replaces an earlier one. Both selections are
    kept as they are; only the per-entry policy is shared. The duplicate cases record that
    existing difference, they do not endorse it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_entry_policy_and_duplicates(self):
        tool = "seren_db_query"
        cases = (
            ("callable", [catalog_entry(tool)], "ok", True),
            ("gated first, callable duplicate later", [catalog_entry(tool, approval_type="always"),
                                                       catalog_entry(tool)], "mismatch", True),
            ("callable first, gated duplicate later", [catalog_entry(tool), catalog_entry(tool, approval_type="always")],
             "ok", False),
            ("checkpoint required", [catalog_entry(tool, checkpoint_required=True)], "mismatch", False),
            ("not allowed", [catalog_entry(tool, effective_policy={"status": "denied"})], "mismatch", False),
            ("optional fields absent", [{"name": tool, "effective_policy": {"status": "allowed"}}], "ok", True))
        for name, entries, status, supported in cases:
            with self.subTest(name):
                root = self.root / name.replace(" ", "-").replace(",", "")
                root.mkdir()
                env = journey_env(root, templates=["data_report_revision"])
                env.scenario["catalog_tools"] = entries
                env.write()
                code, out = env.run(action="readiness")
                report = json.loads(sorted((env.folder / "readiness").glob("*.json"))[-1].read_text())
                row = next(r for r in report["checks"] if r["check"] == "journey tool " + tool)
                self.assertEqual((row["status"], code), (status, 0 if status == "ok" else 1), out)
                run_code, run_out = env.run("--max-tasks", "0")             # discovery only
                support = env.state()["journey_support"]["data_report_revision"]
                self.assertEqual(support["supported"], supported, support)
                self.assertEqual(run_code, 4, run_out)
                self.assertEqual([c for c in env.calls() if c[:4] == ["agent", "cloud", "run", "start"]
                                  or c[:2] == ["agent", "managed-start"]], [])


if __name__ == "__main__":
    unittest.main()
