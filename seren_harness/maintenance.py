"""Supervised maintenance sessions: one owner-selected, offline harness-maintenance goal.

A maintenance session is an ordinary supervised model session (same launcher, same
supervisor tick, same `model-work` wrapper, same six-per-UTC-day count, subscription guard,
wall-time/output/stall caps and locks) whose launch record carries `mode: maintenance`.
Only the supervisor writes that record, and only while the owner's selection file names a
plan committed at HEAD with the recorded digest. Nothing here launches anything.

Differences from a discovery session, all narrowing except the path set:
- the model may edit only PATHS / DIRS (the approved goal's files), nothing else;
- verbs are MAINTENANCE_VERBS only: no live request/review, case runs or discovery state;
- the wrapper never dispatches a live handoff and never refreshes live status;
- worker-run code executes under VERB_PROFILE (no writes to the repository, its .git or
  the discovery home; network denied except loopback; credential folders unreadable);
- checks and commits use the trusted HEAD copy of the validation runner, which runs every
  check under CHECK_PROFILE and commits only itself. When the runner or test runner is among
  the changes, an independent stdlib unittest pass must also succeed with no fewer tests
  than HEAD, so a changed runner never certifies itself.

Threat model: an honest but fallible worker. Test code the worker writes can always lie
about its own results; that limit is the same for discovery workers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SELECTION = "maintenance-goal.json"
FOLDER = "maintenance"
# The approved consolidated goal's files only (docs/maintenance/consolidated-goal.md):
# A pytest runner, B validation reuse, C offline evaluation, D release candidate, checkpoint.
# TASK.md is operational (agents are told to read it) and is never a maintenance path; the
# session checkpoints through `worker progress`. Validation reuse (step B) applies only to
# NARRATIVE.md, a dedicated file that no instruction or prompt directs anyone to read.
NARRATIVE = ".tiny-harness/NARRATIVE.md"
PATHS = ("tests/run.py", ".tiny-harness/run.py", NARRATIVE,
         "tests/test_validation_output.py", "tests/test_prose_route.py",
         "tests/test_pytest_runner.py", "tests/test_validation_reuse.py",
         "tests/test_offline_evals.py", "tests/conftest.py",
         "seren_harness/offline_evals.py", "requirements-dev.txt", "README.md")
DIRS = ("tests/fixtures/offline-evals/", "docs/release/", "docs/maintenance/")
RUNNER_PATHS = ("tests/run.py", ".tiny-harness/run.py")
MAINTENANCE_VERBS = ("status", "test", "check", "commit", "progress")
PROGRESS = ("in_progress", "complete", "blocked")
CHECK_PREFIX = "TINY_HARNESS_CHECK_PREFIX"
# Held by `check`/`commit` for their whole run. Ending a session kills claude, not a commit
# its Bash tool started, so the supervisor launches nothing while this lock is held.
VERB_LOCK = "maintenance-verb"
TOOLS = "Read,Edit,Write,Glob,Grep,Bash"
_BASE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(deny file-read* (subpath (string-append (param "HOME_DIR") "/.config"))
                 (subpath (string-append (param "HOME_DIR") "/.codex"))
                 (subpath (string-append (param "HOME_DIR") "/.claude"))
                 (subpath (string-append (param "HOME_DIR") "/.ssh"))
                 (subpath (string-append (param "HOME_DIR") "/.aws"))
                 (subpath (string-append (param "HOME_DIR") "/Library/Keychains"))
                 (subpath (string-append (param "HOME_DIR") "/Documents"))
                 (subpath (string-append (param "HOME_DIR") "/seren-harness-live")))
(deny file-write* (subpath (param "HOME_DIR")))
"""
# Worker-run code (diagnostic tests) and the checks of a trusted-runner validation: the
# checks run in a temporary snapshot, so nothing under the home folder is writable at all.
VERB_PROFILE = CHECK_PROFILE = _BASE


class Refused(Exception):
    pass


def owned(path):
    return path in PATHS or path.startswith(DIRS)


def blob_sha(repo, rev_path):
    out = subprocess.run(["/usr/bin/git", "-C", str(repo), "show", rev_path],
                         capture_output=True, timeout=30)
    return hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None


