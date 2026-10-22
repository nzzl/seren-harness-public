"""Offline example: one research brief and its dependent revision, through the real entry point.

Everything here is synthetic. A stand-in Seren CLI (tests/functional_standin.py) plays a
research employee that "retrieves" two synthetic foundation pages and answers; an in-process
fake web serves the same synthetic pages to the harness's own source reader. Nothing contacts
Seren, a model or the internet, and no account or credential is used.

    python3 examples/research-journey/run_offline.py [empty-or-new-directory]

It reuses the research journey's test scenario rather than a separate demo framework, then
prints where the local report is and what the harness observed. Exit 0 only when the journey
ends `facts_checked` in exactly two assignments and the revision quoted the actual brief.
"""
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from seren_harness import functional_research as research  # noqa: E402
from test_functional_research import DESIGNATION, GOOD, Web, plans, run, shared_env  # noqa: E402


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else Path(tempfile.mkdtemp(prefix="seren-harness-example-"))
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        print("Choose an empty or new directory: %s is not empty." % root)
        return 2
    env = shared_env(root, isolation=False)
    env.campaign["employee"].update(use="owner_designated_test", designation=DESIGNATION)
    env.scenario["replies"] = plans(*GOOD)
    env.write()

    code, output = run(env, Web())
    print(output.rstrip())
    state = env.state()
    journey = state["journeys"][0]
    stages = [next(t for t in state["tasks"] if t["id"] == s["item"]) for s in journey["stages"]]
    messages = [json.loads(argv_[argv_.index("--message") + 1]) for argv_ in env.starts()]

    print("\n--- Synthetic offline example: what the harness observed ---")
    print("journey %s: %s after %d assignment(s)" % (journey["template"], journey["status"], len(messages)))
    for stage in stages:
        evaluation = stage["evaluation"]
        print("  %-8s verdict %-9s checked claims: %s" % (
            stage["stage"], evaluation["verdict"], ", ".join(evaluation["checked_claims"]) or "none"))
        for note in evaluation["interpretations"]:
            print("           not verified: %s%s" % ("claim %s, " % note["claim"] if note.get("claim") else "",
                                                   note["why"]))
    quoted = False
    if len(messages) == 2:
        context = messages[1]["task"]["context"]
        brief = stages[0]["evaluation"]["deliverable"]
        quoted = context["previousDeliverable"] == brief and context["previousDeliverableSha256"] == research.digest(brief)
        print("revision request quotes the recorded brief (SHA-256 %s...): %s" % (
            context["previousDeliverableSha256"][:12], "yes" if quoted else "NO"))
    print("local report: %s" % (env.folder / "report.md"))
    print("All inputs and results above are synthetic; this is not evidence about the real service.")
    ok = code == 0 and journey["status"] == "facts_checked" and len(messages) == 2 and quoted
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
