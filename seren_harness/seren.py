"""Provisioning and operational journeys for one Seren test employee.

Every Seren interaction goes through `SerenCli` using commands established in
`seren_harness.contract`. Discord input and observation use the configured
sender/observer; unavailable integrations block the dependent steps.
"""
from __future__ import annotations

import hashlib
import time

from seren_harness import assertions, contract, discord
from seren_harness.journey import Step
from seren_harness.models import (AWAITING_HUMAN, DEFECT, ENVIRONMENT, HARNESS_DEFECT, SIMULATION, UNCERTAIN, Finding,
                                  StepResult, blocked, failed, passed, utc_now)
from seren_harness.seren_cli import DECODE_ERROR, ERROR, TIMEOUT, UNAVAILABLE
from seren_harness.state import (AMBIGUOUS, APPLIED, RECONCILED_APPLIED,
                                 RECONCILED_NOT_APPLIED, REJECTED, SUCCEEDED, UNRESOLVED)

FAILURE_LIKE_STATUSES = ("failed", "error", "errored", "blocked", "cancelled", "rejected",
                         "timed_out", "timeout")
# The probe asks for the response the live fixture's instructions require: ACK followed by
# the marker unchanged. The judged expectation comes from config.discord.expected_reply.
PROBE_TASK = "Reply to this message with ACK followed by this marker, unchanged:"
RETAINED_CONTENT_CHARS = 500


class Context:
    def __init__(self, config, store, run_id, cli, observer=None, output=print,
                 sleep=time.sleep, clock=time.monotonic):
        self.config = config
        self.store = store
        self.run_id = run_id
        self.cli = cli
        self.observer = observer
        self.observer_problem = None
        self.output = output
        self.sleep = sleep
        self.clock = clock
        self.resume_changes = None
        # The correlated reply message, held in memory only, never stored as received.
        self.reply = None

    @property
    def deployment(self):
        return self.config.deployment_id

    def evidence(self, step):
        for row in self.store.steps(self.run_id):
            if row["name"] == step:
                return row["evidence"]
        return {}

    def commands_since(self, mark):
        return self.cli.history[mark:]


# Shared helpers -----------------------------------------------------------
def _cli_problem(result, step, purpose):
    """Turn a non-ok CLI result into a blocked or failed step with a classified finding."""
    evidence = {"commands": [result.argv], "cli_status": result.status}
    if result.status == UNAVAILABLE:
        return blocked("Seren CLI could not be executed for " + purpose, Finding(
            "cli_available", ENVIRONMENT, "Configured Seren CLI is not executable",
            "the configured executable runs", "it could not be started",
            context={"step": step}), **evidence)
    if result.status == TIMEOUT:
        return blocked("no answer from Seren CLI within " + str(result.detail.get(
            "timeout_seconds")) + "s for " + purpose, Finding(
            purpose + "_answers", UNCERTAIN, "Seren CLI did not answer " + purpose,
            "a response within the command timeout", "no response",
            context={"cause": "timeout"}), **evidence)
    if result.status == DECODE_ERROR:
        return blocked("Seren CLI output for " + purpose + " did not match the supported "
                       "contract", Finding(
                           purpose + "_output_contract", UNCERTAIN,
                           "Seren CLI output for " + purpose + " does not match the v0.10.0 "
                           "contract", "JSON output as documented in " + contract.SOURCE_REFERENCE,
                           "undecodable output", context={"cause": "decode_error"},
                           unknowns=["Whether the CLI or the service changed its output."]),
                       unsupported="output contract mismatch for " + purpose, **evidence)
    cause = contract.classify_cli_error(result.stderr_tail)
    evidence.update(exit_code=result.exit_code, stderr=result.stderr_tail, cause=cause)
    if cause in contract.ACCESS_CAUSES:
        return blocked("prerequisite missing for " + purpose + ": " + cause, Finding(
            purpose + "_access", ENVIRONMENT, "Access prerequisite missing: " + cause,
            "the authorized test account can " + purpose, "the CLI reported " + cause,
            context={"cause": cause}), **evidence)
    return failed("Seren CLI reported an error for " + purpose, Finding(
        purpose + "_succeeds", UNCERTAIN, "Seren CLI error during " + purpose,
        "the command succeeds", "exit " + str(result.exit_code) + " (" + cause + ")",
        context={"cause": cause}), **evidence)


def _read_detail(ctx, step):
    result = ctx.cli.json("agent", "managed-get", ctx.deployment)
    if not result.ok:
        return None, _cli_problem(result, step, "read_deployment")
    detail = contract.unwrap(result.data)
    if not isinstance(detail, dict) or not _records(detail.get("conditions") or []):
        return None, _cli_problem(_as_decode_error(result), step, "read_deployment")
    return detail, None


def _read_lifecycle(ctx, step):
    """Read operator intent from its supported endpoint, never infer it from status.

    v0.10.0 ManagedAgentDeploymentDetail lacks lifecycle intent/control generation.
    CloudDeploymentSummary supplies them. Preserve complete detail responses from
    existing fixtures, but validate every field used for lifecycle decisions.
    """
    detail, problem = _read_detail(ctx, step)
    if problem:
        return None, problem
    if "desired_lifecycle_state" in detail and "control_generation" in detail:
        state = detail
        target = state.get("deployment_id")
        result = None
    else:
        result = ctx.cli.json("agent", "cloud", "deployment", "status", ctx.deployment)
        if not result.ok:
            return None, _cli_problem(result, step, "read_lifecycle")
        state = contract.unwrap(result.data)
        target = state.get("id") if isinstance(state, dict) else None
    valid = (isinstance(state, dict) and target == ctx.deployment
             and state.get("status") in contract.DEPLOYMENT_STATUSES
             and state.get("desired_lifecycle_state") in contract.LIFECYCLE_STATES
             and type(state.get("control_generation")) is int
             and state["control_generation"] >= 0)
    if not valid:
        if result is not None:
            return None, _cli_problem(_as_decode_error(result), step, "read_lifecycle")
        return None, blocked("lifecycle identity, status or control fields are invalid; "
                             "no lifecycle state inferred", unsupported="lifecycle evidence")
    return state, None


