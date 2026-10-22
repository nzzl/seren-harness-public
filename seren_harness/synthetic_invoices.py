"""Bounded synthetic data under frozen invoice rules; independent Decimal oracle."""
import csv
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import re

from seren_harness.investigation import proposal

ROOT = Path(__file__).resolve().parents[1] / 'docs/synthetic-cases'
COLUMNS = ('invoice_id', 'revision', 'due_date', 'amount', 'paid', 'currency', 'status')
AS_OF = date(2026, 10, 14)


def path(key):
    if not isinstance(key, str) or not re.fullmatch(r'synthetic-[a-z0-9][a-z0-9-]{0,59}', key):
        raise ValueError('synthetic case id must be synthetic-<lowercase-name>')
    result = ROOT / (key + '.json')
    if result.is_symlink() or result.resolve().parent != ROOT.resolve():
        raise ValueError('synthetic case must be a regular local file')
    return result


def known(key):
    return isinstance(key, str) and key.startswith('synthetic-') and path(key).is_file()


def load(key):
    file = path(key)
    if file.stat().st_size > 32000:
        raise ValueError('synthetic case exceeds 32 KB')
    data = json.loads(file.read_text())
    if not isinstance(data, dict) or set(data) != {'id', 'proposal', 'rows', 'expected'} or data['id'] != key:
        raise ValueError('only id, proposal, rows and expected are allowed; id must match filename')
    rationale = proposal(json.dumps(data['proposal']))
    rows = data['rows']
    if not isinstance(rows, list) or not 2 <= len(rows) <= 24:
        raise ValueError('synthetic cases require 2-24 rows')
    groups = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(COLUMNS) or any(not isinstance(v, str) for v in row.values()):
            raise ValueError('exact typed invoice columns required')
        if not re.fullmatch(r'SYN-[A-Z0-9]{1,12}', row['invoice_id']):
            raise ValueError('only synthetic SYN- identifiers allowed')
        if row['revision'] and not re.fullmatch(r'[1-9][0-9]{0,2}', row['revision']):
            raise ValueError('revision must be blank or 1-999')
        if row['status'] != 'issued' or row['currency'] not in ('USD', ''):
            raise ValueError('issued synthetic USD snapshots only')
        for field in ('amount', 'paid'):
            if not row[field] and field == 'paid':
                continue
            if not re.fullmatch(r'[0-9]{1,7}\.[0-9]{2}', row[field]):
                raise ValueError('bounded nonnegative money required')
        if row['paid'] and Decimal(row['paid']) > Decimal(row['amount']):
            raise ValueError('overpayments outside frozen rules')
        if row['due_date']:
            due = date.fromisoformat(row['due_date'])
            if due.isoformat() != row['due_date'] or not 2020 <= due.year <= 2030:
                raise ValueError('bounded ISO due date required')
        groups.setdefault(row['invoice_id'], []).append(row)
    resolved, issues = [], []
    outstanding = overdue_total = Decimal('0')
    for ident, copies in sorted(groups.items()):
        revisions = {bool(r['revision']) for r in copies}
        if len(revisions) != 1:
            raise ValueError('mixed blank/nonblank revisions outside frozen rules')
        versioned = True in revisions
        if versioned:
            latest = max(int(r['revision']) for r in copies)
            copies = [r for r in copies if int(r['revision']) == latest]
        unique = {tuple(r[c] for c in COLUMNS) for r in copies}
        if len(unique) > 1:
            issues.append((ident, 'conflicting_latest_revision' if versioned else 'conflicting_records'))
            continue
        row = copies[0]
        missing = [c for c in ('paid', 'due_date', 'currency') if not row[c]]
        if len(missing) > 1:
            raise ValueError('multiple missing fields have no unambiguous frozen reason')
        if missing:
            issues.append((ident, 'missing_' + missing[0]))
            continue
        balance = Decimal(row['amount']) - Decimal(row['paid'])
        days = (AS_OF - date.fromisoformat(row['due_date'])).days
        late = balance > 0 and days > 0
        resolved.append((ident, format(balance, '.2f'), late, days if late else 0))
        outstanding += balance
        if late:
            overdue_total += balance
    # Preserve the existing needs_review answer schema: mixed supported/unresolved records.
    if not issues or not resolved:
        raise ValueError('case must contain both resolved and unresolved invoices')
    expected = dict(as_of=AS_OF.isoformat(), status='needs_review',
                    resolved_invoices=[dict(invoice_id=i, outstanding_usd=m, overdue=o, days_overdue=d)
                                       for i, m, o, d in resolved],
                    issues=[dict(invoice_id=i, reason=r) for i, r in issues],
                    known_outstanding_usd=format(outstanding, '.2f'),
                    known_overdue_usd=format(overdue_total, '.2f'),
                    complete_outstanding_usd=None, complete_overdue_usd=None)
    if json.dumps(data['expected'], sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError('authored expected answer disagrees with independent frozen-rule oracle')
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return dict(purpose=rationale['hypothesis'], ledger=stream.getvalue(), resolved=resolved,
                issues=issues, known=(format(outstanding, '.2f'), format(overdue_total, '.2f')))


def validate_all():
    return [load(file.stem) for file in sorted(ROOT.glob('*.json'))]
