# Synthetic sanitizer fixture — 2026-10-22

Replace a credential-shaped fixture with a visibly invalid synthetic account/signature.
Retain token-pattern and database-redaction coverage, including a direct assertion without a
credential prefix or known-secret registration. Runtime sanitizer code is unchanged.
Verification: focused sanitizer tests and the required checked initial commit.
Release files remain disabled by default; no live employee work is part of this change.
