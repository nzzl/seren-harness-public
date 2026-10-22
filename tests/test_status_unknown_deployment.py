"""The proposed case seren.managed-employee.status-unknown-deployment, offline.

SYNTHETIC: the shipped case, target request and catalog entry run through the real catalog
campaign path against a stand-in Seren CLI that answers the status command. These are
wiring and classification checks, not service evidence.
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
from seren_harness import campaign, cli, collect, selection
from seren_harness.workflow_review import ANSWER_CLASSES
from seren_harness.state import Store

CASE = "seren.managed-employee.status-unknown-deployment"
FILE = "seren-managed-employee-status-unknown-deployment.json"
UNKNOWN = "7a000033-0000-4000-8000-000000000034"
CANARY = "sk-" + "Q" * 40
STAND_IN = r'''#!{python}
import json, os, sys, time
from pathlib import Path
home = Path(os.environ["XDG_CONFIG_HOME"])
if sys.argv[1:] == ["--version"]:
    print("seren 0.10.0"); sys.exit(0)
calls = home / "calls"
calls.write_text(str(int(calls.read_text()) + 1 if calls.exists() else 1))
assert sys.argv[1:] == ["agent", "cloud", "deployment", "status", "{unknown}", "-o", "json"]
answer = json.loads((home / "answer.json").read_text())
time.sleep(answer.get("sleep", 0))
if "stdout" in answer:
    print(answer["stdout"]); sys.exit(0)
sys.stderr.write(answer["stderr"]); sys.exit(1)
'''
DEPLOYMENT = {"stdout": json.dumps({"data": {
    "id": UNKNOWN, "status": "stopped", "desired_lifecycle_state": "stopped"}})}


class Root:
    """A temporary root holding the shipped case and catalog entry, with the target's
    executable replaced by the checksum-pinned stand-in."""

    def __init__(self, path, answer):
        # Startup allowance is separate from the timeout under test: immediate stand-ins
        # get room to start on a loaded host; the deliberately sleeping answer (3 s) keeps
        # a short limit so the uncertain-timeout path is still exercised.
        self.timeout = 2 if isinstance(answer, dict) and answer.get("sleep") else 10
        self.root = Path(path)
        for name in ("cases", "targets", "catalogs"):
            (self.root / name).mkdir(parents=True)
        shutil.copy(SOURCE / "cases" / FILE, self.root / "cases" / FILE)
        self.config = self.root / "cli-config"
        self.config.mkdir()
        self.home = self.root / "reviews"
        executable = self.root / "seren"
        executable.write_text(standin(STAND_IN.replace("{python}", sys.executable)
                                      .replace("{unknown}", UNKNOWN)))
        executable.chmod(0o755)
        prerun(executable)  # first-run assessment outside the timed campaign call
        shipped = json.loads((SOURCE / "targets" / "seren.json").read_text())["collectors"][CASE]
        collector = dict(shipped, executable="{root}/seren", config_home="{root}/cli-config",
                         executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                         timeout_seconds=self.timeout)
        (self.root / "targets" / "seren.json").write_text(json.dumps(
            {"employee": "stand-in", "root": str(self.root), "collectors": {CASE: collector}}))
        catalog = json.loads((SOURCE / "catalogs" / "seren-read-only.json").read_text())
        catalog.update(request_timeout_seconds=self.timeout,
                       entries=[e for e in catalog["entries"] if e["case"] == CASE])
        self.catalog = self.root / "catalogs" / "catalog.json"
        self.catalog.write_text(json.dumps(catalog))
        (self.config / "answer.json").write_text(json.dumps(answer))

    def authorize(self):
        entry = json.loads(self.catalog.read_text())["entries"][0]
        now = datetime.now(timezone.utc)
        path = self.root / "auth.json"
        path.write_text(json.dumps({
            "catalog": "seren.read-only-catalog",
            "catalog_sha256": hashlib.sha256(self.catalog.read_bytes()).hexdigest(),
            "allow_subset_selection": True, "candidates": {CASE: entry["args"]},
            "max_service_requests": 1, "retries": 0, "request_timeout_seconds": self.timeout,
            "approved_by": "test owner (synthetic)", "approved_at": now.isoformat(),
            "not_before": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat()}))
        return path

    def run(self, auth=None):
        argv = ["campaign", "run", str(self.catalog), "--home", str(self.home),
                "--root", str(self.root), "--authorization", str(auth or self.authorize())]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}), \
                contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def calls(self):
        path = self.config / "calls"
        return int(path.read_text()) if path.exists() else 0

    def entry(self):
        state = json.loads((self.home / "campaigns" / "seren.read-only-catalog"
                            / "state.json").read_text())
        return state, state["cases"][CASE]

    def findings(self):
        if not (self.home / "findings" / "state.sqlite3").exists():
            return {}
        store = Store(self.home / "findings")
        try:
            return {f["context"]["finding"]: f["classification"] for f in store.findings()}
        finally:
            store.close()

    def saved_text(self):
        return "".join(p.read_text() for p in self.home.rglob("*") if p.is_file()
                       and p.suffix in (".json", ".md", ".txt"))


class StatusUnknownDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def root(self, answer):
        self.n += 1
        return Root(Path(self.tmp.name) / str(self.n), answer)

    def test_shipped_definition_is_frozen_allowlisted_and_untested(self):
        catalog = json.loads((SOURCE / "catalogs" / "seren-read-only.json").read_text())
        entry = next(e for e in catalog["entries"] if e["case"] == CASE)
        _, cases, target = selection.load_catalog(catalog, SOURCE)
        self.assertIsNone(selection.entry_problem(entry, cases, target,
                                                  catalog["request_timeout_seconds"]))
        self.assertTrue(collect.read_only_args(entry["args"]))
        self.assertEqual(cases[CASE]["execution_args"], entry["args"])
        outcomes = cases[CASE]["api"]["answer_outcomes"]
        self.assertEqual(set(outcomes), set(ANSWER_CLASSES))
        self.assertEqual(sorted(k for k, v in outcomes.items() if v == "pass"),
                         ["deployment_missing", "not_found"])
        self.assertEqual([k for k, v in outcomes.items() if v == "fail"], ["record"])
        self.assertIn("never_run_live_as_of", entry["history"])

    def test_documented_not_found_passes_with_one_request_and_no_finding(self):
        for stderr in ("Error: Failed: Unexpected Response: Response { status: 404, "
                       "headers: {} }", "Failed: 404 Deployment not found"):
            r = self.root({"stderr": stderr})
            code, out = r.run()
            state, entry = r.entry()
            self.assertEqual((code, r.calls(), entry["result"]), (campaign.OK, 1, "pass"), out)
            self.assertEqual(state["budget"]["max_service_requests"], 1)
            self.assertEqual(r.findings(), {})
            self.assertNotIn(CANARY, r.saved_text() + out)

    def test_a_returned_deployment_is_a_supported_failure_with_an_uncertain_finding(self):
        r = self.root(DEPLOYMENT)
        _, out = r.run()
        self.assertEqual((r.calls(), r.entry()[1]["result"]), (1, "supported_failure"), out)
        self.assertEqual(r.findings(), {"unknown_deployment_returned": "uncertain"})

    def test_access_errors_or_missing_evidence_never_pass(self):
        expected = {
            json.dumps({"stderr": "Failed: 401 Unauthorized " + CANARY}): "access_problem",
            json.dumps({"stderr": "Failed: 403 Forbidden"}): "access_problem",
            json.dumps({"stderr": "Failed: 500 Internal Server Error"}): "service_error",
            json.dumps({"stderr": "Failed: connection reset by peer"}): "inconclusive",
            json.dumps({"stdout": json.dumps({"unexpected": True})}): "inconclusive",
            json.dumps({"stdout": "not json"}): "malformed_response",
            json.dumps({"stdout": "{}", "sleep": 3}): "uncertain_timeout"}
        for answer, result in expected.items():
            r = self.root(json.loads(answer))
            auth = r.authorize()
            _, out = r.run(auth)
            self.assertEqual((r.calls(), r.entry()[1]["result"]), (1, result), out)
            self.assertNotEqual(r.entry()[1].get("verdict"), "passed")
            self.assertEqual(r.findings(), {})
            self.assertNotIn(CANARY, r.saved_text() + out)
            r.run(auth)  # a fresh invocation never replays or resets the single request
            self.assertEqual(r.calls(), 1, answer)


if __name__ == "__main__":
    unittest.main()
