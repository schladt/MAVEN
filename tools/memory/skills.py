"""Approved cloud skill registration and session resource retrieval.

Cache catalog metadata and complete ZIPs, never owner instruction bodies.
Retrieval does not install dependencies or execute a skill.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path

import re


import stat
import struct


import tempfile
import uuid

import zipfile
import zlib

from .client import MemoryClientError

from .validation import MemoryError, validate

MAX_BUNDLE = 268435456
MAX_MANIFEST = 4 * 1024 * 1024
MAX_ENTRIES = 20001
MAX_DIRECTORY = 16 * 1024 * 1024
HASH = re.compile(r'[0-9a-f]{64}\Z')
SLUG = re.compile(r'(?=.{1,80}\Z)[a-z0-9]+(?:-[a-z0-9]+)*\Z')
VERSION = re.compile(r'[A-Za-z0-9._-]{1,80}\Z')
METADATA = ('page_id', 'slug', 'page_kind', 'title', 'scope', 'revision',
            'content_hash', 'data_scope', 'requires', 'bundle')


def _fail(code='skill_bundle_invalid'):
    raise MemoryClientError(code)


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: _fail())
    except (ValueError, UnicodeError, RecursionError):
        _fail()


def _keys(value, required):
    if not isinstance(value, dict) or set(value) != set(required):
        _fail()


def _path(value):
    if (not isinstance(value, str) or len(value) > 240
            or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._/-]*', value)
            or any(part in ('', '.', '..') for part in value.split('/'))
            or value.casefold() == 'manifest.json'):
        _fail()
    return value


def _bundle(value):
    _keys(value, ('version', 'sha256', 'size_bytes', 'manifest_sha256', 'load_policy'))
    if (not isinstance(value['version'], str) or not VERSION.fullmatch(value['version'])
            or not isinstance(value['sha256'], str) or not HASH.fullmatch(value['sha256'])
            or not isinstance(value['manifest_sha256'], str) or not HASH.fullmatch(value['manifest_sha256'])
            or type(value['size_bytes']) is not int or not 1 <= value['size_bytes'] <= MAX_BUNDLE
            or value['load_policy'] not in ('always', 'on-demand')):
        _fail()
    return value


def _manifest(value):
    _keys(value, ('format_version', 'slug', 'version', 'files'))
    if (type(value['format_version']) is not int or value['format_version'] != 1
            or not isinstance(value['slug'], str) or not SLUG.fullmatch(value['slug'])
            or not isinstance(value['version'], str) or not VERSION.fullmatch(value['version'])
            or not isinstance(value['files'], list) or len(value['files']) > 20000):
        _fail()
    files, total = {}, 0
    namespace = {'manifest.json': ('manifest.json', True)}
    for item in value['files']:
        _keys(item, ('path', 'sha256', 'size_bytes'))
        name = _path(item['path'])
        if (name in files or not isinstance(item['sha256'], str) or not HASH.fullmatch(item['sha256'])
                or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_BUNDLE):
            _fail()
        files[name] = item
        total += item['size_bytes']
        components = name.split('/')
        for index in range(len(components)):
            spelling = '/'.join(components[:index + 1])
            canonical = spelling.casefold()
            is_file = index == len(components) - 1
            previous = namespace.get(canonical)
            if previous is not None:
                if previous[0] != spelling or previous[1] or is_file:
                    _fail()
            else:
                namespace[canonical] = (spelling, is_file)
    if total > MAX_BUNDLE:
        _fail()
    return value



def _regular(path):
    """Reject links at every component, not just the final resource name."""
    path = Path(path).absolute()
    try:
        for component in (path, *path.parents):
            if component.is_symlink():
                _fail('skill_cache_unsafe')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            _fail('skill_cache_unsafe')
        return info
    except OSError:
        _fail('skill_resource_unavailable')


def _file_bytes(path, maximum):
    info = _regular(path)
    if info.st_size > maximum:
        _fail()
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            _fail()
        return raw
    except OSError:
        _fail('skill_resource_unavailable')


def _private_dir(path):
    path = Path(path).absolute()
    try:
        if any(component.is_symlink() for component in (path, *path.parents)):
            _fail('skill_cache_unsafe')
        if not path.exists():
            if not path.parent.exists():
                _private_dir(path.parent)
            path.mkdir(mode=0o700)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            _fail('skill_cache_unsafe')
        path.chmod(0o700)
        return path
    except OSError:
        _fail('skill_cache_unsafe')


def _atomic_json(path, value):
    parent = _private_dir(path.parent)
    if path.is_symlink():
        _fail('skill_cache_unsafe')
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.metadata-', dir=parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        _fail('skill_cache_unavailable')
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def prepare_bundle(source: Path, manifest: dict, output: Path) -> dict:
    """Create the same ZIP bytes for the same manifest and exact source tree."""
    manifest = copy.deepcopy(_manifest(manifest))
    source, output = Path(source).absolute(), Path(output).absolute()
    if source.is_symlink() or output.is_symlink():
        _fail('skill_cache_unsafe')
    source = source.resolve()
    output = output.parent.resolve() / output.name
    if not source.is_dir() or output.is_relative_to(source):
        _fail()
    expected = {item['path']: item for item in manifest['files']}
    found = set()
    try:
        authoring_manifest = source / 'manifest.json'
        if authoring_manifest.exists() or authoring_manifest.is_symlink():
            supplied = _manifest(_json(_file_bytes(authoring_manifest, MAX_MANIFEST)))
            if supplied != manifest:
                _fail()
        for directory, dirs, names in os.walk(source, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in dirs):
                _fail()
            for name in names:
                found.add((Path(directory) / name).relative_to(source).as_posix())
        found.discard('manifest.json')
        if found != set(expected):
            _fail()
        raw_manifest = _json_bytes(manifest)
        if len(raw_manifest) > MAX_MANIFEST:
            _fail()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as target:
            for name in ['manifest.json', *sorted(expected)]:
                raw = raw_manifest if name == 'manifest.json' else _file_bytes(source / name, expected[name]['size_bytes'])
                if name != 'manifest.json' and (len(raw) != expected[name]['size_bytes']
                        or hashlib.sha256(raw).hexdigest() != expected[name]['sha256']):
                    _fail()
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                target.writestr(info, raw)
        raw = archive.getvalue()
        if not 1 <= len(raw) <= MAX_BUNDLE:
            _fail()
        if not output.parent.exists():
            _private_dir(output.parent)
        for parent in (output.parent, *output.parent.parents):
            if parent.is_symlink():
                _fail('skill_cache_unsafe')
        if output.is_symlink():
            _fail('skill_cache_unsafe')
        fd, temporary = tempfile.mkstemp(prefix='.bundle-', dir=output.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            os.replace(temporary, output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {'version': manifest['version'], 'sha256': hashlib.sha256(raw).hexdigest(),
                'size_bytes': len(raw), 'manifest_sha256': hashlib.sha256(raw_manifest).hexdigest(),
                'output': str(output)}
    except (OSError, zipfile.BadZipFile, ValueError):
        _fail('skill_bundle_invalid')


def _bound_zip_directory(raw):
    """Count actual central records before ZipFile can allocate ZipInfo objects."""
    end = raw.rfind(b'PK\x05\x06', max(0, len(raw) - 65557))
    if end < 0 or len(raw) - end < 22:
        _fail()
    signature, disk, directory_disk, disk_count, claimed, size, offset, comment = struct.unpack_from(
        '<4s4H2LH', raw, end)
    if (signature != b'PK\x05\x06' or disk or directory_disk or disk_count != claimed
            or not 1 <= claimed <= MAX_ENTRIES or not 1 <= size <= MAX_DIRECTORY
            or end + 22 + comment != len(raw) or offset + size != end):
        _fail()
    position, count = offset, 0
    while position < end:
        if end - position < 46 or raw[position:position + 4] != b'PK\x01\x02':
            _fail()
        name, extra, note, start_disk = struct.unpack_from('<4H', raw, position + 28)
        compressed, expanded = struct.unpack_from('<2L', raw, position + 20)
        if (not 1 <= name <= 240 or start_disk or compressed == 0xffffffff or expanded == 0xffffffff
                or position + 46 + name + extra + note > end):
            _fail()
        position += 46 + name + extra + note
        count += 1
        if count > MAX_ENTRIES:
            _fail()
    if position != end or count != claimed:
        _fail()


def _verified(raw, bundle, slug):
    _bundle(bundle)
    if len(raw) != bundle['size_bytes'] or hashlib.sha256(raw).hexdigest() != bundle['sha256']:
        _fail()
    _bound_zip_directory(raw)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if len(entries) > MAX_ENTRIES or len(set(names)) != len(names) or names.count('manifest.json') != 1:
                _fail()
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.is_dir() or entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    _fail()
                if entry.filename != 'manifest.json':
                    _path(entry.filename)
            info = archive.getinfo('manifest.json')
            if info.file_size > MAX_MANIFEST:
                _fail()
            raw_manifest = archive.read(info)
            if hashlib.sha256(raw_manifest).hexdigest() != bundle['manifest_sha256']:
                _fail()
            manifest = _manifest(_json(raw_manifest))
            if manifest['slug'] != slug or manifest['version'] != bundle['version']:
                _fail()
            files = {item['path']: item for item in manifest['files']}
            if set(names) != {'manifest.json', *files}:
                _fail()
            for entry in entries:
                if entry.filename == 'manifest.json':
                    continue
                item = files[entry.filename]
                if entry.file_size != item['size_bytes']:
                    _fail()
                digest = hashlib.sha256()
                size = 0
                with archive.open(entry) as stream:
                    while chunk := stream.read(1024 * 1024):
                        size += len(chunk)
                        if size > item['size_bytes']:
                            _fail()
                        digest.update(chunk)
                if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
                    _fail()
            return manifest
    except (OSError, ValueError, KeyError, RuntimeError, NotImplementedError, zipfile.BadZipFile, EOFError, zlib.error):
        _fail()


def _storage(client, bundle):
    _bundle(bundle)
    bucket = client.config.get('skill_bucket')
    instance = client.config.get('instance_id')
    if not isinstance(bucket, str) or not bucket or not isinstance(instance, str):
        _fail('skill_storage_unconfigured')
    try:
        if str(uuid.UUID(instance)) != instance:
            _fail('skill_storage_unconfigured')
        return client.session.client('s3'), bucket, f"skills/{instance}/bundles/{bundle['sha256']}.zip"
    except (ValueError, AttributeError):
        _fail('skill_storage_unconfigured')
    except Exception:
        _fail('skill_storage_unavailable')


def _download(client, bundle):
    s3, bucket, key = _storage(client, bundle)
    body = None
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        if response.get('ContentLength') != bundle['size_bytes']:
            _fail()
        body = response['Body']
        raw = body.read(bundle['size_bytes'] + 1)
        if len(raw) != bundle['size_bytes']:
            _fail()
        return raw
    except MemoryClientError:
        raise
    except Exception:
        _fail('skill_storage_unavailable')
    finally:
        if body is not None:
            try:
                body.close()
            except Exception:
                pass


def verify_cloud_bundle(client, page: dict) -> dict:
    if not isinstance(page, dict) or not isinstance(page.get('slug'), str) or 'bundle' not in page:
        _fail()
    bundle = _bundle(page['bundle'])
    # No extraction or installation for review, and no on-disk instruction data.
    manifest = _verified(_download(client, bundle), bundle, page['slug'])
    return {'status': 'verified', 'data': {'slug': page['slug'], 'bundle': copy.deepcopy(bundle), 'manifest': manifest}}


def register_bundle(client, request: dict, bundle_path: Path) -> dict:
    try:
        validate('/v1/proposals/create', request)
    except MemoryError:
        _fail('invalid_request')
    candidate = request.get('candidate', {})
    if candidate.get('page_kind') != 'skill' or 'bundle' not in candidate:
        _fail('invalid_request')
    bundle = _bundle(candidate['bundle'])
    bundle_path = Path(bundle_path)
    if bundle_path.is_symlink():
        _fail('skill_cache_unsafe')
    raw = _file_bytes(bundle_path.parent.resolve() / bundle_path.name, MAX_BUNDLE)
    _verified(raw, bundle, candidate['slug'])
    s3, bucket, key = _storage(client, bundle)
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=raw, IfNoneMatch='*',
                      ContentType='application/zip', ServerSideEncryption='AES256')
    except Exception:
        # Includes a precondition conflict and an uncertain transport result.
        # Reconcile the exact immutable key, never overwrite or guess success.
        existing = _download(client, bundle)
        _verified(existing, bundle, candidate['slug'])
    return client.invoke('/v1/proposals/create', request)


def _metadata(page):
    if (not isinstance(page, dict) or not isinstance(page.get('slug'), str)
            or not SLUG.fullmatch(page['slug']) or page.get('page_kind') not in ('directive', 'skill', 'procedure')
            or type(page.get('revision')) is not int or page['revision'] < 1
            or not isinstance(page.get('content_hash'), str) or not HASH.fullmatch(page['content_hash'])
            or page.get('data_scope') != 'owner' or not isinstance(page.get('scope'), dict)
            or 'title' in page and (not isinstance(page['title'], str) or not 1 <= len(page['title']) <= 160)
            or any(key not in ('project', 'workflow', 'operation') or not isinstance(value, str)
                   or not 1 <= len(value) <= 80
                   for key, value in page['scope'].items())):
        _fail('invalid_service_response')
    if 'page_id' in page:
        try:
            if str(uuid.UUID(page['page_id'])) != page['page_id']:
                _fail('invalid_service_response')
        except (ValueError, TypeError, AttributeError):
            _fail('invalid_service_response')
    requires = page.get('requires', [])
    if (not isinstance(requires, list) or len(requires) > 32
            or any(not isinstance(item, str) or not SLUG.fullmatch(item) for item in requires)
            or len(set(requires)) != len(requires)):
        _fail('invalid_service_response')
    if 'bundle' in page:
        if page['page_kind'] != 'skill':
            _fail('invalid_service_response')
        _bundle(page['bundle'])
    return copy.deepcopy({key: page[key] for key in METADATA if key in page})














class SessionSkills:
    """Cache approved catalog metadata and complete ZIPs for one work session."""

    def __init__(self, client, root: Path, project_id: str, session_id: str):
        try:
            if str(uuid.UUID(project_id)) != project_id or str(uuid.UUID(session_id)) != session_id:
                _fail('invalid_request')
        except (ValueError, TypeError, AttributeError):
            _fail('invalid_request')
        self.client, self.project_id = client, project_id
        self.cache = Path(root).resolve() / '.maven' / 'runtime' / 'sessions' / session_id / 'skills'
        _private_dir(self.cache)
        self.catalog = None
        self.generation = None
        registry = self.cache / 'registry.json'
        if registry.exists() or registry.is_symlink():
            data = _json(_file_bytes(registry, MAX_MANIFEST))
            if (not isinstance(data, dict) or data.get('project_id') != project_id
                    or data.get('session_id') != session_id or not isinstance(data.get('pages'), list)
                    or type(data.get('authority_generation')) is not int):
                _fail('skill_cache_invalid')
            self.catalog = {}
            for page in data['pages']:
                page = _metadata(page)
                if page['page_kind'] != 'skill' or page['slug'] in self.catalog:
                    _fail('skill_cache_invalid')
                self.catalog[page['slug']] = page
            self.generation = data['authority_generation']
        self.session_id = session_id

    def discover(self, refresh=False):
        if self.catalog is None or refresh:
            catalog, after, generation = {}, None, None
            while True:
                request = {'page_kind': 'skill', 'limit': 10}
                if after is not None:
                    request['after_slug'] = after
                response = self.client.invoke('/v1/pages/index', request)
                data = response.get('data', {})
                if (response.get('status') != 'ready' or not isinstance(data.get('pages'), list)
                        or type(data.get('authority_generation')) is not int):
                    _fail('invalid_service_response')
                if generation is None:
                    generation = data['authority_generation']
                elif generation != data['authority_generation']:
                    _fail('context_stale')
                previous = after
                for page in data['pages']:
                    slug = page.get('slug') if isinstance(page, dict) else None
                    if not isinstance(slug, str) or previous is not None and slug <= previous:
                        _fail('invalid_service_response')
                    previous = slug
                    if page.get('data_scope') != 'owner':
                        continue
                    page = _metadata(page)
                    if page['page_kind'] != 'skill':
                        _fail('invalid_service_response')
                    if page['scope'].get('project', self.project_id) == self.project_id:
                        catalog[slug] = page
                next_after = data.get('next_after_slug')
                if next_after is None:
                    break
                if not data['pages'] or next_after != previous or after is not None and next_after <= after:
                    _fail('invalid_service_response')
                after = next_after
            _atomic_json(self.cache / 'registry.json', {
                'project_id': self.project_id, 'session_id': self.session_id,
                'authority_generation': generation, 'pages': list(catalog.values())})
            self.catalog, self.generation = catalog, generation
        return {'status': 'ready', 'data': {
            'pages': list(self.catalog.values()), 'authority_generation': self.generation}}

    def fetch(self, slug, refresh=False):
        self.discover(refresh=refresh)
        if slug not in self.catalog or 'bundle' not in self.catalog[slug]:
            _fail('skill_resources_unavailable')
        page = self.catalog[slug]
        bundle = page['bundle']
        destination = self.cache / (slug + '-' + bundle['sha256'] + '.zip')
        cached = destination.exists() or destination.is_symlink()
        raw = _file_bytes(destination, MAX_BUNDLE) if cached and not refresh else _download(self.client, bundle)
        manifest = _verified(raw, bundle, slug)
        if not cached or refresh:
            fd, temporary = tempfile.mkstemp(prefix='.download-', dir=self.cache)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                if destination.is_symlink():
                    _fail('skill_cache_unsafe')
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {'status': 'cached' if cached and not refresh else 'downloaded', 'data': {
            'slug': slug, 'revision': page['revision'], 'bundle': copy.deepcopy(bundle),
            'path': str(destination), 'manifest': manifest}}

    def start(self):
        self.discover()
        resources = [self.fetch(slug)['data'] for slug, page in self.catalog.items()
                     if page.get('bundle', {}).get('load_policy') == 'always']
        return {'status': 'ready', 'data': {'resources': resources}}




