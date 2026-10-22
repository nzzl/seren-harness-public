"""Guided functional review: paste what you observed, answer a few questions, get a report.

Builds the evidence bundle and assessment the `review-workflow` command expects, then runs
the same review. Nothing is written until every answer is given, so cancelling leaves no
partial evidence. Checks read the pasted text in memory; saved copies are sanitized.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

from seren_harness.report import _write_private, finding_status
from seren_harness.sanitize import Sanitizer
from seren_harness.state import Store, private_directory
from seren_harness.models import LIVE
from seren_harness.workflow_review import candidate_findings, evaluate, review, summary_line

END = "."
# A Discord copy starts each message with the author, a line "APP" for an app, and the
# time line (" — 01:16" or " — Today at 01:16"). Only this explicit pattern splits a paste.
_HEADER = re.compile(r"^\s*—\s*\S")


class Cancelled(Exception):
    pass


class Prompter:
    def __init__(self, input_fn=input, out=sys.stdout):
        self.input_fn, self.out = input_fn, out

    def say(self, text=""):
        print(text, file=self.out)

    def ask(self, question, allowed=None, required=True):
        while True:
            try:
                answer = self.input_fn(question + " ").strip()
            except (EOFError, KeyboardInterrupt):
                raise Cancelled() from None
            if answer.lower() in ("cancel", "quit"):
                raise Cancelled()
            if allowed and answer.lower() not in allowed:
                self.say("  Please answer one of: " + ", ".join(allowed) + ".")
                continue
            if required and not answer:
                self.say("  An answer is needed (type 'cancel' to stop without saving).")
                continue
            return answer.lower() if allowed else answer

    def yes(self, question):
        return self.ask(question + " [yes/no]", ("yes", "no", "y", "n")) in ("yes", "y")

    def paste(self, label):
        """A pasted block ending with a line containing only '.', or '@path' for a file."""
        self.say(label + " Paste it, then a line with only '.' to finish "
                 "(or type @/path/to/file to import a text file).")
        lines = []
        while True:
            try:
                line = self.input_fn("")
            except (EOFError, KeyboardInterrupt):
                raise Cancelled() from None
            if not lines and line.strip().startswith("@"):
                path = Path(line.strip()[1:]).expanduser()
                try:
                    return path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    self.say("  That file could not be read as UTF-8 text; try again.")
                    continue
            if line.strip() == END:
                text = "\n".join(lines)
                if text.strip():
                    return text + "\n"
                self.say("  Nothing was pasted; paste the message or type 'cancel'.")
                lines = []
                continue
            if not lines and line.strip().lower() in ("cancel", "quit"):
                raise Cancelled()
            lines.append(line)


def discord_messages(text):
    """Split only at explicit Discord app headers; otherwise return the text unchanged."""
    lines = text.split("\n")
    starts = [i - 1 for i in range(1, len(lines) - 1)
              if lines[i].strip() == "APP" and _HEADER.match(lines[i + 1]) and i >= 1]
    if len(starts) < 2 or starts[0] != 0:
        return [text]
    bounds = starts + [len(lines)]
    parts = ["\n".join(lines[a:b]).rstrip("\n") + "\n" for a, b in zip(bounds, bounds[1:])]
    return [p for p in parts if p.strip()]


def workflow_cases(root):
    cases = []
    for path in sorted((Path(root) / "cases").glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if "criteria" in case and "scope" in case:
            cases.append((path, case))
    return cases


def choose_case(p, root, requested=None):
    # The guide collects pasted Discord messages; service-call cases are collected by `collect`.
    cases = [(path, case) for path, case in workflow_cases(root)
             if case.get("interface", "discord") == "discord"]
    if not cases:
        raise ValueError("no workflow cases found under cases/")
    if requested:
        for path, case in cases:
            if requested in (case["id"], path.name, path.stem):
                return path, case
        raise ValueError("unknown case; run without --case to choose from the list")
    p.say("Cases:")
    for n, (path, case) in enumerate(cases, 1):
        p.say("  " + str(n) + ". " + case["id"] + " (v" + str(case["version"]) + ") — "
              + case["command"])
    choice = p.ask("Case number:", [str(n) for n in range(1, len(cases) + 1)])
    return cases[int(choice) - 1]


def collect(p, case):
    """Return bundle items (with raw text) from the operator's pastes, in order."""
    p.say("")
    p.say("Scope: " + case["scope"]["statement"])
    p.say("Collect: run " + case["command"] + " exactly as written, then follow the "
          "workflow (for example, choose the button that opens the full content). "
          "Copy every message in order; private messages stay private.")
    p.say("")
    texts = []
    while True:
        text = p.paste("Message " + str(len(texts) + 1) + ("" if texts else
                       " is the first reply to the command."))
        parts = discord_messages(text)
        if len(parts) > 1 and p.yes("This paste contains " + str(len(parts)) + " Discord "
                                    "messages (each starts with a name, 'APP' and a time). "
                                    "Keep them as " + str(len(parts)) + " separate messages?"):
            texts.extend(parts)
        elif len(parts) > 1:
            p.say("  Then paste each message separately, starting again from this one.")
            continue
        else:
            texts.append(text)
        if not p.yes("Add another message or file?"):
            break
    items = []
    invocation = case["command"]
    if not p.yes("Was message 1 the reply to " + case["command"] + "?"):
        invocation = p.ask("What did you type instead?")
    items.append({"id": "response-1", "kind": "discord_response", "role": "initial",
                  "invocation": invocation, "text": texts[0]})
    if len(texts) > 1:
        same = len(texts) > 2 and p.yes("Did messages 2–" + str(len(texts)) + " all come "
                                        "from clicking the same button?")
        label = p.ask("Button label (as shown):") if same else None
        for n, text in enumerate(texts[1:], 2):
            if same:
                kind, role, how = "button_response", "complete_view", "button: " + label
            else:
                choice = p.ask("Message " + str(n) + " came from: 1 a button you clicked, "
                               "2 another command you typed, 3 an attached file. Number:",
                               ("1", "2", "3"))
                if choice == "1":
                    kind, role = "button_response", "complete_view"
                    how = "button: " + p.ask("Button label (as shown):")
                elif choice == "2":
                    kind, role = "discord_response", "continuation"
                    how = p.ask("Command you typed:")
                else:
                    kind, role, how = "attachment", "complete_view", "attachment"
            items.append({"id": ("attachment-" if kind == "attachment" else "response-")
                          + str(n), "kind": kind, "role": role, "invocation": how,
                          "text": text})
    return items


