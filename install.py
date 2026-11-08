#!/usr/bin/env python3
"""Install and configure MAVEN step by step using only system Python to start."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import sys

from tools.interaction import Field, OperationPaused, checkpoint, current, message, require, stage


def _notify(text, *, file=None):
    if current() is None:
        print(text, file=file)
    else:
        message(text)

# Primary discovery documentation:
# https://developers.openai.com/codex/skills
# https://code.claude.com/docs/en/skills
# https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md
# https://docs.github.com/en/copilot/concepts/customizations/agent-skills
# https://geminicli.com/docs/cli/skills/
# https://antigravity.google/docs/skills
HARNESS_PATHS = {
    'codex': Path('.agents/skills'),
    'claude': Path('.claude/skills'),
    'omp': Path('.omp/agent/skills'),
    'copilot': Path('.copilot/skills'),
    'gemini': Path('.gemini/skills'),
    'antigravity': Path('.gemini/config/skills'),
}
SKILLS = (
    'maven-setup', 'maven-connect', 'maven-skill-use',
    'maven-start-work', 'maven-checkpoint', 'maven-stop-work',
)


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


def _skill_plan(source: Path, roots: list[Path]) -> list[tuple[Path, bytes]]:
    plan = []
    for root in dict.fromkeys(roots):
        _no_symlink_parents(root)
        for name in SKILLS:
            target = root / name / 'SKILL.md'
            _no_symlink_parents(target)
            body = (source / 'maven_resources' / 'templates' / 'skills' / name / 'SKILL.md').read_bytes()
            if target.exists():
                if not target.is_file() or target.read_bytes() != body:
                    raise ValueError(f'existing skill differs; preserved without changes: {target}. '
                                     'Use --skill-dir with a separate staging directory for manual review.')
            else:
                if target.parent.exists() and any(target.parent.iterdir()):
                    raise ValueError(f'existing nonempty skill directory preserved: {target.parent}')
                plan.append((target, body))
    return plan




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
                        help='Install skills at a documented default user root; repeatable')
    parser.add_argument('--skill-dir', type=Path, action='append', default=[],
                        help='Explicit alternate skill root for a custom profile or inactive staging directory')
    parser.add_argument('--harness-stopped', action='store_true',
                        help='Confirm selected harnesses are closed before installing their skills')
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
            _options(args)
            result = _install(args, handoff)
            if result == 0 and handoff:
                body = (f'Local tools are installed in {handoff["home"]}.\n\n'
                        'Shell activation:\n' + '\n'.join(handoff['activation']) +
                        '\n\nOpen instance setup now? Decline to finish locally; activate this '
                        'installation and run maven-setup later. No cloud readiness is implied yet.')
                if not ui.review('Local installation complete', body, accept='Open instance setup'):
                    handoff.clear()
                ui.close_when_finished()
            return result

        result = run_workflow('Install MAVEN', [
            'Prerequisites', 'Installation options', 'Review installation', 'Install tools', 'Local completion',
        ], operation)
        if result or not handoff:
            return result
        # The first app is closed before the installed setup app owns the terminal.
        home = handoff['home']
        from tools.tui.bootstrap import run_terminal
        return run_terminal(
            [str(home / 'venv/bin/maven-setup'), '--home', str(home)],
            cwd=home, env=dict(os.environ, MAVEN_HOME=str(home)),
        )
    except (EOFError, KeyboardInterrupt, OperationPaused):
        _notify('Installation paused; existing private configuration is preserved.', file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _notify(f'Installation stopped: {exc}', file=sys.stderr)
        return 1


def _options(args):
    stage('Prerequisites')
    _prerequisites(require_aws=False)
    stage('Installation options')
    fields = [
        Field('home', 'Private MAVEN installation folder',
              str(args.home or Path(os.environ.get('MAVEN_HOME', '~/.maven')).expanduser())),
        Field('google', 'Install optional Google dependencies', args.google, kind='bool'),
        Field('local_only', 'Install local tools only (required without a login-capable AWS CLI)',
              False, kind='bool'),
        Field('ca', 'Optional trusted PEM CA bundle', str(args.ca_bundle or ''), required=False),
        Field('skill_dirs', 'Custom skill roots (one absolute path per line)',
              '\n'.join(map(str, args.skill_dir)), kind='multiline', required=False),
    ]
    fields.extend(Field(name, f'Install {name} skills at {Path.home() / root}',
                        name in args.harness, kind='bool') for name, root in HARNESS_PATHS.items())
    selected = require().form('Local installation options', fields,
                              description='No shell profile or existing differing skill will be replaced. Google dependencies do not authorize account access. Never enter credentials.')
    if selected is None:
        raise OperationPaused('Installation options paused.')
    args.home = Path(selected['home'])
    args.google = selected['google']
    args.local_only = selected['local_only']
    args.ca_bundle = Path(selected['ca']) if selected['ca'] else None
    args.harness = [name for name in HARNESS_PATHS if selected[name]]
    args.skill_dir = [Path(line.strip()) for line in selected['skill_dirs'].splitlines() if line.strip()]


def _install_command(command):
    if current() is None:
        return subprocess.run(command, check=True)
    # Local installation is one coherent unit through its ownership receipt. Do
    # not honor a pause between pip's mutation and recording that ownership.
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        if 'claude' in args.harness and os.environ.get('CLAUDE_CONFIG_DIR'):
            raise ValueError('CLAUDE_CONFIG_DIR is set; use --skill-dir with its skills directory instead of --harness claude.')
        if 'omp' in args.harness and any(os.environ.get(key) for key in (
            'OMP_PROFILE', 'PI_PROFILE', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR',
        )):
            raise ValueError('OMP profile/directory override is set; use --skill-dir for the chosen profile instead of --harness omp.')
        roots = [Path.home() / HARNESS_PATHS[name] for name in args.harness]
        roots.extend(path.expanduser().absolute() for path in args.skill_dir)
        deferred = False
        if roots and not args.harness_stopped:
            if current() is None:
                raise ValueError('Close the selected harnesses, then add --harness-stopped. No skills were changed.')
            if not require().confirm('Are all harnesses using these skill folders closed?'):
                deferred, roots = True, []
                _notify('Harness integration deferred; active harness files will not be changed.')
        try:
            plan = _skill_plan(source, roots)
        except ValueError as exc:
            if current() is None:
                raise
            message(str(exc))
            if not require().confirm('Continue without changing any harness skills?'):
                raise OperationPaused('Skill conflict preserved; installation paused.')
            deferred, roots, plan = True, [], []
        if current() is not None:
            stage('Review installation')
            body = '\n'.join([
                f'Private home: {home}', f'Runtime: {home / "venv"}',
                f'Settings: {home / ".env"} (existing settings preserved)',
                f'Package source: {source}', f'Extras: ui{", google" if args.google else ""}',
                f'Trusted CA: {ca_bundle or "current TLS configuration"}',
                f'Next: {"local tools only; no AWS/project setup" if args.local_only else "installed maven-setup for explicit cloud selection"}',
                'Shell profiles: unchanged',
                'Skill roots:\n' + ('\n'.join(map(str, roots)) or '(none)'),
                'New skill files:\n' + ('\n'.join(str(target) for target, _ in plan) or '(none; existing identical skills preserved)'),
            ])
            if not require().review('Approve exact local installation', body):
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
        package = str(source) + ('[ui,google]' if args.google else '[ui]')
        _install_command([str(python), '-m', 'pip', 'install', '--upgrade', package])
        if not environment.exists():
            body = (source / 'maven_resources' / 'templates' / 'environment.example').read_bytes()
            fd = os.open(environment, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
        for target, body in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            _no_symlink_parents(target)
            with target.open('xb') as stream:
                stream.write(body)
        activation = _activation(home, ca_bundle)
        owned_paths = [target for target, _ in plan]
        if own_runtime:
            owned_paths.append(installation)
        if own_activation:
            owned_paths.append(home / 'activate')
        record_installation(home, source, owned_paths,
                            [root / name / 'SKILL.md' for root in roots for name in SKILLS],
                            cache_roots=new_caches)
        finish_reinstallation(home, completed_marker)
        installation_locks.close()
        if current() is not None:
            stage('Local completion')
        _notify(f'\nLocal tools installed in {installation}. Private settings: {environment}')
        _notify('In each new terminal, activate this installation (no shell profiles were modified):')
        for command in activation:
            _notify(f'  {command}')
        if roots:
            _notify('Bootstrap and lifecycle skills installed/preserved. Start a new harness session to discover them.')
        elif deferred:
            _notify('Harness integration is still pending. After closing the harness, rerun this installer '
                  'with --local-only and the desired --harness/--skill-dir plus --harness-stopped.')
        else:
            _notify('No harness was modified; native skill installation remains optional.')
        checkpoint()
        if args.local_only:
            _notify('\nLocal-only installation complete; AWS and project setup have NOT been performed.')
            _notify('After activation, run maven-setup to configure an existing or new instance; project setup is optional.')
            return 0
        if handoff is not None:
            handoff['home'] = home
            handoff['activation'] = activation
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
