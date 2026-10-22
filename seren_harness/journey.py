"""Execute an ordered journey with dependency gating, interruption and cleanup.

Rules enforced here rather than by individual steps:
- A step runs only when every earlier step passed; otherwise it is `not_run`.
- An `awaiting_human` result pauses the run; `resume` continues from that step.
- On resume, previously passed steps are kept only while the run's recorded
  identity still matches (see `seren_harness.seren.verify_identity`).
- Cleanup obligations recorded by steps are attempted after success, failure,
  block or interruption. Failed or unconfirmed cleanup stays visible.
"""
from __future__ import annotations

from dataclasses import dataclass
import signal
import traceback
from typing import Callable

from seren_harness import assertions, findings as finding_keys
from seren_harness.models import (AWAITING_HUMAN, BLOCKED, FAILED, HARNESS_DEFECT, NOT_RUN,
                                  PASSED, RUN_AWAITING, RUN_COMPLETE, RUN_INTERRUPTED,
                                  Finding, StepResult)


class Interrupted(BaseException):
    pass


@dataclass
class Step:
    name: str
    func: Callable
    description: str = ""


def _interrupt(signum, _frame):
    raise Interrupted(signum)


class JourneyRunner:
    def __init__(self, store, run_id, journey, steps, context, cleanup=None, retest_key=None):
        self.store = store
        self.run_id = run_id
        self.journey = journey
        self.steps = steps
        self.context = context
        self.cleanup = cleanup
        # A run explicitly linked as a retest attributes a reproduction of that finding to the
        # retest (classified after the run) instead of counting another occurrence.
        self.retest_key = retest_key

    def execute(self):
        previous = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGTERM, signal.SIGHUP)}
        status = RUN_COMPLETE
        try:
            status = self._run_steps()
        except (Interrupted, KeyboardInterrupt) as error:
            status = RUN_INTERRUPTED
            self.store.event(self.run_id, "interrupted", {"signal": str(error) or "keyboard"})
        finally:
            if status != RUN_AWAITING and self.cleanup is not None:
                self._run_cleanup()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self.store.update_run(self.run_id, status=status)
        return status

    def _run_steps(self):
        recorded = {s["name"]: s for s in self.store.steps(self.run_id)}
        blocker = None
        for seq, step in enumerate(self.steps):
            existing = recorded.get(step.name)
            if existing is not None and existing["outcome"] == PASSED and blocker is None:
                continue
            if blocker is not None:
                self._record(seq, step, StepResult(
                    NOT_RUN, "not run because " + blocker[0] + " was " + blocker[1]))
                continue
            result = self._record(seq, step, self._call(step))
            if result.outcome == AWAITING_HUMAN:
                self.store.event(self.run_id, "awaiting_human", {"step": step.name})
                return RUN_AWAITING
            if result.outcome != PASSED:
                blocker = (step.name, result.outcome)
        return RUN_COMPLETE

    def _call(self, step):
        try:
            return step.func(self.context)
        except (Interrupted, KeyboardInterrupt):
            raise
        except Exception as error:  # A harness bug must not look like a product result.
            self.store.event(self.run_id, "harness_exception",
                             {"step": step.name, "trace": traceback.format_exc(limit=5)})
            return StepResult(FAILED, "harness error: " + type(error).__name__, {},
                              Finding("harness_step_completes", HARNESS_DEFECT,
                                      "Harness error during " + step.name,
                                      "the step completes and reports an outcome",
                                      type(error).__name__ + ": " + str(error),
                                      context={"exception": type(error).__name__}))

    def _record(self, seq, step, result):
        # Critical checks carry a contract snapshot; a pass without its evidence is blocked.
        result = assertions.enforce(step.name, result)
        key = None
        if result.finding is not None:
            key = finding_keys.finding_key(self.journey, step.name, result.finding,
                                           self.store.run(self.run_id)["mode"])
            if key != self.retest_key:
                self.store.record_finding(key, self.run_id, self.journey, step.name,
                                          result.finding)
        self.store.record_step(self.run_id, seq, step.name, result, key)
        return result

    def _run_cleanup(self):
        try:
            self.cleanup(self.context)
        except (Interrupted, KeyboardInterrupt):
            self.store.event(self.run_id, "cleanup_interrupted", {})
        except Exception as error:
            self.store.event(self.run_id, "cleanup_error",
                             {"error": type(error).__name__ + ": " + str(error)})


def applicable_steps(expected_steps, step_rows, run_status):
    """The steps a run is judged on. A supplementary criterion with no recorded result in a
    run that was not interrupted predates that run's journey: it is omitted, never reported
    as not run or given a verdict the run never recorded."""
    names = {row["name"] for row in step_rows}
    return [s for s in expected_steps if s in names or run_status == RUN_INTERRUPTED
            or s not in assertions.SUPPLEMENTARY_STEPS]


def overall_outcome(step_rows, expected_steps):
    outcomes = {row["name"]: row["outcome"] for row in step_rows}
    values = [outcomes.get(name, NOT_RUN) for name in expected_steps]
    for outcome in (FAILED, BLOCKED, AWAITING_HUMAN):
        if outcome in values:
            return outcome
    return PASSED if values and all(v == PASSED for v in values) else NOT_RUN
