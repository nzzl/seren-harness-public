"""Behavior tests; each fixture is an isolated disposable Git repository."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
RUNNER = SOURCE / ".tiny-harness/run.py"


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo with spaces"
        self.root.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0")
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture Author")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def run_command(self, argv):
        return subprocess.run(argv, cwd=self.root, env=self.env, text=True,
                              capture_output=True, timeout=20)

    def git(self, *args):
        result = self.run_command(["git", *args])
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def harness(self, *args):
        return self.run_command([sys.executable, str(RUNNER), *args])

    def install(self):
        result = self.harness("install", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)

    def configure(self, code="pass", timeout=5):
        self.install()
        self.write_checks([{"name": "behavior", "argv": [sys.executable, "-c", code],
                            "timeout_seconds": timeout}])
        self.git("add", ".tiny-harness")

    def write_checks(self, checks):
        (self.root / ".tiny-harness/checks.json").write_text(json.dumps({"checks": checks}))

    def assert_failed(self, result, text):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(text, result.stdout + result.stderr)

    def test_missing_and_empty_staged_configuration(self):
        self.assert_failed(self.harness("validate"), "missing or invalid")
        self.install()
        self.assert_failed(self.harness("validate"), "missing or invalid")
        self.git("add", ".tiny-harness")
        self.assert_failed(self.harness("validate"), "at least one required check")

    def test_malformed_and_invalid_configuration(self):
        self.install()
        path = self.root / ".tiny-harness/checks.json"
        cases = ["{", "[]", '{"checks": [], "optional": true}',
                 '{"checks": [null]}',
                 json.dumps({"checks": [{"name": "bad", "argv": [], "timeout_seconds": 1}]}),
                 json.dumps({"checks": [{"name": "bad", "argv": ["echo"], "timeout_seconds": False}]}),
                 json.dumps({"checks": [{"name": "bad", "argv": ["echo"], "timeout_seconds": float("inf")}]}),
                 json.dumps({"checks": [{"name": "same", "argv": ["echo"], "timeout_seconds": 1}] * 2})]
        for content in cases:
            with self.subTest(content=content):
                path.write_text(content)
                self.git("add", ".tiny-harness")
                self.assert_failed(self.harness("validate"), "ERROR:")

    def test_validate_reports_failure_and_runs_other_checks(self):
        self.configure()
        self.write_checks([
            {"name": "fails", "argv": [sys.executable, "-c", "raise SystemExit(7)"], "timeout_seconds": 5},
            {"name": "still runs", "argv": [sys.executable, "-c", "print('second-check-evidence')"], "timeout_seconds": 5}])
        self.git("add", ".tiny-harness/checks.json")
        result = self.harness("validate")
        self.assert_failed(result, "exit 7")
        self.assertIn("second-check-evidence", result.stdout)
        self.assertNotEqual(self.run_command(["git", "rev-parse", "--verify", "HEAD"]).returncode, 0)

    def test_commit_stops_on_failure_and_reruns_all_checks_after_repair(self):
        self.configure()
        later = {"name": "later", "argv": [sys.executable, "-c", "print('later-check-evidence')"],
                 "timeout_seconds": 5}
        failures = [([sys.executable, "-c", "raise SystemExit(7)"], 5, "exit 7"),
                    (["tiny-harness-nonexistent-command"], 5, "No such file"),
                    ([sys.executable, "-c", "import time; time.sleep(10)"], 0.05, "timed out")]
        for argv, timeout, evidence in failures:
            with self.subTest(evidence=evidence):
                self.write_checks([{"name": "first", "argv": argv, "timeout_seconds": timeout}, later])
                self.git("add", ".tiny-harness/checks.json")
                result = self.harness("commit", "-m", "Must fail promptly")
                self.assert_failed(result, evidence)
                self.assertNotIn("later-check-evidence", result.stdout)
                self.assertNotIn("PASS all", result.stdout)
                self.assertNotEqual(self.run_command(["git", "rev-parse", "--verify", "HEAD"]).returncode, 0)
        self.write_checks([{"name": "first", "argv": [sys.executable, "-c", "print('first-check-evidence')"],
                            "timeout_seconds": 5}, later])
        self.git("add", ".tiny-harness/checks.json")
        result = self.harness("commit", "-m", "Repair first check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("first-check-evidence", result.stdout)
        self.assertIn("later-check-evidence", result.stdout)
        self.assertIn("PASS all 2", result.stdout)

    def test_empty_commit_does_not_run_checks_or_hooks(self):
        self.configure("print('check-must-not-run')")
        self.git("commit", "-qm", "Fixture baseline")
        previous = self.git("rev-parse", "HEAD")
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho hook-must-not-run >&2\nexit 1\n")
        hook.chmod(0o755)
        (self.root / ".tiny-harness/TASK.md").write_text("Unstaged user work")
        (self.root / "notes").write_text("Untracked user work")
        before = self.git("status", "--porcelain")
        result = self.harness("commit", "-m", "Nothing staged")
        self.assert_failed(result, "Nothing staged to commit")
        self.assertNotIn("Validating staged tree", result.stdout)
        self.assertNotIn("check-must-not-run", result.stdout)
        self.assertNotIn("hook-must-not-run", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), previous)
        self.assertEqual(self.git("status", "--porcelain"), before)

    def test_empty_initial_commit_does_not_validate_untracked_harness(self):
        self.install()
        result = self.harness("commit", "-m", "No files staged yet")
        self.assert_failed(result, "Nothing staged to commit")
        self.assertNotIn("Validating staged tree", result.stdout)
        self.assertNotEqual(self.run_command(["git", "rev-parse", "--verify", "HEAD"]).returncode, 0)

    def test_merge_with_unchanged_tree_still_validates_and_commits(self):
        self.configure("print('merge-check-evidence')")
        self.git("commit", "-qm", "Fixture baseline")
        original = self.git("branch", "--show-current")
        self.git("checkout", "-qb", "side")
        (self.root / "feature").write_text("same final content")
        self.git("add", "feature")
        self.git("commit", "-qm", "Implement on side")
        side = self.git("rev-parse", "HEAD")
        self.git("checkout", original)
        self.git("cherry-pick", "--no-commit", side)
        self.git("commit", "-qm", "Implement independently")
        previous = self.git("rev-parse", "HEAD")
        self.git("merge", "--no-ff", "--no-commit", "side")
        self.assertEqual(self.git("write-tree"), self.git("rev-parse", "HEAD^{tree}"))
        result = self.harness("commit", "-m", "Record merged ancestry")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("merge-check-evidence", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD^1"), previous)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), side)

    def test_missing_executable_and_timeout(self):
        self.configure()
        for argv, timeout, evidence in [(["tiny-harness-nonexistent-command"], 5, "FAIL behavior"),
                                        ([sys.executable, "-c", "import time; time.sleep(10)"], 0.05, "timed out")]:
            with self.subTest(argv=argv):
                self.write_checks([{"name": "behavior", "argv": argv, "timeout_seconds": timeout}])
                self.git("add", ".tiny-harness/checks.json")
                self.assert_failed(self.harness("commit", "-m", "Must fail"), evidence)

    def test_checks_get_empty_input_while_caller_input_stays_open(self):
        self.configure("import sys; assert sys.stdin.read() == ''", timeout=10)
        process = subprocess.Popen([sys.executable, str(RUNNER), "validate"], cwd=self.root,
                                   env=self.env, text=True, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            process.stdin.write("caller input\n")
            process.stdin.flush()
            output = process.stdout.read()
            self.assertEqual(process.wait(timeout=20), 0, output)
            self.assertIn("PASS all 1 required checks", output)
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()

    def test_unstaged_fix_cannot_mask_staged_failure(self):
        self.configure("from pathlib import Path; assert Path('value').read_text() == 'good'")
        value = self.root / "value"
        value.write_text("bad")
        self.git("add", "value")
        value.write_text("good")
        self.assert_failed(self.harness("validate"), "Required validation failed")
        self.assertEqual(value.read_text(), "good")
        self.assertEqual(self.git("show", ":value"), "bad")

    def test_untracked_dependency_is_not_in_snapshot(self):
        self.configure("from pathlib import Path; assert Path('untracked').exists()")
        (self.root / "untracked").write_text("local-only")
        self.assert_failed(self.harness("validate"), "Required validation failed")

    def test_validation_is_fresh_and_preserves_unrelated_changes(self):
        self.configure("from pathlib import Path; p = Path('generated'); assert not p.exists(); p.write_text('built')")
        for _ in range(2):
            result = self.harness("validate")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "generated").exists())
        (self.root / "unrelated").write_text("original")
        self.git("add", "unrelated")
        result = self.harness("commit", "-m", "Establish baseline")
        self.assertEqual(result.returncode, 0, result.stderr)
        old_head = self.git("rev-parse", "HEAD")
        (self.root / "unrelated").write_text("user edit")
        (self.root / "new").write_text("owned change")
        (self.root / "untracked").write_text("user notes")
        self.git("add", "new")
        result = self.harness("commit", "-m", "Add owned change")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD^"), old_head)
        self.assertEqual(self.git("show", "HEAD:unrelated"), "original")
        self.assertEqual((self.root / "unrelated").read_text(), "user edit")
        self.assertEqual((self.root / "untracked").read_text(), "user notes")
        self.assertEqual(self.git("log", "-1", "--format=%an <%ae>"), "Fixture Author <fixture@example.invalid>")

    def test_install_preserves_existing_project_and_refuses_reinstall(self):
        files = {"AGENTS.md": "existing instructions", "CLAUDE.md": "existing agent rules",
                 "project.json": '{"existing":true}', "hooks/pre-commit": "#!/bin/sh\nexit 0\n"}
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("config", "core.hooksPath", "hooks")
        self.git("add", *files)
        self.git("commit", "-qm", "Existing project")
        previous = self.git("rev-parse", "HEAD")
        settings = (self.root / ".git/config").read_bytes()
        self.install()
        for name, content in files.items():
            self.assertEqual((self.root / name).read_text(), content)
        self.assertEqual((self.root / ".git/config").read_bytes(), settings)
        self.assertEqual(self.git("rev-parse", "HEAD"), previous)
        self.assertIn("Replace with", (self.root / ".tiny-harness/TASK.md").read_text())
        self.assertTrue((self.root / ".tiny-harness/LICENSE").is_file())
        self.assert_failed(self.harness("install", str(self.root)), "Refusing to replace")

    def test_install_rejects_subdirectory_and_symlink_destination(self):
        child = self.root / "child"
        child.mkdir()
        self.assert_failed(self.harness("install", str(child)), "repository root")
        (self.root / ".tiny-harness").symlink_to(self.root / "missing")
        self.assert_failed(self.harness("install", str(self.root)), "Refusing to replace")

    def test_existing_hook_runs_and_can_block_commit(self):
        self.configure()
        hooks = self.root / "custom-hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\necho existing-hook-evidence >&2\nexit 1\n")
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", "custom-hooks")
        result = self.harness("commit", "-m", "Hook should block")
        self.assert_failed(result, "existing-hook-evidence")
        self.assertIn("Git commit failed", result.stderr)

    def test_modifying_hook_is_reported_without_rewriting_commit(self):
        self.configure()
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf hook-change > hook-file\ngit add hook-file\n")
        hook.chmod(0o755)
        result = self.harness("commit", "-m", "Hook changes tree")
        self.assert_failed(result, "The commit EXISTS")
        self.assertEqual(self.git("show", "HEAD:hook-file"), "hook-change")

    def test_index_change_during_validation_blocks_commit(self):
        code = "import subprocess; subprocess.run(['git', '-C', " + repr(str(self.root)) + ", 'add', 'later'], check=True)"
        self.configure(code)
        (self.root / "later").write_text("concurrent edit")
        self.assert_failed(self.harness("commit", "-m", "Must fail"), "Index or HEAD changed")

    def test_submodule_fails_instead_of_omitting_required_content(self):
        self.configure()
        self.git("commit", "-qm", "Fixture seed")
        oid = self.git("rev-parse", "HEAD")
        self.git("update-index", "--add", "--cacheinfo", "160000," + oid + ",nested")
        self.assert_failed(self.harness("validate"), "Submodules are unsupported")

    def test_unmerged_index_fails(self):
        self.configure()
        (self.root / "conflict").write_text("base")
        self.git("add", "conflict")
        self.git("commit", "-qm", "Fixture base")
        branch = self.git("branch", "--show-current")
        self.git("checkout", "-qb", "other")
        (self.root / "conflict").write_text("other")
        self.git("commit", "-qam", "Other")
        self.git("checkout", branch)
        (self.root / "conflict").write_text("main")
        self.git("commit", "-qam", "Main")
        self.assertNotEqual(self.run_command(["git", "merge", "other"]).returncode, 0)
        self.assert_failed(self.harness("validate"), "ERROR:")

    def test_installed_copy_adoption_failure_recovery_and_commit(self):
        self.install()
        installed = self.root / ".tiny-harness/run.py"
        def run(*args):
            return self.run_command([sys.executable, str(installed), *args])
        self.git("add", ".tiny-harness")
        self.assert_failed(run("commit", "-m", "Not configured"), "at least one required check")
        (self.root / "check.py").write_text("from pathlib import Path\nassert Path('answer').read_text() == '42'\n")
        (self.root / "answer").write_text("wrong")
        self.write_checks([{"name": "sample behavior", "argv": [sys.executable, "check.py"], "timeout_seconds": 5}])
        self.git("add", ".tiny-harness/checks.json", "check.py", "answer")
        self.assert_failed(run("commit", "-m", "Still broken"), "Required validation failed")
        (self.root / "answer").write_text("42")
        self.git("add", "answer")
        result = run("commit", "-m", "Make the sample meet its acceptance criterion")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_install_outside_repository_is_visible_failure(self):
        outside = Path(self.temporary.name) / "not-a-repo"
        outside.mkdir()
        self.assert_failed(self.harness("install", str(outside)), "not a git repository")

    def test_empty_message_does_not_create_commit(self):
        self.configure()
        self.assert_failed(self.harness("commit", "-m", "   "), "meaningful commit message")

    def test_snapshot_changes_block_commit_before_later_checks(self):
        self.configure()
        (self.root / "value").write_text("bad")
        (self.root / "link").symlink_to("value")
        (self.root / "directory").mkdir()
        (self.root / "directory/nested").write_text("original")
        self.git("add", "value", "link", "directory/nested")
        mutations = [
            "Path('value').write_text('good')",
            "Path('value').unlink()",
            "Path('value').chmod(0o755)",
            "Path('value').unlink(); Path('value').symlink_to('link')",
            "Path('link').unlink(); Path('link').symlink_to('directory/nested')",
            "Path('link').unlink(); Path('link').write_text('replacement')",
            "Path('directory').rename('moved'); Path('directory').symlink_to('moved', target_is_directory=True)",
            "Path('value').write_text('good'); raise SystemExit(7)",
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.write_checks([
                    {"name": "mutating check", "argv": [sys.executable, "-c", "from pathlib import Path; " + mutation], "timeout_seconds": 5},
                    {"name": "later check", "argv": [sys.executable, "-c", "print('later-check-ran')"], "timeout_seconds": 5},
                ])
                self.git("add", ".tiny-harness/checks.json")
                result = self.harness("commit", "-m", "Must reject a modified snapshot")
                self.assert_failed(result, "modified staged snapshot path")
                self.assertNotIn("later-check-ran", result.stdout)
                self.assertNotEqual(self.run_command(["git", "rev-parse", "--verify", "HEAD"]).returncode, 0)
                self.assertEqual(self.git("show", ":value"), "bad")
                self.assertEqual((self.root / "value").read_text(), "bad")
                self.assertEqual(os.readlink(self.root / "link"), "value")
                self.assertFalse((self.root / "directory").is_symlink())

    def test_duplicate_json_keys_block_commit_before_checks(self):
        self.install()
        failing = json.dumps({"name": "required", "argv": [sys.executable, "-c", "raise SystemExit(7)"], "timeout_seconds": 5})
        passing = json.dumps({"name": "passing", "argv": [sys.executable, "-c", "pass"], "timeout_seconds": 5})
        contents = ['{"checks": [' + failing + '], "checks": [' + passing + ']}']
        for field, value in (("name", '"replacement"'), ("argv", '["missing-tool"]'), ("timeout_seconds", '10')):
            contents.append('{"checks": [' + passing[:-1] + ', ' + json.dumps(field) + ': ' + value + '}]}')
        for content in contents:
            with self.subTest(content=content):
                (self.root / ".tiny-harness/checks.json").write_text(content)
                self.git("add", ".tiny-harness")
                result = self.harness("commit", "-m", "Must reject ambiguous configuration")
                self.assert_failed(result, "Duplicate configuration key")
                self.assertNotIn("RUN ", result.stdout)
                self.assertNotEqual(self.run_command(["git", "rev-parse", "--verify", "HEAD"]).returncode, 0)

    def test_required_suite_rejects_empty_discovery(self):
        self.install()
        (self.root / "tests").mkdir()
        shutil.copyfile(SOURCE / "tests/run.py", self.root / "tests/run.py")
        shutil.copyfile(SOURCE / ".tiny-harness/checks.json", self.root / ".tiny-harness/checks.json")
        shutil.copyfile(SOURCE / "ruff.toml", self.root / "ruff.toml")
        (self.root / "examples").mkdir()
        (self.root / "examples/README").write_text("Fixture example directory")
        # Lint paths in the copied checks.json must exist for the test check to be reached.
        (self.root / "seren_harness").mkdir()
        (self.root / "seren_harness/README").write_text("Fixture package directory")
        (self.root / "simulation").mkdir()
        (self.root / "simulation/README").write_text("Fixture simulation directory")
        # The original audit's accidental rename keeps a test file undiscoverable.
        (self.root / "tests/runner_tests.py").write_text("import unittest\nclass Example(unittest.TestCase):\n    def test_example(self):\n        pass\n")
        self.git("add", ".tiny-harness", "tests", "ruff.toml", "examples", "seren_harness", "simulation")
        result = self.harness("commit", "-m", "Must reject zero discovered tests")
        self.assert_failed(result, "no tests discovered")
        self.assertNotEqual(self.run_command(["git", "rev-parse", "--verify", "HEAD"]).returncode, 0)

    def test_suite_entrypoint_runs_discovered_tests_and_propagates_failures(self):
        (self.root / "tests").mkdir()
        shutil.copyfile(SOURCE / "tests/run.py", self.root / "tests/run.py")
        test = self.root / "tests/test_example.py"
        test.write_text("import unittest\nclass Example(unittest.TestCase):\n    def test_example(self):\n        self.fail('behavior-failure-evidence')\n")
        result = self.run_command([sys.executable, "-B", "tests/run.py"])
        self.assert_failed(result, "behavior-failure-evidence")
        test.write_text("import unittest\nclass Example(unittest.TestCase):\n    def test_example(self):\n        pass\n")
        result = self.run_command([sys.executable, "-B", "tests/run.py"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    # Focused route: case-only changes may run a narrower verified check set on commit.

    def focused_fixture(self, focused_check="print('focused-check-ran')"):
        """Full checks print FULL; the focused route keeps 'lint' and adds one check. Seeded
        with a commit through the full route, which records a full-suite pass."""
        self.install()
        full = [{"name": "lint", "argv": [sys.executable, "-c", "print('lint-ran')"], "timeout_seconds": 5},
                {"name": "suite", "argv": [sys.executable, "-c",
                 "import pathlib,sys; print('full-suite-ran'); sys.exit(pathlib.Path('cases/full_fails.py').exists())"],
                 "timeout_seconds": 5}]
        self.policy = {"checks": full, "focused": {
            "paths": ["cases/*.py", "docs/cases/*.md"], "keep": ["lint"],
            "checks": [{"name": "case tests", "argv": [sys.executable, "-c", focused_check],
                        "timeout_seconds": 5}]}}
        (self.root / ".tiny-harness/checks.json").write_text(json.dumps(self.policy))
        for path in ("cases/one.py", "shared/runtime.py", "docs/cases/one.md"):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text("seed\n")
        self.git("add", "-A")
        result = self.harness("commit", "-m", "Seed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ROUTE full", result.stdout)

    def stage(self, path, text="change\n"):
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)
        self.git("add", "--", path)

    def assert_route(self, result, route):
        output = result.stdout + result.stderr
        self.assertIn("ROUTE " + route + " for staged tree " + self.git("write-tree"), output)
        self.assertEqual(route == "full", "full-suite-ran" in output, output)
        if route == "focused":
            self.assertIn('Selected checks: ["lint", "case tests"]', output)
            self.assertIn("the full suite did not run", output)
            self.assertNotIn("PASS all", output)

    def test_an_eligible_case_change_takes_the_focused_route(self):
        self.focused_fixture()
        self.stage("cases/two.py")
        self.stage("docs/cases/one.md")
        result = self.harness("commit", "-m", "Add a case")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("lint-ran", result.stdout)
        self.assertIn("focused-check-ran", result.stdout)
        self.assertIn('Changed paths: ["cases/two.py", "docs/cases/one.md"]', result.stdout)
        self.assertIn("ROUTE focused for staged tree " + self.git("rev-parse", "HEAD^{tree}"), result.stdout)
        self.assertIn("full suite did not run", result.stdout)
        self.assertNotIn("full-suite-ran", result.stdout)

    def test_shared_policy_mixed_and_unestablished_changes_take_the_full_route(self):
        self.focused_fixture()
        widened = dict(self.policy, focused=dict(self.policy["focused"], paths=["*", "*/*", "*/*/*"]))
        scenarios = {
            "shared runtime": lambda: self.stage("shared/runtime.py"),
            "unknown path": lambda: self.stage("cases/nested/deep.py"),
            "mixed": lambda: (self.stage("cases/two.py"), self.stage("shared/runtime.py")),
            "validation policy widened with a case": lambda: (
                self.stage("cases/two.py"),
                self.stage(".tiny-harness/checks.json", json.dumps(widened))),
            "deletion": lambda: self.git("rm", "-q", "--cached", "cases/one.py"),
            "rename": lambda: (self.git("mv", "cases/one.py", "cases/moved.py")),
            "executable mode": lambda: self.git("update-index", "--chmod=+x", "cases/one.py"),
        }
        for name, change in scenarios.items():
            with self.subTest(name):
                change()
                result = self.harness("validate", "--select")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_route(result, "full")
                self.git("reset", "-q")
                self.git("checkout", "-q", "--", ".")
                (self.root / "cases/two.py").unlink(missing_ok=True)
                (self.root / "cases/nested/deep.py").unlink(missing_ok=True)
                (self.root / "cases/moved.py").unlink(missing_ok=True)

    def test_explicit_full_validation_and_default_validate_stay_full(self):
        self.focused_fixture()
        self.stage("cases/two.py")
        self.assert_route(self.harness("validate"), "full")
        self.assert_route(self.harness("validate", "--select"), "focused")
        result = self.harness("commit", "--full", "-m", "Explicit full")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS all 2", result.stdout)

    def test_a_failing_focused_check_blocks_the_commit(self):
        self.focused_fixture("import pathlib,sys; sys.exit(3 if pathlib.Path('cases/bad.py').exists() else 0)")
        head = self.git("rev-parse", "HEAD")
        self.stage("cases/bad.py")
        result = self.harness("commit", "-m", "Must fail")
        self.assert_failed(result, "exit 3")
        self.assertIn("ROUTE focused", result.stdout)
        self.assertEqual(head, self.git("rev-parse", "HEAD"))

    def test_a_known_full_suite_failure_is_not_bypassed_by_the_focused_route(self):
        self.focused_fixture()
        self.stage("cases/full_fails.py")  # eligible path whose content breaks the full suite
        self.assert_failed(self.harness("validate"), "Required validation failed")
        result = self.harness("commit", "-m", "Must not slip through")
        self.assert_failed(result, "Required validation failed")
        self.assertIn("last full-suite validation failed", result.stdout)
        self.git("rm", "-q", "--cached", "cases/full_fails.py")
        self.stage("cases/two.py")
        result = self.harness("commit", "-m", "Full pass clears it")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_route(result, "full")
        self.stage("cases/three.py")
        self.assert_route(self.harness("commit", "-m", "Focused again"), "focused")

    def test_missing_full_suite_record_or_a_changed_runner_takes_the_full_route(self):
        self.focused_fixture()
        self.stage("cases/two.py")
        common = Path(self.git("rev-parse", "--git-common-dir"))
        (common if common.is_absolute() else self.root / common).joinpath(
            "tiny-harness-full-validation.json").unlink()
        result = self.harness("validate", "--select")
        self.assertIn("no recorded full-suite result", result.stdout)
        self.assert_route(result, "full")
        altered = Path(self.temporary.name) / "run.py"
        altered.write_text(RUNNER.read_text() + "\n# altered\n")
        result = self.run_command([sys.executable, str(altered), "validate", "--select"])
        self.assertIn("executing runner differs from HEAD", result.stdout)
        self.assert_route(result, "full")

    def test_tree_changes_during_focused_validation_block_the_commit(self):
        self.focused_fixture("import subprocess; subprocess.run(['git', '-C', " + repr(str(self.root))
                             + ", 'add', 'shared/runtime.py'], check=True)")
        head = self.git("rev-parse", "HEAD")
        (self.root / "shared/runtime.py").write_text("sneaked in after selection\n")
        self.stage("cases/two.py")
        result = self.harness("commit", "-m", "Must fail")
        self.assert_failed(result, "Index or HEAD changed")
        self.assertIn("ROUTE focused", result.stdout)
        self.assertEqual(head, self.git("rev-parse", "HEAD"))

    def test_focused_policy_configuration_is_validated(self):
        self.install()
        base = [{"name": "lint", "argv": ["echo"], "timeout_seconds": 1}]
        bad = [{"paths": [".tiny-harness/*"], "keep": [], "checks": base[:0]},
               {"paths": ["cases/**"], "keep": ["lint"], "checks": []},
               {"paths": ["../x"], "keep": ["lint"], "checks": []},
               {"paths": ["cases/*"], "keep": ["missing"], "checks": []},
               {"paths": ["cases/*"], "keep": [], "checks": []},
               {"paths": ["cases/*"], "keep": ["lint"], "checks": base},
               {"paths": ["cases/*"], "keep": ["lint"]}]
        for focused in bad:
            with self.subTest(focused=focused):
                (self.root / ".tiny-harness/checks.json").write_text(json.dumps({"checks": base, "focused": focused}))
                self.git("add", ".tiny-harness")
                self.assert_failed(self.harness("validate"), "ERROR:")

    def test_named_test_modules_import_the_staged_package_not_working_tree_or_installed(self):
        self.install()
        (self.root / "tests").mkdir()
        shutil.copyfile(SOURCE / "tests/run.py", self.root / "tests/run.py")
        (self.root / "tests/test_probe.py").write_text(
            "import unittest\nimport seren_harness\nclass Probe(unittest.TestCase):\n"
            "    def test_origin(self):\n        self.assertEqual('staged', seren_harness.MARK)\n")
        (self.root / "seren_harness").mkdir()
        (self.root / "seren_harness/__init__.py").write_text("MARK = 'staged'\n")
        self.write_checks([{"name": "probe", "argv": [sys.executable, "-B", "tests/run.py", "test_probe"],
                            "timeout_seconds": 10}])
        self.git("add", "-A")
        (self.root / "seren_harness/__init__.py").write_text("MARK = 'working tree'\n")
        installed = Path(self.temporary.name) / "installed/seren_harness"
        installed.mkdir(parents=True)
        (installed / "__init__.py").write_text("MARK = 'installed'\n")
        self.env["PYTHONPATH"] = str(installed.parent)
        result = self.harness("validate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr + result.stdout)
        result = self.run_command([sys.executable, "-B", "tests/run.py", "test_missing"])
        self.assert_failed(result, "named test modules must be existing")


if __name__ == "__main__":
    unittest.main()