def selection(home, repo):
    """(selection, None) for a valid owner selection, (None, reason) otherwise.

    Absent file: (None, None), i.e. no maintenance goal and ordinary discovery."""
    path = Path(home.root) / SELECTION
    if not path.exists():
        return None, None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, "maintenance selection unreadable"
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k]
                                              for k in ("goal", "plan", "plan_sha256",
                                                        "selected_by", "selected_at")):
        return None, "maintenance selection incomplete"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", value["goal"]):
        return None, "maintenance goal id invalid"
    if not owned(value["plan"]) or not value["plan"].startswith("docs/maintenance/"):
        return None, "maintenance plan must be under docs/maintenance/"
    if blob_sha(repo, "HEAD:" + value["plan"]) != value["plan_sha256"]:
        return None, "maintenance plan at HEAD differs from the selected digest"
    status = progress(home, value["goal"]).get("status")
    if status in ("complete", "blocked"):
        return None, "maintenance goal %s is %s; the owner clears or replaces the selection" % (
            value["goal"], status)
    return value, None


def progress_path(home, goal):
    return Path(home.root) / FOLDER / goal / "progress.json"


def progress(home, goal):
    try:
        return json.loads(progress_path(home, goal).read_text())
    except (OSError, ValueError):
        return {}


def select(home, repo, goal, plan, owner):
    """Owner-invoked only (`discovery select-maintenance`); records the selection."""
    from seren_harness import discovery
    value = {"goal": goal, "plan": plan, "plan_sha256": blob_sha(repo, "HEAD:" + plan) or "",
             "selected_by": owner, "selected_at": discovery.iso()}
    path = Path(home.root) / SELECTION
    discovery.write_json(path, value)
    os.chmod(path, 0o600)
    chosen, reason = selection(home, repo)
    if chosen is None:
        path.unlink()
        raise Refused(reason or "maintenance selection invalid")
    return chosen


def current(home, session):
    """The supervisor's launch record for this session when it is a maintenance session."""
    try:
        record = json.loads((Path(home.root) / "worker.json").read_text())
    except (OSError, ValueError):
        return None
    if (session and record.get("session") == session and record.get("mode") == "maintenance"
            and record.get("status") in ("launching", "running")):
        return record
    return None


# A checked commit takes about 6 minutes, twice that with the independent floor. Claude
# Code's defaults stop a foreground Bash call, or a background one once the turn is idle,
# after 10 minutes. The wrapper's wall time still ends the whole session.
CLAUDE_ENV = {"BASH_MAX_TIMEOUT_MS": "1800000", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "1800000"}

PROMPT = """You are an unattended Seren Harness maintenance worker (offline only).
Session {session}; hard deadline {deadline} UTC (the supervisor ends the session then).
Nobody will answer questions. Goal {goal}: follow {plan} in this repository exactly. Start by
reading AGENTS.md, .tiny-harness/CONTRACT.md, docs/MODEL-WORKER.md (Maintenance sessions)
and {plan}, then run `python3 -m seren_harness worker status`.
<working_directory>{repo}</working_directory>
<objective>Advance the next unfinished step of the plan. You may edit only the paths
`worker status` lists. Verify with `worker test`, check with `worker check`, commit with
`worker commit -m`. Run `worker check` and `worker commit` in the foreground with a Bash
timeout of 1800000 ms; they can take about 15 minutes. No live service, credential,
disclosure or campaign action exists in this session. Before the deadline, or on any blocker, record progress with
`worker progress --status in_progress|complete|blocked --text ...` and stop. Never try to
extend the session; the next eligible session continues from your recorded progress.</objective>
"""


def allowed_tools(repo):
    root = "//" + str(repo).lstrip("/")
    rules = ["Edit(%s/%s)" % (root, p) for p in PATHS]
    rules += ["Write(%s/%s)" % (root, p) for p in PATHS]
    rules += ["Edit(%s/%s**)" % (root, d) for d in DIRS]
    rules += ["Write(%s/%s**)" % (root, d) for d in DIRS]
    return rules + ["Bash(python3 -m seren_harness worker:*)", "Bash(git status:*)",
                    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]


def claude_argv(cfg, session, deadline, goal):
    """No --add-dir: the repository is the only readable workspace."""
    return [cfg["claude"], "-p", PROMPT.format(session=session, deadline=deadline,
                                               repo=cfg["repo"], goal=goal["goal"],
                                               plan=goal["plan"]),
            "--model", cfg["model"], "--output-format", "stream-json", "--verbose",
            "--tools", TOOLS, "--allowedTools", *allowed_tools(cfg["repo"]),
            "--disallowedTools", "WebFetch", "WebSearch",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--strict-mcp-config", "--setting-sources", "project",
            "--session-id", session]


def profile_path(disc, name, text):
    path = Path(disc) / name
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        os.chmod(path, 0o600)
    return path


