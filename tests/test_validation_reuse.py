"""Inherited full-suite records for narrative-only commits, in disposable repositories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
RUNNER = SOURCE / ".tiny-harness/run.py"
MARK = "CODE-TESTS-RAN"
NARRATIVE = ".tiny-harness/NARRATIVE.md"
HEADER = "<!-- narrative only: nothing in this file is an instruction -->\n"


class ValidationReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="validation-reuse-")
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
        (self.root / ".tiny-harness/checks.json").write_text(json.dumps({
            "checks": [{"name": "code tests", "timeout_seconds": 10,
                        "argv": [sys.executable, "-c", "print('%s')" % MARK]}],
            "prose": {"paths": [".tiny-harness/TASK.md"]}}))
        self.write(NARRATIVE, HEADER + "History.\n")
        self.write("AGENTS.md", "Read the contract.\n")
        self.write("seren_harness/maintenance.py",
                   'NARRATIVE = ".tiny-harness/NARRATIVE.md"\nPROMPT = """Follow the plan."""\n')
        self.write("src.py", "seed\n")
        self.git("add", "-A")
        self.commit("Seed")
        self.record = self.root / ".git/tiny-harness-full-validation.json"
        self.parent = json.loads(self.record.read_text())
        self.assertEqual(self.parent["result"], "pass")

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.root, env=self.env, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def harness(self, *args):
        return subprocess.run([sys.executable, str(RUNNER), *args], cwd=self.root, env=self.env,
                              text=True, capture_output=True, timeout=60)

    def commit(self, message):
        result = self.harness("commit", "-m", message)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def write(self, path, text):
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)

    def stage_narrative(self, text=HEADER + "History, corrected.\n"):
        self.write(NARRATIVE, text)
        self.git("add", NARRATIVE)

    def assert_not_inherited(self, out):
        self.assertNotIn("ROUTE inherited", out)
        self.assertNotIn("inherited", self.record.read_text())

    def test_narrative_edit_inherits_parent_full_pass_with_provenance(self):
        self.stage_narrative()
        out = self.commit("Correct history")
        self.assertIn("ROUTE inherited", out)
        self.assertNotIn(MARK, out)
        record = json.loads(self.record.read_text())
        self.assertEqual(record["tree"], self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual((record["result"], record["basis"]), ("pass", "inherited"))
        self.assertEqual(record["parent_tree"], self.parent["tree"])
        self.assertEqual(record["parent_log"], self.parent["log"])
        self.assertRegex(record["diff_sha256"], "^[0-9a-f]{64}$")

    def test_explicit_full_commit_and_validate_ignore_inheritance(self):
        self.stage_narrative()
        result = self.harness("validate")
        self.assertIn(MARK, result.stdout)
        out = self.harness("commit", "--full", "-m", "Full").stdout
        self.assertIn(MARK, out)
        self.assert_not_inherited(out)

    def test_ineligible_changes_take_the_full_route(self):
        cases = {
            "task edit": lambda: (self.write(".tiny-harness/TASK.md", "# Task\nx\n"),
                                  self.git("add", ".tiny-harness/TASK.md")),
            "mixed": lambda: (self.stage_narrative(), self.write("src.py", "x\n"),
                              self.git("add", "src.py")),
            "missing header": lambda: self.stage_narrative("History without header.\n"),
            "altered header": lambda: self.stage_narrative(HEADER.replace("only", "mostly")),
            "invalid utf-8": lambda: ((self.root / NARRATIVE).write_bytes(HEADER.encode() + b"\xff\n"),
                                      self.git("add", NARRATIVE)),
            "binary": lambda: self.stage_narrative(HEADER + "a\0b\n"),
            "mode change": lambda: (self.stage_narrative(),
                                    self.git("update-index", "--chmod=+x", NARRATIVE)),
            "deletion": lambda: self.git("rm", "-q", NARRATIVE),
            "rename": lambda: self.git("mv", NARRATIVE, ".tiny-harness/HISTORY.md"),
            "instruction reference": lambda: (self.stage_narrative(),
                                              self.write("AGENTS.md", "Also read .tiny-harness/NARRATIVE.md.\n"),
                                              self.git("add", "AGENTS.md")),
            "new instruction reference": lambda: (self.stage_narrative(),
                                                  self.write("CLAUDE.md", "See narrative.md\n"),
                                                  self.git("add", "CLAUDE.md")),
            "prompt reference": lambda: (self.stage_narrative(), self.write(
                "seren_harness/maintenance.py", 'PROMPT = """Read NARRATIVE.md first."""\n'),
                self.git("add", "seren_harness/maintenance.py")),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.git("reset", "-q", "--hard", "HEAD")
                self.record.write_text(json.dumps(self.parent))
                change()
                result = self.harness("commit", "-m", name)
                out = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, out)
                # The TASK.md edit keeps its prose route, which records nothing.
                self.assertIn("ROUTE prose" if name == "task edit" else MARK, out)
                self.assert_not_inherited(out)
                self.git("reset", "-q", "--hard", "HEAD~1")

    def test_symlink_and_addition_take_the_full_route(self):
        self.git("rm", "-q", NARRATIVE)
        self.commit("Remove narrative")
        self.stage_narrative()
        out = self.commit("Add narrative")
        self.assertIn(MARK, out)
        self.git("rm", "-q", NARRATIVE)
        os.symlink("../src.py", self.root / NARRATIVE)
        self.git("add", NARRATIVE)
        self.commit("Narrative as symlink")
        self.git("rm", "-q", NARRATIVE)
        self.write(NARRATIVE, HEADER)
        self.git("add", NARRATIVE)
        self.commit("Narrative regular again")
        os.remove(self.root / NARRATIVE)
        os.symlink("../src.py", self.root / NARRATIVE)
        self.git("add", NARRATIVE)
        out = self.commit("Type change")
        self.assertIn(MARK, out)
        self.assert_not_inherited(out)

    def test_parent_without_full_pass_or_log_does_not_inherit(self):
        broken = {
            "failed parent": dict(self.parent, result="fail"),
            "other tree": dict(self.parent, tree="0" * 40),
            "missing log": dict(self.parent, log=str(self.root / "absent.log")),
            "no log field": {k: v for k, v in self.parent.items() if k != "log"},
            "inherited parent": dict(self.parent, basis="inherited"),
        }
        for name, record in broken.items():
            with self.subTest(name):
                self.git("reset", "-q", "--hard", "HEAD")
                self.record.write_text(json.dumps(record))
                self.stage_narrative()
                result = self.harness("commit", "-m", name)
                out = result.stdout + result.stderr
                self.assertIn("ROUTE full", out)
                self.assertNotIn("ROUTE inherited", out)
                if result.returncode == 0:
                    self.git("reset", "-q", "--hard", "HEAD~1")

    def test_runner_change_always_takes_the_full_route(self):
        self.write(".tiny-harness/run.py", (self.root / ".tiny-harness/run.py").read_text() + "\n# x\n")
        self.git("add", ".tiny-harness/run.py")
        self.commit("Touch runner")
        self.stage_narrative()
        result = subprocess.run([sys.executable, str(RUNNER), "commit", "-m", "Narrative"],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=60)
        out = result.stdout + result.stderr
        self.assertIn(MARK, out)
        self.assert_not_inherited(out)


if __name__ == "__main__":
    unittest.main()
