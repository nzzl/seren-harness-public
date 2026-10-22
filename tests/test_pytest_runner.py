"""tests/run.py's unittest and pytest backends agree, on disposable suites."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]

KINDS = '''import unittest
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

PASSING = '''import unittest
class First(unittest.TestCase):
    def test_a(self):
        pass
    def test_b(self):
        with self.subTest(n=1):
            pass
class Second(unittest.TestCase):
    @unittest.expectedFailure
    def test_expected(self):
        self.fail("known")
'''

RUNNERS = ("unittest", "pytest")


class Backends(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pytest-runner-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        (self.root / "tests").mkdir(parents=True)
        shutil.copyfile(SOURCE / "tests/run.py", self.root / "tests/run.py")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}

    def write(self, name, text):
        (self.root / "tests" / name).write_text(text)

    def run_tests(self, runner, *names):
        return subprocess.run([sys.executable, "-B", "tests/run.py", "--runner", runner, *names],
                              cwd=self.root, env=self.env, text=True, capture_output=True,
                              timeout=120)

    def test_each_problem_kind_is_reported_at_once_with_a_rerun_hint_by_both(self):
        self.write("test_kinds.py", KINDS)
        self.write("test_broken.py", "import missing_module_for_fixture\n")
        for runner in RUNNERS:
            with self.subTest(runner=runner):
                result = self.run_tests(runner)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                for label in ("FAIL: test_kinds.Kinds.test_1_assertion",
                              "ERROR: test_kinds.Kinds.test_2_exception",
                              "FAIL: test_kinds.Kinds.test_3_subtests (case='bad')",
                              "UNEXPECTED SUCCESS: test_kinds.Kinds.test_4_unexpected_success",
                              "unexpected-exception-evidence", "missing_module_for_fixture",
                              "Rerun one for diagnosis (not full validation): python3 tests/run.py"):
                    self.assertIn(label, result.stderr)
                self.assertIn("after-failure-marker", result.stdout + result.stderr)

    def test_failure_report_precedes_later_test_output_under_both(self):
        self.write("test_kinds.py", KINDS)
        for runner in RUNNERS:
            with self.subTest(runner=runner):
                merged = subprocess.run(
                    [sys.executable, "-B", "tests/run.py", "--runner", runner, "test_kinds"],
                    cwd=self.root, env=self.env, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, timeout=120).stdout
                self.assertLess(merged.index("FAIL: test_kinds.Kinds.test_1_assertion"),
                                merged.index("after-failure-marker"), merged)

    def test_each_failing_kind_alone_fails_under_both(self):
        self.write("test_kinds.py", KINDS)
        for test in ("test_1_assertion", "test_2_exception", "test_3_subtests",
                     "test_4_unexpected_success"):
            for runner in RUNNERS:
                with self.subTest(test=test, runner=runner):
                    result = self.run_tests(runner, "test_kinds.Kinds." + test)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("test_kinds.Kinds." + test, result.stderr)
                    self.assertIn("FOCUSED DIAGNOSTIC RUN", result.stderr)

    def test_same_tests_pass_under_both_including_subtests_and_expected_failures(self):
        self.write("test_ok.py", PASSING)
        unit = self.run_tests("unittest")
        self.assertEqual(unit.returncode, 0, unit.stderr)
        self.assertIn("Ran 3 tests", unit.stderr)
        py = self.run_tests("pytest")
        self.assertEqual(py.returncode, 0, py.stdout + py.stderr)
        self.assertRegex(py.stdout, r"2 passed, 1 xfailed")
        collected = sorted(set(re.findall(r"tests/test_ok\.py::(\w+::\w+)", py.stdout)))
        self.assertEqual(collected, ["First::test_a", "First::test_b", "Second::test_expected"])
        for runner in RUNNERS:
            with self.subTest(runner=runner):
                module = self.run_tests(runner, "test_ok")
                self.assertEqual(module.returncode, 0)
                self.assertNotIn("FOCUSED DIAGNOSTIC RUN", module.stderr)
                one = self.run_tests(runner, "test_ok.First.test_a")
                self.assertEqual(one.returncode, 0, one.stdout + one.stderr)
                self.assertIn("FOCUSED DIAGNOSTIC RUN", one.stderr)

    def test_empty_suites_and_bad_names_are_rejected_by_both(self):
        self.write("test_empty.py", "import unittest\n")
        for runner in RUNNERS:
            with self.subTest(runner=runner):
                empty = self.run_tests(runner)
                self.assertEqual(empty.returncode, 1)
                self.assertIn("no tests discovered", empty.stderr)
                for bad in ("test_missing", "kinds", "test_empty..X", "test_empty.A.b.c"):
                    rejected = self.run_tests(runner, bad)
                    self.assertEqual(rejected.returncode, 1)
                    self.assertIn("named test modules must be existing", rejected.stderr)
                missing = self.run_tests(runner, "test_empty.Nope.test_nope")
                self.assertEqual(missing.returncode, 1)
                self.assertIn("tests/run.py ", missing.stderr)
                self.assertIn("test_empty.Nope", missing.stderr)
        self.assertEqual(self.run_tests("nose").returncode, 1)

    def test_pytest_backend_loads_no_third_party_plugins(self):
        self.write("test_ok.py", PASSING)
        result = self.run_tests("pytest")
        self.assertEqual(result.returncode, 0)
        self.assertNotRegex(result.stdout, r"(?m)^plugins: ")


if __name__ == "__main__":
    unittest.main()
