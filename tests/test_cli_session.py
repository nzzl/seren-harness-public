"""Explicit CLI session selection (seren.config_home / seren.profile) and its binding.

SYNTHETIC: configurations, environments and a disposable stand-in CLI that records only
XDG_CONFIG_HOME, SEREN_PROFILE and whether SEREN_API_KEY is present. Session roots are
temporary paths that are never created. No Seren or Discord service is involved.
"""
import builtins
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import cli, execution_limits, instruction_update, prerequisite_read as pr
from seren_harness.config import Config, ConfigError

DEPLOYMENT = "7a00003a-0000-4000-8000-00000000003b"
PACKAGE_1 = SOURCE / "docs/lifecycle-setup/prerequisite-read.proposed.json"
PACKAGE_2 = SOURCE / "docs/lifecycle-setup/prerequisite-read-2.proposed.json"
STANDIN = r'''#!{python} -I
import json, os, sys
path = os.environ["STANDIN_STATE"]
args = sys.argv[1:]
if args == ["--version"]:
    print("seren 0.10.0 (simulation)"); sys.exit(0)
seen = {"XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),
        "SEREN_PROFILE": os.environ.get("SEREN_PROFILE"),
        "SEREN_API_KEY_present": "SEREN_API_KEY" in os.environ}
with open(path + ".calls", "a") as f:
    f.write(json.dumps({"command": " ".join(args[:2]), "env": seen}) + "\n")
original = open(os.environ["STANDIN_ORIGINAL"]).read()
print(json.dumps({"data": {"deployment_id": "%s", "agent_identity_id":
      "7a000004-0000-4000-8000-000000000005", "active_revision_id":
      "7a00002b-0000-4000-8000-00000000002c", "status": "stopped",
      "secret_resolution_result_id": "s", "bundle": {"instructions": [
      {"kind": "skill", "content": original}]}}}))
''' % DEPLOYMENT


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "docs/lifecycle-setup").mkdir(parents=True)
        for name in ("SKILL.current.md", "SKILL.corrected.md"):
            shutil.copy(SOURCE / "docs/lifecycle-setup" / name,
                        self.root / "docs/lifecycle-setup" / name)
        self.exe = self.root / "seren"
        self.exe.write_text(standin(STANDIN.replace("{python}", sys.executable)))
        self.exe.chmod(0o755)
        prerun(self.exe)  # first-run assessment outside the 1 s timed commands
        self.state = self.root / "standin"
        # Session roots that must never be created by validation or the harness.
        self.home_a = str(self.root / "never-created-a" / "config")
        self.home_b = str(self.root / "never-created-b" / "config")
        self.config = self.root / "config.json"
        self.write_config()

    def write_config(self, environment="simulation", **seren):
        pins = {k: v for k, v in execution_limits.PINNED.items()
                if k != "reply_timeout_seconds"}
        data = dict(pins, executable=str(self.exe), command_timeout_seconds=1, **seren)
        if environment == "live":
            data["executable_sha256"] = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        self.config.write_text(json.dumps({
            "environment": environment, "seren": data,
            "employee": {"key": "sim", "deployment_id": DEPLOYMENT},
            "discord": {"sender": "human", "observer": "discord_rest", "channel_id": "c",
                        "employee_user_id": "e", "reply_timeout_seconds": 120,
                        "expected_reply": {"template": "ACK {marker}", "source": "SYNTHETIC"}},
            "state_dir": "state"}))

    def run_read(self, package=PACKAGE_2, environ=None):
        out = io.StringIO()
        env = {"STANDIN_STATE": str(self.state),
               "STANDIN_ORIGINAL": str(self.root / "docs/lifecycle-setup/SKILL.current.md")}
        env.update(environ or {})
        pinned = dict(execution_limits.PINNED, command_timeout_seconds=1)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(execution_limits, "PINNED", pinned), \
                contextlib.redirect_stdout(out):
            code = cli.main(["prerequisite-read", "--config", str(self.config), "--package",
                             str(package), "--root", str(self.root)])
        self.output = out.getvalue()
        return code

    def calls(self):
        path = Path(str(self.state) + ".calls")
        return [json.loads(line) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def journal_path(self, package_id="seren.lifecycle.prerequisite-read.2"):
        return self.root / "state/prerequisite-read" / package_id / "journal.json"

    def assertNothingCreated(self):
        self.assertFalse((self.root / "never-created-a").exists())
        self.assertFalse((self.root / "never-created-b").exists())


class Validation(unittest.TestCase):
    def config(self, **seren):
        return Config({"environment": "simulation",
                       "seren": dict({"executable": "/bin/true"}, **seren),
                       "employee": {"key": "k", "deployment_id": "d"}})

    def test_valid_selection(self):
        config = self.config(config_home="/tmp/x/config", profile="default")
        self.assertEqual((config.config_home, config.profile), ("/tmp/x/config", "default"))
        for profile in ("default", "dev-2", "a.b_c", "A" * 64):
            self.assertEqual(self.config(profile=profile).profile, profile)

    def test_invalid_roots_and_profiles_are_refused(self):
        for home in ("relative/config", "~/.config", "/a/../b", "/a/", "/a//b", "", 7,
                     "/a\x00b"):
            with self.subTest(home=home), self.assertRaises(ConfigError):
                self.config(config_home=home)
        for profile in ("", " default", "default ", "../x", "a/b", ".", "..", "-x", "A" * 65,
                        3, "a b"):
            with self.subTest(profile=profile), self.assertRaises(ConfigError):
                self.config(profile=profile)

    def test_inherited_resolution_mirrors_the_pinned_cli(self):
        config = self.config()
        cases = [({"HOME": "/h"}, ("/h/.config", "inherited_default", "default",
                                    "inherited_default")),
                 ({"HOME": "/h", "XDG_CONFIG_HOME": "/x"}, ("/x", "inherited", "default",
                                                            "inherited_default")),
                 ({"HOME": "/h", "XDG_CONFIG_HOME": "rel"}, ("/h/.config", "inherited_default",
                                                             "default", "inherited_default")),
                 ({"HOME": "/h", "SEREN_PROFILE": "  dev "}, ("/h/.config",
                                                             "inherited_default", "dev",
                                                             "inherited")),
                 ({"HOME": "/h", "SEREN_PROFILE": "   "}, ("/h/.config", "inherited_default",
                                                           "default", "inherited_default")),
                 ({}, (None, "unresolved", "default", "inherited_default"))]
        for env, expected in cases:
            with self.subTest(env=env):
                found = config.cli_session(env)
                self.assertEqual((found["config_home"], found["config_home_source"],
                                  found["profile"], found["profile_source"]), expected)
        explicit = self.config(config_home="/c", profile="p").cli_session(
            {"HOME": "/h", "XDG_CONFIG_HOME": "/x", "SEREN_PROFILE": "q"})
        self.assertEqual((explicit["config_home"], explicit["profile"],
                          explicit["config_home_source"], explicit["profile_source"]),
                         ("/c", "p", "explicit", "explicit"))

    def test_child_environment_and_parent_unchanged(self):
        self.assertIsNone(self.config().cli_env())  # documented inherited fallback
        config = self.config(config_home="/c", profile="p")
        inherited = {"XDG_CONFIG_HOME": "/elsewhere", "SEREN_PROFILE": "other",
                     "SEREN_API_KEY": "zqSENTINELkey", "UNRELATED_KEEP": "1"}
        with mock.patch.dict(os.environ, inherited):
            before = dict(os.environ)
            env = config.cli_env()
            self.assertEqual(dict(os.environ), before)  # parent untouched
        self.assertEqual((env["XDG_CONFIG_HOME"], env["SEREN_PROFILE"]), ("/c", "p"))
        self.assertNotIn("SEREN_API_KEY", env)
        self.assertEqual(env["UNRELATED_KEEP"], "1")


class Route(Base):
    def test_explicit_selection_reaches_the_child_over_conflicting_inherited_values(self):
        self.write_config(config_home=self.home_a, profile="default")
        conflicting = {"XDG_CONFIG_HOME": "/elsewhere", "SEREN_PROFILE": "other",
                       "SEREN_API_KEY": "zqSENTINELkey"}
        with mock.patch.dict(os.environ, conflicting):
            before = dict(os.environ)
            self.assertEqual(self.run_read(), pr.READ_MATCHES, self.output)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual([c["env"] for c in self.calls()], [
            {"XDG_CONFIG_HOME": self.home_a, "SEREN_PROFILE": "default",
             "SEREN_API_KEY_present": False}])
        binding = json.loads(self.journal_path().read_text())["binding"]
        self.assertEqual(binding["version"], instruction_update.BINDING_VERSION)
        self.assertEqual(binding["cli_session"], {
            "config_home": self.home_a, "config_home_source": "explicit",
            "profile": "default", "profile_source": "explicit"})
        self.assertNotIn("zqSENTINEL", self.journal_path().read_text() + self.output)
        self.assertNothingCreated()

    def test_a_live_run_without_explicit_selection_is_refused_before_any_call(self):
        for seren in ({}, {"config_home": self.home_a}, {"profile": "default"}):
            with self.subTest(seren=seren):
                self.write_config("live", **seren)
                self.assertEqual(self.run_read(), pr.INVALID)
                self.assertIn("config.seren.config_home and config.seren.profile", self.output)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.journal_path().exists())
        self.write_config("live", config_home=self.home_a, profile="default")
        self.assertEqual(self.run_read(), pr.READ_MATCHES, self.output)
        self.assertEqual(len(self.calls()), 1)
        self.assertNothingCreated()

    def test_changing_root_or_profile_refuses_reuse_before_any_call(self):
        self.write_config(config_home=self.home_a, profile="default")
        self.run_read()
        frozen = self.journal_path().read_bytes()
        for seren in ({"config_home": self.home_b, "profile": "default"},
                      {"config_home": self.home_a, "profile": "other"}):
            with self.subTest(seren=seren):
                self.write_config(**seren)
                self.assertEqual(self.run_read(), pr.INVALID)
                self.assertIn("cli_session", self.output)
                self.assertEqual(len(self.calls()), 1)
                self.assertEqual(self.journal_path().read_bytes(), frozen)
        self.assertNothingCreated()

    def test_an_inherited_shell_change_cannot_silently_select_another_session(self):
        self.write_config()  # simulation, inherited selection: resolved and bound
        self.run_read(environ={"XDG_CONFIG_HOME": self.home_a})
        frozen = self.journal_path().read_bytes()
        self.assertEqual(self.run_read(environ={"XDG_CONFIG_HOME": self.home_b}), pr.INVALID)
        self.assertIn("cli_session", self.output)
        self.assertEqual(self.run_read(environ={"XDG_CONFIG_HOME": self.home_a,
                                                "SEREN_PROFILE": "other"}), pr.INVALID)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.journal_path().read_bytes(), frozen)

    def test_old_or_missing_bindings_fail_safely_and_stay_unchanged(self):
        self.write_config(config_home=self.home_a, profile="default")
        path = self.journal_path()
        path.parent.mkdir(parents=True)
        for journal in ({"binding": {"version": 1}, "invocation": None, "result": None},
                        {"invocation": None, "result": None}):
            path.write_text(json.dumps(journal))
            frozen = path.read_bytes()
            self.assertEqual(self.run_read(), pr.INVALID)
            self.assertIn("no compatible input binding", self.output)
            self.assertEqual(self.calls(), [])
            self.assertEqual(path.read_bytes(), frozen)

    def test_the_new_identity_has_its_own_journal_and_inherits_nothing(self):
        old = self.journal_path("seren.lifecycle.prerequisite-read.1")
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({"binding": {"version": 1}, "result": "not_read",
                                   "invocation": {"command": "agent managed-get"}}))
        frozen = old.read_bytes()
        self.write_config(config_home=self.home_a, profile="default")
        self.assertEqual(self.run_read(PACKAGE_1), pr.INVALID)  # old identity: not adopted
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_read(), pr.READ_MATCHES, self.output)  # fresh allowance
        self.assertEqual(len(self.calls()), 1)
        self.assertNotEqual(self.journal_path(), old)
        self.assertEqual(old.read_bytes(), frozen)
        self.assertEqual(self.run_read(), pr.READ_MATCHES)  # never replayed
        self.assertEqual(len(self.calls()), 1)

    def test_validation_and_the_route_open_nothing_under_the_session_root(self):
        home = self.root / "session-root" / "config"
        (home / "seren/profiles/default").mkdir(parents=True)
        secret = home / "seren/profiles/default/credentials.toml"
        secret.write_text("zqSENTINEL")
        self.write_config(config_home=str(home), profile="default")
        opened = []
        real_open, real_os_open = builtins.open, os.open

        def watch(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        def watch_os(path, *args, **kwargs):
            opened.append(str(path))
            return real_os_open(path, *args, **kwargs)
        with mock.patch.object(builtins, "open", watch), mock.patch.object(os, "open",
                                                                           watch_os):
            self.assertEqual(self.run_read(), pr.READ_MATCHES, self.output)
        self.assertFalse([p for p in opened if p.startswith(str(home))])
        self.assertEqual(sorted(p.name for p in home.rglob("*")),
                         ["credentials.toml", "default", "profiles", "seren"])


class Package(unittest.TestCase):
    def test_the_new_package_is_distinct_and_holds_no_private_path(self):
        text = PACKAGE_2.read_text()
        package = json.loads(text)
        self.assertEqual(package["id"], "seren.lifecycle.prerequisite-read.2")
        self.assertNotEqual(package["id"], json.loads(PACKAGE_1.read_text())["id"])
        self.assertEqual(package["command"], "python -m seren_harness prerequisite-read "
                         "--config ~/seren-harness-live/config.json --package "
                         "docs/lifecycle-setup/prerequisite-read-2.proposed.json")
        self.assertEqual((package["limits"]["service_invocations"],
                          package["limits"]["timeout_seconds"], package["limits"]["retries"]),
                         (pr.MAX_INVOCATIONS, 60, 0))
        self.assertEqual(package["private_config_change"]["add_to_seren"]["profile"], "default")
        for private in ("example-research-app", "Documents/", ".data/seren-cli", "Codex/"):
            self.assertNotIn(private, text)  # the session root stays in the private report


if __name__ == "__main__":
    unittest.main()
