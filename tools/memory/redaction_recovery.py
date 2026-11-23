"""MemoryAdmin-only recovery of an already isolated Aurora restore, never a DB provisioner.

No bootstrap, missing-ledger genesis, password issuance or normal credential use
in the isolation/replay phases. The old instance and replacement compete for one
conditional independent log sequence; they cannot both become current.
"""
from contextlib import ExitStack, contextmanager
from functools import cache
import json
import logging

from . import admin, redaction_ledger as ledger, redaction_objects
from .store import DataAPI, fetch_control, safe_provider_error, sdk_config
from .validation import MemoryError, authorized_actor, canonical, digest


@contextmanager
def _runtime(config, session):
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        admin._config(config)
        kwargs = {'region_name': config['region'], 'config': sdk_config()}
        with ExitStack() as stack:
            @cache
            def client(service):
                sdk = session.client(service, **kwargs)
                stack.callback(sdk.close)
                return sdk
            identity = client('sts').get_caller_identity()
            authorized_actor({'requestContext': {'authorizer': {'iam': {'userArn': identity['Arn']}}}},
                             config['admin_role_arn'])
            yield client
    except MemoryError:
        raise
    except redaction_objects.ObjectRedactionError as exc:
        raise MemoryError(exc.code, 409) from None
    except Exception as exc:
        raise safe_provider_error(exc) from None
    finally:
        logging.disable(previous)


def _isolated(client, config):
    row, physical = ledger.describe_cluster(client('rds'), config)
    if (row.get('Status') != 'available' or row.get('Engine') != 'aurora-postgresql'
            or row.get('IAMDatabaseAuthenticationEnabled') is not False or row.get('HttpEndpointEnabled') is not True):
        ledger.refuse()
    attached = row['VpcSecurityGroups']
    if not attached or any(sg['Status'] != 'active' for sg in attached):
        ledger.refuse()
    groups = {sg['VpcSecurityGroupId'] for sg in attached}
    actual = client('ec2').describe_security_groups(GroupIds=sorted(groups))
    if (actual.get('NextToken') or {sg['GroupId'] for sg in actual['SecurityGroups']} != groups
            or any(sg.get('IpPermissions') != [] for sg in actual['SecurityGroups'])):
        ledger.refuse()
    return physical


def _installed(db, tx):
    raw = db.scalar("SELECT jsonb_object_agg(name,sha256)::text FROM maven.schema_migrations", transaction=tx)
    installed = json.loads(raw) if raw else None
    bundle = admin.migrations()
    if not installed or installed != {name: sha for name, sha, _ in bundle[:len(installed)]}:
        ledger.refuse()
    return installed, bundle[len(installed):]


def _control(s3, config, source_epoch, released):
    # A lost reply after successful activation may see destination ready; every
    # other stage requires the original, non-retired, independently bound control.
    current = {**config, 'recovery_epoch': source_epoch}
    try:
        document, etag = fetch_control(s3, current, allow_maintenance=True)
    except MemoryError:
        if not released:
            raise
        current = config
        document, etag = fetch_control(s3, current, allow_maintenance=True)
    if document.get('uninstall') or document.get('redaction_bound') is not True or not etag:
        ledger.refuse()
    return current, document, etag


