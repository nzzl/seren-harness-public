"""Claude selects; the trusted existing wrapper executes through the live gate."""
import json
import os
import re
from pathlib import Path
import subprocess
import time
import uuid

from seren_harness import discovery, invoice_catalog, journeys, live_discovery, investigation


def prepare(case, rationale, journey=None, home=None):
    if journey:
        return prepare_journey(home, journey, rationale)
    if not invoice_catalog.known(case) or not (rationale or '').strip():
        raise ValueError('frozen catalog case and customer evidence rationale required')
    plan = investigation.proposal(rationale)
    if not case.startswith('synthetic-') and plan['basis'] == 'new_hypothesis':
        raise ValueError('existing catalog reruns need a specific repeat basis, not coverage alone')
    invoice_catalog.case(case)
    return {'id': str(uuid.uuid4()), 'case': case, 'rationale': rationale,
            'case_digest': invoice_catalog.digest(case, live_discovery.TARGET), 'status': 'pending'}


def compared(home, text, selected):
    """Validate the request's cross-journey comparison against the saved current state."""
    try:
        value = json.loads(text or '')
    except ValueError as error:
        raise ValueError('--comparison must be the JSON comparison worker status describes') from error
    catalog = journeys.load()
    rows = journeys.compare(catalog, discovery.read_json(home.root / 'live-authority.json', {}),
                            discovery.read_json(home.root / 'claude-live-status.json', {}))
    return journeys.validate_comparison(catalog, rows, value, selected)


def prepare_journey(home, journey_id, rationale):
    """Queue a frozen journey package only when the comparison finds it eligible now."""
    investigation.proposal(rationale)
    catalog = journeys.load()
    entry = journeys.entry(catalog, journey_id)
    if entry['authority']['kind'] != 'journey_grant':
        raise ValueError('only granted journey packages use --journey')
    authority = discovery.read_json(home.root / 'live-authority.json', {})
    status = discovery.read_json(home.root / 'claude-live-status.json', {})
    row = next(r for r in journeys.compare(catalog, authority, status) if r['id'] == journey_id)
    if row['state'] != 'eligible':
        raise ValueError('journey not eligible: ' + '; '.join(row['blockers']))
    return {'id': str(uuid.uuid4()), 'case': journey_id, 'journey': journey_id,
            'journey_digest': journeys.package_digest(entry), 'rationale': rationale,
            'status': 'pending'}


def request(home, session, args):
    path = home.root / 'claude-live-request.json'
    if path.exists():
        raise ValueError('a request already exists; review its result before selecting another')
    batch = getattr(args, 'batch', None)
    if batch:
        if args.case or args.rationale:
            raise ValueError('choose --batch or --case/--rationale')
        entries = json.loads(batch)
        if not isinstance(entries, list) or not 2 <= len(entries) <= 4:
            raise ValueError('batch requires two to four independent investigations')
        prepared = []
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {'case', 'rationale', 'independence'}
                    or not isinstance(entry['independence'], str)
                    or not 40 <= len(entry['independence'].strip()) <= 800):
                raise ValueError('each batch member requires case, rationale and concrete independence')
            child = prepare(entry['case'], entry['rationale'])
            prepared.append(dict(child, independence=entry['independence']))
        if len({e['case_digest'] for e in prepared}) != len(prepared):
            raise ValueError('batch cannot duplicate a case/oracle')
        item = {'id': str(uuid.uuid4()), 'items': prepared, 'status': 'pending'}
        selected = journeys.SUPPORTING
    else:
        journey = getattr(args, 'journey', None)
        if journey and args.case:
            raise ValueError('choose --journey or --case')
        item = prepare(args.case, args.rationale, journey, home)
        selected = journey or journeys.SUPPORTING
    # Every live request records why it outranks the other journeys, checked against state.
    item['comparison'] = compared(home, getattr(args, 'comparison', None), selected)
    item.update(session=session, created_at=discovery.iso())
    discovery.write_json(path, item)
    with home.edit() as state:
        state.setdefault('model', {}).pop('wait', None)
    print('Queued investigations for the trusted wrapper; no live call made here.')
    return 0


def snapshot(home):
    if not (home.root / 'live-authority.json').exists():
        return
    try:
        result = live_discovery.status(home)
    except (ValueError, KeyError, OSError) as error:
        result = {'blocker': type(error).__name__}
    discovery.write_json(home.root / 'claude-live-status.json', result)
    # Local comparison input for the next session; never a wake signal or authority.
    path = home.root / 'journey-status.json'
    try:
        rows = journeys.compare(journeys.load(), discovery.read_json(
            home.root / 'live-authority.json', {}), result)
        rows = journeys.remember(rows, discovery.read_json(path, {}), discovery.iso())
        discovery.write_json(path, {'checked_at': discovery.iso(), 'journeys': rows,
                                    'next_action': journeys.next_action(rows)})
    except (ValueError, KeyError, OSError, TypeError, AttributeError) as error:
        discovery.write_json(path, {'blocker': type(error).__name__})


def execute(home, case, rationale, case_digest, journey=None):
    # launchd runs this wrapper from an exported snapshot without .git. Execute the
    # gate in the real checkout so its clean-tree/full-validation check is meaningful.
    repo = Path(home.authority()['model_worker']['repo'])
    bound = (['--journey', journey, '--journey-digest', case_digest] if journey
             else ['--case', case, '--case-digest', case_digest])
    result = subprocess.run([str(repo / '.venv/bin/python'), '-m',
                             'seren_harness.live_discovery', 'run', '--home', str(home.root),
                             *bound, '--rationale', rationale], cwd=repo,
                            env=dict(os.environ, PYTHONPATH=str(repo)),
                            capture_output=True, text=True)
    if result.returncode:
        raise live_discovery.Refused('gate refused or failed; inspect live ledger and status')
    return json.loads(result.stdout)


