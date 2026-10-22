"""SIMULATION ONLY: a stand-in Seren CLI for offline harness tests and demos.

It mimics the v0.10.0 command shapes the harness uses (see
seren_harness/contract.py) over a local JSON state file, and runs a simulated
employee process that replies in a JSON-lines channel file. Its behavior is
scripted by the scenario; it proves nothing about the real Seren service.

Usage: fake_seren.py <state-file> <seren arguments...>
       fake_seren.py <state-file> sim-approve <setup-id>
       fake_seren.py <state-file> sim-change-revision
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

DISCORD_EPOCH_MS = 1420070400000
SECRET_CANARY = "SIMULATED-SECRET-CANARY-7f3a9c"
DEFAULT_SCENARIO = {"start": "accept", "readiness": "ready", "ready_after": 2, "reply": "bound",
                    "stop": "ok", "setup": "normal", "canary": True, "sleep_seconds": 3,
                    "managed_get_output": "json", "activity_output": "json",
                    "start_save_delay_seconds": 0, "reply_clock": "wall",
                    # None: "ACK " plus the probe's marker. A string is posted with
                    # "{marker}" replaced; false posts a reply with no content field.
                    "reply_text": None}
SPAWN_AFTER_SAVE = []


def install(directory, scenario=None, channel_id="sim-channel", employee_user_id="sim-employee"):
    """Create a simulated CLI executable and its state; return (executable, state, channel)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "sim-state.json"
    channel_path = directory / "sim-channel.jsonl"
    deployment_id = str(uuid.uuid4())
    state = {
        "version_text": "seren 0.10.0 (simulation)",
        "scenario": dict(DEFAULT_SCENARIO, **(scenario or {})),
        "channel_file": str(channel_path), "channel_id": channel_id,
        "employee_user_id": employee_user_id,
        "deployment": {
            "deployment_id": deployment_id, "name": "Simulated Test Employee",
            "agent_slug": "sim-agent", "status": "stopped", "desired_lifecycle_state": "stopped",
            "desired_egress_state": "enabled", "control_generation": 1,
            "active_revision_id": str(uuid.uuid4()), "agent_identity_id": str(uuid.uuid4()),
            "secret_resolution_result_id": None, "capability_policy": {"mode": "default"},
            "runtime_policy": {"replicas": 1}, "side_effect_policy": {"mutations": "deny"},
            "requirements": {"channels": [{"provider": "discord", "activation":
                                           "mention_or_reply"}]},
            "approval_policy": "read_only",
            "conditions": [{"type": "AwaitingApproval", "status": "True"},
                           {"type": "Ready", "status": "False"}]},
        "runs": [], "setups": {}, "polls_since_start": 0, "log": []}
    state_path.write_text(json.dumps(state, indent=2))
    executable = directory / "seren"
    executable.write_text("#!" + sys.executable + "\n"
                          "import runpy, sys\n"
                          "sys.argv = [" + repr(__file__) + ", " + repr(str(state_path))
                          + "] + sys.argv[1:]\n"
                          "runpy.run_path(" + repr(str(Path(__file__).resolve()))
                          + ", run_name='__main__')\n")
    executable.chmod(0o755)
    # The first execution of a newly written executable was measured at a median 0.43 s
    # and up to 2.04 s under load (later executions about 0.03 s), enough to exceed a
    # short command timeout. Pay that cost here, outside any timed harness command.
    subprocess.run([str(executable), "sim-warm"], check=True, stdin=subprocess.DEVNULL)
    return executable, state_path, channel_path, deployment_id


