"""Classify a failed Seren CLI invocation into a fixed category, in memory only.

Every rule is a concrete signature from the pinned source. Seren v0.10.0 (2317949) `main`
returns `anyhow::Result`, so a failure prints `Error: <message>`, followed by any
`Caused by:` chain, and exits 1:
- `cli/src/config.rs` `Config::load`: "Not authenticated. Run 'seren auth login' first."
  when the config path does not exist. `Path::exists` is also false when the path cannot be
  accessed, so this means not found or not accessible. "Could not read config file" and
  "Could not parse config file" also come from `load`.
- `config_root`/`profile_dir`: "Could not create config directory" / "Could not create
  profile directory"; the following chmod uses a bare `?`, so a refused chmod prints only
  the OS error.
- `cli/src/commands/auth.rs` `get_bearer_token`: "No valid authentication token found";
  `request_token_refresh`: "Token refresh failed: <progenitor error>". The token operation
  (`POST /oauth2/token`, openapi.json) documents 400 and 401 without content; a locally
  generated client of the same generator maps them, and every other non-200 status, to
  progenitor `UnexpectedResponse`, displayed as reqwest's `Response { url, status, headers }`
  Debug. An HTTP refresh failure is therefore `unknown` at stage `token_refresh`: neither
  the status nor a credential rejection is read from that text.
- `cli/src/commands/agent.rs` `managed_agent_get` / `anyhow_from_seren_error`:
  "Failed to get managed agent detail: unexpected response <code> <reason> - <body>",
  "…: client_contract_mismatch…", or "…: <progenitor error>".
- progenitor-client 0.14.0 `Error` Display: "Communication Error: …", "Invalid Response
  Payload …", "Invalid Response Body Bytes: …", "Unexpected Response: …". reqwest 0.13.4
  request errors display "error sending request…". Sources are not appended (non-alternate
  Display), so OS errors inside a request failure never appear in this text.
- Rust std `io::Error` Display: "Permission denied (os error 13)", "Operation not permitted
  (os error 1)".
- clap usage errors print "error: …" and exit 2 (observed in the version-3 attempt).

Only a category, a fixed rule identifier, a stage, an HTTP status parsed from the CLI's own
format, and source-backed booleans are returned. The text is never returned or stored.
Anything unmatched, or matching more than one category, is `unknown`. Speed, a bare exit
code or a missing response never decide a category.
"""
from __future__ import annotations

import re

from seren_harness.seren_cli import DECODE_ERROR, ERROR, OK, TIMEOUT, UNAVAILABLE

CATEGORIES = ("executable_or_argument", "session_unavailable", "local_permission_denied",
              "authentication_rejected", "service_access_denied", "service_client_error",
              "server_error", "network_failure", "timeout", "malformed_response", "unknown")

# Anywhere in the text: alongside a request or refresh signature it makes the result unknown.
_OS_PERMISSION = re.compile(r"Permission denied \(os error 13\)|Operation not permitted "
                            r"\(os error 1\)")
_HTTP = re.compile(r"^Error: Failed to get managed agent detail: unexpected response "
                   r"(\d{3})\b")
_SESSION_STEPS = (
    ("session_unavailable", "config_missing_or_inaccessible",
     re.compile(r"^Error: Not authenticated\. Run 'seren auth login' first\.")),
    ("session_unavailable", "no_token_in_session",
     re.compile(r"^Error: No valid authentication token found")),
    ("session_unavailable", "session_file_unreadable",
     re.compile(r"^Error: Could not read config file")),
    ("session_unavailable", "session_file_invalid",
     re.compile(r"^Error: Could not parse config file")),
    ("session_unavailable", "session_file_unwritable",
     re.compile(r"^Error: Could not write config file")),
    ("session_unavailable", "config_directory_unavailable",
     re.compile(r"^Error: Could not (?:create config directory|create profile directory|"
                r"determine config directory)")),
)
_REFRESH = re.compile(r"^Error: Token refresh failed: (Communication Error|Error Response|"
                      r"Unexpected Response|Invalid Response Payload|Invalid Response Body "
                      r"Bytes)")