def _as_decode_error(result):
    result.status = DECODE_ERROR
    return result


def _mapping(value):
    return value if isinstance(value, dict) else None


def _records(value):
    """A list of JSON objects, or None for any other shape."""
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    return None


def _activity_runs(result):
    return _records((_mapping(contract.unwrap(result.data)) or {}).get("runs"))


def _mutate(ctx, step, kind, args, reconcile):
    """Journal intent, run the mutation, and reconcile any unknown outcome by reading state.

    Returns (status, CliResult). Status is one of the journal states; an unknown
    outcome is never retried here. A failed exit counts as rejected only when it
    shows that nothing was applied (see `contract.definitely_not_applied`).
    """
    action = ctx.store.record_intent(ctx.run_id, step, kind, ctx.deployment,
                                     {"argv": ctx.cli.display_argv(args)})
    result = ctx.cli.json(*args)
    if result.ok:
        ctx.store.resolve_action(action, SUCCEEDED, {"response": result.data})
        return SUCCEEDED, result
    if result.status == ERROR and contract.definitely_not_applied(result.stderr_tail):
        ctx.store.resolve_action(action, REJECTED, {"stderr": result.stderr_tail})
        return REJECTED, result
    ctx.store.resolve_action(action, AMBIGUOUS, {"cli_status": result.status,
                                                 "stderr": result.stderr_tail})
    return reconcile_action(ctx, action, reconcile), result


def _earlier_attempt(ctx, kind):
    """Status of the latest earlier attempt of `kind` in this run, or None."""
    attempts = [a for a in ctx.store.actions(ctx.run_id) if a["kind"] == kind]
    return attempts[-1]["status"] if attempts else None


def _unknown_earlier_attempt(step, kind):
    return blocked("an earlier " + kind + " in this run has an unknown outcome; it is not "
                   "issued again. Verify the resource, then record the answer with "
                   "`python -m seren_harness reconcile`", Finding(
                       step + "_outcome_known", UNCERTAIN,
                       "Earlier " + kind + " outcome is unknown",
                       "a known outcome before any new attempt", "unresolved journal entry",
                       context={"kind": kind}), journal=AMBIGUOUS)


def reconcile_action(ctx, action_id, reconcile):
    verdict = reconcile(ctx)
    status = {True: RECONCILED_APPLIED, False: RECONCILED_NOT_APPLIED}.get(verdict, AMBIGUOUS)
    previous = next(a["detail"] for a in ctx.store.actions(ctx.run_id) if a["id"] == action_id)
    ctx.store.resolve_action(action_id, status, dict(previous, reconciled_by="state read-back"))
    ctx.store.event(ctx.run_id, "action_reconciled", {"action": action_id, "status": status})
    return status


def _identity_guard(ctx, step):
    if ctx.resume_changes:
        return blocked("deployment identity changed since this run recorded it: "
                       + ", ".join(ctx.resume_changes) + "; earlier evidence was invalidated",
                       Finding("identity_stable_during_run", ENVIRONMENT,
                               "Test employee changed while a run was in progress",
                               "identity, policy and revision unchanged across resume",
                               "changed: " + ", ".join(ctx.resume_changes),
                               context={"changed": ",".join(ctx.resume_changes)}),
                       changed=ctx.resume_changes)
    return None


# Shared first step --------------------------------------------------------
def preflight(ctx):
    result = ctx.cli.version()
    if not result.ok:
        return _cli_problem(result, "preflight", "report_version")
    version = contract.parse_version(result.data)
    simulated = "simulation" in (result.data or "").lower()
    ctx.store.update_run(ctx.run_id, cli_version=result.data, server_build="unknown")
    evidence = {"commands": [result.argv], "cli_version_text": result.data,
                "supported_versions": list(contract.SUPPORTED_CLI_VERSIONS),
                "gaps": contract.GAPS}
    if simulated != (ctx.config.environment == SIMULATION):
        return blocked("environment is " + ctx.config.environment + " but the executable "
                       + ("is" if simulated else "is not") + " the simulator; refusing to mix "
                       "simulated and live evidence", Finding(
                           "evidence_mode_consistent", HARNESS_DEFECT,
                           "Simulated and live evidence mixed", "matching modes",
                           "configuration mismatch", context={"simulated": simulated}),
                       **evidence)
    if version not in contract.SUPPORTED_CLI_VERSIONS:
        return blocked("Seren CLI version " + str(version) + " is not a supported contract",
                       unsupported="Seren CLI " + str(version) + " has no established "
                       "contract; supported: " + ", ".join(contract.SUPPORTED_CLI_VERSIONS),
                       **evidence)
    expected = ctx.config.executable_sha256
    actual = _sha256(ctx.config.executable)
    evidence.update(executable_sha256=actual, expected_sha256=expected)
    if expected is None and ctx.config.environment != SIMULATION:
        return blocked("config.seren.executable_sha256 is required for live runs; record the "
                       "checksum of the verified release binary", **evidence)
    if expected is not None and actual != expected:
        return blocked("Seren CLI executable checksum differs from the recorded release "
                       "checksum", Finding("cli_checksum_matches", ENVIRONMENT,
                                           "Seren CLI executable differs from the recorded one",
                                           "the recorded SHA-256", "a different SHA-256",
                                           context={}), **evidence)
    return passed("Seren CLI " + version + " matches the established contract; server build "
                  "unknown", **evidence)


