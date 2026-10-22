"""Operator commands through a fake Seren CLI; no real account or service access."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from seren_support import SOURCE
from seren_harness import cli, operator
from test_seren_cli_collect import Fixture, catalog, CANARY

TARGET = 'bcdefabc-1234-4567-89ab-0123456789ab'


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = Fixture(self.root, answer=catalog(deployment=TARGET))
        self.path = self.root / 'operator.local.json'
        self.settings = {
            'version': 1,
            'seren': {'executable': './seren',
                      'executable_sha256': hashlib.sha256((self.root / 'seren').read_bytes()).hexdigest(),
                      'config_home': './config', 'profile': 'recipient'},
            'deployment_id': TARGET, 'output_dir': './reviews', 'timeout_seconds': 2}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.settings))

    def run_command(self, name):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.dict(os.environ, {
                'SEREN_API_KEY': CANARY, 'SEREN_API_BASE': 'https://example.invalid'}):
            code = cli.main([name, '--config', str(self.path)])
        return code, out.getvalue()

    def test_setup_runs_without_git_or_installed_development_packages_and_never_reads_service(self):
        completed = subprocess.run([sys.executable, '-B', '-S', '-m', 'seren_harness',
                                    'check-setup', '--config', str(self.path)], cwd=SOURCE,
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('Authentication and access remain unverified', completed.stdout)
        self.assertEqual(self.fixture.calls(), 0)
        self.assertTrue(self.fixture.home.is_dir())
        self.assertFalse(any(p.is_file() for p in self.fixture.home.rglob("*")))

    def test_live_command_binds_one_config_without_editing_cases_or_using_test_dependencies(self):
        case_path = SOURCE / 'cases' / operator.CASE_FILE
        original = case_path.read_bytes()
        completed = subprocess.run([sys.executable, '-B', '-S', '-m', 'seren_harness',
                                    'test-live', '--config', str(self.path)], cwd=SOURCE,
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(self.fixture.calls(), 1)
        self.assertEqual(self.fixture.log()['argv'], ['agent', 'managed-deployment-tools', TARGET, '-o', 'json'])
        self.assertEqual(case_path.read_bytes(), original)
        self.assertEqual(self.fixture.report()['verdict'], 'passed')
        for p in self.fixture.home.rglob('*'):
            if p.is_file():
                self.assertEqual(p.stat().st_mode & 0o077, 0)

    def test_target_mismatch_and_bad_catalog_cannot_pass(self):
        for answer in (catalog(), {'data': {'deployment_id': TARGET, 'tools': [{'name': 'incomplete'}]}}):
            with self.subTest(answer=answer):
                (self.fixture.config / 'answer.json').write_text(json.dumps(answer))
                before = self.fixture.calls()
                code, _ = self.run_command('test-live')
                self.assertEqual(code, 1)
                self.assertEqual(self.fixture.calls(), before + 1)

    def test_invalid_config_or_pin_stops_before_service_access(self):
        changes = [lambda x: x.update(deployment_id='--other-target'),
                   lambda x: x.update(timeout_seconds=31),
                   lambda x: x.update(timeout_seconds=True),
                   lambda x: x.update(output_dir=str(SOURCE / 'reports')),
                   lambda x: x['seren'].update(profile='../account'),
                   lambda x: x['seren'].update(executable_sha256='0' * 64),
                   lambda x: x['seren'].update(api_key=CANARY),
                   lambda x: x.update(command=['agent', 'managed-start', TARGET])]
        base = json.dumps(self.settings)
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.settings = json.loads(base)
                change(self.settings)
                self.save()
                code, text = self.run_command('test-live')
                self.assertEqual(code, 5, text)
                self.assertNotIn(CANARY, text)
                self.assertEqual(self.fixture.calls(), 0)

    def test_changed_executable_and_unsupported_version_are_refused(self):
        (self.fixture.config / 'version').write_text('0.11.0')
        self.assertEqual(self.run_command('test-live')[0], 5)
        (self.fixture.config / 'version').write_text('0.10.0')
        with (self.root / 'seren').open('a') as f:
            f.write('\n# changed\n')
        self.assertEqual(self.run_command('test-live')[0], 5)
        self.assertEqual(self.fixture.calls(), 0)

    def test_timeout_never_retries(self):
        self.settings['timeout_seconds'] = 1
        self.save()
        (self.fixture.config / 'mode').write_text('slow')
        code, _ = self.run_command('test-live')
        self.assertEqual(code, 3)
        self.assertEqual(self.fixture.calls(), 1)

    def test_authentication_failure_is_unverified_and_secrets_not_forwarded_or_saved(self):
        (self.fixture.config / 'mode').write_text('error:401 Unauthorized ' + CANARY)
        code, output = self.run_command('test-live')
        self.assertEqual(code, 2, output)
        self.assertEqual(self.fixture.calls(), 1)
        self.assertNotIn('SEREN_API_KEY', self.fixture.log()['env'])
        self.assertNotIn('SEREN_API_BASE', self.fixture.log()['env'])
        self.assertNotIn(CANARY, output)
        for p in self.fixture.home.rglob('*'):
            if p.is_file():
                self.assertNotIn(CANARY.encode(), p.read_bytes())

    def test_unfilled_example_gives_actionable_error_without_service_access(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['check-setup', '--config', str(SOURCE / 'operator.example.json')])
        self.assertEqual(code, 5)
        self.assertIn('Setup needs attention', out.getvalue())
        self.assertEqual(self.fixture.calls(), 0)

    def test_parent_file_is_refused_by_both_commands_before_any_service_request(self):
        blocked = self.root / 'parent-file'
        blocked.write_text('keep this file')
        self.settings['output_dir'] = str(blocked / 'reports')
        self.save()
        for command in ('check-setup', 'test-live'):
            with self.subTest(command=command):
                code, text = self.run_command(command)
                self.assertEqual(code, 5)
                self.assertIn('output_dir cannot be prepared', text)
                self.assertEqual(self.fixture.calls(), 0)
        self.assertEqual(blocked.read_text(), 'keep this file')

    def test_blocked_report_subdirectory_is_refused_before_service_request(self):
        self.fixture.home.mkdir()
        blocked = self.fixture.home / operator.CASE_ID
        blocked.write_text('keep existing content')
        code, text = self.run_command('test-live')
        self.assertEqual(code, 5)
        self.assertIn('output_dir', text)
        self.assertEqual(self.fixture.calls(), 0)
        self.assertEqual(blocked.read_text(), 'keep existing content')

    def test_failed_write_or_sync_stops_before_service_and_removes_probe(self):
        for failure in ('permission', 'disk-full'):
            with self.subTest(failure=failure):
                target = ('seren_harness.operator.tempfile.TemporaryFile' if failure == 'permission'
                          else 'seren_harness.operator.os.fsync')
                error = PermissionError('denied') if failure == 'permission' else OSError('disk full')
                with patch(target, side_effect=error):
                    code, text = self.run_command('test-live')
                self.assertEqual(code, 5)
                self.assertIn('No service request was made', text)
                self.assertEqual(self.fixture.calls(), 0)
                self.assertFalse(any(self.fixture.home.rglob('.seren-write-check-*')))

    def test_existing_reports_are_preserved_and_live_run_rechecks_output(self):
        self.fixture.home.mkdir()
        report = self.fixture.home / 'previous-report.md'
        report.write_text('existing evidence')
        self.assertEqual(self.run_command('check-setup')[0], 0)
        self.assertEqual(report.read_text(), 'existing evidence')
        self.assertFalse(any(self.fixture.home.rglob('.seren-write-check-*')))
        # A successful earlier setup cannot authorize a now-unwritable destination.
        with patch('seren_harness.operator.tempfile.TemporaryFile', side_effect=PermissionError('denied')):
            self.assertEqual(self.run_command('test-live')[0], 5)
        self.assertEqual(self.fixture.calls(), 0)
        self.assertEqual(report.read_text(), 'existing evidence')
