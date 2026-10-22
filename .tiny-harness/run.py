#!/usr/bin/env python3
"""Tiny Harness: validate the staged tree, commit it, or install without overwrites."""
import argparse
import ast
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading


TASK = """# Task

Status: ready

## Outcome and scope
Replace with the authorized outcome and boundaries before implementation.
For multi-session work, reference the existing backlog or feature checklist here;
preserve unfinished requirements when replacing this task.

## Acceptance criteria
- [ ] Replace with observable success criteria.

## Checkpoint
Record existing changes, decisions, checks and results, failures, and next action.
For verification, include the tested revision/environment and evidence location.
For pending external steps, record the action and where its result can be checked.
On resumption, reconcile these notes with Git and external results before acting.
Update before a handoff or interruption and at meaningful milestones.
"""


class Failure(Exception):
    pass


class Interrupted(BaseException):
    pass


def interrupt(signum, _frame):
    raise Interrupted(signum)


def git(root, *args, env=None):
    result = subprocess.run(["git", "-C", str(root), *args], env=env,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise Failure(result.stderr.strip() or "Git command failed: " + " ".join(args))
    return result.stdout.strip()


def repository(path):
    return Path(git(path, "rev-parse", "--show-toplevel")).resolve()


def head(root):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                            text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key: " + repr(key))
        result[key] = value
    return result


def configuration(snapshot):
    path = snapshot / ".tiny-harness/checks.json"
    if path.parent.is_symlink() or path.is_symlink():
        raise Failure("Staged check configuration and its directory must not be symlinks.")
    try:
        config = json.loads(path.read_text(), object_pairs_hook=unique_object)
    except (OSError, ValueError) as error:
        raise Failure("Required staged configuration is missing or invalid: " + str(error))
    if not isinstance(config, dict) or "checks" not in config or not set(config) <= {
            "checks", "focused", "prose"}:
        raise Failure("Configuration must contain only 'checks' (and optionally 'focused', 'prose').")
    checks = config["checks"]
    if not isinstance(checks, list) or not checks:
        raise Failure("Configure at least one required check; empty configuration is not success.")
    names = set()
    validate_checks(checks, names)
    return checks, focused_policy(config.get("focused"), checks, names), prose_policy(config.get("prose"))


# Never narrative, whatever a configuration says: the runner, its rules and contract.
NEVER_PROSE = (".tiny-harness/run.py", ".tiny-harness/checks.json", ".tiny-harness/CONTRACT.md",
               ".tiny-harness/LICENSE")


def prose_policy(value):
    """Optional route for exact, reviewed narrative files: when every staged change is an
    in-place text edit of one of them, no checks run. Exact paths only, never patterns."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"paths"}:
        raise Failure("'prose' needs exactly paths.")
    paths = value["paths"]
    if (not isinstance(paths, list) or not paths or len(set(paths)) != len(paths) or any(
            not isinstance(p, str) or not p or p.startswith("/") or ".." in p.split("/")
            or any(c in p for c in "*?[]\\") or p in NEVER_PROSE or p.startswith(".git/")
            for p in paths)):
        raise Failure("Prose paths must be distinct exact relative file paths, not patterns, "
                      "and never the runner, its checks or contract.")
    return {"paths": paths}


def focused_policy(value, checks, names):
    """Optional narrow route: when every staged change matches `paths`, commit may run the
    full checks named in `keep` plus `checks`. Selection itself is in select()."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"paths", "keep", "checks"}:
        raise Failure("'focused' needs exactly paths, keep, and checks.")
    paths, keep = value["paths"], value["keep"]
    if not isinstance(paths, list) or not paths or any(
            not isinstance(p, str) or not p or p.startswith(("/", ".tiny-harness"))
            or ".." in p.split("/") or "**" in p for p in paths):
        raise Failure("Focused paths must be nonempty relative patterns outside .tiny-harness, without '..' or '**'.")
    if not isinstance(keep, list) or any(k not in names for k in keep) or len(set(keep)) != len(keep):
        raise Failure("Focused 'keep' must name distinct configured checks.")
    extra = value["checks"]
    if not isinstance(extra, list):
        raise Failure("Focused 'checks' must be a list.")
    validate_checks(extra, set(names))
    selected = [c for c in checks if c["name"] in keep] + extra
    if not selected:
        raise Failure("A focused route must select at least one check.")
    return {"paths": paths, "checks": selected}


def validate_checks(checks, names):
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"name", "argv", "timeout_seconds"}:
            raise Failure("Each check needs name, argv, and timeout_seconds (no unknown keys).")
        name, argv, timeout = check["name"], check["argv"], check["timeout_seconds"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise Failure("Check names must be nonempty and unique.")
        names.add(name)
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) for a in argv) or not argv[0]:
            raise Failure("Check argv must be a nonempty list of strings with an executable.")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise Failure("Check timeout_seconds must be a finite positive number.")


