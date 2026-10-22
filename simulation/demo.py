"""SIMULATION: reproducible offline demonstration of Seren Harness.

Drives the real `python -m seren_harness` CLI, adapter and SQLite state against
the simulated Seren CLI. Nothing contacts Seren, Discord or GitHub. Each stage
checks its expected exit code and stops the demo if it is wrong.

    python3 simulation/demo.py [--dir <empty directory>]
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent
sys.path.insert(0, str(HERE))
import fake_seren  # noqa: E402


class Demo:
    def __init__(self, directory):
        self.root = Path(directory)
        self.exe, self.state, self.channel, self.deployment = fake_seren.install(self.root)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "environment": "simulation",
            "seren": {"executable": str(self.exe), "command_timeout_seconds": 3,
                      "poll_interval_seconds": 0.1, "readiness_timeout_seconds": 3,
                      "shutdown_timeout_seconds": 3},
            "employee": {"key": "sim-employee-1", "deployment_id": self.deployment,
                         "names": ["Simulated Test Employee"]},
            "discord": {"sender": "simulation", "observer": "simulation",
                        "channel_id": "sim-channel", "employee_user_id": "sim-employee",
                        "reply_timeout_seconds": 3,
                        "simulation_channel_file": "sim-channel.jsonl",
                        "expected_reply": {"template": "ACK {marker}",
                                           "source": "SYNTHETIC: simulator expectation"}},
            "state_dir": "state"}, indent=2))
        self.env = dict(os.environ, PYTHONPATH=str(SOURCE))

    def harness_argv(self, *args):
        return [sys.executable, "-m", "seren_harness", *args, "--config", str(self.config)]

    def harness(self, title, expected, *args):
        print("\n$ python -m seren_harness " + " ".join(args) + "   # " + title, flush=True)
        result = subprocess.run(self.harness_argv(*args), cwd=str(SOURCE), env=self.env,
                                capture_output=True, text=True)
        print((result.stdout + result.stderr).rstrip())
        print("[exit " + str(result.returncode) + "]")
        if result.returncode != expected:
            raise SystemExit("DEMO FAILED: expected exit " + str(expected))
        return result.stdout + result.stderr

    def sim(self, *args):
        subprocess.run([str(self.exe), *args], check=True)

    def scenario(self, **values):
        print("\n# simulator scenario: " + json.dumps(values))
        self.sim("sim-set", json.dumps(values))

    def latest_run(self):
        runs = sorted((self.root / "state" / "runs").iterdir(), key=lambda p: p.stat().st_mtime)
        return runs[-1].name

    def sim_log(self):
        return fake_seren.read_state(self.state)["log"]


def main():
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--dir", help="empty directory to keep the demo state in")
    args = options.parse_args()
    directory = args.dir or tempfile.mkdtemp(prefix="seren-harness-demo-")
    demo = Demo(directory)
    print("SIMULATION demo directory: " + directory)

    print("\n== 1. Provisioning with a human approval checkpoint ==")
    output = demo.harness("pauses for owner approval", 3, "provision")
    run_id = output.split("resume ", 1)[1].split()[0]
    setup_id = next(iter(fake_seren.read_state(demo.state)["setups"]))
    print("\n# simulated owner approves setup in the Passwords UI")
    demo.sim("sim-approve", setup_id)
    demo.harness("resume after approval", 0, "resume", run_id)

    print("\n== 1b. Controlled condition: readiness rejects the stopped employee ==")
    demo.harness("read-only control", 0, "control")

    print("\n== 2. Successful simulated journey ==")
    demo.harness("operational journey", 0, "run")

    print("\n== 2b. Reply content is judged separately from delivery ==")
    # Operator-supplied demonstration input (the 2026-10-13 reply as reported by the owner),
    # posted by the simulated employee; not a live observation.
    demo.scenario(reply_text="Unable to reply: the isolated test channel and employee "
                  "credential have not been explicitly configured and approved.")
    demo.harness("verify_reply passes; verify_reply_content fails with its own finding", 1,
                 "run")
    demo.scenario(reply_text=False)
    demo.harness("reply without readable content: content unverified", 2, "run")
    demo.scenario(reply_text=None)

    print("\n== 3. Failed simulated journey: start accepted, health ok, connector fails ==")
    demo.scenario(readiness="binding_failure")
    demo.harness("readiness assertion rejects it", 1, "run")
    failed_run = demo.latest_run()
    demo.harness("private draft", 0, "draft-issue", failed_run)
    demo.harness("same failure again", 1, "run")
    demo.harness("draft updated, not duplicated", 0, "draft-issue", demo.latest_run())
    draft = next((demo.root / "state" / "drafts").iterdir())
    print("\n---- " + str(draft) + " ----\n" + draft.read_text())

    print("\n== 4. Recovery: a run killed mid-journey ==")
    demo.scenario(readiness="never")
    config = json.loads(demo.config.read_text())
    config["seren"]["readiness_timeout_seconds"] = 30
    demo.config.write_text(json.dumps(config))
    process = subprocess.Popen(demo.harness_argv("run"), cwd=str(SOURCE), env=demo.env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    starts = sum(1 for entry in demo.sim_log() if entry[1:2] == ["managed-start"])
    while sum(1 for e in demo.sim_log() if e[1:2] == ["managed-start"]) == starts:
        if time.time() > deadline:
            raise SystemExit("DEMO FAILED: run did not start")
        time.sleep(0.05)
    time.sleep(0.3)
    process.kill()
    process.wait()
    print("\n# killed the harness process with SIGKILL after it started the runtime")
    status = demo.harness("open cleanup is visible", 0, "status")
    killed = status.split("OPEN CLEANUP: run ", 1)[1].split()[0]
    demo.harness("new run refused until reconciled", 4, "run")
    demo.scenario(readiness="ready")
    demo.harness("resume reconciles and finishes", 0, "resume", killed)

    print("\n== 5. Persisted output contains no secret canaries ==")
    persisted = b"".join(p.read_bytes() for p in (demo.root / "state").rglob("*") if p.is_file())
    for canary in (fake_seren.SECRET_CANARY, "c2ltdWxhdGVkY2FwYWJpbGl0eXRva2Vu",
                   "Zm9vYmFyYmF6cXV4cXV1eGNhcGFiaWxpdHk", demo.deployment):
        found = canary.encode() in b"".join(
            p.read_bytes() for p in (demo.root / "state" / "drafts").iterdir()) \
            if canary == demo.deployment else canary.encode() in persisted
        where = "drafts" if canary == demo.deployment else "state, reports, drafts"
        print(("FOUND " if found else "absent ") + canary[:20] + "... in " + where)
        if found:
            raise SystemExit("DEMO FAILED: canary persisted")
    print("\nSIMULATION demo complete. State kept in " + directory)


if __name__ == "__main__":
    main()
