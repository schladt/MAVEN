"""Local memory commands; offline preparation never contacts AWS."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .client import MemoryClient, MemoryClientError, read_json, selected_session
from .environment import DEFAULT_ENV, load_environment, memory_config, migration_config

def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description='MAVEN cloud memory; infrastructure changes and owner-data imports require scoped approval.')
    command.add_argument('--env', type=Path, default=DEFAULT_ENV)
    command.add_argument('--format', choices=('json', 'agent'), default='json')
    actions = command.add_subparsers(dest='action', required=True)
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
    skills.add_parser('list', help='List reviewed cloud authority bundles, not project memory skills')
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


def _connection(args, *, upgrade=False):
    values = load_environment(args.env)
    return values, migration_config(values) if upgrade else memory_config(values)


def _scope(args):
    body = {key: getattr(args, key) for key in ('project_id', 'workflow', 'operation')
            if getattr(args, key, None) is not None}
    return body




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
    from .skills import prepare_bundle, register_bundle
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
        return _skill_catalog(client)
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == 'build':
            from .deployment import build
            result = build(args.output, notification_email=args.notification_email, engine_version=args.engine_version,
                           instance_name=args.instance_name, account_id=args.account_id,
                           caller_principal_arn=args.caller_principal_arn, instance_id=args.instance_id,
                           recovery_epoch=args.recovery_epoch, availability_zones=args.availability_zones)
        elif args.action == 'install':
            result = _install(args)
        elif args.action == 'skill':
            result = _skill_action(args)
        elif args.action in ('bootstrap', 'migrate'):
            from .admin import bootstrap, migrate
            values, config = _connection(args, upgrade=args.action == 'migrate')
            if args.confirm_instance != config['instance_id']:
                raise MemoryClientError('instance_confirmation_mismatch')
            operation = bootstrap if args.action == 'bootstrap' else migrate
            result = operation(config, selected_session(config, admin=True, environment=values))
        else:
            from .validation import (validate_query, validate_lookup,
                                     validate_create, validate_challenge, validate_resolve,
                                     validate_index, validate_read)
            approval = args.action == 'lookup' and args.approval
            offline = False
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
                from .validation import digest
                result.update(proposal_id=body['proposal_id'], candidate_hash=digest(body['candidate']))
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
        if exc.detail is not None:
            result['detail'] = exc.detail
        from .client import AUTHENTICATION_CODES
        if exc.code in AUTHENTICATION_CODES:
            from .environment import authentication_handoff
            try:
                handoff = authentication_handoff(exc, load_environment(args.env))
                if handoff:
                    result['authentication'] = handoff
            except (MemoryClientError, OSError, ValueError):
                pass
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
