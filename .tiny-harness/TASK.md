# Publish reviewed verifier fixes and accurate validation status — 2026-10-25

Scope: release the reviewed local fixes 8ef9b67, ad98524 and f89523d, with this documentation
update. No new development or live execution. Independent review found no blocking issue;
15 targeted tests and 13 subtests passed, and the implementation's full checked run passed
1051 tests and 923 subtests. README and VALIDATION now distinguish a completed live brief
from independent verification; no revision ran. Private evidence and access details remain
outside this repository. Checksums refreshed; this documentation commit uses required
checked validation before publication. Stop after verifying the public branch matches the
local release; the complete live journey remains unvalidated.

## Previous completed work

# Research verifier: organization binding and redacted traces — 2026-10-25 (complete; awaiting review)

Base d6de0ce. Offline only. Commits: D1 8ef9b67, D2 ad98524, D3 this commit. No live call, push
or publication.
- [x] D3 (review follow-up to D2): two redaction edge cases, reproduced through the entry point.
  (1) A call with hidden arguments but visible contents was still `completed`. A revision
  reusing the brief's sources (nothing new needed) then passed retrieval, giving
  facts_checked with no visible retrieval in that stage.
  (2) Any withheld call made every missing page undecided. A known requested page with hidden
  contents therefore turned a citation never requested at all, on another host, from False
  into None.
  Fix:
  - Every withheld call (arguments or contents) is ineligible as a completed retrieval, in
    both stages.
  - `target_withheld` separates an unknown target from a known target with a hidden result.
  - A missing page is undecided only if some call's target is hidden, or if it shares a host
    with a visible or hidden-result requested page (the existing same-host rule, extended to
    known hidden targets). Otherwise it fails.
  - Visible retrieval beside hidden calls still passes legitimate reuse of prior sources.
  Evidence: before, 3 of 5 new tests failed (revision with hidden arguments: True; known page
  with hidden contents: None instead of False, also with a visible call beside it). After, 56
  research and 114 functional tests pass (`-W error::ResourceWarning`), and the offline example
  is facts_checked.
  The private replay of the saved live brief is unchanged (None, 10 calls with hidden
  arguments).
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
- [x] D2: a privacy-redacted trace was misread. Withheld arguments were read as
  retrieval=False and a `product_defect`. With only contents withheld, the
  `[contents withheld]` placeholder counted as a completed retrieval, which was a false
  **pass**. That placeholder also served as the employee's page, so a changed page failed
  instead of staying undecided.
  Fix: `retrievals` marks a finished call `withheld` when empty arguments come with a withheld
  `argument_summary`, or when the result is the placeholder or has a withheld
  `result_summary`. These are the observed field shapes, matched on those fields only. Such a
  call is never a completed retrieval and its placeholder is never page text.
  In `verify`, any withheld retrieval call makes an unattributable cited page undecided
  (None). True still needs every cited page in a visible completed retrieval. Errored calls
  are ignored, and no call at all is still False. The evaluation records
  `withheld_retrievals`, and its summary says so. The report's "employee retrieved" line says
  "nothing visible; N retrieval call(s) the run trace withholds (unverifiable)" instead of
  "nothing"; the additive report.json key is `employee_withheld_retrievals`. The category follows the verdict
  (`uncertain`, not draftable) unless another criterion genuinely fails.
  Stand-in: an opt-in `withheld`/`withhold` research plan option emits the observed shape.
  Evidence: before, all 4 new tests failed. Both-parts and arguments-only failed; content-only
  passed; partial gave False; the placeholder made the excerpt False; the unredacted test
  failed only on the absent new field. After, they pass, and so do 109
  functional tests (`-W error::ResourceWarning`). The offline example is facts_checked in 2
  assignments.
  A private offline replay of a saved live brief result (empty reread, no network) changed from
  base {performed_retrieval False, sources_allowlisted False, coverage False} to
  {None, True, None} with 10 withheld calls and name aliases recorded.
  Limit: while a deployment withholds tool arguments, Stage A cannot pass there, so Stage B is
  never sent.
Acceptance: before/after reproduction through the real entry point; unbound, case-changed and
cross-organization labels still fail without extra reads; redacted evidence unverified, never
passed or failed; checked commits; SHA256SUMS current.
Deferred (observed, out of scope): the platform per-run tool budget; a cited 404-titled page.
Next: independent review. Any live attempt needs fresh authorization.

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