_REQUEST = re.compile(r"^Error: Failed to get managed agent detail: (Communication Error: error "
                      r"sending request|client_contract_mismatch|Invalid Response Payload|"
                      r"Invalid Response Body Bytes)")
_CLAP = re.compile(r"^error: ")
_ANYHOW_PREFIX = re.compile(r"^Error: ")


def _result(category, rule, stage=None, http_status=None, before_request=None,
            http_response=None):
    return {"category": category, "rule": rule, "stage": stage, "http_status": http_status,
            # True only where the source shows the error precedes the command's own service
            # request (a token-refresh request may still have been made); False only when an
            # HTTP response to that request was reported. Otherwise None (not established).
            "before_main_request": before_request, "http_response_received": http_response}


def classify(result):
    """A fixed classification of one CliResult. Reads `stderr_tail` in memory only."""
    status, code = result.status, result.exit_code
    if status == OK:
        return None
    if status == TIMEOUT:
        return _result("timeout", "harness_timeout")
    if status == UNAVAILABLE:
        return _result("executable_or_argument", "executable_not_started", "process", None,
                       True)
    if status == DECODE_ERROR:
        return _result("malformed_response", "stdout_not_json", "output")
    text = (result.stderr_tail or "").lstrip()
    if status != ERROR:
        return _result("unknown", "unrecognized_status")
    if code == 2 and _CLAP.match(text):
        return _result("executable_or_argument", "cli_usage_error", "argument_parsing", None,
                       True)
    if code != 1 or not _ANYHOW_PREFIX.match(text):
        return _result("unknown", "unrecognized_exit")
    matches = []
    permission = bool(_OS_PERMISSION.search(text))
    for category, rule, pattern in _SESSION_STEPS:
        if pattern.match(text):
            matches.append(_result("local_permission_denied" if permission and rule in (
                "session_file_unreadable", "session_file_unwritable",
                "config_directory_unavailable") else category,
                rule, "local_session", None, True))
    http = _HTTP.match(text)
    if http:
        code_value = int(http.group(1))
        category = ("authentication_rejected" if code_value == 401 else
                    "service_access_denied" if code_value == 403 else
                    "server_error" if code_value >= 500 else
                    "service_client_error" if 400 <= code_value < 500 else "unknown")
        matches.append(_result(category, "http_status", "request", code_value, False, True))
    refresh = _REFRESH.match(text)
    if refresh:
        kind = refresh.group(1)
        matches.append(_result("network_failure", "token_refresh_communication", "token_refresh",
                               None, True) if kind == "Communication Error" else
                       # An HTTP answer from the token endpoint: the stage is established, the
                       # cause is not. The pinned SDK maps every non-200 token status (including
                       # the documented 400/401) to Unexpected Response, whose text puts the
                       # status after a URL and before server-controlled headers, so no status
                       # or credential rejection is inferred.
                       _result("unknown", "token_refresh_http_error", "token_refresh", None,
                               True)
                       if kind in ("Error Response", "Unexpected Response") else
                       _result("malformed_response", "token_refresh_payload", "token_refresh",
                               None, True))
    request = _REQUEST.match(text)
    if request:
        kind = request.group(1)
        matches.append(_result("network_failure", "request_communication", "request")
                       if kind.startswith("Communication") else
                       _result("malformed_response", "request_payload", "request", None, False,
                               True))
    if not matches and permission and re.match(
            r"^Error: (?:Permission denied \(os error 13\)|Operation not permitted "
            r"\(os error 1\))\s*$", text):
        # A bare OS error: only the config directory chmod propagates one without context.
        matches.append(_result("local_permission_denied", "bare_os_permission_error",
                               "local_session", None, True))
    if len(matches) != 1 or (permission and matches[0]["stage"] not in ("local_session",)):
        return _result("unknown", "conflicting_signatures" if matches else "unrecognized_error")
    return matches[0]
