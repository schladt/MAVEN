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
TEMPLATE = HERE.parent / 'maven_resources/templates/harness/maven-turn.js'


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
        self.extension = self.root / 'maven-turn.mjs'
        self.extension.write_text(TEMPLATE.read_text().replace('__MAVEN_HOME_JSON__', json.dumps(str(self.home))))
        env = self.home / 'venv'
        venv.EnvBuilder(with_pip=False, symlinks=True).create(env)
        self.python = env / 'bin/python'
        self.lib = Path(subprocess.check_output([self.python, '-I', '-c',
            'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
        self.env = {'HOME': str(self.root / 'user'), 'PATH': '/usr/bin:/bin', 'TERM': 'dumb',
                    'PI_CODING_AGENT_DIR': str(self.profile), 'TMPDIR': str(self.root),
                    'PI_BASH_NO_LOGIN': '1', 'PI_NO_TITLE': '1', 'OTEL_SDK_DISABLED': 'true',
                    'AWS_EC2_METADATA_DISABLED': 'true', 'OMP_MCP_REQUIRE_READY': '1'}

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for OMP callback fixture')
    def test_native_callbacks_and_private_bridge(self):
        module = self.lib / 'tools'
        module.mkdir()
        (module / '__init__.py').touch()
        (module / 'turn_hook.py').write_text('''
import json, os, sys
from pathlib import Path
assert sys.flags.isolated and sys.argv[1] == 'omp'
phase = sys.argv[2]
home = Path(sys.argv[4]); assert home == Path.cwd() and sys.argv[3] == '--home'
assert 'PRIVATE_FIXTURE_SECRET' not in os.environ
p = json.load(sys.stdin)
with (home / 'calls.jsonl').open('a') as f: print(json.dumps(dict(phase=phase, **p)), file=f)
off, dirty, asked = [home / k for k in ('off', 'dirty', 'asked')]
if phase == 'begin':
    if p['disable']: off.touch()
    if p['enable']: off.unlink(missing_ok=True)
    dirty.unlink(missing_ok=True); asked.unlink(missing_ok=True)
    rev = (home / 'version').read_text() if (home / 'version').exists() else '1'
    unchanged = p.get('known', {}).get('proj') == rev
    view = {'project_id':'proj','token':rev,'unchanged':unchanged}
    if not unchanged: view['preferences'] = ['manual-commit preference ' + rev]
    ctx = {'turn_key':p['turn_id'], 'active':view, 'nexus':'same_as_active'}
    r = {'status':'off', 'text':'MAVEN is off for this conversation.'} if off.exists() else {'status':'consulted','text':'Header\\n' + json.dumps(ctx),
                                                'turn_key':p['turn_id'], 'context':ctx}
    if (home / 'fail').exists(): raise SystemExit(1)
elif phase == 'observe':
    dirty.touch(); r = {'status':'observed'}
else:
    if dirty.exists() and not asked.exists():
        asked.touch(); r = {'status':'incomplete','request_capture':True,'message':'Classify the actual work.'}
    else: r = {'status':'complete','request_capture':False,'footer':'fixture: verified'}
print(json.dumps(r))
''')
        # A project import or executable on PATH must not intercept the helper.
        (self.cwd / 'tools').mkdir()
        (self.cwd / 'tools/__init__.py').write_text('raise RuntimeError("wrong import root")')
        subprocess.run([shutil.which('node'), HERE / 'test_turn_omp_callbacks.mjs', self.extension,
                        self.home, self.cwd], cwd=self.cwd, env={**self.env, 'PYTHONPATH': str(self.cwd),
                        'PRIVATE_FIXTURE_SECRET': 'do-not-inherit'}, check=True, timeout=45)

    @unittest.skipUnless(os.environ.get('MAVEN_TEST_OMP_LIVE') == '1', 'opt-in real OMP qualification')
    def test_offline_binary_commit_message(self):
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
        profile_extension = self.profile / 'extensions/maven-turn.js'
        profile_extension.parent.mkdir()
        shutil.copyfile(self.extension, profile_extension)
        provider = self.root / 'fixture-provider.js'
        shutil.copyfile(HERE / 'test_turn_omp_provider.js', provider)
        (self.profile / 'config.yml').write_text('mcp:\n  shared: false\n  startupTimeoutMs: 0\n')
        (self.profile / 'mcp.json').write_text(json.dumps({'mcpServers': {'maven': {'type':'stdio',
            'command':str(self.python), 'args':['-I', '-m', 'tools.mcp.server', '--home', str(self.home)]}}}))
        run = subprocess.run([*sandbox, omp, '--trusted-extension', profile_extension, '--trusted-extension', provider,
            '--model', 'maven-fixture/commit-message', '--no-session', '--no-title', '--no-context-files',
            '--no-skills', '--no-rules', '--no-lsp', '--no-pty', '--max-time', '35', '-p', '--mode', 'json',
            'Draft only a commit message.'], cwd=self.cwd, env=self.env, text=True,
            capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr[-1500:])
        events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith('{')]
        answers = [part.get('text') for e in events if e.get('type') == 'message_end'
                   for part in e.get('message', {}).get('content', []) if isinstance(part, dict) and part.get('type') == 'text']
        self.assertTrue(any(answer.startswith('Draft: Respect manual Git commits.\nMAVEN · ')
                            and answer.count('consulted, no update') == 2 for answer in answers),
                        'Missing fixture answer: check isolated OMP model, context injection and actual MCP receipt.')
        self.assertEqual(git_state(), untouched)
        self.assertEqual((pref.inventory(), active.inventory()), before)
