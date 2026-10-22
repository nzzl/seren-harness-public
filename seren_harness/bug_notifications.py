"""Owner-authorized, destination-bound new-bug alerts; trusted supervisor only."""
import json
import re
import subprocess
import urllib.request

from seren_harness import journeys
from seren_harness.sanitize import Sanitizer

GUILD = '100000000000000005'
CHANNEL = '100000000000000001'
KEYCHAIN_SERVICE = 'seren-harness-discord-reader'
API = 'https://discord.com/api/v10'


def candidates(home, state):
    found = {}
    for key, item in state.get('findings', {}).items():
        if item.get('disposition') == 'reproduced_client_defect' and item.get('grade') == 'stated':
            found[key] = dict(summary=item['summary'], impact=item.get('impact'),
                              workaround=item.get('workaround'),
                              classification='Client defect reproduced with the pinned CLI '
                                             'against local fake APIs',
                              provenance='Input: synthetic fake-API replies; execution: no '
                                         'live Seren service',
                              short_classification='Client defect on fake APIs',
                              short_provenance='synthetic replies · no live run',
                              scope='Reproduced CLI defect against local fake APIs; production impact unverified.')
    reviews = [json.loads(p.read_text()) for p in (home.root / 'live-reviews').glob('*.json')]
    for item in sorted(reviews, key=lambda x: (x.get('reviewed_at', ''), x.get('id', ''))):
        if item.get('notification_classification') == 'confirmed_bug':
            kind = item.get('classification') or {}
            found['live-' + item['notification_key']] = dict(
                classification=kind.get('label'),
                provenance=('Input: %s; execution: %s' % (kind['input'], kind['execution'])
                            if kind.get('input') else None),
                short_classification=journeys.SHORT.get(kind.get('reviewed')),
                short_provenance=kind.get('short'),
                summary=item.get('summary') or item['review'], impact=item.get('customer_impact'),
                evidence=item['review'],
                workaround=item.get('workaround'),
                scope=item.get('evidence_limits') or 'Claude-reviewed live finding. Attribution is limited to the saved review; '
                      'customer exposure and production prevalence are not established.')
    return found


def issue_link(policy, key):
    """Only an exact finding-key mapping to this owner's issue tracker is usable."""
    links = policy.get('issue_links', {})
    url = links.get(key) if isinstance(links, dict) else None
    if isinstance(url, str) and re.fullmatch(
            r'https://github\.com/example-owner/seren-harness/issues/[1-9][0-9]{0,11}', url):
        return url
    return None


def format_notification(key, item, github_issue=None):
    """Bound each section without cutting away qualifications or inventing impact."""
    sanitizer = Sanitizer()

    def section(value, limit, fallback):
        text = sanitizer.text(str(value or fallback)).strip()
        if len(text) > limit:
            return 'Too long to quote safely here; consult the full local finding/review using the evidence key below.'
        return text

    title = section(key, 100, 'Confirmed finding')
    summary = section(item.get('summary'), 400, 'See the saved reproduction.')
    impact = section(item.get('impact'), 400,
                     'Not separately established. Consult the saved review; do not infer customer exposure.')
    workaround = section(item.get('workaround'), 200, 'No verified workaround recorded.')
    scope = section(item.get('scope'), 300, 'Scope and attribution remain limited to the saved evidence.')
    kind = section(item.get('short_classification'), 45, 'Classification not recorded')
    source = section(item.get('short_provenance'), 32, 'provenance not recorded')
    tracking = (f'**GitHub tracking:** [example-owner/seren-harness #{github_issue.rsplit("/", 1)[-1]}]'
                f'({github_issue})\n\n') if github_issue else ''
    return (f'## Bug finding · {title}\n\n'
            f'### Expected → observed\n{summary}\n\n'
            f'### Customer impact\n{impact}\n\n'
            f'**Workaround**\n{workaround}\n\n'
            f'### Evidence & limits\n**{kind}** · {source}\n{scope}\nEvidence key: `{title}`\n'
            'Full reproduction and qualifications are retained in the private discovery records.\n\n'
            f'{tracking}'
            '-# Private owner tracking · Public or upstream disclosure requires separate approval.')


def token():
    result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s',
                             KEYCHAIN_SERVICE, '-w'], capture_output=True, text=True, timeout=10)
    if result.returncode or not result.stdout.strip():
        raise ValueError('bot credential unavailable')
    return result.stdout.strip()


def request(path, secret, body=None):
    headers = {'Authorization': 'Bot ' + secret, 'User-Agent': 'SerenHarnessNotifications/1.0'}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    req = urllib.request.Request(API + path, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def deliver(home, state, get_token=token, api=request, issue_api=None):
    """Caller owns supervisor lock and has established no worker is active.

    At most one alert per tick. Intent is persisted before any network operation;
    even an interrupted or uncertain send is never automatically replayed.
    """
    from seren_harness.discovery import read_json, write_json, iso
    if home.stop_path.exists() or (home.root / 'LIVE_STOP').exists():
        return
    policy = read_json(home.root / 'bug-notifications.json', {})
    if policy.get('enabled') is not True:
        return
    if policy.get('guild_id') != GUILD or policy.get('channel_id') != CHANNEL:
        raise ValueError('notification destination differs from owner authorization')
    path = home.root / 'bug-notification-ledger.json'
    ledger = read_json(path)  # absence is a stop, never a silent replay of old findings
    if not isinstance(ledger, dict) or not isinstance(ledger.get('alerts'), dict):
        raise ValueError('notification ledger missing or invalid')
    available = candidates(home, state)
    pending = [key for key in available if key not in ledger['alerts']]
    if not pending:
        return
    key = pending[0]
    url = issue_link(policy, key)
    if 'private_issues' in policy:
        from seren_harness import private_issues
        try:
            url = private_issues.ensure_issue(home, policy, key, available[key],
                                               api=issue_api or private_issues.github)
        except Exception as error:
            state['bug_notification_status'] = {'finding': key, 'status': 'issue_blocked',
                                                'error': type(error).__name__, 'at': iso()}
            return  # Never send a linkless alert or consume its delivery intent.
    content = format_notification(key, available[key], url)
    if len(content) > 2000:
        raise ValueError('notification exceeds Discord limit; do not truncate evidence')
    entry = {'status': 'attempting', 'started_at': iso(), 'channel_id': CHANNEL,
             'content': content}
    ledger['alerts'][key] = entry
    write_json(path, ledger)
    posting = False
    try:
        secret = get_token()
        channel = api('/channels/' + CHANNEL, secret)
        if channel.get('id') != CHANNEL or channel.get('guild_id') != GUILD or channel.get('type') != 0:
            raise ValueError('channel identity mismatch')
        content = Sanitizer([secret]).text(content)
        entry['content'] = content
        write_json(path, ledger)
        posting = True
        reply = api('/channels/' + CHANNEL + '/messages', secret,
                    {'content': content, 'allowed_mentions': {'parse': []}})
        if reply.get('channel_id') != CHANNEL or not str(reply.get('id', '')).isdigit():
            raise ValueError('message receipt identity missing')
        entry.update(status='sent', message_id=reply['id'])
    except Exception as error:
        entry.update(status='uncertain' if posting else 'blocked', error=type(error).__name__)
    entry['ended_at'] = iso()
    write_json(path, ledger)
    state['bug_notification_status'] = {'finding': key, 'status': entry['status'], 'at': iso()}
    home.event(state, 'Owner Discord bug alert %s: %s' % (key, entry['status']))
