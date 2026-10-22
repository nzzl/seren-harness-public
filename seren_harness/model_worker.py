"""Supervised Claude Code model workers for offline discovery (subscription login only).

A model worker derives new Tier 0 candidates, writes their cases and judge tests, verifies
them and commits through the checked route. It never runs the Seren CLI directly: its only
code-executing commands are the `worker` verbs below, and each verb re-executes itself
inside WORKER_PROFILE (network denied except loopback; credential locations unreadable;
writes limited to the repository, the discovery home and temporary files). The model
provider connection belongs to the `claude` process itself, which is not sandboxed by this
profile. Tool scope is enforced by Claude Code permission rules in `dontAsk` mode with
permission prompts disabled, so a disallowed action is denied, never left waiting.

Account route: `claude -p` with an explicit environment that carries no ANTHROPIC_* or
third-party provider variables; the init event must report `apiKeySource: none` (the
claude.ai subscription login) or the wrapper ends the session and blocks model launches.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

EDITABLE = ("seren_harness/tier0_cases.py", "tests/test_tier0.py")
EDITABLE_DIRS = ("docs/tier0/", "docs/synthetic-cases/")
TOOLS = "Read,Edit,Write,Glob,Grep,Bash"
FORBIDDEN_ENV = re.compile(r"^(ANTHROPIC_|CLAUDE_CODE_USE_|AWS_|GOOGLE_|VERTEX|BEDROCK)")
RATE_LIMIT = re.compile(r"rate.?limit|usage limit|429|overloaded|quota", re.I)
LIMIT_KEYS = ("wall_seconds", "log_bytes", "stall_seconds", "sessions_per_day",
              "backoff_seconds", "rate_limit_backoff_seconds")
CONFIG_KEYS = ("enabled", "claude", "model", "repo", "pinned_source", "contract_map", "limits")
WORKER_PROFILE = """(version 1)
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
(allow file-read* (subpath (param "DISC")))
(deny file-write* (subpath (param "HOME_DIR")))
(allow file-write* (subpath (param "REPO")) (subpath (param "DISC")))
"""
PROMPT = """You are an unattended Seren Harness discovery model worker.
Session {session}; hard deadline {deadline} UTC (the supervisor ends the session then).
Nobody will answer questions. Follow docs/MODEL-WORKER.md in this repository exactly; it
derives everything from the repository and durable state. Start by reading AGENTS.md,
.tiny-harness/CONTRACT.md, docs/DISCOVERY.md and docs/MODEL-WORKER.md, then run
`python3 -m seren_harness worker status`.
<working_directory>{repo}</working_directory>
<objective>Own customer discovery, including the separately authorized Clock Test lane.
Read docs/AUTONOMOUS-DISCOVERY.md and its Claude handoff procedure.
Compare live and offline customer questions; inspect saved live status and outcomes.
No disclosures or direct service calls. Finish one useful investigation or preparation
step, save evidence and next action, then stop within this session deadline.</objective>
"""


class ConfigError(RuntimeError):
    pass


def owned(path):
    return path in EDITABLE or path.startswith(EDITABLE_DIRS)


def config(authority):
    value = authority.get("model_worker") or {}
    if not value.get("enabled"):
        return None
    missing = [k for k in CONFIG_KEYS if k not in value]
    missing += [k for k in LIMIT_KEYS if k not in value.get("limits", {})]
    if missing:
        raise ConfigError("model worker config missing: " + ", ".join(missing))
    return value


def profile_path(disc):
    path = Path(disc) / "worker.sb"
    if not path.exists() or path.read_text() != WORKER_PROFILE:
        path.write_text(WORKER_PROFILE)
        os.chmod(path, 0o600)
    return path


def already_sandboxed():
    """True when this process is already confined (macOS sandbox_check, not an env flag).
    macOS refuses to nest sandboxes with file rules, and a child inherits its parent's
    sandbox, so an already-confined caller runs the child under the same outer profile."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        libc.sandbox_check.restype = ctypes.c_int
        libc.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        return libc.sandbox_check(os.getpid(), None, 0) == 1
    except (OSError, AttributeError):
        return False


