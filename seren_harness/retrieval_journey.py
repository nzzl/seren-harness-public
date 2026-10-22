"""Shared-ledger adapter for the employee-retrieval journey package (one attempt, no retry).

Path inside the live gate's reservation: preflight reads -> preview of the frozen
grant+prompt update -> durable intent and restore obligation -> fixed-host update ->
exact read-back -> existing ClockRun start / one invocation / observation / stop ->
rollback to the captured original revision -> exact read-back -> rebinding record.

Preventive controls are the frozen update body, the fixed transport, exact read-backs
and the reviewed tool catalog. The ref grants an *operation* (seren_db_get_branch); no
pinned contract restricts it to one branch path, so the single path is requested
behaviour checked in the trace afterwards, not an enforced restriction.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from seren_harness import branch_read, contract, employee_run, employee_tool, finance_invoices
from seren_harness.employee_run import Halt, body
from seren_harness.state import AMBIGUOUS, REJECTED, SUCCEEDED

ROOT = Path(__file__).resolve().parents[1]
REF = {"kind": "publisher", "publisher_slug": "seren-db", "operation_id": "seren_db_get_branch",
       "execution_surface": "prompt", "require_approval": False}
VOLATILE = ("active_revision_id", "status", "conditions")  # runtime state, not configuration
# managed-get reaches us through the pinned CLI, which deserializes the generated SDK type
# ManagedAgentDeploymentDetail and prints it with serde: a non-required array is skipped when
# empty and a non-required option when null (typify `skip_serializing_if`; pinned
# openapi-seren-agent.json). The fixed-host preview is read raw, so it may carry `[]` / `null`
# where the CLI view has no key (attempt 7a00000c: allowed_remote_agent_origins, cron_schedule).
# Only that exact absence is equated, in every view; any other value, nested shape, unknown key
# or a field present in one view only (e.g. managed_agent_secret_requirements) still differs.
SKIPPED_WHEN_EMPTY = ("allowed_remote_agent_origins", "conditions", "connector_webhooks",
                      "credentials", "effective_model_candidates", "external_databases",
                      "guardrails", "tool_definitions", "tool_refs")
SKIPPED_WHEN_NULL = ("active_revision_id", "agent_identity_id", "alert_policy",
                     "capability_policy", "config", "context_budget_tokens", "cron_schedule",
                     "cron_timezone", "dashboard_config", "eval_gate", "fallback_models",
                     "llm_connection", "managed_agent_secret_requirements", "max_iterations",
                     "max_timeout_seconds", "max_tool_calls_per_run", "max_tool_output_chars",
                     "memory_policy", "network_policy", "requirements_txt", "runtime_policy",
                     "secret_resolution_result_id", "session_database", "side_effect_policy")
SETUP_SECONDS = 60
RESTORE_SECONDS = 60
SHUTDOWN_SECONDS = 180
# Calls kept back from setup/execution: stop 1, status 5, stop read-back 1, restore
# read-backs 2, rollback 1. With the 31-call cap this leaves 21 for preflight to run.
RESERVE_CALLS = 10


def sha(value):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


# Pinned openapi-seren-db.json Branch.required. `seren branches --project-id P get B -o json`
# prints this object itself (cli/src/output.rs print_branch -> print_json(branch)), not a
# {"data": ...} envelope, so the shared contract.unwrap() helper does not apply here.
BRANCH_FIELDS = ("id", "name", "project_id", "timeline_id", "created_by", "created_at",
                 "updated_at", "protected", "archived")


def branch_record(result):
    """The direct Branch object from a successful pinned-CLI read, or None."""
    data = result.data if result.ok and result.exit_code == 0 else None
    if (not isinstance(data, dict) or "data" in data
            or any(k not in data for k in BRANCH_FIELDS)):
        return None
    return data


def projection(detail):
    """The whole configuration as read back, minus revision id and runtime state, in the
    CLI's serialized form (a skipped empty optional is the same as an absent one)."""
    return {k: v for k, v in detail.items() if k not in VOLATILE
            and not (k in SKIPPED_WHEN_EMPTY and v == [])
            and not (k in SKIPPED_WHEN_NULL and v is None)}


