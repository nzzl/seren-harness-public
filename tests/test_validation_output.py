"""Failure reporting and saved validation logs, in disposable repositories and suites."""
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]
RUNNER = SOURCE / ".tiny-harness/run.py"
# These tests pin unittest's report format (labels, "Ran N tests", _FailedTest ids); the
# default pytest backend's agreement on the same outcomes is in test_pytest_runner.
UNITTEST = ("--runner", "unittest")

SUITE = '''import unittest
class Kinds(unittest.TestCase):
    def test_1_assertion(self):
        self.assertEqual("observed-value", "expected-value")
    def test_2_exception(self):
        raise RuntimeError("unexpected-exception-evidence")
    def test_3_subtests(self):
        for case in ("good", "bad"):
            with self.subTest(case=case):
                self.assertEqual(case, "good")
    @unittest.expectedFailure
    def test_4_unexpected_success(self):
        pass
    def test_5_after(self):
        print("after-failure-marker", flush=True)
'''


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="validation-output-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        (self.root / "tests").mkdir(parents=True)
        shutil.copyfile(SOURCE / "tests/run.py", self.root / "tests/run.py")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0")

    def command(self, argv, **kw):
        return subprocess.run(argv, cwd=self.root, env=self.env, text=True,
                              capture_output=True, timeout=60, **kw)

    def repo(self, checks):
        for args in (["init", "-q"], ["config", "user.name", "Fixture"],
                     ["config", "user.email", "fixture@example.invalid"]):
            self.command(["git", *args])
        self.assertEqual(self.command([sys.executable, str(RUNNER), "install",
                                       str(self.root)]).returncode, 0)
        (self.root / ".tiny-harness/checks.json").write_text(json.dumps({"checks": checks}))
        self.command(["git", "add", "-A"])
        self.tree = self.command(["git", "write-tree"]).stdout.strip()

    def log_of(self, output):
        lines = [line for line in output.splitlines() if line.startswith("Validation log")]
        self.assertGreaterEqual(len(lines), 2, output)  # start and completion/failure
        path = Path(lines[0].split(": ", 1)[1])
        self.assertEqual(Path(lines[-1].split(": ", 1)[1]), path)
        self.assertTrue(path.resolve().is_relative_to((self.root / ".git").resolve()), path)
        return path, path.read_text()


