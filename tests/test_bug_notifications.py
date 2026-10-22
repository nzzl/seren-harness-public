"""Owner notifications: destination binding, evidence classification and no replay."""
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from seren_harness import bug_notifications as notify, claude_live, discovery


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = discovery.Home(self.tmp.name)
        self.state = self.home.state()
        self.policy = self.home.root / 'bug-notifications.json'
        self.ledger = self.home.root / 'bug-notification-ledger.json'
        discovery.write_json(self.policy, {'enabled': True, 'guild_id': notify.GUILD,
                                          'channel_id': notify.CHANNEL})
        discovery.write_json(self.ledger, {'alerts': {}})
        self.state['findings']['T0-C99-bug'] = {'summary': 'Expected answer; actual lost answer',
                                              'disposition': 'reproduced_client_defect',
                                              'grade': 'stated'}
        self.calls = []

    def api(self, path, secret, body=None):
        self.calls.append((path, body))
        self.assertEqual(secret, 'fake-secret')
        self.assertEqual(json.loads(self.ledger.read_text())['alerts']['T0-C99-bug']['status'],
                         'attempting')
        if body is None:
            return {'id': notify.CHANNEL, 'guild_id': notify.GUILD, 'type': 0}
        self.assertEqual(body['allowed_mentions'], {'parse': []})
        return {'id': '123', 'channel_id': notify.CHANNEL}

    def deliver(self, api=None):
        notify.deliver(self.home, self.state, get_token=lambda: 'fake-secret', api=api or self.api)

    def test_new_bug_sends_once_and_preserves_evidence_boundary(self):
        self.deliver()
        self.deliver()
        self.assertEqual(len(self.calls), 2)
        message = self.calls[-1][1]['content']
        self.assertIn('production impact unverified', message)
        self.assertEqual(json.loads(self.ledger.read_text())['alerts']['T0-C99-bug']['status'], 'sent')

    def test_sections_include_recorded_impact_and_keep_limits_for_long_summary(self):
        finding = self.state['findings']['T0-C99-bug']
        finding['impact'] = 'Hypothetical: automation cannot read the result; prevalence unknown.'
        finding['summary'] = 'very long evidence ' * 300
        self.deliver()
        content = self.calls[-1][1]['content']
        self.assertLessEqual(len(content), 2000)
        self.assertIn('### Customer impact', content)
        self.assertIn(finding['impact'], content)
        self.assertIn('### Evidence & limits', content)
        self.assertIn('production impact unverified', content)
        self.assertIn('Too long to quote safely', content)
        self.assertIn('No verified workaround recorded.', content)

    def test_maximum_sections_fit_discord_without_dropping_limits(self):
        content = notify.format_notification('k' * 100, dict(
            summary='s' * 400, impact='i' * 400, workaround='w' * 200, scope='l' * 300),
            github_issue='https://github.com/example-owner/seren-harness/issues/999999999999')
        self.assertLessEqual(len(content), 2000)
        self.assertIn('l' * 300, content)

    def test_live_review_cli_accepts_impact_and_existing_workaround_option(self):
        from seren_harness.cli import parser
        args = parser().parse_args(['worker', 'live-review', '--text', 'evidence',
                                    '--customer-impact', 'Unknown exposure',
                                    '--workaround', 'Verified alternative'])
        self.assertEqual(args.customer_impact, 'Unknown exposure')
        self.assertEqual(args.workaround, 'Verified alternative')

    def test_live_format_does_not_invent_customer_impact(self):
        content = notify.format_notification('live-example', {
            'summary': 'Expected answer; observed missing answer.',
            'scope': 'One saved live execution; attribution unresolved.'})
        self.assertIn('Not separately established', content)
        self.assertIn('attribution unresolved', content)
        self.assertIn('### Expected → observed', content)

    def test_automatic_delivery_uses_exact_mapping_and_does_not_repost(self):
        policy = json.loads(self.policy.read_text())
        url = 'https://github.com/example-owner/seren-harness/issues/2'
        policy['issue_links'] = {'T0-C99-bug': url, 'unrelated':
                                'https://github.com/example-owner/seren-harness/issues/3'}
        discovery.write_json(self.policy, policy)
        self.deliver()
        self.deliver()
        self.assertEqual(len(self.calls), 2)  # one channel check and one fake POST
        content = self.calls[-1][1]['content']
        self.assertIn('[example-owner/seren-harness #2](' + url + ')', content)
        self.assertNotIn('/issues/3', content)
        self.assertIn('### Customer impact', content)
        self.assertIn('production impact unverified', content)
        self.assertEqual(json.loads(self.ledger.read_text())['alerts']['T0-C99-bug']
                         ['content'], content)

    def test_unmapped_and_invalid_links_are_never_invented_or_published(self):
        for url in (None, 'https://github.com/seren/other/issues/2',
                    'https://github.com/example-owner/seren-harness/issues/2?token=secret',
                    'https://github.com/example-owner/seren-harness/issues/2\n@everyone',
                    'https://github.com/example-owner/seren-harness/issues/0'):
            with self.subTest(url=url):
                policy = {'issue_links': {'T0-C99-bug': url}}
                self.assertIsNone(notify.issue_link(policy, 'T0-C99-bug'))
                self.assertIsNone(notify.issue_link(policy, 'other'))
        self.deliver()
        self.assertNotIn('**GitHub tracking:**', self.calls[-1][1]['content'])

    def test_adding_issue_mapping_does_not_replay_baselined_findings(self):
        discovery.write_json(self.ledger, {'alerts': {'T0-C99-bug': {'status': 'baselined'}}})
        policy = json.loads(self.policy.read_text())
        policy['issue_links'] = {'T0-C99-bug': 'https://github.com/example-owner/seren-harness/issues/1'}
        discovery.write_json(self.policy, policy)
        self.deliver()
        self.assertFalse(self.calls)

    def test_wrong_guild_never_posts(self):
        def wrong(path, secret, body=None):
            self.assertIsNone(body)
            return {'id': notify.CHANNEL, 'guild_id': 'other', 'type': 0}
        self.deliver(wrong)
        self.assertEqual(json.loads(self.ledger.read_text())['alerts']['T0-C99-bug']['status'], 'blocked')

    def test_uncertain_send_is_not_retried(self):
        def lost(path, secret, body=None):
            if body is not None:
                raise TimeoutError('private exception text')
            return self.api(path, secret)
        self.deliver(lost)
        before = len(self.calls)
        self.deliver()
        self.assertEqual(len(self.calls), before)
        record = json.loads(self.ledger.read_text())['alerts']['T0-C99-bug']
        self.assertEqual(record['status'], 'uncertain')
        self.assertNotIn('private exception text', self.ledger.read_text())

    def test_existing_findings_questions_and_interrupted_intents_do_not_send(self):
        for status in ('baselined', 'attempting', 'sent'):
            discovery.write_json(self.ledger, {'alerts': {'T0-C99-bug': {'status': status}}})
            self.deliver()
        discovery.write_json(self.ledger, {'alerts': {}})
        self.state['findings']['T0-C99-bug']['disposition'] = 'contract_question'
        self.deliver()
        self.assertFalse(self.calls)

    def test_missing_ledger_disabled_policy_and_stops_never_send(self):
        self.home.stop_path.write_text('owner stop')
        self.deliver()
        self.home.stop_path.unlink()
        self.ledger.unlink()
        with self.assertRaises(ValueError):
            self.deliver()
        self.policy.unlink()
        self.deliver()
        self.assertFalse(self.calls)

    def test_secret_patterns_and_known_bot_secret_are_redacted(self):
        self.state['findings']['T0-C99-bug']['summary'] = 'api_key=sk-abcdefghijklmnop fake-secret'
        self.deliver()
        content = self.calls[-1][1]['content']
        self.assertNotIn('sk-abcdefghijklmnop', content)
        self.assertNotIn('fake-secret', content)

    def test_live_alert_requires_explicit_classification_and_stable_key(self):
        path = self.home.root / 'claude-live-request.json'
        item = {'id': 'run-one', 'status': 'finished',
                'result': {'verdict': 'failed', 'operational_complete': True}}
        discovery.write_json(path, item)
        with self.assertRaises(ValueError):
            claude_live.review(self.home, 's', SimpleNamespace(text='evidence', status='confirmed_bug'))
        claude_live.review(self.home, 's', SimpleNamespace(text='expected vs actual; evidence',
                           status='confirmed_bug', finding=['customer-bug'],
                           summary='Expected structured answer; observed invalid answer.',
                           evidence_limits='One controlled run; cause and exposure unknown.',
                           customer_impact='A customer cannot read the answer; exposure unknown.',
                           workaround='Use the verified JSON route.'))
        found = notify.candidates(self.home, self.state)
        self.assertIn('live-customer-bug', found)
        self.assertIn('exposure unknown', found['live-customer-bug']['impact'])
        self.assertEqual(found['live-customer-bug']['workaround'], 'Use the verified JSON route.')
        item['id'] = 'run-two'
        discovery.write_json(path, item)
        claude_live.review(self.home, 's', SimpleNamespace(text='same bug again',
                           status='confirmed_bug', finding=['customer-bug'],
                           summary='Expected structured answer; observed invalid answer.',
                           evidence_limits='Two controlled runs; cause and exposure unknown.',
                           customer_impact='Potential automation failure; exposure unknown.'))
        self.assertEqual(len([k for k in notify.candidates(self.home, self.state) if k.startswith('live-')]), 1)

    def test_short_live_sections_survive_long_review_and_audit_hold_blocks_delivery(self):
        path = self.home.root / 'claude-live-request.json'
        discovery.write_json(path, {'id': 'short-test', 'status': 'finished',
                                    'result': {'verdict': 'failed', 'operational_complete': True}})
        args = SimpleNamespace(text='Full qualified evidence. ' * 100, status='confirmed_bug',
                               finding=['short-test'], summary='Expected conflict flag; observed silent resolution.',
                               customer_impact='Hypothetical incorrect collection; exposure unknown.',
                               evidence_limits='One synthetic input, 1/1 failed; model versus runtime cause unknown.')
        claude_live.review(self.home, 'reviewer', args)
        item = notify.candidates(self.home, self.state)['live-short-test']
        content = notify.format_notification('live-short-test', item)
        for value in (args.summary, args.customer_impact, args.evidence_limits):
            self.assertIn(value, content)
        self.assertNotIn('Too long', content)
        (self.home.root / 'LIVE_STOP').write_text('audit hold')
        before = (self.home.root / 'bug-notification-ledger.json').read_bytes()
        with patch.object(notify, 'token') as token:
            notify.deliver(self.home, self.state, get_token=token, api=lambda *a: self.fail('network'))
            token.assert_not_called()
        self.assertEqual((self.home.root / 'bug-notification-ledger.json').read_bytes(), before)
