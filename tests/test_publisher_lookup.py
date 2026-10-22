"""Fixed publisher lookup with synthetic replies; no service or credential access."""
import copy
import hashlib
import io
import json
import unittest
from unittest import mock

from seren_support import SOURCE
from seren_harness import publisher_lookup as lookup
from seren_harness import employee_tool
from seren_harness.employee_run import validate, ClockRun
from seren_harness.cli import main
import test_employee_run as clock_tests

CASE = json.loads((SOURCE / 'cases/seren-employee-publisher-lookup.json').read_text())


class PublisherLookupTests(unittest.TestCase):
    def setUp(self):
        self.fx = clock_tests.ClockExecutorTests('test_one_invocation_actual_journal_report_and_confirmed_cleanup')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        f = self.fx
        f.case = copy.deepcopy(CASE)
        f.case['target_status'] = 'resolved'
        f.case['employee_tool']['deployment_id'] = f.baseline['deployment_id']
        f.basis['prior_baseline'] = copy.deepcopy(f.baseline)
        f.baseline['active_revision_id'] = '44444444-4444-4444-8444-444444444444'
        f.session['execution_scope'] = f.basis['execution_scope'] = lookup.SCOPE
        f.basis['active_revision_id'] = f.baseline['active_revision_id']
        self.detail = dict(f.baseline, status='stopped', max_tool_output_chars=lookup.OUTPUT_LIMIT,
                           bundle={'instructions': [{'kind': 'skill', 'content': lookup.PROMPT}]})
        p = f.root / 'post-update.json'
        p.write_text(json.dumps({'status': 'ok', 'exit_code': 0, 'response': {'data':
            dict(self.detail, max_tool_output_chars=lookup.INITIAL_OUTPUT_LIMIT)}}))
        f.basis['post_update_detail'] = {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
        f.basis['update_request'] = copy.deepcopy(lookup.PATCH)
        prior_revision = f.baseline['active_revision_id']
        f.baseline['active_revision_id'] = '66666666-6666-4666-8666-666666666666'
        self.detail['active_revision_id'] = f.baseline['active_revision_id']
        f.basis['active_revision_id'] = f.baseline['active_revision_id']
        q = f.root / 'output-update.json'
        q.write_text(json.dumps({'status': 'ok', 'exit_code': 0, 'response': {'data': self.detail}}))
        f.basis['output_limit_update'] = {
            'request': copy.deepcopy(lookup.OUTPUT_PATCH), 'prior_revision': prior_revision,
            'post_update_detail': {'path': str(q), 'sha256': hashlib.sha256(q.read_bytes()).hexdigest()}}
        f.write_basis()
        f.first['request']['message'] = lookup.MESSAGE
        f.first['response']['data']['invocation_payload']['message'] = lookup.MESSAGE

    def driver(self, mode='ok'):
        f = self.fx
        driver, cli = f.driver()
        original = cli.json

        def response(*args):
            result = original(*args)
            data = result.data['data']
            if 'managed-get' in args:
                data.update(bundle=self.detail['bundle'], max_tool_output_chars=lookup.OUTPUT_LIMIT)
                if mode == 'wrong-prompt':
                    data['bundle'] = {'instructions': []}
            elif 'managed-deployment-tools' in args:
                t = next(t for t in data['tools'] if t['name'] == 'seren_publishers_get')
                t['input_schema'] = lookup.SCHEMA
                if mode == 'side-effecting':
                    t['side_effecting'] = True
            elif 'run' in args and 'get' in args:
                events = data['output_events']
                events[0].update(name='seren_publishers_get', arguments='{"slug":"seren-db"}')
                metadata = {'id': '55555555-5555-4555-8555-555555555555',
                            'slug': 'seren-db', 'name': 'Synthetic database publisher'}
                if mode == 'wrong-record':
                    metadata['slug'] = 'other'
                if mode == 'missing-id':
                    metadata.pop('id')
                events[1]['content'] = json.dumps(metadata if mode == 'bare' else {'data': metadata})
                events[2]['text'] = 'seren-db'
                if mode == 'truncated':
                    events[1]['content'] = '{"data":'
                if mode == 'cut-wrapper':
                    events[1]['content'] = '{"truncated":true,"content":"{\\"data\\":' + 'x' * 16000 + '\n... (truncated)'
                if mode == 'marked-identity':
                    events[1]['content'] = json.dumps(dict(metadata, truncated=True))
                if mode == 'marked-envelope':
                    events[1]['content'] = json.dumps({'data': dict(metadata, truncated=True)})
                if mode == 'large':
                    # Synthetic complete payload larger than the old output limit.
                    events[1]['content'] = json.dumps({'data': dict(metadata, description='x' * 22000)})
                if mode == 'wrong-tool':
                    events[0]['name'] = 'system_time'
                if mode == 'extra-call':
                    events.insert(0, copy.deepcopy(events[0]))
                if mode == 'secret':
                    events[2]['text'] = 'sk-' + 'Z' * 40
                if mode == 'wrong-answer':
                    events[2]['text'] = 'other'
            return result
        cli.json = response
        return driver, cli

    def test_skill_frontmatter_and_cli_trimmed_payload_match_readback_gate(self):
        # Regression for the observed 400: a managed prompt becomes SKILL.md.
        prompt = (SOURCE / 'docs/publisher-lookup/prompt.md').read_text()
        lines = prompt.splitlines()
        self.assertEqual(lines[0], '---')
        end = lines.index('---', 1)
        metadata = dict(line.split(': ', 1) for line in lines[1:end])
        self.assertRegex(metadata['name'], r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
        self.assertTrue(metadata['description'].strip())
        # Official CLI trims --prompt before building workload_patch.prompt.
        self.assertEqual(prompt.strip(), lookup.PROMPT)
        request = json.loads((SOURCE / 'docs/publisher-lookup/managed-update.proposed.json').read_text())
        self.assertEqual(request, lookup.PATCH)
        lookup.verify_detail(dict(self.detail, bundle={'instructions': [
            {'kind': 'skill', 'content': prompt.strip()}]}))

    def test_supported_lookup_and_both_documented_metadata_shapes(self):
        for mode in ('ok', 'bare'):
            # Fresh journal authority per synthetic run; validate before each call.
            self.fx.session['authorization_id'] = ('22222222-2222-4222-8222-222222222222'
                                                   if mode == 'ok' else '33333333-3333-4333-8333-333333333333')
            validate(self.fx.session, self.fx.case)
            driver, cli = self.driver(mode)
            self.assertEqual(self.fx.execute(driver), 0)
            self.assertEqual(sum('--async' in x for x in cli.calls), 1)
            self.assertFalse(self.fx.store.open_cleanup())

    def test_wrong_or_partial_result_never_passes(self):
        for mode, verdict in [('wrong-record', 1), ('wrong-answer', 1), ('wrong-tool', 1),
                              ('extra-call', 1), ('missing-id', 2), ('truncated', 2)]:
            self.fx.session['authorization_id'] = mode
            driver, cli = self.driver(mode)
            self.assertEqual(self.fx.execute(driver), verdict)
            self.assertEqual(sum('--async' in x for x in cli.calls), 1)
            self.assertFalse(self.fx.store.open_cleanup())


    def test_large_complete_result_passes_without_more_calls_or_runtime(self):
        driver, cli = self.driver('large')
        self.assertEqual(self.fx.execute(driver), 0)
        self.assertEqual(sum('--async' in x for x in cli.calls), 1)
        self.assertEqual(sum('managed-stop' in x for x in cli.calls), 1)
        self.assertLessEqual(self.fx.ticks, 300)
        self.assertFalse(self.fx.store.open_cleanup())

    def test_truncation_is_unverified_even_with_valid_identity_and_answer(self):
        for mode in ('cut-wrapper', 'marked-identity', 'marked-envelope'):
            with self.subTest(mode=mode):
                self.fx.session['authorization_id'] = mode
                driver, _ = self.driver(mode)
                self.assertEqual(self.fx.execute(driver), 2)
                self.assertFalse(self.fx.store.open_cleanup())
        # Diagnostics retain no content, including secret-shaped partial content.
        driver, cli = self.driver('cut-wrapper')
        run = cli.json('run', 'get').data['data']
        run['output_events'][1]['content'] += 'sk-' + 'Z' * 40
        verdict, reason, diagnostic = employee_tool._publisher_result(
            run['output_events'][1]['content'], 'seren-db')
        self.assertEqual(verdict, 'blocked')
        self.assertIn('truncated', reason)
        self.assertEqual(set(diagnostic), {'truncated', 'captured_characters'})
        self.assertNotIn('sk-', json.dumps(diagnostic))

    def test_only_receipted_one_field_correction_can_enable_new_limit(self):
        f = self.fx
        validate(f.session, f.case)
        original = copy.deepcopy(f.basis)
        for kind in ('missing', 'extra-field', 'wrong-prior', 'bad-hash', 'old-scope'):
            with self.subTest(kind=kind):
                f.basis = copy.deepcopy(original)
                update = f.basis['output_limit_update']
                if kind == 'missing':
                    del f.basis['output_limit_update']
                elif kind == 'extra-field':
                    update['request']['workload_limits']['max_tool_calls_per_run'] = 2
                elif kind == 'wrong-prior':
                    update['prior_revision'] = f.baseline['active_revision_id']
                elif kind == 'bad-hash':
                    update['post_update_detail']['sha256'] = '0' * 64
                else:
                    f.session['execution_scope'] = 'one-call-publisher-lookup-v1'
                f.write_basis()
                with self.assertRaises((ValueError, KeyError)):
                    validate(f.session, f.case)
                f.session['execution_scope'] = lookup.SCOPE
        f.basis = original
        f.write_basis()
        # The larger cap must actually appear in the read-back, not just the request.
        lookup.verify_detail(self.detail)
        with self.assertRaises(ValueError):
            lookup.verify_detail(dict(self.detail, max_tool_output_chars=16000))
        self.assertEqual(json.loads((SOURCE / 'docs/publisher-lookup/output-limit-update.proposed.json').read_text()), lookup.OUTPUT_PATCH)
        self.assertEqual(json.loads((SOURCE / 'docs/publisher-lookup/output-limit-agent-config.json').read_text()), lookup.OUTPUT_PATCH['workload_limits'])

    def test_fixture_and_tool_drift_block_before_start(self):
        for mode in ('wrong-prompt', 'side-effecting'):
            self.fx.session['authorization_id'] = mode
            driver, cli = self.driver(mode)
            self.assertEqual(self.fx.execute(driver), 2)
            self.assertFalse(any('managed-start' in x for x in cli.calls))

    def test_readback_projection_keeps_only_verified_public_controls(self):
        detail = dict(self.detail, credentials={'token': 'sk-' + 'Z' * 40})
        projected = lookup.detail_projection(detail, self.fx.baseline)
        self.assertNotIn('credentials', projected)
        self.assertNotIn('sk-' + 'Z' * 40, json.dumps(projected))
        lookup.verify_detail(projected)
        for bundle in (None, {}, {'instructions': None}, {'instructions': []}):
            detail['bundle'] = bundle
            with self.assertRaises(ValueError):
                lookup.detail_projection(detail, self.fx.baseline)

    def test_prior_clock_authority_and_missing_update_receipt_refused(self):
        validate(self.fx.session, self.fx.case)
        for key in ('prior_baseline', 'post_update_detail', 'update_request'):
            original = copy.deepcopy(self.fx.basis)
            self.fx.basis.pop(key)
            self.fx.write_basis()
            with self.assertRaises((ValueError, KeyError)):
                validate(self.fx.session, self.fx.case)
            self.fx.basis = original
        self.fx.write_basis()
        self.fx.session['execution_scope'] = 'one-call-trace-v2'
        with self.assertRaises(ValueError):
            validate(self.fx.session, self.fx.case)

    def test_changed_query_and_message_cannot_gain_authority(self):
        for key, value in [('arguments', {'slug': 'other'}), ('tool', 'seren_publisher_request'),
                           ('message', 'run SQL'), ('result', {'slug': 'other'})]:
            case = copy.deepcopy(self.fx.case)
            case['employee_tool'][key] = value
            with self.assertRaises(ValueError):
                validate(self.fx.session, case)

    def test_actual_cli_offline_and_secret_sanitization(self):
        f = self.fx
        case_path = f.root / 'case.json'
        case_path.write_text(json.dumps(f.case))
        f.session['case'] = str(case_path)
        session_path = f.root / 'session.json'
        session_path.write_text(json.dumps(f.session))
        _, cli = self.driver('secret')
        def driver(session, case, cli, store):
            return ClockRun(session, case, cli, store, f.clock, f.sleep, f.wall)
        with mock.patch('seren_harness.employee_run.pinned_cli', return_value=cli), \
                mock.patch('seren_harness.employee_run.ClockRun', side_effect=driver), \
                mock.patch('socket.socket', side_effect=AssertionError('network')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('network')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(['employee-lookup', '--session', str(session_path)]), 1)
        self.assertIn('Shutdown: confirmed', out.getvalue())
        for p in f.root.rglob('*'):
            if p.is_file():
                self.assertNotIn(('sk-' + 'Z' * 40).encode(), p.read_bytes())
