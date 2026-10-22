"""Create one supplied-data fixture through the pinned CLI onboarding flow, then hand it
to the existing supplied-answer campaign under the same authorization.

One fixed draft (docs/supplied-fixture/onboarding-draft.json) with the supplied-data
instruction installed from the outset. Every mutation is journaled before it is sent and
is never repeated; an uncertain create, update, preview or apply ends the attempt. Apply
may bootstrap compute, so the returned deployment gets its cleanup obligation (in the
per-employee store) before the first stop. Handoff happens only after confirmed shutdown
and an exact configuration read-back; its setup receipt binds the deployment to this
authorization's own receipts, never to a matching name. Not a provisioning framework.

An interrupted attempt resumes only as recovery: within the original deadlines and call
counts, it may identify a deployment this attempt owns (bounded onboarding reads), take
over or register its one cleanup obligation, and stop it. It never resends create, apply
or an uncertain stop, and never hands a recovered fixture to the campaign. When the
remaining evidence, budget or authority is insufficient the journal says so ("unresolved")
with the next required action. Nothing here is a watchdog: if the process dies and is not
resumed, a bootstrapped runtime keeps running until someone acts.
"""
from __future__ import annotations

from datetime import timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import UUID, uuid5

from seren_harness import (__version__, clock_capabilities, contract, employee_run,
                           employee_tool, finance_invoices as finance, invoice_campaign)
from seren_harness.models import LIVE, utc_now
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer
from seren_harness.state import AMBIGUOUS, REJECTED, SUCCEEDED, StateError, Store, private_directory

ROOT = Path(__file__).resolve().parents[1]
DRAFT = ROOT / "docs" / "supplied-fixture" / "onboarding-draft.json"
NAME = "Seren Harness Supplied Data"
ROUTE = "created-supplied-fixture-v1"
SUITE = "bd-reliability-v1"
JOURNEY = "supplied_fixture_creation"
CREATION_WINDOW = 600          # authorization start through verified, stopped fixture
# Planned observation and cleanup budget from apply to a confirmed stop. It does not make
# compute stop: an unconfirmed shutdown leaves cleanup open and runtime may continue.
BOOTSTRAP_BUDGET = 300
WINDOW = CREATION_WINDOW + invoice_campaign.WINDOW
MAXIMA = {"onboarding_create": 1, "onboarding_update": 1, "onboarding_preview": 1,
          "onboarding_apply": 1, "onboarding_read": 4, "managed_stop": 2, "status": 8,
          "detail": 3, "catalog": 1}
MUTATIONS = ("onboarding_create", "onboarding_update", "onboarding_preview",
             "onboarding_apply", "managed_stop")
# Generalized regression scenario; private investigation history omitted.
# storage config and its database secret. Anything else is unexpected and stops handoff.
PLATFORM_CONFIG = {"seren_cloud_runtime"}
PLATFORM_SECRETS = {"SEREN_INTERNAL_CLOUD_RUNTIME_DATABASE_URL"}
LIMITS = {"max_iterations": 2, "max_timeout_seconds": 60, "max_tool_calls_per_run": 1,
          "max_tool_output_chars": 1000, "context_budget_tokens": 8000}
TEMPLATE_FIELDS = ("executable", "executable_sha256", "config_home", "profile", "state_dir",
                   "home", "type", "timeout_seconds", "other_tools_available_acknowledged")
RECOVERABLE = ("creating", "unresolved")


class Stop(Exception):
    pass


class Unresolved(Stop):
    """Recovery cannot finish within this attempt's evidence, budget or authority."""

    def __init__(self, reason, next_action):
        super().__init__(reason)
        self.next_action = next_action


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def draft():
    return json.loads(DRAFT.read_text())


def keys(authorization_id):
    """Idempotency keys derive from the authorization: a lost journal cannot mint new ones."""
    base = UUID(authorization_id)
    return {"create": str(uuid5(base, "onboarding-create")),
            "apply": str(uuid5(base, "onboarding-apply")),
            "campaign": str(uuid5(base, "campaign"))}


