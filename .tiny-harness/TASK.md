# Bounded release housekeeping — 2026-10-23

Four items, no new features: (1) ruff.toml targets py311, unused mccabe setting removed;
(2) checks whose executable is `{python}` run with the interpreter that launched the runner,
so lint is `{python} -m ruff` and the suite uses that environment's pytest, never another
python3/ruff on PATH; a missing tool fails, nothing installs or skips; generic commands,
arguments, prefix confinement, timeouts and the snapshot boundary are unchanged; three runner
regressions cover absent/misleading PATH tools, snapshot imports, failure propagation, missing
modules, generic PATH commands and the confinement prefix; (3) 22 three-line placeholder docs
removed, references repointed to docs/ADVANCED.md or marked "not in this release"; hash-bound
employee-retrieval packages left byte-identical; one test reading two removed docs now skips
absent files as the runner does; (4) README, operator, advanced and development docs state
macOS-only development/testing, Unix facilities such as fcntl, no native Windows, untested
Linux, no WSL guarantee, 3.11 minimum vs 3.14.7 tested.
Verified before commit: focused modules, offline example (both stages passed, revision bound
to brief SHA-256), internal links, disabled configs. Final gate: checked commit (lint + full
suite). No live call, push or publication.

## Previous completed change

# Owner-view identity compatibility and live-result documentation — 2026-10-23

Transfer the reviewed identity binding and five synthetic regressions into the clean public
release. Keep the original repository, private configuration, logs and history outside this
checkout. Document one failed live submission and link the separately authorized issue #244;
retain the absence of a successfully completed live journey. No new live execution.

Acceptance: consistent readiness/discovery/recheck binding; required checked-commit lint and
full offline suite; synthetic example still succeeds; clean staged diff and refreshed manifest;
only the clean release commit pushed. Preserve existing sanitizer fixtures and release exclusions.

## Previous completed change

# Automated issue-finding purpose — 2026-10-23

Clarify the README's lead: an automated testing agent drives configured user journeys to find
and report user-facing issues and verify successful work. Preserve the limits on scope,
permissions and automatic issue submission; make the absence of completed live validation
prominent. Documentation only; no new capabilities, tests, workflows or live authority.
Verification: wording review, release checksums and required checked commit.
