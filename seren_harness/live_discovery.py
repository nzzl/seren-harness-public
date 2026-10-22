"""Standing Clock Test authority: durable daily reservations around the existing executor.

The investigator chooses a frozen case and evidence question; this gate owns execution.
It does not grant new fixture/configuration operations or turn failed answers into passes.
"""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

from seren_harness import discovery, employee_run, invoice_catalog, investigation, journeys
from seren_harness.invoice_campaign import write
from seren_harness.models import utc_now
from seren_harness.state import StateError, Store

ROOT = Path(__file__).resolve().parents[1]
TARGET = '7a000021-0000-4000-8000-000000000022'
TZ = ZoneInfo('Etc/GMT-7')
LIMIT = 200
DAILY_CALLS = 3100
CALLS = 31
WINDOW = 420


class Refused(ValueError):
    pass


def day(stamp):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(TZ).date().isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def load_authority(home):
    a = read(home.root / 'live-authority.json')
    if (a.get('approved') is not True or a.get('target') != TARGET
            or type(a.get('runs_per_day')) is not int or not 1 <= a['runs_per_day'] <= LIMIT
            or type(a.get('calls_per_day')) is not int
            or not CALLS <= a['calls_per_day'] <= DAILY_CALLS
            or a.get('timezone') != 'Etc/GMT-7' or a.get('no_monetary_cap') is not True):
        raise Refused('standing live authority missing or different')
    s = a['session']
    if (s['baseline']['deployment_id'] != TARGET
            or s['baseline']['name'] != employee_run.CLOCK_NAME
            or s.get('runtime_remaining_seconds') != 300):
        raise Refused('authority does not bind the existing Clock Test session')
    return a


def usage(session, ledger, stamp, runs_per_day=LIMIT, calls_per_day=DAILY_CALLS):
    """Preserve manual reservations; settle only durably finished, stopped attempts."""
    today = day(stamp)
    conn = sqlite3.connect('file:' + str(Path(session['state_dir']) / 'state.sqlite3') + '?mode=ro',
                           uri=True)
    try:
        rows = conn.execute("SELECT config_digest, created_at FROM runs WHERE employee_key=? "
                            "AND journey LIKE 'employee_%'", ('clock:' + TARGET,)).fetchall()
    finally:
        conn.close()
    attempts = ledger['attempts']
    by_id = {r['id']: r for r in attempts}
    if len(by_id) != len(attempts):
        raise Refused('duplicate attempt accounting; inspect ledger')
    ids = {ident for ident, created in rows if day(created) == today}
    ids.update(r['id'] for r in attempts if r['day'] == today)
    actual, reserved, settled = 0, 0, 0
    for ident in ids:
        entry = by_id.get(ident, {})
        calls = entry.get('calls')
        if calls is not None and (type(calls) is not int or not 0 <= calls <= CALLS):
            raise Refused('invalid call accounting; inspect ledger')
        settled_cleanup = (entry.get('cleanup_confirmed') is True
                           or entry.get('cleanup', {}).get('status') == 'not_required')
        if (entry.get('status') == 'finished' and settled_cleanup
                and entry.get('ended_at') and entry.get('report') and calls is not None):
            actual += calls
            settled += 1
        else:
            reserved += CALLS  # legacy/manual/uncertain records never get an inferred refund
    charged = actual + reserved
    return {'day': today, 'runs_used': len(ids), 'calls_used': actual,
            'calls_reserved': reserved, 'calls_charged': charged,
            'settled_attempts': settled,
            'attempts_remaining': max(0, runs_per_day - len(ids)),
            'runs_remaining': max(0, min(runs_per_day - len(ids),
                                         (calls_per_day - charged) // CALLS)),
            'run_limit': runs_per_day, 'call_limit': calls_per_day,
            'calls_remaining': max(0, calls_per_day - charged)}


def operational_complete(report):
    """Only a correlated, tool-free answer with explicit shutdown permits continuation."""
    criteria = {c['id']: c for c in report.get('criteria', [])}
    return (all(criteria.get(k, {}).get('verdict') == 'passed'
                for k in ('run_identity', 'no_tool_activity', 'shutdown'))
            and criteria['shutdown'].get('evidence', {}).get('status') == 'stopped'
            and criteria['shutdown'].get('evidence', {}).get('desired_lifecycle_state') == 'stopped'
            and criteria.get('answer', {}).get('verdict') in ('passed', 'failed'))


def gates(store, ledger):
    key = 'clock:' + TARGET
    if any(r['status'] in ('attempting', 'blocked') for r in ledger['attempts']):
        raise Refused('unfinished attempt: inspect saved evidence; never replay')
    if store.open_cleanup(key) or store.employee_unresolved_actions(key) or store.lock(key):
        raise Refused('employee cleanup, action or lock is unresolved')


def validate_source():
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(ROOT), *args], text=True).strip()
    if git('status', '--porcelain'):
        raise Refused('repository is dirty')
    common = Path(git('rev-parse', '--git-common-dir'))
    if not common.is_absolute():
        common = ROOT / common
    checked = read(common / 'tiny-harness-full-validation.json')
    if checked.get('result') != 'pass' or checked.get('tree') != git('rev-parse', 'HEAD^{tree}'):
        raise Refused('current tree lacks a full-suite pass')
    return git('rev-parse', 'HEAD')


