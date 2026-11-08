"""Explicit administrative work barriers; no migration, deletion, or invented closure."""
from __future__ import annotations

from contextlib import closing
import json

from .common import UninstallError, canonical, checked_path, digest, file_digest, read_private


MINIMUM_BARRIER_MIGRATION = 8
PROJECT_MIGRATION = "011_project_lifecycle.sql"
MAX_EVIDENCE = 1000


def _connection(home, expected=None):
    # Keep local-only uninstall importable without cloud dependencies.
    from tools.memory import admin
    from tools.memory.client import selected_session
    from tools.memory.environment import load_environment, memory_config
    from tools.memory.store import DataAPI, sdk_config
    from tools.memory.validation import authorized_actor

    home = checked_path(home)
    environment_path = checked_path(home / '.env')
    values = load_environment(environment_path)
    config = memory_config(values)
    environment_hash = file_digest(environment_path)
    if expected is not None and (config != expected['config']
                                 or environment_hash != expected['environment_sha256']):
        raise UninstallError('selected instance environment changed; create a new plan')
    session = selected_session(config, admin=True, environment=values)
    kwargs = {'region_name': config['region'], 'config': sdk_config()}
    with closing(session.client('sts', **kwargs)) as sts:
        identity = sts.get_caller_identity()
    actor = authorized_actor({'requestContext': {'authorizer': {'iam': {'userArn': identity['Arn']}}}},
                             config['admin_role_arn'])
    principal = {'arn': actor, 'account': identity['Account'],
                 'role_id': identity['UserId'].split(':', 1)[0]}
    if expected is not None and principal != expected['admin_identity']:
        raise UninstallError('selected administrative identity changed')
    db = DataAPI(session.client('rds-data', **kwargs), config, admin=True)
    try:
        admin._check_logging(db)
        s3 = session.client('s3', **kwargs)
    except BaseException:
        db.client.close()
        raise
    return db, s3, {
        'home': str(home), 'config': config, 'environment_sha256': environment_hash,
        'instance_id': config['instance_id'], 'recovery_epoch': config['recovery_epoch'],
        'admin_identity': principal,
    }


def _schema(db, tx, *, project=False, expected=None):
    from tools.memory.admin import migrations
    installed = json.loads(db.scalar(
        "SELECT COALESCE(jsonb_object_agg(name,sha256),'{}'::jsonb)::text FROM maven.schema_migrations",
        transaction=tx))
    bundle = migrations()
    prefix = {name: sha for name, sha, _ in bundle[:len(installed)]}
    if (len(installed) < MINIMUM_BARRIER_MIGRATION or installed != prefix
            or (project and PROJECT_MIGRATION not in installed)):
        raise UninstallError('explicit reviewed migration required: maven-memory install migrate '
                             '--confirm-instance UUID; uninstall never migrates implicitly')
    if expected is not None and installed != expected:
        raise UninstallError('installed migration inventory changed; create a new plan')
    return installed


def _state(db, config, tx, *, exclusive=False):
    from tools.memory import admin
    db.execute('SELECT singleton FROM maven.service_state WHERE singleton FOR '
               + ('UPDATE' if exclusive else 'SHARE'), transaction=tx)
    state = admin._state(db, tx)
    admin._identity(state, config)
    return state


