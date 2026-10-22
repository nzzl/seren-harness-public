"""Two-session coordination over a SYNTHETIC catalog campaign.

A stand-in replaces the Claude CLI: it checks the restrictive launch flags, runs only the
`RUN:` commands its prompt names that are also in its allowed list, emits stream-json
events like print mode, and logs start/end so tests can prove sessions never overlap.
These tests prove the coordinator's wiring only, not real Claude session continuity.
"""
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin  # noqa: F401  (imports this checkout)
from seren_harness import campaign, sessions
from test_campaign import CANARY, CASES, answers, record
from test_selection import NEVER, Selecting

A, B, C = list(CASES)
FAKE_CLAUDE = r'''#!{python}
import json, os, shlex, subprocess, sys, time
argv = sys.argv[1:]
log = os.environ["FAKE_CLAUDE_LOG"]
if argv == ["auth", "status"]:
    method = os.environ.get("FAKE_AUTH", "claude.ai")
    print(json.dumps({"loggedIn": method != "none", "authMethod": method}))
    sys.exit(0)
def note(**kw):
    with open(log, "a") as f:
        f.write(json.dumps(dict(kw, pid=os.getpid(), t=time.time())) + "\n")
required = ["-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"]
pairs = {"--tools": "Bash", "--permission-mode": "dontAsk", "--permission-prompts": "none"}
bad = [f for f in required if f not in argv] + [k for k, v in pairs.items()
       if k not in argv or argv[argv.index(k) + 1] != v]
if bad or any("dangerously" in a or "bypass" in a for a in argv):
    sys.exit("refusing unsafe launch: " + str(bad))
sid = argv[argv.index("--session-id") + 1]
i = argv.index("--allowedTools") + 1
allowed = []
while i < len(argv) and not argv[i].startswith("--"):
    allowed.append(argv[i][5:-1]); i += 1
prompt = sys.stdin.read()
note(event="start", session=sid, seren_env=[k for k in os.environ if k.startswith("SEREN_")],
     api_key="ANTHROPIC_API_KEY" in os.environ)
mode = os.environ.get("FAKE_CLAUDE_MODE", "")
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "tools": ["Bash"],
                  "model": "stand-in", "permissionMode": "dontAsk", "mcp_servers": []}))
if mode == "hang":
    time.sleep(60)
ran, denied = 0, []
for line in prompt.splitlines():
    if not line.startswith("RUN: ") or mode == "noop":
        continue
    command = line[5:]
    if command not in allowed:
        denied.append(command); continue
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}))
    done = subprocess.run(shlex.split(command), capture_output=True, text=True)
    ran += 1
    if "verify-handoff" in command and not done.stdout.startswith("handoff verified"):
        break
note(event="end", session=sid, ran=ran)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": sid, "num_turns": ran + 1, "total_cost_usd": 0.0,
                  "usage": {"input_tokens": 10, "output_tokens": 5},
                  "permission_denials": denied, "result": "done " + "sk-" + "Y" * 40}))
'''


