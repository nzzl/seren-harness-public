# Seren Harness — advanced workflows and engineering setup

## Status and scope

The main workflow is the functional campaign (`functional run`) in the
[operator guide](OPERATOR.md), which also covers the read-only starter check. This document
covers legacy and advanced single-purpose workflows only. The campaign reuses their CLI
command shapes, action journal, cleanup obligations, finding store and sanitizer. It does not
use or loosen their hash-bound packages.

Live functionality is retained. The repository contains no personal account, runtime
database, credentials, active scheduler or standing permission. These routes have not been
exercised against a real deployment in this release.

The implementation supports Seren CLI 0.10.0. Other versions require compatibility review,
not simply changing the version check. Treat configuration examples and proposed packages
as inactive templates. UUIDs beginning `7a00` are synthetic replacements, not usable targets.
Other test sentinel IDs are also fictional. Never guess which production resource to use.

## Optional advanced development setup

Use Python 3.11+ and Git on macOS ([platform support](../README.md#platform-support): native
Windows is not supported and Linux is untested). From this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple --require-hashes --no-deps -r requirements-dev.txt
.venv/bin/python -m pip check
.venv/bin/python simulation/demo.py
.venv/bin/python tests/run.py
.venv/bin/python -m seren_harness --help
```

Dependency installation uses the network. `simulation/demo.py` is an older lifecycle and
channel simulation with a fake CLI and temporary state; it does not contact Seren and is not
evidence of the research-and-revision journey (see `examples/research-journey/` for that). The suite uses disposable fixtures, subprocesses and loopback
servers. Run it in an isolated development environment. The checked commit runner, Git
snapshots and discovery workers need a Git repository; no remote is needed for the demo or
tests.

## Retained capabilities

| Route | Purpose | Setup boundary |
|---|---|---|
| `collect` | Read catalog/status/run records or one fixed read-only SQL query | Recipient target file, approved read scope, CLI session; SQL also needs pinned psql |
| `preflight`, `provision`, `control`, `run`, `resume` | Dedicated-employee lifecycle, readiness, reply checks, interruption recovery and shutdown | Recipient live configuration and dedicated fixture; channel route additionally requires its own reader and human probe |
| `employee-time`, `employee-lookup`, `employee-data` | Narrow employee-mediated clock, publisher and branch checks | Fresh reviewed session, matching instruction, baseline, grants and caps |
| `stream-replay` | Compare a stored run's final answer with CLI stream output | Recipient-owned record, rebound fixture identity and fresh package/authorization |
| `review-workflow`, `review-response`, `report`, `draft-issue` | Evaluate supplied evidence, classify findings and generate local reports | Explicit evidence provenance and expected behavior; drafts are not submissions |
| `dbread`, invoice and journey routes | Controlled synthetic-data and record-retrieval workflows | Dedicated resources, fresh package hashes, restoration plan and authorization |
| `discovery`, `worker` | Real CLI against controlled fake API responses; optional supervised model work | Optional advanced setup; no scheduler or paid/model permission included |

Use `python -m seren_harness <command> --help` for exact arguments. Help is informational;
commands that collect data or start employees are not authorized by this guide.

## Configure an advanced live workflow (not required for the starter test)

1. Select one narrow investigation and an isolated test employee. Agree the expected
   result and the exact reads or mutations allowed. Start with read-only collection.
2. Install the supported Seren CLI and record its absolute path, SHA-256 and version.
   Log in using your own OAuth profile. Keep session files outside this copy.
   Do not use anyone else's credentials or account.
3. Copy `targets/seren.json` outside the repository and replace its workspace root,
   CLI and optional psql paths/hashes, profile, config home, deployment/run/database IDs.
   Adjust the corresponding case expectations to the same resources you own.
   The shipped paths deliberately do not locate any real installation.
4. For lifecycle work, start from `docs/lifecycle-setup/live-config.template.json`.
   Supply fresh identity-bound baselines and approvals. Configure the channel only when
   testing that channel: human sender, dedicated read-only observer and expected response.
   Direct catalog, SQL, employee-tool and stream routes do not require that channel.
5. Specialized diagnostics retain exact-target safeguards. Their former target constants
   are now synthetic: see `seren_harness/stream_replay.py` (`CLOCK_TEST`), the employee
   baseline checks, the case/catalog bindings and frozen package inputs. Rebind these
   deliberately to the same isolated test fixture and regenerate all dependent
   hashes. Do not remove identity checks to make a run proceed. This is engineering
   configuration work, not a one-click import.
6. Supply new package-specific approval, time/request/cost caps and a cleanup plan.
   Nothing under `docs/` is a current authorization. Review preview/read-back differences
   and restoration behavior before permitting configuration-changing runs.
7. For advanced supervisor or configuration-changing workflows, validate the exact configured revision before live use. With a fresh local Git
   repository and the intended tree staged, activate `.venv` and run
   `python .tiny-harness/run.py validate`. Resolve failures instead of bypassing gates.
8. Execute only the approved route. Inspect the report and confirm cleanup. Missing
   evidence remains unverified; a zero CLI exit or accepted start is not a working employee.

## Limits that still matter

- The functional campaign implements autonomous employee QA end to end, including two
  multi-stage user journeys on a dedicated database. It has been verified only offline
  against fake boundaries. A repeatable stream of valuable live findings is not established.
  Employee-side writes (selective tool approval) and file, storage or knowledge journeys
  remain unestablished routes. Regression tests are not evidence of current service quality.
- Employee-mediated database retrieval and full lifecycle reply success require fresh
  validation. Access requirements may depend on the current service and tool grants.
- Configuration comparison and restoration around derived secret-requirement fields
  remain open engineering concerns. This release does not claim they are resolved.
- Some safety checks intentionally bind to one employee and one package; adaptation is
  required for a new operator. Not all supported individual commands are integrated into
  the discovery supervisor's shared live ledger.
- The offline discovery lab is pinned to a particular CLI binary and uses macOS
  `sandbox-exec`; scheduling uses launchd. Linux operation is not validated.
- Optional model workers need your own subscriptions, budget settings and explicit
  authority. They are unnecessary for manual collection, reporting or the offline demo.
- You must define valuable assignments and expected results. The tool does
  not decide what constitutes useful customer value or certify every feature.

## State and disclosure

Keep live configuration, credentials, databases, logs and reports outside Git in a private
directory. Use environment-variable references for secrets. Report redaction is helpful
but is not a guarantee; inspect any proposed disclosure separately.

Private-issue publishing is disabled by default and its repository binding is a placeholder.
Notification/reporting code is included for review, not permission to send. No GitHub remote,
CI workflow or background worker is configured. Do not publish findings, upload code or
contact anyone without separate explicit authorization.

## Examples and provenance

Research scenarios are fictional replacements. Finance examples are synthetic tasks, not
commercial records. Service-response fixtures have synthetic identities and generalized
shapes; they exercise regression handling, not claims about real customer executions.
Legacy case filenames (for example the `scott.*` research cases) are stable internal
identifiers, not provenance claims. Private reports and investigations are not included. See
[SANITIZATION.md](../SANITIZATION.md).