def dispatch(home, deadline_epoch):
    """Exclusive handoff across gate calls. Persist intent first; never replay it."""
    with home.lock('live-handoff', False) as mine:
        if not mine:
            return
        path = home.root / 'claude-live-request.json'
        if not path.exists():
            return
        item = json.loads(path.read_text())
        if item['status'] != 'pending':
            return
        # Keep an audit-held request untouched until explicit approval lifts the hold.
        if home.stop_path.exists() or (home.root / 'LIVE_STOP').exists():
            return
        entries = item.get('items', [item])
        if time.time() + live_discovery.WINDOW + 30 > deadline_epoch:
            return
        item['status'] = 'dispatching'
        discovery.write_json(path, item)
        for index, child in enumerate(entries):
            if index and (time.time() + live_discovery.WINDOW + 30 > deadline_epoch
                          or home.stop_path.exists() or (home.root / 'LIVE_STOP').exists()):
                child.update(status='not_run', reason='deadline or stop switch; new review required')
                break
            child['status'] = 'dispatching'
            discovery.write_json(path, item)
            try:
                bound = ({'journey': child['journey']} if child.get('journey') else {})
                child['result'] = execute(home, child['case'], child['rationale'],
                                          child.get('journey_digest') or child['case_digest'],
                                          **bound)
                child['status'] = 'finished' if child['result'].get('status') != 'blocked' else 'blocked'
            except live_discovery.Refused as error:
                child.update(status='refused', reason=str(error))
            except Exception as error:
                child.update(status='blocked', reason=type(error).__name__)
            child['ended_at'] = discovery.iso()
            discovery.write_json(path, item)
            # Only explicit operational success permits the next independent question.
            if (child['status'] != 'finished'
                    or child['result'].get('operational_complete') is not True):
                break
        if 'items' in item:
            for child in entries:
                if child['status'] == 'pending':
                    child.update(status='not_run', reason='batch stopped; review before a new proposal')
            item['status'] = 'blocked' if any(c['status'] == 'blocked' for c in entries) else 'finished'
        item['ended_at'] = discovery.iso()
        discovery.write_json(path, item)
        snapshot(home)
        with home.edit() as state:
            state.setdefault('model', {}).pop('wait', None)


def review(home, session, args):
    path = home.root / 'claude-live-request.json'
    envelope = json.loads(path.read_text())
    item = envelope
    if 'items' in envelope:
        selected = [c for c in envelope['items'] if c['case'] == getattr(args, 'case', None)]
        if len(selected) != 1:
            raise ValueError('batch review requires one matching --case')
        item = dict(selected[0], batch_id=envelope['id'], session=envelope['session'],
                    comparison=envelope.get('comparison'))
        if item.get('reviewed_at'):
            raise ValueError('batch member already reviewed')
    if item['status'] not in ('finished', 'refused', 'not_run'):
        raise ValueError('unfinished/blocked handoff cannot be cleared automatically')
    if not (args.text or '').strip():
        raise ValueError('evidence assessment and next question required')
    automatic = journeys.classify_attempt(item)
    reviewed = getattr(args, 'classification', None) or automatic
    if reviewed not in journeys.LABELS:
        raise ValueError('classification must be one of: ' + ', '.join(journeys.LABELS))
    if getattr(args, 'status', None) == 'confirmed_bug':
        if reviewed not in journeys.CONFIRMABLE or reviewed != automatic:
            raise ValueError('a confirmed bug needs a functional or model-answer classification '
                             'that the structured evidence supports (automatic: %s); incomplete '
                             'or unresolved evidence is never a confirmed bug' % automatic)
        if item['status'] != 'finished':
            raise ValueError('a refused run is not a confirmed bug')
        keys = getattr(args, 'finding', None) or []
        if len(keys) != 1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', keys[0]):
            raise ValueError('one stable --finding bug id required to deduplicate live alerts')
        for field in ('summary', 'customer_impact', 'evidence_limits'):
            value = getattr(args, field, None)
            if not isinstance(value, str) or not 20 <= len(value.strip()) <= 300:
                raise ValueError('confirmed live alerts require 20-300 character summary, customer-impact and evidence-limits')
        item['summary'] = args.summary
        item['evidence_limits'] = args.evidence_limits
        item['notification_key'] = keys[0]
        item['notification_classification'] = 'confirmed_bug'
        item['customer_impact'] = getattr(args, 'customer_impact', None)
        item['workaround'] = getattr(args, 'workaround', None)
    item['classification'] = dict(journeys.provenance(item), automatic=automatic,
                                  reviewed=reviewed, label=journeys.LABELS[reviewed])
    item.update(review=args.text, reviewed_by=session, reviewed_at=discovery.iso())
    discovery.write_json(home.root / 'live-reviews' / (item['id'] + '.json'), item)
    if 'items' in envelope:
        envelope['items'] = [item if c['id'] == item['id'] else c for c in envelope['items']]
        if all(c.get('reviewed_at') for c in envelope['items']):
            path.unlink()
        else:
            discovery.write_json(path, envelope)
    else:
        path.unlink()
    print('Saved assessment; original gate ledger and evidence preserved.')
    return 0
