"""Append-only, content-free recovery program, outside Aurora's restore timeline.

Conditional S3 puts publish an entry and a separate high-water seal. Reverse
sequence keys make the first strongly consistent listings the two tips. Neither
has a mutable 'latest' pointer. Entries are hash-linked, not signed: the trust
boundary is the retained, write-once namespace of the original bucket.
"""
import re

from .validation import MemoryError, canonical, decode_json, digest, uuid_string

MAX_SEQUENCE = 10**20 - 1
# One entry contains only a validated SQL review (itself <= Data API's bounded response).
MAX_ENTRY_BYTES = 1024 * 1024
HEX = re.compile(r'[a-f0-9]{64}\Z')
KINDS = frozenset({'record', 'source', 'page', 'project', 'project_record', 'proposal',
                   'challenge', 'publication', 'instruction_receipt', 'operation', 'memory_receipt',
                   'consumption', 'cleanup', 'lifecycle', 'citation', 'skill_bundle', 'recovery_object'})


def refuse():
    # Never copy object keys, provider prose, SQL data or untrusted JSON into errors.
    raise MemoryError('redaction_ledger_unverified', 409)


def describe_cluster(rds, config):
    arn = config['cluster_arn']
    response = rds.describe_db_clusters(DBClusterIdentifier=arn)
    clusters = response.get('DBClusters')
    if not isinstance(clusters, list) or len(clusters) != 1 or response.get('Marker'):
        refuse()
    item = clusters[0]
    rid = item.get('DbClusterResourceId')
    if (item.get('DBClusterArn') != arn or not isinstance(rid, str)
            or not re.fullmatch(r'cluster-[A-Za-z0-9]{8,64}', rid)):
        refuse()
    return item, digest({'arn': arn, 'resource_id': rid})


def cluster_identity(rds, config):
    """The ARN alone can be reused after replacement. The AWS resource ID cannot."""
    return describe_cluster(rds, config)[1]


def _prefix(config):
    return 'redaction-ledger/' + uuid_string(config['instance_id']) + '/'


def _key(config, sequence):
    if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
        refuse()
    return f'{_prefix(config)}entries/{MAX_SEQUENCE-sequence:020}.json'


