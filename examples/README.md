# Examples

## Seren Harness research journey (offline)

[research-journey/run_offline.py](research-journey/run_offline.py) runs one synthetic
research brief and its dependent revision through the real `functional run` entry point with
a stand-in Seren CLI and fake web pages. See the [operator guide](../docs/OPERATOR.md).

## Tiny Harness adoption examples

The remaining examples come from Tiny Harness, the checked development runner in
`.tiny-harness/`.

These are optional project checks and workflow recipes, not extra harness runtime
dependencies. Install Tiny Harness first, then adapt an example to your project's
existing requirements. For check examples, merge entries into
`.tiny-harness/checks.json`; preserve existing required checks. Stage the
configuration, check scripts, source, tests, and dependency locks.

### Pure-Python src layout with unittest

This example expects `src/your_package/` and unittest tests under `tests/`.
Copy [python-src/check.py](python-src/check.py) to `tools/check_python.py` in the
adopting repository. Add the check from [python-src/checks.json](python-src/checks.json)
and replace `your_package` with the actual import package name. If that script path
already exists, review and merge it or choose a new path and update the command.

Reuse the project's isolated Python environment and existing package manager.
If dependencies are needed and no environment exists, create one with
`python3 -m venv .venv`, exclude `.venv/` from Git, and activate it with
`. .venv/bin/activate`. Avoid installing dependencies into the host's system Python.
Make `python3` and third-party tools available on PATH in the same command that
runs the harness (activate and run together, or use a runner such as `uv run`).
Use `python3` rather than a relative `.venv/bin/python` path:
the ignored virtual environment is absent from the temporary snapshot. Provision
third-party dependencies from your lock file before validation, or through an
explicit setup step appropriate to the project.

The script prioritizes the snapshot's `src/`, checks project-module origins before
and after the tests, and rejects empty discovery. This matters when the active
environment contains an editable installation (`pip install -e .`) or an older
installed copy: ordinary imports may otherwise resolve to the original checkout
or installed package and test the wrong source. A missing staged package must not
silently fall back to an external copy.

This recipe covers the named package and its imported submodules. Add checks for
other first-party packages in a multi-package project. It is not environment
isolation or a security boundary; it cannot identify deliberately removed imports,
arbitrary file reads, or every custom import mechanism. For compiled packages,
other layouts, or different test frameworks, adapt source-origin verification or
build/install the staged project in a fresh environment inside the snapshot.
Generated output must not overwrite staged source paths.

### Node with npm and a lock file

Add the check from [node/checks.json](node/checks.json). It requires Node/npm on
PATH, tracked `package.json` and `package-lock.json`, and an `npm test` script that
fails on test failure and zero tests. Pick a timeout that covers dependency setup
and tests on your project's expected machines.

`npm ci` installs the locked dependencies inside each temporary snapshot, then
`npm test` runs there. Existing ignored `node_modules` is intentionally not copied;
dependency download caches can still be reused. npm lifecycle scripts run normally
and must respect the harness rule against modifying staged files. Use read-only
formatter checks and stage generated source before validation.

The automated example probe uses a tracked local dependency to avoid registry
access. It proves that missing dependencies are installed, staged bad source fails,
and a staged repair passes. It does not establish compatibility with every npm
workspace, native addon, external service, or network setup.

### Work across multiple sessions

The [long-running-project recipe](long-running/README.md) shows how to retain an
existing backlog, resume from evidence, verify user-visible behavior, and leave a
useful handoff. It includes an optional [feature checklist](long-running/FEATURES.md).
This adds no required checks or files to the installed harness.