def _work(db, tx, project_id=None):
    """Bind unfinished outcomes and live claims; expired claims remain historical."""
    expression = """(WITH selected AS MATERIALIZED (
        SELECT r.id, r.kind, r.revision, m.project_id, m.session_id, m.claim_session,
               m.claim_fence, m.claim_expires_at, m.data->>'state' AS state,
               v.content_hash, encode(sha256(convert_to(to_jsonb(m)::text,'UTF8')),'hex') AS item_hash
        FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
        JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision
        WHERE (CAST(:project AS uuid) IS NULL OR m.project_id=CAST(:project AS uuid))
          AND ((r.kind='session' AND m.data->>'state'='active')
            OR (r.kind='operation' AND m.data->>'state' IN ('started','blocked'))
            OR (r.kind='checkpoint' AND m.data->>'state'='prepared')
            OR (m.claim_session IS NOT NULL AND m.claim_expires_at > transaction_timestamp()))
        ORDER BY r.id LIMIT 1001
    ), receipts AS MATERIALIZED (
        SELECT o.request_key, o.request_hash, o.action, o.record_id, o.revision,
            encode(sha256(convert_to(o.result::text,'UTF8')),'hex') AS result_hash
        FROM maven.memory_receipts o JOIN selected s ON s.id=o.record_id
        ORDER BY o.request_key LIMIT 1001
    ) SELECT jsonb_build_object(
        'records',COALESCE((SELECT jsonb_agg(to_jsonb(s) ORDER BY s.id) FROM selected s),'[]'::jsonb),
        'receipts',COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.request_key) FROM receipts o),'[]'::jsonb)))"""
    evidence = json.loads(db.json_scalar(expression, {'project': project_id}, tx))
    if any(len(evidence[key]) > MAX_EVIDENCE for key in ('records', 'receipts')):
        raise UninstallError('unresolved work exceeds bounded inventory; close sessions before planning')
    return evidence


def _project(db, tx, project_id, *, lifecycle):
    raw = db.scalar("SELECT jsonb_build_object('project_id',r.id,'revision',r.revision,"
                    "'content_hash',v.content_hash,'item_hash',encode(sha256(convert_to(to_jsonb(m)::text,"
                    "'UTF8')),'hex'))::text FROM maven.records r JOIN maven.memory_items m ON m.id=r.id "
                    "JOIN maven.record_versions v ON v.record_id=r.id AND v.revision=r.revision "
                    "WHERE r.id=CAST(:project AS uuid) AND r.kind='entity' AND r.data_scope='owner' "
                    "AND m.data->>'entity_type'='project' AND maven.memory_visible(r.id)",
                    {'project': project_id}, tx)
    if raw is None:
        raise UninstallError('selected cloud record is not an accessible owner project')
    result = json.loads(raw)
    result['lifecycle'] = None
    if lifecycle:
        raw = db.scalar("SELECT jsonb_build_object('archived',archived,'generation',generation,"
                        "'action_key',action_key)::text FROM maven.project_lifecycle "
                        "WHERE project_id=CAST(:project AS uuid)", {'project': project_id}, tx)
        result['lifecycle'] = json.loads(raw) if raw else None
    return result


def _binding(root):
    from tools.project import _validate, load_project
    root = checked_path(root)
    if (root / '.maven/project.json').exists():
        return load_project(root)
    paths = sorted((root / '.maven').glob('detached-project-*.json'))
    if len(paths) != 1:
        raise UninstallError('exactly one preserved private project binding is required')
    path = paths[0]
    if path.name != 'detached-project-' + file_digest(path) + '.json':
        raise UninstallError('preserved project binding digest changed')
    return _validate(read_private(path), root)


def _fragment(action, *, warnings=(), retained=(), requires=()):
    return {'actions': [action], 'retained': list(retained), 'warnings': list(warnings),
            'targets': [{'kind': action['kind'], 'target': action['target']}],
            'requires': list(requires)}


def plan_project(root, home, *, archive=False, reactivate=False):
    if archive and reactivate:
        raise UninstallError('archive and reactivate are separate choices')
    project = _binding(root)
    db, s3, details = _connection(home)
    with closing(db.client), closing(s3):
        from tools.memory.store import fetch_control
        control, _ = fetch_control(s3, details['config'], allow_maintenance=True)
        if control['state'] != 'ready':
            raise UninstallError('project lifecycle requires a ready, non-retired instance')
        with db.transaction() as tx:
            state = _state(db, details['config'], tx)
            if state['state'] != 'ready':
                raise UninstallError('database is not ready')
            schema = _schema(db, tx, project=archive or reactivate)
            selected = _project(db, tx, project['project_id'], lifecycle=PROJECT_MIGRATION in schema)
            work = _work(db, tx, project['project_id'])
            if work['records']:
                raise UninstallError('project has active sessions or unresolved work; close/recover it normally first')
    kind = 'project-reactivate' if reactivate else 'project-archive' if archive else 'project-verify'
    details.update(project_id=project['project_id'], root=project['root'], project=selected,
                   migrations=schema, work=work, binding_sha256=digest(project))
    action = {'handler': 'lifecycle', 'kind': kind, 'target': project['project_id'],
              'stage': 10, 'details': details}
    return _fragment(action, retained=[{'kind': 'cloud-project-history', 'target': project['project_id']}],
                     warnings=['Reactivation restores cloud write eligibility only; local attachment remains an explicit choice.']
                     if reactivate else [], requires=['writers-quiescent'])


