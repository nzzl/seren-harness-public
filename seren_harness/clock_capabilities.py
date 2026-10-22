"""Narrow clock-fixture gate; optional schema fields are not inferred defaults.

Source: Seren CLI v0.10.0 (2317949), openapi-seren-cloud.json,
AgentCapabilityPolicy and its browser/code/skills/simple policy definitions.
Only authored disabled controls plus documented inactive options are accepted.
No arbitrary input keys or string values are copied to diagnostics.
"""
from __future__ import annotations

import json
from pathlib import Path

from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer

APPROVED = {
    "browser": {"enabled": False},
    "code_execution": {"enabled": False},
    "skills": {"enabled": False, "execute_scripts": False},
    "eval_harness": {"enabled": False},
    "workflows": {"enabled": False},
}
OPTIONAL_NULL = {"audio", "realtime_sessions", "tool_error_recovery"}
ENUMS = {
    "browser.profile": {"minimal", "form_filling", "scraping", "full"},
    "browser.display": {"headless", "headed"},
    "code_execution.sandbox": {"disabled", "managed", "external"},
    "skills.selection.mode": {"auto"},
}
MISSING = object()


def compare_capabilities(authored, observed):
    """Return an allowlisted projection and differences, never raw policy data.

    Missing authored false flags block, even when a service might default them.
    Extra schema-known settings are accepted only under their explicit false parent.
    Optional top-level capabilities may be absent/null, never newly configured.
    """
    differences, controls = [], []

    def entry(path, reason, accepted, actual=MISSING, expected=MISSING):
        def safe(value):
            if value is MISSING:
                return "missing"
            if value is None or type(value) is bool:
                return value
            if type(value) is int and 0 <= value <= 3600:
                return value
            if isinstance(value, str) and value in ENUMS.get(path, set()):
                return value
            if isinstance(value, (list, dict)) and not value:
                return "empty " + type(value).__name__
            return "withheld (" + type(value).__name__ + ")"
        differences.append({"field": path, "reason": reason, "accepted": accepted,
                            "expected": safe(expected), "observed": safe(actual)})

    # Freeze the approved disabled values; bool/int equality cannot grant authority.
    if (not isinstance(authored, dict) or set(authored) != set(APPROVED)
            or any(not isinstance(authored[k], dict) or set(authored[k]) != set(v)
                   or any(authored[k][flag] is not False for flag in v)
                   for k, v in APPROVED.items())):
        entry("policy", "authored controls differ from approved fixture", False)
    if not isinstance(observed, dict):
        entry("policy", "policy must be an object", False, observed)
        return {"compatible": False, "controls": controls, "differences": differences}

    for group, flags in APPROVED.items():
        value = observed.get(group, MISSING)
        if not isinstance(value, dict):
            entry(group, "required control group missing or malformed", False, value)
            continue
        for flag in flags:
            current = value.get(flag, MISSING)
            path = group + "." + flag
            if current is not False:
                entry(path, "explicit false required", False, current, False)
            controls.append({"field": path, "disabled": current is False})

        extras = {
            "browser": {"profile", "display", "max_session_seconds", "timezone"},
            "code_execution": {"sandbox"},
            "skills": {"selection", "max_injected_chars"},
        }.get(group, set())
        for key in value:
            if key in flags:
                continue
            if key not in extras:
                entry(group + ".[unknown]", "unknown field; name/value withheld", False)
                continue
            path, v = group + "." + key, value[key]
            if key == "selection":
                if not isinstance(v, dict):
                    entry(path, "selection must be an object", False, v)
                    continue
                allowed = {"mode", "include_tags", "exclude_tags", "max_selected"}
                if not v:
                    entry(path, "documented inactive selection", True, v)
                for child in v:
                    if child not in allowed:
                        entry(path + ".[unknown]", "unknown field; name/value withheld", False)
                        continue
                    cv, cp = v[child], path + "." + child
                    valid = ((child == "mode" and cv == "auto")
                             or (child in ("include_tags", "exclude_tags") and cv == [])
                             or (child == "max_selected" and type(cv) is int
                                 and 0 <= cv <= 2147483647))
                    entry(cp, "documented inactive option" if valid else "unsupported option",
                          valid, cv)
                continue
            valid = False
            if path in ENUMS:
                valid = isinstance(v, str) and v in ENUMS[path]
                if path == "browser.display" and v is None:
                    valid = True
            elif path == "browser.timezone":
                # No free-text values are needed for this fixture.
                valid = v is None
            elif path == "browser.max_session_seconds":
                valid = v is None or type(v) is int and 60 <= v <= 3600
            elif path == "skills.max_injected_chars":
                valid = v is None or type(v) is int and 0 <= v <= 2147483647
            entry(path, "documented inactive option" if valid else "unsupported option", valid, v)

    for key in observed:
        if key in APPROVED:
            continue
        if key in OPTIONAL_NULL:
            entry(key, "absent optional capability serialized as null"
                  if observed[key] is None else "additional configured capability",
                  observed[key] is None, observed[key])
        else:
            entry("policy.[unknown]", "unknown field; name/value withheld", False)
    return {"compatible": not any(not d["accepted"] for d in differences),
            "controls": controls, "differences": differences}


def require_capabilities(authored, observed, diagnostic_path, sanitizer=None):
    """Save safe evidence BEFORE deciding whether preparation may proceed."""
    result = compare_capabilities(authored, observed)
    sanitizer = sanitizer or Sanitizer()
    _write_private(Path(diagnostic_path), json.dumps(sanitizer.value(result), indent=2))
    if not result["compatible"]:
        raise ValueError("clock controls not verified; see private control diagnostics")
    return result