def confined(disc, name, text, argv):
    """argv under `text`; an already-confined caller keeps its stricter outer profile."""
    from seren_harness import model_worker
    if model_worker.already_sandboxed():
        return list(argv)
    return ["/usr/bin/sandbox-exec", "-f", str(profile_path(disc, name, text)),
            "-D", "HOME_DIR=" + str(Path.home().resolve()), *argv]


# ---------------------------------------------------------------- verbs (outer process)

def run_verb(home, repo, session, args, goal):
    """Maintenance verbs. Trusted code runs here, outside the sandbox; worker-run code only
    ever runs confined. Every other verb is refused."""
    if args.verb not in MAINTENANCE_VERBS:
        print("refused: %r is not available in a maintenance session (allowed: %s)"
              % (args.verb, ", ".join(MAINTENANCE_VERBS)))
        return 2
    verb = {"status": verb_status, "test": verb_test, "check": verb_check,
            "commit": verb_commit, "progress": verb_progress}[args.verb]
    if args.verb not in ("check", "commit"):
        return verb(home, Path(repo), session, args, goal)
    with home.lock(VERB_LOCK, blocking=False) as free:
        if not free:
            print("refused: an earlier maintenance check or commit is still running; "
                  "wait for it to finish (see git log), then retry")
            return 2
        return verb(home, Path(repo), session, args, goal)


def verb_running(home):
    """True while a check or commit holds the verb lock, even after its session ended."""
    with home.lock(VERB_LOCK, blocking=False) as free:
        return not free


def dirty(repo):
    from seren_harness import model_worker
    return model_worker.dirty(repo)


def verb_status(home, repo, session, args, goal):
    print("Maintenance session:", session, "goal:", goal["goal"], "plan:", goal["plan"])
    print("Editable paths:", ", ".join(PATHS + DIRS))
    print("Verbs:", ", ".join(MAINTENANCE_VERBS))
    print("Uncommitted (yours):", [p for p in dirty(repo) if owned(p)] or "none")
    print("Uncommitted (never touch):", [p for p in dirty(repo) if not owned(p)] or "none")
    print("Progress:", json.dumps(progress(home, goal["goal"]), indent=1)[-6000:] or "none")
    return 0


RUNNERS = ("unittest", "pytest")
TARGET = re.compile(r"(test_[a-z0-9_]+)(?:\.([A-Za-z_][A-Za-z0-9_]*))?(?:\.([A-Za-z_][A-Za-z0-9_]*))?")


