"""Private owner reporting with fake GitHub and Discord transports only."""
import tempfile
import unittest
from unittest.mock import patch
from seren_harness import private_issues as issues, bug_notifications as notify, discovery


class PrivateIssuesTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(issues, 'POLICY', dict(issues.POLICY, enabled=True))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = discovery.Home(self.tmp.name)
        self.policy = {'enabled': True, 'guild_id': notify.GUILD, 'channel_id': notify.CHANNEL,
                       'private_issues': dict(issues.POLICY)}
        discovery.write_json(self.home.root/'bug-notifications.json', self.policy)
        discovery.write_json(self.home.root/'private-issue-ledger.json', {'issues': {}})
        discovery.write_json(self.home.root/'bug-notification-ledger.json', {'alerts': {}})
        self.state = self.home.state()
        self.state['findings']['test'] = {'summary': 'Expected A, observed B',
            'impact': 'Hypothetical customer cost; exposure unknown.',
            'grade': 'stated', 'disposition': 'reproduced_client_defect'}
        self.calls = []
        self.saved = []
        self.private = True
        self.url = 'https://github.com/example-owner/seren-harness/issues/4'

    def api(self, path, body=None):
        self.calls.append((path, body))
        if path == 'repos/example-owner/seren-harness':
            return {'full_name': issues.REPO, 'id': issues.REPO_ID, 'private': self.private}
        if body:
            ledger = discovery.read_json(self.home.root/'private-issue-ledger.json')
            self.assertEqual('attempting', ledger['issues']['test']['status'])
            self.saved.append(dict(body, html_url=self.url))
            return self.saved[-1]
        if '/issues?' in path:
            return self.saved
        return self.saved[-1]

    def ensure(self, api=None):
        return issues.ensure_issue(self.home, self.policy, 'test',
                                   notify.candidates(self.home, self.state)['test'], api or self.api)

    def test_create_once_reuse_mapping_and_preserve_qualifications(self):
        self.assertEqual(self.url, self.ensure())
        self.assertEqual(self.url, self.ensure())
        self.assertEqual(1, len(self.saved))
        self.assertIn('exposure unknown', self.saved[0]['body'])
        self.assertIn('production impact unverified', self.saved[0]['body'])
        self.assertEqual(self.url, self.policy['issue_links']['test'])

    def test_report_layout_keeps_limits_visible_and_dense_evidence_collapsed(self):
        evidence = 'Recorded balance is 8,765,975.32. ' * 40 + '</details>PRIVATE-END'
        item = {'summary': 'Expected 8,641,975.32; observed 8,765,975.32.',
                'impact': 'Synthetic; no actual loss.',
                'scope': 'One failure; shorter control passed. Cause unknown.',
                'evidence': evidence}
        body = issues.issue_body('live-live-clock-subtraction', item)
        self.assertLess(body.index('Cause unknown.'), body.index('<details>'))
        self.assertLess(body.index('## Workaround'), body.index('<details>'))
        details = body.split('<summary>Detailed evidence and reproduction notes</summary>')[1]
        self.assertEqual(40, details.count('8,765,975.32'))
        self.assertIn('&lt;/details&gt;PRIVATE-END', details)
        self.assertEqual(1, body.count('</details>'))
        self.assertTrue(all(len(p) <= 500 for p in details.split('</details>')[0].strip().split('\n\n')))
        self.assertEqual('Clock subtraction', issues.issue_title('live-live-clock-subtraction'))
        self.assertIn(issues.marker('live-live-clock-subtraction'), body)

    def test_public_or_wrong_destination_never_creates(self):
        self.private = False
        with self.assertRaises(ValueError):
            self.ensure()
        self.assertFalse(self.saved)
        self.policy['private_issues']['repository'] = 'seren/upstream'
        self.calls.clear()
        with self.assertRaises(ValueError):
            self.ensure()
        self.assertFalse(self.calls)

    def test_lost_receipt_reconciles_marker_without_second_post(self):
        def lost(path, body=None):
            result = self.api(path, body)
            if body:
                raise TimeoutError('secret')
            return result
        with self.assertRaises(ValueError):
            self.ensure(lost)
        self.assertEqual(self.url, self.ensure())
        self.assertEqual(1, len(self.saved))
        self.assertNotIn('secret', (self.home.root/'private-issue-ledger.json').read_text())

    def test_uncertain_absent_issue_never_reposts(self):
        discovery.write_json(self.home.root/'private-issue-ledger.json',
                             {'issues': {'test': {'status': 'attempting'}}})
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.ensure()
        self.assertFalse(self.saved)

    def test_missing_ledger_and_incomplete_search_refuse(self):
        (self.home.root/'private-issue-ledger.json').unlink()
        with self.assertRaises(ValueError):
            self.ensure()
        self.assertFalse(self.calls)
        discovery.write_json(self.home.root/'private-issue-ledger.json', {'issues': {}})
        def many(path, body=None):
            return [{}]*100 if '/issues?' in path else self.api(path, body)
        with self.assertRaises(ValueError):
            self.ensure(many)
        self.assertFalse(self.saved)

    def test_real_delivery_orders_issue_before_linked_discord_and_never_duplicates(self):
        messages = []
        def discord(path, secret, body=None):
            if body:
                self.assertEqual(len(self.saved), 1)
                self.assertIn(self.url, body['content'])
                messages.append(body)
                return {'id': '123', 'channel_id': notify.CHANNEL}
            return {'id': notify.CHANNEL, 'guild_id': notify.GUILD, 'type': 0}
        for _ in range(2):
            notify.deliver(self.home, self.state, get_token=lambda: 'fake', api=discord, issue_api=self.api)
        self.assertEqual(1, len(messages))
        self.assertEqual(1, len(self.saved))

    def test_issue_failure_and_audit_hold_never_send_linkless_alert(self):
        self.private = False
        def forbidden(*args):
            self.fail('Discord called')
        notify.deliver(self.home, self.state, get_token=forbidden, api=forbidden, issue_api=self.api)
        self.assertEqual({}, discovery.read_json(self.home.root/'bug-notification-ledger.json')['alerts'])
        self.assertEqual('issue_blocked', self.state['bug_notification_status']['status'])
        (self.home.root/'LIVE_STOP').write_text('audit')
        notify.deliver(self.home, self.state, get_token=forbidden, api=forbidden, issue_api=forbidden)

    def test_latest_review_wins_over_uuid_filename_order(self):
        folder = self.home.root/'live-reviews'
        folder.mkdir()
        for name, stamp, review in [('z', '2026-10-16T01:00:00Z', 'old'),
                                    ('a', '2026-10-16T02:00:00Z', 'new')]:
            discovery.write_json(folder/(name+'.json'), {'id': name, 'reviewed_at': stamp,
                'notification_key': 'same', 'notification_classification': 'confirmed_bug', 'review': review})
        self.assertEqual('new', notify.candidates(self.home, self.state)['live-same']['evidence'])


class HandoverDisclosureDefaults(unittest.TestCase):
    def test_disabled_policy_refuses_before_any_api_call(self):
        calls = []
        with patch.object(issues, 'POLICY', dict(issues.POLICY, enabled=False)):
            with self.assertRaises(ValueError):
                issues.ensure_issue(None, {'private_issues': dict(issues.POLICY)}, 'x', {},
                                    lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])
