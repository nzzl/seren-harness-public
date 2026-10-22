"""The bounded diagnostic preview (`preview-diagnostic`), against a disposable stand-in CLI.

SYNTHETIC: a local stand-in answers managed-get, cloud deployment status and managed-preview
from a scenario file, logs every command it receives (including any it does not support),
and can time out, fail with stderr, or print malformed output. The real SerenCli wrapper
runs it with the configured timeout. No Seren or Discord service is involved.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import cli, execution_limits, preview_diagnostic as pd
from seren_harness.seren_cli import SerenCli

DEPLOYMENT = "7a00003a-0000-4000-8000-00000000003b"
IDENTITY = "7a000004-0000-4000-8000-000000000005"
REVISION = "7a00002b-0000-4000-8000-00000000002c"
PACKAGE = SOURCE / "docs/lifecycle-setup/preview-diagnostic.proposed.json"
ALLOWED = {"agent managed-get", "agent cloud deployment status", "agent managed-preview"}
SENTINELS = ("zqSENTINELkey1", "zqSENTINELvalue1", "zqSENTINELvalue2", "ZQ_SENTINEL_SESSION_KEY",
             "zqSENTINEL label", "zqSENTINEL message", "zqSENTINEL stderr", "zqSENTINELfield",
             "zqSENTINELcfgfield")
STANDIN = r'''#!{python} -I
import json, os, sys, time
path = os.environ["STANDIN_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
if args == ["--version"]:
    print("seren 0.10.0 (simulation)"); sys.exit(0)
name = " ".join(args[:4] if args[1:2] == ["cloud"] else args[:2])
with open(path + ".calls", "a") as f:
    f.write(name + "\n")
kind = args[1]
fail = state.get("fail", {}).get(name)
if fail == "timeout":
    time.sleep(30)
if fail == "error":
    sys.stderr.write("Error: zqSENTINEL stderr 500\n"); sys.exit(1)
if fail == "malformed":
    print("not json zqSENTINEL stderr"); sys.exit(0)
if kind == "managed-get":
    print(json.dumps({"data": state["detail"]}))
elif kind == "cloud":
    print(json.dumps({"data": state["lifecycle"]}))
elif kind == "managed-preview":
    rest = args[3:]
    prompts = [a for a in rest if a.startswith("--prompt=")]
    if len(prompts) != 1 or len(rest) != 3 or rest[1:] != ["-o", "json"]:
        sys.stderr.write("error: unexpected argument\n"); sys.exit(2)
    state.setdefault("received", []).append(prompts[0][len("--prompt="):])
    print(json.dumps(state["preview"]))
    state["detail"] = state.get("after_preview", state["detail"])
    json.dump(state, open(path, "w"))
else:
    print("{}")
'''


def detail(content, config=None, secret_keys=None, **changes):
    value = {"deployment_id": DEPLOYMENT, "agent_identity_id": IDENTITY,
             "active_revision_id": REVISION, "status": "stopped",
             "secret_resolution_result_id": "srr-1", "approval_policy": "read_only",
             "tool_refs": [{"kind": "connector"}], "model_id": "m",
             "config": {"zqSENTINELkey1": {"zqSENTINELcfgfield": "zqSENTINELvalue1"}}
             if config is None else config,
             "secret_keys": ["DISCORD_BOT_TOKEN", "ZQ_SENTINEL_SESSION_KEY"]
             if secret_keys is None else secret_keys,
             "conditions": [{"type": "Ready", "status": "False", "message": "zqSENTINEL message"},
                            {"type": "zqSENTINELfield", "status": "True"}],
             "zqSENTINELfield": 1,
             "bundle": {"instructions": [{"kind": "skill", "path": "SKILL.md",
                                          "content": content}], "assets": []}}
    value.update(changes)
    return value


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "docs/lifecycle-setup").mkdir(parents=True)
        for name in ("SKILL.current.md", "SKILL.corrected.md"):
            shutil.copy(SOURCE / "docs/lifecycle-setup" / name,
                        self.root / "docs/lifecycle-setup" / name)
        self.original = (self.root / "docs/lifecycle-setup/SKILL.current.md").read_text()
        self.corrected = (self.root / "docs/lifecycle-setup/SKILL.corrected.md").read_text()
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
        self.outputs = []

    def scenario(self, current=None, proposed=None, pre=None, changed=None, preview=None,
                 **extra):
        current = detail(self.original) if current is None else current
        if proposed is None:
            proposed = copy.deepcopy(current)
            proposed["bundle"]["instructions"][0]["content"] = self.corrected.strip()
            proposed["active_revision_id"] = None
        changed = [{"field": "bundle", "label": "zqSENTINEL label", "current_value": {},
                    "proposed_value": {}},
                   {"field": "config.zqSENTINELkey1", "label": "Config",
                    "current_value": "zqSENTINELvalue1", "proposed_value": "zqSENTINELvalue2"},
                   {"field": "zqSENTINELfield", "label": "x", "current_value": 1,
                    "proposed_value": 2}] if changed is None else changed
        state = {"detail": copy.deepcopy(current) if pre is None else pre,
                 "lifecycle": {"id": DEPLOYMENT, "status": "stopped",
                               "desired_lifecycle_state": "stopped", "control_generation": 7},
                 "preview": {"data": {"deployment_id": DEPLOYMENT, "current": current,
                                      "proposed": proposed, "changed_fields": changed}}
                 if preview is None else preview}
        state.update(extra)
        self.state.write_text(json.dumps(state))
        Path(str(self.state) + ".calls").unlink(missing_ok=True)

    def run_diag(self, package=PACKAGE, cli_json=None):
        out = io.StringIO()
        pinned = dict(execution_limits.PINNED, command_timeout_seconds=1)
        patches = [mock.patch.dict(os.environ, {"STANDIN_STATE": str(self.state)}),
                   mock.patch.object(execution_limits, "PINNED", pinned),
                   contextlib.redirect_stdout(out)]
        if cli_json:
            patches.append(mock.patch.object(SerenCli, "json", cli_json))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            try:
                code = cli.main(["preview-diagnostic", "--config", str(self.config),
                                 "--package", str(package), "--root", str(self.root)])
            except KeyboardInterrupt:
                code = "interrupted"
        self.output = out.getvalue()
        self.outputs.append(self.output)
        self.assertLessEqual(set(self.calls()), ALLOWED)  # no other command on any path
        return code

    def calls(self):
        path = Path(str(self.state) + ".calls")
        return path.read_text().splitlines() if path.exists() else []

    def journal_path(self):
        return self.root / "state/preview-diagnostic/seren.lifecycle.preview-diagnostic.1" \
            / "journal.json"

    def journal(self):
        return json.loads(self.journal_path().read_text())

    def evidence(self):
        return self.journal()["observations"]

    def config_path(self, name):
        return next(p for p in self.evidence()["config"]["paths"] if p["path"] == name)


FULL = ["agent managed-get", "agent cloud deployment status", "agent managed-preview",
        "agent managed-get", "agent cloud deployment status"]


class Sequence(Base):
    def test_exact_five_call_sequence_once_with_the_pinned_timeout(self):
        self.scenario()
        self.assertEqual(self.run_diag(), pd.COMPLETE, self.output)
        self.assertEqual(self.calls(), FULL)
        journal = self.journal()
        self.assertEqual([e["purpose"] for e in journal["invocations"]],
                         ["pre-get", "pre-status", "preview", "post-get", "post-status"])
        self.assertTrue(all(e["timeout_seconds"] == 1 for e in journal["invocations"]))
        self.assertEqual(journal["allowance"], 5)
        self.assertEqual(json.loads(self.state.read_text())["received"], [self.corrected])
        self.assertEqual(self.run_diag(), pd.COMPLETE)  # nothing repeated
        self.assertEqual(len(self.calls()), 5)
        self.assertIn("already finished", self.output)

    def test_prerequisite_mismatch_prevents_the_preview(self):
        cases = {"revision": dict(pre=detail("x", active_revision_id="0" * 8 + REVISION[8:])),
                 "identity": dict(pre=detail("x", agent_identity_id="other")),
                 "running": dict(lifecycle={"status": "running",
                                            "desired_lifecycle_state": "running"})}
        for label, changes in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.root / "state", ignore_errors=True)
                if "pre" in changes:
                    changes["pre"]["bundle"]["instructions"][0]["content"] = self.original
                self.scenario(**changes)
                self.assertEqual(self.run_diag(), pd.STOP, self.output)
                self.assertEqual(self.calls(), FULL[:2])
                self.assertEqual(self.journal()["result"], "stopped_before_preview")
        shutil.rmtree(self.root / "state")
        self.scenario(pre=detail("an edited instruction"))
        self.assertEqual(self.run_diag(), pd.STOP)
        self.assertNotIn("agent managed-preview", self.calls())
        shutil.rmtree(self.root / "state")
        self.scenario(fail={"agent managed-get": "error"})
        self.assertEqual(self.run_diag(), pd.STOP)
        self.assertEqual(self.calls(), FULL[:2])

    def test_a_failed_or_timed_out_preview_still_gets_only_the_post_reads(self):
        for mode in ("timeout", "error", "malformed"):
            with self.subTest(mode):
                shutil.rmtree(self.root / "state", ignore_errors=True)
                self.scenario(fail={"agent managed-preview": mode})
                self.assertEqual(self.run_diag(), pd.INCOMPLETE, self.output)
                self.assertEqual(self.calls(), FULL)
                journal = self.journal()
                self.assertIn("preview", journal["unavailable"])
                self.assertEqual(journal["preview"]["status"],
                                 {"timeout": "timeout", "error": "error",
                                  "malformed": "decode_error"}[mode])
                self.assertTrue(journal["observations"]["post"]["detail_available"])
                self.assertEqual(journal["failures"]["preview"]["category"],
                                 {"timeout": "timeout", "error": "unknown",
                                  "malformed": "malformed_response"}[mode])
                self.assertEqual(self.run_diag(), pd.INCOMPLETE)  # no repeat
                self.assertEqual(len(self.calls()), 5)

    def test_an_undocumented_preview_shape_is_unavailable_not_dumped(self):
        self.scenario(preview={"data": "zqSENTINELvalue1"})
        self.assertEqual(self.run_diag(), pd.INCOMPLETE)
        self.assertEqual(self.calls(), FULL)
        self.assertEqual(self.journal()["unavailable"]["preview"], "undocumented shape")
        self.assertEqual(self.evidence()["views_available"],
                         {"pre": True, "current": False, "proposed": False, "post": True})

    def test_a_post_read_failure_is_recorded_and_the_last_read_still_used(self):
        self.scenario()
        state = json.loads(self.state.read_text())
        state["after_preview"] = "not a detail"
        self.state.write_text(json.dumps(state))
        self.assertEqual(self.run_diag(), pd.INCOMPLETE)
        self.assertEqual(self.calls(), FULL)
        self.assertEqual(self.journal()["unavailable"], {"post-get": "undocumented shape"})


class Resumption(Base):
    def interrupt_at(self, purpose):
        original = SerenCli.json

        def json_once(cli_self, *args):
            if tuple(args[:len(dict(pd.SEQUENCE)[purpose])]) == dict(pd.SEQUENCE)[purpose] \
                    and sum(1 for e in json.loads(self.journal_path().read_text())
                            ["invocations"] if e["purpose"] == purpose):
                raise KeyboardInterrupt
            return original(cli_self, *args)
        return json_once

    def test_an_interruption_at_the_preview_never_repeats_it(self):
        self.scenario()
        self.assertEqual(self.run_diag(cli_json=self.interrupt_at("preview")), "interrupted")
        self.assertEqual(self.calls(), FULL[:2])  # interrupted after its intent, before sending
        self.assertEqual(self.journal()["preview"]["status"], "intent")
        self.assertEqual(self.run_diag(), pd.INCOMPLETE, self.output)
        self.assertEqual(self.calls(), FULL[:2] + FULL[3:])  # only the post-preview reads
        journal = self.journal()
        self.assertEqual(journal["preview"]["status"], "interrupted")
        self.assertEqual(len(journal["invocations"]), 5)
        self.assertEqual(self.run_diag(), pd.INCOMPLETE)
        self.assertEqual(len(self.calls()), 4)

    def test_an_interruption_after_the_preview_keeps_its_evidence_and_uses_the_rest(self):
        self.scenario()
        self.assertEqual(self.run_diag(cli_json=self.interrupt_at("post-get")), "interrupted")
        self.assertEqual(self.calls(), FULL[:3])
        self.assertIn("config", self.evidence())  # preview evidence kept
        self.assertEqual(self.run_diag(), pd.INCOMPLETE)
        # A started step counts as used (it may have reached the service): only post-status.
        self.assertEqual(self.calls(), FULL[:3] + FULL[4:])
        self.assertEqual(set(self.journal()["unavailable"]), {"post-get"})
        self.assertTrue(self.evidence()["post"]["status_available"])

    def test_an_interruption_before_the_preview_ends_without_it(self):
        self.scenario()
        self.assertEqual(self.run_diag(cli_json=self.interrupt_at("pre-status")), "interrupted")
        self.assertEqual(self.run_diag(), pd.STOP)
        self.assertEqual(self.calls(), FULL[:1])
        self.assertEqual(self.journal()["result"], "stopped_before_preview")

    def test_changed_inputs_are_refused_before_any_call(self):
        self.scenario(fail={"agent managed-preview": "error"})
        self.run_diag(cli_json=self.interrupt_at("post-get"))
        frozen = self.journal_path().read_bytes()
        before = len(self.calls())
        config = json.loads(self.config.read_text())
        config["employee"]["names"] = ["changed"]
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.run_diag(), pd.INVALID)
        self.assertIn("config_digest", self.output)
        config["employee"].pop("names")
        self.config.write_text(json.dumps(config))
        package = json.loads(PACKAGE.read_text())
        package["limits"]["window_minutes"] = 99
        other = self.root / "package.json"
        other.write_text(json.dumps(package))
        self.assertEqual(self.run_diag(other), pd.INVALID)
        self.assertIn("package_sha256", self.output)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(self.journal_path().read_bytes(), frozen)

    def test_steps_cannot_repeat_and_unlisted_commands_do_not_exist(self):
        self.scenario()
        self.run_diag()
        from seren_harness.config import load
        diagnostic = pd.Diagnostic(load(self.config), PACKAGE, self.root)
        diagnostic.journal = self.journal()
        for purpose in ("preview", "pre-get"):
            with self.assertRaises(RuntimeError):
                diagnostic.call(purpose)
        with self.assertRaises(KeyError):
            diagnostic.call("update")
        self.assertEqual({command for _, command in pd.SEQUENCE}, {pd.GET, pd.STATUS, pd.PREVIEW})
        source = Path(pd.__file__).read_text()
        imports = "\n".join(re.findall(r"^(?:from|import) .*(?:\n    .*)*", source, re.M))
        for name in ("discord", "seren_harness.seren ", "provision", "Update", "journey"):
            self.assertNotIn(name, imports)
        for word in ('"managed-update"', '"managed-start"', '"managed-stop"', '"passwords',
                     '"managed-passwords'):
            self.assertNotIn(word, source)


class Isolation(Base):
    def test_previous_journals_stay_byte_identical(self):
        old = {}
        for package_id in ("seren.lifecycle.response-correctness",
                           "seren.lifecycle.response-correctness.attempt-2"):
            path = self.root / "state/instruction-update" / package_id / "journal.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"package": package_id, "result": "stopped",
                                        "invocations": [{}] * 3}))
            old[path] = path.read_bytes()
        self.scenario()
        self.assertEqual(self.run_diag(), pd.COMPLETE)
        self.assertNotEqual(self.journal_path().parent.parent.name, "instruction-update")
        for path, content in old.items():
            self.assertEqual(path.read_bytes(), content)


class StructuralScenarios(Base):
    def test_omission_and_addition(self):
        current = detail(self.original)
        proposed = copy.deepcopy(current)
        proposed["bundle"]["instructions"][0]["content"] = self.corrected.strip()
        proposed["config"] = {"zqSENTINELfield": "zqSENTINELvalue2"}
        proposed["secret_keys"] = ["ZQ_SENTINEL_SESSION_KEY", "zqSENTINELfield"]
        self.scenario(current, proposed)
        self.assertEqual(self.run_diag(), pd.COMPLETE)
        types = [p["type"] for p in self.evidence()["config"]["paths"]]
        self.assertIn({"pre": "string", "current": "string", "proposed": "absent",
                       "post": "string"}, types)
        self.assertIn({"pre": "absent", "current": "absent", "proposed": "string",
                       "post": "absent"}, types)
        keys = self.evidence()["secret_keys"]
        self.assertEqual(keys["pairs"]["current~proposed"],
                         {"comparable": True, "equal": False, "same_set": False, "added": 1,
                          "removed": 1, "order_only": False})
        self.assertTrue(keys["views"]["current"]["contains_discord_bot_token"])
        self.assertFalse(keys["views"]["proposed"]["contains_discord_bot_token"])

    def test_changed_values_with_unchanged_keys(self):
        current = detail(self.original)
        proposed = copy.deepcopy(current)
        proposed["bundle"]["instructions"][0]["content"] = self.corrected.strip()
        proposed["config"]["zqSENTINELkey1"]["zqSENTINELcfgfield"] = "zqSENTINELvalue2"
        self.scenario(current, proposed)
        self.assertEqual(self.run_diag(), pd.COMPLETE)
        path = self.config_path("config.k1.k1")
        self.assertEqual(path["type"]["current"], path["type"]["proposed"])
        self.assertEqual(path["equal"], {"pre~current": True, "current~proposed": False,
                                         "pre~post": True, "proposed~post": False})
        self.assertTrue(self.evidence()["secret_keys"]["pairs"]["current~proposed"]["same_set"])
        fields = self.evidence()["fields"]
        self.assertEqual(fields["current_vs_proposed_differing_fields"],
                         ["bundle", "config"])
        self.assertFalse(fields["equal"]["config"]["current~proposed"])
        self.assertTrue(fields["equal"]["secret_resolution_result_id"]["current~proposed"])
        bundle = self.evidence()["bundle"]
        self.assertTrue(bundle["views"]["proposed"]["main_is_corrected"])
        self.assertEqual(bundle["pairs"]["current~proposed"]["main_fields_differing"],
                         ["content"])
        self.assertTrue(bundle["pairs"]["current~proposed"]["assets_equal"])

    def test_current_view_mismatch(self):
        pre = detail(self.original)
        current = detail(self.original, config={"zqSENTINELkey1": {}})
        self.scenario(current, pre=pre)
        self.assertEqual(self.run_diag(), pd.COMPLETE)
        self.assertFalse(self.evidence()["config"]["equal"]["pre~current"])
        self.assertTrue(self.evidence()["config"]["equal"]["current~proposed"])

    def test_order_only_differences(self):
        current = detail(self.original)
        proposed = copy.deepcopy(current)
        proposed["secret_keys"] = list(reversed(current["secret_keys"]))
        self.scenario(current, proposed)
        self.assertEqual(self.run_diag(), pd.COMPLETE)
        pair = self.evidence()["secret_keys"]["pairs"]["current~proposed"]
        self.assertTrue(pair["order_only"] and pair["same_set"] and not pair["equal"])

    def test_unclassifiable_data_is_typed_not_dumped(self):
        current = detail(self.original)
        proposed = copy.deepcopy(current)
        proposed["config"] = "zqSENTINELvalue2"
        proposed["secret_keys"] = {"zqSENTINELfield": "zqSENTINELvalue1"}
        self.scenario(current, proposed, changed={"zqSENTINELfield": 1})
        self.assertEqual(self.run_diag(), pd.COMPLETE)
        evidence = self.evidence()
        self.assertEqual(evidence["config"]["type"]["proposed"], "string")
        self.assertEqual(evidence["secret_keys"]["views"]["proposed"], {"type": "object"})
        self.assertFalse(evidence["secret_keys"]["pairs"]["current~proposed"]["comparable"])
        self.assertEqual(evidence["changed_fields"], {"type": "object"})

    def test_changed_fields_keep_only_allowlisted_names(self):
        self.scenario()
        self.run_diag()
        entries = self.evidence()["changed_fields"]["entries"]
        self.assertEqual([e["field"] for e in entries],
                         ["bundle", "config.<withheld>", "<withheld>"])
        self.assertTrue(all(e["label_present"] for e in entries))

    def test_a_post_preview_change_is_reported_as_an_observation(self):
        after = detail(self.original, active_revision_id="11111111-2222-4333-8444-555555555555")
        self.scenario(after_preview=after)
        self.assertEqual(self.run_diag(), pd.COMPLETE)
        change = self.evidence()["post_preview_change"]
        self.assertFalse(change["active_revision_id"])
        self.assertTrue(change["approval_applied"])
        self.assertEqual(self.evidence()["post"]["active_revision_id"],
                         "11111111-2222-4333-8444-555555555555")
        self.assertIn("differs in: active_revision_id", self.output)
        self.assertEqual(self.calls(), FULL)  # reported, nothing repaired


class NoSecretsPersisted(Base):
    HEX = re.compile(r"\b[0-9a-f]{32,64}\b")

    def digests(self, text):
        data = text.encode()
        return {hashlib.sha256(data).hexdigest(), hashlib.sha1(data).hexdigest(),
                hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()[:16]}

    def test_sentinels_never_reach_any_artifact_or_output(self):
        runs = [dict(), dict(fail={"agent managed-preview": "error"}),
                dict(fail={"agent managed-preview": "malformed"})]
        for index, extra in enumerate(runs):
            shutil.rmtree(self.root / "state", ignore_errors=True)
            current = detail(self.original)
            proposed = copy.deepcopy(current)
            proposed["config"] = {"zqSENTINELkey1": "zqSENTINELvalue2"}
            self.scenario(current, proposed, **extra)
            self.run_diag()
            texts = [p.read_text() for p in (self.root / "state").rglob("*") if p.is_file()]
            texts += self.outputs
            raw = json.loads(self.state.read_text())
            forbidden = set(SENTINELS)
            for value in (raw["detail"], raw["preview"], raw["detail"]["config"],
                          raw["detail"]["secret_keys"]):
                for form in (json.dumps(value), json.dumps(value, sort_keys=True)):
                    forbidden |= self.digests(form)
            for sentinel in SENTINELS:
                forbidden |= self.digests(sentinel)
            binding = {v for v in self.journal()["binding"].values() if not isinstance(v, dict)}
            for text in texts:
                for item in forbidden:
                    self.assertNotIn(item, text, (index, item))
                self.assertNotIn("sentinel", text.lower(), index)
                for digest in self.HEX.findall(text):
                    self.assertIn(digest, binding, (index, "unexpected digest"))
            self.assertNotIn("response_sha256", texts[0])
            self.assertNotIn("stderr", texts[0])


class Package(unittest.TestCase):
    def test_the_package_matches_the_command(self):
        package = json.loads(PACKAGE.read_text())
        self.assertEqual(package["kind"], "preview_diagnostic_package")
        self.assertEqual(package["command"], "python -m seren_harness preview-diagnostic "
                         "--config ~/seren-harness-live/config.json --package "
                         "docs/lifecycle-setup/preview-diagnostic.proposed.json")
        self.assertEqual(package["limits"]["service_invocations"], pd.MAX_INVOCATIONS)
        self.assertEqual(len(package["sequence"]), pd.MAX_INVOCATIONS)
        self.assertEqual(package["limits"]["timeout_seconds"],
                         execution_limits.PINNED["command_timeout_seconds"])
        update = json.loads((SOURCE / "docs/lifecycle-setup/"
                             "response-correctness.proposed.json").read_text())
        self.assertNotEqual(package["id"], update["id"])
        self.assertEqual(package["correction"]["corrected_sha256"],
                         update["correction"]["corrected_sha256"])
        self.assertEqual(package["baseline"]["active_revision_id"],
                         update["baseline"]["active_revision_id"])


if __name__ == "__main__":
    unittest.main()