def sandboxed(disc, repo, argv):
    if already_sandboxed():
        return list(argv)
    return ["/usr/bin/sandbox-exec", "-f", str(profile_path(disc)),
            "-D", "HOME_DIR=" + str(Path.home().resolve()), "-D", "REPO=" + str(repo),
            "-D", "DISC=" + str(Path(disc).resolve()), *argv]


def allowed_tools(repo):
    rules = ["Edit(/%s%s/%s)" % ("/", str(repo).lstrip("/"), p) for p in EDITABLE]
    rules += ["Edit(/%s%s/%s**)" % ("/", str(repo).lstrip("/"), d) for d in EDITABLE_DIRS]
    rules += ["Write(/%s%s/docs/synthetic-cases/*.json)" % ("/", str(repo).lstrip("/"))]
    rules += ["Bash(python3 -m seren_harness worker:*)",
              "Bash(python3 -m seren_harness discovery status)",
              "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
              "Bash(git show:*)"]
    return rules


def claude_argv(cfg, session, deadline, disc=None):
    reference = [cfg["pinned_source"], cfg["contract_map"]]
    if disc:  # saved reproductions and finding drafts, read-only (no Edit rule covers them)
        reference += [str(Path(disc) / "evidence"), str(Path(disc) / "findings")]
    if disc and (Path(disc) / "live-authority.json").exists():
        reference += [str(disc), str(Path.home() / "seren-harness-recipient/finance/runs"),
                      str(Path.home() / "seren-harness-recipient/business-development")]
    return [cfg["claude"], "-p", PROMPT.format(session=session, deadline=deadline, repo=cfg["repo"]),
            "--model", cfg["model"], "--output-format", "stream-json", "--verbose",
            "--tools", TOOLS, "--allowedTools", *allowed_tools(cfg["repo"]),
            "--disallowedTools", "WebFetch", "WebSearch",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--strict-mcp-config", "--setting-sources", "project",
            "--add-dir", *reference,
            "--session-id", session]


def claude_env(cfg, session, disc):
    env = {"HOME": os.environ.get("HOME", str(Path.home())),
           "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", ""),
           "LANG": "en_US.UTF-8",
           "PATH": "%s/.venv/bin:%s:/usr/bin:/bin" % (cfg["repo"],
                                                       os.path.dirname(cfg["claude"])),
           "SEREN_DISCOVERY_SESSION": session, "SEREN_DISCOVERY_HOME": str(disc)}
    if any(FORBIDDEN_ENV.match(k) for k in env):
        raise ConfigError("provider credentials must never reach a model worker")
    return env


def dirty(repo):
    out = subprocess.run(["/usr/bin/git", "-C", str(repo), "status", "--porcelain",
                          "--untracked-files=all"], capture_output=True, text=True,
                         timeout=30).stdout
    return sorted(line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip())


def dirty_hashes(repo):
    """Uncommitted paths with a content hash. Ownership of leftover edits is proven by the
    hash a model session recorded when it ended, not by the path: another writer's
    uncommitted change under a worker path is foreign and is never committed or
    discarded by a model worker."""
    out = {}
    for path in dirty(repo):
        target = Path(repo) / path
        out[path] = (hashlib.sha256(target.read_bytes()).hexdigest()
                     if target.is_file() else "deleted")
    return out


def claimable(repo, start, mine=owned):
    """Leftovers a session may claim: worker paths it changed since its start record, or
    recorded worker leftovers it inherited (verified by the supervisor before launch)."""
    before, inherited = start.get("dirty_before") or {}, start.get("inherited") or {}
    return {p: h for p, h in dirty_hashes(repo).items()
            if mine(p) and (before.get(p) != h or p in inherited)}


def foreign_changes(repo_dirty, worker_left, mine=owned):
    return sorted(p for p, h in repo_dirty.items()
                  if not mine(p) or (worker_left or {}).get(p) != h)


