"""Check lifetime, configuration boundaries, and advisory maintenance warnings."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import test_runner


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runner.HarnessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture

    def baseline(self):
        self.f.configure()
        result = self.f.harness("commit", "-m", "Adopt harness")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WARNING:", result.stdout)

    def configure_child(self, ending, timeout=5):
        marker = Path(self.f.temporary.name) / "leaked"
        ready = Path(self.f.temporary.name) / "ready"
        child = ("import pathlib,time; pathlib.Path(" + repr(str(ready)) + ").touch(); "
                 "time.sleep(1); pathlib.Path(" + repr(str(marker)) + ").touch()")
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); " + ending
        self.f.write_checks([{"name": "child lifetime", "argv": [sys.executable, "-c", code],
                              "timeout_seconds": timeout}])
        self.f.git("add", ".tiny-harness/checks.json")
        return marker, ready

    def test_children_stop_after_success_failure_and_timeout(self):
        self.f.configure()
        for ending, timeout, success in [("pass", 5, True), ("sys.exit(7)", 5, False),
                                          ("time.sleep(10)", 0.3, False)]:
            with self.subTest(ending=ending):
                marker, _ = self.configure_child(ending, timeout)
                result = self.f.harness("validate")
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                time.sleep(1.2)
                self.assertFalse(marker.exists(), "child survived its check")

    def test_interruption_stops_children(self):
        self.f.configure()
        marker, ready = self.configure_child("time.sleep(10)")
        process = subprocess.Popen([sys.executable, str(test_runner.RUNNER), "validate"],
                                   cwd=self.f.root, env=self.f.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "child never started")
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            time.sleep(1.2)
            self.assertFalse(marker.exists(), "child survived interruption")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_symlinked_configuration_file_is_rejected(self):
        self.f.configure()
        config = self.f.root / ".tiny-harness/checks.json"
        outside = Path(self.f.temporary.name) / "outside.json"
        outside.write_text(config.read_text())
        config.unlink()
        config.symlink_to(outside)
        self.f.git("add", ".tiny-harness/checks.json")
        self.f.assert_failed(self.f.harness("validate"), "must not be symlinks")

    def test_symlinked_configuration_directory_is_rejected(self):
        outside = Path(self.f.temporary.name) / "outside"
        outside.mkdir()
        (outside / "checks.json").write_text(json.dumps({"checks": [
            {"name": "outside", "argv": [sys.executable, "-c", "pass"], "timeout_seconds": 5}]}))
        (self.f.root / ".tiny-harness").symlink_to(outside, target_is_directory=True)
        self.f.git("add", ".tiny-harness")
        self.f.assert_failed(self.f.harness("validate"), "must not be symlinks")

    def test_check_changes_warn_but_still_validate_and_allow_repair(self):
        self.baseline()
        previous = self.f.git("rev-parse", "HEAD")
        for code, success in [("raise SystemExit(7)", False), ("print('required-check-ran')", True)]:
            self.f.write_checks([{"name": "renamed check", "argv": [sys.executable, "-c", code],
                                  "timeout_seconds": 8}])
            self.f.git("add", ".tiny-harness/checks.json")
            result = self.f.harness("commit", "-m", "Update check")
            self.assertIn("WARNING:", result.stdout)
            self.assertIn(".tiny-harness/checks.json", result.stdout)
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
            if success:
                self.assertIn("required-check-ran", result.stdout)
            else:
                self.assertEqual(self.f.git("rev-parse", "HEAD"), previous)
                self.assertIn("exit 7", result.stdout)

    def test_ordinary_commit_is_quiet(self):
        self.baseline()
        (self.f.root / "feature").write_text("work")
        self.f.git("add", "feature")
        result = self.f.harness("commit", "-m", "Ordinary change")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WARNING:", result.stdout)

    def test_staged_runner_change_warns_without_blocking(self):
        self.baseline()
        runner = self.f.root / ".tiny-harness/run.py"
        runner.write_text(runner.read_text() + "\n# fixture change\n")
        self.f.git("add", ".tiny-harness/run.py")
        result = self.f.harness("commit", "-m", "Update runner")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING:", result.stdout)
        self.assertIn(".tiny-harness/run.py", result.stdout)

    def test_unstaged_executing_runner_change_warns_without_staging_it(self):
        self.baseline()
        runner = self.f.root / ".tiny-harness/run.py"
        runner.write_text(runner.read_text() + "\n# fixture change\n")
        (self.f.root / "feature").write_text("work")
        self.f.git("add", "feature")
        result = self.f.run_command([sys.executable, str(runner), "commit", "-m", "Feature"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("executing runner differs from HEAD", result.stdout)
        self.assertEqual(self.f.git("diff", "--name-only"), ".tiny-harness/run.py")


if __name__ == "__main__":
    unittest.main()