def plan_cloud(home, *, abandon=False):
    db, s3, details = _connection(home)
    with closing(db.client), closing(s3):
        from tools.memory.store import fetch_control
        control, etag = fetch_control(s3, details['config'], allow_maintenance=True)
        if control['state'] != 'ready' and not control.get('uninstall'):
            raise UninstallError('unrelated maintenance is active; preserve it and resolve it explicitly')
        if not etag:
            raise UninstallError('control object has no conditional-write identity')
        with db.transaction() as tx:
            state = _state(db, details['config'], tx)
            if state['state'] != 'ready' and not control.get('uninstall'):
                raise UninstallError('database has unrelated maintenance')
            schema = _schema(db, tx)
            work = _work(db, tx)
    if work['records'] and not abandon:
        raise UninstallError('active cloud sessions or unresolved work remain; close normally or explicitly approve abandonment')
    details.update(migrations=schema, work=work, abandon=bool(abandon),
                   control=control, control_etag=etag)
    details['barrier_token'] = control.get('uninstall') or digest(details)
    action = {'handler': 'lifecycle', 'kind': 'cloud-freeze', 'target': details['instance_id'],
              'stage': 20, 'details': details}
    warnings = ['Cloud maintenance blocks new work across hosts; local unknown receipts remain untouched.']
    if abandon:
        warnings.append('Administrative abandonment preserves unfinished session and request evidence verbatim; '
                        'it neither closes sessions normally nor recovers unsaved work.')
    return _fragment(action, warnings=warnings,
                     retained=[{'kind': 'session-and-request-evidence', 'target': details['instance_id'],
                                'sha256': digest(work)}],
                     requires=['writers-quiescent'] + (['abandon-sessions'] if abandon else []))


def _project_execute(action, progress, save, db, s3):
    from tools.memory.store import fetch_control
    details = action['details']
    project_id = details['project_id']
    if digest(_binding(details['root'])) != details['binding_sha256']:
        raise UninstallError('local project binding changed; create a new plan')
    changing = action['kind'] != 'project-verify'
    token = digest({'kind': action['kind'], 'details': details})
    progress['intent'] = token
    save()
    control, _ = fetch_control(s3, details['config'], allow_maintenance=True)
    if control['state'] != 'ready':
        raise UninstallError('project lifecycle cannot run during cloud maintenance')
    with db.transaction() as tx:
        state = _state(db, details['config'], tx)
        if state['state'] != 'ready':
            raise UninstallError('database is not ready')
        _schema(db, tx, project=changing, expected=details['migrations'])
        # An absent lifecycle row still has a lock, preventing first-session races.
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended('project-work:' || :project,0))",
                   {'project': project_id}, tx)
        selected = _project(db, tx, project_id, lifecycle=PROJECT_MIGRATION in details['migrations'])
        previous = details['project']
        row = selected['lifecycle']
        replayed = changing and row is not None and row['action_key'] == token
        if replayed:
            wanted = action['kind'] == 'project-archive'
            if row['archived'] != wanted or row['generation'] != (
                    (previous['lifecycle'] or {}).get('generation', 0) + 1):
                raise UninstallError('lifecycle replay identity is inconsistent')
        elif selected != previous:
            raise UninstallError('project changed since planning; create a new plan')
        if not replayed:
            if _work(db, tx, project_id) != details['work']:
                raise UninstallError('project acquired unfinished work; close/recover it before proceeding')
            if changing:
                db.execute("INSERT INTO maven.project_lifecycle(project_id,archived,generation,action_key,actor_id) "
                           "VALUES(CAST(:project AS uuid),:archived,1,:key,:actor) "
                           "ON CONFLICT(project_id) DO UPDATE SET archived=EXCLUDED.archived,"
                           "generation=maven.project_lifecycle.generation+1,action_key=EXCLUDED.action_key,"
                           "actor_id=EXCLUDED.actor_id,changed_at=transaction_timestamp()",
                           {'project': project_id, 'archived': action['kind'] == 'project-archive',
                            'key': token, 'actor': details['admin_identity']['arn']}, tx)
    result = {'status': {'project-verify': 'verified', 'project-archive': 'archived',
                         'project-reactivate': 'reactivated'}[action['kind']],
              'project_id': project_id, 'history_preserved': True}
    progress['result'] = result
    save()
    return result


