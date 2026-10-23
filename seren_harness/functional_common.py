"""Primitives shared by the functional campaign (functional) and its disclosure boundary
(functional_issues). Kept here so functional_issues never imports the campaign module."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

# Outcome categories. Only the first three can become issue drafts, and only when reproduced.
PRODUCT_DEFECT = "product_defect"
USABILITY = "usability_problem"
MISSING_CAPABILITY = "missing_capability"
BAD_TASK_SPEC = "bad_task_specification"
HARNESS_FAILURE = "harness_failure"
ACCESS_BLOCK = "access_block"
UNCERTAIN_RESULT = "uncertain"
CATEGORIES = (PRODUCT_DEFECT, USABILITY, MISSING_CAPABILITY, BAD_TASK_SPEC, HARNESS_FAILURE,
              ACCESS_BLOCK, UNCERTAIN_RESULT)
DRAFTABLE = (PRODUCT_DEFECT, USABILITY, MISSING_CAPABILITY)


class ConfigError(ValueError):
    pass


def iso(moment=None):
    return (moment or datetime.now(timezone.utc)).isoformat()


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def campaign_dir(settings):
    return Path(settings["output_dir"]) / "functional" / settings["campaign"]["id"]
