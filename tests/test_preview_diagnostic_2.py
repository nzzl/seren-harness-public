"""Preview diagnostic 2: the same route under the explicit CLI session (binding version 2).

SYNTHETIC: the stand-in CLI of test_preview_diagnostic, extended to record only
XDG_CONFIG_HOME, SEREN_PROFILE and whether SEREN_API_KEY is present. Every run inherits
conflicting session variables and an API key. Session roots are temporary paths that are
never created. The sequence, resumption and no-secrets tests are reused unchanged against
the new package. No Seren or Discord service is involved.
"""
import glob
import hashlib
import json
import os
import sys
from pathlib import Path
import unittest
from unittest import mock

from seren_support import SOURCE
import test_preview_diagnostic as base
from seren_harness import execution_limits, instruction_update, preview_diagnostic as pd

PACKAGE_1 = base.PACKAGE
PACKAGE_2 = SOURCE / "docs/lifecycle-setup/preview-diagnostic-2.proposed.json"
CALL_LOG = '    f.write(name + "\\n")\n'
ENV_LOG = CALL_LOG + (
    'with open(path + ".env", "a") as f:\n'
    '    f.write(json.dumps({"XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),\n'
    '                        "SEREN_PROFILE": os.environ.get("SEREN_PROFILE"),\n'
    '                        "SEREN_API_KEY_present": "SEREN_API_KEY" in os.environ}) + "\\n")\n')
assert CALL_LOG in base.STANDIN
STANDIN = base.STANDIN.replace(CALL_LOG, ENV_LOG)
INHERITED = {"XDG_CONFIG_HOME": "/elsewhere/config", "SEREN_PROFILE": "other",
             "SEREN_API_KEY": "zqSENTINELapikey"}


class Base(base.Base):
    def setUp(self):
        super().setUp()
        self.exe = self.root / "seren"
        self.exe.write_text(STANDIN.replace("{python}", sys.executable))
        self.home = str(self.root / "never-created" / "config")
        self.write_config(config_home=self.home, profile="default")

    def write_config(self, environment="simulation", **seren):
        data = json.loads(self.config.read_text())
        data["environment"] = environment
        for key in ("config_home", "profile", "executable_sha256"):
            data["seren"].pop(key, None)
        data["seren"].update(seren)
        if environment == "live":
            data["seren"]["executable_sha256"] = hashlib.sha256(
                self.exe.read_bytes()).hexdigest()
        self.config.write_text(json.dumps(data))

    def run_diag(self, package=PACKAGE_2, cli_json=None, environ=None):
        with mock.patch.dict(os.environ, dict(INHERITED, **(environ or {}))):
            before = dict(os.environ)
            code = super().run_diag(package, cli_json)
            self.assertEqual(dict(os.environ), before)  # the parent is never changed
        self.assertFalse((self.root / "never-created").exists())
        return code

    def journal_path(self, package_id="seren.lifecycle.preview-diagnostic.2"):
        return self.root / "state/preview-diagnostic" / package_id / "journal.json"

    def envs(self):
        path = Path(str(self.state) + ".env")
        return [json.loads(line) for line in path.read_text().splitlines()] \
            if path.exists() else []


# The existing route tests, unchanged, against package 2 under the explicit session.
class Sequence(Base, base.Sequence):
    pass


class Resumption(Base, base.Resumption):
    def test_changed_inputs_are_refused_before_any_call(self):
        """Replaced by Binding below, which builds on package 2 instead of package 1."""


class NoSecretsPersisted(Base, base.NoSecretsPersisted):
    pass


class Session(Base):
    def test_explicit_selection_reaches_every_child_over_conflicting_inherited_values(self):
        self.scenario()
        self.assertEqual(self.run_diag(), pd.COMPLETE, self.output)
        self.assertEqual(self.calls(), base.FULL)
        self.assertEqual(self.envs(), [{"XDG_CONFIG_HOME": self.home, "SEREN_PROFILE":
                                        "default", "SEREN_API_KEY_present": False}] * 5)
        binding = self.journal()["binding"]
        self.assertEqual(binding["version"], instruction_update.BINDING_VERSION)
        self.assertEqual(binding["cli_session"], {
            "config_home": self.home, "config_home_source": "explicit",
            "profile": "default", "profile_source": "explicit"})
        self.assertEqual(self.journal()["package"], "seren.lifecycle.preview-diagnostic.2")

    def test_a_live_run_without_explicit_selection_is_refused_before_any_call(self):
        self.scenario()
        for seren in ({}, {"config_home": self.home}, {"profile": "default"}):
            with self.subTest(seren=seren):
                self.write_config("live", **seren)
                self.assertEqual(self.run_diag(), pd.INVALID)
                self.assertIn("config.seren.config_home and config.seren.profile", self.output)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.journal_path().exists())
        self.write_config("live", config_home=self.home, profile="default")
        self.assertEqual(self.run_diag(), pd.COMPLETE, self.output)
        self.assertEqual(len(self.calls()), 5)


