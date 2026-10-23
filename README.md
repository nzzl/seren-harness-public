# Seren Harness

[![License: MIT](docs/assets/badges/license.svg)](LICENSE)
[![Python: 3.11+](docs/assets/badges/python.svg)](docs/OPERATOR.md)
[![Validation: offline](docs/assets/badges/validation.svg)](VALIDATION.md)
[![Status: early release](docs/assets/badges/status.svg)](#what-it-does-and-does-not-establish)

**Seren Harness is designed to let an automated agent find and report user-facing issues
in the [Seren](https://docs.serendb.com) AI-employee platform by running functional user
journeys.** Within the scope, permissions and limits you configure, the testing agent assigns
work to AI employees, follows up on their actual results, checks the evidence independently
and reports problems locally. You do not need to direct every stage by hand.

The goal is useful, reproducible findings and verified successful work—not just a bug count.
Findings can include defects, broken workflows, missing capabilities, usability problems and
evidence gaps. A harness or access failure is not automatically a Seren defect. Where the
workflow supports issue drafting, reports can become GitHub-ready local drafts; nothing is
submitted automatically.

**Early release:** the automated workflow has been tested offline with synthetic stand-ins.
A bounded live attempt passed readiness and submitted one assignment, but Seren returned a
dispatch error before employee execution ([upstream issue #244](https://github.com/serenorg/seren/issues/244)).
A complete live journey against the real Seren service has not yet been validated.

Seren Harness is an independent project; it is not made, reviewed or endorsed by Seren.
An individual API call that succeeds, or a fluent answer, does not show that a user got real
work done. The testing agent acts like a customer:

1. **Assign** an AI employee a real piece of work through the Seren CLI/API.
2. **Collect evidence** of how it did the work (its run trace and tool calls).
3. **Follow up** with a revision request that quotes the employee's actual first result.
4. **Verify independently**: the harness rereads the sources itself and checks the
   employee's claims against them, instead of trusting the employee's account.
5. **Report locally**: verdicts, what was and was not verified, problems, useful
   observations, and GitHub issue *drafts* that are never submitted automatically.

It tests employee workflows through the CLI/API. It does not test Seren's website screens.

## Platform support

This release is developed and tested on macOS. It uses Unix-specific facilities such as
`fcntl` and does not support native Windows. Linux has not been tested, and WSL compatibility
is not guaranteed. Python 3.11 is the minimum supported version; the full test suite was run
on Python 3.14.7 only.

## Try it offline (no account needed)

Requires Python 3.11+ and nothing else: no packages, account, network or credentials.

```sh
python3 examples/research-journey/run_offline.py
```

A stand-in Seren CLI plays a research employee, and two synthetic web pages play the
official sources. The harness assigns a grant-programme research brief, rereads the
"sources", checks the quoted claims, sends a revision request that quotes the brief, checks
the revision and writes a local report. All inputs and results are synthetic.

## Run it against your own employee

The recommended first live route is the research brief-and-revision journey on an employee
you own. It needs the Seren CLI **0.10.0**, logged in with your own account, and one private
configuration file. It does not need a database or a separate planner model, and it never
starts, stops or reconfigures the employee. See the **[operator guide](docs/OPERATOR.md)**.

Each live stage is separate and opt-in:

| Command | What it does |
|---|---|
| `functional check` | Local only: configuration, pins and permissions. No service request |
| `functional readiness` | A few read-only service reads; changes nothing and assigns nothing |
| `functional run` | Assigns real, possibly billed work to the employee (at most two assignments on this route) |

Every shipped configuration has all permissions off, approver and expiry unset, reports in a
private directory and GitHub in draft-only mode.

## What it does and does not establish

- A *checked* research claim quotes its source clause word for word in the harness's own
  reread of the page. That shows faithful quotation, not that the source is true, current or
  sufficient. Paraphrases, inferences and rankings are reported as **not verified**.
- Results can legitimately end *unverified*. That is not a failure of the harness.
- Request and time limits bound what the harness sends. They are not a cost cap, and ending
  observation does not cancel a run already started on Seren.
- No completed live journey against the real service is included in this release. The
  offline tests drive the real entry point against stand-in boundaries; see
  [VALIDATION.md](VALIDATION.md).

Other workflows (dedicated-employee database journeys, a Claude-driven planner, lifecycle and
diagnostic routes) are in the [advanced guide](docs/ADVANCED.md). Contributors and
maintainers: [DEVELOPMENT.md](DEVELOPMENT.md). Packaging and privacy:
[SANITIZATION.md](SANITIZATION.md).

## License

Seren Harness is licensed under the [MIT License](LICENSE). Original Seren Harness code is
copyright © 2026 Greg Wolfson. The inherited Tiny Harness copyright notice is retained in
[LICENSE](LICENSE); its development runner also keeps the original MIT notice in
[.tiny-harness/LICENSE](.tiny-harness/LICENSE).