def differing(a, b):
    """Names of the top-level fields that differ, for the stop reason; never values."""
    from seren_harness.preview_diagnostic import DETAIL_FIELDS
    keys = {k for k in set(a) | set(b) if k not in a or k not in b or a[k] != b[k]}
    named = sorted(k for k in keys if k in DETAIL_FIELDS)
    return ", ".join(named + (["%d unlisted" % (len(keys) - len(named))]
                              if len(keys) > len(named) else [])) or "none"


def spec(package):
    case = json.loads((ROOT / "cases/seren-employee-branch-read.json").read_text())
    value = dict(case["employee_tool"], arguments=package["inputs"]["arguments"],
                 deployment_id=package["inputs"]["deployment_id"])
    value["message"] = branch_read.message(value)
    return case, value


def setup_body(package):
    return {"tool_refs": [REF], "workload_patch": {"prompt": branch_read.prompt(spec(package)[1])}}


def load_package(entry, root=ROOT):
    """The frozen package must agree with the code that will execute it."""
    package = json.loads((Path(root) / entry["package"]["path"]).read_text())
    inputs, frozen = package["inputs"], package["frozen"]
    _, value = spec(package)
    if (package["access_path"]["required_ref"] != REF
            or inputs["deployment_id"] != entry["bindings"]["deployment_id"]
            or inputs["arguments"]["operation_id"] != REF["operation_id"]
            or inputs["arguments"]["path"] != entry["bindings"]["path"]
            or frozen["instruction_sha256"] != sha(branch_read.prompt(value))
            or frozen["message_sha256"] != sha(value["message"])
            or frozen["setup_body_sha256"] != sha(setup_body(package))
            or package["expected"]["answer"] != value["answer"]
            or package["bounds"]["calls_per_attempt"] != 31
            or sum(package["bounds"]["call_allocation"].values()) != 31):
        raise ValueError("journey package differs from the executable adapter")
    return package


def case(package):
    value, tool = spec(package)
    value = dict(value, target_status="resolved", employee_tool=tool,
                 id="seren.journey.employee-retrieval.branch-metadata")
    return value


class Budget:
    """Wraps the gate's journaled counter; work calls cannot spend the cleanup reserve."""

    def __init__(self, count, cap):
        self.count, self.cap, self.phase, self.used = count, cap, "work", 0

    def before_send(self, _receipt=None):
        if self.phase == "work" and self.used + 1 > self.cap - RESERVE_CALLS:
            raise Halt("work calls exhausted; remaining calls reserved for stop and restore")
        self.count()  # persisted before the request is sent
        self.used += 1

    def wrap_cli(self, cli):
        original = cli._execute

        def bounded(args, parse=False):
            self.before_send()
            return original(args, parse=parse)
        cli._execute = bounded


