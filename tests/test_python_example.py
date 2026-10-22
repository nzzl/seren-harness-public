"""Test the shipped Python recipe against external editable-style imports."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


SOURCE = Path(__file__).resolve().parents[1]


class PythonExampleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="python-adoption-")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "repo"
        self.root.mkdir()
        environment = base / "venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        self.python = environment / "bin/python"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0", PYTHONDONTWRITEBYTECODE="1",
                        PATH=str(environment / "bin") + os.pathsep + self.env["PATH"])
        for args in (("init", "-q"), ("config", "user.name", "Fixture Author"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("config", "commit.gpgsign", "false")):
            self.git(*args)
        self.command([sys.executable, str(SOURCE / ".tiny-harness/run.py"), "install", "."])
        (self.root / "tools").mkdir()
        shutil.copyfile(SOURCE / "examples/python-src/check.py", self.root / "tools/check_python.py")
        config = json.loads((SOURCE / "examples/python-src/checks.json").read_text())
        config["checks"][0]["argv"][-1] = "example_app"
        (self.root / ".tiny-harness/checks.json").write_text(json.dumps(config))
        (self.root / "src/example_app").mkdir(parents=True)
        self.module = self.root / "src/example_app/__init__.py"
        self.module.write_text("VALUE = 'bad'\n")
        (self.root / "src/README").write_text("Source layout marker\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_value.py").write_text(
            "import unittest\nfrom example_app import VALUE\n"
            "class ValueTest(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(VALUE, 'good')\n")
        # A src-layout editable installation exposes this directory via site-packages.
        # Use a .pth fixture without downloading packaging tools in every test run.
        purelib = self.command([str(self.python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip()
        (Path(purelib) / "editable_fixture.pth").write_text(str(self.root / "src") + "\n")
        self.git("add", ".")

    def command(self, argv, expected=0):
        result = subprocess.run(argv, cwd=self.root, env=self.env, text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def git(self, *args):
        return self.command(["git", *args]).stdout.strip()

    def commit(self, expected):
        return self.command([str(self.python), ".tiny-harness/run.py", "commit", "-m", "Test adoption"], expected)

    def test_unstaged_editable_repair_cannot_mask_staged_failure(self):
        self.module.write_text("VALUE = 'good'\n")
        # Demonstrate the false-green trigger before exercising the shipped recipe.
        self.command([str(self.python), "-c", "import example_app; assert example_app.VALUE == 'good'"])
        result = self.commit(1)
        self.assertIn("AssertionError", result.stderr)
        self.assertNotIn("PASS all", result.stdout)
        self.command(["git", "rev-parse", "--verify", "HEAD"], expected=128)
        self.git("add", "src/example_app/__init__.py")
        self.commit(0)
        self.assertEqual(self.git("show", "HEAD:src/example_app/__init__.py"), "VALUE = 'good'")

    def test_missing_staged_package_cannot_fall_back_to_editable_install(self):
        self.git("rm", "--cached", "src/example_app/__init__.py")
        self.module.write_text("VALUE = 'good'\n")
        result = self.commit(1)
        self.assertIn("resolved outside snapshot source", result.stderr)

    def test_namespace_package_with_external_paths_is_rejected(self):
        self.module.unlink()
        (self.root / "src/example_app/value.py").write_text("VALUE = 'good'\n")
        self.git("add", "src")
        result = self.commit(1)
        self.assertIn("resolved outside snapshot source", result.stderr)

    def test_external_source_directory_symlink_is_rejected(self):
        external = self.root.parent / "external-source"
        (self.root / "src").rename(external)
        (external / "example_app/__init__.py").write_text("VALUE = 'good'\n")
        (self.root / "src").symlink_to(external, target_is_directory=True)
        self.git("add", "src")
        result = self.commit(1)
        self.assertIn("Expected src directory inside the snapshot", result.stderr)

    def test_empty_discovery_is_rejected(self):
        self.module.write_text("VALUE = 'good'\n")
        (self.root / "tests/test_value.py").unlink()
        self.git("add", "-u", "tests/test_value.py")
        (self.root / "tests/README").write_text("No tests present\n")
        self.git("add", "src", "tests")
        result = self.commit(1)
        self.assertIn("No tests discovered", result.stderr)


if __name__ == "__main__":
    unittest.main()
