#!/usr/bin/env python3
"""Install and configure MAVEN step by step using only system Python to start."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
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
import tempfile

from uuid import UUID, uuid4
from tools.interaction import Field, OperationPaused, checkpoint, current, message, require, stage
from tools.harness import (
    HARNESS_PATHS as INTEGRATIONS, SKILL_FILES, apply_reviewed_integration, integration_plan,
    installed_targets, recorded_integration_plan, skill_cutover_plan, retire_skills, validate_default_profiles,
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
    from tools.update import apply_owned_skills
    apply_owned_skills(home, Path(__file__).resolve().parent,
                       [] if target.exists() or target.is_symlink() else [(target, body.encode(), None)], [], activation=True)
    if target.exists() or target.is_symlink():
        _private_file(target)
        if ca_bundle and target.read_text() != body:
            _notify('Existing activation snippet preserved; use the explicit exports below for the selected CA.')
            return commands
    return [f'source {shlex.quote(str(target))}']


def _runtime_intent(home, source, owned):
    """Keep one original installation identity across uncertain local subprocess effects."""
    from tools.uninstall.common import read_private, save_private
    from tools.uninstall.local import inventory
    from tools.update import _checkout_files, _packaging_inputs
    directory = home / 'runtime' / 'runtime-installations'
    inputs = _checkout_files(source)
    inputs.update(_packaging_inputs(source))
    pending = [(path, read_private(path)) for path in directory.glob('*.json')]
    pending = [(path, value) for path, value in pending if not value.get('completed')]
    if len(pending) > 1:
        raise ValueError('Multiple unfinished runtime intents; preserve and select the original installation.')
    target = home / 'venv'
    actual = inventory(target) if target.exists() else None
    if pending:
        path, value = pending[0]
        if value.get('schema') != 1 or value.get('home') != str(home) or value.get('inputs') != inputs:
            raise ValueError('Interrupted runtime installation inputs changed; restore the original checkout inputs.')
        if actual != value['inventory']:
            previous_root = (value['inventory'] or {}).get('')
            current_root = (actual or {}).get('')
            if previous_root and (not current_root or any(
                    previous_root[key] != current_root[key] for key in ('dev', 'ino', 'uid', 'type'))):
                raise ValueError('Interrupted runtime directory identity changed; preserve the original installation intent.')
            # No subprocess receipt can prove which unknown bytes were produced before a kill.
            # A fresh exact local review may reconcile only this original installation intent.
            if current() is None or not value.get('applying') or not require().review(
                    'Reconcile interrupted runtime installation',
                    'The original local installation was interrupted during ' + value['applying'] +
                    '. Review the exact changed runtime inventory before continuing its original intent. '
                    'Private settings and unrelated files outside this runtime remain untouched.',
                    details=json.dumps({'intent': str(path), 'before': value['inventory'], 'current': actual},
                                       indent=2)):
                raise ValueError('Interrupted runtime bytes require exact guided reconciliation; original intent retained.')
            value['reconciliations'].append({'phase': value['applying'], 'before': value['inventory'], 'current': actual})
            value['inventory'] = actual
            save_private(path, value)
        return path, value
    if target.exists() and not (target / 'pyvenv.cfg').is_file():
        raise ValueError(f'Existing non-venv target preserved: {target}')
    path = directory / (str(uuid4()) + '.json')
    value = {'schema': 1, 'home': str(home), 'inputs': inputs, 'owned': owned,
             'inventory': actual, 'applying': None, 'reconciliations': [], 'completed': False}
    save_private(path, value)
    return path, value


@contextlib.contextmanager
def _runtime_effect(intent, home, phase):
    from tools.uninstall.common import save_private
    from tools.uninstall.local import inventory
    path, value = intent
    value['applying'] = phase
    save_private(path, value)
    yield
    value['inventory'] = inventory(home / 'venv')
    value['applying'] = None
    save_private(path, value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, help='Private instance home (default MAVEN_HOME or ~/.maven)')
    parser.add_argument('--harness', choices=tuple(HARNESS_PATHS), action='append', default=[],
                        help='Review stdio MCP, workspace guidance and skills; repeatable')
    parser.add_argument('--skill-dir', type=Path, action='append', default=[],
                        help='Explicit alternate skill root for a custom profile or inactive staging directory')
    parser.add_argument('--stage-harnesses', type=Path,
                        help='Write integrations under an inactive staging home instead of user configuration')
    parser.add_argument('--harness-config', action='append', default=[], metavar='NAME=PATH',
                        help='Explicit custom-profile MCP config file; pair with --harness-context; --skill-dir selects skills')
    parser.add_argument('--harness-context', action='append', default=[], metavar='NAME=PATH',
                        help='Explicit custom-profile startup context file')
    parser.add_argument('--refresh-integrations', action='store_true',
                        help='With --local-only, review all previously recorded harness profiles and skill roots')
    # Old local-update handoffs included the recorded adapter destination. Accept
    # it only as a cross-check of the receipt, never as a registration or removal request.
    parser.add_argument('--harness-adapter', action='append', default=[], help=argparse.SUPPRESS)
    parser.add_argument('--claude-settings', type=Path,
                        help='Exact settings.json for --claude-statusline in a custom Claude profile')
    parser.add_argument('--approve-harness-review', metavar='SHA256',
                        help='Headless only: apply the exact harness review printed by the first invocation')
    parser.add_argument('--harness-stopped', action='store_true',
                        help='Record that affected harnesses are closed (optional); restart them after integration updates')
    parser.add_argument('--ca-bundle', type=Path, help='Explicit trusted PEM CA bundle, e.g. supplied by corporate IT')
    parser.add_argument('--claude-statusline', action='store_true',
                        help="With --harness claude: show the active MAVEN project in Claude Code's status line "
                             '(an owned statusLine entry; an existing different status line is preserved)')
    parser.add_argument('--local-only', action='store_true',
                        help='Install local tools only; no prompts, login, cloud setup, or project initialization')
    args = parser.parse_args(argv)
    if args.approve_harness_review and not args.local_only:
        parser.error('--approve-harness-review applies only to --local-only')
    if args.refresh_integrations and (not args.local_only or args.harness or args.skill_dir or args.stage_harnesses
                                      or args.harness_config or args.harness_context or args.harness_adapter
                                      or args.claude_statusline or args.claude_settings):
        parser.error('--refresh-integrations requires --local-only and no explicit harness targets')
    if args.claude_settings and not args.claude_statusline:
        parser.error('--claude-settings requires --claude-statusline')
    if args.claude_statusline and 'claude' not in args.harness:
        parser.error('--claude-statusline applies only together with --harness claude')
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
                    if not (home / 'venv/bin/python').is_file():
                        source = Path(__file__).resolve().parent
                        if not ui.review('Emergency checkout recovery',
                                         'The installed runtime is missing. Run removal from this checkout, '
                                         'not installed software. Existing removal approvals remain required.',
                                         details=f'Python: {sys.executable}\nCheckout: {source}'):
                            raise OperationPaused('Emergency checkout recovery declined.')
                        handoff['emergency_source'] = str(source)
                    handoff.update(home=home, uninstall=True)
                    ui.close_when_finished()
                    return 0
                if args.home_action or getattr(args, 'deployment_access', False):
                    handoff.update(home=home, home_action=args.home_action,
                                   deployment_access=getattr(args, 'deployment_access', False))
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
                    handoff.update(home=home, migrate_environment=True)
                    ui.close_when_finished()
                    return 0
                handoff.update(mode=args.setup_mode, operation=args.setup_operation,
                               fresh_operation=args.setup_fresh, repair_secrets=getattr(args, 'repair_secrets', False))
                ui.close_when_finished()
                return 0

        result = run_workflow('MAVEN installation and removal', ['Choose a task'], operation)
        if result or not handoff:
            return result
        # The first app is closed before the installed setup app owns the terminal.
        from tools.tui.bootstrap import run_terminal
        environment = {key: value for key, value in os.environ.items()
                       if key not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE')}
        environment['MAVEN_HOME'] = str(handoff['home'])
        with tempfile.TemporaryDirectory(prefix='maven-handoff-') as neutral:
            result = run_terminal(_handoff_command(handoff), cwd=neutral, env=environment)
        if result:
            _notify(f'Selected task exited {result}; its original private operation is retained. '
                    'Resume that exact operation in this home after addressing the child diagnostic.', file=sys.stderr)
        return result
    except (EOFError, KeyboardInterrupt, OperationPaused):
        _notify('Installation paused; existing private configuration is preserved.', file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _notify(f'Installation stopped: {exc}', file=sys.stderr)
        return 1


_INSTALLED_HANDOFF = """
import importlib.metadata
import importlib.util
from pathlib import Path
import runpy
import sys
home = Path(sys.argv.pop(1))
module = sys.argv.pop(1)
expected = (home / 'venv').resolve()
try:
    distribution = importlib.metadata.distribution('maven-cloud-memory')
    package = importlib.util.find_spec('tools')
    valid = (Path(sys.prefix).resolve() == expected and package is not None and package.origin
             and Path(package.origin).resolve().is_relative_to(expected)
             and Path(distribution.locate_file('')).resolve().is_relative_to(expected)
             and Path(package.origin).resolve() == Path(distribution.locate_file('tools/__init__.py')).resolve())
