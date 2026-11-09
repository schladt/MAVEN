"""Stdlib-only, reviewable harness configuration with selective ownership.

Only documented stdio and standing-instruction surfaces are used. Loading
instructions is not an execution hook or a verified project initialization.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
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
SKILLS = ('maven', 'maven-maintenance')


@dataclass(frozen=True)
class Change:
    path: Path
    before: bytes | None
    after: bytes
    receipt: dict | None


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


def _text_change(path: Path, body: str, *, toml=False) -> Change:
    before = _read(path)
    text = (before or b'').decode()
    block = _block(body, toml=toml)
    if 'MAVEN BEGIN' in text or 'MAVEN END' in text:
        if text.count(block) != 1 or text.count('MAVEN BEGIN') != 1 or text.count('MAVEN END') != 1:
            raise ValueError(f'Existing MAVEN guidance differs; preserved: {path}')
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


def integration_plan(home: Path, harnesses, *, user_home: Path | None = None,
                     ca_bundle: Path | None = None, paths=None) -> list[Change]:
    """Plan against an explicit user home (also supports an inactive staging home).

    Never copy ambient credentials, PATH, or model runtime settings. MAVEN loads
    its selected private .env and login cache itself. Explicit trust paths are
    nonsecret and carried even by clients that sanitize process environments.
    """
    home = home.expanduser().absolute()
    user_home = (user_home or Path.home()).expanduser().absolute()
    environment = {'MAVEN_HOME': str(home), 'HOME': str(Path.home())}
    if ca_bundle:
        environment.update({key: str(ca_bundle) for key in (
            'AWS_CA_BUNDLE', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE')})
    if any('$' in value or '\n' in value or '\r' in value for value in environment.values()):
        raise ValueError('Integration paths must be literal, without environment interpolation or newlines')
    guidance = resource_path('templates/harness/startup.md').read_text()
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
        server = {'command': str(home / 'venv/bin/maven-mcp'), 'args': [], 'env': environment}
        if harness in ('claude', 'omp'):
            server['type'] = 'stdio'
        elif harness == 'copilot':
            server.update(type='local', tools=['*'])
        if harness == 'codex':
            body = '[mcp_servers.maven]\ncommand = ' + json.dumps(server['command'])
            body += '\nargs = []\nenv = { ' + ', '.join(
                json.dumps(key) + ' = ' + json.dumps(value) for key, value in environment.items()) + ' }'
            changes.append(_text_change(path, body, toml=True))
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
        changes.append(_text_change(context_path, guidance))
    if len({change.path for change in changes}) != len(changes):
        raise ValueError('Integration targets overlap; select distinct configuration/context files')
    return changes


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
    if change.before != change.after:
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
    if receipt['format'] == 'json-entry':
        document = _json(before)
        servers = document.get('mcpServers', {})
        if receipt['key'] not in servers:
            return None
        if servers[receipt['key']] != receipt['value']:
            raise ValueError(f'Modified MAVEN MCP entry preserved: {path}')
        del servers[receipt['key']]
        if not servers and receipt.get('created_container'):
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
