# Seren Harness — operator guide

This guide covers three things, in order:
1. the offline example;
2. the recommended first live route: a research brief and its revision on an employee you own;
3. the advanced dedicated-employee routes.

Nothing in this repository authorizes a live request. You grant permissions yourself, in one
private configuration file, for a limited time.

## How a journey works

```
assignment -> employee works (run trace) -> harness rereads sources -> verdict
           -> revision request quoting the actual first result -> employee revises
           -> harness verifies the revision and what changed -> local report and drafts
```

The harness never does the employee's work for it, and it never relaxes the frozen criteria
to get a pass. A revision is sent only if the first deliverable met its criteria.

## 1. Offline example (no account)

```sh
python3 examples/research-journey/run_offline.py
```

This needs only Python 3.11+. It drives the real `functional run` entry point with:
- a stand-in Seren CLI that plays the employee;
- an in-process fake web that serves two synthetic pages.

It prints:
- each stage's verdict;
- the claims that were checked and those not verified;
- whether the revision request quoted the recorded brief and its SHA-256;
- the path of the local `report.md`.

It contacts nothing, and all of its content is synthetic. It shows how the harness works,
not how the real service behaves.

## 2. Live research route on your own employee (recommended first route)

**What it tests.** The journey is fixed:
1. A public-source brief compares the Ethereum Foundation Ecosystem Support Program with the
   Web3 Foundation Grants Program for a small open-source tooling team.
2. If, and only if, that brief meets its criteria, one revision follows for a two-person
   team without company, university or investor backing.

That is at most two assignments, no retries or correction loops, and no model calls by the
harness.

**What the harness may do.** It uses the no-lifecycle mode (`campaign.employee.mode:
"shared_running"`), so it only:
- reads the employee's identity, instruction document, status and tool catalog;
- sends at most two journaled `agent cloud run start --async` assignments;
- reads those runs.

**What it never does.** It never creates, starts, stops, reconfigures or deletes the employee,
never touches other runs, never writes a database and never posts messages.

### Prerequisites

- Python 3.11+ on macOS, where this release is developed and tested. It uses Unix-specific
  facilities such as `fcntl`; native Windows is not supported, Linux is untested and WSL
  compatibility is not guaranteed.
- Seren CLI 0.10.0, logged in with your own account. Other versions need a compatibility
  review; the harness checks the version and a SHA-256 pin of the executable.
- A **running** employee you own or are authorized to test, with a read-only public research
  tool such as `seren_publisher_request` in its catalog.
- No database, no Claude CLI and no Python packages.

### Configure (once)

```sh
mkdir -p "$HOME/seren-harness-live" && chmod 700 "$HOME/seren-harness-live"
cp docs/live-validation/Seren-Research-Journey-Live-Test-DISABLED.example.json "$HOME/seren-harness-live/research.json"
chmod 600 "$HOME/seren-harness-live/research.json"
/absolute/path/to/seren --version && shasum -a 256 /absolute/path/to/seren
```

Edit only that private copy. Do not put tokens or keys in it; the CLI uses its own login.

| Field | Value |
|---|---|
| `seren.executable`, `executable_sha256`, `config_home`, `profile` | Absolute CLI path, its SHA-256, the `XDG_CONFIG_HOME` root and the profile of your login |
| `deployment_id`, `output_dir` | The employee's deployment UUID; a private directory for journal and reports |
| `campaign.employee.expected` | Its `name` and `active_revision_id`, and the `instructions_path` and `instructions_sha256` (server hash) of its single instruction document. Copy these from your own read of `seren agent managed-get <deployment-id>`. Any later change blocks the run |
| `campaign.employee.use` | `"owner_designated_test"` for an employee its owner declares unused, with `designation`: `declared_by`, `declared_at` (YYYY-MM-DD), `employee_unused: true` and your `statement`. This records scope only. It does **not** claim fresh context, separate memory or non-interference; history from earlier runs may carry over. For an employee that does other work, use `"shared"` and fill `campaign.employee.isolation` with real evidence instead |
| `campaign.employee.routing.no_outbound_messaging` | A reference to your evidence that the employee's run output is not posted, mirrored or forwarded anywhere (for example a channel binding). While it is null, nothing is assigned. It is your operator evidence, not something the harness enforces |
| `campaign.research.retrieval_tools` | The employee's read-only research tool names, as its catalog lists them |
| `campaign.research.allowed_origins` | May only narrow the four official foundation origins |

Keep these fixed: text task families empty, no database, the `heuristic` planner, no model
calls, no reproductions. The configuration loader rejects anything looser before any request:
- at most 2 assignments;
- at most 6 harness source requests (redirects included). The example sets 300,000 bytes and
  10 s per source; the loader allows at most 1,000,000 bytes and 20 s;
- 900 s of wall time;
- at most 96 Seren CLI calls, plus readiness's 3.

Some managed employee responses omit `agent_identity_id`. In `shared_running` mode, the
harness can bind this owner view to the configured deployment and inspected name/revision,
while separately verifying the instruction content and hash. Missing fields are recorded as
unavailable, not invented. Missing required evidence or changed identity blocks assignment.

### Three separate steps

```sh
python3 -m seren_harness functional check     --config "$HOME/seren-harness-live/research.json"
python3 -m seren_harness functional readiness --config "$HOME/seren-harness-live/research.json"
python3 -m seren_harness functional run       --config "$HOME/seren-harness-live/research.json"
```

- **`check`** is local only. Until every placeholder is filled, it names the first setup
  problem and exits 5. Once the file is filled in, it lists each prerequisite as `ok` or
  `MISSING`. With the shipped permissions off, the authorizations show as `MISSING` (exit 2).