def _sha256(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def inspect_target(ctx):
    """Read-only: the configured deployment is visible and its state is recorded."""
    mark = len(ctx.cli.history)
    detail, problem = _read_detail(ctx, "inspect_target")
    if problem:
        return problem
    current = contract.identity(detail)
    evidence = {"commands": ctx.commands_since(mark), "identity": current,
                "status": detail.get("status"),
                "desired_lifecycle_state": detail.get("desired_lifecycle_state"),
                "awaiting_approval": contract.condition(detail, "AwaitingApproval"),
                "ready": contract.condition(detail, "Ready")}
    if current is None:
        return blocked("deployment detail is opaque to this caller", **evidence)
    return passed("test deployment visible (status " + str(detail.get("status"))
                  + "); nothing was changed", **evidence)


# Operational journey -------------------------------------------------------
def verify_fixture(ctx):
    guard = _identity_guard(ctx, "verify_fixture")
    if guard:
        return guard
    baseline = ctx.store.fixture(ctx.config.employee_key)
    if baseline is None:
        return blocked("no provisioned fixture is recorded for this employee; run provision "
                       "first", Finding("approval_recorded", ENVIRONMENT,
                                        "Test employee has no recorded approved fixture",
                                        "a provisioned, approved fixture",
                                        "no fixture baseline", context={}))
    mark = len(ctx.cli.history)
    detail, problem = _read_detail(ctx, "verify_fixture")
    if problem:
        return problem
    current = contract.identity(detail)
    evidence = {"commands": ctx.commands_since(mark), "identity": current}
    if current is None:
        return blocked("deployment detail is opaque to this caller", Finding(
            "deployment_detail_visible", ENVIRONMENT, "Deployment detail is not visible",
            "ManagedAgentDeploymentDetail with identity fields", "owner view only",
            context={}), **evidence)
    ctx.store.update_run(ctx.run_id, fixture_json=current)
    changes = contract.identity_changes(baseline["identity"], current)
    awaiting = contract.condition(detail, "AwaitingApproval") == "True"
    evidence.update(expected_deployment_id=ctx.deployment, approval_pending=awaiting,
                    binding_consistent=not changes)
    if not current.get("secret_resolution_result_id") or awaiting:
        return blocked("credential approval is missing or pending; dependent steps cannot run",
                       Finding("approval_applied", ENVIRONMENT, "Credential approval not applied",
                               "an applied owner approval", "approval missing or pending",
                               context={"awaiting_condition": awaiting}), **evidence)
    if changes:
        return blocked("fixture differs from its provisioned baseline: " + ", ".join(changes)
                       + "; re-provision before testing", Finding(
                           "fixture_matches_baseline", ENVIRONMENT,
                           "Test employee no longer matches its approved baseline",
                           "unchanged identity, policy and revision",
                           "changed: " + ", ".join(changes),
                           context={"changed": ",".join(changes)}), changed=changes, **evidence)
    return passed("identity, approval, policy and revision match the provisioned baseline",
                  **evidence)


def initial_state(ctx):
    mark = len(ctx.cli.history)
    detail, problem = _read_lifecycle(ctx, "initial_state")
    if problem:
        return problem
    activity = ctx.cli.json("agent", "managed-deployment-activity", ctx.deployment,
                            "--limit", "50")
    runs = _activity_runs(activity) if activity.ok else None
    if runs is None:
        return _cli_problem(activity if not activity.ok else _as_decode_error(activity),
                            "initial_state", "read_activity")
    evidence = {"commands": ctx.commands_since(mark), "status": detail.get("status"),
                "desired_lifecycle_state": detail.get("desired_lifecycle_state"),
                "control_generation": detail.get("control_generation"),
                "known_run_ids": [r.get("run_id") for r in runs]}
    if detail.get("status") != "stopped" or detail.get("desired_lifecycle_state") != "stopped":
        return blocked("test runtime is not stopped (" + str(detail.get("status")) + "); the "
                       "harness will not take over a runtime it did not start", **evidence)
    return passed("test runtime is stopped", **evidence)


def _reconcile_start(ctx):
    detail, problem = _read_lifecycle(ctx, "start_runtime")
    if problem:
        return None
    before = ctx.evidence("initial_state").get("control_generation")
    generation = detail.get("control_generation")
    if detail.get("desired_lifecycle_state") == "running" and isinstance(generation, int) \
            and isinstance(before, int) and generation > before:
        return True
    if detail.get("desired_lifecycle_state") == "stopped" and generation == before:
        return False
    return None


def start_runtime(ctx):
    earlier = _earlier_attempt(ctx, "managed_start")
    if earlier in UNRESOLVED:
        return _unknown_earlier_attempt("start_runtime", "managed_start")
    if earlier in APPLIED and ctx.store.open_cleanup(run_id=ctx.run_id):
        return passed("start already applied earlier in this run (journal " + earlier
                      + "); not issued again", journal=earlier)
    mark = len(ctx.cli.history)
    # The obligation exists before the call: a start that times out may still apply.
    ctx.store.add_cleanup(ctx.run_id, ctx.config.employee_key, "stop_runtime", ctx.deployment)
    status, result = _mutate(ctx, "start_runtime", "managed_start",
                             ["agent", "managed-start", ctx.deployment], _reconcile_start)
    evidence = {"commands": ctx.commands_since(mark), "journal": status,
                "requested_at": utc_now()}
    if status == SUCCEEDED:
        response = contract.unwrap(result.data) or {}
        evidence.update(response_status=response.get("status"),
                        desired_lifecycle_state=response.get("desired_lifecycle_state"))
        return passed("start accepted (" + str(response.get("status")) + "); acceptance is "
                      "not readiness", **evidence)
    if status == RECONCILED_APPLIED:
        return passed("start outcome was unknown; state read-back shows it applied",
                      **evidence)
    if status == REJECTED:
        return _cli_problem(result, "start_runtime", "start_runtime")
    if status == RECONCILED_NOT_APPLIED:
        return failed("start was not acknowledged and state shows it did not apply; not "
                      "retried automatically", Finding(
                          "start_acknowledged", UNCERTAIN, "Start request not acknowledged",
                          "an acknowledged start", "no answer and no state change",
                          context={"cli_status": result.status}), **evidence)
    return blocked("start outcome is unknown and could not be reconciled; refusing to retry",
                   Finding("start_outcome_known", UNCERTAIN, "Start outcome cannot be determined",
                           "a reconcilable start outcome", "unknown after read-back",
                           context={"cli_status": result.status}), **evidence)


def _post_start_failures(ctx, runs):
    known = set(ctx.evidence("initial_state").get("known_run_ids", []))
    failures = []
    for run in runs:
        if run.get("run_id") in known:
            continue
        status = str(run.get("status", "")).lower()
        if run.get("stop_reason") or status in FAILURE_LIKE_STATUSES:
            failures.append({"run_id": run.get("run_id"), "source": run.get("source"),
                             "status": run.get("status"), "stop_reason": run.get("stop_reason"),
                             "status_message": run.get("status_message")})
    return failures


def _observe_runtime(ctx):
    detail, problem = _read_detail(ctx, "runtime_ready")
    if problem:
        return None, problem
    health = ctx.cli.json("agent", "managed-deployment-health", ctx.deployment)
    report = _mapping(contract.unwrap(health.data)) if health.ok else None
    findings = _records(report.get("findings", [])) if report is not None else None
    if findings is None:
        return None, _cli_problem(health if not health.ok else _as_decode_error(health),
                                  "runtime_ready", "read_health")
    activity = ctx.cli.json("agent", "managed-deployment-activity", ctx.deployment,
                            "--limit", "50")
    runs = _activity_runs(activity) if activity.ok else None
    if runs is None:
        return None, _cli_problem(activity if not activity.ok else _as_decode_error(activity),
                                  "runtime_ready", "read_activity")
    return {"status": detail.get("status"), "ready": contract.condition(detail, "Ready"),
            "health": report.get("status"),
            "health_findings": [{"severity": f.get("severity"), "title": f.get("title"),
                                 "detail": f.get("detail")} for f in findings],
            "failures": _post_start_failures(ctx, runs)}, None


def _is_ready(observation):
    """The readiness assertion shared by the journey and the controlled condition."""
    return observation["status"] == "running" and observation["ready"] == "True" \
        and not observation["failures"]


def stopped_rejected(ctx):
    """Named controlled condition: the readiness assertion must reject a stopped employee.

    Read-only. It shows the assertion can fail on the live service; it says nothing
    about whether a started employee works.
    """
    mark = len(ctx.cli.history)
    observation, problem = _observe_runtime(ctx)
    if problem:
        return problem
    evidence = {"commands": ctx.commands_since(mark), "observation": observation,
                "condition": "stopped dedicated test employee"}
    if _is_ready(observation):
        return failed("the readiness assertion accepted an employee recorded as stopped",
                      Finding("readiness_rejects_stopped_employee", UNCERTAIN,
                              "Readiness assertion accepted a stopped employee",
                              "a stopped employee is not ready",
                              "status " + str(observation["status"]) + ", Ready "
                              + str(observation["ready"]), context={}), **evidence)
    return passed("readiness assertion rejected the stopped employee (status "
                  + str(observation["status"]) + ", Ready " + str(observation["ready"])
                  + ", health " + str(observation["health"]) + ")", **evidence)


def runtime_ready(ctx):
    """Poll actual readiness. A healthy flag or accepted start never substitutes for it."""
    mark = len(ctx.cli.history)
    deadline = ctx.clock() + ctx.config.readiness_timeout
    polls = 0
    while True:
        observation, problem = _observe_runtime(ctx)
        if problem:
            return problem
        polls += 1
        evidence = {"commands": ctx.commands_since(mark)[:3], "polls": polls,
                    "observation": observation,
                    "note": "health status is recorded but never treated as readiness"}
        if observation["failures"]:
            first = observation["failures"][0]
            return failed("runtime reported failed runs after start while health was "
                          + str(observation["health"]), Finding(
                              "runtime_ready_without_failed_runs", DEFECT,
                              "Managed runtime fails after start ("
                              + str(first["stop_reason"] or first["status"]) + ")",
                              "the started employee reaches Ready with no failed runs",
                              str(len(observation["failures"])) + " failed run(s); deployment "
                              + str(observation["status"]) + ", health "
                              + str(observation["health"]),
                              # Deployment status depends on poll timing, so it stays in the
                              # evidence, not in the finding's identity.
                              context={"source": first["source"], "status": first["status"],
                                       "stop_reason": first["stop_reason"]},
                              impact="The employee cannot respond in its channel.",
                              unknowns=[contract.GAPS["activity_vocabulary"],
                                        "The underlying cause when status messages are "
                                        "redacted."]), **evidence)
        if observation["status"] == "failed":
            return failed("deployment status is failed", Finding(
                "runtime_not_failed", DEFECT, "Managed deployment fails after start",
                "running and Ready", "status failed",
                context={"deployment_status": "failed"}), **evidence)
        if _is_ready(observation):
            return passed("deployment running with Ready=True after " + str(polls)
                          + " poll(s); response still unverified", **evidence)
        if ctx.clock() >= deadline:
            return failed("not ready within " + str(ctx.config.readiness_timeout) + "s "
                          "(status " + str(observation["status"]) + ", health "
                          + str(observation["health"]) + ")", Finding(
                              "runtime_ready_in_time", DEFECT,
                              "Managed runtime does not become ready",
                              "running and Ready within the limit",
                              "status " + str(observation["status"]) + ", Ready "
                              + str(observation["ready"]),
                              context={"deployment_status": observation["status"],
                                       "ready": observation["ready"]},
                              impact="The employee cannot respond in its channel."),
                          **evidence)
        ctx.sleep(ctx.config.poll_interval)


def _observer_messages(ctx):
    if ctx.observer is None:
        raise discord.ObserverUnavailable(ctx.observer_problem or "no observer configured")
    return ctx.observer.messages()


def send_probe(ctx):
    config = ctx.config
    if config.observer == "unavailable" or ctx.observer is None:
        return blocked("no Discord observer is configured, so a reply cannot be verified",
                       unsupported=ctx.observer_problem or contract.GAPS["reply_observation"])
    if config.sender == "unavailable":
        return blocked("no supported Discord sender is configured",
                       unsupported=contract.GAPS["automated_sender"])
    marker = discord.new_marker()
    channel = config.discord["channel_id"]
    window_start = int(time.time() * 1000) - 30_000
    content = "<@" + config.discord["employee_user_id"] + "> " + PROBE_TASK + " " + marker
    mode = "simulation" if config.sender == "simulation" else "human-assisted"
    if config.sender == "simulation":
        discord.SimulatedChannel(config.simulation_channel).post(
            config.discord.get("simulation_sender_user_id", "sim-operator"), content, channel)
    else:
        ctx.output("ACTION REQUIRED: in the test channel, post exactly:\n  " + content)
    deadline = ctx.clock() + config.reply_timeout
    while True:
        try:
            probe = discord.find_probe(_observer_messages(ctx), marker, channel, window_start,
                                       config.discord.get("human_sender_user_id"))
        except discord.ObserverUnavailable as error:
            return blocked("Discord observer unavailable: " + str(error),
                           unsupported=str(error))
        if probe is not None:
            return passed("probe observed in channel (" + mode + " input)", marker=marker,
                          probe_id=probe["id"], input_mode=mode)
        if ctx.clock() >= deadline:
            return blocked("probe message was not observed within "
                           + str(config.reply_timeout) + "s", marker=marker, input_mode=mode)
        ctx.sleep(config.poll_interval)


def verify_reply(ctx):
    probe_evidence = ctx.evidence("send_probe")
    channel = ctx.config.discord["channel_id"]
    employee = ctx.config.discord["employee_user_id"]
    probe = {"id": probe_evidence["probe_id"]}
    window_ms = int(ctx.config.reply_timeout * 1000)
    deadline = ctx.clock() + ctx.config.reply_timeout
    while True:
        try:
            messages = _observer_messages(ctx)
        except discord.ObserverUnavailable as error:
            return blocked("Discord observer unavailable: " + str(error), unsupported=str(error))
        reply, rejections = discord.correlate_reply(messages, probe, channel, employee,
                                                    probe_evidence["marker"], window_ms)
        if reply is not None:
            matched = discord.reply_kind(reply, probe)
            latency = discord.snowflake_ms(reply["id"]) - discord.snowflake_ms(probe["id"])
            ctx.reply = reply
            # Snowflakes resolve whole milliseconds: 0 means the same millisecond.
            return passed("employee replied to the probe; snowflake time difference "
                          + str(latency) + " ms (millisecond resolution)",
                          reply_id=reply["id"], probe_id=probe["id"], matched_by=matched,
                          latency_ms=latency, window_ms=window_ms,
                          reply_author=(reply.get("author") or {}).get("id"),
                          expected_author=employee, reply_channel=reply.get("channel_id"),
                          expected_channel=channel,
                          rejected_candidates=rejections,
                          input_mode=probe_evidence.get("input_mode"))
        if ctx.clock() >= deadline:
            return failed("no reply bound to the probe within " + str(ctx.config.reply_timeout)
                          + "s", Finding(
                              "employee_replies_to_mention", DEFECT,
                              "Employee does not reply to a channel mention",
                              "a reply from the employee referencing the probe",
                              "no bound reply; rejected candidates: " + str(rejections or "none"),
                              context={"observed": "no_bound_reply"},
                              impact="Customers mentioning the employee get no answer."),
                          rejected_candidates=rejections,
                          input_mode=probe_evidence.get("input_mode"))
        ctx.sleep(ctx.config.poll_interval)


def retained_content(sanitizer, text):
    """Reply text for private evidence: sanitized first, then capped. The checksum covers
    exactly the retained text, so no fingerprint of the raw content is kept."""
    clean = sanitizer.text(text)
    kept = clean[:RETAINED_CONTENT_CHARS]
    return {"text": kept, "redacted": clean != text,
            "truncated": len(clean) > RETAINED_CONTENT_CHARS,
            "retained_chars": len(kept), "sanitized_chars": len(clean),
            "sha256": hashlib.sha256(kept.encode("utf-8")).hexdigest(),
            "sha256_covers": "the retained text (sanitized, then capped), UTF-8"}


def verify_reply_content(ctx):
    """Judge the complete reply verify_reply correlated against the configured expected
    response. Delivery keeps its own verdict; this criterion is not part of lifecycle
    recovery. Retained evidence is never the compared text."""
    reply_id = ctx.evidence("verify_reply").get("reply_id")
    reply = ctx.reply if ctx.reply is not None and ctx.reply.get("id") == reply_id else None
    evidence = {"reply_id": reply_id}
    if reply is None:
        return blocked("reply content unverified: the complete reply is not available in "
                       "this process", unsupported="reply content is judged only in the run "
                       "that received it; retained evidence is not used", **evidence)
    content = reply.get("content")
    readable = isinstance(content, str) and content.strip() != ""
    if readable:
        evidence["content"] = retained_content(ctx.store.sanitizer, content)
    expectation = ctx.config.expected_reply
    if expectation is None:
        return blocked("reply content unverified: no expected response is configured",
                       unsupported="config.discord.expected_reply is not set; content was "
                       "retained, not judged", **evidence)
    expected = expectation["template"].replace("{marker}", ctx.evidence("send_probe")["marker"])
    evidence.update(expected=expected, expectation_source=expectation["source"],
                    comparison="equal after trimming and collapsing whitespace; case-sensitive")
    if not readable:
        return blocked("reply content unverified: the reply had no readable content",
                       unsupported="reply content was empty or not visible to the reader "
                       "(for example without the Message Content intent)", **evidence)
    evidence["compared"] = "complete reply"
    if discord.content_matches(content, expected):
        return passed("reply content equals the expected response", content_matches=True,
                      **evidence)
    return failed("reply content differs from the expected response", Finding(
        "employee_reply_content_matches", UNCERTAIN,
        "Employee reply differs from the expected response",
        "the configured expected response (" + expectation["template"] + ")",
        "a correlated reply with different content; see the retained content",
        context={"observed": "content_differs"},
        impact="The employee answered, but not with the requested response.",
        unknowns=["Whether the employee's instructions, its context or the service caused "
                  "the difference."]), content_matches=False, **evidence)


OPERATIONAL = [
    Step("preflight", preflight, "CLI version and contract"),
    Step("verify_fixture", verify_fixture, "identity, approval, policy, revision"),
    Step("initial_state", initial_state, "runtime stopped before test"),
    Step("start_runtime", start_runtime, "start the test runtime"),
    Step("runtime_ready", runtime_ready, "actual readiness, not health"),
    Step("send_probe", send_probe, "uniquely marked mention"),
    Step("verify_reply", verify_reply, "bound reply from the employee"),
    Step("verify_reply_content", verify_reply_content, "reply equals the expected response"),
]


# Cleanup -------------------------------------------------------------------
def _reconcile_stop(ctx):
    detail, problem = _read_lifecycle(ctx, "cleanup")
    if problem:
        return None
    return detail.get("desired_lifecycle_state") in ("stopping", "stopped")


def record_shutdown(ctx, item, evidence):
    """Close a stop obligation only when the shutdown contract's evidence is present."""
    provenance = assertions.record(assertions.SHUTDOWN, evidence)
    status = "failed" if provenance["unmet"] else "done"
    ctx.store.update_cleanup(item["id"], status, dict(evidence, assertion=provenance))
    return status == "done"


def _confirmed_stopped(ctx, item, how, stop_issued=True, journal=None):
    if not record_shutdown(ctx, item, {"confirmed_status": "stopped",
                                       "confirmed_by": "status read", "how": how,
                                       "stop_journal": journal}):
        return False
    if stop_issued:
        # A start whose answer never arrived no longer matters once a stop issued after
        # it is confirmed. Without that stop, a late-applied start could still follow.
        ctx.store.supersede_actions(item["run_id"], "managed_start", "confirmed stop")
    return True


def stop_and_confirm(ctx, item):
    """Stop the runtime and confirm it reached `stopped`; the obligation stays open otherwise."""
    detail, _problem = _read_lifecycle(ctx, "cleanup")
    if detail and detail.get("status") == "stopped" \
            and detail.get("desired_lifecycle_state") == "stopped":
        return _confirmed_stopped(ctx, item, "already stopped; no stop issued",
                                  stop_issued=False)
    status, result = _mutate(ctx, "cleanup", "managed_stop",
                             ["agent", "managed-stop", item["target"]], _reconcile_stop)
    if status not in APPLIED:
        return record_shutdown(ctx, item, {"reason": "stop " + status,
                                           "stderr": result.stderr_tail})
    deadline = ctx.clock() + ctx.config.shutdown_timeout
    while True:
        detail, problem = _read_detail(ctx, "cleanup")
        state = detail.get("status") if detail else None
        if state == "stopped":
            return _confirmed_stopped(ctx, item, "stop confirmed", journal=status)
        if ctx.clock() >= deadline:
            return record_shutdown(ctx, item, {
                "reason": "shutdown not confirmed within "
                          + str(ctx.config.shutdown_timeout) + "s",
                "last_status": state, "stop_journal": status})
        ctx.sleep(ctx.config.poll_interval)


def cleanup(ctx):
    for item in ctx.store.open_cleanup(run_id=ctx.run_id):
        if item["kind"] == "stop_runtime":
            stop_and_confirm(ctx, item)


def reconcile_employee(ctx):
    """Before new work: settle other runs' open cleanup. Returns a refusal reason or None."""
    unresolved = [a for a in ctx.store.employee_unresolved_actions(ctx.config.employee_key)
                  if a["run_id"] != ctx.run_id]
    if unresolved:
        return ("run " + unresolved[0]["run_id"] + " has a " + unresolved[0]["kind"]
                + " (action " + str(unresolved[0]["id"]) + ") with unknown outcome; resume "
                "that run to reconcile it from state, or record a verified outcome with "
                "`python -m seren_harness reconcile`")
    for item in ctx.store.open_cleanup(employee_key=ctx.config.employee_key):
        if item["run_id"] == ctx.run_id or item["kind"] != "stop_runtime":
            continue
        ctx.store.event(ctx.run_id, "reconciling_prior_cleanup", {"run": item["run_id"]})
        if not stop_and_confirm(ctx, item):
            return ("cleanup from run " + item["run_id"] + " is still unconfirmed; inspect the "
                    "test runtime before running again")
    return None


# Provisioning journey ------------------------------------------------------
def inspect_employee(ctx):
    guard = _identity_guard(ctx, "inspect_employee")
    if guard:
        return guard
    mark = len(ctx.cli.history)
    detail, problem = _read_detail(ctx, "inspect_employee")
    if problem:
        return problem
    current = contract.identity(detail)
    evidence = {"commands": ctx.commands_since(mark), "identity": current,
                "status": detail.get("status")}
    if current is None:
        return blocked("deployment detail is opaque to this caller", Finding(
            "deployment_detail_visible", ENVIRONMENT, "Deployment detail is not visible",
            "ManagedAgentDeploymentDetail with identity fields", "owner view only",
            context={}), **evidence)
    ctx.store.update_run(ctx.run_id, fixture_json=current)
    return passed("isolated test employee identified; creation is out of V1 scope",
                  **evidence)


def request_approval(ctx):
    earlier = _earlier_attempt(ctx, "managed_passwords_setup")
    if earlier in UNRESOLVED:
        return _unknown_earlier_attempt("request_approval", "managed_passwords_setup")
    if earlier in APPLIED:
        return blocked("a Passwords setup from this run already exists; cancel it in the "
                       "Passwords UI and start a new provisioning run rather than creating "
                       "another", journal=earlier)
    mark = len(ctx.cli.history)
    status, result = _mutate(ctx, "request_approval", "managed_passwords_setup",
                             ["agent", "managed-passwords-setup", ctx.deployment],
                             lambda _ctx: None)  # No command lists setups (contract gap).
    evidence = {"commands": ctx.commands_since(mark), "journal": status}
    if status == REJECTED:
        return _cli_problem(result, "request_approval", "request_approval")
    if status != SUCCEEDED:
        return blocked("Passwords setup outcome is unknown; a setup may exist. Check the "
                       "Passwords UI or cancel it before starting a new provisioning run",
                       unsupported=contract.GAPS["setup_listing"], **evidence)
    data = result.data if isinstance(result.data, dict) else {}
    launch = data.get("launch_url")
    if not data.get("setup_id"):
        return _cli_problem(_as_decode_error(result), "request_approval", "request_approval")
    # The launch link is a bearer capability: shown to the operator once, never stored.
    ctx.output("ACTION REQUIRED (owner): open this one-time Passwords link, unlock the vault "
               "yourself and approve the requested credentials:\n  " + str(launch)
               + "\nThen run: python -m seren_harness resume " + ctx.run_id)
    return passed("Passwords setup created; owner approval requested", setup_id=data["setup_id"],
                  setup_status=data.get("status"), expires_at=data.get("expires_at"),
                  **evidence)


def owner_approval(ctx):
    setup_id = ctx.evidence("request_approval")["setup_id"]
    result = ctx.cli.json("agent", "managed-passwords-status", setup_id)
    if not result.ok or not isinstance(result.data, dict):
        return _cli_problem(result if not result.ok else _as_decode_error(result),
                            "owner_approval", "read_setup_status")
    state = result.data.get("status")
    evidence = {"commands": [result.argv], "setup_status": state,
                "next_step": result.data.get("next_step"), "reason": result.data.get("reason")}
    if state in contract.SETUP_WAITING:
        return StepResult(AWAITING_HUMAN, "waiting for the owner to approve in Passwords ("
                          + state + "); resume after approval", evidence)
    if state in (contract.SETUP_APPROVED, contract.SETUP_APPLIED):
        return passed("owner approval recorded (" + state + ")", **evidence)
    reason = (_mapping(result.data.get("reason")) or {}).get("kind")
    return failed("Passwords setup ended " + str(state) + " before approval", Finding(
        "owner_approval_reachable", UNCERTAIN, "Passwords setup ended " + str(state)
        + " before owner approval", "the owner can review and approve the requested credentials",
        "setup status " + str(state) + (" (" + reason + ")" if reason else ""),
        context={"setup_status": state, "reason_kind": reason},
        impact="The employee cannot be connected to its channel credential."), **evidence)


def _reconcile_apply(ctx):
    setup_id = ctx.evidence("request_approval")["setup_id"]
    result = ctx.cli.json("agent", "managed-passwords-status", setup_id)
    if not result.ok or not isinstance(result.data, dict):
        return None
    # `approved` does not show that apply did not happen (contract.GAPS["setup_applied_status"]).
    return True if result.data.get("status") == contract.SETUP_APPLIED else None


def apply_approval(ctx):
    # Re-issuing apply is safe: the CLI reports `already_applied` instead of applying twice.
    # A configured attempt cap overrides that: once used, outcomes come from reads only.
    limit = ctx.config.max_apply_attempts
    attempts = [a for a in ctx.store.actions(ctx.run_id) if a["kind"] == "managed_passwords_apply"]
    if limit is not None and len(attempts) >= limit:
        latest = attempts[-1]["status"]
        evidence = {"journal": latest, "apply_attempts": len(attempts), "attempt_limit": limit}
        if latest in APPLIED:
            return passed("apply took effect (journal " + latest + "); not issued again",
                          **evidence)
        return blocked("the Passwords apply attempt limit (" + str(limit) + ") is used and the "
                       "outcome is " + latest + "; apply is not issued again. Verify in Seren "
                       "and record it with `python -m seren_harness reconcile`, or stop",
                       Finding("apply_outcome_known", UNCERTAIN,
                               "Passwords apply outcome cannot be determined",
                               "a known apply outcome within the attempt limit",
                               "outcome " + latest + " after " + str(len(attempts))
                               + " attempt(s)", context={"journal": latest}), **evidence)
    setup_id = ctx.evidence("request_approval")["setup_id"]
    mark = len(ctx.cli.history)
    status, result = _mutate(ctx, "apply_approval", "managed_passwords_apply",
                             ["agent", "managed-passwords-apply", setup_id], _reconcile_apply)
    evidence = {"commands": ctx.commands_since(mark), "journal": status}
    if status == SUCCEEDED and isinstance(result.data, dict):
        ctx.store.supersede_actions(ctx.run_id, "managed_passwords_apply",
                                    "a later apply of the same setup succeeded")
        evidence.update(result_id=result.data.get("result_id"),
                        already_applied=result.data.get("already_applied"))
        return passed("approved setup applied", **evidence)
    if status == RECONCILED_APPLIED:
        return passed("apply outcome was unknown; setup status shows it applied", **evidence)
    if status == REJECTED:
        return _cli_problem(result, "apply_approval", "apply_approval")
    return blocked("apply outcome is unknown; resume issues the apply again, which the CLI "
                   "answers with already_applied if it took effect", Finding(
                       "apply_outcome_known", UNCERTAIN,
                       "Passwords apply outcome cannot be determined",
                       "a reconcilable apply outcome", "unknown after status read-back",
                       context={"journal": status}), stderr=result.stderr_tail, **evidence)


def read_back(ctx):
    mark = len(ctx.cli.history)
    detail, problem = _read_detail(ctx, "read_back")
    if problem:
        return problem
    current = contract.identity(detail)
    evidence = {"commands": ctx.commands_since(mark), "identity": current}
    if current is None:
        return blocked("deployment detail is opaque after apply", **evidence)
    applied = ctx.evidence("apply_approval").get("result_id")
    pending = contract.condition(detail, "AwaitingApproval") == "True"
    # Consistency needs the applied result: a binding never tied to this apply is not proof.
    evidence.update(expected_deployment_id=ctx.deployment, approval_pending=pending,
                    binding_consistent=bool(applied)
                    and applied == current.get("secret_resolution_result_id"))
    if not current.get("secret_resolution_result_id") or pending:
        return failed("apply reported success but the deployment shows no applied approval",
                      Finding("approval_visible_after_apply", UNCERTAIN,
                              "Applied approval not visible on the deployment",
                              "secret_resolution_result_id set and no pending approval",
                              "approval missing after apply", context={}), **evidence)
    if applied and applied != current["secret_resolution_result_id"]:
        # The CLI checked this equality before reporting success, so a difference means
        # the deployment changed after apply. Do not record it as the approved baseline.
        return blocked("deployment names a different approval result than the apply just "
                       "reported; it changed after apply, so no baseline is recorded",
                       Finding("approval_binding_stable", UNCERTAIN,
                               "Deployment approval changed right after apply",
                               "secret_resolution_result_id equals the applied result_id",
                               "a different result is bound", context={}),
                       unsupported=contract.GAPS["approval_binding"], **evidence)
    if not applied:
        evidence["note"] = "apply result_id was not observed; the binding is taken from the " \
                           "deployment as read back"
    ctx.store.update_run(ctx.run_id, fixture_json=current)
    ctx.store.save_fixture(ctx.config.employee_key, current, ctx.run_id)
    return passed("applied approval read back; fixture baseline recorded", **evidence)


PROVISIONING = [
    Step("preflight", preflight, "CLI version and contract"),
    Step("inspect_employee", inspect_employee, "existing isolated test employee"),
    Step("request_approval", request_approval, "Passwords setup for the owner"),
    Step("owner_approval", owner_approval, "human checkpoint in Seren's UI"),
    Step("apply_approval", apply_approval, "apply the approved setup"),
    Step("read_back", read_back, "verify applied identity and record baseline"),
]

CONTROL = [
    Step("preflight", preflight, "CLI version and contract"),
    Step("verify_fixture", verify_fixture, "identity, approval, policy, revision"),
    Step("initial_state", initial_state, "runtime stopped"),
    Step("stopped_rejected", stopped_rejected, "readiness assertion rejects it"),
]

JOURNEYS = {"operational": OPERATIONAL, "provisioning": PROVISIONING, "control": CONTROL}
