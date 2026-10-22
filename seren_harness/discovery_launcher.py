"""launchd entry point: run the discovery supervisor from a snapshot of the committed HEAD.

The working tree may hold half-written edits (an interactive session or a model worker in
progress). The supervisor, the deterministic worker and every catalog read therefore run
from `<home>/runtime/<HEAD>/`, an export of the last checked commit. A new commit becomes
active at the next tick. Standard library only; keep this file small and stable.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
KEEP = 3


def snapshot(home):
    head = subprocess.run(["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    runtime = home / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = runtime / head
    if not (target / ".complete").exists():
        partial = runtime / (head + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        archive = subprocess.Popen(["/usr/bin/git", "-C", str(REPO), "archive", head],
                                   stdout=subprocess.PIPE)
        subprocess.run(["/usr/bin/tar", "-x", "-C", str(partial)], stdin=archive.stdout,
                       check=True)
        if archive.wait() != 0:
            raise SystemExit("git archive failed")
        (partial / ".complete").write_text(head + "\n")
        shutil.rmtree(target, ignore_errors=True)
        partial.rename(target)
    old = sorted((p for p in runtime.iterdir() if p.is_dir() and p != target
                  and not p.name.endswith(".partial")), key=lambda p: p.stat().st_mtime)
    for stale in old[:-KEEP] if len(old) > KEEP else []:
        shutil.rmtree(stale, ignore_errors=True)
    return head, target


def main(argv):
    home = Path(os.path.expanduser(argv[argv.index("--home") + 1] if "--home" in argv
                                   else "~/seren-harness-live/discovery"))
    head, target = snapshot(home)
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "LANG": "C",
           "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", ""),
           "PYTHONPATH": str(target), "SEREN_DISCOVERY_REPO": str(REPO),
           "SEREN_DISCOVERY_HEAD": head}
    python = str(REPO / ".venv" / "bin" / "python")
    os.chdir(target)
    os.execve(python, [python, "-m", "seren_harness", "discovery", "supervise", "--home",
                       str(home)], env)


if __name__ == "__main__":
    main(sys.argv)
