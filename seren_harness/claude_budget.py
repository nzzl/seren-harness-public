"""Read-only subscription guard. Never refresh credentials or buy/enable extra usage.

The OAuth usage endpoint is observed Claude client behavior, not a stable public API.
Unknown/unavailable data blocks the worker. Percentages are account-wide, not per loop.
"""
from datetime import datetime, timezone
import json
import math
import subprocess
import time
from urllib import error, request

POLICY = {"personal_reserve_percent": 50, "stop_at_percent": 45,
          "poll_seconds": 300, "blocked_poll_seconds": 300,
          "require_extra_usage_disabled": True}


# Only these constant reasons may reach durable logs; never stringify transport errors.
SAFE_REASONS = frozenset({
    "credential read failed", "credential data invalid", "subscription identity mismatch",
    "credential expired", "credential token missing", "usage authentication rejected",
    "usage rate limited", "usage service unavailable", "usage HTTP request rejected",
    "usage transport unavailable", "usage response too large", "invalid usage response",
    "invalid usage window", "invalid usage reset", "stale or invalid usage reset",
    "required usage window missing", "invalid usage limits", "invalid usage limit",
    "active usage window lacks reset", "paid extra usage setting unknown",
    "paid spending setting unknown",
})


class Unavailable(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_usage():
    """One fixed-host GET with an existing unexpired Max 5x OAuth token; no retries.

    Capture credentials only in memory. Never put tokens or raw exceptions in logs.
    Disable environment proxies and redirects to avoid forwarding credentials elsewhere.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5, check=True)
    except Exception:
        raise Unavailable("credential read failed") from None
    try:
        auth = json.loads(result.stdout)["claudeAiOauth"]
        if not isinstance(auth, dict):
            raise ValueError()
        expires = auth.get("expiresAt")
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ValueError()
    except Exception:
        raise Unavailable("credential data invalid") from None
    if (auth.get("subscriptionType") != "max" or
            auth.get("rateLimitTier") != "default_claude_max_5x"):
        raise Unavailable("subscription identity mismatch")
    if expires <= time.time() * 1000:
        raise Unavailable("credential expired")
    if not isinstance(auth.get("accessToken"), str) or not auth["accessToken"]:
        raise Unavailable("credential token missing")
    try:
        req = request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + auth["accessToken"],
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "seren-harness-subscription-budget"})
        with request.build_opener(request.ProxyHandler({}), NoRedirect()).open(req, timeout=10) as response:
            payload = response.read(262145)
    except error.HTTPError as exc:
        reason = ("usage authentication rejected" if exc.code in (401, 403) else
                  "usage rate limited" if exc.code == 429 else
                  "usage service unavailable" if 500 <= exc.code <= 599 else
                  "usage HTTP request rejected")
        raise Unavailable(reason) from None
    except Exception:
        raise Unavailable("usage transport unavailable") from None
    if len(payload) > 262144:
        raise Unavailable("usage response too large")
    try:
        return json.loads(payload)
    except Exception:
        raise Unavailable("invalid usage response") from None


def assess(data, now):
    """Validate required windows and every additional non-null subscription window.

    Also check the newer limits array when supplied; model-specific windows are treated
    conservatively even when marked inactive. A past reset requires a fresh valid read.
    """
    if not isinstance(data, dict):
        raise Unavailable("invalid usage response")
    windows = {}
    missing = object()

    def add(name, percent, reset):
        if (type(percent) not in (int, float) or not math.isfinite(percent) or
                not 0 <= percent <= 100):
            raise Unavailable("invalid usage window")
        # Claude represents a reset, not-yet-started window as explicit zero/null.
        # Missing fields, nonzero/null and stale timestamps remain unavailable.
        if reset is None and percent == 0:
            windows[name] = {"used_percent": percent, "resets_at": None}
            return
        if not isinstance(reset, str):
            raise Unavailable("invalid usage reset")
        try:
            stamp = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.timestamp() <= now:
                raise ValueError()
        except ValueError:
            raise Unavailable("stale or invalid usage reset") from None
        windows[name] = {"used_percent": percent, "resets_at": reset}

    for name in ("five_hour", "seven_day"):
        if not isinstance(data.get(name), dict):
            raise Unavailable("required usage window missing")
    for name, value in data.items():
        if name in ("five_hour", "seven_day") or name.startswith("seven_day_") and name != "seven_day_breakdown":
            if value is not None:
                if not isinstance(value, dict):
                    raise Unavailable("invalid usage window")
                add(name, value.get("utilization"), value.get("resets_at", missing))
    if "limits" in data:
        if not isinstance(data["limits"], list):
            raise Unavailable("invalid usage limits")
        for index, value in enumerate(data["limits"]):
            if not isinstance(value, dict):
                raise Unavailable("invalid usage limit")
            if value.get("is_active") is True and value.get("resets_at") is None:
                raise Unavailable("active usage window lacks reset")
            add("limit_%d" % index, value.get("percent"),
                value.get("resets_at", missing))
    extra = data.get("extra_usage")
    if not isinstance(extra, dict) or type(extra.get("is_enabled")) is not bool:
        raise Unavailable("paid extra usage setting unknown")
    if "spend" in data and (not isinstance(data["spend"], dict) or
                           type(data["spend"].get("enabled")) is not bool):
        raise Unavailable("paid spending setting unknown")
    paid = extra["is_enabled"] or data.get("spend", {}).get("enabled", False)
    exhausted = [k for k, v in windows.items() if v["used_percent"] >= POLICY["stop_at_percent"]]
    reasons = (["paid extra usage is enabled"] if paid else [])
    if exhausted:
        reasons.append("personal reserve guard reached: " + ", ".join(exhausted))
    return {"allowed": not reasons, "reason": "; ".join(reasons) or "within subscription budget",
            "windows": windows, "extra_usage_enabled": paid}


def check(home, cfg, force=False, fetch=None):
    """No configured budget means legacy behavior. A configured guard cannot fail open.

    Only blocked results are cached, so a stale allow can never launch a model. A blocked
    scheduler poll reads at most once per five minutes and consumes no model session.
    """
    from seren_harness import discovery
    if "subscription_budget" not in cfg:
        return {"allowed": True, "reason": "subscription budget not configured"}
    now = time.time()
    path = home.root / "claude-budget-status.json"
    if cfg["subscription_budget"] != POLICY:
        result = {"allowed": False, "reason": "invalid subscription budget policy"}
    else:
        previous = discovery.read_json(path, {})
        if (not force and previous.get("allowed") is False and
                type(previous.get("checked_epoch")) in (int, float) and
                0 <= now - previous["checked_epoch"] < POLICY["blocked_poll_seconds"]):
            return previous
        try:
            result = assess((fetch or fetch_usage)(), now)
        except Unavailable as exc:
            reason = str(exc) if str(exc) in SAFE_REASONS else "usage unavailable or unrecognized"
            result = {"allowed": False, "reason": reason + "; no launch"}
        except Exception:
            result = {"allowed": False, "reason": "usage unavailable or unrecognized; no launch"}
    result.update(checked_epoch=now, checked_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                  policy=POLICY)
    discovery.write_json(path, result)
    return result