def verify_draft(value=None):
    """The shipped draft installs exactly the supplied-data instruction, no tools of its own."""
    value = draft() if value is None else value
    deployment = value["deployment"]
    skills = deployment["workload"]["execution"]["bundle"]["instructions"]
    if (value["employee"]["name"] != NAME or deployment["name"] != NAME
            or len(skills) != 1 or skills[0].get("kind") != "skill"
            or skills[0].get("content") != finance.instruction()
            or deployment["tool_presets"] != [] or deployment["tool_refs"] != []
            or deployment["credentials"] != [] or deployment["allowed_remote_agent_origins"] != []
            or deployment["workload"]["external_databases"] != []
            or deployment["workload"]["config"] != {}
            or deployment["workload"]["limits"] != LIMITS
            or deployment["approval_policy"] != "read_only"
            or deployment["private_output_policy"] != "control_plane"
            or value["requested_capabilities"] or value["selected_toolkit_ids"]
            or value["selected_connection_ids"]):
        raise ValueError("supplied-fixture draft differs from the reviewed creation inputs")
    return value


def package(template, suite=SUITE):
    """Offline proposal: creation inputs plus the unchanged campaign package template."""
    if set(template) != set(TEMPLATE_FIELDS) or template["profile"] != "default":
        raise ValueError("session template must hold exactly the pinned CLI and private paths")
    verify_draft()
    return {"version": 2, "route": ROUTE, "name": NAME, "suite": suite,
            "draft_sha256": _sha(DRAFT.read_bytes()), "instruction_sha256": _sha(finance.instruction()),
            "template": template, "source_sha256": invoice_campaign.source_digest(),
            "catalog": invoice_campaign.invoice_catalog.manifest(suite),
            "cases": list(invoice_campaign.invoice_catalog.case_ids(suite)),
            "limits": {"window_seconds": WINDOW, "creation_window_seconds": CREATION_WINDOW,
                       "bootstrap_shutdown_budget_seconds": BOOTSTRAP_BUDGET,
                       "creation_invocations": dict(MAXIMA),
                       "campaign": invoice_campaign.package({}, suite)["limits"]}}


def bind(proposal, approval):
    """The exact package and approval; the time window is checked separately."""
    if proposal != package(proposal["template"], proposal["suite"]):
        raise ValueError("creation inputs, campaign source, cases or limits changed")
    if (set(approval) != {"approved", "authorization_id", "package_sha256", "issued_at", "expires_at"}
            or approval["approved"] is not True
            or not employee_tool._uuid(approval["authorization_id"])
            or approval["package_sha256"] != invoice_campaign.digest(proposal)):
        raise ValueError("exact creation-through-shutdown authorization required")
    issued, expires = (employee_tool._utc(approval["issued_at"]),
                       employee_tool._utc(approval["expires_at"]))
    if issued is None or expires is None or not 0 < (expires - issued).total_seconds() <= WINDOW:
        raise ValueError("authorization window invalid or exceeds the creation-through-campaign window")
    return invoice_campaign.digest({"package": proposal, "approval": approval}), issued, expires


def authorize(proposal, approval, now):
    _, issued, expires = bind(proposal, approval)
    if not issued <= now < expires:
        raise ValueError("authorization window invalid or expired")
    return expires


def _receipt(path):
    return {"path": str(path), "sha256": _sha(Path(path).read_bytes())}


def _read(ref):
    path = Path(ref["path"])
    if _sha(path.read_bytes()) != ref["sha256"]:
        raise ValueError("creation receipt changed")
    receipt = json.loads(path.read_text())
    if receipt.get("status") != "ok" or receipt.get("exit_code") != 0:
        raise ValueError("successful creation receipt required")
    return receipt


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def verify_setup(basis, baseline, session):
    """Bind a created fixture to THIS authorization's own receipts (employee_run.validate)."""
    creation = basis.get("creation") or {}
    authorization = creation.get("authorization_id")
    if (basis.get("setup_route") != ROUTE or baseline.get("name") != NAME
            or not employee_tool._uuid(authorization)
            or session.get("creation_authorization_id") != authorization
            or session.get("state_dir") != creation.get("state_dir")
            or creation.get("draft_sha256") != _sha(DRAFT.read_bytes())
            or creation.get("instruction_sha256") != _sha(finance.instruction())):
        raise ValueError("created fixture is not bound to this authorization and draft")
    expected = keys(authorization)
    create = _read(creation["create_receipt"])
    apply = _read(creation["apply_receipt"])
    made, applied = create.get("response") or {}, apply.get("response") or {}
    onboarding = made.get("id")
    if (create["argv"][1:4] != ["agent", "onboarding", "create"]
            or _flag(create["argv"], "--idempotency-key") != expected["create"]
            or apply["argv"][1:5] != ["agent", "onboarding", "apply", onboarding]
            or _flag(apply["argv"], "--idempotency-key") != expected["apply"]
            or not employee_tool._uuid(onboarding) or applied.get("id") != onboarding
            or (made.get("draft") or {}).get("deployment", {}).get("name") != NAME
            or (applied.get("draft") or {}).get("deployment", {}).get("name") != NAME
            or (applied.get("operations") or {}).get("deployment_id") != baseline["deployment_id"]
            or basis["tool_refs_evidence"]["creation"] != creation["apply_receipt"]):
        raise ValueError("creation receipts do not establish this deployment")
    # Authored empty refs -> applied onboarding -> returned ID -> stopped detail revision.
    employee_run.verify_fixture_refs(basis["tool_refs_evidence"], baseline)
    catalog = contract.unwrap(_read(creation["catalog_receipt"]).get("response"))
    tools = catalog.get("tools") if isinstance(catalog, dict) else None
    if (not isinstance(tools, list) or catalog.get("deployment_id") != baseline["deployment_id"]
            or not employee_run.reviewed_policy(catalog, [t.get("name") for t in tools
                                                         if isinstance(t, dict)])):
        raise ValueError("created fixture tool exposure differs from the reviewed exposure")


