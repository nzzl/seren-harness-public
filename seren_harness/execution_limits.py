"""Execution ceilings for the Discord response-correctness package, derived from code.

Counting units (kept separate; none stands in for another):
- **service invocations**: Seren CLI invocations the harness issues to the service (every
  `ctx.cli.json` call; the local `--version` check is excluded). The harness's own unit,
  visible in each run's command history.
- **HTTP requests (upper bound)**: Seren API requests those invocations can make, from the
  pinned CLI v0.10.0 source (`cli/src/commands/agent.rs`). Not observable by the harness.
  Managed mutations may re-send once with the same request id and poll while pending; the
  harness's command timeout cuts that polling off.
- **OAuth refreshes**: at most one token request per CLI invocation, only near expiry
  (`auth.rs`). Unobservable; its ceiling equals the service invocations.
- **Discord reads**: REST GETs by the reader observer, one per observation.

Polling loops in seren.py read, then check their deadline, then sleep. With a deadline L
and interval I, a loop makes at most floor(L / I) + 1 reads; command time only lowers that.
"""
from __future__ import annotations

import math

PINNED = {"command_timeout_seconds": 60, "poll_interval_seconds": 5,
          "readiness_timeout_seconds": 180, "shutdown_timeout_seconds": 120,
          "reply_timeout_seconds": 120, "max_apply_attempts": 1}
PROVISIONING_RESUMES = 4      # operator-enforced: at most four `resume` invocations
DISCORD_READ_TIMEOUT = 20     # discord.RestObserver urlopen timeout
LOCAL_OVERHEAD = 2            # assumed per-invocation process start/kill overhead (not enforced)
RECOVERY_DELAY = 600          # procedure: the stop-recovery run starts within 10 min of the run
APPROVAL_WAIT = 20 * 60       # procedure: owner Passwords approval wait
WINDOW_MINUTES = 105          # overall window for the whole package
CLI_MUTATION_POLL_SECONDS = 2  # agent.rs MANAGED_MUTATION_POLL_INTERVAL


def loop_reads(limit, interval):
    """Maximum reads of a read-then-check-deadline-then-sleep loop."""
    return math.floor(limit / interval) + 1


def _settings(config):
    return {"command_timeout_seconds": config.command_timeout,
            "poll_interval_seconds": config.poll_interval,
            "readiness_timeout_seconds": config.readiness_timeout,
            "shutdown_timeout_seconds": config.shutdown_timeout,
            "reply_timeout_seconds": config.reply_timeout,
            "max_apply_attempts": config.max_apply_attempts}


def check_pins(config, discord=True):
    """Problems that make the configuration differ from the package's pinned settings.
    `discord=False` drops only the Discord reply/observer pins, for a package that uses none."""
    actual = _settings(config)
    problems = [key + " is " + str(actual[key]) + ", pinned " + str(value)
                for key, value in PINNED.items() if actual[key] != value]
    if not discord:
        return problems
    if config.expected_reply is None or config.expected_reply.get("template") != "ACK {marker}":
        problems.append("discord.expected_reply must be the sourced 'ACK {marker}' template")
    if config.sender != "human" or config.observer != "discord_rest":
        problems.append("discord sender must be human and observer discord_rest")
    return problems


def http_per_invocation(command_timeout):
    """Upper bound of Seren API requests per CLI command (pinned v0.10.0 source)."""
    polls = math.floor(command_timeout / CLI_MUTATION_POLL_SECONDS)
    submit = 3 + polls  # send, status lookup, one same-id re-send, pending polls
    return {"managed-get": 1, "cloud deployment status": 1, "managed-deployment-activity": 1,
            "managed-deployment-health": 1, "managed-passwords-status": 1,
            "managed-preview": 1, "managed-start": 1, "managed-stop": 1,
            "managed-passwords-setup": 2,  # deployment summary, setup POST
            # setup read, deployment summary, apply target, detail, submit, verifying read
            "managed-passwords-apply": 4 + submit + 1,
            "managed-update": submit}


