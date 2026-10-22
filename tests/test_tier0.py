"""Tier 0 lab pieces that do not need the pinned binary: the loopback fake API, the
bounded runner and the case judges (on synthetic facts). The real-binary experiments run
through `python -m seren_harness discovery run-case` or the discovery loop."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

from seren_harness import tier0, tier0_cases as cases
from test_model_worker import MACOS_SANDBOX


def get(url, method="GET", body=None):
    request = urllib.request.Request(url, method=method, data=body,
                                     headers={"Authorization": "Bearer t",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()


class FakeApiTests(unittest.TestCase):
    def test_scripted_replies_are_consumed_in_order_and_the_last_repeats(self):
        routes = {("GET", "/a"): [{"status": 404, "json": {"n": 1}}, {"status": 200, "json": {"n": 2}}]}
        with tier0.FakeApi(routes) as api:
            codes = [get(api.base + "/a")[0] for _ in range(3)]
        self.assertEqual([404, 200, 200], codes)
        self.assertEqual("Bearer t", api.requests[0]["headers"]["authorization"])

    def test_unscripted_requests_are_flagged_not_answered_successfully(self):
        with tier0.FakeApi({}) as api:
            code, _ = get(api.base + "/elsewhere", "POST", b'{"x":1}')
        self.assertEqual(501, code)
        self.assertTrue(api.requests[0]["unscripted"])
        self.assertEqual({"x": 1}, api.requests[0]["body"])

    def test_stream_holds_open_after_frames_and_records_who_closed(self):
        reply = {"sse": [{"event": "run.event", "id": 1, "data": {"type": "done"}}], "hold": 0.6}
        with tier0.FakeApi({("GET", "/s"): [reply]}) as api:
            started = time.monotonic()
            status, body = get(api.base + "/s")
            elapsed = time.monotonic() - started
        self.assertEqual(200, status)
        self.assertGreaterEqual(elapsed, 0.5)  # the decisive frame did not close the stream
        self.assertIn(b'event: run.event\nid: 1\ndata: {"type":"done"}\n\n', body)
        self.assertTrue(api.requests[0]["sse"][-1].get("server_closed"))

    def test_a_departing_client_is_recorded(self):
        reply = {"sse": [{"event": "x", "data": "{}"}], "hold": 3}
        with tier0.FakeApi({("GET", "/s"): [reply]}) as api:
            sock = socket.create_connection(("127.0.0.1", int(api.base.rsplit(":", 1)[1])))
            sock.sendall(b"GET /s HTTP/1.0\r\n\r\n")
            sock.recv(200)
            sock.close()
            for _ in range(40):
                # The request is recorded with an empty frame list before the first write.
                if api.requests and (api.requests[0].get("sse") or [{}])[-1].get("client_gone"):
                    break
                time.sleep(0.1)
        self.assertTrue(api.requests[0]["sse"][-1].get("client_gone"))

    def test_multiline_data_is_framed_per_line(self):
        self.assertEqual(b"data: a\ndata: b\n\n", tier0.sse_bytes({"data": "a\nb"}))


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def bare_lab(self):
        lab = tier0.Lab.__new__(tier0.Lab)
        lab.workdir, lab.active_pid, lab.counter = self.tmp, None, 0
        return lab

    def test_refuses_an_unpinned_executable(self):
        fake = self.tmp / "seren"
        fake.write_text("#!/bin/sh\n")
        with self.assertRaises(tier0.IsolationError):
            tier0.Lab(fake, self.tmp / "lab", real_home="/nonexistent-home")

    def test_refuses_a_lab_inside_the_operator_home(self):
        with self.assertRaises(tier0.IsolationError):
            tier0.Lab(self.tmp / "x", self.tmp / "lab", real_home=self.tmp)

    def test_environment_is_an_explicit_allowlist(self):
        lab = self.bare_lab()
        env = lab.env("http://127.0.0.1:1", self.tmp)
        self.assertEqual("http://127.0.0.1:1", env["SEREN_OAUTH_HOST"])
        self.assertNotIn("HTTPS_PROXY", env)
        with self.assertRaises(tier0.IsolationError):
            lab.env("http://127.0.0.1:1", self.tmp, {"HTTPS_PROXY": "http://elsewhere"})

    def test_a_hang_is_killed_reaped_and_reported_as_a_supervisor_kill(self):
        lab = self.bare_lab()
        started = time.monotonic()
        out = lab.execute(["/bin/sleep", "30"], {"PATH": "/usr/bin:/bin"}, 0.5)
        self.assertTrue(out["killed_by_supervisor"])
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(lab.active_pid)

    def test_a_process_that_exits_itself_is_not_a_supervisor_kill(self):
        out = self.bare_lab().execute(["/bin/sh", "-c", "echo hi; exit 3"], {}, 5)
        self.assertEqual((3, False, "hi\n"), (out["exit"], out["killed_by_supervisor"],
                                              out["stdout"]))

    def test_saved_credentials_use_the_pinned_field_names(self):
        lab = self.bare_lab()
        home = lab.home(cases.saved_credentials(access="a\nb"))
        text = (home / ".config/seren/profiles/default/credentials.toml").read_text()
        self.assertIn('access_token = "a\\nb"', text)
        self.assertIn('session_id = "7a000016-0000-4000-8000-000000000017"', text)


class LabServerTests(unittest.TestCase):
    """The trusted executor boundary used by sandboxed workers."""

    def serve(self):
        fake = mock.patch.object(tier0, "run_spec",
                                 lambda lab, spec: {"exit": 0, "echo": spec["args"]})
        fake.start()
        self.addCleanup(fake.stop)
        server = tier0.LabServer(object(), {"preflight": "ok"})
        server.__enter__()
        self.addCleanup(server.__exit__)
        return server

    def test_round_trip_and_isolation_evidence(self):
        server = self.serve()
        client = tier0.LabClient(**dict(zip(("url", "token"), server.env().values())))
        self.assertEqual({"preflight": "ok"}, client.isolation())
        self.assertEqual(["a"], client.run_spec({"args": ["a"], "routes": []})["echo"])

    def test_a_wrong_token_is_refused(self):
        server = self.serve()
        client = tier0.LabClient(server.env()["SEREN_TIER0_LAB"], "wrong")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            client.isolation()
        self.assertEqual(403, caught.exception.code)
        caught.exception.close()

    @MACOS_SANDBOX
    def test_a_sandboxed_worker_reaches_the_lab_over_loopback(self):
        from seren_harness import model_worker
        server = self.serve()
        disc = Path(tempfile.mkdtemp())
        code = ("from seren_harness import tier0; c = tier0.lab_from_environment(); "
                "print(c.run_spec({'args': ['ok'], 'routes': []})['echo'][0])")
        argv = model_worker.sandboxed(disc, disc, [sys.executable, "-c", code])
        env = dict(server.env(), PATH="/usr/bin:/bin",
                   PYTHONPATH=str(Path(tier0.__file__).resolve().parents[1]))
        out = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual("ok", out.stdout.strip(), out.stderr[-500:])

    def test_specs_are_bounded_data(self):
        with self.assertRaises(ValueError):
            tier0.run_spec(None, {"args": [], "routes": [], "deadline": 999})
        workdir = Path(tempfile.mkdtemp())
        lab = mock.Mock(workdir=workdir)
        with self.assertRaises(ValueError):
            tier0.run_spec(lab, {"args": [], "routes": [], "files": {"../x": "y"}})

    def test_case_scenarios_serialize_to_specs(self):
        for case_id in cases.ORDER:
            for scenario in cases.CASES[case_id]["scenarios"]():
                data = json.loads(json.dumps(cases.spec(scenario)))
                self.assertEqual(scenario.args, data["args"], case_id)


def stream(ending="self", marker=True, exit=0, events=0):
    return {"ending": ending, "exit": exit, "marker_printed": marker, "requests": 1,
            "frames_printed": ["run.state", "replay_complete"] + ["run.event"] * events}


class JudgeTests(unittest.TestCase):
    def stream_facts(self, defect):
        facts = {name: stream() for name in (s.name for s in cases._stream_scenarios())}
        facts["control-http-404"] = stream(exit=1)
        facts["done-ok-server-closes"] = stream("server_close")
        whole = stream("server_close", events=5)
        for name in ("control-recorded-tool-without-status", "recorded-error-tool-closes",
                     "recorded-completed-tool-closes"):
            facts[name] = whole
        facts["recorded-completed-tool-open"] = stream("supervisor_kill", exit=None, events=5)
        if defect:
            for name in ("done-ok-open", "done-error-open", "state-timeout-terminal-open",
                         "state-blocked-terminal-open", "state-error-terminal-open"):
                facts[name] = stream("supervisor_kill", exit=None)
            facts["tool-completed"] = stream(marker=False)
            facts["recorded-completed-tool-open"] = stream(marker=False, events=1)
            facts["recorded-completed-tool-closes"] = stream(marker=False, events=1)
        return facts

    def test_recorded_shapes_reproduce_early_end_and_a_fix_would_show(self):
        found = {r["part"]: r for r in cases.judge_stream(self.stream_facts(True))}
        tool = found["tool-completed"]
        self.assertEqual(("contract_question", "inferred"), (tool["disposition"], tool["grade"]))
        self.assertIn("recorded", tool["summary"])
        self.assertTrue(any("not observed" in b for b in tool["basis"]))
        fixed = {r["part"]: r for r in cases.judge_stream(self.stream_facts(False))}
        self.assertEqual("not_reproduced", fixed["tool-completed"]["disposition"])

    def test_a_failed_recorded_shape_control_blocks_the_tool_part(self):
        facts = self.stream_facts(True)
        facts["control-recorded-tool-without-status"] = stream(marker=False, events=1)
        found = {r["part"]: r for r in cases.judge_stream(facts)}
        self.assertEqual("blocked", found["tool-completed"]["disposition"])

    def test_recorded_frames_keep_recorded_fields_and_mark_the_rest_synthetic(self):
        frames = cases.recorded_events("completed-tool")
        self.assertEqual([1, 2, 3, 4, 5], [f["id"] for f in frames])
        first = frames[0]["data"]
        self.assertEqual(("tool_call", "completed", "tool_call_started"),
                         (first["type"], first["status"], first["kind"]))
        self.assertEqual("tier0_tool", first["name"])  # placeholder, not a recorded value
        self.assertEqual(cases.MARKER, frames[2]["data"]["text"])
        self.assertNotIn("status", cases.recorded_events(
            "completed-tool", drop_tool_status=True)[0]["data"])

    def test_stream_findings_rest_on_assumed_frames_so_they_are_questions(self):
        # Generalized regression scenario; private investigation history omitted.
        # say when the server closes, so these were overclaimed as client defects.
        found = cases.judge_stream(self.stream_facts(True))
        by_part = {r["part"]: r for r in found}
        for part in ("done-frame", "terminal-statuses", "tool-completed"):
            self.assertEqual("contract_question", by_part[part]["disposition"], part)
            self.assertEqual("inferred", by_part[part]["grade"], part)
            self.assertTrue(by_part[part]["question"], part)
            self.assertTrue(by_part[part]["impact"].startswith("hypothetical"), part)
        self.assertEqual("observed_behavior", by_part["exit-codes"]["disposition"])
        self.assertNotIn("reproduced_client_defect", {r["disposition"] for r in found})
        fixed = {r["part"]: r["disposition"] for r in cases.judge_stream(self.stream_facts(False))}
        self.assertEqual("not_reproduced", fixed["done-frame"])
        self.assertEqual("not_reproduced", fixed["terminal-statuses"])
        self.assertEqual("not_reproduced", fixed["tool-completed"])

    def test_run_id_on_the_scoped_route_is_a_question_under_an_ambiguous_contract(self):
        ctl = {"requests": 1, "run_id_sent": False, "accepted_printed": True}
        facts = {"control-without-run-id": ctl,
                 "with-run-id": dict(ctl, run_id_sent=True)}
        found = cases.judge_runid(facts)[0]
        self.assertEqual(("contract_question", "inferred"), (found["disposition"],
                                                             found["grade"]))
        facts["with-run-id"] = ctl
        self.assertEqual("not_reproduced", cases.judge_runid(facts)[0]["disposition"])
        facts["control-without-run-id"] = dict(ctl, accepted_printed=False)
        self.assertEqual("blocked", cases.judge_runid(facts)[0]["disposition"])

    def test_failed_stream_controls_block_instead_of_reporting(self):
        facts = self.stream_facts(True)
        facts["control-cli-terminal-event"] = stream("supervisor_kill", exit=None)
        self.assertEqual(["blocked"], [r["disposition"] for r in cases.judge_stream(facts)])

    def test_eval_gate_empty_update_is_a_defect_only_when_controls_pass(self):
        ok = {"requests": 1, "body_keys": ["eval_gate"], "eval_gate": {
            "set_id": cases.G, "max_age_seconds": 3600}, "guard_message": False}
        none = {"requests": 0, "guard_message": True}
        empty = {"requests": 1, "body_keys": [], "eval_gate": None, "guard_message": False,
                 "idempotency_key_sent": True}
        probes = ("set-only-preview", "age-only-preview", "set-only-update", "age-only-update")
        facts = dict({"control-both-preview": ok, "control-no-fields": none,
                      "control-both-update": ok}, **{k: empty for k in probes})
        found = cases.judge_gate(facts)[0]
        self.assertEqual("reproduced_client_defect", found["disposition"])
        self.assertIn("Idempotency-Key: set-only-update, age-only-update", found["summary"])
        only_age_update = dict(facts, **{k: none for k in probes[:3]})
        found = cases.judge_gate(only_age_update)[0]
        self.assertEqual("reproduced_client_defect", found["disposition"])
        self.assertIn("(age-only-update)", found["summary"])
        refused = dict(facts, **{k: none for k in probes})
        self.assertEqual("contract_met", cases.judge_gate(refused)[0]["disposition"])
        self.assertEqual("blocked", cases.judge_gate(
            dict(facts, **{"control-both-update": empty}))[0]["disposition"])
        facts["control-no-fields"] = empty
        self.assertEqual("blocked", cases.judge_gate(facts)[0]["disposition"])

    def test_inferred_expectations_are_never_reported_as_defects(self):
        saved = {"authorization": "Bearer " + tier0.SAVED_TOKEN, "refresh_requests": 0}
        key = {"authorization": "Bearer " + tier0.DUMMY_KEY}
        bad = {"requests": 0, "exit": 101, "panicked": True, "error": "panicked"}
        facts = {"control-saved-sign-in": saved, "control-api-key": key,
                 "empty-key-with-saved-sign-in": {"authorization": "Bearer "},
                 "malformed-key-managed": bad, "malformed-key-cloud-status": bad,
                 "malformed-saved-token-managed": bad,
                 "malformed-key-stream-raw": {"requests": 0, "exit": 1, "panicked": False,
                                              "error": "Invalid bearer token"}}
        found = cases.judge_auth(facts)
        self.assertEqual({"contract_question"}, {r["disposition"] for r in found})
        self.assertEqual({"inferred"}, {r["grade"] for r in found})

    def test_cloud_error_breadth_observed_fixed_and_blocked(self):
        shown = {"requests": 1, "exit": 1, "message": True, "request_id": True}
        lost = dict(shown, message=False)
        facts = {}
        for code in (404, 500, 403):
            facts["managed-get-%d" % code] = shown
            facts["cloud-status-%d" % code] = lost
            facts["run-start-%d" % code] = lost if code == 404 else shown
            for name in cases.BREADTH:
                facts["%s-%d" % (name, code)] = lost if name != "run-get" else shown
        detail, breadth = cases.judge_errors(facts)
        self.assertEqual("observed_behavior", detail["disposition"])
        self.assertEqual(("observed_behavior", "unspecified"),
                         (breadth["disposition"], breadth["grade"]))
        self.assertIn("runs-list for HTTP [404, 500, 403]", breadth["summary"])
        self.assertNotIn("run-get for", breadth["summary"])
        self.assertIn("run start (anyhow_from_seren_error) omitted it for HTTP [404]",
                      breadth["summary"])
        fixed = dict(facts, **{k: shown for k in facts if k.startswith(tuple(cases.BREADTH))})
        self.assertEqual("not_reproduced", cases.judge_errors(fixed)[1]["disposition"])
        unsent = dict(facts, **{"run-get-500": dict(shown, requests=0)})
        self.assertEqual("blocked", cases.judge_errors(unsent)[1]["disposition"])
        self.assertEqual("blocked", cases.judge_errors(
            dict(facts, **{"managed-get-403": lost}))[0]["disposition"])

    def test_follow_on_judges_block_on_failed_controls(self):
        fail = {"exit": 0, "sends": 0, "lookups": 0, "api_requests": 0,
                "refresh_requests": 1, "authorization": None}
        self.assertEqual("blocked", cases.judge_undocumented(
            {"managed-get-404-control": fail, "cloud-status-404-control": fail})[0][
                "disposition"])
        self.assertEqual("blocked", cases.judge_retry({"control-rejected-4xx": fail})[0][
            "disposition"])
        self.assertEqual("blocked", cases.judge_refresh({"control-fresh-token": fail})[0][
            "disposition"])

    def test_delete_json_output_defect_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "stdout_json": False, "json_status": None,
                         "says_deleted": True, "status_shown": False}, **kw)
        facts = {"control-start-json": f(stdout_json=True, json_status="starting"),
                 "control-delete-404": f(exit=1), "control-delete-table": f(),
                 "delete-json": f(), "delete-not-converged": f()}
        by_part = {r["part"]: r for r in cases.judge_delete(facts)}
        self.assertEqual(("reproduced_client_defect", "stated"), (
            by_part["delete-json-output"]["disposition"], by_part["delete-json-output"]["grade"]))
        self.assertEqual(("contract_question", "inferred"), (
            by_part["delete-not-converged"]["disposition"],
            by_part["delete-not-converged"]["grade"]))
        fixed = dict(facts, **{"delete-json": f(stdout_json=True, says_deleted=False),
                               "delete-not-converged": f(status_shown=True)})
        self.assertEqual(["contract_met", "not_reproduced"],
                         [r["disposition"] for r in cases.judge_delete(fixed)])
        facts["control-start-json"] = f()
        self.assertEqual(["blocked"], [r["disposition"] for r in cases.judge_delete(facts)])

    def test_mutation_outcomes_questions_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"sends": 1, "lookups": 0, "kept_polling": False,
                         "lookup_uses_key": False, "killed": False, "exit": 0,
                         "printed_state": "applied", "deferral_shown": False,
                         "output_empty": False, "error": ""}, **kw)
        facts = {"control-applied": f(),
                 "control-pending-then-applied": f(lookups=1, lookup_uses_key=True),
                 "failed": f(exit=1, printed_state=None,
                             error="Error: x request <id> ended as failed: runtime_rejected"),
                 "superseded": f(exit=1, printed_state=None, error="Error: x request <id> "
                                 "ended as superseded: revision_superseded"),
                 "deferred-pending": f(lookups=7, kept_polling=True, killed=True, exit=None,
                                       printed_state=None, output_empty=True)}
        by_part = {r["part"]: r for r in cases.judge_outcomes(facts)}
        self.assertEqual("contract_met", by_part["terminal-states"]["disposition"])
        self.assertEqual(("contract_question", "inferred"), (
            by_part["deferred-pending"]["disposition"], by_part["deferred-pending"]["grade"]))
        self.assertNotIn("reproduced_client_defect", {r["disposition"] for r in by_part.values()})
        changed = dict(facts, failed=f(), **{"deferred-pending": f(
            exit=0, deferral_shown=True, lookups=0)})
        self.assertEqual(["contract_question", "not_reproduced"],
                         [r["disposition"] for r in cases.judge_outcomes(changed)])
        facts["control-pending-then-applied"] = f(lookups=1, lookup_uses_key=False)
        self.assertEqual(["blocked"], [r["disposition"] for r in cases.judge_outcomes(facts)])

    def test_auth_diagnostics_questions_fix_detection_and_blocked_controls(self):
        saved, key = "Bearer " + tier0.SAVED_TOKEN, "Bearer " + tier0.DUMMY_KEY

        def f(**kw):
            return dict({"requests": 1, "authorization": saved, "exit": 1,
                         "authenticated": False, "not_authenticated": False,
                         "auth_type": None, "names_override": False,
                         "error": "Error: 401 tier0 token rejected"}, **kw)
        oauth = f(requests=0, authorization=None, exit=0, authenticated=True,
                  auth_type="OAuth", error="")
        facts = {"control-status-saved": oauth,
                 "control-status-none": f(requests=0, authorization=None, exit=0,
                                          not_authenticated=True, error=""),
                 "control-me-saved": f(), "control-me-key": f(authorization=key),
                 "control-get-saved": f(),
                 "status-empty-key-saved": oauth,
                 "me-empty-key-saved": f(authorization="Bearer "),
                 "get-empty-key-saved": f(authorization="Bearer "),
                 "status-key-only": f(requests=0, authorization=None, exit=0,
                                      not_authenticated=True, error=""),
                 "status-key-with-saved": oauth}
        found = cases.judge_diagnostics(facts)
        self.assertEqual([("contract_question", "inferred")] * 2,
                         [(r["disposition"], r["grade"]) for r in found])
        self.assertTrue(all(r["impact"].startswith("hypothetical") for r in found))
        named = dict(facts, **{"me-empty-key-saved": f(authorization="Bearer ",
                                                       names_override=True)})
        self.assertEqual("not_reproduced", cases.judge_diagnostics(named)[0]["disposition"])
        unset = dict(facts, **{"me-empty-key-saved": f(), "get-empty-key-saved": f()})
        self.assertEqual("not_reproduced", cases.judge_diagnostics(unset)[0]["disposition"])
        api_key = dict(facts, **{"status-key-only": dict(oauth, auth_type="API Key"),
                                 "status-key-with-saved": dict(oauth, auth_type="API Key")})
        self.assertEqual("not_reproduced", cases.judge_diagnostics(api_key)[1]["disposition"])
        facts["control-status-none"] = oauth
        self.assertEqual(["blocked"], [r["disposition"]
                                       for r in cases.judge_diagnostics(facts)])

    def test_resources_decode_observation_and_blocked_control(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "stdout_json": False,
                         "json_compaction": False, "names_field": False,
                         "decode_error": False, "request_id_shown": False}, **kw)
        facts = {"control-404": f(exit=1),
                 "spec-minimal-json": f(stdout_json=True, json_compaction=True),
                 "spec-minimal-table": f(),
                 "missing-compaction": f(exit=1, names_field=True, decode_error=True)}
        found = cases.judge_resources(facts)
        self.assertEqual([("contract_met", "stated"), ("observed_behavior", "unspecified")],
                         [(r["disposition"], r["grade"]) for r in found])
        self.assertNotIn("reproduced_client_defect", {r["disposition"] for r in found})
        # A spec reply that fails to decode is a question about the fixture, never a defect.
        broken = dict(facts, **{"spec-minimal-json": f(exit=1, decode_error=True)})
        self.assertEqual("contract_question", cases.judge_resources(broken)[0]["disposition"])
        lenient = dict(facts, **{"missing-compaction": f()})
        self.assertEqual("not_reproduced", cases.judge_resources(lenient)[1]["disposition"])
        facts["control-404"] = f()
        self.assertEqual(["blocked"], [r["disposition"] for r in cases.judge_resources(facts)])

    def test_activity_query_decode_observation_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "stdout_json": False,
                         "json_stop_reason": False, "json_status_message": False,
                         "query_limit": "20", "query_offset": "0", "run_row": False,
                         "stop_reason_shown": False, "status_message_shown": False}, **kw)
        good = f(stdout_json=True, json_stop_reason=True, json_status_message=True,
                 query_limit="50")
        facts = {"control-404": f(exit=1), "control-table": f(run_row=True),
                 "activity-json": good}
        found = cases.judge_activity(facts)
        self.assertEqual([("contract_met", "stated"), ("observed_behavior", "unspecified")],
                         [(r["disposition"], r["grade"]) for r in found])
        self.assertIn("not why", found[1]["impact"])
        # --limit not forwarded is a defect against the stated flag; an undecodable reply
        # is a question about the fixture.
        ignored = dict(facts, **{"activity-json": dict(good, query_limit="20")})
        self.assertEqual("reproduced_client_defect",
                         cases.judge_activity(ignored)[0]["disposition"])
        broken = dict(facts, **{"activity-json": f(exit=1, query_limit="50")})
        self.assertEqual("contract_question", cases.judge_activity(broken)[0]["disposition"])
        shown = dict(facts, **{"control-table": f(run_row=True, stop_reason_shown=True)})
        self.assertNotIn("not why", cases.judge_activity(shown)[1]["impact"])
        for name, bad in (("control-404", f()), ("control-table", f())):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_activity(dict(facts, **{name: bad}))])

    def test_run_start_output_defect_question_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "stdout_json": False, "json_run_id": False,
                         "accepted_printed": True, "answer_shown": False}, **kw)
        as_json = f(stdout_json=True, json_run_id=True, accepted_printed=False)
        facts = {"control-managed-start-json": f(stdout_json=True, accepted_printed=False),
                 "control-async-table": f(), "control-404": f(exit=1, accepted_printed=False),
                 "control-sync-without-ids": f(accepted_printed=False, answer_shown=True),
                 "async-json": f(), "sync-json": f(), "sync-table-result": f()}
        found = cases.judge_start_output(facts)
        self.assertEqual([("reproduced_client_defect", "stated"),
                          ("contract_question", "inferred")],
                         [(r["disposition"], r["grade"]) for r in found])
        self.assertTrue(all(r["impact"].startswith("hypothetical") for r in found))
        fixed = dict(facts, **{"async-json": as_json, "sync-json": as_json,
                               "sync-table-result": f(answer_shown=True)})
        self.assertEqual(["contract_met", "not_reproduced"],
                         [r["disposition"] for r in cases.judge_start_output(fixed)])
        partial = dict(facts, **{"async-json": as_json})
        self.assertEqual("contract_question",
                         cases.judge_start_output(partial)[0]["disposition"])
        facts["control-sync-without-ids"] = f(accepted_printed=False)
        self.assertEqual(["blocked"],
                         [r["disposition"] for r in cases.judge_start_output(facts)])

    def test_json_sweep_defects_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "stdout_json": False, "human_line": True,
                         "env_name_shown": False, "clear_alert_sent": False}, **kw)
        facts = {"control-managed-start-json": f(stdout_json=True, human_line=False),
                 "control-destroy-404": f(exit=1, human_line=False),
                 "control-update-table": f(clear_alert_sent=True),
                 "control-environments-table": f(human_line=False, env_name_shown=True),
                 "destroy-json": f(), "update-config-json": f(clear_alert_sent=True),
                 "environment-list-json": f(human_line=False, env_name_shown=True)}
        found = cases.judge_json_sweep(facts)
        self.assertEqual(["destroy-json-output", "update-config-json-output",
                          "environment-list-json-output"], [r["part"] for r in found])
        self.assertEqual([("reproduced_client_defect", "stated")] * 3,
                         [(r["disposition"], r["grade"]) for r in found])
        self.assertTrue(all(r["impact"].startswith("hypothetical") for r in found))
        # A future binary that prints JSON for one command is detected as fixed for it only.
        fixed = dict(facts, **{"destroy-json": f(stdout_json=True, human_line=False)})
        self.assertEqual(["contract_met", "reproduced_client_defect",
                          "reproduced_client_defect"],
                         [r["disposition"] for r in cases.judge_json_sweep(fixed)])
        failed = dict(facts, **{"environment-list-json": f(exit=1, human_line=False)})
        self.assertEqual("not_reproduced", cases.judge_json_sweep(failed)[2]["disposition"])
        for name, broken in (("control-managed-start-json", f()),
                             ("control-update-table", f()),
                             ("control-environments-table", f(stdout_json=True,
                                                              env_name_shown=True))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_json_sweep(dict(facts, **{name: broken}))])

    def test_runs_list_filter_mapping_defect_question_and_blocked_controls(self):
        defaults = {"limit": "50", "offset": "0"}

        def f(**kw):
            return dict({"requests": 1, "exit": 0, "stdout_json": True, "runs_requests": 1,
                         "deployment_list_requests": 0, "query": dict(cases.FILTER_QUERY),
                         "statuses": list(cases.FILTER_STATUSES), "no_runs_line": False}, **kw)
        facts = {"control-404": f(exit=1, stdout_json=False, query={}, statuses=[]),
                 "control-scoped-defaults": f(stdout_json=False, query=defaults, statuses=[],
                                              no_runs_line=True),
                 "control-org-defaults": f(requests=2, deployment_list_requests=1,
                                           stdout_json=False, query=defaults, statuses=[],
                                           no_runs_line=True),
                 "scoped-filters": f(), "org-filters": f(requests=2,
                                                         deployment_list_requests=1)}
        found = cases.judge_runs_list(facts)
        self.assertEqual([("scoped-filters", "contract_met", "stated"),
                          ("org-filters", "contract_met", "stated")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        # A swapped pair of filters on one route is a defect on that route only.
        swapped = dict(cases.FILTER_QUERY, source="daytona", compute_backend="scheduler")
        bad = cases.judge_runs_list(dict(facts, **{"org-filters": f(query=swapped)}))
        self.assertEqual(["contract_met", "reproduced_client_defect"],
                         [r["disposition"] for r in bad])
        self.assertIn("source=daytona", bad[1]["summary"])
        self.assertTrue(bad[1]["impact"].startswith("hypothetical"))
        dropped = cases.judge_runs_list(dict(facts, **{"scoped-filters": f(statuses=["failed"])}))
        self.assertEqual("reproduced_client_defect", dropped[0]["disposition"])
        extra = dict(cases.FILTER_QUERY, conversation_id="x")
        self.assertEqual("reproduced_client_defect", cases.judge_runs_list(
            dict(facts, **{"scoped-filters": f(query=extra)}))[0]["disposition"])
        failed = cases.judge_runs_list(dict(facts, **{"scoped-filters": f(exit=1)}))
        self.assertEqual("contract_question", failed[0]["disposition"])
        for name, broken in (("control-404", f(stdout_json=False)),
                             ("control-scoped-defaults", f(no_runs_line=True)),
                             ("control-org-defaults", f(runs_requests=0, query=defaults,
                                                        statuses=[], no_runs_line=True))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_runs_list(dict(facts, **{name: broken}))])

    def test_run_approval_decision_set_defect_question_observation_and_blocked(self):
        def f(decision="approve", **kw):
            return dict({"requests": 3, "exit": 0, "resume_requests": 1,
                         "checkpoint": cases.CHECKPOINT,
                         "decisions": sorted([i, decision] for i in cases.APPROVAL_IDS),
                         "resolved": True, "done_line": False, "not_awaiting_message": False,
                         "mismatch_message": False}, **kw)
        facts = {"control-404": f(requests=1, exit=1, resume_requests=0, checkpoint=None,
                                  decisions=None, resolved=None),
                 "control-not-awaiting": f(requests=2, resume_requests=0, checkpoint=None,
                                           decisions=None, resolved=False,
                                           not_awaiting_message=True),
                 "approve-all": f(),
                 "reject-all": f("reject", resolved=None, done_line=True),
                 "identity-mismatch": f(exit=1, resolved=None, mismatch_message=True)}
        found = cases.judge_approvals(facts)
        self.assertEqual([("approve-all", "contract_met", "stated"),
                          ("reject-all", "contract_met", "stated"),
                          ("identity-mismatch", "observed_behavior", "unspecified")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertTrue(found[2]["impact"].startswith("hypothetical"))
        # Only one of two pending approvals decided, or the wrong decision: defect.
        partial = f(decisions=[[cases.APPROVAL_IDS[0], "approve"]])
        bad = cases.judge_approvals(dict(facts, **{"approve-all": partial}))
        self.assertEqual("reproduced_client_defect", bad[0]["disposition"])
        self.assertTrue(bad[0]["impact"].startswith("hypothetical"))
        wrong = cases.judge_approvals(dict(facts, **{"reject-all": f("approve")}))
        self.assertEqual("reproduced_client_defect", wrong[1]["disposition"])
        stale = cases.judge_approvals(dict(facts, **{"approve-all": f(checkpoint="other")}))
        self.assertEqual("reproduced_client_defect", stale[0]["disposition"])
        failed = cases.judge_approvals(dict(facts, **{"approve-all": f(exit=1)}))
        self.assertEqual("contract_question", failed[0]["disposition"])
        quiet = cases.judge_approvals(dict(facts, **{"identity-mismatch": f()}))
        self.assertIsNone(quiet[2]["impact"])
        for name, broken in (("control-404", f(exit=1)),
                             ("control-not-awaiting", f(resolved=False,
                                                        not_awaiting_message=True))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_approvals(dict(facts, **{name: broken}))])

    def test_run_events_filter_mapping_defect_question_and_blocked_controls(self):
        defaults = {"limit": "100", "offset": "0"}

        def f(**kw):
            return dict({"requests": 1, "exit": 0, "stdout_json": True, "events_requests": 1,
                         "query": dict(cases.EVENT_QUERY)}, **kw)
        facts = {"control-404": f(exit=1, stdout_json=False, query={}),
                 "control-scoped-defaults": f(query=defaults),
                 "control-org-defaults": f(query=defaults),
                 "scoped-filters": f(), "org-filters": f()}
        found = cases.judge_run_events(facts)
        self.assertEqual([("scoped-filters", "contract_met", "stated"),
                          ("org-filters", "contract_met", "stated")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        # kind and item_id swapped on one route: a defect on that route only.
        swapped = dict(cases.EVENT_QUERY, kind="tier0-item-1", item_id="tool_call_completed")
        bad = cases.judge_run_events(dict(facts, **{"org-filters": f(query=swapped)}))
        self.assertEqual(["contract_met", "reproduced_client_defect"],
                         [r["disposition"] for r in bad])
        self.assertIn("kind=tier0-item-1", bad[1]["summary"])
        self.assertTrue(bad[1]["impact"].startswith("hypothetical"))
        dropped = {k: v for k, v in cases.EVENT_QUERY.items() if k != "q"}
        self.assertEqual("reproduced_client_defect", cases.judge_run_events(
            dict(facts, **{"scoped-filters": f(query=dropped)}))[0]["disposition"])
        failed = cases.judge_run_events(dict(facts, **{"scoped-filters": f(exit=1)}))
        self.assertEqual("contract_question", failed[0]["disposition"])
        for name, broken in (("control-404", f(exit=0)),
                             ("control-scoped-defaults", f()),
                             ("control-org-defaults", f(query=defaults, events_requests=0))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_run_events(dict(facts, **{name: broken}))])

    def test_failed_sync_run_question_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "accepted_printed": True,
                         "failure_shown": False}, **kw)
        facts = {"control-404": f(exit=1, accepted_printed=False),
                 "control-sync-completed": f(),
                 "control-failed-without-ids": f(accepted_printed=False, failure_shown=True),
                 "sync-failed": f()}
        found = cases.judge_failed_sync(facts)
        self.assertEqual([("sync-failed-hidden", "contract_question", "inferred")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertTrue(found[0]["impact"].startswith("hypothetical"))
        for fixed in (f(failure_shown=True), f(exit=1)):
            self.assertEqual(["contract_met"], [r["disposition"] for r in
                             cases.judge_failed_sync(dict(facts, **{"sync-failed": fixed}))])
        for name, broken in (("control-404", f()),
                             ("control-sync-completed", f(failure_shown=True)),
                             ("control-failed-without-ids", f(accepted_printed=False))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_failed_sync(dict(facts, **{name: broken}))])

    def test_poll_hint_defect_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "hint": "seren agent get <id>",
                         "status_shown": True, "usage_error": False}, **kw)
        facts = {"control-start-converged": f(hint=None, status_shown=False),
                 "control-managed-get-404": f(exit=1, hint=None),
                 "managed-start-pending": f(), "managed-stop-pending": f(),
                 "follow-hint": f(exit=2, requests=0, hint=None, usage_error=True)}
        found = cases.judge_hint(facts)
        self.assertEqual([("poll-hint-command", "reproduced_client_defect", "stated")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        fixed = {p: f(hint="seren agent managed-get <id>") for p in
                 ("managed-start-pending", "managed-stop-pending")}
        for change in (fixed, {"follow-hint": f(exit=1, hint=None)}):
            self.assertEqual(["contract_met"], [r["disposition"] for r in
                                                cases.judge_hint(dict(facts, **change))])
        for name, broken in (("control-start-converged", f()),
                             ("control-managed-get-404", f(exit=2, requests=0,
                                                           usage_error=True)),
                             ("managed-stop-pending", f(exit=1))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_hint(dict(facts, **{name: broken}))])

    def test_cloud_route_and_hint_defect_fix_detection_and_blocked_controls(self):
        cloud = "/publishers/seren-cloud/deployments/<id>/"
        def f(**kw):
            return dict({"requests": 2, "exit": 0, "hint": "seren agent get <id>",
                         "status_shown": True, "usage_error": False,
                         "mutations": [cloud + "start"], "ambiguous_refusal": False}, **kw)
        facts = {"control-lookup-404": f(requests=1, exit=1, hint=None, mutations=[]),
                 "control-start-converged": f(hint=None, status_shown=False),
                 "cloud-start-pending": f(), "cloud-stop-pending": f(mutations=[cloud + "stop"]),
                 "ambiguous-start": f(requests=1, exit=1, hint=None, mutations=[],
                                      ambiguous_refusal=True),
                 "follow-hint": f(requests=0, exit=2, hint=None, mutations=[],
                                  usage_error=True)}
        found = cases.judge_cloud_route(facts)
        self.assertEqual([("lifecycle-route", "contract_met", "stated"),
                          ("cloud-poll-hint", "reproduced_client_defect", "stated")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        fixed = {p: f(hint="seren agent cloud deployment status <id>", mutations=m)
                 for p, m in (("cloud-start-pending", [cloud + "start"]),
                              ("cloud-stop-pending", [cloud + "stop"]))}
        self.assertEqual("contract_met", cases.judge_cloud_route(dict(facts, **fixed))[1][
            "disposition"])
        for change in ({"cloud-start-pending": f(mutations=[
                            "/publishers/seren-agent/deployments/<id>/start"])},
                       {"ambiguous-start": f(requests=2, hint=None)},
                       {"ambiguous-start": f(requests=1, exit=1, hint=None, mutations=[])}):
            self.assertEqual("reproduced_client_defect", cases.judge_cloud_route(
                dict(facts, **change))[0]["disposition"])
        for name, broken in (("control-lookup-404", f(exit=1)),
                             ("control-start-converged", f()),
                             ("control-start-converged", f(hint=None, mutations=[]))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_cloud_route(dict(facts, **{name: broken}))])

    def test_env_file_questions_fix_detection_and_blocked_controls(self):
        def f(secrets, **kw):
            return dict({"requests": 1, "exit": 1, "secrets": secrets,
                         "idempotency_key_sent": False, "stderr_lines": 1}, **kw)
        exported = {"export TOKEN": "tier0-token"}
        facts = {"control-plain": f(dict(cases.PLAIN_SECRETS)),
                 "export-prefix": f(exported),
                 "inline-comment": f({"TOKEN": "tier0-token # rotated"}),
                 "multiline-value": f({"PRIVATE_KEY": "tier0-line-1"}),
                 "unparsed-line": f({"OTHER": "tier0-other"}),
                 "empty-file": f({}),
                 "export-prefix-update": f(exported, idempotency_key_sent=True)}
        found = cases.judge_env(facts)
        self.assertEqual([("env-syntax", "contract_question", "inferred"),
                          ("unparsed-line", "contract_question", "inferred"),
                          ("empty-file", "observed_behavior", "unspecified")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertIn("multiline-value", found[0]["summary"])
        self.assertTrue(found[0]["impact"].startswith("hypothetical"))
        # A fixed parser, a refused file or a warning each meet the inferred reading.
        fixed = {p: f(dict(v)) for p, v in cases.DOTENV_READING.items()}
        refused = {p: f(None, requests=0) for p in cases.DOTENV_READING}
        warned = {p: f(facts[p]["secrets"], stderr_lines=2) for p in cases.DOTENV_READING}
        for change in (fixed, refused, warned):
            self.assertEqual("contract_met", cases.judge_env(dict(facts, **change))[0][
                "disposition"])
        partial = cases.judge_env(dict(facts, **{"export-prefix": f({"TOKEN": "tier0-token"})}))
        self.assertEqual("contract_question", partial[0]["disposition"])
        self.assertNotIn("export-prefix ->", partial[0]["summary"])
        kept = f({"TOKEN": "tier0-token", "OTHER": "tier0-other"})
        self.assertEqual("contract_met", cases.judge_env(dict(facts, **{"unparsed-line": kept}))[
            1]["disposition"])
        for broken in (f({"ALPHA": "tier0-alpha"}), f(None, requests=0)):
            self.assertEqual(["blocked"], [r["disposition"] for r in cases.judge_env(
                dict(facts, **{"control-plain": broken}))])

    def test_run_detail_output_question_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "json_output": False, "status_shown": True,
                         "status_message_shown": True, "stop_reason_shown": True,
                         "output_shown": False}, **kw)
        facts = {"control-404": f(exit=1, status_shown=False),
                 "control-json": f(json_output=True, output_shown=True),
                 "table-scoped": f(), "table-global": f()}
        found = cases.judge_run_detail(facts)
        self.assertEqual([("table-output", "contract_question", "inferred")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertTrue(found[0]["impact"].startswith("hypothetical"))
        fixed = {p: f(output_shown=True) for p in ("table-scoped", "table-global")}
        self.assertEqual(["contract_met"], [r["disposition"] for r in
                                            cases.judge_run_detail(dict(facts, **fixed))])
        one_route = cases.judge_run_detail(dict(facts, **{"table-scoped": f(output_shown=True)}))
        self.assertEqual("contract_question", one_route[0]["disposition"])
        for name, broken in (("control-404", f()), ("control-json", f()),
                             ("table-global", f(status_shown=False)),
                             ("table-scoped", f(exit=1))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_run_detail(dict(facts, **{name: broken}))])

    def test_run_limits_gap_defect_and_blocked_controls(self):
        def f(limits=None, requests=1, **kw):
            return dict({"requests": requests, "exit": 1, "limits": limits,
                         "body_keys": [] if requests else None, "refused": False,
                         "error": ""}, **kw)
        facts = {"control-name-only": f(body_keys=["name"]),
                 "control-unknown-key": f(requests=0, refused=True),
                 "set-limits": f(dict(cases.SET_LIMITS)),
                 "clear-limit": f({"clear_max_iterations": True}),
                 "tool-call-limit": f(requests=0, refused=True)}
        found = cases.judge_limits(facts)
        self.assertEqual([("limits-patch", "contract_met", "stated"),
                          ("tool-call-limit", "capability_gap", "stated")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        fixed = cases.judge_limits(dict(facts, **{
            "tool-call-limit": f({"max_tool_calls_per_run": 10})}))
        self.assertEqual("contract_met", fixed[1]["disposition"])
        for name, wrong in (("set-limits", f({"max_iterations": 5})),
                            ("clear-limit", f(None))):
            out = cases.judge_limits(dict(facts, **{name: wrong}))
            self.assertEqual("reproduced_client_defect", out[0]["disposition"])
            self.assertTrue(out[0]["impact"].startswith("hypothetical"))
        dropped = cases.judge_limits(dict(facts, **{"tool-call-limit": f(None)}))
        self.assertEqual("observed_behavior", dropped[1]["disposition"])
        for name, broken in (("control-name-only", f(body_keys=["name"], limits={"x": 1})),
                             ("control-unknown-key", f())):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_limits(dict(facts, **{name: broken}))])

    def test_run_list_cost_question_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "run_shown": True, "compute_shown": True,
                         "inference_shown": False, "total_shown": False}, **kw)
        facts = {"control-404": f(exit=1, run_shown=False, compute_shown=False),
                 "control-json": f(inference_shown=True),
                 "control-detail": f(inference_shown=True),
                 "list-scoped": f(), "list-org": f(requests=2)}
        found = cases.judge_costs(facts)
        self.assertEqual([("list-cost", "contract_question", "inferred")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertTrue(found[0]["impact"].startswith("hypothetical"))
        for fixed in (f(total_shown=True, compute_shown=False), f(inference_shown=True)):
            out = cases.judge_costs(dict(facts, **{"list-scoped": fixed, "list-org": fixed}))
            self.assertEqual(["contract_met"], [r["disposition"] for r in out])
        one_route = cases.judge_costs(dict(facts, **{"list-org": f(total_shown=True)}))
        self.assertEqual("contract_question", one_route[0]["disposition"])
        for name, broken in (("control-404", f()), ("control-json", f()),
                             ("control-detail", f()), ("list-org", f(run_shown=False)),
                             ("list-scoped", f(exit=1))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_costs(dict(facts, **{name: broken}))])

    def test_rollback_contract_met_defects_question_and_blocked_controls(self):
        guarded = {"revision_id": cases.PRIOR, "expected_active_revision_id": cases.R,
                   "secret_resolution_result_id": cases.SECRET_RESULT}

        def f(**kw):
            return dict({"sends": 1, "lookups": 0, "exit": 0, "body": guarded,
                         "key_sent": True, "one_key_throughout": True,
                         "printed_state": "applied", "conflict_shown": False, "error": ""}, **kw)
        facts = {"control-404": f(exit=1, printed_state=None,
                                  body={"revision_id": cases.PRIOR}),
                 "control-applied": f(body={"revision_id": cases.PRIOR}),
                 "preconditions": f(),
                 "conflict-409": f(exit=1, printed_state=None, conflict_shown=True),
                 "ambiguous-recovered": f(lookups=1)}
        met = cases.judge_rollback(facts)
        self.assertEqual([("rollback-request", "contract_met", "stated"),
                          ("revision-conflict", "contract_met", "inferred"),
                          ("ambiguous-recovery", "contract_met", "stated")],
                         [(r["part"], r["disposition"], r["grade"]) for r in met])
        dropped = cases.judge_rollback(dict(facts, preconditions=f(
            body={"revision_id": cases.PRIOR})))
        self.assertEqual("reproduced_client_defect", dropped[0]["disposition"])
        for conflict in (f(exit=0, printed_state=None, conflict_shown=True),
                         f(sends=2, exit=1, conflict_shown=True),
                         f(exit=1, printed_state=None)):
            out = cases.judge_rollback(dict(facts, **{"conflict-409": conflict}))
            self.assertEqual("contract_question", out[1]["disposition"])
        rekeyed = cases.judge_rollback(dict(facts, **{"ambiguous-recovered": f(
            sends=2, lookups=1, one_key_throughout=False)}))
        self.assertEqual("reproduced_client_defect", rekeyed[2]["disposition"])
        for name, broken in (("control-404", f(exit=0)),
                             ("control-applied", f()),
                             ("control-applied", f(body={"revision_id": cases.PRIOR},
                                                   key_sent=False))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_rollback(dict(facts, **{name: broken}))])

    def test_rollback_facts_drop_nulls_and_track_one_identity(self):
        key = "7a000012-0000-4000-8000-000000000013"
        send = {"method": "POST", "path": cases.ROLLBACK, "headers": {"idempotency-key": key},
                "body": {"revision_id": cases.PRIOR, "expected_active_revision_id": None,
                         "secret_resolution_result_id": None}}
        lookup = {"method": "GET", "path": cases.AGENT + "/managed/actions/" + key,
                  "headers": {}, "body": None}
        obs = {"requests": [send, lookup], "exit": 0, "stderr": "",
               "stdout": json.dumps(cases._mutation("applied")["json"])}
        fact = cases.rollback_facts(obs)
        self.assertEqual({"revision_id": cases.PRIOR}, fact["body"])
        self.assertEqual((1, 1, True, "applied"), (fact["sends"], fact["lookups"],
                                                   fact["one_key_throughout"],
                                                   fact["printed_state"]))
        lookup["path"] = cases.AGENT + "/managed/actions/" + cases.R
        self.assertFalse(cases.rollback_facts(obs)["one_key_throughout"])

    def test_schedule_request_occurrence_question_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "body": {}, "schedule_shown": True,
                         "last_error_shown": False, "occurrence_error_shown": False,
                         "occurrence_status_shown": False, "occurrence_due_shown": False}, **kw)
        facts = {"control-404": f(exit=1, schedule_shown=False),
                 "control-json": f(occurrence_error_shown=True),
                 "control-last-error": f(last_error_shown=True),
                 "create-request": f(body=dict(cases.CREATE_FLAGS)),
                 "list-failed-occurrence": f(), "cancel-failed-occurrence": f()}
        found = cases.judge_schedules(facts)
        self.assertEqual([("create-request", "contract_met", "stated"),
                          ("occurrence-failure", "contract_question", "inferred")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertTrue(found[1]["impact"].startswith("hypothetical"))
        no_tz = {k: v for k, v in cases.CREATE_FLAGS.items() if k != "timezone"}
        dropped = cases.judge_schedules(dict(facts, **{"create-request": f(body=no_tz)}))
        self.assertEqual("reproduced_client_defect", dropped[0]["disposition"])
        shown = f(occurrence_error_shown=True)
        fixed = cases.judge_schedules(dict(facts, **{"list-failed-occurrence": shown,
                                                     "cancel-failed-occurrence": shown}))
        self.assertEqual("contract_met", fixed[1]["disposition"])
        one_view = cases.judge_schedules(dict(facts, **{"cancel-failed-occurrence": shown}))
        self.assertEqual("contract_question", one_view[1]["disposition"])
        for name, broken in (("control-404", f()), ("control-json", f()),
                             ("control-last-error", f()), ("create-request", f(exit=1)),
                             ("list-failed-occurrence", f(schedule_shown=False))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_schedules(dict(facts, **{name: broken}))])

    def test_conversation_paging_unavailable_question_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "cursor_sent": None, "question_row": True,
                         "assistant_row": True, "answer_shown": False,
                         "unavailable_shown": False, "next_cursor_shown": False}, **kw)
        facts = {"control-404": f(exit=1, question_row=False, assistant_row=False),
                 "control-content": f(answer_shown=True),
                 "control-json": f(unavailable_shown=True),
                 "unavailable-table": f(),
                 "paging": f(cursor_sent=cases.PAGE_CURSOR, next_cursor_shown=True)}
        found = cases.judge_conversations(facts)
        self.assertEqual([("paging", "contract_met", "inferred"),
                          ("unavailable-content", "contract_question", "inferred")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertTrue(found[1]["impact"].startswith("hypothetical"))
        dropped = cases.judge_conversations(dict(facts, paging=f(next_cursor_shown=True)))
        self.assertEqual(("reproduced_client_defect", "stated"),
                         (dropped[0]["disposition"], dropped[0]["grade"]))
        no_hint = cases.judge_conversations(dict(facts, paging=f(cursor_sent=cases.PAGE_CURSOR)))
        self.assertEqual("contract_question", no_hint[0]["disposition"])
        fixed = cases.judge_conversations(
            dict(facts, **{"unavailable-table": f(unavailable_shown=True)}))
        self.assertEqual("contract_met", fixed[1]["disposition"])
        for name, broken in (("control-404", f()), ("control-content", f()),
                             ("control-json", f()), ("unavailable-table", f(exit=1)),
                             ("paging", f(question_row=False))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_conversations(dict(facts, **{name: broken}))])

    def test_conversation_facts_read_cursor_and_markers(self):
        obs = {"requests": [{"method": "GET", "path": cases.MESSAGES,
                             "query": {"cursor": [cases.PAGE_CURSOR], "limit": ["50"]}}],
               "exit": 0, "stdout": "role=assistant\nrole=user content=tier0 question\n\n"
                                    "Next cursor: " + cases.NEXT_CURSOR + "\n"}
        fact = cases.conversation_facts(obs)
        self.assertEqual((cases.PAGE_CURSOR, True, True, False),
                         (fact["cursor_sent"], fact["next_cursor_shown"], fact["question_row"],
                          fact["unavailable_shown"]))

    def test_cancel_met_misreport_detection_question_and_blocked_controls(self):
        def f(**kw):
            return dict({"requests": 1, "exit": 0, "scoped_posts": 0, "org_posts": 0,
                         "printed_status": "running", "claims_cancelled": False}, **kw)
        facts = {"control-404": f(exit=1, scoped_posts=1, printed_status=None),
                 "control-cancelled": f(org_posts=1, printed_status="cancelled",
                                        claims_cancelled=True),
                 "scoped-requested": f(scoped_posts=1),
                 "org-requested": f(org_posts=1)}
        met = cases.judge_cancel(facts)
        self.assertEqual([("scoped-requested", "contract_met", "stated"),
                          ("org-requested", "contract_met", "stated")],
                         [(r["part"], r["disposition"], r["grade"]) for r in met])
        claimed = cases.judge_cancel(
            dict(facts, **{"scoped-requested": f(scoped_posts=1, claims_cancelled=True)}))
        self.assertEqual("reproduced_client_defect", claimed[0]["disposition"])
        self.assertTrue(claimed[0]["impact"].startswith("hypothetical"))
        wrong_route = cases.judge_cancel(dict(facts, **{"org-requested": f(scoped_posts=1)}))
        self.assertEqual("reproduced_client_defect", wrong_route[1]["disposition"])
        failed = cases.judge_cancel(dict(facts, **{"org-requested": f(exit=1, org_posts=1)}))
        self.assertEqual("contract_question", failed[1]["disposition"])
        for name, broken in (("control-404", f(exit=0)), ("control-404", f(exit=1, requests=2)),
                             ("control-cancelled", f(org_posts=1))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_cancel(dict(facts, **{name: broken}))])

    def test_cancel_facts_read_route_and_printed_status(self):
        obs = {"requests": [{"method": "POST", "path": cases.CANCEL_GLOBAL, "query": {}}],
               "exit": 0, "stdout": json.dumps({"data": {"status": "running"}}), "stderr": ""}
        fact = cases.cancel_facts(obs)
        self.assertEqual((1, 0, "running", False),
                         (fact["org_posts"], fact["scoped_posts"], fact["printed_status"],
                          fact["claims_cancelled"]))

    def test_task_follow_question_fix_detection_and_blocked_controls(self):
        def f(**kw):
            return dict({"ending": "self", "exit": 0, "requests": 1,
                         "status_line": "Task failed", "error_shown": True,
                         "cost_shown": True}, **kw)
        hidden = {"control-404": f(exit=1, status_line=None, error_shown=False),
                  "control-flat-failed": f(),
                  "spec-event-named": f(status_line="Task unknown", error_shown=False),
                  "spec-event-unnamed": f(ending="supervisor_kill", exit=None,
                                          status_line=None, error_shown=False)}
        found = cases.judge_tasks(hidden)
        self.assertEqual([("spec-event-shape", "contract_question", "inferred"),
                          ("failed-exit", "observed_behavior", "unspecified")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertTrue(found[0]["impact"].startswith("hypothetical"))
        fixed = cases.judge_tasks(dict(hidden, **{"spec-event-named": f(),
                                                  "spec-event-unnamed": f()}))
        self.assertEqual("contract_met", fixed[0]["disposition"])
        partly = cases.judge_tasks(dict(hidden, **{"spec-event-named": f()}))
        self.assertEqual("contract_question", partly[0]["disposition"])
        for name, broken in (("control-404", f(exit=0)), ("control-404", f(exit=1, requests=2)),
                             ("control-flat-failed", f(error_shown=False)),
                             ("control-flat-failed", f(ending="supervisor_kill"))):
            self.assertEqual(["blocked"], [r["disposition"] for r in
                                           cases.judge_tasks(dict(hidden, **{name: broken}))])

    def test_task_facts_strip_colour_and_read_status_and_error(self):
        obs = {"requests": [{}], "exit": 0, "killed_by_supervisor": False,
               "stderr": "Following task x... (Ctrl+C to stop)\n\x1b[1;31mTask failed\x1b[0m\n"
                         "\x1b[1;31mError\x1b[0m: " + cases.TASK_ERROR + "\n"}
        fact = cases.task_facts(obs)
        self.assertEqual(("self", "Task failed", True, False),
                         (fact["ending"], fact["status_line"], fact["error_shown"],
                          fact["cost_shown"]))

    def test_start_views_question_fix_detection_and_blocked_controls(self):
        def f(*shown, exit=0, requests=1, lines=()):
            return {"requests": requests, "exit": exit, "shown": sorted(shown),
                    "lifecycle_lines": list(lines)}
        row = ["│ Status ┆ building │"]
        hidden = {"control-get-json-failed": f("error", "failed", "ready_condition", "reason",
                                               "since"),
                  "control-get-table-running": f("running"),
                  "get-table-failed": f("failed"),
                  "get-table-stuck": f("building", lines=row),
                  "control-status-json-stuck": f("building", "since", "stopping"),
                  "control-status-table-before-stop": f("building", lines=row),
                  "status-table-after-stop": f("building", lines=row),
                  "status-table-failed": f("failed")}
        found = cases.judge_start_views(hidden)
        self.assertEqual([("failure-reason-default-view", "contract_question", "inferred"),
                          ("stuck-progress-default-view", "contract_question", "inferred")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertIn("output identical: True", found[1]["summary"])
        self.assertIn("-o json", found[0]["impact"])
        fixed = cases.judge_start_views(dict(hidden, **{
            "get-table-failed": f("error", "failed"),
            "status-table-failed": f("error", "failed"),
            "get-table-stuck": f("building", "ready_condition", "since"),
            "status-table-after-stop": f("building", "since", "stopping")}))
        self.assertEqual(["contract_met", "contract_met"],
                         [r["disposition"] for r in fixed])
        self.assertIsNone(fixed[0]["impact"])
        partly = cases.judge_start_views(dict(hidden, **{"get-table-failed": f("error",
                                                                               "failed")}))
        self.assertEqual("contract_question", partly[0]["disposition"])
        for name, broken in (("control-get-json-failed", f("failed", "since")),
                             ("control-get-table-running", f("running", exit=1)),
                             ("control-status-json-stuck", f("building")),
                             ("control-status-table-before-stop", f("building", requests=2)),
                             ("get-table-failed", f("failed", exit=1))):
            self.assertEqual(["blocked"], [r["disposition"] for r in cases.judge_start_views(
                dict(hidden, **{name: broken}))])

    def test_start_view_facts_find_markers_in_either_stream(self):
        obs = {"requests": [{}], "exit": 0,
               "stdout": "│ Status   ┆ building │\n",
               "stderr": "note: " + cases.START_ERROR + " since " + cases.SINCE + "\n"}
        fact = cases.start_view_facts(obs)
        self.assertEqual((["building", "error", "since"], ["│ Status ┆ building │"]),
                         (fact["shown"], fact["lifecycle_lines"]))

    def test_stop_views_observation_workaround_and_blocked_controls(self):
        def f(*shown, headline=None, note=None, hint=None, exit=0, requests=1):
            return {"requests": requests, "exit": exit, "headline": headline,
                    "status": None, "note": note, "hint": hint, "shown": sorted(shown)}
        pending = "not yet converged"
        hint = "seren agent get <id>"
        seen = {"control-stop-converged": f(headline="converged", note=cases.STOP_NOTE),
                "control-stop-pending-no-note": f("stopping", headline=pending, hint=hint),
                "stop-pending-stopped-note": f("stopping", headline=pending,
                                               note=cases.STOP_NOTE, hint=hint),
                "stop-pending-already-note": f("stopping", headline=pending,
                                               note=cases.ALREADY_NOTE, hint=hint),
                "stop-json-pending-note": f("confirmation", "stopping"),
                "control-status-json-running": f("running"),
                "status-json-failed-stopping": f("error", "failed", "since", "stopping"),
                "status-table-failed-stopping": f("failed")}
        found = cases.judge_stop_views(seen)
        self.assertEqual([("stop-note-while-unconverged", "observed_behavior", "unspecified"),
                          ("failed-stop-status-json", "contract_met", "inferred")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertIn("['stopped-note', 'already-note']", found[0]["summary"])
        self.assertIn("never stopped", " ".join(found[0]["basis"]))
        # A future CLI that withholds the note under a pending headline is still recorded.
        quiet = cases.judge_stop_views(dict(seen, **{
            "stop-pending-stopped-note": f("stopping", headline=pending, hint=hint),
            "stop-pending-already-note": f("stopping", headline=pending, hint=hint)}))
        self.assertEqual("observed_behavior", quiet[0]["disposition"])
        self.assertIn("not-converged headline: none", quiet[0]["summary"])
        hidden = cases.judge_stop_views(dict(seen, **{
            "status-json-failed-stopping": f("failed", "stopping")}))
        self.assertEqual("contract_question", hidden[1]["disposition"])
        for name, broken in (("control-stop-converged", f(headline=pending, note=cases.STOP_NOTE)),
                             ("control-stop-pending-no-note", f(headline=pending)),
                             ("control-status-json-running", f("error", "running")),
                             ("status-table-failed-stopping", f("failed", exit=1))):
            self.assertEqual(["blocked"], [r["disposition"] for r in cases.judge_stop_views(
                dict(seen, **{name: broken}))])

    def test_stop_view_facts_read_headline_note_hint_and_marks(self):
        out = ("Agent %s stop accepted; not yet converged (status: stopping).\n"
               "  note: %s\n  poll `seren agent get %s` until it reports stopped.\n"
               % (cases.D, cases.STOP_NOTE, cases.D))
        fact = cases.stop_view_facts({"requests": [{}], "exit": 0, "stdout": out, "stderr": ""})
        self.assertEqual(("not yet converged", "stopping", cases.STOP_NOTE,
                          "seren agent get <id>", ["stopping"]),
                         (fact["headline"], fact["status"], fact["note"], fact["hint"],
                          fact["shown"]))
        done = cases.stop_view_facts({"requests": [{}], "exit": 0, "stderr": "",
                                      "stdout": "Agent x converged (status: stopped).\n"})
        self.assertEqual(("converged", None, None), (done["headline"], done["note"],
                                                     done["hint"]))

    def test_answer_text_faithful_altered_and_blocked_controls(self):
        def obs(reply, exit=0):
            body = reply.get("body") or json.dumps(reply["json"])
            return {"requests": [{}], "exit": exit, "stdout": body, "stderr": ""}
        split = cases._completed_run(cases._trace_events(cases.ANSWER_PARTS))
        numbers = cases._completed_run(cases._trace_events(["ok"], cases.BIG_NUMBERS))
        seen = {"control-404": cases.answer_facts(dict(obs({"json": {}}, exit=1), stdout="")),
                "control-json": cases.answer_facts(obs(cases._completed_run(
                    cases._trace_events(["tier0 plain answer"])))),
                "split-answer": cases.answer_facts(obs(split)),
                "escaped-close": cases.answer_facts(obs(cases._escaped_run())),
                "event-numbers": cases.answer_facts(obs(numbers))}
        self.assertIn("\\u007d", cases._escaped_run()["body"])
        self.assertTrue(seen["escaped-close"]["answer_parses"])
        found = cases.judge_answer(seen)
        self.assertEqual([("answer-text", "contract_met", "stated"),
                          ("event-numbers", "observed_behavior", "unspecified")],
                         [(r["part"], r["disposition"], r["grade"]) for r in found])
        self.assertIn("changed none", found[1]["summary"])
        # A CLI that drops or reorders a text event is a defect.
        dropped = dict(seen["split-answer"], answer="".join(cases.ANSWER_PARTS[:-1]))
        self.assertEqual("reproduced_client_defect",
                         cases.judge_answer(dict(seen, **{"split-answer": dropped}))[0][
                             "disposition"])
        # A rounded big integer is recorded, not judged.
        rounded = dict(seen["event-numbers"], numbers=dict(
            seen["event-numbers"]["numbers"], beyond_u64="1.2345678901234568e+29"))
        out = cases.judge_answer(dict(seen, **{"event-numbers": rounded}))
        self.assertEqual("observed_behavior", out[1]["disposition"])
        self.assertIn("changed ['beyond_u64']", out[1]["summary"])
        for name, broken in (("control-404", dict(seen["control-404"], exit=0)),
                             ("control-json", dict(seen["control-json"], status=None))):
            self.assertEqual(["blocked"], [r["disposition"] for r in cases.judge_answer(
                dict(seen, **{name: broken}))])

    def test_differing_repeats_are_inconclusive(self):
        outputs = iter([{"exit": 0}, {"exit": 1}])
        fake = {"title": "t", "kind": "tier0-offline",
                "scenarios": lambda: [cases.Scenario("s", "probe", [], {})],
                "facts": lambda obs: {"exit": obs["exit"]}, "judge": lambda f: []}
        with mock.patch.dict(cases.CASES, {"CX": fake}), \
                mock.patch.object(cases, "run_scenario", lambda lab, s: next(outputs)):
            out = cases.execute(None, "CX", repeats=2)
        self.assertEqual("inconclusive", out["results"][0]["disposition"])

    def test_case_catalog_is_complete_and_offline(self):
        for case_id in cases.ORDER:
            case = cases.CASES[case_id]
            self.assertEqual("tier0-offline", case["kind"])
            names = [s.name for s in case["scenarios"]()]
            self.assertEqual(len(names), len(set(names)), case_id)
            self.assertTrue(any(s.role == "control" for s in case["scenarios"]()), case_id)


if __name__ == "__main__":
    unittest.main()
