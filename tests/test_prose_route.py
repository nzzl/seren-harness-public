"""Prose-only commit route, in disposable repositories with stand-in checks."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from seren_harness import live_discovery as live

SOURCE = Path(__file__).resolve().parents[1]
RUNNER = SOURCE / ".tiny-harness/run.py"
MARK = "CODE-TESTS-RAN"
TASK = ".tiny-harness/TASK.md"


class ProseRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="prose-route-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0")
        for args in (["init", "-q"], ["config", "user.name", "Fixture"],
                     ["config", "user.email", "fixture@example.invalid"],
                     ["config", "commit.gpgsign", "false"]):
            self.git(*args)
        self.assertEqual(self.harness("install", str(self.root)).returncode, 0)
        self.policy = {
            "checks": [{"name": "code tests", "timeout_seconds": 10,
                        "argv": [sys.executable, "-c", "print('%s')" % MARK]}],
            "prose": {"paths": [TASK]},
            "focused": {"paths": ["cases/*.py"], "keep": [],
                        "checks": [{"name": "case tests", "timeout_seconds": 10,
                                    "argv": [sys.executable, "-c", "print('CASE-TESTS-RAN')"]}]}}
        (self.root / ".tiny-harness/checks.json").write_text(json.dumps(self.policy))
        for path in ("src.py", "AGENTS.md", "examples/run.md", "cases/one.py",
                     "docs/journeys/x/package.json"):
            self.write(path, "seed\n")
        self.git("add", "-A")
        seeded = self.harness("commit", "-m", "Seed")
        self.assertEqual(seeded.returncode, 0, seeded.stdout + seeded.stderr)
        self.record = self.root / ".git/tiny-harness-full-validation.json"

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.root, env=self.env, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def harness(self, *args):
        return subprocess.run([sys.executable, str(RUNNER), *args], cwd=self.root, env=self.env,
                              text=True, capture_output=True, timeout=60)

    def write(self, path, text):
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)

    def route(self, result):
        out = result.stdout + result.stderr
        line = next(x for x in out.splitlines() if x.startswith("ROUTE "))
        return line.split()[1], out

    def test_narrative_checkpoint_edit_commits_without_code_tests_and_says_so(self):
        before = self.record.read_bytes()
        self.write(TASK, "# Task\nStatus corrected: the campaign is pending.\n")
        self.git("add", TASK)
        result = self.harness("commit", "-m", "Correct checkpoint wording")
        route, out = self.route(result)
        self.assertEqual((result.returncode, route), (0, "prose"), out)
        self.assertNotIn(MARK, out)
        self.assertIn('SKIPPED checks (prose-only route; code tests did not run): ["code tests"]', out)
        self.assertIn("This is not full-suite validation", out)
        self.assertIn("Committed validated tree", out)
        log = Path(next(x for x in out.splitlines()
                        if x.startswith("Validation log (pass)")).split(": ", 1)[1]).read_text()
        for text in ("ROUTE prose", "SKIPPED checks", "RESULT pass", "tree " + self.git("rev-parse", "HEAD^{tree}")):
            self.assertIn(text, log)
        # No full-suite evidence is written or inherited: the live gate refuses this HEAD.
        self.assertEqual(self.record.read_bytes(), before)
        with patch.object(live, "ROOT", self.root):
            with self.assertRaisesRegex(live.Refused, "lacks a full-suite pass"):
                live.validate_source()

    def test_explicit_full_validation_still_runs_every_check(self):
        self.write(TASK, "# Task\nNarrative only.\n")
        self.git("add", TASK)
        for args in (("validate",), ("commit", "--full", "-m", "Full anyway")):
            with self.subTest(args):
                result = self.harness(*args)
                route, out = self.route(result)
                self.assertEqual((result.returncode, route), (0, "full"), out)
                self.assertIn(MARK, out)
                self.assertNotIn("SKIPPED", out)
        with patch.object(live, "ROOT", self.root):
            live.validate_source()  # an exact-tree full pass still satisfies the gate

    def test_code_instructions_artifacts_examples_and_config_stay_checked(self):
        for paths in (["src.py"], [TASK, "src.py"], ["AGENTS.md"], ["examples/run.md"],
                      ["docs/journeys/x/package.json"], [TASK, ".tiny-harness/checks.json"]):
            with self.subTest(paths):
                for path in paths:
                    text = (self.root / path).read_text()
                    self.write(path, text.replace('"timeout_seconds": 10', '"timeout_seconds": 11', 1)
                               if path.endswith("checks.json") else text + "edit\n")
                    self.git("add", path)
                result = self.harness("commit", "-m", "Checked change")
                route, out = self.route(result)
                self.assertEqual((result.returncode, route), (0, "full"), out)
                self.assertIn(MARK, out)

    def test_unsafe_modes_symlinks_renames_deletions_additions_and_binary_stay_checked(self):
        cases = {
            "mode": lambda: (self.write(TASK, "x\n"), os.chmod(self.root / TASK, 0o755)),
            "symlink": lambda: ((self.root / TASK).unlink(), os.symlink("../src.py", self.root / TASK)),
            "rename": lambda: self.git("mv", TASK, ".tiny-harness/NOTES.md"),
            "delete": lambda: (self.root / TASK).unlink(),
            "addition": lambda: self.write(".tiny-harness/TASK2.md", "new\n"),
            "binary": lambda: (self.root / TASK).write_bytes(b"text\0binary\n"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.git("reset", "-q", "--hard")
                change()
                self.git("add", "-A")
                result = self.harness("validate", "--select")
                route, out = self.route(result)
                self.assertEqual(route, "full", out)
                self.assertIn(MARK, out)

    def test_prior_full_failure_is_neither_cleared_nor_overwritten(self):
        self.write("src.py", "broken\n")
        self.git("add", "src.py")
        failing = dict(self.policy, checks=[{"name": "code tests", "timeout_seconds": 10, "argv": [
            sys.executable, "-c", "import pathlib,sys; sys.exit('broken' in pathlib.Path('src.py').read_text())"]}])
        (self.root / ".tiny-harness/checks.json").write_text(json.dumps(failing))
        self.git("add", ".tiny-harness/checks.json")
        self.assertNotEqual(self.harness("validate").returncode, 0)
        failed = self.record.read_bytes()
        self.assertEqual(json.loads(failed)["result"], "fail")
        self.git("reset", "-q", "--hard")
        self.write(TASK, "# Task\nRecord the failure.\n")
        self.git("add", TASK)
        result = self.harness("commit", "-m", "Record the failed validation")
        self.assertEqual((result.returncode, self.route(result)[0]), (0, "prose"))
        self.assertEqual(self.record.read_bytes(), failed)

    def test_hooks_still_run_and_can_block_a_prose_commit(self):
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho hook-ran > \"$(git rev-parse --git-dir)/hook-mark\"\nexit 1\n")
        hook.chmod(0o755)
        head = self.git("rev-parse", "HEAD")
        self.write(TASK, "# Task\nBlocked by hook.\n")
        self.git("add", TASK)
        result = self.harness("commit", "-m", "Hook blocks")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.route(result)[0], "prose")
        self.assertTrue((self.root / ".git/hook-mark").exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_focused_case_route_and_invalid_prose_policies_are_unchanged(self):
        self.write("cases/one.py", "changed\n")
        self.git("add", "cases/one.py")
        result = self.harness("validate", "--select")
        self.assertEqual(self.route(result)[0], "focused", result.stdout)
        self.git("reset", "-q", "--hard")
        for paths in (["docs/*.md"], [".tiny-harness/run.py"], ["../outside.md"], []):
            with self.subTest(paths):
                (self.root / ".tiny-harness/checks.json").write_text(
                    json.dumps(dict(self.policy, prose={"paths": paths})))
                self.git("add", ".tiny-harness/checks.json")
                result = self.harness("validate", "--select")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Prose paths must", result.stderr)

    def test_index_change_during_prose_validation_blocks_the_commit(self):
        spec = importlib.util.spec_from_file_location("tiny_harness_runner", RUNNER)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        self.write(TASK, "# Task\nNarrative.\n")
        self.git("add", TASK)
        original, calls = runner.git, []

        def git(root, *args, env=None):
            value = original(root, *args, env=env)
            if args == ("write-tree",) and env is None:
                calls.append(value)
                if len(calls) > 1:
                    return "0" * 40  # the index moved while validating
            return value
        with patch.object(runner, "git", git), patch.object(sys, "stdout", open(os.devnull, "w")):
            with self.assertRaisesRegex(runner.Failure, "Index or HEAD changed"):
                runner.validate(self.root, allow_focused=True)


if __name__ == "__main__":
    unittest.main()
