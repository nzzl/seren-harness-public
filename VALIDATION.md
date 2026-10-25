# Validation

What was checked for this release, and what was not.

## Initial release offline checks

The initial release checks ran on macOS against a fresh copy of the released files, in a temporary location
with a temporary `HOME` and no Seren, Claude or GitHub credentials. No real service was
contacted.

| Check | Result |
|---|---|
| Required checks (`python3 .tiny-harness/run.py validate`, development tools from `requirements-dev.txt`, Python 3.14.7) | `PASS basic Python lint`; `1023 passed, 872 subtests passed`, none skipped; `PASS all 2 required checks` |
| Offline example (`python3 examples/research-journey/run_offline.py`) | Journey `facts_checked` in 2 assignments. The revision request contained the recorded brief and its SHA-256. `report.md` written and inspected |
| `functional check` on the unfilled research example | Stops before any request with a setup message (exit 5) |
| Disabled configurations | Existing tests verify that all-false permissions, a missing owner declaration or missing routing evidence lead to zero service and source calls, and that GitHub stays draft-only |
| Markdown links | All internal links resolve |

The full run covered staged tree `694be9f0158279a5fa25fba42f6137947adca6b3`. Two changes
followed, neither to executable code:
- one descriptive text field in the synthetic case `cases/scott-saved-research-reading.json`
  was generalized; the tests that read cases and catalogs were rerun (92 passed, 48 subtests);
- this file and `SHA256SUMS` were written.

A subsequent documentation-only update clarified the README's purpose and added the original
Seren Harness copyright credit under MIT, retaining the Tiny Harness notices. Packaging notes
and checksums were refreshed. No executable code, test or fixture changed in that update;
the prior validation results remain applicable.

The sanitizer test fixture was subsequently replaced by a visibly invalid synthetic account
and signature. A direct redaction assertion also checks the token pattern without relying on
a credential prefix or registered secret. Runtime sanitization code is unchanged. This change
is subject to the required checked-commit validation before upload.

`SHA256SUMS` lists the final files.

The required checks were first run with `TMPDIR` unset by the test environment. One sandbox
test then failed because it needs a writable temporary directory. With `TMPDIR` restored, it
and the full run passed. No check was weakened.

The test suite drives the real command-line entry points against stand-in boundaries:
- a stand-in Seren CLI implementing the CLI 0.10.0 command shapes;
- a stand-in Claude CLI;
- a fake web;
- a fake GitHub transport;
- loopback fixtures.

These tests show how the harness behaves: orchestration, limits, verification rules and
safety stops. They do not measure the real service or real employee quality.

Python 3.11 or later is the supported minimum. The full suite was run on 3.14.7 only. The
offline example also ran on 3.9.6 and 3.10.14, which does not extend the supported range.

## Identity compatibility update — 2026-10-23

The functional campaign now accepts the supported owner view without `agent_identity_id` in
`shared_running` mode. Readiness, discovery and pre-assignment checks use the same binding.
It preserves deployment, inspected name/revision and instruction verification, records
unavailable fields explicitly, and blocks missing identities and changed/lost/new evidence.
Other employee modes retain the full identity contract.

Five synthetic regressions cover readiness and both journey stages, wrong or missing identity
and pins, changes before the revision, loss of a recorded full identity, and the invalid case
where two missing identities would otherwise compare equal. The implementation passed the
94-test functional suite and the full 1028-test suite (880 subtests) in the working copy.
The clean public update is also gated by the checked-commit runner's lint and full offline
suite; private validation logs are not shipped. No credential or runtime fixture is imported.

## Bounded live attempt — 2026-10-23

Using Seren CLI 0.10.0, read-only readiness passed and the harness submitted one research-brief
assignment. Seren accepted it and returned a terminal dispatch error before employee execution:

```text
Core dispatch requires its exact prepared framework run_id
```

The service reported `execution_observation: not_started`, zero iterations and no employee
tool calls. No brief was produced, so the conditional revision was not sent. The harness
recorded the failure without retrying; temporary operator permissions were disabled afterward.
There were three readiness reads and nine campaign CLI calls, including one submission.
No verifier source request or harness Discord/GitHub posting call occurred during that run.

