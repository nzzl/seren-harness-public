"""Offline tests of Claude-to-executor handoff; no service or model calls."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from seren_harness import claude_live, discovery, live_discovery
from tests.investigation_fixture import RATIONALE, comparison, live_state


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = discovery.Home(self.tmp.name)
        self.path = self.home.root / 'claude-live-request.json'
        live_state(self.home)

    def queue(self):
        live_state(self.home)  # a real snapshot rewrites saved state after each dispatch
        with patch.object(claude_live.invoice_catalog, 'known', return_value=True), \
                patch.object(claude_live.invoice_catalog, 'case', return_value={}), \
                patch.object(claude_live.invoice_catalog, 'digest', return_value='frozen-digest'):
            claude_live.request(self.home, 'session', SimpleNamespace(
                case='frozen', rationale=RATIONALE, comparison=comparison(self.home)))

    def test_request_is_local_and_cannot_overwrite_or_replay(self):
        with patch.object(claude_live, 'execute') as execute:
            self.queue()
            execute.assert_not_called()
            with self.assertRaises(ValueError):
                self.queue()
            discovery.write_json(self.path, dict(json.loads(self.path.read_text()),
                                                 status='dispatching'))
            claude_live.dispatch(self.home, time.time() + 1000)
            execute.assert_not_called()

    def test_short_window_defers_and_completed_outcome_requires_review(self):
        self.queue()
        with patch.object(claude_live, 'execute', return_value={'verdict': 'reject'}) as execute:
            claude_live.dispatch(self.home, time.time() + 20)
            execute.assert_not_called()
            claude_live.dispatch(self.home, time.time() + 1000)
            execute.assert_called_once_with(self.home, 'frozen', RATIONALE, 'frozen-digest')
            claude_live.dispatch(self.home, time.time() + 1000)
            self.assertEqual(execute.call_count, 1)
        claude_live.review(self.home, 'next', SimpleNamespace(text='failure preserved; next gap'))
        self.assertFalse(self.path.exists())
        saved = list((self.home.root / 'live-reviews').glob('*.json'))
        self.assertEqual(json.loads(saved[0].read_text())['result']['verdict'], 'reject')

    def test_gate_refusal_preserves_budget_reason_without_retry(self):
        self.queue()
        with patch.object(claude_live, 'execute', side_effect=live_discovery.Refused('budget')):
            claude_live.dispatch(self.home, time.time() + 1000)
        self.assertEqual(json.loads(self.path.read_text())['reason'], 'budget')

    def test_uncertain_execution_blocks_review_and_replay(self):
        self.queue()
        with patch.object(claude_live, 'execute', side_effect=OSError('private')) as execute:
            claude_live.dispatch(self.home, time.time() + 1000)
            claude_live.dispatch(self.home, time.time() + 1000)
            self.assertEqual(execute.call_count, 1)
        with self.assertRaises(ValueError):
            claude_live.review(self.home, 's', SimpleNamespace(text='cannot clear'))
        self.assertNotIn('private', self.path.read_text())

    def test_successful_wrapper_dispatches_after_releasing_work_lock(self):
        from tests.test_model_worker import enabled_home, fake_claude, INIT, OK
        from seren_harness import model_worker
        home = enabled_home()
        cfg = model_worker.config(home.authority())
        def dispatch(h, deadline):
            with h.lock('work', False) as mine:
                self.assertTrue(mine)
        with patch.object(claude_live, 'dispatch', side_effect=dispatch) as send:
            outcome, _ = model_worker.run_session(home, 'test', time.time() + 1000, cfg,
                                                  popen=fake_claude([INIT, OK]))
            self.assertEqual(outcome, 'model:success')
            send.assert_called_once()
        with patch.object(claude_live, 'dispatch') as send:
            model_worker.run_session(home, 'failed', time.time() + 1000, cfg,
                                     popen=fake_claude([INIT, dict(OK, is_error=True)]))
            send.assert_not_called()

    def test_snapshot_wrapper_uses_real_checkout_gate_and_home(self):
        from pathlib import Path
        from subprocess import CompletedProcess
        repo = Path(self.tmp.name) / 'real-checkout'
        with patch.object(self.home, 'authority', return_value={'model_worker': {'repo': str(repo)}}):
            with patch.object(claude_live.subprocess, 'run', return_value=CompletedProcess(
                    [], 0, '{"verdict": "reject"}', '')) as run:
                result = claude_live.execute(self.home, 'case', 'question', 'digest')
        self.assertEqual(result['verdict'], 'reject')
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], [str(repo / '.venv/bin/python'), '-m',
                                   'seren_harness.live_discovery', 'run'])
        self.assertEqual(run.call_args.kwargs['cwd'], repo)
        self.assertEqual(run.call_args.kwargs['env']['PYTHONPATH'], str(repo))
        self.assertEqual(argv[argv.index('--home') + 1], str(self.home.root))

    def test_gate_cli_progress_does_not_corrupt_wrapper_result_or_replay(self):
        from subprocess import CompletedProcess
        self.queue()
        entry = {'id': 'attempt', 'status': 'finished', 'verdict': 'failed',
                 'calls': 15, 'reserved_calls': 31, 'cleanup_confirmed': True}

        def completed_run(*args):
            print('Answer: failed')
            print('Report: /private/report.json')
            print('Shutdown: confirmed')
            return entry

        def gate_process(argv, **kwargs):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('sys.argv', ['live_discovery', *argv[3:]]), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = live_discovery.main()
            self.assertIn('Shutdown: confirmed', stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue()), entry)
            return CompletedProcess(argv, code, stdout.getvalue(), stderr.getvalue())

        with patch.object(self.home, 'authority', return_value={
                'model_worker': {'repo': str(self.home.root)}}), \
                patch.object(claude_live.subprocess, 'run', side_effect=gate_process), \
                patch.object(live_discovery, 'execute', side_effect=completed_run) as run:
            claude_live.dispatch(self.home, time.time() + 1000)
            claude_live.dispatch(self.home, time.time() + 1000)
            run.assert_called_once()
        item = json.loads(self.path.read_text())
        self.assertEqual(item['status'], 'finished')
        self.assertEqual(item['result'], entry)
        claude_live.review(self.home, 'reviewer', SimpleNamespace(text='failed answer; preserve evidence'))
        self.assertFalse(self.path.exists())

    def batch(self):
        live_state(self.home)
        cases = ['synthetic-one', 'synthetic-two', 'synthetic-three']
        entries = [dict(case=c, rationale=RATIONALE,
                        independence='Independent rule and oracle; does not depend on another result.')
                   for c in cases]
        with patch.object(claude_live.invoice_catalog, 'known', return_value=True), \
                patch.object(claude_live.invoice_catalog, 'case', return_value={}), \
                patch.object(claude_live.invoice_catalog, 'digest', side_effect=cases):
            claude_live.request(self.home, 'session', SimpleNamespace(
                case=None, rationale=None, batch=json.dumps(entries),
                comparison=comparison(self.home)))
        return cases

    def test_batch_runs_independent_case_after_wrong_answer_and_requires_each_review(self):
        cases = self.batch()
        result = {'status': 'finished', 'verdict': 'failed', 'cleanup_confirmed': True,
                  'operational_complete': True}
        def execute(*args):
            # Intent is durable before each call; reentrant dispatch cannot replay.
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved['status'], 'dispatching')
            self.assertEqual(next(c for c in saved['items'] if c['case'] == args[1])['status'], 'dispatching')
            claude_live.dispatch(self.home, time.time() + 2000)
            return result
        with patch.object(claude_live, 'execute', side_effect=execute) as run:
            claude_live.dispatch(self.home, time.time() + 2000)
            self.assertEqual(run.call_count, 3)
            claude_live.dispatch(self.home, time.time() + 2000)
            self.assertEqual(run.call_count, 3)
        for i, case in enumerate(cases):
            claude_live.review(self.home, 'reviewer', SimpleNamespace(case=case, text='failure; next question'))
            self.assertEqual(self.path.exists(), i != 2)
        self.assertEqual(len(list((self.home.root / 'live-reviews').glob('*.json'))), 3)

    def test_batch_stops_on_uncertainty_without_replay_or_clearing_evidence(self):
        self.batch()
        with patch.object(claude_live, 'execute', side_effect=OSError('uncertain')) as run:
            claude_live.dispatch(self.home, time.time() + 2000)
            claude_live.dispatch(self.home, time.time() + 2000)
            run.assert_called_once()
        item = json.loads(self.path.read_text())
        self.assertEqual(item['status'], 'blocked')
        self.assertEqual([c['status'] for c in item['items']], ['blocked', 'not_run', 'not_run'])
        with self.assertRaises(ValueError):
            claude_live.review(self.home, 's', SimpleNamespace(case='synthetic-one', text='cannot clear'))

    def test_audit_hold_does_not_dispatch_or_consume_queued_batch(self):
        self.batch()
        before = self.path.read_bytes()
        (self.home.root / 'LIVE_STOP').write_text('audit')
        with patch.object(claude_live, 'execute') as run:
            claude_live.dispatch(self.home, time.time() + 2000)
            run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_batch_stops_on_bad_operational_result_and_mid_batch_hold(self):
        self.batch()
        with patch.object(claude_live, 'execute', return_value={
                'status': 'finished', 'verdict': 'failed', 'operational_complete': False}) as run:
            claude_live.dispatch(self.home, time.time() + 2000)
            run.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text())['items'][1]['status'], 'not_run')
        self.path.unlink()
        self.batch()
        def stop(*args):
            (self.home.root / 'LIVE_STOP').write_text('stop')
            return {'status': 'finished', 'operational_complete': True}
        with patch.object(claude_live, 'execute', side_effect=stop) as run:
            claude_live.dispatch(self.home, time.time() + 2000)
            run.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text())['items'][1]['status'], 'not_run')

    def test_batch_rejects_duplicates_and_oversize_before_queueing(self):
        entry = dict(case='synthetic-one', rationale=RATIONALE,
                     independence='Independent stated rules, not a dependent follow-up investigation.')
        for entries in ([entry] * 5, [entry] * 2):
            with patch.object(claude_live.invoice_catalog, 'known', return_value=True), \
                    patch.object(claude_live.invoice_catalog, 'case', return_value={}), \
                    patch.object(claude_live.invoice_catalog, 'digest', return_value='same'):
                with self.assertRaises(ValueError):
                    claude_live.request(self.home, 's', SimpleNamespace(
                        case=None, rationale=None, batch=json.dumps(entries)))
            self.assertFalse(self.path.exists())

    def test_scheduler_cannot_launch_between_batch_members(self):
        with self.home.lock('live-handoff'):
            with patch.object(discovery, '_launch_model') as launch:
                self.assertEqual(discovery.supervise(self.home), 'busy')
                launch.assert_not_called()

    def test_batch_refusal_and_deadline_require_review_before_new_request(self):
        self.batch()
        with patch.object(claude_live, 'execute', side_effect=live_discovery.Refused('budget')) as run:
            claude_live.dispatch(self.home, time.time() + 2000)
            run.assert_called_once()
        self.assertEqual([c['status'] for c in json.loads(self.path.read_text())['items']],
                         ['refused', 'not_run', 'not_run'])
        self.path.unlink()
        self.batch()
        with patch.object(claude_live.time, 'time', side_effect=[0, 1000]), \
                patch.object(claude_live, 'execute', return_value={
                    'status': 'finished', 'operational_complete': True}) as run:
            claude_live.dispatch(self.home, 1000)
            run.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text())['items'][1]['status'], 'not_run')
