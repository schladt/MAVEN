"""Local memory commands; offline preparation never contacts AWS."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .client import MemoryClient, MemoryClientError, read_json, selected_session
from .environment import DEFAULT_ENV, load_environment, memory_config

def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description='MAVEN cloud memory; infrastructure changes and owner-data imports require scoped approval.')
    command.add_argument('--env', type=Path, default=DEFAULT_ENV)
    command.add_argument('--format', choices=('json', 'agent'), default='json')
    actions = command.add_subparsers(dest='action', required=True)
    starter = actions.add_parser('starter', help='Prepare generic new-owner authority offline; never submit or activate')
    starters = starter.add_subparsers(dest='starter_action', required=True)
    starter_prepare = starters.add_parser('prepare')
    starter_prepare.add_argument('--output', type=Path, required=True)
    starter_prepare.add_argument('--source-origin', required=True)
    starter_prepare.add_argument('--source-excerpt', required=True)
    starter_approval = starters.add_parser('approval')
    starter_approval.add_argument('--draft', type=Path, required=True)
    starter_approval.add_argument('--challenge', type=Path, required=True)
    starter_approval.add_argument('--output', type=Path, required=True)
    starter_approval.add_argument('--approval-origin', required=True)
    starter_approval.add_argument('--approval-excerpt', required=True)
    build = actions.add_parser('build', help='Generate a deployable template and review manifest offline; never deploy')
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--notification-email', required=True)
    build.add_argument('--engine-version', default='18.6')
    build.add_argument('--instance-name', default='maven-memory')
    build.add_argument('--account-id')
    build.add_argument('--caller-principal-arn')
    build.add_argument('--instance-id')
    build.add_argument('--recovery-epoch')
    build.add_argument('--availability-zones', nargs=2, metavar=('AZ_A', 'AZ_B'))
    validate = actions.add_parser('validate', help='Validate a fact change without login or network access')
    validate.add_argument('input', type=Path)
    change = actions.add_parser('change', help='Submit an explicitly authorized synthetic fact change')
    change.add_argument('input', type=Path)
    query = actions.add_parser('query', help='Read one exact fact or historical revision')
    query.add_argument('--record-id', required=True)
    query.add_argument('--revision', type=int)
    lookup = actions.add_parser('lookup', help='Reconcile one exact stable request key')
    lookup.add_argument('--request-key', required=True)
    lookup.add_argument('--approval', action='store_true', help='Use the separate approval role to reconcile activation')
    bootstrap = actions.add_parser('bootstrap', help='Initialize an approved memory deployment using its separate admin role')
    bootstrap.add_argument('--confirm-instance', required=True)
    migrate = actions.add_parser('migrate', help='Apply reviewed migrations to the confirmed existing instance using its admin role')
    migrate.add_argument('--confirm-instance', required=True)
    instruction = actions.add_parser('instruction', help='Versioned instruction drafts and explicitly approved immutable publications')
    instructions = instruction.add_subparsers(dest='instruction_action', required=True)
    draft = instructions.add_parser('draft', help='Create an immutable proposal, not operative authority')
    draft.add_argument('input', type=Path)
    draft.add_argument('--validate', action='store_true', help='Validate offline without configuration or login')
    index = instructions.add_parser('index', help='Discover active published instructions; not a complete authority snapshot')
    index.add_argument('--page-kind', choices=('directive', 'skill', 'procedure'))
    index.add_argument('--after-slug')
    index.add_argument('--limit', type=int)
    read = instructions.add_parser('read', help='Retrieve and verify a complete instruction body')
    selector = read.add_mutually_exclusive_group(required=True)
    selector.add_argument('--slug')
    selector.add_argument('--proposal-id')
    read.add_argument('--revision', type=int, help='Historical nonoperative revision, only with --slug')
    load = instructions.add_parser('load', help='Load one verified current active skill; does not execute or resolve global authority')
    load.add_argument('--slug', required=True)
    load.add_argument('--synthetic', action='store_true', help='Test only: require synthetic rather than owner scope')
    load.add_argument('--project-id')
    load.add_argument('--workflow')
    load.add_argument('--operation')
    challenge = instructions.add_parser('challenge', help='Explicit owner action using the separate approval role')
    challenge.add_argument('--proposal-id', required=True)
    activate = instructions.add_parser('activate', help='Approve the exact reviewed challenge/candidate using the separate approval role')
    activate.add_argument('input', type=Path)
    activate.add_argument('--confirm-candidate', required=True)
    skill = actions.add_parser('skill', help='Prepare, register and retrieve approved cloud skill resources')
    skills = skill.add_subparsers(dest='skill_action', required=True)
    prepare_skill = skills.add_parser('prepare', help='Create and verify an exact bundle offline; never execute')
    prepare_skill.add_argument('--source', type=Path, required=True)
    prepare_skill.add_argument('--manifest', type=Path, required=True)
    prepare_skill.add_argument('--output', type=Path, required=True)
    register_skill = skills.add_parser('register', help='Upload an exact bundle and submit the complete existing draft; never approve')
    register_skill.add_argument('input', type=Path)
    register_skill.add_argument('--bundle', type=Path, required=True)
    list_skills = skills.add_parser('list', help='List the approved skill catalog')
    list_skills.add_argument('--refresh', action='store_true', help='Explicitly refresh cached catalog metadata')
    fetch_skill = skills.add_parser('fetch', help='Retrieve a verified resource ZIP without installing or executing it')
    fetch_skill.add_argument('--slug', required=True)
    fetch_skill.add_argument('--refresh', action='store_true', help='Explicitly refresh catalog metadata and re-download')
    list_skills.add_argument('--root', type=Path)
    list_skills.add_argument('--session')
    fetch_skill.add_argument('--root', type=Path, required=True)
    fetch_skill.add_argument('--session', required=True)
    context = actions.add_parser('context', help='Load complete applicable cloud authority and exact dependencies')
    for key in ('project-id', 'workflow', 'operation'):
        context.add_argument('--' + key)
    record = actions.add_parser('record', help='Sourced versioned owner memory; notes are never instructions')
    records = record.add_subparsers(dest='record_action', required=True)
    put = records.add_parser('put')
    put.add_argument('input', type=Path)
    put.add_argument('--root', type=Path)
    put.add_argument('--session')
    get = records.add_parser('get')
    get.add_argument('--record-id', required=True)
    get.add_argument('--revision', type=int)
    listing = records.add_parser('list')
    for key in ('kind', 'project-id', 'query', 'tag', 'subject-id', 'state', 'after-id'):
        listing.add_argument('--' + key)
    listing.add_argument('--limit', type=int)
    claim = actions.add_parser('claim', help='Claim/release/complete a task using an explicit fenced request')
    claim.add_argument('input', type=Path)
    claim.add_argument('--root', type=Path, required=True)
    claim.add_argument('--session', required=True)
    session = actions.add_parser('session', help='Record an unscoped session; project lifecycle uses maven-start-work and maven-stop-work')
    session.add_argument('state', choices=('active', 'closed', 'abandoned'))
    session.add_argument('--record-id', required=True)
    session.add_argument('--request-key', required=True)
    session.add_argument('--expected-revision', type=int, required=True)
    session.add_argument('--summary', required=True)
    session.add_argument('--source-origin', required=True)
    session.add_argument('--source-excerpt', default='')
    session.add_argument('--change-reason', required=True)
    for key in ('project-id', 'workflow', 'operation'):
        session.add_argument('--' + key)
    install = actions.add_parser('install', help='Explicit owner-selected build, review, exact deployment and initialization')
    steps = install.add_subparsers(dest='install_action', required=True)
    prepare = steps.add_parser('prepare', help='Build private review artifacts offline')
    prepare.add_argument('--output', type=Path, required=True)
    plan = steps.add_parser('plan', help='Create a change set only; never execute')
    plan.add_argument('--package', type=Path, required=True)
    plan.add_argument('--review', type=Path, required=True)
    plan.add_argument('--type', choices=('CREATE', 'UPDATE'), required=True)
    plan.add_argument('--budget', type=float, default=300)
    apply = steps.add_parser('apply', help='Execute only the exact reviewed change set and connect')
    apply.add_argument('--package', type=Path, required=True)
    apply.add_argument('--review', type=Path, required=True)
    apply.add_argument('--confirm-review', required=True)
    apply.add_argument('--budget', type=float, default=1800)
    for name in ('connect', 'init', 'migrate'):
        step = steps.add_parser(name)
        step.add_argument('--confirm-instance', required=True)
    return command


def _connection(args):
    values = load_environment(args.env)
    return values, memory_config(values)


def _scope(args):
    body = {key: getattr(args, key) for key in ('project_id', 'workflow', 'operation')
            if getattr(args, key, None) is not None}
    return body


def _memory_action(args):
    values, config = _connection(args)
    client = MemoryClient(config, selected_session(config, environment=values))
    try:
        if args.action == 'context':
            return client.load_context(_scope(args))
        if args.action == 'record':
            if args.record_action == 'put':
                body = read_json(args.input, maximum=196608)
                route = '/v1/memory/write'
            elif args.record_action == 'get':
                body = {'record_id': args.record_id}
                if args.revision is not None:
                    body['revision'] = args.revision
                route = '/v1/memory/read'
            else:
                body = {key: getattr(args, key) for key in
                        ('kind', 'project_id', 'query', 'tag', 'subject_id', 'state', 'after_id', 'limit')
                        if getattr(args, key) is not None}
                route = '/v1/memory/search'
        elif args.action == 'claim':
            body = read_json(args.input, maximum=16384)
            route = '/v1/memory/claim'
        else:
            scope = _scope(args)
            body = {'request_key': args.request_key, 'record_id': args.record_id,
                    'expected_revision': args.expected_revision, 'kind': 'session',
                    'classification': 'Sensitive', 'data_scope': 'owner',
                    'data': {'state': args.state, 'summary': args.summary,
                             **{key: value for key, value in scope.items() if key != 'project_id'}},
                    'source': {'origin': args.source_origin, 'excerpt': args.source_excerpt},
                    'change_reason': args.change_reason}
            if 'project_id' in scope:
                body['project_id'] = scope['project_id']
            route = '/v1/memory/write'
        if route in ('/v1/memory/write', '/v1/memory/claim'):
            from .validation import validate
            validate(route, body)
            root = getattr(args, 'root', None)
            session_id = getattr(args, 'session', None)
            project_bound = 'project_id' in body or route == '/v1/memory/claim'
            if project_bound or root is not None or session_id is not None:
                if body.get('kind') in ('session', 'checkpoint'):
                    raise MemoryClientError('lifecycle_command_required')
                if root is None or session_id is None:
                    raise MemoryClientError('explicit_project_and_session_required')
                from tools.project import load_project
                from tools.coordination import SessionRegistry, session_lock
                from .boundary import MemoryBoundary
                project = load_project(root)
                project_id = project['project_id']
                if route == '/v1/memory/write' and body.get('project_id') != project_id:
                    raise MemoryClientError('project_scope_mismatch')
                if route == '/v1/memory/claim' and body['session_id'] != session_id:
                    raise MemoryClientError('session_scope_mismatch')
                if body.get('kind') == 'operation' and body['data']['session_id'] != session_id:
                    raise MemoryClientError('session_scope_mismatch')
                with session_lock(Path(project['root']), session_id), \
                        SessionRegistry().writer(Path(project['root']), session_id):
                    boundary = MemoryBoundary(client, Path(project['root']), project_id, session_id)
                    boundary.session()
                    return boundary.submit(route, body)
        return client.invoke(route, body)
    finally:
        client.close()


def _install(args):
    from . import bootstrap
    values = load_environment(args.env)
    if args.install_action == 'prepare':
        bootstrap.prepare(values, args.output)
        return {'status': 'prepared', 'path': str(args.output)}
    if args.install_action == 'plan':
        return bootstrap.plan(values, args.package, args.review, change_type=args.type, budget=args.budget)
    if args.install_action == 'apply':
        return bootstrap.apply(values, args.package, args.review, args.env,
                               confirm_review=args.confirm_review, budget=args.budget)
    if args.install_action == 'connect':
        return bootstrap.connect(values, args.env, confirm_instance=args.confirm_instance)
    return bootstrap.initialize(values, confirm_instance=args.confirm_instance,
                                upgrade=args.install_action == 'migrate')


def _skill_catalog(client):
    pages = []
    after = None
    generation = None
    while True:
        request = {'page_kind': 'skill'}
        if after is not None:
            request['after_slug'] = after
        response = client.invoke('/v1/pages/index', request)
        data = response.get('data')
        if (response.get('status') != 'ready' or not isinstance(data, dict)
                or not isinstance(data.get('pages'), list)
                or type(data.get('authority_generation')) is not int):
            raise MemoryClientError('invalid_service_response')
        if generation is None:
            generation = data['authority_generation']
        elif generation != data['authority_generation']:
            raise MemoryClientError('context_stale')
        for page in data['pages']:
            if (not isinstance(page, dict) or not isinstance(page.get('slug'), str)
                    or page.get('page_kind') != 'skill'
                    or (pages and page['slug'] <= pages[-1]['slug'])):
                raise MemoryClientError('invalid_service_response')
            pages.append(page)
        next_after = data.get('next_after_slug')
        if next_after is None:
            return {'status': 'ready', 'data': {'pages': pages, 'authority_generation': generation}}
        if not data['pages'] or next_after != pages[-1]['slug'] or next_after == after:
            raise MemoryClientError('invalid_service_response')
        after = next_after


def _skill_action(args):
    from .skills import SessionSkills, prepare_bundle, register_bundle
    if args.skill_action == 'prepare':
        return prepare_bundle(args.source, read_json(args.manifest, maximum=1024 * 1024), args.output)
    # Keep the complete original draft; registration owns validation and never
    # invents UUIDs, source evidence, revisions or approval.
    request = read_json(args.input, maximum=1024 * 1024) if args.skill_action == 'register' else None
    values, config = _connection(args)
    client = MemoryClient(config, selected_session(config, environment=values))
    try:
        if args.skill_action == 'register':
            return register_bundle(client, request, args.bundle)
        from .boundary import BoundaryError, MemoryBoundary
        from tools.project import load_project
        if args.skill_action == 'list' and args.root is None and args.session is None:
            return _skill_catalog(client)
        if args.root is None or args.session is None:
            raise MemoryClientError('explicit_project_and_session_required')
        project = load_project(args.root)
        root, project_id = Path(project['root']), project['project_id']
        try:
            boundary = MemoryBoundary(client, root, project_id, args.session)
            session = boundary.session()
        except BoundaryError:
            raise MemoryClientError('active_session_required') from None
        skills = SessionSkills(client, root, project_id, session['record_id'])
        if args.skill_action == 'list':
            return skills.discover(refresh=args.refresh)
        return skills.fetch(args.slug, refresh=args.refresh)
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == 'starter':
            from .onboarding import prepare, approval
            if args.starter_action == 'prepare':
                result = prepare(args.output, source_origin=args.source_origin,
                                 source_excerpt=args.source_excerpt)
            else:
                result = approval(args.draft, args.challenge, args.output,
                                  approval_origin=args.approval_origin,
                                  approval_excerpt=args.approval_excerpt)
        elif args.action == 'build':
            from .deployment import build
            result = build(args.output, notification_email=args.notification_email, engine_version=args.engine_version,
                           instance_name=args.instance_name, account_id=args.account_id,
                           caller_principal_arn=args.caller_principal_arn, instance_id=args.instance_id,
                           recovery_epoch=args.recovery_epoch, availability_zones=args.availability_zones)
        elif args.action == 'install':
            result = _install(args)
        elif args.action == 'skill':
            result = _skill_action(args)
        elif args.action in ('record', 'context', 'session', 'claim'):
            result = _memory_action(args)
        elif args.action in ('bootstrap', 'migrate'):
            from .admin import bootstrap, migrate
            values, config = _connection(args)
            if args.confirm_instance != config['instance_id']:
                raise MemoryClientError('instance_confirmation_mismatch')
            operation = bootstrap if args.action == 'bootstrap' else migrate
            result = operation(config, selected_session(config, admin=True, environment=values))
        else:
            from .validation import (validate_change, validate_query, validate_lookup,
                                     validate_create, validate_challenge, validate_resolve,
                                     validate_index, validate_read)
            approval = args.action == 'lookup' and args.approval
            offline = args.action == 'validate'
            full_read = False
            load_instruction = False
            if args.action == 'instruction':
                action = args.instruction_action
                if action == 'draft':
                    body = validate_create(read_json(args.input, maximum=1024 * 1024))
                    route = '/v1/proposals/create'
                    offline = args.validate
                elif action == 'activate':
                    body = validate_resolve(read_json(args.input, maximum=8192))
                    if args.confirm_candidate != body['candidate_hash']:
                        raise MemoryClientError('candidate_confirmation_mismatch')
                    route = '/v1/proposals/resolve'
                    approval = True
                elif action == 'challenge':
                    body = validate_challenge({'proposal_id': args.proposal_id})
                    route = '/v1/proposals/challenge'
                    approval = True
                elif action == 'index':
                    body = validate_index({key: getattr(args, key) for key in ('page_kind', 'after_slug', 'limit')
                                           if getattr(args, key) is not None})
                    route = '/v1/pages/index'
                elif action == 'load':
                    body = validate_read({'slug': args.slug})
                    load_instruction = True
                else:
                    body = validate_read({key: getattr(args, key) for key in ('slug', 'proposal_id', 'revision')
                                          if getattr(args, key) is not None})
                    route = '/v1/pages/read'
                    full_read = True
            elif args.action in ('validate', 'change'):
                body = validate_change(read_json(args.input, maximum=65536))
                route = '/v1/records/change'
            elif args.action == 'query':
                body = {'record_id': args.record_id}
                if args.revision is not None:
                    body['revision'] = args.revision
                body = validate_query(body)
                route = '/v1/records/query'
            else:
                body = validate_lookup({'request_key': args.request_key})
                route = '/v1/operations/lookup'
            if offline:
                result = {'status': 'valid', 'request_key': body['request_key']}
                if args.action == 'instruction':
                    from .validation import digest
                    result.update(proposal_id=body['proposal_id'], candidate_hash=digest(body['candidate']))
                else:
                    result['action'] = body['action']
            else:
                values, config = _connection(args)
                client = MemoryClient(config, selected_session(config, approval=approval, environment=values))
                try:
                    if load_instruction:
                        result = client.load_instruction(body['slug'], synthetic=args.synthetic,
                                                         context=_scope(args))
                    elif args.action == 'instruction' and args.instruction_action == 'activate':
                        reader = MemoryClient(config, selected_session(config, environment=values))
                        try:
                            candidate = reader.read_full({'proposal_id': body['proposal_id']})['data']
                        finally:
                            reader.close()
                        if candidate.get('content_hash') != body['candidate_hash']:
                            raise MemoryClientError('candidate_confirmation_mismatch')
                        if 'bundle' in candidate:
                            from .skills import verify_cloud_bundle
                            verify_cloud_bundle(client, candidate)
                        result = client.invoke(route, body)
                    else:
                        result = client.read_full(body) if full_read else client.invoke(route, body)
                finally:
                    client.close()
        if args.format == 'agent':
            from .display import agent_display
            print(agent_display(result))
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except MemoryClientError as exc:
        result = {'status': exc.code}
        if exc.request_key:
            result['request_key'] = exc.request_key
        print(json.dumps(result))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({'status': 'interrupted', 'note': 'For a submitted change, reconcile its original request key before retrying.'}))
        return 130
    except Exception as exc:
        # Only our domain exception fields are eligible for model-visible output.
        # Never stringify SDK/provider/SQL/file errors or echo untrusted field values.
        from .validation import MemoryError as DomainError
        if isinstance(exc, DomainError):
            result = {'status': exc.status, **exc.metadata}
        else:
            result = {'status': 'operation_failed'}
        print(json.dumps(result))
        return 2


if __name__ == '__main__':
    sys.exit(main())
