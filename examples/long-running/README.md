# Work across multiple sessions

Use this optional recipe when a project has several features or will outlast one
agent session. Small fixes can keep using the ordinary task checkpoint. This is
instructional guidance, not an automatic agent loop or a new validation gate.

## Set up once

Reuse the project's existing backlog and setup instructions. If there is no
persistent backlog, adapt [FEATURES.md](FEATURES.md) into a tracked file such as
`docs/FEATURES.md`; merge with existing files rather than overwriting them.
Link that file or issue tracker from `.tiny-harness/TASK.md`. The installer does
not add this example to adopted repositories.

Capture only the authorized requirements. Give each feature a stable identifier,
a status, concrete acceptance steps, and space for verification evidence. Start
untested work as not started or unverified, not as passed. Do not invent extra
features to fill the list. Keep the backlog when the current task changes.

Record the actual setup/start command and a cheap smoke check in the project's
existing documentation. Use an existing script if available; create one only
when setup needs it. A browser tool or separate initializer agent is not required.

## Resume

1. Read the contract, current task, backlog, Git status/diffs, and recent commits.
2. Resolve stale notes against the files and referenced evidence. If a checkpoint
   says publication is pending, inspect the relevant remote commit, CI run, or
   deployment result before doing it again. Record a missing result as unknown.
3. When resuming implementation with uncertain behavior or stale evidence, run a
   small relevant smoke check. Skip unnecessary startup/testing for documentation
   work or unchanged, already verified behavior. Diagnose relevant baseline failures
   before building on them; record unrelated failures without erasing them.
4. Choose a bounded unfinished item and update the current task. Continue within
   existing authorization; a new session is not a reason to request approval again.

## Verify and hand off

Test the observable acceptance steps, not just the existence of code. For example,
for a note-saving feature: create a uniquely named note through the UI, reload,
and confirm its title and body persist. Record the observed result, environment,
source revision, and test command or evidence link. If browser access is missing,
record that gap; passing unit tests alone does not verify the UI workflow.

For a CLI, exercise its documented command and output; for a library, use a small
consumer test. Use disposable data and appropriate permissions. Existing test
commands can be configured in `checks.json` when they run reproducibly against
the staged snapshot. A check needing a server should start and stop it within the
same check. A manual working-tree smoke test does not replace staged validation.

Mark a feature verified only when its acceptance steps have evidence. If later
changes invalidate that evidence, mark the affected feature unverified again.
Keep blocked requirements visible. Move work out of scope only with the user's
authorization and record the reason; do not delete criteria to claim completion.

Before stopping, leave a compact checkpoint: work completed, unfinished changes,
verification evidence, failures, and the next action. Commit coherent tested work
through the harness. If interrupted mid-change, document it accurately and preserve
it rather than making a misleading completion commit.

A checkpoint written before commit/CI/publication can explicitly say those steps
are pending and name the source of their eventual result. Report the resulting
commit and CI link in the handoff; the next session reconciles them without an
extra bookkeeping commit. Recheck the overall backlog before declaring the whole
project complete. A green commit is only evidence for the checks it actually ran.

## Boundary and background

Tiny Harness does not parse this checklist or enforce feature completion. The
agent follows these instructions, while the runner enforces configured check
results when invoked. No new files, dependencies, approval steps, or recurring
processes are installed by this recipe.

Inspired by Anthropic's [long-running agent workflow](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents),
adapted to Tiny Harness's existing checkpoint and commit workflow.
