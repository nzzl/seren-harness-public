"""The Seren CLI contract this harness relies on, with its evidence and gaps.

Established from the public Seren CLI source at tag v0.10.0 (commit 2317949)
and its bundled OpenAPI specifications. Nothing here was verified against the
running service. A CLI version outside SUPPORTED_CLI_VERSIONS is reported as an
unsupported contract instead of being driven on assumptions.
"""
from __future__ import annotations

import hashlib
import json
import re

SUPPORTED_CLI_VERSIONS = ("0.10.0",)
SOURCE_REFERENCE = "serenorg/seren v0.10.0 (2317949), cli/src/commands/agent.rs"

# Typed enums in the v0.10.0 specification.
DEPLOYMENT_STATUSES = ("pending", "building", "running", "stopped", "failed")
LIFECYCLE_STATES = ("running", "stopping", "stopped")
HEALTH_STATUSES = ("ok", "attention", "critical")
SETUP_WAITING = ("unconsumed", "consumed", "pending", "partially_approved")
SETUP_APPROVED = "approved"
SETUP_APPLIED = "applied"
SETUP_TERMINAL = ("rejected", "cancelled", "superseded", "expired", "declined", "conflicted")

# Fields that identify what an approval and a passing result were bound to.
IDENTITY_FIELDS = ("deployment_id", "active_revision_id", "agent_identity_id",
                   "secret_resolution_result_id", "policy_digest")
POLICY_FIELDS = ("capability_policy", "runtime_policy", "side_effect_policy", "requirements",
                 "approval_policy")

# Gaps that limit what any result may claim.
GAPS = {
    "server_build": "No response exposes a server build identifier; results cannot be "
                    "attributed to a deployed server commit.",
    "activity_vocabulary": "Activity run `source`, `status` and `stop_reason` are free strings "
                           "with no documented values; failure detection treats any stop "
                           "reason or failure-like status after start as failure evidence.",
    "reply_observation": "No Seren API exposes Discord message authors or reply references; "
                         "replies are observed through a separate Discord channel reader.",
    "automated_sender": "Whether the managed Discord connector answers bot- or webhook-authored "
                        "mentions is not established; no automated sender is available.",
    "lifecycle_read": "v0.10.0 ManagedAgentDeploymentDetail omits desired_lifecycle_state "
                      "and control_generation. Read them through agent cloud deployment "
                      "status (CloudDeploymentSummary); observed status alone does not "
                      "establish operator intent. A successful stop still requires a "
                      "subsequent observed stopped status to confirm shutdown.",
    "start_idempotency": "managed-start and managed-stop accept no idempotency key; an "
                         "unanswered call is reconciled by reading deployment state.",
    "setup_listing": "No command lists Passwords setups for a deployment; a setup request with "
                     "an unknown outcome cannot be reconciled automatically.",
    "approval_binding": "Established from CLI source, not a documented API contract: "
                        "v0.10.0 managed-passwords-apply reports success only after reading "
                        "the deployment's secret_resolution_result_id equal to the printed "
                        "result_id (sdk/src/shared.rs managed_agent_secrets_application). "
                        "Whether the server then keeps that binding is unverified.",
    "setup_applied_status": "The setup status enum includes `applied`, but whether the server "
                            "reports it after the CLI binds the deployment is unverified; an "
                            "`approved` setup is not evidence that apply did not happen.",
}

# Commands whose repetition is safe by the CLI's own check, so an unknown outcome may be
# settled by issuing them again: apply returns `already_applied` when the deployment
# already names the setup's result (cli/src/commands/agent.rs managed_agent_secrets_apply).
REISSUABLE = ("managed_passwords_apply",)

_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")
# `anyhow_from_seren_error`: "<context>: unexpected response 404 Not Found - <body>";
# payment errors: "Payment Required (402): ...".
_HTTP_STATUS = re.compile(r"unexpected response (\d{3})|Payment Required \((\d{3})\)")


def parse_version(text):
    match = _VERSION.search(text or "")
    return match.group(1) if match else None


def unwrap(data):
    """`{"data": ...}` envelope used by managed-get/health/activity/start/stop."""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return None


def policy_digest(detail):
    material = {key: detail.get(key) for key in POLICY_FIELDS}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def identity(detail):
    """Identity of a `ManagedAgentDeploymentDetail`, or None for the opaque owner view."""
    if not isinstance(detail, dict) or "deployment_id" not in detail \
            or "agent_identity_id" not in detail:
        return None
    result = {key: detail.get(key) for key in IDENTITY_FIELDS[:-1]}
    result["policy_digest"] = policy_digest(detail)
    return result


def identity_changes(recorded, current):
    return [key for key in IDENTITY_FIELDS if (recorded or {}).get(key) != (current or {}).get(key)]


def condition(detail, kind):
    for item in detail.get("conditions") or []:
        if item.get("type") == kind:
            return item.get("status")
    return None


def http_status(stderr):
    match = _HTTP_STATUS.search(stderr or "")
    return int(match.group(1) or match.group(2)) if match else None


def definitely_not_applied(stderr):
    """Whether a failed mutation provably changed nothing.

    The v0.10.0 CLI itself treats 5xx responses, communication errors and invalid
    response bodies as ambiguous (`managed_mutation_outcome_is_ambiguous`). Only a
    4xx answer or the client-side session check, which runs before any request,
    shows the server did not apply the request. Everything else is unknown.
    """
    text = stderr or ""
    code = http_status(text)
    return "interactive OAuth user session" in text or (code is not None and 400 <= code < 500)


def classify_cli_error(stderr):
    """Map CLI stderr text (errors are never JSON) to an access, payment or unknown cause."""
    text = stderr or ""
    if "interactive OAuth user session" in text:
        return "oauth_session_required"
    code = http_status(text)
    if code in (401, 403):
        return "access_denied"
    if code == 404 or "not found" in text.lower():
        return "not_found"
    if code == 402 or "deficit" in text.lower():
        return "payment_required"
    if "client_contract_mismatch" in text:
        return "contract_mismatch"
    return "unclassified_cli_error"


ACCESS_CAUSES = ("oauth_session_required", "access_denied", "not_found", "payment_required")
