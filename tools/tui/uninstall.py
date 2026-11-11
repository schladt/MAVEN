"""Textual uninstall workflow backed by immutable plans and a pinned subprocess."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import select
import shlex
import subprocess

from tools.interaction import Field, OperationPaused
from tools.uninstall import cli
from tools.uninstall.common import UninstallError


STEPS = ['Dashboard', 'Scopes', 'Projects', 'Machine', 'Cloud', 'Recovery',
         'Inventory', 'Review', 'Approvals', 'Execution', 'Result']
HARNESS_WARNING = (
    'Removal does not require closing assistant harnesses. Running sessions keep their loaded '
    'configuration until restarted and lose MAVEN tools once its runtime is removed.\n\n'
    'A harness that saves its own configuration file while running can write the removed MAVEN '
    'entry back. After closing each harness, reopen it and confirm MAVEN is no longer listed.')
NO_ARCHIVE_WARNING = (
    'No archive directory was given. Machine removal permanently deletes each selected installation '
    'folder, including its private settings (.env), saved AWS sign-in caches, unfinished setup state '
    'and session records. A folder is kept only when it cannot be proven safe to delete; the review '
    'lists any exceptions.\n\n'
    'Go Back to enter an archive directory and keep a copy of the settings and records.')


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)


def _boolean(name, label, default=False, *, section='', help=''):
    return Field(name, label, default, kind='bool', required=False, section=section, help=help)


def _optional(name, label, default='', *, multiline=False):
    return Field(name, label, default, kind='multiline' if multiline else 'text', required=False)


def _lines(value):
    return [line.strip() for line in value.splitlines() if line.strip()]


def _wizard(ui):
    # All backend flags have a field. Namespace defaults come from the same CLI
    # parser, but user-entered text never passes through argparse/SystemExit.
    # Scope-specific pages appear only when their Scopes checkbox is selected.
    # The Machine page always appears: its home also defaults project/cloud work.
    pages = [
        ('Scopes', None, [
            _boolean('project_scope', 'Detach, archive, or reactivate projects', section='What to remove'),
            _boolean('machine', 'Remove machine installation(s)'),
            _boolean('cloud', 'Retire or purge a cloud instance'),
            _boolean('offline', 'Offline: never contact AWS', section='How to run',
                     help='Changes how the selections above run; it removes nothing by itself. '
                          'Cannot be combined with cloud removal, project archive or reactivation.')],
         'Select what to remove; each item is independent. Opening this dashboard does not contact '
         'the cloud. Inventory may contact the cloud only after you approve the selections.'),
        ('Projects', 'project_scope', [
            _optional('project', 'Project roots to detach (one absolute path per line)', multiline=True),
            _optional('reactivate_project', 'Project roots to reactivate instead (one per line)', multiline=True),
            _boolean('archive_project', 'Also archive selected cloud projects (all their checkouts)'),
            _optional('project_home', 'Installation home for project cloud operations')],
         'Reactivation is reversible and cannot accompany machine/cloud removal. '
         'Offline detachment preserves unresolved cloud evidence.'),
        ('Machine', None, [
            _optional('home', 'Installation homes (one per line; blank uses default or all-homes discovery)',
                      cli._default_home(), multiline=True),
            _boolean('all_homes', 'Include all discovered current-user homes'),
            _boolean('adopt_legacy', 'Inventory recognized legacy installations without ownership receipts')],
         'Home selection also supplies the default installation for project/cloud work. '
         'Multiple homes, all-homes, and legacy adoption require machine removal. '
         'Exact discovered inventories will be reviewed before any removal.'),
        ('Cloud', 'cloud', [
            _optional('cloud_home', 'Installation home for cloud inventory'),
            Field('cloud_mode', 'Cloud removal policy', 'retire', kind='select',
                  choices=(('Retire — preserve recoverable data (default)', 'retire'),
                           ('Purge — remove explicitly selected data', 'purge'))),
            _boolean('abandon_sessions', 'Administratively abandon unreachable sessions'),
            _optional('optional_resource', 'Dedicated resource selectors: s3:BUCKET, ssm:/prefix/, secret:ARN (one per line)', multiline=True),
            _optional('retained_resource', 'Previously retained resource ARNs (one per line)', multiline=True)],
         'Retirement is the default. Purge and session abandonment require separate '
         'instance confirmations after full resource review. Unselected/shared resources remain.'),
        ('Recovery', None, [
            _optional('export', 'Archive directory for settings and records (blank: machine removal deletes '
                                'them with the installation folder; project files stay in place)'),
            _optional('output', 'Private plan/recovery directory (blank uses ~/.maven-uninstall/<UUID>)')],
         'Recovery and export directories must be outside every selected removal root. '
         'Keep the complete plan, journal, runner, and staged dependencies for recovery.')]
    values = {}

    def shown(page):
        return page[1] is None or bool(values.get(page[1]))

    def step(index, direction):
        index += direction
        while 0 <= index < len(pages) and not shown(pages[index]):
            index += direction
        return index

    index = 0
    while True:
        title, _, fields, description = pages[index]
        ui.stage(title)
        fields = [replace(field, default=values.get(field.name, field.default)) for field in fields]
        result = ui.form(title, fields, description=description, back=True)
        if result is None:
            if index == 0:
                return None
            index = step(index, -1)
            continue
        values.update(result)
        if step(index, 1) < len(pages):
            index = step(index, 1)
            continue
        # A page hidden after its scope was deselected contributes parser defaults,
        # never stale entries that would request options for an unselected scope.
        for page in pages:
            if not shown(page):
                for field in page[2]:
                    values.pop(field.name, None)
        args = cli.parser().parse_args(['plan'])
        for name in vars(args):
            if name not in values:
                continue
            value = values[name]
            if name in ('project', 'reactivate_project', 'home', 'optional_resource', 'retained_resource'):
                value = _lines(value)
            elif isinstance(value, str):
                value = value.strip() or None
            setattr(args, name, value)
        error = None
        if values['project_scope'] and not (args.project or args.reactivate_project):
            error = 'Project scope requires at least one detach or reactivation root.'
        elif not (values['project_scope'] or args.machine or args.cloud):
            error = 'Select at least one item under "What to remove". Offline only changes how they run.'
        if error:
            ui.review('Revise selections', error, accept='Edit selections')
            index = 0
            continue
        if args.machine and not args.export:
            approved = ui.review('Delete without an archive?', NO_ARCHIVE_WARNING,
                                 accept='Delete without archive', back=True)
            if not approved:
                index = len(pages) - 1
                continue
        ui.stage('Inventory')
        if not ui.review('Approve read-only inventory', _json(vars(args)),
                         accept='Inventory exact targets', back=True):
            index = 0
            continue
        try:
            ui.checkpoint()
            return cli.build_plan(args)['plan_path']
        except (UninstallError, OSError, ValueError, ImportError) as error:
            detail = str(error) if isinstance(error, UninstallError) else type(error).__name__
            ui.review('Inventory could not complete', detail + '\n\nNo removal was applied. '
                      'Revise selections. If a custom operation directory was created, '
                      'preserve it and choose a new empty directory.', accept='Edit selections')
            index = 0


def _targets(plan):
    """Full canonical selected identities, never a prefix lookup into candidates."""
    seen = set()
    for target in plan['targets']:
        scope = target.get('scope')
        if scope == 'project':
            identity = target['root']
        elif scope == 'machine':
            identity = target['home']
        elif scope == 'cloud':
            identity = target['instance_id']
        elif target.get('kind') in ('project-archive', 'cloud-freeze'):
            identity = target['target']
        else:
            continue
        if identity not in seen:
            seen.add(identity)
            yield identity, target


def _unchanged(path, reviewed):
    _, current = cli._load_plan(path)
    if current['plan_digest'] != reviewed['plan_digest']:
        raise UninstallError('Plan changed after review. All approvals are invalid; inspect and review the new full plan.')
    return current


def _approve(ui, path, plan):
    ui.stage('Review')
    if not ui.review('Review exact immutable removal plan', _json(plan),
                     accept='Continue to approvals', back=True):
        return None
    ui.stage('Approvals')
    for identity, target in _targets(plan):
        if not ui.review('Approve selected target', _json(target) + '\n\nFull canonical identity: ' + identity,
                         identity=identity, destructive=True, accept='Approve target', back=True):
            return None
    arguments = ['resume', '--plan', str(path), '--confirm', plan['plan_digest'], '--tui-worker']
    for name, identity in plan['confirmations'].items():
        if name not in ('purge', 'abandon'):
            raise UninstallError('Unsupported separate destructive approval')
        warning = ('Purge explicitly selected cloud data; recovery windows may still apply.'
                   if name == 'purge' else
                   'Administratively abandon unreachable sessions. This does not prove remote writers stopped.')
        if not ui.review('Separately approve ' + name, warning + '\n\nFull instance identity: ' + identity,
                         identity=identity, destructive=True, accept='Approve ' + name, back=True):
            return None
        arguments.extend(['--confirm-' + name, identity])
    if 'writers-quiescent' in plan['requires']:
        if not ui.confirm('Have all affected writers actually stopped?'):
            return None
        arguments.append('--writers-quiescent')
    if 'harnesses-stopped' in plan['requires']:
        if not ui.review('Assistant harnesses may still be running', HARNESS_WARNING,
                         accept='Continue with removal', back=True):
            return None
        arguments.append('--harnesses-stopped')
    _unchanged(path, plan)
    return arguments


def _progress_text(data):
    actions = data.get('actions', {})
    completed = sum(entry['state'] == 'completed' for entry in actions.values())
    scopes = ', '.join(f'{scope}: {state}' for scope, state in data.get('scope_status', {}).items())
    last = data.get('last_action') or {}
    return (f"{data['status']} — {completed}/{len(actions)} actions complete — {scopes}\n"
            f"{last.get('kind', 'Preparing')} {last.get('target', '')}")


def _execute(ui, path, plan, arguments):
    from tools.tui.runtime import worker_command

    ui.checkpoint()
    command, environment = worker_command(path, arguments)
    _unchanged(path, plan)
    ui.stage('Execution')
    ui.message('Recovery directory: ' + str(Path(path).parent) +
               '\nStaged worker dependencies, when needed, are retained at ' +
               str(Path(path).parent / 'ui-runtime') + '. '
               'Pause waits for the current action; remote work may continue. No rollback is implied.')
    ui.checkpoint()
    child = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    streams = {child.stdout.fileno(): 'stdout', child.stderr.fileno(): 'stderr'}
    buffer = b''
    result = None
    protocol_error = False
    pause_sent = False
    interrupted = None
    last_progress = None

    def request_pause():
        nonlocal pause_sent
        if not pause_sent:
            pause_sent = True
            try:
                child.stdin.write(b'pause\n')
                child.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    # Never leave a live mutation worker behind, even when presentation fails.
    try:
        while streams:
            try:
                if ui.pause_requested.is_set() or interrupted is not None or protocol_error:
                    request_pause()
                readable, _, _ = select.select(list(streams), [], [], 0.1)
                for descriptor in readable:
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        del streams[descriptor]
                        continue
                    if descriptor != child.stdout.fileno():
                        continue  # Drain but never expose raw SDK/credential diagnostics.
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        try:
                            event = json.loads(line)
                            data = event['data']
                            if not isinstance(data, dict):
                                raise ValueError('invalid worker data')
                            if event['event'] == 'result':
                                if result is not None:
                                    raise ValueError('duplicate worker result')
                                result = data
                            elif event['event'] == 'progress':
                                progress = _progress_text(data)
                                if interrupted is None and progress != last_progress:
                                    ui.message(progress)
                                    last_progress = progress
                            else:
                                raise ValueError('invalid worker event')
                        except (ValueError, KeyError, TypeError):
                            protocol_error = True
            except BaseException as error:
                if interrupted is None:
                    interrupted = error
                request_pause()
        code = child.wait()
    finally:
        # A presentation/control error must request a durable boundary, not kill
        # the worker. Normal draining above continues until the child exits.
        if child.poll() is None:
            request_pause()
            while child.poll() is None:
                try:
                    readable, _, _ = select.select(list(streams), [], [], 0.1)
                    for descriptor in readable:
                        if not os.read(descriptor, 65536):
                            del streams[descriptor]
                except (KeyboardInterrupt, InterruptedError):
                    continue
            child.wait()
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
    if interrupted is not None and not isinstance(interrupted, (OperationPaused, KeyboardInterrupt)):
        raise interrupted
    if protocol_error or buffer or result is None or code != cli._exit_status(result):
        raise UninstallError('Worker did not return a valid final result. Preserve the recovery directory and inspect its local status.')
    return result


def _dashboard(ui):
    while True:
        ui.stage('Dashboard')
        ui.set_context('Removal and recovery • Selected installation: ' + cli._default_home() +
                       '\nProject, machine and cloud scopes are independent. No removal is authorized by opening this dashboard.')
        paths = sorted((Path.home() / '.maven-uninstall').glob('*/plan.json'))
        summaries = []
        choices = [('New removal / reactivation plan', 'new'),
                   ('Open an explicit custom plan path', 'custom')]
        for index, path in enumerate(paths):
            try:
                state = cli.status(path)
                summaries.append({'plan_path': str(path), **state})
                label = f"{state['operation_id']} — {state['status']} — " + ', '.join(
                    f'{scope}: {value}' for scope, value in state['scope_status'].items())
            except (UninstallError, OSError, ValueError) as error:
                label = str(path.parent.name) + ' — unreadable local plan'
                summaries.append({'plan_path': str(path), 'error': str(error) if isinstance(error, UninstallError) else type(error).__name__})
            choices.append((label, str(index)))
        choices.append(('Exit', 'exit'))
        selected = ui.choose('Local removal dashboard', tuple(choices),
                             description='Selected installation: ' + cli._default_home() +
                                         '\nStored receipts only; no live cloud checks. Existing plans retain their original targets.\n' + _json(summaries))
        if selected == 'exit':
            ui.close_when_finished()
            return 0
        try:
            if selected == 'new':
                path = _wizard(ui)
                if path is None:
                    continue
            elif selected == 'custom':
                value = ui.form('Open recovery plan', [Field('path', 'Exact plan.json path')], back=True)
                if value is None:
                    continue
                path = value['path'].strip()
            else:
                path = paths[int(selected)]
            path, plan = cli._load_plan(path)
            state = cli.status(path)
            ui.stage('Review')
            choice = ui.choose('Inspect / resume stored operation',
                               (('Inspect complete plan and stored status', 'inspect'),
                                ('Review and apply / resume', 'resume'), ('Back to dashboard', 'back')),
                               description=_json(state))
            if choice == 'inspect':
                ui.review('Exact plan and local journal status', _json({'plan': plan, 'status': state}), accept='Dashboard')
                continue
            if choice == 'back':
                continue
            if plan.get('tui_worker_protocol') != 1:
                recovery = [plan['recovery_python'], '-B', str(path.parent / 'runner/run.py'),
                            'resume', '--plan', str(path), '--confirm', plan['plan_digest']]
                for requirement in ('writers-quiescent', 'harnesses-stopped'):
                    if requirement in plan['requires']:
                        recovery.append('--' + requirement)
                for name, identity in plan['confirmations'].items():
                    recovery.extend(['--confirm-' + name, identity])
                ui.review('Existing snapshot requires CLI recovery',
                          'This immutable snapshot predates structured TUI workers. It will not be '
                          'upgraded or executed by newer apply code. Review the complete plan below, '
                          'then use its original recovery CLI only after satisfying every attestation '
                          'and separate purge/abandon approval.\n\n' + shlex.join(recovery) +
                          '\n\n' + _json(plan), accept='Dashboard')
                continue
            arguments = _approve(ui, path, plan)
            if arguments is None:
                continue
            result = _execute(ui, path, plan, arguments)
            ui.message(_json(result))
            if result['status'] == 'paused':
                ui.message('Paused at a saved action boundary. Preserve the recovery directory and '
                           'resume the exact plan later. Remote work may continue; no rollback is implied.')
            if ui.pause_requested.is_set() or result['status'] == 'paused':
                return cli._exit_status(result)
            ui.stage('Result')
            ui.review('Removal result — ' + result['status'], _json(result) +
                      '\n\nPreserve the recovery directory. A paused/blocked operation may have '
                      'completed actions; remote work may continue. No cloud rollback is claimed.', accept='Dashboard')
        except (UninstallError, OSError, ValueError, ImportError) as error:
            detail = str(error) if isinstance(error, UninstallError) else type(error).__name__
            ui.review('Operation needs attention', detail + '\n\nNo success is implied. '
                      'Preserve the operation directory and inspect its stored status.', accept='Dashboard')


def main():
    from tools.tui import run_workflow
    return run_workflow('MAVEN removal and recovery', STEPS, _dashboard)