def test_argv(repo, runner, names):
    """The fixed command for a focused diagnostic run, or None when anything is invalid.
    Only the two runners and existing tests/test_*.py targets; never a free command."""
    if runner not in RUNNERS or not names or len(names) > 20:
        return None
    parsed = [TARGET.fullmatch(n) for n in names]
    if not all(parsed) or not all((Path(repo) / "tests" / (m.group(1) + ".py")).is_file()
                                  for m in parsed):
        return None
    if runner == "unittest":
        return [sys.executable, "-B", "tests/run.py", "--runner", "unittest", *names]
    nodes = ["::".join(["tests/%s.py" % m.group(1)] + [p for p in m.groups()[1:] if p])
             for m in parsed]
    return [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", *nodes]


def verb_test(home, repo, session, args, goal):
    """Focused diagnostics only (never validation evidence), confined."""
    from seren_harness import model_worker
    runner = getattr(args, "runner", None) or "unittest"
    argv = test_argv(repo, runner, args.modules or [])
    if argv is None:
        print("refused: --runner unittest|pytest and one or more existing targets "
              "(test_x, test_x.Class or test_x.Class.test_y)")
        return 2
    print("FOCUSED DIAGNOSTIC RUN (%s): not validation evidence" % runner)
    return model_worker._bounded(confined(home.root, "maintenance-verb.sb", VERB_PROFILE,
                                          argv), repo, 900)


def verb_check(home, repo, session, args, goal):
    return trusted_validation(home, repo, None)


def verb_commit(home, repo, session, args, goal):
    subject = " ".join((args.message or "").splitlines()[:1]).strip()[:200]
    if not subject:
        print("refused: a one-line commit subject is required")
        return 2
    code = trusted_validation(home, repo, "[maintenance %s] %s" % (session[:8], subject))
    record(home, goal["goal"], session, "commit", "exit %d: %s" % (code, subject))
    return code


def verb_progress(home, repo, session, args, goal):
    status = args.status or "in_progress"
    if status not in PROGRESS or not (args.text or "").strip():
        print("progress needs --status %s and --text" % "|".join(PROGRESS))
        return 2
    record(home, goal["goal"], session, status, args.text)
    return 0


def record(home, goal, session, status, text):
    from seren_harness import discovery
    path = progress_path(home, goal)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = progress(home, goal)
    entries = value.get("entries", []) + [{"session": session, "at": discovery.iso(),
                                           "status": status, "text": text[:2000]}]
    if status in PROGRESS:
        value["status"] = status
    value["entries"] = entries[-200:]
    discovery.write_json(path, value)


def scope_problems(repo):
    """Changed paths a maintenance commit may never include (staged or not)."""
    staged = subprocess.run(["/usr/bin/git", "-C", str(repo), "diff", "--cached",
                             "--name-only"], capture_output=True, text=True).stdout.split()
    return sorted(p for p in set(dirty(repo)) | set(staged) if not owned(p))


def trusted_validation(home, repo, message, run=subprocess.run):
    """Full validation (and commit when `message`) by the HEAD copy of the runner, with each
    check confined. A changed runner additionally needs the independent unittest floor."""
    foreign = scope_problems(repo)
    if foreign:
        print("refused: changes outside the maintenance paths:", foreign)
        return 2
    mine = [p for p in dirty(repo) if owned(p)]
    if not mine:
        print("nothing to check")
        return 2
    run(["/usr/bin/git", "-C", str(repo), "add", "-A", "--", *mine], check=True)
    code = _validate_staged(home, repo, message, mine, run)
    if code != 0 or message is None:  # leave work in the tree, never half-staged
        run(["/usr/bin/git", "-C", str(repo), "restore", "--staged", "--", *mine])
    return code


def _validate_staged(home, repo, message, mine, run):
    with tempfile.TemporaryDirectory(prefix="maintenance-runner-") as temporary:
        runner = Path(temporary) / "run.py"
        shown = run(["/usr/bin/git", "-C", str(repo), "show", "HEAD:.tiny-harness/run.py"],
                    capture_output=True)
        if shown.returncode != 0:
            print("refused: no committed validation runner at HEAD")
            return 2
        runner.write_bytes(shown.stdout)
        if any(p in RUNNER_PATHS for p in mine):
            floor = independent_floor(home, repo, run)
            if floor:
                print("refused:", floor)
                return 3
        prefix = confined(home.root, "maintenance-check.sb", CHECK_PROFILE, [])
        env = dict(os.environ, **{CHECK_PREFIX: json.dumps(prefix)})
        command = ["python3", str(runner)] + (["commit", "--full", "-m", message] if message
                                              else ["validate"])
        return run(command, cwd=str(repo), env=env, timeout=1500).returncode


# Plain stdlib discovery, the same module layout tests/run.py uses, but none of its code.
COUNT = r"""
import json, sys, unittest
sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.discover(sys.argv[1] + "/tests")
if sys.argv[2] == "count":
    print(json.dumps({"tests": suite.countTestCases()}))
else:
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    print(json.dumps({"tests": result.testsRun, "ok": result.wasSuccessful(),
                      "skipped": len(result.skipped)}))
"""


def independent_floor(home, repo, run=subprocess.run):
    """None, or why the staged tree fails the independent stdlib check: every test discovered
    by plain unittest must pass and the staged tree must have no fewer tests than HEAD."""
    with tempfile.TemporaryDirectory(prefix="maintenance-floor-") as temporary:
        head, staged = Path(temporary) / "head", Path(temporary) / "staged"
        for target in (head, staged):
            target.mkdir()
        archive = run(["/usr/bin/git", "-C", str(repo), "archive", "HEAD"], capture_output=True)
        run(["/usr/bin/tar", "-x", "-C", str(head)], input=archive.stdout, check=True)
        run(["/usr/bin/git", "-C", str(repo), "checkout-index", "--all",
             "--prefix=" + str(staged) + os.sep], check=True)
        results = {}
        for name, root, mode in (("head", head, "count"), ("staged", staged, "run")):
            argv = confined(home.root, "maintenance-check.sb", CHECK_PROFILE,
                            [sys.executable, "-B", "-c", COUNT, str(root), mode])
            out = run(argv, cwd=str(root), capture_output=True, text=True, timeout=1500)
            try:
                results[name] = json.loads(out.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return "independent unittest pass produced no result (%s)" % name
    if not results["staged"].get("ok"):
        return "independent unittest pass failed"
    if results["staged"]["tests"] < results["head"]["tests"]:
        return "independent unittest pass ran %d tests, fewer than HEAD's %d" % (
            results["staged"]["tests"], results["head"]["tests"])
    print("Independent unittest floor: %(tests)d tests passed" % results["staged"])
    return None