def reserve(ledger, status, case_id, rationale, stamp):
    if status['runs_remaining'] < 1:
        raise Refused('daily budget exhausted; wait until next UTC+07:00 day')
    dt = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if day((dt + timedelta(seconds=WINDOW)).isoformat()) != day(stamp):
        raise Refused('too close to UTC+07:00 midnight for a bounded run')
    entry = {'id': str(uuid.uuid4()), 'day': day(stamp), 'case': case_id,
             'rationale': rationale, 'started_at': stamp, 'status': 'attempting', 'calls': 0,
             'reserved_calls': CALLS, 'expires_at': (dt + timedelta(seconds=WINDOW)).isoformat()}
    ledger['attempts'].append(entry)
    return entry


def count_call(entry, persist):
    if entry['calls'] >= CALLS:
        raise employee_run.Halt('standing call budget exhausted')
    entry['calls'] += 1
    persist()  # before sending, including commands whose outcome may be lost


def journey_gate(home, authority, journey, digest, rationale, head, get_key=None, opener=None):
    """One granted journey package through the same ledger, budget and cleanup gates.

    Every refusal precedes any CLI, reservation or service call. The standing Clock Test
    grant never covers a journey: authorize() needs an active grant for this digest.
    """
    from seren_harness import grant_transport, retrieval_journey
    path = home.root / 'live-ledger.json'
    ledger = read(path)
    try:
        entry_ = journeys.authorize(journeys.load(), authority, journey, digest, ledger)
        investigation.proposal(rationale)
        journeys.adapter(entry_)
        package = retrieval_journey.load_package(entry_)
        case = retrieval_journey.case(package)
        session = retrieval_journey.current_session(authority['session'], ledger)
        # Two separate authorities must both hold: the journey grant above, and the original
        # supplied-data fixture's preparation receipt chain (with any verified rebinding).
        # Checked with a provisional id and expiry, before any reservation, client or key.
        if session['baseline'].get('name') != employee_run.CLOCK_NAME:
            raise ValueError('journey target is not the prepared Clock Test fixture')
        stamp = utc_now()
        employee_run.verify_preparation(dict(
            session, authorization_id=str(uuid.uuid4()),
            expires_at=(datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                        + timedelta(seconds=WINDOW)).isoformat()), TARGET, 'supplied')
    except (ValueError, KeyError, OSError) as error:
        raise Refused('fixture preparation or journey package not established: '
                      + (str(error) if isinstance(error, ValueError) else type(error).__name__)) from None
    store = Store(session['state_dir'])
    try:
        gates(store, ledger)
        stamp = utc_now()
        status = usage(session, ledger, stamp, authority['runs_per_day'], authority['calls_per_day'])
        if status['runs_remaining'] < 1:
            raise Refused('daily budget exhausted; wait until next UTC+07:00 day')
        entry = reserve(ledger, status, journey, rationale, stamp)  # the whole 31-call attempt
        entry.update(head=head, journey=journey, journey_digest=digest,
                     case_sha256=hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest())
        write(path, ledger)
        session.update(authorization_id=entry['id'], expires_at=entry['expires_at'])
        folder = home.root / 'live-attempts' / entry['id']
        write(folder / 'session.json', session)
        write(folder / 'case.json', case)
        budget = retrieval_journey.Budget(lambda: count_call(entry, lambda: write(path, ledger)), CALLS)
        driver = None
        try:
            cli = employee_run.pinned_cli(session, store.sanitizer)
            budget.wrap_cli(cli)
            transport = grant_transport.GrantTransport(
                TARGET, retrieval_journey.setup_body(package),
                get_key or grant_transport.keychain_api_key, store.sanitizer, budget.before_send,
                **({'opener': opener} if opener else {}))
            driver = retrieval_journey.GrantedBranchRun(session, case, cli, store, transport, budget)
            driver.run()
            entry['run_id'] = driver.run_id
            entry['report'] = str(Path(session['home']) / driver.run_id / 'report/report.json')
            report = read(entry['report'])
            entry['verdict'] = report['verdict']
            shutdown = next((c for c in report.get('criteria', []) if c['id'] == 'shutdown'), {})
            open_items = store.open_cleanup(run_id=driver.run_id) or store.actions(driver.run_id, True)
            entry['restore'] = driver.restore
            restored = driver.restore['status'] in ('not_needed', 'not_applied', 'restored')
            entry['cleanup_confirmed'] = bool(
                shutdown.get('verdict') == 'passed' and restored and not open_items
                and shutdown.get('evidence') == {'status': 'stopped', 'desired_lifecycle_state': 'stopped'})
            identity = retrieval_journey.observation_identity(report)
            idle = not entry['cleanup_confirmed'] and retrieval_journey.never_started(
                driver, store, transport.receipts)
            if idle:
                entry['cleanup'] = dict(retrieval_journey.NOT_REQUIRED)
            result = journeys.classify(
                journeys.observe(retrieval_journey.output_events(driver)), case['employee_tool'],
                provenance='live', run_identity=identity,
                shutdown=('confirmed' if shutdown.get('verdict') == 'passed'
                          else 'not_required' if idle else 'unconfirmed'),
                restore='confirmed' if restored else 'unconfirmed',
                grant_verified=driver.setup_revision is not None)
            entry['classification'] = result['classification']
            entry['classification_detail'] = result
            entry['operational_complete'] = entry['cleanup_confirmed'] and identity == 'match'
            if driver.restore['status'] == 'restored' and entry['cleanup_confirmed']:
                record = retrieval_journey.rebinding_record(entry['id'], driver)
                write(folder / 'rebinding.json', record)
                entry['rebinding'] = {'path': str(folder / 'rebinding.json'), 'sha256':
                                      hashlib.sha256((folder / 'rebinding.json').read_bytes()).hexdigest()}
            write(folder / 'transport-receipts.json', transport.receipts)
            entry['status'] = 'finished' if entry['cleanup_confirmed'] or idle else 'blocked'
        except Exception as error:
            entry['status'] = 'blocked'
            entry['error'] = type(error).__name__
            if driver:
                entry['run_id'] = driver.run_id
                entry['restore'] = driver.restore
            raise
        finally:
            entry['ended_at'] = utc_now()
            write(path, ledger)
            write(folder / 'outcome.json', entry)
        return entry
    finally:
        store.close()


