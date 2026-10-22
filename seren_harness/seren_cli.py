"""Invoke the explicitly configured Seren CLI without a shell and parse its JSON.

Outcomes are kept distinct because they mean different things to a journey:
- `ok`: the command exited 0 and printed a JSON document.
- `error`: the CLI ran and reported failure (non-zero exit).
- `timeout`: no answer in time; for a mutation the outcome is unknown.
- `decode_error`: output was not the documented JSON; a compatibility finding.
- `unavailable`: the executable could not be started.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import signal
import subprocess
import threading
import time
from typing import Optional

OK = "ok"
ERROR = "error"
TIMEOUT = "timeout"
DECODE_ERROR = "decode_error"
UNAVAILABLE = "unavailable"
CAPTURE_LIMIT = 65536  # retained bytes per stream for `capture`


@dataclass
class Captured:
    """Bounded rendered output of one long-lived command (see `SerenCli.capture`)."""
    argv: list
    started: bool
    exit_code: Optional[int] = None
    killed_at_timeout: bool = False
    elapsed_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    group_remaining: bool = False  # a process of the CLI's group survived cleanup


@dataclass
class CliResult:
    status: str
    argv: list
    data: object = None
    exit_code: Optional[int] = None
    stderr_tail: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.status == OK


class SerenCli:
    def __init__(self, executable, timeout, sanitizer, env=None):
        self.executable = executable
        self.timeout = timeout
        self.sanitizer = sanitizer
        self.env = env
        self.history = []

    def display_argv(self, args):
        return ["seren", *[self.sanitizer.text(str(a)) for a in args]]

    def version(self):
        """`seren --version` prints plain text, not JSON."""
        result = self._execute(["--version"])
        if result.ok and not result.data:
            result.status = DECODE_ERROR
        return result

    def json(self, *args):
        return self._execute([*args, "-o", "json"], parse=True)

    def _execute(self, args, parse=False):
        argv = [self.executable, *[str(a) for a in args]]
        shown = self.display_argv(args)
        self.history.append(shown)
        if not os.access(self.executable, os.X_OK):
            return CliResult(UNAVAILABLE, shown, detail={"reason": "not executable"})
        try:
            process = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, timeout=self.timeout, env=self.env)
        except subprocess.TimeoutExpired:
            return CliResult(TIMEOUT, shown, detail={"timeout_seconds": self.timeout})
        except OSError as error:
            return CliResult(UNAVAILABLE, shown, detail={"reason": type(error).__name__})
        stderr_tail = self.sanitizer.text(_excerpt(process.stderr))
        if process.returncode != 0:
            return CliResult(ERROR, shown, _parse(process.stdout), process.returncode,
                             stderr_tail)
        if not parse:
            return CliResult(OK, shown, self.sanitizer.text(process.stdout[:400]).strip(), 0,
                             stderr_tail)
        data = _parse(process.stdout)
        if data is None:
            return CliResult(DECODE_ERROR, shown, None, 0, stderr_tail,
                             {"stdout_excerpt": self.sanitizer.text(process.stdout[:200])})
        return CliResult(OK, shown, data, 0, stderr_tail)


    def capture(self, args, timeout, limit=CAPTURE_LIMIT, extra_env=None):
        """Opt-in for a command that may not end by itself (`run stream`). Unlike `_execute`,
        output read before a timeout is kept. At most `limit` bytes per stream are retained
        while reading; the rest is drained so the CLI never blocks on a full pipe. On
        timeout the CLI's process group is killed and reaped; afterwards any process left in
        that group is killed and reported. Output is raw rendered text, not sanitized."""
        argv = [self.executable, *[str(a) for a in args]]
        shown = self.display_argv(args)
        self.history.append(shown)
        env = dict(self.env or os.environ, **(extra_env or {}))
        started = time.monotonic()
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, start_new_session=True)
        except OSError:
            return Captured(shown, False)
        sinks = [_BoundedSink(child.stdout, limit), _BoundedSink(child.stderr, limit)]
        killed = False
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            killed = True
        finally:
            _kill_group(child.pid)
            child.wait()
        for sink in sinks:
            sink.thread.join(5)
        remaining = _group_alive(child.pid) or any(s.thread.is_alive() for s in sinks)
        out, err = sinks
        return Captured(shown, True, None if killed else child.returncode, killed,
                        round(time.monotonic() - started, 3), out.text(), err.text(),
                        out.truncated, err.truncated, remaining)


class _BoundedSink:
    """Reads one pipe to EOF in a thread, keeping at most `limit` bytes."""

    def __init__(self, pipe, limit):
        self.pipe, self.limit = pipe, limit
        self.data, self.truncated = bytearray(), False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        with self.pipe:
            while True:
                chunk = os.read(self.pipe.fileno(), 65536)
                if not chunk:
                    return
                room = self.limit - len(self.data)
                if len(chunk) > room:
                    self.truncated = True
                self.data += chunk[:max(room, 0)]

    def text(self):
        return self.data.decode("utf-8", "replace")


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid, wait=2.0):
    deadline = time.monotonic() + wait
    while True:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        if time.monotonic() >= deadline:
            return True
        _kill_group(pgid)
        time.sleep(0.05)


def _excerpt(text):
    """Keep the start (the CLI's context and HTTP status) and the end of long stderr."""
    return text if len(text) <= 800 else text[:300] + " ... " + text[-480:]


def _parse(text):
    try:
        return json.loads(text)
    except ValueError:
        return None
