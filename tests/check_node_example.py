"""Run the shipped npm recipe against a disposable, registry-free project."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SOURCE = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="node-adoption-") as directory:
        root = Path(directory)
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", npm_config_cache=str(root / "cache"),
                   npm_config_offline="true", npm_config_update_notifier="false")

        def run(argv, expected=0):
            result = subprocess.run(argv, cwd=root, env=env, text=True, capture_output=True, timeout=60)
            if result.returncode != expected:
                raise RuntimeError(result.stdout + result.stderr)
            return result

        for args in (("init", "-q"), ("config", "user.name", "Fixture Author"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("config", "commit.gpgsign", "false")):
            run(["git", *args])
        run([sys.executable, str(SOURCE / ".tiny-harness/run.py"), "install", "."])
        shutil.copyfile(SOURCE / "examples/node/checks.json", root / ".tiny-harness/checks.json")
        (root / ".gitignore").write_text("node_modules/\ncache/\n")
        (root / "vendor/audit-dep").mkdir(parents=True)
        (root / "vendor/audit-dep/package.json").write_text(json.dumps(
            {"name": "audit-dep", "version": "1.0.0", "main": "index.cjs"}))
        module = root / "vendor/audit-dep/index.cjs"
        module.write_text("module.exports = { value: 0 };\n")
        (root / "package.json").write_text(json.dumps({"name": "audit-consumer", "version": "1.0.0",
            "private": True, "scripts": {"test": "node check.cjs"},
            "dependencies": {"audit-dep": "file:vendor/audit-dep"}}))
        (root / "check.cjs").write_text("require('node:assert/strict').equal(require('audit-dep').value, 42);\n")
        run(["npm", "install", "--package-lock-only", "--no-audit", "--no-fund"])
        run(["git", "add", "."])
        module.write_text("module.exports = { value: 42 };\n")
        run(["npm", "ci", "--no-audit", "--no-fund"])
        run(["npm", "test"])
        failed = run([sys.executable, ".tiny-harness/run.py", "commit", "-m", "Reject staged bad code"], 1)
        if "AssertionError" not in failed.stderr:
            raise RuntimeError("Expected behavior failure, not a setup error: " + failed.stdout + failed.stderr)
        run(["git", "rev-parse", "--verify", "HEAD"], 128)
        run(["git", "add", "vendor/audit-dep/index.cjs"])
        run([sys.executable, ".tiny-harness/run.py", "commit", "-m", "Stage the repair"])
        if run(["git", "status", "--porcelain"]).stdout.strip():
            raise RuntimeError("Example left unexpected working-tree changes")
        print("PASS Node recipe: locked setup, staged failure, and staged repair")


if __name__ == "__main__":
    main()