class GrantedBranchRun(employee_run.ClockRun):
    """ClockRun with grant setup before start and exact restoration after stop."""

    def __init__(self, session, case_, cli, store, transport, budget, **kw):
        super().__init__(session, case_, cli, store, **kw)
        self.transport, self.budget = transport, budget
        self.body = transport.body
        self.original = self.original_revision = None
        self.granted = self.setup_revision = None
        self.restore_id = self.grant_action = None
        self.restore = {"status": "not_needed"}
        self.preflight_branch = None
        self.restore_deadline = 0

    # -- deadlines: one budget from the gate's expiry; restore and shutdown kept back
    def execute(self):
        self.restore_deadline = self.deadline
        self.deadline = self.restore_deadline - RESTORE_SECONDS
        self.work_deadline = self.deadline - SHUTDOWN_SECONDS
        setup_deadline = min(self.clock() + SETUP_SECONDS, self.work_deadline - 30)
        self.preflight(setup_deadline)
        self.setup(setup_deadline)
        super().execute()  # prepare -> check_baseline (granted), start, invoke, observe

    def remote(self, kind, send, deadline, maximum=1):
        remaining = min(deadline, self.work_deadline) - self.clock()
        if self.counts.get(kind, 0) >= maximum or remaining <= 0:
            raise Halt("call or time budget exhausted: " + kind)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return send(min(30, remaining))

    def preflight(self, deadline):
        args = branch_read_args(self.case["employee_tool"])
        result = self.call("branch_read", args, 1, deadline=deadline)
        branch = branch_record(result) or {}
        expected = self.case["employee_tool"]["result"]
        if (not result.ok or branch.get("id") != expected["id"]
                or branch.get("project_id") != expected["project_id"]
                or not isinstance(branch.get("name"), str) or not branch["name"].strip()):
            raise Halt("preflight could not establish the target branch; nothing changed")
        self.preflight_branch = {k: branch.get(k) for k in ("id", "project_id", "name")}
        result = self.call("baseline_detail", ["agent", "managed-get", self.target], 1,
                           deadline=deadline)
        detail = body(result)
        try:
            baseline = employee_run.fixture_baseline(detail)
            finance_invoices.verify_detail(detail)
        except ValueError:
            raise Halt("baseline is not the reviewed unbound supplied-data fixture") from None
        if baseline != self.session["baseline"] or detail.get("external_databases") not in (None, []):
            raise Halt("baseline differs from the reviewed fixture; nothing changed")
        self.original, self.original_revision = projection(detail), detail["active_revision_id"]

    def setup(self, deadline):
        preview = self.remote("grant_preview",
                              lambda t: self.transport.preview(self.body, t), deadline)
        current, proposed = preview.get("current"), preview.get("proposed")
        if (preview.get("deployment_id") != self.target or not isinstance(current, dict)
                or not isinstance(proposed, dict)):
            raise Halt("preview did not return the documented current/proposed views")
        # Preview views carry no revision (optional in the schema; absent in attempt 7a00000c,
        # null in a revision-less preview example), so the preview binds to the preflight
        # read by its whole configuration. A revision it does report must still match.
        if current.get("active_revision_id") not in (None, self.original_revision):
            raise Halt("preview current revision differs from the preflight read")
        if projection(current) != self.original:
            raise Halt("preview current state differs from the preflight read: "
                       + differing(projection(current), self.original))
        before, after = projection(current), projection(proposed)
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        try:
            branch_read.verify_detail(proposed, self.case["employee_tool"])
        except ValueError:
            raise Halt("preview does not install the frozen instruction") from None
        if changed != {"bundle", "tool_refs"} or proposed.get("tool_refs") != [REF]:
            raise Halt("preview changes more than the one reviewed ref and instruction")
        self.granted = projection(proposed)
        request_id = str(uuid.uuid4())
        # Obligation and intent are durable before the mutation is sent.
        self.restore_id = self.store.add_cleanup(self.run_id, self.key, "restore_configuration",
                                                 self.target)
        self.restore = {"status": "open", "original_revision": self.original_revision}
        self.grant_action = self.store.record_intent(
            self.run_id, "grant_update", "grant_update", self.target,
            {"request_id": request_id, "body_sha256": sha(self.body)})
        from seren_harness.grant_transport import TransportError
        try:
            data = self.remote("grant_update",
                               lambda t: self.transport.update(self.body, request_id, t), deadline)
        except TransportError as error:
            if error.kind == "rejected":
                self.store.resolve_action(self.grant_action, REJECTED, {"reason": error.detail})
                raise Halt("grant update rejected; nothing applied") from None
            if error.kind != "uncertain":
                raise Halt("grant update not sent: " + error.detail) from None
            try:  # one lookup under the same key; the update is never re-sent
                data = self.remote("grant_action",
                                   lambda t: self.transport.action(request_id, t), deadline)
            except (TransportError, Halt):
                self.store.resolve_action(self.grant_action, AMBIGUOUS,
                                          {"reason": "update outcome unknown"})
                raise Halt("grant update outcome unknown; never re-sent") from None
        deployment, action = data.get("deployment") or {}, data.get("reconciliation") or {}
        revision = deployment.get("active_revision_id")
        if action.get("state") == "pending":
            data = self.remote("grant_action",
                               lambda t: self.transport.action(request_id, t), deadline)
            action = data.get("reconciliation") or {}
        if (deployment.get("id") != self.target or not employee_tool._uuid(revision)
                or revision == self.original_revision or action.get("request_id") != request_id
                or action.get("revision_id") != revision or action.get("state") != "applied"):
            self.store.resolve_action(self.grant_action, AMBIGUOUS,
                                      {"reason": "update response did not establish the revision"})
            raise Halt("grant update not established; restore decides from read-back")
        self.store.resolve_action(self.grant_action, SUCCEEDED,
                                  {"revision": revision, "request_id": request_id})
        self.setup_revision = revision

    def check_baseline(self, detail):
        """Journey-only exact-grant check; the invoice lane keeps fixture_baseline."""
        if (self.setup_revision is None or detail.get("status") != "stopped"
                or detail.get("active_revision_id") != self.setup_revision
                or projection(detail) != self.granted):
            raise Halt("read-back differs from the previewed grant change: "
                       + differing(projection(detail), self.granted or {}))

    def cleanup(self):
        self.budget.phase = "cleanup"
        try:
            super().cleanup()  # a failed stop raises: never mutate an unconfirmed runtime
        except (Halt, OSError, ValueError):
            if self.restore_id is not None:
                self.restore = dict(self.restore, status="open",
                                    reason="not attempted: shutdown unconfirmed")
            raise
        try:
            self.restore_configuration()
        except (Halt, OSError, ValueError) as error:
            self.problem = "restore unconfirmed (%s); obligation stays open" % error

    def restore_configuration(self):
        if self.restore_id is None:
            return
        self.deadline = self.restore_deadline
        detail = body(self.call("restore_detail", ["agent", "managed-get", self.target], 2,
                                cleanup=True))
        revision = detail.get("active_revision_id")
        if revision == self.original_revision and projection(detail) == self.original:
            self._close("not_applied", revision)
            return
        if not (projection(detail) == self.granted and (
                revision == self.setup_revision or (self.setup_revision is None
                                                    and employee_tool._uuid(revision)))):
            self.restore = dict(self.restore, status="open",
                                reason="configuration is neither original nor the reviewed change")
            raise Halt("restore target not established; obligation stays open")
        result = self.call("rollback", ["agent", "managed-rollback", self.target,
                                        self.original_revision, "--expected-active-revision-id",
                                        revision], 1, mutation=True, cleanup=True)
        data = body(result)
        restored = (data.get("deployment") or {}).get("active_revision_id")
        action = data.get("reconciliation") or {}
        if (not result.ok or not employee_tool._uuid(restored) or restored == revision
                or action.get("revision_id") != restored or action.get("state") != "applied"):
            self.restore = dict(self.restore, status="open", reason="rollback not established")
            raise Halt("rollback outcome not established; obligation stays open")
        after = body(self.call("restore_detail", ["agent", "managed-get", self.target], 2,
                               cleanup=True))
        if (after.get("active_revision_id") != restored or after.get("status") != "stopped"
                or projection(after) != self.original):
            self.restore = dict(self.restore, status="open", reason="restored read-back differs")
            raise Halt("restored configuration differs from the captured original")
        self._close("restored", restored)

    def _close(self, outcome, revision):
        self.restore = {"status": outcome, "original_revision": self.original_revision,
                        "setup_revision": self.setup_revision, "restored_revision": revision,
                        "projection_sha256": sha(self.original)}
        self.store.update_cleanup(self.restore_id, "done", self.restore)
        self.store.supersede_actions(self.run_id, "grant_update", "exact restore read-back")
        self.store.supersede_actions(self.run_id, "rollback", "exact restore read-back")


