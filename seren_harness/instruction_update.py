"""The response-correctness package's update phase as one bounded harness command.

`python -m seren_harness instruction-update --config <c> --package <p>` runs the package's
exact procedure through the existing SerenCli wrapper. Every invocation (prerequisite reads,
preview, pre-update read, the update, read-back) has the configured command timeout:
subprocess.run kills and reaps the CLI process when it expires. A timed-out or otherwise
ambiguous update is an unknown remote outcome. The update is journaled durably before it is
sent and is never sent again, by this or any later invocation; an unknown outcome is
settled only by the bounded read-back allowance. Exit 0 means the corrected instruction is
read back on a new revision and provisioning may follow; anything else stops before it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

from seren_harness import contract, execution_limits
from seren_harness.config import ConfigError, load
from seren_harness.models import utc_now
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import OK, SerenCli
from seren_harness.state import private_directory

PROCEED, STOP, INVALID = 0, 2, 5
MAX_INVOCATIONS = 7
MAX_READBACKS = 2
# 2: the binding includes the CLI session location (`cli_session`); version-1 journals are
# never adopted.
BINDING_VERSION = 2
# Fields a prompt-only update must leave unchanged (compared in memory, never persisted).
GUARDED = ("deployment_id", "agent_identity_id", "secret_resolution_result_id", "approval_policy",
           "capability_policy", "tool_refs", "tool_presets", "config", "credentials", "model_id",
           "allowed_publisher_operations", "memory_policy", "max_tool_calls_per_run",
           "max_iterations", "max_timeout_seconds", "mode")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _instruction(detail):
    try:
        return detail["bundle"]["instructions"][0]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def discord_unused(package, config):
    """Only an instruction-update package that declares it uses no Discord, under a config
    whose Discord sender and observer are both unavailable, is exempt from the Discord pins.
    Every other package (response correctness, preview diagnostics) keeps them."""
    return (package.get("kind") == "instruction_update_package"
            and package.get("discord") == "not_used"
            and config.sender == "unavailable" and config.observer == "unavailable")


def input_binding(config, cli, package_path, package, current, corrected):
    """(problem, binding) from local checks only: pins, CLI checksum and version, and
    non-secret digests of every input. Shared with `preview-diagnostic`."""
    problems = execution_limits.check_pins(config, discord=not discord_unused(package, config))
    if problems:
        return "settings differ from the package pins: " + "; ".join(problems), None
    session = config.cli_session()
    if config.environment == "live" and (session["config_home_source"] != "explicit"
                                         or session["profile_source"] != "explicit"):
        return ("a live run needs config.seren.config_home and config.seren.profile; the CLI "
                "session location is not taken from the shell"), None
    expected = config.executable_sha256
    if expected is None and config.environment == "live":
        return "a live run needs config.seren.executable_sha256", None
    try:
        actual = hashlib.sha256(Path(config.executable).read_bytes()).hexdigest()
    except OSError:
        return "the Seren CLI executable is missing", None
    if expected is not None and actual != expected:
        return "the Seren CLI does not match its pinned checksum", None
    version = cli.version()  # local only
    found = contract.parse_version(version.data) if version.ok else None
    if found not in contract.SUPPORTED_CLI_VERSIONS:
        return "the Seren CLI version is not supported", None
    correction = package["correction"]
    # Non-secret digests of everything the attempt depends on. The config digest covers
    # the whole configuration (which holds no secret values, only references).
    binding = {"version": BINDING_VERSION, "config_digest": config.digest,
               "command_timeout_seconds": config.command_timeout,
               "package_sha256": hashlib.sha256(Path(package_path).read_bytes()).hexdigest(),
               "current_sha256": _sha(current),
               "corrected_sha256": _sha(corrected),
               "corrected_trimmed_sha256": correction["corrected_trimmed_sha256"],
               "deployment_id": config.deployment_id,
               "executable_sha256": actual, "cli_version": found,
               # The CLI session location in effect (explicit or as resolved from the
               # inherited environment): a changed root or profile refuses reuse.
               "cli_session": session}
    return None, binding


class Update:
    def __init__(self, config, package_path, root):
        self.config = config
        self.root = Path(root)
        self.package_path = Path(package_path)
        self.package = json.loads(self.package_path.read_text())
        correction = self.package["correction"]
        self.current = (self.root / correction["current_file"]).read_text()
        self.corrected = (self.root / correction["corrected_file"]).read_text()
        if (_sha(self.current) != correction["current_sha256"]
                or _sha(self.corrected) != correction["corrected_sha256"]
                or _sha(self.corrected.strip()) != correction["corrected_trimmed_sha256"]):
            raise ValueError("the instruction files differ from the package")
        target = self.package["target"]
        if target["deployment_id"] != config.deployment_id:
            raise ValueError("the configuration names a different deployment")
        self.deployment = config.deployment_id
        self.baseline = self.package["baseline"]
        self.cli = SerenCli(config.executable, config.command_timeout,
                            Sanitizer(config.secret_values()), config.cli_env())
        self.directory = Path(config.state_dir) / "instruction-update" / self.package["id"]
        self.journal_path = self.directory / "journal.json"
        self.journal = None

    def prompt_argument(self):
        """One `--prompt=<contents>` argument. The pinned CLI declares `prompt` as a plain long
        option (no allow_hyphen_values), so a separate value beginning with `-` (the
        instruction's `---` frontmatter) is parsed as an unknown argument and rejected."""
        return "--prompt=" + self.corrected

    # Durable journal -----------------------------------------------------------------
    def save(self):
        private_directory(self.directory)
        temporary = self.journal_path.with_suffix(".tmp")
        _write_private(temporary, json.dumps(self.journal, indent=2) + "\n")
        os.replace(temporary, self.journal_path)

    def call(self, purpose, *args):
        if len(self.journal["invocations"]) >= MAX_INVOCATIONS:
            raise RuntimeError("invocation allowance exhausted")
        entry = {"purpose": purpose, "argv": self.cli.display_argv(list(args))[:3],
                 "started_at": utc_now()}
        self.journal["invocations"].append(entry)
        self.save()  # durable before the command can reach the service
        result = self.cli.json(*args)
        entry.update(status=result.status, exit_code=result.exit_code, finished_at=utc_now(),
                     timeout_seconds=self.config.command_timeout)
        if result.status != OK:
            entry["stderr"] = result.stderr_tail[:300]
        if result.ok:
            entry["response_sha256"] = _sha(json.dumps(result.data, sort_keys=True))
        self.save()
        return result

    def stop(self, reason, **facts):
        self.journal.update(result="stopped", reason=reason, finished_at=utc_now(), **facts)
        self.save()
        return STOP

    # Reads ---------------------------------------------------------------------------
    def detail(self, purpose):
        result = self.call(purpose, "agent", "managed-get", self.deployment)
        detail = contract.unwrap(result.data) if result.ok else None
        return detail if isinstance(detail, dict) else None

    def projection(self, detail):
        content = _instruction(detail)
        return {"deployment_id": detail.get("deployment_id"),
                "agent_identity_id": detail.get("agent_identity_id"),
                "active_revision_id": detail.get("active_revision_id"),
                "status": detail.get("status"),
                "approval_applied": bool(detail.get("secret_resolution_result_id")),
                "awaiting_approval": contract.condition(detail, "AwaitingApproval") == "True",
                "instruction_sha256": _sha(content) if content is not None else None,
                "instruction_trimmed_sha256": _sha(content.strip()) if content else None}

    # Procedure -----------------------------------------------------------------------
    def validate(self):
        """(problem, binding) from local checks only; runs on every invocation before any
        service call, including a resumed one."""
        return input_binding(self.config, self.cli, self.package_path, self.package,
                             self.current, self.corrected)

    def run(self, out):
        say = lambda text: print(text, file=out)  # noqa: E731
        problem, binding = self.validate()
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
                    "it is not adopted under the current inputs and is left as it is")
                return INVALID
            changed = sorted(k for k in set(recorded) | set(binding)
                             if recorded.get(k) != binding.get(k))
            if changed:
                say("Refused before any call: inputs changed since the recorded attempt ("
                    + ", ".join(changed) + "); the journal is left as it is")
                return INVALID
            self.journal = journal
            return self.resume(say)
        self.journal = {"package": self.package["id"], "deployment": self.deployment,
                        "binding": binding, "started_at": utc_now(), "invocations": [],
                        "observations": {}, "update": None, "readbacks": 0, "result": None}
        self.save()
        code = self.procedure()
        say(self.summary())
        return code

    def procedure(self):
        b, obs = self.baseline, self.journal["observations"]
        # 1. Prerequisite reads: still the recorded fixture, stopped.
        before = self.detail("prerequisite")
        status = self.call("prerequisite", "agent", "cloud", "deployment", "status",
                           self.deployment)
        lifecycle = contract.unwrap(status.data) if status.ok else None
        if before is None or not isinstance(lifecycle, dict):
            return self.stop("prerequisite reads did not return the documented objects")
        obs["prerequisite"] = dict(self.projection(before),
                                   lifecycle=[lifecycle.get("status"),
                                              lifecycle.get("desired_lifecycle_state")])
        if (before.get("deployment_id") != self.deployment
                or before.get("agent_identity_id") != b["agent_identity_id"]
                or before.get("active_revision_id") != b["active_revision_id"]
                or _instruction(before) != self.current
                or lifecycle.get("status") != "stopped"
                or lifecycle.get("desired_lifecycle_state") != "stopped"):
            return self.stop("the target differs from the recorded fixture")
        # 2. Preview gate: only the instruction changes.
        preview = self.call("preview", "agent", "managed-preview", self.deployment,
                            self.prompt_argument())
        data = contract.unwrap(preview.data) if preview.ok else None
        current = data.get("current") if isinstance(data, dict) else None
        proposed = data.get("proposed") if isinstance(data, dict) else None
        changed = data.get("changed_fields") if isinstance(data, dict) else None
        if not (isinstance(current, dict) and isinstance(proposed, dict)
                and isinstance(changed, list)):
            return self.stop("the preview did not return the documented object")
        obs["preview"] = {"changed_fields": [c.get("field") for c in changed
                                             if isinstance(c, dict)],
                          "proposed": self.projection(proposed)}
        differs = [k for k in GUARDED if proposed.get(k) != current.get(k)]
        if (len(changed) != 1 or differs
                or contract.policy_digest(proposed) != contract.policy_digest(current)
                or _instruction(proposed) is None
                or _instruction(proposed).strip() != self.corrected.strip()
                or contract.condition(proposed, "AwaitingApproval") == "True"):
            return self.stop("the preview changes more than the instruction or proposes a "
                             "pending approval", guarded_differences=differs)
        # 3. Immediately before the update: revision unchanged.
        fresh = self.detail("pre-update")
        if fresh is None or fresh.get("active_revision_id") != b["active_revision_id"]:
            return self.stop("the revision changed or could not be read before the update")
        # 4. The update, once. Journaled before it is sent; never sent again.
        self.journal["update"] = {"status": "intent", "at": utc_now()}
        self.save()
        result = self.call("update", "agent", "managed-update", self.deployment,
                           self.prompt_argument())
        rejected = (not result.ok and result.status == "error"
                    and contract.definitely_not_applied(result.stderr_tail))
        self.journal["update"].update(
            status=result.status, outcome="sent_ok" if result.ok else
            "not_applied" if rejected else "unknown")
        self.save()
        if rejected:
            return self.stop("the update was rejected; nothing applied")
        return self.read_back()

    def read_back(self):
        """Settle the update from reads only, within the allowance."""
        while self.journal["readbacks"] < MAX_READBACKS:
            if self.journal["readbacks"] == 1 and self.journal["update"]["outcome"] != "unknown":
                break  # the second read-back is only for an unknown outcome
            if len(self.journal["invocations"]) >= MAX_INVOCATIONS:
                break  # the cumulative allowance is used; nothing more is requested
            self.journal["readbacks"] += 1
            detail = self.detail("read-back")
            if detail is None:
                continue
            seen = self.projection(detail)
            self.journal["observations"]["read_back"] = seen
            applied = (seen["instruction_trimmed_sha256"]
                       == self.package["correction"]["corrected_trimmed_sha256"]
                       and seen["active_revision_id"] != self.baseline["active_revision_id"])
            if (applied and seen["deployment_id"] == self.deployment
                    and seen["agent_identity_id"] == self.baseline["agent_identity_id"]
                    and seen["status"] == "stopped"):
                self.journal["update"]["outcome"] = "applied_verified"
                self.journal.update(result="proceed", finished_at=utc_now())
                self.save()
                return PROCEED
            if (self.journal["update"]["outcome"] == "unknown"
                    and seen["instruction_sha256"]
                    == self.package["correction"]["current_sha256"]
                    and seen["active_revision_id"] == self.baseline["active_revision_id"]):
                continue  # not visible yet, or not applied: use the remaining read only
            return self.stop("the read-back does not show exactly the corrected instruction "
                             "on a new revision of the stopped fixture")
        return self.stop("the update outcome could not be established within the read-back "
                         "allowance; it is not sent again")

    def resume(self, say):
        """A later invocation never repeats the procedure or the update."""
        if self.journal.get("result") == "proceed":
            say("Update already applied and verified; nothing requested.")
            return PROCEED
        update = self.journal.get("update")
        if update and "outcome" not in update:
            # Interrupted after the intent was journaled: the update may have been sent.
            update["outcome"] = "unknown"
            update.setdefault("status", "interrupted")
            self.save()
        if update and update.get("outcome") == "unknown" \
                and self.journal["readbacks"] < MAX_READBACKS:
            code = self.read_back()
            say(self.summary())
            return code
        say("An earlier attempt stopped (" + str(self.journal.get("reason"))
            + "); nothing is repeated.")
        return STOP

    def summary(self):
        j = self.journal
        return ("Instruction update " + str(j.get("result")) + ": "
                + str(j.get("reason") or (j.get("update") or {}).get("outcome")) + ". "
                + str(len(j["invocations"])) + " of " + str(MAX_INVOCATIONS)
                + " service invocations; journal " + str(self.journal_path))


def run(args, out=None):
    out = out or sys.stdout
    try:
        config = load(args.config)
        update = Update(config, args.package, args.root)
    except (ConfigError, OSError, ValueError, KeyError) as error:
        print("Refused before any call: " + str(error), file=out)
        return INVALID
    return update.run(out)