# ---------------------------------------------------------------- wrapper session

def run_session(home, session, deadline_epoch, cfg, popen=subprocess.Popen, goal=None):
    """The supervised wrapper: holds the work lock, runs one claude session, enforces the
    wall-time and output caps, verifies the account route, and summarizes the result.
    A maintenance `goal` narrows it: no live status, no live dispatch, its own paths."""
    from seren_harness import discovery, maintenance
    from seren_harness import claude_live, claude_budget
    session_owns = maintenance.owned if goal else owned
    if goal is None:
        claude_live.snapshot(home)
    limits = cfg["limits"]
    log = home.root / "log" / ("model-%s.jsonl" % session)
    summary = {"session": session, "started_at": discovery.iso(),
               "dirty_before": dirty_hashes(cfg["repo"]),
               "inherited": (home.state().get("model") or {}).get("owned_dirty") or {}}
    with home.lock("work", blocking=False) as mine:
        with home.lock("live-handoff", False) as handoff:
            if not mine or not handoff:
                return "busy", summary
        budget = claude_budget.check(home, cfg, force=True)
        if not budget["allowed"]:
            summary["subscription_budget"] = budget
            return "blocked:subscription_budget", summary
        # Durable before launch: if this wrapper is killed, the supervisor can still tell
        # which worker-path edits this session made.
        discovery.write_json(home.root / "log" / ("model-%s.start.json" % session),
                             {k: summary[k] for k in ("dirty_before", "inherited")})
        deadline = discovery.iso(discovery.datetime.fromtimestamp(deadline_epoch,
                                                                 discovery.timezone.utc))
        argv = (maintenance.claude_argv(cfg, session, deadline, goal) if goal
                else claude_argv(cfg, session, deadline, home.root))
        env = dict(claude_env(cfg, session, home.root), **(maintenance.CLAUDE_ENV if goal else {}))
        child = popen(argv, cwd=cfg["repo"], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        state = {"outcome": None, "bytes": 0, "beat": 0.0}

        def pump():
            with open(log, "ab") as out:
                for raw in child.stdout:
                    state["bytes"] += len(raw)
                    if state["bytes"] <= limits["log_bytes"]:
                        out.write(raw)
                        out.flush()
                    _inspect(raw, summary, state)
                    if time.time() - state["beat"] > 15:
                        state["beat"] = time.time()
                        discovery._heartbeat(home, session,
                                             current={"model_turns": summary.get("turns", 0)})
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        next_budget_check = time.monotonic() + claude_budget.POLICY["poll_seconds"]
        while child.poll() is None:
            reason = None
            if "subscription_budget" in cfg and time.monotonic() >= next_budget_check:
                budget = claude_budget.check(home, cfg, force=True)
                summary["subscription_budget"] = budget
                next_budget_check = time.monotonic() + claude_budget.POLICY["poll_seconds"]
                if not budget["allowed"]:
                    reason = "blocked:subscription_budget"
            if time.time() > deadline_epoch:
                reason = "limit:wall_time"
            elif state["bytes"] > limits["log_bytes"]:
                reason = "limit:output"
            elif state["outcome"] == "blocked:not_subscription":
                reason = "blocked:not_subscription"
            if reason:
                state["outcome"] = reason
                child.kill()
                break
            time.sleep(1)
        child.wait()
        reader.join(10)
    summary.update(exit=child.returncode, ended_at=discovery.iso(),
                   dirty_after=claimable(cfg["repo"], summary, session_owns))
    outcome = state["outcome"] or _result_outcome(summary)
    summary["outcome"] = outcome
    if goal:
        summary["maintenance_goal"] = goal["goal"]
    elif outcome == "model:success":
        claude_live.dispatch(home, deadline_epoch)
        summary["ended_at"] = discovery.iso()
    return outcome, summary


def _inspect(raw, summary, state):
    try:
        message = json.loads(raw)
    except ValueError:
        return
    if message.get("type") == "system" and message.get("subtype") == "init":
        summary["init"] = {k: message.get(k) for k in ("apiKeySource", "model",
                                                        "permissionMode", "tools")}
        summary["init"]["mcp_servers"] = len(message.get("mcp_servers") or [])
        if message.get("apiKeySource") != "none":
            state["outcome"] = "blocked:not_subscription"
    elif message.get("type") == "assistant":
        summary["turns"] = summary.get("turns", 0) + 1
    elif message.get("type") == "result":
        summary["result"] = {k: message.get(k) for k in (
            "subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd")}
        summary["result"]["text"] = str(message.get("result") or "")[:2000]
        summary["result"]["permission_denials"] = len(message.get("permission_denials")
                                                      or [])


def _result_outcome(summary):
    result = summary.get("result")
    if not result:
        return "failed:no_result"
    if result.get("is_error"):
        return "rate_limited" if RATE_LIMIT.search(result.get("text", "")) else \
            "failed:" + str(result.get("subtype"))
    return "model:success"


# ---------------------------------------------------------------- worker verbs

def run(args):
    """`python -m seren_harness worker <verb>`: every verb runs inside WORKER_PROFILE."""
    from seren_harness import discovery, maintenance
    home = discovery.Home(os.environ.get("SEREN_DISCOVERY_HOME") or args.home)
    repo = Path.cwd().resolve()
    # The supervisor's launch record, never the caller, decides a maintenance session.
    if maintenance.current(home, os.environ.get("SEREN_DISCOVERY_SESSION")) is not None:
        goal, reason = maintenance.selection(home, repo)
        record = maintenance.current(home, os.environ.get("SEREN_DISCOVERY_SESSION"))
        if goal is None or goal["goal"] != record.get("goal"):
            print("refused: the maintenance selection is no longer valid:", reason)
            return 2
        return maintenance.run_verb(home, repo, os.environ["SEREN_DISCOVERY_SESSION"], args,
                                    goal)
    if not os.environ.get("SEREN_WORKER_SANDBOXED"):
        env = dict(os.environ, SEREN_WORKER_SANDBOXED="1",
                   PYTHONPATH="%s:%s/tests" % (repo, repo))
        child = [sys.executable, "-m", "seren_harness", "worker",
                 *sys.argv[sys.argv.index("worker") + 1:]]
        if args.verb == "run-case":  # the trusted parent owns the lab (see tier0.LabServer)
            code, isolation = discovery.lab_host(home, home.authority(), child, env, repo, 1500)
            if code is None:
                print("isolation check failed; no scenario ran:", isolation)
                return 3
            return code
        argv = sandboxed(home.root, repo, child)
        try:
            return subprocess.run(argv, env=env, cwd=str(repo), timeout=1500).returncode
        except subprocess.TimeoutExpired:
            print("worker verb exceeded 1500 s and was stopped")
            return 124
    session = os.environ.get("SEREN_DISCOVERY_SESSION", "interactive")
    if args.verb in ("live-request", "live-review"):
        from seren_harness import claude_live
        return (claude_live.request if args.verb == "live-request" else claude_live.review)(
            home, session, args)
    return VERBS[args.verb](home, repo, session, args)


def _bounded(argv, repo, timeout, capture=None):
    try:
        out = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        print("stopped after %d s" % timeout)
        return 124
    text = out.stdout + out.stderr
    print(text[-12000:] if len(text) > 12000 else text)
    if capture is not None:
        capture.append(text)
    return out.returncode


REPAIR_BUDGET = 3
FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): \S+ \(([\w.]+)\)", re.M)
ROUTE = re.compile(r"^ROUTE (focused|full) for staged tree ([0-9a-f]+)", re.M)
VALIDATION_PATHS = (".tiny-harness/", "tests/run.py")


