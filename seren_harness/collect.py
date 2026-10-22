"""Automatic collection: gather a case's evidence through a configured interface, then
review it with the existing engine and store. No operator files, JSON or questions.

Collectors are target configuration (targets/<target>.json), never engine branches:
- `command` runs a configured local adapter that prints {"messages": [...]} as JSON;
- `--from-copy` imports a retained Discord copy, split only at explicit message headers.
Provenance comes from the collector, never from a default that upgrades evidence.
Human judgments are not collected here; they stay unverified unless supplied later.
"""
from __future__ import annotations


import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from seren_harness import sql_probe
from seren_harness.contract import SUPPORTED_CLI_VERSIONS
from seren_harness.guide import _new_folder, discord_messages, workflow_cases
from seren_harness.models import LIVE, utc_now
from seren_harness.seren_cli import DECODE_ERROR, TIMEOUT, UNAVAILABLE, SerenCli
from seren_harness.report import _write_private
from seren_harness.response_review import read_json, read_text
from seren_harness.sanitize import Sanitizer
from seren_harness.state import Store, private_directory
from seren_harness.workflow_review import (KINDS, MODES, READING, candidate_findings, review,
                                           summary_line)

# Adapters render or relay output themselves; they may not claim operator or independent
# observation, which would let local output satisfy a live-evidence scope.
COMMAND_PROVENANCE = ("local", "simulated")
# Seren CLI commands the `seren_cli` collector may run: documented read-only (GET) operations.
SEREN_READ_ONLY = {("agent", "managed-deployment-tools"),
                   ("agent", "cloud", "deployment", "status")}
# The one flagged read-only shape: `agent cloud run get --deployment-id <uuid> <run uuid>`
# (v0.10.0 cloud_run_get: a single GET of /deployments/{id}/runs/{run_id}).
RUN_GET = ("agent", "cloud", "run", "get", "--deployment-id")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ATTEMPTS = 2  # one bounded retry for transient failures (for example, the app restarting)


