"""Fixed publisher identity lookup and receipt checks; no I/O to Seren."""
import json
from pathlib import Path
from seren_harness import contract
from seren_harness.collect import _sha256

SCOPE = "one-call-publisher-lookup-v2"
INITIAL_OUTPUT_LIMIT = 16000
OUTPUT_LIMIT = 32768
PROMPT = """---
name: seren-harness-publisher-lookup
description: One explicitly invoked read-only publisher metadata lookup.
---

# Seren Harness Publisher Lookup

You are an isolated non-production employee. Remain idle until explicitly invoked.
For the fixed harness request only, call seren_publishers_get exactly once with
{\"slug\":\"seren-db\"}. Read the returned publisher metadata and reply with exactly
its slug. Do not guess or answer without the successful tool result. Use no other
tool. Do not query a database, invoke a publisher API operation, schedule work,
browse, execute code, delegate, or connect to messaging services.
""".strip()
MESSAGE = 'Call seren_publishers_get exactly once with {"slug":"seren-db"}. Read the returned publisher record and reply with exactly its slug, without commentary. Use no other tool; do not guess or answer without the tool result.'
PATCH = {"workload_patch": {"prompt": PROMPT},
         "workload_limits": {"max_tool_output_chars": INITIAL_OUTPUT_LIMIT}}
# Separate from the already applied prompt change; never resubmit that change.
OUTPUT_PATCH = {"workload_limits": {"max_tool_output_chars": OUTPUT_LIMIT}}
SCHEMA = {"type": "object", "properties": {"slug": {"type": "string"}},
          "required": ["slug"], "additionalProperties": False}


def verify_detail(detail, output_limit=OUTPUT_LIMIT):
    bundle = detail.get("bundle")
    instructions = bundle.get("instructions") if isinstance(bundle, dict) else None
    if (not isinstance(instructions, list)
            or detail.get("max_tool_output_chars") != output_limit
            or not any(isinstance(i, dict) and i.get("kind") == "skill"
                       and i.get("content") == PROMPT for i in instructions)):
        raise ValueError("fixed lookup instruction and output limit not established")


def verify_update(basis, baseline, fixture_baseline):
    previous = basis["prior_baseline"]
    if any(baseline[k] != previous[k] for k in baseline if k != "active_revision_id"):
        raise ValueError("fixture changed beyond the reviewed instruction/output patch")
    if basis.get("update_request") != PATCH:
        raise ValueError("reviewed lookup update receipt changed")
    prior_detail = _receipt_detail(basis["post_update_detail"])
    prior_lookup = fixture_baseline(prior_detail)
    verify_detail(prior_detail, INITIAL_OUTPUT_LIMIT)
    if any(prior_lookup[k] != baseline[k] for k in baseline if k != "active_revision_id"):
        raise ValueError("original lookup baseline differs")
    correction = basis["output_limit_update"]
    if (correction.get("request") != OUTPUT_PATCH
            or correction.get("prior_revision") != prior_lookup["active_revision_id"]):
        raise ValueError("output correction differs from the one-field proposal")
    detail = _receipt_detail(correction["post_update_detail"])
    if fixture_baseline(detail) != baseline:
        raise ValueError("post-correction baseline differs")
    verify_detail(detail)


def _receipt_detail(ref):
    path = Path(ref["path"])
    if _sha256(path) != ref["sha256"]:
        raise ValueError("reviewed lookup update receipt changed")
    receipt = json.loads(path.read_text())
    if receipt.get("status") != "ok" or receipt.get("exit_code") != 0:
        raise ValueError("successful post-update read-back required")
    return contract.unwrap(receipt.get("response"))


def detail_projection(detail, baseline):
    """Retain only checked public controls; never copy secrets from managed detail.

    Exact prompt comparison occurs before projection. Known authored tool names
    are public instruction text, not secret values to reconstruct after redaction.
    """
    verify_detail(detail)
    return dict(baseline, status=detail["status"], max_tool_output_chars=OUTPUT_LIMIT,
                bundle={"instructions": [{"kind": "skill", "content": PROMPT}]})