def _inspect(client, config, prior_review=None):
    s3 = client('s3')
    physical = _isolated(client, config)
    def resuming_cutover(entry):
        return (isinstance(prior_review, dict) and entry['type'] == 'cutover'
                and entry['previous'] == prior_review.get('checkpoint') and entry['cluster'] == physical
                and entry['epoch'] == config['recovery_epoch'])
    tip = ledger.latest(s3, config, resume=resuming_cutover)
    released = tip['cluster'] == physical and tip['epoch'] == config['recovery_epoch']
    if released:
        if not isinstance(prior_review, dict) or type(prior_review.get('sequence')) is not int:
            ledger.refuse()
        source = ledger._read(s3, config, prior_review['sequence'])
        release = ledger._read(s3, config, source['sequence'] + 1)
        if (release['type'], release['cluster'], release['epoch']) != ('cutover', physical, config['recovery_epoch']):
            ledger.refuse()
    else:
        source = tip
    plan = {'instance_id': config['instance_id'], 'recovery_epoch': config['recovery_epoch'],
            'source_epoch': source['epoch'], 'source_cluster': source['cluster'], 'destination_cluster': physical,
            'checkpoint': digest(source), 'sequence': source['sequence']}
    if prior_review is not None and canonical(plan) != canonical(prior_review):
        ledger.refuse()
    current, _, _ = _control(s3, config, source['epoch'], released)
    data = client('rds-data')
    db = DataAPI(data, config, admin=True)
    admin._check_logging(db)
    with db.transaction() as tx:
        installed, _ = _installed(db, tx)
        state = admin._state(db, tx)
        admin._identity(state, {**config, 'recovery_epoch': state['recovery_epoch']}, allow_legacy=True)
        mark = ledger.lock(db, tx) if '026_reviewed_redaction.sql' in installed else None
    origin_proven = False
    for entry in ledger.history(s3, config, tip):
        if entry['sequence'] <= source['sequence'] and (entry['cluster'] == physical or entry['epoch'] == config['recovery_epoch']):
            ledger.refuse()  # Never promote the original or reuse an old epoch/physical cluster.
        if state['recovery_epoch'] == entry['epoch'] and (mark == digest(entry) or
                (entry['type'] == 'genesis' and '026_reviewed_redaction.sql' not in installed)):
            origin_proven = True
    if not origin_proven or ledger.latest(s3, config, resume=resuming_cutover) != tip:
        ledger.refuse()
    return plan, source, released, current, s3, data, db


def review(config, session):
    """Read-only; hashes identify deployments, not payloads or provider locators."""
    with _runtime(config, session) as client:
        plan, *_ = _inspect(client, config)
        return plan


def _isolate(db, genesis):
    with db.transaction() as tx:
        db.execute('SELECT 1 FROM pg_advisory_xact_lock(1967318421)', transaction=tx)
        # Separate commit: LOGIN must remain disabled even if a later stage aborts.
        db.execute('ALTER ROLE maven_app NOLOGIN', transaction=tx)
        db.execute("UPDATE maven.service_state SET state='maintenance' WHERE singleton", transaction=tx)
    with db.transaction() as tx:
        if db.scalar("SELECT COALESCE(bool_and(pg_terminate_backend(pid,1000)),true) "
                     "FROM pg_stat_activity WHERE usename='maven_app'", transaction=tx) is not True:
            ledger.refuse()
        db.execute('SELECT 1 FROM pg_advisory_xact_lock(1967318421)', transaction=tx)
        installed, remaining = _installed(db, tx)
        for name, checksum, statements in remaining:
            admin._apply_migration(db, tx, name, checksum, statements)
        admin._grant_instructions(db, tx)
        ledger.restore_salt(db, tx, genesis['salt'])
        if '026_reviewed_redaction.sql' not in installed:
            ledger.advance(db, tx, None, digest(genesis))


def _replay(db, tx, entry):
    result = json.loads(db.json_scalar('maven.redaction_replay(CAST(:job AS uuid),CAST(:targets AS jsonb))',
                                       {'job': entry['job'], 'targets': canonical(entry['targets'])}, tx))
    bundles = result.get('pending_bundles')
    reviewed = {obj['id'] for obj in entry['object_plan']['objects'] if obj['kind'] == 'skill_bundle'}
    if (result.get('job_id') != entry['job'] or result.get('live_data_redacted') is not True
            or not isinstance(bundles, list) or not set(bundles) <= reviewed):
        # A backup-local dependency can be logically scrubbed, but new physical
        # purges must never inherit an old approval.
        ledger.refuse()


