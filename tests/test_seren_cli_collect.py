"""Service-answer collection through the pinned Seren CLI, evaluated by the shared engine.

The CLI here is a local stand-in script (pinned by its own checksum in a temporary target);
it verifies the harness path and its refusals, not the Seren service.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import cli, collect, guide
from seren_harness.sanitize import Sanitizer
from seren_harness.state import Store
from seren_harness.workflow_review import evaluate

CASE_ID = "seren.managed-tools.catalog-retrieval"
GRANTS_ID = "seren.managed-tools.declared-grants"
DEPLOYMENT = "7a00002f-0000-4000-8000-000000000030"
BASELINE_DEPLOYMENT = "7a000009-0000-4000-8000-00000000000a"
BASELINE = ("seren-db:seren_db_query", "exa:post_search", "exa:post_contents")
CANARY = "sk-" + "Q" * 40
FAKE_CLI = r'''#!{python}
import json, os, sys, time
from pathlib import Path
home = Path(os.environ["XDG_CONFIG_HOME"])
if sys.argv[1:] == ["--version"]:
    print("seren " + (home / "version").read_text()); sys.exit(0)
calls = home / "calls"
n = int(calls.read_text()) + 1 if calls.exists() else 1
calls.write_text(str(n))
(home / "log.json").write_text(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))
mode = (home / "mode").read_text()
if mode == "slow" or (mode == "slow-once" and n == 1):
    time.sleep(5)
if mode.startswith("error:"):
    sys.stderr.write(mode[6:]); sys.exit(1)
print((home / "answer.json").read_text())
'''


def tool(name, **override):
    entry = {"name": name, "description": "d", "source": "publisher", "approval_type": "none",
             "approval_rule_count": 0, "effective_policy": {"status": "allowed"},
             "side_effecting": False, "checkpoint_required": False}
    entry.update(override)
    return entry


def catalog(operations=BASELINE, tools=None, deployment=DEPLOYMENT):
    return {"data": {"deployment_id": deployment, "tool_presets": [],
                     "allowed_publisher_operations": list(operations),
                     "approval_policy": "allow_mutations",
                     "tools": tools if tools is not None else [tool("exa_post_search")]}}


class Fixture:
    def __init__(self, root, answer=None, mode="ok", version="0.10.0", args=None,
                 checksum=None, case_id=CASE_ID, cases_root=SOURCE, timeout_seconds=None):
        # Startup allowance is separate from the timeout under test: immediate stand-ins
        # get room to start on a loaded host; slow modes (5 s sleep) keep a short limit so
        # timeout and retry behaviour are still exercised.
        if timeout_seconds is None:
            timeout_seconds = 2 if mode in ("slow", "slow-once") else 10
        self.root = Path(root)
        self.case_id, self.cases_root = case_id, cases_root
        self.config = self.root / "config"
        self.config.mkdir(parents=True)
        self.home = self.root / "reviews"
        cli = self.root / "seren"
        cli.write_text(standin(FAKE_CLI.replace("{python}", sys.executable)))
        cli.chmod(0o755)
        prerun(cli)  # first-run assessment outside the collector's timed calls
        (self.config / "version").write_text(version)
        (self.config / "mode").write_text(mode)
        (self.config / "answer.json").write_text(json.dumps(answer or catalog()))
        self.target = self.root / "target.json"
        self.target.write_text(json.dumps({"employee": "Fixture", "root": str(self.root),
            "collectors": {case_id: {
                "type": "seren_cli", "executable": "{root}/seren",
                "executable_sha256": checksum or hashlib.sha256(cli.read_bytes()).hexdigest(),
                "config_home": "{root}/config", "profile": "test-profile",
                "args": args or ["agent", "managed-deployment-tools", DEPLOYMENT],
                "timeout_seconds": timeout_seconds}}}))

    def run(self, **options):
        args = argparse.Namespace(case=self.case_id, target=str(self.target), target_root=None,
                                  from_copy=None, retest=None, home=str(self.home),
                                  state_dir=None, root=str(self.cases_root), **options)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY,
                                          "SEREN_API_BASE": "https://example.invalid"}):
            code = collect.run(args, out)
        return code, out.getvalue()

    def run_cli(self, *extra):
        """Through the real argument parser, as an operator would type it. Exit code only:
        collect.run binds sys.stdout as its default, so its output is not captured here."""
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}), \
                contextlib.redirect_stdout(io.StringIO()):
            return cli.main(["collect", self.case_id, "--target", str(self.target),
                             "--home", str(self.home), "--root", str(self.cases_root), *extra])

    def calls(self):
        path = self.config / "calls"
        return int(path.read_text()) if path.exists() else 0

    def log(self):
        return json.loads((self.config / "log.json").read_text())

    def report(self):
        [path] = (self.home / self.case_id).glob("*/report/report.json")
        return json.loads(path.read_text())

    def findings(self):
        store = Store(self.home / "findings")
        try:
            return {f["context"]["finding"]: f for f in store.findings()}
        finally:
            store.close()

    def saved_text(self):
        return "".join(p.read_text() for p in self.home.rglob("*") if p.is_file()
                       and p.suffix in (".json", ".md", ".txt"))


class SerenCliCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def fixture(self, **options):
        self.n += 1
        return Fixture(Path(self.tmp.name) / str(self.n), **options)

    def test_matching_catalog_passes_as_harness_observed_evidence(self):
        f = self.fixture()
        code, out = f.run()
        self.assertEqual(code, collect.OK, out)
        report = f.report()
        self.assertEqual((report["verdict"], report["provenance"], report["mode"]),
                         ("passed", "independently_observed", "live"))
        self.assertIn("pinned Seren CLI 0.10.0", report["environment"])
        self.assertTrue(report["observed"].startswith("harness clock "))
        call = f.log()
        self.assertEqual(call["argv"], ["agent", "managed-deployment-tools", DEPLOYMENT,
                                        "-o", "json"])
        self.assertEqual(call["env"]["SEREN_PROFILE"], "test-profile")
        self.assertNotIn("SEREN_API_KEY", call["env"])
        self.assertNotIn("SEREN_API_BASE", call["env"])
        self.assertNotIn(CANARY, f.saved_text())
        self.assertEqual(f.findings(), {})

    def synthetic_resolved_grants(self):
        """SYNTHETIC test copy of the grant case marked resolved, in a temporary cases folder,
        so its criteria can be exercised; the shipped case stays unresolved."""
        root = Path(self.tmp.name) / "synthetic-cases"
        (root / "cases").mkdir(parents=True, exist_ok=True)
        case = json.loads((SOURCE / "cases/seren-managed-tools-declared-grants.json")
                          .read_text())
        case["target_status"] = "resolved"
        case["target_note"] = "SYNTHETIC test copy; not a real target resolution."
        (root / "cases/grants.json").write_text(json.dumps(case))
        return root

    def grants_fixture(self, operations, **options):
        return self.fixture(case_id=GRANTS_ID, cases_root=self.synthetic_resolved_grants(),
                            answer=catalog(operations, deployment=BASELINE_DEPLOYMENT),
                            args=["agent", "managed-deployment-tools", BASELINE_DEPLOYMENT],
                            **options)

    def test_shipped_grant_case_never_reaches_the_service(self):
        # The shipped (unresolved) case is refused before any request, whatever the target.
        for deployment in (BASELINE_DEPLOYMENT, DEPLOYMENT):
            f = self.fixture(case_id=GRANTS_ID,
                             args=["agent", "managed-deployment-tools", deployment])
            code, out = f.run()
            self.assertEqual((code, f.calls()), (collect.COLLECTION_FAILED, 0), out)
            self.assertIn("unresolved", out)
        # A collector aimed at a different deployment than its case is refused too.
        g = self.fixture(case_id=GRANTS_ID, cases_root=self.synthetic_resolved_grants(),
                         args=["agent", "managed-deployment-tools", DEPLOYMENT])
        code, out = g.run()
        self.assertEqual((code, g.calls()), (collect.COLLECTION_FAILED, 0), out)
        self.assertIn("differs from the case's target", out)

    def test_baseline_stays_with_its_documented_deployment(self):
        grants = json.loads((SOURCE / "cases/seren-managed-tools-declared-grants.json")
                            .read_text())
        retrieval = json.loads((SOURCE / "cases/seren-managed-tools-catalog-retrieval.json")
                               .read_text())
        target = json.loads((SOURCE / "targets/seren.json").read_text())["collectors"]
        self.assertEqual(grants["target_status"], "unresolved")
        self.assertEqual(grants["api"]["equals"]["deployment_id"], BASELINE_DEPLOYMENT)
        self.assertEqual([d["id"] for d in grants["api"]["declared"]],
                         ["seren-db.seren_db_query", "exa.post_search", "exa.post_contents"])
        self.assertIn(BASELINE_DEPLOYMENT, json.dumps(grants["basis"]))
        self.assertNotIn(DEPLOYMENT, json.dumps(grants["api"]))
        self.assertNotIn("declared", retrieval["api"])
        self.assertEqual(target[GRANTS_ID]["args"][2], BASELINE_DEPLOYMENT)
        self.assertEqual(target[CASE_ID]["args"][2], DEPLOYMENT)

    def test_a_further_operation_is_not_a_failure_but_prohibited_ones_are(self):
        f = self.grants_fixture(BASELINE + ("other-publisher:post_lookup",))
        code, out = f.run()
        self.assertEqual(code, collect.OK, out)
        criteria = {c["id"]: c for c in f.report()["criteria"]}
        self.assertEqual(criteria["no_prohibited_grants"]["evidence"],
                         {"prohibited": [], "unclassified": ["other-publisher:post_lookup"]})
        self.assertIn("not classifiable", criteria["no_prohibited_grants"]["summary"])
        for operation, prohibition in (("exa:post_monitors", "monitors"),
                                       ("crm:post_outreach_email", "outreach")):
            g = self.grants_fixture(BASELINE + (operation,))
            self.assertEqual(g.run()[0], collect.FAILED_VERDICT)
            criteria = {c["id"]: c for c in g.report()["criteria"]}
            self.assertEqual(criteria["no_prohibited_grants"]["evidence"]["prohibited"],
                             [{"entry": operation, "prohibition": prohibition}])
            self.assertEqual(set(g.findings()), {"prohibited_grant"})
        h = self.grants_fixture(BASELINE[:2])
        self.assertEqual(h.run()[0], collect.FAILED_VERDICT)
        self.assertEqual(set(h.findings()), {"grant_drift"})

    def test_unresolved_case_cannot_pass_on_matching_evidence(self):
        grants = json.loads((SOURCE / "cases/seren-managed-tools-declared-grants.json")
                            .read_text())
        envelope = {"status": "ok", "exit_code": 0, "stderr_tail": "",
                    "harness_clock": {"started": "x", "finished": "y"},
                    "response": catalog(deployment=BASELINE_DEPLOYMENT)}
        items = [{"id": "response-1", "kind": "api_response", "role": "initial",
                  "invocation": "synthetic", "text": json.dumps(envelope)}]
        bundle = {"provenance": "independently_observed", "complete_reading_captured": True}
        report = evaluate(grants, bundle, items)[0]
        self.assertEqual({c["verdict"] for c in report["criteria"]}, {"passed"})
        self.assertEqual(report["verdict"], "incomplete")
        self.assertFalse(report["scoped_success"])
        self.assertIn("not established to match", "; ".join(report["verdict_reasons"]))

    def test_same_answer_later_is_a_retest_not_a_duplicate(self):
        f = self.fixture(mode="error:404 Deployment not found")
        self.assertEqual(f.run()[0], collect.FAILED_VERDICT)
        self.assertEqual(f.run()[0], collect.FAILED_VERDICT)
        finding = f.findings()["catalog_unavailable"]
        self.assertEqual(finding["occurrences"], 1)
        store = Store(f.home / "findings")
        try:
            self.assertEqual([r["outcome"] for r in store.retests(finding["key"])],
                             ["still_failing"])
        finally:
            store.close()

    def test_wrong_target_and_missing_documented_fields_fail(self):
        bad = catalog(deployment="00000000-0000-0000-0000-000000000000",
                      tools=[tool("x"), {k: v for k, v in tool("y").items()
                                         if k != "side_effecting"}])
        f = self.fixture(answer=bad)
        self.assertEqual(f.run()[0], collect.FAILED_VERDICT)
        criteria = {c["id"]: c for c in f.report()["criteria"]}
        self.assertEqual(criteria["target_deployment"]["verdict"], "failed")
        self.assertEqual(criteria["tool_metadata"]["evidence"]["incomplete"],
                         [{"entry": 1, "name": "y", "fields": ["side_effecting"]}])
        self.assertEqual(set(f.findings()), {"catalog_unavailable", "catalog_contract"})

    def test_refused_target_fails_but_access_errors_stay_unverified(self):
        f = self.fixture(mode="error:Failed to list managed deployment tools: 404 "
                              "Deployment not found")
        self.assertEqual(f.run()[0], collect.FAILED_VERDICT)
        self.assertEqual(set(f.findings()), {"catalog_unavailable"})
        g = self.fixture(mode="error:401 Unauthorized " + CANARY)
        code, out = g.run()
        self.assertEqual(code, collect.INCOMPLETE, out)
        criteria = {c["id"]: c["verdict"] for c in g.report()["criteria"]}
        self.assertEqual(set(criteria.values()), {"unverified"})
        self.assertEqual(g.findings(), {})
        self.assertNotIn(CANARY, g.saved_text() + out)

    def test_only_a_timeout_is_retried_once(self):
        f = self.fixture(mode="slow-once")
        code, out = f.run()
        self.assertEqual((code, f.calls()), (collect.OK, 2), out)
        g = self.fixture(mode="slow")
        code, out = g.run()
        self.assertEqual((code, g.calls()), (collect.COLLECTION_FAILED, 2), out)
        self.assertFalse((g.home / "findings").exists())
        h = self.fixture(mode="error:500 upstream")
        h.run()
        self.assertEqual(h.calls(), 1)

    def test_no_retry_makes_at_most_one_service_request(self):
        # calls() counts service commands only; the local `--version` check is excluded.
        ok = self.fixture()
        code, out = ok.run(no_retry=True)
        self.assertEqual((code, ok.calls()), (collect.OK, 1), out)
        slow = self.fixture(mode="slow")
        code, out = slow.run(no_retry=True)
        self.assertEqual((code, slow.calls()), (collect.COLLECTION_FAILED, 1), out)
        self.assertIn("timed out 1 time(s)", out)
        self.assertIn("No review was recorded", out)
        self.assertFalse((slow.home / "findings").exists())
        once = self.fixture(mode="slow-once")
        code, out = once.run(no_retry=True)
        self.assertEqual((code, once.calls()), (collect.COLLECTION_FAILED, 1), out)
        default = self.fixture(mode="slow")
        default.run()
        self.assertEqual(default.calls(), collect.ATTEMPTS)

    def test_no_retry_option_reaches_the_collector_through_the_cli(self):
        slow = self.fixture(mode="slow")
        self.assertEqual((slow.run_cli("--no-retry"), slow.calls()),
                         (collect.COLLECTION_FAILED, 1))
        failing = self.fixture(mode="error:500 upstream")
        self.assertEqual((failing.run_cli("--no-retry"), failing.calls()),
                         (collect.INCOMPLETE, 1))
        malformed = self.fixture()
        (malformed.config / "answer.json").write_text("not json")
        self.assertEqual((malformed.run_cli("--no-retry"), malformed.calls()),
                         (collect.COLLECTION_FAILED, 1))
        again = self.fixture()
        (again.config / "answer.json").write_text("not json")
        code, out = again.run(no_retry=True)
        self.assertEqual((code, again.calls()), (collect.COLLECTION_FAILED, 1), out)
        self.assertIn("not the documented JSON", out)
        unsupported = self.fixture(version="0.11.0")
        self.assertEqual((unsupported.run_cli("--no-retry"), unsupported.calls()),
                         (collect.COLLECTION_FAILED, 0))

    def test_unpinned_unsupported_or_mutating_calls_are_refused_before_any_request(self):
        cases = [self.fixture(checksum="0" * 64), self.fixture(version="0.11.0"),
                 self.fixture(args=["agent", "managed-stop", DEPLOYMENT]),
                 self.fixture(args=["agent", "managed-deployment-tools", DEPLOYMENT,
                                    "--api-key", CANARY])]
        for f in cases:
            code, out = f.run()
            self.assertEqual(code, collect.COLLECTION_FAILED, out)
            self.assertIn("No review was recorded", out)
            self.assertEqual(f.calls(), 0)
            self.assertNotIn(CANARY, out)

    def test_service_cases_are_kept_out_of_the_paste_guide(self):
        listed = io.StringIO()

        class Prompt:
            def say(self, text=""):
                listed.write(text + "\n")

            def ask(self, *_):
                return "1"
        guide.choose_case(Prompt(), SOURCE)
        for case_id in (CASE_ID, GRANTS_ID):
            self.assertNotIn(case_id, listed.getvalue())
            with self.assertRaises(ValueError):
                guide.choose_case(Prompt(), SOURCE, case_id)


class EvidenceFidelityTests(unittest.TestCase):
    """SYNTHETIC tool names and secret-shaped values: legitimate identifiers in documented
    `name` fields survive in saved evidence and reports; secret material does not."""

    NAMES = ["seren_publisher_call", "seren_memory_recall", "seren-db-query", "system_time"]
    SECRETS = ["seren_Q7xK2mP9vL4nR8tB3wZ6", "seren_k7f2m9q4x8z1w5n3",
               "seren_" + "a" * 30, "ghp_" + "B" * 36]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_fixture(self, tools):
        f = Fixture(Path(self.tmp.name) / "f", answer=catalog(tools=tools))
        code, out = f.run()
        return f, code, out

    def test_documented_tool_names_survive_and_secrets_do_not(self):
        tools = [tool(n) for n in self.NAMES]
        tools += [tool(s) for s in self.SECRETS]  # secret-shaped values in a name field
        tools.append(tool("system_clock", description="Calls with token=" + CANARY + " and "
                                                      + self.SECRETS[0]))
        tools.append({k: v for k, v in tool("seren_web_fetch").items()
                      if k != "side_effecting"})
        f, code, out = self.run_fixture(tools)
        self.assertEqual(code, collect.FAILED_VERDICT, out)  # the incomplete entry fails
        saved = f.saved_text()
        evidence = next(f.home.rglob("response-1.txt")).read_text()
        for name in self.NAMES + ["system_clock", "seren_web_fetch"]:
            self.assertIn('"name": "' + name + '"', evidence)
        self.assertIn("seren_web_fetch", json.dumps(
            {c["id"]: c["evidence"] for c in f.report()["criteria"]}["tool_metadata"]))
        for secret in self.SECRETS + [CANARY]:
            self.assertNotIn(secret, saved + out)

    def test_name_exemption_is_limited_to_name_fields(self):
        sanitizer = Sanitizer(["seren_known_value"])
        self.assertEqual(sanitizer.text('{"name": "seren_publisher_call"}'),
                         '{"name": "seren_publisher_call"}')
        # The same string outside a `name` value, or as part of a longer value, stays redacted.
        self.assertEqual(sanitizer.text('{"label": "seren_publisher_call"}'),
                         '{"label": "[redacted]"}')
        self.assertEqual(sanitizer.text("use seren_publisher_call now"), "use [redacted] now")
        self.assertEqual(sanitizer.text('{"name": "x seren_publisher_call"}'),
                         '{"name": "x [redacted]"}')
        self.assertNotIn("seren_known_value", sanitizer.text('{"name": "seren_known_value"}'))
        self.assertNotIn("seren_known_value",
                         json.dumps(sanitizer.value({"name": "seren_known_value"})))
        self.assertEqual(sanitizer.value({"name": "seren_publisher_call"}),
                         {"name": "seren_publisher_call"})

    def test_direct_cli_report_states_its_own_collection_method(self):
        f, code, out = self.run_fixture([tool(n) for n in self.NAMES])
        self.assertEqual(code, collect.OK, out)
        report = f.report()
        limits = " ".join(report["limitations"])
        self.assertIn("Direct harness-executed CLI request", limits)
        self.assertIn("no request ID or server build", limits)
        self.assertNotIn("Copied or locally captured", limits)
        markdown = next(f.home.rglob("report.md")).read_text()
        self.assertIn("Direct harness-executed CLI request", markdown)
        g = Fixture(Path(self.tmp.name) / "headers",
                    mode='error:Failed: Response { status: 404, headers: '
                         '{"x-request-id": "7a000030-0000-4000-8000-000000000031"} }')
        g.run()
        limits = " ".join(g.report()["limitations"])
        self.assertIn("Request ID from the captured response headers: "
                      "7a000030-0000-4000-8000-000000000031", limits)
        self.assertNotIn("no request ID", limits)
        not_established = {u["id"] for u in report["unverified"]}
        self.assertEqual(not_established, {"grant_correctness", "tool_execution",
                                           "deployment_state", "server_build"})

    def test_copied_local_and_simulated_evidence_keep_their_limitations(self):
        case = json.loads((SOURCE / "cases/scott-arbitrum-parked-reading.json").read_text())
        items = [{"id": "response-1", "kind": "discord_response", "role": "initial",
                  "invocation": case["command"], "text": "SYNTHETIC reply"}]
        for provenance in ("operator_supplied", "local", "simulated"):
            report = evaluate(case, {"provenance": provenance}, items)[0]
            limits = " ".join(report["limitations"])
            self.assertIn("Copied or locally captured responses", limits, provenance)
            self.assertNotIn("Direct harness-executed", limits, provenance)


if __name__ == "__main__":
    unittest.main()