def choose_retest(p, store, case):
    # Guided copies are operator-supplied (live); aliases and simulated findings are not offered.
    open_ = [(store.finding(key), finding_status(store.retests(key)))
             for key in candidate_findings(store, case, LIVE, open_only=False)]
    if not open_ or not p.yes("Is this a retest of an earlier finding for this case?"):
        return None
    for n, (f, status) in enumerate(open_, 1):
        p.say("  " + str(n) + ". " + f["key"] + " [" + status + "] " + f["title"])
    choice = p.ask("Finding number:", [str(n) for n in range(1, len(open_) + 1)])
    return open_[int(choice) - 1][0]["key"]


def assess(p, case, report, items):
    """Ask for each human criterion; nothing is preselected or invented."""
    criteria = [c for c in case["criteria"] if c["check"] == "operator"]
    if not criteria:
        return None
    clean = [Sanitizer().text(i["text"]) for i in items]
    p.say("")
    p.say("Automated checks on what you pasted:")
    for c in report["criteria"]:
        if c["category"] != "human_assessment":
            p.say("  " + c["verdict"] + " — " + c["claim"])
    assessment = {"bundle_sha256": report["bundle_sha256"],
                  "case_digest": report["case"]["digest"], "assessor": "", "checks": {}}
    for c in criteria:
        p.say("")
        p.say("Your judgment is needed: " + c["claim"])
        if c.get("expected"):
            p.say("Look for: " + c["expected"])
        decision = p.ask("Decision: pass, fail, or skip if you cannot assess:",
                         ("pass", "fail", "skip"))
        if decision == "skip":
            assessment["checks"][c["id"]] = {"verdict": "unverified", "excerpt": "", "note": ""}
            continue
        note = p.ask("In your own words, why?")
        while True:
            excerpt = p.ask("Paste a short exact phrase from the messages that supports this"
                            + (":" if decision == "pass" else " (or press Enter for none):"),
                            required=decision == "pass")
            if not excerpt or any(excerpt in t for t in clean):
                break
            p.say("  That phrase is not in the messages exactly as saved; copy it again.")
        assessment["checks"][c["id"]] = {"verdict": "passed" if decision == "pass" else
                                         "failed", "excerpt": excerpt, "note": note}
    if any(v["verdict"] != "unverified" for v in assessment["checks"].values()):
        assessment["assessor"] = p.ask("Your assessor label (a name or role):")
    return assessment