def ceilings(settings=None, resumes=PROVISIONING_RESUMES):
    """Per-phase command counts, mutations, Discord reads, HTTP and wall-clock bounds."""
    s = dict(PINNED, **(settings or {}))
    interval, command = s["poll_interval_seconds"], s["command_timeout_seconds"]
    ready = loop_reads(s["readiness_timeout_seconds"], interval)
    observe = loop_reads(s["reply_timeout_seconds"], interval)
    confirm = loop_reads(s["shutdown_timeout_seconds"], interval)
    lifecycle = {"managed-get": 1, "cloud deployment status": 1}  # detail lacks intent

    def add(*parts):
        total = {}
        for part in parts:
            for key, value in part.items():
                total[key] = total.get(key, 0) + value
        return total

    stop_and_confirm = add(lifecycle, {"managed-stop": 1}, lifecycle,  # reconcile if unknown
                           {"managed-get": confirm})
    phases = {
        "update": add({"managed-get": 1, "cloud deployment status": 1},  # prerequisite
                      {"managed-preview": 1, "managed-get": 1, "managed-update": 1},
                      {"managed-get": 2}),  # read-back, one read for an unknown outcome
        "provisioning": add({"managed-get": 1, "managed-passwords-setup": 1,
                             "managed-passwords-status": 1},
                            *[{"managed-passwords-status": 3, "managed-get": 2,
                               "managed-passwords-apply": 0} for _ in range(resumes)],
                            {"managed-passwords-apply": s["max_apply_attempts"]}),
        "operational": add({"managed-get": 1}, lifecycle,
                           {"managed-deployment-activity": 1, "managed-start": 1},
                           lifecycle,  # start reconciliation if its outcome is unknown
                           {"managed-get": ready, "managed-deployment-health": ready,
                            "managed-deployment-activity": ready}),
        "cleanup": stop_and_confirm,
        "stop_recovery": add(stop_and_confirm, {"managed-get": 1}, lifecycle,
                             {"managed-deployment-activity": 1},
                             {"managed-get": 1, "managed-deployment-health": 1,
                              "managed-deployment-activity": 1}),
    }
    http = http_per_invocation(command)
    mutation_kinds = ("managed-update", "managed-passwords-setup", "managed-passwords-apply",
                      "managed-start", "managed-stop")
    # Wall-clock bounds in seconds. Each invocation takes at most the command timeout (the
    # wrapper kills and reaps it) plus an assumed local overhead; each loop stops at its
    # first deadline check at or after its limit, so it lasts at most limit + interval + one
    # iteration. Operator delays are procedure bounds, not enforced by code.
    per = command + LOCAL_OVERHEAD
    discord_read = DISCORD_READ_TIMEOUT + LOCAL_OVERHEAD
    wall = {
        "update": 7 * per,
        "provisioning_commands": sum(phases["provisioning"].values()) * per,
        # verify 1, initial 3, start 1, start reconciliation 2; then the three loops
        "operational": 7 * per + (s["readiness_timeout_seconds"] + interval + 3 * per)
        + 2 * (s["reply_timeout_seconds"] + interval + discord_read),
        # lifecycle 2, stop 1, stop reconciliation 2; then the confirmation loop
        "cleanup": 5 * per + s["shutdown_timeout_seconds"] + interval + per,
    }
    wall["stop_recovery"] = wall["cleanup"] + 7 * per  # its stop, then the control run's reads
    started = wall["operational"] - 4 * per  # from the start request (after verify, initial)
    time_model = {
        "assumptions": {"command_timeout_seconds": command,
                        "local_overhead_seconds_per_invocation": LOCAL_OVERHEAD,
                        "recovery_start_delay_seconds_max": RECOVERY_DELAY,
                        "owner_approval_wait_seconds_max": APPROVAL_WAIT},
        "ordinary_execution_seconds": wall["operational"],
        "initial_shutdown_seconds": wall["cleanup"],
        "stop_recovery_seconds": wall["stop_recovery"],
        # Runtime from the start request until a confirmed stop. If shutdown is never
        # confirmed there is no bound: runtime continues and is reported as unknown.
        "runtime_until_confirmed_stop_seconds": {
            "confirmed_by_initial_cleanup": started + wall["cleanup"],
            "confirmed_by_stop_recovery": started + wall["cleanup"] + RECOVERY_DELAY
            + wall["stop_recovery"],
            "unconfirmed": None},
        "operational_start_gate_seconds": wall["operational"] + wall["cleanup"]
        + RECOVERY_DELAY + wall["stop_recovery"],
        "window_minutes": WINDOW_MINUTES,
        "window_budget_seconds": {"update": wall["update"],
                                  "provisioning": APPROVAL_WAIT + wall["provisioning_commands"]},
    }
    result = {"settings": s, "provisioning_resumes": resumes,
              "loop_reads": {"readiness": ready, "probe": observe, "reply": observe,
                             "shutdown_confirmation": confirm},
              "phases": {}}
    for name, counts in phases.items():
        counts = {k: v for k, v in counts.items() if v}
        result["phases"][name] = {
            "by_command": counts, "service_invocations": sum(counts.values()),
            "mutations": {k: counts[k] for k in mutation_kinds if counts.get(k)},
            "http_upper": sum(http[k] * v for k, v in counts.items()),
            "wall_seconds_upper": wall.get(name, wall.get("provisioning_commands"))}
    result["phases"]["update"]["uncertain_mutations"] = {"managed-preview": 1}
    result["phases"]["operational"]["discord_reads"] = 2 * observe
    ordinary = ("update", "provisioning", "operational")
    reserve = ("cleanup", "stop_recovery")
    total = lambda names, key: sum(result["phases"][n][key] for n in names)  # noqa: E731
    result["totals"] = {
        "service_invocations": {"ordinary": total(ordinary, "service_invocations"),
                                "cleanup_reserve": total(reserve, "service_invocations")},
        "http_upper": {"ordinary": total(ordinary, "http_upper"),
                       "cleanup_reserve": total(reserve, "http_upper")},
        "discord_reads": 2 * observe,
        "oauth_refresh_upper": total(ordinary + reserve, "service_invocations")}
    result["time_model"] = time_model
    return result


def runtime_bound(outcome, settings=None):
    """Runtime bound for a shutdown outcome: 'initial', 'recovery' or 'unconfirmed' (None)."""
    model = ceilings(settings)["time_model"]["runtime_until_confirmed_stop_seconds"]
    return {"initial": model["confirmed_by_initial_cleanup"],
            "recovery": model["confirmed_by_stop_recovery"], "unconfirmed": None}[outcome]


def operational_may_start(remaining_seconds, settings=None):
    """Window gate: enough time remains for the run, cleanup and one stop recovery."""
    return remaining_seconds >= ceilings(settings)["time_model"]["operational_start_gate_seconds"]


def run(args, out=None):
    import json
    import sys
    from seren_harness.config import ConfigError, load
    out = out or sys.stdout
    try:
        config = load(args.config)
    except (ConfigError, OSError, ValueError) as error:
        print("Cannot read the configuration: " + str(error), file=out)
        return 5
    problems = check_pins(config)
    print(json.dumps({"pin_problems": problems, "ceilings": ceilings(_settings(config))},
                     indent=2), file=out)
    return 2 if problems else 0
