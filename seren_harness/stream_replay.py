"""One read-only replay of a completed Clock Test run through `seren agent cloud run stream`.

Customer question: does pinned CLI 0.10.0 end a completed tool-using run's replay before it
shows the final answer? (F-C1-tool-completed, F-C1-stream-closing; docs/stream-replay.)

`stream-replay prepare --record <invocation.json> --output <package.json>` is offline: it
derives the package (identities, expected event structure, answer hash) from a saved
`cloud run get` record. `stream-replay run --package --session --authorization` makes at
most three service invocations, ever, for the package's journal, in this order and only if
each prerequisite holds:

1. `agent cloud deployment status D -o json` (30 s): the deployment is D and stopped.
2. `agent cloud run get --deployment-id D R -o json` (30 s): the same run, deployment and
   execution, status completed, the recorded event structure and the same answer hash.
3. `agent cloud run stream --deployment-id D R` (45 s, `SerenCli.capture`): bounded
   rendered output, killed and reaped at the timeout.

One process owns the journal: an exclusive non-blocking `flock` on `evidence/replay.lock`
(never replaced, released by the kernel when the owner exits) is taken before the journal
is read and held until the last evidence write; a competing launch sends nothing and
writes nothing. Each intent is made durable before its command can reach the service
(private temporary file, flush and fsync, atomic replace, directory fsync); if that fails,
neither that command nor any later one is sent and the journal is not reset. An interrupted
invocation is never resent and nothing after it is sent. Changed inputs are refused before
any call. No retry, no other run, no `--last-event-id`, no employee start/stop/message.

The evidence is the CLI's rendered output (`[<event>] <payload re-serialized as JSON>` on
stderr, `Stream ended.` only when the server closes). SSE `id:` lines, raw frames and HTTP
status are not visible and are never inferred.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from seren_harness import contract, employee_run, employee_tool, tier0
from seren_harness.models import utc_now
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer
from seren_harness.state import private_directory

CLOCK_TEST = "7a000021-0000-4000-8000-000000000022"
TIMEOUTS = (30, 30, 45)
WINDOW = 600
MARGIN = 10  # seconds a step needs beyond its timeout before the window closes
MAX_INVOCATIONS = 3
LIMITS = {"service_invocations": 3, "api_requests": 3, "oauth_refreshes_max": 3,
          "redirects_per_request_max": 10, "http_requests_max": 66,
          "window_seconds": WINDOW, "timeouts_seconds": list(TIMEOUTS),
          "retained_bytes_per_stream": 65536, "retries": 0, "substitute_runs": 0}
EVENT_FIELDS = ("sequence_number", "type", "kind", "event_type", "status", "is_error")
DELIVERED, FAILURE, INCONCLUSIVE, INVALID = 0, 1, 2, 5
VERDICT_CODE = {"answer_delivered": DELIVERED, "reproduced_customer_failure": FAILURE,
                "inconclusive": INCONCLUSIVE}

# Rendered output of agent.rs cloud_run_stream_url (pinned 0.10.0, 9774-9883).
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEADER = re.compile(r"^Streaming run ([0-9a-fA-F-]{36})\.\.\. \(Ctrl\+C to stop\)$")
FRAME = re.compile(r"^\[([^\]]*)\] ?(.*)$")
STREAM_ENDED = "Stream ended."
TERMINAL_EVENTS = ("done", "error", "run.completed", "run.failed", "run.cancelled",
                   "run.canceled")
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "canceled")


def sha256_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return sha256_text(canonical(value))


def structure(events):
    return [{key: event.get(key) for key in EVENT_FIELDS} for event in events]


def answer_of(events):
    """The one text event of a completed run, or None."""
    texts = [e for e in events if e.get("type") == "text" and isinstance(e.get("text"), str)]
    return texts[0] if len(texts) == 1 else None


def build_package(record_path):
    """Offline: derive the package from a saved `cloud run get` record."""
    path = Path(record_path).expanduser().resolve()
    raw = path.read_bytes()
    record = json.loads(raw)
    request, data = record["request"], record["response"]["data"]
    ids = (data["deployment_id"], data["id"], data["execution_id"])
    if (ids != (request["deployment_id"], request["run_id"], request["execution_id"])
            or ids[0] != CLOCK_TEST or not all(employee_tool._uuid(i) for i in ids)
            or data["status"] != "completed"):
        raise ValueError("the record is not one consistent completed Clock Test run")
    events = data["output_events"]
    answer = answer_of(events)
    if answer is None or not any(e.get("type") == "tool_call" and e.get("status") ==
                                 "completed" and e["sequence_number"] < answer["sequence_number"]
                                 for e in events):
        raise ValueError("the record has no completed tool_call before one answer")
    deployment, run, execution = ids
    return {"kind": "stream_replay_package", "version": 1,
            "target": {"employee": "Seren Harness Clock Test", "deployment_id": deployment,
                       "run_id": run, "execution_id": execution},
            "source_record": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()},
            "expected": {"run_status": "completed", "events": structure(events),
                         "answer_sequence": answer["sequence_number"],
                         "answer_length": len(answer["text"]),
                         "answer_sha256": sha256_text(answer["text"])},
            "commands": [list(c) for c in commands(deployment, run)],
            "limits": LIMITS}


def commands(deployment, run):
    return (("agent", "cloud", "deployment", "status", deployment),
            ("agent", "cloud", "run", "get", "--deployment-id", deployment, run),
            ("agent", "cloud", "run", "stream", "--deployment-id", deployment, run))


# ------------------------------------------------------------------ rendered-output verdict

def parse_rendered(stderr):
    """Frames as the pinned CLI prints them. Non-JSON data may span lines (joined '\\n')."""
    header, frames, ended, other = None, [], False, []
    for line in ANSI.sub("", stderr).splitlines():
        match = HEADER.match(line)
        if match and header is None and not frames:
            header = match.group(1).lower()
            continue
        if line == STREAM_ENDED:
            ended = True
            continue
        match = FRAME.match(line) if header is not None else None
        if match:
            try:
                payload = json.loads(match.group(2))
            except ValueError:
                payload = None
            frames.append({"event": match.group(1), "payload": payload, "raw": match.group(2)})
        elif frames and frames[-1]["payload"] is None and not ended:
            frames[-1]["raw"] += "\n" + line
        else:
            other.append(line)
    return {"header_run_id": header, "frames": frames, "stream_ended": ended, "other": other}


def terminal_by_cli(frame):
    """agent.rs 9845-9870: terminal event name, or a JSON object whose `status` is final."""
    payload = frame["payload"]
    by_status = isinstance(payload, dict) and payload.get("status") in TERMINAL_STATUSES
    return frame["event"] in TERMINAL_EVENTS or by_status


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def carries(frame, text):
    if frame["payload"] is not None:
        return any(s == text for s in _strings(frame["payload"]))
    return text in frame["raw"]


def _summary(frame):
    payload = frame["payload"] if isinstance(frame["payload"], dict) else {}
    return {"event": frame["event"], "json": frame["payload"] is not None,
            **{k: payload.get(k) for k in ("type", "kind", "event_type", "status", "terminal",
                                           "sequence_number") if k in payload}}


def _identity_conflicts(frames, deployment, run):
    found = []
    for frame in frames:
        payload = frame["payload"] if isinstance(frame["payload"], dict) else {}
        for key, want in (("run_id", run), ("deployment_id", deployment)):
            value = payload.get(key)
            if isinstance(value, str) and value.lower() != want:
                found.append(key)
    return sorted(set(found))


def classify(captured, answer_text, deployment, run, expected_sequences):
    """Outcome of one replay against the fresh authoritative read (never the raw SSE)."""
    parsed = parse_rendered(captured.stderr)
    frames = parsed["frames"]
    hits = [i for i, frame in enumerate(frames) if carries(frame, answer_text)]
    sequences = [f["payload"]["sequence_number"] for f in frames
                 if isinstance(f["payload"], dict)
                 and isinstance(f["payload"].get("sequence_number"), int)]
    facts = {"exit_code": captured.exit_code, "killed_at_timeout": captured.killed_at_timeout,
             "elapsed_seconds": captured.elapsed_seconds,
             "stderr_truncated": captured.stderr_truncated,
             "stdout_truncated": captured.stdout_truncated,
             "stdout_bytes": len(captured.stdout.encode()),
             "group_remaining": captured.group_remaining,
             "header_run_matches": parsed["header_run_id"] == run if parsed["header_run_id"]
             else None,
             "frames": len(frames), "frame_summaries": [_summary(f) for f in frames[:20]],
             "answer_frame_index": hits[0] if hits else None,
             "stream_ended_line": parsed["stream_ended"],
             "rendered_sequences": sequences,
             "sequences_in_order": sequences == sorted(sequences),
             "sequences_known": set(sequences) <= set(expected_sequences),
             "identity_conflicts": _identity_conflicts(frames, deployment, run),
             "unparsed_lines": len(parsed["other"])}
    last = frames[-1] if frames else None
    if not captured.started:
        outcome = "not_started"
    elif facts["header_run_matches"] is False or facts["identity_conflicts"]:
        outcome = "identity_mismatch"
    elif captured.killed_at_timeout:
        outcome = ("timeout_after_answer" if hits else
                   "truncated" if captured.stderr_truncated else "timeout_before_answer")
    elif captured.exit_code != 0 or parsed["header_run_id"] is None:
        outcome = "stream_error"
    elif hits:
        outcome = "answer_delivered"
    elif captured.stderr_truncated:
        outcome = "truncated"
    elif parsed["stream_ended"]:
        outcome = "closed_without_answer"
    elif last is not None and terminal_by_cli(last):
        outcome = "early_exit_without_answer"
        facts["stopping_frame"] = _summary(last)
    else:
        outcome = "exit_unexplained"
    if hits and not captured.killed_at_timeout:
        facts["termination"] = ("server_closed" if parsed["stream_ended"] else
                                "cli_terminal_frame" if last and terminal_by_cli(last)
                                else "unexplained")
    elif captured.killed_at_timeout:
        facts["termination"] = "local_timeout"
    verdict = ("answer_delivered" if outcome in ("answer_delivered", "timeout_after_answer")
               else "reproduced_customer_failure" if outcome == "early_exit_without_answer"
               else "inconclusive")
    return {"outcome": outcome, "verdict": verdict, "facts": facts}


# ------------------------------------------------------------------ one-shot execution

class Refused(ValueError):
    pass


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_directory(path):
    """Private directory; a newly created one is made durable in its parent."""
    created = not path.is_dir()
    private_directory(path)
    if created:
        fsync_directory(path.parent)


def durable_write(path, text):
    """Same order as invoice_campaign.write: private temporary file, flush and fsync,
    atomic replace, fsync of the directory. Any failure raises before the caller goes on."""
    ensure_directory(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def _read_json(path, what):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise Refused("%s unreadable: %s" % (what, type(error).__name__))


def check_session(session, pinned_sha256):
    keys = {"executable", "executable_sha256", "config_home", "profile"}
    if (not isinstance(session, dict) or set(session) != keys
            or session["profile"] != "default"
            or session["executable_sha256"] != pinned_sha256
            or not os.path.isabs(session["config_home"])):
        raise Refused("the session must bind exactly the pinned executable, its sha256, an "
                      "absolute config home and profile default")


def check_authorization(approval, package_sha256):
    keys = {"approved", "authorization_id", "package_sha256", "issued_at", "expires_at"}
    if (not isinstance(approval, dict) or set(approval) != keys
            or approval["approved"] is not True
            or not employee_tool._uuid(approval["authorization_id"])
            or approval["package_sha256"] != package_sha256):
        raise Refused("exact separate authorization for this package required")
    issued = employee_tool._utc(approval["issued_at"])
    expires = employee_tool._utc(approval["expires_at"])
    if issued is None or expires is None or not 0 < (expires - issued).total_seconds() <= WINDOW:
        raise Refused("authorization window invalid or longer than %d s" % WINDOW)
    return issued, expires


class StreamReplay:
    def __init__(self, package_path, session_path, authorization_path, out,
                 pinned_sha256=tier0.PINNED_SHA256, timeouts=TIMEOUTS, now=None):
        self.out, self.timeouts = out, timeouts
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.package_path = Path(package_path)
        self.package = _read_json(package_path, "package")
        if self.package.get("kind") != "stream_replay_package":
            raise Refused("not a stream replay package")
        try:
            rebuilt = build_package(self.package["source_record"]["path"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise Refused("the source record cannot rebuild the package: %s" % error)
        if canonical(rebuilt) != canonical(self.package):
            raise Refused("the package differs from its source record or this code")
        self.package_sha256 = digest(self.package)
        self.session = _read_json(session_path, "session")
        check_session(self.session, pinned_sha256)
        self.approval = _read_json(authorization_path, "authorization")
        self.issued, self.expires = check_authorization(self.approval, self.package_sha256)
        target = self.package["target"]
        self.deployment, self.run_id = target["deployment_id"], target["run_id"]
        self.execution = target["execution_id"]
        self.directory = self.package_path.parent / "evidence"
        self.journal_path = self.directory / "journal.json"
        self.binding = {"package_sha256": self.package_sha256,
                        "session_sha256": digest(self.session),
                        "authorization_id": self.approval["authorization_id"]}
        self.journal = None
        self.answer_text = None

    def say(self, text):
        print(text, file=self.out)

    def save(self):
        durable_write(self.journal_path, json.dumps(self.journal, indent=2) + "\n")

    def finish(self, verdict, outcome, detail=None):
        self.journal.update(verdict=verdict, outcome=outcome, detail=detail,
                            finished_at=utc_now())
        self.save()
        return VERDICT_CODE[verdict]

    def stop(self, outcome, detail=None):
        return self.finish("inconclusive", outcome, detail)

    def run(self):
        """Owns the journal exclusively from before it is read until the last evidence write.
        A persistence failure stops everything after it; the journal is never reset."""
        lock = self.own()
        try:
            return self.owned_run()
        except OSError as error:
            self.say("Stopped: persisting evidence failed (%s); nothing further was sent and "
                     "the journal is left as it is" % type(error).__name__)
            return INCONCLUSIVE
        finally:
            os.close(lock)

    def own(self):
        try:
            ensure_directory(self.directory)
            lock = os.open(self.directory / "replay.lock", os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as error:
            raise Refused("cannot take ownership of the journal: %s" % type(error).__name__)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock)
            raise Refused("another stream-replay process owns this journal; nothing was "
                          "sent and its evidence is untouched")
        return lock

    def owned_run(self):
        if self.journal_path.exists():
            return self.resume()
        now = self.now()
        if not self.issued <= now < self.expires:
            raise Refused("authorization is not currently valid")
        try:
            cli = employee_run.pinned_cli(self.session, Sanitizer())  # local --version only
        except (OSError, ValueError) as error:
            raise Refused("CLI binding failed: %s" % error)
        self.journal = {"package_sha256": self.package_sha256, "binding": self.binding,
                        "target": self.package["target"], "allowance": MAX_INVOCATIONS,
                        "started_at": utc_now(), "steps": [], "verdict": None}
        self.save()
        code = self.sequence(cli)
        self.say(self.summary())
        return code

    def resume(self):
        journal = _read_json(self.journal_path, "journal")
        if not isinstance(journal, dict) or journal.get("binding") != self.binding:
            raise Refused("inputs differ from the recorded journal; it is left as it is")
        self.journal = journal
        if journal.get("verdict") is None:
            # Interrupted: a journaled step may have reached the service; never resend it or
            # send anything after it.
            self.stop("interrupted", "resumed after interruption; nothing was resent")
        else:
            self.say("This replay was already used; nothing is requested.")
        self.say(self.summary())
        return VERDICT_CODE[self.journal["verdict"]]

    def step(self, number, cli, run):
        timeout = self.timeouts[number - 1]
        remaining = (self.expires - self.now()).total_seconds()
        if remaining < timeout + MARGIN:
            return None
        argv = commands(self.deployment, self.run_id)[number - 1]
        if len(self.journal["steps"]) >= MAX_INVOCATIONS:
            raise RuntimeError("invocation allowance exhausted")
        entry = {"step": number, "command": " ".join(("seren",) + argv),
                 "timeout_seconds": timeout, "intent_at": utc_now()}
        self.journal["steps"].append(entry)
        self.save()  # durable before the command can reach the service
        result = run(cli, argv, timeout)
        entry["finished_at"] = utc_now()
        return entry, result

    def sequence(self, cli):
        def json_read(c, argv, timeout):
            c.timeout = timeout
            return c.json(*argv)

        taken = self.step(1, cli, json_read)
        if taken is None:
            return self.stop("insufficient_time", "before step 1")
        entry, result = taken
        entry.update(status=result.status, exit_code=result.exit_code)
        detail = contract.unwrap(result.data) if result.ok else None
        if not isinstance(detail, dict):
            return self.stop("preflight_failed", result.status)
        observed = {"deployment_matches": detail.get("id") == self.deployment,
                    "status": detail.get("status"),
                    "desired_lifecycle_state": detail.get("desired_lifecycle_state")}
        entry["observed"] = observed
        if not (observed["deployment_matches"] and observed["status"] == "stopped"
                and observed["desired_lifecycle_state"] == "stopped"):
            return self.stop("preflight_unexpected_state", observed)

        taken = self.step(2, cli, json_read)
        if taken is None:
            return self.stop("insufficient_time", "before step 2")
        entry, result = taken
        entry.update(status=result.status, exit_code=result.exit_code)
        detail = contract.unwrap(result.data) if result.ok else None
        if not isinstance(detail, dict):
            return self.stop("run_read_failed", result.status)
        expected = self.package["expected"]
        events = detail.get("output_events") if isinstance(detail.get("output_events"),
                                                           list) else []
        answer = answer_of(events)
        observed = {"run_matches": detail.get("id") == self.run_id,
                    "deployment_matches": detail.get("deployment_id") == self.deployment,
                    "execution_matches": detail.get("execution_id") == self.execution,
                    "status": detail.get("status"),
                    "structure_matches": structure(events) == expected["events"],
                    "answer_matches": answer is not None
                    and answer["sequence_number"] == expected["answer_sequence"]
                    and sha256_text(answer["text"]) == expected["answer_sha256"]}
        entry["observed"] = observed
        if not (observed["run_matches"] and observed["deployment_matches"]
                and observed["execution_matches"] and observed["status"] == "completed"
                and observed["structure_matches"] and observed["answer_matches"]):
            return self.stop("comparison_evidence_missing_or_different", observed)
        self.answer_text = answer["text"]  # memory only; the journal keeps the hash

        def stream(c, argv, timeout):
            return c.capture(argv, timeout, LIMITS["retained_bytes_per_stream"],
                             {"NO_COLOR": "1"})

        taken = self.step(3, cli, stream)
        if taken is None:
            return self.stop("insufficient_time", "before step 3")
        entry, captured = taken
        durable_write(self.directory / "stream-stdout.txt", captured.stdout)
        durable_write(self.directory / "stream-stderr.txt", captured.stderr)
        result = classify(captured, self.answer_text, self.deployment, self.run_id,
                          [e["sequence_number"] for e in expected["events"]])
        entry.update(exit_code=captured.exit_code, killed_at_timeout=captured.killed_at_timeout,
                     elapsed_seconds=captured.elapsed_seconds,
                     group_remaining=captured.group_remaining)
        self.journal["comparison"] = result["facts"]
        return self.finish(result["verdict"], result["outcome"])

    def summary(self):
        j = self.journal
        return ("Stream replay: %s (%s). %d of %d service invocations; journal %s"
                % (j.get("verdict"), j.get("outcome"), len(j.get("steps", [])),
                   MAX_INVOCATIONS, self.journal_path))


def run(args, out=None):
    out = out or sys.stdout
    if args.action == "prepare":
        try:
            package = build_package(args.record)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print("Not prepared: %s" % error, file=out)
            return INVALID
        _write_private(Path(args.output), json.dumps(package, indent=2) + "\n")
        print("Offline package prepared. Authorization must bind SHA256: %s"
              % digest(package), file=out)
        return 0
    try:
        return StreamReplay(args.package, args.session, args.authorization, out).run()
    except Refused as error:
        print("Refused before any call: %s" % error, file=out)
        return INVALID