def _new_folder(base):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder, n = base / stamp, 1
    while folder.exists():
        n += 1
        folder = base / (stamp + "-" + str(n))
    return folder


def run(args, input_fn=input, out=sys.stdout):
    p = Prompter(input_fn, out)
    home = Path(args.home).expanduser()
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else home / "findings"
    try:
        case_path, case = choose_case(p, Path(args.root), args.case)
        items = collect(p, case)
        captured = p.yes("Did you copy everything the workflow showed (every message or "
                         "file you reached)?")
        revision = p.ask("Revision or build, if you know it (Enter if unknown):",
                         required=False) or None
        observed = p.ask("When you observed it, as displayed (Enter if unknown):",
                         required=False) or None
        notes = p.ask("Anything else to note (Enter to skip):", required=False) or None
        bundle = {"provenance": "operator_supplied", "revision": revision,
                  "environment": "operator-copied output, entered with the guided review",
                  "observed": observed, "notes": notes, "complete_reading_captured": captured,
                  "items": [{k: v for k, v in i.items() if k != "text"} for i in items]}
        store = Store(state_dir)
        try:
            retest = choose_retest(p, store, case)
        finally:
            store.close()
        first, _ = evaluate(case, bundle, items)
        assessment = assess(p, case, first, items)
    except Cancelled:
        p.say("")
        p.say("Cancelled. Nothing was saved.")
        return 130
    except (OSError, UnicodeError, ValueError, KeyError):
        p.say("Cannot continue: the case or finding store could not be read.")
        return 5
    folder = _new_folder(private_directory(private_directory(home) / case["id"]))
    try:
        private_directory(folder / "evidence")
        sanitizer = Sanitizer()
        for item in items:
            item["path"] = "evidence/" + item["id"] + ".txt"
            _write_private(folder / item["path"], sanitizer.text(item["text"]))
        saved = dict(bundle, items=[{k: v for k, v in i.items() if k != "text"}
                                    for i in items],
                     notes=((notes + " ") if notes else "") + "Evidence files are sanitized "
                     "copies; the review ran on the pasted text in memory.")
        _write_private(folder / "bundle.json", json.dumps(saved, indent=2) + "\n")
        if assessment:
            _write_private(folder / "assessment.json", json.dumps(assessment, indent=2) + "\n")
        report = review(case, bundle, items, state_dir, folder / "report", assessment, retest)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        p.say("The review could not be completed; the pasted evidence is kept in " + str(folder)
              + " for inspection. No verdict was recorded.")
        return 5
    p.say("")
    p.say(summary_line(report) + ".")
    if report["verdict"] != "passed" and report["verdict_reasons"]:
        p.say("Not passed because: " + "; ".join(report["verdict_reasons"]) + ".")
    p.say("Report: " + str(folder / "report" / "report.md"))
    # Paths come from the store location, not the sanitized report, which may redact them.
    for key in report["findings"]:
        p.say("Private draft for " + key + ": " + str(state_dir / "drafts" / (key + ".md")))
    if report["retest"]:
        p.say("Draft with retest history: " + str(state_dir / "drafts"
                                                  / (report["retest"]["finding"] + ".md")))
    p.say("A pass covers only this workflow and case version; it never certifies the employee.")
    return {"passed": 0, "failed": 1}.get(report["verdict"], 2)
