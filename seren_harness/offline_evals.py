"""One deterministic offline Pydantic Evals path over saved, sanitized retrieval fixtures.

The task is the existing classifier (journeys.observe + journeys.classify); evaluators are
deterministic Evaluator subclasses only. No model, LLM judge, live executor, retry, logfire
configuration or upload: the dataset runs serially with no progress output.
"""
import copy
from dataclasses import dataclass
import json
from pathlib import Path

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from seren_harness import journeys

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/offline-evals/branch-read-cases.json"
SPEC = json.loads((ROOT / "cases/seren-employee-branch-read.json").read_text())["employee_tool"]
NAME = "offline-branch-read-classification"
# journeys.classify folds an unresolved contract question into "failed"; keep it apart.
VERDICTS = {"passed": "pass", "incomplete": "incomplete",
            "unresolved_contract_or_attribution": "unknown"}


def verdict(result):
    return VERDICTS.get(result["classification"], "fail")


def events(fixture, case):
    """Saved output-event shape of the branch route, built from the fixture's saved shapes."""
    answer = case.get("answer", SPEC["answer"])
    if case["result"] == "none":
        return [{"type": "text", "text": answer}]
    out = [{"type": "tool_call", "id": "call-0", "name": SPEC["tool"],
            "arguments": json.dumps(SPEC["arguments"])}]
    if case["result"] == "record":
        record = dict(fixture["record"], **case.get("record_changes", {}))
        result = {"content": json.dumps({"data": record})}
    elif case["result"] == "truncated":
        result = {"content": '{"truncated":true,"content":"cut'}
    elif case["result"] == "denied":
        result = copy.deepcopy(fixture["denied"])
    else:
        raise ValueError("unknown fixture result " + case["result"])
    out.append(dict(result, type="tool_result", id="call-0"))
    out.append({"type": "text", "text": answer})
    return out


def inputs(fixture, case):
    return {"events": events(fixture, case), "provenance": "synthetic",
            "run_identity": case.get("run_identity", "match"),
            "shutdown": case.get("shutdown", "confirmed"),
            "restore": case.get("restore", "confirmed"),
            "grant_verified": case.get("grant_verified", False)}


def classify(item):
    """The task: the existing classifier, unchanged."""
    return journeys.classify(journeys.observe(item["events"]), SPEC,
                             provenance=item["provenance"], run_identity=item["run_identity"],
                             shutdown=item["shutdown"], restore=item["restore"],
                             grant_verified=item["grant_verified"])


@dataclass
class MatchesExpected(Evaluator):
    """Outcome, classification and inability report equal the fixture's expectation."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return all(ctx.output[k] == v for k, v in ctx.expected_output.items())


@dataclass
class KeepsProvenance(Evaluator):
    """The result names the input's provenance and never becomes a confirmable finding."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return (ctx.output["provenance"] == ctx.inputs["provenance"]
                and ctx.output["service_execution"] == journeys.EXECUTION[ctx.inputs["provenance"]]
                and (ctx.inputs["provenance"] == "live" or not ctx.output["confirmable"]))


EVALUATORS = (MatchesExpected, KeepsProvenance)


def dataset(path=FIXTURES):
    fixture = json.loads(Path(path).read_text())
    cases = [Case(name=c["name"], inputs=inputs(fixture, c), expected_output=c["expected"],
                  metadata={"source": fixture["source"], "provenance": "synthetic",
                            "verdict": verdict(c["expected"])})
             for c in fixture["cases"]]
    return Dataset(name=NAME, cases=cases, evaluators=[e() for e in EVALUATORS])


def run(path=FIXTURES):
    return dataset(path).evaluate_sync(classify, name=NAME, max_concurrency=1, progress=False)
