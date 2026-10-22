"""Offline Seren->psql process chain; both programs are explicitly synthetic."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_seren_cli_collect import CANARY, Fixture
from seren_support import SOURCE
from seren_harness import collect, sql_probe

CASE = "seren.database.constant-query"
CASE_DATA = json.loads((SOURCE / "cases/seren-database-constant-query.json").read_text())
ARGS = CASE_DATA["execution_args"]
EXPECTED = {"database": "serendb", "answer": 42, "read_only": "on"}
SEREN = r'''#!{python}
import os, subprocess, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("seren 0.10.0"); sys.exit(0)
home = Path(os.environ["XDG_CONFIG_HOME"])
n = int((home / "calls").read_text()) if (home / "calls").exists() else 0
(home / "calls").write_text(str(n + 1))
args = sys.argv[1:]
assert args[:3] == ["-o", "json", "psql"]
args = args[args.index("--") + 1:]
# Fake sensitive URI: never sent to a real service or saved by the harness.
raise SystemExit(subprocess.run(["psql", "postgres://user:opaque-private-value@invalid/db", *args]).returncode)
'''
PSQL = r'''#!{python}
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ["--version"]:  # fixture pre-run only: no log, no mode
    sys.exit(0)
home = Path(__file__).parent.parent / "config"
mode = (home / "mode").read_text()
(home / "log.json").write_text(json.dumps({"argv":sys.argv[2:],"env":dict(os.environ)}))
if mode == "slow":
    time.sleep(2)
    (home / "orphan_finished").write_text("should never finish after timeout")
if mode.startswith("error:"):
    sys.stderr.write(mode[6:] + " password opaque-private-value " + sys.argv[1]); sys.exit(1)
if mode == "invalid":
    print("not JSON opaque-private-value"); sys.exit(0)
print((home / "answer.json").read_text())
'''


class SqlProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def fixture(self, answer=None, mode="ok"):
        self.n += 1
        # The stand-in chains two processes (seren -> psql). Immediate modes get headroom
        # for slow hosts (the suite runs about 2x slower inside the worker sandbox); the
        # deliberately slow mode keeps the 1 s limit so the timeout path stays tested.
        f = Fixture(Path(self.tmp.name)/str(self.n), case_id=CASE, args=ARGS,
                    answer=EXPECTED if answer is None else answer, mode=mode,
                    timeout_seconds=1 if mode == "slow" else 4)
        cli = f.root / "seren"
        cli.write_text(SEREN.replace("{python}", sys.executable))
        bin_ = f.root / "bin"
        bin_.mkdir()
        psql = bin_ / "psql"
        psql.write_text(PSQL.replace("{python}", sys.executable))
        psql.chmod(0o755)
        # macOS assesses every newly created executable on its first run (Gatekeeper
        # exec-time scan: about 0.15 s median, 0.9 s and once 6.6 s observed during worker
        # checks). Run both fresh stand-ins once here, untimed, so that one-time cost never
        # lands inside the collector's timed version check or probe; later runs of the same
        # files are not reassessed (about 0.01 s). Neither pre-run counts as a call or logs.
        for program in (cli, psql):
            subprocess.run([str(program), "--version"], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=120, check=True)
        self.update(f, executable_sha256=hashlib.sha256(cli.read_bytes()).hexdigest(),
                    psql_executable=str(psql), psql_sha256=hashlib.sha256(psql.read_bytes()).hexdigest())
        return f

    def update(self, f, **values):
        target = json.loads(f.target.read_text())
        target["collectors"][CASE].update(values)
        f.target.write_text(json.dumps(target))

    def test_fixture_pre_run_is_not_a_service_call_or_a_psql_invocation(self):
        f = self.fixture(mode="slow")
        self.assertEqual(f.calls(), 0)
        self.assertFalse((f.config / "log.json").exists())
        self.assertFalse((f.config / "orphan_finished").exists())

    def test_exact_query_runs_noninteractively_through_both_programs(self):
        f = self.fixture()
        with mock.patch.dict(os.environ, {"PGOPTIONS":"-c default_transaction_read_only=off",
                "PGPASSWORD":CANARY, "PGSERVICE":"unwanted", "SEREN_API_KEY":CANARY}):
            code, out = f.run()
        self.assertEqual(code, collect.OK, out)
        self.assertEqual(f.calls(), 1)
        self.assertEqual(f.log()["argv"], ARGS[10:])
        env = f.log()["env"]
        self.assertIn("default_transaction_read_only=on", env["PGOPTIONS"])
        self.assertIn("statement_timeout=10000", env["PGOPTIONS"])
        self.assertEqual(env["PGPASSFILE"], "/dev/null")
        for key in ("PGPASSWORD", "PGSERVICE", "SEREN_API_KEY", "SEREN_API_BASE"):
            self.assertNotIn(key, env)
        self.assertEqual(f.report()["verdict"], "passed")
        self.assertFalse(f.report()["employee_certification"])
        self.assertEqual(f.report()["criteria"][1]["evidence"]["observed"], EXPECTED)
        self.assertIn("employee_tool", [x["id"] for x in f.report()["unverified"]])
        self.assertEqual(f.findings(), {})

    def test_wrong_missing_extra_or_typed_differently_result_never_passes(self):
        answers = [dict(EXPECTED, answer=41), dict(EXPECTED, answer="42"),
                   dict(EXPECTED, answer=True), dict(EXPECTED, read_only="off"),
                   dict(EXPECTED, database="other"), dict(EXPECTED, unexpected=1),
                   {"database":"serendb"}, {"status":"accepted"}, {"health":"ok"}, []]
        for answer in answers:
            with self.subTest(answer=answer):
                f = self.fixture(answer=answer)
                # Fixture's helper substitutes a catalog for falsey data; use exact input.
                (f.config / "answer.json").write_text(json.dumps(answer))
                self.assertEqual(f.run()[0], collect.FAILED_VERDICT)
                self.assertFalse(f.report()["scoped_success"])

    def test_connection_failure_is_inconclusive_and_never_retried_or_leaked(self):
        for mode in ("error:401 Unauthorized " + CANARY, "error:connection refused"):
            f = self.fixture(mode=mode)
            code, out = f.run()
            self.assertEqual(code, collect.INCOMPLETE, out)
            self.assertEqual(f.calls(), 1)
            self.assertTrue(all(c["verdict"] == "unverified" for c in f.report()["criteria"]))
            for secret in (CANARY, "opaque-private-value", "postgres://user:"):
                self.assertNotIn(secret, out + f.saved_text())

    def test_changed_query_target_options_and_unpinned_binary_make_no_call(self):
        variants = [ARGS[:-1]+["SELECT * FROM business_records"],
                    ARGS[:-1]+[sql_probe.SQL+" DELETE FROM business_records"],
                    ARGS+["-c", "SELECT 1"], ARGS[:2]+["0"*36]+ARGS[3:],
                    ARGS[:6]+["other"]+ARGS[7:],
                    [a for a in ARGS if a != "-X"]]
        for args in variants:
            f = self.fixture()
            self.update(f, args=args)
            self.assertEqual(f.run()[0], collect.COLLECTION_FAILED)
            self.assertEqual(f.calls(), 0)
            self.assertFalse(f.home.exists())
        for values in ({"psql_sha256":"0"*64}, {"executable_sha256":"0"*64},
                       {"timeout_seconds":31}):
            f = self.fixture()
            self.update(f, **values)
            self.assertEqual(f.run()[0], collect.COLLECTION_FAILED)
            self.assertEqual(f.calls(), 0)

    def test_even_matching_case_cannot_authorize_arbitrary_sql(self):
        f = self.fixture()
        unsafe = ARGS[:-1] + ["DELETE FROM business_records"]
        self.update(f, args=unsafe)
        cases = f.root / "cases-root" / "cases"
        cases.mkdir(parents=True)
        case = dict(CASE_DATA, execution_args=unsafe)
        (cases / "unsafe.json").write_text(json.dumps(case))
        f.cases_root = cases.parent
        self.assertEqual(f.run()[0], collect.COLLECTION_FAILED)
        self.assertEqual(f.calls(), 0)
        self.assertFalse(f.home.exists())

    def test_timeout_kills_child_and_never_retries(self):
        f = self.fixture(mode="slow")
        code, out = f.run()
        self.assertEqual(code, collect.COLLECTION_FAILED, out)
        self.assertEqual(f.calls(), 1)
        self.assertFalse(f.home.exists())
        time.sleep(1.3)
        self.assertFalse((f.config / "orphan_finished").exists())

    def test_malformed_output_and_secrets_do_not_reach_saved_artifacts(self):
        f = self.fixture(mode="invalid")
        code, out = f.run()
        self.assertEqual(code, collect.COLLECTION_FAILED, out)
        self.assertEqual(f.calls(), 1)
        self.assertFalse(f.home.exists())
        self.assertNotIn("opaque-private-value", out)
        g = self.fixture(answer=dict(EXPECTED, extra_secret=CANARY))
        self.assertEqual(g.run()[0], collect.FAILED_VERDICT)
        self.assertNotIn(CANARY, g.saved_text())