def matches(path, pattern):
    """Component-wise glob: '*' never crosses '/', and the depth must be equal."""
    parts, wanted = path.split("/"), pattern.split("/")
    return len(parts) == len(wanted) and all(fnmatch.fnmatchcase(p, w) for p, w in zip(parts, wanted))


def full_record_path(root):
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else root / common) / "tiny-harness-full-validation.json"


def last_full(root):
    try:
        record = json.loads(full_record_path(root).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def record_full(root, tree, previous_head, passed, log=None, inherited=None):
    path = full_record_path(root)
    temporary = path.with_name(path.name + ".tmp")
    record = {"tree": tree, "head": previous_head, "result": "pass" if passed else "fail",
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if inherited is not None:
        record.update(inherited)
    if log is not None:
        record["log"] = str(log.path)
    temporary.write_text(json.dumps(record) + "\n")
    os.replace(temporary, path)


def prose_route(root, tree, previous_head, prose):
    """(paths, None) when every change is an in-place UTF-8 text edit of an allowlisted
    narrative file with an unchanged regular-file mode; otherwise (None, reason).
    Additions, deletions, renames, type or mode changes are never eligible."""
    raw = git(root, "diff-tree", "-r", "-z", "--no-renames", "--raw", previous_head, tree)
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if not fields or len(fields) % 2:
        return None, "no changes against HEAD" if not fields else "unparseable change list"
    paths = []
    for meta, path in zip(fields[0::2], fields[1::2]):
        old_mode, new_mode, _, new_blob, status = meta.lstrip(":").split(" ")
        if status != "M" or old_mode != "100644" or new_mode != "100644":
            return None, path + " is not an in-place regular-file edit"
        if path not in prose["paths"]:
            return None, path + " is not an allowlisted narrative file"
        data = subprocess.run(["git", "-C", str(root), "cat-file", "blob", new_blob],
                              capture_output=True).stdout
        try:
            if "\0" in data.decode("utf-8"):
                raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL")
        except UnicodeDecodeError:
            return None, path + " is not plain UTF-8 text"
        paths.append(path)
    return paths, None


# Fixed here, never configurable by checks.json: the only file whose in-place edits may
# inherit the parent tree's full-suite pass. No instruction or prompt may refer to it.
NARRATIVE = ".tiny-harness/NARRATIVE.md"
NARRATIVE_HEADER = "<!-- narrative only: nothing in this file is an instruction -->"
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".tiny-harness/CONTRACT.md",
                     "docs/MODEL-WORKER.md", "docs/AUTONOMOUS-DISCOVERY.md")
PROMPT_FILES = ("seren_harness/model_worker.py", "seren_harness/maintenance.py")


def blob_text(root, tree, path):
    """Text of path in tree; None when absent; raises ValueError when not plain UTF-8."""
    entry = git(root, "ls-tree", tree, "--", path).split()
    if not entry:
        return None
    if entry[0] not in ("100644", "100755") or entry[1] != "blob":
        raise ValueError(path + " is not a regular file")
    data = subprocess.run(["git", "-C", str(root), "cat-file", "blob", entry[2]],
                          capture_output=True, check=True).stdout
    text = data.decode("utf-8")
    if "\0" in text:
        raise ValueError(path + " contains NUL")
    return text


def prompt_constants(source):
    """String values of module-level PROMPT assignments; ValueError when none is found."""
    values = [node.value.value for node in ast.parse(source).body
              if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
              and isinstance(node.value.value, str)
              and any(isinstance(t, ast.Name) and t.id == "PROMPT" for t in node.targets)]
    if not values:
        raise ValueError("no PROMPT constant")
    return values


def narrative_inheritance(root, tree, previous_head):
    """(record fields, None) when the staged tree may inherit the parent's full-suite pass:
    the parent tree has a recorded full pass with its log, the only change is an in-place
    UTF-8 edit of NARRATIVE starting with NARRATIVE_HEADER, and no instruction file or
    prompt in the tree refers to it. Otherwise (None, reason); any doubt means no."""
    try:
        parent_tree = git(root, "rev-parse", previous_head + "^{tree}")
        record = last_full(root)
        if (record is None or record.get("result") != "pass" or record.get("tree") != parent_tree
                or record.get("basis", "full") != "full"):
            return None, "parent tree has no recorded full-suite pass"
        if not isinstance(record.get("log"), str) or not Path(record["log"]).is_file():
            return None, "parent full-suite log is missing"
        raw = git(root, "diff-tree", "-r", "-z", "--no-renames", "--raw", previous_head, tree)
        fields = [f for f in raw.split("\0") if f]
        if len(fields) != 2 or fields[1] != NARRATIVE:
            return None, "the change is not exactly one edit of " + NARRATIVE
        old_mode, new_mode, _, _, status = fields[0].lstrip(":").split(" ")
        if status != "M" or old_mode != "100644" or new_mode != "100644":
            return None, NARRATIVE + " is not an in-place regular-file edit"
        text = blob_text(root, tree, NARRATIVE)
        if text is None or text.split("\n", 1)[0] != NARRATIVE_HEADER:
            return None, NARRATIVE + " lacks its exact narrative-only header"
        name = Path(NARRATIVE).name.lower()
        for path in INSTRUCTION_FILES + PROMPT_FILES:
            text = blob_text(root, tree, path)
            if text is None:
                continue
            texts = prompt_constants(text) if path in PROMPT_FILES else [text]
            if any(name in t.lower() for t in texts):
                return None, path + " refers to " + NARRATIVE
        diff = subprocess.run(["git", "-C", str(root), "diff-tree", "-p", "--binary", "--no-renames",
                               previous_head, tree], capture_output=True, check=True).stdout
    except (Failure, ValueError, SyntaxError, subprocess.CalledProcessError):
        return None, "narrative eligibility could not be established"
    return {"basis": "inherited", "parent_tree": parent_tree, "parent_log": record["log"],
            "diff_sha256": hashlib.sha256(diff).hexdigest()}, None


def select(root, tree, previous_head, focused, prose=None):
    """Return (route, reason, changed paths) for exactly this staged tree against HEAD.
    Anything not positively established as eligible takes the full route."""
    if previous_head is not None and not (
            root / git(root, "rev-parse", "--git-path", "MERGE_HEAD")).is_file():
        previous = git(root, "ls-tree", previous_head, "--", ".tiny-harness/run.py").split()
        if previous and git(root, "hash-object", str(Path(__file__).resolve())) == previous[2]:
            inherited, _ = narrative_inheritance(root, tree, previous_head)
            if inherited:
                return "inherited", "the only change is a verified narrative-only edit", [NARRATIVE]
    if focused is None and prose is None:
        return "full", "no focused route configured", []
    if prose is not None and previous_head is not None and not (
            root / git(root, "rev-parse", "--git-path", "MERGE_HEAD")).is_file():
        previous = git(root, "ls-tree", previous_head, "--", ".tiny-harness/run.py").split()
        if previous and git(root, "hash-object", str(Path(__file__).resolve())) == previous[2]:
            paths, _ = prose_route(root, tree, previous_head, prose)
            if paths:
                return "prose", "every staged change is an allowlisted narrative edit", paths
    if focused is None:
        return "full", "no focused route configured", []
    if previous_head is None:
        return "full", "no HEAD baseline", []
    if (root / git(root, "rev-parse", "--git-path", "MERGE_HEAD")).is_file():
        return "full", "merge in progress", []
    previous = git(root, "ls-tree", previous_head, "--", ".tiny-harness/run.py").split()
    if not previous or git(root, "hash-object", str(Path(__file__).resolve())) != previous[2]:
        return "full", "executing runner differs from HEAD", []
    record = last_full(root)
    if record is None:
        return "full", "no recorded full-suite result", []
    if record.get("result") != "pass":
        return "full", ("last full-suite validation failed (tree " + str(record.get("tree"))
                        + "); only a full pass clears it"), []
    raw = git(root, "diff-tree", "-r", "-z", "--no-renames", "--raw", previous_head, tree)
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if not fields or len(fields) % 2:
        return "full", "no changes against HEAD" if not fields else "unparseable change list", []
    paths = []
    for meta, path in zip(fields[0::2], fields[1::2]):
        old_mode, new_mode, _, _, status = meta.lstrip(":").split(" ")
        if status not in ("A", "M"):
            return "full", "change " + status + " to " + path + " is not eligible", []
        if new_mode != "100644" or (status == "M" and old_mode != "100644"):
            return "full", "mode " + old_mode + "->" + new_mode + " for " + path + " is not eligible", []
        if path.startswith(".tiny-harness/") or not any(matches(path, p) for p in focused["paths"]):
            return "full", path + " is outside the focused allowlist", []
        paths.append(path)
    return "focused", "every staged change is in the focused allowlist", paths


LOG_KEEP = 200
MARKERS = (b"FAIL:", b"ERROR:", b"UNEXPECTED SUCCESS:", b"Failing tests", b"Rerun one")


class Log:
    """Complete validation output on disk, mirrored to the terminal while it streams.

    Lives beside the full-validation record in the Git common directory: never in the
    staged snapshot or a commit. A closed terminal pipe (`| head`) stops only the mirror;
    checks, the log and the exit status are unaffected.
    """

    def __init__(self, root, tree):
        directory = full_record_path(root).with_name("tiny-harness-logs")
        directory.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.path = directory / (stamp + "-" + tree[:12] + ".log")
        self.file = self.path.open("ab")
        self.terminal = True
        self.lock = threading.Lock()
        for old in sorted(directory.glob("*.log"))[:-LOG_KEEP]:
            old.unlink(missing_ok=True)

    def write(self, data, stream=None):
        stream = stream or sys.stdout
        with self.lock:
            self.file.write(data)
            self.file.flush()
            if self.terminal:
                try:
                    stream.buffer.write(data)
                    stream.buffer.flush()
                except (BrokenPipeError, ValueError, OSError):
                    self.terminal = False

    def say(self, text):
        self.write((text + "\n").encode())

    def close(self):
        self.file.close()


def say(log, text):
    if log is None:
        print(text, flush=True)
    else:
        log.say(text)


def check_prefix():
    """An optional confinement command (JSON list) the caller puts before every check, e.g.
    sandbox-exec. It only wraps a check; it cannot change which checks run or their result."""
    raw = os.environ.get("TINY_HARNESS_CHECK_PREFIX")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise Failure("TINY_HARNESS_CHECK_PREFIX must be a JSON list of strings.")
    return value


def run_check(check, snapshot, log=None):
    prefix = check_prefix()
    say(log, "RUN " + check["name"] + ": " + json.dumps(check["argv"])
        + (" (confined by " + json.dumps(prefix) + ")" if prefix else ""))
    # The prefix confines this check only. Its children inherit that confinement, and a
    # nested runner (tests drive run.py) must not re-apply it: macOS refuses nested sandboxes.
    env = {k: v for k, v in os.environ.items() if k != "TINY_HARNESS_CHECK_PREFIX"}
    try:
        process = subprocess.Popen(prefix + check["argv"], cwd=snapshot, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   env=dict(env, PYTHONUNBUFFERED="1"))
    except OSError as error:
        say(log, "FAIL " + check["name"] + ": " + str(error))
        return False
    found = []

    def copy(pipe, stream):
        # Stream in chunks: nothing is buffered beyond one partial line for markers.
        # Each stream keeps its terminal destination; both go to the one log.
        partial = b""
        for chunk in iter(lambda: os.read(pipe.fileno(), 65536), b""):
            if log is None:
                stream.buffer.write(chunk)
                stream.buffer.flush()
            else:
                log.write(chunk, stream)
            lines = (partial + chunk).split(b"\n")
            partial = lines.pop()[-4096:]
            found.extend(line[:300] for line in lines
                         if line.startswith(MARKERS) and len(found) < 40)

    readers = [threading.Thread(target=copy, args=(pipe, stream), daemon=True)
               for pipe, stream in ((process.stdout, sys.stdout), (process.stderr, sys.stderr))]
    for reader in readers:
        reader.start()
    code = None
    try:
        code = process.wait(timeout=check["timeout_seconds"])
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Stop same-group children even after their parent exits successfully.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for reader in readers:
            reader.join(5)
        process.stdout.close()
        process.stderr.close()
    if code is None:
        say(log, "FAIL " + check["name"] + ": timed out")
    else:
        say(log, ("PASS " if code == 0 else "FAIL ") + check["name"] + " (exit " + str(code) + ")")
    if code != 0 and found:
        say(log, "Failures reported by " + check["name"] + ":")
        for line in found:
            say(log, "  " + line.decode(errors="replace"))
    return code == 0


def unchanged(root, tree, previous_head):
    if git(root, "write-tree") != tree or head(root) != previous_head:
        raise Failure("Index or HEAD changed during validation; inspect changes and run again.")


def snapshot_signature(path):
    """Record content, file kind, and executable bit without following symlinks."""
    try:
        mode = path.lstat().st_mode
        content = None
        if stat.S_ISLNK(mode):
            content = os.readlink(path)
        elif stat.S_ISREG(mode):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            content = digest.digest()
        return stat.S_IFMT(mode), mode & stat.S_IXUSR, content
    except OSError:
        return None


def snapshot_baseline(snapshot):
    # At checkout time these are staged paths and their parent directories only.
    # Retaining this list lets checks add untracked build output afterward.
    baseline = {}
    for path in [snapshot, *snapshot.rglob("*")]:
        signature = snapshot_signature(path)
        if signature is None:
            raise Failure("Cannot inspect staged snapshot path: " + str(path.relative_to(snapshot)))
        baseline[path] = signature
    return baseline


def snapshot_unchanged(snapshot, baseline, check):
    for path, signature in baseline.items():
        if snapshot_signature(path) != signature:
            raise Failure("Check " + repr(check["name"]) + " modified staged snapshot path "
                          + repr(str(path.relative_to(snapshot))) + "; no commit was made. "
                          "Run repairs before staging, then validate again.")


def validate(root, *, fail_fast=False, allow_focused=False):
    tree, previous_head = git(root, "write-tree"), head(root)
    log = Log(root, tree)
    print("Validation log: " + str(log.path), flush=True)
    log.file.write(("tiny-harness validation log\ntree " + tree + "\nHEAD " + str(previous_head)
                    + "\nstarted " + datetime.now(timezone.utc).isoformat(timespec="seconds")
                    + "\n").encode())
    outcome = "error"
    try:
        result = _validate(root, tree, previous_head, log, fail_fast, allow_focused)
        outcome = "pass"
        return result
    except Failure:
        outcome = "fail"
        raise
    except (KeyboardInterrupt, Interrupted):
        outcome = "interrupted"
        raise
    finally:
        log.file.write(("RESULT " + outcome + " for staged tree " + tree + " at "
                        + datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n").encode())
        log.close()
        try:
            print("Validation log (" + outcome + "): " + str(log.path), flush=True)
        except (BrokenPipeError, ValueError, OSError):
            pass


def _validate(root, tree, previous_head, log, fail_fast, allow_focused):
    log.say("Validating staged tree " + tree + " against HEAD " + str(previous_head))
    with tempfile.TemporaryDirectory(prefix="tiny-harness-") as directory:
        temporary = Path(directory)
        snapshot = temporary / "snapshot"
        snapshot.mkdir()
        env = dict(os.environ, GIT_INDEX_FILE=str(temporary / "index"))
        git(root, "read-tree", tree, env=env)
        entries = git(root, "ls-files", "--stage", env=env)
        if any(line.startswith("160000 ") for line in entries.splitlines()):
            raise Failure("Submodules are unsupported; required contents cannot be silently omitted.")
        git(root, "checkout-index", "--all", "--prefix=" + str(snapshot) + os.sep, env=env)
        baseline = snapshot_baseline(snapshot)
        checks, focused, prose = configuration(snapshot)
        route, reason, paths = "full", "explicit full validation", []
        if allow_focused:
            route, reason, paths = select(root, tree, previous_head, focused, prose)
        skipped, inherited = [], None
        if route == "inherited":
            inherited, why = narrative_inheritance(root, tree, previous_head)
            if inherited is None:
                route, reason, paths = "full", why, []
        if route == "focused" and focused:
            checks = focused["checks"]
        if route in ("prose", "inherited"):
            skipped, checks = [c["name"] for c in checks], []
        log.say("ROUTE " + route + " for staged tree " + tree + ": " + reason)
        if paths:
            log.say("Changed paths: " + json.dumps(paths))
        log.say("Selected checks: " + json.dumps([c["name"] for c in checks]))
        if skipped:
            log.say("SKIPPED checks (" + ("prose-only" if route == "prose" else "inherited narrative")
                    + " route; code tests did not run): " + json.dumps(skipped))
        results = []
        for check in checks:
            results.append(run_check(check, snapshot, log))
            snapshot_unchanged(snapshot, baseline, check)
            if fail_fast and not results[-1]:
                log.say("Stopping after the first failed check; run validate for all check results.")
                break
        unchanged(root, tree, previous_head)
        if route == "full":
            record_full(root, tree, previous_head, all(results), log)
        elif route == "inherited":
            record_full(root, tree, previous_head, True, log, inherited)
        if not all(results):
            raise Failure("Required validation failed; no commit was made.")
    if route == "prose":
        log.say("PASS prose-only route for staged tree " + tree + ": no checks ran. This is not "
                "full-suite validation, records no full-suite result and cannot authorize live work.")
    elif route == "inherited":
        log.say("PASS inherited narrative route for staged tree " + tree + ": no checks ran; the full-suite "
                "record inherits parent tree " + inherited["parent_tree"] + " (log " + inherited["parent_log"]
                + ", diff sha256 " + inherited["diff_sha256"] + ").")
    elif route == "focused":
        log.say("PASS focused validation (" + str(len(checks)) + " selected checks) for staged tree " + tree
                + "; the full suite did not run.")
    else:
        log.say("PASS all " + str(len(checks)) + " required checks for staged tree " + tree)
    return tree, previous_head


def warn_harness_changes(root):
    if head(root) is None:
        return
    paths = (".tiny-harness/checks.json", ".tiny-harness/run.py")
    changed = git(root, "diff", "--cached", "--name-only", "HEAD", "--", *paths).splitlines()
    previous = git(root, "ls-tree", "HEAD", "--", paths[1]).split()
    if previous and git(root, "hash-object", str(Path(__file__).resolve())) != previous[2]:
        changed.append("executing runner differs from HEAD")
    if changed:
        print("WARNING: validation rules or runner changed: " + "; ".join(changed)
              + ". Review these changes; passing checks do not prove standards were preserved.", flush=True)


def commit(root, message, *, full=False):
    if not message.strip():
        raise Failure("A meaningful commit message is required.")
    staged = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--quiet"],
                            text=True, capture_output=True)
    if staged.returncode not in (0, 1):
        raise Failure(staged.stderr.strip() or "Cannot inspect staged changes.")
    # A pending merge can record meaningful ancestry without changing the tree.
    if staged.returncode == 0 and not (root / git(root, "rev-parse", "--git-path", "MERGE_HEAD")).is_file():
        raise Failure("Nothing staged to commit; stage intended changes before committing.")
    warn_harness_changes(root)
    tree, previous_head = validate(root, fail_fast=True, allow_focused=not full)
    unchanged(root, tree, previous_head)
    result = subprocess.run(["git", "-C", str(root), "commit", "-m", message])
    if result.returncode:
        raise Failure("Git commit failed (including any existing hooks); diagnose and retry without bypassing checks.")
    if git(root, "rev-parse", "HEAD^{tree}") != tree:
        raise Failure("Commit tree differs from the validated tree (possibly a modifying hook). "
                      "The commit EXISTS; inspect it and correct with a new validated commit. Do not rewrite history.")
    print("Committed validated tree: " + git(root, "rev-parse", "HEAD"), flush=True)


def install(path):
    root = repository(path)
    if root != path.resolve():
        raise Failure("Install target must be the repository root.")
    target = root / ".tiny-harness"
    if os.path.lexists(target):
        raise Failure("Refusing to replace existing .tiny-harness; review and merge updates manually.")
    source = Path(__file__).resolve().parent
    # Prepare completely before adding the directory; no existing project files are edited.
    with tempfile.TemporaryDirectory(prefix="tiny-harness-install-") as directory:
        staged = Path(directory) / ".tiny-harness"
        staged.mkdir()
        for name in ("run.py", "CONTRACT.md", "LICENSE"):
            shutil.copyfile(source / name, staged / name)
        (staged / "TASK.md").write_text(TASK)
        (staged / "checks.json").write_text('{"checks": []}\n')
        shutil.copytree(staged, target)
    print("Installed " + str(target), flush=True)
    print("Configure .tiny-harness/checks.json and TASK.md, then stage them deliberately.\n"
          "Load .tiny-harness/CONTRACT.md in the agent, or add a reference to existing instructions.\n"
          "No instructions, hooks, Git settings, or project configuration were replaced.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validating = commands.add_parser("validate", help="run required checks on a fresh copy of the index")
    validating.add_argument("--select", action="store_true",
                            help="apply the same route selection as commit (default: full)")
    committing = commands.add_parser("commit", help="validate, then commit using existing Git identity and hooks")
    committing.add_argument("-m", "--message", required=True)
    committing.add_argument("--full", action="store_true", help="never take the focused route")
    installing = commands.add_parser("install", help="add the harness to an existing Git repository")
    installing.add_argument("target", type=Path)
    args = parser.parse_args()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)
    try:
        if args.command == "install":
            install(args.target)
        else:
            root = repository(Path.cwd())
            if args.command == "validate":
                validate(root, allow_focused=args.select)
            else:
                commit(root, args.message, full=args.full)
    except (Failure, OSError) as error:
        print("ERROR: " + str(error), file=sys.stderr, flush=True)
        return 1
    except (KeyboardInterrupt, Interrupted) as error:
        print("ERROR: interrupted; validation is incomplete. Inspect task and Git state before resuming.", file=sys.stderr)
        return 128 + error.args[0] if isinstance(error, Interrupted) else 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