READ_ONLY_KINDS = ("branch_read", "baseline_detail")
NOT_REQUIRED = {"status": "not_required",
                "reason": "stopped before any start, preview or mutation: only read-only preflight "
                          "calls were sent, so no stop, restore or read-back was needed",
                "shutdown_observed": False}
# Offline reconciliation only, never a runtime shortcut: the runtime keeps any attempt that
# sent a preview blocked. The documented operation is "Preview a managed update before
# applying it" (seren_agent_preview_managed_deployment_update; the update is the separate
# PATCH), and saved post-preview reads showed no change: this deployment's revision on
# Preview reconciliation must preserve exact configuration identity.
# included, on 7a00003a (preview-diagnostic-2). No read-back followed this attempt's preview.
NOT_REQUIRED_AFTER_PREVIEW = {
    "status": "not_required",
    "reason": "stopped before any start or mutation: the read-only preflight calls and one "
              "successful preview were sent; no update, action lookup, start, stop or rollback",
    "shutdown_observed": False, "preview_sent": True, "post_attempt_readback": False,
    "stopped_observed_in": ["baseline managed-get", "preview current view"],
    "non_mutation_basis": "documented preview semantics and earlier post-preview reads "
                          "(preview semantic equivalence requires recipient verification)"}


def preview_only(transport_receipts, target):
    """True only for exactly one successful fixed-host preview of `target`, observed stopped."""
    if not isinstance(transport_receipts, list) or len(transport_receipts) != 1:
        return False
    receipt = transport_receipts[0]
    response = receipt.get("response") or {}
    current = response.get("current") or {}
    from seren_harness.grant_transport import BASE
    url = receipt.get("url") or ""
    prefix, suffix = BASE + "/deployments/", "/managed/preview"
    return (receipt.get("kind") == "preview" and receipt.get("method") == "POST"
            and url.startswith(prefix) and url.endswith(suffix)
            and "/" not in url[len(prefix):-len(suffix)]
            and receipt.get("request_id") is None and receipt.get("outcome") == "ok"
            and receipt.get("http_status") == 200 and response.get("deployment_id") == target
            and current.get("status") == "stopped")