def execute(home, case_id, rationale, case_digest=None, journey=None):
    # Holding the supervisor lock prevents launches; the work lock proves idle and
    # prevents an independently invoked worker. STOP is respected, never cleared here.
    with home.lock('supervisor', False) as supervisor, home.lock('work', False) as worker:
        if not supervisor or not worker:
            raise Refused('discovery busy; wait without interrupting')
        if home.stop_path.exists() or (home.root / 'LIVE_STOP').exists():
            raise Refused('stop switch on')
        authority = load_authority(home)
        head = validate_source()
        if journey:
            return journey_gate(home, authority, journey, case_digest, rationale, head)
        if not invoice_catalog.known(case_id) or not rationale.strip():
            raise Refused('frozen case and concrete evidence question required')
        plan = investigation.proposal(rationale)
        if not case_id.startswith('synthetic-') and plan['basis'] == 'new_hypothesis':
            raise Refused('existing case needs a specific repeat justification')
        if case_digest and invoice_catalog.digest(case_id, TARGET) != case_digest:
            raise Refused('queued case or oracle changed; review a new proposal')
        case = invoice_catalog.case(case_id, TARGET)
        path = home.root / 'live-ledger.json'
        ledger = read(path)  # missing/corrupt ledger is a stop, never a budget reset
        from seren_harness import retrieval_journey
        # A verified journey restore carries the baseline revision forward; nothing else does.
        session = retrieval_journey.current_session(authority['session'], ledger)
        with_store = Store(session['state_dir'])
        try:
            gates(with_store, ledger)
            stamp = utc_now()
            status = usage(session, ledger, stamp, authority['runs_per_day'], authority['calls_per_day'])
            # No CLI is created until budget, identity and saved setup are checked.
            if status['runs_remaining'] < 1:
                raise Refused('daily budget exhausted; wait until next UTC+07:00 day')
            preview = dict(session, authorization_id=str(uuid.uuid4()),
                           expires_at=(datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                                       + timedelta(seconds=WINDOW)).isoformat())
            employee_run.validate(preview, case)
            entry = reserve(ledger, status, case_id, rationale, stamp)
            entry['head'] = head
            entry['case_sha256'] = hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest()
            write(path, ledger)
            session.update(authorization_id=entry['id'], expires_at=entry['expires_at'])
            folder = home.root / 'live-attempts' / entry['id']
            write(folder / 'session.json', session)
            write(folder / 'case.json', case)
            driver = None
            try:
                cli = employee_run.pinned_cli(session, with_store.sanitizer)
                original = cli._execute

                def bounded(args, parse=False):
                    count_call(entry, lambda: write(path, ledger))
                    return original(args, parse=parse)

                cli._execute = bounded
                driver = employee_run.ClockRun(session, case, cli, with_store)
                driver.run()  # existing finally-path stop, readback, evidence and review
                entry['run_id'] = driver.run_id
                entry['report'] = str(Path(session['home']) / driver.run_id / 'report/report.json')
                result = read(entry['report'])
                entry['verdict'] = result['verdict']
                shutdown = next((c for c in result.get('criteria', []) if c['id'] == 'shutdown'), {})
                entry['cleanup_confirmed'] = (shutdown.get('verdict') == 'passed'
                    and shutdown.get('evidence') == {'status': 'stopped', 'desired_lifecycle_state': 'stopped'}
                    and not with_store.open_cleanup(run_id=driver.run_id))
                entry['operational_complete'] = operational_complete(result)
                entry['status'] = 'finished' if entry['cleanup_confirmed'] else 'blocked'
            except Exception as error:
                entry['status'] = 'blocked'
                entry['error'] = type(error).__name__  # no sensitive exception text
                if driver:
                    entry['run_id'] = driver.run_id
                raise
            finally:
                entry['ended_at'] = utc_now()
                write(path, ledger)
                write(folder / 'outcome.json', entry)
            return entry
        finally:
            with_store.close()


