"""One bounded diagnostic preview of the corrected instruction; never an update.

`python -m seren_harness preview-diagnostic --config <c> --package <p>` issues at most this
fixed sequence, each step at most once, through the SerenCli wrapper with the pinned command
timeout and no retry:

  1. managed-get   2. cloud deployment status   3. managed-preview --prompt=<corrected>
  4. managed-get   5. cloud deployment status

It stops before the preview unless the prerequisite reads show the recorded fixture
(deployment, identity, revision, original instruction, stopped/stopped). Once the preview
has been attempted, the two post-preview reads are used whatever its result. There is no
route to managed-update, provisioning, apply, start, stop, Discord or a model session.

Responses are compared in memory only. The journal keeps a structural record: presence,
types, counts, opaque paths, allowlisted names and equality booleans. It never keeps a
value, an unlisted key name, a label, a message, stderr, or any hash of response content.
It describes observations, not causes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

from seren_harness import cli_errors, contract
from seren_harness.config import ConfigError, load
from seren_harness.instruction_update import BINDING_VERSION, GUARDED, _instruction, _sha, \
    input_binding
from seren_harness.models import utc_now
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import OK, SerenCli
from seren_harness.state import private_directory

COMPLETE, STOP, INCOMPLETE, INVALID = 0, 2, 3, 5
GET = ("agent", "managed-get")
STATUS = ("agent", "cloud", "deployment", "status")
PREVIEW = ("agent", "managed-preview")
SEQUENCE = (("pre-get", GET), ("pre-status", STATUS), ("preview", PREVIEW),
            ("post-get", GET), ("post-status", STATUS))
MAX_INVOCATIONS = len(SEQUENCE)

# Allowlisted names (pinned v0.10.0 openapi-seren-agent.json); anything else is withheld.
DETAIL_FIELDS = frozenset((
    "active_revision_id", "agent_identity_id", "agent_slug", "alert_policy",
    "allowed_publisher_operations", "allowed_remote_agent_origins", "approval_policy", "bundle",
    "capability_policy", "compute_backend", "conditions", "config", "connector_webhooks",
    "context_budget_tokens", "credentials", "cron_schedule", "cron_timezone",
    "dashboard_config", "deployment_id", "effective_model_candidates", "eval_gate",
    "external_databases", "fallback_models", "guardrails", "llm_connection",
    "managed_agent_secret_requirements", "max_iterations", "max_timeout_seconds",
    "max_tool_calls_per_run", "max_tool_output_chars", "memory_policy", "mode", "model_config",
    "model_id", "model_policy", "name", "network_policy", "private_output_policy",
    "requirements", "requirements_txt", "resolved_tools", "routing_reason", "runtime_adapter",
    "runtime_kind", "runtime_policy", "secret_keys", "secret_resolution_result_id",
    "session_database", "side_effect_policy", "status", "template", "tool_definitions",
    "tool_presets", "tool_refs", "visibility"))
BUNDLE_FIELDS = ("assets", "instructions")
INSTRUCTION_FIELDS = ("allowed_tools", "content", "kind", "path", "sha256")
CONDITION_TYPES = ("Accepted", "Ready", "AwaitingApproval", "PolicyDenied", "ImagePullFailed",
                   "ConfigFetchFailed", "ReadinessProbeFailed", "Suspended", "Expired", "Failed")
CONDITION_STATES = ("True", "False", "Unknown")
CHANGE_SUFFIXES = ("count",)
# The one secret-key name established locally as non-sensitive (connector requirement, A7/A9).
SAFE_SECRET_KEY = "DISCORD_BOT_TOKEN"
COMPARED = tuple(dict.fromkeys(GUARDED + contract.POLICY_FIELDS + (
    "active_revision_id", "status", "conditions", "tool_refs", "secret_keys", "bundle")))
# Views compared pairwise: preview view vs read, the preview's difference, state change, and
# whether the read after the preview shows the proposal.
PAIRS = (("pre", "current"), ("current", "proposed"), ("pre", "post"), ("proposed", "post"))
VIEWS = ("pre", "current", "proposed", "post")
STATE_KEYS = ("active_revision_id", "status", "approval_applied", "instruction_is_original",
              "instruction_is_corrected", "conditions", "lifecycle_status",
              "desired_lifecycle_state", "control_generation")
MAX_DEPTH, MAX_PATHS, MAX_ITEMS = 6, 64, 16
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ABSENT = object()


def kind(value):
    if value is ABSENT:
        return "absent"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "other"


def field(detail, name):
    return detail.get(name, ABSENT) if isinstance(detail, dict) else ABSENT


def pairwise(views, compare):
    """{"a~b": compare(a, b) or None when either view is unavailable}."""
    return {a + "~" + b: (compare(views[a], views[b])
                          if views.get(a) is not None and views.get(b) is not None else None)
            for a, b in PAIRS}


# Structural evidence ---------------------------------------------------------------------
def config_evidence(views):
    """Presence, types, opaque paths and equality booleans; no key names or values."""
    names, leaves, truncated = {}, {v: {} for v in VIEWS}, [False]

    def walk(view, value, path, depth):
        if isinstance(value, dict) and value and depth < MAX_DEPTH:
            for key in value:
                slot = names.setdefault(path, {})
                label = slot.setdefault(key, "k" + str(len(slot) + 1))
                walk(view, value[key], path + (label,), depth + 1)
        elif isinstance(value, list) and value and depth < MAX_DEPTH:
            for index, item in enumerate(value[:MAX_ITEMS]):
                walk(view, item, path + ("[" + str(index) + "]",), depth + 1)
            truncated[0] |= len(value) > MAX_ITEMS
        else:
            truncated[0] |= depth >= MAX_DEPTH and isinstance(value, (dict, list)) and bool(value)
            leaves[view][path] = value

    for view in VIEWS:
        if views.get(view) is not None:
            walk(view, field(views[view], "config"), ("config",), 0)
    paths = sorted(set().union(*(set(leaves[v]) for v in VIEWS)))
    truncated[0] |= len(paths) > MAX_PATHS
    records = []
    for path in paths[:MAX_PATHS]:
        present = {v: (kind(leaves[v].get(path, ABSENT)) if views.get(v) is not None else None)
                   for v in VIEWS}
        equal = {a + "~" + b: (leaves[a].get(path, ABSENT) == leaves[b].get(path, ABSENT)
                               if views.get(a) is not None and views.get(b) is not None
                               else None) for a, b in PAIRS}
        records.append({"path": ".".join(path), "type": present, "equal": equal})
    return {"type": {v: (kind(field(views[v], "config")) if views.get(v) is not None else None)
                     for v in VIEWS},
            "equal": pairwise(views, lambda a, b: field(a, "config") == field(b, "config")),
            "paths": records, "truncated": truncated[0],
            "key_names": "withheld; k<n> placeholders are consistent across views"}


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def secret_keys_evidence(views):
    """Counts, set and order comparisons and one allowlisted membership; no other names."""
    per_view = {}
    for view in VIEWS:
        if views.get(view) is None:
            per_view[view] = None
            continue
        value = field(views[view], "secret_keys")
        entry = {"type": kind(value)}
        if _string_list(value):
            entry.update(count=len(value), duplicates=len(set(value)) != len(value),
                         contains_discord_bot_token=SAFE_SECRET_KEY in value)
        per_view[view] = entry

    def compare(a, b):
        x, y = field(a, "secret_keys"), field(b, "secret_keys")
        if not (_string_list(x) and _string_list(y)):
            return {"comparable": False, "equal": x == y}
        return {"comparable": True, "equal": x == y, "same_set": set(x) == set(y),
                "added": len(set(y) - set(x)), "removed": len(set(x) - set(y)),
                "order_only": x != y and sorted(x) == sorted(y)}

    return {"views": per_view, "pairs": pairwise(views, compare)}


def _safe_field_name(name):
    if not isinstance(name, str):
        return "<unclassified:" + kind(name) + ">"
    head, _, rest = name.partition(".")
    if head not in DETAIL_FIELDS:
        return "<withheld>"
    if not rest:
        return head
    return name if all(part in CHANGE_SUFFIXES for part in rest.split(".")) \
        else head + ".<withheld>"


def _size(value):
    return len(value) if isinstance(value, (list, dict)) else None


def changed_fields_evidence(changed):
    if not isinstance(changed, list):
        return {"type": kind(changed)}
    entries = []
    for item in changed:
        if not isinstance(item, dict):
            entries.append({"field": "<unclassified:" + kind(item) + ">"})
            continue
        current, proposed = item.get("current_value", ABSENT), item.get("proposed_value", ABSENT)
        entries.append({"field": _safe_field_name(item.get("field")),
                        "label_present": isinstance(item.get("label"), str),
                        "current_value": {"type": kind(current), "size": _size(current)},
                        "proposed_value": {"type": kind(proposed), "size": _size(proposed)},
                        "values_equal": current == proposed})
    return {"type": "array", "count": len(changed), "entries": entries}


def fields_evidence(views):
    out = {name: pairwise(views, lambda a, b, n=name: field(a, n) == field(b, n))
           for name in COMPARED}
    current, proposed = views.get("current"), views.get("proposed")
    differing = undocumented = None
    if isinstance(current, dict) and isinstance(proposed, dict):
        keys = set(current) | set(proposed)
        changed = [k for k in keys if field(current, k) != field(proposed, k)]
        differing = sorted(k for k in changed if k in DETAIL_FIELDS)
        undocumented = sum(1 for k in changed if k not in DETAIL_FIELDS)
    policy = pairwise(views, lambda a, b: contract.policy_digest(a) == contract.policy_digest(b))
    return {"equal": out, "policy_digest_equal": policy,
            "current_vs_proposed_differing_fields": differing,
            "current_vs_proposed_undocumented_differing_count": undocumented}


def _instructions(detail):
    bundle = field(detail, "bundle")
    items = bundle.get("instructions") if isinstance(bundle, dict) else None
    return items if isinstance(items, list) else None


def bundle_evidence(views, current_text, corrected_text):
    per_view = {}
    for view in VIEWS:
        detail = views.get(view)
        if detail is None:
            per_view[view] = None
            continue
        bundle = field(detail, "bundle")
        items = _instructions(detail)
        assets = bundle.get("assets") if isinstance(bundle, dict) else None
        main = _instruction(detail)
        per_view[view] = {
            "type": kind(bundle),
            "instructions": len(items) if items is not None else None,
            "assets": len(assets) if isinstance(assets, list) else None,
            "undocumented_keys": (sum(1 for k in bundle if k not in BUNDLE_FIELDS)
                                  if isinstance(bundle, dict) else None),
            "main_is_original": main == current_text if isinstance(main, str) else None,
            "main_is_corrected": (main.strip() == corrected_text.strip()
                                  if isinstance(main, str) else None)}

    def compare(a, b):
        x, y = field(a, "bundle"), field(b, "bundle")
        if not (isinstance(x, dict) and isinstance(y, dict)):
            return {"comparable": False, "equal": x == y}
        ix, iy = _instructions(a) or [], _instructions(b) or []
        mx = ix[0] if ix and isinstance(ix[0], dict) else {}
        my = iy[0] if iy and isinstance(iy[0], dict) else {}
        return {"comparable": True, "equal": x == y,
                "assets_equal": x.get("assets", ABSENT) == y.get("assets", ABSENT),
                "instruction_count_equal": len(ix) == len(iy),
                "other_instructions_equal": ix[1:] == iy[1:],
                "main_fields_differing": sorted(k for k in INSTRUCTION_FIELDS
                                                if mx.get(k, ABSENT) != my.get(k, ABSENT)),
                "main_undocumented_fields_differing": sum(
                    1 for k in set(mx) | set(my)
                    if k not in INSTRUCTION_FIELDS and mx.get(k, ABSENT) != my.get(k, ABSENT)),
                "undocumented_keys_differing": sum(
                    1 for k in set(x) | set(y)
                    if k not in BUNDLE_FIELDS and x.get(k, ABSENT) != y.get(k, ABSENT))}

    return {"views": per_view, "pairs": pairwise(views, compare)}


def _allowed(value, allowed):
    return value if value in allowed else ("absent" if value is ABSENT else "<other>")


def state_evidence(detail, lifecycle, deployment, baseline, current_text, corrected_text):
    """Safe identity/revision references, lifecycle and approval observations of one read."""
    out = {"detail_available": isinstance(detail, dict),
           "status_available": isinstance(lifecycle, dict)}
    if isinstance(detail, dict):
        revision = detail.get("active_revision_id")
        conditions = detail.get("conditions")
        listed = [c for c in conditions if isinstance(c, dict)] \
            if isinstance(conditions, list) else []
        main = _instruction(detail)
        out.update(
            deployment_matches_target=detail.get("deployment_id") == deployment,
            agent_identity_matches_baseline=(detail.get("agent_identity_id")
                                             == baseline["agent_identity_id"]),
            active_revision_id=(revision if isinstance(revision, str)
                                and _UUID.fullmatch(revision) else kind(revision)),
            revision_matches_baseline=revision == baseline["active_revision_id"],
            status=_allowed(detail.get("status", ABSENT), contract.DEPLOYMENT_STATUSES),
            approval_applied=bool(detail.get("secret_resolution_result_id")),
            instruction_is_original=main == current_text if isinstance(main, str) else None,
            instruction_is_corrected=(main.strip() == corrected_text.strip()
                                      if isinstance(main, str) else None),
            conditions=[{"type": c.get("type"),
                         "status": _allowed(c.get("status", ABSENT), CONDITION_STATES)}
                        for c in listed if c.get("type") in CONDITION_TYPES],
            undocumented_conditions=(sum(1 for c in listed if c.get("type") not in
                                         CONDITION_TYPES) + (len(conditions) - len(listed)
                                                             if isinstance(conditions, list)
                                                             else 0)))
    if isinstance(lifecycle, dict):
        generation = lifecycle.get("control_generation")
        out.update(lifecycle_status=_allowed(lifecycle.get("status", ABSENT),
                                             contract.DEPLOYMENT_STATUSES),
                   desired_lifecycle_state=_allowed(
                       lifecycle.get("desired_lifecycle_state", ABSENT),
                       contract.LIFECYCLE_STATES),
                   control_generation=generation if isinstance(generation, int)
                   and not isinstance(generation, bool) else kind(generation))
    return out


# The command ----------------------------------------------------------------------------
class Diagnostic:
    def __init__(self, config, package_path, root):
        self.config = config
        self.root = Path(root)
        self.package_path = Path(package_path)
        self.package = json.loads(self.package_path.read_text())
        if self.package.get("kind") != "preview_diagnostic_package":
            raise ValueError("the package is not a preview diagnostic package")
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
        self.directory = Path(config.state_dir) / "preview-diagnostic" / self.package["id"]
        self.journal_path = self.directory / "journal.json"
        self.journal = None

    def save(self):
        private_directory(self.directory)
        temporary = self.journal_path.with_suffix(".tmp")
        _write_private(temporary, json.dumps(self.journal, indent=2) + "\n")
        os.replace(temporary, self.journal_path)

    def call(self, purpose):
        """Run one step of SEQUENCE, at most once ever, durably journaled before it is sent.
        No stderr, response content or digest of it is kept."""
        command = dict(SEQUENCE)[purpose]
        invocations = self.journal["invocations"]
        if len(invocations) >= MAX_INVOCATIONS or any(e["purpose"] == purpose
                                                      for e in invocations):
            raise RuntimeError("step already used or allowance exhausted: " + purpose)
        args = [*command, self.deployment]
        if command == PREVIEW:
            args.append("--prompt=" + self.corrected)
        entry = {"purpose": purpose, "command": " ".join(command), "started_at": utc_now(),
                 "timeout_seconds": self.config.command_timeout}
        invocations.append(entry)
        self.save()  # durable before the command can reach the service
        result = self.cli.json(*args)
        entry.update(status=result.status, exit_code=result.exit_code, finished_at=utc_now())
        self.save()
        return result

    def read(self, purpose):
        result = self.call(purpose)
        data = contract.unwrap(result.data) if result.ok else None
        if not isinstance(data, dict):
            self.journal["unavailable"][purpose] = (
                result.status if result.status != OK else "undocumented shape")
            self.classify(purpose, result)
            return None
        return data

    def classify(self, purpose, result):
        """A fixed category for a failed step (cli_errors); never the error text."""
        if not result.ok:
            self.journal.setdefault("failures", {})[purpose] = cli_errors.classify(result)

    def finish(self, result, reason, code):
        self.journal.update(result=result, reason=reason, finished_at=utc_now())
        self.save()
        return code

    def state(self, detail, lifecycle):
        return state_evidence(detail, lifecycle, self.deployment, self.baseline, self.current,
                              self.corrected)

    def procedure(self):
        obs, b = self.journal["observations"], self.baseline
        pre = self.read("pre-get")
        lifecycle = self.read("pre-status")
        obs["pre"] = self.state(pre, lifecycle)
        if not (isinstance(pre, dict) and isinstance(lifecycle, dict)
                and pre.get("deployment_id") == self.deployment
                and pre.get("agent_identity_id") == b["agent_identity_id"]
                and pre.get("active_revision_id") == b["active_revision_id"]
                and _instruction(pre) == self.current
                and lifecycle.get("status") == "stopped"
                and lifecycle.get("desired_lifecycle_state") == "stopped"):
            return self.finish("stopped_before_preview", "the prerequisite reads do not show "
                               "the recorded stopped fixture; the preview was not sent", STOP)
        self.journal["preview"] = {"status": "intent", "at": utc_now()}
        self.save()
        response = self.call("preview")
        self.journal["preview"].update(status=response.status, exit_code=response.exit_code)
        self.classify("preview", response)
        data = contract.unwrap(response.data) if response.ok else None
        current = data.get("current") if isinstance(data, dict) else None
        proposed = data.get("proposed") if isinstance(data, dict) else None
        changed = data.get("changed_fields", ABSENT) if isinstance(data, dict) else ABSENT
        if not (isinstance(current, dict) and isinstance(proposed, dict)):
            self.journal["unavailable"]["preview"] = (
                response.status if response.status != OK else "undocumented shape")
            current = proposed = None
        views = {"pre": pre, "current": current, "proposed": proposed, "post": None}
        self.record(views, changed)  # kept even if the process stops before the reads below
        # The post-preview reads are used whatever the preview's result.
        views["post"] = self.read("post-get")
        post_lifecycle = self.read("post-status")
        obs["post"] = self.state(views["post"], post_lifecycle)
        self.record(views, changed)
        if self.journal["unavailable"]:
            return self.finish("incomplete", "some observations are unavailable", INCOMPLETE)
        return self.finish("complete", "all five observations were made", COMPLETE)

    def record(self, views, changed):
        obs = self.journal["observations"]
        obs["views_available"] = {v: views.get(v) is not None for v in VIEWS}
        obs["changed_fields"] = (changed_fields_evidence(changed) if changed is not ABSENT
                                 else {"type": "unavailable"})
        obs["config"] = config_evidence(views)
        obs["secret_keys"] = secret_keys_evidence(views)
        obs["fields"] = fields_evidence(views)
        obs["bundle"] = bundle_evidence(views, self.current, self.corrected)
        self.compare_states()

    def compare_states(self):
        """Post-preview state against the prerequisite state, from the persisted
        observations: an observation only, concurrent changes are not excluded."""
        obs = self.journal["observations"]
        pre, post = obs.get("pre") or {}, obs.get("post") or {}
        obs["post_preview_change"] = {
            key: (pre[key] == post[key] if key in pre and key in post else None)
            for key in STATE_KEYS}
        self.save()

    def resume(self, say):
        """A later invocation never repeats a step or resets the allowance."""
        if self.journal.get("result"):
            say("This diagnostic already finished (" + str(self.journal["result"])
                + "); nothing is requested.")
            return {"complete": COMPLETE, "stopped_before_preview": STOP}.get(
                self.journal["result"], INCOMPLETE)
        used = {e.get("purpose") for e in self.journal["invocations"]}
        if "preview" not in used:
            return self.finish("stopped_before_preview", "interrupted before the preview; the "
                               "prerequisite observations were not retained, so the preview "
                               "is not sent", STOP)
        lost = "not retained after an interruption"
        if (self.journal.get("preview") or {}).get("status") == "intent":
            self.journal["preview"]["status"] = "interrupted"
        unavailable, obs = self.journal["unavailable"], self.journal["observations"]
        if "config" not in obs:
            unavailable.setdefault("preview", lost)
        post = lifecycle = None
        for purpose in ("post-get", "post-status"):
            if purpose in used:
                unavailable.setdefault(purpose, lost)
            elif purpose == "post-get":
                post = self.read(purpose)
            else:
                lifecycle = self.read(purpose)
        obs["post"] = self.state(post, lifecycle)
        obs["post_cross_view"] = "unavailable: the preview views were not retained"
        self.compare_states()
        return self.finish("incomplete", "resumed after an interruption; only the remaining "
                           "post-preview reads were used", INCOMPLETE)

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
                say("Refused before any call: inputs changed since the recorded diagnostic ("
                    + ", ".join(changed) + "); the journal is left as it is")
                return INVALID
            self.journal = journal
            code = self.resume(say)
        else:
            self.journal = {"package": self.package["id"], "deployment": self.deployment,
                            "binding": binding, "started_at": utc_now(),
                            "allowance": MAX_INVOCATIONS, "invocations": [], "preview": None,
                            "observations": {}, "unavailable": {}, "result": None}
            self.save()
            code = self.procedure()
        say(self.summary())
        return code

    def summary(self):
        j = self.journal
        change = j["observations"].get("post_preview_change") or {}
        differing = sorted(k for k, same in change.items() if same is False)
        return ("Preview diagnostic " + str(j.get("result")) + ": " + str(j.get("reason"))
                + ". " + str(len(j["invocations"])) + " of " + str(MAX_INVOCATIONS)
                + " service invocations; post-preview state differs in: "
                + (", ".join(differing) or "nothing observed") + "; journal "
                + str(self.journal_path))


def run(args, out=None):
    out = out or sys.stdout
    try:
        config = load(args.config)
        diagnostic = Diagnostic(config, args.package, args.root)
    except (ConfigError, OSError, ValueError, KeyError) as error:
        print("Refused before any call: " + str(error), file=out)
        return INVALID
    return diagnostic.run(out)
