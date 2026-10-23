# Seren Harness — optional developer tests and maintenance

Operators should use docs/OPERATOR.md. Nothing here is a prerequisite for `functional`,
`check-setup` or `test-live`. The starter runtime uses Python's standard library and the Seren CLI.

## Quick checks while changing the operator interface

From this folder, the new operator regression checks use the standard library and a
local fake CLI. They do not use a real account, make service requests or require Git:

```sh
python3 -B -m unittest discover -s tests -p test_operator.py
```

These cover target binding, local-only setup, response mismatches, executable/version
pins, invalid configuration, report-directory creation and write failures before service access,
timeouts without retries, and credential handling.
They are focused checks, not a substitute for release validation.

The functional campaign's behaviour checks drive `python -m seren_harness functional` through
stand-in Seren and Claude executables (tests/functional_standin.py,
tests/functional_model_standin.py) and a fake GitHub transport, with the standard library only:

```sh
python3 -B -m unittest discover -s tests -p "test_functional*.py"
```

`test_functional_journeys.py` drives both user journeys through the same entry point, using a
stand-in employee that answers only through simulated `seren_db_query` calls on the stand-in
database.

Their scripted model replies test orchestration, limits and safety handling. They do not
measure how well a real model chooses tasks.

## Full developer suite (optional for operators)

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple --require-hashes --no-deps -r requirements-dev.txt
.venv/bin/python -m pip check
.venv/bin/python tests/run.py
```

This installs development tools, including pytest, lint and offline-evaluation libraries.
The suite uses disposable fixtures and loopback services; it does not exercise a real
Seren account. A full run takes roughly eight to ten minutes on a recent Mac. Some tests are
timing-sensitive under heavy load. Tests do not run before each operator command.

Development and testing happen on macOS. The code uses Unix-specific facilities such as
`fcntl`; native Windows is not supported, Linux is untested and WSL is not guaranteed to work.

## Maintainer release checks and advanced supervision

The checked runner validates the staged Git tree in a temporary snapshot. Stage intended
changes, then run it with the development environment's Python; no shell activation is needed:

```sh
.venv/bin/python .tiny-harness/run.py validate
.venv/bin/python .tiny-harness/run.py commit -m "Reason for this change"
```

A check whose executable is `{python}` in `.tiny-harness/checks.json` runs with the interpreter
that launched the runner, so lint uses that environment's pinned Ruff (`{python} -m ruff`) and
the suite its pytest, never another `python3` or `ruff` on PATH. If the pinned tool is missing
from that environment, the check fails; nothing is installed or skipped. See
`.tiny-harness/CONTRACT.md`. Full regression and lint checks remain required for releases and
checked commits. Do not delete safety tests to hide failures.

The advanced live supervisor requires a clean committed tree with a matching full-suite
validation record. It reuses that record for the unchanged tree; it does not run the whole
suite before every live request. This requirement has not been removed. Ordinary operator
catalog reads do not use that supervisor or need its Git state.

Identity matching, CLI fingerprint/version pins, allowed commands, time limits, bounded
attempts, redaction and private reporting remain operational checks, not optional developer
tests. Advanced mutation, shutdown and restoration controls are also unchanged.

After changing released files, regenerate SHA256SUMS so it describes the actual files.
Keep operator configurations, credentials, runtime evidence and reports outside source
control. No live service test, paid call, publication or sharing is authorized by these
instructions alone.
