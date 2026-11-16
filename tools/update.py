"""Read-only inspection of what an installed MAVEN home needs to match its checkout.

Covers local software and harness skills, the deployed service stack, the database schema
and the active owner instructions. Nothing here changes local files or cloud resources;
the administration controller applies each part only after its own reviewed approval.
"""
from __future__ import annotations

import base64
import csv
import difflib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from uuid import uuid4

_PACKAGES = ('tools', 'maven_resources')
_STATEFUL = frozenset(('AWS::RDS::DBCluster', 'AWS::RDS::DBInstance', 'AWS::SecretsManager::Secret',
                       'AWS::S3::Bucket', 'AWS::KMS::Key'))


def _record_path(prefix: Path) -> Path | None:
    matches = sorted(Path(prefix).glob('lib/python*/site-packages/maven_cloud_memory-*.dist-info/RECORD'))
    return matches[0] if len(matches) == 1 else None


def _record_digest(prefix: Path) -> str | None:
    path = _record_path(prefix)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


# The software this process loaded; a later local update changes the RECORD on disk.
_STARTED_RECORD = _record_digest(Path(sys.prefix))


def running_software_stale() -> bool:
    """True when MAVEN was reinstalled after this process started, so its loaded code is old."""
    return _STARTED_RECORD is not None and _record_digest(Path(sys.prefix)) != _STARTED_RECORD


def _installed_files(venv: Path) -> dict[str, str] | None:
    path = _record_path(venv)
    if path is None:
        return None
    result = {}
    for row in csv.reader(io.StringIO(path.read_text(encoding='utf-8'))):
        if len(row) < 2 or row[0].split('/', 1)[0] not in _PACKAGES or '__pycache__' in row[0].split('/'):
            continue
        target = path.parent.parent / row[0]
        if ('..' in Path(row[0]).parts or target.is_symlink()
                or not target.resolve().is_relative_to(path.parent.parent.resolve())):
            result[row[0]] = 'unsafe'
        else:
            result[row[0]] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else 'missing'
    for package in _PACKAGES:
        for target in (path.parent.parent / package).rglob('*'):
            if target.is_file() and '__pycache__' not in target.parts and target.suffix != '.pyc':
                name = target.relative_to(path.parent.parent).as_posix()
                if name not in result:
                    result[name] = 'unrecorded'
    return result


def _packaging_inputs(checkout):
    return {name: hashlib.sha256((Path(checkout) / name).read_bytes()).hexdigest()
            if (Path(checkout) / name).is_file() else None
            for name in ('pyproject.toml', 'README.md', 'LICENSE')}


