"""Tier 0 lab: the real pinned Seren CLI against a disposable loopback fake API.

No live Seren call is possible from here by construction, not by convention:
- every CLI run is wrapped in a macOS sandbox profile that denies all outbound network
  except loopback and denies reading or writing the operator's home directory;
- the CLI gets an explicit minimal environment (no inherited proxy, auth or profile
  variables), a disposable HOME/XDG_CONFIG_HOME and `--api-base`, `SEREN_API_BASE`
  and `SEREN_OAUTH_HOST` all pointing at the fake API (pinned source: defaults.rs
  `DEFAULT_API_HOST`/`DEFAULT_OAUTH_HOST`, main.rs `--api-base`, auth.rs `oauth_host`);
- `preflight` proves the sandbox denies a non-loopback connect and a home-directory read
  before any scenario runs, and that the executable is the pinned build.

Pinned build: Seren CLI 0.10.0, commit 2317949, sha256 PINNED_SHA256. The binary is copied
into the disposable lab and re-hashed; it is never modified.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
from urllib.parse import urlsplit, parse_qs

PINNED_SHA256 = "ddf617c405ce0a90c676a0342295782dcab5f392ef3695aa69ed86caa776b2fa"
PINNED_VERSION = "seren 0.10.0"
SOURCE = "serenorg/seren v0.10.0 (2317949): cli/src, sdk/src, sdk/openapi"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
NOT_ROUTABLE = "192.0.2.1"  # TEST-NET-1; the deny probe never leaves the host
OUTPUT_LIMIT = 65536
PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(deny network-bind)
(deny file-read* (subpath (param "REAL_HOME")))
(deny file-write* (subpath (param "REAL_HOME")))
"""
ENV_KEYS = ("PATH", "HOME", "XDG_CONFIG_HOME", "SEREN_API_BASE", "SEREN_OAUTH_HOST",
            "NO_COLOR", "LANG", "TMPDIR", "SEREN_API_KEY", "SEREN_PROFILE")
SAVED_TOKEN = "tier0-saved-oauth-token"
DUMMY_KEY = "seren_tier0_dummy_key"


class IsolationError(RuntimeError):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------- fake API

