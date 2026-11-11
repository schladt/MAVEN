#!/usr/bin/env python3
"""Install and configure MAVEN step by step using only system Python to start."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import sys

from uuid import UUID
from tools.interaction import Field, OperationPaused, checkpoint, current, message, require, stage
from tools.harness import (
    HARNESS_PATHS as INTEGRATIONS, SKILLS, apply_integration, integration_plan,
    skill_plan, retired_skill_plan, retire_skills, validate_default_profiles, write_skill,
    launcher_permission_plan, repair_launcher_permissions,
)


def _notify(text, *, file=None):
    if current() is None:
        print(text, file=file)
    else:
        message(text)

HARNESS_PATHS = {name: Path(paths[2]) for name, paths in INTEGRATIONS.items()}


class BackToOptions(OperationPaused):
    """Return from an unsubmitted local review without rolling back anything."""


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError(f'{path} must be an owner-private directory, not a symlink')


def _no_symlink_parents(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f'refusing symlink target: {parent}')






def _prerequisites(require_aws: bool) -> None:
    if sys.platform == 'win32':
        raise ValueError('Use Windows Subsystem for Linux; native Windows is not supported.')
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required. Install it, then rerun python3 install.py.')
    missing = [name for name in ('venv', 'ensurepip') if importlib.util.find_spec(name) is None]
    if missing:
        raise ValueError(
            f'Python is missing {", ".join(missing)}. On Debian/Ubuntu install the venv package '
            'matching your Python (for example sudo apt install python3-venv), then rerun. '
            'On macOS use a complete Python installation from python.org or Homebrew.')
    _notify(f'Python {sys.version.split()[0]}: ready; venv and pip bootstrap available.')
    if shutil.which('git'):
        _notify('Git: available.')
    else:
        _notify('Git is not installed. Install Git for repository workflows, or choose a memory-only project.')
    aws = shutil.which('aws')
    version = None
    if aws:
        result = subprocess.run([aws, '--version'], capture_output=True, text=True, timeout=15)
        match = re.search(r'aws-cli/(\d+)\.(\d+)\.(\d+)', result.stdout + result.stderr)
        if result.returncode == 0 and match:
            version = tuple(int(part) for part in match.groups())
    if version is None or version < (2, 32, 0):
        notice = ('AWS CLI v2 version 2.32.0 or newer is required for aws login. '
                  'Install/update it from https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html '
                  '(macOS/Homebrew: brew install awscli), then rerun this installer.')
        if require_aws:
            raise ValueError(notice + ' Use --local-only only to install tools without configuring AWS.')
        _notify(f'Not configured yet: {notice}')
    else:
        _notify(f'AWS CLI {".".join(map(str, version))}: ready.')


def _configure_ca(path: Path | None) -> Path | None:
    if path is None:
        return None
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('The trusted CA bundle must be a readable PEM file supplied by your administrator.')
    ssl.create_default_context(cafile=str(path))
    for key in ('AWS_CA_BUNDLE', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'):
        os.environ[key] = str(path)
    return path


def _private_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError(f'{path} preserved: require an owner-owned regular file with mode 0600.')


def _activation(home: Path, ca_bundle: Path | None) -> list[str]:
    commands = [
        f'export MAVEN_HOME={shlex.quote(str(home))}',
        f'export PATH={shlex.quote(str(home / "venv/bin"))}:"$PATH"',
    ]
    if ca_bundle:
        commands.extend(f'export {key}={shlex.quote(str(ca_bundle))}' for key in (
            'AWS_CA_BUNDLE', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE',
        ))
    body = '# MAVEN shell environment. Never source the separate .env file.\n' + '\n'.join(commands) + '\n'
    target = home / 'activate'
    if target.exists() or target.is_symlink():
        _private_file(target)
        if ca_bundle and target.read_text() != body:
            _notify('Existing activation snippet preserved; use the explicit exports below for the selected CA.')
            return commands
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
    return [f'source {shlex.quote(str(target))}']


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, help='Private instance home (default MAVEN_HOME or ~/.maven)')
    parser.add_argument('--google', action='store_true', help='Include optional Google client dependencies')
    parser.add_argument('--harness', choices=tuple(HARNESS_PATHS), action='append', default=[],
                        help='Install documented stdio MCP configuration, startup guidance and skills; repeatable')
    parser.add_argument('--skill-dir', type=Path, action='append', default=[],
                        help='Explicit alternate skill root for a custom profile or inactive staging directory')
    parser.add_argument('--stage-harnesses', type=Path,
                        help='Write integrations under an inactive staging home instead of user configuration')
    parser.add_argument('--harness-config', action='append', default=[], metavar='NAME=PATH',
                        help='Explicit custom-profile MCP config file; pair with --harness-context and --skill-dir')
    parser.add_argument('--harness-context', action='append', default=[], metavar='NAME=PATH',
                        help='Explicit custom-profile startup context file')
    parser.add_argument('--harness-stopped', action='store_true',
                        help='Confirm affected harnesses are closed before changing live integration files')
    parser.add_argument('--ca-bundle', type=Path, help='Explicit trusted PEM CA bundle, e.g. supplied by corporate IT')
    parser.add_argument('--local-only', action='store_true',
                        help='Install local tools only; no prompts, login, cloud setup, or project initialization')
    args = parser.parse_args(argv)
    try:
        # Bootstrap pip must inherit explicitly approved corporate trust settings.
        _configure_ca(args.ca_bundle)
        if args.local_only:
            return _install(args)
        if not sys.stdin.isatty():
            raise ValueError('Guided setup needs an interactive terminal; use --local-only for unconfigured tools.')
        from tools.tui.bootstrap import ensure_ui
        result = ensure_ui([str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)])
        if result is not None:
            return result
        from tools.tui import run_workflow
        handoff = {}

        def operation(ui):
            while True:
                _options(args)
                home = args.home.expanduser().absolute()
                if args.uninstall:
                    handoff.update(home=home, uninstall=True)
                    ui.close_when_finished()
                    return 0
                if args.restart_operation:
                    if not (home / 'venv/bin/python').is_file():
                        # A prior local uninstall may have retained setup receipts.
                        # Restore tools only after the normal local-install review.
                        args.local_only = True
                        try:
                            result = _install(args)
                        except BackToOptions:
                            continue
                        if result:
                            return result
                    handoff.update(home=home, restart_operation=args.restart_operation)
                    ui.close_when_finished()
                    return 0
                if args.check_existing and (home / 'venv/bin/python').is_file():
                    # Selection is sufficient to open the next workflow. Cloud
                    # effects retain their own exact reviews there.
                    handoff.update(home=home)
                else:
                    try:
                        result = _install(args, handoff)
                    except BackToOptions:
                        continue
                    if result:
                        return result
                if args.local_only:
                    from tools.tui.harness import show_completion
                    show_completion(ui, home, handoff['integration'])
                    handoff.clear()
                    return 0
                handoff.update(mode=args.setup_mode, operation=args.setup_operation,
                               fresh_operation=args.setup_fresh)
                ui.close_when_finished()
                return 0

        result = run_workflow('MAVEN installation and removal', ['Choose a task'], operation)
        if result or not handoff:
            return result
        # The first app is closed before the installed setup app owns the terminal.
        home = handoff['home']
        from tools.tui.bootstrap import run_terminal
        if handoff.get('uninstall'):
            python = home / 'venv/bin/python'
            command = [str(python) if python.is_file() else sys.executable, '-m', 'tools.uninstall']
        elif handoff.get('restart_operation'):
            python = home / 'venv/bin/python'
            command = [str(python) if python.is_file() else sys.executable, '-m', 'tools.setup',
                       '--home', str(home), '--restart-operation', handoff['restart_operation']]
        else:
            command = [str(home / 'venv/bin/python'), '-m', 'tools.setup', '--home', str(home),
                       '--mode', handoff['mode']]
            if handoff.get('operation'):
                command.extend(['--operation', handoff['operation']])
            if handoff.get('fresh_operation'):
                command.append('--fresh-operation')
        return run_terminal(
            command, cwd=Path(__file__).resolve().parent,
            env=dict(os.environ, MAVEN_HOME=str(home), PYTHONPATH=str(Path(__file__).resolve().parent)),
        )
    except (EOFError, KeyboardInterrupt, OperationPaused):
        _notify('Installation paused; existing private configuration is preserved.', file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _notify(f'Installation stopped: {exc}', file=sys.stderr)
        return 1


def _saved_installation(home):
    """Display saved labels only; never treat this SDK-free overview as verified config."""
    values, notice, pending, completed = {}, '', None, False
    path = home / '.env'
    if path.exists() or path.is_symlink():
        try:
            _no_symlink_parents(path)
            _private_file(path)
            with path.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('oversized settings')
            display_keys = {
                'MAVEN_INSTANCE_NAME', 'MAVEN_MEMORY_INSTANCE_ID', 'MAVEN_STACK_NAME',
                'MAVEN_ACCOUNT_ID', 'MAVEN_MEMORY_CLIENT_ROLE_ARN', 'MAVEN_MEMORY_REGION',
                'MAVEN_AWS_REGION', 'MAVEN_AWS_PROFILE', 'MAVEN_DEPLOY_PROFILE',
            }
            for line in raw.decode('utf-8').splitlines():
                key, separator, value = line.strip().partition('=')
                if not separator or key not in display_keys:
                    continue
                value = value.strip()
                if value.startswith('"'):
                    value = json.loads(value)
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                if (key in values or not isinstance(value, str)
                        or any(ord(char) < 32 for char in value)):
                    raise ValueError('invalid saved label')
                values[key] = value
        except (OSError, ValueError, UnicodeError):
            values = {}
            notice = '\nSaved settings could not be read safely. Preserve them; check ownership, mode 0600 and literal values.'
    account = values.get('MAVEN_ACCOUNT_ID')
    role = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/.+', values.get('MAVEN_MEMORY_CLIENT_ROLE_ARN', ''))
    if role:
        account = role[1]
    if not account or not re.fullmatch(r'[0-9]{12}', account):
        account = 'Unknown until sign-in and confirmation'
    summary = '\n'.join([
        f'Installation: {home}', 'Saved configuration — NOT verified against AWS in this run.',
        'Instance: ' + (values.get('MAVEN_INSTANCE_NAME') or values.get('MAVEN_STACK_NAME')
                       or values.get('MAVEN_MEMORY_INSTANCE_ID') or 'Not connected yet'),
        f'AWS account: {account}',
        'Region: ' + (values.get('MAVEN_MEMORY_REGION') or values.get('MAVEN_AWS_REGION') or 'Not selected'),
        'Daily-use profile: ' + (values.get('MAVEN_AWS_PROFILE') or 'Not selected'),
        'Maintenance profile: ' + (values.get('MAVEN_DEPLOY_PROFILE') or 'Not selected; requested only when needed'),
    ]) + notice
    state_path = home / 'runtime/setup/state.json'
    if state_path.exists():
        try:
            from tools.uninstall.common import read_private
            saved = read_private(state_path)
            identifier = saved.get('administration_operation')
            if identifier:
                if str(UUID(identifier)) != identifier:
                    raise ValueError('invalid operation')
                state = read_private(home / 'runtime/administration' / (identifier + '.json'))
                completed = state['state'] == 'completed' and any(
                    receipt.get('step') == 'verify' and receipt.get('result', {}).get('instance_ready') is True
                    for receipt in state.get('receipts', []))
                if state['state'] not in ('completed', 'cancelled'):
                    pending = identifier
                    steps = ', '.join(receipt['step'] for receipt in state.get('receipts', [])) or 'None yet'
                    summary += (f'\n\nUnfinished setup: {identifier}\nCompleted checkpoints: {steps}'
                                f'\nNext saved step: {state["step"]}\nResume retains the exact requests and approvals; nothing is rolled back.')
        except (OSError, ValueError, KeyError, TypeError):
            summary += '\nSetup receipts need inspection; do not delete them or start over to bypass an unresolved operation.'
    return summary, pending, completed


def _advanced_options(args):
    selected = require().form('Advanced software options', [
        Field('google', 'Include optional Google integrations', args.google, kind='bool',
              help='Adds Google client dependencies. Does not sign in to Google or send any data.'),
        Field('ca', 'Trusted PEM certificate bundle', str(args.ca_bundle or ''), required=False,
              help='Only for a corporate/private certificate authority. Leave blank to keep normal TLS verification.'),
        Field('skill_dirs', 'Custom skill folders, one absolute path per line',
              '\n'.join(map(str, args.skill_dir)), kind='multiline', required=False,
              help='For custom harness profiles or inactive staging, not your project folder.'),
        Field('stage_harnesses', 'Inactive staging home', str(args.stage_harnesses or ''), required=False,
              help='Write explicitly selected integrations away from live harness configuration.'),
    ], description='These affect local software only. No AWS account, login or cloud resources are changed.',
       back=True)
    if selected is not None:
        args.google = selected['google']
        args.ca_bundle = Path(selected['ca']) if selected['ca'] else None
        args.skill_dir = [Path(line.strip()) for line in selected['skill_dirs'].splitlines() if line.strip()]
        args.stage_harnesses = Path(selected['stage_harnesses']) if selected['stage_harnesses'] else None


def _options(args):
    ui = require()
    args.uninstall = False
    args.restart_operation = None
    while True:
        stage('Choose a task')
        home = (args.home or Path(os.environ.get('MAVEN_HOME', '~/.maven'))).expanduser().absolute()
        _no_symlink_parents(home)
        existing = any((home / name).exists() for name in ('.env', 'venv', 'runtime/setup/state.json'))
        summary, pending, completed = _saved_installation(home) if existing else ('', None, False)
        ui.set_context('Local setup • No AWS identity verified yet • No cloud changes approved')
        if existing:
            task = ui.choose('Your existing MAVEN installation', [
                *([('Continue / configure coding harnesses — reuse prior readiness; local files only', 'harness')]
                  if completed else []),
                ('Resume unfinished setup — keep the original requests and approvals' if pending else
                 'Check or repair this connection — verify the saved instance; no automatic redeployment', 'existing'),
                *([('Abandon unfinished setup and start over — preserve receipts and cloud resources', 'restart')]
                  if pending else []),
                *([('Update MAVEN — local software and harness skills, then cloud service, database schema '
                    'and instructions, each separately reviewed', 'upgrade')] if completed and not pending else []),
                ('Update local MAVEN software — preserve cloud settings; no AWS login or cloud changes', 'update'),
                ('Set up a separate instance — choose another private installation folder', 'separate'),
                ('Uninstall — find an installation and choose what to remove', 'uninstall'),
                ('Advanced options — software-only installation and custom local settings', 'advanced'),
            ], description=summary)
        else:
            task = ui.choose('What would you like to do?', [
                ('Connect to existing MAVEN — install software and connect to existing cloud memory', 'existing'),
                ('Create a new MAVEN instance — review and provision billable AWS infrastructure', 'new'),
                ('Uninstall — find an installation and choose what to remove', 'uninstall'),
                ('Advanced options — install software without connecting to cloud memory', 'advanced'),
            ], description='Choose your goal. The wizard explains each step before asking you to decide.\n'
                           'Maintenance access manages infrastructure; daily-use access operates memory.\n'
                           'They may be separate identities in the same AWS account. Neither is assumed yet.')
        if task == 'harness':
            args.home, args.local_only, args.check_existing = home, False, True
            args.setup_mode, args.setup_operation, args.setup_fresh = 'existing', None, False
            return
        if task == 'upgrade':
            from tools.update import local_report
            from tools.uninstall.common import read_private
            from tools.uninstall.local import MANIFEST
            source = read_private(home / MANIFEST).get('source') if (home / MANIFEST).exists() else None
            # The installer itself is the checkout; its harnesses are the ones this home configured.
            report = local_report(home, Path(__file__).resolve().parent)
            args.home, args.local_only, args.check_existing = home, False, False
            args.harness = report['harnesses']
            args.setup_mode, args.setup_operation, args.setup_fresh = 'update', None, False
            if source and Path(source).resolve() != Path(__file__).resolve().parent:
                message(f'This installation was installed from {source}; updating it from {Path(__file__).resolve().parent}.')
            return
        if task == 'restart':
            args.home, args.restart_operation = home, pending
            return
        if task == 'uninstall':
            from tools.tui.installations import choose_removal_home
            selected = choose_removal_home(ui, extra=(home,))
            if selected is None:
                continue
            args.home, args.uninstall = selected, True
            return
        separate = task == 'separate'
        if separate:
            task = ui.choose('Set up a separate installation', [
                ('Connect to an existing cloud instance from another private home', 'existing'),
                ('Create new cloud infrastructure in another private home', 'new'),
                ('Back — keep the current installation unchanged', 'back'),
            ], description='The current installation will not be overwritten or disconnected.')
            if task == 'back':
                continue
        if task == 'advanced':
            advanced = ui.choose('Advanced options', [
                ('Install software without connecting to cloud memory — no AWS login or cloud changes', 'local'),
                ('Change optional software, certificate or staging settings', 'settings'),
                ('Back to task selection', 'back'),
            ], description='Software-only installs the MCP server and recovery tools. Memory will not work until you connect an instance.')
            if advanced == 'settings':
                _advanced_options(args)
                continue
            if advanced == 'back':
                continue
            task = 'local'
        stage('Installation folder')
        default_home = home.with_name(home.name + '-new') if separate else home
        selected = ui.form('Where should MAVEN keep this installation?', [
            Field('home', 'Private MAVEN installation folder', str(default_home),
                  help='Stores software, private connection settings and recovery receipts. This is NOT a project folder.'),
        ], description=('Use an unused folder for this separate installation. Existing files will not be replaced.'
                        if separate else 'Keep the suggested folder unless you need a separate installation. '
                        'Existing settings and recovery receipts are preserved.'),
           details='Google dependencies, certificate bundles and custom staging paths are under Advanced options.',
           back=True)
        if selected is None:
            continue
        chosen = Path(selected['home']).expanduser().absolute()
        _no_symlink_parents(chosen)
        if (separate or task == 'new') and chosen.exists() and any(chosen.iterdir()):
            ui.review('Choose an unused installation folder',
                      'Creating a separate/new instance must not reuse existing settings or receipts. '
                      'Nothing was changed. Choose another folder, or connect/check the existing installation.',
                      accept='Return to task selection')
            continue
        if chosen != home and not separate:
            # Reclassify a different saved home before offering any operation on it.
            if any((chosen / name).exists() for name in ('.env', 'venv', 'runtime/setup/state.json')):
                args.home = chosen
                continue
            pending = None
        args.home = chosen
        args.local_only = task in ('local', 'update')
        args.check_existing = task == 'existing' and existing and chosen == home and not separate
        args.setup_mode = 'new' if task == 'new' else 'existing'
        args.setup_operation = pending if args.check_existing else None
        args.setup_fresh = args.check_existing and completed and pending is None
        return


def _install_command(command):
    if current() is None:
        return subprocess.run(command, check=True, umask=0o022)
    # Local installation is one coherent unit through its ownership receipt. Do
    # not honor a pause between pip's mutation and recording that ownership.
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            umask=0o022)
    if result.returncode:
        raise ValueError('Local dependency installation failed. Check network access and the trusted CA bundle; existing private settings and skills are preserved.')
    return result


def _install(args, handoff=None):
    installation_locks = contextlib.ExitStack()
    try:
        if current() is None:
            _notify('\nStep 1/4 — Check prerequisites')
        else:
            stage('Prerequisites')
        _prerequisites(require_aws=not args.local_only)
        home = args.home or Path(os.environ.get('MAVEN_HOME', '~/.maven')).expanduser()
        home = home.expanduser().absolute()
        _no_symlink_parents(home)
        ca_bundle = _configure_ca(args.ca_bundle)
        source = Path(__file__).resolve().parent
        staging = getattr(args, 'stage_harnesses', None)
        integration_home = staging.expanduser().absolute() if staging else Path.home()
        if staging and (integration_home == Path.home()
                        or any((integration_home / paths[0]).resolve() ==
                               (Path.home() / paths[0]).resolve() for paths in INTEGRATIONS.values())):
            raise ValueError('Staging must use an inactive directory, not the live user home.')
        def custom_paths(values):
            result = {}
            for value in values:
                name, separator, location = value.partition('=')
                if not separator or name not in args.harness or not Path(location).expanduser().is_absolute():
                    raise ValueError('Custom integration paths require a selected NAME=ABSOLUTE_PATH.')
                if name in result:
                    raise ValueError(f'Duplicate custom integration selection: {name}')
                result[name] = Path(location).expanduser().absolute()
            return result
        configs = custom_paths(getattr(args, 'harness_config', []))
        contexts = custom_paths(getattr(args, 'harness_context', []))
        if configs.keys() != contexts.keys() or (configs and not args.skill_dir):
            raise ValueError('Custom profiles require paired --harness-config/--harness-context and explicit --skill-dir.')
        if staging and configs:
            raise ValueError('Stage under the inactive home first; custom live paths require --harness-stopped.')
        custom = {name: (path, contexts[name]) for name, path in configs.items()}
        if not staging:
            validate_default_profiles(name for name in args.harness if name not in custom)
        roots = [integration_home / HARNESS_PATHS[name] for name in args.harness if name not in custom]
        roots.extend(path.expanduser().absolute() for path in args.skill_dir)
        selected_harnesses = list(args.harness)
        deferred = False
        require_stopped = bool(roots and not args.harness_stopped and not (staging and not args.skill_dir))
        if require_stopped and current() is None:
            raise ValueError('Close affected harnesses and add --harness-stopped, or use '
                             '--stage-harnesses with an inactive directory. No integration changed.')
        try:
            plan = skill_plan(roots, home)
            integrations = integration_plan(home, selected_harnesses,
                                            user_home=integration_home, ca_bundle=ca_bundle, paths=custom)
            retired_skills = retired_skill_plan(home, roots)
        except ValueError as exc:
            if current() is None:
                raise
            message(str(exc))
            deferred, roots, plan, integrations, retired_skills = True, [], [], [], []
            selected_harnesses, require_stopped = [], False
        if current() is not None:
            stage('Review installation')
            body = '\n'.join([
                f'Private home: {home}', f'Runtime: {home / "venv"}',
                f'Settings: {home / ".env"} (existing settings preserved)',
                f'Package source: {source}', f'Extras: ui, mcp{", google" if args.google else ""}',
                f'Trusted CA: {ca_bundle or "current TLS configuration"}',
                f'Next: {"local tools only; no AWS/project setup" if args.local_only else "installed maven-setup for explicit cloud selection"}',
                'Shell profiles: unchanged',
                'Skill roots:\n' + ('\n'.join(map(str, roots)) or '(none)'),
                'Skill files to create or update:\n' + ('\n'.join(
                    str(target) + (' (update installer-owned copy)' if expected else '')
                    for target, _, expected in plan) or '(none; existing identical skills preserved)'),
                'Exact owned legacy skills to retire:\n' + ('\n'.join(
                    str(path) for path, _ in retired_skills) or '(none)'),
                f'Integration mode: {"inactive staging" if staging else "live"}',
                'Exact MAVEN integration content (unrelated configuration preserved):\n' + ('\n'.join(
                    f'{change.path}\n{change.receipt["value"]}' for change in integrations
                    if change.receipt is not None) or '(none)'),
            ])
            summary = (f'Install/update MAVEN software in {home}.\n'
                       'This step downloads Python dependencies and writes local software and ownership receipts.\n'
                       'It does not log in to AWS, create cloud resources, or enable any project.\n'
                       'Existing private connection settings and unrelated harness configuration are preserved.\n'
                       'Quit after installation does not uninstall the software; rerun to continue setup.')
            if deferred:
                summary += '\nHarness integration has a local conflict and will be skipped; its existing files are preserved.'
            if require_stopped:
                summary += ('\nRunning harness sessions do not block installation; restart them to load MAVEN. '
                            'A harness that saves its own configuration file while running can overwrite the '
                            'MAVEN entry; after restarting it, confirm MAVEN is listed.')
            review = require().review('Review local software installation', summary, details=body,
                                      back=True, accept='Install software')
            if review is None:
                raise BackToOptions('Return to installation choices; no local mutation was started.')
            if not review:
                raise OperationPaused('Installation declined before local mutation.')
        checkpoint()
        _private_directory(home)
        from tools.uninstall.local import _lock, reinstall_marker, finish_reinstallation
        installation_locks.enter_context(_lock(home / 'runtime/registry.lock'))
        completed_marker = reinstall_marker(home)
        new_caches = [home / 'runtime' / name for name in ('aws-login-cache', 'aws-deploy-login-cache')
                      if not (home / 'runtime' / name).exists()]
        for folder in ('runtime', 'runtime/aws-login-cache', 'runtime/aws-deploy-login-cache'):
            _private_directory(home / folder)
        environment = home / '.env'
        if environment.exists() or environment.is_symlink():
            _private_file(environment)
        installation = home / 'venv'
        _no_symlink_parents(installation)
        # Never adopt a pre-existing virtual environment or pre-existing skills.
        # Existing receipts are refreshed only for the runtime we actually upgrade.
        from tools.uninstall.common import read_private
        from tools.uninstall.local import MANIFEST, record_installation
        receipt = home / MANIFEST
        ownership = read_private(receipt) if receipt.exists() else {}
        if ownership and (ownership.get('schema') != 1 or ownership.get('home') != str(home)):
            raise ValueError('Installer ownership receipt does not match this home; preserved for review.')
        own_runtime = not installation.exists() or str(installation) in ownership.get('owned', {})
        own_activation = not (home / 'activate').exists()
        if installation.exists() and not (installation / 'pyvenv.cfg').is_file():
            raise ValueError(f'Existing non-venv target preserved: {installation}')
        if current() is None:
            _notify('\nStep 3/4 — Create/reuse the virtual environment and install MAVEN')
        else:
            stage('Install tools')
        import venv
        if not installation.exists():
            venv.EnvBuilder(with_pip=True).create(installation)
        python = installation / 'bin' / 'python'
        if not python.is_file():
            raise ValueError(f'Existing virtual environment is incomplete: {installation}. '
                             'Preserve it for review and choose another --home, or repair that environment.')
        pip_ready = subprocess.run([str(python), '-m', 'pip', '--version'], capture_output=True, text=True)
        if pip_ready.returncode:
            _install_command([str(python), '-m', 'ensurepip', '--upgrade'])
        package = str(source) + ('[ui,mcp,google]' if args.google else '[ui,mcp]')
        _install_command([str(python), '-m', 'pip', 'install', '--upgrade', package])
        # pip derives entry-point mode from its umask. Never leave 0775 launchers
        # for the harness reader, which correctly rejects group-writable files.
        launcher_repair = launcher_permission_plan(home)
        if launcher_repair is not None:
            repair_launcher_permissions(home, launcher_repair)
        if not environment.exists():
            body = (source / 'maven_resources' / 'templates' / 'environment.example').read_bytes()
            fd = os.open(environment, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
        for target, body, expected in plan:
            _no_symlink_parents(target)
            write_skill(target, body, expected)
        activation = _activation(home, ca_bundle)
        owned_paths = [target for target, _, _ in plan]
        if own_runtime:
            owned_paths.append(installation)
        if own_activation:
            owned_paths.append(home / 'activate')
        record_installation(home, source, owned_paths,
                            [root / name / 'SKILL.md' for root in roots for name in SKILLS],
                            cache_roots=new_caches)
        # Journal exact ownership before mutation: interruption leaves either an
        # absent entry or a safely removable exact entry, never an orphan.
        for change in integrations:
            if change.receipt is not None:
                record_installation(home, source, [], [], integrations=[change.receipt])
            apply_integration(change)
        retire_skills(retired_skills)
        finish_reinstallation(home, completed_marker)
        installation_locks.close()
        if current() is not None:
            stage('Local completion')
        _notify(f'\nLocal tools installed in {installation}. Private settings: {environment}')
        _notify('In each new terminal, activate this installation (no shell profiles were modified):')
        for command in activation:
            _notify(f'  {command}')
        if roots:
            _notify('MAVEN front-door and maintenance skills installed/preserved.')
            if staging:
                _notify(f'Inactive integration staged under {integration_home}; live harness configuration unchanged. '
                        'Review and install against the selected profile after affected harnesses stop.')
            else:
                _notify('Start a new harness session to load startup guidance. MCP connection alone is not project readiness.')
        elif deferred:
            _notify('Harness integration is pending. Stage with --stage-harnesses, or rerun after closing '
                    'affected harnesses with --harness-stopped.')
        else:
            _notify('No harness configuration was modified.')
        checkpoint()
        if handoff is not None:
            handoff['home'] = home
            handoff['activation'] = activation
            changed = ([str(target) for target, _, _ in plan] +
                       [str(change.path) for change in integrations if change.before != change.after] +
                       [str(target) for target, _ in retired_skills])
            handoff['integration'] = {
                'status': 'written' if changed else 'unchanged' if selected_harnesses else 'skipped',
                'selected': selected_harnesses, 'written': changed,
                'reason': ('Harness integration deferred; live files were not changed.' if deferred else
                           'Cloud access was not checked during this software installation.'),
            }
        if args.local_only:
            _notify('\nLocal-only installation complete; AWS and project setup have NOT been performed.')
            _notify('After activation, run maven-setup to connect an instance. Enable projects later in your harness.')
            return 0
        return 0
    except OperationPaused:
        raise
    except (EOFError, KeyboardInterrupt):
        _notify('\nInstallation paused. Rerun python3 install.py; existing private configuration is preserved.',
              file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _notify(f'Installation stopped: {exc}', file=sys.stderr)
        _notify('If TLS verification failed on a corporate VPN, use --ca-bundle with an IT-approved PEM bundle. '
              'TLS verification must remain enabled.', file=sys.stderr)
        return 1
    finally:
        installation_locks.close()


if __name__ == '__main__':
    raise SystemExit(main())