class Coordinated(Selecting):
    def __init__(self, root, answer_map):
        super().__init__(root, answer_map)
        self.claude = self.root / "claude"
        self.claude.write_text(standin(FAKE_CLAUDE.replace("{python}", sys.executable)))
        self.claude.chmod(0o755)
        prerun(self.claude)  # first-run assessment outside the timed sessions
        self.log = self.root / "claude-log.jsonl"
        self.catalog({k: {"history": NEVER} for k in CASES})
        self.auth = self.authorize_catalog()

    def coordinate(self, popen=None, now=None, mode="", auth="claude.ai"):
        out = io.StringIO()
        env = {"SEREN_API_KEY": CANARY, "ANTHROPIC_API_KEY": CANARY,
               "FAKE_CLAUDE_LOG": str(self.log), "FAKE_CLAUDE_MODE": mode, "FAKE_AUTH": auth}
        with mock.patch.dict(os.environ, env):
            kwargs = {"now": now} if now else {}
            if popen:
                kwargs["popen"] = popen
            code = sessions.Coordinator(self.catalog_path, self.auth, self.home, self.root,
                                        self.claude, **kwargs).run(out)
        return code, out.getvalue()

    def directory(self):
        return self.home / "campaigns" / "synthetic.catalog"

    def record(self):
        return json.loads((self.directory() / "sessions.json").read_text())

    def launches(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def after_first(action):
    """A popen wrapper that runs `action` once, after session 1 has exited."""
    import subprocess
    state = {"n": 0}

    def popen(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 2:
            raise AssertionError("session 2 must not launch")
        process = subprocess.Popen(*args, **kwargs)
        original = process.communicate

        def communicate(*a, **k):
            result = original(*a, **k)
            action()
            return result
        process.communicate = communicate
        return process
    return popen


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def fixture(self, **kwargs):
        self.n += 1
        return Coordinated(Path(self.tmp.name) / str(self.n), answers(**kwargs))

    def test_session_two_continues_from_the_verified_handoff(self):
        f = self.fixture(b=record())  # B returns a record: one uncertain finding
        code, out = f.coordinate()
        self.assertEqual(code, campaign.OK, out)
        rec = f.record()
        self.assertEqual((rec["used"], [s["status"] for s in rec["sessions"]]),
                         (2, ["exited", "exited"]))
        ids = [s["session_id"] for s in rec["sessions"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual([s["result"]["session_id"] for s in rec["sessions"]], ids)
        self.assertEqual([(s["cases_done_before"], s["cases_done_after"])
                          for s in rec["sessions"]], [(0, 1), (1, 3)])
        log = f.launches()
        self.assertEqual([e["event"] for e in log], ["start", "end", "start", "end"])
        self.assertLess(log[1]["t"], log[2]["t"])  # never two active sessions
        self.assertEqual([e["seren_env"] for e in log if e["event"] == "start"], [[], []])
        # Sessions use the Claude account login, never the API key.
        self.assertEqual([e["api_key"] for e in log if e["event"] == "start"], [False, False])
        self.assertEqual(rec["auth_method"], "claude.ai")
        state = f.catalog_state()
        self.assertEqual(state["selection"]["queue"], [A, B, C])
        self.assertEqual(state["budget"], {"max_service_requests": 3, "reserved": 3,
                                           "issued": 3, "issued_unknown": 0})
        self.assertEqual((f.calls(), f.findings()), (3, {"run_visible_elsewhere": "uncertain"}))
        commands = rec["sessions"][1]["result"]["commands"]
        self.assertIn("verify-handoff", commands[0])  # session 2 starts from the handoff
        self.assertEqual(rec["sessions"][1]["result"]["denials"], [])
        report = (f.directory() / "final-report.md").read_text()
        self.assertIn("Sessions used 2 of 2", report)
        saved = "".join(p.read_text() for p in f.directory().iterdir() if p.is_file())
        self.assertNotIn(CANARY, saved)
        self.assertNotIn("sk-" + "Y" * 40, saved)  # transcripts are sanitized
        code, out = f.coordinate()  # nothing left: no relaunch, no third session
        self.assertEqual((code, len(f.launches())), (campaign.REFUSED, 4))

    def test_no_account_login_launches_nothing(self):
        f = self.fixture()
        code, _ = f.coordinate(auth="api_key")
        self.assertEqual((code, f.record()["used"], f.launches(), f.calls()),
                         (campaign.STOPPED, 0, [], 0))
        self.assertIn("no Claude account login", f.record()["stopped"]["reason"])

    def test_no_progress_or_timeout_ends_coordination_without_relaunch(self):
        f = self.fixture()
        code, _ = f.coordinate(mode="noop")
        self.assertEqual((code, f.record()["used"], f.calls()), (campaign.STOPPED, 1, 0))
        self.assertIn("no durable progress", f.record()["stopped"]["reason"])
        g = self.fixture()
        with mock.patch.object(sessions, "SESSION_SECONDS", 2):
            code, _ = g.coordinate(mode="hang")
        rec = g.record()
        self.assertEqual((code, rec["used"], rec["sessions"][0]["status"]),
                         (campaign.STOPPED, 1, "timed_out"))

    def test_session_two_is_not_started_on_changed_state_or_authority(self):
        f = self.fixture()
        path = f.directory() / "state.json"

        def corrupt():
            state = json.loads(path.read_text())
            state["budget"]["reserved"] = 0  # an optimistic edit the handoff does not describe
            path.write_text(json.dumps(state))
        code, _ = f.coordinate(popen=after_first(corrupt))
        self.assertEqual((code, f.record()["used"]), (campaign.STOPPED, 1))
        self.assertIn("handoff not verified", f.record()["stopped"]["reason"])
        g = self.fixture()

        def uncertain():
            state = json.loads((g.directory() / "state.json").read_text())
            state["cases"][B]["status"] = "in_flight"
            (g.directory() / "state.json").write_text(json.dumps(state))
        g.coordinate(popen=after_first(uncertain))
        self.assertIn("uncertain", g.record()["stopped"]["reason"])
        h = self.fixture()
        h.coordinate(popen=after_first(lambda: h.authorize_catalog(h.auth.name)))
        self.assertIn("authorization differs", h.record()["stopped"]["reason"])
        self.assertEqual(h.calls(), 1)

    def test_expired_window_and_active_session_block_launches(self):
        f = self.fixture()
        late = datetime.now(timezone.utc) + timedelta(minutes=19, seconds=30)
        clock = iter([datetime.now(timezone.utc)] + [late] * 50)
        code, _ = f.coordinate(now=lambda: next(clock))
        self.assertEqual(f.record()["used"], 0)
        self.assertIn("window", f.record()["stopped"]["reason"])
        g = self.fixture()
        g.directory().mkdir(parents=True)
        (g.directory() / "sessions.json").write_text(json.dumps({
            "campaign": "synthetic.catalog", "limit": 2, "used": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
            "sessions": [{"n": 1, "status": "running", "session_id": "x"}], "stopped": None}))
        code, _ = g.coordinate()
        self.assertEqual((code, g.launches()), (campaign.STOPPED, []))
        self.assertIn("no recorded exit", g.record()["stopped"]["reason"])


if __name__ == "__main__":
    unittest.main()
