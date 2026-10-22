"""Subscription guard checks use synthetic usage and stand-in processes only."""
import copy
from datetime import datetime, timezone
import json
import time
import unittest
from unittest import mock

from seren_harness import claude_budget as budget, discovery, model_worker
from test_model_worker import enabled_home, fake_claude, INIT


def usage(percent=10, extra=False):
    reset = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
    return {"five_hour": {"utilization": percent, "resets_at": reset},
            "seven_day": {"utilization": percent, "resets_at": reset},
            "limits": [{"percent": percent, "resets_at": reset}],
            "extra_usage": {"is_enabled": extra}, "spend": {"enabled": extra}}


def configured():
    home = enabled_home()
    authority = home.authority()
    authority["model_worker"]["subscription_budget"] = copy.deepcopy(budget.POLICY)
    discovery.write_json(home.root / "authority.json", authority)
    return home, authority["model_worker"]


class BudgetTests(unittest.TestCase):
    def test_both_windows_and_additional_scoped_limits_preserve_reserve(self):
        self.assertTrue(budget.assess(usage(44.9), time.time())["allowed"])
        for key in ("five_hour", "seven_day", "seven_day_sonnet"):
            data = usage()
            data[key] = dict(data["five_hour"], utilization=45)
            self.assertFalse(budget.assess(data, time.time())["allowed"])
        data = usage()
        data["limits"][0]["percent"] = 50
        self.assertFalse(budget.assess(data, time.time())["allowed"])

    def test_paid_usage_enabled_or_unknown_never_allows(self):
        self.assertFalse(budget.assess(usage(extra=True), time.time())["allowed"])
        data = usage()
        data["spend"]["enabled"] = True
        self.assertFalse(budget.assess(data, time.time())["allowed"])
        for extra in ({}, {"is_enabled": None}, {"is_enabled": 0}):
            with self.assertRaises(budget.Unavailable):
                budget.assess(dict(usage(), extra_usage=extra), time.time())

    def test_missing_invalid_and_expired_evidence_is_not_zero_usage(self):
        mutations = [lambda d: d.pop("seven_day"),
                     lambda d: d["five_hour"].update(utilization=True),
                     lambda d: d["five_hour"].update(utilization=float("nan")),
                     lambda d: d["five_hour"].update(utilization=-1),
                     lambda d: d["five_hour"].update(resets_at="2020-01-01T00:00:00Z"),
                     lambda d: d["five_hour"].update(resets_at="2030-01-01"),
                     lambda d: d.update(limits=[{}])]
        for change in mutations:
            data = usage()
            change(data)
            with self.assertRaises(budget.Unavailable):
                budget.assess(data, time.time())

    def test_explicit_zero_null_reset_preserves_other_budget_checks(self):
        data = usage()
        data["five_hour"] = {"utilization": 0.0, "resets_at": None}
        data["limits"] = [{"percent": 0, "resets_at": None, "is_active": False}]
        self.assertTrue(budget.assess(data, time.time())["allowed"])
        data["seven_day"]["utilization"] = 48
        self.assertFalse(budget.assess(data, time.time())["allowed"])
        data["seven_day"] = {"utilization": 0, "resets_at": None}
        self.assertTrue(budget.assess(data, time.time())["allowed"])
        data["extra_usage"]["is_enabled"] = True
        self.assertFalse(budget.assess(data, time.time())["allowed"])

    def test_null_reset_does_not_accept_missing_nonzero_or_active_evidence(self):
        for window in ({"utilization": 0}, {"utilization": 1, "resets_at": None},
                       {"utilization": False, "resets_at": None},
                       {"utilization": 0, "resets_at": ""}):
            with self.assertRaises(budget.Unavailable):
                budget.assess(dict(usage(), five_hour=window), time.time())
        for window in ({"percent": 0}, {"percent": 1, "resets_at": None},
                       {"percent": 0, "resets_at": None, "is_active": True}):
            with self.assertRaises(budget.Unavailable):
                budget.assess(dict(usage(), limits=[window]), time.time())

    def test_no_cached_allow_and_blocked_polls_consume_no_requests(self):
        home, cfg = configured()
        fetch = mock.Mock(side_effect=[usage(), usage(48), usage()])
        self.assertTrue(budget.check(home, cfg, fetch=fetch)["allowed"])
        self.assertFalse(budget.check(home, cfg, fetch=fetch)["allowed"])
        self.assertFalse(budget.check(home, cfg, fetch=fetch)["allowed"])
        self.assertEqual(2, fetch.call_count)
        self.assertTrue(budget.check(home, cfg, force=True, fetch=fetch)["allowed"])

    def test_errors_and_bad_policy_fail_closed_without_secret_logging(self):
        home, cfg = configured()
        fetch = mock.Mock(side_effect=RuntimeError("SECRET"))
        self.assertFalse(budget.check(home, cfg, fetch=fetch)["allowed"])
        self.assertNotIn("SECRET", (home.root / "claude-budget-status.json").read_text())
        cfg["subscription_budget"] = None
        self.assertFalse(budget.check(home, cfg, fetch=fetch)["allowed"])
        self.assertEqual(1, fetch.call_count)

    def test_scheduler_blocks_before_session_reservation_or_worker_launch(self):
        home, cfg = configured()
        before = home.state()["sessions"]
        launch = mock.Mock()
        with mock.patch.object(budget, "fetch_usage", return_value=usage(48)):
            for _ in range(3):
                state = home.state()
                result = discovery._model(home, state, home.authority(), launch, lambda r: {})
                self.assertEqual("limited", result)
                self.assertEqual(before, state["sessions"])
        launch.assert_not_called()
        self.assertFalse((home.root / "worker.json").exists())

    def test_wrapper_checks_again_and_cannot_dispatch_on_block(self):
        home, cfg = configured()
        launch = mock.Mock()
        with mock.patch.object(budget, "fetch_usage", return_value=usage(extra=True)), \
                mock.patch("seren_harness.claude_live.dispatch") as dispatch:
            result, _ = model_worker.run_session(home, "budget-test", time.time()+30, cfg, launch)
        self.assertEqual("blocked:subscription_budget", result)
        launch.assert_not_called()
        dispatch.assert_not_called()

    def test_running_worker_stops_on_budget_loss_without_live_dispatch(self):
        home, cfg = configured()
        later = budget.POLICY["poll_seconds"] + 1
        with mock.patch.object(budget, "check", side_effect=[
                {"allowed": True}, {"allowed": False, "reason": "quota"}]), \
                mock.patch.object(model_worker.time, "monotonic", side_effect=[0, later, later]), \
                mock.patch("seren_harness.claude_live.dispatch") as dispatch:
            result, summary = model_worker.run_session(
                home, "budget-test", time.time()+30, cfg, fake_claude([INIT], sleep=30))
        self.assertEqual("blocked:subscription_budget", result)
        self.assertFalse(summary["subscription_budget"]["allowed"])
        dispatch.assert_not_called()

    def test_existing_audit_wait_does_not_query_usage_or_launch(self):
        home, cfg = configured()
        state = home.state()
        state["model"] = {"wait": {"until": "2030-01-01T00:00:00Z", "wake_on": "change",
                          "reason": "audit hold", "signals": discovery.model_wake_signals(
                              home, state, home.authority())}}
        launch = mock.Mock()
        with mock.patch.object(budget, "fetch_usage") as fetch:
            discovery._model(home, state, home.authority(), launch, lambda r: {})
        fetch.assert_not_called()
        launch.assert_not_called()
        self.assertEqual("audit hold", state["model"]["wait"]["reason"])

    def test_failure_diagnostics_are_safe_and_recovery_requires_fresh_evidence(self):
        home, cfg = configured()
        for reason in ("usage rate limited", "credential expired", "invalid usage reset"):
            result = budget.check(home, cfg, force=True,
                                  fetch=mock.Mock(side_effect=budget.Unavailable(reason)))
            self.assertFalse(result["allowed"])
            self.assertEqual(reason + "; no launch", result["reason"])
        result = budget.check(home, cfg, force=True,
                              fetch=mock.Mock(side_effect=budget.Unavailable("SECRET")))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertTrue(budget.check(home, cfg, force=True, fetch=usage)["allowed"])
        self.assertFalse(budget.check(home, cfg, force=True,
                                     fetch=lambda: usage(45))["allowed"])
        self.assertFalse(budget.check(home, cfg, force=True,
                                     fetch=lambda: usage(extra=True))["allowed"])

    def test_transport_failures_classified_without_logging_body_or_retry(self):
        credentials = {"claudeAiOauth": {"subscriptionType": "max",
                       "rateLimitTier": "default_claude_max_5x", "expiresAt": (time.time()+60)*1000,
                       "accessToken": "PRIVATE"}}
        for status, reason in ((401, "usage authentication rejected"),
                               (429, "usage rate limited"),
                               (503, "usage service unavailable"),
                               (302, "usage HTTP request rejected")):
            opener = mock.Mock()
            opener.open.side_effect = budget.error.HTTPError(
                "https://secret", status, "SECRET", {}, None)
            with mock.patch.object(budget.subprocess, "run",
                                   return_value=mock.Mock(stdout=json.dumps(credentials))), \
                    mock.patch.object(budget.request, "build_opener", return_value=opener):
                with self.assertRaises(budget.Unavailable) as caught:
                    budget.fetch_usage()
            self.assertEqual(reason, str(caught.exception))
            self.assertEqual(1, opener.open.call_count)

    def test_reader_uses_fixed_host_no_refresh_or_redirect_and_hides_errors(self):
        credentials = {"claudeAiOauth": {"subscriptionType": "max",
                       "rateLimitTier": "default_claude_max_5x", "expiresAt": (time.time()+60)*1000,
                       "accessToken": "PRIVATE"}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(usage()).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(budget.subprocess, "run", return_value=mock.Mock(stdout=json.dumps(credentials))), \
                mock.patch.object(budget.request, "build_opener", return_value=opener):
            budget.fetch_usage()
        req = opener.open.call_args.args[0]
        self.assertEqual("https://api.anthropic.com/api/oauth/usage", req.full_url)
        self.assertEqual("GET", req.get_method())
        self.assertIsNone(budget.NoRedirect().redirect_request(None, None, 302, None, None, "https://other"))
        credentials["claudeAiOauth"]["expiresAt"] = 0
        with mock.patch.object(budget.subprocess, "run", return_value=mock.Mock(stdout=json.dumps(credentials))), \
                mock.patch.object(budget.request, "build_opener") as network:
            with self.assertRaises(budget.Unavailable):
                budget.fetch_usage()
        network.assert_not_called()