def record_package_inputs(home: Path, checkout: Path) -> None:
    """Record installation inputs only after pip completed, alongside actual wheel metadata."""
    from tools.uninstall.common import save_private
    record = _record_path(Path(home) / 'venv')
    if record is None:
        raise ValueError('Installed package metadata is missing or ambiguous.')
    metadata = {}
    for name in ('METADATA', 'entry_points.txt'):
        path = record.parent / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Installed package metadata is incomplete or unsafe: ' + name)
        metadata[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    save_private(Path(home) / 'runtime/package-inputs.json', {
        'schema': 1, 'source_inputs': _packaging_inputs(checkout),
        'distribution': str(record.parent.relative_to(Path(home) / 'venv')), 'metadata': metadata})


def _package_inputs_current(home: Path, checkout: Path) -> bool:
    from tools.uninstall.common import read_private
    record = _record_path(home / 'venv')
    receipt_path = home / 'runtime/package-inputs.json'
    if record is None or not receipt_path.is_file():
        return False
    receipt = read_private(receipt_path)
    if (receipt.get('schema') != 1 or receipt.get('source_inputs') != _packaging_inputs(checkout)
            or receipt.get('distribution') != str(record.parent.relative_to(home / 'venv'))):
        return False
    return all((record.parent / name).is_file() and not (record.parent / name).is_symlink()
               and receipt.get('metadata', {}).get(name) ==
               hashlib.sha256((record.parent / name).read_bytes()).hexdigest()
               for name in ('METADATA', 'entry_points.txt'))



def _installed_integrity(venv):
    record = _record_path(venv)
    if record is None:
        return []
    failed = []
    for row in csv.reader(io.StringIO(record.read_text(encoding='utf-8'))):
        if len(row) < 2 or not row[1].startswith('sha256='):
            continue
        target = record.parent.parent / row[0]
        if not target.resolve().is_relative_to(venv.resolve()) or target.is_symlink() or not target.is_file():
            failed.append(row[0])
            continue
        encoded = base64.urlsafe_b64encode(hashlib.sha256(target.read_bytes()).digest()).rstrip(b'=').decode()
        if row[1] != 'sha256=' + encoded:
            failed.append(row[0])
    return failed

def _checkout_files(checkout: Path) -> dict[str, str]:
    """Files the checkout's pyproject packages into the wheel: package modules plus package data."""
    data = tomllib.loads((checkout / 'pyproject.toml').read_text(encoding='utf-8'))
    package_data = data.get('tool', {}).get('setuptools', {}).get('package-data', {})
    paths = set()
    for package in _PACKAGES:
        paths.update(path for path in (checkout / package).rglob('*.py') if '__pycache__' not in path.parts)
    for package, patterns in package_data.items():
        root = checkout / package.replace('.', '/')
        for pattern in patterns:
            paths.update(path for path in root.glob(pattern) if path.is_file())
    return {path.relative_to(checkout).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def _git(checkout: Path) -> dict:
    """Local Git facts only; never fetches, so 'behind' reflects the last fetch."""
    def run(*arguments):
        result = subprocess.run(['git', '-C', str(checkout), *arguments], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        head = run('rev-parse', 'HEAD')
        if head is None:
            return {'repository': False}
        counts = run('rev-list', '--left-right', '--count', 'HEAD...@{upstream}')
        ahead, behind = (int(value) for value in counts.split()) if counts else (None, None)
        return {'repository': True, 'head': head, 'branch': run('rev-parse', '--abbrev-ref', 'HEAD'),
                'upstream': run('rev-parse', '--abbrev-ref', '@{upstream}'), 'ahead': ahead, 'behind': behind,
                'dirty': bool(run('status', '--porcelain')), 'fetched': False}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'repository': False}


def local_report(home: Path, checkout: Path) -> dict:
    """Compare the installed software and installer-owned harness skills with the checkout."""
    from tools.harness import installed_targets
    from tools.uninstall.common import read_private
    from tools.uninstall.local import MANIFEST
    home, checkout = Path(home), Path(checkout)
    if not (checkout / 'install.py').is_file() or not (checkout / 'pyproject.toml').is_file():
        raise ValueError(f'Not a MAVEN checkout (install.py and pyproject.toml required): {checkout}')
    installed = _installed_files(home / 'venv')
    source = _checkout_files(checkout)
    if installed is None:
        software = {'installed': False, 'changed': [], 'added': sorted(source), 'removed': []}
    else:
        software = {'installed': True,
                    'changed': sorted(name for name in source if name in installed and installed[name] != source[name]),
                    'added': sorted(name for name in source if name not in installed),
                    'removed': sorted(name for name in installed if name not in source)}
    manifest = read_private(home / MANIFEST) if (home / MANIFEST).exists() else {}
    templates = checkout / 'maven_resources/templates/skills'
    names = sorted(path.parent.name for path in templates.glob('*/SKILL.md'))
    roots = sorted({str(Path(path).parent.parent) for path in manifest.get('skill_references', [])})
    stale, missing = [], []
    for root in roots:
        for name in names:
            target = Path(root) / name / 'SKILL.md'
            if not target.exists():
                missing.append(str(target))
            elif target.read_bytes() != (templates / name / 'SKILL.md').read_bytes():
                stale.append(str(target))
    targets = installed_targets(home)
    harnesses = sorted(targets['paths'])
    metadata_current = _package_inputs_current(home, checkout)
    integrity_failures = _installed_integrity(home / 'venv')
    current = software['installed'] and metadata_current and not integrity_failures and not (
        software['changed'] or software['added'] or software['removed'])
    return {'checkout': str(checkout), 'home': str(home), 'software': software,
            'package_metadata_current': metadata_current,
            'integrity_failures': integrity_failures,
            'skills': {'stale': stale, 'missing': missing}, 'harnesses': harnesses,
            'skills_current': not (stale or missing),
            'unresolved_integration_targets': targets['unresolved'],
            'git': _git(checkout), 'current': current,
            'limitations': ['Installed package bytes and recorded installation inputs are compared; '
                            'dependency environment health is not verified.',
                            'Git ahead/behind uses the last fetch; nothing was fetched.']}


def install_command(home: Path, checkout: Path) -> list[str]:
    from tools.harness import installed_targets
    targets = installed_targets(home)
    command = [sys.executable, str(Path(checkout) / 'install.py'), '--home', str(home)]
    # Recorded destinations, not today's ambient profile, determine update targets.
    for harness, (config, context) in targets['paths'].items():
        command.extend(['--harness', harness, '--harness-config', f'{harness}={config}',
                        '--harness-context', f'{harness}={context}'])
    for root in targets['skill_roots']:
        command.extend(['--skill-dir', str(root)])
    if not targets['lifecycle']:
        command.append('--no-lifecycle-hooks')
    if targets['statusline'] and 'claude' in targets['paths']:
        command.append('--claude-statusline')
    return command


def pending_owned_skills(home):
    """Read exact unfinished file intents for local review without applying them."""
    from tools.uninstall.common import read_private
    result = []
    for path in sorted((Path(home) / 'runtime/skill-installations').glob('*.json')):
        intent = read_private(path)
        if intent.get('completed'):
            continue
        if intent.get('schema') != 1 or intent.get('home') != str(home):
            raise ValueError('Skill installation intent belongs to another home or schema.')
        result.append({'path': str(path), 'intent': intent})
    return result


def apply_owned_skills(home, source, plan, references):
    """Apply exact journaled skill writes under the caller's installation lock."""
    from tools.harness import _read, write_skill
    from tools.uninstall.common import save_private
    from tools.uninstall.local import inventory, record_installation
    home = Path(home)
    references = {str(path) for path in references}
    directory = home / 'runtime' / 'skill-installations'
    pending = {}
    for saved in pending_owned_skills(home):
        path, intent = Path(saved['path']), saved['intent']
        target = Path(intent['target'])
        body = base64.b64decode(intent['body'], validate=True)
        before = _read(target)
        if before == body:
            record_installation(home, intent['source'], [target],
                                [target] if intent.get('skill_reference', str(target) in references) else [])
            intent['completed'] = True
            save_private(path, intent)
        elif ((before is None and intent['expected'] is None) or
              (before is not None and intent['expected'] is not None and inventory(target) == intent['expected'])):
            pending[str(target)] = (path, intent)
        else:
            raise ValueError(f'Interrupted skill installation changed; original intent retained: {target}')
    selected = {str(target): (target, body, expected) for target, body, expected in plan}
    for target, (_, intent) in pending.items():
        original = base64.b64decode(intent['body'], validate=True)
        if target in selected and selected[target][1] != original:
            raise ValueError('Interrupted skill candidate changed; finish the original installation first.')
        selected[target] = (Path(target), original, intent['expected'])
    for key, (target, body, expected) in selected.items():
        if key in pending:
            path, intent = pending[key]
        else:
            path = directory / (str(uuid4()) + '.json')
            intent = {'schema': 1, 'home': str(home), 'source': str(source), 'target': key,
                      'body': base64.b64encode(body).decode(), 'expected': expected,
                      'skill_reference': key in references, 'completed': False}
            save_private(path, intent)
        write_skill(target, body, expected)
        record_installation(home, intent['source'], [target],
                            [target] if intent.get('skill_reference', str(target) in references) else [])
        intent['completed'] = True
        save_private(path, intent)
    record_installation(home, source, [], references)


def schema_status(values: dict) -> dict:
    """Compare applied SQL migrations with the bundled ordered set (read-only, admin role)."""
    from tools.memory.admin import migrations
    from tools.memory.client import selected_session
    from tools.memory.environment import migration_config, transport_ca_bundle
    from tools.memory.store import DataAPI, safe_provider_error, sdk_config
    from tools.memory.validation import MemoryError
    config = migration_config(values)
    try:
        session = selected_session(config, admin=True, environment=values)
        client = session.client('rds-data', region_name=config['region'], config=sdk_config(),
                                verify=transport_ca_bundle())
        raw = DataAPI(client, config, admin=True).scalar(
            "SELECT COALESCE(jsonb_object_agg(name, sha256), '{}'::jsonb)::text FROM maven.schema_migrations")
    except MemoryError:
        raise
    except Exception as error:
        if getattr(error, 'code', None):
            raise
        raise safe_provider_error(error) from None
    applied = json.loads(raw)
    bundle = [(name, sha) for name, sha, _ in migrations()]
    if applied == dict(bundle):
        return {'state': 'current', 'applied': len(applied)}
    if applied == dict(bundle[:len(applied)]) and len(applied) < len(bundle):
        return {'state': 'pending', 'applied': len(applied), 'pending': [name for name, _ in bundle[len(applied):]]}
    return {'state': 'incompatible', 'applied': sorted(applied),
            'expected': [name for name, _ in bundle]}


def stack_parameters(stack: dict) -> dict:
    return {item['ParameterKey']: item['ParameterValue'] for item in stack.get('Parameters', [])}


def build_service_package(stack: dict, destination: Path, instance_id: str) -> dict:
    """Build the current service template with the live stack's exact deployment inputs."""
    from tools.memory.deployment import build
    parameters = stack_parameters(stack)
    if parameters.get('InstanceId') != instance_id:
        raise ValueError('The stack belongs to a different instance than this installation.')
    zones = parameters.get('AvailabilityZones')
    return build(destination, notification_email=parameters.get('NotificationEmail', ''),
                 engine_version=parameters.get('EngineVersion', '18.6'),
                 instance_name=parameters.get('InstanceName', 'maven-memory'),
                 account_id=parameters.get('ExpectedAccountId') or None,
                 caller_principal_arn=parameters.get('CallerPrincipalArn') or None,
                 instance_id=parameters['InstanceId'], recovery_epoch=parameters.get('RecoveryEpoch'),
                 availability_zones=zones.split(',') if zones else None)


def change_summary(document: dict) -> list[dict]:
    rows = []
    for change in document['review']['change_set'].get('Changes') or []:
        resource = change.get('ResourceChange', {})
        rows.append({'action': resource.get('Action'), 'logical_id': resource.get('LogicalResourceId'),
                     'type': resource.get('ResourceType'), 'replacement': resource.get('Replacement')})
    return rows


def stateful_risks(document: dict) -> list[dict]:
    """Changes that would remove or replace a resource holding memory data or credentials."""
    return [row for row in change_summary(document) if row['type'] in _STATEFUL and
            (row['action'] == 'Remove' or row['replacement'] in ('True', 'Conditional'))]


def _index(client) -> dict[str, dict]:
    from tools.memory.client import MemoryClientError
    pages, after, generation = {}, None, None
    while True:
        request = {} if after is None else {'after_slug': after}
        response = client.invoke('/v1/pages/index', request)
        data = response.get('data')
        if (response.get('status') != 'ready' or not isinstance(data, dict)
                or not isinstance(data.get('pages'), list) or type(data.get('authority_generation')) is not int):
            raise MemoryClientError('invalid_service_response')
        if generation is not None and generation != data['authority_generation']:
            raise MemoryClientError('context_stale')
        generation = data['authority_generation']
        for page in data['pages']:
            pages[page['slug']] = page
        after = data.get('next_after_slug')
        if after is None:
            return pages


def authority_changes(client) -> list[dict]:
    """Bundled instruction pages that are new, or differ from the active cloud version."""
    from tools.memory.onboarding import catalog
    from tools.memory.validation import digest
    candidates, _ = catalog()
    active = _index(client)
    changes = []
    for candidate in candidates:
        current = active.get(candidate['slug'])
        if current is None:
            changes.append({'slug': candidate['slug'], 'title': candidate['title'], 'action': 'add',
                            'page_id': None, 'revision': 0, 'candidate': candidate, 'diff': ''})
        elif current.get('content_hash') != digest(candidate):
            published = client.read_full({'slug': candidate['slug'], 'expected_revision': current['revision']})['data']
            # Omission is not an instruction to erase an owner's approved discovery description.
            if candidate['page_kind'] == 'skill' and 'description' not in candidate and 'description' in published:
                candidate = {**candidate, 'description': published['description']}
            if digest(candidate) == current['content_hash']:
                continue
            diff = ''.join(difflib.unified_diff(published['body_markdown'].splitlines(keepends=True),
                                                candidate['body_markdown'].splitlines(keepends=True),
                                                f'active/{candidate["slug"]}.md', f'bundled/{candidate["slug"]}.md'))
            metadata_keys = ('slug', 'page_kind', 'title', 'scope', 'requires', 'bundle', 'description')
            before = {key: published[key] for key in metadata_keys if key in published}
            before['authority_state'] = published['candidate_authority_state']
            after = {key: value for key, value in candidate.items() if key != 'body_markdown'}
            diff += ''.join(difflib.unified_diff(
                (json.dumps(before, indent=2, sort_keys=True, ensure_ascii=False) + '\n').splitlines(keepends=True),
                (json.dumps(after, indent=2, sort_keys=True, ensure_ascii=False) + '\n').splitlines(keepends=True),
                f'active/{candidate["slug"]}.metadata.json', f'bundled/{candidate["slug"]}.metadata.json'))
            changes.append({'slug': candidate['slug'], 'title': candidate['title'], 'action': 'update',
                            'page_id': current['page_id'], 'revision': current['revision'],
                            'candidate': candidate, 'diff': diff})
    return changes


def prepare_authority_update(destination: Path, changes: list[dict], evidence: dict) -> dict:
    from tools.memory.onboarding import catalog, draft, write_drafts
    _, hashes = catalog()
    drafts = [draft(change['candidate'], evidence, page_id=change['page_id'], expected_revision=change['revision'],
                    reason='Update MAVEN instructions to the installed bundled version')
              for change in changes]
    return write_drafts(destination, drafts, hashes)
