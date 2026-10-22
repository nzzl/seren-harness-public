"""Catchable termination must unwind validation and stop its process group."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest

import test_runner


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.f = test_runner.HarnessTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.configure()
        self.f.git("commit", "-m", "Baseline fixture")

    def assert_cleanup(self, signum, whole_group=False):
        for command in [("validate",), ("commit", "-m", "Interrupted change")]:
            with self.subTest(command=command):
                self.check_interruption(command, signum, whole_group)

    def check_interruption(self, command, signum, whole_group):
        ready = Path(self.f.temporary.name) / "ready"
        ready.unlink(missing_ok=True)
        child = ("import json,os,pathlib,time; ready=pathlib.Path(" + repr(str(ready))
                 + "); pending=ready.with_suffix('.tmp'); "
                 "pending.write_text(json.dumps([os.getpgrp(),str(pathlib.Path.cwd())])); "
                 "pending.replace(ready); time.sleep(60)")
        code = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
                + repr(child) + "]); time.sleep(60)")
        self.f.write_checks([{"name": "termination", "argv": [sys.executable, "-c", code],
                              "timeout_seconds": 30}])
        self.f.git("add", ".tiny-harness/checks.json")
        previous_head = self.f.git("rev-parse", "HEAD")
        previous_tree = self.f.git("write-tree")
        process = subprocess.Popen([sys.executable, str(test_runner.RUNNER), *command],
                                   cwd=self.f.root, env=self.f.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "check child never started")
            if whole_group:
                os.killpg(process.pid, signum)
            else:
                process.send_signal(signum)
            # Both check and child inherit the pipes: EOF also verifies their termination.
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 128 + signum, stdout + stderr)
            self.assertIn("interrupted", stderr)
            self.assertNotIn("PASS all", stdout)
            snapshot = Path(json.loads(ready.read_text())[1])
            self.assertFalse(snapshot.parent.exists(), "temporary snapshot survived interruption")
            self.assertEqual(self.f.git("rev-parse", "HEAD"), previous_head)
            self.assertEqual(self.f.git("write-tree"), previous_tree)
        finally:
            # Also clean up when this regression is run against the unfixed runner.
            if ready.exists():
                group, snapshot = json.loads(ready.read_text())
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                shutil.rmtree(Path(snapshot).parent, ignore_errors=True)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_sigterm_to_runner(self):
        self.assert_cleanup(signal.SIGTERM)

    def test_sigterm_to_runner_group(self):
        self.assert_cleanup(signal.SIGTERM, whole_group=True)

    def test_sighup_to_runner(self):
        self.assert_cleanup(signal.SIGHUP)

    def test_sigint_to_runner(self):
        self.assert_cleanup(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