def classify_check_failure(text):
    """('worker_change' | 'harness' | 'unknown', failing ids). A failure in a test module or
    lint path the worker may edit is the worker's change; failures confined to other tests
    are harness problems the worker cannot repair (they still consume the budget)."""
    tests = sorted(set(FAILED_TEST.findall(text)))
    modules = [p[len("tests/"):-3] for p in EDITABLE if p.startswith("tests/")]
    owned_tests = tuple(m + "." for m in modules) + tuple("tests.%s." % m for m in modules)
    if "FAIL basic Python lint" in text:
        lint = [p for p in EDITABLE if p in text]
        return ("worker_change" if lint else "harness"), tests or ["lint"]
    if not tests:
        return "unknown", []
    if any(t.startswith(owned_tests) for t in tests):
        return "worker_change", tests
    return "harness", tests


def verb_status(home, repo, session, args):
    from seren_harness import tier0_cases
    state = home.state()
    model = state.get("model", {})
    authority = home.authority()
    cfg = authority.get("model_worker", {})
    print("Session:", session)
    print("Pinned source (read-only):", cfg.get("pinned_source"))
    print("Contract map (read-only):", cfg.get("contract_map"))
    print("Saved reproductions (read-only): %s/<case>/<attempt>/{observations.jsonl,"
          "result.json}; finding drafts: %s" % (home.root / "evidence",
                                                 home.root / "findings"))
    print("Editable paths:", ", ".join(EDITABLE + EDITABLE_DIRS))
    print("Your uncommitted owned changes:", [p for p in dirty(repo) if owned(p)] or "none")
    print("Other uncommitted changes (never touch):",
          [p for p in dirty(repo) if not owned(p)] or "none")
    print("\nCatalog (execution status from the deterministic worker):")
    for case_id in tier0_cases.ORDER:
        entry = state["cases"].get(case_id, {})
        print("  %s %s: %s %s%s" % (case_id, tier0_cases.CASES[case_id]["title"],
                                    entry.get("status", "not yet run"),
                                    "; ".join("%s=%s" % (r["part"], r["disposition"])
                                              for r in entry.get("results", [])),
                                    " [latest evidence: evidence/%s/%s]" % (
                                        case_id, entry["last_attempt"])
                                    if entry.get("last_attempt") else ""))
    print("\nCandidates recorded by model workers:")
    for key, item in sorted(model.get("candidates", {}).items()):
        print("  %s [%s] %s | basis: %s" % (key, item["status"], item["rationale"][:200],
                                            item.get("basis", "")[:160]))
    print("\nRecent model-worker notes:")
    for note in model.get("notes", [])[-12:]:
        print("  %s %s: %s" % (note["at"], note["session"][:8], note["text"][:300]))
    eligible, gated = followup_groups(state)
    print("\nFollow-ups you may pick (Tier 0; compare with new coverage by expected customer "
          "value and information per effort):")
    for item in eligible or []:
        print("  %s [%s, cost %s] findings %s\n    problem: %s\n    consequence: %s\n"
              "    confidence: %s | uncertainty: %s\n    exposure: %s | workaround: %s\n"
              "    next step: %s" % (item["id"], item["status"], item.get("cost"),
                                     item.get("findings"), item["problem"],
                                     item["consequence"], item["confidence"],
                                     item["uncertainty"], item["exposure"],
                                     item["workaround"], item["next_step"]))
    if not eligible:
        print("  none")
    print("\nFollow-ups awaiting authority (never pick; continue independent work):")
    for item in gated:
        print("  %s [needs %s] %s -> %s" % (item["id"], item["needs"], item["problem"][:120],
                                            item["next_step"][:160]))
    repair = {k: v for k, v in model.get("repair", {}).items() if v.get("failures")}
    print("\nRepair budget used (failed checked commits of %d):" % REPAIR_BUDGET,
          repair or "none")
    print("Harness test failures seen in checked commits:",
          state.get("harness_failures") or "none")
    print_journeys(home)
    print("\nModel wait:", model.get("wait") or "none")
    return 0


