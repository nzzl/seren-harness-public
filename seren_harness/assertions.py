"""Critical assertion contracts: what a pass claims, why, and the evidence it needs.

Each contract separates the customer outcome (the claim) from what its basis
actually rests on. Basis kinds are recorded as stated, never upgraded: a
simulated pass does not turn a source observation or assumption into an
established service contract.

A snapshot of the applicable contract (with version and digest) is stored with
each result, so historical reports render the expectations that were in force
when the result was recorded, not today's registry.
"""
from __future__ import annotations

import hashlib
import json

from seren_harness.models import BLOCKED, HARNESS_DEFECT, PASSED, Finding, StepResult

DOCUMENTED = "documented_behavior"
SOURCE = "source_code_observation"
REQUIREMENT = "customer_requirement"
ASSUMPTION = "unverified_assumption"
BASIS_KINDS = (DOCUMENTED, SOURCE, REQUIREMENT, ASSUMPTION)

_CONTRACTS = {
    "passwords.approval_applied": {
        "version": 1,
        "claim": "The owner-approved credential binding is applied to the intended test "
                 "employee and to the configuration (identity, revision, policy) under test.",
        "basis": [
            {"kind": REQUIREMENT, "reference": "pilot notes (not in this release): owner approval is an "
             "explicit checkpoint; implementation plan (not in this release) provisioning step 5"},
            {"kind": SOURCE, "reference": "serenorg/seren v0.10.0 cli/src/commands/agent.rs "
             "managed_agent_secrets_apply and sdk/src/shared.rs "
             "managed_agent_secrets_application, where apply reports success only after "
             "the deployment names the setup's result_id"},
            {"kind": ASSUMPTION, "reference": "the server keeps that binding after apply; no "
             "documented API contract states it"},
        ],
        "required_evidence": [
            "deployment identity read back for the configured deployment_id",
            "an approval result bound on the deployment (secret_resolution_result_id set)",
            "no pending approval condition",
            "binding consistent: equal to the applied result_id, or unchanged from the "
            "approved baseline",
        ],
        "insufficient_evidence": [
            "setup status approved or applied alone",
            "apply exit status alone",
            "a binding that differs from the applied result or approved baseline",
        ],
        "limitations": [
            "Does not show that the credential works for the connector.",
            "Whether the server reports setup status applied is unverified.",
        ],
    },
    "discord.reply_to_probe": {
        "version": 1,
        "claim": "The intended employee answered this specific probe in the test channel.",
        "basis": [
            {"kind": REQUIREMENT, "reference": "implementation plan (not in this release) operational "
             "step 5; pilot notes (not in this release) acceptance: a verified response"},
            {"kind": DOCUMENTED, "reference": "https://docs.discord.com/developers/reference "
             "(Snowflakes: unique ids, millisecond timestamp)"},
            {"kind": DOCUMENTED, "reference": "https://docs.discord.com/developers/resources/"
             "message (message reference types: DEFAULT replies, FORWARD snapshots)"},
        ],
        "required_evidence": [
            "a reply message distinct from the probe",
            "authored by the configured employee user",
            "in the configured test channel",
            "bound to the probe by a standard reply reference or the probe's unique marker",
            "observed within the reply window after the probe",
        ],
        "insufficient_evidence": [
            "healthy status, Ready=True or an accepted start",
            "a message older than the probe or outside the window",
            "a message from another author or channel, or unrelated to the probe",
            "a forward of the probe",
        ],
        "limitations": [
            "Demonstrates responsiveness, not answer quality.",
            "Live probes are posted by a human; bot-authored mentions are not established.",
        ],
    },
    "discord.reply_content": {
        "version": 1,
        "claim": "The employee's reply to this probe is exactly the expected response.",
        "basis": [
            {"kind": REQUIREMENT, "reference": "the configured expected response "
             "(config.discord.expected_reply); its source is recorded with each result"},
        ],
        "required_evidence": [
            "an expected response built from the configured template and this probe's marker",
            "the complete content of the correlated reply, compared in memory",
            "reply equal to the expected response after trimming and collapsing whitespace "
            "only (case-sensitive)",
        ],
        "insufficient_evidence": [
            "a correlated reply alone",
            "a reply that contains ACK or the marker among other text",
            "retained evidence, which may be truncated or redacted",
            "keyword or semantic judgement",
        ],
        "limitations": [
            "Checks one exact response, not answer quality in general.",
            "Separate from reply correlation and from lifecycle retest recovery.",
        ],
    },
    "runtime.shutdown_confirmed": {
        "version": 1,
        "claim": "The test runtime this run started is stopped at the end of the run.",
        "basis": [
            {"kind": REQUIREMENT, "reference": "implementation plan (not in this release) operational "
             "step 6 and stop conditions: verify shutdown; unconfirmed cleanup is visible"},
            {"kind": SOURCE, "reference": "serenorg/seren v0.10.0 bundled OpenAPI: deployment "
             "status enum includes stopped"},
        ],
        "required_evidence": [
            "a deployment read showing status stopped",
            "taken after the stop was issued, or before any stop when already stopped",
        ],
        "insufficient_evidence": [
            "a stop request that was accepted or latched",
            "desired state stopping without a stopped status",
            "absence of errors",
        ],
        "limitations": [
            "Status is read through the CLI; billing stop is not observable.",
            "Manual preparation on 2026-10-09 observed managed-get without "
            "desired_lifecycle_state; confirmation relies on the status read.",
        ],
    },
}

