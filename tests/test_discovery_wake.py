"""Actionable wake decisions with fake launches, no model or service calls."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from seren_harness import discovery, model_worker
from tests.test_model_worker import enabled_home


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.home = enabled_home()
        self.repo = Path(self.home.authority()['model_worker']['repo'])
        self.launched = []

    def wait(self, mode='change', hours=24):
        model_worker.verb_wait(self.home, self.repo, 'prior', SimpleNamespace(
            hours=str(hours), reason='no new eligible customer question', wake_on=mode))

    def tick(self):
        def launch(*args):
            self.launched.append(args)
            return 99999999
        return discovery.supervise(self.home, due=lambda *a: {'due': [], 'refused': []},
                                   model_launch=launch, repo_dirty=lambda *a: {})

    def test_unrelated_commits_and_expired_change_wait_do_not_spend_sessions(self):
        self.wait()
        for index in range(3):
            (self.repo / 'other.txt').write_text('new report %d' % index)
            subprocess.run(['git', '-C', str(self.repo), 'add', 'other.txt'], check=True)
            subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t',
                            '-c', 'user.email=t@t', 'commit', '-qm', 'Report only'], check=True)
            with patch.object(discovery, '_head', return_value=str(index)):
                self.assertEqual(self.tick(), 'waiting')
        with patch.object(discovery, 'now', return_value=discovery.now() + discovery.timedelta(days=2)):
            self.assertEqual(self.tick(), 'waiting')
        self.assertFalse(self.launched)
        self.assertEqual(self.home.state()['sessions'], [])

    def test_repeated_gated_blockers_and_generic_checkpoint_updates_do_not_wake(self):
        self.wait()
        with self.home.edit() as state:
            state['followups'] = {'F-old': {'status': 'open', 'needs': 'owner_approval',
                                           'problem': 'known unavailable evidence'}}
        discovery.write_json(self.home.root / 'investigation-checkpoint.json',
                             {'last_verified_at': discovery.iso(), 'status': 'still blocked'})
        self.assertEqual(self.tick(), 'waiting')
        self.assertFalse(self.launched)

    def test_new_eligible_followup_wakes_and_records_reason(self):
        self.wait()
        with self.home.edit() as state:
            state['followups'] = {'F-new': {'status': 'open', 'needs': 'tier0',
                                           'problem': 'different customer question'}}
        self.assertEqual(self.tick(), 'launched')
        self.assertIn('followup:F-new', self.home.state()['model_wake_reason'])
        self.assertEqual(len(self.launched), 1)

    def test_owner_direction_and_completed_evidence_each_wake(self):
        for signal in ('owner', 'result'):
            with self.subTest(signal=signal):
                self.home = enabled_home()
                self.repo = Path(self.home.authority()['model_worker']['repo'])
                self.wait()
                if signal == 'owner':
                    discovery.write_json(self.home.root / 'investigation-checkpoint.json',
                                         {'owner_direction': 'Investigate a different question'})
                else:
                    with self.home.edit() as state:
                        state['cases']['C99'] = {'status': 'done', 'last_attempt': 'new'}
                self.assertEqual(self.tick(), 'launched')

    def test_explicit_timer_wakes_only_when_due(self):
        self.wait(mode='time', hours=1)
        self.assertEqual(self.tick(), 'waiting')
        with patch.object(discovery, 'now', return_value=discovery.now() + discovery.timedelta(hours=2)):
            self.assertEqual(self.tick(), 'launched')
        self.assertIn('timed wait', self.home.state()['model_wake_reason'])

    def test_legacy_head_wait_migrates_without_a_launch(self):
        with self.home.edit() as state:
            state.setdefault('model', {})['wait'] = {
                'head': 'old', 'until': discovery.iso(discovery.now() - discovery.timedelta(days=1)),
                'reason': 'no eligible question'}
        self.assertEqual(self.tick(), 'waiting')
        self.assertIn('signals', self.home.state()['model']['wait'])
        self.assertFalse(self.launched)

    def test_pending_owner_direction_survives_daily_limit_without_reset(self):
        self.wait()
        with self.home.edit() as state:
            state['sessions'] = [{'id': str(i), 'kind': 'model', 'started_at': discovery.iso()}
                                 for i in range(24)]
        discovery.write_json(self.home.root / 'investigation-checkpoint.json',
                             {'owner_direction': 'Move to independent work'})
        self.assertEqual(self.tick(), 'limited')
        self.assertEqual(len(self.home.state()['sessions']), 24)
        with patch.object(discovery, 'now', return_value=discovery.now() + discovery.timedelta(days=1)):
            self.assertEqual(self.tick(), 'launched')
        self.assertEqual(len(self.home.state()['sessions']), 25)
        self.assertEqual(self.home.authority()['model_worker']['limits']['sessions_per_day'], 24)

    def test_live_handoff_wakes_but_unchanged_blocked_handoff_stays_asleep(self):
        self.wait()
        path = self.home.root / 'claude-live-request.json'
        discovery.write_json(path, {'id': 'one', 'status': 'blocked', 'reason': 'uncertain'})
        self.assertEqual(self.tick(), 'launched')
        # A following session has reviewed the blocker and requested a wait.
        with self.home.edit() as state:
            state['model']['wait'] = {'until': discovery.iso(), 'reason': 'unchanged blocker',
                                      'signals': discovery.model_wake_signals(
                                          self.home, state, self.home.authority())}
        worker = json.loads((self.home.root / 'worker.json').read_text())
        worker.update(status='ended')
        discovery.write_json(self.home.root / 'worker.json', worker)
        self.assertEqual(self.tick(), 'waiting')
        self.assertEqual(len(self.launched), 1)
