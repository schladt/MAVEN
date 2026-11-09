"""Owner-selected project onboarding with root-scoped durable request receipts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from tools.interaction import Field, checkpoint, message, require, stage

from tools.setup_common import (
    SetupPending, approve, ask, private_directory, read_private,
    save_json, setup_lock, verify_authority,
)


CHOICES = ('create', 'project_id', 'name', 'source_origin', 'source_excerpt',
           'no_git', 'remote', 'accepted_url', 'branch', 'check')


def _selection(args, root):
    from tools.project import ProjectError, _uuid, validate_accepted_url
    selected = {key: getattr(args, key, None) for key in CHOICES}
    selected['check'] = selected['check'] or []
    if not args.non_interactive:
        approve(f'Configure exactly {root}; no commit, push, pull or branch switch will occur.', str(root))
        choices = require().form('Project selection', [
            Field('mode', 'Cloud project', 'create' if selected['create'] else 'existing', kind='select',
                  choices=(('Select an existing project', 'existing'), ('Create a new project', 'create'))),
        ], description=f'Exact project root: {root}. No commit, push, pull or branch switch.')
        if choices is None:
            raise SetupPending('Project selection paused.')
        selected['create'] = choices['mode'] == 'create'
        fields = ([Field('name', 'Truthful project name', selected['name'] or ''),
                   Field('source_origin', 'Owner approval origin', selected['source_origin'] or 'owner MAVEN setup TUI'),
                   Field('source_excerpt', 'Owner request / project purpose', selected['source_excerpt'] or '', kind='multiline')]
                  if selected['create'] else [Field('project_id', 'Existing cloud project UUID', selected['project_id'] or '')])
        fields.append(Field('checks', 'Optional project checks (one JSON argv array per line)',
                            '\n'.join(selected['check']), kind='multiline', required=False))
        details = require().form('Project identity and checks', fields)
        if details is None:
            raise SetupPending('Project identity selection paused.')
        selected['check'] = [line.strip() for line in details.pop('checks').splitlines() if line.strip()]
        selected.update(details)
        if selected['create']:
            selected['project_id'] = None
        else:
            selected.update(name=None, source_origin=None, source_excerpt=None)
    if bool(selected['create']) == bool(selected['project_id']):
        raise ProjectError('Choose --create --name NAME or --project-id UUID, not both.')
    if selected['create']:
        if not all(isinstance(selected[key], str) and selected[key].strip()
                   for key in ('name', 'source_origin', 'source_excerpt')):
            raise ProjectError('--create requires --name, --source-origin and --source-excerpt from the actual owner approval.')
        selected.update(project_id=str(uuid4()), request_key=str(uuid4()))
    else:
        _uuid(selected['project_id'])
        if any(selected[key] for key in ('name', 'source_origin', 'source_excerpt')):
            raise ProjectError('Creation provenance and name require --create.')
        selected['request_key'] = None
    destinations = [selected[key] for key in ('remote', 'accepted_url', 'branch')]
    if any(destinations) and not all(destinations):
        raise ProjectError('Optional Git setup requires all of --remote, --accepted-url and --branch.')
    if all(destinations):
        validate_accepted_url(selected['accepted_url'])
    selected['no_git'] = not any(destinations)
    selected.update(root=str(root), existing=False)
    if not args.non_interactive:
        approve('Approve this exact project selection and provenance.', selected['project_id'],
                body=json.dumps(selected, indent=2))
    return selected


def _assert_choices(args, saved):
    from tools.project import ProjectError
    for key in CHOICES:
        value = getattr(args, key, None)
        if value not in (None, False, []) and value != saved.get(key):
            raise ProjectError(f'--{key.replace("_", "-")} conflicts with the preserved project selection; no new project was created.')


def _legacy(env, root, identity, non_interactive):
    """Adopt only the original matching wizard receipt, never its global pointer."""
    path = env.parent / 'runtime' / 'setup' / 'state.json'
    if not path.exists() and not path.is_symlink():
        return None
    private_directory(path.parent)
    state = read_private(path)
    saved = state.get('project')
    if not saved:
        return None
    migrated_path = Path(saved['root']) / '.maven' / 'runtime' / 'project-setup' / 'state.json'
    migrated_complete = False
    if migrated_path.exists() or migrated_path.is_symlink():
        for parent in (migrated_path.parents[2], migrated_path.parents[1], migrated_path.parent):
            private_directory(parent)
        migrated = read_private(migrated_path)
        migrated_complete = (
            migrated.get('complete') is True and migrated.get('identity') == identity
            and migrated.get('selection', {}).get('project_id') == saved.get('project_id')
            and migrated.get('selection', {}).get('request_key') == saved.get('request_key')
        )
    if saved.get('root') != str(root):
        if not state.get('ready_project') and not migrated_complete:
            raise SetupPending(f'An interrupted instance-wizard project remains at {saved.get("root")}. Resume that exact root first; preserve {path}.')
        return None
    if state.get('instance_id') and state['instance_id'] != identity['instance_id']:
        raise SetupPending('Legacy project receipt belongs to a different instance; preserve its original settings and IDs.')
    if non_interactive:
        raise SetupPending('Legacy instance-wizard receipt requires one interactive adoption: rerun setup with this exact --root without --non-interactive. Original project/request IDs will be preserved.')
    approve(f'Adopt the original project/request receipt from {path} for instance {identity["instance_id"]}, recovery epoch {identity["recovery_epoch"]}? Confirm these are its original connection settings; never substitute another instance.', 'ADOPT ' + saved['project_id'])
    return {'selection': saved, 'submitted': saved.get('request_key') in state.get('submitted_requests', [])}


def setup_project(args):
    from tools.memory.client import MemoryClientError
    from tools.memory.environment import configured_client, load_environment, memory_config
    from tools.project import (
        ProjectError, _ensure_ignore, _initialize, _uuid, _validate, load_project, validate_git,
    )
    if not args.non_interactive:
        stage('Project selection')
    checkpoint()
    root = args.root.expanduser().resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise ProjectError('--root must select an existing project directory, never filesystem /.')
    target = root / '.maven' / 'project.json'
    # Validate private leaves before reading either a project or its pending receipt.
    if (root / '.maven').exists() or (root / '.maven').is_symlink():
        private_directory(root / '.maven')
    existing = load_project(root) if target.exists() or target.is_symlink() else None
    directory = root / '.maven' / 'runtime' / 'project-setup'
    state_path = directory / 'state.json'
    for parent in (root / '.maven' / 'runtime', directory):
        if parent.exists() or parent.is_symlink():
            private_directory(parent)
    pending = state_path.exists() or state_path.is_symlink()
    selected = None
    values = load_environment(args.env)
    if getattr(args, 'reviewed_environment', None) is not None and values != args.reviewed_environment:
        raise SetupPending('Project connection changed after owner review; review the current environment before any effects.')
    config = memory_config(values)
    identity = {key: config[key] for key in ('instance_id', 'recovery_epoch', 'endpoint', 'region')}
    legacy = None if pending else _legacy(args.env, root, identity, args.non_interactive)
    if legacy:
        selected = legacy['selection']
        _assert_choices(args, selected)
    elif not existing and not pending:
        # Every fresh choice is required before cloud access or durable request creation.
        selected = _selection(args, root)
    if existing and not pending:
        selected = {'root': str(root), 'project_id': existing['project_id'], 'existing': True,
                    'create': False, 'no_git': not existing['git_enabled'], 'check': [],
                    **{key: existing[key] for key in ('remote', 'accepted_url', 'branch')}} if not legacy else selected
        _assert_choices(args, selected)
    _ensure_ignore(root)  # Exclude all personal receipts before creating any of them.
    private_directory(root / '.maven')
    private_directory(root / '.maven' / 'runtime')
    private_directory(directory)
    with setup_lock(directory):
        fresh_receipt = not (state_path.exists() or state_path.is_symlink())
        if not fresh_receipt:
            state = read_private(state_path)
            if (state.get('schema') != 1 or state.get('root') != str(root)
                    or state.get('identity') != identity):
                raise SetupPending('Saved project setup root/instance/recovery identity differs. Restore its original connection; no request was replaced.')
            selected = state['selection']
            _assert_choices(args, selected)
        else:
            state = {'schema': 1, 'root': str(root), 'identity': identity, 'selection': selected,
                     'submitted': legacy['submitted'] if legacy else False}
        if selected.get('root') != str(root):
            raise SetupPending('Saved project selection root differs; preserve the original receipt.')
        _uuid(selected['project_id'])
        if selected.get('create'):
            _uuid(selected['request_key'])
        if existing:
            if existing['project_id'] != selected['project_id']:
                raise SetupPending('Local project differs from the preserved setup receipt; neither was overwritten.')
            validate_git(root, existing)
        else:
            # Validate local choices before any resumed mutation or lookup.
            project = _validate({'schema': 1, 'root': str(root), 'project_id': selected['project_id'],
                                 'git_enabled': not selected['no_git'],
                                 'checks': [json.loads(value) for value in selected.get('check', [])],
                                 **{key: selected.get(key) for key in ('remote', 'accepted_url', 'branch')}}, root)
            validate_git(root, project)
        if fresh_receipt:
            save_json(state_path, state)
        if not args.non_interactive:
            stage('Project binding')
        checkpoint()
        verify_authority(args.env, values=values)
        if not args.non_interactive:
            approve('Preserve/use this project. Resumed creation uses only the original request.',
                    selected['project_id'], body=json.dumps({'identity': identity, 'selection': selected}, indent=2))
        if not existing:
            if selected.get('create'):
                client = configured_client(args.env, values=values)
                try:
                    if state['submitted']:
                        try:
                            client.invoke('/v1/operations/lookup', {'request_key': selected['request_key']})
                        except MemoryClientError as exc:
                            if exc.code != 'operation_unavailable':
                                raise
                    else:
                        state['submitted'] = True
                        save_json(state_path, state)
                finally:
                    client.close()
            initialize = {key: value for key, value in selected.items() if key != 'existing'}
            initialize.update(root=root, env=args.env, values=values)
            _initialize(argparse.Namespace(**initialize))
        project = load_project(root)
        if project['project_id'] != selected['project_id'] or (existing and project != existing):
            raise SetupPending('Project configuration changed during setup; preserve the receipt.')
        generation = verify_authority(args.env, project['project_id'], values=values)
        state['complete'] = True
        save_json(state_path, state)
        checkpoint()
        return {'status': 'ready', 'project': project, 'authority_generation': generation,
                'verification': f'maven-project show --root {root}',
                'next': 'Initialize MAVEN in this enabled project through your harness or maven-start-work.'}


def interactive_project(args):
    """Run under the presenter's existing worker (including the instance wizard)."""
    from tools.memory.client import MemoryClientError
    try:
        return setup_project(args)
    except MemoryClientError as exc:
        message('Project setup pending: ' + exc.code)
        if exc.request_key:
            message('Preserved request key: ' + exc.request_key)
        message(f'Preserve {args.root}/.maven/runtime/project-setup and resume this exact root; never replace pending IDs.')
        raise SetupPending('Project readiness was not declared.') from None
