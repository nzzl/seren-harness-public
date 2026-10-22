#!/usr/bin/env python3
"""SYNTHETIC scripted stand-in for `claude -p --output-format json`, for offline tests.

Its replies are reproducible scripts, not a model: they test the campaign's orchestration and
safety handling, never the quality of a real model's choices. FUNCTIONAL_FAKE_MODEL names a
directory with script.json {"planner": [...], "judge": [...]}; each entry is
{"text": str, "cost": number | null}. Prompts and argv are logged to calls.jsonl there.
"""
import json
import os
from pathlib import Path
import sys

HOME = Path(os.environ["FUNCTIONAL_FAKE_MODEL"])
LOCAL = json.loads((HOME / "local.json").read_text()) if (HOME / "local.json").exists() else {}
FLAGS = ["--output-format", "--tools", "--max-turns", "--max-budget-usd", "--model",
         "--no-session-persistence", "--strict-mcp-config", "--disallowedTools"]
if sys.argv[1:] in (["--version"], ["--help"], ["auth", "status"]):   # local commands, no model
    with open(HOME / "local-calls.jsonl", "a") as stream:
        stream.write(json.dumps(sys.argv[1:]) + "\n")
    if sys.argv[1:] == ["--version"]:
        print("2.9.0 (Claude Code stand-in)")
    elif sys.argv[1:] == ["--help"]:
        print("\n".join(f for f in FLAGS if f not in LOCAL.get("missing_flags", [])))
    else:
        print(json.dumps({"loggedIn": LOCAL.get("logged_in", True), "authMethod": "claude.ai"}))
    sys.exit(0)
prompt = sys.stdin.read()
kind = "judge" if prompt.startswith("You are grading") else "planner"
state_path = HOME / "used.json"
used = json.loads(state_path.read_text()) if state_path.exists() else {"planner": 0, "judge": 0}
script = json.loads((HOME / "script.json").read_text())[kind]
entry = script[used[kind]] if used[kind] < len(script) else {"text": '{"decision": "stop"}', "cost": 0.01}
used[kind] += 1
state_path.write_text(json.dumps(used))
with open(HOME / "calls.jsonl", "a") as stream:
    stream.write(json.dumps({"kind": kind, "argv": sys.argv[1:], "prompt": prompt}) + "\n")
result = {"type": "result", "subtype": "success", "is_error": False, "result": entry["text"]}
if entry.get("cost") is not None:
    result["total_cost_usd"] = entry["cost"]
print(json.dumps(result))