def _hex(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def _object_plan(plan):
    if (set(plan) != {'format', 'scope', 'objects'} or type(plan['format']) is not int
            or plan['format'] != 1 or not isinstance(plan['objects'], list)):
        refuse()
    uuid_string(plan['scope'])
    seen = set()
    classes = {'skill_bundle': {'bundle'}, 'recovery_object': {'control_history', 'retirement_manifest', 'orphan'}}
    for obj in plan['objects']:
        required = {'kind', 'id', 'class', 'versions', 'preserve'}
        if (set(obj) not in (required, required | {'blocked'}) or
                'blocked' in obj and obj['blocked'] not in ('object_retained', 'replication_unverified')):
            refuse()
        identity = obj['kind'], uuid_string(obj['id'])
        if (identity in seen or obj['class'] not in classes[obj['kind']]
                or not isinstance(obj['versions'], list) or not all(_hex(p) for p in obj['versions'])
                or obj['versions'] != sorted(set(obj['versions']))):
            refuse()
        if obj['class'] == 'control_history':
            if not _hex(obj['preserve']):
                refuse()
        elif obj['preserve'] is not None:
            refuse()
        seen.add(identity)
    return seen


def _validate(entry, config, sequence):
    base = {'type', 'instance_id', 'epoch', 'cluster', 'previous', 'sequence'}
    extras = {'genesis': {'salt'}, 'redact': {'job', 'targets', 'plan_digest', 'object_plan'}, 'cutover': set()}
    try:
        kind = entry['type']
        if (set(entry) != base | extras[kind] or entry['instance_id'] != config['instance_id']
                or type(entry['sequence']) is not int or entry['sequence'] != sequence
                or not _hex(entry['cluster']) or (sequence == 0) != (kind == 'genesis')):
            refuse()
        uuid_string(entry['epoch'])
        if sequence == 0:
            uuid_string(entry['salt'])
            if entry['previous'] is not None:
                refuse()
        elif not _hex(entry['previous']):
            refuse()
        if kind == 'redact':
            uuid_string(entry['job'])
            objects = _object_plan(entry['object_plan'])
            if not _hex(entry['plan_digest']) or not isinstance(entry['targets'], list) or not entry['targets']:
                refuse()
            seen = set()
            for target in entry['targets']:
                token = target['kind']
                if token not in KINDS or set(target) != ({'kind', 'id', 'project_id'} if token == 'project_record' else {'kind', 'id'}):
                    refuse()
                key = (token, uuid_string(target['id']), uuid_string(target['project_id']) if token == 'project_record' else None)
                if key in seen:
                    refuse()
                seen.add(key)
            if objects != {(k, i) for k, i, _ in seen if k in {'skill_bundle', 'recovery_object'}}:
                refuse()
    except (TypeError, KeyError, ValueError, MemoryError):
        refuse()
    return entry


def _read(s3, config, sequence):
    obj = s3.get_object(Bucket=config['journal_bucket'], Key=_key(config, sequence))
    stream = obj['Body']
    try:
        raw = stream.read(MAX_ENTRY_BYTES + 1)
    finally:
        stream.close()
    if len(raw) > MAX_ENTRY_BYTES:
        refuse()
    try:
        entry = decode_json(raw)
        if canonical(entry).encode() != raw:
            refuse()
    except MemoryError:
        refuse()
    return _validate(entry, config, sequence)


def _first(s3, config, subdirectory, suffix):
    prefix = _prefix(config) + subdirectory + '/'
    response = s3.list_objects_v2(Bucket=config['journal_bucket'], Prefix=prefix, MaxKeys=1)
    items = response.get('Contents', [])
    if items == [] and response.get('KeyCount') == 0 and response.get('IsTruncated') is False:
        return None
    if not isinstance(items, list) or len(items) != 1:
        refuse()
    key = items[0]['Key']
    match = re.fullmatch(re.escape(prefix) + suffix, key)
    if not match:
        refuse()
    return match


def latest(s3, config, *, initializing=False, resume=None):
    first = _first(s3, config, 'entries', r'([0-9]{20})\.json')
    seal = _first(s3, config, 'seals', r'([0-9]{20})-([0-9a-f]{64})')
    if first is None:
        if initializing and seal is None:
            return None
        refuse()
    sequence = MAX_SEQUENCE - int(first[1])
    entry = _read(s3, config, sequence)
    if seal and seal[1] == first[1] and seal[2] == digest(entry):
        return entry
    # A partial publish is never serving authority. Only the exact same
    # authorized operation can finish its missing seal, not a shortened log.
    one_ahead = (seal is None and sequence == 0 or seal is not None
                 and MAX_SEQUENCE - int(seal[1]) == sequence - 1 and seal[2] == entry['previous'])
    if one_ahead and ((initializing and sequence == 0) or (resume is not None and resume(entry))):
        return entry
    refuse()


def _put(s3, config, key, body):
    try:
        s3.put_object(Bucket=config['journal_bucket'], Key=key,
                      IfNoneMatch='*', Body=body, ContentType='application/json',
                      ServerSideEncryption='AES256', ChecksumAlgorithm='SHA256')
    except Exception as error:
        if getattr(error, 'response', {}).get('Error', {}).get('Code') not in {'PreconditionFailed', '412'}:
            raise


def _publish(s3, config, entry):
    sequence = entry['sequence']
    _validate(entry, config, sequence)
    body = canonical(entry).encode()
    if len(body) > MAX_ENTRY_BYTES:
        refuse()
    _put(s3, config, _key(config, sequence), body)
    # Never seal a losing same-sequence writer's intent. An independent
    # high-water evidence stream makes a truncated but valid chain detectable.
    if latest(s3, config, resume=lambda actual: actual == entry) != entry:
        refuse()
    _put(s3, config, f'{_prefix(config)}seals/{MAX_SEQUENCE-sequence:020}-{digest(entry)}', b'')
    if latest(s3, config) != entry:
        refuse()
    return digest(entry)


def history(s3, config, tip):
    """Validate every link and immutable incarnation transition, streaming bounded entries."""
    previous = None
    epochs, clusters = set(), set()
    for i in range(tip['sequence'] + 1):
        current = _read(s3, config, i)
        if previous is not None and current['previous'] != digest(previous):
            refuse()
        if current['type'] in {'cutover', 'genesis'}:
            if current['epoch'] in epochs or current['cluster'] in clusters:
                refuse()
            epochs.add(current['epoch'])
            clusters.add(current['cluster'])
        elif (current['epoch'], current['cluster']) != (previous['epoch'], previous['cluster']):
            refuse()
        previous = current
        yield current
    if previous != tip:
        refuse()


def bound(entry, config, physical):
    if entry['cluster'] != physical or entry['epoch'] != config['recovery_epoch']:
        refuse()
    return digest(entry)


def lock(db, tx):
    return db.scalar('SELECT redaction_checkpoint FROM maven.service_state WHERE singleton FOR UPDATE', transaction=tx)


def advance(db, tx, old, new):
    if db.scalar('UPDATE maven.service_state SET redaction_checkpoint=:new WHERE singleton '
                 'AND redaction_checkpoint IS NOT DISTINCT FROM CAST(:old AS text) RETURNING true',
                 {'new': new, 'old': old}, tx) is not True:
        refuse()


def genesis(db, tx, s3, config, physical):
    """Only caller's positively identified fresh-install/legacy-adoption branch may use this."""
    if db.scalar("SELECT true FROM maven.service_state WHERE singleton AND redaction_checkpoint IS NULL "
                 "AND state='maintenance' AND instance_id=CAST(:instance AS uuid) AND recovery_epoch=CAST(:epoch AS uuid) "
                 "FOR UPDATE", {'instance': config['instance_id'], 'epoch': config['recovery_epoch']}, tx) is not True:
        refuse()
    prior = latest(s3, config, initializing=True)
    # On a DB rollback a random SQL salt created by 026 is lost. Recover the original
    # salt from an exact, same-cluster unadvanced genesis, never invent a second root.
    if prior is not None:
        if prior['type'] != 'genesis':
            refuse()
        bound(prior, config, physical)
        restore_salt(db, tx, prior['salt'])
    salt = db.scalar('SELECT maven.redaction_salt()::text', transaction=tx)
    entry = {'type': 'genesis', 'sequence': 0, 'instance_id': config['instance_id'],
             'epoch': config['recovery_epoch'], 'cluster': physical, 'previous': None, 'salt': salt}
    checkpoint = _publish(s3, config, entry)
    advance(db, tx, None, checkpoint)
    return checkpoint


def restore_salt(db, tx, salt):
    if db.scalar('UPDATE maven.redaction_identity SET salt=CAST(:salt AS uuid) WHERE singleton RETURNING true',
                 {'salt': uuid_string(salt)}, tx) is not True:
        refuse()


def verify(db, tx, s3, config, physical, *, job=None, plan_digest=None):
    mark = lock(db, tx)
    entry = latest(s3, config, resume=lambda e: e['type'] == 'redact' and e['job'] == job
                   and e['plan_digest'] == plan_digest and e['previous'] == mark)
    checkpoint = bound(entry, config, physical)
    if mark == checkpoint:
        return entry
    # Ambiguous S3 success / aborted SQL commit. No OTHER operation may use this gap.
    if (entry['type'] == 'redact' and entry['job'] == job and entry['plan_digest'] == plan_digest
            and entry['previous'] == mark):
        return entry
    refuse()


def record_applied(db, tx, s3, config, physical, job, permanent_targets, plan_digest, *, object_plan):
    """Called AFTER locked SQL eligibility/scrub but BEFORE that transaction can commit."""
    tip = verify(db, tx, s3, config, physical, job=job, plan_digest=plan_digest)
    old = lock(db, tx)
    if tip['type'] == 'redact' and tip['job'] == job:
        if tip['targets'] != permanent_targets or tip['plan_digest'] != plan_digest or tip['object_plan'] != object_plan:
            refuse()
        checkpoint = _publish(s3, config, tip)
    else:
        entry = {'type': 'redact', 'sequence': tip['sequence'] + 1, 'instance_id': config['instance_id'],
                 'epoch': config['recovery_epoch'], 'cluster': physical, 'previous': digest(tip),
                 'job': job, 'targets': permanent_targets, 'plan_digest': plan_digest, 'object_plan': object_plan}
        checkpoint = _publish(s3, config, entry)
    advance(db, tx, old, checkpoint)
    return checkpoint


def cutover(s3, config, physical, tip):
    entry = {'type': 'cutover', 'sequence': tip['sequence'] + 1, 'instance_id': config['instance_id'],
             'epoch': config['recovery_epoch'], 'cluster': physical, 'previous': digest(tip)}
    return _publish(s3, config, entry)
