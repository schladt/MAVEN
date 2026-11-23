"""Admin-only physical erasure. Never persist a provider key, ETag, or version ID.

The private per-instance salt and strongly consistent S3 inventories resolve the
same opaque handles after a crash, even after SQL has removed the last reference.
Only selected keys are read; no payload is read except the protected service fence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import hashlib
import hmac
import json
import logging
import re
import threading
import uuid

SAFE_CODES = frozenset({
    'invalid_redaction_scope', 'provider_unverified', 'unmanaged_or_protected_object',
    'inventory_unverified', 'versioning_required', 'object_retained', 'replication_unverified',
    'retention_unverified', 'control_unverified', 'version_scope_changed',
    'redaction_fence_unverified', 'multipart_purge_unverified', 'partial_provider_deletion',
    'ambiguous_redaction_target', 'object_not_found', 'issuer_revocation_required',
    'writer_isolation_required', 'provider_locator_required', 'provider_deletion_unverified',
})
_PRIVATE_IO = threading.RLock()


class ObjectRedactionError(Exception):
    def __init__(self, code):
        self.code = code if code in SAFE_CODES else 'provider_unverified'
        super().__init__(self.code)


def refuse(code):
    raise ObjectRedactionError(code)


def guarded(function):
    @wraps(function)
    def call(*args, **kwargs):
        # SDK debug serialization can include URLs, parameters and bodies.
        with _PRIVATE_IO:
            previous = logging.root.manager.disable
            logging.disable(max(logging.CRITICAL, previous))
            try:
                return function(*args, **kwargs)
            except ObjectRedactionError:
                raise
            except Exception:
                raise ObjectRedactionError('provider_unverified') from None
            finally:
                logging.disable(previous)
    return call


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        refuse('invalid_redaction_scope')
    return value


def handle(salt, kind, value):
    # This domain-separated, fixed-length skill lookup is also computed by SQL.
    data = f'{_uuid(salt)}:{kind}:{value}'.encode()
    return str(uuid.UUID(bytes=hashlib.sha256(data).digest()[:16]))


def pin(salt, *parts):
    raw = json.dumps(parts, separators=(',', ':'), ensure_ascii=True).encode()
    return hmac.new(uuid.UUID(_uuid(salt)).bytes, raw, hashlib.sha256).hexdigest()


def _code(exc):
    return getattr(exc, 'response', {}).get('Error', {}).get('Code')


def _account(config):
    match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+', config['admin_role_arn'])
    if not match:
        refuse('invalid_redaction_scope')
    return match[1]


def _scope(config, salt):
    return handle(salt, 'object_store', json.dumps([_uuid(config['instance_id']), _account(config),
                  config['journal_bucket'], config.get('skill_bucket')], separators=(',', ':')))


def classify_recovery(instance, key):
    if key == 'control.json':
        return 'control_history'
    if key in ('redaction-ledger', 'narrow-erases') or key.startswith(('redaction-ledger/', 'narrow-erases/')):
        return 'protected_ledger'
    if re.fullmatch(r'uninstall/' + re.escape(_uuid(instance)) + r'/[0-9a-f]{64}\.json', key):
        return 'retirement_manifest'
    if key.startswith('uninstall/'):
        return 'foreign_or_malformed_manifest'
    if not key or len(key.encode('utf-8')) > 1024:
        return 'unclassified'
    return 'orphan'


@guarded
def target(config, salt, kind, locator):
    if kind == 'skill_bundle' and isinstance(locator, str) and re.fullmatch('[0-9a-f]{64}', locator):
        return {'kind': kind, 'id': handle(salt, kind, locator)}
    if kind == 'recovery_object' and classify_recovery(config['instance_id'], locator) in ('control_history', 'retirement_manifest', 'orphan'):
        return {'kind': kind, 'id': handle(salt, kind, locator)}
    refuse('unmanaged_or_protected_object')


def _pages(fetch, args, secondary):
    seen, marker = set(), {}
    while True:
        page = fetch(**args, **marker)
        if type(page.get('IsTruncated')) is not bool:
            refuse('inventory_unverified')
        yield page
        if not page['IsTruncated']:
            return
        key, value = page.get('NextKeyMarker'), page.get('Next' + secondary)
        if not isinstance(key, str) or not key or (value is not None and not isinstance(value, str)):
            refuse('inventory_unverified')
        pair = (key, value)
        if pair in seen:
            refuse('inventory_unverified')
        seen.add(pair)
        marker = {'KeyMarker': key, **({secondary: value} if value else {})}


def _items(page, field):
    rows = page.get(field, [])
    if not isinstance(rows, list):
        refuse('inventory_unverified')
    return rows


def _entries(s3, bucket, owner, prefix):
    args = {'Bucket': bucket, 'ExpectedBucketOwner': owner, 'Prefix': prefix}
    for fn, secondary, fields in [(s3.list_object_versions, 'VersionIdMarker', ('Versions', 'DeleteMarkers')),
                                  (s3.list_multipart_uploads, 'UploadIdMarker', ('Uploads',))]:
        for page in _pages(fn, args, secondary):
            for field in fields:
                for entry in _items(page, field):
                    key = entry['Key']
                    if not isinstance(key, str) or not key.startswith(prefix):
                        refuse('inventory_unverified')
                    yield field, entry


def _bucket_status(s3, bucket, owner, *, versioned):
    args = {'Bucket': bucket, 'ExpectedBucketOwner': owner}
    state = s3.get_bucket_versioning(**args)
    if versioned and state.get('Status') != 'Enabled':
        refuse('versioning_required')
    blocked = 'object_retained' if state.get('MFADelete') == 'Enabled' else None
    try:
        s3.get_bucket_replication(**args)
    except Exception as error:
        if _code(error) != 'ReplicationConfigurationNotFoundError':
            raise
    else:
        blocked = 'replication_unverified'
    try:
        result = s3.get_object_lock_configuration(**args)
    except Exception as error:
        if _code(error) != 'ObjectLockConfigurationNotFoundError':
            raise
        return False, blocked
    if result.get('ObjectLockConfiguration', {}).get('ObjectLockEnabled') != 'Enabled':
        refuse('retention_unverified')
    return True, blocked


def _retention(s3, args):
    for fn, key in [(s3.get_object_legal_hold, 'LegalHold'), (s3.get_object_retention, 'Retention')]:
        try:
            response = fn(**args)
        except Exception as exc:
            if _code(exc) == 'NoSuchObjectLockConfiguration':
                continue
            raise
        info = response.get(key)
        if not isinstance(info, dict):
            refuse('retention_unverified')
        if key == 'LegalHold':
            if info.get('Status') == 'ON':
                return True
            if info.get('Status') != 'OFF':
                refuse('retention_unverified')
        if key == 'Retention' and info:
            until = info.get('RetainUntilDate')
            if not isinstance(until, datetime) or until.tzinfo is None:
                refuse('retention_unverified')
            if until > datetime.now(timezone.utc):
                return True
    return False


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        refuse('inventory_unverified')
    return value.isoformat()


def _parts_pin(s3, args, salt):
    # A multipart upload is mutable until committed/aborted. Pin observed parts as
    # well as UploadId so a change before review/application is not silently accepted.
    digest = hmac.new(uuid.UUID(salt).bytes, b'multipart-parts:', hashlib.sha256)
    last, marker = 0, {}
    while True:
        page = s3.list_parts(**args, **marker)
        for part in _items(page, 'Parts'):
            number, size, etag = part['PartNumber'], part['Size'], part['ETag']
            if (type(number) is not int or not last < number <= 10000 or type(size) is not int or size < 0
                    or not isinstance(etag, str) or not etag):
                refuse('inventory_unverified')
            digest.update(json.dumps([number, size, etag, _time(part['LastModified'])]).encode())
            last = number
        if type(page.get('IsTruncated')) is not bool:
            refuse('inventory_unverified')
        if not page['IsTruncated']:
            return digest.hexdigest()
        nxt = page.get('NextPartNumberMarker')
        if type(nxt) is not int or nxt != last or nxt <= marker.get('PartNumberMarker', 0):
            refuse('inventory_unverified')
        marker = {'PartNumberMarker': nxt}


def _read_control(s3, args, config):
    from .store import parse_control
    result = s3.get_object(**args)
    body = result['Body']
    try:
        data = body.read(4097)
    finally:
        body.close()
    if len(data) > 4096 or result.get('VersionId') != args['VersionId']:
        refuse('control_unverified')
    try:
        parse_control(data, config, allow_maintenance=True)
    except Exception:
        refuse('control_unverified')


def _inspect(s3, bucket, owner, key, salt, object_id, cls, *, config=None):
    """Map an exact live inventory to private version tokens. HEAD is not purge proof."""
    locked, blocked = _bucket_status(s3, bucket, owner, versioned=cls != 'encrypted-file')
    args = {'Bucket': bucket, 'Key': key, 'ExpectedBucketOwner': owner}
    entries, latest, preserved = {}, None, None
    for field, row in _entries(s3, bucket, owner, key):
        if row['Key'] != key:
            continue  # S3 Prefix is not an equality predicate.
        if field == 'Uploads':
            version = row['UploadId']
            if not isinstance(version, str) or not version:
                refuse('inventory_unverified')
            entry = pin(salt, object_id, field, version, _time(row['Initiated']),
                        _parts_pin(s3, {**args, 'UploadId': version}, salt))
        else:
            version = row['VersionId']
            if not isinstance(version, str) or not version or type(row.get('IsLatest')) is not bool:
                refuse('inventory_unverified')
            etag, size = row.get('ETag'), row.get('Size')
            entry = pin(salt, object_id, field, version, _time(row['LastModified']), etag, size)
            if row['IsLatest']:
                if latest is not None:
                    refuse('inventory_unverified')
                latest = (field, etag, entry)
            if field == 'Versions':
                if not isinstance(etag, str) or not etag or type(size) is not int or size < 0:
                    refuse('inventory_unverified')
                head = s3.head_object(**args, VersionId=version)
                if head.get('ReplicationStatus'):
                    blocked = 'replication_unverified'
                if (head.get('ContentLength') != size or head.get('ETag') != etag or
                        head.get('VersionId', 'null') != version):
                    refuse('version_scope_changed')
                if row['IsLatest'] and cls == 'control_history':
                    _read_control(s3, {**args, 'VersionId': version}, config)
                    preserved = entry
                elif row['IsLatest'] and cls == 'bundle' and size == 0 and head.get('Metadata', {}).get('maven-redacted') == 'true':
                    tags = s3.get_object_tagging(**args, VersionId=version)['TagSet']
                    if {'Key': 'maven-redacted', 'Value': 'true'} not in tags:
                        refuse('redaction_fence_unverified')
                    preserved = entry
                elif locked and _retention(s3, {**args, 'VersionId': version}):
                    blocked = blocked or 'object_retained'
        if entry in entries:
            refuse('inventory_unverified')
        entries[entry] = (field, version, row.get('ETag'))
    if latest is None and any(field != 'Uploads' for field, _, _ in entries.values()):
        refuse('inventory_unverified')
    if cls == 'control_history' and preserved is None:
        refuse('control_unverified')
    entries.pop(preserved, None)
    return entries, latest, preserved, blocked


def _delete(s3, bucket, owner, key, entries):
    base = {'Bucket': bucket, 'ExpectedBucketOwner': owner}
    objects = []
    for field, version, etag in entries.values():
        if field == 'Uploads':
            args = {**base, 'Key': key, 'UploadId': version}
            s3.abort_multipart_upload(**args)
            try:
                s3.list_parts(**args, MaxParts=1)
            except Exception as exc:
                if _code(exc) != 'NoSuchUpload':
                    raise
            else:
                refuse('multipart_purge_unverified')
        elif version == 'null' or not key.isprintable():
            # A null version can be overwritten in place. An ETag condition adds
            # protection even with quiesced writers; never use an unqualified DELETE.
            # Use the URI form for keys that XML-normalization could change.
            result = s3.delete_object(**base, Key=key, VersionId=version,
                                      **({'IfMatch': etag} if etag and version == 'null' else {}))
            if result.get('VersionId', version) != version:
                refuse('partial_provider_deletion')
        else:
            objects.append({'Key': key, 'VersionId': version})
    for start in range(0, len(objects), 1000):
        batch = objects[start:start + 1000]
        result = s3.delete_objects(**base, Delete={'Objects': batch, 'Quiet': False})
        if result.get('Errors'):
            refuse('partial_provider_deletion')
        confirmed = [(row.get('Key'), row.get('VersionId') or row.get('DeleteMarkerVersionId'))
                     for row in _items(result, 'Deleted')]
        if sorted(confirmed) != sorted((row['Key'], row['VersionId']) for row in batch):
            refuse('partial_provider_deletion')


def secret_file_keys(s3, bucket, owner, prefix):
    return {entry['Key'] for _, entry in _entries(s3, bucket, owner, prefix)}


def secret_file_versions(s3, bucket, owner, key, salt, ident):
    entries, _, _, blocked = _inspect(s3, bucket, owner, key, salt, ident, 'encrypted-file')
    if blocked:
        refuse(blocked)
    return sorted(entries)


def purge_secret_file(s3, bucket, owner, key, salt, ident, approved):
    entries, _, _, blocked = _inspect(s3, bucket, owner, key, salt, ident, 'encrypted-file')
    if blocked:
        refuse(blocked)
    if not entries.keys() <= set(approved):
        refuse('version_scope_changed')
    _delete(s3, bucket, owner, key, entries)
    if secret_file_versions(s3, bucket, owner, key, salt, ident):
        refuse('provider_deletion_unverified')


def verify_absent_secret_file(s3, bucket, owner):
    # Strongly consistent, complete listing has already established absence.
    _, blocked = _bucket_status(s3, bucket, owner, versioned=False)
    if blocked:
        refuse(blocked)


def _catalog(config, s3, salt, kinds):
    owner, instance = _account(config), _uuid(config['instance_id'])
    found = {}
    for kind in sorted(kinds):
        if kind == 'skill_bundle':
            bucket = config['skill_bucket']
            prefix = f'skills/{instance}/bundles/'
        elif kind == 'recovery_object':
            bucket, prefix = config['journal_bucket'], ''
        else:
            refuse('invalid_redaction_scope')
        for _, row in _entries(s3, bucket, owner, prefix):
            key = row['Key']
            if kind == 'skill_bundle':
                match = re.fullmatch(re.escape(prefix) + r'([0-9a-f]{64})\.zip', key)
                if not match:
                    continue
                cls, name = 'bundle', match[1]
            else:
                cls, name = classify_recovery(instance, key), key
                if cls not in ('control_history', 'retirement_manifest', 'orphan'):
                    continue
            identity = (kind, handle(salt, kind, name))
            value = (bucket, key, cls)
            if found.setdefault(identity, value) != value:
                refuse('ambiguous_redaction_target')
    return found


@guarded
def recovery_inventory(config, session):
    """Report unknown recovery keys without permitting their deletion or exposing names."""
    classes = {}
    for _, row in _entries(session.client('s3'), config['journal_bucket'], _account(config), ''):
        cls = classify_recovery(config['instance_id'], row['Key'])
        classes[cls] = classes.get(cls, 0) + 1
    return classes


@guarded
def candidates(config, session, salt):
    kinds = {'recovery_object'} | ({'skill_bundle'} if config.get('skill_bucket') else set())
    return [{'kind': kind, 'id': ident} for kind, ident in sorted(_catalog(config, session.client('s3'), salt, kinds))]


def _identities(targets, *, plans=False):
    if not isinstance(targets, list):
        refuse('invalid_redaction_scope')
    identities = []
    for obj in targets:
        keys = {'kind', 'id'} | ({'class', 'versions', 'preserve'} if plans else set())
        if plans and isinstance(obj, dict) and 'blocked' in obj:
            if obj['blocked'] not in ('replication_unverified', 'object_retained'):
                refuse('invalid_redaction_scope')
            keys.add('blocked')
        if not isinstance(obj, dict) or set(obj) != keys or obj['kind'] not in ('skill_bundle', 'recovery_object'):
            refuse('invalid_redaction_scope')
        identities.append((obj['kind'], _uuid(obj['id'])))
        if plans:
            cls = obj['class']
            if (cls not in (('bundle',) if obj['kind'] == 'skill_bundle' else ('retirement_manifest', 'control_history', 'orphan')) or
                    (cls != 'control_history' and obj['preserve'] is not None) or
                    (cls == 'control_history' and not _valid_pin(obj['preserve']))):
                refuse('invalid_redaction_scope')
            validate_pins(obj['versions'])
    if len(set(identities)) != len(identities):
        refuse('invalid_redaction_scope')
    return identities


def _valid_pin(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{64}', value))


def validate_pins(pins):
    if not isinstance(pins, list) or not all(_valid_pin(v) for v in pins) or sorted(set(pins)) != pins:
        refuse('invalid_redaction_scope')


def _exclusive_recovery(config, s3):
    args = {'Bucket': config['journal_bucket'], 'ExpectedBucketOwner': _account(config)}
    rows = s3.get_bucket_tagging(**args)['TagSet']
    tags = {row['Key']: row['Value'] for row in rows}
    if (len(tags) != len(rows) or tags.get('MavenExclusive') != 'true' or
            tags.get('MavenInstanceId') != config['instance_id'] or tags.get('DataScope') != 'owner_memory'):
        refuse('unmanaged_or_protected_object')
    head = s3.head_object(**args, Key='control.json')
    _read_control(s3, {**args, 'Key': 'control.json', 'VersionId': head['VersionId']}, config)


def _bundle_key(config, salt, ident, sha):
    if target(config, salt, 'skill_bundle', sha)['id'] != ident:
        refuse('invalid_redaction_scope')
    return f"skills/{_uuid(config['instance_id'])}/bundles/{sha}.zip"


def _sentinel(s3, bucket, owner, key, latest):
    precondition = {'IfMatch': latest[1]} if latest and latest[0] == 'Versions' else {'IfNoneMatch': '*'}
    s3.put_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner, Body=b'',
                  ContentType='application/octet-stream', ServerSideEncryption='AES256',
                  Metadata={'maven-redacted': 'true'}, Tagging='maven-redacted=true', **precondition)


@guarded
def review(config, session, salt, targets, *, missing_keys=None):
    scope, s3 = _scope(config, salt), session.client('s3')
    ids = _identities(targets)
    catalog = _catalog(config, s3, salt, {kind for kind, _ in ids})
    objects = []
    for kind, ident in sorted(ids):
        location = catalog.get((kind, ident))
        if not location and kind == 'skill_bundle' and ident in (missing_keys or {}):
            location = (config['skill_bucket'], _bundle_key(config, salt, ident, missing_keys[ident]), 'bundle')
        if not location:
            refuse('object_not_found')
        bucket, key, cls = location
        if cls == 'orphan':
            _exclusive_recovery(config, s3)
        entries, _, preserved, blocked = _inspect(s3, bucket, _account(config), key, salt, ident, cls, config=config)
        objects.append({'kind': kind, 'id': ident, 'class': cls, 'versions': sorted(entries),
                        'preserve': preserved if cls == 'control_history' else None,
                        **({'blocked': blocked} if blocked else {})})
    return {'format': 1, 'scope': scope, 'objects': objects}


def _validate_plan(config, salt, plan):
    if (not isinstance(plan, dict) or set(plan) != {'format', 'scope', 'objects'} or
            type(plan['format']) is not int or plan['format'] != 1 or plan['scope'] != _scope(config, salt)):
        refuse('invalid_redaction_scope')
    return _identities(plan['objects'], plans=True)


@guarded
def prepare_missing(config, session, salt, plan, missing_keys):
    """Inside the approved SQL transaction, before a never-uploaded locator is scrubbed."""
    _validate_plan(config, salt, plan)
    s3, owner = session.client('s3'), _account(config)
    for obj in plan['objects']:
        if obj['kind'] != 'skill_bundle' or obj['versions'] or obj['id'] not in missing_keys:
            continue
        ident, bucket = obj['id'], config['skill_bucket']
        key = _bundle_key(config, salt, ident, missing_keys[ident])
        args = (s3, bucket, owner, key, salt, ident, 'bundle')
        entries, latest, preserved, _ = _inspect(*args)
        if entries:
            refuse('version_scope_changed')
        if not preserved:
            _sentinel(s3, bucket, owner, key, latest)
            entries, _, preserved, _ = _inspect(*args)
            if entries or not preserved:
                refuse('redaction_fence_unverified')


@guarded
def apply(config, session, salt, plan):
    return _apply(config, session, salt, plan, restoring=False)


@guarded
def restore(config, session, salt, plan):
    return _apply(config, session, salt, plan, restoring=True)


def _apply(config, session, salt, plan, *, restoring):
    ids = _validate_plan(config, salt, plan)
    s3, owner = session.client('s3'), _account(config)
    catalog = _catalog(config, s3, salt, {kind for kind, _ in ids})
    results = []
    pending = None
    for obj in plan['objects']:
        kind, ident, cls = obj['kind'], obj['id'], obj['class']
        location = catalog.get((kind, ident))
        if cls == 'orphan':
            _exclusive_recovery(config, s3)
        if not location:
            # A retired bundle must still have its zero-byte write fence. Recovery
            # manifests, by contrast, can be completely absent after a lost reply.
            if cls not in ('retirement_manifest', 'orphan'):
                refuse('redaction_fence_unverified')
            _, blocked = _bucket_status(s3, config['journal_bucket'], owner, versioned=True)
        else:
            bucket, key, actual_cls = location
            if actual_cls != cls:
                refuse('invalid_redaction_scope')
            def inspect():
                return _inspect(s3, bucket, owner, key, salt, ident, cls, config=config)
            entries, latest, preserved, blocked = inspect()
            approved = set(obj['versions'])
            historical = restoring and cls == 'control_history'
            if historical:
                if preserved in approved:
                    refuse('control_unverified')
                entries = {k: v for k, v in entries.items() if k in approved}
            elif not entries.keys() <= approved or (cls == 'control_history' and preserved != obj['preserve']):
                refuse('version_scope_changed')
            if cls == 'bundle' and not preserved:
                _sentinel(s3, bucket, owner, key, latest)
                entries, _, preserved, observed = inspect()
                blocked = observed if observed == 'replication_unverified' else blocked or observed
                if not preserved or not entries.keys() <= approved:
                    refuse('version_scope_changed')
            if not blocked and obj.get('blocked') != 'replication_unverified':
                _delete(s3, bucket, owner, key, entries)
                remaining, _, final, blocked = inspect()
                if (remaining.keys() & approved if historical else remaining) or final != preserved:
                    refuse('version_scope_changed')
        # Deleting a replication rule is not evidence that earlier destination
        # copies were purged. Never release a restore on that basis.
        if obj.get('blocked') == 'replication_unverified':
            blocked = 'replication_unverified'
        if blocked:
            pending = blocked if blocked == 'replication_unverified' else pending or blocked
            continue
        results.append({'kind': kind, 'id': ident, 'versions': len(obj['versions'])})
    if pending:
        refuse(pending)
    return {'status': 'objects_verified_purged', 'objects': results}
