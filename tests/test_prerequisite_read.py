"""Fixed CLI error classification and the one-read `prerequisite-read` route.

SYNTHETIC: error texts are constructed from the pinned v0.10.0 source formats (anyhow
`Error: …` / `Caused by:`, config.rs, auth.rs, agent.rs, progenitor-client 0.14.0 and
reqwest 0.13.4 Display, Rust io::Error) with sentinel secrets embedded. A disposable
stand-in CLI prints them; the real SerenCli wrapper runs it. No Seren or Discord service.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import cli, cli_errors, execution_limits, prerequisite_read as pr
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import DECODE_ERROR, ERROR, TIMEOUT, UNAVAILABLE, CliResult, SerenCli

DEPLOYMENT = "7a00003a-0000-4000-8000-00000000003b"
IDENTITY = "7a000004-0000-4000-8000-000000000005"
REVISION = "7a00002b-0000-4000-8000-00000000002c"
PACKAGE = SOURCE / "docs/lifecycle-setup/prerequisite-read.proposed.json"
SENTINELS = ("zqSENTINELpath", "zqSENTINELhost", "zqSENTINELbody", "zqSENTINELheader",
             "zqSENTINELvalue", "ZQ_SENTINEL_KEY", "zqSENTINELmessage", "zqSENTINELfield")
PATH = "/opt/seren-handover"
URL = "https://zqSENTINELhost.example/oauth/token?code=zqSENTINELvalue"

SOURCE_BACKED = [
    # (stderr, exit, category, rule, stage, http_status, before_main_request)
    ("Error: Not authenticated. Run 'seren auth login' first.\nConfig path: " + PATH, 1,
     "session_unavailable", "config_missing_or_inaccessible", "local_session", None, True),
    ("Error: No valid authentication token found", 1,
     "session_unavailable", "no_token_in_session", "local_session", None, True),
    ("Error: Could not read config file\n\nCaused by:\n    Permission denied (os error 13)", 1,
     "local_permission_denied", "session_file_unreadable", "local_session", None, True),
    ("Error: Could not read config file\n\nCaused by:\n    Is a directory (os error 21)", 1,
     "session_unavailable", "session_file_unreadable", "local_session", None, True),
    ("Error: Could not parse config file\n\nCaused by:\n    TOML parse error zqSENTINELvalue", 1,
     "session_unavailable", "session_file_invalid", "local_session", None, True),
    ("Error: Could not write config file\n\nCaused by:\n    Operation not permitted (os error 1)",
     1, "local_permission_denied", "session_file_unwritable", "local_session", None, True),
    ("Error: Could not create profile directory\n\nCaused by:\n    Permission denied (os error "
     "13)", 1, "local_permission_denied", "config_directory_unavailable", "local_session", None,
     True),
    ("Error: Operation not permitted (os error 1)", 1,
     "local_permission_denied", "bare_os_permission_error", "local_session", None, True),
    ("Error: Token refresh failed: Communication Error: error sending request for url (" + URL
     + ")", 1, "network_failure", "token_refresh_communication", "token_refresh", None, True),
    # Refresh HTTP failures: the stage is established, the cause is not (no status or
    # credential rejection is read from progenitor's Unexpected/Error Response text).
    ("Error: Token refresh failed: Unexpected Response: Response { status: 400, headers: "
     "{\"x-zqSENTINELheader\": \"1\"} }", 1,
     "unknown", "token_refresh_http_error", "token_refresh", None, True),
    ("Error: Token refresh failed: Unexpected Response: Response { url: \"" + URL + "\", "
     "status: 500, headers: {\"x-zqSENTINELheader\": \"zqSENTINELvalue\"} }", 1,
     "unknown", "token_refresh_http_error", "token_refresh", None, True),
    ("Error: Token refresh failed: Unexpected Response: Response { url: \"" + URL + "\", "
     "headers: {} }", 1,  # no trustworthy status
     "unknown", "token_refresh_http_error", "token_refresh", None, True),
    ("Error: Token refresh failed: Error Response: status: 401 Unauthorized; headers: "
     "{\"x-zqSENTINELheader\": \"1\"}; value: ()", 1,  # no evidence of credential rejection
     "unknown", "token_refresh_http_error", "token_refresh", None, True),
    ("Error: Token refresh failed: Invalid Response Payload (b\"zqSENTINELbody\"): expected "
     "value", 1, "malformed_response", "token_refresh_payload", "token_refresh", None, True),
    ("Error: Failed to get managed agent detail: unexpected response 401 Unauthorized - "
     "{\"error\":\"zqSENTINELbody\"}", 1,
     "authentication_rejected", "http_status", "request", 401, False),
    ("Error: Failed to get managed agent detail: unexpected response 403 Forbidden - "
     "zqSENTINELbody", 1, "service_access_denied", "http_status", "request", 403, False),
    ("Error: Failed to get managed agent detail: unexpected response 404 Not Found - "
     "zqSENTINELbody", 1, "service_client_error", "http_status", "request", 404, False),
    ("Error: Failed to get managed agent detail: unexpected response 503 Service Unavailable - "
     "zqSENTINELbody", 1, "server_error", "http_status", "request", 503, False),
    ("Error: Failed to get managed agent detail: Communication Error: error sending request "
     "for url (" + URL + ")", 1, "network_failure", "request_communication", "request", None,
     None),
    ("Error: Failed to get managed agent detail: Invalid Response Payload (b\"zqSENTINELbody\")"
     ": expected value", 1, "malformed_response", "request_payload", "request", None, False),
    ("error: unexpected argument '--zqSENTINELvalue' found\n\nUsage: seren agent managed-get", 2,
     "executable_or_argument", "cli_usage_error", "argument_parsing", None, True),
]
AMBIGUOUS = [
    ("Error: something unfamiliar zqSENTINELmessage", 1),
    ("", 1),
    ("Error: Not authenticated. Run 'seren auth login' first.", 3),  # unexpected exit code
    ("Not authenticated. Run 'seren auth login' first.", 1),  # no anyhow prefix
    ("Error: wrapper: Not authenticated. Run 'seren auth login' first.", 1),  # not leading
    ("Permission denied (os error 13)", 1),
    ("Error: Failed to get managed agent detail: unexpected response 403 Forbidden - "
     "Permission denied (os error 13)", 1),  # service denial and local OS text: conflicting
    ("Error: Token refresh failed: Communication Error: x (Permission denied (os error 13))",
     1),  # network and local permission signatures together
    ("Error: Failed to get managed agent detail: unexpected response 2xx", 1),
    ("Error: Token refresh failed: zqSENTINELmessage", 1),
]


class Classifier(unittest.TestCase):
    def test_source_backed_errors_map_to_their_fixed_category(self):
        for text, code, category, rule, stage, status, before in SOURCE_BACKED:
            with self.subTest(rule=rule, category=category):
                found = cli_errors.classify(CliResult(ERROR, [], None, code, text))
                self.assertEqual((found["category"], found["rule"], found["stage"],
                                  found["http_status"], found["before_main_request"]),
                                 (category, rule, stage, status, before))
                self.assertEqual(found["http_response_received"],
                                 True if status or rule == "request_payload" else None)

    def test_no_refresh_failure_is_reported_as_authentication_rejection(self):
        for text, code, *_ in SOURCE_BACKED + [(t, c) for t, c in AMBIGUOUS]:
            if "Token refresh failed" in text:
                found = cli_errors.classify(CliResult(ERROR, [], None, code, text))
                self.assertNotEqual(found["category"], "authentication_rejected", text[:60])
                self.assertIsNone(found["http_status"])
        conflicting = ("Error: Token refresh failed: Unexpected Response: Response { status: "
                       "500, headers: {\"x\": \"Permission denied (os error 13)\"} }")
        found = cli_errors.classify(CliResult(ERROR, [], None, 1, conflicting))
        self.assertEqual((found["category"], found["rule"]),
                         ("unknown", "conflicting_signatures"))

    def test_structured_outcomes(self):
        cases = {TIMEOUT: "timeout", UNAVAILABLE: "executable_or_argument",
                 DECODE_ERROR: "malformed_response"}
        for status, category in cases.items():
            self.assertEqual(cli_errors.classify(CliResult(status, []))["category"], category)
        self.assertIsNone(cli_errors.classify(CliResult("ok", [], {}, 0)))
        timeout = cli_errors.classify(CliResult(TIMEOUT, []))
        self.assertIsNone(timeout["before_main_request"])  # a timeout says nothing about reach

    def test_ambiguous_contradictory_and_unfamiliar_errors_stay_unknown(self):
        for text, code in AMBIGUOUS:
            with self.subTest(text=text[:40]):
                found = cli_errors.classify(CliResult(ERROR, [], None, code, text))
                self.assertEqual(found["category"], "unknown")
                self.assertIsNone(found["before_main_request"])
                self.assertIsNone(found["http_status"])

    def test_every_category_and_value_is_a_fixed_string(self):
        for text, code, *_ in SOURCE_BACKED + [(t, c) for t, c in AMBIGUOUS]:
            found = cli_errors.classify(CliResult(ERROR, [], None, code, text))
            self.assertIn(found["category"], cli_errors.CATEGORIES)
            self.assertNotIn("sentinel", json.dumps(found).lower())


STANDIN = r'''#!{python} -I
import json, os, sys, time
path = os.environ["STANDIN_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
if args == ["--version"]:
    print("seren 0.10.0 (simulation)"); sys.exit(0)
with open(path + ".calls", "a") as f:
    f.write(" ".join(args[:4] if args[1:2] == ["cloud"] else args[:2]) + "\n")
if args[1:2] != ["managed-get"] or args[3:] != ["-o", "json"]:
    sys.stderr.write("error: unexpected argument\n"); sys.exit(2)
time.sleep(state.get("sleep", 0))
sys.stderr.write(state.get("stderr", ""))
print(state.get("stdout", ""))
sys.exit(state.get("exit", 0))
'''


def detail(**changes):
    original = (SOURCE / "docs/lifecycle-setup/SKILL.current.md").read_text()
    value = {"deployment_id": DEPLOYMENT, "agent_identity_id": IDENTITY,
             "active_revision_id": REVISION, "status": "stopped",
             "secret_resolution_result_id": "srr-zqSENTINELvalue",
             "config": {"zqSENTINELfield": "zqSENTINELvalue"},
             "secret_keys": ["DISCORD_BOT_TOKEN", "ZQ_SENTINEL_KEY"],
             "conditions": [{"type": "AwaitingApproval", "status": "False",
                             "message": "zqSENTINELmessage"}],
             "zqSENTINELfield": "zqSENTINELbody",
             "bundle": {"instructions": [{"kind": "skill", "content": original}]}}
    value.update(changes)
    return value


class Route(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "docs/lifecycle-setup").mkdir(parents=True)
        for name in ("SKILL.current.md", "SKILL.corrected.md"):
            shutil.copy(SOURCE / "docs/lifecycle-setup" / name,
                        self.root / "docs/lifecycle-setup" / name)
        exe = self.root / "seren"
        exe.write_text(standin(STANDIN.replace("{python}", sys.executable)))
        exe.chmod(0o755)
        prerun(exe)  # first-run assessment outside the 1 s timed command
        self.state = self.root / "standin.json"
        self.config = self.root / "config.json"
        pins = {k: v for k, v in execution_limits.PINNED.items()
                if k != "reply_timeout_seconds"}
        self.config.write_text(json.dumps({
            "environment": "simulation",
            "seren": dict(pins, executable=str(exe), command_timeout_seconds=1),
            "employee": {"key": "sim", "deployment_id": DEPLOYMENT},
            "discord": {"sender": "human", "observer": "discord_rest", "channel_id": "c",
                        "employee_user_id": "e", "reply_timeout_seconds": 120,
                        "expected_reply": {"template": "ACK {marker}", "source": "SYNTHETIC"}},
            "state_dir": "state"}))
        self.outputs = []

    def scenario(self, **state):
        self.state.write_text(json.dumps(state))

    def ok(self, **changes):
        self.scenario(stdout=json.dumps({"data": detail(**changes)}))

    def run_read(self, package=PACKAGE, cli_json=None):
        out = io.StringIO()
        pinned = dict(execution_limits.PINNED, command_timeout_seconds=1)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"STANDIN_STATE": str(self.state)}))
            stack.enter_context(mock.patch.object(execution_limits, "PINNED", pinned))
            stack.enter_context(contextlib.redirect_stdout(out))
            if cli_json:
                stack.enter_context(mock.patch.object(SerenCli, "json", cli_json))
            try:
                code = cli.main(["prerequisite-read", "--config", str(self.config),
                                 "--package", str(package), "--root", str(self.root)])
            except KeyboardInterrupt:
                code = "interrupted"
        self.output = out.getvalue()
        self.outputs.append(self.output)
        self.assertLessEqual(set(self.calls()), {"agent managed-get"})
        return code

    def calls(self):
        path = Path(str(self.state) + ".calls")
        return path.read_text().splitlines() if path.exists() else []

    def journal_path(self):
        return self.root / "state/prerequisite-read/seren.lifecycle.prerequisite-read.1" \
            / "journal.json"

    def journal(self):
        return json.loads(self.journal_path().read_text())

    def test_a_successful_read_keeps_only_the_safe_projection_once(self):
        self.ok()
        self.assertEqual(self.run_read(), pr.READ_MATCHES, self.output)
        self.assertEqual(self.calls(), ["agent managed-get"])
        journal = self.journal()
        self.assertEqual(journal["invocation"]["timeout_seconds"], 1)
        self.assertEqual(set(journal["observation"]), {
            "detail_available", "deployment_matches_target", "agent_identity_matches_baseline",
            "active_revision_id", "revision_matches_baseline", "status", "approval_applied",
            "instruction_is_original", "instruction_is_corrected", "conditions",
            "undocumented_conditions"})
        self.assertEqual(journal["observation"]["conditions"],
                         [{"type": "AwaitingApproval", "status": "False"}])
        self.assertIsNone(journal["failure"])
        self.assertEqual(self.run_read(), pr.READ_MATCHES)  # never resent
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("already used", self.output)

    def test_a_read_of_a_different_state_is_reported_not_repaired(self):
        self.ok(active_revision_id="11111111-2222-4333-8444-555555555555")
        self.assertEqual(self.run_read(), pr.READ_DIFFERS)
        self.assertFalse(self.journal()["observation"]["revision_matches_baseline"])
        self.assertEqual(len(self.calls()), 1)

    def test_a_failure_keeps_only_its_fixed_classification(self):
        self.scenario(stderr="Error: Not authenticated. Run 'seren auth login' first.\n"
                      "Config path: " + PATH + "\n", exit=1)
        self.assertEqual(self.run_read(), pr.NOT_READ)
        failure = self.journal()["failure"]
        self.assertEqual((failure["category"], failure["rule"]),
                         ("session_unavailable", "config_missing_or_inaccessible"))
        self.assertIsNone(self.journal()["observation"])
        self.assertIn("failure session_unavailable", self.output)

    def test_source_backed_errors_classify_the_same_through_the_wrapper(self):
        for text, code, category, rule, *_ in SOURCE_BACKED:
            if code != 1:
                continue
            with self.subTest(rule=rule):
                shutil.rmtree(self.root / "state", ignore_errors=True)
                self.scenario(stderr=text + "\n", exit=code)
                self.assertEqual(self.run_read(), pr.NOT_READ)
                failure = self.journal()["failure"]
                self.assertEqual((failure["category"], failure["rule"]), (category, rule))

    def test_a_long_refresh_error_keeps_its_stage_through_wrapper_truncation(self):
        headers = ", ".join("\"x-zqSENTINELheader-%d\": \"zqSENTINELvalue\"" % i
                            for i in range(60))  # well over the wrapper's 800-character cut
        self.scenario(stderr="Error: Token refresh failed: Unexpected Response: Response { "
                      "url: \"" + URL + "\", status: 503, headers: {" + headers + "} }\n",
                      exit=1)
        self.assertEqual(self.run_read(), pr.NOT_READ)
        failure = self.journal()["failure"]
        self.assertEqual((failure["category"], failure["rule"], failure["stage"],
                          failure["http_status"]),
                         ("unknown", "token_refresh_http_error", "token_refresh", None))
        self.assertNotIn("sentinel", self.journal_path().read_text().lower())

    def test_timeout_malformed_and_interruption_consume_the_allowance(self):
        for label, state, category in (
                ("timeout", dict(sleep=30), "timeout"),
                ("not json", dict(stdout="zqSENTINELbody"), "malformed_response"),
                ("shape", dict(stdout=json.dumps({"data": "zqSENTINELbody"})),
                 "malformed_response")):
            with self.subTest(label):
                shutil.rmtree(self.root / "state", ignore_errors=True)
                Path(str(self.state) + ".calls").unlink(missing_ok=True)
                self.scenario(**state)
                self.assertEqual(self.run_read(), pr.NOT_READ)
                self.assertEqual(self.journal()["failure"]["category"], category)
                self.assertEqual(self.run_read(), pr.NOT_READ)
                self.assertEqual(len(self.calls()), 1)

    def test_an_interrupted_read_is_never_resent(self):
        self.ok()

        def interrupt(cli_self, *args):
            raise KeyboardInterrupt
        self.assertEqual(self.run_read(cli_json=interrupt), "interrupted")
        self.assertEqual(self.journal()["invocation"]["command"], "agent managed-get")
        self.assertIsNone(self.journal()["result"])
        self.assertEqual(self.run_read(), pr.NOT_READ)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.journal()["failure"]["rule"], "interrupted")
        self.assertEqual(self.run_read(), pr.NOT_READ)
        self.assertEqual(self.calls(), [])

    def test_changed_inputs_are_refused_before_any_call(self):
        self.scenario(stderr="Error: No valid authentication token found\n", exit=1)
        self.run_read()
        frozen = self.journal_path().read_bytes()
        config = json.loads(self.config.read_text())
        config["employee"]["names"] = ["changed"]
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.run_read(), pr.INVALID)
        self.assertIn("config_digest", self.output)
        config["employee"].pop("names")
        self.config.write_text(json.dumps(config))
        package = json.loads(PACKAGE.read_text())
        package["limits"]["window_minutes"] = 99
        other = self.root / "package.json"
        other.write_text(json.dumps(package))
        self.assertEqual(self.run_read(other), pr.INVALID)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.journal_path().read_bytes(), frozen)

    def test_only_managed_get_exists_and_earlier_journals_are_untouched(self):
        old = {}
        for relative in ("instruction-update/seren.lifecycle.response-correctness",
                         "instruction-update/seren.lifecycle.response-correctness.attempt-2",
                         "preview-diagnostic/seren.lifecycle.preview-diagnostic.1"):
            path = self.root / "state" / relative / "journal.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"result": "stopped", "invocations": [{}]}))
            old[path] = path.read_bytes()
        self.ok()
        self.assertEqual(self.run_read(), pr.READ_MATCHES)
        for path, content in old.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(pr.COMMAND, ("agent", "managed-get"))
        source = Path(pr.__file__).read_text()
        for word in ('"managed-preview"', '"managed-update"', '"cloud"', '"managed-start"',
                     '"managed-stop"', "discord"):
            self.assertNotIn(word, source)
        reader = pr.PrerequisiteRead.__new__(pr.PrerequisiteRead)
        reader.journal = self.journal()
        with self.assertRaises(RuntimeError):
            reader.read()

    def test_sentinels_never_reach_any_artifact_or_output(self):
        runs = [dict(stdout=json.dumps({"data": detail()}))]
        runs += [dict(stderr=text + "\n", exit=code) for text, code, *_ in SOURCE_BACKED]
        runs += [dict(stderr=text + "\n", exit=code) for text, code in AMBIGUOUS]
        runs += [dict(stdout="zqSENTINELbody " + URL)]
        for index, state in enumerate(runs):
            shutil.rmtree(self.root / "state", ignore_errors=True)
            self.scenario(**state)
            self.run_read()
            texts = [p.read_text() for p in (self.root / "state").rglob("*") if p.is_file()]
            texts.append(self.output)
            forbidden = set()
            for item in list(SENTINELS) + [PATH, URL, state.get("stderr", ""),
                                            state.get("stderr", "").strip(),
                                            state.get("stdout", "")]:
                if item.strip():
                    data = item.encode()
                    forbidden |= {item, hashlib.sha256(data).hexdigest(),
                                  hashlib.sha1(data).hexdigest(), hashlib.md5(data).hexdigest(),
                                  hashlib.sha256(data).hexdigest()[:16]}
            binding = set(map(str, self.journal()["binding"].values()))
            for text in texts:
                self.assertNotIn("sentinel", text.lower(), index)
                for item in forbidden:
                    self.assertNotIn(item, text, index)
                for digest in re.findall(r"\b[0-9a-f]{32,64}\b", text):
                    self.assertIn(digest, binding, index)
                self.assertNotIn("stderr", text)
                self.assertNotIn("http://", text)
                self.assertNotIn("https://", text)


class Package(unittest.TestCase):
    def test_the_package_matches_the_command(self):
        package = json.loads(PACKAGE.read_text())
        self.assertEqual(package["kind"], "prerequisite_read_package")
        self.assertEqual(package["command"], "python -m seren_harness prerequisite-read "
                         "--config ~/seren-harness-live/config.json --package "
                         "docs/lifecycle-setup/prerequisite-read.proposed.json")
        self.assertEqual(package["limits"]["service_invocations"], pr.MAX_INVOCATIONS)
        self.assertEqual(package["limits"]["timeout_seconds"],
                         execution_limits.PINNED["command_timeout_seconds"])
        self.assertEqual(package["target"]["deployment_id"], DEPLOYMENT)
        others = [json.loads((SOURCE / "docs/lifecycle-setup" / name).read_text())["id"]
                  for name in ("preview-diagnostic.proposed.json",
                               "response-correctness.proposed.json")]
        self.assertNotIn(package["id"], others)


class SanitizerKeepsSignatures(unittest.TestCase):
    def test_wrapper_sanitizing_does_not_change_a_category(self):
        sanitizer = Sanitizer(["zqSENTINELvalue"])
        for text, code, category, *_ in SOURCE_BACKED:
            cleaned = sanitizer.text(text)
            found = cli_errors.classify(CliResult(ERROR, [], None, code, cleaned))
            self.assertEqual(found["category"], category, text[:50])


if __name__ == "__main__":
    unittest.main()
