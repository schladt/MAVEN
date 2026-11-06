#!/usr/bin/env python3
"""Install MAVEN locally without logging in, deploying, or replacing harness authority."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import venv

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('MAVEN_HOME', '~/.maven')).expanduser(),
                        help='Private instance home (default MAVEN_HOME or ~/.maven)')
    parser.add_argument('--google', action='store_true', help='Include optional Google client dependencies')
    parser.add_argument('--harness', choices=tuple(HARNESS_PATHS), action='append', default=[],
                        help='Install skills at the documented default user root; custom profiles use --skill-dir')
    parser.add_argument('--skill-dir', type=Path, action='append', default=[],
                        help='Explicit alternate skill root, useful for inactive harness staging; repeatable')
    args = parser.parse_args(argv)
    try:
        if sys.platform == 'win32':
            raise ValueError('Use Windows Subsystem for Linux; native Windows is not supported')
        if sys.version_info < (3, 11):
            raise ValueError('Python 3.11 or newer is required')
        for command, purpose in (
            ('git', 'required for Git workflows; optional for --no-git memory-only projects'),
            ('aws', 'AWS CLI with login support is required for the owner interactive login flow'),
        ):
            executable = shutil.which(command)
            if executable:
                print(f'Prerequisite available: {command} ({executable})')
            else:
                print(f'Missing prerequisite: {command}: {purpose}. Install it separately before using that capability.',
                      file=sys.stderr)
        home = args.home.expanduser().absolute()
        _no_symlink_parents(home)
        source = Path(__file__).resolve().parent
        if 'claude' in args.harness and os.environ.get('CLAUDE_CONFIG_DIR'):
            raise ValueError('CLAUDE_CONFIG_DIR is set; use --skill-dir with its skills directory instead of --harness claude')
        if 'omp' in args.harness and any(os.environ.get(key) for key in (
            'OMP_PROFILE', 'PI_PROFILE', 'PI_CONFIG_DIR', 'PI_CODING_AGENT_DIR',
        )):
            raise ValueError('OMP profile/directory override is set; use --skill-dir with the active profile skills directory instead of --harness omp')
        roots = [Path.home() / HARNESS_PATHS[name] for name in args.harness]
        roots.extend(path.expanduser().absolute() for path in args.skill_dir)
        plan = _skill_plan(source, roots)
        _private_directory(home)
        environment = home / '.env'
        if environment.exists() or environment.is_symlink():
            info = environment.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('existing .env preserved: require owner-owned regular file with mode 0600')
        installation = home / 'venv'
        _no_symlink_parents(installation)
        if installation.exists() and not (installation / 'pyvenv.cfg').is_file():
            raise ValueError(f'existing non-venv target preserved: {installation}')
        if not installation.exists():
            venv.EnvBuilder(with_pip=True).create(installation)
        python = installation / 'bin' / 'python'
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
        print(f'Installed command directory: {installation / "bin"}')
        print(f'Private settings (preserved on rerun): {environment}')
        print('Add the command directory to PATH; no shell profiles were modified.')
        if home != Path.home() / '.maven':
            print(f'Set MAVEN_HOME to {home} for every MAVEN invocation.')
        if not roots:
            print('No harness was modified. Select --harness or --skill-dir explicitly to install bootstrap skills.')
        else:
            print('Only absent bootstrap skills were installed. Start a new harness session to discover them.')
        print('No login, cloud deployment, authority activation or project initialization was performed.')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Installation stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
