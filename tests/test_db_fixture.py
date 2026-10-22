"""The dedicated-project fixture adapter (`fixture setup|cleanup|status`), offline.

SYNTHETIC: a stand-in for the pinned Seren CLI (tests/fixture_standin.py) and a dummy psql
are pinned by checksum in a temporary target; scenarios and service state live in the
stand-in's config folder. Every service request the stand-in receives is logged. These
checks establish adapter behaviour against synthetic services, not live provisioning.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from seren_support import SOURCE, prerun, standin
from seren_harness import cli, db_fixture

STANDIN = SOURCE / "tests" / "fixture_standin.py"
CANARY = "sk-" + "F" * 40
PACKAGE_ID = "seren.db-read.dedicated-project"
WRITE_CALLS = ("GET /projects/{id}/connection_uri",)


class Env:
    def __init__(self, path, scenario, **limits):
        self.root = Path(path) / "root"
        (self.root / "docs" / "db-fixture").mkdir(parents=True)
        (self.root / "targets").mkdir()
        self.config = self.root / "cli-config"
        self.config.mkdir()
        self.home = Path(path) / "home"
        package = json.loads((SOURCE / "docs/db-fixture/lifecycle-package.proposed.json")
                             .read_text())
        package["limits"].update({"api_invocation_timeout_seconds": 2,
                                  "psql_invocation_timeout_seconds": 2,
                                  "reattempt_delay_seconds": 0,
                                  "recovery_list_spacing_seconds": 0,
                                  "output_cap_bytes": 8192}, **limits)
        self.package = self.root / "docs/db-fixture/lifecycle-package.proposed.json"
        self.package.write_text(json.dumps(package, indent=2))
        self.manifest = self.root / "docs/db-fixture/fixture-manifest.json"
        shutil.copy(SOURCE / "docs/db-fixture/fixture-manifest.json", self.manifest)
        executable = self.root / "seren"
        executable.write_text(standin("#!" + sys.executable + " -I\nimport runpy\n"
                                      "runpy.run_path(" + repr(str(STANDIN))
                                      + ", run_name='__main__')\n"))
        executable.chmod(0o755)
        psql = self.root / "bin" / "psql"
        psql.parent.mkdir()
        psql.write_text(standin("#!/bin/sh\nexit 99\n"))
        psql.chmod(0o755)
        prerun(executable, psql)  # first-run assessment outside the timed invocations
        self.target = self.root / "targets" / "seren.json"
        self.target.write_text(json.dumps({"employee": "stand-in", "root": str(self.root),
                                           "collectors": {}, "fixtures": {PACKAGE_ID: {
            "executable": "{root}/seren", "executable_sha256": _sha(executable),
            "config_home": "{root}/cli-config", "profile": "default",
            "psql_executable": str(psql), "psql_sha256": _sha(psql)}}}))
        self.scenario(scenario)

    def scenario(self, scenario):
        (self.config / "scenario.json").write_text(json.dumps(dict(scenario, canary=CANARY)))

    def authorize(self, name="auth.json", **override):
        now = datetime.now(timezone.utc)
        auth = {"kind": "db_fixture_authorization", "package": PACKAGE_ID,
                "package_sha256": _sha(self.package), "manifest_sha256": _sha(self.manifest),
                "target_sha256": _sha(self.target), "approved_by": "test owner (synthetic)",
                "approved_at": now.isoformat(), "max_service_requests": 19,
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(minutes=30)).isoformat(),
                "cleanup_expires_at": (now + timedelta(minutes=60)).isoformat()}
        auth.update(override)
        auth = {k: v for k, v in auth.items() if v is not None}
        path = self.root / name
        path.write_text(json.dumps(auth))
        return path

    def run(self, action, auth=None):
        argv = ["fixture", action, "--package", str(self.package), "--manifest",
                str(self.manifest), "--home", str(self.home), "--root", str(self.root)]
        if auth:
            argv += ["--authorization", str(auth)]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}), \
                contextlib.redirect_stdout(out):
            code = cli.main(argv)
        self.output = getattr(self, "output", "") + out.getvalue()
        return code

    def calls(self):
        path = self.config / "calls.jsonl"
        return [json.loads(line)["request"] for line in path.read_text().splitlines()] \
            if path.exists() else []

    def state(self):
        return json.loads((self.home / "fixtures" / PACKAGE_ID / "state.json").read_text())

    def world(self):
        return json.loads((self.config / "world.json").read_text())

    def actions(self):
        return [(e["action"], e["status"]) for e in self.state()["actions"]]

    def saved_text(self):
        return "".join(p.read_text() for p in self.home.rglob("*") if p.is_file())

    def registered(self):
        return (self.home / "fixtures" / PACKAGE_ID / "registration.json").exists()


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


SETUP_CALLS = ["POST /projects", "GET /projects/{id}/branches",
               "GET /projects/{id}/branches/{bid}/databases",
               "GET /projects/{id}/connection_uri", "DB session",
               "GET /projects/{id}/connection_uri", "DB session"]
DELETE_CALLS = ["GET /projects/{id}", "DELETE /projects/{id}", "GET /projects/{id}"]


class FixtureAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def env(self, scenario=None, **limits):
        self.n += 1
        return Env(Path(self.tmp.name) / str(self.n), scenario or {}, **limits)

    def assertClean(self, e):
        """No secret reached saved artifacts or output, and requests stayed within the
        durable reservations."""
        self.assertNotIn(CANARY, e.saved_text() + e.output)
        if (e.home / "fixtures").exists():
            reserved = sum(e.state()["reserved"].values())
            self.assertLessEqual(len(e.calls()), reserved)
            self.assertLessEqual(reserved, db_fixture.MAX_REQUESTS)

    def test_setup_registers_the_exact_fixture_and_cleanup_verifies_deletion(self):
        for mode in ("soft", "hard"):
            e = self.env({"delete_mode": mode})
            auth = e.authorize()
            self.assertEqual(e.run("setup", auth), db_fixture.OK, e.output)
            self.assertEqual(e.calls(), SETUP_CALLS)
            registration = json.loads((e.home / "fixtures" / PACKAGE_ID / "registration.json")
                                      .read_text())
            project = e.world()["projects"][0]
            self.assertEqual((registration["project"]["id"], registration["branch_id"],
                              registration["database"]),
                             (project["id"], project["default_branch_id"], "serendb"))
            self.assertEqual(e.state()["obligation"]["status"], "open")
            self.assertEqual(e.state()["reserved"], {"ordinary": 7, "cleanup": 0})
            self.assertEqual(e.run("status"), db_fixture.OK)
            self.assertEqual(e.run("setup", auth), db_fixture.OK)  # no duplicate work
            self.assertEqual(len(e.calls()), 7)
            self.assertEqual(e.run("cleanup", auth), db_fixture.OK, e.output)
            self.assertEqual(e.calls(), SETUP_CALLS + DELETE_CALLS)
            self.assertEqual(e.state()["obligation"]["status"], "closed")
            self.assertEqual((e.state()["phase"]), "cleaned")
            self.assertEqual(e.run("setup", auth), db_fixture.INVALID)
            self.assertEqual(e.run("cleanup", auth), db_fixture.OK)
            self.assertEqual(len(e.calls()), 10)
            self.assertClean(e)

    def assertUnproven(self, e, candidates, calls):
        """An uncertain create stays unresolved: no ID bound, nothing written, registered or
        deleted, candidates kept as evidence only, and only R1 listings were added."""
        state = e.state()
        obligation = state["obligation"]
        self.assertEqual((obligation["status"], obligation["project_id"]), ("unresolved", None))
        self.assertNotIn("project.id", state["bindings"])
        self.assertEqual(len(obligation.get("candidates", [])), candidates)
        self.assertIn("does not show this attempt created it", obligation["reason"])
        self.assertIn(str(candidates) + " same-name project(s) listed" if candidates
                      else "no same-name project listed", obligation["reason"])
        self.assertEqual(e.calls(), calls)
        for forbidden in ("DELETE /projects/{id}", "DB session", "GET /projects/{id}/branches"):
            self.assertNotIn(forbidden, e.calls())
        self.assertFalse(e.registered())
        for entry in state["actions"]:
            if entry["action"] == "R1":
                retained = json.loads(Path(entry["evidence"]).read_text())["retained"]
                self.assertTrue(retained["ownership"].startswith("unproven"))
        self.assertEqual(state["reserved"]["ordinary"], 2)  # A1 + one listing
        self.assertClean(e)

    def test_create_output_must_be_the_banner_and_a_top_level_project(self):
        # Each of these really created the project but its response was unusable: the
        # listing shows it, yet that is not proof this attempt created it.
        for behaviour in ("wrapped", "malformed", "banner_mismatch", "huge", "error_503"):
            e = self.env({"create": behaviour})
            self.assertEqual(e.run("setup", e.authorize()), db_fixture.BLOCKED, behaviour)
            self.assertEqual(e.actions(), [("A1", "uncertain"), ("R1", "done")], behaviour)
            self.assertUnproven(e, 1, ["POST /projects", "GET /projects"])
            self.assertIsNone(e.world()["projects"][0]["deleted_at"])
        # A usable create response identifies the project itself: bound and cleaned up.
        e = self.env({"create": "config_mismatch"})
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.FAILED)
        self.assertEqual(e.calls(), ["POST /projects"] + DELETE_CALLS)
        self.assertIn("configuration differs", e.state()["failure"])
        self.assertEqual(e.state()["obligation"]["provenance"], db_fixture.CREATE_RESPONSE)

    def test_refused_creation_closes_the_obligation_without_cleanup_calls(self):
        e = self.env({"create": "reject_400"})
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.FAILED)
        self.assertEqual(e.calls(), ["POST /projects"])
        self.assertEqual(e.state()["obligation"]["status"], "closed")
        self.assertClean(e)

    def test_uncertain_creation_never_binds_a_listed_project(self):
        now = datetime.now(timezone.utc)
        name = "seren-harness-dbread-51ee20c6"

        def listed(minutes, uid):
            return {"id": uid, "name": name, "deleted_at": None,
                    "created_at": (now + timedelta(minutes=minutes)).isoformat()}
        recent = listed(-2, "11111111-2222-4333-8444-555555555555")  # pre-existing, 2 min old
        later = listed(3, "66666666-7777-4888-8999-aaaaaaaaaaaa")    # outside the window
        scenarios = (
            ({"create": "timeout_before", "list_extra": [recent]}, 1),
            ({"create": "timeout_before", "list_extra": [later]}, 1),
            ({"create": "timeout_after"}, 1),            # really created; response lost
            ({"create": "timeout_after", "list_hide": True}, 0),
            ({"create": "timeout_before"}, 0),
            ({"create": "timeout_after", "list_extra": [recent]}, 2),
            ({"create": "timeout_after", "list_has_more": True}, 1))
        for scenario, candidates in scenarios:
            e = self.env(dict(scenario, stall_seconds=5), api_invocation_timeout_seconds=1)
            auth = e.authorize()
            self.assertEqual(e.run("setup", auth), db_fixture.BLOCKED, scenario)
            self.assertUnproven(e, candidates, ["POST /projects", "GET /projects"])
            r1 = json.loads(Path(e.state()["actions"][1]["evidence"]).read_text())["retained"]
            self.assertEqual(r1["list_complete"], not scenario.get("list_has_more", False))
            # A fresh cleanup lists once more (cleanup reserve) and still binds nothing.
            self.assertEqual(e.run("cleanup", auth), db_fixture.BLOCKED, scenario)
            self.assertEqual(e.run("cleanup", auth), db_fixture.BLOCKED, scenario)
            self.assertEqual(e.calls(), ["POST /projects", "GET /projects", "GET /projects"])
            self.assertEqual(e.state()["reserved"], {"ordinary": 2, "cleanup": 1})
            self.assertEqual(e.run("setup", auth), db_fixture.INVALID)  # never re-created
            self.assertEqual(e.calls().count("POST /projects"), 1)
            self.assertEqual(e.state()["obligation"]["status"], "unresolved")
            self.assertIsNone(e.state()["obligation"]["project_id"])
            self.assertTrue(all(p.get("deleted_at") is None for p in e.world()["projects"]))
            self.assertIn("confirming in Seren", e.state()["blocker"])
            self.assertClean(e)

    def test_legacy_listing_binding_never_becomes_deletion_authority(self):
        e = self.env({"create": "timeout_after", "stall_seconds": 5})
        auth = e.authorize()
        self.assertEqual(e.run("setup", auth), db_fixture.BLOCKED)
        # Recreate the record the previous adapter version wrote after a name/time match.
        path = e.home / "fixtures" / PACKAGE_ID / "state.json"
        state = json.loads(path.read_text())
        project = e.world()["projects"][0]["id"]
        state["bindings"].update({"project.id": project, "project.source": "recovery listing"})
        state["obligation"] = {"status": "open", "project_id": project,
                               "reason": "project " + project + " bound from recovery listing"}
        state["actions"][1]["outcome"] = "one owned project bound"
        state["blocker"] = None
        path.write_text(json.dumps(state))
        before = e.calls()
        self.assertEqual(e.run("cleanup", auth), db_fixture.BLOCKED)
        self.assertEqual(e.run("setup", auth), db_fixture.INVALID)
        self.assertEqual(e.calls(), before)  # no GET, DELETE, write or create
        after = e.state()
        self.assertEqual(after["obligation"]["status"], "unresolved")
        self.assertEqual(after["obligation"]["project_id"], project)  # kept, not rewritten
        self.assertEqual(after["bindings"]["project.source"], "recovery listing")
        self.assertEqual(after["actions"][1]["outcome"], "one owned project bound")
        self.assertEqual(after["reserved"], state["reserved"])
        self.assertIn("ownership is unproven", after["blocker"])
        self.assertIsNone(e.world()["projects"][0]["deleted_at"])
        self.assertEqual(e.run("status"), db_fixture.BLOCKED)
        self.assertClean(e)

    def test_a_proven_project_is_still_cleaned_after_a_later_setup_failure(self):
        e = self.env({"readback": "wrong_value"})
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.FAILED)
        state = e.state()
        self.assertEqual(state["bindings"]["project.source"], db_fixture.CREATE_RESPONSE)
        self.assertEqual(state["obligation"]["provenance"], db_fixture.CREATE_RESPONSE)
        self.assertEqual(state["obligation"]["status"], "closed")
        self.assertEqual(e.calls(), SETUP_CALLS + DELETE_CALLS)
        self.assertEqual(state["reserved"], {"ordinary": 7, "cleanup": 3})
        self.assertIsNotNone(e.world()["projects"][0]["deleted_at"])

    def test_inputs_and_authority_are_checked_before_any_request(self):
        e = self.env()
        auth = e.authorize()
        manifest = json.loads(e.manifest.read_text())
        manifest["rows"][0]["amount_cents"] = 1
        e.manifest.write_text(json.dumps(manifest))
        self.assertEqual(e.run("setup", auth), db_fixture.INVALID)  # changed manifest
        self.assertEqual(e.run("setup", e.authorize("re.json")), db_fixture.INVALID)
        self.assertIn("write statement is not exactly its rows", e.output)
        e = self.env()
        package = json.loads(e.package.read_text())
        package["actions"][0]["argv"].insert(-2, "--psql")
        e.package.write_text(json.dumps(package))
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.INVALID)
        self.assertIn("A1 is not the supported command", e.output)
        e = self.env()
        target = json.loads(e.target.read_text())
        target["fixtures"][PACKAGE_ID]["executable_sha256"] = "0" * 64
        auth = e.authorize()
        e.target.write_text(json.dumps(target))
        self.assertEqual(e.run("setup", auth), db_fixture.INVALID)  # target changed
        self.assertEqual(e.run("setup", e.authorize("t.json")), db_fixture.INVALID)
        self.assertIn("pinned checksum", e.output)
        e = self.env()
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        for override in ({"expires_at": past.isoformat()}, {"max_service_requests": 25},
                         {"approved_by": ""}, {"package": "other"},
                         {"cleanup_expires_at": past.isoformat()}):
            self.assertEqual(e.run("setup", e.authorize("x.json", **override)),
                             db_fixture.INVALID, override)
        self.assertEqual(e.calls(), [])
        self.assertFalse((e.home / "fixtures" / PACKAGE_ID / "state.json").exists())
        # A recorded attempt keeps its authority; a different file is refused.
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.OK)
        self.assertEqual(e.run("cleanup", e.authorize("other.json", approved_by="someone")),
                         db_fixture.INVALID)
        self.assertIn("different authorization", e.output)
        # No command-line route to a target, ID, SQL or extra argument.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parser().parse_args(["fixture", "cleanup", "--project-id", "x"])

    def test_unexpected_bindings_stop_setup_and_clean_up(self):
        for scenario in ({"branches": "two"}, {"databases": "two"}, {"databases": "service"}):
            e = self.env(scenario)
            self.assertEqual(e.run("setup", e.authorize()), db_fixture.FAILED, scenario)
            self.assertNotIn("DB session", e.calls())
            self.assertEqual(e.calls()[-3:], DELETE_CALLS)
            self.assertFalse(e.registered())

    def test_write_is_retried_only_after_a_pre_send_failure(self):
        e = self.env({"write": ["uri_fail", "ok"]})
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.OK, e.output)
        self.assertEqual([s for a, s in e.actions() if a == "A4"], ["pre_send_failure", "done"])
        self.assertEqual(e.calls().count("DB session"), 2)
        e = self.env({"write": "uri_fail"})
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.FAILED)
        self.assertEqual([s for a, s in e.actions() if a == "A4"], ["pre_send_failure"] * 3)
        self.assertNotIn("DB session", e.calls())
        self.assertNotIn("A5", [a for a, _ in e.actions()])
        # psql may have run: never rewritten; the read-back decides.
        for behaviour, expected in (("conn_fail", db_fixture.FAILED),
                                    ("lost_before_commit", db_fixture.FAILED),
                                    ("lost_after_commit", db_fixture.OK)):
            e = self.env({"write": behaviour})
            self.assertEqual(e.run("setup", e.authorize()), expected, behaviour)
            self.assertEqual([s for a, s in e.actions() if a == "A4"], ["failed"], behaviour)
            self.assertEqual(e.world()["counters"]["write"], 1)
            self.assertEqual(e.registered(), expected == db_fixture.OK)
            self.assertClean(e)

    def test_wrong_or_incomplete_fixture_data_prevents_registration(self):
        for change in ("wrong_value", "wrong_type", "bool_for_int", "reorder", "missing_row",
                       "null_as_empty", "extra_relation", "canary"):
            e = self.env({"readback": change})
            self.assertEqual(e.run("setup", e.authorize()), db_fixture.FAILED, change)
            self.assertFalse(e.registered(), change)
            self.assertEqual(e.actions()[-3:], [("A5", "failed"), ("C1", "done"),
                                                 ("C2", "done")], change)
            self.assertClean(e)
        e = self.env({"read": "uri_fail"})  # no read-back observation at all
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.FAILED)
        self.assertFalse(e.registered())

    def test_uncertain_deletion_has_bounded_recovery(self):
        cases = ((["hang_after"], {}, db_fixture.OK, 3),
                 (["noop_ok", "ok"], {}, db_fixture.OK, 6),
                 (["ok"], {"get": ["hang", "ok"]}, db_fixture.OK, 4),
                 (["error_500"], {}, db_fixture.BLOCKED, 6))
        for delete, extra, code, count in cases:
            e = self.env(dict({"delete": delete, "stall_seconds": 5}, **extra),
                         api_invocation_timeout_seconds=1)
            auth = e.authorize()
            self.assertEqual(e.run("setup", auth), db_fixture.OK)
            self.assertEqual(e.run("cleanup", auth), code, delete)
            self.assertEqual(len(e.calls()) - 7, count, (delete, e.calls()[7:]))
            if code == db_fixture.BLOCKED:
                obligation = e.state()["obligation"]
                self.assertEqual(obligation["status"], "unresolved")
                self.assertEqual(obligation["project_id"], e.world()["projects"][0]["id"])
                self.assertEqual(e.run("cleanup", auth), db_fixture.BLOCKED)
                self.assertEqual(len(e.calls()) - 7, count)  # limits hold across processes
            self.assertClean(e)

    def test_interruption_resumes_from_durable_state_without_replay(self):
        e = self.env({"write": "hang_before", "stall_seconds": 30},
                     psql_invocation_timeout_seconds=40)
        auth = e.authorize()
        argv = [sys.executable, "-m", "seren_harness", "fixture", "setup", "--package",
                str(e.package), "--manifest", str(e.manifest), "--authorization", str(auth),
                "--home", str(e.home), "--root", str(e.root)]
        process = subprocess.Popen(argv, cwd=str(SOURCE), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   env=dict(os.environ, PYTHONPATH=str(SOURCE)))
        try:
            for _ in range(200):
                state = e.home / "fixtures" / PACKAGE_ID / "state.json"
                if state.exists() and ("A4", "in_flight") in e.actions():
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            process.send_signal(signal.SIGINT)
            process.wait(10)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(e.actions()[-1], ("A4", "uncertain"))
        reserved = e.state()["reserved"]["ordinary"]
        lock = e.home / "fixtures" / PACKAGE_ID / "lock"
        lock.write_text("999999")  # a crashed holder's stale lock
        e.scenario({})
        self.assertEqual(e.run("setup", auth), db_fixture.FAILED, e.output)
        self.assertEqual(e.calls().count("GET /projects/{id}/connection_uri"), 2)  # A4, A5
        self.assertNotIn(("A4", "done"), e.actions())
        self.assertEqual(e.state()["reserved"]["ordinary"], reserved + 2)  # not reset
        self.assertEqual(e.state()["obligation"]["status"], "closed")
        self.assertClean(e)

    def test_timeout_kills_the_process_group_and_the_read_back_decides(self):
        e = self.env({"write": "hang_after_commit", "stall_seconds": 30})
        started = time.monotonic()
        self.assertEqual(e.run("setup", e.authorize()), db_fixture.OK, e.output)
        self.assertLess(time.monotonic() - started, 20)
        pid = int((e.config / "child.pid").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual([s for a, s in e.actions() if a == "A4"], ["uncertain"])
        self.assertEqual(e.world()["counters"]["write"], 1)
        self.assertClean(e)

    def test_expiry_allows_only_an_explicit_cleanup_window(self):
        soon = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
        for window, code in ((True, db_fixture.OK), (False, db_fixture.INVALID)):
            e = self.env()
            auth = e.authorize(expires_at=soon, cleanup_expires_at=None if not window else
                               (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
            self.assertEqual(e.run("setup", auth), db_fixture.OK)
        time.sleep(max(0, (datetime.fromisoformat(soon) - datetime.now(timezone.utc))
                       .total_seconds()) + 0.2)
        for n, code in ((self.n - 1, db_fixture.OK), (self.n, db_fixture.INVALID)):
            e = self._existing(n)
            auth = e.root / "auth.json"
            self.assertEqual(e.run("cleanup", auth), code)
            self.assertEqual(e.run("setup", auth), db_fixture.INVALID)  # main window over
        self.assertEqual(e.state()["obligation"]["status"], "open")
        self.assertEqual(e.run("status"), db_fixture.OK)

    def _existing(self, n):
        e = Env.__new__(Env)
        base = Path(self.tmp.name) / str(n)
        e.root, e.home, e.config = base / "root", base / "home", base / "root" / "cli-config"
        e.package = e.root / "docs/db-fixture/lifecycle-package.proposed.json"
        e.manifest = e.root / "docs/db-fixture/fixture-manifest.json"
        return e

    def test_cleanup_reserve_survives_exhausted_ordinary_work(self):
        e = self.env()
        auth = e.authorize()
        self.assertEqual(e.run("setup", auth), db_fixture.OK)
        fixture = db_fixture.Fixture(e.package, e.manifest, e.home, e.root)
        fixture.auth = json.loads(auth.read_text())
        fixture._load_state(_sha(auth))
        fixture.state["reserved"]["ordinary"] = db_fixture.MAX_REQUESTS - \
            db_fixture.CLEANUP_RESERVE - 1
        fixture.state["invocations"]["A5"] = 0
        with self.assertRaises(db_fixture._Stop) as stop:
            fixture._invoke("A5", "ordinary")
        self.assertIn("cleanup reserve", str(stop.exception))
        fixture.save()
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fixture.cleanup(auth, io.StringIO()), db_fixture.OK)
        self.assertEqual(e.calls()[-3:], DELETE_CALLS)
        self.assertEqual(e.state()["reserved"]["cleanup"], 3)

    def test_ceilings_are_recomputed_from_the_implemented_actions(self):
        package = json.loads((SOURCE / "docs/db-fixture/lifecycle-package.proposed.json")
                             .read_text())
        accounting = package["accounting"]["fixture_adapter"]
        self.assertEqual(db_fixture.MAX_REQUESTS, 19)
        self.assertEqual(db_fixture.CLEANUP_RESERVE, 7)
        self.assertEqual(accounting["max_cli_invocations"], sum(db_fixture.MAX.values()))
        self.assertEqual(accounting["max_mutating_invocations"],
                         sum(db_fixture.MAX[a] for a in db_fixture.MUTATING))
        self.assertEqual(accounting["reserved_requests_per_invocation"], db_fixture.COST)
        reads = package["accounting"]["campaign_reads"]["service_requests"]
        self.assertEqual(package["accounting"]["package_total"]["service_requests"],
                         db_fixture.MAX_REQUESTS + reads)
        self.assertEqual(package["campaign"]["max_service_requests"], reads)
        self.assertEqual(package["authorization"]["kind"], db_fixture.CAMPAIGN_AUTHORIZATION)


if __name__ == "__main__":
    unittest.main()
