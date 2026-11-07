#!/usr/bin/env python3
"""Install and configure MAVEN step by step using only system Python to start."""
from __future__ import annotations

import argparse
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
SKILLS = ('maven-setup', 'maven-connect', 'maven-skill-use')


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


def _ask(label: str, default: str = '') -> str:
    suffix = f' [{default}]' if default else ''
    return input(f'{label}{suffix}: ').strip() or default


def _yes(label: str, default: bool = False) -> bool:
    while True:
        answer = _ask(f'{label} {"[Y/n]" if default else "[y/N]"}').lower()
        if not answer:
            return default
        if answer in ('y', 'yes', 'n', 'no'):
            return answer in ('y', 'yes')
        print('Enter yes or no.')


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
    print(f'Python {sys.version.split()[0]}: ready; venv and pip bootstrap available.')
    if shutil.which('git'):
        print('Git: available.')
    else:
        print('Git is not installed. Install Git for repository workflows, or choose a memory-only project.')
    aws = shutil.which('aws')
    version = None
    if aws:
        result = subprocess.run([aws, '--version'], capture_output=True, text=True, timeout=15)
        match = re.search(r'aws-cli/(\d+)\.(\d+)\.(\d+)', result.stdout + result.stderr)
        if result.returncode == 0 and match:
            version = tuple(int(part) for part in match.groups())
    if version is None or version < (2, 32, 0):
        message = ('AWS CLI v2 version 2.32.0 or newer is required for aws login. '
                   'Install/update it from https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html '
                   '(macOS/Homebrew: brew install awscli), then rerun this installer.')
        if require_aws:
            raise ValueError(message + ' Use --local-only only to install tools without configuring AWS.')
        print(f'Not configured yet: {message}')
    else:
        print(f'AWS CLI {".".join(map(str, version))}: ready.')


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
            print('Existing activation snippet preserved; use the explicit exports below for the selected CA.')
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
        if not args.local_only and not sys.stdin.isatty():
            raise ValueError('Guided setup needs an interactive terminal. Run python3 install.py in your terminal, '
                             'or explicitly choose --local-only to install unconfigured tools.')
        print('\nStep 1/4 — Check prerequisites')
        _prerequisites(require_aws=not args.local_only)
        home = args.home or Path(os.environ.get('MAVEN_HOME', '~/.maven')).expanduser()
        if not args.local_only:
            print('\nStep 2/4 — Choose local installation options')
            if args.home is None:
                home = Path(_ask('Private MAVEN installation folder', str(home)))
            if not args.google:
                args.google = _yes('Install optional Google integration dependencies?')
            if not args.harness and not args.skill_dir:
                choices = ', '.join(HARNESS_PATHS)
                print(f'Optional harness integration: {choices}. Choose none to install it later.')
                while True:
                    selection = _ask('Harness names (comma separated)', 'none').lower()
                    names = [name.strip() for name in selection.split(',')]
                    if selection == 'none':
                        break
                    if all(name in HARNESS_PATHS for name in names):
                        args.harness = list(dict.fromkeys(names))
                        break
                    print(f'Choose names from {choices}, or none.')
            if args.ca_bundle is None:
                selected = _ask('Optional trusted PEM CA bundle for a corporate VPN (Enter to keep current TLS settings)')
                args.ca_bundle = Path(selected) if selected else None
        home = home.expanduser().absolute()
        _no_symlink_parents(home)
        ca_bundle = _configure_ca(args.ca_bundle)
        source = Path(__file__).resolve().parent
        project_root = Path.cwd().resolve()
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
            if args.local_only:
                raise ValueError('Close the selected harnesses, then add --harness-stopped. No skills were changed.')
            if not _yes('Are all harnesses using these skill folders closed?'):
                deferred, roots = True, []
                print('Harness integration deferred; active harness files will not be changed.')
        try:
            plan = _skill_plan(source, roots)
        except ValueError as exc:
            if args.local_only:
                raise
            print(str(exc))
            if not _yes('Continue cloud/project setup without changing any harness skills?', default=True):
                raise
            deferred, roots, plan = True, [], []
        _private_directory(home)
        for folder in ('runtime', 'runtime/aws-login-cache', 'runtime/aws-deploy-login-cache'):
            _private_directory(home / folder)
        environment = home / '.env'
        if environment.exists() or environment.is_symlink():
            _private_file(environment)
        installation = home / 'venv'
        _no_symlink_parents(installation)
        if installation.exists() and not (installation / 'pyvenv.cfg').is_file():
            raise ValueError(f'Existing non-venv target preserved: {installation}')
        print('\nStep 3/4 — Create/reuse the virtual environment and install MAVEN')
        import venv
        if not installation.exists():
            venv.EnvBuilder(with_pip=True).create(installation)
        python = installation / 'bin' / 'python'
        if not python.is_file():
            raise ValueError(f'Existing virtual environment is incomplete: {installation}. '
                             'Preserve it for review and choose another --home, or repair that environment.')
        pip_ready = subprocess.run([str(python), '-m', 'pip', '--version'], capture_output=True, text=True)
        if pip_ready.returncode:
            subprocess.run([str(python), '-m', 'ensurepip', '--upgrade'], check=True)
        package = str(source) + ('[google]' if args.google else '')
        subprocess.run([str(python), '-m', 'pip', 'install', '--upgrade', package], check=True)
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
        print(f'\nLocal tools installed in {installation}. Private settings: {environment}')
        print('In each new terminal, activate this installation (no shell profiles were modified):')
        for command in activation:
            print(f'  {command}')
        if roots:
            print('Bootstrap skills installed/preserved. Start a new harness session to discover them.')
        elif deferred:
            print('Harness integration is still pending. After closing the harness, rerun this installer '
                  'with --local-only and the desired --harness/--skill-dir plus --harness-stopped.')
        else:
            print('No harness was modified; native skill installation remains optional.')
        if args.local_only:
            print('\nLocal-only installation complete; AWS and project setup have NOT been performed.')
            print('After activation, run maven-setup to configure an existing or new instance and your project.')
            return 0
        print('\nStep 4/4 — Configure an existing or new AWS instance and choose your project')
        result = subprocess.run(
            [str(installation / 'bin' / 'maven-setup'), '--home', str(home),
             '--project-root', str(project_root)],
            cwd=home, env=dict(os.environ, MAVEN_HOME=str(home)),
        )
        if result.returncode:
            print('Setup is not complete. After activation, run maven-setup to resume; existing state is preserved.')
        return result.returncode
    except (EOFError, KeyboardInterrupt):
        print('\nInstallation paused. Rerun python3 install.py; existing private configuration is preserved.',
              file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Installation stopped: {exc}', file=sys.stderr)
        print('If TLS verification failed on a corporate VPN, use --ca-bundle with an IT-approved PEM bundle. '
              'TLS verification must remain enabled.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
