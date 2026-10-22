"""Run this project's required tests, rejecting empty discovery.

With no arguments every test module is discovered. Named modules (`test_tier0 ...`) run
only those, for the focused commit route in .tiny-harness/checks.json. A single test can be
named for diagnosis (`test_x.Class.test_y`); that is a FOCUSED DIAGNOSTIC run, never full
validation. Either way the project package must import from this checkout (the staged
snapshot during validation), never from the working tree, an installed copy or an
inherited PYTHONPATH.

Each failure, error, failing subtest and unexpected success is printed with its test id
and traceback the moment it happens (flushed), so a saved validation log identifies it
without rerunning the suite; the usual end-of-run summary is kept.

The default runner is pytest over the same discovery root and named targets: no cache, no
reruns or parallelism and no plugins beyond pytest's own, with the same immediate reports,
rerun hint, empty-suite rejection and import check. `--runner unittest` (first argument)
runs the stdlib unittest runner instead, for diagnosis only; tests stay unittest `TestCase`
classes so both runners, and the independent unittest floor, discover them."""
from pathlib import Path
import importlib
import os
import re
import sys
import unittest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def snapshot_imports():
    if not (ROOT / "seren_harness/__init__.py").is_file():
        return None
    if sys.path[:1] != [str(ROOT)]:
        sys.path.insert(0, str(ROOT))
    package = importlib.import_module("seren_harness")
    origin = Path(package.__file__ or "<namespace>").resolve()
    if origin != ROOT / "seren_harness/__init__.py":
        return "seren_harness imports from %s, not this checkout %s" % (origin, ROOT)
    return None


NAME = re.compile(r"(test_[a-z0-9_]+)(\.[A-Za-z_][A-Za-z0-9_]*){0,2}")