class ImmediateReportingTests(Fixture):
    def test_failure_and_traceback_appear_before_a_later_blocked_test_is_released(self):
        release = self.root / "release"
        (self.root / "tests/test_sync.py").write_text(
            "import pathlib, time, unittest\n"
            "class Sync(unittest.TestCase):\n"
            "    def test_a_fails(self):\n"
            "        self.fail('sync-failure-evidence')\n"
            "    def test_b_waits(self):\n"
            "        deadline = time.time() + 30\n"
            "        while not pathlib.Path(%r).exists():\n"
            "            self.assertLess(time.time(), deadline, 'never released')\n"
            "            time.sleep(0.02)\n" % str(release))
        child = subprocess.Popen([sys.executable, "-B", "tests/run.py", *UNITTEST], cwd=self.root,
                                 env=self.env, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(x) for x in child.stdout], daemon=True).start()
        seen = []
        try:
            # Synchronized on content, not time: release only after the report is out.
            while not ("FAIL: test_sync.Sync.test_a_fails\n" in seen
                       and any("sync-failure-evidence" in x for x in seen)
                       and any(x.startswith("Traceback") for x in seen)):
                seen.append(lines.get(timeout=30))
            self.assertFalse(release.exists())
            release.write_text("go")
            self.assertEqual(child.wait(timeout=30), 1)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()

    def test_each_problem_kind_is_identified_with_a_rerun_command(self):
        (self.root / "tests/test_kinds.py").write_text(SUITE)
        (self.root / "tests/test_broken.py").write_text("import missing_module_for_fixture\n")
        result = self.command([sys.executable, "-B", "tests/run.py", *UNITTEST])
        self.assertEqual(result.returncode, 1)
        out = result.stderr
        for label in ("FAIL: test_kinds.Kinds.test_1_assertion",
                      "ERROR: test_kinds.Kinds.test_2_exception",
                      "FAIL: test_kinds.Kinds.test_3_subtests (case='bad')",
                      "UNEXPECTED SUCCESS: test_kinds.Kinds.test_4_unexpected_success",
                      "ERROR: unittest.loader._FailedTest.test_broken"):
            self.assertIn(label, out)
        self.assertIn("unexpected-exception-evidence", out)
        self.assertIn("missing_module_for_fixture", out)
        self.assertIn("Rerun one for diagnosis (not full validation): python3 tests/run.py "
                      "--runner unittest ", out)
        self.assertIn("Ran 6 tests", out)  # the normal summary is kept

    def test_focused_rerun_selects_one_test_and_is_labelled_diagnostic(self):
        (self.root / "tests/test_kinds.py").write_text(SUITE)
        run = [sys.executable, "-B", "tests/run.py", *UNITTEST]
        result = self.command([*run, "test_kinds.Kinds.test_5_after"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
        self.assertIn("FOCUSED DIAGNOSTIC RUN", result.stderr)
        for bad in ("test_kinds.Kinds.x.y", "test_missing", "kinds", "test_kinds..Kinds"):
            with self.subTest(bad):
                rejected = self.command([*run, bad])
                self.assertEqual(rejected.returncode, 1)
                self.assertIn("named test modules must be existing", rejected.stderr)
        missing = self.command([*run, "test_kinds.Kinds.test_nope"])
        self.assertEqual(missing.returncode, 1)
        self.assertIn("python3 tests/run.py --runner unittest test_kinds.Kinds.test_nope",
                      missing.stderr)


class SavedLogTests(Fixture):
    def suite_check(self, timeout=60, runner=()):
        return {"name": "suite", "argv": [sys.executable, "-B", "tests/run.py", *runner],
                "timeout_seconds": timeout}

    def test_log_keeps_failure_later_output_and_identity_and_status_is_nonzero(self):
        (self.root / "tests/test_kinds.py").write_text(SUITE)
        self.repo([self.suite_check(runner=UNITTEST)])
        result = self.command([sys.executable, str(RUNNER), "validate"])
        self.assertNotEqual(result.returncode, 0)
        path, log = self.log_of(result.stdout)
        self.assertIn("tree " + self.tree, log)
        self.assertIn('RUN suite: ["%s", "-B", "tests/run.py", "--runner", "unittest"]'
                      % sys.executable, log)
        for text in ("FAIL: test_kinds.Kinds.test_1_assertion", "Traceback", "after-failure-marker",
                     "Failures reported by suite:", "FAIL suite (exit 1)",
                     "RESULT fail for staged tree " + self.tree):
            self.assertIn(text, log)
        self.assertLess(log.index("FAIL: test_kinds.Kinds.test_1_assertion"),
                        log.index("after-failure-marker"))
        record = json.loads((self.root / ".git/tiny-harness-full-validation.json").read_text())
        self.assertEqual((record["tree"], record["result"], record["log"]),
                         (self.tree, "fail", str(path)))
        # The terminal mirror keeps each stream: unittest's report arrives on stderr.
        self.assertIn("FAIL: test_kinds.Kinds.test_1_assertion", result.stderr)
        self.assertIn("after-failure-marker", result.stdout)
        tracked = self.command(["git", "ls-files"]).stdout
        self.assertNotIn("tiny-harness-logs", tracked)

    def test_closed_output_pipe_keeps_running_logging_and_failing(self):
        (self.root / "tests/test_kinds.py").write_text(SUITE)
        self.repo([self.suite_check()])
        script = '"%s" "%s" validate | head -1; echo "harness=${PIPESTATUS[0]}"' % (
            sys.executable, RUNNER)
        result = self.command(["/bin/bash", "-c", script])
        self.assertIn("harness=1", result.stdout)
        logs = sorted((self.root / ".git/tiny-harness-logs").glob("*.log"))
        log = logs[-1].read_text()
        self.assertIn("after-failure-marker", log)
        self.assertIn("RESULT fail", log)

    def test_success_still_passes_and_records_its_log(self):
        (self.root / "tests/test_ok.py").write_text(
            "import unittest\nclass Ok(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        self.repo([self.suite_check()])
        result = self.command([sys.executable, str(RUNNER), "validate"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS all 1 required checks", result.stdout)
        path, log = self.log_of(result.stdout)
        self.assertIn("RESULT pass for staged tree " + self.tree, log)
        record = json.loads((self.root / ".git/tiny-harness-full-validation.json").read_text())
        self.assertEqual((record["result"], record["log"]), ("pass", str(path)))
        # A focused diagnostic rerun never writes or changes full-validation evidence.
        before = (self.root / ".git/tiny-harness-full-validation.json").read_bytes()
        self.command([sys.executable, "-B", "tests/run.py", "test_ok.Ok.test_ok"])
        self.assertEqual((self.root / ".git/tiny-harness-full-validation.json").read_bytes(), before)

    def grandchild_check(self, seconds):
        pid_file = self.root.parent / "grandchild.pid"
        code = ("import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "open(%r, 'w').write(str(child.pid))\n"
                "print('before-stop-marker', flush=True)\n"
                "time.sleep(%d)\n" % (str(pid_file), seconds))
        return pid_file, {"name": "slow", "argv": [sys.executable, "-c", code], "timeout_seconds": 2}

    def assert_gone(self, pid_file):
        pid = int(pid_file.read_text())
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("child process survived the check")

    def test_timeout_keeps_output_and_kills_the_process_group(self):
        pid_file, check = self.grandchild_check(30)
        self.repo([check])
        result = self.command([sys.executable, str(RUNNER), "validate"])
        self.assertNotEqual(result.returncode, 0)
        _, log = self.log_of(result.stdout)
        self.assertIn("before-stop-marker", log)
        self.assertIn("FAIL slow: timed out", log)
        self.assertIn("RESULT fail", log)
        self.assert_gone(pid_file)

    def test_interruption_keeps_output_and_kills_the_process_group(self):
        pid_file, check = self.grandchild_check(30)
        check["timeout_seconds"] = 60
        self.repo([check])
        harness = subprocess.Popen([sys.executable, str(RUNNER), "validate"], cwd=self.root,
                                   env=self.env, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        seen = []
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(x) for x in harness.stdout], daemon=True).start()
        try:
            while "before-stop-marker\n" not in seen:
                seen.append(lines.get(timeout=30))
            harness.send_signal(signal.SIGTERM)
            self.assertEqual(harness.wait(timeout=30), 128 + signal.SIGTERM)
        finally:
            if harness.poll() is None:
                harness.kill()
            harness.wait()
        while True:
            try:
                seen.append(lines.get(timeout=5))
            except queue.Empty:
                break
        _, log = self.log_of("".join(seen))
        self.assertIn("before-stop-marker", log)
        self.assertIn("RESULT interrupted", log)
        self.assert_gone(pid_file)


if __name__ == "__main__":
    unittest.main()
