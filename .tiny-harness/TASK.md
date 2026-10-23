# Behavior-preserving maintenance refactor — 2026-10-23 (in progress)

Base 7bb0431. Three ordered commits, no observable behavior change, offline only:
- [x] A: shared per-entry employee-precondition predicates (pins, lifecycle, tool policy) used
  by readiness and the campaign; each caller keeps its selection, messages, rows and order.
  Duplicate/ordered catalog entries pinned by new tests (they also pass on the base source).
  Comparison: 94 functional tests' artifacts in 79 scenario dirs equal to base; resume of
  base-made research and journey state equal (2 and 5 assignments, none repeated).
- [ ] B: drafts, approval and GitHub submission in `functional_issues.py`; byte-identical
  drafts and hashes; transport mocks retargeted to the real lookup.
- [ ] C: research verdict-to-status mapping as a pure function; explicit read-back branch.
Acceptance: before/after comparison of the functional suites' synthetic artifacts (exit codes,
CLI output, readiness rows, state, store rows, stand-in calls, drafts) equal after
normalizing ids/timestamps; same-input draft re-render byte-identical; offline example
facts_checked in two assignments; checked commits; SHA256SUMS current.
Deferred, unchanged: readiness instruction-row status chosen by substring match (path text can
turn mismatch into blocked); CLI parser; tier0_cases.py.

## Previous completed change

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
