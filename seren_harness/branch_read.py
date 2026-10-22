"""One existing branch-metadata read through a publisher; no service I/O."""

from seren_harness import publisher_lookup

SCOPE = "one-call-branch-read-v1"
TOOL = "seren_publisher_request"
OUTPUT_LIMIT = publisher_lookup.OUTPUT_LIMIT
SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "body": {
            "description": "JSON request body. Mutually exclusive with body_base64.",
            "type": "object",
        },
        "body_base64": {
            "description": "Raw request body, base64-encoded. Mutually "
            "exclusive with body. When set, "
            "headers.Content-Type controls upstream encoding "
            "and defaults to application/octet-stream when "
            "omitted.",
            "type": "string",
        },
        "connection_id": {
            "description": "Optional OAuth connection UUID for BYOC publishers.",
            "format": "uuid",
            "type": "string",
        },
        "headers": {"additionalProperties": {"type": "string"}, "type": "object"},
        "idempotency_key": {
            "description": "Required for mutating operations. Use a stable "
            "UUID or other unique key for the real-world "
            "action; repeated use in one run is blocked "
            "before a second external call.",
            "type": "string",
        },
        "operation": {
            "enum": [
                "get",
                "post",
                "estimate",
                "mcp_list_tools",
                "mcp_call_tool",
                "mcp_read_resource",
            ],
            "type": "string",
        },
        "operation_id": {
            "description": "Optional publisher operation identifier used to "
            "match typed publisher grants.",
            "type": "string",
        },
        "path": {"type": "string"},
        "publisher": {"type": "string"},
        "query": {"type": "object"},
        "resource_uri": {"type": "string"},
        "tool_name": {"type": "string"},
    },
    "required": ["publisher", "operation"],
    "type": "object",
}


def message(spec):
    import json

    return (
        "Call seren_publisher_request exactly once with "
        + json.dumps(spec["arguments"], separators=(",", ":"), sort_keys=True)
        + ". Read the returned branch record and reply with exactly its id. "
        "Do not guess. Use no other tool, query or operation."
    )


def prompt(spec):
    return (
        "---\nname: seren-harness-branch-read\n"
        "description: One explicitly invoked read-only branch metadata request.\n---\n\n"
        "# Seren Harness Branch Read\n\n"
        "You are an isolated non-production employee. Remain idle until explicitly invoked.\n"
        + message(spec)
        + "\n"
        "Never obtain credentials or connection strings, read application tables, run SQL, "
        "create or change resources, schedule work, browse, execute code, or connect to messaging."
    )


def patch(spec):
    return {"workload_patch": {"prompt": prompt(spec)}}


def verify_detail(detail, spec):
    bundle = detail.get("bundle")
    instructions = bundle.get("instructions") if isinstance(bundle, dict) else None
    if (
        detail.get("max_tool_output_chars") != OUTPUT_LIMIT
        or not isinstance(instructions, list)
        or not any(
            isinstance(i, dict)
            and i.get("kind") == "skill"
            and i.get("content") == prompt(spec)
            for i in instructions
        )
    ):
        raise ValueError("branch read instruction/output limit not established")


def verify_update(basis, baseline, fixture_baseline, spec):
    previous = basis["lookup_baseline"]
    publisher_lookup.verify_update(basis, previous, fixture_baseline)
    if any(previous[k] != baseline[k] for k in baseline if k != "active_revision_id"):
        raise ValueError("branch update changed existing fixture controls")
    update = basis["branch_read_update"]
    if (
        update.get("request") != patch(spec)
        or update.get("prior_revision") != previous["active_revision_id"]
    ):
        raise ValueError("branch update differs from the reviewed prompt-only request")
    detail = publisher_lookup._receipt_detail(update["post_update_detail"])
    if fixture_baseline(detail) != baseline:
        raise ValueError("branch post-update baseline differs")
    verify_detail(detail, spec)


def detail_projection(detail, baseline, spec):
    verify_detail(detail, spec)
    return dict(
        baseline,
        status=detail["status"],
        max_tool_output_chars=OUTPUT_LIMIT,
        bundle={"instructions": [{"kind": "skill", "content": prompt(spec)}]},
    )