def _freeze(action, progress, save, db, s3):
    from tools.memory.store import control_document, fetch_control
    details = action['details']
    config = details['config']
    token = details['barrier_token']
    if progress.get('intent') not in (None, token):
        raise UninstallError('journal belongs to a different cloud barrier')
    progress['intent'] = token
    save()
    control, etag = fetch_control(s3, config, allow_maintenance=True)
    if control.get('uninstall') != token:
        if control != details['control'] or etag != details['control_etag'] or control['state'] != 'ready':
            raise UninstallError('cloud control identity changed; preserve maintenance and create a new plan')
        frozen = dict(control_document(config, 'maintenance'), uninstall=token)
        s3.put_object(Bucket=config['journal_bucket'], Key='control.json', Body=canonical(frozen),
                      ContentType='application/json', ServerSideEncryption='AES256', IfMatch=etag)
    control, _ = fetch_control(s3, config, allow_maintenance=True)
    if control.get('uninstall') != token or control['state'] != 'maintenance':
        raise UninstallError('cloud write barrier outcome is unresolved; resume this plan')
    progress['s3_barrier'] = True
    save()
    # UPDATE takes the exclusive state-row lock shared by every service transaction.
    # Its commit is the drain point, including instruction approval and activation.
    with db.transaction() as tx:
        db.execute('SELECT pg_advisory_xact_lock(1967318421)', transaction=tx)
        _state(db, config, tx, exclusive=True)
        _schema(db, tx, expected=details['migrations'])
        db.execute("UPDATE maven.service_state SET state='maintenance' WHERE singleton", transaction=tx)
    progress['database_barrier'] = True
    save()
    with db.transaction() as tx:
        state = _state(db, config, tx, exclusive=True)
        if state['state'] != 'maintenance':
            raise UninstallError('database barrier changed; resume only after explicit recovery')
        work = _work(db, tx)
    if work != details['work']:
        progress['inventory_changed'] = True
        save()
        raise UninstallError('cloud is frozen but unfinished-work evidence changed; create a new plan against this barrier')
    if work['records'] and not details['abandon']:
        raise UninstallError('cloud is frozen; clean session closure was not established')
    control, _ = fetch_control(s3, config, allow_maintenance=True)
    if control.get('uninstall') != token:
        raise UninstallError('persistent retirement marker changed; preserve all recovery evidence')
    result = {'status': 'frozen', 'instance_id': details['instance_id'],
              'recovery_epoch': details['recovery_epoch'], 'barrier_token': token,
              'administratively_abandoned': bool(details['abandon'] and work['records']),
              'unfinished_evidence_sha256': digest(work),
              'session_records_modified': False, 'unknown_receipts_preserved': True,
              'unsaved_work_recovered': False, 'normal_cloud_closure': not work['records']}
    progress['result'] = result
    save()
    return result


def execute(action, progress, save):
    if action.get('handler') != 'lifecycle' or action.get('kind') not in {
            'project-verify', 'project-archive', 'project-reactivate', 'cloud-freeze'}:
        raise UninstallError('unknown lifecycle action')
    try:
        details = action['details']
        db, s3, _ = _connection(details['home'], details)
        with closing(db.client), closing(s3):
            if action['kind'] == 'cloud-freeze':
                result = _freeze(action, progress, save, db, s3)
            else:
                result = _project_execute(action, progress, save, db, s3)
            if progress.pop('outcome_unresolved', None):
                save()
            return result
    except UninstallError:
        raise
    except Exception:
        # Provider errors and record bodies can contain sensitive material.
        progress['outcome_unresolved'] = True
        save()
        raise UninstallError('administrative lifecycle outcome unresolved; preserve journal and resume') from None
