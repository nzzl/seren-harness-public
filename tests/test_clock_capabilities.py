"""Offline controls, representation differences and secret-safe diagnostic evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seren_support import SOURCE
from seren_harness.clock_capabilities import APPROVED, compare_capabilities, require_capabilities
from seren_harness.sanitize import Sanitizer


def expanded():
    """Schema-valid synthetic expansion, NOT recovered live values/defaults."""
    value = copy.deepcopy(APPROVED)
    value['browser'].update(profile='minimal', display=None, timezone=None,
                            max_session_seconds=None)
    value['code_execution']['sandbox'] = 'disabled'
    value['skills'].update(max_injected_chars=None,
                           selection={'mode': 'auto', 'include_tags': [],
                                      'exclude_tags': [], 'max_selected': 0})
    value.update(audio=None, realtime_sessions=None, tool_error_recovery=None)
    return value


class ClockControlTests(unittest.TestCase):
    def test_authored_policy_and_documented_inactive_fields_pass(self):
        draft = json.loads((SOURCE / 'docs/system-time/unbound-onboarding-draft.json').read_text())
        self.assertEqual(draft['deployment']['capability_policy'], APPROVED)
        self.assertTrue(compare_capabilities(APPROVED, APPROVED)['compatible'])
        result = compare_capabilities(APPROVED, expanded())
        self.assertTrue(result['compatible'])
        self.assertTrue(result['differences'])
        self.assertTrue(all(d['accepted'] for d in result['differences']))

    def test_every_authored_flag_must_remain_explicitly_false(self):
        for group, flags in APPROVED.items():
            for flag in flags:
                for mutation in (True, None, 0, 'false', 'missing'):
                    observed = expanded()
                    if mutation == 'missing':
                        del observed[group][flag]
                    else:
                        observed[group][flag] = mutation
                    with self.subTest(group=group, flag=flag, mutation=mutation):
                        self.assertFalse(compare_capabilities(APPROVED, observed)['compatible'])

    def test_missing_groups_new_controls_and_malformed_objects_block(self):
        for observed in (None, [], {}, '[redacted]'):
            self.assertFalse(compare_capabilities(APPROVED, observed)['compatible'])
        for group in APPROVED:
            observed = expanded()
            del observed[group]
            self.assertFalse(compare_capabilities(APPROVED, observed)['compatible'])
        for group in ('audio', 'realtime_sessions', 'tool_error_recovery', 'new_feature'):
            observed = expanded()
            observed[group] = {'enabled': False}
            self.assertFalse(compare_capabilities(APPROVED, observed)['compatible'])

    def test_unknown_fields_invalid_options_and_changed_authored_controls_block(self):
        variants = [('browser', 'profile', 'future'), ('browser', 'enabled', 0),
                    ('browser', 'max_session_seconds', True),
                    ('browser', 'max_session_seconds', 3601),
                    ('browser', 'timezone', 'some text'),
                    ('skills', 'selection', {'mode': 'future'}),
                    ('skills', 'selection', {'include_tags': ['extra']}),
                    ('skills', 'selection', {'max_selected': True}),
                    ('skills', 'selection', {'unknown': False}),
                    ('code_execution', 'unknown', False)]
        for group, key, value in variants:
            observed = expanded()
            observed[group][key] = value
            self.assertFalse(compare_capabilities(APPROVED, observed)['compatible'])
        authored = copy.deepcopy(APPROVED)
        authored['browser']['enabled'] = True
        self.assertFalse(compare_capabilities(authored, expanded())['compatible'])

    def test_diagnostics_saved_before_refusal_without_raw_names_or_secrets(self):
        secret = 'short-secret-without-a-recognized-prefix'
        observed = expanded()
        observed[secret] = {'url': 'https://example.test/' + secret}
        observed['browser']['timezone'] = secret
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch('socket.socket', side_effect=AssertionError('network')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('process')):
            path = Path(tmp) / 'controls.json'
            with self.assertRaisesRegex(ValueError, 'controls not verified'):
                require_capabilities(APPROVED, observed, path)
            text = path.read_text()
            self.assertNotIn(secret, text)
            self.assertNotIn('https://', text)
            self.assertIn('browser.timezone', text)
            self.assertIn('policy.[unknown]', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            require_capabilities(APPROVED, expanded(), path)
            self.assertTrue(json.loads(path.read_text())['compatible'])
        self.assertEqual(Sanitizer().value({'capability_policy': observed}),
                         {'capability_policy': '[redacted]'})

    def test_named_controls_and_safe_enum_values_remain_diagnostic(self):
        observed = expanded()
        observed['skills']['execute_scripts'] = True
        result = compare_capabilities(APPROVED, observed)
        change = next(d for d in result['differences'] if d['field'] == 'skills.execute_scripts')
        self.assertIs(change['observed'], True)
        self.assertIs(change['expected'], False)
        self.assertFalse(change['accepted'])
        self.assertIn('minimal', json.dumps(result))