def owned_deployment(record, onboarding, source_revision):
    """The deployment an onboarding record proves THIS attempt applied, else None.

    Pinned cloud spec ManagedAgentOnboarding: id, application{source_revision, attempt,
    deployment_outcome}, operations.deployment_id, draft. The record must be this attempt's
    onboarding (created under its derived key), carry an application of the revision this
    attempt previewed, describe this fixture, and name exactly one deployment UUID.
    """
    if not isinstance(record, dict):
        return None
    application = record.get("application")
    operations = record.get("operations")
    deployment = operations.get("deployment_id") if isinstance(operations, dict) else None
    named = ((record.get("draft") or {}).get("deployment") or {}).get("name") \
        if isinstance(record.get("draft"), dict) else None
    if (not employee_tool._uuid(onboarding) or record.get("id") != onboarding
            or type(source_revision) is not int or not isinstance(application, dict)
            or application.get("source_revision") != source_revision
            or application.get("deployment_outcome") not in ("pending", "confirmed", "uncertain",
                                                             "failed")
            or named != NAME or not employee_tool._uuid(deployment)):
        return None
    return deployment


class Creation:
    """One attempt. State lives in a durable journal; nothing is repeated after a crash."""

    def __init__(self, proposal, approval, cli, store, journal_path, clock=time.monotonic,
                 sleep=time.sleep, wall=utc_now):
        self.proposal, self.approval, self.cli, self.store = proposal, approval, cli, store
        self.path, self.clock, self.sleep, self.wall = journal_path, clock, sleep, wall
        self.folder = private_directory(journal_path.parent / approval["authorization_id"])
        self.keys = keys(approval["authorization_id"])
        self.sanitizer = Sanitizer()
        self.state = {}
        self.target = self.run_id = self.cleanup_id = None
        self.create_path = self.preview_path = self.apply_path = self.detail_path = None

    # Durable time: deadlines are wall-clock instants in the journal, never re-granted.
    def until(self, instant):
        when = employee_tool._utc(instant)
        return self.clock() + (when - employee_tool._utc(self.wall())).total_seconds()

    @property
    def deadline(self):
        return self.until(self.state["deadline_at"])

    def save(self):
        invoice_campaign.write(self.path, self.state)

    def call(self, kind, args, deadline=None):
        counter = "onboarding_read" if kind in ("onboarding_get", "onboarding_resume") else kind
        limit = self.deadline if deadline is None else min(self.deadline, deadline)
        remaining = limit - self.clock()
        if self.state["counts"].get(counter, 0) >= MAXIMA[counter] or remaining <= 0:
            raise Unresolved("call or time budget exhausted: " + kind,
                             "owner decision under new authority; this attempt's budget is spent")
        self.state["counts"][counter] = self.state["counts"].get(counter, 0) + 1
        number = self.state["counts"][counter]
        action = intent = None
        if kind in MUTATIONS:
            intent = {"kind": kind, "number": number, "at": self.wall(),
                      "argv": self.cli.display_argv(args), "outcome": None}
            self.state["intents"].append(intent)
            if self.run_id is not None:
                action = self.store.record_intent(self.run_id, kind, kind, self.target,
                                                  {"argv": self.cli.display_argv(args)})
        self.save()  # Durable intent BEFORE the call; a crash leaves it visible as unsent-or-sent.
        self.cli.timeout = min(30, remaining)
        started = self.wall()
        result = self.cli.json(*args)
        receipt = {"kind": kind, "started_at": started, "finished_at": self.wall(),
                   "status": result.status, "exit_code": result.exit_code,
                   "argv": result.argv, "response": result.data, "stderr": result.stderr_tail}
        path = self.folder / ("%s-%d.json" % (kind, number))
        _write_private(path, json.dumps(self.sanitizer.value(receipt), indent=2))
        self.state["receipts"]["%s-%d" % (kind, number)] = _receipt(path)
        definite = contract.definitely_not_applied(result.stderr_tail)
        if intent is not None:
            intent["outcome"] = "ok" if result.ok else ("rejected" if definite else "uncertain")
        if action is not None:
            self.store.resolve_action(action, SUCCEEDED if result.ok else (
                REJECTED if definite else AMBIGUOUS), receipt)
            self.store.event(self.run_id, "cli_receipt", receipt)
        self.save()
        return result, path

    def mutate(self, kind, args, deadline=None):
        result, path = self.call(kind, args, deadline)
        data = result.data if isinstance(result.data, dict) else {}
        if not result.ok:
            definite = contract.definitely_not_applied(result.stderr_tail)
            raise Stop(kind + (" rejected" if definite else " outcome unknown") + "; not repeated")
        return data, path

    # ---------------------------------------------------------------- onboarding
    def create(self):
        authored = verify_draft()
        made, self.create_path = self.mutate("onboarding_create", [
            "agent", "onboarding", "create", "--entry-mode", "additional-employee",
            "--draft", str(DRAFT), "--idempotency-key", self.keys["create"]])
        onboarding, revision = made.get("id"), made.get("revision")
        if (not employee_tool._uuid(onboarding) or type(revision) is not int
                or made.get("status") != "draft"
                or (made.get("draft") or {}).get("deployment", {}).get("name") != NAME):
            raise Stop("create reply does not establish the onboarding")
        self.state["onboarding_id"] = onboarding
        self.save()
        if made.get("step") != "review":
            made, _ = self.mutate("onboarding_update", [
                "agent", "onboarding", "update", onboarding, "--expected-revision",
                str(revision), "--step", "review", "--draft", str(DRAFT)])
            if (made.get("id") != onboarding or made.get("step") != "review"
                    or type(made.get("revision")) is not int):
                raise Stop("update did not reach review on the same onboarding")
        preview, self.preview_path = self.mutate("onboarding_preview",
                                                 ["agent", "onboarding", "preview", onboarding])
        preview = contract.unwrap(preview) if isinstance(preview, dict) else {}
        self.gate_preview(authored, preview, onboarding)
        if self.deadline - self.clock() < BOOTSTRAP_BUDGET + 60:
            raise Stop("insufficient time for bootstrap shutdown and read-back; nothing applied")
        # Everything recovery needs is durable before apply is sent (call() saves it).
        self.state.update(source_revision=preview["source_revision"], apply="intent",
                          bootstrap_deadline_at=(employee_tool._utc(self.wall()) + timedelta(
                              seconds=BOOTSTRAP_BUDGET)).isoformat())
        result, self.apply_path = self.call("onboarding_apply", [
            "agent", "onboarding", "apply", onboarding, "--expected-revision",
            str(preview["source_revision"]), "--idempotency-key", self.keys["apply"]])
        applied = result.data if result.ok and isinstance(result.data, dict) else {}
        deployment = owned_deployment(applied, onboarding, preview["source_revision"])
        confirmed = (applied.get("application") or {}).get("deployment_outcome") == "confirmed"
        if not (result.ok and confirmed and deployment):
            self.state["apply"] = "uncertain"
            self.save()
            self.target = self.recover()
            self.recovery_read()
            self.own(self.target)
            raise Stop("apply outcome not confirmed; created fixture is never handed off")
        self.state.update(apply="confirmed", deployment_id=deployment)
        self.save()
        self.own(deployment)

    def gate_preview(self, authored, preview, onboarding):
        spec = preview.get("desired_spec") or {}
        wanted = authored["deployment"]
        workload = spec.get("workload") or {}
        bundle = (workload.get("execution") or {}).get("bundle") or {}
        effective = preview.get("effective_policy") or {}
        same = ("name", "mode", "template", "approval_policy", "model_policy", "tool_presets",
                "tool_refs", "allowed_remote_agent_origins", "credentials",
                "private_output_policy")
        # Generalized regression scenario; private investigation history omitted.
        # the three authored kinds, each disabled, are accepted.
        memory = spec.get("memory_policy")
        if (not isinstance(memory, dict) or set(memory) != set(wanted["memory_policy"])
                or any(not isinstance(v, dict) or v.get("enabled") is not False
                       for v in memory.values())):
            raise Stop("preview memory policy differs; nothing applied")
        if (preview.get("onboarding_id") != onboarding
                or type(preview.get("source_revision")) is not int
                or not set(spec) <= set(wanted)
                or any(spec.get(k) != wanted[k] for k in same)
                or workload.get("config") != {} or workload.get("limits") != LIMITS
                or workload.get("compute_backend") != wanted["workload"]["compute_backend"]
                or workload.get("external_databases") not in (None, [])
                or bundle.get("instructions") is None
                or [i.get("content") for i in bundle["instructions"]] != [finance.instruction()]
                or not employee_run.reviewed_policy(effective, effective.get("resolved_tools"))):
            raise Stop("preview differs from the reviewed draft; nothing applied")
        try:
            clock_capabilities.require_capabilities(
                wanted["capability_policy"], spec.get("capability_policy"),
                self.folder / "preview-controls.json", self.sanitizer)
        except ValueError:
            raise Stop("preview capability controls differ; nothing applied") from None

    def recover(self):
        """Bounded onboarding reads; only a deployment this attempt owns is accepted."""
        onboarding, revision = self.state.get("onboarding_id"), self.state.get("source_revision")
        while self.state["counts"].get("onboarding_read", 0) < MAXIMA["onboarding_read"]:
            result, _ = self.call("onboarding_get", ["agent", "onboarding", "get", onboarding])
            record = result.data if result.ok and isinstance(result.data, dict) else None
            deployment = owned_deployment(record, onboarding, revision)
            if deployment:
                self.state["deployment_id"] = deployment
                self.save()
                return deployment
        raise Unresolved("apply was sent but no deployment owned by this attempt is readable",
                         "owner check of onboarding " + str(onboarding) + " under new authority; "
                         "a runtime may exist and keep running")

    def own(self, deployment):
        """One cleanup obligation per attempt and deployment, registered before any stop.
        After an interruption the existing run and obligation are taken over, not duplicated."""
        self.target = deployment
        key = "clock:" + deployment
        runs = [r for r in self.store.runs() if r["journey"] == JOURNEY
                and r["config_digest"] == self.approval["authorization_id"]]
        if len(runs) > 1 or any(r["employee_key"] != key for r in runs):
            raise Unresolved("store holds contradictory creation records for this attempt",
                             "owner review of the store before any action")
        if runs:
            self.run_id = runs[0]["id"]
        else:
            self.run_id = self.store.create_run(JOURNEY, key, LIVE,
                                                self.approval["authorization_id"], __version__)
        self.store.acquire_lock(key, self.run_id)
        items = [c for c in self.store.cleanup_items(self.run_id)
                 if c["kind"] == "stop_runtime" and c["target"] == deployment]
        self.cleanup_id = items[0]["id"] if items else self.store.add_cleanup(
            self.run_id, key, "stop_runtime", deployment)
        done = bool(items) and items[0]["status"] == "done"
        self.state["cleanup"] = {"run_id": self.run_id, "cleanup_id": self.cleanup_id,
                                 "status": "done" if done else "pending"}
        self.save()

    # ---------------------------------------------------------------- bootstrap shutdown
    def status(self, deadline):
        result, _ = self.call("status", ["agent", "cloud", "deployment", "status", self.target],
                              deadline)
        data = employee_run.body(result)
        if data.get("id") != self.target:
            raise Unresolved("lifecycle read does not establish the created deployment",
                             "owner check of the deployment under new authority")
        return data

    def shutdown(self):
        """Stop once; one recovery stop only after an accepted stop and two running reads.
        An uncertain (unanswered or interrupted) stop is never resent: reads only."""
        stop_deadline = min(self.deadline, self.until(self.state["bootstrap_deadline_at"]))
        observe_by = stop_deadline - 30  # the last 30 s are kept for the read-back

        def stop():
            # Every bootstrap stop is gated by, and its command timeout bounded by, the
            # bootstrap budget; the broader creation deadline never extends it.
            if self.clock() >= observe_by:
                raise Unresolved("bootstrap budget expired before a stop could be sent; no "
                                 "stop sent", "owner stop and confirmation under new authority")
            self.mutate("managed_stop", ["agent", "managed-stop", self.target], observe_by)

        stops = [i for i in self.state["intents"] if i["kind"] == "managed_stop"]
        if any(i["outcome"] != "ok" for i in stops):
            observe_only = True
        else:
            observe_only = False
            if not stops:
                stop()
        running = 0
        while True:
            self.sleep(min(15, max(0, observe_by - self.clock())))
            if self.clock() >= observe_by:
                raise Unresolved("shutdown not confirmed within the bootstrap budget",
                                 "owner confirmation or stop under new authority; runtime and "
                                 "charges may continue")
            data = self.status(observe_by)
            if data.get("status") == "stopped" and data.get("desired_lifecycle_state") == "stopped":
                break
            running = running + 1 if data.get("status") == "running" else 0
            stops = [i for i in self.state["intents"] if i["kind"] == "managed_stop"]
            if (not observe_only and running >= 2 and len(stops) == 1
                    and stops[0]["outcome"] == "ok"):
                stop()
                running = 0
        result, self.detail_path = self.call("detail", ["agent", "managed-get", self.target],
                                             stop_deadline)
        detail = employee_run.body(result)
        if detail.get("deployment_id") != self.target or detail.get("status") != "stopped":
            raise Unresolved("managed stop read-back is unconfirmed",
                             "owner confirmation under new authority")
        self.store.update_cleanup(self.cleanup_id, "done", {"observed_at": self.wall()})
        self.store.supersede_actions(self.run_id, "managed_stop", "confirmed stopped read-back")
        self.state["cleanup"]["status"] = "done"
        self.save()
        return detail

    def recovery_read(self):
        """Before acting on a recovered deployment, its own lifecycle view must name this
        fixture (onboarding linkage was already required)."""
        data = self.status(self.deadline)
        if data.get("name") != NAME:
            raise Unresolved("recovered deployment does not describe this fixture",
                             "owner check under new authority; nothing was stopped")

    # ---------------------------------------------------------------- verification
    def verify(self, detail):
        try:
            baseline = employee_run.fixture_baseline(detail)
            finance.verify_detail(detail)
        except ValueError:
            raise Stop("created fixture fields or instruction differ") from None
        config = detail.get("config") or {}
        secrets = detail.get("secret_keys") or []
        if (baseline["name"] != NAME or baseline["deployment_id"] != self.target
                or baseline["tool_presets"] not in ([], ["live_data"])
                or any(baseline[k] != v for k, v in (("approval_policy", "read_only"),
                       ("max_iterations", 2), ("max_tool_calls_per_run", 1),
                       ("max_timeout_seconds", 60), ("private_output_policy", "control_plane")))
                or not isinstance(config, dict) or not set(config) <= PLATFORM_CONFIG
                or not isinstance(secrets, list) or not set(secrets) <= PLATFORM_SECRETS):
            raise Stop("created fixture configuration differs; no handoff")
        result, catalog_path = self.call("catalog", ["agent", "managed-deployment-tools",
                                                     self.target])
        data = employee_run.body(result)
        tools = data.get("tools")
        if (data.get("deployment_id") != self.target or not isinstance(tools, list)
                or any(not isinstance(t, dict) or not isinstance(t.get("effective_policy"), dict)
                       or t["effective_policy"].get("status") not in ("allowed", "blocked")
                       for t in tools)
                or not employee_run.reviewed_policy(data, [t.get("name") for t in tools])):
            raise Stop("created fixture tool exposure differs from the reviewed exposure")
        template = self.proposal["template"]
        basis = {
            "deployment_id": self.target, "active_revision_id": baseline["active_revision_id"],
            "execution_scope": finance.SCOPE, "setup_route": ROUTE,
            "unbound_fixture_verified": True, "creation_receipt": str(self.apply_path),
            "preview_receipt": str(self.preview_path),
            "effective_policy": {"resolved_tools": sorted(t["name"] for t in tools),
                                 "tool_presets": data["tool_presets"],
                                 "allowed_publisher_operations": data["allowed_publisher_operations"],
                                 "approval_policy": data["approval_policy"]},
            "tool_refs_evidence": employee_run.fixture_refs_evidence(
                self.apply_path, self.preview_path, self.detail_path),
            "creation": {"authorization_id": self.approval["authorization_id"],
                         "package_sha256": self.approval["package_sha256"],
                         "state_dir": template["state_dir"],
                         "draft_sha256": _sha(DRAFT.read_bytes()),
                         "instruction_sha256": _sha(finance.instruction()),
                         "create_receipt": _receipt(self.create_path),
                         "apply_receipt": _receipt(self.apply_path),
                         "catalog_receipt": _receipt(catalog_path),
                         "cleanup_run": self.run_id}}
        path = self.folder / "setup-receipt.json"
        _write_private(path, json.dumps(basis, indent=2))
        # authorization_id, expires_at and runtime are replaced per case by the campaign.
        session = dict(template, baseline=baseline, execution_scope=finance.SCOPE,
                       setup_receipt=str(path), setup_receipt_sha256=_sha(path.read_bytes()),
                       creation_authorization_id=self.approval["authorization_id"],
                       authorization_id=self.keys["campaign"], runtime_remaining_seconds=300,
                       expires_at=self.approval["expires_at"])
        verify_setup(basis, baseline, session)
        return session

    # ---------------------------------------------------------------- entry points
    def finish(self, error):
        self.state["status"] = "unresolved" if isinstance(error, Unresolved) else "stopped"
        self.state["reason"] = str(error)
        if isinstance(error, Unresolved):
            self.state["next_action"] = error.next_action
            if self.target is not None and self.state.get("cleanup", {}).get("status") != "done":
                self.state["next_action"] += ("; cleanup for " + self.target + " is open: "
                                              "runtime and charges may continue")
        self.save()

    def release(self):
        if self.run_id is not None:
            open_ = bool(self.store.open_cleanup(run_id=self.run_id)
                         or self.store.actions(self.run_id, True))
            self.store.update_run(self.run_id, status="unresolved" if open_ else "complete")
            self.store.release_lock("clock:" + self.target, self.run_id)

    def run(self, binding, expires):
        now = employee_tool._utc(self.wall())
        self.state = {"binding": binding, "authorization_id": self.approval["authorization_id"],
                      "status": "creating", "counts": {}, "receipts": {}, "intents": [],
                      "deadline_at": min(expires, now + timedelta(seconds=CREATION_WINDOW))
                      .isoformat()}
        self.save()
        try:
            self.create()
            detail = self.shutdown()
            session = self.verify(detail)
        except Stop as error:
            # A deployment that exists but was never stopped still gets the bounded
            # shutdown; a shutdown already attempted is never restarted here.
            if (self.target is not None and not isinstance(error, Unresolved)
                    and self.state.get("cleanup", {}).get("status") != "done"):
                try:
                    self.shutdown()
                except Stop as again:
                    error = again if isinstance(again, Unresolved) else error
                    self.state["shutdown"] = str(again)
            self.finish(error)
            return None
        finally:
            self.release()
        self.state.update(status="fixture_ready", session=session)
        self.save()
        return session

    def resume(self, state):
        """Recovery only, inside the original deadlines and counts; never a handoff."""
        self.state = state
        try:
            if self.clock() >= self.deadline:
                raise Unresolved("creation deadline passed before recovery finished",
                                 "owner decision under new authority")
            unsent = [i for i in state["intents"] if i["outcome"] is None]
            if state.get("apply") is None:
                if unsent and unsent[-1]["kind"] == "onboarding_create":
                    raise Unresolved("create was sent and its result is unknown; the onboarding "
                                     "is not identified", "owner check of onboardings created "
                                     "with key " + self.keys["create"] + " under new authority")
                raise Stop("interrupted before apply; apply was never sent")
            deployment = state.get("deployment_id")
            if deployment is None:
                deployment = self.recover()
            elif owned_deployment({"id": state.get("onboarding_id"), "application": {
                    "source_revision": state.get("source_revision"),
                    "deployment_outcome": "confirmed"}, "operations": {
                    "deployment_id": deployment}, "draft": {"deployment": {"name": NAME}}},
                    state.get("onboarding_id"), state.get("source_revision")) != deployment:
                raise Unresolved("journal identity is malformed", "owner review; nothing stopped")
            self.target = deployment
            if not [i for i in state["intents"] if i["kind"] == "managed_stop"]:
                self.recovery_read()
            self.own(deployment)
            if self.state["cleanup"]["status"] != "done":
                self.shutdown()
            raise Stop("recovered after interruption: shutdown confirmed, cleanup done; "
                       "a recovered fixture is never handed to the campaign")
        except Stop as error:
            self.finish(error)
            return self.state
        finally:
            self.release()