class ImmediateResult(unittest.TextTestResult):
    """Report each problem with its id and traceback at once, not only in the summary."""

    def report(self, label, test, err=None):
        detail = self._exc_info_to_string(err, test) if err is not None else ""
        self.stream.write("\n%s: %s\n%s%s\n" % (label, test.id(), detail, "-" * 70))
        self.stream.flush()
        self.problems.append(test.id())

    def startTestRun(self):
        self.problems = []
        super().startTestRun()

    def addError(self, test, err):
        super().addError(test, err)
        self.report("ERROR", test, err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.report("FAIL", test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self.report("FAIL" if failed else "ERROR", subtest, err)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.report("UNEXPECTED SUCCESS", test)


def rerun_hint(result, stream, names=(), option=""):
    # An import or lookup failure is reported as unittest.loader._FailedTest.<last part>;
    # point back at the requested name (or the module, for discovery).
    def name(test_id):
        test_id = test_id.split(" ")[0]
        if not test_id.startswith("unittest.loader._FailedTest."):
            return test_id
        part = test_id.rsplit(".", 1)[1]
        return next((n for n in names if n == part or n.endswith("." + part)), part)
    ids = list(dict.fromkeys(name(i) for i in result.problems))
    if ids:
        stream.write("\nFailing tests (%d): %s\n" % (len(ids), ", ".join(ids)))
        stream.write("Rerun one for diagnosis (not full validation): python3 tests/run.py %s%s\n"
                     % (option, ids[0].replace("tests.", "", 1)))
        stream.flush()


def check_names(names):
    if any(not NAME.fullmatch(n) or not (TESTS / (NAME.fullmatch(n).group(1) + ".py")).is_file()
           for n in names):
        raise ValueError("named test modules must be existing tests/test_*.py modules "
                         "(optionally .Class or .Class.test): %s" % names)


def load(names):
    if not names:
        return unittest.defaultTestLoader.discover(str(TESTS))
    check_names(names)
    if str(TESTS) not in sys.path:
        sys.path.insert(1, str(TESTS))
    return unittest.defaultTestLoader.loadTestsFromNames(names)


def dotted(nodeid):
    """tests/test_x.py::Class::test_y[param] -> test_x.Class.test_y, the name this runner takes."""
    path, _, rest = nodeid.partition("::")
    parts = [Path(path).stem] + [p.split("[")[0] for p in rest.split("::") if p]
    return ".".join(parts)


class ImmediateReports:
    """pytest hooks: the same immediate FAIL/ERROR/UNEXPECTED SUCCESS reports as unittest's."""

    def __init__(self, stream):
        self.stream, self.problems = stream, []

    def report(self, label, name, detail):
        self.stream.write("\n%s: %s\n%s\n%s\n" % (label, name, detail, "-" * 70))
        self.stream.flush()
        self.problems.append(name)

    def pytest_collectreport(self, report):
        if report.failed:
            self.report("ERROR", dotted(report.nodeid), report.longreprtext)

    def pytest_runtest_logreport(self, report):
        if not report.failed:
            return
        name = dotted(report.nodeid)
        kwargs = getattr(getattr(report, "context", None), "kwargs", None)
        if kwargs:  # a failing subtest, labelled as unittest labels it (values are reprs)
            name += " (%s)" % ", ".join("%s=%s" % item for item in kwargs.items())
        text = report.longreprtext
        crash = getattr(getattr(report.longrepr, "reprcrash", None), "message", "")
        if text.strip() == "Unexpected success":
            label = "UNEXPECTED SUCCESS"
        elif report.when == "call" and crash.startswith("AssertionError"):
            label = "FAIL"
        else:
            label = "ERROR"
        self.report(label, name, text)

    def pytest_sessionfinish(self, session):
        # unittest's summary line on the same stream, so logs read the same under both runners.
        count = session.testscollected
        self.stream.write("\nRan %d test%s (pytest)\n" % (count, "" if count == 1 else "s"))
        self.stream.flush()


def run_pytest(names):
    try:
        import pytest
    except ImportError as problem:
        print("ERROR: pytest runner unavailable: %s" % problem, file=sys.stderr)
        return 1
    for variable in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):  # only these fixed options
        os.environ.pop(variable, None)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    targets = ["::".join([str(TESTS / (n.split(".")[0] + ".py"))] + n.split(".")[1:])
               for n in names] or [str(TESTS)]
    hooks = ImmediateReports(sys.stderr)
    # As under unittest: test output is not captured (it stays in the validation log), and a
    # module that fails to import is an error while the rest still run.
    code = pytest.main(["-p", "no:cacheprovider", "-v", "-s", "--continue-on-collection-errors",
                        "--rootdir", str(ROOT), *targets], plugins=[hooks])
    if code == pytest.ExitCode.NO_TESTS_COLLECTED:
        print("ERROR: no tests discovered; required validation did not run.", file=sys.stderr)
        return 1
    if code != pytest.ExitCode.OK and not hooks.problems:  # e.g. a named test not found
        hooks.problems.extend(names[:1])
    rerun_hint(hooks, sys.stderr, names)
    return 0 if code == pytest.ExitCode.OK else 1


def main(args=()):
    args = list(args)
    runner = "pytest"
    if args[:1] == ["--runner"]:
        runner, args = (args[1] if len(args) > 1 else ""), args[2:]
    if runner not in ("unittest", "pytest"):
        print("ERROR: --runner must be unittest or pytest", file=sys.stderr)
        return 1
    names = args
    error = snapshot_imports()
    if error:
        print("ERROR: " + error, file=sys.stderr)
        return 1
    single = any("." in n for n in names)
    try:
        if runner == "unittest":
            suite = load(list(names))
        else:
            check_names(names)
    except ValueError as problem:
        print("ERROR: " + str(problem), file=sys.stderr)
        return 1
    if runner == "unittest" and suite.countTestCases() == 0:
        print("ERROR: no tests discovered; required validation did not run.", file=sys.stderr)
        return 1
    if single:
        print("FOCUSED DIAGNOSTIC RUN of %s: not full validation and never commit or live "
              "evidence." % ", ".join(names), file=sys.stderr, flush=True)
    if runner == "pytest":
        return run_pytest(names)
    runner = unittest.TextTestRunner(verbosity=2, resultclass=ImmediateResult)
    result = runner.run(suite)
    rerun_hint(result, runner.stream, names, "--runner unittest ")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