def apply(config, session, review):
    from . import erase_fence
    with _runtime(config, session) as client:
        plan, source, released, current, s3, data, db = _inspect(client, config, review)
        physical = plan['destination_cluster']
        root = ledger._read(s3, config, 0)
        _, document, etag = _control(s3, config, source['epoch'], released)
        if released and document['recovery_epoch'] == config['recovery_epoch'] and document['state'] == 'ready':
            # A later live redaction does not invalidate the receipt for this
            # already completed release. Never disable its clients to replay again.
            with db.transaction() as tx:
                ledger.verify(db, tx, s3, config, physical)
                erase_fence.verify_restored(db, tx, s3, config)
                if admin._state(db, tx)['state'] != 'ready':
                    ledger.refuse()
            release = ledger._read(s3, config, source['sequence'] + 1)
            return {'status': 'restore_verified', 'instance_id': config['instance_id'],
                    'recovery_epoch': config['recovery_epoch'], 'checkpoint': digest(release)}
        if not released and document['state'] != 'maintenance':
            admin._put_control(s3, current, 'maintenance', etag=etag, bound=True)
        _isolate(db, root)
        for entry in ledger.history(s3, config, source):
            if entry['type'] != 'redact':
                continue
            with db.transaction() as tx:
                _replay(db, tx, entry)
            # Provider work is outside the DB transaction. Never use an old
            # outbox.verified bit as evidence, even on a recent snapshot.
            result = redaction_objects.restore(current, session, root['salt'], entry['object_plan'])
            if result['status'] != 'objects_verified_purged':
                ledger.refuse()
            with db.transaction() as tx:
                db.execute('INSERT INTO maven.redaction_outbox(job_id,review_digest,object_plan,verified) '
                           'VALUES(CAST(:job AS uuid),:review,CAST(:plan AS jsonb),true) ON CONFLICT(job_id) '
                           'DO UPDATE SET review_digest=EXCLUDED.review_digest,object_plan=EXCLUDED.object_plan,verified=true',
                           {'job': entry['job'], 'review': entry['plan_digest'], 'plan': canonical(entry['object_plan'])}, tx)
        if _isolated(client, config) != physical:
            ledger.refuse()
        with db.transaction() as tx:
            mark = ledger.lock(db, tx)
            erase_fence.restore(db, tx, s3, config)
            if db.scalar('SELECT NOT EXISTS (SELECT 1 FROM maven.redaction_outbox WHERE NOT verified)',
                         transaction=tx) is not True:
                ledger.refuse()
            checkpoint = ledger.cutover(s3, config, physical, source)
            ledger.advance(db, tx, mark, checkpoint)
            db.execute("UPDATE maven.service_state SET recovery_epoch=CAST(:epoch AS uuid), state='maintenance' "
                       "WHERE singleton", {'epoch': config['recovery_epoch']}, tx)
        # Only after the committed restored DB and independent log agree may the
        # existing application credential be used. No new credential is issued.
        admin._verify_application(db, config, pre_login=True)
        with db.transaction() as tx:
            ledger.verify(db, tx, s3, config, physical)
            erase_fence.restore(db, tx, s3, config)
            admin._identity(admin._state(db, tx), config)
            db.execute('ALTER ROLE maven_app LOGIN', transaction=tx)
        admin._verify_application(DataAPI(data, config), config)
        with db.transaction() as tx:
            ledger.verify(db, tx, s3, config, physical)
            db.execute("UPDATE maven.service_state SET state='ready' WHERE singleton", transaction=tx)
        _, document, etag = _control(s3, config, source['epoch'], True)
        if document['recovery_epoch'] != config['recovery_epoch'] or document['state'] != 'ready':
            admin._put_control(s3, config, 'ready', etag=etag, bound=True)
        active, _ = fetch_control(s3, config)
        if active.get('redaction_bound') is not True or ledger.bound(ledger.latest(s3, config), config, physical) != checkpoint:
            ledger.refuse()
        return {'status': 'restore_verified', 'instance_id': config['instance_id'],
                'recovery_epoch': config['recovery_epoch'], 'checkpoint': checkpoint}