STEP_ASSERTIONS = {"read_back": "passwords.approval_applied",
                   "verify_fixture": "passwords.approval_applied",
                   "verify_reply": "discord.reply_to_probe",
                   "verify_reply_content": "discord.reply_content"}
SHUTDOWN = "runtime.shutdown_confirmed"
CRITICAL_STEPS = tuple(STEP_ASSERTIONS)
# Criteria judged after delivery. They count in the workflow outcome but are not part of a
# lifecycle finding's recovery chain, and runs recorded before they existed omit them.
SUPPLEMENTARY_STEPS = ("verify_reply_content",)


def _approval_unmet(e):
    identity = e.get("identity") or {}
    checks = [
        (identity.get("deployment_id") is not None
         and identity.get("deployment_id") == e.get("expected_deployment_id"), 0),
        (bool(identity.get("secret_resolution_result_id")), 1),
        (e.get("approval_pending") is False, 2),
        (e.get("binding_consistent") is True, 3),
    ]
    return checks


def _reply_unmet(e):
    window = e.get("window_ms")
    latency = e.get("latency_ms")
    return [
        (bool(e.get("reply_id")) and bool(e.get("probe_id"))
         and e.get("reply_id") != e.get("probe_id"), 0),
        (e.get("reply_author") is not None and e.get("reply_author") == e.get("expected_author"),
         1),
        (e.get("reply_channel") is not None
         and e.get("reply_channel") == e.get("expected_channel"), 2),
        (e.get("matched_by") in ("reply_reference", "marker"), 3),
        (isinstance(latency, int) and isinstance(window, int) and 0 <= latency <= window, 4),
    ]


def _content_unmet(e):
    return [(bool(e.get("expected")), 0), (e.get("compared") == "complete reply", 1),
            (e.get("content_matches") is True, 2)]


def _shutdown_unmet(e):
    return [(e.get("confirmed_status") == "stopped" and e.get("confirmed_by") == "status read", 0),
            (e.get("how") in ("stop confirmed", "already stopped; no stop issued"), 1)]


_CHECKS = {"passwords.approval_applied": _approval_unmet,
           "discord.reply_to_probe": _reply_unmet,
           "discord.reply_content": _content_unmet,
           "runtime.shutdown_confirmed": _shutdown_unmet}


def definition(assertion_id):
    contract = dict(_CONTRACTS[assertion_id], id=assertion_id)
    return snapshot(contract)


def snapshot(contract):
    """Preserve a versioned expectation and its digest, including offline cases."""
    return dict(contract, digest=_digest(contract))


def _digest(contract):
    material = {k: v for k, v in contract.items() if k != "digest"}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def unmet(assertion_id, evidence):
    """Required-evidence items not established by `evidence`."""
    required = _CONTRACTS[assertion_id]["required_evidence"]
    return [required[index] for ok, index in _CHECKS[assertion_id](evidence or {}) if not ok]


def record(assertion_id, evidence):
    """The provenance stored with a result: definition snapshot plus requirement status."""
    missing = unmet(assertion_id, evidence)
    required = _CONTRACTS[assertion_id]["required_evidence"]
    return {"snapshot": definition(assertion_id), "unmet": missing,
            "observed": [{"requirement": item, "met": item not in missing}
                         for item in required]}


def enforce(step_name, result):
    """Attach provenance to a critical step; a pass without required evidence is blocked."""
    assertion_id = STEP_ASSERTIONS.get(step_name)
    if assertion_id is None:
        return result
    provenance = record(assertion_id, result.evidence)
    evidence = dict(result.evidence, assertion=provenance)
    if result.outcome != PASSED or not provenance["unmet"]:
        return StepResult(result.outcome, result.summary, evidence, result.finding)
    return StepResult(BLOCKED, "pass not established: missing required evidence for "
                      + assertion_id + " (" + "; ".join(provenance["unmet"]) + ")", evidence,
                      Finding("critical_pass_has_evidence", HARNESS_DEFECT,
                              "Critical check claimed a pass without its required evidence",
                              "all required evidence for " + assertion_id,
                              "missing: " + "; ".join(provenance["unmet"]),
                              context={"assertion": assertion_id}))


def stale(step_name, evidence):
    """Whether a recorded critical pass was made under another (or no) contract version."""
    assertion_id = STEP_ASSERTIONS.get(step_name)
    if assertion_id is None:
        return False
    snapshot = ((evidence or {}).get("assertion") or {}).get("snapshot") or {}
    current = definition(assertion_id)
    return (snapshot.get("id"), snapshot.get("version"), snapshot.get("digest")) != \
        (current["id"], current["version"], current["digest"])