- **`readiness`** needs `campaign.authorization.live_seren: true`, a named `approved_by` and a
  near `expires_at`. It makes at most 3 read requests:
  - identity and instruction document;
  - status (it must be running);
  - tool catalog.

  It changes nothing, assigns nothing and fetches no source. A ready verdict grants no
  permission to run. The report goes to `<output_dir>/functional/<campaign.id>/readiness/`.
- **`run`** assigns real work, and Seren may bill for the employee's inference and tools.
  Besides `live_seren`, it needs `shared_assignments: true` (the two assignments) and
  `live_sources: true` (the harness's own bounded reread of the cited official pages).
  Turn the permissions off again afterwards.

### Reading the result

```sh
python3 -m seren_harness functional report --config "$HOME/seren-harness-live/research.json"
```

The report is `<output_dir>/functional/<campaign.id>/report.md`. For each stage it lists:
- the employee's own completed retrievals;
- the harness's reread of each source (time, SHA-256, size);
- the checked claims and everything not verified;
- for the revision, which claims changed against the actual first brief.

What the verdicts mean:
- **Checked** means a claim quotes the full source clause around its excerpt word for word,
  with only formatting ignored: case of ordinary words, spacing, punctuation, quote and dash
  style, digit grouping and contractions. Signs, units, URL paths, queries and fragments must
  match exactly. That shows faithful quotation, not that the page is true, current or enough.
- A claim that negates, reverses or changes the figures of its source **fails**. Paraphrases,
  inferences, rankings, next steps and reasons are listed as **not verified**.
- A page that changed since the employee read it, or could not be reread, is reported as
  undecided.
- The assignment asks the employee to label every source and claim with the organization's
  id. The exact listed name is also accepted and recorded in the stage's
  `organization_aliases`. Any other label (a different case, an abbreviation) binds to no
  organization: its sources are refused without being read and its claims fail.
- If the run trace withholds a retrieval call's arguments or contents (for privacy), which
  pages the employee read cannot be shown. Retrieval is then reported as unverifiable, with
  the number of withheld calls, never as passed or failed. The stage is at best uncertain, so
  the revision is not sent.
- A fully passing journey ends `facts_checked`, never "accepted". It can also end failed,
  unverified or partially verified, and each is a valid result.
- **Timeouts.** If observation ends before a run finishes, the run is not cancelled. The report
  lists it as possibly still running and incurring cost.
- **Unknown submission outcome.** If an assignment's outcome is unknown, it is never resent.
  Check the employee's recent runs in Seren and record the outcome with `functional reconcile`.

| `run` exit | Meaning |
|---|---|
| `0` | The campaign ended (journey complete or a limit). Read the report for the journey's verdict |
| `2` | Blocked: a prerequisite, permission, access or identity problem. Nothing unsafe was retried |
| `3` | Unresolved: an outcome is unknown. Follow the printed next action |
| `4` | Paused or interrupted at a safe point. Run the same command to resume |
| `5` | Configuration or local reporting problem |

## 3. Advanced routes (extra resources and permissions)

The default `operator.example.json` describes the dedicated-employee campaign:
- **The journeys.** `data_report_revision` (a revenue report and its revision) and
  `document_brief_followup` (a costed plan revised after a new decision record).
- **The employee.** A **dedicated, stopped** employee, or one created through onboarding. The
  campaign owns its lifecycle and stops it at the end of every invocation.
- **The database.** A dedicated SerenDB database the employee reads with `seren_db_query`. The
  harness writes only its own ownership-tagged tables through a pinned `seren psql`, and drops
  them at the end.
- **The planner.** A deterministic `heuristic` planner, or a Claude CLI planner/judge, which
  needs its own pinned executable, login and `model_calls` permission.

Each live action needs its own `true` flag in `campaign.authorization`, plus a named approver
and an unexpired time:
- `live_seren`;
- `create_employee`;
- `resource_writes` for the journey tables;
- `model_calls`.

Readiness for this route makes at most four reads. These include one read-only `psql` probe.

Findings:
- A failure must be reproduced on an identical task before it becomes a local draft.
- Submitting a draft to GitHub is a separate command that needs a per-finding approval file,
  bound to the exact draft text:

  ```sh
  python3 -m seren_harness functional approval-template --config <config> --finding F-xxxxxxxxxxxx
  python3 -m seren_harness functional submit-issue --config <config> --finding F-xxxxxxxxxxxx --approval <file>
  ```

- Automatic filing does not exist.

Other routes are described in [ADVANCED.md](ADVANCED.md):
- legacy catalog campaigns;
- lifecycle, preview and diagnostic packages;
- discovery and model workers.

The starter `check-setup` / `test-live` pair makes one read-only catalog request.

## Permission boundaries

- **Off by default.** Live requests, assignments, source fetches, employee creation, table
  writes and model calls each need their own flag, approver and expiry. There is no scheduler
  and nothing runs in the background.
- **Untrusted content.** Employee replies, source pages and fixture text are treated as data.
  They cannot change the criteria, which are frozen by SHA-256. They cannot add URLs or move
  the reader off its allowlist.
- **Source reads.** The source reader:
  - uses only `https`;
  - resolves to public addresses;
  - sends no credentials, cookies or proxy settings;
  - follows at most two redirects, each counted.
- **Prompts are not access control.** Asking an employee not to message anyone is not
  enforcement. The routing field records your evidence; the harness itself posts nothing.

## Not verified in this release

None of the following has been exercised, so treat each as a live-only unknown:
- a real account, employee or run trace;
- the real official pages;
- `seren_db_query`, `seren psql` or onboarding;
- the Claude CLI planner;
- real GitHub.

Context and memory isolation between runs of the same employee is not established by Seren's
published documentation. Command shapes follow the CLI 0.10.0 contracts; the current service
may differ. See [VALIDATION.md](../VALIDATION.md).
