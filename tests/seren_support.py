"""Make tests import the package from this checkout (the staged snapshot in CI).

Also: `standin` and `prerun` for freshly written stand-in executables. macOS assesses
every newly created executable on its first run (Gatekeeper exec-time scan: about 0.15 s
median, 0.9 s and once 6.6 s observed during worker checks; later runs of the same file
about 0.01 s). Tests that time a stand-in's run must pay that one-time cost first, untimed.
"""
import os
from pathlib import Path
import subprocess
import sys

PRERUN_ENV = "SEREN_HARNESS_STANDIN_PRERUN"


def standin(script):
    """Add an early exit, taken only by `prerun`, right after the shebang line, so the
    pre-run has no side effects (no call counts, logs, sleeps or state changes)."""
    first, _, rest = script.partition("\n")
    if not first.startswith("#!"):
        raise ValueError("stand-in scripts start with a shebang")
    if "/sh" in first:
        guard = '[ -n "$%s" ] && exit 0\n' % PRERUN_ENV
    else:
        guard = ("import os as _prerun_os, sys as _prerun_sys\n"
                 "if _prerun_os.environ.get(%r): _prerun_sys.exit(0)\n" % PRERUN_ENV)
    return first + "\n" + guard + rest


def prerun(*paths):
    """Run each freshly written stand-in once, untimed (bounded at 120 s, failing loudly),
    so its first-run assessment never lands inside a timed harness call."""
    for path in paths:
        subprocess.run([str(path)], env=dict(os.environ, **{PRERUN_ENV: "1"}),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
                       check=True)

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path[:1]:
    sys.path.insert(0, str(SOURCE))

import seren_harness  # noqa: E402

if Path(seren_harness.__file__).resolve().parents[1] != SOURCE:
    raise ImportError("seren_harness imported from " + seren_harness.__file__
                      + " instead of the tested tree " + str(SOURCE))
