"""Result vocabulary shared by journeys, state and reports."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"
NOT_RUN = "not_run"
AWAITING_HUMAN = "awaiting_human"
OUTCOMES = (PASSED, FAILED, BLOCKED, NOT_RUN, AWAITING_HUMAN)

# Finding classes are provisional until triage agrees expected behavior.
DEFECT = "defect"
FEATURE_GAP = "feature_gap"
DOCUMENTATION_GAP = "documentation_gap"
ENVIRONMENT = "environment_access"
HARNESS_DEFECT = "harness_defect"
UNCERTAIN = "uncertain"
FINDING_CLASSES = (DEFECT, FEATURE_GAP, DOCUMENTATION_GAP, ENVIRONMENT, HARNESS_DEFECT,
                   UNCERTAIN)

LIVE = "live"
SIMULATION = "simulation"

# Run lifecycle states.
RUN_ACTIVE = "active"
RUN_AWAITING = "awaiting_human"
RUN_COMPLETE = "complete"
RUN_INTERRUPTED = "interrupted"
# Finished, but a mutation's outcome is still unknown: resumable, and other runs for the
# employee are refused until it is reconciled.
RUN_UNRESOLVED = "unresolved"

UNKNOWN_SERVER_BUILD = "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Finding:
    """An assertion failure or blocked prerequisite worth triage."""

    assertion: str
    classification: str
    title: str
    expected: str
    observed: str
    # Stable context distinguishes findings that share generic error text.
    context: dict = field(default_factory=dict)
    impact: str = ""
    unknowns: list = field(default_factory=list)

    def __post_init__(self):
        if self.classification not in FINDING_CLASSES:
            raise ValueError("unknown finding classification: " + self.classification)


@dataclass
class StepResult:
    outcome: str
    summary: str
    evidence: dict = field(default_factory=dict)
    finding: Optional[Finding] = None

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError("unknown outcome: " + self.outcome)


def passed(summary, **evidence):
    return StepResult(PASSED, summary, evidence)


def failed(summary, finding, **evidence):
    return StepResult(FAILED, summary, evidence, finding)


def blocked(summary, finding=None, **evidence):
    return StepResult(BLOCKED, summary, evidence, finding)
