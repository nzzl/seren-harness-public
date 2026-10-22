"""Standing live authority is tested with local stores and fake executors only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from seren_harness import discovery, live_discovery as live, employee_run
from seren_harness.state import Store
from tests.investigation_fixture import RATIONALE, comparison


class LiveDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = discovery.Home(self.root / 'discovery')
        self.store = Store(self.root / 'state')
        self.addCleanup(self.store.close)
        self.session = {'state_dir': str(self.root / 'state'), 'home': str(self.root / 'runs'),
                        'runtime_remaining_seconds': 300,
                        'baseline': {'deployment_id': live.TARGET,
                                     'name': employee_run.CLOCK_NAME}}
        self.ledger = {'version': 1, 'attempts': []}
        self.stamp = '2026-10-15T18:00:00+00:00'  # 16 Oct UTC+07:00
        (self.home.root / 'live-authority.json').write_text(json.dumps({
            'approved': True, 'target': live.TARGET, 'runs_per_day': 10,
            'calls_per_day': 310, 'timezone': 'Etc/GMT-7', 'no_monetary_cap': True,
            'session': self.session}))
        (self.home.root / 'live-ledger.json').write_text(json.dumps(self.ledger))

    def test_manual_runs_and_reservations_share_budget_without_double_count(self):
        for i in range(3):
            rid = self.store.create_run('employee_supplied_answer', 'clock:' + live.TARGET,
                                        'live', str(i), 'test')
            self.store.db.execute('UPDATE runs SET created_at=? WHERE id=?', (self.stamp, rid))
        self.ledger['attempts'] = [{'id': '0', 'day': '2026-10-16', 'status': 'finished'}]
        status = live.usage(self.session, self.ledger, self.stamp, 10, 310)
        self.assertEqual(status['runs_used'], 3)
        self.assertEqual(status['calls_reserved'], 93)
        self.assertEqual(status['runs_remaining'], 7)
        self.assertEqual(status['calls_remaining'], 217)
        old = live.usage(self.session, self.ledger, self.stamp, 3, 93)
        self.assertEqual(old['runs_remaining'], 0)
        self.assertEqual(old['runs_used'], status['runs_used'])

    def test_midnight_does_not_allow_cross_day_reservations(self):
        with self.assertRaises(live.Refused):
            live.reserve(self.ledger, {'runs_remaining': 3}, 'case', 'question',
                         '2026-10-16T16:56:00+00:00')
        self.assertEqual(self.ledger['attempts'], [])
        self.assertEqual(live.day(self.stamp), '2026-10-16')

    def test_interruption_never_replays_even_next_day(self):
        live.reserve(self.ledger, {'runs_remaining': 3}, 'case', 'question', self.stamp)
        with self.assertRaises(live.Refused):
            live.gates(self.store, self.ledger)
        self.assertEqual(live.usage(self.session, self.ledger,
                                   '2026-10-16T18:00:00Z', 10, 310)['runs_remaining'], 10)
        with self.assertRaises(live.Refused):
            live.gates(self.store, self.ledger)

    def test_cleanup_blocks_without_any_cli(self):
        rid = self.store.create_run('employee_supplied_answer', 'clock:' + live.TARGET,
                                   'live', 'old', 'test')
        self.store.add_cleanup(rid, 'clock:' + live.TARGET, 'stop_runtime', live.TARGET)
        with patch.object(live, 'validate_source', return_value='checked'), \
                patch.object(employee_run, 'pinned_cli') as cli:
            with self.assertRaises(live.Refused):
                live.execute(self.home, 'bd-complete-control', RATIONALE)
            cli.assert_not_called()

    def test_stop_and_worker_lock_prevent_execution(self):
        with patch.object(employee_run, 'pinned_cli') as cli:
            self.home.stop_path.write_text('owner stop')
            with self.assertRaises(live.Refused):
                live.execute(self.home, 'bd-complete-control', RATIONALE)
            self.home.stop_path.unlink()
            with self.home.lock('work'):
                with self.assertRaises(live.Refused):
                    live.execute(self.home, 'bd-complete-control', RATIONALE)
            cli.assert_not_called()

    def test_call_is_journaled_before_send_and_limit_cannot_be_exceeded(self):
        entry = {'calls': 0}
        persisted = []
        for _ in range(31):
            live.count_call(entry, lambda: persisted.append(entry['calls']))
        with self.assertRaises(employee_run.Halt):
            live.count_call(entry, lambda: persisted.append(entry['calls']))
        self.assertEqual(persisted, list(range(1, 32)))

    def test_target_and_limits_cannot_expand(self):
        p = self.home.root / 'live-authority.json'
        a = json.loads(p.read_text())
        a['calls_per_day'] = 3101
        p.write_text(json.dumps(a))
        with self.assertRaises(live.Refused):
            live.load_authority(self.home)

    def test_daily_exhaustion_refuses_before_version_or_service_call(self):
        ledger = {'version': 1, 'attempts': [
            {'id': str(i), 'day': live.day(self.stamp), 'status': 'finished'} for i in range(10)]}
        (self.home.root / 'live-ledger.json').write_text(json.dumps(ledger))
        with patch.object(live, 'validate_source', return_value='checked'), \
                patch.object(live, 'utc_now', return_value=self.stamp), \
                patch.object(employee_run, 'pinned_cli') as cli:
            with self.assertRaises(live.Refused):
                live.execute(self.home, 'bd-complete-control', RATIONALE)
            cli.assert_not_called()

    def test_failed_executor_leaves_durable_blocker_and_no_replay(self):
        with patch.object(live, 'validate_source', return_value='checked'), \
                patch.object(live, 'utc_now', return_value=self.stamp), \
                patch.object(employee_run, 'validate'), \
                patch.object(employee_run, 'pinned_cli', side_effect=ValueError('bad pin')) as cli:
            with self.assertRaises(ValueError):
                live.execute(self.home, 'bd-complete-control', RATIONALE)
            ledger = live.read(self.home.root / 'live-ledger.json')
            self.assertEqual(ledger['attempts'][0]['status'], 'blocked')
            with self.assertRaises(live.Refused):
                live.execute(self.home, 'bd-complete-control', RATIONALE)
            self.assertEqual(cli.call_count, 1)

    def test_success_uses_existing_driver_and_persists_calls_and_report(self):
        class Cli:
            def _execute(self, args, parse=False):
                return 'saved fake reply'

        class Driver:
            def __init__(self, session, case, cli, store):
                self.session, self.cli, self.store = session, cli, store

            def run(self):
                self.run_id = self.store.create_run('employee_supplied_answer',
                    'clock:' + live.TARGET, 'live', self.session['authorization_id'], 'test')
                for _ in range(3):
                    self.cli._execute(['agent', 'cloud', 'deployment', 'status', live.TARGET])
                folder = Path(self.session['home']) / self.run_id / 'report'
                folder.mkdir(parents=True)
                (folder / 'report.json').write_text(json.dumps({'verdict': 'failed', 'criteria': [
                    {'id': 'run_identity', 'verdict': 'passed'},
                    {'id': 'no_tool_activity', 'verdict': 'passed'},
                    {'id': 'answer', 'verdict': 'failed'},
                    {'id': 'shutdown', 'verdict': 'passed', 'evidence': {
                        'status': 'stopped', 'desired_lifecycle_state': 'stopped'}}]}))

        with patch.object(live, 'validate_source', return_value='checked'), \
                patch.object(live, 'utc_now', return_value=self.stamp), \
                patch.object(employee_run, 'validate'), \
                patch.object(employee_run, 'pinned_cli', return_value=Cli()), \
                patch.object(employee_run, 'ClockRun', Driver):
            result = live.execute(self.home, 'bd-complete-control', RATIONALE)
        self.assertEqual(result['status'], 'finished')
        self.assertEqual(result['verdict'], 'failed')
        self.assertEqual(result['calls'], 3)
        ledger = live.read(self.home.root / 'live-ledger.json')
        self.assertEqual(ledger['attempts'][0]['id'], result['id'])
        self.assertTrue(Path(result['report']).is_file())

    def test_changed_case_digest_refuses_before_any_cli(self):
        with patch.object(live, 'validate_source', return_value='checked'), \
                patch.object(employee_run, 'pinned_cli') as cli:
            with self.assertRaisesRegex(live.Refused, 'oracle changed'):
                live.execute(self.home, 'payment-revisions', RATIONALE, 'stale-digest')
            cli.assert_not_called()
        self.assertEqual(live.read(self.home.root / 'live-ledger.json')['attempts'], [])

    def test_eleventh_attempt_refused_and_day_change_preserves_history(self):
        self.ledger['attempts'] = [dict(id=str(i), day=live.day(self.stamp), status='finished')
                                   for i in range(10)]
        before = json.dumps(self.ledger)
        status = live.usage(self.session, self.ledger, self.stamp, 10, 310)
        self.assertEqual(status['calls_reserved'], 310)
        with self.assertRaises(live.Refused):
            live.reserve(self.ledger, status, 'case', RATIONALE, self.stamp)
        self.assertEqual(json.dumps(self.ledger), before)
        self.assertEqual(live.usage(self.session, self.ledger, '2026-10-16T18:00:00Z', 10, 310)
                         ['runs_remaining'], 10)
        self.assertEqual(json.dumps(self.ledger), before)

    def test_source_check_rejects_unvalidated_tree(self):
        with patch.object(live, 'read', return_value={'tree': 'old', 'result': 'pass'}), \
                patch.object(live.subprocess, 'check_output',
                             side_effect=['', '.git', 'new']):
            with self.assertRaises(live.Refused):
                live.validate_source()

    def finished(self, ident, calls=15, **changes):
        return dict(dict(id=ident, day=live.day(self.stamp), status='finished', calls=calls,
                         cleanup_confirmed=True, ended_at=self.stamp, report='saved-report'), **changes)

    def test_completed_calls_settle_but_legacy_and_uncertain_reservations_remain(self):
        entries = [self.finished('done'), self.finished('failed-answer', calls=17),
                   self.finished('uncertain', calls=2, status='blocked'),
                   self.finished('not-stopped', calls=2, cleanup_confirmed=False),
                   {'id': 'legacy', 'day': live.day(self.stamp), 'status': 'finished'}]
        self.ledger['attempts'] = entries
        before = json.dumps(self.ledger)
        status = live.usage(self.session, self.ledger, self.stamp)
        self.assertEqual((status['calls_used'], status['calls_reserved']), (32, 93))
        self.assertEqual(status['calls_remaining'], 2975)
        self.assertEqual(status['runs_used'], 5)
        self.assertEqual(json.dumps(self.ledger), before)
        with self.assertRaises(live.Refused):
            live.gates(self.store, self.ledger)
        # UTC+07:00 rollover resets eligibility, never history or unresolved gates.
        tomorrow = live.usage(self.session, self.ledger, '2026-10-16T18:00:00Z')
        self.assertEqual(tomorrow['calls_remaining'], 3100)
        self.assertEqual(json.dumps(self.ledger), before)
        with self.assertRaises(live.Refused):
            live.gates(self.store, self.ledger)

    def test_independent_call_ceiling_and_attempt_safeguard(self):
        self.ledger['attempts'] = [self.finished(str(i), calls=31) for i in range(100)]
        status = live.usage(self.session, self.ledger, self.stamp)
        self.assertEqual(status['attempts_remaining'], 100)
        self.assertEqual(status['runs_remaining'], 0)
        with self.assertRaises(live.Refused):
            live.reserve(self.ledger, status, 'case', RATIONALE, self.stamp)
        self.ledger['attempts'] = [self.finished(str(i), calls=1) for i in range(200)]
        status = live.usage(self.session, self.ledger, self.stamp)
        self.assertEqual(status['calls_remaining'], 2900)
        self.assertEqual(status['runs_remaining'], 0)
        with self.assertRaises(live.Refused):
            live.reserve(self.ledger, status, 'case', RATIONALE, self.stamp)

    def test_audit_hold_blocks_before_any_call_and_authority_ceiling_is_independent(self):
        p = self.home.root / 'live-authority.json'
        a = json.loads(p.read_text())
        a.update(runs_per_day=200, calls_per_day=3100)
        p.write_text(json.dumps(a))
        self.assertEqual(live.load_authority(self.home)['runs_per_day'], 200)
        (self.home.root / 'LIVE_STOP').write_text('audit')
        with patch.object(employee_run, 'pinned_cli') as cli:
            with self.assertRaisesRegex(live.Refused, 'stop switch'):
                live.execute(self.home, 'payment-revisions', RATIONALE)
            cli.assert_not_called()
        a['runs_per_day'] = 201
        p.write_text(json.dumps(a))
        with self.assertRaises(live.Refused):
            live.load_authority(self.home)

    def test_missing_final_evidence_and_invalid_counts_never_release_reservations(self):
        self.ledger['attempts'] = [self.finished('a', ended_at=None),
                                   self.finished('b', report=None)]
        self.assertEqual(live.usage(self.session, self.ledger, self.stamp)['calls_reserved'], 62)
        self.ledger['attempts'] = [self.finished('bad', calls=-1)]
        with self.assertRaises(live.Refused):
            live.usage(self.session, self.ledger, self.stamp)
        self.ledger['attempts'] = [self.finished('same'), self.finished('same')]
        with self.assertRaises(live.Refused):
            live.usage(self.session, self.ledger, self.stamp)

    def test_tool_activity_or_missing_shutdown_is_not_ordinary_answer_failure(self):
        report = {'criteria': [{'id': 'run_identity', 'verdict': 'passed'},
                              {'id': 'no_tool_activity', 'verdict': 'passed'},
                              {'id': 'answer', 'verdict': 'failed'},
                              {'id': 'shutdown', 'verdict': 'passed', 'evidence': {
                                  'status': 'stopped', 'desired_lifecycle_state': 'stopped'}}]}
        self.assertTrue(live.operational_complete(report))
        report['criteria'][1]['verdict'] = 'failed'
        self.assertFalse(live.operational_complete(report))
        self.assertFalse(live.operational_complete({'criteria': []}))

    def test_batch_through_real_gate_settles_calls_and_retains_each_failed_answer(self):
        import time
        from types import SimpleNamespace
        from seren_harness import claude_live
        class Cli:
            def _execute(self, args, parse=False):
                return 'fake result'
        class Driver:
            def __init__(self, session, case, cli, store):
                self.session, self.cli, self.store = session, cli, store
            def run(self):
                self.run_id = self.store.create_run('employee_supplied_answer',
                    'clock:' + live.TARGET, 'live', self.session['authorization_id'], 'fake')
                self.store.db.execute('UPDATE runs SET created_at=? WHERE id=?',
                                      ('2026-10-15T18:00:00+00:00', self.run_id))
                for _ in range(3):
                    self.cli._execute(['fake'])
                folder = Path(self.session['home']) / self.run_id / 'report'
                folder.mkdir(parents=True)
                (folder / 'report.json').write_text(json.dumps({'verdict': 'failed', 'criteria': [
                    {'id': 'run_identity', 'verdict': 'passed'},
                    {'id': 'no_tool_activity', 'verdict': 'passed'},
                    {'id': 'answer', 'verdict': 'failed'},
                    {'id': 'shutdown', 'verdict': 'passed', 'evidence': {
                        'status': 'stopped', 'desired_lifecycle_state': 'stopped'}}]}))
        batch = [dict(case=c, rationale=RATIONALE,
                      independence='Independent case with its own oracle and decision; no outcome dependency.')
                 for c in ['payment-revisions', 'missing-fields']]
        authority = json.loads((self.home.root / 'live-authority.json').read_text())
        authority['session']['execution_scope'] = 'supplied-answer-finance-v1'
        (self.home.root / 'live-authority.json').write_text(json.dumps(authority))
        (self.home.root / 'claude-live-status.json').write_text(json.dumps({'runs_remaining': 9}))
        claude_live.request(self.home, 'test', SimpleNamespace(
            case=None, rationale=None, batch=json.dumps(batch), comparison=comparison(self.home)))
        with patch.object(live, 'validate_source', return_value='checked'), \
                patch.object(live, 'utc_now', return_value=self.stamp), \
                patch.object(employee_run, 'validate'), \
                patch.object(employee_run, 'pinned_cli', side_effect=lambda *a: Cli()), \
                patch.object(employee_run, 'ClockRun', Driver), \
                patch.object(claude_live, 'execute', side_effect=live.execute):
            claude_live.dispatch(self.home, time.time() + 2000)
            claude_live.dispatch(self.home, time.time() + 2000)
        ledger = live.read(self.home.root / 'live-ledger.json')
        self.assertEqual(len(ledger['attempts']), 2)
        self.assertTrue(all(a['verdict'] == 'failed' and a['cleanup_confirmed'] for a in ledger['attempts']))
        status = live.usage(self.session, ledger, self.stamp, 200, 3100)
        self.assertEqual((status['runs_used'], status['calls_used'], status['calls_reserved']), (2, 6, 0))
        self.assertEqual(status['calls_remaining'], 3094)


if __name__ == '__main__':
    unittest.main()