def print_journeys(home):
    """Cross-journey comparison: priority journeys first, unchanged blockers dated once."""
    from seren_harness import journeys
    saved = {r["id"]: r for r in discovery_json(home, "journey-status.json").get("journeys", [])}
    try:
        rows = journeys.compare(journeys.load(), discovery_json(home, "live-authority.json"),
                                discovery_json(home, "claude-live-status.json"))
    except (ValueError, KeyError, OSError, TypeError, AttributeError) as error:
        print("\nCustomer journeys: unavailable (%s)" % type(error).__name__)
        return
    print("\nCustomer journeys (compare before selecting; docs/AUTONOMOUS-DISCOVERY.md):")
    for row in rows:
        since = saved.get(row["id"], {})
        dated = (" since " + since["blocked_since"] if since.get("fingerprint")
                 == row["fingerprint"] and since.get("blocked_since") else "")
        print("  %s [%s, %s%s, fingerprint %s] %s" % (
            row["id"], row["priority"], row["state"], dated, row["fingerprint"], row["title"]))
        for reason in row["blockers"]:
            print("    - " + reason)
    print("  Next:", journeys.next_action(rows))


def discovery_json(home, name):
    try:
        return json.loads((home.root / name).read_text())
    except (OSError, ValueError):
        return {}


