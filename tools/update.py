"""Read-only inspection of what an installed MAVEN home needs to match its checkout.

Covers local software and harness skills, the service stack and database schema.
The administration controller applies cloud changes only after their own reviews.
"""
from __future__ import annotations

import base64
import csv
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
    from tools.harness import installed_targets, recorded_integration_plan, skill_bodies, skill_cutover_plan
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
    targets = installed_targets(home)
    harnesses = sorted({name for name, _, _ in targets['profiles']})
    roots = targets['skill_roots']
    templates = checkout / 'maven_resources/templates'
    bodies = skill_bodies(templates / 'skills') if roots else {}
    conflicts = []
    skills, retired, _ = skill_cutover_plan(home, roots, bodies=bodies, conflicts=conflicts)
    pending = pending_owned_skills(home)
    stale = sorted([str(path) for path, _, prior in skills if prior is not None] +
                   [item['target'] for item in conflicts])
    missing = sorted(str(path) for path, _, prior in skills if prior is None)
    if harnesses and not roots:
        missing.extend(f'{name}: no recorded skill root' for name in harnesses)
        conflicts.extend({'target': name, 'component': 'skill', 'status': 'conflict',
                          'reason': 'No canonical skill root recorded for this profile; select --skill-dir.'}
                         for name in harnesses)
    guidance = (templates / 'harness/startup.md').read_text() if harnesses else ''
    integrations = recorded_integration_plan(home, targets, guidance=guidance)
    changed_integrations = [str(change.path) for change in integrations if not change.receipt.get('conflict')
                            and (change.before != change.after or change.receipt.get('retired'))]
    conflicts.extend({'target': str(change.path), 'component': change.receipt.get('target'),
                      'harness': change.receipt.get('harness'), 'reason': change.receipt['conflict']} for change in integrations
                     if change.receipt.get('conflict'))
    metadata_current = _package_inputs_current(home, checkout)
    integrity_failures = _installed_integrity(home / 'venv')
    current = software['installed'] and metadata_current and not integrity_failures and not (
        software['changed'] or software['added'] or software['removed'] or skills or retired or changed_integrations
        or conflicts or pending or targets['unresolved'])
    return {'checkout': str(checkout), 'home': str(home), 'software': software,
            'package_metadata_current': metadata_current,
            'integrity_failures': integrity_failures,
            'skills': {'stale': stale, 'missing': missing, 'retire': [str(path) for path, _ in retired]},
            'harnesses': harnesses, 'integration_changes': changed_integrations, 'preserved': conflicts,
            'integration_inventory': [
                {'harness': c.receipt.get('harness'), 'component': c.receipt.get('target'), 'target': str(c.path),
                 'status': ('conflict' if c.receipt.get('conflict') else 'retire' if c.receipt.get('retired') else
                            'update' if c.before != c.after else 'current')}
                for c in integrations],
            'skills_current': not (stale or missing or retired or pending),
            'unfinished_files': [saved['intent']['target'] for saved in pending],
            'unresolved_integration_targets': targets['unresolved'],
            'git': _git(checkout), 'current': current,
            'limitations': ['Installed package bytes and recorded installation inputs are compared; '
                            'dependency environment health is not verified.',
                            'Git ahead/behind uses the last fetch; nothing was fetched.',
                            'Restart affected harnesses to load updated software and workspace guidance.']}


def install_command(home: Path, checkout: Path) -> list[str]:
    # The guided "Update local MAVEN software" task reads the complete private
    # inventory at review time, including multiple profiles of one harness.
    return [sys.executable, str(Path(checkout) / 'install.py'), '--home', str(home)]


def pending_owned_skills(home, targets=None):
    """Return reviewed candidates in scope; obsolete intents can close but never write."""
    from tools.uninstall.common import read_private
    from tools.harness import SKILL_FILES, skill_root, skill_bodies
    allowed = None if targets is None else set(map(str, targets))
    bodies = None
    result = []
    for path in sorted((Path(home) / 'runtime/skill-installations').glob('*.json')):
        intent = read_private(path)
        if intent.get('completed'):
            continue
        if intent.get('schema') != 1 or intent.get('home') != str(home):
            raise ValueError('Skill installation intent belongs to another home or schema.')
        target = Path(intent['target'])
        root = skill_root(target)
        if target == Path(home) / 'activate':
            obsolete = False
        else:
            relative = target.relative_to(root).as_posix() if root is not None else None
            if relative in SKILL_FILES:
                if bodies is None:
                    bodies = skill_bodies()
                obsolete = base64.b64decode(intent['body'], validate=True) != bodies[relative]
            else:
                obsolete = True
        if obsolete or allowed is None or str(target) in allowed:
            result.append({'path': str(path), 'intent': intent,
                           **({'disposition': 'retire obsolete intent without writing its target'} if obsolete else {})})
    return result


