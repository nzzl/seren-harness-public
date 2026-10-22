"""SIMULATION: offline evaluation of the harness's critical checks.

Runs each case in `evaluation.json` through the real CLI, state and reports
against the simulated Seren CLI (the same setup as `demo.py`), then compares the
observed result with the case's independently written expectation. Observed
outcomes come only from the reports these runs produce.

    python3 simulation/evaluate.py [--manifest PATH] [--out DIR]

Exit 0 when every case matches its expectation, 1 otherwise. A passing
evaluation shows harness behavior on synthetic challenges only.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent
sys.path.insert(0, str(HERE))
import demo  # noqa: E402
import fake_seren  # noqa: E402

LABELS = {("accept", "accept"): "correct acceptance", ("reject", "reject"): "correct rejection",
          ("inconclusive", "inconclusive"): "correctly inconclusive"}


def verdict(outcome, clean):
    """The manifest's verdict rule, applied to an observed report."""
    if clean:
        return "accept"
    return "inconclusive" if outcome in ("blocked", "not_run") else "reject"


def classify(expected, observed):
    if (expected, observed) in LABELS:
        return LABELS[(expected, observed)]
    return {"accept": "false pass", "reject": "false failure"}.get(
        observed, "inconclusive where a decision was expected")


def baseline_verdict(steps):
    """Start/health-only: start answered with acceptance and first health was ok."""
    start = steps.get("start_runtime", {}).get("evidence", {})
    health = (steps.get("runtime_ready", {}).get("evidence", {}).get("observation") or {})
    return "accept" if start.get("journal") == "succeeded" \
        and health.get("health") == "ok" else "reject"


def judge(case, observation):
    """Compare one observation with its expectation; any difference is a mismatch."""
    expected = case["expected"]
    actual = {"verdict": observation["verdict"], "outcome": observation["outcome"],
              "step_outcome": observation["steps"].get(expected["step"], {}).get("outcome"),
              "clean_completion": observation["clean_completion"],
              "managed_start_calls": observation["managed_start_calls"]}
    mismatches = [{"field": key, "expected": expected[key], "observed": actual[key]}
                  for key in actual if key in expected and expected[key] != actual[key]]
    result = {"id": case["id"], "title": case["title"], "assertions": case["assertions"],
              "expected": expected, "observed": actual, "match": not mismatches,
              "mismatches": mismatches,
              "classification": classify(expected["verdict"], actual["verdict"]),
              "existing_tests": case["existing_tests"]}
    if case.get("baseline_applicable"):
        base = observation["baseline_verdict"]
        result["baseline"] = {"verdict": base, "match": base == expected["verdict"],
                              "classification": classify(expected["verdict"], base)}
    else:
        result["baseline"] = {"verdict": "not applicable",
                              "reason": case.get("baseline_not_applicable_reason", "")}
    return result


