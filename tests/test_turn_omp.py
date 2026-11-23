"""OMP contracts; opt in to the real binary with MAVEN_TEST_OMP_LIVE=1."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import venv
from uuid import uuid4


HERE = Path(__file__).resolve().parent


class OmpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'MAVEN fixture home'
        self.cwd = self.root / 'widget'
        self.profile = self.root / 'user/.omp/agent'
        for d in (self.home, self.cwd, self.profile):
            d.mkdir(mode=0o700, parents=True)
        env = self.home / 'venv'
        venv.EnvBuilder(with_pip=False, symlinks=True).create(env)
        self.python = env / 'bin/python'
        self.lib = Path(subprocess.check_output([self.python, '-I', '-c',
            'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
        self.env = {'HOME': str(self.root / 'user'), 'PATH': '/usr/bin:/bin', 'TERM': 'dumb',
                    'PI_CODING_AGENT_DIR': str(self.profile), 'TMPDIR': str(self.root),
                    'PI_BASH_NO_LOGIN': '1', 'PI_NO_TITLE': '1', 'OTEL_SDK_DISABLED': 'true',
                    'AWS_EC2_METADATA_DISABLED': 'true', 'OMP_MCP_REQUIRE_READY': '1'}

    @unittest.skipUnless(os.environ.get('MAVEN_TEST_OMP_LIVE') == '1', 'opt-in real OMP qualification')
    def test_retained_context_and_before_reply_save(self):
        from tools.home import save_home
        from tools.memory.boundary import save_private
        from tools.reconcile import local_write
        from tools.reconcile_local import LocalStore

        omp = shutil.which('omp')
        self.assertIsNotNone(omp, 'omp is required for real host qualification')
        # No real provider, network, installation or ambient harness profile. Refuse
        # to run an opt-in probe without OS-enforced outbound network isolation.
        if sys.platform == 'darwin' and shutil.which('sandbox-exec'):
            deny_profiles = ' '.join(f'(subpath {json.dumps(str(Path.home() / name))})' for name in
                                    ('.omp', '.codex', '.claude', '.gemini', '.copilot', '.aws', '.maven'))
            # Developers can run this test from ~/.maven/venv. Permit only that
            # interpreter's package code read-only, never its config, keys or stores.
            protected = Path.home() / '.maven/venv'
            deps = {Path(sysconfig.get_path(k)).resolve() for k in ('purelib', 'platlib')}
            allow_packages = ''.join('(allow file-read* (subpath ' + json.dumps(str(path)) + '))'
                                     for path in deps if path.name == 'site-packages' and path.is_relative_to(protected))
            sandbox = [shutil.which('sandbox-exec'), '-p', '(version 1)(allow default)'
                       '(deny network-outbound (remote ip "*:*"))' + f'(deny file-read* file-write* {deny_profiles})' +
                       allow_packages]
        elif sys.platform.startswith('linux') and shutil.which('bwrap'):
            sandbox = [shutil.which('bwrap'), '--unshare-net', '--die-with-parent', '--ro-bind', '/', '/',
                       '--dev', '/dev', '--proc', '/proc', '--bind', str(self.root), str(self.root), '--']
        else:
            self.fail('real OMP probe requires sandbox-exec on macOS or bubblewrap on Linux')
        # Put this checkout and its test interpreter dependencies on a private
        # venv path, without installing a wheel or setting PYTHONPATH in the host.
        libs = [str(p) for p in map(Path, sys.path) if p.is_absolute() and p.is_dir()]
        (self.lib / 'fixture.pth').write_text('\n'.join(dict.fromkeys([str(HERE.parent), *libs])) + '\n')
        instance, epoch, project, nexus_project = (str(uuid4()) for _ in range(4))
        nexus = self.root / 'nexus'
        nexus.mkdir(mode=0o700)
        for root, key in ((self.cwd, project), (nexus, nexus_project)):
            save_private(root, '.maven/project.json', {'schema': 2, 'root': str(root), 'project_id': key,
                         'instance_id': instance, 'recovery_epoch': epoch})
        save_home(self.home, root=nexus, project_id=nexus_project, instance_id=instance, label='Nexus',
                  consent={'origin':'Test fixture', 'excerpt':'Use Nexus for standing preferences.'})
        pref = LocalStore(nexus, nexus_project)
        local_write(pref, pref.inventory()['token'], 'setting', {'title':'Git commits are manual by default',
                    'summary':'Leave changes uncommitted unless I explicitly ask you to make a commit.',
                    'category':'preference', 'scope':'nexus'}, 'Owner preference in fixture')
        active = LocalStore(self.cwd, project)
        before = (pref.inventory(), active.inventory())
        subprocess.run(['git', 'init', '-q'], cwd=self.cwd, env=self.env, check=True)
        def git_state():
            return {str(p.relative_to(self.cwd)): p.read_bytes() for p in (self.cwd / '.git').rglob('*') if p.is_file()}
        untouched = git_state()
        templates = HERE.parent / 'maven_resources/templates'
        shutil.copyfile(templates / 'harness/startup.md', self.profile / 'AGENTS.md')
        shutil.copytree(templates / 'skills/maven', self.profile / 'skills/maven')
        provider = self.root / 'fixture-provider.js'
        shutil.copyfile(HERE / 'test_turn_omp_provider.js', provider)
        (self.profile / 'config.yml').write_text('mcp:\n  shared: false\n  startupTimeoutMs: 0\n')
        (self.profile / 'mcp.json').write_text(json.dumps({'mcpServers': {'maven': {'type':'stdio',
            'command':str(self.python), 'args':['-I', '-m', 'tools.mcp.server', '--home', str(self.home)]}}}))
        def ask(prompt, expected, calls, *, resume=True):
            run = subprocess.run([*sandbox, omp, '--trusted-extension', provider,
                '--model', 'maven-fixture/commit-message', '--no-title', '--no-rules', '--no-lsp',
                '--no-pty', '--max-time', '35', '--session-dir', str(self.root / 'sessions'),
                *(['--continue'] if resume else []), '-p', '--mode', 'json', prompt],
                cwd=self.cwd, env=self.env, text=True, capture_output=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr[-1500:])
            events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith('{')]
            messages = [e['message'] for e in events if e.get('type') == 'message_end'
                        and e.get('message', {}).get('role') == 'assistant']
            answers = [b['text'] for m in messages for b in m['content'] if b['type'] == 'text']
            commands = [b['arguments']['command'].split(' ', 1)[0] for m in messages
                        for b in m['content'] if b['type'] == 'toolCall']
            self.assertEqual((answers, commands), ([expected], calls))

        ask('Draft only a commit message.', 'Draft: Respect manual Git commits.\nMAVEN · widget: no local change.',
            ['mcp__maven_context'], resume=False)
        ask('Should you commit it?', 'No; commits are manual.\nMAVEN · widget: no local change.', [])
        self.assertEqual((pref.inventory(), active.inventory()), before)
        ask('Remember the widget decision.', 'Recorded the widget decision.\nMAVEN · widget: saved locally.',
            ['mcp__maven_local_write'])
        self.assertEqual([(r['kind'], r['summary']) for r in active.inventory()['items']],
                         [('decision', 'Build the widget offline')])
        # Another writer advances the store while OMP retains the last write token.
        local_write(active, active.inventory()['token'], 'note', {'title':'Concurrent update'}, 'Fixture writer.')
        concurrent = active.inventory()
        ask('Record the second decision.', 'Another writer changed the project.\nMAVEN · widget: not saved.',
            ['mcp__maven_local_write'])
        self.assertEqual(active.inventory(), concurrent)
        ask('Turn off MAVEN for this session.', 'MAVEN is off for this conversation.', [])
        ask('What is two plus two?', 'Four.', [])
        self.assertEqual(git_state(), untouched)
        self.assertEqual(pref.inventory(), before[0])