except importlib.metadata.PackageNotFoundError:
    valid = False
if not valid:
    raise SystemExit('Installed MAVEN package origin is unavailable or mismatched; repair this exact runtime.')
selected = importlib.util.find_spec(module)
if selected is None or not selected.origin or not Path(selected.origin).resolve().is_relative_to(expected):
    raise SystemExit('Installed task module origin is unavailable or mismatched; no checkout fallback was used.')
sys.argv[0] = module
runpy.run_module(module, run_name='__main__', alter_sys=True)
"""


def _handoff_command(handoff):
    """The installed program that continues the task chosen in the installer menu."""
    home = handoff['home']
    python = home / 'venv/bin/python'
    prefix = [str(python), '-I', '-c', _INSTALLED_HANDOFF, str(home)]
    if not python.is_file():
        source = handoff.get('emergency_source')
        if not source or not (handoff.get('uninstall') or handoff.get('restart_operation')):
            raise ValueError('Installed runtime is missing; explicitly review emergency checkout recovery.')
        source = Path(source).resolve()
        if source != Path(__file__).resolve().parent or not (source / 'pyproject.toml').is_file():
            raise ValueError('Emergency recovery checkout does not match the reviewed installer.')
        prefix = [sys.executable, '-I', '-c',
                  'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); '
                  'module=sys.argv.pop(1); runpy.run_module(module,run_name="__main__",alter_sys=True)',
                  str(source)]
    if handoff.get('uninstall'):
        return [*prefix, 'tools.uninstall']
    if handoff.get('home_action'):
        return [*prefix, 'tools.tui.administration', '--home', str(home),
                '--action', handoff['home_action']]
    if handoff.get('restart_operation'):
        return [*prefix, 'tools.setup',
                '--home', str(home), '--restart-operation', handoff['restart_operation']]
    if handoff.get('repair_secrets'):
        return [*prefix, 'tools.setup', '--home', str(home), '--repair-secrets']
    if handoff.get('deployment_access'):
        return [*prefix, 'tools.setup', '--home', str(home), '--deployment-access']
    if handoff.get('migrate_environment'):
        return [*prefix, 'tools.setup', '--home', str(home), '--migrate-environment']
    command = [*prefix, 'tools.setup', '--home', str(home), '--mode', handoff['mode']]
    if handoff.get('operation'):
        command.extend(['--operation', handoff['operation']])
    if handoff.get('fresh_operation'):
        command.append('--fresh-operation')
    return command


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
                'MAVEN_DEPLOY_MODE', 'MAVEN_DEPLOY_ROLE_ARN',
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
        'Deployment access: ' + (values.get('MAVEN_DEPLOY_ROLE_ARN', 'Missing role ARN')
            if values.get('MAVEN_DEPLOY_MODE') == 'role' else 'Configure later'
            if values.get('MAVEN_DEPLOY_MODE') == 'deferred' else
            values.get('MAVEN_DEPLOY_PROFILE', 'Not selected')),
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
        args.ca_bundle = Path(selected['ca']) if selected['ca'] else None
        args.skill_dir = [Path(line.strip()) for line in selected['skill_dirs'].splitlines() if line.strip()]
        args.stage_harnesses = Path(selected['stage_harnesses']) if selected['stage_harnesses'] else None


def _options(args):
    ui = require()
    args.refresh_integrations = False
    args.uninstall = False
    args.restart_operation = None
    args.home_action = None
    args.repair_secrets = False
    args.deployment_access = False
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
                *([('Repair secrets / integrations — import or verify exact portable locators; read-only checks', 'repair-secrets')]
                  if not pending else []),
                *([('Change deployment access — use an existing IAM role, a separate sign-in profile, or configure later',
                    'deployment-access')] if (home / '.env').is_file() else []),
                *([('Abandon unfinished setup and start over — preserve receipts and cloud resources', 'restart')]
                  if pending else []),
                *([('Update MAVEN — local software and harness skills, then database schema '
                    'and cloud service, each separately reviewed', 'upgrade')] if completed and not pending else []),
                *([('Set up or change MAVEN Home — choose, change or turn off the Home project', 'home')]
                  if completed and not pending else []),
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
        if task in ('repair-secrets', 'deployment-access'):
            args.home, args.local_only, args.check_existing = home, False, True
            args.setup_mode, args.setup_operation, args.setup_fresh = 'existing', None, False
            args.repair_secrets = task == 'repair-secrets'
            args.deployment_access = task == 'deployment-access'
            return
        if task == 'harness':
            args.home, args.local_only, args.check_existing = home, False, True
            args.setup_mode, args.setup_operation, args.setup_fresh = 'existing', None, False
            return
        if task == 'upgrade':
            from tools.uninstall.common import read_private
            from tools.uninstall.local import MANIFEST
            source = read_private(home / MANIFEST).get('source') if (home / MANIFEST).exists() else None
            args.home, args.local_only, args.check_existing = home, False, False
            args.refresh_integrations = True
            args.setup_mode, args.setup_operation, args.setup_fresh = 'update', None, False
            if source and Path(source).resolve() != Path(__file__).resolve().parent:
                message(f'This installation was installed from {source}; updating it from {Path(__file__).resolve().parent}.')
            return
        if task == 'restart':
            args.home, args.restart_operation = home, pending
            return
        if task == 'home':
            action = 'enable'
            if (home / 'home.json').exists() or (home / 'home.json').is_symlink():
                action = ui.choose('MAVEN Home', [
                    ('Change MAVEN Home — choose a different Home folder or project', 'enable'),
                    ('Turn off MAVEN Home — remove only this machine’s Home selection', 'disable'),
                    ('Back to task selection', 'back'),
                ], description=f'MAVEN Home is set up in {home / "home.json"}. Each change is reviewed before it is applied.')
                if action == 'back':
                    continue
            args.home, args.home_action = home, action
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
           details='Certificate bundles and custom staging paths are under Advanced options.',
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
        args.refresh_integrations = task == 'update'
        args.check_existing = task == 'existing' and existing and chosen == home and not separate
        args.setup_mode = 'new' if task == 'new' else 'existing'
        args.setup_operation = pending if args.check_existing else None
        if args.setup_operation:
            # Resume continues the unfinished operation in its own mode (e.g. an update).
            from tools.uninstall.common import read_private
            saved = read_private(home / 'runtime/administration' / (pending + '.json'))
            args.setup_mode = saved.get('parameters', {}).get('mode') or args.setup_mode
        args.setup_fresh = args.check_existing and completed and pending is None
        return


def _install_command(command):
    if current() is None:
        return subprocess.run(command, check=True, umask=0o022)
    # Local installation is one coherent unit through its ownership receipt. Do
    # not honor a pause between pip's mutation and recording that ownership.
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            umask=0o022)
    if result.returncode:
        diagnostic = (result.stderr or b'').decode('utf-8', errors='replace').lower()
        reason = ('TLS trust verification failed; select the approved CA bundle' if
                  'certificate_verify_failed' in diagnostic or 'sslcertverificationerror' in diagnostic else
                  'dependency requirements could not be resolved; review the selected Python/package requirements' if
                  'no matching distribution' in diagnostic or 'resolutionimpossible' in diagnostic else
                  'the installation filesystem denied a write; check its ownership and available space' if
                  'permission denied' in diagnostic or 'no space left' in diagnostic else
                  'package installation failed; check network access and package-index availability')
        raise ValueError(f'Local installation exited {result.returncode}: {reason}. '
                         'Original local intent and private settings are preserved; resume this same home.')
    return result


@contextlib.contextmanager
def _package_source(source):
    """Build from current distribution inputs, never stale checkout build/egg-info output."""
    with tempfile.TemporaryDirectory(prefix='maven-package-') as directory:
        clean = Path(directory)
        for name in ('pyproject.toml', 'README.md', 'LICENSE'):
            shutil.copy2(source / name, clean / name)
        for name in ('tools', 'maven_resources'):
            shutil.copytree(source / name, clean / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        yield clean


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
        refresh = (getattr(args, 'refresh_integrations', False) and not
                   (args.harness or args.skill_dir or getattr(args, 'stage_harnesses', None)))
        recorded = installed_targets(home) if refresh else None
        if refresh and not (home / 'venv/pyvenv.cfg').is_file() and current() is None:
            raise ValueError('No existing virtual environment at the selected home; nothing updated.')
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
        adapters = custom_paths(getattr(args, 'harness_adapter', []))
        if adapters:
            saved_sites = installed_targets(home)['adapters']
        contexts = custom_paths(getattr(args, 'harness_context', []))
        if configs.keys() != contexts.keys():
            raise ValueError('Custom profiles require paired --harness-config/--harness-context.')
        if adapters and any(name not in configs or path != saved_sites.get((name, configs[name], contexts[name]))
                            for name, path in adapters.items()):
            raise ValueError('Obsolete --harness-adapter must match this installation’s recorded profile; '
                             'use --refresh-integrations to review the complete inventory.')
        if getattr(args, 'claude_settings', None) and 'claude' not in configs:
            raise ValueError('--claude-settings requires an explicit paired Claude profile.')
        if staging and configs:
            raise ValueError('Choose either an inactive staging home or explicit custom destination paths, not both.')
        custom = {name: (path, contexts[name]) for name, path in configs.items()}
        if not staging:
            validate_default_profiles(name for name in args.harness if name not in custom)
        roots = (list(recorded['skill_roots']) if refresh else
                 [integration_home / HARNESS_PATHS[name] for name in args.harness if name not in custom]
                 + [path.expanduser().absolute() for path in args.skill_dir])
        selected_harnesses = sorted({name for name, _, _ in recorded['profiles']}) if refresh else list(args.harness)
        deferred = False
        require_stopped = bool(roots and not args.harness_stopped and not (staging and not args.skill_dir))
        if require_stopped and current() is None:
            _notify('Restart affected harnesses to load updated tools. Concurrent harness configuration writes '
                    'can overwrite the reviewed integration; unrelated configuration is preserved.')
        skill_conflicts = []
        if refresh:
            skill_conflicts.extend({'target': path, 'status': 'conflict',
                                    'reason': 'Ambiguous recorded destination; specify exact paths separately.'}
                                   for path in recorded['unresolved'])
            if selected_harnesses and not roots:
                skill_conflicts.append({'target': ', '.join(selected_harnesses), 'status': 'conflict',
                                        'reason': 'No recorded canonical skill root; select --skill-dir.'})
        if not args.skill_dir:
            skill_conflicts.extend({'target': name, 'status': 'conflict', 'component': 'skill',
                                    'reason': 'Custom profile has no selected canonical skill root; use --skill-dir.'}
                                   for name in custom)
        from tools.update import pending_owned_skills
        try:
            plan, retired_skills, ready_roots = skill_cutover_plan(home, roots, conflicts=skill_conflicts)
            def plan_integrations():
                if refresh:
                    targets = installed_targets(home)
                    if targets != recorded:
                        raise ValueError('Recorded harness destinations changed; review the new plan.')
                    return recorded_integration_plan(home, targets, ca_bundle=ca_bundle)
                return integration_plan(home, selected_harnesses, user_home=integration_home, ca_bundle=ca_bundle,
                                        paths=custom, statusline=getattr(args, 'claude_statusline', False),
                                        claude_settings=getattr(args, 'claude_settings', None))
            integrations = plan_integrations()
        except ValueError as exc:
            if current() is None:
                raise
            message(str(exc))
            deferred, roots, ready_roots, plan, integrations, retired_skills = True, [], [], [], [], []
            selected_harnesses, require_stopped = [], False
        pending_targets = [home / 'activate', *(root / name for root in ready_roots for name in SKILL_FILES)]
        pending_skills = pending_owned_skills(home, pending_targets)
        review_files = bool(roots or integrations or refresh)
        if current() is not None or review_files:
            if current() is not None:
                stage('Review installation')
            body = '\n'.join([
                f'Private home: {home}', f'Runtime: {home / "venv"}',
                f'Settings: {home / ".env"} (existing settings preserved)',
                f'Package source: {source}', 'Extras: ui, mcp',
                f'Trusted CA: {ca_bundle or "current TLS configuration"}',
                f'Next: {"local tools only; no AWS/project setup" if args.local_only else "installed maven-setup for explicit cloud selection"}',
                'Shell profiles: unchanged',
                'Skill roots:\n' + ('\n'.join(map(str, roots)) or '(none)'),
                'Original unfinished file intents to reconcile:\n' + json.dumps(pending_skills, indent=2),
                'Preserved skill conflicts:\n' + ('\n'.join(
                    str(item['target']) + ': ' + item['reason'] for item in skill_conflicts) or '(none)'),
                'Skill files to create or update:\n' + ('\n'.join(
                    str(target) + (' (update installer-owned copy)' if expected else '')
                    for target, _, expected in plan) or '(none; existing identical skills preserved)'),
                'Exact owned legacy skills to retire:\n' + ('\n'.join(
                    str(path) for path, _ in retired_skills) or '(none)'),
                f'Integration mode: {"inactive staging" if staging else "live"}',
                'MCP, workspace guidance and skills only. No turn or lifecycle hooks will be installed.',
                'Exact MAVEN integration content (unrelated configuration preserved):\n' + ('\n'.join(
                    (f'Conflict, not changed: {change.receipt["conflict"]}' if change.receipt.get('conflict')
                     else change.retirement_review() if change.receipt.get('retired')
                     else f'{change.path}\n{change.receipt["value"]}') for change in integrations
                    if change.receipt is not None) or '(none)'),
            ])
        if current() is None and review_files:
            # Bind skill bytes, old file snapshots and selective removal ownership
            # without echoing unrelated profile contents.
            def digest(raw):
                return hashlib.sha256(raw).hexdigest() if raw is not None else None
            inputs = [body, [(str(p), digest(b), expected) for p, b, expected in plan],
                      [(str(p), inventory) for p, inventory in retired_skills],
                      [(c.receipt, c.remaining, digest(c.before), digest(c.after)) for c in integrations]]
            token = digest(json.dumps(inputs, sort_keys=True).encode())
            if getattr(args, 'approve_harness_review', None) != token:
                _notify(body)
                _notify('No changes applied. Review the proposals above; to apply, repeat the same arguments with '
                        f'--approve-harness-review {token}')
                return 1
        if current() is not None:
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
        if not deferred and (selected_harnesses or refresh) and plan_integrations() != integrations:
            raise ValueError('Harness configuration or ownership changed after review; re-plan before writing.')
        if pending_owned_skills(home, pending_targets) != pending_skills:
            raise ValueError('Pending file installation intents changed after review; review the original intents again.')
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
        if current() is None:
            _notify('\nStep 3/4 — Create/reuse the virtual environment and install MAVEN')
        else:
            stage('Install tools')
        import venv
        runtime_intent = _runtime_intent(home, source, own_runtime)
        own_runtime = runtime_intent[1]['owned']
        if not installation.exists() or (own_runtime and (
                not (installation / 'bin/python').is_file() or not (installation / 'pyvenv.cfg').is_file())):
            with _runtime_effect(runtime_intent, home, 'venv creation'):
                venv.EnvBuilder(with_pip=True).create(installation)
        python = installation / 'bin' / 'python'
        if not python.is_file():
            raise ValueError(f'Existing virtual environment is incomplete: {installation}. '
                             'Preserve it for review and choose another --home, or repair that environment.')
        from tools.update import install_skill_bundle, record_package_inputs, _checkout_files, _packaging_inputs
        with _runtime_effect(runtime_intent, home, 'package installation'):
            pip_ready = subprocess.run([str(python), '-I', '-m', 'pip', '--version'], capture_output=True, text=True)
            if pip_ready.returncode:
                _install_command([str(python), '-I', '-m', 'ensurepip', '--upgrade'])
            with _package_source(source) as package_source:
                packaged_inputs = {**_checkout_files(package_source), **_packaging_inputs(package_source)}
                if packaged_inputs != runtime_intent[1]['inputs']:
                    raise ValueError('Package inputs changed after the original installation intent; restore them before retrying.')
                package = str(package_source) + '[ui,mcp]'
                _install_command([str(python), '-I', '-m', 'pip', 'install', '--upgrade', package])
                record_package_inputs(home, package_source)
            # pip derives entry-point mode from its umask; repair only reviewed owned launchers.
            launcher_repair = launcher_permission_plan(home)
            if launcher_repair is not None:
                repair_launcher_permissions(home, launcher_repair)
        if not environment.exists():
            body = (source / 'maven_resources' / 'templates' / 'environment.example').read_bytes()
            fd = os.open(environment, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
        install_skill_bundle(home, source, ready_roots, plan, retired_skills)
        activation = _activation(home, ca_bundle)
        # Skill and activation writes already have exact, per-file receipts; never re-inventory them.
        record_installation(home, source, [installation] if own_runtime else [], [],
                            cache_roots=new_caches, skill_roots=roots)
        from tools.uninstall.common import save_private
        runtime_intent[1]['completed'] = True
        save_private(runtime_intent[0], runtime_intent[1])
        # Journal exact ownership before mutation: interruption leaves either an
        # absent entry or a safely removable exact entry, never an orphan.
        for change in integrations:
            apply_reviewed_integration(home, source, change)
        retire_skills(home, retired_skills)
        finish_reinstallation(home, completed_marker)
        installation_locks.close()
        if current() is not None:
            stage('Local completion')
        _notify(f'\nLocal tools installed in {installation}. Private settings: {environment}')
        _notify('In each new terminal, activate this installation (no shell profiles were modified):')
        for command in activation:
            _notify(f'  {command}')
        if roots or integrations:
            _notify('Selected harness targets processed; conflicting owner files are preserved.')
            for conflict in skill_conflicts:
                _notify(f'Preserved skill conflict: {conflict["target"]}: {conflict["reason"]}')
            for change in integrations:
                if change.receipt and change.receipt.get('conflict'):
                    _notify(f'Preserved integration conflict: {change.path}: {change.receipt["conflict"]}')
            if staging:
                _notify(f'Inactive integration staged under {integration_home}; live harness configuration unchanged. '
                        'Review and install against the selected profile after affected harnesses stop.')
            else:
                _notify('Restart affected harnesses to load local project orientation and the maven skill.')
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
                       [str(change.path) for change in integrations if change.before != change.after
                        and not (change.receipt and change.receipt.get('conflict'))] +
                       [str(target) for target, _ in retired_skills])
            conflicts = [*skill_conflicts, *(
                {'target': str(change.path), 'status': 'conflict', 'reason': change.receipt['conflict']}
                for change in integrations if change.receipt.get('conflict'))]
            handoff['integration'] = {
                'status': ('partial' if changed else 'conflict') if conflicts else
                    'written' if changed else 'unchanged' if selected_harnesses else 'skipped',
                'selected': selected_harnesses, 'written': changed, 'targets': conflicts,
                'reconciled': [saved['intent']['target'] for saved in pending_skills],
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