def _merge(target, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def run_case(case, directory):
    """Provision a fresh simulated employee, apply the case, run the journey, read the report."""
    sim = demo.Demo(directory)
    env = dict(sim.env)
    env.pop("SEREN_HARNESS_EVAL_UNSET_READER", None)

    def harness(*args):
        return subprocess.run(sim.harness_argv(*args), cwd=str(SOURCE), env=env,
                              capture_output=True, text=True)

    try:
        first = harness("provision")
        run_id = first.stdout.split("Run ", 1)[1].split()[0] if "Run " in first.stdout else ""
        setups = list(fake_seren.read_state(sim.state)["setups"])
        if first.returncode != 3 or not setups:
            raise RuntimeError("provisioning did not pause for approval")
        sim.sim("sim-approve", setups[0])
        if harness("resume", run_id).returncode != 0:
            raise RuntimeError("provisioning did not complete")
        if case["scenario"]:
            sim.sim("sim-set", json.dumps(case["scenario"]))
        config = json.loads(sim.config.read_text())
        _merge(config, case.get("config", {}))
        sim.config.write_text(json.dumps(config))
        starts = len([e for e in sim.sim_log() if e[1:2] == ["managed-start"]])
        result = harness("run")
        run_id = result.stdout.split("Run ", 1)[1].split()[0]
        report = json.loads((sim.root / "state" / "runs" / run_id / "report.json").read_text())
    finally:
        with fake_seren.locked_state(sim.state) as state:
            state["deployment"]["desired_lifecycle_state"] = "stopped"
        fake_seren.wait_for_employees(sim.state)
    steps = {s["name"]: s for s in report["steps"]}
    return {"exit": result.returncode, "outcome": report["outcome"], "steps": steps,
            "clean_completion": report["completion"]["clean"],
            "verdict": verdict(report["outcome"], report["completion"]["clean"]),
            "baseline_verdict": baseline_verdict(steps), "label": report["label"],
            "managed_start_calls": len([e for e in sim.sim_log()
                                        if e[1:2] == ["managed-start"]]) - starts}


def revision():
    try:
        top = subprocess.run(["git", "-C", str(SOURCE), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        if Path(top).resolve() != SOURCE:  # e.g. a staged snapshot inside another checkout
            return "unknown (not this repository's checkout)"
        head = subprocess.run(["git", "-C", str(SOURCE), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(SOURCE), "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown (not a Git checkout)"
    return head + (" with uncommitted changes" if dirty else "")


def tally(results, key=None):
    counts = {}
    for result in results:
        source = result if key is None else result[key]
        if key is not None and source["verdict"] == "not applicable":
            counts["not applicable"] = counts.get("not applicable", 0) + 1
            continue
        counts[source["classification"]] = counts.get(source["classification"], 0) + 1
    return counts


def summarize(manifest, results):
    return {"evaluation_version": manifest["version"], "harness_revision": revision(),
            "evidence_mode": manifest["evidence_mode"], "boundary": manifest["boundary"],
            "verdict_rule": manifest["verdict_rule"], "baseline": manifest["baseline"],
            "operator_minutes": None,
            "operator_minutes_note": "unknown: record manually; unknown is not zero",
            "successful": all(r["match"] for r in results), "harness": tally(results),
            "baseline_results": tally(results, "baseline"), "cases": results}


def render(summary):
    lines = ["# Seren Harness offline evaluation", "",
             "> SIMULATION. " + summary["boundary"], "",
             "- Result: **" + ("all cases matched" if summary["successful"]
                               else "UNSUCCESSFUL: expectation mismatch") + "**",
             "- Evaluation definition v" + str(summary["evaluation_version"])
             + "; harness " + summary["harness_revision"],
             "- Evidence mode: " + summary["evidence_mode"],
             "- Operator minutes: " + summary["operator_minutes_note"],
             "- Verdict rule: " + summary["verdict_rule"],
             "- Baseline (" + summary["baseline"]["name"] + "): "
             + summary["baseline"]["definition"], "",
             "| Case | Expected | Harness observed | Match | Baseline |", "|---|---|---|---|---|"]
    for case in summary["cases"]:
        observed = case["observed"]
        base = case["baseline"]
        lines.append("| " + case["id"] + " | " + case["expected"]["verdict"] + " | "
                     + observed["verdict"] + " (" + str(observed["outcome"]) + ", "
                     + case["expected"]["step"] + " " + str(observed["step_outcome"]) + ") | "
                     + ("yes" if case["match"] else "NO: " + "; ".join(
                         m["field"] + " expected " + json.dumps(m["expected"]) + ", observed "
                         + json.dumps(m["observed"]) for m in case["mismatches"])) + " | "
                     + (base["verdict"] if base["verdict"] == "not applicable"
                        else base["verdict"] + " (" + base["classification"] + ")") + " |")
    lines += ["", "Harness: " + json.dumps(summary["harness"], sort_keys=True),
              "Baseline: " + json.dumps(summary["baseline_results"], sort_keys=True)]
    return "\n".join(lines) + "\n"


def main(argv=None):
    options = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    options.add_argument("--manifest", default=str(HERE / "evaluation.json"))
    options.add_argument("--out", help="directory for summary.json and summary.md")
    args = options.parse_args(argv)
    manifest = json.loads(Path(args.manifest).read_text())
    out = Path(args.out or tempfile.mkdtemp(prefix="seren-harness-evaluation-"))
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for case in manifest["cases"]:
        directory = out / "cases" / case["id"]
        directory.mkdir(parents=True, exist_ok=True)
        results.append(judge(case, run_case(case, directory)))
    summary = summarize(manifest, results)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    text = render(summary)
    (out / "summary.md").write_text(text)
    print(text + "Written to " + str(out))
    return 0 if summary["successful"] else 1


if __name__ == "__main__":
    sys.exit(main())
