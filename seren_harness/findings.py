"""Stable finding identity for deduplication across runs.

The key covers the evidence mode, journey, failed step and assertion plus the finding's stable
context (for example the activity kind and stop reason). Run IDs, timestamps
and request IDs never enter it. A generic error string alone is not a key:
two findings that share `channel_binding_failure` in different steps or
activity contexts remain separate.
"""
from __future__ import annotations

import hashlib
import json
import re

_VOLATILE = re.compile(r"(?i)(_id$|^id$|_ids$|_at$|_time$|^time|timestamp|request|sequence"
                       r"|attempt|^run_|_count$)")


def stable_context(context):
    """Drop volatile fields defensively even if a step included them."""
    return {key: value for key, value in sorted(context.items())
            if not _VOLATILE.search(key) and isinstance(value, (str, int, bool, type(None)))}


def finding_key(journey, step, finding, mode):
    """`mode` (live or simulation) is part of the identity: simulated occurrences must never
    count towards, or be drafted as, a live finding."""
    material = {"mode": mode, "journey": journey, "step": step,
                "assertion": finding.assertion, "context": stable_context(finding.context)}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return "F-" + digest[:12]
