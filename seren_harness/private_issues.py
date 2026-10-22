"""Owner-authorized private issue reporting, with durable no-replay creation intents."""
import hashlib
import json
import os
import re
import html
import subprocess

from seren_harness.sanitize import Sanitizer

REPO = 'example-owner/seren-harness'
REPO_ID = 0
POLICY = {'enabled': False, 'repository': REPO, 'repository_id': REPO_ID, 'require_private': True}


def github(path, body=None):
    env = {k: v for k, v in os.environ.items() if k not in
           ('GH_HOST', 'GH_REPO', 'GH_DEBUG', 'GH_PAGER', 'GH_FORCE_TTY')}
    argv = ['/opt/homebrew/bin/gh', 'api', '--hostname', 'github.com',
            '--method', 'POST' if body is not None else 'GET', path]
    if body is not None:
        argv += ['--input', '-']
    result = subprocess.run(argv, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True, timeout=30, env=env)
    if result.returncode:
        raise ValueError('private GitHub request failed')
    return json.loads(result.stdout)


def marker(key):
    return '<!-- seren-harness-finding:%s -->' % hashlib.sha256(key.encode()).hexdigest()


def issue_title(key):
    label = key
    while label.startswith('live-'):
        label = label[5:]
    return Sanitizer().text(label.replace('-', ' ').capitalize())[:200]


def readable_text(value):
    """Render untrusted prose as short paragraphs, without altering or dropping facts.

    Escape HTML so evidence cannot close the details container or hide qualifications.
    Prefer sentence boundaries; unusually long sentences wrap at word boundaries.
    """
    text = html.escape(Sanitizer().text(str(value)), quote=False)
    paragraphs = []
    for original in text.split('\n\n'):
        paragraph = ''
        for sentence in re.split(r'(?<=[.!?])\s+(?=[A-Z0-9])', original):
            for word in sentence.split():
                if paragraph and len(paragraph) + len(word) + 1 > 500:
                    paragraphs.append(paragraph)
                    paragraph = ''
                paragraph += (' ' if paragraph else '') + word
            if len(paragraph) >= 250:
                paragraphs.append(paragraph)
                paragraph = ''
        if paragraph:
            paragraphs.append(paragraph)
    return '\n\n'.join(paragraphs)


def issue_body(key, item):
    sections = [marker(key),
        '## Expected → observed',
        readable_text(item.get('summary') or 'See detailed evidence below.'),
        '## Classification',
        readable_text(item.get('classification')
                      or 'Not recorded in this legacy review; attribution unresolved.'),
        readable_text(item.get('provenance') or 'Evidence provenance not recorded.'),
        '## Customer impact',
        readable_text(item.get('impact') or 'Not established; exposure unknown.'),
        '## Evidence limits',
        readable_text(item.get('scope') or 'Attribution and prevalence unestablished.'),
        '## Workaround', readable_text(item.get('workaround') or 'None verified.'),
        '<details>\n<summary>Detailed evidence and reproduction notes</summary>',
        readable_text(item.get('evidence') or 'No additional evidence recorded.'),
        '</details>', '---',
        '**Finding key:** `' + Sanitizer().text(key) + '`',
        '*Private owner tracking. No upstream or public disclosure authorized.*']
    return '\n\n'.join(sections)


def ensure_issue(home, policy, key, item, api=github):
    """Caller holds supervisor lock. Never create twice after an uncertain POST.

    Check privacy before every lookup/create. Exact marker matching includes closed issues.
    A bounded incomplete listing blocks creation. Existing explicit mappings are verified.
    """
    from seren_harness.discovery import read_json, write_json, iso
    from seren_harness.bug_notifications import issue_link
    if not POLICY.get('enabled') or policy.get('private_issues') != POLICY:
        raise ValueError('private issue authority missing or invalid')
    path = home.root / 'private-issue-ledger.json'
    ledger = read_json(path)
    if not isinstance(ledger, dict) or not isinstance(ledger.get('issues'), dict):
        raise ValueError('private issue ledger missing')
    repo = api('repos/' + REPO)
    if repo.get('private') is not True or repo.get('id') != REPO_ID or repo.get('full_name') != REPO:
        raise ValueError('issue destination identity or privacy mismatch')
    records = ledger['issues']
    existing = records.get(key, {})
    mapped = issue_link(policy, key)
    url = mapped or existing.get('url')
    match = None
    if url:
        if issue_link({'issue_links': {key: url}}, key) != url:
            raise ValueError('invalid saved issue URL')
        match = api('repos/' + REPO + '/issues/' + url.rsplit('/', 1)[-1])
        if match.get('html_url') != url or match.get('pull_request'):
            raise ValueError('issue identity mismatch')
    else:
        for page in range(1, 6):
            issues = api('repos/' + REPO + '/issues?state=all&per_page=100&page=' + str(page))
            if not isinstance(issues, list):
                raise ValueError('invalid issue listing')
            matches = [i for i in issues if marker(key) in (i.get('body') or '') and not i.get('pull_request')]
            if len(matches) > 1 or match and matches:
                raise ValueError('duplicate issue markers require reconciliation')
            if matches:
                match = matches[0]
            if len(issues) < 100:
                break
        else:
            raise ValueError('issue listing incomplete; no creation')
    if match is None:
        if existing:
            raise ValueError('prior issue creation uncertain; no automatic retry')
        body = issue_body(key, item)
        if len(body) > 60000:
            raise ValueError('issue evidence too long; no truncation')
        records[key] = {'status': 'attempting', 'started_at': iso(),
                        'body_sha256': hashlib.sha256(body.encode()).hexdigest()}
        write_json(path, ledger)
        try:
            match = api('repos/' + REPO + '/issues', {
                'title': issue_title(key), 'body': body})
        except Exception:
            records[key].update(status='uncertain')
            write_json(path, ledger)
            raise ValueError('issue creation outcome uncertain; no automatic retry') from None
    url = match.get('html_url')
    if not issue_link({'issue_links': {key: url}}, key) or match.get('pull_request'):
        raise ValueError('issue receipt identity missing; no automatic retry')
    records[key] = {'status': 'filed', 'url': url, 'verified_at': iso()}
    write_json(path, ledger)
    policy.setdefault('issue_links', {})[key] = url
    write_json(home.root / 'bug-notifications.json', policy)
    return url
