"""State, locking, journal, deduplication, sanitizing and reply correlation."""
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

from seren_support import SOURCE
from seren_harness import discord, seren
from seren_harness.findings import finding_key
from seren_harness.models import DEFECT, FAILED, Finding, StepResult
from seren_harness.sanitize import REDACTED, Sanitizer, pseudonymize
from seren_harness.state import LockHeld, Store

# Deliberately invalid account/signature, but within our conservative redaction shape.
DISCORD_TOKEN = "M_NOT_A_REAL_ACCOUNT_ID__.FAKE00.NOT_A_REAL_SIGNATURE_FOR_TESTS"
REPLY_TYPE = 19  # Discord message type REPLY.
PROBE_SEQUENCE = 0x800
LAUNCH_URL ="https://passwords.example.invalid/setup/0f1e2d3c4b5a69788796a5b4c3d2e1f0?code=s3cret"


class Temporary(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="seren-harness-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def store(self, sanitizer=None):
        store = Store(self.root / "state", sanitizer)
        self.addCleanup(store.close)
        return store


class SnapshotImportTests(unittest.TestCase):
    def test_package_is_imported_from_the_tested_tree(self):
        import seren_harness
        self.assertEqual(Path(seren_harness.__file__).resolve().parents[1], SOURCE)


class LockTests(Temporary):
    def test_second_run_cannot_lock_same_employee(self):
        store = self.store()
        first = store.create_run("operational", "emp", "simulation", "d", "0")
        second = store.create_run("operational", "emp", "simulation", "d", "0")
        store.acquire_lock("emp", first)
        with self.assertRaisesRegex(LockHeld, first):
            store.acquire_lock("emp", second)
        other = store.create_run("operational", "other-emp", "simulation", "d", "0")
        store.acquire_lock("other-emp", other)  # Different employee is independent.

    def test_concurrent_processes_cannot_both_lock(self):
        state = self.root / "state"
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from seren_harness.state import Store, LockHeld\n"
            "import time\n"
            "s = Store(sys.argv[2]); r = s.create_run('operational','emp','simulation','d','0')\n"
            "try:\n s.acquire_lock('emp', r); print('LOCKED', flush=True); time.sleep(1.5)\n"
            "except LockHeld: print('REFUSED', flush=True)\n")
        Store(state).close()
        processes = [subprocess.Popen([sys.executable, "-c", script, str(SOURCE), str(state)],
                                      stdout=subprocess.PIPE, text=True) for _ in range(4)]
        outputs = [p.communicate(timeout=30)[0].strip() for p in processes]
        self.assertEqual(outputs.count("LOCKED"), 1, outputs)
        self.assertEqual(outputs.count("REFUSED"), 3, outputs)

    def test_lock_left_by_exited_process_is_not_silently_taken(self):
        store = self.store()
        old = store.create_run("operational", "emp", "simulation", "d", "0")
        store.db.execute("INSERT INTO locks VALUES ('emp', ?, 999999, ?, 'x')",
                         (old, socket.gethostname()))
        new = store.create_run("operational", "emp", "simulation", "d", "0")
        with self.assertRaisesRegex(LockHeld, "resume that run"):
            store.acquire_lock("emp", new)


class DeduplicationTests(Temporary):
    def finding(self, step_context):
        return Finding("connector_ready", DEFECT, "Connector not ready", "ready",
                       "channel_binding_failure", context=step_context)

    def test_identical_failures_accumulate_occurrences(self):
        store = self.store()
        context = {"activity_kind": "dispatch_channel", "stop_reason": "channel_binding_failure",
                   "run_id": "volatile-1", "request_id": "r-1"}
        key = finding_key("operational", "connector_ready", self.finding(context), "simulation")
        for index in range(3):
            run = store.create_run("operational", "emp", "simulation", "d", "0")
            changed = dict(context, run_id="volatile-" + str(index), request_id="r" + str(index))
            same = finding_key("operational", "connector_ready", self.finding(changed),
                               "simulation")
            self.assertEqual(same, key)
            store.record_finding(same, run, "operational", "connector_ready",
                                 self.finding(changed))
            store.record_finding(same, run, "operational", "connector_ready",
                                 self.finding(changed))  # Re-recorded in one run: one occurrence.
        self.assertEqual(len(store.findings()), 1)
        self.assertEqual(store.finding(key)["occurrences"], 3)

    def test_generic_error_text_does_not_merge_distinct_contexts(self):
        a = self.finding({"activity_kind": "dispatch_channel",
                          "stop_reason": "channel_binding_failure"})
        b = self.finding({"activity_kind": "connector_binding_apply",
                          "stop_reason": "channel_binding_failure"})
        key = finding_key("operational", "connector_ready", a, "live")
        self.assertNotEqual(key, finding_key("operational", "connector_ready", b, "live"))
        self.assertNotEqual(key, finding_key("provisioning", "connector_ready", a, "live"))
        self.assertNotEqual(key, finding_key("operational", "verify_fixture", a, "live"))

    def test_simulated_occurrences_never_merge_into_live_findings(self):
        a = self.finding({"stop_reason": "channel_binding_failure"})
        self.assertNotEqual(finding_key("operational", "connector_ready", a, "live"),
                            finding_key("operational", "connector_ready", a, "simulation"))