def apply_owned_skills(home, source, plan, references, *, activation=False):
    """Apply only reviewed leaves; pin publication and check sharing under the installation locks."""
    from contextlib import nullcontext
    from tools.harness import _read, _active_manifest, skill_root, SKILL_FILES, write_skill
    from tools.uninstall.common import save_private
    from tools.uninstall.local import _shared_skills, _sharing_locks, inventory, record_installation
    home = Path(home)
    references = set(map(str, references))
    allowed = references | ({str(home / 'activate')} if activation else set())
    selected = {str(target): (target, body, expected) for target, body, expected in plan}
    pending = {}
    directory = home / 'runtime/skill-installations'
    saved_intents = pending_owned_skills(home, allowed)
    if not selected and not saved_intents:
        return
    lock_shared = bool(references and (selected or any(not item.get('disposition') for item in saved_intents)))
    with _sharing_locks(home, []) if lock_shared else nullcontext():
        for saved in saved_intents:
            intent, journal = saved['intent'], Path(saved['path'])
            key = intent['target']
            if saved.get('disposition'):
                save_private(journal, {**intent, 'completed': True, 'outcome': saved['disposition']})
                continue
            if key in pending:
                raise ValueError(f'Ambiguous unfinished skill intents; preserved: {key}')
            pending[key] = journal, intent
            candidate = Path(key), base64.b64decode(intent['body'], validate=True), intent['expected']
            if key in selected and selected[key] != candidate:
                raise ValueError(f'Interrupted skill candidate changed; original intent retained: {key}')
            selected[key] = candidate
        shared, unknown = _shared_skills(home, []) if lock_shared else (set(), False)
        for key, (target, body, expected) in selected.items():
            root = skill_root(target)
            is_skill = root is not None and target.relative_to(root).as_posix() in SKILL_FILES
            if key not in allowed or not (is_skill or (activation and target == home / 'activate')):
                raise ValueError(f'Unreviewed file target; preserved: {target}')
            if is_skill and (key in shared or unknown):
                raise ValueError(f'Shared or uncertain skill ownership; preserved: {target}')
            manifest = _active_manifest(home)
            recorded = manifest.get('owned', {}).get(key)
            if is_skill and recorded is not None and key not in manifest.get('skill_references', []):
                raise ValueError(f'Ambiguous installer ownership; preserved: {target}')
            before = _read(target)
            actual = inventory(target) if before is not None else None
            if key in pending:
                path, intent = pending[key]
                if (before == body and actual is not None and actual in (intent.get('published'), recorded)
                        and recorded in (expected, actual)):
                    record_installation(home, intent['source'], [target], [target] if is_skill else [],
                                        registry_locked=lock_shared, pinned={key: actual})
                    save_private(path, {**intent, 'completed': True})
                    continue
            else:
                path = directory / (str(uuid4()) + '.json')
                intent = {'schema': 1, 'home': str(home), 'source': str(source), 'target': key,
                          'body': base64.b64encode(body).decode(), 'expected': expected,
                          'skill_reference': is_skill, 'completed': False}
            if recorded != expected or actual != expected:
                raise ValueError(f'Skill or ownership changed since review; preserved: {target}')
            save_private(path, intent)

            def publishing(pin):
                intent['published'] = pin
                save_private(path, intent)

            write_skill(target, body, expected, publishing=publishing)
            record_installation(home, intent['source'], [target], [target] if is_skill else [],
                                registry_locked=lock_shared, pinned={key: intent['published']})
            save_private(path, {**intent, 'completed': True})


def install_skill_bundle(home, source, roots, writes, retirements):
    """Recheck the exact cutover review and verify publication before callers change guidance or retire."""
    from tools.harness import SKILL_FILES, retired_skill_plan, skill_plan, verify_skill_bundle
    # Roots that were blocked at review are never passed here, even if a conflict goes away.
    for planned, reviewed in ((skill_plan(roots, home), writes), (retired_skill_plan(home, roots), retirements)):
        current = {item[0]: item[1:] for item in planned}
        prior = {item[0]: item[1:] for item in reviewed}
        if current != prior:
            changed = next(path for path in sorted(current.keys() | prior.keys()) if current.get(path) != prior.get(path))
            raise ValueError(f'Skill changed since review; preserved: {changed}')
    apply_owned_skills(home, source, writes, [root / name for root in roots for name in SKILL_FILES])
    verify_skill_bundle(home, roots)


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
