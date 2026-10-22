"""One bounded `managed-get` of the dedicated fixture, to show whether it can be read.

`python -m seren_harness prerequisite-read --config <c> --package <p>` makes at most one
service invocation, ever, for the package's journal: `seren agent managed-get <deployment>
-o json`, through the SerenCli wrapper with the pinned command timeout and no retry. The
intent is journaled before it is sent. A later invocation never sends it again, and changed
inputs are refused before any call.

On success it keeps only the safe identity, revision, status and baseline-match projection
(`preview_diagnostic.state_evidence`). On failure it keeps only the fixed classification
from `cli_errors.classify`. Never kept: the response, stderr, messages, paths, URLs or any
hash of them. A successful read establishes only that this deployment read worked; not
cloud-status access, preview behaviour, service health or permission to continue.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from seren_harness import cli_errors, contract
from seren_harness.config import ConfigError, load
from seren_harness.instruction_update import BINDING_VERSION, _sha, input_binding
from seren_harness.models import utc_now
from seren_harness.preview_diagnostic import state_evidence
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import SerenCli
from seren_harness.state import private_directory

READ_MATCHES, READ_DIFFERS, NOT_READ, INVALID = 0, 2, 3, 5
COMMAND = ("agent", "managed-get")
MAX_INVOCATIONS = 1


class PrerequisiteRead:
    def __init__(self, config, package_path, root):
        self.config = config
        self.root = Path(root)
        self.package_path = Path(package_path)
        self.package = json.loads(self.package_path.read_text())
        if self.package.get("kind") != "prerequisite_read_package":
            raise ValueError("the package is not a prerequisite read package")
        correction = self.package["correction"]
        self.current = (self.root / correction["current_file"]).read_text()
        self.corrected = (self.root / correction["corrected_file"]).read_text()
        if (_sha(self.current) != correction["current_sha256"]
                or _sha(self.corrected) != correction["corrected_sha256"]
                or _sha(self.corrected.strip()) != correction["corrected_trimmed_sha256"]):
            raise ValueError("the instruction files differ from the package")
        if self.package["target"]["deployment_id"] != config.deployment_id:
            raise ValueError("the configuration names a different deployment")
        self.deployment = config.deployment_id
        self.baseline = self.package["baseline"]
        self.cli = SerenCli(config.executable, config.command_timeout,
                            Sanitizer(config.secret_values()), config.cli_env())
        self.directory = Path(config.state_dir) / "prerequisite-read" / self.package["id"]
        self.journal_path = self.directory / "journal.json"
        self.journal = None

    def save(self):
        private_directory(self.directory)
        temporary = self.journal_path.with_suffix(".tmp")
        _write_private(temporary, json.dumps(self.journal, indent=2) + "\n")
        os.replace(temporary, self.journal_path)

    def finish(self, result, code):
        self.journal.update(result=result, finished_at=utc_now())
        self.save()
        return code

    def read(self):
        if self.journal["invocation"] is not None:
            raise RuntimeError("the one permitted read was already used")
        entry = {"command": " ".join(COMMAND), "started_at": utc_now(),
                 "timeout_seconds": self.config.command_timeout}
        self.journal["invocation"] = entry
        self.save()  # durable before the command can reach the service
        result = self.cli.json(*COMMAND, self.deployment)
        entry.update(status=result.status, exit_code=result.exit_code, finished_at=utc_now())
        detail = contract.unwrap(result.data) if result.ok else None
        if result.ok and not isinstance(detail, dict):
            self.journal["failure"] = {"category": "malformed_response",
                                       "rule": "undocumented_shape", "stage": "output",
                                       "http_status": None, "before_main_request": None,
                                       "http_response_received": None}
            return self.finish("not_read", NOT_READ)
        if not result.ok:
            self.journal["failure"] = cli_errors.classify(result)
            return self.finish("not_read", NOT_READ)
        observed = state_evidence(detail, None, self.deployment, self.baseline, self.current,
                                  self.corrected)
        observed.pop("status_available", None)
        self.journal["observation"] = observed
        matches = (observed["deployment_matches_target"]
                   and observed["agent_identity_matches_baseline"]
                   and observed["revision_matches_baseline"]
                   and observed["instruction_is_original"] is True)
        return self.finish("read_matches_fixture" if matches else "read_differs_from_fixture",
                           READ_MATCHES if matches else READ_DIFFERS)

    def run(self, out):
        say = lambda text: print(text, file=out)  # noqa: E731
        problem, binding = input_binding(self.config, self.cli, self.package_path,
                                         self.package, self.current, self.corrected)
        if problem:
            say("Refused before any call: " + problem)
            return INVALID
        if self.journal_path.exists():
            try:
                journal = json.loads(self.journal_path.read_text())
            except (OSError, ValueError):
                say("Refused before any call: the journal is unreadable; it is left as it is")
                return INVALID
            recorded = journal.get("binding") if isinstance(journal, dict) else None
            if not isinstance(recorded, dict) or recorded.get("version") != BINDING_VERSION:
                say("Refused before any call: the journal has no compatible input binding; "
                    "it is left as it is")
                return INVALID
            changed = sorted(k for k in set(recorded) | set(binding)
                             if recorded.get(k) != binding.get(k))
            if changed:
                say("Refused before any call: inputs changed since the recorded read ("
                    + ", ".join(changed) + "); the journal is left as it is")
                return INVALID
            self.journal = journal
            if not journal.get("result"):
                # Interrupted after the intent: the read may have been sent; never resent.
                self.journal["failure"] = {"category": "unknown", "rule": "interrupted",
                                           "stage": None, "http_status": None,
                                           "before_main_request": None,
                                           "http_response_received": None}
                self.finish("not_read", NOT_READ)
            else:
                say("This read was already used; nothing is requested.")
            code = {"read_matches_fixture": READ_MATCHES,
                    "read_differs_from_fixture": READ_DIFFERS}.get(journal["result"], NOT_READ)
        else:
            self.journal = {"package": self.package["id"], "deployment": self.deployment,
                            "binding": binding, "started_at": utc_now(),
                            "allowance": MAX_INVOCATIONS, "invocation": None,
                            "observation": None, "failure": None, "result": None}
            self.save()
            code = self.read()
        say(self.summary())
        return code

    def summary(self):
        j = self.journal
        failure = j.get("failure") or {}
        detail = ("; failure " + str(failure.get("category")) + " (rule "
                  + str(failure.get("rule")) + ", stage " + str(failure.get("stage"))
                  + (", HTTP " + str(failure["http_status"]) if failure.get("http_status")
                     else "") + ")") if failure else ""
        return ("Prerequisite read " + str(j.get("result")) + detail + ". "
                + ("1" if j.get("invocation") else "0") + " of " + str(MAX_INVOCATIONS)
                + " service invocation; journal " + str(self.journal_path))


def run(args, out=None):
    out = out or sys.stdout
    try:
        config = load(args.config)
        reader = PrerequisiteRead(config, args.package, args.root)
    except (ConfigError, OSError, ValueError, KeyError) as error:
        print("Refused before any call: " + str(error), file=out)
        return INVALID
    return reader.run(out)
