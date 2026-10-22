"""Single-file operator setup for one bounded, read-only catalog check.

No developer test runner or Git dependency. The existing collector still enforces
CLI pins, allowed arguments, target agreement, evidence review and private storage.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace

from seren_harness import collect
from seren_harness.contract import SUPPORTED_CLI_VERSIONS
from seren_harness.report import _write_private
from seren_harness.sanitize import Sanitizer
from seren_harness.seren_cli import SerenCli
from seren_harness.state import private_directory

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = 'seren.managed-tools.catalog-retrieval'
CASE_FILE = 'seren-managed-tools-catalog-retrieval.json'
FIELDS = {'version', 'seren', 'deployment_id', 'output_dir', 'timeout_seconds'}
CLI_FIELDS = {'executable', 'executable_sha256', 'config_home', 'profile'}


class SetupError(ValueError):
    pass


def _path(value, base, field):
    if not isinstance(value, str) or not value.strip():
        raise SetupError(field + ' must be a filled-in path.')
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load(path):
    """Read only configuration metadata; never read CLI credentials."""
    config_path = Path(path).expanduser().resolve()
    try:
        if config_path.stat().st_size > 16384:
            raise SetupError('Configuration exceeds 16 KB.')
        value = json.loads(config_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SetupError('Cannot read configuration. Copy operator.example.json and fill it in.') from None
    return parse(value, config_path)


def parse(value, config_path, require_deployment=True):
    """Validate the shared fields. The optional `campaign` section belongs to the functional
    campaign (functional.py) and is ignored here; a campaign that creates its own employee
    may leave deployment_id null."""
    if not isinstance(value, dict) or set(value) - {'campaign'} != FIELDS:
        raise SetupError('Use exactly the fields in operator.example.json; do not add credentials.')
    if type(value['version']) is not int or value['version'] != 1:
        raise SetupError('Configuration version must be 1.')
    settings = value['seren']
    if not isinstance(settings, dict) or set(settings) != CLI_FIELDS:
        raise SetupError('The seren section must contain executable, executable_sha256, config_home and profile only.')
    if not isinstance(settings['executable_sha256'], str) or not re.fullmatch(
            r'[0-9a-f]{64}', settings['executable_sha256']):
        raise SetupError('seren.executable_sha256 must be the verified executable SHA-256 (64 lowercase hex characters).')
    profile = settings['profile']
    if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', profile):
        raise SetupError('seren.profile must be a simple profile name, not a path.')
    deployment = value['deployment_id']
    if (require_deployment or deployment is not None) and (
            not isinstance(deployment, str) or not collect.UUID.fullmatch(deployment)
            or deployment == '00000000-0000-0000-0000-000000000000'):
        raise SetupError('deployment_id must be the managed employee UUID, in lowercase.')
    seconds = value['timeout_seconds']
    if type(seconds) is not int or not 1 <= seconds <= 30:
        raise SetupError('timeout_seconds must be an integer from 1 to 30.')
    executable = _path(settings['executable'], config_path.parent, 'seren.executable')
    config_home = _path(settings['config_home'], config_path.parent, 'seren.config_home')
    output = _path(value['output_dir'], config_path.parent, 'output_dir')
    if output == ROOT or ROOT in output.parents:
        raise SetupError('output_dir must be outside the handover folder so live evidence stays out of source control.')
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise SetupError('seren.executable must name an installed executable file.')
    if not config_home.is_dir():
        raise SetupError('seren.config_home must be the existing configuration root used by your Seren CLI login.')
    if output.exists() and not output.is_dir():
        raise SetupError('output_dir already exists as a file; choose a private directory.')
    return dict({k: v for k, v in value.items() if k != 'campaign'},
                seren=dict(settings, executable=str(executable), config_home=str(config_home)),
                output_dir=str(output))


def check(settings):
    """Hash and local --version only. Authentication is deliberately not probed."""
    pinned = settings['seren']
    try:
        if collect._sha256(pinned['executable']) != pinned['executable_sha256']:
            raise SetupError('CLI checksum mismatch. Verify the executable; do not bypass the pin.')
    except OSError:
        raise SetupError('Cannot read the configured CLI executable.') from None
    env = {'HOME': os.environ.get('HOME', ''), 'PATH': '/usr/bin:/bin',
           'XDG_CONFIG_HOME': pinned['config_home'], 'SEREN_PROFILE': pinned['profile']}
    result = SerenCli(pinned['executable'], settings['timeout_seconds'], Sanitizer(), env).version()
    versions = re.findall(r'\d+\.\d+\.\d+', str(result.data or ''))
    if not result.ok or not versions or versions[0] not in SUPPORTED_CLI_VERSIONS:
        raise SetupError('This handover requires Seren CLI 0.10.0. A different version needs compatibility review.')


def check_output(settings):
    """Prepare private report directories and prove writes work before collecting.

    Use disposable files, not os.access: ACLs, read-only mounts and disk errors must
    fail at the actual operation. Existing reports and database files are untouched.
    """
    output = Path(settings['output_dir'])
    try:
        for directory in (output, output / CASE_ID, output / 'findings'):
            private_directory(directory)
            with tempfile.TemporaryFile(mode='w+b', dir=directory,
                                        prefix='.seren-write-check-') as probe:
                probe.write(b'seren report write check\n')
                probe.flush()
                os.fsync(probe.fileno())
                probe.seek(0)
                if probe.read() != b'seren report write check\n':
                    raise OSError('write verification failed')
    except OSError:
        raise SetupError('output_dir cannot be prepared for private reports. Check that its path and parent directories are directories, writable, and have free space. No service request was made.') from None


def bound_case(settings):
    """Derive request and expectation from one user-selected identity, preserving checks."""
    template = json.loads((ROOT / 'cases' / CASE_FILE).read_text())
    old = template['api']['equals']['deployment_id']
    deployment = settings['deployment_id']

    def bind(value):
        if isinstance(value, str):
            return value.replace(old, deployment)
        if isinstance(value, list):
            return [bind(item) for item in value]
        if isinstance(value, dict):
            return {key: bind(item) for key, item in value.items()}
        return value

    case = bind(template)
    case['version_note'] = 'Recipient target bound from the single operator configuration; response checks retained.'
    case['scope']['statement'] = ('Only the declared tool catalog of recipient-selected deployment '
                                  + deployment + '. Does not establish tool execution or employee task success.')
    case['basis'] = [item for item in case['basis'] if item['kind'] == 'documented_interface']
    case['basis'].append({'kind': 'operator_configuration',
                          'reference': 'Recipient selected this deployment; the returned identity must match.'})
    for finding in case['findings'].values():
        finding['retest'] = 'Repeat test-live with the same operator configuration when authorized.'
    return case


def run(args, out=None):
    out = sys.stdout if out is None else out
    try:
        settings = load(args.config)
        check(settings)
        check_output(settings)
        if args.command == 'check-setup':
            print('Local setup checks passed. No service request was made.', file=out)
            print('Authentication and access remain unverified. Next: test-live with this same --config.', file=out)
            return 0
        # Ephemeral derivation, not a second operator-maintained configuration.
        with tempfile.TemporaryDirectory(prefix='seren-operator-') as temp:
            root = Path(temp)
            (root / 'cases').mkdir(mode=0o700)
            _write_private(root / 'cases' / CASE_FILE, json.dumps(bound_case(settings)))
            target = {'employee': 'Recipient managed employee', 'root': str(root), 'collectors': {
                CASE_ID: dict(settings['seren'], type='seren_cli',
                              timeout_seconds=settings['timeout_seconds'],
                              args=['agent', 'managed-deployment-tools', settings['deployment_id']])}}
            _write_private(root / 'target.json', json.dumps(target))
            print('Reading the selected employee tool catalog once. No start, update or retry.', file=out)
            print('A pass checks catalog retrieval and structure, not employee task performance.', file=out)
            options = SimpleNamespace(case=CASE_ID, target=str(root / 'target.json'),
                                      target_root=None, root=str(root), home=settings['output_dir'],
                                      state_dir=None, from_copy=None, retest=None, no_retry=True)
            return collect.run(options, out=out)
    except SetupError as error:
        print('Setup needs attention: ' + str(error), file=out)
        return collect.INVALID
    except (OSError, ValueError, KeyError, TypeError):
        print('Cannot complete the check. Verify file access and the handover installation. No retry was attempted.', file=out)
        return collect.INVALID
