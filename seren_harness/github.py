"""Optional, read-only suggestions of related GitHub issues via `gh issue list`.

V1 never creates issues or comments. The only command this module can build is
`gh issue list`, and the guard below rejects anything else.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

READ_ONLY_PREFIX = ["issue", "list"]


def _search_terms(finding):
    context = finding.get("context", {})
    words = [str(v) for k, v in sorted(context.items())
             if isinstance(v, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{3,60}", v)]
    return " ".join(words[:4]) or finding["assertion"].replace("_", " ")


def build_command(executable, repository, terms):
    argv = [executable, *READ_ONLY_PREFIX, "--repo", repository, "--state", "all",
            "--search", terms, "--limit", "5", "--json", "number,title,state,url"]
    if argv[1:3] != READ_ONLY_PREFIX:
        raise RuntimeError("refusing a non-read-only GitHub command")
    return argv


def suggest_related(github_config, finding, timeout=30):
    if not github_config.get("related_search"):
        return {"status": "unavailable", "reason": "related-issue search is not enabled"}
    executable = github_config.get("gh_executable", "")
    repository = github_config.get("repository", "")
    if not os.path.isabs(executable) or not os.access(executable, os.X_OK):
        return {"status": "unavailable", "reason": "github.gh_executable must be an absolute "
                "path to an executable gh CLI"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return {"status": "unavailable", "reason": "github.repository must be owner/name"}
    argv = build_command(executable, repository, _search_terms(finding))
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "unavailable", "reason": "gh failed: " + type(error).__name__}
    if result.returncode != 0:
        return {"status": "unavailable", "reason": "gh exited " + str(result.returncode)}
    try:
        issues = json.loads(result.stdout)
        issues = [{"number": int(i["number"]), "title": str(i["title"]),
                   "state": str(i["state"]), "url": str(i["url"])} for i in issues]
    except (ValueError, KeyError, TypeError):
        return {"status": "unavailable", "reason": "gh returned output in an unexpected shape"}
    return {"status": "ok", "terms": argv[argv.index("--search") + 1], "issues": issues}