def never_started(driver, store, transport_receipts):
    """True only when nothing that could change or start the employee was ever sent.

    A preview counts as a possible side effect, so any transport request keeps the normal
    cleanup obligations. This is never a confirmed shutdown or a completed retrieval.
    """
    return (driver.cleanup_id is None and driver.restore_id is None
            and driver.restore.get("status") == "not_needed" and not transport_receipts
            and all(r.get("kind") in READ_ONLY_KINDS for r in driver.receipts)
            and not store.actions(driver.run_id) and not store.open_cleanup(run_id=driver.run_id))


def branch_read_args(tool):
    path = tool["arguments"]["path"].split("/")
    return ["branches", "--project-id", str(uuid.UUID(path[2])), "get", str(uuid.UUID(path[4]))]


# ---------------------------------------------------------------- receipt rebinding

def rebinding_record(attempt_id, driver):
    """Written only after an exact restored read-back with a new revision id."""
    r = driver.restore
    return {"journey_attempt": attempt_id, "from_revision": r["original_revision"],
            "to_revision": r["restored_revision"], "setup_revision": r["setup_revision"],
            "original_projection": driver.original, "projection_sha256": sha(driver.original)}


def current_session(session, ledger):
    """Invoice-lane session with the baseline revision carried through verified restores."""
    chain = []
    hops = [a["rebinding"] for a in ledger.get("attempts", []) if a.get("rebinding")
            and a.get("status") == "finished" and a.get("cleanup_confirmed") is True]
    if not hops:
        return dict(session)
    revision = session["baseline"]["active_revision_id"]
    for ref in hops:
        record = _read_record(ref)
        if record["from_revision"] != revision:
            raise ValueError("rebinding chain broken; reconcile before invoice work")
        chain.append(ref)
        revision = record["to_revision"]
    if not chain:
        return dict(session)
    return dict(session, baseline=dict(session["baseline"], active_revision_id=revision),
                rebinding=chain)


def _read_record(ref):
    path = Path(ref["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
        raise ValueError("rebinding record changed")
    record = json.loads(path.read_text())
    if (record["projection_sha256"] != sha(record["original_projection"])
            or not employee_tool._uuid(record["to_revision"])
            or record["to_revision"] == record["from_revision"]):
        raise ValueError("rebinding record is not an exact restore")
    return record


def original_baseline(session):
    """The baseline the setup receipt binds, after verifying every rebinding hop."""
    chain = session.get("rebinding") or []
    if not chain:
        return session.get("baseline", {})
    records = [_read_record(ref) for ref in chain]
    for before, after in zip(records, records[1:]):
        if before["to_revision"] != after["from_revision"]:
            raise ValueError("rebinding chain broken")
    baseline = session["baseline"]
    if records[-1]["to_revision"] != baseline["active_revision_id"]:
        raise ValueError("session baseline does not match the last verified restore")
    for record in records:
        fields = {k: record["original_projection"].get(k) for k in baseline
                  if k != "active_revision_id"}
        if fields != {k: v for k, v in baseline.items() if k != "active_revision_id"}:
            raise ValueError("rebinding changed a reviewed baseline field")
    return dict(baseline, active_revision_id=records[0]["from_revision"])


def observation_identity(report):
    criteria = {c["id"]: c for c in report.get("criteria", [])}
    verdict = criteria.get("run_identity", {}).get("verdict")
    return "match" if verdict == "passed" else "mismatch" if verdict == "failed" else "missing"


def output_events(driver):
    data = contract.unwrap((driver.initial or {}).get("response")) if driver.initial else None
    return data.get("output_events") or [] if isinstance(data, dict) else []
