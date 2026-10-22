"""Synthetic event receipts only: no CLI, credential, database or network access."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE
from seren_harness import employee_tool as tool
from seren_harness.models import PASSED, BLOCKED, FAILED
from seren_harness.workflow_review import evaluate, review

CASE = json.loads(
    (SOURCE / "cases/seren-employee-tool-constant-query.json").read_text()
)
RUN = "11111111-1111-4111-8111-111111111111"


def fixture():
    spec = CASE["employee_tool"]
    request = {
        "deployment_id": spec["deployment_id"],
        "run_id": RUN,
        "execution_id": "synthetic-execution",
        "message": spec["message"],
    }
    events = [
        {
            "type": "tool_call",
            "id": "synthetic-call",
            "name": spec["tool"],
            "arguments": json.dumps(spec["arguments"]),
        },
        {
            "type": "tool_result",
            "id": "synthetic-call",
            "is_error": False,
            "content": json.dumps({"data": spec["result"]}),
        },
        {"type": "text", "text": "42"},
        {"type": "done", "status": "ok"},
    ]
    run = {
        "id": RUN,
        "deployment_id": spec["deployment_id"],
        "execution_id": "synthetic-execution",
        "status": "completed",
        "invocation_payload": {"message": spec["message"]},
        "output_events": events,
        "completed_at": "2026-10-11T00:00:01Z",
    }
    first = {
        "status": "ok",
        "exit_code": 0,
        "request": request,
        "response": {"data": run},
    }
    close = {
        "status": "ok",
        "exit_code": 0,
        "observed_at": "2026-10-11T00:00:02Z",
        "response": {
            "data": {
                "id": spec["deployment_id"],
                "status": "stopped",
                "desired_lifecycle_state": "stopped",
            }
        },
    }
    return first, close


def items(first, close):
    return [
        {
            "id": "run",
            "role": "initial",
            "kind": "api_response",
            "text": json.dumps(first),
        },
        {
            "id": "shutdown",
            "role": "complete_view",
            "kind": "api_response",
            "text": json.dumps(close),
        },
    ]


class EmployeeToolTests(unittest.TestCase):
    def test_complete_chain_passes_checks_but_never_claims_live_acceptance(self):
        first, close = fixture()
        receipt = items(first, close)
        for check in (
            tool.run_identity,
            tool.tool_execution,
            tool.employee_answer,
            tool.shutdown,
        ):
            self.assertEqual(check(CASE, receipt, True)[0], PASSED)
        report, _ = evaluate(
            CASE,
            {"provenance": "simulated", "complete_reading_captured": True},
            receipt,
        )
        self.assertEqual(report["verdict"], "incomplete")
        self.assertFalse(report["scoped_success"])
        # Even otherwise accepted evidence cannot bypass the shipped unresolved setup gate.
        report, _ = evaluate(
            CASE,
            {"provenance": "independently_observed", "complete_reading_captured": True},
            receipt,
        )
        self.assertEqual(report["verdict"], "incomplete")

    def test_answer_without_tool_and_old_unstructured_receipt_cannot_pass(self):
        for events in (
            None,
            [],
            [{"type": "text", "text": "42"}, {"type": "done", "status": "ok"}],
        ):
            first, close = fixture()
            run = first["response"]["data"]
            run["output_events"] = events
            run["output"] = json.dumps(
                {"response": "42", "tool_calls": [{"tool": "seren_db_query"}]}
            )
            self.assertNotEqual(
                tool.tool_execution(CASE, items(first, close), True)[0], PASSED
            )
            self.assertEqual(
                tool.employee_answer(CASE, items(first, close), True)[0], BLOCKED
            )

    def test_wrong_identity_or_uncorrelated_invocation_is_unverified(self):
        for field, value in [
            ("id", "other"),
            ("deployment_id", "other"),
            ("execution_id", "other"),
            ("invocation_payload", {"message": "unrelated"}),
        ]:
            first, close = fixture()
            first["response"]["data"][field] = value
            self.assertEqual(
                tool.run_identity(CASE, items(first, close), True)[0], BLOCKED
            )
            self.assertEqual(
                tool.tool_execution(CASE, items(first, close), True)[0], BLOCKED
            )

    def test_wrong_tool_arguments_results_answer_and_extra_call_are_rejected(self):
        for location, field, value in [
            (0, "name", "other"),
            (0, "arguments", json.dumps({"query": "SELECT * FROM accounts"})),
            (1, "is_error", True),
            (
                1,
                "content",
                json.dumps(
                    {"data": {"columns": ["answer"], "row_count": 1, "rows": [[41]]}}
                ),
            ),
        ]:
            first, close = fixture()
            first["response"]["data"]["output_events"][location][field] = value
            self.assertEqual(
                tool.tool_execution(CASE, items(first, close), True)[0], FAILED
            )
        first, close = fixture()
        ev = first["response"]["data"]["output_events"]
        ev.insert(1, copy.deepcopy(ev[0]))
        self.assertEqual(
            tool.tool_execution(CASE, items(first, close), True)[0], FAILED
        )
        first, close = fixture()
        first["response"]["data"]["output_events"][2]["text"] = "41"
        self.assertEqual(
            tool.employee_answer(CASE, items(first, close), True)[0], FAILED
        )

    def test_missing_link_partial_trace_and_unconfirmed_shutdown_never_pass(self):
        first, close = fixture()
        first["response"]["data"]["output_events"][1]["id"] = "other"
        self.assertEqual(
            tool.tool_execution(CASE, items(first, close), True)[0], BLOCKED
        )
        first, close = fixture()
        self.assertEqual(
            tool.tool_execution(CASE, items(first, close), False)[0], BLOCKED
        )
        for change in (
            {"status": "running"},
            {"desired_lifecycle_state": "running"},
            {"id": "other"},
        ):
            first, close = fixture()
            close["response"]["data"].update(change)
            self.assertEqual(tool.shutdown(CASE, items(first, close), True)[0], BLOCKED)
        for at in (None, "unknown", "2026-10-10T00:00:00Z"):
            first, close = fixture()
            close["observed_at"] = at
            self.assertEqual(tool.shutdown(CASE, items(first, close), True)[0], BLOCKED)

    def test_selective_approval_names_only_one_exact_call_and_checkpoint(self):
        spec = CASE["employee_tool"]
        pending = {
            "data": {
                "run_id": RUN,
                "status": "awaiting_approval",
                "checkpoint_id": "checkpoint-1",
                "pending_approvals": [
                    {
                        "id": "approval-1",
                        "tool": spec["tool"],
                        "args": spec["arguments"],
                    }
                ],
            }
        }
        self.assertEqual(
            tool.exact_approval(spec, pending, RUN),
            {
                "resume_checkpoint_id": "checkpoint-1",
                "approval_decisions": [{"id": "approval-1", "decision": "approve"}],
            },
        )
        for mutation in (
            "extra",
            "wrong-tool",
            "wrong-query",
            "missing-id",
            "wrong-run",
            "no-checkpoint",
            "script",
        ):
            p = copy.deepcopy(pending)
            d = p["data"]
            a = d["pending_approvals"][0]
            if mutation == "extra":
                d["pending_approvals"].append(copy.deepcopy(a))
            if mutation == "wrong-tool":
                a["tool"] = "other"
            if mutation == "wrong-query":
                a["args"] = {"query": "DELETE FROM accounts"}
            if mutation == "missing-id":
                a.pop("id")
            if mutation == "wrong-run":
                d["run_id"] = "other"
            if mutation == "no-checkpoint":
                d.pop("checkpoint_id")
            if mutation == "script":
                a["script_publisher_approval"] = {}
            with self.assertRaises(ValueError):
                tool.exact_approval(spec, p, RUN)

    def test_existing_report_path_is_offline_and_sanitizes_sensitive_text(self):
        first, close = fixture()
        secret = "sk-" + "X" * 40
        first["response"]["data"]["output_events"][2]["text"] = secret
        bundle = {
            "provenance": "simulated",
            "complete_reading_captured": True,
            "notes": "Synthetic fixture only",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("socket.socket", side_effect=AssertionError("network")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess")),
        ):
            root = Path(tmp)
            report = review(
                CASE,
                bundle,
                items(first, close),
                root / "state",
                root / "report",
                None,
                None,
            )
            self.assertEqual(report["verdict"], "failed")
            for p in root.rglob("*"):
                if p.is_file():
                    self.assertNotIn(secret.encode(), p.read_bytes())
