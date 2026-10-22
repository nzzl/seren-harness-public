"""Stream replay route: bounded capture (real processes), rendered-output verdicts and the
one-shot three-read sequence against a local stand-in CLI. No Seren contact."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import seren_support  # noqa: F401
from seren_support import prerun, standin

from seren_harness import seren_cli, stream_replay
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import SerenCli

ROOT = Path(__file__).resolve().parents[1]
SHAPES = json.loads((ROOT / "docs/tier0/fixtures/c1-recorded-event-shapes.json")
                    .read_text())["shapes"]["completed-tool"]
D = stream_replay.CLOCK_TEST
R = "7a000026-0000-4000-8000-000000000027"
E = "7a00002a-0000-4000-8000-00000000002b"
ANSWER = "synthetic-answer-placeholder-30"


def events():
    """The recorded structure with placeholder values (sequence = recorded position)."""
    out = []
    for position, shape in enumerate(SHAPES, 1):
        event = {f: "placeholder" for f in shape["fields"]}
        event.update({k: shape[k] for k in ("type", "kind", "event_type") if k in shape})
        for key in ("status", "is_error", "retryable"):
            if key in shape:
                event[key] = shape[key]
        event["sequence_number"] = position
        if event["type"] == "text":
            event["text"] = ANSWER
        out.append(event)
    return out


def render(event_name, payload):
    """agent.rs: `[<event>] <serde_json::to_string(payload)>` on stderr."""
    return "[%s] %s" % (event_name, json.dumps(payload, separators=(",", ":")))


HEADER = "Streaming run %s... (Ctrl+C to stop)" % R
STATE_RUNNING = {"run_id": R, "deployment_id": D, "status": "running", "terminal": False,
                 "phase": "p", "pending_approval_count": 0, "latest_sequence": 5,
                 "started_at": "t", "updated_at": "t"}


def interim(status="in_progress"):
    """Hypothetical live emission whose tool_call is not yet `completed`."""
    return [dict(e, status=status) if e["type"] == "tool_call" else e for e in events()]


def replay(evts, state=None, ended=False, tail=()):
    """What the pinned CLI prints for these frames: it returns at the first terminal frame
    (agent.rs 9845-9870), so nothing after it is printed and `Stream ended.` only follows a
    server close."""
    frames = ([("run.state", state)] if state is not None else []) + \
        [("run.event", e) for e in evts] + list(tail)
    lines = [HEADER]
    for name, payload in frames:
        lines.append(render(name, payload))
        if stream_replay.terminal_by_cli({"event": name, "payload": payload}):
            return "\n".join(lines) + "\n"
    return "\n".join(lines + (["Stream ended."] if ended else [])) + "\n"


def captured(stderr, exit_code=0, killed=False, truncated=False):
    return seren_cli.Captured(["seren"], True, None if killed else exit_code, killed, 0.5,
                              "", stderr, False, truncated, False)


def script(directory, name, body):
    path = Path(directory) / name
    path.write_text(standin("#!%s\n%s" % (sys.executable, body)))
    os.chmod(path, 0o755)
    prerun(path)
    return path


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, body, timeout, limit=seren_cli.CAPTURE_LIMIT):
        path = script(self.dir, "cli", body)
        return SerenCli(str(path), 30, Sanitizer()).capture([], timeout, limit)

    def test_answer_frame_in_the_middle_of_long_stderr_is_kept_whole(self):
        result = self.capture(
            "import sys\nfor i in range(400): sys.stderr.write('[run.event] {\"n\":%d}\\n' % i)\n"
            "sys.stderr.write('[run.event] {\"text\":\"MID-ANSWER\"}\\n')\n"
            "for i in range(400): sys.stderr.write('[run.event] {\"n\":%d}\\n' % i)\n", 10)
        self.assertEqual((0, False, False), (result.exit_code, result.killed_at_timeout,
                                             result.stderr_truncated))
        self.assertIn('[run.event] {"text":"MID-ANSWER"}\n', result.stderr)
        self.assertEqual(801, result.stderr.count("[run.event]"))

    def test_output_before_a_hang_is_kept_and_the_process_is_killed(self):
        result = self.capture("import sys, time, os\nopen(%r, 'w').write(str(os.getpid()))\n"
                              "sys.stderr.write('[run.event] ANSWER\\n'); sys.stderr.flush()\n"
                              "time.sleep(60)\n" % (self.dir + "/pid"), 1)
        self.assertTrue(result.killed_at_timeout)
        self.assertIsNone(result.exit_code)
        self.assertIn("ANSWER", result.stderr)
        self.assertLess(result.elapsed_seconds, 10)
        self.assertFalse(result.group_remaining)
        self.assertFalse(alive(int(Path(self.dir, "pid").read_text())))

    def test_a_hang_without_output_times_out_empty(self):
        result = self.capture("import time\ntime.sleep(60)\n", 1)
        self.assertEqual((True, "", ""), (result.killed_at_timeout, result.stdout, result.stderr))

    def test_excess_output_is_bounded_while_reading_and_never_blocks_the_cli(self):
        body = ("import sys, os\nchunk = b'x' * 65536\n"
                "for _ in range(32): os.write(1, chunk); os.write(2, chunk)\n%s")
        exits = self.capture(body % "", 20, limit=4096)
        self.assertEqual((0, False), (exits.exit_code, exits.killed_at_timeout))
        hangs = self.capture(body % "import time\ntime.sleep(60)\n", 2, limit=4096)
        for result in (exits, hangs):
            self.assertTrue(result.stdout_truncated and result.stderr_truncated)
            self.assertEqual((4096, 4096), (len(result.stdout), len(result.stderr)))
            self.assertFalse(result.group_remaining)
        self.assertTrue(hangs.killed_at_timeout)

    def test_a_child_left_behind_by_the_cli_is_killed(self):
        result = self.capture("import subprocess, os\n"
                              "p = subprocess.Popen(['/bin/sleep', '60'])\n"
                              "open(%r, 'w').write(str(p.pid))\n" % (self.dir + "/child"), 10)
        self.assertEqual((0, False, False), (result.exit_code, result.killed_at_timeout,
                                             result.group_remaining))
        child = int(Path(self.dir, "child").read_text())
        for _ in range(40):
            if not alive(child):
                break
            time.sleep(0.05)
        self.assertFalse(alive(child))
        self.assertLess(result.elapsed_seconds, 9)

    def test_an_unstartable_executable_is_reported(self):
        result = SerenCli(self.dir + "/missing", 30, Sanitizer()).capture([], 1)
        self.assertFalse(result.started)


class VerdictTests(unittest.TestCase):
    def verdict(self, stderr, **kw):
        return stream_replay.classify(captured(stderr, **kw), ANSWER, D, R, [1, 2, 3, 4, 5])

    def test_answer_delivered_when_the_tool_call_frame_is_not_completed(self):
        closed = self.verdict(replay(interim(), STATE_RUNNING, ended=True))
        self.assertEqual(("answer_delivered", "answer_delivered", "server_closed"),
                         (closed["outcome"], closed["verdict"], closed["facts"]["termination"]))
        self.assertEqual([1, 2, 3, 4, 5], closed["facts"]["rendered_sequences"])
        self.assertTrue(closed["facts"]["header_run_matches"])
        ended = self.verdict(replay(interim(), tail=[("run.completed", {"run_id": R})]))
        self.assertEqual("cli_terminal_frame", ended["facts"]["termination"])

    def test_done_ok_is_not_terminal_for_the_cli_so_a_quiet_exit_is_unexplained(self):
        result = self.verdict(replay(interim()))
        self.assertEqual(("answer_delivered", "unexplained"),
                         (result["outcome"], result["facts"]["termination"]))

    def test_exit_at_the_completed_tool_call_is_a_reproduced_customer_failure(self):
        # The pinned CLI returns at the first payload with status completed: tool_call.
        result = self.verdict(replay(events(), STATE_RUNNING))
        self.assertEqual(("early_exit_without_answer", "reproduced_customer_failure"),
                         (result["outcome"], result["verdict"]))
        self.assertEqual({"event": "run.event", "json": True, "type": "tool_call",
                          "kind": "tool_call_started", "event_type":
                          "response.output_item.added", "status": "completed",
                          "sequence_number": 1}, result["facts"]["stopping_frame"])

    def test_exit_at_a_completed_run_state_frame_is_recorded_as_the_stopping_frame(self):
        state = dict(STATE_RUNNING, status="completed", terminal=True)
        result = self.verdict(replay([], state))
        self.assertEqual("early_exit_without_answer", result["outcome"])
        self.assertEqual(("run.state", "completed"),
                         (result["facts"]["stopping_frame"]["event"],
                          result["facts"]["stopping_frame"]["status"]))

    def test_timeout_after_the_answer_is_not_a_lost_answer(self):
        result = self.verdict(replay(events()[2:3]), killed=True)
        self.assertEqual(("timeout_after_answer", "answer_delivered", "local_timeout"),
                         (result["outcome"], result["verdict"],
                          result["facts"]["termination"]))

    def test_inconclusive_outcomes(self):
        cases = {
            "timeout_before_answer": (replay(interim()[:2]), {"killed": True}),
            "truncated": (replay(events()[1:2]), {"truncated": True}),
            "closed_without_answer": (replay(events()[1:2], ended=True), {}),
            "stream_error": ("Error: Failed to stream run: 404 Not Found - {}\n",
                             {"exit_code": 1}),
            "identity_mismatch": (replay(interim(), ended=True).replace(R, E, 1), {}),
            "exit_unexplained": (replay(events()[1:2]), {}),
        }
        for outcome, (stderr, kw) in cases.items():
            result = self.verdict(stderr, **kw)
            self.assertEqual((outcome, "inconclusive"), (result["outcome"], result["verdict"]),
                             outcome)

    def test_a_payload_naming_another_run_is_an_identity_mismatch(self):
        state = dict(STATE_RUNNING, run_id=E)
        self.assertEqual("identity_mismatch",
                         self.verdict(replay(interim(), state, ended=True))["outcome"])

    def test_colour_codes_and_plain_text_frames_are_parsed(self):
        lines = ["\x1b[2m%s\x1b[0m" % HEADER, "\x1b[36m[run.event]\x1b[0m %s" % ANSWER,
                 "\x1b[2mStream ended.\x1b[0m"]
        result = self.verdict("\n".join(lines) + "\n")
        self.assertEqual(("answer_delivered", "server_closed"),
                         (result["outcome"], result["facts"]["termination"]))


STANDIN_CLI = r'''
import json, os, sys, time
home = os.environ["XDG_CONFIG_HOME"]
args = sys.argv[1:]
if args == ["--version"]:
    print("seren 0.10.0"); sys.exit(0)
mode = json.load(open(os.path.join(home, "mode.json")))
with open(os.path.join(home, "calls.log"), "a") as log:
    log.write(" ".join(args) + "\n")
if args[:4] == ["agent", "cloud", "deployment", "status"]:
    print(json.dumps({"data": mode["status"]})); sys.exit(0)
if args[:4] == ["agent", "cloud", "run", "get"]:
    print(json.dumps({"data": mode["run"]})); sys.exit(0)
if args[:4] == ["agent", "cloud", "run", "stream"]:
    if mode.get("gate"):  # barrier: announce, then wait for the test to release us
        open(os.path.join(home, "stream.pid"), "w").write(str(os.getpid()))
        os.rename(os.path.join(home, "stream.pid"), os.path.join(home, "entered"))
        deadline = time.time() + 120
        while not os.path.exists(os.path.join(home, "release")) and time.time() < deadline:
            time.sleep(0.02)
    sys.stderr.write(mode["stream"]); sys.stderr.flush()
    time.sleep(mode.get("hang", 0)); sys.exit(0)
sys.exit(9)
'''


DRIVER = r'''
import io, sys
from seren_harness import stream_replay
package, session, authorization, sha = sys.argv[1:5]
try:
    code = stream_replay.StreamReplay(package, session, authorization, sys.stdout,
                                      pinned_sha256=sha).run()
except stream_replay.Refused as error:
    print("Refused before any call: %s" % error)
    code = stream_replay.INVALID
sys.exit(code)
'''


def wait_for(path, seconds=60):
    """Barrier on a file the other process creates; generous bound, no timing guess."""
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError("barrier %s not reached" % path)
        time.sleep(0.02)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "config"
        self.home.mkdir()
        self.cli = script(base, "seren", STANDIN_CLI)
        self.sha = hashlib.sha256(self.cli.read_bytes()).hexdigest()
        record = {"request": {"deployment_id": D, "run_id": R, "execution_id": E},
                  "response": {"data": {"deployment_id": D, "id": R, "execution_id": E,
                                        "status": "completed", "output_events": events()}}}
        self.record = base / "invocation.json"
        self.record.write_text(json.dumps(record))
        self.package = base / "pkg" / "package.json"
        self.package.parent.mkdir()
        self.package.write_text(json.dumps(stream_replay.build_package(self.record)))
        self.session = base / "session.json"
        self.session.write_text(json.dumps({"executable": str(self.cli),
                                            "executable_sha256": self.sha,
                                            "config_home": str(self.home),
                                            "profile": "default"}))
        self.mode({})
        self.addCleanup(self.tmp.cleanup)  # runs after per-test cleanups (LIFO)

    def mode(self, overrides):
        mode = {"status": {"id": D, "status": "stopped", "desired_lifecycle_state": "stopped"},
                "run": {"id": R, "deployment_id": D, "execution_id": E, "status": "completed",
                        "output_events": events()},
                "stream": replay(interim(), STATE_RUNNING, ended=True)}
        mode.update(overrides)
        (self.home / "mode.json").write_text(json.dumps(mode))

    def authorize(self, seconds=600, offset=0, package_sha=None):
        from datetime import datetime, timedelta, timezone
        issued = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=offset)
        self.authorizations = getattr(self, "authorizations", 0) + 1
        path = Path(self.tmp.name) / ("authorization-%d.json" % self.authorizations)
        path.write_text(json.dumps({
            "approved": True, "authorization_id": "7a000002-0000-4000-8000-000000000003",
            "package_sha256": package_sha or stream_replay.digest(
                json.loads(self.package.read_text())),
            "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(seconds=seconds)).isoformat()}))
        return path

    def run_route(self, authorization=None, **kw):
        out = io.StringIO()
        try:
            code = stream_replay.StreamReplay(self.package, self.session,
                                              authorization or self.authorize(), out,
                                              pinned_sha256=self.sha, **kw).run()
        except stream_replay.Refused as error:
            return stream_replay.INVALID, "Refused: %s" % error
        return code, out.getvalue()

    def calls(self):
        log = self.home / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def journal(self):
        return json.loads((self.package.parent / "evidence/journal.json").read_text())

    def test_the_full_route_makes_three_reads_within_bounds_and_keeps_evidence_private(self):
        started = time.monotonic()
        code, out = self.run_route()
        self.assertEqual(stream_replay.DELIVERED, code, out)
        self.assertEqual(["agent cloud deployment status " + D,
                          "agent cloud run get --deployment-id %s %s -o json" % (D, R),
                          "agent cloud run stream --deployment-id %s %s" % (D, R)],
                         [c.replace(" -o json", "", 1) if c.startswith("agent cloud deploy")
                          else c for c in self.calls()])
        journal = self.journal()
        self.assertEqual([30, 30, 45], [s["timeout_seconds"] for s in journal["steps"]])
        self.assertTrue(all(s["intent_at"] <= s["finished_at"] for s in journal["steps"]))
        self.assertEqual("answer_delivered", journal["outcome"])
        self.assertNotIn(ANSWER, json.dumps(journal))
        evidence = self.package.parent / "evidence"
        for name in ("journal.json", "stream-stderr.txt", "stream-stdout.txt"):
            self.assertEqual(0o600, (evidence / name).stat().st_mode & 0o777)
        self.assertLess(time.monotonic() - started, 30)

    def launch(self, authorization):
        driver = Path(self.tmp.name) / "driver.py"
        driver.write_text(DRIVER)
        return subprocess.Popen(
            [sys.executable, str(driver), str(self.package), str(self.session),
             str(authorization), self.sha], cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=dict(os.environ, PYTHONPATH=str(ROOT)))

    def evidence(self):
        directory = self.package.parent / "evidence"
        return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}

    def release_orphan(self):
        (self.home / "release").write_text("")
        entered = self.home / "entered"
        if entered.exists():
            try:
                os.kill(int(entered.read_text()), 9)
            except ProcessLookupError:
                pass

    def test_an_overlapping_launch_sends_nothing_and_leaves_the_owners_evidence(self):
        self.mode({"gate": True})
        self.addCleanup(self.release_orphan)
        authorization = self.authorize()
        owner = self.launch(authorization)
        wait_for(self.home / "entered")  # the owner is inside step 3, holding ownership
        before = self.evidence()
        competitor = self.launch(authorization)
        output, _ = competitor.communicate(timeout=60)
        self.assertEqual(stream_replay.INVALID, competitor.returncode, output)
        self.assertIn("another stream-replay process owns this journal", output)
        self.assertEqual(3, len(self.calls()))
        self.assertEqual(before, self.evidence())
        (self.home / "release").write_text("")
        output, _ = owner.communicate(timeout=120)
        self.assertEqual(stream_replay.DELIVERED, owner.returncode, output)
        self.assertEqual(("answer_delivered", 3), (self.journal()["outcome"],
                                                   len(self.calls())))

    def test_killing_the_owner_releases_ownership_and_its_intent_is_never_resent(self):
        self.mode({"gate": True})
        self.addCleanup(self.release_orphan)
        authorization = self.authorize()
        owner = self.launch(authorization)
        wait_for(self.home / "entered")
        owner.kill()
        owner.communicate(timeout=60)
        # Limitation kept visible: the CLI runs in its own process group, so killing the
        # harness with SIGKILL leaves the CLI running until it ends or is killed.
        self.assertTrue(alive(int((self.home / "entered").read_text())))
        steps = self.journal()["steps"]
        self.assertEqual((3, None), (len(steps), steps[2].get("finished_at")))
        code, out = self.run_route(authorization)
        self.assertEqual((stream_replay.INCONCLUSIVE, "interrupted"),
                         (code, self.journal()["outcome"]), out)
        self.assertEqual(3, len(self.calls()))

    def test_each_intent_is_synchronized_and_replaced_before_its_invocation(self):
        log, real_fsync, real_replace = [], os.fsync, os.replace
        real_json, real_capture = SerenCli.json, SerenCli.capture

        def fsync(fd):
            log.append("fsync-dir" if os.path.isdir("/dev/fd/%d" % fd)
                       or __import__("stat").S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
            return real_fsync(fd)

        def replace(source, target):
            log.append("replace:" + Path(target).name)
            return real_replace(source, target)

        def invoked(real):
            def call(cli, *args, **kw):
                log.append("invoke")
                return real(cli, *args, **kw)
            return call
        with mock.patch("os.fsync", fsync), mock.patch("os.replace", replace), \
                mock.patch.object(SerenCli, "json", invoked(real_json)), \
                mock.patch.object(SerenCli, "capture", invoked(real_capture)):
            code, out = self.run_route()
        self.assertEqual(stream_replay.DELIVERED, code, out)
        starts = [i for i, item in enumerate(log) if item == "invoke"]
        self.assertEqual(3, len(starts))
        previous = 0
        for start in starts:
            window = log[previous:start]
            tail = window[-3:]
            self.assertEqual(["fsync-file", "replace:journal.json", "fsync-dir"], tail, log)
            previous = start + 1

    def test_a_persistence_failure_stops_that_request_and_every_later_one(self):
        for skip in (0, 1):  # 0: temporary-file fsync fails; 1: directory fsync after replace
            with self.subTest(skip=skip):
                (self.home / "calls.log").unlink(missing_ok=True)
                for path in (self.package.parent / "evidence").glob("*"):
                    path.unlink()
                real_fsync, real_json = os.fsync, SerenCli.json
                armed = {"after_first_call": False, "skip": skip}

                def fsync(fd):
                    if armed["after_first_call"]:
                        if armed["skip"] == 0:
                            raise OSError(5, "injected fsync failure")
                        armed["skip"] -= 1
                    return real_fsync(fd)

                def first_then_arm(cli, *args):
                    result = real_json(cli, *args)
                    armed["after_first_call"] = True
                    return result
                with mock.patch("os.fsync", fsync), \
                        mock.patch.object(SerenCli, "json", first_then_arm):
                    code, out = self.run_route()
                self.assertEqual(stream_replay.INCONCLUSIVE, code)
                self.assertIn("persisting evidence failed", out)
                self.assertEqual(1, len(self.calls()))
                code, out = self.run_route()
                self.assertEqual((stream_replay.INCONCLUSIVE, "interrupted", 1),
                                 (code, self.journal()["outcome"], len(self.calls())), out)

    def test_an_early_exit_at_the_tool_call_is_reported_as_the_customer_failure(self):
        self.mode({"stream": replay(events(), STATE_RUNNING)})
        code, _ = self.run_route()
        self.assertEqual(stream_replay.FAILURE, code)
        self.assertEqual("tool_call", self.journal()["comparison"]["stopping_frame"]["type"])

    def test_a_hanging_stream_is_killed_at_its_timeout_with_the_answer_kept(self):
        self.mode({"stream": replay(interim()), "hang": 60})
        code, _ = self.run_route(timeouts=(30, 30, 1))
        journal = self.journal()
        self.assertEqual((stream_replay.DELIVERED, "timeout_after_answer"),
                         (code, journal["outcome"]))
        self.assertTrue(journal["steps"][2]["killed_at_timeout"])
        self.assertFalse(journal["steps"][2]["group_remaining"])

    def test_failed_prerequisites_prevent_the_replay(self):
        running = {"id": D, "status": "running", "desired_lifecycle_state": "running"}
        other = {"id": R, "deployment_id": D, "execution_id": R, "status": "completed",
                 "output_events": events()}
        changed = [dict(e, text="different") if e["type"] == "text" else e for e in events()]
        cases = [({"status": running}, 1, "preflight_unexpected_state"),
                 ({"status": dict(running, id=E, status="stopped",
                                  desired_lifecycle_state="stopped")}, 1,
                  "preflight_unexpected_state"),
                 ({"run": other}, 2, "comparison_evidence_missing_or_different"),
                 ({"run": dict(other, execution_id=E, output_events=changed)}, 2,
                  "comparison_evidence_missing_or_different"),
                 ({"run": dict(other, execution_id=E, status="running")}, 2,
                  "comparison_evidence_missing_or_different")]
        for overrides, sent, outcome in cases:
            (self.home / "calls.log").unlink(missing_ok=True)
            evidence = self.package.parent / "evidence"
            for path in evidence.glob("*"):
                path.unlink()
            self.mode(overrides)
            code, _ = self.run_route()
            self.assertEqual((stream_replay.INCONCLUSIVE, outcome, sent),
                             (code, self.journal()["outcome"], len(self.calls())), overrides)
            self.assertFalse(any("stream" in c for c in self.calls()))

    def test_changed_inputs_are_refused_before_any_call(self):
        self.run_route()
        sent = len(self.calls())
        package = json.loads(self.package.read_text())
        package["target"]["run_id"] = E
        self.package.write_text(json.dumps(package))
        code, out = self.run_route(self.authorize(package_sha=stream_replay.digest(package)))
        self.assertEqual(stream_replay.INVALID, code)
        self.assertIn("differs from its source record", out)
        self.package.write_text(json.dumps(stream_replay.build_package(self.record)))
        session = json.loads(self.session.read_text())
        session["config_home"] = str(Path(self.tmp.name))
        self.session.write_text(json.dumps(session))
        code, out = self.run_route()
        self.assertEqual(stream_replay.INVALID, code)
        self.assertIn("differ from the recorded journal", out)
        self.assertEqual(sent, len(self.calls()))

    def test_authorization_must_be_exact_and_current(self):
        for authorization, text in ((self.authorize(package_sha="0" * 64), "exact separate"),
                                    (self.authorize(seconds=601), "longer than"),
                                    (self.authorize(offset=-700), "not currently valid")):
            code, out = self.run_route(authorization)
            self.assertEqual(stream_replay.INVALID, code)
            self.assertIn(text, out)
        self.assertEqual([], self.calls())

    def test_an_interrupted_step_is_never_resent(self):
        real = SerenCli.json
        seen = []

        def interrupt(cli, *args):
            seen.append(args)
            if len(seen) == 2:  # intent for step 2 is already journaled
                raise KeyboardInterrupt
            return real(cli, *args)
        with mock.patch.object(SerenCli, "json", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_route()
        self.assertEqual(2, len(self.journal()["steps"]))
        sent = len(self.calls())
        code, out = self.run_route()
        self.assertEqual((stream_replay.INCONCLUSIVE, "interrupted"),
                         (code, self.journal()["outcome"]))
        code, out = self.run_route()
        self.assertIn("already used; nothing is requested", out)
        self.assertEqual(sent, len(self.calls()))

    def test_no_step_starts_without_time_for_its_timeout(self):
        from datetime import datetime, timedelta
        authorization = self.authorize()
        late = json.loads(authorization.read_text())["expires_at"]
        now = lambda: datetime.fromisoformat(late) - timedelta(seconds=35)  # noqa: E731
        code, _ = self.run_route(authorization, now=now)
        self.assertEqual((stream_replay.INCONCLUSIVE, "insufficient_time", []),
                         (code, self.journal()["outcome"], self.calls()))

    def test_prepare_refuses_a_record_without_a_completed_tool_call_before_the_answer(self):
        record = json.loads(self.record.read_text())
        record["response"]["data"]["output_events"][0]["status"] = "running"
        self.record.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            stream_replay.build_package(self.record)

    def test_the_command_line_runs_prepare_offline(self):
        output = Path(self.tmp.name) / "prepared.json"
        result = subprocess.run([sys.executable, "-m", "seren_harness", "stream-replay",
                                 "prepare", "--record", str(self.record), "--output",
                                 str(output)], cwd=str(ROOT), capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(stream_replay.digest(json.loads(output.read_text())), result.stdout)
        self.assertEqual(0o600, output.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
