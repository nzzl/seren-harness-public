# Agent operating contract

## Scope

The requirements below govern whichever agent or person is changing this repository.
Use one writer per working tree at a time.

This contract governs development and checked commits. It does not require operators
to install development tools, initialize Git or run regression tests to use `functional`,
`check-setup` or `test-live`. Follow docs/OPERATOR.md for those commands. Their live safety checks,
authorization flags, limits and approval-bound submission remain mandatory. The advanced
live supervisor retains its full-validation gate.

## Mandatory constraints

- Read applicable repository instructions, this contract, and TASK.md before work.
  Inspect actual code and Git status/diffs before choosing a solution. Follow the
  user's authorized scope; distinguish existing work from your changes.
- Record the outcome, boundaries, observable acceptance criteria, and existing
  changes in TASK.md. Keep one compact checkpoint: decisions, evidence, unresolved
  failures, and next action. Never mark an incomplete task or check as passed.
  For work spanning multiple tasks or sessions, reference an existing project
  backlog or keep a small persistent feature checklist. Preserve unfinished
  requirements when replacing the current task; record authorized scope changes.
- Implement, test, diagnose, repair, and make local commits autonomously within
  the authorized task. Ask only for missing information that blocks progress or
  consequential actions outside existing authorization (for example publication,
  destructive data changes, or new external commitments). Routine failures are
  problems to investigate, not approval gates.
- Preserve unrelated edits, existing instructions, hooks, and Git history. Stage
  named files or selected hunks; inspect the staged diff. Include only work you
  own or are explicitly authorized to include. Never reset, clean, force-push,
  amend, or otherwise discard/rewrite work to get unstuck.
- For Python work, reuse the project's existing isolated environment and tooling
  (such as uv, Poetry, Conda, or a development container). If Python dependencies
  are needed and no isolated environment exists, create a local `.venv` and keep
  it out of Git. Do not install project dependencies into the host's system Python.
  Put the environment's tools on PATH in the same command that runs the harness
  (activate and run together, or use the tool's runner such as `uv run`); an
  activation in an earlier shell may not carry over. An ignored working-tree
  `.venv` is absent from the staged snapshot. A project using only the harness's
  standard-library runtime does not need an environment just for the harness.
- Configure real, relevant checks in checks.json. Use behavior-focused tests
  proportional to risk; do not add tests that merely restate implementation.
  Never weaken or remove a required check to make a failure disappear. A missing
  prerequisite is a failure to resolve or report, never a silent skip.
  Verify changed user-visible behavior through the relevant interface when
  appropriate; unit checks alone do not establish end-to-end completion. Record
  the result and tested revision/environment, or the remaining verification gap.
  Project checks must read snapshot source; inherited environments and editable
  installs can otherwise redirect imports to code outside the staged tree.
- Commit with `python3 .tiny-harness/run.py commit -m "Reason for this change"`.
  This validates the staged tree afresh, stopping at the first failed check;
  an empty commit exits before checks. For all ordinary check results, use
  `python3 .tiny-harness/run.py validate`. Do not bypass failed
  checks or existing hooks. Keep commits small, coherent, and meaningful.
- On failure, inspect the error and relevant state, identify a cause, make a
  targeted repair, and rerun. Change approach when evidence contradicts it. If
  blocked by unavailable access, a prerequisite, or an unresolved decision,
  record exact evidence and the smallest needed intervention; do independent work.
- On resumption, read TASK.md, Git status, staged/unstaged diffs, and recent log.
  Read the referenced backlog if present. Reconcile notes with actual files and
  authoritative external results before proceeding; inspect recorded CI runs or
  published refs before repeating a push, deployment, or release. For resumed
  implementation, use the smallest relevant smoke check when behavior or evidence
  is uncertain, then choose the next bounded piece of unfinished work. Avoid
  repeating completed checks solely to regain context; required commit validation
  still runs.
  Do not repeat external actions or create duplicate commits just to refresh notes.
- Before handoff, remove temporary files and stop background processes created
  for the task when they are no longer needed. Limit cleanup to resources you can
  identify as your own; preserve user files, unrelated work, requested deliverables,
  and evidence needed to reproduce unresolved failures. Report anything deliberately
  retained or left running, with its location or process identifier and the reason.
- Before handoff, update the checkpoint and report completed work, check results,
  commit identifiers, remaining failures/limitations, and next action if any.
  Include pending external steps and how the next session can verify their result.
  A finished task does not complete the project while required features or
  acceptance steps remain unverified.

## Preferences (adapt with a reason)

Prefer the smallest direct solution, few dependencies, concise documentation, and
existing project conventions. Record meaningful assumptions and tradeoffs in
TASK.md; a separate design document is rarely needed. Stop when acceptance
criteria and relevant checks pass. Extra polish is a new scope decision.

## Enforcement boundary

These instructions depend on the agent reading and following them. The runner
rejects missing/malformed/empty check configuration and unsuccessful checks;
its commit command validates first. It does not judge test quality, task truth,
authorization, or deliberate staging. Plain Git bypasses it. Existing hooks run
normally and may reject a commit. Checks see the staged snapshot plus inherited
environment and tools, with empty stdin; use one writer per repository while
validating or committing. See the checked runner and VALIDATION.md for the validation boundary.
