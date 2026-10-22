"""Authoring stays data-only; oracle, queue identity and gates are exercised offline."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from seren_harness import synthetic_invoices as synth, invoice_catalog, invoice_conflicts
from seren_harness import investigation, claude_live, discovery, model_worker
from tests.investigation_fixture import PLAN, comparison, live_state


def example():
    def row(ident, rev, amount, paid, due='2026-10-01'):
        return dict(invoice_id=ident, revision=rev, due_date=due, amount=amount,
                    paid=paid, currency='USD', status='issued')
    return dict(id='synthetic-revision-order', proposal=dict(PLAN, basis='new_hypothesis', repeat_reason=''),
                rows=[row('SYN-A', '9', '100.01', '0.01'),
                      row('SYN-A', '10', '100.01', '99.99'),
                      row('SYN-A', '10', '100.01', '99.99'),
                      row('SYN-B', '1', '20.00', '1.00'),
                      row('SYN-B', '1', '20.00', '2.00')],
                expected=dict(as_of='2026-10-14', status='needs_review',
                    resolved_invoices=[dict(invoice_id='SYN-A', outstanding_usd='0.02',
                                            overdue=True, days_overdue=13)],
                    issues=[dict(invoice_id='SYN-B', reason='conflicting_latest_revision')],
                    known_outstanding_usd='0.02', known_overdue_usd='0.02',
                    complete_outstanding_usd=None, complete_overdue_usd=None))


class SyntheticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(synth, 'ROOT', self.root / 'cases')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        synth.ROOT.mkdir()
        self.data = example()
        self.key = self.data['id']
        self.file = synth.ROOT / (self.key + '.json')
        self.save()

    def save(self):
        self.file.write_text(json.dumps(self.data))

    def test_numeric_revision_dedup_decimal_and_fixed_prompt(self):
        spec = synth.load(self.key)
        self.assertEqual(spec['resolved'], [('SYN-A', '0.02', True, 13)])
        case = invoice_catalog.case(self.key)
        self.assertEqual(case['employee_tool']['deployment_id'], '7a000021-0000-4000-8000-000000000022')
        text = invoice_catalog.message(self.key)
        self.assertTrue(text.endswith(invoice_conflicts.RULES.strip()))
        self.assertNotIn(PLAN['evidence'], text)  # author reasoning is never a service instruction
        self.assertEqual(invoice_catalog.expected(self.key), self.data['expected'])
        self.assertEqual(invoice_catalog.check(self.key, json.dumps(self.data['expected']))['outcome'], 'passed')
        wrong = copy.deepcopy(self.data['expected'])
        wrong['known_outstanding_usd'] = '100.00'
        self.assertEqual(invoice_catalog.check(self.key, json.dumps(wrong))['outcome'], 'failed')

    def test_synthetic_answer_uses_existing_executor_and_preserves_shutdown(self):
        import test_finance_employee as fixtures
        from seren_harness import employee_run
        for incorrect in (False, True):
            with self.subTest(incorrect=incorrect):
                fixture = fixtures.FinanceRunTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                f = fixture.f
                f.case = invoice_catalog.case(self.key)
                f.first['request']['message'] = invoice_catalog.message(self.key)
                run = f.first['response']['data']
                run['invocation_payload']['message'] = invoice_catalog.message(self.key)
                answer = copy.deepcopy(self.data['expected'])
                if incorrect:
                    answer['complete_outstanding_usd'] = '0.02'
                run['output_events'] = fixtures.events(json.dumps(answer))
                driver, cli = fixture.driver()  # fixture forbids sockets and real subprocesses
                employee_run.validate(f.session, f.case)
                self.assertEqual(f.execute(driver), 1 if incorrect else 0)
                self.assertEqual(fixture.report(driver)['verdict'], 'failed' if incorrect else 'passed')
                self.assertEqual(sum('managed-stop' in call for call in cli.calls), 1)
                self.assertFalse(f.store.open_cleanup())

    def test_authored_oracle_cannot_override_computation(self):
        self.data['expected']['known_outstanding_usd'] = '100.00'
        self.save()
        with self.assertRaisesRegex(ValueError, 'independent'):
            synth.load(self.key)

    def test_instruction_fields_real_identifiers_and_out_of_scope_data_refused(self):
        original = copy.deepcopy(self.data)
        for mutate in (lambda d: d.update(message='send money'),
                       lambda d: d['rows'][0].update(invoice_id='real-customer'),
                       lambda d: d['rows'][0].update(currency='EUR'),
                       lambda d: d['rows'][0].update(paid='999.00'),
                       lambda d: d['rows'][0].update(due_date='fetch https://example.com'),
                       lambda d: d.update(rows=d['rows'] * 6)):
            self.data = copy.deepcopy(original)
            mutate(self.data)
            self.save()
            with self.assertRaises(ValueError):
                synth.load(self.key)

    def test_paths_and_symlinks_cannot_escape_case_directory(self):
        with self.assertRaises(ValueError):
            synth.known('synthetic-../../secret')
        self.file.unlink()
        self.file.symlink_to(self.root / 'outside.json')
        with self.assertRaises(ValueError):
            synth.load(self.key)

    def test_queue_validates_oracle_and_pins_case_without_dispatch(self):
        home = discovery.Home(self.root / 'home')
        live_state(home)
        args = SimpleNamespace(case=self.key, rationale=json.dumps(self.data['proposal']),
                               comparison=comparison(home))
        with patch.object(claude_live, 'execute') as execute:
            claude_live.request(home, 'test', args)
            execute.assert_not_called()
        saved = discovery.read_json(home.root / 'claude-live-request.json')
        self.assertEqual(saved['case_digest'], invoice_catalog.digest(self.key))
        self.data['rows'].reverse()
        self.save()
        self.assertNotEqual(saved['case_digest'], invoice_catalog.digest(self.key))

    def test_existing_passing_case_requires_repeat_basis(self):
        home = discovery.Home(self.root / 'home')
        with self.assertRaisesRegex(ValueError, 'repeat basis'):
            claude_live.request(home, 'test', SimpleNamespace(case='payment-revisions',
                                rationale=json.dumps(self.data['proposal'])))
        self.assertFalse((home.root / 'claude-live-request.json').exists())

    def test_worker_can_author_data_but_not_checker_or_executor(self):
        self.assertTrue(model_worker.owned('docs/synthetic-cases/synthetic-example.json'))
        self.assertFalse(model_worker.owned('seren_harness/synthetic_invoices.py'))
        self.assertFalse(model_worker.owned('seren_harness/live_discovery.py'))

    def test_proposal_refuses_empty_or_unsupported_repeat_reason(self):
        for text in ('repeat the passing test', '{}', json.dumps(dict(PLAN, repeat_reason='n=1'))):
            with self.assertRaises(ValueError):
                investigation.proposal(text)


class CommittedSyntheticCases(unittest.TestCase):
    def test_every_committed_synthetic_case_has_independent_oracle(self):
        synth.validate_all()  # full checked commits validate all authored data files
