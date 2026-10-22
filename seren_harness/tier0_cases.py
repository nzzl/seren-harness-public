"""Tier 0 candidates: bounded experiments on the pinned CLI against the loopback fake API.

Each case states its evidence basis and grade before it runs:
- stated: an authoritative source says it (bundled OpenAPI spec, CLI help/README, or an
  explicit CLI guard/message for the same input). A mismatch can be a client defect.
- inferred: reasonable but unwritten. A mismatch is a contract question, never a defect.
- unspecified: nothing says what should happen. Results are observed behaviour, kept as
  change detectors.

Fake-server sequences are fixed per scenario and do not depend on the suspected bug.
Every case has nearby controls; a failed control makes the case blocked, not a finding.
Facts are deterministic (no raw timings) so repeated runs can be compared exactly.
A known defect is recorded as `reproduced`; if a later binary meets the contract the same
case reports `contract_met` (fix detected). The defect is never the expectation.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from seren_harness import tier0

D = "7a00001a-0000-4000-8000-00000000001b"
R = "7a000017-0000-4000-8000-000000000018"
G = "7a00001b-0000-4000-8000-00000000001c"
T = "2026-10-14T00:00:00Z"
AGENT = "/publishers/seren-agent/deployments/" + D
CLOUD = "/publishers/seren-cloud/deployments/" + D
STREAM = CLOUD + "/runs/" + R + "/stream"
STREAM_ARGS = ["agent", "cloud", "run", "stream", "--deployment-id", D, R]
MARKER = "tier0-after-tool-marker"
FIXED_400 = {"status": 400, "json": {"error": {"code": "tier0_fixed_response",
                                               "message": "tier0 fixed response"}}}
ERROR_BODY = {"error": {"code": "tier0_deployment_not_found",
                        "message": "tier0 synthetic deployment not found"}}
REQUEST_ID = "tier0-request-id-1"

DISPOSITIONS = ("reproduced_client_defect", "not_reproduced", "contract_met",
                "contract_question", "capability_gap", "observed_behavior", "blocked",
                "inconclusive")


class Scenario:
    def __init__(self, name, role, args, routes, auth="key", extra_env=None, deadline=10,
                 files=None):
        self.name, self.role, self.args, self.routes = name, role, args, routes
        self.auth, self.extra_env, self.deadline = auth, extra_env or {}, deadline
        self.files = files or {}


def saved_credentials(access=tier0.SAVED_TOKEN, expires_in=86400, refresh="tier0-refresh"):
    return {"access_token": access, "refresh_token": refresh,
            "expires_at": int(time.time()) + expires_in,
            "session_id": "7a000016-0000-4000-8000-000000000017"}  # config.rs: Uuid


def spec(scenario):
    """The scenario as pure data for the trusted executor (tier0.run_spec)."""
    return {"args": list(scenario.args),
            "routes": [[m, p, replies] for (m, p), replies in scenario.routes.items()],
            "auth": scenario.auth, "extra_env": dict(scenario.extra_env),
            "deadline": scenario.deadline, "files": dict(scenario.files)}


def run_scenario(lab, scenario):
    """One bounded run through a Lab (direct) or LabClient (from inside the sandbox)."""
    return lab.run_spec(spec(scenario))


# ---------------------------------------------------------------- fact helpers

def to(obs, method, path):
    return [r for r in obs["requests"] if r["method"] == method and r["path"] == path]


def ending(obs):
    if obs["killed_by_supervisor"]:
        return "supervisor_kill"
    return "server_close" if "Stream ended." in obs["stderr"] else "self"


def printed(obs):
    return [line.split("]", 1)[0][1:] for line in obs["stderr"].splitlines()
            if line.startswith("[")]


def stream_facts(obs):
    return {"ending": ending(obs), "exit": None if obs["killed_by_supervisor"] else obs["exit"],
            "frames_printed": printed(obs), "marker_printed": MARKER in obs["stderr"],
            "requests": len(obs["requests"])}


def meaningful(body):
    """Keys carrying any user-supplied value (nulls, empty objects/lists dropped)."""
    if not isinstance(body, dict):
        return body
    return sorted(k for k, v in body.items() if v not in (None, {}, []))


def first_error_line(obs):
    """First stderr line with volatile thread ids and fake-API ports removed."""
    lines = [x for x in obs["stderr"].splitlines() if x.strip()]
    if not lines:
        return ""
    line = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:PORT", re.sub(r"\(\d+\)", "(N)", lines[0]))
    return line[:160]


def result(case_id, part, disposition, summary, **extra):
    assert disposition in DISPOSITIONS
    return dict({"case": case_id, "part": part, "disposition": disposition,
                 "summary": summary}, **extra)


# ---------------------------------------------------------------- C1 stream

def _state(status, terminal):
    return {"event": "run.state", "data": {
        "run_id": R, "deployment_id": D, "status": status, "phase": status,
        "pending_approval_count": 0, "latest_sequence": 0, "started_at": T, "updated_at": T,
        "terminal": terminal}}


REPLAY = {"event": "replay_complete", "data": {"latest_sequence": 0}}


def _event(seq, payload):
    return {"event": "run.event", "id": seq, "data": dict(payload, sequence_number=seq)}


def _stream(name, role, frames, hold=6, deadline=3, frame_delay=0):
    reply = {"sse": frames, "hold": hold, "frame_delay": frame_delay}
    return Scenario(name, role, STREAM_ARGS, {("GET", STREAM): [reply]}, deadline=deadline)


SHAPES = (Path(__file__).resolve().parents[1] / "docs" / "tier0" / "fixtures" /
          "c1-recorded-event-shapes.json")
RECORDED_FIELDS = ("type", "kind", "event_type", "status", "is_error", "retryable")
PLACEHOLDER = {"id": "tier0-call-1", "item_id": "tier0-item-1", "name": "tier0_tool",
               "arguments": "{}", "content": "tier0 tool output", "text": MARKER,
               "tool": "tier0_tool", "reason": "tier0", "code": "tier0_error",
               "recovery": None, "input_bytes": 1, "output_bytes": 1, "latency_ms": 1}


def recorded_events(shape, drop_tool_status=False):
    """`run.event` frames whose data are recorded run-event structures (recorded fields
    kept, every other value a placeholder; the answer text is MARKER). Frame id is the
    recorded sequence number, as the stream spec documents for `run.event` ids."""
    frames = []
    for seq, rec in enumerate(json.loads(SHAPES.read_text())["shapes"][shape], 1):
        data = {}
        for field in rec["fields"]:
            if field == "sequence_number":
                data[field] = seq
            elif field in RECORDED_FIELDS:
                data[field] = rec[field]
            else:
                data[field] = PLACEHOLDER[field]
        if drop_tool_status and data["type"] == "tool_call":
            del data["status"]  # control: the only change from the recorded shape
        frames.append({"event": "run.event", "id": seq, "data": data})
    return frames


def _stream_scenarios():
    running = [_state("running", False), REPLAY]
    done = lambda status: [_event(1, {"type": "text", "text": "tier0 text"}),  # noqa: E731
                           _event(2, {"type": "done", "status": status})]
    tool = lambda status: [_event(1, {"type": "tool_call", "id": "call-1",  # noqa: E731
                                      "name": "system_time", "status": status}),
                           _event(2, {"type": "text", "text": MARKER})]
    finish = {"event": "run.completed", "data": {"status": "completed"}}
    out = [
        _stream("control-cli-terminal-event", "control", running + [finish]),
        _stream("control-state-completed", "control", [_state("completed", True), REPLAY]),
        _stream("control-tool-running", "control", running + tool("running") + [finish],
                frame_delay=0.2),
        Scenario("control-http-404", "control", STREAM_ARGS,
                 {("GET", STREAM): [{"status": 404, "json": ERROR_BODY}]}),
        _stream("done-ok-server-closes", "observation", running + done("ok"), hold=1,
                deadline=6),
        _stream("tool-completed", "probe",
                running + tool("completed") + [_event(3, {"type": "done", "status": "ok"})],
                frame_delay=0.2),
        _stream("error-event", "probe", running + [
            {"event": "error", "data": {"message": "tier0 synthetic failure",
                                        "code": "runtime_error"}}]),
        _stream("run-failed-event", "probe", running + [
            {"event": "run.failed", "data": {"status": "failed"}}]),
    ]
    out += [
        # Recorded run-event shapes (docs/tier0/fixtures/c1-recorded-event-shapes.json).
        _stream("recorded-completed-tool-open", "probe",
                running + recorded_events("completed-tool"), frame_delay=0.2),
        _stream("recorded-completed-tool-closes", "observation",
                running + recorded_events("completed-tool"), hold=1.5, deadline=8,
                frame_delay=0.2),
        _stream("control-recorded-tool-without-status", "control",
                running + recorded_events("completed-tool", drop_tool_status=True),
                hold=1.5, deadline=8, frame_delay=0.2),
        _stream("recorded-error-tool-closes", "observation",
                running + recorded_events("error-tool"), hold=1.5, deadline=8,
                frame_delay=0.2),
    ]
    out += [_stream("done-%s-open" % s, "probe", running + done(s))
            for s in ("ok", "error", "cancelled")]
    out += [_stream("state-%s-terminal-open" % s, "probe", [_state(s, True), REPLAY])
            for s in ("timeout", "blocked", "error")]
    return out


def _run_events_printed(fact):
    return sum(1 for name in fact["frames_printed"] if name == "run.event")


def _judge_tool_completed(facts, shape):
    synthetic = facts["tool-completed"]
    rec_open, rec_close = (facts["recorded-completed-tool-open"],
                           facts["recorded-completed-tool-closes"])
    control, err = (facts["control-recorded-tool-without-status"],
                    facts["recorded-error-tool-closes"])
    if not (control["marker_printed"] and _run_events_printed(control) == 5
            and err["marker_printed"]):
        return result("C1", "tool-completed", "blocked", "recorded-shape controls did not "
                      "print the answer text; fixture or harness problem")
    early = (rec_open["ending"] == "self" and not rec_open["marker_printed"]
             and _run_events_printed(rec_open) == 1 and rec_open["exit"] == 0
             and not rec_close["marker_printed"])
    if not early:
        return result("C1", "tool-completed", "not_reproduced",
                      "with recorded run-event shapes the CLI printed the run's answer text")
    return result(
        "C1", "tool-completed", "contract_question",
        "with run-event shapes recorded from four real tool-using runs delivered as "
        "`run.event` frames (frame id = sequence number), the pinned CLI printed only the "
        "first run event, a tool_call whose recorded status is `completed`, and exited %s "
        "by itself; tool_result, the answer text, tool_audit and done (events 2-5) were "
        "never printed, "
        "also when the server would have closed later (%s). Removing only that status "
        "(control) let it print all five events including the answer text; the recorded "
        "error-status tool call did not stop it. The earlier synthetic fixture agrees (%s)."
        % (rec_open["exit"], rec_close["ending"], synthetic["ending"]),
        grade="inferred", basis=[
            "Synthetic event-shape examples exercise tool calls followed by final answers.",
            "cloud spec run stream: 'Each `run.event` frame's `id` is its monotonic sequence "
            "number'; response `text/event-stream: {}` declares no frame data schema",
            "main.rs run stream help: 'Stream run events over SSE'; agent.rs "
            "cloud_run_stream_url treats any payload with top-level status completed as "
            "the end of the run", shape,
            "not observed: a live `run.event` frame's payload, the status a live tool_call "
            "carries when emitted (records hold the final status), server closing"],
        question="Are `run.event` frame payloads the stored run-event envelopes (as replay "
                 "of a finished run would suggest)? If so, `run stream` ends at the first "
                 "successful tool call of any tool-using run.",
        impact="hypothetical but likely if frames carry the stored envelopes: watching or "
               "replaying a tool-using run would silently stop after its first successful "
               "tool call, hide the answer and report success (exit 0)")


def judge_stream(facts):
    controls = (facts["control-cli-terminal-event"]["ending"] == "self"
                and facts["control-state-completed"]["ending"] == "self"
                and facts["control-tool-running"]["marker_printed"]
                and facts["control-tool-running"]["ending"] == "self"
                and facts["control-http-404"]["exit"] == 1)
    if not controls:
        return [result("C1", "controls", "blocked", "stream controls did not behave as the "
                       "CLI's own terminal detection predicts; harness or fake API problem")]
    # Generalized regression scenario; private investigation history omitted.
    # `text/event-stream: {}` with no frame data schema, and nothing states whether the
    # service closes the stream at a terminal event or which statuses are terminal. The
    # fixtures below are assumed shapes, so these parts are contract questions, not defects
    # (previously reported as reproduced_client_defect).
    shape = ("assumed fixture: `run.event` data is a flattened CloudRunOutputEventEnvelope "
             "(the stream spec gives no data schema; inferred from frame id = sequence)")
    out = []
    hung = [s for s in ("ok", "error", "cancelled")
            if facts["done-%s-open" % s]["ending"] == "supervisor_kill"]
    out.append(result(
        "C1", "done-frame", "contract_question" if hung else "not_reproduced",
        ("a `run.event` frame whose data is a `done` event (status %s; assumed shape) did "
         "not end the CLI; with the connection held open it waited until the supervisor "
         "killed it. With the server closing instead, it exited 0 with 'Stream ended.' (%s)"
         % ("/".join(hung), facts["done-ok-server-closes"]["ending"])) if hung else
        "the CLI ended by itself on a `run.event` done frame",
        grade="inferred", basis=[
            "cloud spec run stream (seren_cloud_deployment_run_stream): frames `run.state`, "
            "`replay_complete`, `run.event`; response `text/event-stream: {}` (no data "
            "schema); nothing states whether the server closes after a terminal event",
            "cloud spec CloudRunOutputEvent done: 'Run completed; status indicates how it "
            "ended' (CloudRunDoneStatus ok|error|cancelled)",
            "main.rs run stream help: 'Stream run events over SSE, with optional "
            "Last-Event-ID resume' (no promise to exit at run end)", shape],
        question="Does the service close the run stream after a terminal event? If it can "
                 "stay open, should `run stream` end itself on a `run.event` done frame?",
        impact="hypothetical: only if the service keeps the stream open after a run "
               "finishes, `seren agent cloud run stream` would not return"))
    stuck = [s for s in ("timeout", "blocked", "error")
             if facts["state-%s-terminal-open" % s]["ending"] == "supervisor_kill"]
    out.append(result(
        "C1", "terminal-statuses", "contract_question" if stuck else "not_reproduced",
        ("`run.state` with terminal=true and status %s did not end the CLI (killed at the "
         "deadline), while status completed did" % "/".join(stuck)) if stuck else
        "every listed run status with terminal=true ended the stream",
        grade="inferred", basis=[
            "cloud spec runs list status vocabulary: queued, running, awaiting_approval, "
            "completed, failed, cancelled, timeout, blocked, error (terminality not stated)",
            "cloud spec CloudRunLiveState.terminal: boolean with no description",
            "assumed fixture: `run.state` data is a CloudRunLiveState (no stream data "
            "schema); server-side stream closing is unspecified"],
        question="Is a `run.state` frame with terminal=true the end of the stream, and are "
                 "timeout, blocked and error terminal run statuses?",
        impact="hypothetical: only if the service keeps the stream open after such a "
               "state, stream watchers would not return"))
    out.append(_judge_tool_completed(facts, shape))
    codes = {k: facts[k]["exit"] for k in ("error-event", "run-failed-event",
                                           "control-cli-terminal-event")}
    out.append(result(
        "C1", "exit-codes", "observed_behavior",
        "exit status after an `error` frame: %(error-event)s; after `run.failed`: "
        "%(run-failed-event)s; after `run.completed`: %(control-cli-terminal-event)s. "
        "A failed run is indistinguishable from success by exit code." % codes,
        grade="unspecified", basis=["no spec or help text defines stream exit codes"],
        question="Should `run stream` exit non-zero when the run fails or errors?"))
    return out


# ---------------------------------------------------------------- C2 eval gate

def _gate(name, role, command, flags):
    method = "POST" if command == "managed-preview" else "PATCH"
    path = AGENT + ("/managed/preview" if command == "managed-preview" else "/managed")
    return Scenario(name, role, ["agent", command, D] + flags,
                    {("POST", AGENT + "/managed/preview"): [FIXED_400],
                     ("PATCH", AGENT + "/managed"): [FIXED_400]}), (method, path)


def _gate_scenarios():
    both = ["--eval-gate-set-id", G, "--eval-gate-max-age-seconds", "3600"]
    return [
        _gate("control-both-preview", "control", "managed-preview", both)[0],
        _gate("control-no-fields", "control", "managed-preview", [])[0],
        _gate("set-only-preview", "probe", "managed-preview", both[:2])[0],
        _gate("age-only-preview", "probe", "managed-preview", both[2:])[0],
        _gate("set-only-update", "probe", "managed-update", both[:2])[0],
        # F-C2-age-only-update: the audit's missing scenario, with its own update control.
        _gate("control-both-update", "control", "managed-update", both)[0],
        _gate("age-only-update", "probe", "managed-update", both[2:])[0],
    ]


def gate_facts(obs):
    sent = [r for r in obs["requests"] if r["path"].startswith(AGENT + "/managed")]
    return {"requests": len(sent),
            "body_keys": meaningful(sent[0]["body"]) if sent else None,
            "eval_gate": (sent[0]["body"] or {}).get("eval_gate") if sent else None,
            "idempotency_key_sent": bool(sent and sent[0]["headers"].get("idempotency-key")),
            "guard_message": "No managed deployment updates specified" in obs["stderr"],
            "exit": obs["exit"]}


def judge_gate(facts):
    gate = {"set_id": G, "max_age_seconds": 3600}
    both, none = facts["control-both-preview"], facts["control-no-fields"]
    update = facts["control-both-update"]
    if not (both["requests"] == 1 and both["eval_gate"] == gate
            and update["requests"] == 1 and update["eval_gate"] == gate
            and none["requests"] == 0 and none["guard_message"]):
        return [result("C2", "controls", "blocked", "eval-gate controls failed")]
    empty = [k for k in ("set-only-preview", "age-only-preview", "set-only-update",
                         "age-only-update")
             if facts[k]["requests"] == 1 and not facts[k]["body_keys"]]
    keyed = [k for k in empty if k.endswith("-update") and facts[k]["idempotency_key_sent"]]
    return [result(
        "C2", "partial-eval-gate", "reproduced_client_defect" if empty else "contract_met",
        ("with only one eval-gate flag the CLI sent an update with no fields at all (%s), "
         "bypassing its own empty-update refusal; the user's eval-gate input was silently "
         "discarded. Sent as a managed-update mutation with an Idempotency-Key: %s."
         % (", ".join(empty), ", ".join(keyed) or "none")) if empty else
        "partial eval-gate input is rejected or represented",
        grade="stated", basis=[
            "main.rs managed-update/managed-preview help: --eval-gate-set-id 'Updated eval "
            "set ID that gates execution'; --eval-gate-max-age-seconds 'Updated eval gate "
            "freshness window in seconds' (each advertised as an update)",
            "agent spec EvalGate: required set_id and max_age_seconds; 'set_id and "
            "max_age_seconds always travel together; either both are set or the gate is "
            "absent' (a partial gate cannot be sent)",
            "agent.rs build_managed_agent_update_request: explicit CLI guard for the same "
            "effective input, 'No managed deployment updates specified. Provide at least "
            "one field or --agent-config.'; the gate is assembled only when both flags are "
            "present, after that check",
            "Synthetic contract test: request bodies represented as `{}` before any server "
            "reply; the fake 400 does not shape the request"],
        impact="hypothetical (server reaction to `{}` not tested): `managed-update "
               "--eval-gate-set-id X` submits an empty mutation and reports whatever the "
               "server does with it while the operator believes an eval gate was set")]


# ---------------------------------------------------------------- C3 run id

RUN_ACCEPTED = {"status": 202, "json": {"data": {"status": "queued", "run_id": R,
                                                 "execution_id": "tier0-exec-1"}}}


def _runid_scenarios():
    base = ["agent", "cloud", "run", "start", "--deployment-id", D, "--message", "tier0 hello",
            "--async"]
    routes = {("POST", CLOUD + "/runs"): [RUN_ACCEPTED]}
    return [Scenario("control-without-run-id", "control", base, routes),
            Scenario("with-run-id", "probe", base + ["--run-id", "tier0-client-run"], routes)]


def runid_facts(obs):
    sent = to(obs, "POST", CLOUD + "/runs")
    body = sent[0]["body"] if sent else None
    return {"requests": len(sent), "body_keys": meaningful(body),
            "run_id_sent": isinstance(body, dict) and "run_id" in body,
            "accepted_printed": "Run ID: " + R in obs["stdout"], "exit": obs["exit"]}


def judge_runid(facts):
    ctl = facts["control-without-run-id"]
    if not (ctl["requests"] == 1 and not ctl["run_id_sent"] and ctl["accepted_printed"]):
        return [result("C3", "controls", "blocked", "run start control failed")]
    # Generalized regression scenario; private investigation history omitted.
    # operation, POST /deployments/{id}/runs, and its run_id is 'used by some
    # orchestrators'. Reading 'deployment-scoped callers' as every caller of that route
    # would make the field unusable, so it may mean deployment-scoped (runtime) identities.
    # Ambiguous contract: a question, not a defect (previously reproduced_client_defect).
    sent = facts["with-run-id"]["run_id_sent"]
    return [result(
        "C3", "run-id", "contract_question" if sent else "not_reproduced",
        "`run start --run-id` put `run_id` in the body of POST /deployments/{id}/runs, the "
        "only route the command uses" if sent else "run_id omitted on the scoped route",
        grade="inferred", basis=[
            "cloud spec CloudDeploymentRunRequest.run_id: 'Optional client correlation "
            "identifier used by some orchestrators. Deployment-scoped callers must omit it "
            "because Seren Cloud assigns child run identifiers.'",
            "cloud spec: CloudDeploymentRunRequest is the request body of seren_cloud_run "
            "(POST /deployments/{id}/runs) only; 'deployment-scoped callers' is not defined",
            "main.rs run start --run-id help: 'Optional run identifier (useful for "
            "resumable orchestrations)'"],
        question="Does 'deployment-scoped callers must omit it' apply to a user running "
                 "`seren agent cloud run start --run-id`? If so, should the CLI refuse or "
                 "drop the flag?",
        impact="hypothetical: if the sentence applies to CLI users, every use of the "
               "advertised flag sends a field the service says to omit; the server's "
               "reaction is unknown and not tested here")]


# ---------------------------------------------------------------- C4 auth selection

AUTH_ROUTES = {("GET", AGENT + "/managed"): [{"status": 404, "json": ERROR_BODY}],
               ("GET", CLOUD): [{"status": 404, "json": ERROR_BODY}],
               ("GET", STREAM): [{"status": 404, "json": ERROR_BODY}],
               ("POST", "/oauth2/token"): [{"status": 400, "json": {"error": "tier0"}}]}
BAD = "seren_tier0_bad\nline"


def _auth_scenarios():
    get = ["agent", "managed-get", D]
    status = ["agent", "cloud", "deployment", "status", D]
    saved = saved_credentials()
    return [
        Scenario("control-saved-sign-in", "control", get, AUTH_ROUTES, auth=saved),
        Scenario("control-api-key", "control", get, AUTH_ROUTES),
        Scenario("empty-key-with-saved-sign-in", "probe", get, AUTH_ROUTES, auth=saved,
                 extra_env={"SEREN_API_KEY": ""}),
        Scenario("malformed-key-managed", "probe", get, AUTH_ROUTES,
                 extra_env={"SEREN_API_KEY": BAD}),
        Scenario("malformed-key-cloud-status", "probe", status, AUTH_ROUTES,
                 extra_env={"SEREN_API_KEY": BAD}),
        Scenario("malformed-key-stream-raw", "probe", STREAM_ARGS, AUTH_ROUTES,
                 extra_env={"SEREN_API_KEY": BAD}),
        Scenario("malformed-saved-token-managed", "probe", get, AUTH_ROUTES,
                 auth=saved_credentials(access="tier0_bad\nline")),
    ]


def auth_facts(obs):
    api = [r for r in obs["requests"] if r["path"] != "/oauth2/token"]
    return {"requests": len(api),
            "refresh_requests": len(to(obs, "POST", "/oauth2/token")),
            "authorization": api[0]["headers"].get("authorization") if api else None,
            "exit": obs["exit"], "panicked": "panicked" in obs["stderr"],
            "error": first_error_line(obs).replace("\n", " ")}


def judge_auth(facts):
    saved, key = facts["control-saved-sign-in"], facts["control-api-key"]
    if not (saved["authorization"] == "Bearer " + tier0.SAVED_TOKEN
            and key["authorization"] == "Bearer " + tier0.DUMMY_KEY
            and saved["refresh_requests"] == 0):
        return [result("C4", "controls", "blocked", "credential controls failed")]
    empty = facts["empty-key-with-saved-sign-in"]
    out = [result(
        "C4", "empty-api-key",
        "contract_question" if empty["authorization"] != saved["authorization"] else
        "not_reproduced",
        ("with SEREN_API_KEY set to an empty string the CLI ignored the valid saved sign-in "
         "and sent Authorization %r" % empty["authorization"])
        if empty["authorization"] != saved["authorization"] else
        "an empty SEREN_API_KEY falls back to the saved sign-in",
        grade="inferred", basis=[
            "main.rs: --api-key 'API key for authentication (overrides stored "
            "credentials)', env SEREN_API_KEY",
            "auth.rs get_bearer_token returns any override verbatim",
            "sibling variables SEREN_API_BASE/SEREN_PROFILE/SEREN_OAUTH_HOST treat blank "
            "as unset (defaults.rs, config.rs, auth.rs)"],
        question="Should an empty SEREN_API_KEY count as unset, like the other SEREN_* "
                 "variables?",
        impact="CI or shell profiles exporting SEREN_API_KEY= make an authenticated user's "
               "commands fail with an authorization error from the server")]
    paths = {k: facts[k] for k in ("malformed-key-managed", "malformed-key-cloud-status",
                                   "malformed-key-stream-raw", "malformed-saved-token-managed")}
    panics = sorted(k for k, f in paths.items() if f["panicked"])
    out.append(result(
        "C4", "malformed-credential",
        "contract_question" if panics else "not_reproduced",
        ("a credential containing a newline crashed the CLI with a Rust panic (exit %s) on "
         "%s; the raw-HTTP stream path reported a normal error (exit %s, %r). No request "
         "was sent in any case (%s)." % (
             paths[panics[0]]["exit"], ", ".join(panics),
             paths["malformed-key-stream-raw"]["exit"],
             paths["malformed-key-stream-raw"]["error"][:80],
             sum(f["requests"] for f in paths.values())))
        if panics else "malformed credentials produce ordinary errors on all paths",
        grade="inferred", basis=[
            "sdk lib.rs Client::from_config: HeaderValue::from_str(...).expect('Invalid "
            "bearer token')",
            "command_context.rs http_client_with_redirect maps the same failure to an error"],
        question="Should the SDK path report an invalid credential as an error (exit 1) "
                 "instead of panicking (exit 101)?",
        impact="a pasted credential with a stray newline crashes with a panic message"))
    return out


# ---------------------------------------------------------------- C5 error output

# F-C5-error-detail-breadth: everyday cloud reads with the same replies. run start is a
# cloud command on anyhow_from_seren_error (agent.rs 7705); it is recorded as an
# observation, managed-get stays the control.
BREADTH = {
    "deployment-list": (["agent", "cloud", "deployment", "list"],
                        ("GET", "/publishers/seren-cloud/deployments")),
    "runs-list": (["agent", "cloud", "runs", "list", "--deployment-id", D],
                  ("GET", CLOUD + "/runs")),
    "run-get": (["agent", "cloud", "run", "get", "--deployment-id", D, R],
                ("GET", CLOUD + "/runs/" + R)),
}
RUN_START = ["agent", "cloud", "run", "start", "--deployment-id", D, "--message", "tier0 hello"]


def _error_scenarios():
    get = ["agent", "managed-get", D]
    status = ["agent", "cloud", "deployment", "status", D]
    out = []
    for code in (404, 500, 403):
        reply = {"status": code, "json": ERROR_BODY, "headers": {"x-request-id": REQUEST_ID}}
        routes = {("GET", AGENT + "/managed"): [reply], ("GET", CLOUD): [reply],
                  ("POST", CLOUD + "/runs"): [reply]}
        routes.update({route: [reply] for _, route in BREADTH.values()})
        out.append(Scenario("managed-get-%d" % code, "control", get, routes))
        out.append(Scenario("cloud-status-%d" % code, "probe", status, routes))
        out += [Scenario("%s-%d" % (name, code), "probe", args, routes)
                for name, (args, _) in BREADTH.items()]
        out.append(Scenario("run-start-%d" % code, "observation", RUN_START, routes))
    return out


def error_facts(obs):
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "message": ERROR_BODY["error"]["message"] in obs["stderr"],
            "code": ERROR_BODY["error"]["code"] in obs["stderr"],
            "request_id": REQUEST_ID in obs["stderr"],
            "status_shown": any(str(c) in obs["stderr"] for c in (403, 404, 500))}


def judge_errors(facts):
    managed = [facts["managed-get-%d" % c] for c in (404, 500, 403)]
    if not all(f["requests"] == 1 and f["exit"] == 1 and f["message"] for f in managed):
        return [result("C5", "controls", "blocked", "managed-get did not show the server "
                       "message; the comparison baseline is missing")]
    lost = sorted(c for c in (404, 500, 403) if not facts["cloud-status-%d" % c]["message"])
    rid = sorted(c for c in (404, 500, 403) if facts["cloud-status-%d" % c]["request_id"])
    return [_error_detail(lost, rid), _error_breadth(facts)]


def _error_breadth(facts):
    codes = (404, 500, 403)
    probes = {n: [facts["%s-%d" % (n, c)] for c in codes] for n in BREADTH}
    if not all(f["requests"] == 1 and f["exit"] == 1 for fs in probes.values() for f in fs):
        return result("C5", "cloud-error-breadth", "blocked", "a cloud read did not send "
                      "exactly one request and fail; arguments or routes do not match")
    lost = {n: [c for c, f in zip(codes, fs) if not f["message"]] for n, fs in probes.items()}
    start = [c for c in codes if not facts["run-start-%d" % c]["message"]]
    any_lost = any(lost.values())
    return result(
        "C5", "cloud-error-breadth", "observed_behavior" if any_lost else "not_reproduced",
        "for the reply managed-get shows in full, the server's error message was omitted by "
        "%s; run start (anyhow_from_seren_error) omitted it for HTTP %s"
        % ("; ".join("%s for HTTP %s" % (n, c) for n, c in lost.items() if c) or "none",
           start or "none"),
        grade="unspecified", basis=[
            "follow-up F-C5-error-detail-breadth (C5 cloud-error-detail)",
            "agent.rs 6971 cloud_list, 10164 cloud_runs, 10192 cloud_run_get: "
            "map_err(|e| anyhow!(\"Failed: {}\", e))",
            "agent.rs 7705 cloud_run: anyhow_from_seren_error (462-492 prints up to 1200 "
            "characters of the body)",
            "no spec or README text defines CLI error output"],
        question="Should cloud-* reads show the server's error message like managed-* "
                 "commands do?",
        impact="hypothetical: operators listing deployments or runs, or reading a run, see "
               "only a status line without the server's reason")


def _error_detail(lost, rid):
    return result(
        "C5", "cloud-error-detail", "observed_behavior" if lost else "not_reproduced",
        ("for the same server reply, managed-get printed the server's error message while "
         "`cloud deployment status` omitted it for HTTP %s (request id shown for %s)"
         % (lost, rid or "none")) if lost else "cloud errors keep the server message",
        grade="unspecified", basis=[
            "agent.rs anyhow_from_seren_error prints up to 1200 characters of the body "
            "(used by managed-* and run start)",
            "agent.rs cloud_status: map_err(|e| anyhow!(\"Failed: {}\", e))",
            "no spec or README text defines CLI error output"],
        question="Should cloud-* commands show the server's error message like managed-* "
                 "commands do?",
        impact="operators see only a status line for cloud command failures, without the "
               "server's reason")


# ---------------------------------------------------------------- C6 revision precondition

def _revision_scenarios():
    config = json.dumps({"expected_active_revision_id": R})
    routes = {("PATCH", AGENT + "/managed"): [FIXED_400]}
    return [
        Scenario("control-rollback-help", "control", ["agent", "managed-rollback", "--help"],
                 routes),
        Scenario("update-help", "probe", ["agent", "managed-update", "--help"], routes),
        Scenario("update-agent-config", "probe",
                 ["agent", "managed-update", D, "--agent-config", "{file:precondition.json}"],
                 routes, files={"precondition.json": config}),
    ]


def revision_facts(obs):
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "flag_listed": "--expected-active-revision-id" in obs["stdout"],
            "refused_key": "Unsupported managed agent config keys" in obs["stderr"]}


def judge_revision(facts):
    if not facts["control-rollback-help"]["flag_listed"]:
        return [result("C6", "controls", "blocked", "rollback help did not list the flag")]
    gap = (not facts["update-help"]["flag_listed"]
           and facts["update-agent-config"]["refused_key"]
           and facts["update-agent-config"]["requests"] == 0)
    return [result(
        "C6", "update-precondition", "capability_gap" if gap else "contract_met",
        "managed-update offers no --expected-active-revision-id and refuses it in "
        "--agent-config before any request; rollback and managed-files offer it" if gap else
        "managed-update can send the revision precondition",
        grade="stated", basis=[
            "agent spec AgentSpecUpdate.expected_active_revision_id: 'Active revision "
            "against which the policy result was approved. Required when replacing Seren "
            "Secrets credentials, Employee identity, or result.'",
            "main.rs: the flag exists for managed-files and managed-rollback(-preview) only"],
        impact="the CLI cannot send this precondition on update; no lost update or server "
               "race is claimed",
        question="Which updates require the precondition, and should managed-update expose "
                 "it?")]


# ---------------------------------------------------------------- C7 mutation retry

ACTIONS = AGENT + "/managed/actions/*"


def _retry_scenarios():
    update = ["agent", "managed-update", D, "--name", "tier0-name"]
    unavailable = {"status": 503, "json": {"error": {"message": "tier0 unavailable"}}}
    not_found = {"status": 404, "json": {"error": {"message": "tier0 no such action"}}}
    lookup_error = {"status": 500, "json": {"error": {"message": "tier0 lookup failed"}}}
    return [
        Scenario("control-rejected-4xx", "control", update,
                 {("PATCH", AGENT + "/managed"): [FIXED_400],
                  ("GET", ACTIONS): [not_found]}),
        Scenario("ambiguous-then-not-found-then-rejected", "probe", update,
                 {("PATCH", AGENT + "/managed"): [unavailable, FIXED_400],
                  ("GET", ACTIONS): [not_found]}),
        Scenario("ambiguous-twice", "probe", update,
                 {("PATCH", AGENT + "/managed"): [unavailable, unavailable],
                  ("GET", ACTIONS): [not_found]}),
        Scenario("ambiguous-then-lookup-error", "probe", update,
                 {("PATCH", AGENT + "/managed"): [unavailable, FIXED_400],
                  ("GET", ACTIONS): [lookup_error]}),
    ]


def retry_facts(obs):
    sends = to(obs, "PATCH", AGENT + "/managed")
    lookups = [r for r in obs["requests"] if r["path"].startswith(ACTIONS[:-1])]
    keys = [r["headers"].get("idempotency-key") for r in sends]
    looked = [r["path"].rsplit("/", 1)[1] for r in lookups]
    return {"sends": len(sends), "lookups": len(lookups),
            "one_key_throughout": len(set(keys + looked)) == 1 and None not in keys,
            "same_body": all(r["body"] == sends[0]["body"] for r in sends),
            "exit": obs["exit"],
            "message": ("could not be confirmed after retry" in obs["stderr"] and "retry") or
                       ("unknown outcome" in obs["stderr"] and "unknown") or
                       ("unexpected response 400" in obs["stderr"] and "rejected") or "other"}


def judge_retry(facts):
    ctl = facts["control-rejected-4xx"]
    if not (ctl["sends"] == 1 and ctl["lookups"] == 0 and ctl["exit"] == 1):
        return [result("C7", "controls", "blocked", "4xx control did not behave as expected")]
    a, b, c = (facts["ambiguous-then-not-found-then-rejected"], facts["ambiguous-twice"],
               facts["ambiguous-then-lookup-error"])
    safe = (a["sends"] == 2 and a["lookups"] == 1 and a["one_key_throughout"] and a["same_body"]
            and b["sends"] == 2 and b["message"] == "retry"
            and c["sends"] == 1 and c["message"] == "unknown")
    return [result(
        "C7", "ambiguous-update-retry", "contract_met" if safe else "reproduced_client_defect",
        ("after a 5xx the CLI looked up the action by the same request id it sent as "
         "Idempotency-Key, resent once only when the lookup said not found (same key, same "
         "body), never a third time, and did not resend when the lookup itself failed")
        if safe else "ambiguous-update retry did not keep one mutation identity: %s" % facts,
        grade="stated", basis=[
            "agent spec PATCH managed: Idempotency-Key 'Stable mutation request identity'; "
            "409 'Mutation identity or revision conflict'",
            "agent.rs submit_managed_mutation: lookup then at most one resend"],
        impact="a retry with a new key or body could apply an update twice")]


# ---------------------------------------------------------------- C8 unauthorized

def _unauthorized_scenarios():
    reply = {"status": 401, "json": {"error": {"code": "tier0_unauthorized",
                                               "message": "tier0 token rejected"}}}
    routes = {("GET", AGENT + "/managed"): [reply], ("PATCH", AGENT + "/managed"): [reply],
              ("GET", ACTIONS): [reply], ("POST", "/oauth2/token"): [FIXED_400]}
    return [
        Scenario("read-401", "probe", ["agent", "managed-get", D], routes,
                 auth=saved_credentials()),
        Scenario("update-401", "probe", ["agent", "managed-update", D, "--name", "tier0"],
                 routes, auth=saved_credentials()),
        Scenario("control-read-404", "control", ["agent", "managed-get", D],
                 {("GET", AGENT + "/managed"): [{"status": 404, "json": ERROR_BODY}]},
                 auth=saved_credentials()),
    ]


def unauthorized_facts(obs):
    return {"api_requests": len([r for r in obs["requests"] if r["path"] != "/oauth2/token"]),
            "refresh_requests": len(to(obs, "POST", "/oauth2/token")), "exit": obs["exit"],
            "body_shown": "tier0 token rejected" in obs["stderr"]}


def judge_unauthorized(facts):
    if facts["control-read-404"]["api_requests"] != 1:
        return [result("C8", "controls", "blocked", "read control failed")]
    read, update = facts["read-401"], facts["update-401"]
    return [result(
        "C8", "unauthorized", "observed_behavior",
        "HTTP 401 is final: read %d request(s), update %d request(s), refresh attempts %d, "
        "exit %s/%s, server message shown: %s" % (
            read["api_requests"], update["api_requests"],
            read["refresh_requests"] + update["refresh_requests"], read["exit"],
            update["exit"], read["body_shown"] and update["body_shown"]),
        grade="unspecified", basis=[
            "agent spec documents 403/404 (not 401) for these routes",
            "auth.rs: refresh is time-based only; no code path refreshes on 401"],
        question="Should a 401 with a refreshable saved sign-in trigger one refresh and "
                 "retry?")]


# ---------------------------------------------------------------- C9 token refresh

def _token(expires_in=3600, refresh="tier0-rotated-refresh"):
    return {"status": 200, "json": {"access_token": "tier0-refreshed-token",
                                    "token_type": "Bearer", "expires_in": expires_in,
                                    "refresh_token": refresh}}


def _refresh_scenarios():
    get = ["agent", "managed-get", D]
    api = {("GET", AGENT + "/managed"): [{"status": 404, "json": ERROR_BODY}]}
    expired = saved_credentials(expires_in=-30)

    def token(reply):
        return {**api, ("POST", "/oauth2/token"): [reply]}
    return [
        Scenario("control-fresh-token", "control", get, token(_token()),
                 auth=saved_credentials()),
        Scenario("expired-refresh-ok", "probe", get, token(_token()), auth=expired),
        Scenario("expired-refresh-zero-lifetime", "probe", get, token(_token(expires_in=0)),
                 auth=expired),
        Scenario("expired-refresh-rejected", "probe", get,
                 token({"status": 401, "json": {"error": "invalid_grant"}}), auth=expired),
        Scenario("expired-without-refresh-token", "probe", get, token(_token()),
                 auth=saved_credentials(expires_in=-30, refresh="")),
    ]


def _stored(obs):
    text = (obs.get("credentials_after") or {}).get("text", "")
    values = {}
    for line in text.splitlines():
        if " = " in line:
            key, value = line.split(" = ", 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def refresh_facts(obs):
    token = to(obs, "POST", "/oauth2/token")
    api = [r for r in obs["requests"] if r["path"] != "/oauth2/token"]
    stored = _stored(obs)
    lifetime = None
    if stored.get("expires_at", "").lstrip("-").isdigit():
        delta = int(stored["expires_at"]) - obs["credentials_after"]["read_at"]
        lifetime = next((b for b in (3600, 900) if abs(delta - b) < 120), "other")
    body = token[0]["body"] if token and isinstance(token[0]["body"], dict) else {}
    return {"refresh_requests": len(token), "api_requests": len(api),
            "authorization": api[0]["headers"].get("authorization") if api else None,
            "grant": [body.get(k) for k in ("grant_type", "client_id", "refresh_token")]
            if token else None,
            "stored_refresh_token": stored.get("refresh_token"),
            "stored_lifetime": lifetime,
            "mode": (obs.get("credentials_after") or {}).get("mode"),
            "refresh_failed_message": "Token refresh failed" in obs["stderr"]}


def judge_refresh(facts):
    fresh = facts["control-fresh-token"]
    if not (fresh["refresh_requests"] == 0 and fresh["authorization"] == "Bearer " +
            tier0.SAVED_TOKEN):
        return [result("C9", "controls", "blocked", "fresh-token control failed")]
    ok, zero = facts["expired-refresh-ok"], facts["expired-refresh-zero-lifetime"]
    bad, none = facts["expired-refresh-rejected"], facts["expired-without-refresh-token"]
    out = [result(
        "C9", "refresh",
        "observed_behavior",
        "expired sign-in: %d refresh request to the configured (loopback) OAuth host with "
        "(grant_type, client_id, refresh_token) = %s, then the API call used %s; rotated "
        "refresh token stored: %s; lifetime %s; file mode %s. expires_in 0 stored lifetime "
        "%s. Rejected refresh: %d API requests, message %s." % (
            ok["refresh_requests"], ok["grant"], ok["authorization"],
            ok["stored_refresh_token"] == "tier0-rotated-refresh", ok["stored_lifetime"],
            ok["mode"], zero["stored_lifetime"], bad["api_requests"],
            bad["refresh_failed_message"]),
        grade="unspecified", basis=[
            "auth.rs maybe_refresh_oauth_token / access_token_expires_at (900 s fallback)",
            "openapi.json /oauth2/token: 200 'Tokens issued', 401 'Invalid authorization "
            "code or refresh token'"])]
    out.append(result(
        "C9", "expired-without-refresh-token",
        "contract_question" if none["refresh_requests"] == 0 and none["authorization"] ==
        "Bearer " + tier0.SAVED_TOKEN else "observed_behavior",
        "an expired saved sign-in with an empty refresh token was sent as-is (%s, %d refresh "
        "attempts) instead of asking the user to sign in again" % (
            none["authorization"], none["refresh_requests"]),
        grade="inferred", basis=["auth.rs maybe_refresh_oauth_token returns early when the "
                                 "refresh token is empty"],
        question="Should an expired sign-in that cannot be refreshed fail locally with a "
                 "sign-in prompt?",
        impact="the user sees a server authorization error instead of 'sign in again'"))
    return out


# ---------------------------------------------------------------- C10 undocumented 2xx

def _undocumented_scenarios():
    get = ["agent", "managed-get", D]
    status = ["agent", "cloud", "deployment", "status", D]
    accepted = {"status": 202, "json": {"data": {"accepted": True}},
                "headers": {"x-request-id": REQUEST_ID}}
    out = []
    for name, args, path in (("managed-get", get, AGENT + "/managed"),
                             ("cloud-status", status, CLOUD)):
        out.append(Scenario("%s-202" % name, "probe", args, {("GET", path): [accepted]}))
        out.append(Scenario("%s-404-control" % name, "control", args,
                            {("GET", path): [{"status": 404, "json": ERROR_BODY}]}))
    return out


def undocumented_facts(obs):
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "contract_mismatch": "client_contract_mismatch" in obs["stderr"],
            "request_id": REQUEST_ID in obs["stderr"], "stdout_empty": not obs["stdout"],
            "error": first_error_line(obs)[:120]}


def judge_undocumented(facts):
    if not all(facts["%s-404-control" % n]["exit"] == 1 for n in ("managed-get",
                                                                  "cloud-status")):
        return [result("C10", "controls", "blocked", "404 controls did not fail")]
    managed, cloud = facts["managed-get-202"], facts["cloud-status-202"]
    return [result(
        "C10", "undocumented-success", "observed_behavior",
        "an undocumented 202 on managed-get: exit %s, contract-mismatch message %s, request "
        "id shown %s; on cloud deployment status: exit %s, contract-mismatch message %s, "
        "first line %r" % (managed["exit"], managed["contract_mismatch"],
                           managed["request_id"], cloud["exit"], cloud["contract_mismatch"],
                           cloud["error"]),
        grade="unspecified", basis=[
            "agent spec GET managed documents 200/403/404; cloud spec GET deployment "
            "documents 200/404",
            "agent.rs anyhow_from_seren_error: undocumented 2xx -> client_contract_mismatch "
            "with request id (managed-* and run start)",
            "agent.rs cloud_status uses a plain error mapping"],
        question="Should cloud-* commands report contract drift the way managed-* do?")]


# ---------------------------------------------------------------- C11 managed delete output

def _action(status, lifecycle, egress="muted", generation=2):
    # agent spec DataResponse_CloudDeploymentActionStatusResponse: all required fields.
    return {"status": 200, "json": {"data": {
        "status": status, "desired_lifecycle_state": lifecycle,
        "desired_egress_state": egress, "control_generation": generation,
        "confirmation": None}}, "headers": {"x-request-id": REQUEST_ID}}


def _delete_scenarios():
    delete = ["agent", "managed-delete", D]
    start = ["agent", "managed-start", D]
    done = _action("deleted", "stopped")
    pending = _action("deleting", "stopping")
    return [
        Scenario("control-start-json", "control", ["-o", "json"] + start,
                 {("POST", AGENT + "/start"): [_action("starting", "running", "enabled")]}),
        Scenario("control-delete-404", "control", delete,
                 {("DELETE", AGENT): [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-delete-table", "control", delete, {("DELETE", AGENT): [done]}),
        Scenario("delete-json", "probe", ["-o", "json"] + delete, {("DELETE", AGENT): [done]}),
        Scenario("delete-not-converged", "probe", delete, {("DELETE", AGENT): [pending]}),
    ]


def _json_stdout(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def delete_facts(obs):
    body = _json_stdout(obs["stdout"])
    data = body.get("data") if isinstance(body, dict) else None
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "stdout_json": body is not None,
            "json_status": data.get("status") if isinstance(data, dict) else None,
            "says_deleted": "deleted." in obs["stdout"],
            "status_shown": any(s in obs["stdout"] for s in ("deleting", "stopping"))}


def judge_delete(facts):
    start, missing = facts["control-start-json"], facts["control-delete-404"]
    table = facts["control-delete-table"]
    if not (start["stdout_json"] and start["json_status"] == "starting" and
            missing["exit"] == 1 and table["exit"] == 0 and table["requests"] == 1):
        return [result("C11", "controls", "blocked",
                       "start -o json, delete 404 or delete table control failed")]
    js, pending = facts["delete-json"], facts["delete-not-converged"]
    defect = js["requests"] == 1 and js["exit"] == 0 and not js["stdout_json"]
    out = [result(
        "C11", "delete-json-output",
        "reproduced_client_defect" if defect else "contract_met",
        "-o json agent managed-delete after a 200 ActionStatus reply: exit %s, stdout is "
        "JSON %s, human 'deleted.' line %s (managed-start -o json with the same reply "
        "schema printed JSON)" % (js["exit"], js["stdout_json"], js["says_deleted"]),
        grade="stated", basis=[
            "main.rs 16-18: global '-o/--format' 'Output format (json or table)'",
            "cli/README.md 767-773: '-o json' for machine-readable output",
            "agent spec DELETE /deployments/{id} 200 'Managed deployment deleted': "
            "DataResponse_CloudDeploymentActionStatusResponse",
            "agent.rs 6325-6345: managed_agent_delete discards the reply and ignores "
            "ctx.format; managed_agent_start/stop print the payload under Json",
            "nearby: cloud_environment_delete (agent.rs 6955-6961) also prints only a human "
            "line, but its spec reply is 204 with no body; managed-delete's 200 has one"],
        impact="hypothetical: a script parsing -o json output of managed-delete fails on a "
               "successful delete; fake-API reproduction only")]
    out.append(result(
        "C11", "delete-not-converged",
        "contract_question" if pending["says_deleted"] and not pending["status_shown"]
        else "not_reproduced",
        "a 200 ActionStatus with status 'deleting' and desired_lifecycle_state 'stopping' "
        "printed 'deleted.' %s, status shown %s, exit %s" % (
            pending["says_deleted"], pending["status_shown"], pending["exit"]),
        grade="inferred", basis=[
            "agent spec CloudDeploymentActionStatusResponse.status is a free string; "
            "the delete status vocabulary and convergence are not documented",
            "agent.rs 6332-6344: reply discarded, 'deleted.' printed unconditionally"],
        question="Should managed-delete report the returned status instead of always "
                 "'deleted.'?",
        impact="hypothetical: an operator reads a pending teardown as complete, only if "
               "the service returns non-final delete statuses"))
    return out


# ---------------------------------------------------------------- C12 mutation outcomes

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _mutation(state, target="controller", deferral=None, reason=None, code=200,
              lifecycle="running", status="running"):
    # agent spec DataResponse_ManagedAgentDeploymentMutationResponse: every required field
    # of CloudDeploymentSummary and ManagedRuntimeReconciliationSummary, enum values only.
    deployment = {
        "id": D, "organization_id": G, "user_id": G, "name": "tier0-name",
        "skill_slug": "tier0-skill", "compute_backend": "aws_container",
        "runtime_kind": "python", "mode": "always_on", "status": status,
        "desired_lifecycle_state": lifecycle, "desired_egress_state": "enabled",
        "control_generation": 2, "code_bundle_hash": "tier0-hash",
        "orchestration_mode": "llm", "requirements": None, "visibility": "open",
        "created_at": T, "updated_at": T, "active_revision_id": R}
    action = {
        "id": R, "request_id": R, "deployment_id": D, "revision_id": R, "state": state,
        "target": target, "control_generation": 2,
        "desired_runtime_fingerprint": "tier0-fingerprint", "attempt_count": 1,
        "status_path": AGENT + "/managed/actions/" + R, "accepted_at": T, "updated_at": T,
        "deferral": deferral, "reason": reason,
        "reason_detail": "tier0 detail" if deferral or reason else None}
    return {"status": code, "json": {"data": {"deployment": deployment,
                                              "reconciliation": action}}}


def _outcome_scenarios():
    update = ["agent", "managed-update", D, "--name", "tier0-name"]

    def run(name, role, sends, lookups=(_mutation("applied"),), deadline=10):
        return Scenario(name, role, update, {("PATCH", AGENT + "/managed"): list(sends),
                                             ("GET", ACTIONS): list(lookups)},
                        deadline=deadline)
    def waiting(code):  # PATCH documents 200/202; the action GET documents 200 only
        return _mutation("pending", deferral="awaiting_runtime_start", code=code,
                         lifecycle="stopped", status="stopped")
    return [
        run("control-applied", "control", [_mutation("applied")]),
        run("control-pending-then-applied", "control", [_mutation("pending", code=202)]),
        run("failed", "probe", [_mutation("failed", reason="runtime_rejected")]),
        run("superseded", "probe", [_mutation("superseded", reason="revision_superseded")]),
        # Lookups keep answering the same waiting pending; the CLI is killed at 15 s.
        run("deferred-pending", "probe", [waiting(202)], [waiting(200)], deadline=15),
    ]


def outcome_facts(obs):
    sends = to(obs, "PATCH", AGENT + "/managed")
    lookups = [r for r in obs["requests"] if r["path"].startswith(ACTIONS[:-1])]
    keys = {r["headers"].get("idempotency-key") for r in sends}
    body = _json_stdout(obs["stdout"])
    data = body.get("data") if isinstance(body, dict) else None
    action = data.get("reconciliation") if isinstance(data, dict) else None
    text = obs["stdout"] + obs["stderr"]
    return {"sends": len(sends), "lookups": len(lookups),
            "kept_polling": len(lookups) >= 3,
            "lookup_uses_key": bool(lookups) and None not in keys and
            {r["path"].rsplit("/", 1)[1] for r in lookups} == keys,
            "killed": obs["killed_by_supervisor"],
            "exit": None if obs["killed_by_supervisor"] else obs["exit"],
            "printed_state": action.get("state") if isinstance(action, dict) else None,
            "deferral_shown": "awaiting_runtime_start" in text,
            "output_empty": not text.strip(),
            "error": UUID.sub("<id>", first_error_line(obs))[:120]}


def judge_outcomes(facts):
    ok, polled = facts["control-applied"], facts["control-pending-then-applied"]
    if not (ok["sends"] == 1 and ok["lookups"] == 0 and ok["exit"] == 0 and
            ok["printed_state"] == "applied" and polled["exit"] == 0 and
            polled["lookups"] == 1 and polled["lookup_uses_key"] and
            polled["printed_state"] == "applied"):
        return [result("C12", "controls", "blocked",
                       "applied or pending-then-applied control failed: %s / %s" % (ok, polled))]
    failed, superseded, waiting = (facts["failed"], facts["superseded"],
                                   facts["deferred-pending"])
    reported = all(f["sends"] == 1 and f["exit"] not in (0, None) and
                   f["printed_state"] is None and "ended as %s: %s" % pair in f["error"]
                   for f, pair in ((failed, ("failed", "runtime_rejected")),
                                   (superseded, ("superseded", "revision_superseded"))))
    out = [result(
        "C12", "terminal-states", "contract_met" if reported else "contract_question",
        "managed-update whose action reply is failed / superseded: exit %s / %s, success "
        "JSON printed %s / %s, error %r / %r" % (
            failed["exit"], superseded["exit"], failed["printed_state"],
            superseded["printed_state"], failed["error"], superseded["error"]),
        grade="inferred", basis=[
            "agent spec ManagedRuntimeReconciliationState pending|applied|failed|superseded; "
            "ManagedRuntimeReconciliationFailure 'Stable redacted reason'",
            "agent.rs 290-297: failed/superseded -> '<op> request <id> ended as <state>: "
            "<reason>'"],
        question="Must a failed or superseded update action make managed-update fail?",
        impact="hypothetical: a script treats a rejected update as applied")]
    silent = (waiting["killed"] and waiting["kept_polling"] and not waiting["deferral_shown"])
    out.append(result(
        "C12", "deferred-pending", "contract_question" if silent else "not_reproduced",
        "a 202 pending action with deferral awaiting_runtime_start (deployment stopped), "
        "re-read unchanged: still running at the 15 s deadline %s, polled the action 3+ "
        "times %s, deferral shown %s, any output %s" % (
            waiting["killed"], waiting["kept_polling"], waiting["deferral_shown"],
            not waiting["output_empty"]),
        grade="inferred", basis=[
            "agent spec ManagedRuntimeReconciliationDeferral: 'A deferral is distinct from a "
            "failure: the action is durable and admitted ... It resolves on its own once the "
            "named precondition clears (the Employee restarts ...), so the operator sees a "
            "specific reason instead of an opaque pending'",
            "agent.rs 301-328: polls every 2 s up to 600 s, then '<op> request <id> is still "
            "pending after 600 seconds' (exit 1); the deferral is not read",
            "nearby: agent.rs 5598-5617 proposal apply reports a pending rollout as recorded "
            "with a next_step note instead of waiting"],
        question="Should managed-update stop waiting and report the deferral (exit 0, the "
                 "revision is admitted) instead of polling silently and failing after 600 s?",
        impact="hypothetical: an update to a stopped Employee waits ten minutes and exits 1 "
               "although the spec calls the action admitted; the 600 s end is from source, "
               "not observed (lab deadline 120 s)"))
    return out


# ---------------------------------------------------------------- C13 auth diagnostics

REJECTED = {"status": 401, "json": {"error": {"code": "tier0_unauthorized",
                                              "message": "tier0 token rejected"}}}


def _diagnostic_scenarios():
    # Every API route answers the same fixed 401, whichever bearer arrives; `auth status`
    # sends no request (auth.rs 277-317 reads only the saved config).
    routes = {("GET", "/auth/me"): [REJECTED], ("GET", AGENT + "/managed"): [REJECTED],
              ("POST", "/oauth2/token"): [FIXED_400]}
    status, me, get = ["auth", "status"], ["me"], ["agent", "managed-get", D]
    saved, empty = saved_credentials(), {"SEREN_API_KEY": ""}
    return [
        Scenario("control-status-saved", "control", status, routes, auth=saved),
        Scenario("control-status-none", "control", status, routes, auth="none"),
        Scenario("control-me-saved", "control", me, routes, auth=saved),
        Scenario("control-me-key", "control", me, routes),
        Scenario("control-get-saved", "control", get, routes, auth=saved),
        Scenario("status-empty-key-saved", "probe", status, routes, auth=saved,
                 extra_env=empty),
        Scenario("me-empty-key-saved", "probe", me, routes, auth=saved, extra_env=empty),
        Scenario("get-empty-key-saved", "probe", get, routes, auth=saved, extra_env=empty),
        Scenario("status-key-only", "probe", status, routes),
        Scenario("status-key-with-saved", "probe", status, routes, auth=saved,
                 extra_env={"SEREN_API_KEY": tier0.DUMMY_KEY}),
    ]


def diagnostic_facts(obs):
    api = [r for r in obs["requests"] if r["path"] != "/oauth2/token"]
    text = obs["stdout"] + obs["stderr"]
    kind = re.search(r"Auth Type: (\w[\w ]*)", obs["stdout"])
    return {"requests": len(api),
            "authorization": api[0]["headers"].get("authorization") if api else None,
            "exit": obs["exit"],
            "authenticated": "Authenticated" in obs["stdout"],
            "not_authenticated": "Not authenticated" in obs["stdout"],
            "auth_type": kind.group(1).strip() if kind else None,
            "names_override": "SEREN_API_KEY" in text or "--api-key" in text,
            # `me` prints the raw response including its volatile date header; cut it.
            "error": "" if obs["exit"] == 0 else
            re.sub(r", headers: .*", "", first_error_line(obs))}


def judge_diagnostics(facts):
    st_saved, st_none = facts["control-status-saved"], facts["control-status-none"]
    me_saved, me_key = facts["control-me-saved"], facts["control-me-key"]
    get_saved = facts["control-get-saved"]
    if not (st_saved["authenticated"] and st_saved["auth_type"] == "OAuth"
            and st_saved["requests"] == 0 and st_none["not_authenticated"]
            and me_saved["authorization"] == "Bearer " + tier0.SAVED_TOKEN
            and me_saved["exit"] not in (0, None)
            and me_key["authorization"] == "Bearer " + tier0.DUMMY_KEY
            and get_saved["authorization"] == "Bearer " + tier0.SAVED_TOKEN
            and get_saved["exit"] not in (0, None)):
        return [result("C13", "controls", "blocked", "auth status or 401 controls failed: "
                       "%s" % {k: v for k, v in facts.items() if k.startswith("control")})]
    st_empty, me_empty, get_empty = (facts["status-empty-key-saved"],
                                     facts["me-empty-key-saved"],
                                     facts["get-empty-key-saved"])
    empty_sent = (me_empty["authorization"] != me_saved["authorization"]
                  and get_empty["authorization"] != get_saved["authorization"])
    hidden = (empty_sent and st_empty["authenticated"]
              and not any(f["names_override"] for f in (st_empty, me_empty, get_empty)))
    out = [result(
        "C13", "empty-key-diagnostics", "contract_question" if hidden else "not_reproduced",
        ("with SEREN_API_KEY='' and a valid saved sign-in, `auth status` printed "
         "Authenticated, Auth Type %s (exit %s); `me` and `agent managed-get` sent "
         "Authorization %r / %r and, on the fixed 401, printed %r / %r (exit %s / %s), the "
         "same lines as with the saved token: %s / %s. No output named SEREN_API_KEY."
         % (st_empty["auth_type"], st_empty["exit"], me_empty["authorization"],
            get_empty["authorization"], me_empty["error"], get_empty["error"],
            me_empty["exit"], get_empty["exit"], me_empty["error"] == me_saved["error"],
            get_empty["error"] == get_saved["error"])) if hidden else
        "the empty SEREN_API_KEY override is either not used (empty bearer sent %s) or "
        "named in the CLI's diagnostics" % empty_sent,
        grade="inferred", basis=[
            "follow-up F-C4-empty-key-diagnostics (C4 empty-api-key: an empty "
            "SEREN_API_KEY overrides a valid saved sign-in with `Bearer `)",
            "cli/README.md 123-128 '### Check Status' lists `seren auth status` and "
            "`seren me` as the way to check authentication",
            "main.rs 24-26 --api-key 'API key for authentication (overrides stored "
            "credentials)', env SEREN_API_KEY; main.rs 2782 auth status 'Show "
            "authentication status'",
            "auth.rs 277-317 status() reads only the saved config; auth.rs 575-579 "
            "get_bearer_token returns the override verbatim",
            "fixture: the fake answers 401 to every bearer; the real server's reply to an "
            "empty bearer is not known"],
        question="Should `auth status` (or the 401 error) say that SEREN_API_KEY/--api-key "
                 "overrides the saved sign-in, at least when it is empty?",
        impact="hypothetical: a CI job or shell exporting SEREN_API_KEY= sees `auth status` "
               "report a valid OAuth sign-in while every command fails with the server's "
               "authorization error; nothing points at the variable")]
    key_only, key_saved = facts["status-key-only"], facts["status-key-with-saved"]
    ignored = key_only["not_authenticated"] and key_saved["auth_type"] == "OAuth"
    out.append(result(
        "C13", "status-ignores-env-key", "contract_question" if ignored else "not_reproduced",
        ("`auth status` ignores SEREN_API_KEY: with only the key set it printed Not "
         "authenticated (exit %s), although `me` sent that key (%s); with the key and a "
         "saved sign-in it printed Auth Type %s, while commands use the key"
         % (key_only["exit"], me_key["authorization"] == "Bearer " + tier0.DUMMY_KEY,
            key_saved["auth_type"])) if ignored else
        "`auth status` reflects the SEREN_API_KEY credential commands use",
        grade="inferred", basis=[
            "cli/README.md 112-128: '### API Key  export SEREN_API_KEY=your_seren_api_key' "
            "followed by '### Check Status  seren auth status / seren me' (not stated "
            "that status covers the environment key)",
            "main.rs 24-26: SEREN_API_KEY 'overrides stored credentials'",
            "auth.rs 277-317: status() reports Config::load() only; 'Not authenticated' "
            "branch prints \"Run 'seren auth login' to authenticate\""],
        question="Is `seren auth status` meant to report the credential commands will use "
                 "(including SEREN_API_KEY / --api-key), or only the saved sign-in?",
        impact="hypothetical: an API-key user following the README is told 'Not "
               "authenticated, run seren auth login' while their commands authenticate"))
    return out


# ---------------------------------------------------------------- C14 resources decode

def _resources(drop_compaction=False):
    # agent spec DataResponse_ManagedAgentDeploymentResources: required fields only, enum
    # values only, empty connector list (ManagedAgentConnectorResource never needed).
    memory = {"policy_configured": False, "semantic_memory_enabled": False,
              "graph_memory_enabled": False, "knowledge_enabled": False,
              "compaction_configured": False}
    if drop_compaction:
        del memory["compaction_configured"]
    flags = ("policy_configured", "skills_enabled", "browser_enabled", "audio_enabled",
             "realtime_sessions_enabled", "code_execution_enabled", "workflows_enabled",
             "eval_harness_enabled", "tool_error_recovery_enabled")
    return {"status": 200, "headers": {"x-request-id": REQUEST_ID}, "json": {"data": {
        "organization_id": G, "deployment_id": D, "generated_at": T,
        "deployment": {"name": "tier0", "agent_slug": "tier0", "status": "stopped",
                       "mode": "always_on", "visibility": "private",
                       "template": "workflow_agent", "approval_policy": "read_only",
                       "model_policy": "fast"},
        "runtime": {"compute_backend": "aws_container", "runtime_kind": "python",
                    "runtime_adapter": "seren_agent", "private_output_policy": "control_plane",
                    "runtime_policy_configured": False, "filesystem_policy_configured": False,
                    "network_policy_configured": False, "process_policy_configured": False,
                    "effective_cpu_request_millicores": 250, "effective_memory_request_mib": 512,
                    "deployment_network_policy_configured": False,
                    "alert_policy_configured": False},
        "connectors": [],
        "schedule": {"mode": "always_on", "eval_gate_configured": False,
                     "eval_gate_schedule_configured": False},
        "tools": {"tool_presets": ["live_data"], "resolved_tools": [],
                  "allowed_publisher_operations": [], "allowed_remote_agent_origins": [],
                  "tool_ref_count": 0, "credential_count": 0, "guardrail_count": 0},
        "publisher_credentials": {"status": "not_required", "required_credential_count": 0,
                                  "checked_at": T},
        "memory": memory,
        "capabilities": {k: False for k in flags}}}}


def _resources_scenarios():
    cmd = ["agent", "managed-deployment-resources", D]
    route = ("GET", AGENT + "/resources")
    return [
        Scenario("control-404", "control", cmd,
                 {route: [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("spec-minimal-json", "probe", ["-o", "json"] + cmd, {route: [_resources()]}),
        Scenario("spec-minimal-table", "probe", cmd, {route: [_resources()]}),
        Scenario("missing-compaction", "probe", cmd,
                 {route: [_resources(drop_compaction=True)]}),
    ]


def resources_facts(obs):
    body = _json_stdout(obs["stdout"])
    data = body.get("data") if isinstance(body, dict) else None
    memory = data.get("memory") if isinstance(data, dict) else None
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "stdout_json": body is not None,
            "json_compaction": isinstance(memory, dict) and "compaction_configured" in memory,
            "names_field": "compaction_configured" in obs["stderr"],
            "decode_error": "missing field" in obs["stderr"],
            "request_id_shown": REQUEST_ID in obs["stdout"] + obs["stderr"]}


def judge_resources(facts):
    missing = facts["control-404"]
    if not (missing["exit"] == 1 and missing["requests"] == 1):
        return [result("C14", "controls", "blocked", "404 control did not fail")]
    js, table, drop = (facts["spec-minimal-json"], facts["spec-minimal-table"],
                       facts["missing-compaction"])
    decodes = (js["exit"] == 0 and js["stdout_json"] and js["json_compaction"]
               and table["exit"] == 0)
    out = [result(
        "C14", "spec-reply-decodes", "contract_met" if decodes else "contract_question",
        "a reply with exactly the spec's required fields: -o json exit %s, JSON %s with "
        "memory.compaction_configured %s; table exit %s; decode error %s" % (
            js["exit"], js["stdout_json"], js["json_compaction"], table["exit"],
            js["decode_error"] or table["decode_error"]),
        grade="stated", basis=[
            "agent spec GET /deployments/{id}/resources 200: "
            "DataResponse_ManagedAgentDeploymentResources",
            "agent spec ManagedAgentMemoryResources required: policy_configured, "
            "semantic_memory_enabled, graph_memory_enabled, knowledge_enabled, "
            "compaction_configured",
            "agent.rs 4883-4908: managed_agent_deployment_resources prints the decoded payload"],
        question=None if decodes else
        "Is the fixture spec-conformant? If so, the pinned CLI cannot decode the published "
        "0.10.0 resources reply (review the fixture before treating this as a defect).")]
    rejected = drop["exit"] not in (0, None) and drop["requests"] == 1
    out.append(result(
        "C14", "missing-compaction",
        "observed_behavior" if rejected else "not_reproduced",
        "the same reply without the spec-required memory.compaction_configured: exit %s, "
        "'missing field' decode error %s, names the field %s, request id shown %s" % (
            drop["exit"], drop["decode_error"], drop["names_field"],
            drop["request_id_shown"]),
        grade="unspecified", basis=[
            "agent spec marks compaction_configured required; the client's handling of a "
            "nonconformant reply is not documented",
            "contract map 1.13 / docs/LIFECYCLE-SETUP.md: live managed-deployment-resources "
            "failed with 'missing field compaction_configured'"],
        impact="bounds the live symptom: if the spec reply decodes and only this omission "
               "reproduces the message, the live failure points at a reply that omits a "
               "spec-required field (fake API; production reply not observed here)"))
    return out


# ---------------------------------------------------------------- C15 run start output

ANSWER = "tier0-run-answer-marker"


def _invocation(code, status, ids=True):
    # cloud spec DataResponse_CloudDeploymentRunInvocationResponse: only `status` required;
    # `result` is a 'Backend-specific result payload' (any JSON), so a string is valid.
    data = {"status": status}
    if ids:
        data.update(run_id=R, execution_id="tier0-exec-1")
    if status == "completed":
        data["result"] = ANSWER
    return {"status": code, "json": {"data": data}, "headers": {"x-request-id": REQUEST_ID}}


def _start_output_scenarios():
    start = ["agent", "cloud", "run", "start", "--deployment-id", D, "--message", "tier0 hello"]
    route = ("POST", CLOUD + "/runs")
    return [
        Scenario("control-managed-start-json", "control", ["-o", "json", "agent",
                                                           "managed-start", D],
                 {("POST", AGENT + "/start"): [_action("starting", "running", "enabled")]}),
        Scenario("control-async-table", "control", start + ["--async"],
                 {route: [_invocation(202, "queued")]}),
        Scenario("control-404", "control", start + ["--async"],
                 {route: [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-sync-without-ids", "control", start,
                 {route: [_invocation(200, "completed", ids=False)]}),
        Scenario("async-json", "probe", ["-o", "json"] + start + ["--async"],
                 {route: [_invocation(202, "queued")]}),
        Scenario("sync-json", "probe", ["-o", "json"] + start,
                 {route: [_invocation(200, "completed")]}),
        Scenario("sync-table-result", "probe", start, {route: [_invocation(200, "completed")]}),
    ]


def start_output_facts(obs):
    body = _json_stdout(obs["stdout"])
    data = body.get("data") if isinstance(body, dict) else None
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "stdout_json": body is not None,
            "json_run_id": isinstance(data, dict) and data.get("run_id") == R,
            "accepted_printed": "Run ID: " + R in obs["stdout"],
            "answer_shown": ANSWER in obs["stdout"]}


def judge_start_output(facts):
    managed, table = facts["control-managed-start-json"], facts["control-async-table"]
    missing, bare = facts["control-404"], facts["control-sync-without-ids"]
    if not (managed["stdout_json"] and table["exit"] == 0 and table["accepted_printed"]
            and table["requests"] == 1 and missing["exit"] == 1 and missing["requests"] == 1
            and bare["exit"] == 0 and bare["answer_shown"]):
        return [result("C15", "controls", "blocked",
                       "managed-start -o json, table run start, 404 or id-less sync reply "
                       "control failed")]
    probes = [facts["async-json"], facts["sync-json"]]
    defect = all(p["requests"] == 1 and p["exit"] == 0 and not p["stdout_json"]
                 for p in probes)
    met = all(p["stdout_json"] and p["json_run_id"] for p in probes)
    out = [result(
        "C15", "run-start-json-output",
        "reproduced_client_defect" if defect else "contract_met" if met
        else "contract_question",
        "-o json agent cloud run start after a spec reply (202 queued with --async; 200 "
        "completed without): exit %s/%s, stdout is JSON %s/%s, human 'Run ID:' line %s/%s "
        "(managed-start -o json printed JSON)" % tuple(
            p[k] for k in ("exit", "stdout_json", "accepted_printed") for p in probes),
        grade="stated", basis=[
            "main.rs 16-18: global '-o/--format' 'Output format (json or table)'",
            "cli/README.md 16: 'Agent automation | Use stable command groups, `-o json`, and "
            "saved context so coding agents can inspect state, make changes, and verify "
            "results'",
            "cloud spec POST /deployments/{id}/runs 200/202: "
            "DataResponse_CloudDeploymentRunInvocationResponse (required: status)",
            "agent.rs 7694-7744: cloud_run never reads ctx.format; managed_agent_start "
            "(6253-6287) prints the payload under Json"],
        impact="hypothetical: a script or coding agent starting a run with -o json gets "
               "human text and must scrape it for the run id; fake-API reproduction only")]
    sync = facts["sync-table-result"]
    hidden = sync["exit"] == 0 and sync["accepted_printed"] and not sync["answer_shown"]
    out.append(result(
        "C15", "sync-result-hidden",
        "contract_question" if hidden else "not_reproduced",
        "run start without --async, 200 'Run completed' reply carrying run_id, execution_id "
        "and result: exit %s, 'Run ID:' line %s, result shown %s (the same reply without ids "
        "printed the body, result included)" % (
            sync["exit"], sync["accepted_printed"], sync["answer_shown"]),
        grade="inferred", basis=[
            "cloud spec POST /deployments/{id}/runs 200 'Run completed (always_on "
            "deployments)'; result: 'Backend-specific result payload.'",
            "cli/README.md 536-538: '# Invoke the deployment' run start --message 'Give me "
            "the latest BTC update.' (no --async); the README does not say where the answer "
            "appears",
            "main.rs 2265: --async 'Request async execution for always_on deployments "
            "(returns run_id + execution_id)'",
            "agent.rs 7708-7723: with run_id and execution_id the reply body is not printed"],
        question="Should a synchronous `cloud run start` show the completed run's result, or "
                 "is the user expected to fetch it with `cloud run get`?",
        impact="hypothetical: a user invoking a deployment synchronously sees only ids, not "
               "the answer, if the service returns ids with a completed result"))
    return out


# ---------------------------------------------------------------- C16 -o json sweep

ENVIRONMENTS = "/publishers/seren-cloud/environments"
ENV_NAME = "tier0-env-marker"


def _cloud_action(status, lifecycle):
    # cloud spec DataResponse_CloudDeploymentActionStatusResponse (json 9854-9884): status,
    # desired_lifecycle_state, desired_egress_state, control_generation required.
    return {"status": 200, "json": {"data": {
        "status": status, "desired_lifecycle_state": lifecycle,
        "desired_egress_state": "muted", "control_generation": 2, "confirmation": None}},
        "headers": {"x-request-id": REQUEST_ID}}


def _environments():
    # cloud spec DataResponse_Vec_CloudDeploymentEnvironment (json 18169, item 10246-10297):
    # every required field present.
    return {"status": 200, "json": {"data": [{
        "id": G, "organization_id": "7a000011-0000-4000-8000-000000000012",
        "user_id": "7a000013-0000-4000-8000-000000000014", "name": ENV_NAME,
        "docker_image": "python:3.12-slim", "setup_commands": [], "is_default": False,
        "description": None, "created_at": T, "updated_at": T}]},
        "headers": {"x-request-id": REQUEST_ID}}


def _json_sweep_scenarios():
    destroy = ["agent", "cloud", "deployment", "destroy", D]
    update = ["agent", "cloud", "deployment", "update-config", D, "--clear-alert-policy"]
    envs = ["agent", "cloud", "environment", "list"]
    gone = {("DELETE", CLOUD): [_cloud_action("deleted", "stopped")]}
    patched = {("PATCH", CLOUD): [_cloud_action("updated", "running")]}
    listed = {("GET", ENVIRONMENTS): [_environments()]}
    return [
        Scenario("control-managed-start-json", "control", ["-o", "json", "agent",
                                                           "managed-start", D],
                 {("POST", AGENT + "/start"): [_action("starting", "running", "enabled")]}),
        Scenario("control-destroy-404", "control", destroy,
                 {("DELETE", CLOUD): [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-update-table", "control", update, patched),
        Scenario("control-environments-table", "control", envs, listed),
        Scenario("destroy-json", "probe", ["-o", "json"] + destroy, gone),
        Scenario("update-config-json", "probe", ["-o", "json"] + update, patched),
        Scenario("environment-list-json", "probe", ["-o", "json"] + envs, listed),
    ]


def json_sweep_facts(obs):
    body = _json_stdout(obs["stdout"])
    sent = obs["requests"][0].get("body") if obs["requests"] else None
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "stdout_json": body is not None,
            "human_line": any(s in obs["stdout"] for s in ("destroyed.", "settings updated")),
            "env_name_shown": ENV_NAME in obs["stdout"],
            "clear_alert_sent": isinstance(sent, dict) and sent.get("clear_alert_policy") is True}


JSON_SWEEP = {
    "destroy-json": (
        "cloud deployment destroy", "DELETE /deployments/{id} 200 'Deployment deleted' "
        "(json 676-684)", "agent.rs 10115-10123: cloud_destroy discards the reply and prints "
        "'✓ Deployment {} destroyed.' without reading ctx.format",
        "a script destroying a deployment with -o json cannot parse the confirmation"),
    "update-config-json": (
        "cloud deployment update-config", "PATCH /deployments/{id} 200 'Config updated' "
        "(json 725-733)", "agent.rs 10871-10880: cloud_update_config discards the reply and "
        "prints 'Deployment settings updated for {}.' without reading ctx.format",
        "a script changing deployment settings with -o json cannot parse the outcome"),
    "environment-list-json": (
        "cloud environment list", "GET /environments 200 "
        "DataResponse_Vec_CloudDeploymentEnvironment (json 4620-4631)",
        "agent.rs 6802-6838: cloud_environment_list prints a fixed-width table without "
        "reading ctx.format (cloud_environment_get prints JSON)",
        "a script or coding agent inspecting environments with -o json must parse a table"),
}


def judge_json_sweep(facts):
    managed, missing = facts["control-managed-start-json"], facts["control-destroy-404"]
    update, envs = facts["control-update-table"], facts["control-environments-table"]
    if not (managed["stdout_json"] and missing["exit"] == 1 and missing["requests"] == 1
            and update["exit"] == 0 and update["requests"] == 1 and update["clear_alert_sent"]
            and envs["exit"] == 0 and envs["env_name_shown"] and not envs["stdout_json"]):
        return [result("C16", "controls", "blocked",
                       "managed-start -o json, destroy 404, table update-config or table "
                       "environment list control failed")]
    out = []
    for name, (command, reply, source, impact) in JSON_SWEEP.items():
        p = facts[name]
        if p["stdout_json"]:
            disposition = "contract_met"
        elif p["requests"] == 1 and p["exit"] == 0:
            disposition = "reproduced_client_defect"
        else:
            disposition = "not_reproduced"
        out.append(result(
            "C16", name.replace("-json", "-json-output"), disposition,
            "-o json agent %s after a spec 200 reply: exit %s, requests %s, stdout is JSON %s, "
            "human line %s, table row %s (managed-start -o json printed JSON)" % (
                command, p["exit"], p["requests"], p["stdout_json"], p["human_line"],
                p["env_name_shown"]),
            grade="stated", basis=[
                "main.rs 16-18: global '-o/--format' 'Output format (json or table)'",
                "cli/README.md 16: 'Agent automation | Use stable command groups, `-o json`, "
                "and saved context so coding agents can inspect state, make changes, and "
                "verify results'",
                "cloud spec " + reply, source,
                "follow-up F-C15-json-sweep (C11 managed-delete, C15 run start)"],
            impact="hypothetical: %s; fake-API reproduction only" % impact))
    return out


# ---------------------------------------------------------------- C17 deployment activity

STOP_REASON = "tier0_stop_reason_marker"
STATUS_MESSAGE = "tier0-status-message-marker"


def _activity():
    # agent spec DataResponse_ManagedAgentDeploymentActivityReport (json 10000-10037):
    # required fields only plus the nullable run diagnostics (ManagedAgentRunActivity,
    # json 11943-12046); enum values from CloudDeployment* schemas.
    run = {"run_id": R, "execution_id": "tier0-execution-1", "source": "dispatch_channel",
           "status": "failed", "compute_backend": "aws_container", "execution_time_ms": 10,
           "billed_duration_ms": 10, "inference_input_tokens": 0,
           "inference_output_tokens": 0, "compute_cost_usd": "0", "inference_cost_usd": "0",
           "artifact_count": 0, "started_at": T, "updated_at": T, "completed_at": T,
           "stop_reason": STOP_REASON, "status_message": STATUS_MESSAGE}
    counts = ("total_run_count", "failed_run_count")
    zero = ("completed_run_count", "running_run_count", "awaiting_approval_run_count",
            "cancelled_run_count", "blocked_run_count", "artifact_count",
            "inference_input_tokens", "inference_output_tokens")
    summary = dict({k: 1 for k in counts}, **{k: 0 for k in zero})
    summary.update(compute_cost_usd="0", inference_cost_usd="0", last_run_at=T)
    return {"status": 200, "headers": {"x-request-id": REQUEST_ID}, "json": {"data": {
        "organization_id": G, "deployment_id": D, "generated_at": T,
        "deployment": {"name": "tier0", "agent_slug": "tier0", "status": "stopped",
                       "mode": "always_on", "compute_backend": "aws_container",
                       "runtime_kind": "python"},
        "summary": summary, "runs": [run]}}}


def _activity_scenarios():
    cmd = ["agent", "managed-deployment-activity", D]
    route = ("GET", AGENT + "/activity")
    return [
        Scenario("control-404", "control", cmd,
                 {route: [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-table", "control", cmd, {route: [_activity()]}),
        Scenario("activity-json", "probe", ["-o", "json"] + cmd + ["--limit", "50"],
                 {route: [_activity()]}),
    ]


def activity_facts(obs):
    body = _json_stdout(obs["stdout"])
    data = body.get("data") if isinstance(body, dict) else None
    runs = data.get("runs") if isinstance(data, dict) else None
    run = runs[0] if isinstance(runs, list) and runs and isinstance(runs[0], dict) else {}
    query = obs["requests"][0].get("query", {}) if obs["requests"] else {}
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "stdout_json": body is not None,
            "json_stop_reason": run.get("stop_reason") == STOP_REASON,
            "json_status_message": run.get("status_message") == STATUS_MESSAGE,
            "query_limit": ",".join(query.get("limit", [])),
            "query_offset": ",".join(query.get("offset", [])),
            "run_row": R in obs["stdout"] and "failed" in obs["stdout"],
            "stop_reason_shown": STOP_REASON in obs["stdout"] + obs["stderr"],
            "status_message_shown": STATUS_MESSAGE in obs["stdout"] + obs["stderr"]}


def judge_activity(facts):
    missing, table, js = facts["control-404"], facts["control-table"], facts["activity-json"]
    if not (missing["exit"] == 1 and missing["requests"] == 1 and table["exit"] == 0
            and table["requests"] == 1 and table["run_row"]):
        return [result("C17", "controls", "blocked",
                       "404 control did not fail or the table view did not show the run")]
    decoded = (js["exit"] == 0 and js["stdout_json"] and js["json_stop_reason"]
               and js["json_status_message"])
    forwarded = (js["query_limit"] == "50" and js["query_offset"] == "0"
                 and table["query_limit"] == "20" and table["query_offset"] == "0")
    if decoded and forwarded:
        disposition = "contract_met"
    elif decoded:
        disposition = "reproduced_client_defect"
    else:
        disposition = "contract_question"
    out = [result(
        "C17", "activity-json", disposition,
        "-o json --limit 50 on a spec activity reply with one failed run: exit %s, JSON %s, "
        "stop_reason kept %s, status_message kept %s, query limit=%s offset=%s; table "
        "without flags sent limit=%s offset=%s" % (
            js["exit"], js["stdout_json"], js["json_stop_reason"], js["json_status_message"],
            js["query_limit"], js["query_offset"], table["query_limit"],
            table["query_offset"]),
        grade="stated", basis=[
            "agent spec GET /deployments/{id}/activity (json 458-497): query limit, offset; "
            "200 DataResponse_ManagedAgentDeploymentActivityReport",
            "agent spec ManagedAgentRunActivity (json 11943-12046): stop_reason and "
            "status_message nullable strings",
            "main.rs 1687-1696: --limit 'Max run activity entries to return' default 20, "
            "--offset 'Pagination offset' default 0",
            "agent.rs 4999-5026: managed_agent_deployment_activity prints the decoded payload"],
        question=None if decoded else
        "Is the fixture spec-conformant? If so, the pinned CLI does not pass the activity "
        "reply's run diagnostics through -o json (review the fixture first).",
        impact=None if disposition != "reproduced_client_defect" else
        "hypothetical: activity pages differ from what --limit/--offset ask for; fake API "
        "only")]
    hidden = not (table["stop_reason_shown"] or table["status_message_shown"])
    out.append(result(
        "C17", "table-failure-reason", "observed_behavior",
        "table view of the same reply: run row shown %s, stop_reason shown %s, "
        "status_message shown %s" % (table["run_row"], table["stop_reason_shown"],
                                     table["status_message_shown"]),
        grade="unspecified", basis=[
            "agent.rs 3702-3726: the table lists runs as 'started status source run "
            "artifacts tokens'; no documented table columns",
            "contract map 1.6: live failed runs carried stop_reason channel_binding_failure"],
        impact="hypothetical: a person reading the default table sees that a run failed but "
               "not why; -o json carries the reason" if hidden else
               "the table shows the failure reason"))
    return out


# ---------------------------------------------------------------- C18 runs list filters

ORG_RUNS = "/publishers/seren-cloud/runs"
ORG_DEPLOYMENTS = "/publishers/seren-cloud/deployments"
AFTER, BEFORE = "2026-10-01T00:00:00Z", "2026-10-13T23:59:59Z"
FILTER_FLAGS = ["--limit", "7", "--offset", "3", "--status", "failed,cancelled",
                "--status", "timeout", "--compute-backend", "daytona", "--source", "scheduler",
                "--has-artifacts", "--started-after", AFTER, "--started-before", BEFORE,
                "--q", "tier0-needle"]
# main.rs 2492-2526 flag -> cloud spec query name (json 3081-3172, 6125-6216).
FILTER_QUERY = {"limit": "7", "offset": "3", "compute_backend": "daytona",
                "source": "scheduler", "has_artifacts": "true", "started_after": AFTER,
                "started_before": BEFORE, "q": "tier0-needle"}
FILTER_STATUSES = ["cancelled", "failed", "timeout"]


def _empty_list():
    # cloud spec DataResponse_Vec_CloudDeploymentRunEvent (json 18329) and
    # DataResponse_Vec_CloudDeploymentView (json 18518): only `data` (an array) is required.
    return {"status": 200, "json": {"data": []}, "headers": {"x-request-id": REQUEST_ID}}


def _runs_list_scenarios():
    scoped = ["agent", "cloud", "runs", "list", "--deployment-id", D]
    org = ["agent", "cloud", "runs", "list"]
    scoped_routes = {("GET", CLOUD + "/runs"): [_empty_list()]}
    org_routes = {("GET", ORG_DEPLOYMENTS): [_empty_list()], ("GET", ORG_RUNS): [_empty_list()]}
    return [
        Scenario("control-404", "control", scoped,
                 {("GET", CLOUD + "/runs"): [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-scoped-defaults", "control", scoped, scoped_routes),
        Scenario("control-org-defaults", "control", org, org_routes),
        Scenario("scoped-filters", "probe", ["-o", "json"] + scoped + FILTER_FLAGS,
                 scoped_routes),
        Scenario("org-filters", "probe", ["-o", "json"] + org + FILTER_FLAGS, org_routes),
    ]


def runs_list_facts(obs):
    runs = [r for r in obs["requests"] if r["method"] == "GET"
            and r["path"] in (CLOUD + "/runs", ORG_RUNS)]
    query = runs[0].get("query", {}) if runs else {}
    statuses = sorted(s for v in query.get("status", []) for s in v.split(",") if s)
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "stdout_json": _json_stdout(obs["stdout"]) is not None,
            "runs_requests": len(runs),
            "deployment_list_requests": len(to(obs, "GET", ORG_DEPLOYMENTS)),
            "query": {k: ",".join(v) for k, v in sorted(query.items()) if k != "status"},
            "statuses": statuses,
            "no_runs_line": "No runs found" in obs["stdout"]}


def _filter_mismatches(fact):
    got = fact["query"]
    wrong = ["%s=%s (expected %s)" % (k, got.get(k), v) for k, v in FILTER_QUERY.items()
             if got.get(k) != v]
    extra = ["unexpected %s=%s" % (k, v) for k, v in got.items() if k not in FILTER_QUERY]
    if fact["statuses"] != FILTER_STATUSES:
        wrong.append("status=%s (expected %s)" % (",".join(fact["statuses"]),
                                                   ",".join(FILTER_STATUSES)))
    return wrong + extra


def judge_runs_list(facts):
    missing, scoped = facts["control-404"], facts["control-scoped-defaults"]
    org = facts["control-org-defaults"]
    defaults = {"limit": "50", "offset": "0"}
    if not (missing["exit"] == 1 and missing["requests"] == 1
            and scoped["exit"] == 0 and scoped["runs_requests"] == 1
            and scoped["query"] == defaults and not scoped["statuses"]
            and scoped["no_runs_line"] and org["exit"] == 0 and org["runs_requests"] == 1
            and org["query"] == defaults and not org["statuses"] and org["no_runs_line"]):
        return [result("C18", "controls", "blocked",
                       "404 control did not fail, or a runs list without flags did not send "
                       "exactly limit=50 offset=0 and print 'No runs found'")]
    out = []
    for name, route in (("scoped-filters", "GET /deployments/{id}/runs (json 3063-3172)"),
                        ("org-filters", "GET /runs (json 6117-6216)")):
        p = facts[name]
        wrong = _filter_mismatches(p)
        if p["runs_requests"] != 1 or p["exit"] != 0:
            disposition = "contract_question"
        elif wrong:
            disposition = "reproduced_client_defect"
        else:
            disposition = "contract_met"
        out.append(result(
            "C18", name, disposition,
            "-o json runs list%s with every advertised filter flag: exit %s, runs requests "
            "%s, JSON %s; mismatched query: %s" % (
                " --deployment-id" if name.startswith("scoped") else "", p["exit"],
                p["runs_requests"], p["stdout_json"], "; ".join(wrong) or "none"),
            grade="stated", basis=[
                "main.rs 2492-2526: runs list --limit (default 50), --offset, --status "
                "'repeat or comma-separate', --compute-backend, --source, --has-artifacts, "
                "--started-after, --started-before, --q",
                "cloud spec " + route + ": query limit, offset, status 'Repeat or "
                "comma-separate statuses', compute_backend, source, has_artifacts, "
                "started_after, started_before, q",
                "agent.rs 10136-10165 cloud_runs, 10401-10442 cloud_all_runs: positional SDK "
                "arguments"],
            question=None if disposition != "contract_question" else
            "The filtered list did not complete against an empty spec reply; review the "
            "fixture before drawing any conclusion.",
            impact=None if disposition != "reproduced_client_defect" else
            "hypothetical: a filtered run history silently answers a different question "
            "than asked; fake API only"))
    return out


# ---------------------------------------------------------------- C19 run approve/reject

RUNS = "/publishers/seren-cloud/runs/" + R
CHECKPOINT = "tier0-checkpoint-1"
EXEC = "tier0-exec-1"
APPROVAL_IDS = ["tier0-approval-1", "tier0-approval-2"]


def _run_event(status, execution_id=EXEC):
    # cloud spec CloudDeploymentRunEvent (json 10556-10720): every required field, typed.
    data = {"id": R, "deployment_id": D, "organization_id": G, "user_id": G,
            "execution_id": execution_id, "compute_backend": "daytona", "source": "cli",
            "status": status, "execution_time_ms": 0, "billed_duration_ms": 0,
            "requested_cpu_millicores": 0, "requested_memory_mib": 0,
            "compute_cost_atomic": 0, "compute_cost_usd": "0", "inference_input_tokens": 0,
            "inference_output_tokens": 0, "inference_cost_atomic": 0,
            "inference_cost_usd": "0", "started_at": T, "invocation_payload": {
                "message": "tier0 hello"}, "metadata": {}, "created_at": T, "updated_at": T}
    return {"status": 200, "json": {"data": data}, "headers": {"x-request-id": REQUEST_ID}}


def _pending(awaiting=True):
    # cloud spec CloudRunPendingApprovalsResponse (json 13205-13233): run_id, status required;
    # CloudRunPendingApproval (13153-13203): id, tool required.
    data = {"run_id": R, "status": "awaiting_approval" if awaiting else "running"}
    if awaiting:
        data.update(checkpoint_id=CHECKPOINT, pending_approvals=[
            {"id": i, "tool": "tier0_tool", "reason": "tier0 approval"} for i in APPROVAL_IDS])
    return {"status": 200, "json": {"data": data}}


def _approval_scenarios():
    def routes(awaiting=True, resumed=EXEC):
        return {("GET", RUNS): [_run_event("awaiting_approval" if awaiting else "running")],
                ("GET", RUNS + "/pending_approvals"): [_pending(awaiting)],
                ("POST", RUNS + "/resume"): [_run_event("running", resumed)]}
    approve = ["agent", "cloud", "run", "approve", R]
    reject = ["agent", "cloud", "run", "reject", R]
    return [
        Scenario("control-404", "control", approve,
                 {("GET", RUNS): [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-not-awaiting", "control", ["-o", "json"] + approve,
                 routes(awaiting=False)),
        Scenario("approve-all", "probe", ["-o", "json"] + approve, routes()),
        Scenario("reject-all", "probe", reject, routes()),
        Scenario("identity-mismatch", "observation", ["-o", "json"] + approve,
                 routes(resumed="tier0-exec-other")),
    ]


def approval_facts(obs):
    resumes = to(obs, "POST", RUNS + "/resume")
    body = (resumes[0]["body"] or {}) if resumes else {}
    out = _json_stdout(obs["stdout"])
    decisions = body.get("approval_decisions") if isinstance(body, dict) else None
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "resume_requests": len(resumes),
            "checkpoint": body.get("resume_checkpoint_id") if isinstance(body, dict) else None,
            "decisions": sorted([d.get("id"), d.get("decision")] for d in decisions)
            if isinstance(decisions, list) else None,
            "resolved": out.get("resolved") if isinstance(out, dict) else None,
            "done_line": "pending approvals for run " + R in obs["stdout"],
            "not_awaiting_message": "not currently awaiting approval" in obs["stdout"],
            "mismatch_message": "did not match the suspended run identity" in obs["stderr"]}


def judge_approvals(facts):
    missing, idle = facts["control-404"], facts["control-not-awaiting"]
    if not (missing["exit"] == 1 and missing["resume_requests"] == 0
            and idle["exit"] == 0 and idle["resume_requests"] == 0
            and idle["resolved"] is False and idle["not_awaiting_message"]):
        return [result("C19", "controls", "blocked",
                       "404 control did not fail without a resume, or a run that is not "
                       "awaiting approval was resumed or not reported as such")]
    out = []
    for name, decision in (("approve-all", "approve"), ("reject-all", "reject")):
        p = facts[name]
        expected = sorted([i, decision] for i in APPROVAL_IDS)
        if p["resume_requests"] != 1 or p["exit"] != 0:
            disposition = "contract_question"
        elif p["checkpoint"] != CHECKPOINT or p["decisions"] != expected:
            disposition = "reproduced_client_defect"
        else:
            disposition = "contract_met"
        out.append(result(
            "C19", name, disposition,
            "run %s with two pending approvals at checkpoint %s: exit %s, resume requests %s, "
            "sent checkpoint %s, decisions %s (expected %s)" % (
                decision, CHECKPOINT, p["exit"], p["resume_requests"], p["checkpoint"],
                p["decisions"], expected),
            grade="stated", basis=[
                "main.rs 2376-2385: '%s all current pending approvals for a run and resume "
                "it'" % decision.capitalize(),
                "cloud spec CloudDeploymentRunResumeRequest (json 10853-10884): "
                "approval_decisions 'Complete decision set for the checkpoint's current "
                "pending approvals'; resume_checkpoint_id 'must name the exact checkpoint'",
                "cloud spec GET /runs/{run_id}/pending_approvals, POST /runs/{run_id}/resume "
                "(json 6642-6748)"],
            question=None if disposition != "contract_question" else
            "The decision did not complete against spec-shaped replies; review the fixture "
            "before drawing any conclusion.",
            impact=None if disposition != "reproduced_client_defect" else
            "hypothetical: an approval decision applies to a different set of tool calls "
            "than the person decided; fake API only"))
    m = facts["identity-mismatch"]
    out.append(result(
        "C19", "identity-mismatch", "observed_behavior",
        "resume reply naming another execution: resume requests %s, exit %s, mismatch "
        "message %s, JSON on stdout %s" % (m["resume_requests"], m["exit"],
                                          m["mismatch_message"], m["resolved"] is not None),
        grade="unspecified", basis=[
            "shared.rs 1906-1918 validate_cloud_approval_resume_identity runs after the "
            "resume POST (agent.rs 7619-7629); the spec says only 'Resume the exact managed "
            "run' (json 6688) and states no client reaction"],
        impact="hypothetical: if the server resumed, a failing exit after the decision may "
               "lead a script to decide again; the server's reaction to a repeat is "
               "unknown (409 'Run does not require durable resume' is documented)"
        if m["resume_requests"] == 1 and m["exit"] != 0 else None))
    return out


# ---------------------------------------------------------------- C20 run events filters

EVENT_FLAGS = ["--item-id", "tier0-item-1", "--kind", "tool_call_completed", "--limit", "9",
               "--offset", "4", "--q", "tier0-needle"]
# main.rs 2326-2348 flag -> cloud spec query name (json 3515-3561, 6572-6618).
EVENT_QUERY = {"item_id": "tier0-item-1", "kind": "tool_call_completed", "limit": "9",
               "offset": "4", "q": "tier0-needle"}


def _events_list():
    # cloud spec DataResponse_Vec_CloudRunOutputEventEnvelope (json 19146-19151): only `data`.
    return {"status": 200, "json": {"data": []}, "headers": {"x-request-id": REQUEST_ID}}


def _run_events_scenarios():
    scoped = ["agent", "cloud", "run", "events", "--deployment-id", D, R]
    org = ["agent", "cloud", "run", "events", R]
    scoped_routes = {("GET", CLOUD + "/runs/" + R + "/events"): [_events_list()]}
    org_routes = {("GET", RUNS + "/events"): [_events_list()]}
    return [
        Scenario("control-404", "control", scoped,
                 {("GET", CLOUD + "/runs/" + R + "/events"): [
                     {"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-scoped-defaults", "control", ["-o", "json"] + scoped, scoped_routes),
        Scenario("control-org-defaults", "control", ["-o", "json"] + org, org_routes),
        Scenario("scoped-filters", "probe", ["-o", "json"] + scoped + EVENT_FLAGS,
                 scoped_routes),
        Scenario("org-filters", "probe", ["-o", "json"] + org + EVENT_FLAGS, org_routes),
    ]


def run_events_facts(obs):
    gets = [r for r in obs["requests"] if r["method"] == "GET"
            and r["path"] in (CLOUD + "/runs/" + R + "/events", RUNS + "/events")]
    query = gets[0].get("query", {}) if gets else {}
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "stdout_json": _json_stdout(obs["stdout"]) is not None,
            "events_requests": len(gets),
            "query": {k: ",".join(v) for k, v in sorted(query.items())}}


def judge_run_events(facts):
    missing = facts["control-404"]
    defaults = {"limit": "100", "offset": "0"}
    controls = [facts["control-scoped-defaults"], facts["control-org-defaults"]]
    if not (missing["exit"] == 1 and missing["requests"] == 1
            and all(c["exit"] == 0 and c["events_requests"] == 1 and c["requests"] == 1
                    and c["query"] == defaults and c["stdout_json"] for c in controls)):
        return [result("C20", "controls", "blocked",
                       "404 control did not fail, or run events without flags did not send "
                       "exactly limit=100 offset=0 and print JSON")]
    out = []
    for name, route in (("scoped-filters",
                         "GET /deployments/{id}/runs/{run_id}/events (json 3487-3583)"),
                        ("org-filters", "GET /runs/{run_id}/events (json 6554-6641)")):
        p = facts[name]
        got = p["query"]
        wrong = ["%s=%s (expected %s)" % (k, got.get(k), v) for k, v in EVENT_QUERY.items()
                 if got.get(k) != v]
        wrong += ["unexpected %s=%s" % (k, v) for k, v in got.items() if k not in EVENT_QUERY]
        if p["events_requests"] != 1 or p["exit"] != 0:
            disposition = "contract_question"
        elif wrong:
            disposition = "reproduced_client_defect"
        else:
            disposition = "contract_met"
        out.append(result(
            "C20", name, disposition,
            "-o json run events%s with every advertised filter flag: exit %s, events "
            "requests %s, JSON %s; mismatched query: %s" % (
                " --deployment-id" if name.startswith("scoped") else "", p["exit"],
                p["events_requests"], p["stdout_json"], "; ".join(wrong) or "none"),
            grade="stated", basis=[
                "main.rs 2326-2348: run events --item-id 'Filter by tool/output item ID', "
                "--kind 'Filter by event kind', --limit (default 100), --offset (default 0), "
                "--q 'Case-insensitive search query'",
                "cloud spec " + route + ": query limit, offset, kind, item_id, q",
                "agent.rs 9068-9114: item_id and kind are both Option<&str> passed "
                "positionally to the generated SDK call"],
            question=None if disposition != "contract_question" else
            "The filtered event list did not complete against an empty spec reply; review "
            "the fixture before drawing any conclusion.",
            impact=None if disposition != "reproduced_client_defect" else
            "hypothetical: a filtered event listing silently answers a different question "
            "than asked; fake API only"))
    return out


# ---------------------------------------------------------------- C21 failed sync run start

FAILED_MARKER = "tier0-run-failure-marker"


def _failed_invocation(ids=True):
    # Schema-valid 200 reply (only `status` required; `exit_code` is a documented field);
    # whether the service ever answers a synchronous run this way is not established.
    data = {"status": "failed", "exit_code": 1, "result": FAILED_MARKER}
    if ids:
        data.update(run_id=R, execution_id="tier0-exec-1")
    return {"status": 200, "json": {"data": data}, "headers": {"x-request-id": REQUEST_ID}}


def _failed_sync_scenarios():
    start = ["agent", "cloud", "run", "start", "--deployment-id", D, "--message", "tier0 hello"]
    route = ("POST", CLOUD + "/runs")
    return [
        Scenario("control-404", "control", start, {route: [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-sync-completed", "control", start,
                 {route: [_invocation(200, "completed")]}),
        Scenario("control-failed-without-ids", "control", start,
                 {route: [_failed_invocation(ids=False)]}),
        Scenario("sync-failed", "probe", start, {route: [_failed_invocation()]}),
    ]


def failed_sync_facts(obs):
    out, text = obs["stdout"], obs["stdout"] + obs["stderr"]
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "accepted_printed": "Run accepted" in out and "Run ID: " + R in out,
            "failure_shown": "failed" in text.lower() or FAILED_MARKER in text}


def judge_failed_sync(facts):
    missing, done = facts["control-404"], facts["control-sync-completed"]
    bare = facts["control-failed-without-ids"]
    if not (missing["exit"] == 1 and missing["requests"] == 1 and done["exit"] == 0
            and done["requests"] == 1 and done["accepted_printed"]
            and not done["failure_shown"] and bare["requests"] == 1
            and bare["failure_shown"]):
        return [result("C21", "controls", "blocked",
                       "404 did not fail, the completed sync reply did not print 'Run accepted', "
                       "or the id-less failed reply did not reach the output")]
    p = facts["sync-failed"]
    hidden = p["requests"] == 1 and p["exit"] == 0 and not p["failure_shown"]
    return [result(
        "C21", "sync-failed-hidden", "contract_question" if hidden else "contract_met",
        "run start without --async, 200 reply status failed, exit_code 1, with run_id and "
        "execution_id: exit %s, 'Run accepted' %s, failure shown %s (the same reply without "
        "ids printed the body, exit %s)" % (p["exit"], p["accepted_printed"],
                                            p["failure_shown"], bare["exit"]),
        grade="inferred", basis=[
            "cloud spec 10722-10796 CloudDeploymentRunInvocationResponse: status 'Lifecycle "
            "status reported by the backend (`queued`, `running`, `completed`, etc.)'; "
            "exit_code 'Process exit code for runtimes that expose it'",
            "cloud spec POST /deployments/{id}/runs 200 'Run completed (always_on "
            "deployments)'; a failed status in that reply is not enumerated",
            "agent.rs 7703-7743: with run_id and execution_id cloud_run prints 'Run accepted' "
            "and returns Ok without reading status or exit_code"],
        question=None if not hidden else
        "Should a synchronous `cloud run start` whose reply reports a failed status (or a "
        "non-zero exit_code) show that status and exit non-zero, rather than '✓ Run "
        "accepted' with exit 0?",
        impact=None if not hidden else
        "hypothetical: a user or script invoking a deployment synchronously is told the run "
        "was accepted and gets exit 0 while the reply says it failed; fake API only, and "
        "whether the service sends such a reply is unknown")]


# ---------------------------------------------------------------- C22 lifecycle poll hint

HINT = re.compile(r"poll `([^`]*)`")


def _hint_scenarios():
    get = {("GET", AGENT): [{"status": 404, "json": ERROR_BODY}]}
    return [
        Scenario("control-start-converged", "control", ["agent", "managed-start", D],
                 {("POST", AGENT + "/start"): [_action("running", "running", "enabled")]}),
        Scenario("control-managed-get-404", "control", ["agent", "managed-get", D], get),
        Scenario("managed-start-pending", "probe", ["agent", "managed-start", D],
                 {("POST", AGENT + "/start"): [_action("building", "running", "enabled")]}),
        Scenario("managed-stop-pending", "probe", ["agent", "managed-stop", D],
                 {("POST", AGENT + "/stop"): [_action("stopping", "stopped")]}),
        # cloud deployment start/stop render the same hint (agent.rs 7271, 7308) but first
        # read the full deployment view to pick a route (shared.rs 89-95); not exercised.
        # The command the pre-fix hint names, run as printed (fixed args, same fake routes
        # as the managed-get control so a real command would reach the API).
        Scenario("follow-hint", "probe", ["agent", "get", D], get),
    ]


def hint_facts(obs):
    found = HINT.search(obs["stdout"])
    usage = "unrecognized subcommand" in obs["stderr"]
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "hint": found.group(1).replace(D, "<id>") if found else None,
            "status_shown": any(s in obs["stdout"] for s in ("building", "stopping")),
            "usage_error": usage}


def judge_hint(facts):
    done, get = facts["control-start-converged"], facts["control-managed-get-404"]
    if not (done["exit"] == 0 and done["requests"] == 1 and done["hint"] is None
            and get["exit"] == 1 and get["requests"] == 1 and not get["usage_error"]):
        return [result("C22", "controls", "blocked",
                       "converged start printed a poll hint or failed, or managed-get did not "
                       "reach the fake API")]
    probes = ("managed-start-pending", "managed-stop-pending")
    if any(facts[p]["exit"] != 0 or facts[p]["requests"] != 1 or not facts[p]["status_shown"]
           for p in probes):
        return [result("C22", "controls", "blocked",
                       "a pending start/stop reply did not succeed with one request and its "
                       "status")]
    follow = facts["follow-hint"]
    named = sorted({facts[p]["hint"] for p in probes if facts[p]["hint"]})
    stale = [p for p in probes if facts[p]["hint"] == "seren agent get <id>"]
    broken = bool(stale) and follow["exit"] != 0 and follow["requests"] == 0
    return [result(
        "C22", "poll-hint-command",
        "reproduced_client_defect" if broken else "contract_met",
        "not-yet-converged managed-start/stop replies printed poll hints %s (%s); running "
        "`seren agent get <id>` as printed: exit %s, requests %s, usage error %s; "
        "`seren agent managed-get <id>` reached the API (404, exit 1)" % (
            named, ", ".join(stale) or "none stale", follow["exit"], follow["requests"],
            follow["usage_error"]),
        grade="stated", basis=[
            "agent.rs 597-601: render_lifecycle_status prints 'poll `seren agent get "
            "{deployment_id}` until it reports {running|stopped}' when not converged; used "
            "by managed-start/stop (6276, 6312); cloud deployment start/stop (7271, 7308) "
            "share it but are not exercised here",
            "main.rs 1011-1949: AgentAction (flattening managed.rs 62 ManagedAction) has "
            "ManagedGet (1484) and cloud deployment Status (2075) but no Get subcommand",
            "cli/README.md 388: 'seren agent managed-get <deployment-id>'"],
        impact=None if not broken else
        "an operator whose start or stop has not converged follows the CLI's own "
        "instruction and gets a usage error instead of the deployment status; workaround: "
        "managed-get or cloud deployment status. Fake-API reproduction of CLI output only")]


# ---------------------------------------------------------------- C23 cloud lifecycle route

CLOUD_START = ["agent", "cloud", "deployment", "start", D]
CLOUD_STOP = ["agent", "cloud", "deployment", "stop", D]


def _cloud_summary(active_revision_id=None):
    # cloud spec CloudDeploymentSummary (json 10909-11110): every required field, enum values
    # from 10237, 10469, 10887, 10898, 19406, 19414, 21987. No `template`, so it cannot
    # match ManagedAgentOwnerView (json 20452-20467), the first CloudDeploymentView branch.
    return {"status": 200, "json": {"data": {
        "id": D, "organization_id": "7a000011-0000-4000-8000-000000000012",
        "user_id": "7a000013-0000-4000-8000-000000000014", "name": "tier0-always-on",
        "skill_slug": "tier0-skill", "compute_backend": "aws_container",
        "runtime_kind": "python", "mode": "always_on", "status": "stopped",
        "desired_lifecycle_state": "stopped", "desired_egress_state": "muted",
        "control_generation": 1, "code_bundle_hash": "tier0-hash",
        "orchestration_mode": "script", "requirements": {}, "visibility": "open",
        "created_at": T, "updated_at": T, "managed_agent": None,
        "active_revision_id": active_revision_id, "platform_managed": False}},
        "headers": {"x-request-id": REQUEST_ID}}


def _cloud_route_scenarios():
    view = ("GET", CLOUD)
    # Every lifecycle route answers, so the reply never depends on which one the CLI picks.
    def routes(summary, start, stop):
        return {view: [summary], ("POST", CLOUD + "/start"): [start],
                ("POST", CLOUD + "/stop"): [stop], ("POST", AGENT + "/start"): [start],
                ("POST", AGENT + "/stop"): [stop]}
    pending = routes(_cloud_summary(), _cloud_action("building", "running"),
                     _cloud_action("stopping", "stopped"))
    return [
        Scenario("control-lookup-404", "control", CLOUD_START,
                 {view: [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-start-converged", "control", CLOUD_START,
                 routes(_cloud_summary(), _cloud_action("running", "running"),
                        _cloud_action("stopped", "stopped"))),
        Scenario("cloud-start-pending", "probe", CLOUD_START, pending),
        Scenario("cloud-stop-pending", "probe", CLOUD_STOP, pending),
        # active_revision_id without managed_agent: shared.rs 52-54 refuses before mutating.
        Scenario("ambiguous-start", "probe", CLOUD_START,
                 routes(_cloud_summary(active_revision_id=G),
                        _cloud_action("building", "running"),
                        _cloud_action("stopping", "stopped"))),
        Scenario("follow-hint", "probe", ["agent", "get", D],
                 {("GET", AGENT): [{"status": 404, "json": ERROR_BODY}]}),
    ]


def cloud_route_facts(obs):
    facts = hint_facts(obs)
    facts["mutations"] = [r["path"].replace(D, "<id>") for r in obs["requests"]
                          if r["method"] == "POST"]
    # shared.rs 16-23 AmbiguousDeploymentType message, as opposed to a decode failure.
    facts["ambiguous_refusal"] = "active managed revision but no managed-agent" in obs["stderr"]
    return facts


def judge_cloud_route(facts):
    lookup, done = facts["control-lookup-404"], facts["control-start-converged"]
    cloud = "/publishers/seren-cloud/deployments/<id>/"
    if not (lookup["exit"] == 1 and lookup["mutations"] == []
            and done["exit"] == 0 and done["hint"] is None
            and done["mutations"] == [cloud + "start"]):
        return [result("C23", "controls", "blocked",
                       "a failed lookup mutated or exited other than 1, or a converged cloud "
                       "start did not POST once to the seren-cloud route without a hint")]
    start, stop = facts["cloud-start-pending"], facts["cloud-stop-pending"]
    ambiguous, follow = facts["ambiguous-start"], facts["follow-hint"]
    routed = (start["mutations"] == [cloud + "start"] and stop["mutations"] == [cloud + "stop"]
              and start["exit"] == 0 and stop["exit"] == 0)
    refused = (ambiguous["mutations"] == [] and ambiguous["exit"] != 0
               and ambiguous["ambiguous_refusal"])
    stale = [p for p in ("cloud-start-pending", "cloud-stop-pending")
             if facts[p]["hint"] == "seren agent get <id>"]
    broken = bool(stale) and follow["exit"] != 0 and follow["requests"] == 0
    route_basis = [
        "sdk shared.rs 75-80: 'issues exactly one mutation to the route the deployment "
        "metadata selects. Deployments that cannot be classified fail before any mutation'",
        "sdk shared.rs 45-56: managed_agent null and active_revision_id null -> seren-cloud "
        "route; active_revision_id without managed_agent -> AmbiguousDeploymentType",
        "cloud spec /deployments/{id}/start, /stop (json 4325-4440); CloudDeploymentSummary "
        "active_revision_id 'Current managed-agent revision, when this is a managed "
        "seren-agent deployment' (json 10933-10940)"]
    return [
        result("C23", "lifecycle-route",
               "contract_met" if routed and refused else "reproduced_client_defect",
               "plain cloud summary: start mutations %s exit %s, stop mutations %s exit %s; "
               "summary with active_revision_id and no managed_agent: mutations %s, exit %s, "
               "ambiguity refusal shown %s" % (
                   start["mutations"], start["exit"], stop["mutations"], stop["exit"],
                   ambiguous["mutations"], ambiguous["exit"], ambiguous["ambiguous_refusal"]),
               grade="stated", basis=route_basis,
               impact=None if routed and refused else
               "a lifecycle change reaches a route the deployment metadata does not select, "
               "or an unclassifiable deployment is mutated. Fake-API CLI behaviour only"),
        result("C23", "cloud-poll-hint",
               "reproduced_client_defect" if broken else "contract_met",
               "not-yet-converged cloud deployment start/stop printed poll hints %s (%s); "
               "`seren agent get <id>` as printed: exit %s, requests %s, usage error %s" % (
                   sorted({facts[p]["hint"] for p in ("cloud-start-pending",
                                                      "cloud-stop-pending")
                           if facts[p]["hint"]}),
                   ", ".join(stale) or "none stale", follow["exit"], follow["requests"],
                   follow["usage_error"]),
               grade="stated", basis=[
                   "agent.rs 597-601 render_lifecycle_status hint, called by cloud_start "
                   "(7271) and cloud_stop (7308)",
                   "main.rs 2066-2088: CloudDeploymentAction has Status/Start/Stop; "
                   "AgentAction has no Get subcommand (C22)"],
               impact=None if not broken else
               "breadth of C22/poll-hint-command: an always-on cloud deployment operator "
               "whose start or stop has not converged follows the CLI's instruction and gets "
               "a usage error; workaround: agent cloud deployment status <id>. Fake-API "
               "reproduction of CLI output only")]


# ---------------------------------------------------------------- C24 --env secrets file

# Synthetic values only. The control is the CLI's own writer format (env.rs 22-54 writes
# KEY=value; a connection URI carries '=' and '&') plus comments, a blank line and quotes.
DB_URL = "postgresql://tier0:tier0@127.0.0.1/db?sslmode=require&application_name=tier0"
ENV_FILES = {
    "plain.env": ("# tier0 control file\nALPHA=tier0-alpha\nDATABASE_URL=%s\n"
                  "QUOTED=\"tier0 quoted value\"\n\n" % DB_URL),
    "export.env": "export TOKEN=tier0-token\n",
    "comment.env": "TOKEN=tier0-token # rotated\n",
    "multiline.env": "PRIVATE_KEY=\"tier0-line-1\ntier0-line-2\"\n",
    "noequals.env": "TOKEN tier0-token\nOTHER=tier0-other\n",
    "empty.env": "# every secret moved elsewhere\n",
}
PLAIN_SECRETS = {"ALPHA": "tier0-alpha", "DATABASE_URL": DB_URL, "QUOTED": "tier0 quoted value"}
# Common dotenv reading of each probe file (inferred: no Seren document defines the syntax).
DOTENV_READING = {"export-prefix": {"TOKEN": "tier0-token"},
                  "inline-comment": {"TOKEN": "tier0-token"},
                  "multiline-value": {"PRIVATE_KEY": "tier0-line-1\ntier0-line-2"}}


def _env(name, role, command, file):
    # The flag is --env-file (clap long from the field env_file; name = "env" only sets the
    # value name: pinned usage 'managed-preview --env-file <env> <DEPLOYMENT_ID>').
    return Scenario(name, role, ["agent", command, D, "--env-file", "{file:%s}" % file],
                    {("POST", AGENT + "/managed/preview"): [FIXED_400],
                     ("PATCH", AGENT + "/managed"): [FIXED_400]},
                    files={file: ENV_FILES[file]})


def _env_scenarios():
    return [
        _env("control-plain", "control", "managed-preview", "plain.env"),
        _env("export-prefix", "probe", "managed-preview", "export.env"),
        _env("inline-comment", "probe", "managed-preview", "comment.env"),
        _env("multiline-value", "probe", "managed-preview", "multiline.env"),
        _env("unparsed-line", "probe", "managed-preview", "noequals.env"),
        _env("empty-file", "probe", "managed-preview", "empty.env"),
        # The same builder feeds the mutation (agent.rs 6683-6701): one PATCH, 4xx not retried.
        _env("export-prefix-update", "probe", "managed-update", "export.env"),
    ]


def env_facts(obs):
    sent = [r for r in obs["requests"] if r["path"].startswith(AGENT + "/managed")]
    body = (sent[0]["body"] or {}) if sent else {}
    return {"requests": len(sent), "exit": obs["exit"],
            "secrets": (body.get("workload_patch") or {}).get("secrets") if sent else None,
            "idempotency_key_sent": bool(sent and sent[0]["headers"].get("idempotency-key")),
            "stderr_lines": len([x for x in obs["stderr"].splitlines() if x.strip()])}


def judge_env(facts):
    plain, update = facts["control-plain"], facts["export-prefix-update"]
    if not (plain["requests"] == 1 and plain["secrets"] == PLAIN_SECRETS):
        return [result("C24", "controls", "blocked",
                       "the control .env file (the CLI's own KEY=value format) was not sent "
                       "as exactly its keys and values in one request")]
    baseline = plain["stderr_lines"]

    def silent(name):  # sent without refusing the file or printing anything extra
        return facts[name]["requests"] == 1 and facts[name]["stderr_lines"] <= baseline

    differ = [p for p in DOTENV_READING
              if silent(p) and facts[p]["secrets"] != DOTENV_READING[p]]
    dropped, empty = facts["unparsed-line"], facts["empty-file"]
    lost = silent("unparsed-line") and "TOKEN" not in (dropped["secrets"] or {})
    mutation = (update["requests"] == 1 and update["idempotency_key_sent"]
                and update["secrets"] == facts["export-prefix"]["secrets"])
    basis = [
        "agent.rs 11265-11283 parse_env_file: split at the first '=', key and value trimmed, "
        "value trim_matches('\"') then trim_matches('\\''), lines without '=' skipped",
        "agent.rs 6497-6501, 6576-6578, 6384: the parsed object becomes "
        "workload_patch.secrets for managed-preview and managed-update",
        "agent spec ManagedAgentWorkloadPatch.secrets (json 13612-13614): 'Replaces "
        "caller-authored secrets'",
        "main.rs 1814-1816, 1889-1891: '--env-file <env>: Path to .env secrets file'; no Seren "
        "document defines the file syntax, so the dotenv reading is inferred"]
    return [
        result("C24", "env-syntax", "contract_question" if differ else "contract_met",
               "silently sent instead of the common dotenv reading: %s; managed-update with "
               "the export file sent the same secrets in one PATCH with an Idempotency-Key: "
               "%s" % ("; ".join("%s -> %s" % (p, json.dumps(facts[p]["secrets"]))
                                 for p in differ) or "none", mutation),
               grade="inferred", basis=basis,
               question=None if not differ else
               "Which .env syntax does --env accept? An `export` prefix, an inline `#` "
               "comment or a multi-line quoted value is sent as a different key or value "
               "without a warning, and the result replaces all caller-authored secrets",
               impact=None if not differ else
               "hypothetical: an operator rotating secrets with a dotenv file written for "
               "other tools replaces the employee's secrets with a renamed key, a value "
               "carrying the comment, or a PEM key cut to its first line; secret values are "
               "redacted on read, so this would surface only as later run failures. "
               "Fake-API CLI request bodies only"),
        result("C24", "unparsed-line", "contract_question" if lost else "contract_met",
               "a line without '=' (`TOKEN tier0-token`) next to `OTHER=...`: requests %s, "
               "secrets keys sent %s, extra stderr lines %s" % (
                   dropped["requests"], sorted(dropped["secrets"] or {}),
                   dropped["stderr_lines"] - baseline),
               grade="inferred", basis=basis[:2],
               question=None if not lost else
               "Should --env report a line it cannot parse instead of dropping that secret "
               "from a replace-all update?"),
        result("C24", "empty-file", "observed_behavior",
               "a comments-only file: requests %s, secrets sent %s, exit %s (the fake "
               "answers every request with a fixed 400), extra stderr lines %s" % (empty["requests"], json.dumps(empty["secrets"]), empty["exit"],
                             empty["stderr_lines"] - baseline),
               grade="unspecified", basis=basis[:3]),
    ]


# ---------------------------------------------------------------- C26 run detail output

OUTPUT_MARKER = "tier0-run-output-marker"


def _failed_run_detail():
    # C19's spec-complete CloudDeploymentRunEvent plus the documented nullable diagnostics:
    # `output` (json 10654-10660) carries a failed script's stderr; output_events stays null
    # as the spec says for non-agentic scripts.
    reply = _run_event("failed")
    reply["json"]["data"].update(
        status_message=STATUS_MESSAGE, stop_reason=STOP_REASON, output_events=None,
        output="Traceback (most recent call last):\nValueError: " + OUTPUT_MARKER + "\n")
    return reply


def _run_detail_scenarios():
    scoped = ["agent", "cloud", "run", "get", "--deployment-id", D, R]
    scoped_route = ("GET", CLOUD + "/runs/" + R)
    return [
        Scenario("control-404", "control", scoped,
                 {scoped_route: [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-json", "control", ["-o", "json"] + scoped,
                 {scoped_route: [_failed_run_detail()]}),
        Scenario("table-scoped", "probe", scoped, {scoped_route: [_failed_run_detail()]}),
        Scenario("table-global", "probe", ["agent", "cloud", "run", "get", R],
                 {("GET", RUNS): [_failed_run_detail()]}),
    ]


def run_detail_facts(obs):
    body = _json_stdout(obs["stdout"])
    data = body.get("data") if isinstance(body, dict) else None
    text = obs["stdout"] + obs["stderr"]
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "json_output": isinstance(data, dict) and OUTPUT_MARKER in (data.get("output") or ""),
            "status_shown": R in obs["stdout"] and "failed" in obs["stdout"],
            "status_message_shown": STATUS_MESSAGE in text,
            "stop_reason_shown": STOP_REASON in text,
            "output_shown": OUTPUT_MARKER in text}


def judge_run_detail(facts):
    missing, js = facts["control-404"], facts["control-json"]
    tables = [facts["table-scoped"], facts["table-global"]]
    if not (missing["exit"] == 1 and missing["requests"] == 1 and js["exit"] == 0
            and js["requests"] == 1 and js["json_output"]
            and all(t["exit"] == 0 and t["requests"] == 1 and t["status_shown"]
                    for t in tables)):
        return [result("C26", "controls", "blocked",
                       "404 did not fail, -o json did not carry the run output, or a table "
                       "view did not show the failed run")]
    hidden = [name for name in ("table-scoped", "table-global")
              if not facts[name]["output_shown"]]
    return [result(
        "C26", "table-output", "contract_question" if hidden else "contract_met",
        "failed run detail with output (stderr) set: -o json carries it %s; default table "
        "shows status_message %s/%s, stop_reason %s/%s, output %s/%s (scoped/global)" % (
            js["json_output"], tables[0]["status_message_shown"],
            tables[1]["status_message_shown"], tables[0]["stop_reason_shown"],
            tables[1]["stop_reason_shown"], tables[0]["output_shown"],
            tables[1]["output_shown"]),
        grade="inferred", basis=[
            "cloud spec CloudDeploymentRunEvent (json 10654-10663): output 'Raw stdout/stderr "
            "output. Universal fallback for all backend types.'; output_events 'Null for "
            "non-agentic scripts that produce plain stdout/stderr'",
            "main.rs 2278: run get 'Get details of a run by ID'; no documented table fields",
            "agent.rs 10201-10324: print_cloud_run_detail_response prints summary, trace, "
            "capture and eval rows; no row reads output"],
        question=None if not hidden else
        "Should the default `cloud run get` view show (or point to) the run's raw output, "
        "which the spec makes the universal fallback, when that is the only record of why a "
        "non-agentic run failed?",
        impact=None if not hidden else
        "hypothetical: a person diagnosing a failed script run with the default view sees "
        "status, message and stop reason but not the stderr; -o json carries it and no other "
        "run subcommand is documented to print it. Fake API only")]


# ---------------------------------------------------------------- C27 run limits

SET_LIMITS = {"max_iterations": 5, "max_timeout_seconds": 120, "max_tool_output_chars": 1000,
              "context_budget_tokens": 2000}
REFUSED = "Unsupported managed agent config keys"


def _limits(name, role, config):
    args = ["agent", "managed-update", D]
    files = {}
    if config is None:
        args += ["--name", "tier0-name"]
    else:
        files = {"limits.json": json.dumps(config)}
        args += ["--agent-config", "{file:limits.json}"]
    return Scenario(name, role, args, {("PATCH", AGENT + "/managed"): [FIXED_400]}, files=files)


def _limits_scenarios():
    return [
        _limits("control-name-only", "control", None),
        _limits("control-unknown-key", "control", {"tier0_unknown": 1}),
        _limits("set-limits", "probe", SET_LIMITS),
        _limits("clear-limit", "probe", {"max_iterations": None}),
        _limits("tool-call-limit", "probe", {"max_tool_calls_per_run": 10}),
    ]


def limits_facts(obs):
    sent = to(obs, "PATCH", AGENT + "/managed")
    body = (sent[0]["body"] or {}) if sent else {}
    patch = body.get("workload_limits") if isinstance(body, dict) else None
    # Unset values (null, false clear_* flags) carry no instruction; keep the rest verbatim.
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "limits": {k: v for k, v in patch.items() if v not in (None, False)}
            if isinstance(patch, dict) else None,
            "body_keys": meaningful(body) if sent else None,
            "refused": REFUSED in obs["stderr"], "error": first_error_line(obs)}


def judge_limits(facts):
    name, unknown = facts["control-name-only"], facts["control-unknown-key"]
    if not (name["requests"] == 1 and name["limits"] is None and "name" in (name["body_keys"] or [])
            and unknown["requests"] == 0 and unknown["exit"] not in (0, None)
            and unknown["refused"]):
        return [result("C27", "controls", "blocked",
                       "a --name update did not send one PATCH without workload_limits, or an "
                       "unknown --agent-config key was not refused before any request")]
    set_, clear, tool = facts["set-limits"], facts["clear-limit"], facts["tool-call-limit"]
    sent_ok = set_["requests"] == 1 and set_["limits"] == SET_LIMITS
    clear_ok = clear["requests"] == 1 and clear["limits"] == {"clear_max_iterations": True}
    basis = [
        "agent spec AgentSpecUpdate.workload_limits (json 5573-5583) 'Partially update workload "
        "limits'; WorkloadLimitsPatch (json 15391-15449): 'Each value field preserves the "
        "current value when omitted. Its matching clear_* flag explicitly removes the current "
        "limit'; fields include max_tool_calls_per_run",
        "main.rs 1892-1894 managed-update --agent-config 'Path to a managed agent JSON config "
        "for advanced tuning'; agent.rs 2472-2495 MANAGED_AGENT_CONFIG_FIELDS accepts "
        "context_budget_tokens, max_iterations, max_timeout_seconds, max_tool_output_chars and "
        "refuses other keys (2675-2698)",
        "agent.rs 6419-6440 build_managed_agent_limits_patch: value -> field, null -> clear_*"]
    if sent_ok and clear_ok:
        patch = result("C27", "limits-patch", "contract_met",
                       "accepted limits reached workload_limits exactly (%s); a null limit sent "
                       "clear_max_iterations true; one PATCH each" % json.dumps(set_["limits"]),
                       grade="stated", basis=basis)
    else:
        patch = result("C27", "limits-patch", "reproduced_client_defect",
                       "accepted --agent-config limits were not sent as the spec patch: set -> "
                       "requests %s, workload_limits %s; null -> requests %s, workload_limits %s"
                       % (set_["requests"], json.dumps(set_["limits"]), clear["requests"],
                          json.dumps(clear["limits"])),
                       grade="stated", basis=basis,
                       impact="hypothetical: an operator bounding a runaway employee believes "
                       "a run limit is set or cleared when it is not. Fake-API request bodies "
                       "only")
    if tool["requests"] == 1 and tool["limits"] == {"max_tool_calls_per_run": 10}:
        gap = result("C27", "tool-call-limit", "contract_met",
                     "max_tool_calls_per_run 10 reached workload_limits in one PATCH",
                     grade="stated", basis=basis)
    elif tool["requests"] == 0 and tool["refused"]:
        gap = result("C27", "tool-call-limit", "capability_gap",
                     "--agent-config {max_tool_calls_per_run: 10} is refused locally before any "
                     "request (exit %s: %s); no other managed-update flag sets it" % (
                         tool["exit"], tool["error"]),
                     grade="stated", basis=basis + [
                         "main.rs: no flag mentions max_tool_calls_per_run or limits"],
                     impact="an operator cannot cap an employee's tool calls per run from the "
                     "CLI although the update contract accepts it; the refusal is explicit, so "
                     "nothing is lost silently. Whether the server enforces the cap is unknown")
    else:
        gap = result("C27", "tool-call-limit", "observed_behavior",
                     "max_tool_calls_per_run 10: requests %s, workload_limits %s, refused %s, "
                     "exit %s" % (tool["requests"], json.dumps(tool["limits"]), tool["refused"],
                                  tool["exit"]),
                     grade="unspecified", basis=basis)
    return [patch, gap]


# ---------------------------------------------------------------- C28 run cost in list views

# Distinct decimal strings so each one is recognisable in table output; TOTAL is their sum.
COMPUTE_COST, INFERENCE_COST, TOTAL_COST = "0.017311", "2.468024", "2.485335"


def _costed_run():
    # C19's spec-complete CloudDeploymentRunEvent (json 10556-10637) with nonzero costs.
    run = _run_event("completed")["json"]["data"]
    run.update(compute_cost_atomic=17311, compute_cost_usd=COMPUTE_COST,
               inference_cost_atomic=2468024, inference_cost_usd=INFERENCE_COST,
               inference_input_tokens=73519, inference_output_tokens=2207)
    return run


def _costed_list():
    return {"status": 200, "json": {"data": [_costed_run()]},
            "headers": {"x-request-id": REQUEST_ID}}


def _cost_scenarios():
    scoped = ["agent", "cloud", "runs", "list", "--deployment-id", D]
    scoped_routes = {("GET", CLOUD + "/runs"): [_costed_list()]}
    detail = {"status": 200, "json": {"data": _costed_run()},
              "headers": {"x-request-id": REQUEST_ID}}
    return [
        Scenario("control-404", "control", scoped,
                 {("GET", CLOUD + "/runs"): [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-json", "control", ["-o", "json"] + scoped, scoped_routes),
        Scenario("control-detail", "control",
                 ["agent", "cloud", "run", "get", "--deployment-id", D, R],
                 {("GET", CLOUD + "/runs/" + R): [detail]}),
        Scenario("list-scoped", "probe", scoped, scoped_routes),
        Scenario("list-org", "probe", ["agent", "cloud", "runs", "list"],
                 {("GET", ORG_DEPLOYMENTS): [_empty_list()], ("GET", ORG_RUNS): [_costed_list()]}),
    ]


def cost_facts(obs):
    out = obs["stdout"]
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "run_shown": R in out, "compute_shown": COMPUTE_COST in out,
            "inference_shown": INFERENCE_COST in out, "total_shown": TOTAL_COST in out}


def judge_costs(facts):
    missing, js, detail = facts["control-404"], facts["control-json"], facts["control-detail"]
    lists = {name: facts[name] for name in ("list-scoped", "list-org")}
    if not (missing["exit"] == 1 and missing["requests"] == 1
            and js["exit"] == 0 and js["compute_shown"] and js["inference_shown"]
            and detail["exit"] == 0 and detail["compute_shown"] and detail["inference_shown"]
            and all(p["exit"] == 0 and p["run_shown"] for p in lists.values())):
        return [result("C28", "controls", "blocked",
                       "404 did not fail, -o json or the run detail table did not carry both "
                       "costs, or a runs list did not print the run")]
    partial = [name for name, p in lists.items()
               if not (p["inference_shown"] or p["total_shown"])]
    return [result(
        "C28", "list-cost", "contract_question" if partial else "contract_met",
        "run with compute_cost_usd %s and inference_cost_usd %s (sum %s): run get table shows "
        "both; runs list COST column shows compute %s/%s, inference %s/%s, total %s/%s "
        "(scoped/org)" % (
            COMPUTE_COST, INFERENCE_COST, TOTAL_COST, lists["list-scoped"]["compute_shown"],
            lists["list-org"]["compute_shown"], lists["list-scoped"]["inference_shown"],
            lists["list-org"]["inference_shown"], lists["list-scoped"]["total_shown"],
            lists["list-org"]["total_shown"]),
        grade="inferred", basis=[
            "cloud spec CloudDeploymentRunEvent (json 10556-10637): compute_cost_usd and "
            "inference_cost_usd both required; no per-run total field",
            "cloud spec DeploymentSpendSummary (json 19366-19403): total_cost_usd reported "
            "alongside compute_cost_usd and inference_cost_usd",
            "agent.rs 10640-10700 print_cloud_run_rows: column 'COST' reads compute_cost_usd "
            "only; agent.rs 10234-10253 run get labels 'Compute Cost (USD)' and 'Inference Cost "
            "(USD)'; main.rs and README document no runs list columns"],
        question=None if not partial else
        "Should the default `cloud runs list` COST column include inference cost (or be "
        "labelled as compute cost), since the spec splits each run's cost into compute and "
        "inference and the deployment spend view totals both?",
        impact=None if not partial else
        "hypothetical: an operator reading run costs from the default runs list sees only the "
        "compute part under an unqualified 'COST' header and may underestimate what runs cost; "
        "run get, -o json and deployment spend carry the inference cost. Fake API only; real "
        "cost proportions are unknown")]


# ---------------------------------------------------------------- C29 rollback recovery

ROLLBACK = AGENT + "/managed/rollback"
PRIOR = "7a000018-0000-4000-8000-000000000019"    # the revision rolled back to
SECRET_RESULT = "7a00001c-0000-4000-8000-00000000001d"
CONFLICT = {"status": 409, "json": {"error": {"code": "tier0_revision_conflict",
                                              "message": "tier0 active revision changed"}}}


def _rollback_scenarios():
    plain = ["agent", "managed-rollback", D, PRIOR]
    guarded = plain + ["--expected-active-revision-id", R,
                       "--secret-resolution-result-id", SECRET_RESULT]
    unavailable = {"status": 503, "json": {"error": {"message": "tier0 unavailable"}}}

    def run(name, role, args, sends, lookups=(_mutation("applied"),)):
        return Scenario(name, role, args, {("POST", ROLLBACK): list(sends),
                                           ("GET", ACTIONS): list(lookups)})
    return [
        run("control-404", "control", plain, [{"status": 404, "json": ERROR_BODY}]),
        run("control-applied", "control", plain, [_mutation("applied")]),
        run("preconditions", "probe", guarded, [_mutation("applied")]),
        run("conflict-409", "probe", guarded, [CONFLICT, _mutation("applied")]),
        run("ambiguous-recovered", "probe", guarded, [unavailable, _mutation("applied")]),
    ]


def rollback_facts(obs):
    sends = to(obs, "POST", ROLLBACK)
    lookups = [r for r in obs["requests"] if r["path"].startswith(ACTIONS[:-1])]
    keys = [r["headers"].get("idempotency-key") for r in sends]
    body = _json_stdout(obs["stdout"])
    data = body.get("data") if isinstance(body, dict) else None
    action = data.get("reconciliation") if isinstance(data, dict) else None
    sent = (sends[0]["body"] or {}) if sends else {}
    return {"sends": len(sends), "lookups": len(lookups), "exit": obs["exit"],
            "body": {k: v for k, v in sent.items() if v is not None}
            if isinstance(sent, dict) else sent,
            "key_sent": bool(keys) and None not in keys,
            "one_key_throughout": bool(keys) and None not in keys and len(
                set(keys + [r["path"].rsplit("/", 1)[1] for r in lookups])) == 1,
            "printed_state": action.get("state") if isinstance(action, dict) else None,
            "conflict_shown": "tier0 active revision changed" in obs["stderr"],
            "error": UUID.sub("<id>", first_error_line(obs))[:120]}


def judge_rollback(facts):
    missing, ok = facts["control-404"], facts["control-applied"]
    if not (missing["exit"] == 1 and missing["sends"] == 1 and missing["lookups"] == 0
            and ok["exit"] == 0 and ok["sends"] == 1 and ok["key_sent"]
            and ok["printed_state"] == "applied" and ok["body"] == {"revision_id": PRIOR}):
        return [result("C29", "controls", "blocked",
                       "404 did not fail once, or a plain rollback did not send revision_id "
                       "only and print the applied action: %s / %s" % (missing, ok))]
    pre, conflict, amb = (facts["preconditions"], facts["conflict-409"],
                          facts["ambiguous-recovered"])
    expected = {"revision_id": PRIOR, "expected_active_revision_id": R,
                "secret_resolution_result_id": SECRET_RESULT}
    sent = pre["sends"] == 1 and pre["key_sent"] and pre["body"] == expected
    out = [result(
        "C29", "rollback-request", "contract_met" if sent else "reproduced_client_defect",
        "managed-rollback with --expected-active-revision-id and --secret-resolution-result-id "
        "sent %d request(s), body %s, Idempotency-Key %s, exit %s" % (
            pre["sends"], pre["body"], pre["key_sent"], pre["exit"]),
        grade="stated", basis=[
            "agent json 14720-14748 RollbackSerenAgentDeploymentRequest: revision_id, "
            "expected_active_revision_id 'Current active revision the rollback was approved "
            "against', secret_resolution_result_id",
            "agent json 1746-1755: Idempotency-Key required; main.rs 1736-1747 flags"],
        impact="hypothetical: a rollback that dropped its precondition could replace a newer "
               "revision than the one the operator reviewed")]
    refused = (conflict["sends"] == 1 and conflict["lookups"] == 0
               and conflict["exit"] not in (0, None) and conflict["conflict_shown"])
    out.append(result(
        "C29", "revision-conflict", "contract_met" if refused else "contract_question",
        "a 409 revision conflict on rollback: sends %d, action lookups %d, exit %s, server "
        "message shown %s, error %r" % (conflict["sends"], conflict["lookups"], conflict["exit"],
                                        conflict["conflict_shown"], conflict["error"]),
        grade="inferred", basis=[
            "agent json 1797-1798: 409 'Mutation identity or revision conflict'",
            "agent.rs 216-226, 281: only 5xx/transport errors are retried; other errors "
            "are returned with the server detail"],
        question=None if refused else
        "Must a rollback refused for a revision conflict fail visibly, once, with the "
        "server's reason?",
        impact=None if refused else
        "hypothetical: an operator believes a refused rollback applied, or it is resent"))
    recovered = (amb["sends"] == 1 and amb["lookups"] == 1 and amb["one_key_throughout"]
                 and amb["exit"] == 0 and amb["printed_state"] == "applied")
    out.append(result(
        "C29", "ambiguous-recovery", "contract_met" if recovered else "reproduced_client_defect",
        "a 503 on rollback whose action lookup answers applied: sends %d, lookups %d, one key "
        "throughout %s, exit %s, printed state %s" % (
            amb["sends"], amb["lookups"], amb["one_key_throughout"], amb["exit"],
            amb["printed_state"]),
        grade="stated", basis=[
            "agent json 1746-1755: Idempotency-Key 'Stable mutation request identity'",
            "agent.rs 255-260: an ambiguous send is looked up by the same request id"],
        impact="hypothetical: a resent rollback under a new identity could apply twice"))
    return out


# ---------------------------------------------------------------- C30 agent schedules

SCHEDULES = CLOUD + "/schedules"
SCHEDULE = "7a000019-0000-4000-8000-00000000001a"
OCCURRENCE_ERROR = "tier0-occurrence-error-marker"
LAST_ERROR = "tier0-last-error-marker"
OCCURRENCE_DUE = "2026-10-12T07:00:00Z"
CREATE_FLAGS = {"schedule_key": "tier0-weekly", "message": "tier0 weekly digest",
                "conversation_id": "tier0-conversation-1", "cron": "0 9 * * 1",
                "timezone": "Europe/Berlin", "max_attempts": 3}


def _schedule(occurrence_failed=False, last_error=None):
    # Spec-complete CloudDeploymentAgentSchedule (cloud json 9885-10008). The failed shape is
    # a recurring schedule that stays active after its last occurrence used up its attempts:
    # the per-occurrence fields carry the failure, the current budget is reset.
    schedule = {"id": SCHEDULE, "deployment_id": D, "schedule_key": "tier0-weekly",
                "schedule_kind": "cron", "status": "active",
                "next_run_at": "2026-10-19T07:00:00Z", "occurrence_due_at": "2026-10-19T07:00:00Z",
                "attempts": 0, "max_attempts": 3, "cron_schedule": "0 9 * * 1",
                "cron_timezone": "Europe/Berlin", "last_error": last_error,
                "last_run_at": None, "last_run_event_id": None, "last_occurrence_attempts": None,
                "last_occurrence_due_at": None, "last_occurrence_error": None,
                "last_occurrence_run_event_id": None, "last_occurrence_status": None,
                "created_at": T, "updated_at": T}
    if occurrence_failed:
        schedule.update(last_occurrence_attempts=3, last_occurrence_due_at=OCCURRENCE_DUE,
                        last_occurrence_error=OCCURRENCE_ERROR, last_occurrence_status="failed",
                        last_run_at="2026-10-12T07:20:00Z")   # last retry, after the due time
    return schedule


def _schedule_scenarios():
    listing = ["agent", "cloud", "schedule", "list", "--deployment-id", D]
    create = ["agent", "cloud", "schedule", "create", "--deployment-id", D]
    for flag, value in CREATE_FLAGS.items():
        create += ["--" + flag.replace("_", "-"), str(value)]
    failed_list = {"status": 200, "json": {"data": [_schedule(occurrence_failed=True)]}}
    return [
        Scenario("control-404", "control", listing,
                 {("GET", SCHEDULES): [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-json", "control", ["-o", "json"] + listing,
                 {("GET", SCHEDULES): [failed_list]}),
        Scenario("control-last-error", "control", listing,
                 {("GET", SCHEDULES): [{"status": 200, "json": {
                     "data": [_schedule(last_error=LAST_ERROR)]}}]}),
        Scenario("create-request", "probe", create,
                 {("POST", SCHEDULES): [{"status": 201, "json": {"data": _schedule()}}]}),
        Scenario("list-failed-occurrence", "probe", listing, {("GET", SCHEDULES): [failed_list]}),
        Scenario("cancel-failed-occurrence", "probe",
                 ["agent", "cloud", "schedule", "cancel", "--deployment-id", D, SCHEDULE],
                 {("DELETE", SCHEDULES + "/" + SCHEDULE): [{"status": 200, "json": {
                     "data": dict(_schedule(occurrence_failed=True), status="cancelled")}}]}),
    ]


def schedule_facts(obs):
    sends = to(obs, "POST", SCHEDULES)
    sent = (sends[0]["body"] or {}) if sends else {}
    out = obs["stdout"]
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "body": {k: v for k, v in sent.items() if v is not None}
            if isinstance(sent, dict) else sent,
            "schedule_shown": SCHEDULE in out,
            "last_error_shown": LAST_ERROR in out,
            "occurrence_error_shown": OCCURRENCE_ERROR in out,
            "occurrence_status_shown": "failed" in out,
            "occurrence_due_shown": OCCURRENCE_DUE[:16] in out}


def judge_schedules(facts):
    missing, js, last = facts["control-404"], facts["control-json"], facts["control-last-error"]
    create = facts["create-request"]
    views = {name: facts[name] for name in ("list-failed-occurrence", "cancel-failed-occurrence")}
    if not (missing["exit"] == 1 and missing["requests"] == 1
            and js["exit"] == 0 and js["occurrence_error_shown"]
            and last["exit"] == 0 and last["schedule_shown"] and last["last_error_shown"]
            and create["exit"] == 0
            and all(v["exit"] == 0 and v["requests"] == 1 and v["schedule_shown"]
                    for v in views.values())):
        return [result("C30", "controls", "blocked",
                       "404 did not fail, -o json did not carry last_occurrence_error, the table "
                       "did not show last_error, create failed, or a view did not print the "
                       "schedule")]
    sent = create["requests"] == 1 and create["body"] == CREATE_FLAGS
    out = [result(
        "C30", "create-request", "contract_met" if sent else "reproduced_client_defect",
        "schedule create with --schedule-key, --message, --conversation-id, --cron, --timezone "
        "and --max-attempts sent %d request(s), body %s, exit %s" % (
            create["requests"], create["body"], create["exit"]),
        grade="stated", basis=[
            "cloud spec CloudDeploymentAgentScheduleRequest (json 10010-10066) fields; POST "
            "/deployments/{id}/schedules 201 (json 3883-3934)",
            "main.rs 2446-2481: create flags 'Cron expression for a recurring future run', "
            "'Timezone for cron schedules', 'Maximum worker retry attempts'"],
        impact="hypothetical: a schedule created without the requested cron, timezone or retry "
               "budget runs at the wrong time or retries differently than asked")]
    hidden = [name for name, v in views.items() if not v["occurrence_error_shown"]]
    out.append(result(
        "C30", "occurrence-failure", "contract_question" if hidden else "contract_met",
        "active cron schedule whose last occurrence failed after 3 attempts (last_error null): "
        "-o json carries last_occurrence_error %s; list/cancel tables show the occurrence error "
        "%s/%s, a 'failed' status %s/%s, its due time %s/%s" % (
            js["occurrence_error_shown"],
            views["list-failed-occurrence"]["occurrence_error_shown"],
            views["cancel-failed-occurrence"]["occurrence_error_shown"],
            views["list-failed-occurrence"]["occurrence_status_shown"],
            views["cancel-failed-occurrence"]["occurrence_status_shown"],
            views["list-failed-occurrence"]["occurrence_due_shown"],
            views["cancel-failed-occurrence"]["occurrence_due_shown"]),
        grade="inferred", basis=[
            "cloud spec CloudDeploymentAgentSchedule (json 9885-10008): last_occurrence_status, "
            "last_occurrence_error, last_occurrence_attempts, last_occurrence_due_at; "
            "occurrence_due_at 'Due time of the occurrence the current attempts budget belongs "
            "to'; no description separates last_error from last_occurrence_error",
            "agent.rs 9658-9737: list and detail rows read last_error, never a "
            "last_occurrence_* field; agent.rs 12507-12526 tests last_error only"],
        question=None if not hidden else
        "Should the default schedule list and detail views show a failed last occurrence "
        "(last_occurrence_status/error), or does the server always mirror it into last_error?",
        impact=None if not hidden else
        "hypothetical: an operator checking a recurring scheduled task with the default views "
        "sees it active with no error while its last occurrence failed; -o json carries the "
        "failure. Whether the server leaves last_error empty in that state is unknown. Fake "
        "API only"))
    return out


# ---------------------------------------------------------------- C31 conversation messages

CONVERSATION = "tier0-conversation-1"
MESSAGES = CLOUD + "/conversations/" + CONVERSATION + "/messages"
MESSAGES_ARGS = ["agent", "cloud", "conversation", "messages", "--deployment-id", D, CONVERSATION]
ANSWER = "tier0-assistant-answer-marker"
UNAVAILABLE_CODE = "tier0_content_unavailable"
PAGE_CURSOR, NEXT_CURSOR = "tier0-cursor-1", "tier0-cursor-2"


def _message(n, role, content, unavailable=None):
    # Spec-complete CloudConversationMessageResponse (cloud json 9579-9699); events kept empty.
    return {"message_id": "5e7a0000-0000-4000-8000-0000000007%02d" % n,
            "conversation_id": CONVERSATION, "deployment_id": D,
            "organization_id": "7a000014-0000-4000-8000-000000000015",
            "user_id": "7a000015-0000-4000-8000-000000000016", "role": role, "source": "api",
            "collaboration_target": "individual", "content": content, "events": [],
            "client_message_id": None, "deleted_at": None, "edited_at": None, "run": None,
            "run_id": None, "run_summary": None, "unavailable": unavailable,
            "created_at": "2026-10-14T00:0%d:00Z" % n, "updated_at": T}


def _messages_page(unavailable=False, has_more=False):
    # DataResponse_CloudConversationMessagesResponse (json 1401, 9767-9790), default desc order.
    answer = _message(2, "assistant", "", {"error_code": UNAVAILABLE_CODE, "fields": ["content"],
                                           "retryable": True}) \
        if unavailable else _message(2, "assistant", ANSWER)
    return {"status": 200, "json": {"data": {
        "messages": [answer, _message(1, "user", "tier0 question")], "has_more": has_more,
        "next_cursor": NEXT_CURSOR if has_more else None}}}


def _conversation_scenarios():
    return [
        Scenario("control-404", "control", MESSAGES_ARGS,
                 {("GET", MESSAGES): [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-content", "control", MESSAGES_ARGS,
                 {("GET", MESSAGES): [_messages_page()]}),
        Scenario("control-json", "control", ["-o", "json"] + MESSAGES_ARGS,
                 {("GET", MESSAGES): [_messages_page(unavailable=True)]}),
        Scenario("unavailable-table", "probe", MESSAGES_ARGS,
                 {("GET", MESSAGES): [_messages_page(unavailable=True)]}),
        Scenario("paging", "probe", MESSAGES_ARGS + ["--cursor", PAGE_CURSOR],
                 {("GET", MESSAGES): [_messages_page(has_more=True)]}),
    ]


def conversation_facts(obs):
    gets = to(obs, "GET", MESSAGES)
    query = gets[0].get("query", {}) if gets else {}
    out = obs["stdout"]
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "cursor_sent": ",".join(query.get("cursor", [])) or None,
            "question_row": "tier0 question" in out, "assistant_row": "role=assistant" in out,
            "answer_shown": ANSWER in out,
            "unavailable_shown": UNAVAILABLE_CODE in out or "unavailable" in out.lower(),
            "next_cursor_shown": NEXT_CURSOR in out}


def judge_conversations(facts):
    missing, content, js = facts["control-404"], facts["control-content"], facts["control-json"]
    table, paging = facts["unavailable-table"], facts["paging"]
    if not (missing["exit"] == 1 and missing["requests"] == 1
            and content["exit"] == 0 and content["answer_shown"] and content["question_row"]
            and js["exit"] == 0 and js["unavailable_shown"]
            and all(v["exit"] == 0 and v["requests"] == 1 and v["question_row"]
                    for v in (table, paging))):
        return [result("C31", "controls", "blocked",
                       "404 did not fail, the table did not show a plain answer, -o json did not "
                       "carry the unavailable marker, or a probe did not print the transcript")]
    cursor_ok = paging["cursor_sent"] == PAGE_CURSOR
    out = [result(
        "C31", "paging",
        "reproduced_client_defect" if not cursor_ok
        else "contract_met" if paging["next_cursor_shown"] else "contract_question",
        "messages --cursor %s sent cursor=%s; a page with has_more=true printed its next_cursor "
        "%s, exit %s" % (PAGE_CURSOR, paging["cursor_sent"], paging["next_cursor_shown"],
                         paging["exit"]),
        grade="stated" if not cursor_ok else "inferred", basis=[
            "main.rs 2420-2422: --cursor 'Opaque keyset cursor returned by a previous page'; "
            "cloud json 1367-1375 cursor query parameter",
            "cloud json 9767-9790 CloudConversationMessagesResponse has_more (required), "
            "next_cursor"],
        impact="hypothetical: earlier messages of a long conversation, including answers, "
               "cannot be reached from the default view")]
    hidden = not table["unavailable_shown"]
    out.append(result(
        "C31", "unavailable-content", "contract_question" if hidden else "contract_met",
        "assistant message with empty content and unavailable {error_code, fields [content], "
        "retryable}: -o json carries it %s; default table shows the assistant row %s, any "
        "unavailable marker %s" % (js["unavailable_shown"], table["assistant_row"],
                                   table["unavailable_shown"]),
        grade="inferred", basis=[
            "cloud json 9680-9689, 9745-9766: CloudConversationMessageResponse.unavailable -> "
            "CloudConversationMessageUnavailable {error_code, fields, retryable}; no description",
            "agent.rs 9388-9441: the message row reads created_at, role, source, run_id, "
            "run_summary/run status, events and non-empty content, never unavailable"],
        question=None if not hidden else
        "Should the default conversation messages view say when a message's content is "
        "unavailable (error_code, retryable), rather than print the row without content?",
        impact=None if not hidden else
        "hypothetical: an operator reading a transcript sees an assistant turn with no content "
        "and no reason, and cannot tell a withheld or failed load (possibly retryable) from an "
        "empty answer; -o json carries the reason. When the server sets unavailable is "
        "unknown. Fake API only"))
    return out


# ---------------------------------------------------------------- C32 run cancel outcome

CANCEL_SCOPED = CLOUD + "/runs/" + R + "/cancel"
CANCEL_GLOBAL = RUNS + "/cancel"


def _cancel_scenarios():
    # cloud spec POST .../cancel (json 3385-3428, 6421-6454): 200 'Run cancellation requested'
    # with the spec-complete run event (C19's _run_event); status is a free string (10697).
    scoped = ["agent", "cloud", "run", "cancel", "--deployment-id", D, R]
    org = ["agent", "cloud", "run", "cancel", R]
    return [
        Scenario("control-404", "control", scoped,
                 {("POST", CANCEL_SCOPED): [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-cancelled", "control", org,
                 {("POST", CANCEL_GLOBAL): [_run_event("cancelled")]}),
        Scenario("scoped-requested", "probe", scoped,
                 {("POST", CANCEL_SCOPED): [_run_event("running")]}),
        Scenario("org-requested", "probe", org,
                 {("POST", CANCEL_GLOBAL): [_run_event("running")]}),
    ]


def cancel_facts(obs):
    out = _json_stdout(obs["stdout"])
    data = out.get("data", out) if isinstance(out, dict) else None
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "scoped_posts": len(to(obs, "POST", CANCEL_SCOPED)),
            "org_posts": len(to(obs, "POST", CANCEL_GLOBAL)),
            "printed_status": data.get("status") if isinstance(data, dict) else None,
            "claims_cancelled": "cancelled" in (obs["stdout"] + obs["stderr"]).lower()}


def judge_cancel(facts):
    missing, done = facts["control-404"], facts["control-cancelled"]
    if not (missing["exit"] == 1 and missing["requests"] == 1
            and done["exit"] == 0 and done["org_posts"] == 1
            and done["printed_status"] == "cancelled"):
        return [result("C32", "controls", "blocked",
                       "404 control did not fail after one request, or a cancelled reply was "
                       "not sent to the org route and printed with its status")]
    out = []
    for name, route in (("scoped-requested", "scoped_posts"), ("org-requested", "org_posts")):
        p = facts[name]
        if p["exit"] != 0 or p["requests"] != 1:
            disposition = "contract_question"
        elif p[route] != 1 or p["printed_status"] != "running" or p["claims_cancelled"]:
            disposition = "reproduced_client_defect"
        else:
            disposition = "contract_met"
        out.append(result(
            "C32", name, disposition,
            "cancel reply 200 with run status running: exit %s, requests %s, posts to the "
            "%s route %s, printed status %s, output claims cancelled %s" % (
                p["exit"], p["requests"], name.split("-")[0], p[route], p["printed_status"],
                p["claims_cancelled"]),
            grade="stated", basis=[
                "main.rs 2386-2393: 'Cancel a queued/running run; provide --deployment-id for "
                "deployment-scoped cancellation'",
                "cloud spec POST /deployments/{id}/runs/{run_id}/cancel and "
                "/runs/{run_id}/cancel (json 3385-3428, 6421-6454): 200 'Run cancellation "
                "requested' returning the run event; status is a string (10697)",
                "agent.rs 9202-9211, 10766-10779: the reply is printed with print_json"],
            question=None if disposition != "contract_question" else
            "The cancel did not complete against a spec-shaped reply; review the fixture "
            "before drawing any conclusion.",
            impact=None if disposition != "reproduced_client_defect" else
            "hypothetical: an operator is told a run was cancelled, or the wrong run route was "
            "used, while the reply says the run is still running; fake API only"))
    return out


# ---------------------------------------------------------------- C34 agent task follow

ORG = "7a000011-0000-4000-8000-000000000012"
TASK = "7a000012-0000-4000-8000-000000000013"
TASK_STREAM = "/organizations/" + ORG + "/agents/tasks/" + TASK + "/stream"
TASK_ARGS = ["agent", "tasks-get", "--org-id", ORG, TASK, "--follow"]
TASK_ERROR = "tier0 synthetic task failure"


def _task_event(event_type, status, seq):
    # cloud spec AgentTaskEvent (json 12223-12250), 'An event in the agent task lifecycle
    # (for SSE streaming)': required id, task_id, event_type, payload, created_at.
    return {"id": "5e7a0000-0000-4000-8000-0000000000c%d" % seq, "task_id": TASK,
            "event_type": event_type, "created_at": T,
            "payload": {"status": status, "error_message": TASK_ERROR,
                        "cost_total_atomic": 1500}}


def _task_stream(name, role, frames, hold=6, deadline=3):
    return Scenario(name, role, TASK_ARGS,
                    {("GET", TASK_STREAM): [{"sse": frames, "hold": hold, "frame_delay": 0}]},
                    deadline=deadline)


def _task_scenarios():
    working = {"event": "task.working", "data": _task_event("task.working", "working", 1)}
    flat = {"status": "failed", "error_message": TASK_ERROR, "cost_total_atomic": 1500}
    failed = _task_event("task.failed", "failed", 2)
    return [
        Scenario("control-404", "control", TASK_ARGS,
                 {("GET", TASK_STREAM): [{"status": 404, "json": ERROR_BODY}]}),
        # The CLI's own frame layout (agent.rs 2014-2060): flat status/error_message.
        _task_stream("control-flat-failed", "control",
                     [{"event": "task.failed", "data": flat}]),
        _task_stream("spec-event-named", "probe",
                     [working, {"event": "task.failed", "data": failed}]),
        _task_stream("spec-event-unnamed", "probe", [{"data": failed}]),
    ]


def task_facts(obs):
    stderr = re.sub(r"\x1b\[[0-9;]*m", "", obs["stderr"])
    status = [x.strip() for x in stderr.splitlines() if x.strip().startswith("Task ")]
    return {"ending": ending(obs),
            "exit": None if obs["killed_by_supervisor"] else obs["exit"],
            "requests": len(obs["requests"]), "status_line": status[0] if status else None,
            "error_shown": ("Error: " + TASK_ERROR) in stderr,
            "cost_shown": "Cost: $" in stderr}


def judge_tasks(facts):
    missing, flat = facts["control-404"], facts["control-flat-failed"]
    if not (missing["exit"] == 1 and missing["requests"] == 1
            and flat["ending"] == "self" and flat["status_line"] == "Task failed"
            and flat["error_shown"]):
        return [result("C34", "controls", "blocked",
                       "404 control did not fail after one request, or a flat failed frame was "
                       "not reported as 'Task failed' with its error")]
    basis = [
        "agent.rs 2014-2060: a frame is terminal by event name task.completed|failed|"
        "canceled|cancelled or a top-level data.status; status, output, error_message and "
        "cost_total_atomic are read from the top level of the frame data",
        "cloud spec AgentTaskEvent (json 12223-12250): 'An event in the agent task lifecycle "
        "(for SSE streaming)' with required event_type and payload; stream_task (json "
        "3342-3375) documents 200 'SSE event stream' without a schema",
        "AgentTask (json 12107-12222) carries status, error_message and cost_total_atomic"]
    named, unnamed = facts["spec-event-named"], facts["spec-event-unnamed"]
    shown = lambda p: (p["ending"] == "self" and p["status_line"] == "Task failed"  # noqa: E731
                       and p["error_shown"])
    out = [result(
        "C34", "spec-event-shape",
        "contract_met" if shown(named) and shown(unnamed) else "contract_question",
        "AgentTaskEvent frames for a failed task (status and error in payload): with event "
        "name task.failed the CLI ended %s, printed %r, error shown %s; without an event name "
        "it ended %s, printed %r, error shown %s" % (
            named["ending"], named["status_line"], named["error_shown"], unnamed["ending"],
            unnamed["status_line"], unnamed["error_shown"]),
        grade="inferred", basis=basis,
        question="The stream route declares no frame schema; AgentTaskEvent is described as "
        "the SSE object. Does the task stream send AgentTaskEvent (status and error_message "
        "inside payload, event_type in the data), or the flat layout the CLI reads?",
        impact="hypothetical: an operator following a failed task sees 'Task unknown' without "
        "the failure reason, or a follow that never recognises the end; fake API only")]
    out.append(result(
        "C34", "failed-exit", "observed_behavior",
        "a followed task reported failed with its error exits %s" % flat["exit"],
        grade="unspecified", basis=["agent.rs 2033-2060: a terminal frame returns Ok(()) "
                                    "for any status; no exit-status contract is documented"],
        impact="hypothetical: a script that waits on 'tasks-get --follow' cannot tell a failed "
        "task from a completed one by exit status"))
    return out


# ---------------------------------------------------------------- C35 failed/stuck start views

# Generalized regression scenario; private investigation history omitted.
# accepted start stayed `building` (Ready=False, reason Building); an accepted stop then
# left it building / desired stopping, unchanged since the start. These fixtures copy the
# shape of those replies with synthetic values. The failed-start condition is synthetic:
# the spec says conditions are synthesized from status and the last error_message, but no
# real failed sample exists, so its exact type/reason are unspecified.
SINCE = "2026-10-14T04:20:42.27335Z"
SINCE_MARK = "04:20:42"
START_ERROR = "tier0 synthetic start failure: runtime config could not be fetched"
START_REASON = "tier0_start_failed"
GET_ARGS = ["agent", "managed-get", D]
STATUS_ARGS = ["agent", "cloud", "deployment", "status", D]
C35_MARKS = {"failed": "failed", "building": "building", "running": "running",
             "stopping": "stopping", "ready_condition": "Ready", "ready_reason": "Building",
             "since": SINCE_MARK, "error": START_ERROR, "reason": START_REASON}


def _condition(kind, state, reason, message=None):
    # agent spec ManagedDeploymentCondition (json 13740-13790): type, status required.
    return {"type": kind, "status": state, "reason": reason, "message": message,
            "last_transition_time": SINCE}


def _managed_detail(status, conditions):
    # agent spec ManagedAgentDeploymentDetail (json 10037-10300): every required field.
    return {"status": 200, "json": {"data": {
        "deployment_id": D, "name": "tier0-employee", "agent_slug": "tier0-employee",
        "mode": "always_on", "compute_backend": "aws_container", "runtime_kind": "python",
        "status": status, "bundle": {"instructions": [
            {"kind": "skill", "path": "SKILL.md", "content": "# tier0\n"}]},
        "model_id": "tier0/model", "model_config": {}, "template": "research_monitor",
        "tool_presets": ["live_data"], "allowed_publisher_operations": ["get"],
        "resolved_tools": ["system_time"], "approval_policy": "read_only",
        "model_policy": "fast", "runtime_adapter": "seren_agent",
        "private_output_policy": "control_plane", "secret_keys": [], "requirements": [],
        "visibility": "open", "routing_reason": "tier0 routing", "conditions": conditions}},
        "headers": {"x-request-id": REQUEST_ID}}


def _deployment(status, desired, error_message=None):
    # cloud spec CloudDeploymentSummary (json 10909-11110): the C23 summary with the
    # lifecycle fields of the observed reply: status, desired_lifecycle_state ('Operator-
    # desired lifecycle. Independent of observed compute status.', json 10992), updated_at.
    reply = _cloud_summary()
    reply["json"]["data"].update(status=status, desired_lifecycle_state=desired,
                                 control_generation=35, updated_at=SINCE,
                                 error_message=error_message)
    return reply


def _start_view_scenarios():
    stuck = [_condition("Accepted", "True", "building"),
             _condition("Ready", "False", "Building")]
    failed = [_condition("Accepted", "True", "failed"),
              _condition("Failed", "True", START_REASON, START_ERROR),
              _condition("Ready", "False", START_REASON, START_ERROR)]
    get = lambda reply: {("GET", AGENT + "/managed"): [reply]}  # noqa: E731
    status = lambda reply: {("GET", CLOUD): [reply]}  # noqa: E731
    return [
        Scenario("control-get-json-failed", "control", ["-o", "json"] + GET_ARGS,
                 get(_managed_detail("failed", failed))),
        Scenario("control-get-table-running", "control", GET_ARGS,
                 get(_managed_detail("running", [_condition("Ready", "True", "Running")]))),
        Scenario("get-table-failed", "probe", GET_ARGS, get(_managed_detail("failed", failed))),
        Scenario("get-table-stuck", "probe", GET_ARGS, get(_managed_detail("building", stuck))),
        Scenario("control-status-json-stuck", "control", ["-o", "json"] + STATUS_ARGS,
                 status(_deployment("building", "stopping"))),
        Scenario("control-status-table-before-stop", "control", STATUS_ARGS,
                 status(_deployment("building", "running"))),
        Scenario("status-table-after-stop", "probe", STATUS_ARGS,
                 status(_deployment("building", "stopping"))),
        Scenario("status-table-failed", "probe", STATUS_ARGS,
                 status(_deployment("failed", "running", START_ERROR))),
    ]


def start_view_facts(obs):
    out = obs["stdout"] + obs["stderr"]
    lines = [x for x in obs["stdout"].splitlines()
             if "running" in x or "stopping" in x or "building" in x]
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "shown": sorted(k for k, mark in C35_MARKS.items() if mark in out),
            "lifecycle_lines": [re.sub(r"\s+", " ", x.strip()) for x in lines]}


def judge_start_views(facts):
    shown = lambda name, *marks: all(m in facts[name]["shown"] for m in marks)  # noqa: E731
    ok = lambda name: facts[name]["exit"] == 0 and facts[name]["requests"] == 1  # noqa: E731
    controls = (ok("control-get-json-failed")
                and shown("control-get-json-failed", "error", "reason", "since")
                and ok("control-get-table-running") and shown("control-get-table-running",
                                                              "running")
                and ok("control-status-json-stuck")
                and shown("control-status-json-stuck", "stopping", "since")
                and ok("control-status-table-before-stop")
                and shown("control-status-table-before-stop", "building"))
    if not controls:
        return [result("C35", "controls", "blocked",
                       "a JSON control did not carry the fixture's error, reason, desired state "
                       "or timestamp, or a table control did not decode and show its status")]
    probes = ("get-table-failed", "get-table-stuck", "status-table-after-stop",
              "status-table-failed")
    if not all(ok(p) for p in probes):
        return [result("C35", "controls", "blocked",
                       "a probe read did not succeed with one request: %s" % {
                           p: (facts[p]["exit"], facts[p]["requests"]) for p in probes})]
    get_failed, status_failed = facts["get-table-failed"], facts["status-table-failed"]
    reason_shown = shown("get-table-failed", "error") and shown("status-table-failed", "error")
    before, after = facts["control-status-table-before-stop"], facts["status-table-after-stop"]
    stuck = facts["get-table-stuck"]
    progress_shown = (shown("status-table-after-stop", "stopping", "since")
                      and shown("get-table-stuck", "ready_condition", "since"))
    workaround = ("`-o json` on managed-get carries the conditions with the error message, "
                  "reason and transition time; on cloud deployment status it carries "
                  "desired_lifecycle_state and updated_at (both verified here; error_message "
                  "in that JSON view is not exercised)")
    return [
        result("C35", "failure-reason-default-view",
               "contract_met" if reason_shown else "contract_question",
               "failed start, default views: managed-get showed %s (error shown %s); cloud "
               "deployment status showed %s (error shown %s); the same managed-get reply with "
               "-o json showed %s" % (
                   get_failed["shown"], "error" in get_failed["shown"],
                   status_failed["shown"], "error" in status_failed["shown"],
                   facts["control-get-json-failed"]["shown"]),
               grade="inferred", basis=[
                   "agent spec ManagedAgentDeploymentDetail.conditions (json 10130): "
                   "'synthesized from the flat lifecycle `status` and the last persisted "
                   "`error_message`'; the detail has no error_message field of its own",
                   "agent spec ManagedDeploymentCondition.message (json 13751): 'Human-"
                   "readable explanation; safe to surface in operator UI'",
                   "cloud spec CloudDeploymentSummary.error_message (json 11007)",
                   "agent.rs 3797-3930 print_managed_agent_detail_table and 3243-3313 "
                   "print_cloud_deployment_detail_table: no condition, error or desired-state "
                   "row; README 487: 'Inspect the resolved managed deployment: managed-get'",
                   "precedent: agent.rs 2053 prints a task's error_message in tasks-get"],
               question="Should the default managed-get / cloud deployment status views show "
               "the failure reason (condition message, error_message) the server returns?",
               impact=None if reason_shown else
               "an operator whose employee failed to start sees status 'failed' in the "
               "documented reads but not why, unless they know to add -o json. Fake API: "
               "client rendering only; the server's failed-condition shape is unspecified. "
               "Workaround: " + workaround),
        result("C35", "stuck-progress-default-view",
               "contract_met" if progress_shown else "contract_question",
               "stuck start then accepted stop (real reply shape): cloud deployment status "
               "default view showed %s before the stop and %s after it (output identical: %s; "
               "lines %s); managed-get default view of the stuck reply showed %s" % (
                   before["shown"], after["shown"],
                   before["lifecycle_lines"] == after["lifecycle_lines"],
                   after["lifecycle_lines"], stuck["shown"]),
               grade="inferred", basis=[
                   "cloud spec desired_lifecycle_state (json 10992): 'Operator-desired "
                   "lifecycle. Independent of observed compute status.'; updated_at required",
                   "agent.rs 556-602 render_lifecycle_status prints the desired lifecycle for "
                   "start/stop replies and tells the operator to poll until the status "
                   "converges",
                   "Synthetic transition: observed building while desired state is stopping."],
               question="Should the reads an operator polls after start/stop show the desired "
               "lifecycle, the Ready condition and since when, so a stuck transition can be "
               "told from one in progress?",
               impact=None if progress_shown else
               "an operator polling a start or stop that never converges sees only "
               "'building', the same before and after their stop was accepted, with no "
               "reason or age; this invites repeated stop/start requests. Fake API: client "
               "rendering of real reply shapes only; server behaviour not inferred. "
               "Workaround: " + workaround)]


# ---------------------------------------------------------------- C37 stop note, failed status

# Generalized regression scenario; private investigation history omitted.
# employees replied status `stopping` with a server note "Stopped. ..." or "Already
# stopped. ...", while every later read showed building or failed / desired stopping,
# unchanged; neither employee was ever observed stopped. The notes below copy the reply
# template; the failed reply copies the shape (failed, desired stopping, error_message) with
# a synthetic message and the synthetic deployment.
STOP_NOTE = ("Stopped. This employee will not run or post until it is explicitly started "
             "again. Control generation 1.")
ALREADY_NOTE = ("Already stopped. This employee will not run or post until it is explicitly "
                "started again. Control generation 35.")
DEPLOY_ERROR = "tier0 synthetic deploy failure: runtime controller failed to reconcile"
STOP_ARGS = ["agent", "managed-stop", D]
CONVERGED = re.compile(r"(not yet converged|converged) \(status: ([a-z_]+)\)")
NOTE = re.compile(r"note: (.*)")
C37_MARKS = {"failed": "failed", "stopping": "stopping", "running": "running",
             "error": DEPLOY_ERROR, "since": SINCE_MARK, "confirmation": '"confirmation"'}


def _stop(status, desired, note):
    reply = _action(status, desired)
    reply["json"]["data"]["confirmation"] = note
    return {("POST", AGENT + "/stop"): [reply]}


def _stop_view_scenarios():
    status = lambda reply: {("GET", CLOUD): [reply]}  # noqa: E731
    return [
        Scenario("control-stop-converged", "control", STOP_ARGS,
                 _stop("stopped", "stopped", STOP_NOTE)),
        Scenario("control-stop-pending-no-note", "control", STOP_ARGS,
                 _stop("stopping", "stopping", None)),
        Scenario("stop-pending-stopped-note", "probe", STOP_ARGS,
                 _stop("stopping", "stopping", STOP_NOTE)),
        Scenario("stop-pending-already-note", "probe", STOP_ARGS,
                 _stop("stopping", "stopping", ALREADY_NOTE)),
        Scenario("stop-json-pending-note", "probe", ["-o", "json"] + STOP_ARGS,
                 _stop("stopping", "stopping", STOP_NOTE)),
        Scenario("control-status-json-running", "control", ["-o", "json"] + STATUS_ARGS,
                 status(_deployment("running", "running"))),
        Scenario("status-json-failed-stopping", "probe", ["-o", "json"] + STATUS_ARGS,
                 status(_deployment("failed", "stopping", DEPLOY_ERROR))),
        Scenario("status-table-failed-stopping", "probe", STATUS_ARGS,
                 status(_deployment("failed", "stopping", DEPLOY_ERROR))),
    ]


def stop_view_facts(obs):
    out = obs["stdout"] + obs["stderr"]
    head, note, hint = CONVERGED.search(out), NOTE.search(out), HINT.search(out)
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "headline": head.group(1) if head else None,
            "status": head.group(2) if head else None,
            "note": note.group(1).strip() if note else None,
            "hint": hint.group(1).replace(D, "<id>") if hint else None,
            "shown": sorted(k for k, mark in C37_MARKS.items() if mark in out)}


def judge_stop_views(facts):
    ok = lambda name: facts[name]["exit"] == 0 and facts[name]["requests"] == 1  # noqa: E731
    done, bare = facts["control-stop-converged"], facts["control-stop-pending-no-note"]
    running = facts["control-status-json-running"]
    controls = (ok("control-stop-converged") and done["headline"] == "converged"
                and done["note"] == STOP_NOTE and done["hint"] is None
                and ok("control-stop-pending-no-note") and bare["headline"] == "not yet converged"
                and bare["note"] is None and bare["hint"] is not None
                and ok("control-status-json-running") and "running" in running["shown"]
                and "error" not in running["shown"])
    if not controls:
        return [result("C37", "controls", "blocked",
                       "a converged stop, a pending stop without a note or a running status "
                       "read did not render as its reply says")]
    probes = ("stop-pending-stopped-note", "stop-pending-already-note", "stop-json-pending-note",
              "status-json-failed-stopping", "status-table-failed-stopping")
    if not all(ok(p) for p in probes):
        return [result("C37", "controls", "blocked",
                       "a probe did not succeed with one request: %s" % {
                           p: (facts[p]["exit"], facts[p]["requests"]) for p in probes})]
    stopped, already = facts["stop-pending-stopped-note"], facts["stop-pending-already-note"]
    as_json, failed = facts["stop-json-pending-note"], facts["status-json-failed-stopping"]
    table = facts["status-table-failed-stopping"]
    reassured = [p for p, f in (("stopped-note", stopped), ("already-note", already))
                 if f["headline"] == "not yet converged" and f["note"]
                 and f["note"].lower().startswith(("stopped.", "already stopped."))]
    json_carries = all(m in failed["shown"] for m in ("error", "failed", "since", "stopping"))
    return [
        result("C37", "stop-note-while-unconverged", "observed_behavior",
               "accepted stop replying status stopping with the server's note: headline %r, "
               "note %r, poll hint %r ('Stopped.' reply); headline %r, note %r ('Already "
               "stopped.' reply); reassuring note under a not-converged headline: %s; -o json "
               "shows status and confirmation: %s" % (
                   stopped["headline"], stopped["note"], stopped["hint"], already["headline"],
                   already["note"], reassured or "none",
                   all(m in as_json["shown"] for m in ("stopping", "confirmation"))),
               grade="unspecified", basis=[
                   "agent and cloud specs CloudDeploymentActionStatusResponse.confirmation: "
                   "type string|null with no description; status and desired_lifecycle_state "
                   "required",
                   "agent.rs 556-602 render_lifecycle_status: 'accepted; not yet converged' "
                   "headline, then `note: {confirmation}` verbatim, then the poll hint (C22)",
                   "Synthetic stop replies contain completion-like text while the fixture never stopped."],
               impact="an operator stopping an employee reads 'Stopped. This employee will "
               "not run or post…' (or 'Already stopped.') in the same output that says the "
               "stop has not converged, followed by a poll command the CLI rejects (C22). The "
               "note reads as confirmed shutdown; in this synthetic scenario no later read ever "
               "showed stopped. The note's wording comes from the server reply; the fake API "
               "establishes only how the pinned CLI presents it, not what the server means "
               "or whether compute stopped."),
        result("C37", "failed-stop-status-json",
               "contract_met" if json_carries else "contract_question",
               "failed deployment with desired stopping: -o json cloud deployment status "
               "showed %s (error, desired state and updated_at carried: %s); the default view "
               "of the same reply showed %s" % (failed["shown"], json_carries, table["shown"]),
               grade="inferred", basis=[
                   "cloud spec CloudDeploymentSummary: error_message (json 11007), "
                   "desired_lifecycle_state 'Operator-desired lifecycle. Independent of "
                   "observed compute status.' (json 10992), updated_at required",
                   "C35/failure-reason-default-view: the default views omit the reason; its "
                   "-o json error_message path on cloud deployment status was not exercised"],
               question=None if json_carries else
               "Should -o json cloud deployment status carry the error_message and desired "
               "state the server returns?",
               impact=None if json_carries else
               "an operator cannot see why a failed deployment failed or whether a stop is "
               "pending even with -o json"),
    ]


# ---------------------------------------------------------------- C38 run answer via -o json

# A JSON answer split across text events at its braces, with non-ASCII, escaped quotes
# and braces inside a string: the shape a malformed-looking answer would need to survive.
ANSWER_PARTS = ['{"company": "Tier0 Ltd", "figures": {', '"revenue": 1200.5, "items": [1, 2]',
                '}', ', "summary": "Résumé – ok \U0001f642 \\"q\\" {not a brace}"',
                '}']
ESCAPED_CLOSE = "@@tier0-escaped-close@@"
BIG_NUMBERS = {"beyond_u64": 123456789012345678901234567890, "u64_plus_one": 18446744073709551616,
               "amount": 0.1, "large_float": 1e21, "negative": -9007199254740993}


def _trace_events(parts, extra=None):
    # Event types and fields follow the run-event shapes C1 recorded (thinking, tool_call
    # with status, tool_result, text, done); output_events items are untyped in the spec.
    result_event = {"type": "tool_result", "id": "call-1", "is_error": False,
                    "content": '{"data": {"rows": [[1]]}}', "sequence_number": 3}
    result_event.update(extra or {})
    events = [{"type": "thinking", "text": "plan", "sequence_number": 1},
              {"type": "tool_call", "id": "call-1", "name": "tier0_tool",
               "arguments": '{"q": 1}', "status": "completed", "sequence_number": 2},
              result_event]
    events += [{"type": "text", "text": p, "sequence_number": 4 + i}
               for i, p in enumerate(parts)]
    return events + [{"type": "done", "status": "ok", "sequence_number": 4 + len(parts)}]


def _completed_run(events):
    reply = _run_event("completed")
    reply["json"]["data"]["output_events"] = events
    return reply


def _escaped_run():
    # Raw body: the closing text event is written as the JSON escape }, which a decoder
    # must read as "}" (RFC 8259 section 7).
    reply = _completed_run(_trace_events(ANSWER_PARTS[:-1] + [ESCAPED_CLOSE]))
    body = json.dumps(reply.pop("json")).replace('"%s"' % ESCAPED_CLOSE, '"\\u007d"')
    reply["body"] = body
    return reply


def _answer_scenarios():
    scoped = ["-o", "json", "agent", "cloud", "run", "get", "--deployment-id", D, R]
    route = ("GET", CLOUD + "/runs/" + R)
    return [
        Scenario("control-404", "control", scoped,
                 {route: [{"status": 404, "json": ERROR_BODY}]}),
        Scenario("control-json", "control", scoped,
                 {route: [_completed_run(_trace_events(["tier0 plain answer"]))]}),
        Scenario("split-answer", "probe", scoped,
                 {route: [_completed_run(_trace_events(ANSWER_PARTS))]}),
        Scenario("escaped-close", "probe", scoped, {route: [_escaped_run()]}),
        Scenario("event-numbers", "probe", scoped,
                 {route: [_completed_run(_trace_events(["ok"], BIG_NUMBERS))]}),
    ]


def _answer_text(events):
    after = next((i for i, e in enumerate(events) if e.get("type") == "tool_result"), None)
    if after is None:
        return None
    return "".join(e.get("text", "") for e in events[after + 1:] if e.get("type") == "text")


def answer_facts(obs):
    body = _json_stdout(obs["stdout"])
    data = body.get("data") if isinstance(body, dict) else None
    events = data.get("output_events") if isinstance(data, dict) else None
    events = events if isinstance(events, list) and all(
        isinstance(e, dict) for e in events) else None
    answer = _answer_text(events) if events else None
    try:
        parses = answer is not None and isinstance(json.loads(answer), dict)
    except ValueError:
        parses = False
    numbers = None
    if events:
        found = next((e for e in events if e.get("type") == "tool_result"), {})
        numbers = {k: repr(found.get(k)) for k in sorted(BIG_NUMBERS)}
    return {"requests": len(obs["requests"]), "exit": obs["exit"],
            "run_id": data.get("id") if isinstance(data, dict) else None,
            "status": data.get("status") if isinstance(data, dict) else None,
            "event_types": [e.get("type") for e in events] if events else None,
            "answer": answer, "answer_parses": parses, "numbers": numbers}


def judge_answer(facts):
    missing, plain = facts["control-404"], facts["control-json"]
    if not (missing["exit"] == 1 and missing["requests"] == 1 and plain["exit"] == 0
            and plain["requests"] == 1 and plain["run_id"] == R
            and plain["status"] == "completed"):
        return [result("C38", "controls", "blocked",
                       "404 did not fail once, or -o json run get did not print the "
                       "completed run's identity and status")]
    expected = "".join(ANSWER_PARTS)
    types = ["thinking", "tool_call", "tool_result"] + ["text"] * len(ANSWER_PARTS) + ["done"]
    probes = {name: facts[name] for name in ("split-answer", "escaped-close")}
    faithful = [name for name, f in probes.items()
                if f["exit"] == 0 and f["answer"] == expected and f["event_types"] == types]
    wrong = sorted(set(probes) - set(faithful))
    basis = [
        "cloud spec CloudDeploymentRunEvent.output_events (json 10661-10663): 'Structured "
        "event trace for agentic executions (thinking, tool calls, text)'; agent spec items {} "
        "(json 8809-8811), so no event field is typed away",
        "agent.rs 10187-10196: cloud_run_get prints the decoded reply with print_json for "
        "-o json; README.md 16 '-o json ... so coding agents can inspect state'",
        "RFC 8259 section 7: \\u007d is the same character as '}'"]
    expected_numbers = {k: repr(BIG_NUMBERS[k]) for k in sorted(BIG_NUMBERS)}
    numbers = facts["event-numbers"]["numbers"]
    changed = sorted(k for k in expected_numbers if (numbers or {}).get(k) != expected_numbers[k])
    return [
        result("C38", "answer-text", "reproduced_client_defect" if wrong else "contract_met",
               "a completed run whose JSON answer is split across %d text events at its "
               "braces (non-ASCII, escaped quotes, a brace inside a string; and the same with "
               "the last '}' sent as \\u007d): -o json run get reproduced the event order "
               "and the concatenated answer exactly for %s; differed for %s; the concatenated "
               "answer parses as one JSON object: %s/%s (split/escaped)" % (
                   len(ANSWER_PARTS), faithful or "none", wrong or "none",
                   probes["split-answer"]["answer_parses"],
                   probes["escaped-close"]["answer_parses"]),
               grade="stated", basis=basis,
               impact=None if not wrong else
               "a caller reading the employee's answer from -o json run get receives text "
               "or event order the server did not send. Fake-API CLI output only"),
        result("C38", "event-numbers", "observed_behavior",
               "numbers inside an untyped tool_result event printed by -o json run get: "
               "unchanged %s; changed %s (sent %s, printed %s)" % (
                   sorted(set(expected_numbers) - set(changed)), changed or "none",
                   {k: expected_numbers[k] for k in changed},
                   {k: (numbers or {}).get(k) for k in changed}),
               grade="unspecified", basis=basis[:2]),
    ]


CASES = {
    "C1": {"title": "run stream terminal handling and exit codes", "kind": "tier0-offline",
           "scenarios": _stream_scenarios, "facts": stream_facts, "judge": judge_stream},
    "C2": {"title": "eval-gate flag without its age limit", "kind": "tier0-offline",
           "scenarios": _gate_scenarios, "facts": gate_facts, "judge": judge_gate},
    "C3": {"title": "deployment-scoped run start with --run-id", "kind": "tier0-offline",
           "scenarios": _runid_scenarios, "facts": runid_facts, "judge": judge_runid},
    "C4": {"title": "empty SEREN_API_KEY and malformed credentials", "kind": "tier0-offline",
           "scenarios": _auth_scenarios, "facts": auth_facts, "judge": judge_auth},
    "C5": {"title": "cloud vs managed error detail", "kind": "tier0-offline",
           "scenarios": _error_scenarios, "facts": error_facts, "judge": judge_errors},
    "C6": {"title": "managed-update revision precondition (static gap, local refusal)",
           "kind": "tier0-offline", "scenarios": _revision_scenarios,
           "facts": revision_facts, "judge": judge_revision},
    # Follow-on queue derived from the contract map after the initial batch.
    "C7": {"title": "ambiguous managed-update retry keeps one mutation identity",
           "kind": "tier0-offline", "scenarios": _retry_scenarios, "facts": retry_facts,
           "judge": judge_retry},
    "C8": {"title": "HTTP 401 handling on reads and updates", "kind": "tier0-offline",
           "scenarios": _unauthorized_scenarios, "facts": unauthorized_facts,
           "judge": judge_unauthorized},
    "C9": {"title": "saved sign-in refresh against a loopback token endpoint",
           "kind": "tier0-offline", "scenarios": _refresh_scenarios, "facts": refresh_facts,
           "judge": judge_refresh},
    "C10": {"title": "undocumented success status: managed vs cloud handling",
            "kind": "tier0-offline", "scenarios": _undocumented_scenarios,
            "facts": undocumented_facts, "judge": judge_undocumented},
    "C11": {"title": "managed-delete output: -o json and the returned action status",
            "kind": "tier0-offline", "scenarios": _delete_scenarios, "facts": delete_facts,
            "judge": judge_delete},
    "C12": {"title": "managed-update action outcomes: failed, superseded, deferred pending",
            "kind": "tier0-offline", "scenarios": _outcome_scenarios,
            "facts": outcome_facts, "judge": judge_outcomes},
    "C13": {"title": "auth diagnostics when SEREN_API_KEY overrides the saved sign-in",
            "kind": "tier0-offline", "scenarios": _diagnostic_scenarios,
            "facts": diagnostic_facts, "judge": judge_diagnostics},
    "C14": {"title": "managed-deployment-resources decode: spec reply vs missing field",
            "kind": "tier0-offline", "scenarios": _resources_scenarios,
            "facts": resources_facts, "judge": judge_resources},
    "C15": {"title": "cloud run start output: -o json and the synchronous result",
            "kind": "tier0-offline", "scenarios": _start_output_scenarios,
            "facts": start_output_facts, "judge": judge_start_output},
    "C16": {"title": "-o json sweep: cloud destroy, update-config, environment list",
            "kind": "tier0-offline", "scenarios": _json_sweep_scenarios,
            "facts": json_sweep_facts, "judge": judge_json_sweep},
    "C17": {"title": "managed-deployment-activity: query, decode and failure reason",
            "kind": "tier0-offline", "scenarios": _activity_scenarios,
            "facts": activity_facts, "judge": judge_activity},
    "C18": {"title": "cloud runs list: filter flags reach the spec query on both routes",
            "kind": "tier0-offline", "scenarios": _runs_list_scenarios,
            "facts": runs_list_facts, "judge": judge_runs_list},
    "C19": {"title": "cloud run approve/reject: decision set, idle run, identity check",
            "kind": "tier0-offline", "scenarios": _approval_scenarios,
            "facts": approval_facts, "judge": judge_approvals},
    "C20": {"title": "cloud run events: filter flags reach the spec query on both routes",
            "kind": "tier0-offline", "scenarios": _run_events_scenarios,
            "facts": run_events_facts, "judge": judge_run_events},
    "C21": {"title": "synchronous cloud run start with a failed reply: status and exit",
            "kind": "tier0-offline", "scenarios": _failed_sync_scenarios,
            "facts": failed_sync_facts, "judge": judge_failed_sync},
    "C22": {"title": "start/stop poll hint names a command the CLI accepts",
            "kind": "tier0-offline", "scenarios": _hint_scenarios, "facts": hint_facts,
            "judge": judge_hint},
    "C23": {"title": "cloud deployment start/stop: lifecycle route selection and poll hint",
            "kind": "tier0-offline", "scenarios": _cloud_route_scenarios,
            "facts": cloud_route_facts, "judge": judge_cloud_route},
    "C24": {"title": "--env secrets file: what managed-preview/update send as secrets",
            "kind": "tier0-offline", "scenarios": _env_scenarios, "facts": env_facts,
            "judge": judge_env},
    "C26": {"title": "cloud run get: failed run output in the default view",
            "kind": "tier0-offline", "scenarios": _run_detail_scenarios,
            "facts": run_detail_facts, "judge": judge_run_detail},
    "C27": {"title": "managed-update run limits via --agent-config: set, clear, tool calls",
            "kind": "tier0-offline", "scenarios": _limits_scenarios, "facts": limits_facts,
            "judge": judge_limits},
    "C28": {"title": "cloud runs list: what the COST column reports for a run",
            "kind": "tier0-offline", "scenarios": _cost_scenarios, "facts": cost_facts,
            "judge": judge_costs},
    "C29": {"title": "managed-rollback recovery: preconditions, 409 conflict, ambiguous send",
            "kind": "tier0-offline", "scenarios": _rollback_scenarios,
            "facts": rollback_facts, "judge": judge_rollback},
    "C30": {"title": "cloud agent schedules: create request and failed-occurrence visibility",
            "kind": "tier0-offline", "scenarios": _schedule_scenarios,
            "facts": schedule_facts, "judge": judge_schedules},
    "C31": {"title": "cloud conversation messages: unavailable content and paging",
            "kind": "tier0-offline", "scenarios": _conversation_scenarios,
            "facts": conversation_facts, "judge": judge_conversations},
    "C32": {"title": "cloud run cancel: route, requested vs cancelled status, exit",
            "kind": "tier0-offline", "scenarios": _cancel_scenarios, "facts": cancel_facts,
            "judge": judge_cancel},
    "C34": {"title": "agent tasks-get --follow: spec task events, failure reason, exit",
            "kind": "tier0-offline", "scenarios": _task_scenarios, "facts": task_facts,
            "judge": judge_tasks},
    "C35": {"title": "failed or stuck start: what managed-get and deployment status show",
            "kind": "tier0-offline", "scenarios": _start_view_scenarios,
            "facts": start_view_facts, "judge": judge_start_views},
    "C37": {"title": "accepted stop: the server's note, and a failed deployment's status JSON",
            "kind": "tier0-offline", "scenarios": _stop_view_scenarios,
            "facts": stop_view_facts, "judge": judge_stop_views},
    "C38": {"title": "cloud run get -o json: a run's answer text events reproduced exactly",
            "kind": "tier0-offline", "scenarios": _answer_scenarios, "facts": answer_facts,
            "judge": judge_answer},
}
ORDER = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C12", "C13",
         "C14", "C15", "C16", "C17", "C18", "C19", "C20", "C21", "C22", "C23", "C24", "C26",
         "C27", "C28", "C29", "C30", "C31", "C32", "C34", "C35", "C37", "C38")
# Shared helpers every case depends on; changing one makes every case due again.
SHARED = (spec, run_scenario, saved_credentials, to, ending, printed, stream_facts,
          meaningful, first_error_line, result)


def execute(lab, case_id, repeats=3, record=None):
    """Run every scenario `repeats` times; facts must agree across repeats."""
    case = CASES[case_id]
    scenarios = case["scenarios"]()
    facts, observations = {}, []
    for scenario in scenarios:
        seen = []
        for rep in range(repeats):
            obs = run_scenario(lab, scenario)
            fact = case["facts"](obs)
            observations.append({"scenario": scenario.name, "role": scenario.role,
                                 "repeat": rep, "args": scenario.args,
                                 "auth": "saved" if isinstance(scenario.auth, dict)
                                 else scenario.auth,
                                 "extra_env": {k: repr(v) for k, v in
                                               scenario.extra_env.items()},
                                 "routes": {"%s %s" % k: v for k, v in
                                            scenario.routes.items()},
                                 "observation": obs, "facts": fact})
            if record:
                record(observations[-1])
            seen.append(fact)
        if any(f != seen[0] for f in seen):
            return {"case": case_id, "results": [result(
                case_id, scenario.name, "inconclusive",
                "facts differed across %d repeats of %s" % (repeats, scenario.name))],
                "facts": facts, "observations": observations}
        facts[scenario.name] = seen[0]
    return {"case": case_id, "results": case["judge"](facts), "facts": facts,
            "observations": observations}
