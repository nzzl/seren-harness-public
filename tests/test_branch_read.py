"""Synthetic branch-read tests. No live subprocess, network or credentials."""

import copy
import hashlib
import io
import json
import unittest
from unittest import mock

from seren_support import SOURCE
from seren_harness import branch_read
from seren_harness.cli import main
from seren_harness.employee_run import ClockRun, validate
import test_publisher_lookup as lookup_tests

CASE = json.loads((SOURCE / "cases/seren-employee-branch-read.json").read_text())


class BranchReadTests(unittest.TestCase):
    def setUp(self):
        self.parent = lookup_tests.PublisherLookupTests(
            "test_actual_cli_offline_and_secret_sanitization"
        )
        self.parent.setUp()
        self.addCleanup(self.parent.doCleanups)
        self.f = f = self.parent.fx
        f.basis["lookup_baseline"] = copy.deepcopy(f.baseline)
        old_revision = f.baseline["active_revision_id"]
        f.baseline["active_revision_id"] = "77777777-7777-4777-8777-777777777777"
        f.basis["active_revision_id"] = f.baseline["active_revision_id"]
        f.case = copy.deepcopy(CASE)
        f.case["target_status"] = "resolved"
        f.case["employee_tool"]["deployment_id"] = f.baseline["deployment_id"]
        f.session["execution_scope"] = f.basis["execution_scope"] = branch_read.SCOPE
        spec = f.case["employee_tool"]
        self.detail = dict(
            f.baseline,
            status="stopped",
            max_tool_output_chars=32768,
            bundle={
                "instructions": [{"kind": "skill", "content": branch_read.prompt(spec)}]
            },
        )
        p = f.root / "branch-post-update.json"
        p.write_text(
            json.dumps(
                {"status": "ok", "exit_code": 0, "response": {"data": self.detail}}
            )
        )
        f.basis["branch_read_update"] = {
            "request": branch_read.patch(spec),
            "prior_revision": old_revision,
            "post_update_detail": {
                "path": str(p),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            },
        }
        f.write_basis()
        f.first["request"]["message"] = spec["message"]
        f.first["response"]["data"]["invocation_payload"]["message"] = spec["message"]
        for target in ("socket.socket", "subprocess.Popen"):
            patcher = mock.patch(target, side_effect=AssertionError("external access"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def driver(self, mode="ok"):
        driver, cli = self.parent.driver()
        original = cli.json
        spec = self.f.case["employee_tool"]

        def response(*args):
            r = original(*args)
            d = r.data["data"]
            if "managed-get" in args:
                d["bundle"] = self.detail["bundle"]
                if mode == "old-prompt":
                    d["bundle"] = {"instructions": []}
            elif "managed-deployment-tools" in args:
                t = next(t for t in d["tools"] if t["name"] == branch_read.TOOL)
                t["input_schema"] = branch_read.SCHEMA
                if mode == "blocked":
                    t["effective_policy"]["status"] = "blocked"
                if mode == "approval":
                    t["approval_type"] = "confirm"
                if mode == "side-effecting":
                    t["side_effecting"] = True
                if mode == "checkpoint":
                    t["checkpoint_required"] = True
            elif "run" in args and "get" in args:
                events = d["output_events"]
                events[0].update(
                    name=branch_read.TOOL, arguments=json.dumps(spec["arguments"])
                )
                data = dict(spec["result"], name="Synthetic branch")
                if mode == "wrong-project":
                    data["project_id"] = "88888888-8888-4888-8888-888888888888"
                if mode == "wrong-branch":
                    data["id"] = "88888888-8888-4888-8888-888888888888"
                if mode == "missing":
                    data.pop("name")
                if mode == "truncated":
                    data["truncated"] = True
                if mode == "secret":
                    data["credential_value"] = "sk-" + "Z" * 40
                events[1]["content"] = json.dumps(
                    data if mode == "bare" else {"data": data}
                )
                events[2]["text"] = spec["answer"]
                if mode == "cut":
                    events[1]["content"] = '{"truncated":true,"content":"cut'
                if mode == "wrong-tool":
                    events[0]["name"] = "seren_mcp_call_tool"
                if mode == "wrong-args":
                    events[0]["arguments"] = json.dumps(
                        dict(spec["arguments"], operation="post")
                    )
                if mode == "uncorrelated":
                    events[1]["id"] = "different"
                if mode == "extra-call":
                    events.insert(0, copy.deepcopy(events[0]))
                if mode == "wrong-answer":
                    events[2]["text"] = "guessed"
            return r

        cli.json = response
        return driver, cli

    def test_data_operation_matches_both_supported_shapes_and_exact_answer(self):
        for mode in ("ok", "bare"):
            validate(self.f.session, self.f.case)
            self.f.session["authorization_id"] = mode
            driver, cli = self.driver(mode)
            self.assertEqual(self.f.execute(driver), 0)
            self.assertEqual(sum("--async" in a for a in cli.calls), 1)
            self.assertEqual(sum("managed-stop" in a for a in cli.calls), 1)
            self.assertFalse(self.f.store.open_cleanup())
            self.assertLessEqual(self.f.ticks, 300)
            self.f.session["authorization_id"] = "22222222-2222-4222-8222-222222222222"

    def test_wrong_results_or_execution_fail_partial_trace_stays_incomplete(self):
        for mode, code in [
            ("wrong-project", 1),
            ("wrong-branch", 1),
            ("wrong-tool", 1),
            ("wrong-args", 1),
            ("extra-call", 1),
            ("wrong-answer", 1),
            ("missing", 2),
            ("cut", 2),
            ("truncated", 2),
            ("uncorrelated", 2),
        ]:
            with self.subTest(mode=mode):
                self.f.session["authorization_id"] = mode
                driver, _ = self.driver(mode)
                self.assertEqual(self.f.execute(driver), code)
                self.assertFalse(self.f.store.open_cleanup())

    def test_changed_policy_or_prompt_stops_before_start(self):
        for mode in (
            "blocked",
            "approval",
            "side-effecting",
            "checkpoint",
            "old-prompt",
        ):
            self.f.session["authorization_id"] = mode
            driver, cli = self.driver(mode)
            self.assertEqual(self.f.execute(driver), 2)
            self.assertFalse(any("managed-start" in a for a in cli.calls))

    def test_old_authority_changed_arguments_and_unproven_update_refused(self):
        f = self.f
        validate(f.session, f.case)
        for name in (
            "old-scope",
            "missing-receipt",
            "extra-patch",
            "wrong-prior",
            "query",
        ):
            with self.subTest(name=name):
                old = copy.deepcopy(f.basis)
                case = copy.deepcopy(f.case)
                if name == "old-scope":
                    f.session["execution_scope"] = "one-call-publisher-lookup-v2"
                elif name == "missing-receipt":
                    del f.basis["branch_read_update"]
                elif name == "extra-patch":
                    f.basis["branch_read_update"]["request"]["workload_limits"] = {
                        "max_iterations": 3
                    }
                elif name == "wrong-prior":
                    f.basis["branch_read_update"]["prior_revision"] = f.baseline[
                        "active_revision_id"
                    ]
                else:
                    case["employee_tool"]["arguments"]["query"] = {"sql": "SELECT 42"}
                f.write_basis()
                with self.assertRaises((KeyError, ValueError)):
                    validate(f.session, case)
                f.basis = old
                f.session["execution_scope"] = branch_read.SCOPE
        f.write_basis()
        # Changing every case-side ID cannot bypass the instruction read-back.
        case = copy.deepcopy(f.case)
        spec = case["employee_tool"]
        spec["result"]["id"] = spec["answer"] = "88888888-8888-4888-8888-888888888888"
        spec["arguments"]["path"] = (
            "/projects/"
            + spec["result"]["project_id"]
            + "/branches/"
            + spec["result"]["id"]
        )
        spec["message"] = branch_read.message(spec)
        with self.assertRaises(ValueError):
            validate(f.session, case)

    def test_actual_cli_is_noninteractive_and_saved_outputs_redact_secrets(self):
        f = self.f
        cp = f.root / "case.json"
        cp.write_text(json.dumps(f.case))
        f.session["case"] = str(cp)
        sp = f.root / "session.json"
        sp.write_text(json.dumps(f.session))
        _, cli = self.driver("secret")

        def driver(session, case, cli, store):
            return ClockRun(session, case, cli, store, f.clock, f.sleep, f.wall)

        with (
            mock.patch("seren_harness.employee_run.pinned_cli", return_value=cli),
            mock.patch("seren_harness.employee_run.ClockRun", side_effect=driver),
            mock.patch("builtins.input", side_effect=AssertionError("prompt")),
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            self.assertEqual(main(["employee-data", "--session", str(sp)]), 0)
        self.assertIn("Shutdown: confirmed", out.getvalue())
        for p in f.root.rglob("*"):
            if p.is_file():
                self.assertNotIn(("sk-" + "Z" * 40).encode(), p.read_bytes())
        spec = f.case["employee_tool"]
        projected = branch_read.detail_projection(
            dict(self.detail, credentials={"value": "private"}), f.baseline, spec
        )
        self.assertNotIn("credentials", projected)
        self.assertEqual(
            json.loads(
                (SOURCE / "docs/branch-read/managed-update.proposed.json").read_text()
            ),
            branch_read.patch(CASE["employee_tool"]),
        )
        self.assertEqual(
            (SOURCE / "docs/branch-read/prompt.md").read_text().strip(),
            branch_read.prompt(CASE["employee_tool"]),
        )