class _Handler(BaseHTTPRequestHandler):
    server_version = "tier0-fake"
    sys_version = ""

    def log_message(self, *args):  # silence stderr access logs
        pass

    def _record(self, body):
        api = self.server.api
        split = urlsplit(self.path)
        try:
            if self.headers.get("Content-Type", "").startswith(
                    "application/x-www-form-urlencoded"):
                parsed = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            else:
                parsed = json.loads(body) if body else None
        except ValueError:
            parsed = {"_unparsed_text": body.decode("utf-8", "replace")[:4096]}
        entry = {"at": round(time.monotonic() - api.started, 3), "method": self.command,
                 "path": split.path, "query": parse_qs(split.query),
                 "headers": {k.lower(): v for k, v in self.headers.items()
                             if k.lower() in api.KEPT_HEADERS},
                 "body_present": bool(body), "body": parsed}
        with api.lock:
            api.requests.append(entry)
        return entry

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        entry = self._record(body)
        reply = self.server.api.next_reply(self.command, entry["path"])
        if reply is None:
            entry["unscripted"] = True
            reply = {"status": 501, "json": {"fake_api": "unscripted request"}}
        if "sse" in reply:
            self._stream(reply, entry)
            return
        payload = (json.dumps(reply["json"]).encode() if "json" in reply
                   else reply.get("body", "").encode())
        self.send_response(reply["status"])
        for key, value in reply.get("headers", {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", reply.get("content_type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _stream(self, reply, entry):
        timeline = entry.setdefault("sse", [])
        start = self.server.api.started
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for frame in reply["sse"]:
                if reply.get("frame_delay"):
                    time.sleep(reply["frame_delay"])
                self.wfile.write(sse_bytes(frame))
                self.wfile.flush()
                timeline.append({"at": round(time.monotonic() - start, 3),
                                 "sent": frame.get("event") or "message"})
            deadline = time.monotonic() + reply.get("hold", 0)
            while time.monotonic() < deadline:
                time.sleep(0.1)
                self.wfile.write(b": keepalive\n\n")  # comment; detects a departed client
                self.wfile.flush()
            timeline.append({"at": round(time.monotonic() - start, 3), "server_closed": True})
        except (BrokenPipeError, ConnectionResetError):
            timeline.append({"at": round(time.monotonic() - start, 3), "client_gone": True})

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _respond


def sse_bytes(frame):
    lines = []
    if frame.get("event"):
        lines.append("event: " + frame["event"])
    if frame.get("id") is not None:
        lines.append("id: " + str(frame["id"]))
    data = frame.get("data")
    text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    lines.extend("data: " + part for part in text.split("\n"))
    return ("\n".join(lines) + "\n\n").encode()


class FakeApi:
    """Scripted loopback API. Routes map (METHOD, path) to a list of replies, consumed in
    order (the last one repeats). Anything unscripted gets 501 and is flagged."""
    KEPT_HEADERS = ("authorization", "accept", "last-event-id", "idempotency-key",
                    "user-agent", "content-type", "api-version")

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.requests = []
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.api = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def next_reply(self, method, path):
        with self.lock:
            queue = self.routes.get((method, path))
            if queue is None:  # "prefix/*" routes match generated ids (e.g. request ids)
                queue = next((v for (m, p), v in self.routes.items()
                              if m == method and p.endswith("/*") and
                              path.startswith(p[:-1])), None)
            if not queue:
                return None
            return queue.pop(0) if len(queue) > 1 else queue[0]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


# ---------------------------------------------------------------- lab

class Lab:
    """One disposable lab per suite run: a verified copy of the pinned binary, the sandbox
    profile and a scratch directory outside the operator's home."""

    def __init__(self, executable, workdir, real_home=None, sandbox=SANDBOX_EXEC):
        self.real_home = str(Path(real_home or Path.home()).resolve())
        self.workdir = Path(workdir).resolve()
        if str(self.workdir).startswith(self.real_home + os.sep):
            raise IsolationError("the lab directory must be outside the operator's home")
        self.sandbox = sandbox
        source = Path(executable)
        self.source_sha256 = sha256_file(source)
        if self.source_sha256 != PINNED_SHA256:
            raise IsolationError("executable is not the pinned Seren CLI 0.10.0 build")
        (self.workdir / "bin").mkdir(parents=True, exist_ok=True)
        self.executable = self.workdir / "bin" / "seren"
        shutil.copyfile(source, self.executable)
        os.chmod(self.executable, 0o755)
        if sha256_file(self.executable) != PINNED_SHA256:
            raise IsolationError("copied executable checksum changed")
        self.profile = self.workdir / "tier0.sb"
        self.profile.write_text(PROFILE)
        self.counter = 0
        self.active_pid = None

    # One scenario home with optional synthetic saved credentials (pinned config.rs fields).
    def home(self, saved=None):
        self.counter += 1
        home = self.workdir / ("home-%03d" % self.counter)
        profile = home / ".config" / "seren" / "profiles" / "default"
        profile.mkdir(parents=True)
        if saved is not None:
            lines = ["%s = %s" % (key, json.dumps(value) if isinstance(value, str) else value)
                     for key, value in saved.items()]
            (profile / "credentials.toml").write_text("\n".join(lines) + "\n")
        return home

    def env(self, base, home, extra=None):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home),
               "XDG_CONFIG_HOME": str(home / ".config"), "SEREN_API_BASE": base,
               "SEREN_OAUTH_HOST": base, "NO_COLOR": "1", "LANG": "C",
               "TMPDIR": str(self.workdir)}
        env.update(extra or {})
        if set(env) - set(ENV_KEYS):
            raise IsolationError("unexpected environment variable for the CLI")
        return env

    def sandboxed(self, argv):
        return [self.sandbox, "-f", str(self.profile), "-D", "REAL_HOME=" + self.real_home,
                *argv]

    def run_spec(self, spec):
        return run_spec(self, spec)

    def run(self, args, env, deadline, api_base=None):
        argv = [str(self.executable)] + (["--api-base", api_base] if api_base else []) + args
        return self.execute(self.sandboxed(argv), env, deadline)

    def execute(self, argv, env, deadline):
        """Bounded child; a hang is killed and reported as such. The child stays in the
        caller's process group, so a supervisor that kills a worker's group also kills it
        (sandbox-exec execs the CLI in place: one process)."""
        started = time.monotonic()
        child = subprocess.Popen(argv, env=env, cwd=str(self.workdir), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.active_pid = child.pid
        killed = False
        try:
            out, err = child.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            killed = True
            child.kill()
            out, err = child.communicate()
        finally:
            if child.poll() is None:  # interrupted: nothing may outlive the scenario
                child.kill()
                child.wait()
            self.active_pid = None
        return {"exit": child.returncode, "killed_by_supervisor": killed,
                "elapsed": round(time.monotonic() - started, 3),
                "stdout": out.decode("utf-8", "replace")[:OUTPUT_LIMIT],
                "stderr": err.decode("utf-8", "replace")[:OUTPUT_LIMIT]}


# ---------------------------------------------------------------- trusted scenario execution

FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_DEADLINE = 120


def run_spec(lab, spec):
    """Execute one scenario described purely as data (no caller code runs here).

    spec: {"args": [str], "routes": [[METHOD, path, [reply, ...]]], "auth": "key" | "none"
    | {saved credential fields}, "extra_env": {str: str}, "deadline": seconds,
    "files": {name: text}} where an arg "{file:name}" becomes that file's lab path."""
    deadline = float(spec.get("deadline", 10))
    if not 0 < deadline <= MAX_DEADLINE:
        raise ValueError("deadline out of range")
    args = [str(a) for a in spec["args"]]
    for name, content in (spec.get("files") or {}).items():
        if not FILE_NAME.fullmatch(name):
            raise ValueError("unsafe file name")
        path = lab.workdir / "files" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(str(content))
        args = [str(path) if a == "{file:%s}" % name else a for a in args]
    extra = {str(k): str(v) for k, v in (spec.get("extra_env") or {}).items()}
    auth, saved = spec.get("auth", "key"), None
    if auth == "key":
        extra.setdefault("SEREN_API_KEY", DUMMY_KEY)
    elif isinstance(auth, dict):
        saved = auth
    routes = {(m, p): list(replies) for m, p, replies in spec["routes"]}
    with FakeApi(routes) as api:
        home = lab.home(saved)
        result = lab.run(args, lab.env(api.base, home, extra), deadline, api.base)
    result["requests"] = api.requests
    stored = home / ".config" / "seren" / "profiles" / "default" / "credentials.toml"
    if saved is not None and stored.exists():  # synthetic values only
        result["credentials_after"] = {"text": stored.read_text(),
                                       "mode": oct(stored.stat().st_mode & 0o777),
                                       "read_at": int(time.time())}
    return result


class LabServer:
    """Serves run_spec to a sandboxed worker over loopback, one scenario at a time.

    macOS refuses to apply a sandbox with file rules from inside another such sandbox, so
    the lab cannot run inside the worker sandbox. Worker-authored code (scenario builders,
    facts, judges) stays sandboxed and sends only data here; this trusted process applies
    the lab sandbox to the pinned CLI exactly as in direct mode."""

    def __init__(self, lab, isolation):
        self.lab, self.isolation = lab, isolation
        self.token = hashlib.sha256(os.urandom(32)).hexdigest()
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, value):
                body = json.dumps(value, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.headers.get("X-Lab-Token") != server.token:
                    return self._reply(403, {"error": "token"})
                self._reply(200, {"isolation": server.isolation})

            def do_POST(self):
                if self.headers.get("X-Lab-Token") != server.token:
                    return self._reply(403, {"error": "token"})
                try:
                    spec = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    with server.lock:
                        self._reply(200, run_spec(server.lab, spec))
                except (ValueError, KeyError, TypeError) as error:
                    self._reply(400, {"error": str(error)[:300]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def env(self):
        return {"SEREN_TIER0_LAB": "http://127.0.0.1:%d" % self.httpd.server_address[1],
                "SEREN_TIER0_LAB_TOKEN": self.token}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class LabClient:
    """The sandboxed side: same `run_spec` interface as a Lab."""

    def __init__(self, url, token):
        self.url, self.token = url, token

    def _call(self, method, body=None, timeout=30):
        request = urllib.request.Request(self.url, method=method, data=body,
                                         headers={"X-Lab-Token": self.token})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def isolation(self):
        return self._call("GET")["isolation"]

    def run_spec(self, spec):
        return self._call("POST", json.dumps(spec).encode(),
                          timeout=float(spec.get("deadline", 10)) + 60)


def lab_from_environment():
    url, token = os.environ.get("SEREN_TIER0_LAB"), os.environ.get("SEREN_TIER0_LAB_TOKEN")
    return LabClient(url, token) if url and token else None


def preflight(lab, canary_dir):
    """Prove isolation on this host before any scenario. Raises IsolationError."""
    checks = {"pinned_sha256": lab.source_sha256, "copy_sha256": sha256_file(lab.executable)}
    home = lab.home()
    env = lab.env("http://127.0.0.1:9", home)
    version = lab.run(["--version"], env, 10)
    checks["version"] = version["stdout"].strip()
    if version["exit"] != 0 or checks["version"] != PINNED_VERSION:
        raise IsolationError("pinned CLI version check failed")
    probe = lab.execute(lab.sandboxed(["/usr/bin/nc", "-v", "-z", "-G", "2", NOT_ROUTABLE,
                                       "443"]),
                        env, 10)
    checks["non_loopback_connect"] = probe["stderr"].strip()[-200:]
    if probe["exit"] == 0 or "Operation not permitted" not in probe["stderr"]:
        raise IsolationError("sandbox did not deny a non-loopback connection")
    canary = Path(canary_dir).resolve() / "tier0-read-canary.txt"
    if not str(canary).startswith(lab.real_home + os.sep):
        raise IsolationError("the read canary must be inside the operator's home")
    canary.parent.mkdir(parents=True, exist_ok=True)
    canary.write_text("tier0 canary\n")
    read = lab.execute(lab.sandboxed(["/bin/cat", str(canary)]), env, 10)
    checks["home_read"] = read["stderr"].strip()[-200:]
    if read["exit"] == 0 or "Operation not permitted" not in read["stderr"]:
        raise IsolationError("sandbox did not deny reading the operator's home")
    with FakeApi({}) as api:
        reach = lab.execute(lab.sandboxed(["/usr/bin/nc", "-v", "-z", "-G", "2", "127.0.0.1",
                                           api.base.rsplit(":", 1)[1]]), env, 10)
    checks["loopback_connect_exit"] = reach["exit"]
    if reach["exit"] != 0:
        raise IsolationError("sandbox blocked loopback; scenarios cannot reach the fake API")
    checks["environment_allowlist"] = list(ENV_KEYS)
    checks["endpoint_basis"] = [
        "main.rs: --api-base (env SEREN_API_BASE) selects the API host; set explicitly",
        "defaults.rs: release default https://api.serendb.com is overridden; never used",
        "auth.rs oauth_host(): SEREN_OAUTH_HOST selects the refresh host; set to the fake",
        "auth.rs maybe_refresh_oauth_token: refresh only near expiry; synthetic tokens far "
        "from expiry unless a scenario scripts the fake token endpoint",
        "redirects: the fake API never sends 3xx; any escape is denied by the sandbox",
    ]
    return checks
