"""Stdlib-only, reviewable harness configuration with selective ownership.

Only documented stdio and standing-instruction surfaces are used. Loading
instructions is not an execution hook or a verified project initialization.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile
import tomllib

from maven_resources import resource_path

HARNESS_PATHS = {
    'omp': ('.omp/agent/mcp.json', '.omp/agent/AGENTS.md', '.omp/agent/skills'),
    'claude': ('.claude.json', '.claude/CLAUDE.md', '.claude/skills'),
    'codex': ('.codex/config.toml', '.codex/AGENTS.md', '.agents/skills'),
    'copilot': ('.copilot/mcp-config.json', '.copilot/copilot-instructions.md', '.copilot/skills'),
    'gemini': ('.gemini/settings.json', '.gemini/GEMINI.md', '.gemini/skills'),
}
SKILL_FILES = ('maven/SKILL.md', 'maven/references/project.md', 'maven/references/reconcile.md',
               'maven/references/secrets.md', 'maven/references/update.md')
RETIRED_SKILLS = ('maven-setup', 'maven-connect', 'maven-skill-use', 'maven-start-work',
                  'maven-checkpoint', 'maven-stop-work', 'maven-maintenance',
                  'maven-reconcile', 'maven-secrets', 'maven-update')
_SKILL_SUFFIXES = tuple(Path(name) for name in SKILL_FILES) + tuple(
    Path(name, 'SKILL.md') for name in RETIRED_SKILLS)
CLAUDE_SETTINGS = '.claude/settings.json'
LIFECYCLE_PATHS = {
    'omp': '.omp/agent/extensions/maven-lifecycle.js',
    'claude': CLAUDE_SETTINGS,
    'codex': '.codex/hooks.json',
    'copilot': '.copilot/hooks/maven-lifecycle.json',
    'gemini': '.gemini/settings.json',
}


@dataclass(frozen=True)
class Change:
    path: Path
    before: bytes | None
    after: bytes | None
    receipt: dict | None



def validate_default_profiles(harnesses) -> None:
    """Refuse ambiguous live targets; custom profiles need explicit paths."""
    overrides = {
        'claude': ('CLAUDE_CONFIG_DIR',),
        'omp': ('OMP_PROFILE', 'PI_PROFILE', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR'),
        'codex': ('CODEX_HOME',),
        'copilot': ('COPILOT_HOME',),
        'gemini': ('GEMINI_CLI_HOME',),
    }
    for name in harnesses:
        if any(os.environ.get(key) for key in overrides[name]):
            raise ValueError(f'{name} configuration override is set; select its explicit '
                             '--harness-config, --harness-context and --skill-dir, or stage first.')


def skill_root(target: Path) -> Path | None:
    """Recognize only shipped bundle leaves and explicit historical entry points."""
    if target.is_absolute() and '..' not in target.parts:
        for suffix in _SKILL_SUFFIXES:
            if target.parts[-len(suffix.parts):] == suffix.parts:
                return target.parents[len(suffix.parts) - 1]
    return None


def skill_bodies(directory: Path | None = None) -> dict[str, bytes]:
    directory = directory or resource_path('templates/skills')
    return {name: (directory / name).read_bytes() for name in SKILL_FILES}


def _skill_manifest(home):
    from tools.uninstall.common import read_private
    from tools.uninstall.local import MANIFEST
    receipt = home / MANIFEST
    prior = read_private(receipt) if receipt.exists() else {}
    if prior and (prior.get('schema') != 1 or prior.get('home') != str(home)):
        raise ValueError('Installer ownership receipt does not match this home.')
    return prior if prior.get('active', True) else {}


def skill_plan(roots: list[Path], home: Path | None = None, *,
               conflicts: list[dict] | None = None, bodies: dict[str, bytes] | None = None) -> list[tuple[Path, bytes, dict | None]]:
    """Plan the complete bundle or leave that root alone, including identical unowned files."""
    from tools.uninstall.local import _shared_skills, inventory
    from tools.update import pending_owned_skills
    bodies = skill_bodies() if bodies is None else bodies
    prior = _skill_manifest(home) if home is not None else {}
    references = set(prior.get('skill_references', ()))
    owned = prior.get('owned', {})
    pending = {}
    targets = {str(root / name) for root in roots for name in SKILL_FILES}
    if home is not None and targets:
        for saved in pending_owned_skills(home, targets):
            if not saved.get('disposition'):
                intent = saved['intent']
                pending.setdefault(intent['target'], []).append(intent)
    shared, unknown = _shared_skills(home, []) if home is not None and targets else (set(), False)
    plan = []
    for root in dict.fromkeys(roots):
        candidates, blocked = [], False
        for name in SKILL_FILES:
            target, body = root / name, bodies[name]
            try:
                if skill_root(target) != root or any(p.exists() and not p.is_dir() for p in target.parents):
                    raise ValueError(f'Invalid skill destination; preserved: {target}')
                key = str(target)
                before = _read(target)
                actual = inventory(target) if before is not None else None
                expected = owned.get(key)
                if expected is not None and key not in references:
                    raise ValueError(f'Ambiguous installer ownership; preserved: {target}')
                if key in pending:
                    intents = pending[key]
                    if len(intents) != 1:
                        raise ValueError(f'Ambiguous unfinished skill intents; preserved: {target}')
                    intent = intents[0]
                    if (actual is not None and actual == intent.get('published')
                            and expected in (actual, intent['expected'])):
                        expected = actual
                    elif actual != expected or (expected != intent['expected'] and before != body):
                        raise ValueError(f'Interrupted skill or ownership changed; preserved: {target}')
                if key in shared or unknown:
                    raise ValueError(f'Shared or uncertain skill ownership; preserved: {target}')
                if before is not None:
                    if expected is None or actual != expected:
                        raise ValueError(f'Existing skill differs from installer ownership; preserved: {target}')
                    if before != body:
                        candidates.append((target, body, expected))
                elif expected is not None:
                    raise ValueError(f'Previously owned skill is missing; preserved: {target}')
                else:
                    if name == SKILL_FILES[0] and target.parent.exists() and any(target.parent.iterdir()):
                        raise ValueError(f'Existing nonempty skill directory; not creating {target}')
                    candidates.append((target, body, None))
            except (OSError, ValueError) as error:
                if conflicts is None:
                    raise
                blocked = True
                conflicts.append({'target': str(target), 'status': 'conflict', 'reason': str(error)})
        if not blocked:
            plan.extend(candidates)
    return plan


def write_skill(target: Path, body: bytes, expected: dict | None, *, publishing=None) -> None:
    """Publish reviewed bytes, pinning the file identity before replacing an old version."""
    from tools.uninstall.local import inventory

    def publish(descriptor, path):
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(descriptor, 0o600)
            if publishing:
                # Pin the exclusively created inode, not a later lookalike at this pathname.
                info = os.fstat(descriptor)
                pin = inventory(path)
                if (info.st_dev, info.st_ino) != (pin['']['dev'], pin['']['ino']):
                    raise ValueError(f'Skill changed during publication; preserved: {path}')
                pin[''].update(size=len(body), sha256=hashlib.sha256(body).hexdigest())
                publishing(pin)
            stream.write(body)
            stream.flush()
            os.fsync(descriptor)

    before = _read(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if expected is None:
        if before is not None:
            raise ValueError(f'Skill appeared since review; preserved: {target}')
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        publish(descriptor, target)
        return
    if inventory(target) != expected:
        raise ValueError(f'Skill changed since review; preserved: {target}')
    descriptor, temporary = tempfile.mkstemp(prefix='.maven-skill-', dir=target.parent)
    try:
        publish(descriptor, Path(temporary))
        if _read(target) is None or inventory(target) != expected:
            raise ValueError(f'Skill changed since review; preserved: {target}')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def retired_skill_plan(home: Path, roots: list[Path], *, conflicts: list[dict] | None = None):
    """Retire only exact installer-owned CLI-ritual skills during cutover."""
    from tools.uninstall.local import _shared_skills, inventory
    prior = _skill_manifest(home)
    candidates = [root / name / 'SKILL.md' for root in dict.fromkeys(roots) for name in RETIRED_SKILLS]
    existing = [path for path in candidates if path.exists() or path.is_symlink()
                or str(path) in prior.get('owned', {})]
    if not existing:
        return []
    shared, unknown = _shared_skills(home, [])
    result = []
    for path in existing:
        try:
            before = _read(path)
            expected = prior.get('owned', {}).get(str(path))
            if (not expected or str(path) not in prior.get('skill_references', [])
                    or str(path) in shared or unknown or (before is not None and inventory(path) != expected)):
                raise ValueError(f'Legacy skill is modified, shared or unowned; preserved: {path}. '
                                 'Review its removal separately or use an inactive staging home.')
            result.append((path, expected if before is not None else {'absent': expected}))
        except (OSError, ValueError) as error:
            if conflicts is None:
                raise
            conflicts.append({'target': str(path), 'status': 'conflict', 'reason': str(error)})
    return result


def skill_cutover_plan(home, roots, *, conflicts, bodies=None):
    """Only offer retirements in roots that can receive the entire bundle."""
    writes = skill_plan(roots, home, conflicts=conflicts, bodies=bodies)
    retired = retired_skill_plan(home, roots, conflicts=conflicts)
    blocked = {skill_root(Path(item['target'])) for item in conflicts}
    ready = [root for root in dict.fromkeys(roots) if root not in blocked]
    writes = [item for item in writes if skill_root(item[0]) not in blocked]
    retired = [item for item in retired if skill_root(item[0]) not in blocked]
    return writes, retired, ready


def verify_skill_bundle(home, roots):
    from tools.uninstall.local import _shared_skills, inventory
    if not roots:
        return
    bodies = skill_bodies()
    prior = _skill_manifest(home)
    shared, unknown = _shared_skills(home, [])
    for root in roots:
        for name, body in bodies.items():
            target = root / name
            key = str(target)
            if (_read(target) != body or key in shared or unknown or key not in prior.get('skill_references', [])
                    or inventory(target) != prior.get('owned', {}).get(key)):
                raise ValueError(f'Replacement bundle is not verified; preserved: {target}')


def retire_skills(home, plan) -> None:
    """Remove reviewed legacy files only while their pinned inventory matches."""
    from tools.uninstall.common import read_private, save_private
    from tools.uninstall.local import MANIFEST, _shared_skills, _sharing_locks, inventory
    for path, expected in plan:
        root = skill_root(path)
        if root is None or path.parent.name not in RETIRED_SKILLS:
            raise ValueError(f'Not an explicit retired skill: {path}')
        with _sharing_locks(home, []):
            # Check every leaf, including unchanged files that had no write in the review.
            verify_skill_bundle(home, [root])
            shared, uncertain = _shared_skills(home, [])
            before = _read(path)
            manifest = read_private(home / MANIFEST)
            actual = inventory(path) if before is not None else None
            recorded = manifest['owned'].get(str(path))
            reviewed = expected.get('absent', expected)
            if (str(path) in shared or uncertain or actual != (None if 'absent' in expected else expected)
                    or recorded != reviewed or str(path) not in manifest['skill_references']):
                raise ValueError(f'Legacy skill changed or shared since review; preserved: {path}')
            manifest['skill_references'].remove(str(path))
            del manifest['owned'][str(path)]
            if before is not None:
                path.unlink()
            # rmdir cannot remove owner files. Never traverse or purge a legacy directory.
            directory = path.parent
            if directory.exists() and not directory.is_symlink():
                info = directory.stat()
                if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022:
                    try:
                        directory.rmdir()
                    except OSError as error:
                        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                            raise
            save_private(home / MANIFEST, manifest)


def _read(path: Path) -> bytes | None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Symlink integration target preserved: {part}')
    if not path.exists():
        return None
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise ValueError(f'Unsafe integration target preserved: {path}')
    return path.read_bytes()


@contextmanager
def _launcher(home: Path):
    path = home / 'venv/bin/maven-mcp'
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Symlink launcher target preserved: {part}')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or not mode & 0o100
                or (mode != 0o775 and mode & 0o7022)):
            raise ValueError(f'Unsafe launcher preserved: {path}; require an owner-owned, single-link executable.')
        yield descriptor
    finally:
        os.close(descriptor)


def _launcher_entry(descriptor):
    info = os.fstat(descriptor)
    if info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError('Launcher ownership or link count changed; preserved.')
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while block := os.read(descriptor, 65536):
        digest.update(block)
    after = os.fstat(descriptor)
    if any(getattr(after, key) != getattr(info, key) for key in (
            'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
        raise ValueError('Launcher changed while inspecting it; preserved for review.')
    return {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid,
            'dev': info.st_dev, 'ino': info.st_ino, 'type': 'file',
            'size': info.st_size, 'sha256': digest.hexdigest()}


def launcher_permission_plan(home: Path, *, recorded=None) -> dict | None:
    """Plan the historical 0775 repair, including its already-repaired receipt."""
    with _launcher(home) as descriptor:
        entry = _launcher_entry(descriptor)
    if entry['mode'] == 0o775 or (
            entry['mode'] == 0o755 and recorded == {**entry, 'mode': 0o775}):
        return entry
    return None


def repair_launcher_permissions(home: Path, expected: dict) -> None:
    """Change only the reviewed inode's mode; never follow links or replace bytes."""
    if expected.get('mode') != 0o775:
        raise ValueError('Only an exact 0775 launcher permission repair is supported.')
    with _launcher(home) as descriptor:
        if _launcher_entry(descriptor) != expected:
            raise ValueError('Launcher changed since review; preserved.')
        os.fchmod(descriptor, 0o755)
        os.fsync(descriptor)