def handoff(proposal, approval, session, now):
    """The campaign package and its derived approval follow from the bound session."""
    campaign = invoice_campaign.package(session, proposal["suite"])
    if (campaign["catalog"] != proposal["catalog"] or campaign["cases"] != proposal["cases"]
            or campaign["source_sha256"] != proposal["source_sha256"]
            or campaign["limits"] != proposal["limits"]["campaign"]):
        raise ValueError("campaign package differs from the authorized template")
    expires = min(employee_tool._utc(approval["expires_at"]),
                  now + timedelta(seconds=invoice_campaign.WINDOW))
    derived = {"approved": True, "authorization_id": keys(approval["authorization_id"])["campaign"],
               "package_sha256": invoice_campaign.digest(campaign),
               "issued_at": now.isoformat(), "expires_at": expires.isoformat()}
    return campaign, derived


def _campaign(path, state, campaign, derived, runner, wall):
    result = invoice_campaign.execute(campaign, derived, runner=runner, wall=wall)
    state.update(status="complete" if result["status"] == "complete" else "stopped",
                 campaign=result)
    invoice_campaign.write(path, state)
    return state


def execute(proposal, approval, cli_factory=employee_run.pinned_cli, clock=time.monotonic,
            sleep=time.sleep, wall=utc_now, runner=invoice_campaign.execute_case):
    binding, issued, expires = bind(proposal, approval)
    now = employee_tool._utc(wall())
    template = proposal["template"]
    home = private_directory(Path(template["state_dir"]) / "fixture-creations")
    path = home / (approval["authorization_id"] + ".json")
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(path.read_text()) if path.exists() else None
        if state is not None and (state.get("binding") != binding or state.get(
                "authorization_id") != approval["authorization_id"]):
            raise ValueError("journal belongs to another package or authorization")
        if state is not None and state.get("status") not in RECOVERABLE + ("fixture_ready",
                                                                           "campaign"):
            return state  # stopped or complete: never repeated
        if not issued <= now < expires:
            if state is not None and state.get("status") in RECOVERABLE:
                state.update(status="unresolved", reason="authorization expired before "
                             "recovery finished", next_action="owner decision under new "
                             "authority; a runtime may exist and keep running")
                invoice_campaign.write(path, state)
                return state
            raise ValueError("authorization window invalid or expired")
        if state is not None and state["status"] == "campaign":
            # Same derived approval: the campaign's own journal never duplicates a case.
            campaign, _ = handoff(proposal, approval, state["session"], now)
            return _campaign(path, state, campaign, state["campaign_authorization"], runner, wall)
        if state is None or state["status"] in RECOVERABLE:
            sanitizer = Sanitizer()
            cli = cli_factory(dict(template), sanitizer)
            store = Store(template["state_dir"], sanitizer)
            try:
                creation = Creation(proposal, approval, cli, store, path, clock, sleep, wall)
                if state is not None:
                    return creation.resume(state)
                session = creation.run(binding, expires)
                state = creation.state
            finally:
                store.close()
            if session is None:
                return state
        campaign, derived = handoff(proposal, approval, state["session"], now)
        state.update(status="campaign", campaign_authorization=derived)
        invoice_campaign.write(path, state)
        return _campaign(path, state, campaign, derived, runner, wall)


def run(args):
    try:
        if args.action == "prepare":
            template = json.loads(Path(args.template).expanduser().read_text())
            proposal = package(template, args.suite)
            invoice_campaign.write(Path(args.output).expanduser(), proposal)
            print("Offline package prepared. Authorization must bind SHA256: "
                  + invoice_campaign.digest(proposal))
            return 0
        proposal = json.loads(Path(args.package).expanduser().read_text())
        approval = json.loads(Path(args.authorization).expanduser().read_text())
        state = execute(proposal, approval)
        print(json.dumps({k: v for k, v in state.items() if k != "session"}, indent=2))
        return 0 if state.get("status") == "complete" else 2
    except (ValueError, KeyError, OSError, StateError, employee_run.Halt, BlockingIOError):
        print("Supplied-fixture attempt stopped. Inspect its journal and cleanup; no replay.")
        return 3