class SanitizerTests(Temporary):
    def test_secrets_and_capability_urls_never_reach_the_database(self):
        # Exercise the token pattern without a Bearer/token prefix or a known-secret entry.
        self.assertEqual(Sanitizer().text("before " + DISCORD_TOKEN + " after"),
                         "before " + REDACTED + " after")
        sanitizer = Sanitizer(["canary-value-from-env-1234"])
        store = self.store(sanitizer)
        run = store.create_run("operational", "emp", "simulation", "d", "0")
        evidence = {"message": "token " + DISCORD_TOKEN + " link " + LAUNCH_URL,
                    "launch_url": LAUNCH_URL, "nested": [{"api_key": "abc"},
                                                         "x canary-value-from-env-1234 y"],
                    "header": "Authorization: Bearer abcdefghijklmnop"}
        store.record_step(run, 0, "s", StepResult(FAILED, "saw " + LAUNCH_URL, evidence,
                                                  None))
        store.event(run, "e", evidence)
        store.close()
        raw = (self.root / "state" / "state.sqlite3").read_bytes()
        for secret in (DISCORD_TOKEN, "s3cret", "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
                       "canary-value-from-env-1234", "abcdefghijklmnop"):
            self.assertNotIn(secret.encode(), raw)
        self.assertIn(b"passwords.example.invalid", raw)  # Host kept for diagnosis.

    def test_url_keeps_ordinary_path_and_drops_query(self):
        text = Sanitizer().text("see https://docs.example.invalid/guides/configure?x=1.")
        self.assertEqual(text, "see https://docs.example.invalid/guides/configure" + REDACTED + ".")

    def test_pseudonymize_is_stable(self):
        text = pseudonymize("dep 7a000006-0000-4000-8000-000000000007 by Example Employee "
                            "again 7A000006-0000-4000-8000-000000000007",
                            names=["Example Employee"])
        self.assertEqual(text, "dep <id-1> by <name-1> again <id-1>")


