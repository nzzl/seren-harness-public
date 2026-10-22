"""Synthetic status answers through the existing pinned-CLI collection path; no service calls."""
import json
from pathlib import Path
import tempfile
import unittest

from test_seren_cli_collect import CANARY, Fixture
from seren_harness import collect

CASE_ID = "seren.managed-employee.status-retrieval"
DEPLOYMENT = "7a00003a-0000-4000-8000-00000000003b"
ARGS = ["agent", "cloud", "deployment", "status", DEPLOYMENT]


def status_answer(status="stopped", desired="stopped", **extra):
    # Only the independently documented common subset is required. Optional fields such as
    # control_generation are deliberately absent (the supported owner view omits them).
    return {"fixture_notice": "SYNTHETIC stand-in response, not a live Seren observation",
            "data": {"id": DEPLOYMENT, "status": status,
                     "desired_lifecycle_state": desired, **extra}}


class StatusCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def fixture(self, **options):
        self.n += 1
        return Fixture(Path(self.tmp.name) / str(self.n), case_id=CASE_ID,
                       args=options.pop("args", ARGS),
                       answer=options.pop("answer", status_answer()), **options)

    def test_documented_states_are_retrievable_without_claiming_health_or_readiness(self):
        for observed, desired in [("stopped", "stopped"), ("building", "running"),
                                  ("running", "stopping"), ("failed", "running"),
                                  ("pending", "running")]:
            with self.subTest(observed=observed, desired=desired):
                f = self.fixture(answer=status_answer(observed, desired, health="ok"))
                code, out = f.run()
                self.assertEqual(code, collect.OK, out)
                report = f.report()
                states = report["criteria"][2]["evidence"]["observed"]
                self.assertEqual(states, {"status": observed, "desired_lifecycle_state": desired})
                self.assertEqual(report["verdict"], "passed")
                self.assertFalse(report["employee_certification"])
                unverified = {u["id"]: u["verdict"] for u in report["unverified"]}
                self.assertEqual(unverified["readiness"], "unverified")
                self.assertEqual(unverified["intent_convergence"], "unverified")
                self.assertEqual(f.log()["argv"], ARGS + ["-o", "json"])
                self.assertNotIn("SEREN_API_KEY", f.log()["env"])
                self.assertEqual(f.findings(), {})

    def test_wrong_response_identity_fails_and_wrong_config_target_makes_no_call(self):
        f = self.fixture(answer=status_answer(id="00000000-0000-0000-0000-000000000000"))
        self.assertEqual(f.run()[0], collect.FAILED_VERDICT)
        self.assertEqual(f.report()["criteria"][1]["verdict"], "failed")
        self.assertEqual(set(f.findings()), {"status_unavailable"})
        g = self.fixture(args=ARGS[:-1] + ["00000000-0000-0000-0000-000000000000"])
        self.assertEqual(g.run()[0], collect.COLLECTION_FAILED)
        self.assertEqual(g.calls(), 0)
        self.assertFalse(g.home.exists())

    def test_missing_invalid_or_health_only_fields_cannot_pass(self):
        for field in ("status", "desired_lifecycle_state"):
            for value in (None, "", True, {}, "healthy", "unknown-new-state", "MISSING"):
                with self.subTest(field=field, value=value):
                    answer = status_answer(health="ok")
                    if value == "MISSING":
                        del answer["data"][field]
                    else:
                        answer["data"][field] = value
                    f = self.fixture(answer=answer)
                    self.assertEqual(f.run()[0], collect.FAILED_VERDICT)
                    criterion = f.report()["criteria"][2]
                    self.assertEqual(criterion["verdict"], "failed")
                    self.assertIn(field, criterion["evidence"]["invalid_fields"])
        h = self.fixture(answer={"data": {"id": DEPLOYMENT, "health": "ok"}})
        self.assertEqual(h.run()[0], collect.FAILED_VERDICT)

    def test_access_errors_remain_unverified_and_secrets_stay_out_of_saved_output(self):
        for error in ("401 Unauthorized", "403 Forbidden"):
            f = self.fixture(mode="error:" + error + " " + CANARY)
            code, out = f.run()
            self.assertEqual(code, collect.INCOMPLETE, out)
            self.assertEqual({c["verdict"] for c in f.report()["criteria"]}, {"unverified"})
            self.assertEqual(f.findings(), {})
            self.assertNotIn(CANARY, f.saved_text() + out)
        g = self.fixture(answer=status_answer(extra_secret=CANARY))
        self.assertEqual(g.run()[0], collect.OK)
        self.assertNotIn(CANARY, g.saved_text())

    def test_not_found_and_missing_response_are_not_success(self):
        f = self.fixture(mode="error:404 Not Found")
        self.assertEqual(f.run()[0], collect.FAILED_VERDICT)
        self.assertEqual(f.report()["criteria"][0]["verdict"], "failed")
        g = self.fixture(answer={"data": None})
        self.assertEqual(g.run()[0], collect.FAILED_VERDICT)
        self.assertEqual(g.report()["criteria"][2]["verdict"], "unverified")

    def test_status_allowlist_rejects_mutations_options_and_extra_arguments(self):
        for args in (["agent", "cloud", "deployment", "start", DEPLOYMENT],
                     ["agent", "cloud", "deployment", "stop", DEPLOYMENT],
                     ARGS + ["unexpected"], ARGS + ["--api-key", CANARY]):
            with self.subTest(command=args[:4]):
                f = self.fixture(args=args)
                code, out = f.run()
                self.assertEqual(code, collect.COLLECTION_FAILED, out)
                self.assertEqual(f.calls(), 0)
                self.assertFalse(f.home.exists())
                self.assertNotIn(CANARY, out)

    def test_invalid_case_enums_are_rejected(self):
        from seren_support import SOURCE
        from seren_harness.workflow_review import _validate
        case = json.loads((SOURCE / "cases/seren-managed-employee-status-retrieval.json").read_text())
        for invalid in ({}, {"status": []}, {"status": [True]}, {"status": "stopped"}):
            case["api"]["field_values"] = invalid
            with self.assertRaises(ValueError):
                _validate(case)
