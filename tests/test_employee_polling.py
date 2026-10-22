"""Synthetic delayed states informed by saved timings, not a replayed live pass."""
import io
import json
from unittest import TestCase, mock
import test_publisher_lookup as fixtures
from seren_harness.cli import main
from seren_harness.employee_run import ClockRun


class EmployeePollingTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.PublisherLookupTests('test_supported_lookup_and_both_documented_metadata_shapes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fx = self.fixture.fx

    def delayed(self, ready_after=35, stopped_after=65, late_detail=False):
        driver, cli = self.fixture.driver()
        original = cli.json
        observed = {'start': None, 'stop': None, 'polls': []}

        def call(*args):
            if 'managed-start' in args:
                observed['start'] = self.fx.ticks
            if 'managed-stop' in args:
                observed['stop'] = self.fx.ticks
                observed['stop_remaining'] = driver.deadline - self.fx.ticks
            if 'managed-get' in args and observed['stop'] is not None and late_detail:
                self.fx.ticks = min(driver.deadline, observed['stop'] + 180) + 1
                # Deliberately noncompliant response arriving beyond the close deadline.
            result = original(*args)
            if 'status' in args and observed['start'] is not None:
                data = result.data['data']
                if observed['stop'] is None:
                    if self.fx.ticks - observed['start'] < ready_after:
                        data.update(status='building', desired_lifecycle_state='running')
                else:
                    observed['polls'].append((self.fx.ticks, cli.timeout))
                    if self.fx.ticks - observed['stop'] < stopped_after:
                        data.update(status='running', desired_lifecycle_state='stopping')
            return result
        cli.json = call
        return driver, cli, observed

    def test_delayed_readiness_and_shutdown_pass_without_extra_reads(self):
        driver, cli, observed = self.delayed()
        self.assertEqual(self.fx.execute(driver), 0)
        self.assertEqual(sum('--async' in c for c in cli.calls), 1)
        self.assertEqual(sum('managed-stop' in c for c in cli.calls), 1)
        self.assertLessEqual(driver.counts['status'], 12)
        self.assertGreaterEqual(observed['stop_remaining'], 180)
        self.assertGreaterEqual(observed['polls'][-1][0] - observed['stop'], 65)
        self.assertLessEqual(self.fx.ticks - observed['start'], 300)
        self.assertFalse(self.fx.store.open_cleanup())

    def test_never_ready_stops_without_invocation_with_shortened_allowance(self):
        self.fx.session['runtime_remaining_seconds'] = 210
        driver, cli, observed = self.delayed(ready_after=1000)
        self.assertEqual(self.fx.execute(driver), 2)
        self.assertFalse(any('--async' in c for c in cli.calls))
        self.assertGreaterEqual(observed['stop_remaining'], 180)
        self.assertLessEqual(self.fx.ticks - observed['start'], 210)
        self.assertFalse(self.fx.store.open_cleanup())

    def test_never_stopped_preserves_obligation_without_retry(self):
        driver, cli, observed = self.delayed(stopped_after=1000)
        self.assertEqual(self.fx.execute(driver), 2)
        self.assertEqual(sum('managed-stop' in c for c in cli.calls), 1)
        self.assertEqual(len(observed['polls']), 5)
        self.assertGreater(observed['polls'][-1][0] - observed['stop'], 110)
        self.assertLessEqual(driver.counts['status'], 12)
        self.assertLessEqual(self.fx.ticks - observed['start'], 300)
        self.assertTrue(self.fx.store.open_cleanup())

    def test_late_final_readback_cannot_claim_confirmed_shutdown(self):
        driver, cli, _ = self.delayed(late_detail=True)
        self.assertEqual(self.fx.execute(driver), 2)
        self.assertTrue(self.fx.store.open_cleanup())
        self.assertEqual(sum('managed-stop' in c for c in cli.calls), 1)

    def test_actual_lookup_cli_handles_synthetic_delays_offline(self):
        f = self.fx
        case = f.root / 'case.json'
        case.write_text(json.dumps(f.case))
        f.session['case'] = str(case)
        session = f.root / 'session.json'
        session.write_text(json.dumps(f.session))
        _, cli, observed = self.delayed()
        def factory(session, case, cli, store):
            return ClockRun(session, case, cli, store, f.clock, f.sleep, f.wall)
        with mock.patch('seren_harness.employee_run.pinned_cli', return_value=cli), \
                mock.patch('seren_harness.employee_run.ClockRun', side_effect=factory), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('network')), \
                mock.patch('socket.socket', side_effect=AssertionError('network')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(['employee-lookup', '--session', str(session)]), 0)
        self.assertIn('Shutdown: confirmed', out.getvalue())
        self.assertLessEqual(f.ticks - observed['start'], 300)