def _json(raw: bytes | None) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON configuration key; preserved for review')
            result[key] = value
        return result
    value = json.loads(raw or b'{}', object_pairs_hook=pairs)
    if not isinstance(value, dict) or not isinstance(value.get('mcpServers', {}), dict):
        raise ValueError('Expected a JSON object with an mcpServers object')
    return value


def _encode(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def _block(body: str, *, toml=False) -> str:
    start, end = ('# MAVEN BEGIN', '# MAVEN END') if toml else (
        '<!-- MAVEN BEGIN -->', '<!-- MAVEN END -->')
    return start + '\n' + body.rstrip() + '\n' + end + '\n'


def _text_change(path: Path, body: str, *, toml=False, prior: dict | None = None) -> Change:
    """Plan the MAVEN block; an unmodified installer-owned block is upgraded in place."""
    before = _read(path)
    text = (before or b'').decode()
    block = _block(body, toml=toml)
    if 'MAVEN BEGIN' in text or 'MAVEN END' in text:
        if text.count('MAVEN BEGIN') != 1 or text.count('MAVEN END') != 1:
            raise ValueError(f'Existing MAVEN guidance differs; preserved: {path}')
        if text.count(block) != 1:
            owned = prior is not None and prior.get('owned', True) and prior.get('format') == 'text-block'
            if not owned or text.count(prior['value']) != 1:
                raise ValueError(f'Existing MAVEN guidance differs; preserved: {path}')
            separator = prior['value'][:len(prior['value']) - len(prior['value'].lstrip('\n'))]
            value = separator + block
            after = text.replace(prior['value'], value).encode()
            receipt = {'path': str(path), 'format': 'text-block', 'key': 'maven',
                       'value': value, 'created': prior.get('created', False)}
            if toml:
                receipt['server'] = tomllib.loads(after.decode())['mcp_servers']['maven']
            return Change(path, before, after, receipt)
        if toml and tomllib.loads(text).get('mcp_servers', {}).get('maven') != tomllib.loads(body)['mcp_servers']['maven']:
            raise ValueError(f'Existing MAVEN MCP table differs; preserved: {path}')
        receipt = {'path': str(path), 'format': 'text-block', 'key': 'maven',
                   'value': block, 'created': False, 'owned': False}
        if toml:
            receipt['server'] = tomllib.loads(body)['mcp_servers']['maven']
        return Change(path, before, before, receipt)
    if toml and 'maven' in tomllib.loads(text).get('mcp_servers', {}):
        raise ValueError(f'Existing MAVEN MCP configuration preserved: {path}')
    # Own the separator too, so removing the block restores the original bytes.
    value = ('\n' if text and not text.endswith('\n') else '') + block
    after = (text + value).encode()
    if toml:
        tomllib.loads(after.decode())
    receipt = {'path': str(path), 'format': 'text-block', 'key': 'maven',
               'value': value, 'created': before is None}
    if toml:
        receipt['server'] = tomllib.loads(after.decode())['mcp_servers']['maven']
    return Change(path, before, after, receipt)


def installed_targets(home: Path) -> dict:
    """Recover recorded destinations, never substitute defaults for unknown custom paths."""
    from tools.uninstall.common import read_private
    from tools.uninstall.local import MANIFEST
    home = Path(home).expanduser().absolute()
    manifest = read_private(home / MANIFEST) if (home / MANIFEST).exists() else {}
    if manifest and (manifest.get('schema') != 1 or manifest.get('home') != str(home)):
        raise ValueError('Installer ownership receipt does not match this home.')
    destinations, unresolved = {}, []
    statusline_candidates = set()
    retired = {}
    for item in manifest.get('integrations', []):
        if item.get('conflict'):
            continue
        path = Path(item['path'])
        role, harness = item.get('target'), item.get('harness')
        if item.get('key') == 'maven-lifecycle':
            if harness not in HARNESS_PATHS:
                matches = [name for name, suffix in LIFECYCLE_PATHS.items()
                           if path.parts[-len(Path(suffix).parts):] == Path(suffix).parts]
                harness = matches[0] if len(matches) == 1 else None
            if harness is not None:
                retired.setdefault(harness, []).append(item)
            else:
                unresolved.append(str(path))
            continue
        if item.get('key') == 'statusLine':
            statusline_candidates.add(path)
            continue
        if harness not in HARNESS_PATHS or role not in ('config', 'context'):
            matches = [(name, kind) for name, paths in HARNESS_PATHS.items()
                       for kind, suffix in zip(('config', 'context'), paths[:2])
                       if path.parts[-len(Path(suffix).parts):] == Path(suffix).parts]
            # Codex deliberately prefers its existing override context.
            if path.name == 'AGENTS.override.md' and path.parent.name == '.codex':
                matches = [('codex', 'context')]
            if len(matches) != 1:
                unresolved.append(str(path))
                continue
            harness, role = matches[0]
        slots = destinations.setdefault(harness, {})
        if role in slots and slots[role] != path:
            unresolved.extend((str(slots[role]), str(path)))
            slots[role] = None
        else:
            slots[role] = path
    paths = {}
    for harness, slots in destinations.items():
        if slots.get('config') and slots.get('context'):
            paths[harness] = (slots['config'], slots['context'])
        else:
            unresolved.extend(str(path) for path in slots.values() if path is not None)
    retired_hooks = []
    for harness, receipts in retired.items():
        if len(receipts) == 1:
            retired_hooks.append((harness, receipts[0]))
        else:
            unresolved.extend(item['path'] for item in receipts)
    if len(statusline_candidates) > 1:
        unresolved.extend(map(str, statusline_candidates))
    roots = set()
    for recorded in manifest.get('skill_roots', []):
        root = Path(recorded)
        if root.is_absolute() and '..' not in root.parts:
            roots.add(root)
        else:
            unresolved.append(recorded)
    # A first publication can be interrupted before this home's first manifest is durable.
    from tools.update import pending_owned_skills
    for saved in pending_owned_skills(home):
        root = skill_root(Path(saved['intent']['target']))
        if root is not None and not saved.get('disposition'):
            roots.add(root)
    for path in manifest.get('skill_references', []):
        root = skill_root(Path(path))
        if root is None:
            unresolved.append(path)
        else:
            roots.add(root)
    return {'paths': paths,
            'skill_roots': sorted(roots),
            'unresolved': sorted(set(unresolved)), 'statusline': bool(statusline_candidates),
            'retired_hooks': retired_hooks,
            'statusline_path': next(iter(statusline_candidates)) if len(statusline_candidates) == 1 else None}


def integration_plan(home: Path, harnesses, *, user_home: Path | None = None,
                     ca_bundle: Path | None = None, paths=None, statusline: bool = False,
                     guidance: str | None = None) -> list[Change]:
    """Plan independent harness targets; conflicts never suppress safe optional targets."""
    home = Path(home).expanduser().absolute()
    user_home = (user_home or Path.home()).expanduser().absolute()
    changes = []
    occupied = set()
    recorded = installed_targets(home)

    def conflict(path, error, harness, target):
        return Change(path, None, b'', {'path': str(path), 'format': 'conflict',
                      'key': 'maven', 'value': None, 'owned': False, 'conflict': str(error),
                      'harness': harness, 'target': target})

    for harness in dict.fromkeys(harnesses):
        config, context, _ = HARNESS_PATHS[harness]
        selected = (paths or {}).get(harness, (user_home / config, user_home / context))
        explicit_paths = {harness: selected} if paths and harness in paths else None
        try:
            planned = _integration_plan(home, [harness], user_home=user_home, ca_bundle=ca_bundle,
                                        paths=explicit_paths, guidance=guidance)
            if occupied.intersection(change.path for change in planned):
                raise ValueError('Integration targets overlap; select distinct configuration/context files')
            for change in planned:
                if (change.before != change.after and change.before and b'MAVEN BEGIN' in change.before
                        and change.receipt['format'] == 'text-block'):
                    _require_unshared(home, change.path)
        except (OSError, ValueError) as error:
            changes.append(conflict(Path(selected[0]), error, harness, 'config'))
            continue
        for index, change in enumerate(planned):
            change.receipt.update(harness=harness, target='config' if index == 0 else 'context')
        occupied.update(change.path for change in planned if not change.receipt.get('conflict'))
        changes.extend(planned)
    if statusline:
        if 'claude' not in harnesses:
            raise ValueError('The MAVEN status line is a Claude Code option; select the claude harness.')
        target = user_home / CLAUDE_SETTINGS
        try:
            if paths and 'claude' in paths and tuple(map(Path, paths['claude'])) != (
                    user_home / HARNESS_PATHS['claude'][0], user_home / HARNESS_PATHS['claude'][1]):
                target = recorded['statusline_path']
                if target is None:
                    raise ValueError('Custom Claude profile: select its status line manually; default settings preserved.')
            change = _statusline_change(home, target)
            change.receipt.update(harness='claude', target='statusline')
            changes.append(change)
        except (OSError, ValueError) as error:
            changes.append(conflict(target or Path(paths['claude'][0]), error, 'claude', 'statusline'))

    # Only consider receipts for the exact profile being configured. A staged
    # preview must never retire a hook from the user's live configuration.
    for harness, receipt in recorded['retired_hooks']:
        if harness not in harnesses:
            continue
        config, context, _ = HARNESS_PATHS[harness]
        selected = tuple(map(Path, (paths or {}).get(harness, (user_home / config, user_home / context))))
        target = Path(receipt['path'])
        if selected != recorded['paths'].get(harness) and not (
                selected == (user_home / config, user_home / context) and target == user_home / LIFECYCLE_PATHS[harness]):
            continue
        try:
            if not receipt.get('owned', True):
                raise ValueError(f'Unowned legacy hook preserved: {target}')
            _require_unshared(home, target)
            earlier = next((c for c in reversed(changes) if c.path == target and not c.receipt.get('conflict')), None)
            before = earlier.after if earlier else _read(target)
            after = _removal_bytes(receipt, before) if before is not None else None
            if after == b'' and receipt.get('created'):
                after = None
            changes.append(Change(target, before, after, {**receipt, 'retired': True}))
        except (OSError, ValueError) as error:
            changes.append(conflict(target, error, harness, 'retired-hook'))
    return changes


def apply_reviewed_integration(home, source, change):
    """Journal new ownership before mutation; release old ownership only after retirement."""
    from tools.uninstall.common import read_private, save_private
    from tools.uninstall.local import MANIFEST, record_installation
    receipt = change.receipt
    if receipt.get('conflict'):
        return
    if not receipt.get('retired'):
        record_installation(home, source, [], [], integrations=[receipt])
        apply_integration(change)
        return
    from tools.uninstall.local import _sharing_locks
    with _sharing_locks(home, []):
        _require_unshared(home, change.path)
        original = {k: v for k, v in receipt.items() if k != 'retired'}
        manifest = read_private(home / MANIFEST)
        manifest['integrations'].remove(original)
        apply_integration(change)
        save_private(home / MANIFEST, manifest)


def _require_unshared(home, path):
    from tools.uninstall.local import _integration_references
    shared, uncertain = _integration_references(home, [])
    if uncertain or str(path) in shared:
        raise ValueError(f'Shared or uncertain harness configuration preserved: {path}')


def _integration_plan(home: Path, harnesses, *, user_home: Path | None = None,
                      ca_bundle: Path | None = None, paths=None, guidance=None) -> list[Change]:
    """Plan against an explicit user home (also supports an inactive staging home).

    Never copy ambient credentials, PATH, or model runtime settings. MAVEN loads
    its selected private .env and login cache itself. Explicit trust paths are
    nonsecret and carried even by clients that sanitize process environments.
    """
    home = home.expanduser().absolute()
    user_home = (user_home or Path.home()).expanduser().absolute()
    base_env = {'MAVEN_HOME': str(home), 'HOME': str(Path.home())}
    if guidance is None:
        guidance = resource_path('templates/harness/startup.md').read_text()
    receipts = _owned_integrations(home)
    changes = []
    for harness in dict.fromkeys(harnesses):
        config, instructions, _ = HARNESS_PATHS[harness]
        path, context_path = (paths or {}).get(
            harness, (user_home / config, user_home / instructions))
        path, context_path = Path(path).absolute(), Path(context_path).absolute()
        if harness == 'codex' and harness not in (paths or {}):
            override = context_path.with_name('AGENTS.override.md')
            if _read(override):
                context_path = override
        environment = dict(base_env)
        ca_keys = ('AWS_CA_BUNDLE', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE')
        selected_ca = str(ca_bundle) if ca_bundle else None
        if selected_ca is None:
            # Refresh the profile without dropping its installer-owned corporate trust.
            text = receipts.get((str(path), 'text-block', 'maven'), {})
            entry = receipts.get((str(path), 'json-entry', 'maven'), {})
            old_env = (text.get('server') or entry.get('value') or {}).get('env', {})
            original = old_env.get(ca_keys[0])
            if isinstance(original, str) and all(old_env.get(key) == original for key in ca_keys):
                selected_ca = original
        if selected_ca is not None:
            environment.update(dict.fromkeys(ca_keys, selected_ca))
        if any('$' in value or '\n' in value or '\r' in value for value in environment.values()):
            raise ValueError('Integration paths must be literal, without environment interpolation or newlines')
        server = {'command': str(home / 'venv/bin/maven-mcp'), 'args': [], 'env': environment}
        if harness in ('claude', 'omp'):
            server['type'] = 'stdio'
        elif harness == 'copilot':
            server.update(type='local', tools=['*'])
        if harness == 'codex':
            body = '[mcp_servers.maven]\ncommand = ' + json.dumps(server['command'])
            body += '\nargs = []\nenv = { ' + ', '.join(
                json.dumps(key) + ' = ' + json.dumps(value) for key, value in environment.items()) + ' }'
            changes.append(_text_change(path, body, toml=True, prior=receipts.get((str(path), 'text-block', 'maven'))))
        else:
            before = _read(path)
            value = _json(before)
            if harness == 'gemini' and harness not in (paths or {}):
                filenames = value.get('context', {}).get('fileName', 'GEMINI.md')
                if 'GEMINI.md' not in ([filenames] if isinstance(filenames, str) else filenames):
                    raise ValueError('Gemini uses custom context filenames; select --harness-context explicitly.')
            servers = value.setdefault('mcpServers', {})
            if 'maven' in servers:
                if servers['maven'] != server:
                    raise ValueError(f'Existing MAVEN MCP entry differs; preserved: {path}')
                changes.append(Change(path, before, before, {
                    'path': str(path), 'format': 'json-entry', 'key': 'maven',
                    'value': server, 'created': False, 'owned': False,
                    'created_container': False,
                }))
            else:
                servers['maven'] = server
                receipt = {'path': str(path), 'format': 'json-entry', 'key': 'maven',
                           'value': server, 'created': before is None,
                           'created_container': not before or 'mcpServers' not in _json(before)}
                changes.append(Change(path, before, _encode(value), receipt))
        changes.append(_text_change(context_path, guidance, prior=receipts.get((str(context_path), 'text-block', 'maven'))))
    if len({change.path for change in changes}) != len(changes):
        raise ValueError('Integration targets overlap; select distinct configuration/context files')
    return changes


def _statusline_change(home: Path, path: Path) -> Change:
    """Plan the owned top-level statusLine; never replace an owner's different one."""
    entry = {'type': 'command', 'command': shlex.join(
        ['env', 'MAVEN_HOME=' + str(home), str(home / 'venv/bin/maven-statusline')])}
    before = _read(path)
    value = _json(before)
    receipt = {'path': str(path), 'format': 'json-setting', 'key': 'statusLine', 'value': entry}
    if 'statusLine' in value:
        receipt.update(created=False, owned=False)
        prior = _owned_integrations(home).get((str(path), 'json-setting', 'statusLine'))
        if value['statusLine'] != entry and prior and value['statusLine'] == prior['value']:
            from tools.uninstall.local import _integration_references
            shared, uncertain = _integration_references(home, [])
            if not uncertain and str(path) not in shared:
                value['statusLine'] = entry
                return Change(path, before, _encode(value),
                              {**receipt, 'owned': True, 'created': prior.get('created', False)})
        if value['statusLine'] != entry:
            receipt['conflict'] = (f'Existing Claude Code statusLine preserved; the MAVEN status line '
                                   f'was not added: {path}')
        return Change(path, before, before, receipt)
    value['statusLine'] = entry
    return Change(path, before, _encode(value), {**receipt, 'created': before is None})


def _owned_integrations(home: Path) -> dict:
    from tools.uninstall.common import read_private
    from tools.uninstall.local import MANIFEST
    path = home / MANIFEST
    if not path.exists():
        return {}
    prior = read_private(path)
    if prior.get('schema') != 1 or prior.get('home') != str(home):
        raise ValueError('Installer ownership receipt does not match this home.')
    return {(item['path'], item['format'], item['key']): item
            for item in prior.get('integrations', []) if item.get('owned', True)}


def _hook_map(document, path):
    hooks = document.setdefault('hooks', {})
    if not isinstance(hooks, dict) or any(not isinstance(value, list) for value in hooks.values()):
        raise ValueError(f'Unsupported hooks configuration preserved: {path}')
    return hooks


def _replace(path: Path, before: bytes | None, after: bytes) -> None:
    if _read(path) != before:
        raise ValueError(f'Integration changed since review; preserved: {path}')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Recheck parents after creating them; never follow a substituted symlink.
    _read(path)
    mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600
    fd, temporary = tempfile.mkstemp(prefix='.maven-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        if _read(path) != before:
            raise ValueError(f'Integration changed since review; preserved: {path}')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_integration(change: Change) -> dict | None:
    """Apply a reviewed change, returning ownership or an unowned reference."""
    if change.receipt.get('conflict'):
        return change.receipt
    if change.before != change.after:
        if change.after is None:
            if _read(change.path) != change.before:
                raise ValueError(f'Integration changed since review: {change.path}')
            change.path.unlink()
        else:
            _replace(change.path, change.before, change.after)
    elif _read(change.path) != change.before:
        raise ValueError(f'Integration changed since review: {change.path}')
    return change.receipt


def removal_content(receipt: dict) -> bytes | None:
    """Read-only selective removal preview; differing content raises ValueError.

    None means the integration is already absent. Empty bytes mean an owned,
    otherwise empty file can be removed. Other bytes preserve unrelated content.
    """
    if not receipt.get('owned', True):
        raise ValueError('Referenced pre-existing integration is not installer-owned')
    path = Path(receipt['path'])
    before = _read(path)
    if before is None:
        return None
    return _removal_bytes(receipt, before)


def _removal_bytes(receipt, before):
    """Apply one selective receipt to bytes, also used by grouped uninstall plans."""
    path = Path(receipt['path'])
    if receipt['format'] == 'file':
        if before != receipt['value'].encode():
            raise ValueError(f'Modified lifecycle extension preserved: {path}')
        return b''
    if receipt['format'] == 'json-hooks':
        document = _json(before)
        hooks = _hook_map(document, path)
        for event, entries in receipt['value'].items():
            current = hooks.get(event, [])
            for entry in entries:
                if current.count(entry) != 1:
                    raise ValueError(f'Modified or missing MAVEN lifecycle hook preserved: {path}')
                current.remove(entry)
            if not current:
                hooks.pop(event, None)
        if not hooks and receipt.get('created_container'):
            document.pop('hooks', None)
        if receipt.get('created_version') and document == {'version': 1}:
            document.pop('version')
        return b'' if not document and receipt['created'] else _encode(document)
    if receipt['format'] in ('json-entry', 'json-setting'):
        document = _json(before)
        entries = document.get('mcpServers', {}) if receipt['format'] == 'json-entry' else document
        if receipt['key'] not in entries:
            return None
        if entries[receipt['key']] != receipt['value']:
            label = 'MCP' if receipt['format'] == 'json-entry' else receipt['key']
            raise ValueError(f'Modified MAVEN {label} entry preserved: {path}')
        del entries[receipt['key']]
        if receipt['format'] == 'json-entry' and not entries and receipt.get('created_container'):
            document.pop('mcpServers', None)
        return b'' if not document and receipt['created'] else _encode(document)
    if receipt['format'] != 'text-block':
        raise ValueError('Unknown integration ownership format')
    text, block = before.decode(), receipt['value']
    if block not in text:
        if 'MAVEN BEGIN' in text or 'MAVEN END' in text:
            raise ValueError(f'Modified MAVEN guidance preserved: {path}')
        return None
    if text.count(block) != 1 or text.count('MAVEN BEGIN') != 1 or text.count('MAVEN END') != 1:
        raise ValueError(f'Duplicated MAVEN guidance preserved: {path}')
    if 'server' in receipt and tomllib.loads(text).get('mcp_servers', {}).get('maven') != receipt['server']:
        raise ValueError(f'Modified MAVEN MCP table preserved: {path}')
    return text.replace(block, '', 1).encode()


def grouped_removal_content(receipts):
    """One snapshot/approval for several owned fragments in the same JSON file."""
    if not receipts or len({item['path'] for item in receipts}) != 1:
        raise ValueError('Grouped removal requires one integration file')
    if any(not item.get('owned', True) for item in receipts):
        raise ValueError('Referenced pre-existing integration is not installer-owned')
    before = _read(Path(receipts[0]['path']))
    if before is None:
        return None
    result = before
    for receipt in receipts:
        changed = _removal_bytes(receipt, result)
        if changed is not None:
            result = changed
    if result in (b'{}\n', b'{}', b'') and any(item.get('created') for item in receipts):
        return b''
    return result


def remove_integrations(receipts):
    path = Path(receipts[0]['path'])
    before = _read(path)
    after = grouped_removal_content(receipts)
    if after is None:
        return {'removed': False, 'already_absent': True, 'path': str(path)}
    if not after and any(item.get('created') for item in receipts):
        if _read(path) != before:
            raise ValueError(f'Integration changed during removal: {path}')
        path.unlink()
    else:
        _replace(path, before, after)
    return {'removed': True, 'path': str(path), 'unrelated_content_preserved': True}


def remove_integration(receipt: dict) -> dict:
    """Remove only exact owned content after the uninstall approval/guard."""
    path = Path(receipt['path'])
    before = _read(path)
    after = removal_content(receipt)
    if after is None:
        return {'removed': False, 'already_absent': True, 'path': str(path)}
    if not after and receipt['created']:
        if _read(path) != before:
            raise ValueError(f'Integration changed during removal: {path}')
        path.unlink()
    else:
        _replace(path, before, after)
    return {'removed': True, 'path': str(path), 'unrelated_content_preserved': True}
