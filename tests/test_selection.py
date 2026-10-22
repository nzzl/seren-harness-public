"""Automatic selection from a finite catalog, executed through the campaign runner.

SYNTHETIC: a temporary root with the three run-record cases, a stand-in Seren CLI and
synthetic catalogs whose entries carry different histories, blockers and authorizations.
Selection reads only saved local evidence; the stand-in counts every service request.
"""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE  # noqa: F401  (imports this checkout)
from seren_harness import assertions, campaign, cli, collect
from test_campaign import CANARY, CASES, Fixture, answers, record

A, B, C = list(CASES)
NEVER = {"never_run_live_as_of": "2026-10-13", "source": "synthetic test record"}


class Selecting(Fixture):
    def catalog(self, entries=None, name="catalog.json"):
        target = json.loads((self.root / "targets" / "seren.json").read_text())["collectors"]
        cases = {c["id"]: c for c in (json.loads((self.root / "cases" / n).read_text())
                                      for n in CASES.values())}
        options = entries or {}
        body = {"kind": "catalog", "id": "synthetic.catalog", "version": 1,
                "target": "targets/seren.json", "request_timeout_seconds": self.timeout,
                "entries": [dict({"case": k, "version": cases[k]["version"],
                                  "digest": assertions.snapshot(cases[k])["digest"],
                                  "args": target[k]["args"], "expectation_sources": ["case basis"],
                                  "prerequisites": [], "max_requests": 1},
                                 **options.get(k, {})) for k in CASES]}
        self.catalog_path = self.root / "catalogs" / name
        self.catalog_path.parent.mkdir(exist_ok=True)
        self.catalog_path.write_text(json.dumps(body))
        return self.catalog_path

    def authorize_catalog(self, name="catalog-auth.json", candidates=None, **override):
        body = json.loads(self.catalog_path.read_text())
        now = datetime.now(timezone.utc)
        auth = {"catalog": body["id"],
                "catalog_sha256": hashlib.sha256(self.catalog_path.read_bytes()).hexdigest(),
                "allow_subset_selection": True,
                "candidates": candidates or {e["case"]: e["args"] for e in body["entries"]},
                "max_service_requests": 3, "retries": 0,
                "request_timeout_seconds": self.timeout,
                "approved_by": "test owner (synthetic)", "approved_at": now.isoformat(),
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(minutes=30)).isoformat()}
        auth.update(override)
        path = self.root / name
        path.write_text(json.dumps(auth))
        return path

    def run_catalog(self, auth, execute=collect.run):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}):
            code = campaign.Campaign(self.catalog_path, self.home, self.root).run(
                auth, out, execute)
        return code, out.getvalue()

    def collect(self, case_id):
        args = argparse.Namespace(case=case_id, target=str(self.root / "targets" / "seren.json"),
                                  target_root=None, from_copy=None, retest=None,
                                  home=str(self.home), state_dir=None, root=str(self.root),
                                  no_retry=True)
        return collect.run(args, io.StringIO())

    def catalog_state(self):
        return json.loads((self.home / "campaigns" / "synthetic.catalog" / "state.json")
                          .read_text())

    def decisions(self):
        return {d["case"]: (d["decision"], d["reason"])
                for d in self.catalog_state()["selection"]["decisions"]}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def fixture(self, **kwargs):
        self.n += 1
        return Selecting(Path(self.tmp.name) / str(self.n), answers(**kwargs))

    def test_selects_untested_behaviour_and_explains_every_decision(self):
        f = self.fixture()
        f.catalog({A: {"history": NEVER},
                   B: {"history": NEVER, "prerequisites": [
                       {"id": "fixture", "status": "unresolved", "source": "synthetic"}]}})
        code, out = f.run_catalog(f.authorize_catalog())
        self.assertEqual((code, f.calls()), (campaign.OK, 1), out)
        decisions = f.decisions()
        self.assertEqual(decisions[A][0], "selected")
        self.assertIn("not yet demonstrated", decisions[A][1])
        self.assertEqual(decisions[B], ("skipped", "blocked: prerequisite fixture is unresolved"))
        self.assertEqual(decisions[C][0], "skipped")
        self.assertIn("ambiguous history", decisions[C][1])  # no record is not "untested"
        self.assertEqual(list(f.catalog_state()["cases"]), [A])
        self.assertEqual(f.catalog_state()["budget"]["max_service_requests"], 1)
        summary = (f.home / "campaigns" / "synthetic.catalog" / "summary.md").read_text()
        self.assertIn("## Selection (made once, before any request)", summary)

    def test_completed_uncertain_open_and_unauthorized_cases_are_not_selected(self):
        f = self.fixture(c=record(id="7a000003-0000-4000-8000-000000000004"))
        self.assertEqual(f.collect(A), collect.OK)              # completed, unchanged
        self.assertEqual(f.collect(C), collect.FAILED_VERDICT)  # open uncertain finding
        uncertain = f.home / "campaigns" / "earlier" / "state.json"
        uncertain.parent.mkdir(parents=True)
        uncertain.write_text(json.dumps({"cases": {B: {"status": "done",
                                                       "result": "uncertain_timeout"}}}))
        f.catalog({k: {"history": NEVER} for k in CASES})
        before = f.calls()
        code, out = f.run_catalog(f.authorize_catalog())
        self.assertEqual((code, f.calls()), (campaign.NOTHING_ELIGIBLE, before), out)
        decisions = f.decisions()
        self.assertIn("completed unchanged live test (passed)", decisions[A][1])
        self.assertIn("never replayed", decisions[B][1])
        self.assertIn("open finding", decisions[C][1])
        self.assertEqual(f.catalog_state()["stopped"]["reason"], "no eligible cases")
        self.assertIn("No eligible cases: nothing was requested.", out)
        g = self.fixture()
        g.catalog({k: {"history": NEVER} for k in CASES})
        auth = g.authorize_catalog(candidates={B: g.target_args(B)})
        g.run_catalog(auth)
        reasons = g.decisions()
        self.assertEqual(reasons[A], ("skipped", "not covered by the authorization's exact "
                                                 "requests"))
        self.assertEqual(reasons[B][0], "selected")
        self.assertEqual(g.calls(), 1)

    def test_ambiguous_history_and_changed_cases_are_skipped(self):
        f = self.fixture()
        old = f.home / A / "old" / "report"
        old.mkdir(parents=True)
        old.joinpath("report.json").write_text(json.dumps(
            {"mode": "live", "case": {"id": A, "version": 0, "digest": "0" * 16},
             "verdict": "passed"}))
        f.catalog({k: {"history": NEVER} for k in CASES})
        case_file = f.root / "cases" / CASES[C]
        changed = json.loads(case_file.read_text())
        changed["expectation_notes"].append("changed after approval")
        case_file.write_text(json.dumps(changed))
        f.run_catalog(f.authorize_catalog())
        decisions = f.decisions()
        self.assertIn("only for other case versions", decisions[A][1])
        self.assertEqual(decisions[B][0], "selected")
        self.assertEqual(decisions[C], ("skipped", "catalog entry invalid: the case changed "
                                                   "since the catalog was approved"))
        self.assertEqual(f.calls(), 1)

    def test_limit_budget_and_untested_before_recorded_repeats(self):
        f = self.fixture()
        self.assertEqual(f.collect(A), collect.OK)
        before = f.calls()
        f.catalog({A: {"repeat_reason": {"reason": "regression check after a fix",
                                         "evidence": "synthetic deployment note"}},
                   B: {"history": NEVER}, C: {"history": NEVER}})
        code, out = f.run_catalog(f.authorize_catalog(max_service_requests=2))
        self.assertEqual(f.catalog_state()["selection"]["queue"], [B, C], out)
        self.assertIn("selection limit of 2", f.decisions()[A][1])
        self.assertEqual((code, f.calls() - before), (campaign.OK, 2))

    def test_fresh_context_keeps_the_bound_queue_budget_and_authority(self):
        f = self.fixture()
        f.catalog({k: {"history": NEVER} for k in CASES})
        auth = f.authorize_catalog()

        def interrupted(args, out, record):
            code = collect.run(args, out, record)
            if args.case == B:
                raise KeyboardInterrupt
            return code

        with self.assertRaises(KeyboardInterrupt):
            f.run_catalog(auth, interrupted)
        bound = f.catalog_state()["selection"]
        self.assertEqual((bound["queue"], f.calls()), ([A, B, C], 2))
        code, _ = f.run_catalog(auth)  # new evidence now exists for A; no reselection
        state = f.catalog_state()
        self.assertEqual(state["selection"], bound)
        self.assertEqual([e["result"] for e in state["cases"].values()],
                         ["pass", "uncertain_interrupted", "pass"])
        self.assertEqual((code, f.calls()), (campaign.OK, 3))
        self.assertEqual(state["budget"], {"max_service_requests": 3, "reserved": 3,
                                           "issued": 2, "issued_unknown": 1})
        refused = io.StringIO()
        self.assertEqual(campaign.run(_args("run", f, f.authorize_catalog("other.json")),
                                      refused), campaign.REFUSED)
        self.assertIn("cannot extend or reset", refused.getvalue())
        self.assertEqual(f.calls(), 3)

    def test_altered_catalog_requests_or_authority_are_rejected(self):
        f = self.fixture()
        f.catalog({k: {"history": NEVER} for k in CASES})
        for override in ({"allow_subset_selection": False},
                         {"candidates": {A: ["agent", "cloud", "run", "get", "--deployment-id",
                                             "7a000021-0000-4000-8000-000000000022",
                                             "00000000-0000-4000-8000-000000000000"]}},
                         {"catalog_sha256": "0" * 64}, {"max_service_requests": 4}):
            out = io.StringIO()
            code = campaign.run(_args("run", f, f.authorize_catalog(**override)), out)
            self.assertEqual(code, campaign.REFUSED, override)
        self.assertEqual(f.calls(), 0)
        auth = f.authorize_catalog()
        f.run_catalog(auth)
        f.catalog_path.write_text(f.catalog_path.read_text().replace('"version": 1', '"version": 2', 1))
        out = io.StringIO()
        self.assertEqual(campaign.run(_args("run", f, auth), out), campaign.REFUSED)
        self.assertIn("definition changed", out.getvalue())

    def test_real_command_previews_without_writing_then_runs(self):
        f = self.fixture()
        f.catalog({A: {"history": NEVER}})
        argv = [str(f.catalog_path), "--home", str(f.home), "--root", str(f.root)]
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["campaign", "select", *argv])
        self.assertEqual((code, f.calls()), (campaign.OK, 0))
        self.assertFalse((f.home / "campaigns").exists())
        with mock.patch.dict(os.environ, {"SEREN_API_KEY": CANARY}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["campaign", "run", *argv, "--authorization",
                             str(f.authorize_catalog())])
        self.assertEqual((code, f.calls()), (campaign.OK, 1))
        self.assertNotIn(CANARY, f.saved_text())
        empty = self.fixture()
        empty.catalog()
        out = io.StringIO()
        self.assertEqual(campaign.run(_args("select", empty), out), campaign.NOTHING_ELIGIBLE)
        self.assertIn("no eligible cases", out.getvalue())
        self.assertEqual(empty.calls(), 0)


def _target_args(self, case_id):
    return json.loads((self.root / "targets" / "seren.json").read_text())["collectors"][
        case_id]["args"]


Selecting.target_args = _target_args


def _args(action, f, auth=None):
    return argparse.Namespace(action=action, definition=str(f.catalog_path),
                              authorization=str(auth) if auth else None, home=str(f.home),
                              root=str(f.root))


if __name__ == "__main__":
    unittest.main()
