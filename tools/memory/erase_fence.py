"""Restore witnesses for the *older*, narrower Erase and reconciled terminal records.

Object names contain opaque IDs only. Never give the Lambda permission to issue
legacy cleanup approvals or to modify the separate reviewed-redaction ledger.
"""
from .validation import MemoryError, canonical, decode_json, uuid_string

from hashlib import sha256
from itertools import zip_longest


def _refuse():
    raise MemoryError('erase_fence_unverified', 409)


def _prefix(config):
    return 'narrow-erases/' + uuid_string(config['instance_id']) + '/'


def _get(s3, config, key):
    body = s3.get_object(Bucket=config['journal_bucket'], Key=key)['Body']
    try:
        value = body.read(65001)
    finally:
        body.close()
    if len(value) > 65000:
        _refuse()
    return value


def _put_one(s3, config, suffix, data):
    key = _prefix(config) + suffix
    try:
        s3.put_object(Bucket=config['journal_bucket'], Key=key, Body=data, IfNoneMatch='*', ServerSideEncryption='AES256')
    except Exception as exc:
        if getattr(exc, 'response', {}).get('Error', {}).get('Code') not in {'PreconditionFailed', '412'}:
            _refuse()
        # Empty, immutable project markers have a single valid representation. Legacy
        # bodies must agree with their original scope even after a lost commit reply.
        if _get(s3, config, key) != data:
            _refuse()


def _put(s3, config, suffix, data=b''):
    _put_one(s3, config, 'entries/'+suffix, data)
    _put_one(s3, config, 'seals/'+suffix+'/'+sha256(data).hexdigest(), b'')


def record_project_erases(s3, config, project, ids):
    for rid in ids:
        _put(s3, config, f'project/{uuid_string(project)}/{uuid_string(rid)}')


def _manifest(value):
    if not isinstance(value, dict) or set(value) != {'job', 'records', 'sources'}:
        _refuse()
    uuid_string(value['job'])
    for key in ('records', 'sources'):
        rows = value[key]
        if not isinstance(rows, list) or sorted({uuid_string(x) for x in rows}) != rows:
            _refuse()
    if not 1 <= len(value['records']) <= 200:
        _refuse()
    return value


def record_legacy_erase(db, tx, s3, config, job):
    raw = db.json_scalar('maven.narrow_legacy_manifest(CAST(:op AS uuid))', {'op': uuid_string(job)}, tx)
    data = canonical(_manifest(decode_json(raw))).encode()
    if len(data) > 65000:
        _refuse()
    _put(s3, config, 'legacy/' + job, data)


def initialize(db, tx, s3, config):
    """Adopt only positively identified original installations, under the migration lock."""
    cursor = ''
    while True:
        page = decode_json(db.json_scalar("(SELECT coalesce(jsonb_agg(k ORDER BY k),'[]'::jsonb) FROM ("
            "SELECT k FROM (SELECT 'legacy/'||id AS k FROM maven.cleanup_receipts WHERE action='erase' UNION ALL "
            "SELECT 'project/'||project_id||'/'||id FROM maven.reconcile_records WHERE item->>'kind'='erased') all_ids "
            "WHERE k>:after ORDER BY k LIMIT 200) page)", {'after': cursor}, tx))
        if not page:
            break
        for suffix in page:
            if suffix <= cursor:
                _refuse()
            cursor = suffix
            parts = suffix.split('/')
            if parts[0] == 'legacy' and len(parts) == 2:
                record_legacy_erase(db, tx, s3, config, parts[1])
            elif parts[0] == 'project' and len(parts) == 3:
                record_project_erases(s3, config, parts[1], [parts[2]])
            else:
                _refuse()
    _put(s3, config, 'bound')


def _objects(s3, config, category):
    prefix = _prefix(config) + category + '/'
    after = ''
    while True:
        result = s3.list_objects_v2(Bucket=config['journal_bucket'], Prefix=prefix, **({'StartAfter': after} if after else {}))
        rows = result.get('Contents', [])
        if result.get('KeyCount') != len(rows) or not rows:
            _refuse()
        for row in rows:
            key = row['Key']
            if not key.startswith(prefix) or key <= after:
                _refuse()
            after = key
            yield key[len(prefix):], row['Size']
        if result.get('IsTruncated') is False:
            return
        if result.get('IsTruncated') is not True:
            _refuse()


def _check(db, tx, s3, config, apply):
    seen_root = False
    entries = _objects(s3, config, 'entries')
    seals = _objects(s3, config, 'seals')
    for obj, seal in zip_longest(entries, seals):
        if obj is None or seal is None:
            _refuse()
        suffix, size = obj
        parts = suffix.split('/')
        data = b''
        if parts == ['bound'] and size == 0:
            seen_root = True
        elif len(parts) == 3 and parts[0] == 'project' and size == 0:
            sql = 'SELECT maven.narrow_replay_project(CAST(:p AS uuid),CAST(:r AS uuid),:apply)'
            args = {'p': uuid_string(parts[1]), 'r': uuid_string(parts[2])}
        elif len(parts) == 2 and parts[0] == 'legacy':
            data = _get(s3, config, _prefix(config)+'entries/'+suffix)
            manifest = _manifest(decode_json(data.decode()))
            if manifest['job'] != parts[1]:
                _refuse()
            sql = 'SELECT maven.narrow_replay_legacy(CAST(:manifest AS jsonb),:apply)'
            args = {'manifest': canonical(manifest)}
        else:
            _refuse()
        if seal != (suffix+'/'+sha256(data).hexdigest(), 0):
            _refuse()
        if parts != ['bound'] and db.scalar(sql, {**args, 'apply': apply}, tx) is not True:
            _refuse()
    if not seen_root:
        _refuse()


def restore(db, tx, s3, config):
    _check(db, tx, s3, config, True)


def verify_restored(db, tx, s3, config):
    _check(db, tx, s3, config, False)
