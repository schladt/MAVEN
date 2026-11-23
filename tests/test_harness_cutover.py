"""Local harness bootstrap and receipt-scoped retirement of historical native hooks."""
import json
import os
import shlex
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.harness import (
    HARNESS_PATHS, LIFECYCLE_PATHS, TURN_PATHS, apply_integration, apply_reviewed_integration, integration_plan,
    validate_default_profiles,
)
from tools.uninstall.common import read_private
from tools.uninstall.local import MANIFEST, record_installation


class HarnessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.user = Path(self.directory.name).resolve()
        self.home = self.user / "instance with spaces ' and ; characters"
        self.home.mkdir(mode=0o700)
        self.enterContext(patch.dict(os.environ, {'MAVEN_HOME': str(self.home)}))
        self.source = Path(__file__).resolve().parents[1]
        self.home_patch = patch.object(Path, 'home', return_value=self.user)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def apply(self, harnesses, **options):
        changes = integration_plan(self.home, harnesses, user_home=self.user, **options)
        for change in changes:
            apply_reviewed_integration(self.home, self.source, change)
        return changes

    def legacy(self, harness, *, modified=False):
        """Seed an old installation receipt, independent of today's configuration planner."""
        path = self.user / LIFECYCLE_PATHS[harness]
        path.parent.mkdir(parents=True, exist_ok=True)
        if harness == 'omp':
            value = '// old shutdown extension'
            path.write_text(value + ('\n// owner edit' if modified else ''))
            fmt = 'file'
        else:
            start, end = ('sessionStart', 'sessionEnd') if harness == 'copilot' else ('SessionStart', 'SessionEnd')
            value = {start: [{'command': 'old startup'}], end: [{'command': 'old shutdown'}]}
            document = {'version': 1, 'theme': 'owner',
                        'hooks': {start: [*value[start], {'command': 'keep'}], end: value[end]}}
            if modified:
                document['hooks'][end] = [{'command': 'owner replacement'}]
            path.write_text(json.dumps(document))
            fmt = 'json-hooks'
        path.chmod(0o600)
        record_installation(self.home, self.source, [], [], integrations=[{
            'path': str(path), 'format': fmt, 'key': 'maven-lifecycle', 'harness': harness,
            'target': 'lifecycle', 'value': value, 'created': harness == 'omp'}])
        return path

    def previous_turn(self, harness, *, base=None, profile=None, target=None):
        """A historical registration and its receipt; no retired program/template is needed."""
        base = base or self.user
        profile = profile or tuple(base / p for p in HARNESS_PATHS[harness][:2])
        target = target or base / TURN_PATHS[harness]
        target.parent.mkdir(parents=True, exist_ok=True)
        receipt = {'path': str(target), 'key': 'maven-turn', 'harness': harness,
                   'target': 'adapter', 'profile': list(map(str, profile)), 'created': not target.exists()}
        if harness == 'omp':
            body = '// previously installed extension for ' + json.dumps(str(self.home)) + '\n'
            target.write_text(body)
            receipt.update(format='file', value=body)
        else:
            events = {
                'claude': ('UserPromptSubmit', 'PostToolUse', 'Stop'),
                'codex': ('UserPromptSubmit', 'PostToolUse', 'Stop'),
                'gemini': ('BeforeAgent', 'AfterTool', 'AfterAgent'),
                'copilot': ('userPromptSubmitted', 'postToolUse', 'agentStop', 'preToolUse'),
            }[harness]
            document = json.loads(target.read_text()) if target.exists() else {}
            receipt.update(format='json-hooks', created_container='hooks' not in document,
                           created_version=harness == 'copilot' and 'version' not in document)
            if harness == 'copilot':
                document.setdefault('version', 1)
            hooks = document.setdefault('hooks', {})
            receipt['created_events'] = [event for event in events if event not in hooks]
            value = {}
            for event, phase in zip(events, ('begin', 'observe', 'end', 'guard')):
                cmd = shlex.join([str(self.home / 'venv/bin/python'), '-I', '-m', 'tools.turn_hook',
                                  harness, phase, '--home', str(self.home)])
                entry = ({'type': 'command', 'bash': cmd} if harness == 'copilot' else
                         {'hooks': [{'type': 'command', 'command': cmd}]})
                value[event] = [entry]
                hooks.setdefault(event, []).append(entry)
            target.write_text(json.dumps(document))
            receipt['value'] = value
        target.chmod(0o600)
        record_installation(self.home, self.source, [], [], integrations=[receipt])
        return target, receipt

    def test_staged_install_and_machine_removal_for_all_five_clients(self):
        from tools.uninstall import local
        stage = self.user / 'inactive'
        changes = integration_plan(self.home, HARNESS_PATHS, user_home=stage)
        self.assertFalse(stage.exists(), 'planning may not create files or directories')
        for change in changes:
            self.assertNotIn('conflict', change.receipt)
            apply_reviewed_integration(self.home, self.source, change)
        for name, (_, context, _) in HARNESS_PATHS.items():
            with self.subTest(harness=name):
                self.assertIn('Every turn', (stage / context).read_text())
                target = stage / TURN_PATHS[name]
                if name == 'gemini':
                    self.assertNotIn('hooks', json.loads(target.read_text()))
                else:
                    self.assertFalse(target.exists())
        self.assertEqual({c.receipt['target'] for c in changes}, {'config', 'context'})
        again = integration_plan(self.home, HARNESS_PATHS, user_home=stage)
        self.assertTrue(all(c.before == c.after and not c.receipt.get('conflict') for c in again))
        plan = local.plan_machine(self.home, offline=True)
        for action in plan['actions']:
            if action['kind'] in ('local_barrier', 'local_integration'):
                local.execute(action, {}, lambda: None)
        for c in changes:
            self.assertFalse(c.path.exists(), c.path)
        self.assertFalse(any((self.user / path[0]).exists() for path in HARNESS_PATHS.values()))

    def test_modified_subscription_is_preserved_while_other_exact_entries_retire(self):
        path = self.user / TURN_PATHS['claude']
        path.parent.mkdir()
        original = {'theme': 'owner', 'hooks': {'UserPromptSubmit': [], 'Stop': [
            {'hooks': [{'type': 'command', 'command': 'my-check'}]}]}}
        path.write_text(json.dumps(original))
        self.previous_turn('claude')
        correct = path.read_bytes()
        document = json.loads(correct)
        document['hooks']['Stop'][-1]['hooks'][0]['command'] = 'owner-edit'
        changed = json.dumps(document).encode()
        path.write_bytes(changed)
        attempt = self.apply(['claude'])
        self.assertEqual([c.path for c in attempt if c.receipt.get('conflict')], [path])
        after = json.loads(path.read_text())
        self.assertEqual(after['hooks'], {**original['hooks'], 'Stop': document['hooks']['Stop']})
        self.assertEqual(after['theme'], original['theme'])
        remaining, = [r for r in read_private(self.home / MANIFEST)['integrations'] if r['key'] == 'maven-turn']
        self.assertEqual(set(remaining['value']), {'Stop'})
        after['hooks']['Stop'][-1] = json.loads(correct)['hooks']['Stop'][-1]
        path.write_text(json.dumps(after))
        self.apply(['claude'])
        self.assertEqual(json.loads(path.read_text()), original)

    def test_duplicate_and_unowned_registrations_are_not_adopted(self):
        from tools.uninstall.common import save_private
        extension, unowned = self.previous_turn('omp')
        path, receipt = self.previous_turn('copilot')
        manifest = read_private(self.home / MANIFEST)
        next(item for item in manifest['integrations'] if item['path'] == str(extension))['owned'] = False
        save_private(self.home / MANIFEST, manifest)
        document = json.loads(path.read_text())
        event = 'agentStop'
        document['hooks'][event] += receipt['value'][event]
        document['editor'] = 'owner'
        path.write_text(json.dumps(document))
        changes = self.apply(['omp', 'copilot'])
        self.assertEqual({c.path for c in changes if c.receipt.get('conflict')}, {extension, path})
        self.assertEqual(extension.read_text(), unowned['value'])
        self.assertEqual(json.loads(path.read_text()), {'editor': 'owner', 'version': 1,
                                                       'hooks': {event: document['hooks'][event]}})

    def test_saved_launcher_accepts_only_a_matching_recorded_legacy_destination(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        import install
        config, context = self.user / 'separate/mcp.json', self.user / 'separate/AGENTS.md'
        path, receipt = self.previous_turn('omp', profile=(config, context),
                                           target=self.user / 'separate/extensions/maven-turn.js')
        arguments = ['--local-only', '--home', str(self.home), '--harness', 'omp',
                     '--harness-config', f'omp={config}', '--harness-context', f'omp={context}',
                     '--skill-dir', str(self.user / 'separate/skills'), '--harness-adapter']
        with patch.object(install, '_prerequisites'), redirect_stdout(io.StringIO()) as out, \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(install.main([*arguments, f'omp={path.with_name("other.js")}']), 1)
            self.assertIn('must match', err.getvalue())
            self.assertNotIn('--approve-harness-review', out.getvalue())
            self.assertEqual(install.main([*arguments, f'omp={path}']), 1)
            self.assertIn(f'{path} | maven-turn | SHA256', out.getvalue())
        self.assertEqual(path.read_text(), receipt['value'])
        self.assertFalse(config.exists())

    def test_headless_existing_install_review_then_cutover_without_recreating_runtime(self):
        import hashlib
        import io
        from contextlib import redirect_stdout
        import install

        # Isolate subprocess/package installation; the real planner, locks,
        # manifest, selective mutations and review digest run end to end.
        runtime = self.home / 'venv'
        (runtime / 'bin').mkdir(parents=True)
        (runtime / 'pyvenv.cfg').write_text('home = fixture\n')
        (runtime / 'bin/python').touch()
        launcher = runtime / 'bin/maven-mcp'
        launcher.touch(mode=0o755)
        settings = self.home / '.env'
        settings.write_text('OWNER=unchanged\n')
        settings.chmod(0o600)
        inactive = self.user / 'inactive'
        roots = [inactive / p[2] for p in HARNESS_PATHS.values()]
        record_installation(self.home, self.source, [runtime], [], skill_roots=roots)
        hooks = {}
        for name in HARNESS_PATHS:
            target, receipt = self.previous_turn(name, base=inactive)
            hooks[target] = receipt['value']
        manifest = (self.home / MANIFEST).read_bytes()
        runtime_ino = runtime.stat().st_ino
        options = ['--local-only', '--home', str(self.home), '--refresh-integrations']
        with patch.object(install, '_prerequisites') as prerequisites, \
                patch('venv.EnvBuilder.create', side_effect=AssertionError('healthy environment replaced')), \
                patch.object(install.subprocess, 'run', return_value=Mock(returncode=0)), \
                patch.object(install, '_install_command') as pip, patch('tools.update.record_package_inputs'), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(install.main(options), 1)
            review = output.getvalue()
            pip.assert_not_called()
            self.assertEqual((self.home / MANIFEST).read_bytes(), manifest)
            for target, value in hooks.items():
                raw = value.encode() if isinstance(value, str) else json.dumps(
                    value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
                self.assertIn(f'{target} | maven-turn | SHA256 {hashlib.sha256(raw).hexdigest()}', review)
            self.assertNotIn('previously installed extension', review)
            self.assertNotIn('tools.turn_hook', review)
            token = review.split('--approve-harness-review ')[-1].split()[0]
            self.assertEqual(install.main([*options, '--approve-harness-review', token]), 0)
            prerequisites.assert_called_with(require_aws=False)
            pip.assert_called_once()
        self.assertEqual(runtime.stat().st_ino, runtime_ino)
        self.assertEqual(settings.read_text(), 'OWNER=unchanged\n')
        for name, (config, context, skills) in HARNESS_PATHS.items():
            self.assertTrue((inactive / config).exists())
            self.assertTrue((inactive / context).exists())
            self.assertTrue((inactive / skills / 'maven/SKILL.md').exists())
            self.assertFalse((self.user / config).exists())
            if name == 'gemini':
                self.assertNotIn('hooks', json.loads((inactive / config).read_text()))
            else:
                self.assertFalse((inactive / TURN_PATHS[name]).exists())

    def test_native_drift_inventory_is_independent_of_pointer_and_skill_drift(self):
        from tools.harness import SKILL_FILES, skill_plan
        from tools.update import apply_owned_skills, local_report
        custom = self.user / 'inactive custom profile'
        config, pointer = custom / 'mcp.json', custom / 'AGENTS.md'
        extension, skills = custom / 'extensions/maven-turn.js', custom / 'skills'
        plan = skill_plan([skills], self.home)
        apply_owned_skills(self.home, self.source, plan, [p for p, _, _ in plan])
        self.apply(['omp'], paths={'omp': (config, pointer)})
        self.previous_turn('omp', profile=(config, pointer), target=extension)
        self.apply(['claude'], guidance='old guidance')
        drift = {extension: 'owner extension', pointer: 'owner pointer', skills / SKILL_FILES[0]: 'owner skill'}
        for path, value in drift.items():
            path.write_text(value)
        with patch('tools.update._git', return_value={}):
            report = local_report(self.home, self.source)
        self.assertEqual(set(item['target'] for item in report['preserved']), {str(p) for p in drift})
        self.assertIn(str(self.user / HARNESS_PATHS['claude'][1]), report['integration_changes'])
        self.assertEqual({(i['component'], i['status']) for i in report['integration_inventory']
                          if i['harness'] == 'omp'}, {('retired-hook', 'conflict'), ('config', 'current'),
                                                     ('context', 'conflict')})
        self.assertEqual({p: p.read_text() for p in drift}, drift)
        self.assertFalse((self.user / '.omp').exists())

    def test_foreign_hooks_and_disabled_policies_are_never_adopted_or_changed(self):
        codex = self.user / HARNESS_PATHS['codex'][0]
        codex.parent.mkdir()
        codex.write_text('[features]\nhooks = false\n')
        for name in ('claude', 'gemini', 'copilot'):
            path = self.user / TURN_PATHS[name]
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'claude': {'disableAllHooks': True}, 'gemini': {
                'hooksConfig': {'enabled': False}}, 'copilot': {'version': 1, 'hooks': {}}}[name]))
        other = self.user / '.omp/agent/extensions/copied.js'
        other.parent.mkdir(parents=True)
        other.write_text('run("tools.turn_hook")')
        changes = self.apply(HARNESS_PATHS)
        self.assertFalse(any(c.receipt.get('conflict') for c in changes))
        self.assertEqual(other.read_text(), 'run("tools.turn_hook")')
        self.assertFalse((self.user / TURN_PATHS['omp']).exists())
        self.assertFalse((self.user / TURN_PATHS['codex']).exists())
        self.assertTrue(json.loads((self.user / TURN_PATHS['claude']).read_text())['disableAllHooks'])
        self.assertNotIn('hooks', json.loads((self.user / TURN_PATHS['gemini']).read_text()))

    def test_uninstall_removes_intact_mcp_but_preserves_modified_native_group(self):
        from tools.uninstall import local
        self.apply(['gemini'])
        self.previous_turn('gemini')
        path = self.user / TURN_PATHS['gemini']
        document = json.loads(path.read_text())
        document['hooks']['AfterAgent'][0]['hooks'][0]['command'] = 'owner-custom'
        path.write_text(json.dumps(document))
        plan = local.plan_machine(self.home, offline=True)
        self.assertTrue(any(item['path'] == str(path) and 'maven-turn' in item['reason']
                            for item in plan['retained']))
        local.execute(next(a for a in plan['actions'] if a['kind'] == 'local_barrier'), {}, lambda: None)
        action = next(a for a in plan['actions'] if a['kind'] == 'local_integration' and a['target'] == str(path))
        local.execute(action, {}, lambda: None)
        self.assertEqual(json.loads(path.read_text()), {'hooks': document['hooks']})

    def test_cutover_removes_exact_owned_hooks_and_keeps_owner_entries(self):
        # Several clients host lifecycle, turn, MCP and/or status line in one file.
        for name in HARNESS_PATHS:
            self.legacy(name)
            self.previous_turn(name)
        changes = self.apply(HARNESS_PATHS, statusline=True)
        self.assertFalse(any(change.receipt.get('conflict') for change in changes))
        self.assertEqual(sum(bool(c.receipt.get('retired')) for c in changes), 2 * len(HARNESS_PATHS))
        manifest = read_private(self.home / MANIFEST)
        self.assertFalse(any(r['key'] in ('maven-turn', 'maven-lifecycle') for r in manifest['integrations']))
        for name in ('omp', 'copilot'):
            self.assertFalse((self.user / TURN_PATHS[name]).exists())
        for name, relative in LIFECYCLE_PATHS.items():
            path = self.user / relative
            if name == 'omp':
                self.assertFalse(path.exists())
                continue
            document = json.loads(path.read_text())
            start = 'sessionStart' if name == 'copilot' else 'SessionStart'
            self.assertEqual(document['hooks'][start], [{'command': 'keep'}])
            self.assertNotIn('sessionEnd' if name == 'copilot' else 'SessionEnd', document['hooks'])
            self.assertEqual(document['theme'], 'owner')
            if name == 'claude':
                self.assertIn('statusLine', document)
            if name == 'gemini':
                self.assertIn('maven', document['mcpServers'])
        self.assertNotIn('maven-lifecycle', [r['key'] for r in read_private(self.home / MANIFEST)['integrations']])
        self.assertTrue(all(change.before == change.after for change in self.apply(HARNESS_PATHS, statusline=True)))

    def test_modified_or_shared_hooks_survive_upgrade(self):
        modified = self.legacy('omp', modified=True)
        shared = self.legacy('codex')
        before = [modified.read_bytes(), shared.read_bytes()]
        with patch('tools.uninstall.local._integration_references', return_value=({str(shared)}, False)):
            changes = self.apply(['omp', 'codex'])
        self.assertEqual({c.path for c in changes if c.receipt.get('conflict')}, {modified, shared})
        self.assertEqual(before, [modified.read_bytes(), shared.read_bytes()])

    def test_retirement_rechecks_bytes_after_review(self):
        path = self.legacy('omp')
        plan = integration_plan(self.home, ['omp'], user_home=self.user)
        retirement = next(c for c in plan if c.receipt.get('retired'))
        path.write_text('owner changed this after review')
        with self.assertRaisesRegex(ValueError, 'changed since review'):
            apply_reviewed_integration(self.home, self.source, retirement)
        self.assertEqual(path.read_text(), 'owner changed this after review')

    def test_staging_does_not_retire_live_hook(self):
        path = self.legacy('omp')
        changes = integration_plan(self.home, ['omp'], user_home=self.user / 'inactive')
        self.assertFalse(any(c.path == path for c in changes))
        for change in changes:
            apply_reviewed_integration(self.home, self.source, change)
        # The live hook receipt still belongs to the old live profile, including
        # on a later update which recovered the staged destinations from receipts.
        from tools.harness import installed_targets
        recorded = installed_targets(self.home)
        changes = integration_plan(self.home, ['omp'], paths=recorded['paths'])
        self.assertFalse(any(c.path == path for c in changes))
        self.assertTrue(path.exists())

    def test_no_owner_memory_or_session_lifecycle_entrypoints(self):
        import tomllib
        package = tomllib.loads((self.source / 'pyproject.toml').read_text())
        scripts = set(package['project']['scripts'])
        self.assertFalse(scripts & {'maven-memory', 'maven-session-hook', 'maven-workspace', 'maven-start-work',
                                    'maven-checkpoint', 'maven-stop-work', 'maven-workflow'})
        self.assertTrue({'maven-setup', 'maven-mcp', 'maven-secrets', 'maven-uninstall'} <= scripts)

    def test_owned_guidance_refresh_preserves_profile_trust_and_retires_maintenance_skill(self):
        from tools.harness import retired_skill_plan, retire_skills, skill_plan
        from tools.update import apply_owned_skills
        self.apply(['claude'], ca_bundle=self.user / 'owner-ca.pem', guidance='Previous startup')
        context = self.user / HARNESS_PATHS['claude'][1]
        context.write_text('Owner context\n' + context.read_text())
        config = (self.user / HARNESS_PATHS['claude'][0]).read_bytes()
        self.apply(['claude'])
        self.assertEqual((self.user / HARNESS_PATHS['claude'][0]).read_bytes(), config)
        self.assertTrue(context.read_text().startswith('Owner context\n'))
        self.assertIn('Every turn', context.read_text())
        self.assertNotIn('Previous startup', context.read_text())
        root = self.user / HARNESS_PATHS['claude'][2]
        old = root / 'maven-maintenance/SKILL.md'
        old.parent.mkdir(parents=True)
        old.write_text('obsolete owned skill')
        old.chmod(0o600)
        record_installation(self.home, self.source, [old], [old])
        retiring = retired_skill_plan(self.home, [root])
        writes = skill_plan([root], self.home)
        apply_owned_skills(self.home, self.source, writes[:-1], [path for path, _, _ in writes[:-1]])
        with self.assertRaises(ValueError):
            retire_skills(self.home, retiring)
        self.assertTrue(old.exists())
        target = writes[-1][0]
        apply_owned_skills(self.home, self.source, writes[-1:], [target])
        with patch('tools.uninstall.local._shared_skills', return_value=({str(target)}, False)):
            with self.assertRaises(ValueError):
                retire_skills(self.home, retiring)
        self.assertTrue(old.exists())
        retire_skills(self.home, retiring)
        self.assertFalse(old.exists())
        receipt = read_private(self.home / MANIFEST)
        self.assertNotIn(str(old), receipt['owned'])
        self.assertNotIn(str(old), receipt['skill_references'])

    def test_recorded_refresh_includes_every_custom_profile_without_touching_defaults(self):
        from tools.harness import installed_targets, recorded_integration_plan
        custom = self.user / 'inactive profile'
        for profile in ('first', 'second'):
            config, context = custom / profile / 'config.toml', custom / profile / 'guidance.md'
            self.apply(['codex'], paths={'codex': (config, context)}, guidance='old guidance')
            self.previous_turn('codex', profile=(config, context), target=config.parent / 'hooks.json')
        targets = installed_targets(self.home)
        self.assertEqual(targets['unresolved'], [])
        changes = recorded_integration_plan(self.home, targets, guidance='new guidance')
        self.assertEqual({c.path for c in changes if c.receipt.get('retired')},
                         {custom / p / 'hooks.json' for p in ('first', 'second')})
        for change in changes:
            apply_reviewed_integration(self.home, self.source, change)
        for profile in ('first', 'second'):
            self.assertIn('new guidance', (custom / profile / 'guidance.md').read_text())
            self.assertFalse((custom / profile / 'hooks.json').exists())
        self.assertFalse((self.user / '.codex').exists())

    def test_custom_profile_requires_explicit_configuration(self):
        for harness, variable in (('omp', 'OMP_PROFILE'), ('claude', 'CLAUDE_CONFIG_DIR'),
                                  ('codex', 'CODEX_HOME'), ('copilot', 'COPILOT_HOME'), ('gemini', 'GEMINI_CLI_HOME')):
            with self.subTest(harness=harness), patch.dict(os.environ, {variable: 'custom'}), self.assertRaises(ValueError):
                validate_default_profiles([harness])

    def test_conflicting_harness_does_not_suppress_other_selected_harness(self):
        path = self.user / HARNESS_PATHS['claude'][0]
        original = json.dumps({'mcpServers': {'maven': {'command': 'owner-command'}}})
        path.write_text(original)
        path.chmod(0o600)
        changes = integration_plan(self.home, ['claude', 'omp'], user_home=self.user)
        self.assertTrue(any(change.receipt.get('conflict') for change in changes))
        for change in changes:
            apply_integration(change)
        self.assertEqual(path.read_text(), original)
        self.assertTrue((self.user / HARNESS_PATHS['omp'][0]).exists())

    def test_recorded_custom_destinations_are_exact_and_legacy_unknowns_never_default(self):
        from tools.harness import installed_targets
        from tools.uninstall.common import save_private
        config, context = self.user / 'custom/mcp.json', self.user / 'custom/policy.txt'
        changes = integration_plan(self.home, ['claude'], paths={'claude': (config, context)})
        for change in changes:
            record_installation(self.home, self.source, [], [], integrations=[change.receipt])
            apply_integration(change)
        recorded = installed_targets(self.home)
        self.assertEqual(recorded['paths'], {'claude': (config, context)})
        self.assertEqual(recorded['unresolved'], [])
        manifest = read_private(self.home / MANIFEST)
        for item in manifest['integrations']:
            item.pop('harness', None)
            item.pop('target', None)
        save_private(self.home / MANIFEST, manifest)
        recorded = installed_targets(self.home)
        self.assertEqual(recorded['paths'], {})
        self.assertEqual(set(recorded['unresolved']), {str(config), str(context)})
        self.assertFalse((self.user / '.claude.json').exists())

    def test_owned_legacy_status_command_migrates_without_adopting_owner_changes(self):
        from tools.harness import _statusline_change
        target = self.user / '.claude/settings.json'
        target.parent.mkdir()
        old = {'type': 'command', 'command': str(self.home / 'venv/bin/maven-statusline')}
        target.write_text(json.dumps({'statusLine': old, 'theme': 'owner'}))
        target.chmod(0o600)
        record_installation(self.home, self.source, [], [], integrations=[{
            'path': str(target), 'format': 'json-setting', 'key': 'statusLine',
            'value': old, 'created': False}])
        with patch('tools.uninstall.local._integration_references', return_value=(set(), False)):
            change = _statusline_change(self.home, target)
        self.assertIn('MAVEN_HOME=', change.receipt['value']['command'])
        apply_integration(change)
        self.assertEqual(json.loads(target.read_text())['theme'], 'owner')
        target.write_text(json.dumps({'statusLine': {'command': 'owner-command'}}))
        self.assertTrue(_statusline_change(self.home, target).receipt.get('conflict'))

    def test_tui_upgrades_legacy_layouts_and_second_review_is_noop(self):
        from maven_resources import resource_path
        from tools.tui.harness import configure_harnesses
        launcher = self.home / 'venv/bin/maven-mcp'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/fixture/python\n')
        launcher.chmod(0o755)
        roots = {self.user / value[2] for value in HARNESS_PATHS.values()} | {self.user / 'custom skills'}
        previous = []
        for root in roots:
            for name in ('maven', 'maven-reconcile', 'maven-update', 'maven-secrets', 'maven-maintenance'):
                target = root / name / 'SKILL.md'
                target.parent.mkdir(parents=True)
                target.write_text('installer-owned ' + name)
                target.chmod(0o600)
                previous.append(target)
            (root / 'maven-update/owner-note').write_text('not installer-owned')
        record_installation(self.home, self.source, previous, previous)
        ui = Mock()
        ui.form.return_value = dict.fromkeys(HARNESS_PATHS, True)
        ui.review.return_value = True
        with patch.dict(os.environ, {}, clear=True):
            result = configure_harnesses(ui, self.home)
            self.assertEqual(result['status'], 'written', result)
            for root in roots:
                self.assertEqual(list(root.rglob('SKILL.md')), [root / 'maven/SKILL.md'])
                for leaf in ('SKILL.md', 'references/project.md', 'references/reconcile.md',
                             'references/secrets.md', 'references/update.md'):
                    self.assertEqual((root / 'maven' / leaf).read_bytes(),
                                     resource_path('templates/skills/maven/' + leaf).read_bytes())
                self.assertEqual((root / 'maven-update/owner-note').read_text(), 'not installer-owned')
            self.assertEqual(configure_harnesses(ui, self.home)['status'], 'unchanged')
            ui.review.assert_called_once()

    def test_skill_conflict_names_the_file_and_preserves_its_bytes(self):
        from tools.harness import skill_plan
        root = self.user / 'custom skills'
        target = root / 'maven/SKILL.md'
        target.parent.mkdir(parents=True)
        target.write_text('owner skill')
        target.chmod(0o600)
        with self.assertRaises(ValueError):
            skill_plan([root], self.home)
        conflicts = []
        plan = skill_plan([root], self.home, conflicts=conflicts)
        self.assertEqual([item['target'] for item in conflicts], [str(target)])
        self.assertEqual(plan, [])
        self.assertEqual(target.read_text(), 'owner skill')

    def test_tui_reviews_and_reconciles_pending_skill_receipt_even_when_bytes_match(self):
        import base64
        from tools.tui.harness import configure_harnesses
        from tools.uninstall.common import save_private
        launcher = self.home / 'venv/bin/maven-mcp'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/fixture/python\n')
        launcher.chmod(0o755)
        ui = Mock()
        ui.form.return_value = {name: name == 'omp' for name in HARNESS_PATHS}
        ui.review.return_value = True
        with patch.dict(os.environ, {}, clear=True):
            configure_harnesses(ui, self.home)
        target = self.user / HARNESS_PATHS['omp'][2] / 'maven/SKILL.md'
        pending = self.home / 'runtime/skill-installations/original.json'
        save_private(pending, {'schema': 1, 'home': str(self.home), 'source': str(self.source),
                              'target': str(target), 'body': base64.b64encode(target.read_bytes()).decode(),
                              'expected': None, 'completed': False, 'skill_reference': True})
        with patch.dict(os.environ, {}, clear=True):
            result = configure_harnesses(ui, self.home)
        self.assertEqual(ui.review.call_count, 2)
        self.assertIn('Resume exact journaled local file installation', ui.review.call_args.args[1])
        self.assertTrue(read_private(pending)['completed'])
        self.assertIn({'target': str(target), 'status': 'reconciled'}, result['targets'])



if __name__ == '__main__':
    unittest.main()