def read_only_args(args):
    """True only for a declared read-only command with exactly its supported arguments."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return False
    if len(args) == len(RUN_GET) + 2 and tuple(args[:len(RUN_GET)]) == RUN_GET:
        return all(UUID.fullmatch(a) for a in args[len(RUN_GET):])
    return tuple(args[:-1]) in SEREN_READ_ONLY and not any(a.startswith("-") for a in args)
OK, FAILED_VERDICT, INCOMPLETE, COLLECTION_FAILED, INVALID = 0, 1, 2, 3, 5


class CollectionError(Exception):
    """The configured interface did not produce usable evidence (no review recorded)."""


def load_target(path, root=None):
    target = read_json(path)
    if not isinstance(target.get("collectors"), dict):
        raise ValueError("target configuration needs collectors")
    target["root"] = str(Path(root or target.get("root") or ".").expanduser())
    return target


def _messages(payload):
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CollectionError("the adapter returned no messages")
    if sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "initial") != 1:
        raise CollectionError("the adapter must return exactly one initial message")
    for m in messages:
        if (not isinstance(m, dict) or m.get("role") not in READING + ("alternative",)
                or m.get("kind") not in KINDS or not isinstance(m.get("text"), str)
                or not m["text"].strip() or not isinstance(m.get("invocation", ""), str)):
            raise CollectionError("the adapter returned a malformed message")
    return messages


def run_command(collector, target, harness_root, attempts=ATTEMPTS):
    argv = [a.replace("{root}", target["root"]).replace("{harness}", str(harness_root))
            for a in collector["argv"]]
    timeout = int(collector.get("timeout_seconds", 60))
    problems = []
    for _ in range(attempts):
        try:
            result = subprocess.run(argv, cwd=target["root"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            problems.append("timed out after " + str(timeout) + "s")
            continue
        except OSError as error:
            # A missing program will not appear on retry.
            raise CollectionError("the adapter could not start (" + type(error).__name__
                                  + ")") from None
        if result.returncode != 0:
            problems.append("exited " + str(result.returncode))
            continue
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            raise CollectionError("the adapter output was not JSON") from None
        return payload, problems
    raise CollectionError("the adapter failed " + str(attempts) + " time(s): "
                          + "; ".join(problems))


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run_seren_cli(collector, target, sanitizer, attempts=ATTEMPTS, record=None):
    """Call one allowlisted read-only command through the pinned Seren CLI and return the
    harness's own envelope of the answer. Credentials stay in the CLI's existing session; no
    API key or API base reaches the process. A non-zero exit is an answer (recorded and
    evaluated); only a timeout is retried (never with attempts=1 or SQL), and an unstartable
    or unreadable CLI records nothing. The local `--version` check is not a service request;
    `record["service_requests"]` counts each service command just before it is issued."""
    record = {} if record is None else record
    if any(not isinstance(collector.get(k), str) or not collector[k]
           for k in ("executable", "executable_sha256", "config_home", "profile")):
        raise CollectionError("the Seren CLI collector needs executable, executable_sha256, "
                              "config_home and profile")
    expand = lambda value: value.replace("{root}", target["root"])  # noqa: E731
    executable, args = expand(collector["executable"]), collector.get("args")
    sql = sql_probe.allowed(args)
    if not sql and not read_only_args(args):
        raise CollectionError("the Seren CLI collector allows only declared read-only commands")
    try:
        identity = _sha256(executable)
    except OSError:
        raise CollectionError("the Seren CLI executable is missing") from None
    if identity != collector["executable_sha256"]:
        raise CollectionError("the Seren CLI executable does not match its pinned checksum")
    env = {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin",
           "XDG_CONFIG_HOME": expand(collector["config_home"]),
           "SEREN_PROFILE": collector["profile"]}
    if sql:
        psql = collector.get("psql_executable", "")
        try:
            if (not Path(psql).is_absolute() or Path(psql).name != "psql"
                    or not os.access(psql, os.X_OK)
                    or _sha256(psql) != collector.get("psql_sha256")):
                raise CollectionError("the psql executable does not match its pinned checksum")
        except OSError:
            raise CollectionError("the pinned psql executable is missing") from None
        env = sql_probe.environment(env, psql)
    cli_type = sql_probe.SqlProbeCli if sql else SerenCli
    timeout = int(collector.get("timeout_seconds", 30))
    if sql and not 1 <= timeout <= 30:
        raise CollectionError("the SQL probe timeout must be between 1 and 30 seconds")
    cli = cli_type(executable, timeout, sanitizer, env)
    version = cli.version()
    found = re.findall(r"\d+\.\d+\.\d+", str(version.data or ""))
    if not version.ok or not found or found[0] not in SUPPORTED_CLI_VERSIONS:
        raise CollectionError("the Seren CLI version is not a supported contract")
    problems, started = [], utc_now()
    attempts = 1 if sql else attempts
    for _ in range(attempts):
        record["service_requests"] = record.get("service_requests", 0) + 1
        result = cli.json(*args)
        if result.status != TIMEOUT:
            break
        problems.append("timed out after " + str(cli.timeout) + "s")
    else:
        raise CollectionError("the Seren CLI timed out " + str(attempts) + " time(s)")
    if result.status == UNAVAILABLE:
        raise CollectionError("the Seren CLI could not start")
    if result.status == DECODE_ERROR:
        raise CollectionError("the Seren CLI output was not the documented JSON")
    # Each call is its own observation: the harness's call window is part of the evidence, so
    # an unchanged answer at a later time is a new observation (a retest), never a duplicate.
    finished = utc_now()
    envelope = {"status": result.status, "exit_code": result.exit_code,
                "stderr_tail": result.stderr_tail,
                "harness_clock": {"started": started, "finished": finished},
                "response": result.data if result.status == "ok" else None}
    invocation = " ".join(result.argv)
    payload = {"messages": [{"role": "initial", "kind": "api_response",
                             "invocation": invocation, "text": json.dumps(envelope, indent=2)}],
               "complete": True,  # the whole answer of a single call is the reading
               "observed": "harness clock " + started + " to " + finished}
    environment = ("Seren service answer read by the harness through the pinned Seren CLI "
                   + found[0] + " (sha256 " + identity[:12] + "), profile "
                   + collector["profile"] + "; server build not exposed")
    if sql:
        environment += ("; fixed table-free SQL via pinned psql sha256 "
                        + collector["psql_sha256"][:12] + "; not employee-mediated execution")
    return payload, problems, environment


def from_copy(path, case, target):
    text = read_text(path)
    parts = discord_messages(text)
    follow = target.get("reading_paths", {}).get(case["id"], {}).get("follow_up")
    if len(parts) > 1 and not follow:
        raise CollectionError("the target does not declare how later messages are reached")
    messages = [{"role": "initial", "kind": "discord_response",
                 "invocation": case["command"], "text": parts[0]}]
    messages += [{"role": "complete_view", "kind": "button_response",
                  "invocation": follow, "text": p} for p in parts[1:]]
    return {"messages": messages, "complete": None}, []


def _git_revision(root):
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _auto_retest(state_dir, case, provenance):
    """Link to the case's single open finding this evidence is compatible with (same case and
    declared criterion group, not an alias, same side of the simulation boundary); with
    several, link none rather than guess."""
    store = Store(state_dir)
    try:
        open_ = candidate_findings(store, case, MODES[provenance])
    finally:
        store.close()
    return (open_[0] if len(open_) == 1 else None), open_


def run(args, out=sys.stdout, record=None):
    """`record`, when given, receives what happened for a caller such as a campaign:
    service_requests issued, collection_error, folder and report verdict."""
    record = {} if record is None else record

    def say(text=""):
        print(text, file=out)

    harness_root = Path(args.root)
    try:
        target = load_target(args.target, args.target_root)
        case = next((c for p, c in workflow_cases(harness_root)
                     if args.case in (c["id"], p.name, p.stem)), None)
        if case is None:
            raise ValueError("unknown case")
        home = Path(args.home).expanduser()
        state_dir = Path(args.state_dir).expanduser() if args.state_dir else home / "findings"
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        say("Cannot start: check the target configuration and case name.")
        return INVALID
    sanitizer = Sanitizer()
    attempts = 1 if getattr(args, "no_retry", False) else ATTEMPTS
    try:
        if args.from_copy:
            payload, problems = from_copy(args.from_copy, case, target)
            provenance = "operator_supplied"
            environment = "retained Discord copy supplied earlier by the operator"
            revision = None
        elif target["collectors"].get(case["id"], {}).get("type") == "seren_cli":
            # The harness itself observed the service answer through the supported CLI;
            # configuration cannot choose this provenance for any other collector.
            # Never call the service for a case whose target is not established to match its
            # expectation, or with a collector aimed at a different target than the case.
            if case.get("target_status") == "unresolved":
                raise CollectionError("this case's live target and expectation baseline are "
                                      "unresolved, so it is not run against the service")
            args_ = target["collectors"][case["id"]].get("args", [])
            expected = case.get("api", {}).get("equals", {}).values()
            fixed = case.get("execution_args")
            matches = (args_ == fixed if fixed is not None else
                       bool(expected) and all(v in args_ for v in expected))
            if not matches:
                raise CollectionError("the collector's target differs from the case's target")
            payload, problems, environment = run_seren_cli(
                target["collectors"][case["id"]], target, sanitizer, attempts, record)
            provenance, revision = "independently_observed", None
        else:
            collector = target["collectors"].get(case["id"])
            if not collector or collector.get("type") != "command":
                raise CollectionError("no command collector is configured for this case")
            provenance = collector.get("provenance", "local")
            if provenance not in COMMAND_PROVENANCE:
                raise CollectionError("a command collector may only provide local or "
                                      "simulated evidence")
            environment = collector.get("environment", "configured local adapter")
            payload, problems = run_command(collector, target, harness_root, attempts)
            revision = _git_revision(target["root"])
            revision = (target.get("employee", "target") + " checkout " + revision
                        + " (running process revision not verified)") if revision else None
        messages = _messages(payload)
    except (CollectionError, OSError, UnicodeError, ValueError) as error:
        # Generalized regression scenario; private investigation history omitted.
        message = str(error) if isinstance(error, CollectionError) else "evidence unreadable"
        record["collection_error"] = sanitizer.text(message)
        say("Collection failed: " + sanitizer.text(message) + ". No review was recorded.")
        return COLLECTION_FAILED
    items = []
    for m in messages:
        stem = "attachment" if m["kind"] == "attachment" else "response"
        items.append({"id": stem + "-" + str(len(items) + 1), "kind": m["kind"],
                      "role": m["role"], "invocation": m.get("invocation") or None,
                      "text": m["text"]})
    complete = payload.get("complete")
    bundle = {"provenance": provenance, "revision": revision, "environment": environment,
              "observed": payload.get("observed") if isinstance(payload.get("observed"), str)
              else None,
              "notes": "Collected automatically by `collect`"
              + (" (roles from the target's declared reading path)" if args.from_copy else "")
              + ("; retried after: " + "; ".join(problems) if problems else "") + ".",
              "complete_reading_captured": complete is True,
              "items": [{k: v for k, v in i.items() if k != "text"} for i in items]}
    retest, open_ = ((args.retest, None) if args.retest
                     else _auto_retest(state_dir, case, provenance))
    folder = _new_folder(private_directory(private_directory(home) / case["id"]))
    try:
        private_directory(folder / "evidence")
        for item in items:
            item["path"] = "evidence/" + item["id"] + ".txt"
            _write_private(folder / item["path"], sanitizer.text(item["text"]))
        saved = dict(bundle, items=[{k: v for k, v in i.items() if k != "text"} for i in items])
        _write_private(folder / "bundle.json", json.dumps(saved, indent=2) + "\n")
        record["folder"] = str(folder)
        report = review(case, bundle, items, state_dir, folder / "report", None, retest)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        record["review_error"] = True
        say("Evidence was collected but the review could not be recorded; the sanitized "
            "evidence is in " + str(folder) + ".")
        return INVALID
    record["verdict"] = report["verdict"]
    say("Harness run: completed (" + str(len(items)) + " message(s), " + provenance
        + " evidence" + (", after a retry" if problems else "") + ").")
    say(summary_line(report) + ".")
    if report["verdict_reasons"]:
        say("Not passed because: " + "; ".join(report["verdict_reasons"]) + ".")
    if MODES[provenance] != LIVE:
        say("Evidence limit: " + environment + ". It cannot produce a scoped pass.")
    if complete is not True:
        say("Completeness of the reading was not established by the collector.")
    if open_ and len(open_) > 1:
        say("Not linked as a retest: " + str(len(open_)) + " open findings could match ("
            + ", ".join(open_) + "); the harness does not guess. The evidence is saved; link "
            "it later with `review-workflow --retest <key>`.")
    pending = [c["id"] for c in report["criteria"]
               if c["category"] == "human_assessment" and c["verdict"] == "unverified"]
    if pending:
        say("Awaiting a person's judgment (left unverified): " + ", ".join(pending) + ".")
    say("Report: " + str(folder / "report" / "report.md"))
    for key in report["findings"]:
        say("Private draft for " + key + ": " + str(state_dir / "drafts" / (key + ".md")))
    if report["retest"]:
        say("Linked as a retest of " + report["retest"]["finding"] + ": "
            + str(state_dir / "drafts" / (report["retest"]["finding"] + ".md")))
        if not report["retest"]["eligible"]:
            say("This evidence is a diagnostic observation of that finding, not a definitive "
                "retest: the case scope does not accept " + provenance + " evidence, so the "
                "finding status stays " + report["retest"]["finding_status"] + "."
                + (" Diagnostic only: " + "; ".join(report["retest"]["diagnostic"]) + "."
                   if report["retest"]["diagnostic"] else ""))
    return {"passed": OK, "failed": FAILED_VERDICT}.get(report["verdict"], INCOMPLETE)