def reconcile_not_started(home, attempt_id):
    """Trusted offline correction for one blocked journey attempt that never started.

    Only when saved evidence proves that nothing but read-only preflight calls and at most
    one successful preview of the stopped employee were sent (no update, action lookup,
    start, mutation intent or cleanup obligation), the attempt is marked finished with
    cleanup 'not_required'; a preview-only record says no read-back followed the preview.
    This is never a confirmed shutdown or a retrieval result. The original outcome.json and run evidence stay untouched; backups
    and an audit record are written first. The consumed attempt and its calls remain.
    """
    from seren_harness import retrieval_journey
    with home.lock('supervisor', False) as supervisor, home.lock('work', False) as worker:
        if not supervisor or not worker:
            raise Refused('discovery busy; wait without interrupting')
        path = home.root / 'live-ledger.json'
        ledger = read(path)
        matches = [a for a in ledger['attempts'] if a.get('id') == attempt_id]
        if len(matches) != 1:
            raise Refused('attempt not found exactly once')
        entry = matches[0]
        if (entry.get('status') != 'blocked' or not entry.get('journey') or entry.get('error')
                or entry.get('restore') != {'status': 'not_needed'} or entry.get('rebinding')
                or entry.get('cleanup_confirmed') is not False):
            raise Refused('attempt is not a blocked, unmutated journey attempt')
        folder = home.root / 'live-attempts' / attempt_id
        transport = folder / 'transport-receipts.json'
        sent = read(transport)
        previewed = sent != []
        if previewed and not retrieval_journey.preview_only(sent, TARGET):
            raise Refused('a transport request other than one stopped, successful preview was '
                          'sent; cleanup obligations stay')
        run_dir = Path(entry['report']).parents[1]
        receipts = read(run_dir / 'receipts.json')
        if (len(receipts) + len(sent) != entry.get('calls')
                or any(r.get('kind') not in retrieval_journey.READ_ONLY_KINDS for r in receipts)):
            raise Refused('saved receipts are not exactly the read-only calls journaled')
        authority = load_authority(home)
        store = Store(authority['session']['state_dir'])
        try:
            try:
                run = store.run(entry['run_id'])
            except StateError:
                run = None
            if (run is None or run['config_digest'] != attempt_id or run['status'] != 'complete'
                    or store.actions(entry['run_id']) or store.db.execute(
                        'SELECT COUNT(*) FROM cleanup WHERE run_id=?', (entry['run_id'],)).fetchone()[0]):
                raise Refused('store shows a start, mutation intent or cleanup for this run')
        finally:
            store.close()
        stamp = utc_now()
        suffix = attempt_id[:8] + '-' + stamp.replace(':', '').replace('.', '')
        backups = {'ledger': str(home.root / ('live-ledger.before-reconcile-' + suffix + '.json'))}
        Path(backups['ledger']).write_bytes(path.read_bytes())
        handoff = home.root / 'claude-live-request.json'
        if handoff.exists():
            backups['handoff'] = str(home.root / ('claude-live-request.before-reconcile-' + suffix + '.json'))
            Path(backups['handoff']).write_bytes(handoff.read_bytes())
        before_usage = usage(authority['session'], ledger, stamp, authority['runs_per_day'],
                             authority['calls_per_day'])
        previous = {k: entry.get(k) for k in ('status', 'cleanup_confirmed', 'classification_detail')}
        detail = dict(entry.get('classification_detail') or {}, cleanup_open=False)
        detail['reasons'] = [r for r in detail.get('reasons', []) if not r.startswith('cleanup unconfirmed')]
        detail['reasons'].append(
            'no cleanup required: execution never started and no mutation was sent'
            + ('; one preview was sent and no read-back followed it' if previewed else
               ' and nothing changed') + '; shutdown was not observed')
        evidence = {name: {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                    for name, p in (('run_receipts', run_dir / 'receipts.json'),
                                    ('transport_receipts', transport),
                                    ('original_outcome', folder / 'outcome.json'))}
        entry.update(status='finished', classification_detail=detail, cleanup=dict(
            retrieval_journey.NOT_REQUIRED_AFTER_PREVIEW if previewed else retrieval_journey.NOT_REQUIRED))
        entry['reconciliation'] = {
            'at': stamp, 'kind': 'cleanup_not_required',
            'basis': ('read-only preflight calls and one successful preview of the stopped '
                      'employee were sent; no update, start, mutation intent or cleanup obligation '
                      'exists; the retrieval itself was never attempted' if previewed else
                      'only read-only preflight calls were sent; no start, preview, mutation intent '
                      'or cleanup obligation exists; the retrieval itself was never attempted'),
            'previous': previous, 'evidence': evidence, 'backups': backups,
            'record': str(folder / 'reconciliation.json')}
        after_usage = usage(authority['session'], ledger, stamp, authority['runs_per_day'],
                            authority['calls_per_day'])
        write(folder / 'reconciliation.json', {'attempt': attempt_id, 'entry_after': entry,
                                               'usage_before': before_usage, 'usage_after': after_usage})
        write(path, ledger)
        if handoff.exists():
            item = read(handoff)
            children = item.get('items', [item])
            for child in children:
                if (child.get('result') or {}).get('id') == attempt_id:
                    child.update(status='finished', result=entry, reconciled_at=stamp)
            if 'items' in item and all(c.get('status') != 'blocked' for c in children):
                item['status'] = 'finished'
            write(handoff, item)
        return {'attempt': attempt_id, 'usage_before': before_usage, 'usage_after': after_usage,
                'backups': backups, 'record': str(folder / 'reconciliation.json')}


def status(home):
    a = load_authority(home)
    ledger = read(home.root / 'live-ledger.json')
    result = usage(a['session'], ledger, utc_now(), a['runs_per_day'], a['calls_per_day'])
    result['attempts'] = ledger['attempts']
    result['stop_switch'] = home.stop_path.exists() or (home.root / 'LIVE_STOP').exists()
    store = Store(a['session']['state_dir'])
    try:
        gates(store, ledger)
    except Refused as error:
        result['blocker'] = str(error)
    finally:
        store.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'run', 'reconcile-not-started'])
    parser.add_argument('--attempt')
    parser.add_argument('--home')
    parser.add_argument('--case')
    parser.add_argument('--rationale', default='')
    parser.add_argument('--case-digest')
    parser.add_argument('--journey')
    parser.add_argument('--journey-digest')
    args = parser.parse_args()
    try:
        home = discovery.Home(args.home)
        # Executor progress is diagnostic output; stdout is the wrapper's JSON protocol.
        with redirect_stdout(sys.stderr):
            journey = {'journey': args.journey} if args.journey else {}
            result = status(home) if args.action == 'status' else reconcile_not_started(
                home, args.attempt) if args.action == 'reconcile-not-started' else execute(
                home, args.case, args.rationale,
                args.journey_digest if args.journey else args.case_digest, **journey)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        print('Live discovery refused: ' + type(error).__name__ +
              (': ' + str(error) if isinstance(error, Refused) else ' (inspect local evidence)'))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