See [Seren issue #244](https://github.com/serenorg/seren/issues/244), filed separately with the
owner's explicit approval. One occurrence does not establish reproducibility or root cause;
the report's automatic `product_defect` label remains provisional. The request omitted `run_id`
as the published deployment-scoped contract requires. No speculative ID workaround was added.

This demonstrates live readiness, submission, polling and failure recording. It does not
validate successful employee research, evidence checking or a live follow-up. A campaign's
`complete` status or exit code 0 means orchestration ended, not that the journey succeeded.
Private operator configuration, raw receipts, run identifiers and logs are not included here.

## Release housekeeping — 2026-10-23

Lint now targets Python 3.11. Required checks run with the interpreter that launched the
checked runner (`.venv/bin/python .tiny-harness/run.py commit ...`), including its pinned Ruff;
new runner tests cover misleading or absent `python3`/`ruff` on PATH. Placeholder design-note
pages were removed and their references repaired; internal Markdown links resolve. The offline
example still ends `facts_checked` with both stages passed and the revision bound to the
brief's SHA-256. The change is gated by the checked commit's lint and full offline suite on
macOS with Python 3.14.7. No live request was made.

## Maintenance refactor — 2026-10-23

Three behavior-preserving changes to the functional campaign: readiness and the campaign share
per-entry employee pin, lifecycle and tool-policy predicates (each keeps its own catalog
selection, rows and messages); issue drafting, approval and GitHub submission moved to
`seren_harness/functional_issues.py` (shared primitives in `functional_common.py`, all
previous `functional` names still importable); the research journey's status mapping is a
pure function and the read-back step an explicit research/database branch.

Offline evidence, all synthetic: the functional test modules' artifacts (exit codes, command
output, readiness rows, campaign state, store rows, stand-in call sequences, reports, drafts)
were compared before and after with identifiers and timestamps normalized, and were equal;
campaigns started by the previous code resumed identically; drafts re-rendered from saved
campaigns were byte-identical, including their hashes, and existing approvals behaved the
same with a fake transport. New tests pin catalog-entry order and duplicate handling, the
research status mapping and the GitHub transport patch point. The offline example still ends
`facts_checked` in two assignments with the revision bound to the brief's SHA-256. Each commit
passed the checked runner's lint and full offline suite on macOS with Python 3.14.7. No live
request was made, and nothing here changes what is listed below as not validated.

## Research verifier fixes — 2026-10-25

Organization fields now explicitly request canonical IDs. Exact, uniquely matching listed
names are accepted as recorded aliases; unknown or ambiguous labels cannot widen source
access. The same binding applies to claims, source rereads and the revision.

Hidden retrieval arguments or results cannot satisfy verification in either stage. Hidden
arguments leave the target unknown; hidden results for a known target only leave that target
and the existing same-host case uncertain. Hidden evidence cannot erase a provable missing
citation or falsely pass a revision that reuses prior sources.

The implementation passed the checked runner's lint and full offline suite: 1051 tests and
923 subtests on staged tree `d880715855de27e49dce29b78f101260a7dd9cd2`. Independent review
also ran 15 organization/redaction regression tests and 13 subtests. All inputs were synthetic.
This release documentation update changes no executable code; it is subject to the same
required checked-commit validation. Private logs and runtime evidence are not shipped.

## Bounded live retry — 2026-10-25

Using the existing CLI 0.10.0 route, readiness passed and one research-brief assignment ran
to completion. The earlier issue #244 dispatch error did not recur in this attempt; this
observation does not establish platform-wide rollout or compatibility. The employee returned
a structured brief, but the returned trace withheld retrieval arguments and results.

The run exposed organization-binding and redaction-classification errors in the harness,
corrected offline in the fixes above. The original verification therefore did not establish
research quality. An offline replay of the saved brief with the corrected verifier and no
source rereads leaves retrieval uncertain; it is not a newly verified live result.

The conditional revision was not sent. Temporary permissions were disabled afterward, and
no harness Discord or GitHub posting call occurred during the attempt. This demonstrates
live assignment and brief execution, not a verified brief-and-revision journey. No further
employee assignment was made during the subsequent evidence-access investigation. Private
configuration, employee/run identifiers, receipts, trace contents and logs remain excluded.

## Not validated

- A successful complete live journey, including a verified brief and revision.
- Live official-source retrieval and claim verification for this research journey.
- The release's other live routes: `seren_db_query`, `seren psql`, onboarding, the Claude CLI
  planner and GitHub submission through the harness.
- General live compatibility beyond this bounded CLI 0.10.0 observation. Other versions,
  deployments or later service changes may differ.
- Context and memory isolation between runs of the same employee, and whether an employee's
  output is forwarded by deployment-side integrations, are not established by the harness.
  The operator records evidence for routing; the harness does not enforce it.
- Request and time limits are not a cost cap. Ending observation does not cancel a remote run.
- Platforms other than macOS: Linux and WSL are untested; native Windows is not supported.
