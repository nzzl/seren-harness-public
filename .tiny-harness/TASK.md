# Research verifier: organization binding and redacted traces — 2026-10-25 (in progress)

Base d6de0ce. Offline only; two commits, one per defect. No live call, push or publication.
- [x] D1: the assignment named both an organization id and a name without saying which to use
  in `organization`, and the verifier and reread look organizations up by id only. Exact-name
  labels therefore refused allowlisted pages ("not allowlisted"), reread nothing and checked no
  claim. Two identical unbound labels also passed `claims_cited`. Fix: the sourceRules ask for
  the id, and the schema enum lists the ids in every organization field.
  `organization_labels`/`organization_id` bind ids, plus exact names that are unique and no
  other organization's id; no folding or partial match. Labels bind once in `verify` (both
  stages, previous brief included), and the same resolver drives the reread. An unbound label
  is refused unread, with the label named. A claim needs a source of the same *bound*
  organization. Aliases are recorded as `organization_aliases`.
  Evidence: before the fix, the 4 new entry-point tests failed as described (the resolver's own
  unit test errored, absent); after, all 6 pass with the existing functional suites. Spec and criteria digests are unchanged (base and new `generate` equal).
  A base-made campaign interrupted after a name-labelled brief resumes under the new code to
  facts_checked with its spec unchanged.
- [ ] D2: redacted trace (withheld arguments/contents) read as retrieval=False, or as completed
  retrieval when only contents were withheld (a false pass).
Acceptance: before/after reproduction through the real entry point; unbound, case-changed and
cross-organization labels still fail without extra reads; redacted evidence unverified, never
passed or failed; checked commits; SHA256SUMS current.
Deferred (observed, out of scope): the platform per-run tool budget; a cited 404-titled page.

## Previous completed change

# Behavior-preserving maintenance refactor — 2026-10-23 (complete; awaiting review)

Base 7bb0431. Three ordered commits, no observable behavior change, offline only:
- [x] A: shared per-entry employee-precondition predicates (pins, lifecycle, tool policy) used
  by readiness and the campaign; each caller keeps its selection, messages, rows and order.
  Duplicate/ordered catalog entries pinned by new tests (they also pass on the base source).
  Comparison: 94 functional tests' artifacts in 79 scenario dirs equal to base; resume of
  base-made research and journey state equal (2 and 5 assignments, none repeated).
- [x] B: drafts, approval and GitHub submission in `functional_issues.py` (one shared
  sanitize/pseudonymize helper, each renderer's call order kept); `ConfigError`, `iso`,
  `parse_time`, `campaign_dir` and outcome categories in `functional_common.py`; one-way imports;
  every moved name still importable from `functional`. Transport mocks retargeted to
  `functional_issues.GhTransport`; a new fake-only test proves submission without an injected
  transport constructs through that lookup. Comparison: moved definitions verbatim; 32 drafts
  re-rendered from base-made stores/ledgers byte-identical (Markdown and metadata, hashes,
  paths); base-made approvals give identical outcomes (filed once, expired/changed/draft-mode
  refused, uncertain create not retried); functional suites and resume equal to base.
- [x] C: `functional_research.journey_status` holds the research verdict-to-status mapping
  (blocked, observations, saves and findings stay in `record_journey`, in order); the observed
  read-back is one explicit research/database branch; read_sources and the SLEEP, MONOTONIC,
  SOURCE_TRANSPORT and SOURCE_RESOLVER seams unchanged. Comparison: new function equals the
  base mapping on 168 stage/verdict/unfinished inputs; functional suites, resume and the
  offline example (facts_checked, two assignments, revision bound to the brief) equal to base.
Commits: A 35254d4, B 141b164, C this commit; each through the checked runner. No live call,
GitHub request, push or publication. Next: independent review.
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
