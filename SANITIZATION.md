# Packaging and privacy

This repository was assembled from a private working copy using an explicit inclusion review.
It is not a copy of that working directory.

**Included:** the `seren_harness` package, its regression tests and synthetic stand-ins,
synthetic cases and fixtures, generic templates and proposed packages, examples, the
development runner in `.tiny-harness/` and the license notices.

**Not included:** Git history and private development records. These include agent prompts,
reviews, task checkpoints, handoffs and validation history. Also excluded:
- operator configurations, authorizations, credentials, reports and runtime state;
- real account, employee, channel and resource identifiers;
- machine-specific paths;
- an adapter for a private application.

**Test data in the repository is synthetic.** Research scenarios, companies, invoices and
service-response fixtures are fictional or generalized. They test the harness's handling,
not any real customer, record or execution.
- Identifiers such as `7a00…`, `0000…` and repeated-digit UUIDs are placeholders.
- Credential-shaped strings in tests are fake canaries.
- The pinned CLI SHA-256 identifies a public Seren CLI 0.10.0 binary.
- Legacy case and module names (for example `scott.*` or `bd_*`) are stable internal
  identifiers, not references to real people or records.

**Checks performed on the actual file inventory for this release:**
- for hidden files, symlinks, archives, databases and binaries;
- for credential and token shapes;
- for email addresses and long numeric IDs;
- for UUIDs other than placeholder patterns;
- for machine paths;
- for known private names and identifiers.

Matches were inspected; remaining fixture matches are synthetic test values. The copyright
credit to Greg Wolfson is intentionally public. These searches support
review; they cannot prove the absence of all sensitive data.

The Tiny Harness MIT notices in `LICENSE` and `.tiny-harness/LICENSE` are preserved unchanged.
Public provider URLs and hostnames are intentional.

The 2026-10-23 compatibility update includes only generalized code, synthetic tests and a
summary of a separately authorized public issue. The validation summary describes a real
failed attempt; it does not include the private configuration, raw receipts, identifiers or
logs. The public issue link is intentional. Private prompts and handover records were not copied.

Placeholder pages that only said design notes were not included were removed; references now
point to the operator or advanced guide, or say the source is not in this release. The two
hash-bound proposed packages in `docs/journeys/employee-retrieval/` keep their original text,
including a historical mention of a design note that is not shipped.