class Binding(Base):
    interrupt_at = base.Resumption.interrupt_at

    def test_changed_session_or_inputs_are_refused_before_any_call(self):
        self.scenario(fail={"agent managed-preview": "error"})
        self.run_diag(cli_json=self.interrupt_at("post-get"))
        frozen, before = self.journal_path().read_bytes(), len(self.calls())
        for seren, environ in (({"config_home": self.home + "-b", "profile": "default"}, {}),
                               ({"config_home": self.home, "profile": "other"}, {}),
                               ({}, {"XDG_CONFIG_HOME": self.home})):
            with self.subTest(seren=seren):
                self.write_config(**seren)
                self.assertEqual(self.run_diag(environ=environ), pd.INVALID)
                self.assertIn("cli_session", self.output)
        self.write_config(config_home=self.home, profile="default")
        package = json.loads(PACKAGE_2.read_text())
        package["limits"]["window_minutes"] = 99
        other = self.root / "package.json"
        other.write_text(json.dumps(package))
        self.assertEqual(self.run_diag(other), pd.INVALID)
        self.assertIn("package_sha256", self.output)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(self.journal_path().read_bytes(), frozen)

    def test_old_or_missing_bindings_fail_safely_and_stay_unchanged(self):
        self.scenario()
        path = self.journal_path()
        path.parent.mkdir(parents=True)
        for journal in ({"binding": {"version": 1}, "invocations": [], "result": None},
                        {"invocations": [], "result": None}):
            path.write_text(json.dumps(journal))
            frozen = path.read_bytes()
            self.assertEqual(self.run_diag(), pd.INVALID)
            self.assertIn("no compatible input binding", self.output)
            self.assertEqual(self.calls(), [])
            self.assertEqual(path.read_bytes(), frozen)


class Isolation(Base):
    def test_a_distinct_journal_that_adopts_and_overwrites_no_earlier_evidence(self):
        state = self.root / "state"
        old = {}
        for relative, journal in (
                ("preview-diagnostic/seren.lifecycle.preview-diagnostic.1",
                 {"binding": {"version": 1}, "result": "stopped_before_preview",
                  "invocations": [{"purpose": "pre-get"}, {"purpose": "pre-status"}]}),
                ("prerequisite-read/seren.lifecycle.prerequisite-read.1",
                 {"binding": {"version": 1}, "result": "not_read"}),
                ("prerequisite-read/seren.lifecycle.prerequisite-read.2",
                 {"binding": {"version": 2}, "result": "read_matches_fixture"}),
                ("instruction-update/seren.lifecycle.response-correctness",
                 {"result": "stopped"}),
                ("instruction-update/seren.lifecycle.response-correctness.attempt-2",
                 {"result": "stopped"})):
            path = state / relative / "journal.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(journal))
            old[path] = path.read_bytes()
        self.scenario()
        self.assertEqual(self.run_diag(PACKAGE_1), pd.INVALID)  # the old identity: not adopted
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_diag(), pd.COMPLETE, self.output)  # fresh, own allowance
        self.assertEqual(self.calls(), base.FULL)
        self.assertEqual(self.run_diag(), pd.COMPLETE)  # never replayed
        self.assertEqual(len(self.calls()), 5)
        self.assertNotIn(self.journal_path(), old)
        for path, content in old.items():
            self.assertEqual(path.read_bytes(), content)


class Package(unittest.TestCase):
    def test_the_package_matches_the_route_and_its_source_backed_limits(self):
        text = PACKAGE_2.read_text()
        package = json.loads(text)
        first = json.loads(PACKAGE_1.read_text())
        self.assertEqual((package["kind"], package["id"], package["version"]),
                         ("preview_diagnostic_package", "seren.lifecycle.preview-diagnostic.2",
                          1))
        self.assertEqual(package["command"], "python -m seren_harness preview-diagnostic "
                         "--config ~/seren-harness-live/config.json --package "
                         "docs/lifecycle-setup/preview-diagnostic-2.proposed.json")
        for key in ("target", "correction"):
            self.assertEqual(package[key], first[key])
        for key in ("recorded_by_run", "active_revision_id", "agent_identity_id"):
            self.assertEqual(package["baseline"][key], first["baseline"][key])
        self.assertEqual(package["sequence"], first["sequence"])
        self.assertEqual([" ".join(c) for _, c in pd.SEQUENCE],
                         [" ".join(step.split()[:4 if " cloud " in step else 2])
                          for step in package["sequence"]])
        limits = package["limits"]
        n = limits["service_invocations"]
        self.assertEqual(n, pd.MAX_INVOCATIONS)
        self.assertEqual(limits["managed_preview"],
                         sum(1 for _, c in pd.SEQUENCE if c == pd.PREVIEW))
        self.assertEqual(limits["timeout_seconds"],
                         execution_limits.PINNED["command_timeout_seconds"])
        self.assertEqual((limits["retries"], limits["harness_mutations"]), (0, 0))
        # Source accounting: one optional refresh, one request, reqwest's 10 redirects.
        self.assertEqual(limits["http_upper_per_invocation"], 1 + 1 + 10)
        self.assertEqual(limits["http_upper"], n * limits["http_upper_per_invocation"])
        self.assertEqual(limits["oauth_refresh_upper"], n)
        self.assertEqual(limits["preview_body_transmissions_upper"], 1 + 10)
        self.assertEqual(limits["wall_seconds_upper"], (n + 1) * (limits["timeout_seconds"] + 2))
        others = [json.loads(Path(p).read_text()).get("id") for p in
                  glob.glob(str(SOURCE / "docs/lifecycle-setup/*.proposed.json"))
                  if Path(p) != PACKAGE_2]
        self.assertNotIn(package["id"], others)
        for private in ("example-research-app", "Documents/", ".data/seren-cli", "Codex/"):
            self.assertNotIn(private, text)  # the session root stays in private records


if __name__ == "__main__":
    unittest.main()
