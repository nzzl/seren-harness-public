"""Synthetic clock receipts; no Seren/Discord calls or current-time oracle leakage."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE
from seren_harness import employee_tool as tool
from seren_harness.models import BLOCKED, FAILED, PASSED
from seren_harness.workflow_review import evaluate, review, _validate
from test_employee_tool import fixture as database_fixture, items

CASE = json.loads((SOURCE / "cases/seren-employee-system-time.json").read_text())
STAMP = "2026-10-11T00:00:01Z"


def fixture():
    first, close = database_fixture()
    first["request"]["message"] = CASE["employee_tool"]["message"]
    run = first["response"]["data"]
    run["invocation_payload"]["message"] = CASE["employee_tool"]["message"]
    run["output_events"][0].update(name="system_time", arguments="{}")
    run["output_events"][1]["content"] = STAMP
    run["output_events"][2]["text"] = STAMP
    first["observation_window"] = {
        "started_at": "2026-10-11T00:00:00Z",
        "finished_at": "2026-10-11T00:00:02Z",
        "elapsed_seconds": 2.0,
    }
    return first, close


class EmployeeTimeTests(unittest.TestCase):
    def test_valid_trace_reuses_checks_but_shipped_case_stays_incomplete(self):
        _validate(CASE)
        first, close = fixture()
        for check in (
            tool.run_identity,
            tool.tool_execution,
            tool.employee_answer,
            tool.shutdown,
        ):
            self.assertEqual(check(CASE, items(first, close), True)[0], PASSED)
        for provenance in ("simulated", "independently_observed"):
            report, _ = evaluate(
                CASE,
                {"provenance": provenance, "complete_reading_captured": True},
                items(first, close),
            )
            self.assertEqual(report["verdict"], "incomplete")

    def test_missing_unsupported_or_unstable_window_is_unverified(self):
        for value in (
            None,
            {},
            {"started_at": STAMP},
            {"started_at": STAMP, "finished_at": STAMP, "elapsed_seconds": 50},
            {
                "started_at": STAMP,
                "finished_at": STAMP,
                "elapsed_seconds": float("nan"),
            },
            {"started_at": STAMP, "finished_at": STAMP, "elapsed_seconds": True},
        ):
            first, close = fixture()
            first["observation_window"] = value
            self.assertEqual(
                tool.tool_execution(CASE, items(first, close), True)[0], BLOCKED
            )

    def test_unknown_encoding_is_not_guessed(self):
        for value in (
            "today",
            "2026-10-11T00:00:01",
            "2026-99-11T00:00:01Z",
            json.dumps({"utc": STAMP}),
            "time: " + STAMP,
        ):
            first, close = fixture()
            first["response"]["data"]["output_events"][1]["content"] = value
            self.assertEqual(
                tool.tool_execution(CASE, items(first, close), True)[0], BLOCKED
            )

    def test_clock_tolerance_boundaries_and_stale_time(self):
        for stamp, expected in [
            ("2026-10-10T23:59:55Z", PASSED),
            ("2026-10-11T00:00:07Z", PASSED),
            ("2026-10-10T23:59:54.999999Z", FAILED),
            ("2026-10-11T00:00:07.000001Z", FAILED),
        ]:
            first, close = fixture()
            first["response"]["data"]["output_events"][1]["content"] = stamp
            self.assertEqual(
                tool.tool_execution(CASE, items(first, close), True)[0], expected
            )

    def test_guessed_answer_wrong_tool_extra_arguments_and_extra_call_never_pass(self):
        for change in (
            "answer-only",
            "wrong-tool",
            "arguments",
            "extra-call",
            "wrong-answer",
        ):
            first, close = fixture()
            ev = first["response"]["data"]["output_events"]
            if change == "answer-only":
                del ev[:2]
            if change == "wrong-tool":
                ev[0]["name"] = "seren_db_query"
            if change == "arguments":
                ev[0]["arguments"] = '{"query":"anything"}'
            if change == "extra-call":
                ev.insert(1, copy.deepcopy(ev[0]))
            if change == "wrong-answer":
                ev[2]["text"] = "2026-10-11T00:00:02Z"
            self.assertNotEqual(
                tool.employee_answer(CASE, items(first, close), True)[0], PASSED
            )

    def test_partial_trace_wrong_identity_and_unconfirmed_shutdown_stay_unverified(
        self,
    ):
        first, close = fixture()
        self.assertEqual(
            tool.tool_execution(CASE, items(first, close), False)[0], BLOCKED
        )
        first["response"]["data"]["id"] = "other"
        self.assertEqual(
            tool.tool_execution(CASE, items(first, close), True)[0], BLOCKED
        )
        first, close = fixture()
        close["response"]["data"]["status"] = "running"
        self.assertEqual(tool.shutdown(CASE, items(first, close), True)[0], BLOCKED)

    @staticmethod
    def structured_time(stamp="2026-10-11T00:00:01.123456789Z"):
        return {"utc": stamp, "unix_epoch": 1791676801, "timezone": "UTC",
                "utc_offset": "+00:00", "date": "2026-10-11",
                "time": "00:00:01.123456789"}

    def test_observed_json_shape_and_exact_nanosecond_answer(self):
        first, close = fixture()
        data = self.structured_time()
        ev = first["response"]["data"]["output_events"]
        ev[1]["content"] = json.dumps(data)
        ev[2]["text"] = data["utc"]
        case = copy.deepcopy(CASE)
        case["target_status"] = "resolved"
        with mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("process")):
            report, _ = evaluate(case, {"provenance": "independently_observed",
                                       "complete_reading_captured": True}, items(first, close))
        self.assertEqual(report["verdict"], "passed")
        ev[2]["text"] = "2026-10-11T00:00:01.123456788Z"
        self.assertEqual(tool.employee_answer(case, items(first, close), True)[0], FAILED)

    def test_structured_time_requires_consistent_typed_metadata(self):
        for key, value in [("utc", "today"), ("unix_epoch", True),
                           ("unix_epoch", 1791676802), ("unix_epoch", 1791676801.0),
                           ("timezone", "local"), ("utc_offset", "+01:00"),
                           ("date", "2026-10-12"), ("time", "00:00:02.123456789")]:
            with self.subTest(key=key, value=value):
                first, close = fixture()
                data = self.structured_time()
                data[key] = value
                first["response"]["data"]["output_events"][1]["content"] = json.dumps(data)
                self.assertEqual(tool.tool_execution(CASE, items(first, close), True)[0], BLOCKED)
        for mutate in (lambda d: d.pop("time"), lambda d: d.update(other="unknown")):
            data = self.structured_time()
            mutate(data)
            self.assertIsNone(tool._clock_result(json.dumps(data)))
        text = json.dumps(self.structured_time())
        self.assertIsNone(tool._clock_result(text[:-1] + ', "timezone": "UTC"}'))

    def test_nanosecond_boundaries_are_not_rounded_into_passes(self):
        for stamp, expected in [
            ("2026-10-10T23:59:55.000000000Z", PASSED),
            ("2026-10-10T23:59:54.999999999Z", FAILED),
            ("2026-10-11T00:00:07.000000000Z", PASSED),
            ("2026-10-11T00:00:07.000000001Z", FAILED),
            ("2026-10-11T00:00:01.1234567890Z", BLOCKED),
            ("2026-10-11T00:00:01.123456789+01:00", BLOCKED),
        ]:
            first, close = fixture()
            first["response"]["data"]["output_events"][1]["content"] = stamp
            self.assertEqual(tool.tool_execution(CASE, items(first, close), True)[0], expected)

    def test_structured_clock_still_requires_trace_window_and_shutdown(self):
        for change in ("extra-call", "wrong-id", "is-error", "no-done", "no-window", "running"):
            first, close = fixture()
            ev = first["response"]["data"]["output_events"]
            data = self.structured_time()
            ev[1]["content"], ev[2]["text"] = json.dumps(data), data["utc"]
            if change == "extra-call":
                ev.insert(0, copy.deepcopy(ev[0]))
            if change == "wrong-id":
                ev[1]["id"] = "unrelated"
            if change == "is-error":
                ev[1]["is_error"] = True
            if change == "no-done":
                ev.pop()
            if change == "no-window":
                first.pop("observation_window")
            if change == "running":
                close["response"]["data"]["status"] = "running"
            case = copy.deepcopy(CASE)
            case["target_status"] = "resolved"
            report, _ = evaluate(case, {"provenance": "independently_observed",
                                       "complete_reading_captured": True}, items(first, close))
            self.assertNotEqual(report["verdict"], "passed")

    def test_invalid_clock_case_cannot_relax_limits(self):
        for key, value in [
            ("tool", "seren_db_query"),
            ("arguments", {"query": "SELECT 1"}),
            ("clock_tolerance_seconds", 999),
            ("max_observation_seconds", 999),
        ]:
            case = copy.deepcopy(CASE)
            case["employee_tool"][key] = value
            with self.assertRaises(ValueError):
                _validate(case)

    def test_report_is_offline_and_secrets_never_persist(self):
        first, close = fixture()
        secret = "sk-" + "Z" * 40
        first["response"]["data"]["output_events"][2]["text"] = secret
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("socket.socket", side_effect=AssertionError("network")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("process")),
        ):
            root = Path(tmp)
            report = review(
                CASE,
                {"provenance": "simulated", "complete_reading_captured": True},
                items(first, close),
                root / "state",
                root / "report",
                None,
                None,
            )
            self.assertEqual(report["verdict"], "failed")
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())
