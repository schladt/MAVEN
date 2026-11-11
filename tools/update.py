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
        if len(row) < 2 or not row[1].startswith('sha256=') or row[0].split('/', 1)[0] not in _PACKAGES:
            continue
        encoded = row[1][len('sha256='):]
        result[row[0]] = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).hex()
    return result


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
    from tools.harness import HARNESS_PATHS
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
    configured = {str(Path.home() / paths[0]): name for name, paths in HARNESS_PATHS.items()}
    harnesses = sorted({configured[item['path']] for item in manifest.get('integrations', [])
                        if item.get('path') in configured})
    current = not (software['changed'] or software['added'] or software['removed'] or stale or missing)
    return {'checkout': str(checkout), 'home': str(home), 'software': software,
            'skills': {'stale': stale, 'missing': missing}, 'harnesses': harnesses,
            'git': _git(checkout), 'current': current,
            'limitations': ['Dependency version changes in pyproject.toml are applied by the installer but not '
                            'compared here.', 'Git ahead/behind uses the last fetch; nothing was fetched.']}


def install_command(home: Path, checkout: Path, harnesses: list[str]) -> list[str]:
    command = [sys.executable, str(Path(checkout) / 'install.py'), '--home', str(home)]
    for harness in harnesses:
        command.extend(['--harness', harness])
    return command


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
            body = client.read_full({'slug': candidate['slug']})['data']['body_markdown']
            diff = ''.join(difflib.unified_diff(body.splitlines(keepends=True),
                                                candidate['body_markdown'].splitlines(keepends=True),
                                                f'active/{candidate["slug"]}.md', f'bundled/{candidate["slug"]}.md'))
            changes.append({'slug': candidate['slug'], 'title': candidate['title'], 'action': 'update',
                            'page_id': current['page_id'], 'revision': current['revision'],
                            'candidate': candidate, 'diff': diff or '(metadata such as title or dependencies changed)'})
    return changes


def prepare_authority_update(destination: Path, changes: list[dict], evidence: dict) -> dict:
    from tools.memory.onboarding import catalog, draft, write_drafts
    _, hashes = catalog()
    drafts = [draft(change['candidate'], evidence, page_id=change['page_id'], expected_revision=change['revision'],
                    reason='Update MAVEN instructions to the installed bundled version')
              for change in changes]
    return write_drafts(destination, drafts, hashes)