@contextlib.contextmanager
def locked_state(path):
    with open(path, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = json.load(stream)
        yield state
        stream.seek(0)
        stream.truncate()
        json.dump(state, stream, indent=2)


def read_state(path):
    """Read the state under a shared lock; writers truncate before rewriting."""
    with open(path, encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        return json.load(stream)


def envelope(data):
    return {"data": data, "pagination": None}


def fail(message):
    print("Error: " + message, file=sys.stderr)
    return 1


def set_condition(deployment, kind, status):
    for item in deployment["conditions"]:
        if item["type"] == kind:
            item["status"] = status
            return
    deployment["conditions"].append({"type": kind, "status": status})


def snowflake(ms=None):
    ms = int(time.time() * 1000) if ms is None else ms
    return str((ms - DISCORD_EPOCH_MS) << 22)


def cmd_get(state):
    deployment = state["deployment"]
    scenario = state["scenario"]
    if scenario["managed_get_output"] == "garbage":
        print("<html>upstream proxy error</html>")
        return 0
    if deployment["desired_lifecycle_state"] == "running":
        state["polls_since_start"] += 1
        if scenario["readiness"] == "ready" and state["polls_since_start"] >= scenario["ready_after"]:
            deployment["status"] = "running"
            set_condition(deployment, "Ready", "True")
    elif deployment["desired_lifecycle_state"] == "stopping":
        if scenario["stop"] != "never_stops":
            deployment.update(status="stopped", desired_lifecycle_state="stopped")
            set_condition(deployment, "Ready", "False")
    print(json.dumps(envelope(deployment), indent=2))
    return 0


def cmd_health(state):
    deployment = state["deployment"]
    findings = [{"id": "rollout", "severity": "info", "category": "rollout",
                 "title": "MinimumReplicasUnavailable", "detail": "replicas_pending:runtime",
                 "recommendation": "wait", "source": "simulation"}]
    if state["scenario"]["canary"]:
        findings.append({"id": "leak", "severity": "info", "category": "diagnostic",
                         "title": "diagnostic echo", "source": "simulation",
                         "detail": "env DISCORD_BOT_TOKEN=" + SECRET_CANARY + " setup "
                                   "https://passwords.example.invalid/launch/"
                                   "Zm9vYmFyYmF6cXV4cXV1eGNhcGFiaWxpdHk?code=" + SECRET_CANARY,
                         "recommendation": "none"})
    print(json.dumps(envelope({"status": "ok", "summary": {}, "findings": findings,
                               "deployment": {"deployment_id": deployment["deployment_id"],
                                              "status": deployment["status"]}}), indent=2))
    return 0


def cmd_activity(state):
    deployment = state["deployment"]
    if state["scenario"]["activity_output"] == "unexpected_shape":
        print(json.dumps(envelope({"runs": {"run": "not a list"}})))
        return 0
    if state["scenario"]["readiness"] == "binding_failure" and \
            deployment["desired_lifecycle_state"] == "running" and len(state["runs"]) < 5:
        state["runs"].append({"run_id": str(uuid.uuid4()), "execution_id": str(uuid.uuid4()),
                              "source": "dispatch_channel", "status": "failed",
                              "stop_reason": "channel_binding_failure",
                              "status_message": "[redacted by private_session_database]",
                              "compute_cost_usd": "0", "inference_cost_usd": "0",
                              "started_at": "2026-10-09T00:00:00Z"})
    print(json.dumps(envelope({"deployment_id": deployment["deployment_id"],
                               "summary": {}, "runs": state["runs"]}), indent=2))
    return 0


def action_response(deployment, status):
    return envelope({"status": status, "confirmation": None,
                     "desired_lifecycle_state": deployment["desired_lifecycle_state"],
                     "desired_egress_state": "enabled",
                     "control_generation": deployment["control_generation"]})


def cmd_start(state, state_path):
    deployment = state["deployment"]
    mode = state["scenario"]["start"]
    if mode == "reject_403":
        return fail("start deployment: unexpected response 403 Forbidden - "
                    "{\"error\":\"forbidden\"}")
    if mode == "error_503_not_applied":
        return fail("Failed to start managed agent deployment: unexpected response 503 "
                    "Service Unavailable - upstream")
    if mode not in ("timeout_not_applied", "error_503_not_applied"):
        deployment.update(desired_lifecycle_state="running", status="building")
        deployment["control_generation"] += 1
        state["polls_since_start"] = 0
        # Spawned by main() only after this state is saved: the employee exits as soon as
        # it reads a deployment that is not running.
        SPAWN_AFTER_SAVE.append(state_path)
    if mode.startswith("timeout"):
        return "sleep"
    if mode == "error_applied":  # The request applied, but its answer was lost.
        return fail("Failed to start managed agent deployment: error sending request for url "
                    "(https://api.example.invalid/agent/managed/start)")
    print(json.dumps(action_response(deployment, "accepted"), indent=2))
    return 0


def cmd_stop(state):
    deployment = state["deployment"]
    mode = state["scenario"]["stop"]
    if mode == "reject":
        return fail("stop deployment: unexpected response 400 Bad Request - "
                    "{\"error\":\"stop_rejected\"}")
    deployment.update(desired_lifecycle_state="stopping")
    deployment["control_generation"] += 1
    if mode == "timeout_applied":
        return "sleep"
    print(json.dumps(action_response(deployment, "stop latched"), indent=2))
    return 0


def cmd_setup(state):
    setup_id = str(uuid.uuid4())
    state["setups"][setup_id] = {"status": "unconsumed"}
    if state["scenario"]["setup"] == "timeout":  # Created, but the caller sees no answer.
        return "sleep"
    print(json.dumps({"status": "unconsumed", "setup_id": setup_id,
                      "deployment_id": state["deployment"]["deployment_id"],
                      "launch_url": "https://passwords.example.invalid/launch/"
                                    "c2ltdWxhdGVkY2FwYWJpbGl0eXRva2Vu?code=" + SECRET_CANARY,
                      "expires_at": "2026-10-09T23:59:00Z",
                      "requested_fields": ["DISCORD_BOT_TOKEN"]}, indent=2))
    return 0


def cmd_setup_status(state, setup_id):
    setup = state["setups"].get(setup_id)
    if setup is None:
        return fail("setup status: unexpected response 404 Not Found - {}")
    body = {"status": setup["status"], "next_step": "simulated"}
    if setup.get("reason"):
        body["reason"] = setup["reason"]
    print(json.dumps(body, indent=2))
    return 0


def cmd_apply(state, setup_id):
    setup = state["setups"].get(setup_id)
    deployment = state["deployment"]
    if setup is None or setup["status"] not in ("approved", "applied"):
        return fail("apply: unexpected response 409 Conflict - setup is not approved")
    if state["scenario"]["setup"] == "apply_timeout_applied":
        state["scenario"]["setup"] = "normal"  # Only the first apply loses its answer.
        setup["result_id"] = str(uuid.uuid4())
        deployment["secret_resolution_result_id"] = setup["result_id"]
        set_condition(deployment, "AwaitingApproval", "False")
        return "sleep"  # Setup status stays `approved`, as the server may report it.
    if setup.get("result_id") and deployment["secret_resolution_result_id"] == setup["result_id"]:
        setup["status"] = "applied"
    already = setup["status"] == "applied"
    if not already:
        setup["status"] = "applied"
        setup["result_id"] = str(uuid.uuid4())
        deployment["secret_resolution_result_id"] = setup["result_id"]
        set_condition(deployment, "AwaitingApproval", "False")
    if state["scenario"]["setup"] == "rebound_after_apply":  # Another change lands at once.
        deployment["secret_resolution_result_id"] = str(uuid.uuid4())
    print(json.dumps({"status": "applied", "setup_id": setup_id,
                      "deployment_id": deployment["deployment_id"],
                      "result_id": setup["result_id"],
                      "active_revision_id": deployment["active_revision_id"],
                      "already_applied": already}, indent=2))
    return 0


def sim_approve(state, setup_id):
    setup = state["setups"][setup_id]
    if state["scenario"]["setup"] == "rejected":
        setup.update(status="rejected", reason={"kind": "setup_consume_rejected",
                                                "message": "Start this setup again."})
    else:
        setup["status"] = "approved"
    return 0


def note_employee(state_path, **event):
    """Diagnostics: one JSON line per employee start/exit, beside the state file."""
    event = dict(event, pid=os.getpid(), at=round(time.time(), 3))
    with open(Path(state_path).parent / "sim-employee.log", "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def wait_for_employees(state_path, timeout=10):
    """After a stop, wait until every spawned employee has logged its exit.

    Employees write their exit line beside the state file, so removing that
    directory before they finish races with the write.
    """
    log = Path(state_path).parent / "sim-employee.log"
    deadline = time.time() + timeout
    while True:
        events = []
        for line in log.read_text().splitlines() if log.exists() else []:
            try:
                events.append(json.loads(line))
            except ValueError:  # A line still being appended; read again.
                pass
        spawned = {e["child"] for e in events if e["event"] == "spawned"}
        running = spawned - {e["pid"] for e in events if e["event"] == "exit"}
        if not running:
            return
        if time.time() > deadline:
            raise RuntimeError("simulated employees still running: " + str(sorted(running)))
        time.sleep(0.02)


def employee(state_path):
    """Simulated employee: replies to mentions while the deployment is running and Ready."""
    answered = set()
    deadline = time.time() + 60
    reads = 0
    note_employee(state_path, event="start")
    while time.time() < deadline:
        try:
            state = json.loads(Path(state_path).read_text())
        except ValueError:  # Caught mid-write; read again.
            time.sleep(0.02)
            continue
        reads += 1
        deployment = state["deployment"]
        if reads == 1:
            note_employee(state_path, event="first_read",
                          desired=deployment["desired_lifecycle_state"])
        if deployment["desired_lifecycle_state"] != "running":
            note_employee(state_path, event="exit", reason="not_running", reads=reads,
                          desired=deployment["desired_lifecycle_state"],
                          generation=deployment["control_generation"], answered=len(answered))
            return 0
        ready = any(c["type"] == "Ready" and c["status"] == "True"
                    for c in deployment["conditions"])
        channel = Path(state["channel_file"])
        if ready and channel.exists():
            mention = "<@" + state["employee_user_id"] + ">"
            for line in channel.read_text().splitlines():
                message = json.loads(line)
                if mention in message.get("content", "") and message["id"] not in answered:
                    answered.add(message["id"])
                    reply(state, channel, message)
        time.sleep(0.05)
    note_employee(state_path, event="exit", reason="deadline", reads=reads,
                  answered=len(answered))
    return 0


def reply(state, channel, probe):
    mode = state["scenario"]["reply"]
    if mode == "none":
        return
    author = state["employee_user_id"] if mode != "wrong_author" else "sim-other-bot"
    # `reply_clock: probe_millisecond` models a reply in the probe's own millisecond.
    ms = (int(probe["id"]) >> 22) + DISCORD_EPOCH_MS \
        if state["scenario"]["reply_clock"] == "probe_millisecond" else None
    # Discord ids are unique; a wall-clock-only id collides with the probe's when the
    # reply lands in the same millisecond, and the reply then reads as the probe itself.
    existing = [int(json.loads(line)["id"]) for line in channel.read_text().splitlines()]
    message = {"id": str(max(int(snowflake(ms)), max(existing, default=0) + (1 << 22))),
               "channel_id": state["channel_id"],
               "author": {"id": author, "bot": True}}
    marker = next((w for w in probe.get("content", "").split() if w.startswith("SH-")), "")
    text = state["scenario"].get("reply_text")
    if text is not False:
        message["content"] = "ACK " + marker if text is None else text.replace("{marker}",
                                                                               marker)
    if mode != "unrelated":
        message["message_reference"] = {"message_id": probe["id"],
                                        "channel_id": state["channel_id"]}
    with open(channel, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(message) + "\n")


def dispatch(state, state_path, args):
    if args in (["--version"], ["-V"]):
        print(state["version_text"])
        return 0
    if args[-2:] == ["-o", "json"]:
        args = args[:-2]
    if len(args) < 2 or args[0] != "agent":
        return fail("simulation does not implement: " + " ".join(args))
    command, rest = args[1], args[2:]
    if command == "managed-get":
        return cmd_get(state)
    if command == "managed-deployment-health":
        return cmd_health(state)
    if command == "managed-deployment-activity":
        return cmd_activity(state)
    if command == "managed-start":
        return cmd_start(state, state_path)
    if command == "managed-stop":
        return cmd_stop(state)
    if command == "managed-passwords-setup":
        return cmd_setup(state)
    if command == "managed-passwords-status":
        return cmd_setup_status(state, rest[0])
    if command == "managed-passwords-apply":
        return cmd_apply(state, rest[0])
    return fail("simulation does not implement: " + command)


def main(argv):
    state_path, args = argv[1], argv[2:]
    if args == ["sim-employee"]:
        return employee(state_path)
    if args == ["sim-warm"]:  # See install(): no state access, no log entry.
        return 0
    with locked_state(state_path) as state:
        state["log"].append(args)
        state.setdefault("timeline", []).append([round(time.time(), 3), args[:2]])
        if args[:1] == ["sim-approve"]:
            return sim_approve(state, args[1])
        if args == ["sim-change-revision"]:
            state["deployment"]["active_revision_id"] = str(uuid.uuid4())
            return 0
        if args[:1] == ["sim-set"]:
            state["scenario"].update(json.loads(args[1]))
            return 0
        code = dispatch(state, state_path, args)
        sleep = state["scenario"]["sleep_seconds"]
        if args[1:2] == ["managed-start"]:  # Models a descheduled process before saving.
            time.sleep(state["scenario"]["start_save_delay_seconds"])
    if args[1:2] == ["managed-start"]:
        note_employee(state_path, event="start_state_saved")
    for path in SPAWN_AFTER_SAVE:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(path),
                                  "sim-employee"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        note_employee(path, event="spawned", child=child.pid)
    if code == "sleep":
        time.sleep(sleep)  # The state change (if any) is saved; the caller sees no answer.
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
