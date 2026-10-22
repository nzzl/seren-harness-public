"""The bounded update route (`instruction-update`), against a disposable stand-in CLI.

SYNTHETIC: a local stand-in answers managed-get, cloud deployment status, managed-preview
and managed-update like the pinned CLI and logs every call; scenarios make the update stall,
apply, or be rejected. The real SerenCli wrapper runs it with the configured timeout. No
Seren or Discord service is involved.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import cli, execution_limits, instruction_update

DEPLOYMENT = "7a00003a-0000-4000-8000-00000000003b"
PACKAGE = SOURCE / "docs/lifecycle-setup/response-correctness.proposed.json"
STANDIN = r'''#!{python} -I
import json, os, sys, time
state_path = os.environ["STANDIN_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
if args == ["--version"]:
    print("seren 0.10.0 (simulation)"); sys.exit(0)
def parse_prompt(rest):
    """Like the pinned CLI's clap parser for `#[arg(long)] prompt` (no allow_hyphen_values):
    `--prompt=<v>` is taken verbatim; a separate value beginning with '-' is an unexpected
    argument (exit 2, before any request)."""
    prompt, i = None, 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("--prompt="):
            prompt, i = a[len("--prompt="):], i + 1
        elif a == "--prompt":
            if i + 1 >= len(rest) or rest[i + 1].startswith("-"):
                sys.stderr.write("error: unexpected argument '" + (rest[i + 1] if i + 1 < len(rest)
                                 else "") + "' found\n")
                sys.exit(2)
            prompt, i = rest[i + 1], i + 2
        elif rest[i:i + 2] == ["-o", "json"]:
            i += 2
        else:
            sys.stderr.write("error: unexpected argument '" + a + "' found\n")
            sys.exit(2)
    return prompt
if args[1] in ("managed-preview", "managed-update"):
    received = parse_prompt(args[3:])
    state.setdefault("received_prompts", []).append(received)
    json.dump(state, open(state_path, "w"))
with open(state_path + ".calls", "a") as f:
    f.write(json.dumps(args[:2] if args[1] != "cloud" else args[:4]) + "\n")
def save():
    json.dump(state, open(state_path, "w"))
def detail():
    return {"deployment_id": state["deployment"], "agent_identity_id": "7a000004-0000-4000-8000-000000000005",
            "active_revision_id": state["revision"], "status": "stopped",
            "secret_resolution_result_id": "s", "approval_policy": "read_only",
            "tool_refs": state["tool_refs"], "conditions": [{"type": "Ready", "status": "False"}],
            "bundle": {"instructions": [{"kind": "skill", "path": "SKILL.md",
                                         "content": state["content"]}]}}
kind = args[1]
if kind == "managed-get":
    print(json.dumps({"data": detail()}))
elif kind == "cloud":
    print(json.dumps({"data": {"id": state["deployment"], "status": "stopped",
                               "desired_lifecycle_state": "stopped", "control_generation": 7}}))
elif kind == "managed-preview":
    prompt = received.strip()  # the CLI trims before sending
    current = detail()
    proposed = json.loads(json.dumps(current))
    proposed["bundle"]["instructions"][0]["content"] = prompt.strip()
    changed = [{"field": "prompt", "label": "Instruction", "current_value": None,
                "proposed_value": None}]
    if state.get("preview_extra"):
        proposed["tool_refs"] = []
        changed.append({"field": "tool_refs", "label": "Tools", "current_value": None,
                        "proposed_value": None})
    print(json.dumps({"data": {"deployment_id": state["deployment"], "current": current,
                               "proposed": proposed, "changed_fields": changed}}))
elif kind == "managed-update":
    behaviour = state["update"]
    prompt = received.strip()
    if behaviour == "reject":
        sys.stderr.write("Error: Managed agent deployment update: unexpected response 400 Bad Request - {}\n")
        sys.exit(1)
    if behaviour in ("ok", "stall_applied", "stall_other"):
        state["revision"] = "11111111-2222-4333-8444-555555555555"
        state["content"] = prompt if behaviour != "stall_other" else "something else"
        save()
    if behaviour.startswith("stall"):
        open(state_path + ".pid", "w").write(str(os.getpid()))
        time.sleep(30)
    print(json.dumps({"data": {"deployment_id": state["deployment"]}}))
'''


class RouteBase(unittest.TestCase):
    """Shared stand-in setup and helpers; holds no tests."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("SKILL.current.md", "SKILL.corrected.md"):
            (self.root / "docs/lifecycle-setup").mkdir(parents=True, exist_ok=True)
            shutil.copy(SOURCE / "docs/lifecycle-setup" / name,
                        self.root / "docs/lifecycle-setup" / name)
        exe = self.root / "seren"
        exe.write_text(standin(STANDIN.replace("{python}", sys.executable)))
        exe.chmod(0o755)
        prerun(exe)  # first-run assessment outside the 1 s timed commands
        self.state = self.root / "standin.json"
        self.config = self.root / "config.json"
        pins = {k: v for k, v in execution_limits.PINNED.items()
                if k != "reply_timeout_seconds"}
        self.config.write_text(json.dumps({
            "environment": "simulation",
            "seren": dict(pins, executable=str(exe), command_timeout_seconds=1),
            "employee": {"key": "sim", "deployment_id": DEPLOYMENT},
            "discord": {"sender": "human", "observer": "discord_rest", "channel_id": "c",
                        "employee_user_id": "e", "reply_timeout_seconds": 120,
                        "expected_reply": {"template": "ACK {marker}", "source": "SYNTHETIC"}},
            "state_dir": "state"}))

    def scenario(self, update="ok", **extra):
        current = (self.root / "docs/lifecycle-setup/SKILL.current.md").read_text()
        self.state.write_text(json.dumps(dict({
            "deployment": DEPLOYMENT, "revision": "7a00002b-0000-4000-8000-00000000002c",
            "content": current, "tool_refs": [{"kind": "connector"}], "update": update},
            **extra)))

    def run_route(self, package=PACKAGE):
        out = io.StringIO()
        pinned = dict(execution_limits.PINNED, command_timeout_seconds=1)
        with mock.patch.dict(os.environ, {"STANDIN_STATE": str(self.state)}), \
                mock.patch.object(execution_limits, "PINNED", pinned), \
                contextlib.redirect_stdout(out):
            code = cli.main(["instruction-update", "--config", str(self.config), "--package",
                             str(package), "--root", str(self.root)])
        self.output = out.getvalue()
        return code

    def calls(self):
        path = Path(str(self.state) + ".calls")
        return [" ".join(json.loads(line)) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def journal(self):
        return json.loads(self.journal_path().read_text())

    def journal_path(self, package=PACKAGE):
        package_id = json.loads(Path(package).read_text())["id"]
        return self.root / "state/instruction-update" / package_id / "journal.json"


class UpdateRoute(RouteBase):
    def test_success_runs_the_exact_procedure_once(self):
        self.scenario("ok")
        self.assertEqual(self.run_route(), instruction_update.PROCEED, self.output)
        self.assertEqual(self.calls(), ["agent managed-get", "agent cloud deployment status",
                                        "agent managed-preview", "agent managed-get",
                                        "agent managed-update", "agent managed-get"])
        self.assertTrue(all(e["timeout_seconds"] == 1 for e in self.journal()["invocations"]))
        corrected = (self.root / "docs/lifecycle-setup/SKILL.corrected.md").read_text()
        self.assertTrue(corrected.startswith("---\n") and "\n\n" in corrected)
        received = json.loads(self.state.read_text())["received_prompts"]
        self.assertEqual(received, [corrected, corrected])  # preview and update, exact
        self.assertEqual(self.run_route(), instruction_update.PROCEED)  # nothing repeated
        self.assertEqual(len(self.calls()), 6)

    def test_a_stalled_update_is_killed_reaped_and_never_resent(self):
        self.scenario("stall")
        started = time.monotonic()
        self.assertEqual(self.run_route(), instruction_update.STOP, self.output)
        self.assertLess(time.monotonic() - started, 15)
        pid = int(Path(str(self.state) + ".pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)  # killed and reaped by the wrapper
        journal = self.journal()
        self.assertEqual(journal["update"]["status"], "timeout")
        self.assertEqual(journal["update"]["outcome"], "unknown")
        self.assertEqual(journal["readbacks"], instruction_update.MAX_READBACKS)
        self.assertEqual(self.calls().count("agent managed-update"), 1)
        self.assertEqual(len(self.calls()), instruction_update.MAX_INVOCATIONS)
        self.assertEqual(self.run_route(), instruction_update.STOP)  # a later invocation
        self.assertEqual(len(self.calls()), instruction_update.MAX_INVOCATIONS)
        self.assertIn("nothing is repeated", self.output)

    def test_an_unknown_outcome_is_settled_only_by_reads(self):
        self.scenario("stall_applied")
        self.assertEqual(self.run_route(), instruction_update.PROCEED, self.output)
        self.assertEqual(self.journal()["update"]["outcome"], "applied_verified")
        self.assertEqual(self.calls().count("agent managed-update"), 1)
        self.scenario("stall_other")
        Path(str(self.state) + ".calls").unlink()
        shutil.rmtree(self.root / "state")
        self.assertEqual(self.run_route(), instruction_update.STOP)
        self.assertIn("does not show exactly the corrected instruction",
                      self.journal()["reason"])

    def test_gates_stop_before_the_update(self):
        self.scenario("ok", preview_extra=True)
        self.assertEqual(self.run_route(), instruction_update.STOP)
        self.assertNotIn("agent managed-update", self.calls())
        self.assertEqual(self.journal()["observations"]["preview"]["changed_fields"],
                         ["prompt", "tool_refs"])
        self.assertEqual(self.run_route(), instruction_update.STOP)  # not retried
        self.assertEqual(len(self.calls()), 3)
        shutil.rmtree(self.root / "state")
        Path(str(self.state) + ".calls").unlink()
        self.scenario("ok", revision="00000000-0000-4000-8000-000000000000")
        self.assertEqual(self.run_route(), instruction_update.STOP)
        self.assertEqual(self.calls(), ["agent managed-get", "agent cloud deployment status"])

    def test_rejection_and_pin_mismatch(self):
        self.scenario("reject")
        self.assertEqual(self.run_route(), instruction_update.STOP)
        self.assertEqual(self.journal()["update"]["outcome"], "not_applied")
        self.assertEqual(self.calls()[-1], "agent managed-update")  # no read-back needed
        shutil.rmtree(self.root / "state")
        Path(str(self.state) + ".calls").unlink()
        config = json.loads(self.config.read_text())
        config["seren"]["max_apply_attempts"] = 2
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.run_route(), instruction_update.INVALID)
        self.assertEqual(self.calls(), [])

    def test_the_documented_route_is_this_command_and_its_timeout(self):
        package = json.loads(PACKAGE.read_text())
        route = package["update_route"]
        self.assertEqual(route["command"], "python -m seren_harness instruction-update "
                         "--config ~/seren-harness-live/config.json --package "
                         "docs/lifecycle-setup/response-correctness.proposed.json")
        self.assertEqual(route["timeout_seconds"], execution_limits.PINNED[
            "command_timeout_seconds"])
        doc = (SOURCE / "docs/lifecycle-setup/RESPONSE-CORRECTNESS.md").read_text()
        self.assertIn("python -m seren_harness instruction-update", doc)


class ResumeValidation(RouteBase):
    """An interrupted update (intent journaled, no outcome) with recovery reads remaining."""

    def bound_journal(self, **changes):
        from seren_harness.config import load
        with mock.patch.dict(os.environ, {"STANDIN_STATE": str(self.state)}), \
                mock.patch.object(execution_limits, "PINNED",
                                  dict(execution_limits.PINNED, command_timeout_seconds=1)):
            update = instruction_update.Update(load(self.config), PACKAGE, self.root)
            problem, binding = update.validate()
        self.assertIsNone(problem)
        journal = {"package": json.loads(PACKAGE.read_text())["id"], "deployment": DEPLOYMENT,
                   "binding": binding, "started_at": "2026-10-13T00:00:00Z",
                   "invocations": [{"purpose": p} for p in (
                       "prerequisite", "prerequisite", "preview", "pre-update", "update")],
                   "observations": {}, "update": {"status": "intent", "at": "t"},
                   "readbacks": 0, "result": None}
        journal.update(changes)
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(journal))
        return path

    def assertRefusedUntouched(self, path, package=PACKAGE):
        before = path.read_bytes()
        self.assertEqual(self.run_route(package), instruction_update.INVALID, self.output)
        self.assertIn("Refused before any call", self.output)
        self.assertEqual(self.calls(), [])
        self.assertEqual(path.read_bytes(), before)

    def test_unchanged_inputs_permit_only_the_remaining_reads(self):
        self.scenario("ok")  # read-backs show the old instruction: not visible or not applied
        path = self.bound_journal()
        self.assertEqual(self.run_route(), instruction_update.STOP, self.output)
        self.assertEqual(self.calls(), ["agent managed-get", "agent managed-get"])
        journal = json.loads(path.read_text())
        self.assertEqual((journal["update"]["outcome"], journal["readbacks"],
                          len(journal["invocations"])), ("unknown", 2, 7))
        self.assertEqual(self.run_route(), instruction_update.STOP)  # allowance used
        self.assertEqual(len(self.calls()), 2)
        self.assertNotIn("agent managed-update", self.calls())

    def test_a_visible_applied_update_is_verified_by_one_read(self):
        corrected = (self.root / "docs/lifecycle-setup/SKILL.corrected.md").read_text()
        self.scenario("ok", content=corrected.strip(),
                      revision="11111111-2222-4333-8444-555555555555")
        self.bound_journal()
        self.assertEqual(self.run_route(), instruction_update.PROCEED, self.output)
        self.assertEqual(self.calls(), ["agent managed-get"])

    def test_changed_inputs_are_refused_before_any_call(self):
        self.scenario("ok")
        path = self.bound_journal()
        config = json.loads(self.config.read_text())
        config["employee"]["names"] = ["changed"]  # any configuration change
        self.config.write_text(json.dumps(config))
        self.assertRefusedUntouched(path)
        self.assertIn("config_digest", self.output)
        config["employee"].pop("names")
        config["seren"]["command_timeout_seconds"] = 2  # a changed timeout
        self.config.write_text(json.dumps(config))
        self.assertRefusedUntouched(path)
        config["seren"]["command_timeout_seconds"] = 1
        self.config.write_text(json.dumps(config))
        exe = self.root / "seren"
        exe.write_text(exe.read_text() + "\n# changed\n")  # a different executable
        self.assertRefusedUntouched(path)
        self.assertIn("executable_sha256", self.output)

    def test_a_changed_package_or_artifact_is_refused(self):
        self.scenario("ok")
        path = self.bound_journal()
        package = json.loads(PACKAGE.read_text())
        package["limits"]["probes"] = 2
        other = self.root / "package.json"
        other.write_text(json.dumps(package))
        self.assertRefusedUntouched(path, other)
        self.assertIn("package_sha256", self.output)

    def test_a_completed_journal_does_not_carry_over_to_new_inputs(self):
        self.scenario("ok")
        path = self.bound_journal(result="proceed",
                                  update={"status": "ok", "outcome": "applied_verified"})
        self.assertEqual(self.run_route(), instruction_update.PROCEED)  # same inputs
        config = json.loads(self.config.read_text())
        config["state_dir"] = "state"
        config["employee"]["names"] = ["changed"]
        self.config.write_text(json.dumps(config))
        self.assertRefusedUntouched(path)

    def test_missing_or_incompatible_bindings_fail_closed(self):
        self.scenario("ok")
        path = self.bound_journal()
        journal = json.loads(path.read_text())
        journal.pop("binding")
        path.write_text(json.dumps(journal))
        self.assertRefusedUntouched(path)
        journal["binding"] = {"version": 0}
        path.write_text(json.dumps(journal))
        self.assertRefusedUntouched(path)
        path.write_text("{not json")
        self.assertRefusedUntouched(path)

    def test_an_exhausted_allowance_requests_nothing(self):
        self.scenario("ok")
        self.bound_journal(invocations=[{"purpose": "x"}] * instruction_update.MAX_INVOCATIONS)
        self.assertEqual(self.run_route(), instruction_update.STOP, self.output)
        self.assertEqual(self.calls(), [])


class ParserAndAttemptIsolation(RouteBase):
    def test_the_old_separate_argument_shape_is_rejected_before_any_activity(self):
        self.scenario("ok")
        corrected = (self.root / "docs/lifecycle-setup/SKILL.corrected.md").read_text()
        for kind in ("managed-preview", "managed-update"):
            done = subprocess.run([str(self.root / "seren"), "agent", kind, DEPLOYMENT,
                                   "--prompt", corrected, "-o", "json"], capture_output=True,
                                  text=True, env=dict(os.environ, STANDIN_STATE=str(self.state)))
            self.assertEqual(done.returncode, 2, kind)
            self.assertIn("unexpected argument '---", done.stderr)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("received_prompts", json.loads(self.state.read_text()))
        done = subprocess.run([str(self.root / "seren"), "agent", "managed-preview", DEPLOYMENT,
                               "--prompt=" + corrected, "-o", "json"], capture_output=True,
                              text=True, env=dict(os.environ, STANDIN_STATE=str(self.state)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(self.state.read_text())["received_prompts"], [corrected])

    def old_package(self):
        package = json.loads(PACKAGE.read_text())
        package.update(id="seren.lifecycle.response-correctness", version=3)
        path = self.root / "v3-package.json"
        path.write_text(json.dumps(package))
        return path

    def test_the_new_attempt_has_its_own_journal_and_inherits_nothing(self):
        old = self.old_package()
        self.scenario("ok", preview_extra=True)  # the old attempt stops at its preview gate
        self.assertEqual(self.run_route(old), instruction_update.STOP)
        old_journal = self.journal_path(old)
        frozen = old_journal.read_bytes()
        before = len(self.calls())
        self.assertEqual(self.run_route(old), instruction_update.STOP)  # stays stopped
        self.assertEqual(len(self.calls()), before)
        self.scenario("ok")
        self.assertEqual(self.run_route(), instruction_update.PROCEED, self.output)
        self.assertNotEqual(self.journal_path(), old_journal)
        self.assertEqual(len(self.journal()["invocations"]), 6)  # fresh allowance, full run
        self.assertEqual(old_journal.read_bytes(), frozen)  # untouched, byte for byte

    def test_an_old_success_or_exhausted_allowance_does_not_carry_over(self):
        old = self.old_package()
        path = self.journal_path(old)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"package": "seren.lifecycle.response-correctness",
                                    "result": "proceed", "binding": {"version": 1},
                                    "invocations": [{}] * instruction_update.MAX_INVOCATIONS}))
        frozen = path.read_bytes()
        self.scenario("ok")
        self.assertEqual(self.run_route(), instruction_update.PROCEED, self.output)
        self.assertEqual(self.calls().count("agent managed-update"), 1)  # really ran
        self.assertEqual(path.read_bytes(), frozen)


if __name__ == "__main__":
    unittest.main()
