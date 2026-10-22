"""The run-record campaign: fixed queue, durable progress and honest classification.

A local stand-in replaces the Seren CLI (pinned by its own checksum in a temporary target);
it answers `agent cloud run get` per deployment/run and counts service requests separately
from the local `--version` check. These are SYNTHETIC wiring checks, not service evidence.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import campaign, cli, collect
from seren_harness.state import Store

CANARY = "sk-" + "Z" * 40
OWN, OTHER = "7a000021-0000-4000-8000-000000000022", "7a00003a-0000-4000-8000-00000000003b"
RUN, GENERATED = "7a00002c-0000-4000-8000-00000000002d", "7a000003-0000-4000-8000-000000000004"
CASES = {"seren.cloud-run.record-retrieval": "seren-cloud-run-record-retrieval.json",
         "seren.cloud-run.other-deployment": "seren-cloud-run-other-deployment.json",
         "seren.cloud-run.generated-id": "seren-cloud-run-generated-id.json"}
STAND_IN = r'''#!{python}
import json, os, sys, time
from pathlib import Path
home = Path(os.environ["XDG_CONFIG_HOME"])
if sys.argv[1:] == ["--version"]:
    print("seren 0.10.0"); sys.exit(0)
calls = home / "calls"
calls.write_text(str(int(calls.read_text()) + 1 if calls.exists() else 1))
key = sys.argv[6] + " " + sys.argv[7]
answer = json.loads((home / "answers.json").read_text())[key]
time.sleep(answer.get("sleep", 0))
if "stdout" in answer:
    print(answer["stdout"]); sys.exit(0)
sys.stderr.write(answer["stderr"]); sys.exit(1)
'''


def record(**override):
    data = {"id": RUN, "deployment_id": OWN, "organization_id": "7a000032-0000-4000-8000-000000000033",
            "user_id": "7a00000e-0000-4000-8000-00000000000f",
            "execution_id": "7a000025-0000-4000-8000-000000000026",
            "compute_backend": "aws_container", "source": "api", "status": "completed",
            "stop_reason": "completed", "execution_time_ms": 5860, "billed_duration_ms": 0,
            "requested_cpu_millicores": 500, "requested_memory_mib": 256,
            "compute_cost_atomic": 0, "compute_cost_usd": "0", "inference_input_tokens": 3951,
            "inference_output_tokens": 106, "inference_cost_atomic": 0,
            "inference_cost_usd": "0", "started_at": "2026-10-13T04:13:46.88209Z",
            "invocation_payload": {"async": True}, "metadata": {},
            "created_at": "2026-10-13T04:13:46.780128Z",
            "completed_at": "2026-10-13T04:13:52.742876Z",
            "updated_at": "2026-10-13T05:00:00Z"}
    data.update(override)
    return {"stdout": json.dumps({"data": {k: v for k, v in data.items() if v is not None}})}


NOT_FOUND = {"stderr": "Error: Failed: Error Response: status 404 Not Found"}


class Fixture:
    def __init__(self, root, answers):
        self.root = Path(root)
        # Startup allowance is separate from the timeout under test: immediate stand-ins get
        # room to start on a loaded host; a fixture with a deliberately sleeping answer
        # (sleep 3 s) keeps a short limit so the timeout path is still exercised.
        self.timeout = 2 if any(isinstance(v, dict) and v.get("sleep")
                                for v in answers.values()) else 10
        (self.root / "cases").mkdir(parents=True)
        (self.root / "campaigns").mkdir()
        (self.root / "targets").mkdir()
        self.config = self.root / "cli-config"
        self.config.mkdir()
        self.home = self.root / "reviews"
        for name in CASES.values():
            shutil.copy(SOURCE / "cases" / name, self.root / "cases" / name)
        definition = json.loads((SOURCE / "campaigns" / "seren-run-record-retrieval.json")
                                .read_text())
        definition["request_timeout_seconds"] = self.timeout
        self.definition = self.root / "campaigns" / "run-record.json"
        self.definition.write_text(json.dumps(definition))
        executable = self.root / "seren"
        executable.write_text(standin(STAND_IN.replace("{python}", sys.executable)))
        executable.chmod(0o755)
        prerun(executable)  # first-run assessment outside the timed campaign calls
        shipped = json.loads((SOURCE / "targets" / "seren.json").read_text())["collectors"]
        collectors = {k: dict(shipped[k], executable="{root}/seren", config_home="{root}/cli-config",
                              executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                              timeout_seconds=self.timeout) for k in CASES}
        (self.root / "targets" / "seren.json").write_text(json.dumps(
            {"employee": "stand-in", "root": str(self.root), "collectors": collectors}))
        self.answer(answers)

    def answer(self, answers):
        (self.config / "answers.json").write_text(json.dumps(answers))

    def authorize(self, name="auth.json", **override):
        cases = {c["id"]: c["execution_args"] for c in
                 (json.loads((self.root / "cases" / n).read_text()) for n in CASES.values())}
        now = datetime.now(timezone.utc)
        auth = {"campaign": "seren.run-record-retrieval",
                "definition_sha256": hashlib.sha256(self.definition.read_bytes()).hexdigest(),
                "requests": cases, "max_service_requests": 3, "retries": 0,
                "request_timeout_seconds": self.timeout, "profile": "default",
                "approved_by": "test owner (synthetic)", "approved_at": now.isoformat(),
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(minutes=30)).isoformat()}
        auth.update(override)
        path = self.root / name
        path.write_text(json.dumps(auth))
        return path

    def campaign(self):
        return campaign.Campaign(self.definition, self.home, self.root)

    def run(self, auth, execute=collect.run):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}):
            code = self.campaign().run(auth, out, execute)
        return code, out.getvalue()

    def calls(self):
        path = self.config / "calls"
        return int(path.read_text()) if path.exists() else 0

    def state(self):
        return json.loads((self.home / "campaigns" / "seren.run-record-retrieval"
                           / "state.json").read_text())

    def results(self):
        return [e.get("result", e["status"]) for e in self.state()["cases"].values()]

    def findings(self):
        store = Store(self.home / "findings")
        try:
            return {f["context"]["finding"]: f["classification"] for f in store.findings()}
        finally:
            store.close()

    def saved_text(self):
        return "".join(p.read_text() for p in self.home.rglob("*") if p.is_file()
                       and p.suffix in (".json", ".md", ".txt"))


def answers(a=None, b=None, c=None):
    return {OWN + " " + RUN: a or record(), OTHER + " " + RUN: b or NOT_FOUND,
            OWN + " " + GENERATED: c or NOT_FOUND}


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def fixture(self, **kwargs):
        self.n += 1
        return Fixture(Path(self.tmp.name) / str(self.n), answers(**kwargs))

    def test_three_cases_progress_automatically_to_one_summary(self):
        f = self.fixture()
        code, out = f.run(f.authorize())
        self.assertEqual((code, f.calls(), f.results()), (campaign.OK, 3, ["pass"] * 3), out)
        state = f.state()
        self.assertEqual(state["stopped"]["reason"], "complete")
        self.assertEqual(state["budget"], {"max_service_requests": 3, "reserved": 3,
                                           "issued": 3, "issued_unknown": 0})
        directory = f.home / "campaigns" / "seren.run-record-retrieval"
        self.assertIn("Results: pass 3.", (directory / "summary.md").read_text())
        self.assertIn("not authority", (directory / "handoff.md").read_text())
        self.assertEqual(f.findings(), {})

    def test_contract_failures_are_supported_but_history_stays_uncertain(self):
        missing = self.fixture(a=record(metadata=None))
        missing.run(missing.authorize())
        self.assertEqual(missing.results()[0], "supported_failure")
        self.assertEqual(missing.findings(), {"run_record_contract": "defect"})
        other = self.fixture(a=record(deployment_id=OTHER))
        other.run(other.authorize())
        self.assertEqual(other.findings(), {"run_record_contract": "defect"})
        changed = self.fixture(a=record(status="failed",
                                        created_at="2026-10-13T04:13:46.780128+00:00"))
        changed.run(changed.authorize())
        self.assertEqual(changed.findings(), {"run_record_changed": "uncertain"})
        case = changed.state()["cases"]["seren.cloud-run.record-retrieval"]
        self.assertEqual(case["criteria"]["baseline_consistency"], "failed")
        self.assertEqual(case["criteria"]["record_contract"], "passed")
        report = json.loads((Path(case["folder"]) / "report" / "report.json").read_text())
        baseline = next(c for c in report["criteria"] if c["id"] == "baseline_consistency")
        # The same instant in another format is not a difference; the changed status is.
        self.assertEqual(set(baseline["evidence"]["different"]), {"status"})
        gone = self.fixture(a=NOT_FOUND)
        gone.run(gone.authorize())
        self.assertEqual(gone.findings(), {"run_not_retrievable": "uncertain"})

    def test_negative_cases_pass_only_on_the_documented_not_found(self):
        f = self.fixture(b=record(), c={"stderr": "Failed: 404 Deployment not found"})
        code, out = f.run(f.authorize())
        self.assertEqual(f.results(), ["pass", "supported_failure", "fixture_problem"], out)
        self.assertEqual(f.findings(), {"run_visible_elsewhere": "uncertain"})
        g = self.fixture(b={"stderr": "Failed: 403 Forbidden"},
                         c={"stderr": "Failed: 500 Internal Server Error"})
        g.run(g.authorize())
        self.assertEqual(g.results(), ["pass", "access_problem", "service_error"])
        h = self.fixture(b={"stderr": "Failed: connection reset by peer"},
                         c={"stdout": json.dumps({"unexpected": True})})
        h.run(h.authorize())
        self.assertEqual(h.results(), ["pass", "inconclusive", "inconclusive"])
        self.assertEqual((g.findings(), h.findings()), ({}, {}))

    def test_rejected_authentication_stops_without_a_pass(self):
        f = self.fixture(a={"stderr": "Failed: 401 Unauthorized " + CANARY})
        auth = f.authorize()
        code, out = f.run(auth)
        self.assertEqual((code, f.calls()), (campaign.STOPPED, 1), out)
        self.assertEqual(f.results(), ["access_problem", "pending", "pending"])
        self.assertEqual(f.state()["stopped"]["reason"], "the service rejected authentication")
        self.assertNotIn(CANARY, f.saved_text() + out)
        code, _ = f.run(auth)  # a fresh invocation: still stopped, nothing requested
        self.assertEqual((code, f.calls()), (campaign.STOPPED, 1))

    def test_fresh_context_resumes_without_repeats_or_budget_reset(self):
        f = self.fixture()
        auth = f.authorize()

        def interrupted(args, out, record):
            code = collect.run(args, out, record)
            if args.case == "seren.cloud-run.other-deployment":
                raise KeyboardInterrupt  # after the request, before its result is recorded
            return code

        with self.assertRaises(KeyboardInterrupt):
            f.run(auth, interrupted)
        self.assertEqual(f.results(), ["pass", "in_flight", "pending"])
        self.assertEqual(f.calls(), 2)
        code, _ = f.run(auth)  # a new Campaign object reads only the durable record
        self.assertEqual(f.results(), ["pass", "uncertain_interrupted", "pass"])
        self.assertEqual((code, f.calls()), (campaign.OK, 3))
        self.assertEqual(f.state()["budget"], {"max_service_requests": 3, "reserved": 3,
                                               "issued": 2, "issued_unknown": 1})
        before = f.findings()
        code, _ = f.run(auth)
        self.assertEqual((code, f.calls(), f.findings()), (campaign.OK, 3, before))

    def test_timeout_budget_and_changed_authority_are_honest(self):
        f = self.fixture(a=dict(record(), sleep=3))
        auth = f.authorize(max_service_requests=2)
        code, out = f.run(auth)
        self.assertEqual(f.results(), ["uncertain_timeout", "pass", "pending"], out)
        self.assertEqual((code, f.calls()), (campaign.STOPPED, 2))
        self.assertIn("budget exhausted", f.state()["stopped"]["reason"])
        code, _ = f.run(auth)
        self.assertEqual((code, f.calls()), (campaign.STOPPED, 2))
        bigger = f.authorize("auth-2.json")
        out = io.StringIO()
        code = campaign.run(_args("run", f, bigger), out)
        self.assertEqual((code, f.calls()), (campaign.REFUSED, 2))
        self.assertIn("cannot extend or reset", out.getvalue())

    def test_authorization_must_match_this_exact_campaign(self):
        f = self.fixture()
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        for override in ({"definition_sha256": "0" * 64}, {"expires_at": expired},
                         {"max_service_requests": 4}, {"retries": 1},
                         {"requests": {"seren.cloud-run.record-retrieval": ["agent"]}}):
            out = io.StringIO()
            code = campaign.run(_args("run", f, f.authorize(**override)), out)
            self.assertEqual(code, campaign.REFUSED, override)
        self.assertEqual(f.calls(), 0)
        self.assertFalse((f.home / "campaigns" / "seren.run-record-retrieval"
                          / "state.json").exists())

    def test_real_entry_point_runs_and_reports_status(self):
        f = self.fixture()
        auth = f.authorize()
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["campaign", "run", str(f.definition), "--authorization", str(auth),
                             "--home", str(f.home), "--root", str(f.root)])
        self.assertEqual((code, f.calls()), (campaign.OK, 3))
        out = io.StringIO()
        self.assertEqual(campaign.run(_args("status", f), out), campaign.OK)
        self.assertIn("Next action: None. The campaign is closed (complete)", out.getvalue())
        self.assertNotIn(CANARY, f.saved_text())

    def test_only_the_exact_run_get_shape_is_allowed(self):
        good = ["agent", "cloud", "run", "get", "--deployment-id", OWN, RUN]
        self.assertTrue(collect.read_only_args(good))
        for bad in (good + ["--watch"], good[:4] + [OWN, RUN],
                    good[:5] + [OWN, "not-a-uuid"], good[:5] + [OWN.upper(), RUN],
                    good[:4] + ["--deployment", OWN, RUN], ["agent", "cloud", "run", "cancel",
                                                            "--deployment-id", OWN, RUN]):
            self.assertFalse(collect.read_only_args(bad), bad)


def _args(action, f, auth=None):
    import argparse
    return argparse.Namespace(action=action, definition=str(f.definition),
                              authorization=str(auth) if auth else None, home=str(f.home),
                              root=str(f.root))


if __name__ == "__main__":
    unittest.main()