def verb_test(home, repo, session, args):
    modules = args.modules or ["tests.test_tier0"]
    if getattr(args, "runner", None) not in (None, "unittest"):
        print("refused: --runner is available only in maintenance sessions")
        return 2
    if not all(re.fullmatch(r"tests\.test_[a-z0-9_]+", m) for m in modules):
        print("only tests.test_* modules")
        return 2
    return _bounded([sys.executable, "-m", "unittest", *modules], repo, 900)


def verb_validate(home, repo, session, args):
    return _bounded(["python3", ".tiny-harness/run.py", "validate"], repo, 1200)


def verb_synthetic_check(home, repo, session, args):
    from seren_harness import synthetic_invoices, invoice_catalog
    synthetic_invoices.load(args.case)
    print(json.dumps({'case': args.case, 'case_digest': invoice_catalog.digest(args.case),
                      'expected': invoice_catalog.expected(args.case),
                      'status': 'offline_validated_no_live_calls'}, indent=2))
    return 0


def verb_run_case(home, repo, session, args):
    return _bounded([sys.executable, "-m", "seren_harness", "discovery", "run-case",
                     "--case", args.case, "--repeats", str(args.repeats), "--home",
                     str(home.root)], repo, 900)


def verb_commit(home, repo, session, args):
    from seren_harness import discovery
    staged = subprocess.run(["/usr/bin/git", "diff", "--cached", "--name-only"],
                            cwd=str(repo), capture_output=True, text=True).stdout.split()
    foreign = [p for p in staged if not owned(p)]
    if foreign:
        print("refused: files outside the worker's paths are staged:", foreign)
        return 2
    changed = dirty(repo)
    # The runner and its check policy execute from the working tree; a worker session must
    # never commit through an altered copy (it could not select or report honestly).
    tampered = [p for p in changed if p.startswith(VALIDATION_PATHS)]
    if tampered:
        print("refused: validation runner or policy differs from HEAD:", tampered)
        return 2
    mine = [p for p in changed if owned(p)]
    if not mine:
        print("nothing owned to commit")
        return 2
    candidate = (getattr(args, "candidate", None) or "").strip()
    if not candidate:
        print("refused: --candidate <id> (candidate or follow-up) is required")
        return 2
    budget = home.state().get("model", {}).get("repair", {}).get(candidate, {})
    if budget.get("failures", 0) >= REPAIR_BUDGET:
        print("refused: repair budget exhausted for %s (%d failed checked commits; last: %s). "
              "Record it as deferred with its diagnostics and pick independent work."
              % (candidate, budget["failures"], budget.get("last")))
        return 3
    # Owner rule: one subject line, no body or attribution trailer.
    subject = " ".join((args.message or "").splitlines()[:1]).strip()[:200]
    if not subject:
        print("refused: a one-line commit subject is required")
        return 2
    subprocess.run(["/usr/bin/git", "add", "--", *mine], cwd=str(repo), check=True)
    message = "[model-worker %s] %s" % (session[:8], subject)
    output = []
    code = _bounded(["python3", ".tiny-harness/run.py", "commit", "-m", message], repo, 1200,
                    output)
    cause, failing = (None, []) if code == 0 else classify_check_failure("".join(output))
    routes = ROUTE.findall("".join(output))
    route = "%s %s" % routes[-1] if routes else "not reached"
    if code != 0:  # leave the work in the tree, not half-staged in the shared index
        subprocess.run(["/usr/bin/git", "restore", "--staged", "--", *mine], cwd=str(repo))
    head = subprocess.run(["/usr/bin/git", "rev-parse", "--short", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()
    with home.edit() as state:
        model = state.setdefault("model", {})
        model.setdefault("commits", []).append({
            "session": session, "at": discovery.iso(), "exit": code, "head": head,
            "message": message, "files": mine, "candidate": candidate, "cause": cause,
            "failing": failing[:20], "route": route})
        entry = model.setdefault("repair", {}).setdefault(candidate, {"failures": 0})
        if code == 0:
            entry.update(failures=0, last="committed " + head)
        else:
            entry["failures"] += 1
            entry["last"] = "%s: %s" % (cause, ", ".join(failing[:5]) or "see output")
            if cause == "harness":
                for test in failing:
                    record = state.setdefault("harness_failures", {}).setdefault(
                        test, {"count": 0, "sessions": []})
                    record["count"] += 1
                    record["sessions"] = (record["sessions"] + [session[:8]])[-10:]
            if entry["failures"] >= REPAIR_BUDGET:
                item = model.setdefault("candidates", {}).setdefault(candidate, {
                    "rationale": "", "basis": "", "created_by": session, "history": []})
                item["status"] = "deferred"
                item["history"].append({"session": session, "at": discovery.iso(),
                                        "status": "deferred (repair budget exhausted)"})
        home.event(state, "model worker %s commit %s for %s: %s" % (
            session[:8], "ok %s (%s validation)" % (head, route) if code == 0 else "FAILED (%s, %d/%d: %s)" % (
                cause, entry["failures"], REPAIR_BUDGET, ", ".join(failing[:3])),
            candidate, message))
    return code


def verb_discard(home, repo, session, args):
    mine = [p for p in dirty(repo) if owned(p)]
    tracked = subprocess.run(["/usr/bin/git", "ls-files", "--", *mine], cwd=str(repo),
                             capture_output=True, text=True).stdout.split() if mine else []
    if tracked:
        subprocess.run(["/usr/bin/git", "restore", "--staged", "--worktree", "--source=HEAD",
                        "--", *tracked], cwd=str(repo), check=True)
    for path in set(mine) - set(tracked):
        (repo / path).unlink(missing_ok=True)
    print("discarded owned changes:", mine or "none")
    return 0


def verb_candidate(home, repo, session, args):
    from seren_harness import discovery
    if args.status not in ("proposed", "implemented", "committed", "executed", "deferred",
                           "rejected"):
        print("unknown status")
        return 2
    with home.edit() as state:
        model = state.setdefault("model", {})
        item = model.setdefault("candidates", {}).setdefault(args.id, {
            "rationale": args.rationale or "", "basis": args.basis or "",
            "created_by": session, "history": []})
        if args.rationale:
            item["rationale"] = args.rationale
        if args.basis:
            item["basis"] = args.basis
        item["status"] = args.status
        item["history"].append({"session": session, "at": discovery.iso(),
                                "status": args.status})
        home.event(state, "model candidate %s %s (session %s)" % (args.id, args.status,
                                                                 session[:8]))
    return 0


FOLLOWUP_STATUS = ("open", "in_progress", "done", "deferred")
FOLLOWUP_NEEDS = ("tier0", "live_authority", "owner_approval")
FOLLOWUP_FIELDS = ("problem", "consequence", "confidence", "uncertainty", "exposure",
                   "workaround", "next_step", "cost")


def followup_eligible(item):
    """A Tier 0 worker may pick it up: open or in progress, and its next step needs no
    authority beyond Tier 0. Live or owner-gated steps stay visible but are never picked."""
    return item.get("status") in ("open", "in_progress") and item.get("needs") == "tier0"


def followup_groups(state):
    items = (state.get("followups") or {}).values()
    return ([i for i in items if followup_eligible(i)],
            [i for i in items if i.get("status") in ("open", "in_progress")
             and i.get("needs") != "tier0"])


def verb_followup(home, repo, session, args):
    """Create or update a durable customer-value follow-up linked to findings."""
    from seren_harness import discovery
    if not args.id:
        print("--id required")
        return 2
    if args.status and args.status not in FOLLOWUP_STATUS:
        print("status must be one of", FOLLOWUP_STATUS)
        return 2
    if args.needs and args.needs not in FOLLOWUP_NEEDS:
        print("needs must be one of", FOLLOWUP_NEEDS)
        return 2
    with home.edit() as state:
        items = state.setdefault("followups", {})
        item = items.get(args.id)
        if item is None:
            missing = [f for f in ("problem", "consequence", "next_step") if
                       not getattr(args, f, None)]
            if missing or not args.needs:
                print("a new follow-up needs --problem, --consequence, --next-step and "
                      "--needs (missing: %s)" % ", ".join(missing + ([] if args.needs
                                                                     else ["needs"])))
                return 2
            item = items[args.id] = {"id": args.id, "findings": [], "status": "open",
                                     "created_by": session, "history": [],
                                     **{f: "unknown" for f in FOLLOWUP_FIELDS}}
        elif item["status"] == "done" and args.status in ("open", "in_progress") \
                and not args.evidence:
            print("refused: reopening completed work needs --evidence naming the new "
                  "evidence that justifies it")
            return 2
        for field in FOLLOWUP_FIELDS:
            value = getattr(args, field, None)
            if value:
                item[field] = value[:800]
        for key in args.finding or []:
            if key not in item["findings"]:
                item["findings"].append(key)
        if args.needs:
            item["needs"] = args.needs
        if args.status:
            item["status"] = args.status
        item["history"].append({"session": session, "at": discovery.iso(),
                                "status": item["status"],
                                "note": (args.evidence or args.text or "")[:500]})
        item["history"] = item["history"][-30:]
        home.event(state, "follow-up %s %s (needs %s; session %s)" % (
            args.id, item["status"], item.get("needs"), session[:8]))
    return 0


def verb_note(home, repo, session, args):
    from seren_harness import discovery
    with home.edit() as state:
        notes = state.setdefault("model", {}).setdefault("notes", [])
        notes.append({"session": session, "at": discovery.iso(), "text": args.text[:2000]})
        state["model"]["notes"] = notes[-200:]
    return 0


def verb_wait(home, repo, session, args):
    from seren_harness import discovery
    hours = max(1.0, min(float(args.hours), 168.0))
    until = discovery.now() + discovery.timedelta(hours=hours)
    with home.edit() as state:
        state.setdefault("model", {})["wait"] = {
            "until": discovery.iso(until), "reason": args.reason[:500], "session": session,
            "wake_on": getattr(args, "wake_on", "change"),
            "signals": discovery.model_wake_signals(home, state, home.authority())}
        home.event(state, "model worker %s requested a wait until %s: %s" % (
            session[:8], discovery.iso(until), args.reason[:200]))
    return 0


VERBS = {"status": verb_status, "test": verb_test, "validate": verb_validate,
         "run-case": verb_run_case, "synthetic-check": verb_synthetic_check, "commit": verb_commit, "discard": verb_discard,
         "candidate": verb_candidate, "note": verb_note, "wait": verb_wait,
         "followup": verb_followup}
