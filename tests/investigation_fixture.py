"""Local investigation metadata shared by gate/handoff tests."""
import json

PLAN = dict(customer_job='Finance reviewer decides which invoices need collection.',
            consequence='Wrong balances could cause an unsupported collection request.',
            hypothesis='A changed serving revision may misread cumulative snapshot payments.',
            evidence='Synthetic test fixture supplies a documented changed-revision signal.',
            new_information='Compare the changed revision against the retained previous answer.',
            alternative='A calendar boundary repeat would not isolate the changed payment behavior.',
            decision_if_pass='Keep the scoped observation; do not claim production reliability.',
            decision_if_fail='Record exact incorrect balances and stop for evidence review.',
            basis='relevant_change',
            repeat_reason='Test-only changed revision gives a specific signal; not a real service observation.')
RATIONALE = json.dumps(PLAN)


def live_state(home, runs_remaining=90):
    """Saved-state stand-ins: the standing Clock Test scope and an available budget."""
    from seren_harness import discovery
    discovery.write_json(home.root / 'live-authority.json', {
        'approved': True, 'session': {'execution_scope': 'supplied-answer-finance-v1'}})
    discovery.write_json(home.root / 'claude-live-status.json', {'runs_remaining': runs_remaining})


def comparison(home, selected=None):
    """A structurally valid comparison built from the current catalog and saved state."""
    from seren_harness import discovery, journeys
    catalog = journeys.load()
    selected = selected or journeys.SUPPORTING
    rows = journeys.compare(catalog, discovery.read_json(home.root / 'live-authority.json', {}),
                            discovery.read_json(home.root / 'claude-live-status.json', {}))
    considered = []
    for row in rows:
        if row['id'] != selected and (row['priority'] == 'supporting' or row['state'] == 'completed'):
            continue
        e = journeys.entry(catalog, row['id'])
        ref = (e['prerequisites'] or e['prior_evidence'])[0]
        considered.append({
            'journey': row['id'], 'state': row['state'], 'fingerprint': row['fingerprint'],
            'evidence': 'Saved state cites ' + ref.get('id', ref.get('source', '')) + ' for this route.',
            'customer_consequence': 'Consequence specific to ' + row['id'] + ' for its customer task.',
            'new_information': 'Information only ' + row['id'] + ' would add beyond saved evidence.',
            'decision_changed': 'The decision ' + row['id'] + ' would change for the owner next.'})
    return json.dumps({'considered': considered, 'customer_justification':
                       'A finance reviewer relies on this distinct reconciliation rule, untested so far.'})