class CorrelationTests(unittest.TestCase):
    channel, employee, human = "500", "900", "700"

    def message(self, offset_ms, author, content="", reference=None, channel=None,
                sequence=0, message_type=None, reference_type=None):
        # Low 22 bits model Discord's worker, process and increment fields, so distinct
        # ids can share one millisecond.
        snowflake = ((self.base + offset_ms - discord.DISCORD_EPOCH_MS) << 22) | sequence
        message = {"id": str(snowflake), "channel_id": channel or self.channel,
                   "author": {"id": author}, "content": content}
        if reference:
            message["message_reference"] = {"message_id": reference}
            if reference_type is not None:
                message["message_reference"]["type"] = reference_type
        if message_type is not None:
            message["type"] = message_type
        return message

    def explicit_reply(self, offset_ms, sequence, **fields):
        fields = dict({"author": self.employee, "content": "pong",
                       "reference": self.probe["id"], "message_type": REPLY_TYPE}, **fields)
        return self.message(offset_ms, sequence=sequence, **fields)

    def setUp(self):
        self.base = 1_800_000_000_000
        self.marker = "SH-ABC123"
        self.probe = self.message(0, self.human, "<@900> ping " + self.marker,
                                  sequence=PROBE_SEQUENCE)

    def test_same_millisecond_explicit_reply_passes_whichever_id_is_larger(self):
        for sequence in (PROBE_SEQUENCE - 1, PROBE_SEQUENCE + 1):
            reply = self.explicit_reply(0, sequence)
            self.assertNotEqual(reply["id"], self.probe["id"])
            self.assertEqual(self.correlate([reply])[0], reply, sequence)
            self.assertEqual(discord.reply_kind(reply, self.probe), "reply_reference")

    def test_same_millisecond_exception_needs_a_genuine_explicit_reply(self):
        candidates = [
            self.explicit_reply(0, 1, author=self.human),                 # wrong author
            self.explicit_reply(0, 2, channel="501"),                     # other channel
            self.explicit_reply(0, 3, reference="12345"),                 # other message
            self.explicit_reply(0, 4, reference_type=1),                  # forward
            self.explicit_reply(0, 5, message_type=0),                    # not a reply
            self.message(0, self.employee, "echo " + self.marker, sequence=6),  # marker only
            self.explicit_reply(-1, 7),                                   # earlier ms
        ]
        reply, rejections = self.correlate(candidates)
        self.assertIsNone(reply)
        self.assertEqual(rejections, {"wrong_author": 1, "other_channel": 1,
                                      "stale_before_probe": 5})

    def test_probe_identity_never_passes(self):
        echo = dict(self.probe, author={"id": self.employee}, type=REPLY_TYPE,
                    message_reference={"message_id": self.probe["id"]})
        self.assertEqual(self.correlate([echo]), (None, {}))

    def test_later_forward_is_not_a_reply_but_later_marker_still_matches(self):
        forward = self.message(1000, self.employee, reference=self.probe["id"],
                               reference_type=1)
        self.assertEqual(self.correlate([forward]), (None, {"unrelated_to_probe": 1}))
        echo = self.message(1000, self.employee, "done " + self.marker)
        self.assertEqual(self.correlate([echo])[0], echo)
        self.assertEqual(discord.reply_kind(echo, self.probe), "marker")

    def test_journey_step_accepts_same_millisecond_explicit_reply(self):
        reply = self.explicit_reply(0, PROBE_SEQUENCE - 1)
        channel = types.SimpleNamespace(messages=lambda: [self.probe, reply])
        ctx = types.SimpleNamespace(
            config=types.SimpleNamespace(discord={"channel_id": self.channel,
                                                  "employee_user_id": self.employee},
                                         reply_timeout=1, poll_interval=1),
            evidence=lambda step: {"probe_id": self.probe["id"], "marker": self.marker,
                                   "input_mode": "human-assisted"},
            observer=channel, observer_problem=None, clock=lambda: 0,
            sleep=lambda seconds: self.fail("reply not accepted; the step kept polling"))
        result = seren.verify_reply(ctx)
        self.assertEqual(result.outcome, "passed", result.summary)
        self.assertEqual((result.evidence["matched_by"], result.evidence["latency_ms"]),
                         ("reply_reference", 0))
        self.assertIn("millisecond resolution", result.summary)

    def test_window_boundary_is_unchanged(self):
        self.assertIsNotNone(self.correlate([self.message(60_000, self.employee,
                                                          self.marker)])[0])
        self.assertEqual(self.correlate([self.message(60_001, self.employee, self.marker)])[1],
                         {"outside_window": 1})

    def correlate(self, messages):
        return discord.correlate_reply([self.probe, *messages], self.probe, self.channel,
                                       self.employee, self.marker, window_ms=60_000)

    def test_bound_reply_passes(self):
        reply = self.message(2000, self.employee, "pong", reference=self.probe["id"])
        self.assertEqual(self.correlate([reply])[0], reply)

    def test_stale_unrelated_wrong_author_and_late_replies_cannot_pass(self):
        candidates = [
            self.message(-5000, self.employee, "old " + self.marker),       # stale
            self.message(1000, self.employee, "hello from earlier task"),  # unrelated
            self.message(1500, self.human, "fake " + self.marker,          # wrong author
                         reference=self.probe["id"]),
            self.message(2000, self.employee, self.marker, channel="501"),  # other channel
            self.message(120_000, self.employee, self.marker),             # outside window
        ]
        reply, rejections = self.correlate(candidates)
        self.assertIsNone(reply)
        self.assertEqual(rejections, {"stale_before_probe": 1, "unrelated_to_probe": 1,
                                      "wrong_author": 1, "other_channel": 1,
                                      "outside_window": 1})

    def test_rest_observer_reads_documented_shape_without_network(self):
        requests = []

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.body

        def opener(request, timeout):
            requests.append(request)
            return Response(b'[{"id": "1", "channel_id": "500", "author": {"id": "900"}}]')

        os.environ["SEREN_HARNESS_TEST_READER"] = "reader-token-value"
        self.addCleanup(os.environ.pop, "SEREN_HARNESS_TEST_READER")
        observer = discord.RestObserver("500", "SEREN_HARNESS_TEST_READER", opener=opener)
        self.assertEqual(observer.messages()[0]["author"]["id"], "900")
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertTrue(requests[0].full_url.startswith(discord.API + "/channels/500/messages"))
        with self.assertRaises(discord.ObserverUnavailable):
            discord.RestObserver("500", "SEREN_HARNESS_UNSET_VARIABLE")
        # Undocumented shapes block observation instead of crashing correlation.
        for body in (b'{"message": "rate limited"}', b'[{"id": 1, "channel_id": "500"}]',
                     b'[{"id": "1", "channel_id": "500", "author": "900"}]'):
            observer = discord.RestObserver(
                "500", "SEREN_HARNESS_TEST_READER",
                opener=lambda request, timeout, body=body: Response(body))
            with self.assertRaises(discord.ObserverUnavailable):
                observer.messages()

    def test_probe_must_follow_window_start(self):
        self.assertIsNone(discord.find_probe([self.probe], self.marker, self.channel,
                                             self.base + 1))
        self.assertEqual(discord.find_probe([self.probe], self.marker, self.channel, self.base),
                         self.probe)


if __name__ == "__main__":
    unittest.main()
