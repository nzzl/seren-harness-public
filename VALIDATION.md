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
