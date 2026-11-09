#!/usr/bin/env python3
"""Memory-native lifecycle and maintenance; Git publication is explicitly maven-git."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from tools.memory.boundary import BoundaryError
from tools.project import default_environment


class ArgumentError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message)


def parser(action=None):
    value = Parser(description=__doc__)
    if action is None:
        commands = value.add_subparsers(dest='action', required=True, parser_class=Parser)
        parsers = [(name, commands.add_parser(name)) for name in (
            'start', 'context', 'status', 'search', 'read', 'save', 'claim', 'checkpoint',
            'stop-work', 'pause', 'resume', 'recover', 'maintenance')]
    else:
        value.set_defaults(action=action)
        parsers = [(action, value)]
    for name, command in parsers:
        command.add_argument('--root', type=Path, required=True)
        command.add_argument('--env', type=Path, default=default_environment())
        command.add_argument('--session', required=name != 'start', metavar='SESSION_UUID')
        command.add_argument('--json', action='store_true')
        if name == 'start':
            command.add_argument('--resume', action='store_true', help='Resume only the explicit --session identity')
            command.add_argument('--harness')
            command.add_argument('--harness-session')
        if name in ('search', 'read'):
            command.add_argument('--scope', choices=('context', 'project', 'universal', 'all'), default='context')
            command.add_argument('--project-id')
        if name == 'search':
            command.add_argument('--query', default='')
            command.add_argument('--kind')
            command.add_argument('--after-id')
            command.add_argument('--limit', type=int, default=20)
        if name == 'read':
            command.add_argument('--record-id', required=True)
            command.add_argument('--revision', type=int)
        if name in ('save', 'claim', 'maintenance'):
            command.add_argument('--input', type=Path, required=name != 'maintenance', help='Owner-private JSON operation arguments')
        if name == 'maintenance':
            command.add_argument('--operation', choices=('enqueue', 'claim', 'apply', 'revise', 'complete',
                                                        'release', 'recover', 'advance_checkpoint', 'status', 'summary'), required=True)
        if name == 'checkpoint':
            command.add_argument('--summary', required=True)
            command.add_argument('--memory-ref', action='append', default=[], metavar='UUID:REVISION')
            command.add_argument('--checkpoint-id')
            command.add_argument('--wait', action='store_true')
            command.add_argument('--wait-seconds', type=int, default=20)
        if name == 'stop-work':
            command.add_argument('--checkpoint-id', required=True)
        if name == 'pause':
            command.add_argument('--anchor')
        if name == 'resume':
            command.add_argument('--fresh', action='store_true')
            command.add_argument('--input', type=Path, help='Private JSON with available discussion, source and context_complete')
        if name == 'recover':
            command.add_argument('--checkpoint-id')
            command.add_argument('--worker-stopped', action='store_true')
    return value


def parse_refs(values):
    from tools.coordination import session_uuid
    refs = []
    for value in values:
        identifier, separator, revision = value.rpartition(':')
        if not separator or not revision.isdigit() or int(revision) < 1:
            raise ArgumentError('memory references require UUID:positive_revision')
        refs.append({'record_id': session_uuid(identifier), 'revision': int(revision)})
    return refs


def _input(path):
    from tools.memory.client import read_json
    return read_json(path, maximum=196608)


def _main(argv=None, *, action=None):
    from tools.memory.client import MemoryClientError
    from tools.memory.lifecycle import SessionService
    from tools.memory.maintenance import Maintenance
    from tools.memory.validation import MemoryError
    argv = list(sys.argv[1:] if argv is None else argv)
    service = None
    report = {'status': 'blocked', 'action': action or 'workflow'}
    try:
        args = parser(action).parse_args(argv)
        if args.action == 'start' and args.resume and not args.session:
            raise ArgumentError('--resume requires the exact --session UUID')
        identifier = args.session or str(uuid.uuid4())
        report.update(action=args.action, session_id=identifier, root=str(args.root))
        service = SessionService(args.root, identifier, env=args.env,
                                 harness=getattr(args, 'harness', None),
                                 harness_session=getattr(args, 'harness_session', None))
        maintenance = Maintenance(service)
        if args.action == 'start':
            result = service.initialize(resume=args.resume)
        elif args.action == 'context':
            result = service.context()
        elif args.action == 'status':
            result = dict(service.status(), maintenance=maintenance.status())
        elif args.action == 'search':
            result = service.search(query=args.query, kind=args.kind, scope=args.scope,
                                    project_id=args.project_id, after_id=args.after_id, limit=args.limit)
        elif args.action == 'read':
            result = service.read_record(args.record_id, revision=args.revision, scope=args.scope, project_id=args.project_id)
        elif args.action == 'save':
            result = maintenance.save(**_input(args.input))
        elif args.action == 'claim':
            result = maintenance.claim_task(**_input(args.input))
        elif args.action == 'checkpoint':
            result = maintenance.checkpoint(summary=args.summary, refs=parse_refs(args.memory_ref),
                                            checkpoint_id=args.checkpoint_id, wait=args.wait, wait_seconds=args.wait_seconds)
        elif args.action == 'stop-work':
            result = maintenance.finish(args.checkpoint_id)
        elif args.action == 'pause':
            result = maintenance.pause(anchor=args.anchor)
        elif args.action == 'resume':
            options = _input(args.input) if args.input else {}
            if set(options) - {'discussion', 'source', 'context_complete'}:
                raise ArgumentError('resume input accepts only discussion, source and context_complete')
            result = maintenance.resume(fresh=args.fresh, **options)
        elif args.action == 'maintenance':
            result = getattr(maintenance, args.operation)(**(_input(args.input) if args.input else {}))
        else:
            if args.checkpoint_id:
                if not args.worker_stopped:
                    raise ArgumentError('checkpoint recovery requires --worker-stopped attestation')
                result = maintenance.recover_checkpoint(args.checkpoint_id, worker_stopped=True)
            else:
                result = service.recover()
        report.update(status='ready', **result) if 'status' not in result else report.update(result)
    except (BoundaryError, MemoryClientError, MemoryError, ValueError, OSError, TypeError, KeyError) as error:
        report['status'] = 'blocked'
        report['error'] = {'code': getattr(error, 'code', getattr(error, 'status', type(error).__name__)),
                           'message': str(error) if isinstance(error, (BoundaryError, ArgumentError, MemoryClientError))
                           else 'Invalid operation or unavailable local selection; no success was reported.'}
        request_key = getattr(error, 'request_key', None)
        if request_key:
            report['error']['request_key'] = request_key
        if service is not None:
            try:
                local = service.status()
                report['session_status'] = local
                if local['pending_request']:
                    report['status'] = 'uncertain'
            except (BoundaryError, OSError):
                pass
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:
                report.update(status='blocked', error={'code': 'client_close_failed',
                                                      'message': 'Client cleanup failed; preserved receipts remain available.'})
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report['status'] in ('ready', 'bound', 'closed', 'checkpointed', 'recovered',
                                    'completed', 'paused', 'saved', 'applied', 'claimed', 'superseded', 'queued', 'pending') else 2


def start_main(argv=None):
    return _main(argv, action='start')


def checkpoint_main(argv=None):
    return _main(argv, action='checkpoint')


def stop_main(argv=None):
    return _main(argv, action='stop-work')


def main(argv=None):
    return _main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
