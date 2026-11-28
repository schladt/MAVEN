"""Automatic reads/captures use real private project stores, without a harness, Git or cloud."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools.home import save_home
from tools.memory.boundary import save_private
from tools.project import ProjectError
from tools.project_switch import barrier_path
from tools.reconcile_records import make_change
from tools.reconcile_local import LocalStore, StaleView
from tools.reconcile_redaction import Replica
from tools.setup_common import SetupPending
from tools.turn_memory import LocalApplication, capture, consult


def uid():
    return str(uuid4())


def preference(scope, summary, **extra):
    return {'title': 'Owner preference', 'scope': scope, 'summary': summary, **extra}


class TurnMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / 'installation'
        self.home.mkdir(mode=0o700)
        self.app = LocalApplication(home=self.home)
        self.ident = {'instance_id': uid(), 'recovery_epoch': uid()}
        self.project = self.bind(self.base / 'project')
        self.nexus = self.bind(self.base / 'nexus')
        self.choose_home(self.nexus)
        self.untracked = self.base / 'ordinary'
        self.untracked.mkdir()

    def bind(self, root, project_id=None, **identity):
        root.mkdir(mode=0o700, exist_ok=True)
        key = project_id or uid()
        save_private(root, '.maven/project.json', {'schema': 2, 'root': str(root), 'project_id': key,
                                                   **self.ident, **identity})
        return LocalStore(root, key)

    def choose_home(self, store, **identity):
        save_home(self.home, root=store.root, project_id=store.project_id, **(self.ident | identity),
                  label='Nexus', consent={'origin': 'owner', 'excerpt': 'Use Nexus as Home.'})

    def seed(self, store, kind, data, record_id=None):
        # Model existing v1 records on disk independently of the v2 authoring boundary.
        token = store.inventory()['token']
        key = record_id or (store.brief_id if kind == 'brief' else uid())
        entry = make_change(store.lookup(key, token), kind, data, 'fixture import', record_id=key)
        store.apply([entry], token, strict_current=False)
        return key

    def write(self, view, *, cwd=None, kind='risk', data=None, **options):
        return capture(self.app, cwd or self.project.root, **({'project_id': view['project_id'],
                       'use_home': view['use_home'], 'expected_token': view['token'], **options}),
                       kind=kind, data={'state': 'open', 'title': 'Fixture risk'} if data is None else data, reason='Observed')

    def test_profile_requires_configured_home_not_a_matching_project_uuid(self):
        from tools.reconcile_records import profile_id
        clone = self.bind(self.base / 'other-copy-of-home', self.nexus.project_id)
        view = consult(self.app, clone.root)
        with self.assertRaisesRegex(ValueError, 'configured Home'):
            self.write(view['active'], cwd=clone.root, kind='profile', data={'body': 'Unrelated copy.'})
        with self.assertRaisesRegex(ValueError, 'reserved'):
            self.write(view['nexus'], cwd=clone.root, kind='profile', record_id=uid(), data={'body': 'Wrong ID.'})
        saved = self.write(view['nexus'], cwd=clone.root, kind='profile', data={'body': 'Owner-stated background.'})
        self.assertEqual(saved['id'], profile_id(self.nexus.project_id))
        self.assertFalse(clone.inventory()['items'])

    def test_home_reference_authored_and_corrected_from_another_project(self):
        other = self.seed(self.nexus, 'entity', {'type': 'instrument', 'name': 'Meter'})
        home = consult(self.app, self.project.root)['nexus']
        data = {'name': 'Instruments', 'read_when': 'Before calibrating', 'state': 'active',
                'as_of': '2026-11-27', 'body': 'Fixture background.'}
        saved = self.write(home, kind='reference', data=data)
        result = consult(self.app, self.project.root)
        self.assertEqual([r['id'] for r in result['nexus']['references']], [saved['id']])
        self.assertFalse(result['active']['references'])
        with self.assertRaises(ProjectError):
            self.write(result['nexus'], kind='reference', data=data, record_id=other)
        self.write(result['nexus'], kind='reference', data={**data, 'state': 'retired'}, record_id=saved['id'])
        self.assertFalse(consult(self.app, self.project.root)['nexus']['references'])

    def test_footer_label_is_bound_to_project_and_epoch_not_checkout_name(self):
        from tools.project import save_project_name
        binding = {**self.ident, 'project_id': self.project.project_id}
        for identity, expected in ((binding, 'maven-code'),
                                   ({**binding, 'recovery_epoch': uid()}, self.project.project_id),
                                   ({**binding, 'project_id': uid()}, self.project.project_id)):
            save_project_name(self.project.root, identity, 'maven-code')
            context = consult(self.app, self.project.root)
            self.assertEqual(context['active']['label'], expected)
            self.assertEqual(context['nexus']['label'], 'Nexus')

    def test_clipboard_only_fresh_project_consults_manual_commit_preference_without_git_or_copy(self):
        key = self.seed(self.nexus, 'preference', preference('nexus', 'I commit manually. Provide the message only.'))
        nested = self.project.root / 'src'
        nested.mkdir()
        with patch('subprocess.run', side_effect=AssertionError('must not invoke Git or a shell')), \
                patch('socket.socket.connect', side_effect=AssertionError('must stay offline')):
            result = consult(self.app, nested)
        self.assertEqual(result['status'], 'consulted')
        self.assertFalse(result['cloud_checked'])
        self.assertEqual(result['nexus']['preferences'], [{'id': key, 'scope': 'nexus',
                                                           'summary': 'I commit manually. Provide the message only.'}])
        self.assertFalse(result['active']['preferences'])
        self.assertEqual(self.project.inventory()['items'], [])
        self.assertFalse((self.project.root / '.git').exists())
        self.assertFalse((self.home / 'runtime/sessions.json').exists())

    def test_scoped_read_excludes_unrelated_home_material_and_displaced_wording(self):
        key = self.seed(self.nexus, 'preference', preference('nexus', 'old wording not for display'))
        self.seed(self.nexus, 'preference', preference('nexus', 'Use reviewed destination',
                                                   commentary='extra private field'), key)
        self.seed(self.nexus, 'brief', {'purpose': 'private Home brief'})
        self.seed(self.nexus, 'preference', preference('project', 'private Home project preference'))
        self.seed(self.nexus, 'setting', {'scope': 'nexus', 'summary': 'unrelated Home setting'})
        self.seed(self.nexus, 'note', {'summary': 'unrelated Home note'})
        self.seed(self.project, 'preference', preference('nexus', 'misplaced Nexus setting'))
        project_key = self.seed(self.project, 'preference', preference('project', 'Keep the SQLite schema'))
        self.seed(self.project, 'brief', {'purpose': 'Read this briefing'})
        skill = self.seed(self.project, 'skill', {'name': 'Verify', 'when_to_use': 'Before release',
                                                'steps': ['private procedure'], 'verification': 'Check result'})
        result = consult(self.app, self.project.root)
        self.assertEqual(result['active']['preferences'], [{'scope': 'project', 'summary': 'Keep the SQLite schema',
                                                           'id': project_key}])
        self.assertEqual(result['active']['brief']['data']['purpose'], 'Read this briefing')
        self.assertEqual(result['active']['skills'], [{'id': skill, 'name': 'Verify', 'when_to_use': 'Before release'}])
        self.assertEqual(result['nexus']['preferences'][0]['summary'], 'Use reviewed destination')
        self.assertNotIn('brief', result['nexus'])
        for private in ('extra private field', 'old wording', 'private Home', 'unrelated Home', 'misplaced Nexus',
                        'private procedure'):
            self.assertNotIn(private, json.dumps(result))

    def test_workspace_change_invalidates_even_a_shared_nexus_token(self):
        first = consult(self.app, self.project.root)
        second = self.bind(self.base / 'second')
        with self.assertRaises(StaleView):
            self.write(first['nexus'], cwd=second.root, kind='preference',
                       data=preference('nexus', 'From another workspace'))
        self.assertEqual(self.nexus.inventory()['items'], [])
        second_view = consult(self.app, second.root)
        self.assertNotEqual(first['nexus']['token'], second_view['nexus']['token'])

    def test_active_home_keeps_project_and_nexus_preferences_distinct_on_correction(self):
        view = consult(self.app, self.nexus.root)['active']
        saved = self.write(view, cwd=self.nexus.root, kind='preference', data=preference('project', 'Only here'))
        with self.assertRaises(ProjectError):
            self.write(view, cwd=self.nexus.root, expected_token=saved['token'], record_id=saved['id'],
                       kind='preference', data=preference('nexus', 'Apply everywhere instead'))
        fresh = consult(self.app, self.nexus.root)['active']
        self.assertEqual(fresh['token'], saved['token'])
        self.assertEqual(fresh['preferences'], [{'id': saved['id'], 'scope': 'project', 'summary': 'Only here'}])

    def test_active_home_reads_once_and_requires_new_projection_then_allows_untracked_capture(self):
        self.seed(self.nexus, 'brief', {'purpose': 'Only when active'})
        self.seed(self.nexus, 'preference', preference('nexus', 'global'))
        self.seed(self.nexus, 'preference', preference('project', 'scoped to the Home project'))
        separate = consult(self.app, self.project.root)
        with patch.object(LocalStore, 'context_view', autospec=True, side_effect=LocalStore.context_view) as reads:
            same = consult(self.app, self.nexus.root)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(same['nexus'], 'same_as_active')
        self.assertEqual(same['active']['brief']['data']['purpose'], 'Only when active')
        self.assertCountEqual([p['summary'] for p in same['active']['preferences']], ['global', 'scoped to the Home project'])
        untracked = consult(self.app, self.untracked)
        self.assertEqual(untracked['nexus'], 'same_as_active')
        self.assertTrue(self.write(untracked['active'], cwd=self.untracked)['changed'])
        self.assertFalse((self.untracked / '.maven').exists())
        another = self.base / 'another-untracked'
        another.mkdir()
        fresh = consult(self.app, self.untracked)
        with self.assertRaises(StaleView):
            self.write(fresh['active'], cwd=another)
        with self.assertRaises(StaleView):
            self.write(separate['nexus'], cwd=self.nexus.root)

    def test_preference_capture_requires_scope_and_writes_only_the_selected_destination(self):
        view = consult(self.app, self.project.root)
        for side, scope in [('active', None), ('active', 'nexus'), ('nexus', 'project')]:
            with self.subTest(side=side, scope=scope), self.assertRaises(ValueError):
                self.write(view[side], kind='preference', data=preference(scope, 'Requested'))
        with self.assertRaises(ProjectError):
            self.write(view['nexus'])
        with self.assertRaises(ProjectError):
            self.write(view['active'], project_id=self.nexus.project_id)
        with self.assertRaises(ValueError):
            self.write(view['active'], data={'state': 'open', 'title': 'Fixture risk', 'credentials': 'not ordinary memory'})
        self.assertFalse(self.project.inventory()['items'])
        self.assertFalse(self.nexus.inventory()['items'])
        receipt = self.write(view['nexus'], kind='preference', data=preference('nexus', 'Use the manual workflow'))
        self.assertTrue(receipt['changed'])
        self.assertEqual(self.project.inventory()['items'], [])
        fresh = consult(self.app, self.project.root)
        self.assertEqual(fresh['nexus']['token'], receipt['token'])
        self.write(fresh['active'], kind='preference', data=preference('project', 'Pin this project convention'))
        fresh = consult(self.app, self.project.root)
        self.assertEqual([p['summary'] for p in fresh['active']['preferences']], ['Pin this project convention'])
        self.assertEqual([p['summary'] for p in fresh['nexus']['preferences']], ['Use the manual workflow'])

    def test_capture_rejects_stale_retries_and_reuses_reviewed_record_id(self):
        view = consult(self.app, self.project.root)['active']
        created = self.write(view)
        self.assertTrue(created['changed'])
        with self.assertRaises(StaleView):
            self.write(view, record_id=created['id'])
        corrected = {'state': 'mitigated', 'title': 'Corrected risk'}
        updated = self.write(view, expected_token=created['token'], record_id=created['id'], data=corrected)
        self.assertEqual(updated['id'], created['id'])
        retry = self.write(view, expected_token=updated['token'], record_id=updated['id'], data=corrected)
        self.assertFalse(retry['changed'])
        self.assertEqual(retry['token'], updated['token'])
        inventory = self.project.inventory()
        self.assertEqual([r['id'] for r in inventory['items']], [created['id']])
        self.assertEqual(len(self.project.lookup(created['id'], inventory['token'])['history']), 1)

    def test_erasure_requires_exact_active_id_no_payload_and_current_scope(self):
        key = self.seed(self.project, 'note', {'summary': 'Displaced local wording'})
        other = self.seed(self.nexus, 'note', {'summary': 'Separate Nexus knowledge'})
        view = consult(self.app, self.project.root)

        def erase(side, **options):
            return capture(self.app, self.project.root, **({'project_id': side['project_id'], 'use_home': side['use_home'],
                           'expected_token': side['token'], 'reason': 'Owner selected the exact record', 'erase': True,
                           **options}))

        for options in ({}, {'record_id': uid()}, {'record_id': key, 'data': {}}, {'record_id': key, 'kind': 'note'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                erase(view['active'], **options)
        with self.assertRaises(ProjectError):
            erase(view['nexus'], record_id=other)
        self.assertFalse(self.project.inventory()['items'][0].get('erased'))
        receipt = erase(view['active'], record_id=key)
        self.assertEqual((receipt['id'], receipt['changed']), (key, True))
        with self.assertRaises(StaleView):
            erase(view['active'], record_id=key)
        retry = erase(view['active'], record_id=key, expected_token=receipt['token'])
        self.assertFalse(retry['changed'])
        record = self.project.lookup(key, self.project.inventory()['token'])
        self.assertTrue(record['record']['erased'])
        self.assertEqual(record['history'], [])
        self.assertNotIn(b'Displaced local wording', self.project.path.read_bytes())
        self.assertFalse(self.nexus.inventory()['items'][0].get('erased'))

    def test_copied_database_even_with_same_uuid_and_sqlite_revision_never_reuses_other_root_pin(self):
        original = consult(self.app, self.project.root)
        clone = self.bind(self.base / 'other-root', self.project.project_id)
        shutil.copytree(self.project.path.parent, clone.path.parent)
        self.assertEqual(self.project.inventory()['token'], clone.inventory()['token'])
        other = consult(self.app, clone.root)
        self.assertNotEqual(other['active']['token'], original['active']['token'])
        with self.assertRaises(StaleView):
            self.write(original['active'], cwd=clone.root)
        self.assertEqual(clone.inventory()['items'], [])

    def test_two_bound_roots_with_same_project_uuid_still_have_isolated_projections(self):
        checkout = self.bind(self.base / 'other-checkout', self.nexus.project_id)
        self.seed(checkout, 'brief', {'purpose': 'Independent checkout'})
        self.seed(self.nexus, 'preference', preference('nexus', 'Use the configured copy only'))
        result = consult(self.app, checkout.root)
        self.assertEqual(result['status'], 'consulted')
        self.assertEqual(result['active']['brief']['data']['purpose'], 'Independent checkout')
        self.assertFalse(result['active']['preferences'])
        self.assertEqual(result['nexus']['preferences'][0]['summary'], 'Use the configured copy only')
        self.assertNotEqual(result['nexus']['token'], result['active']['token'])
        with self.assertRaises(StaleView):
            self.write(result['active'], cwd=checkout.root, use_home=True, kind='preference',
                       data=preference('nexus', 'Wrong checkout token'))

    def test_instance_environment_is_still_authoritative_offline(self):
        self.app.env.write_text(''.join(f'MAVEN_MEMORY_{k.upper()}="{v}"\n' for k, v in self.ident.items()))
        self.app.env.chmod(0o600)
        with patch('tools.memory.environment.configured_client', side_effect=AssertionError('local only')):
            first = consult(self.app, self.project.root)
            self.assertEqual(first['status'], 'consulted')
            self.bind(self.project.root, self.project.project_id, instance_id=uid())
            changed = consult(self.app, self.project.root)
            self.assertEqual((changed['status'], changed['error']), ('unavailable', 'project_unavailable'))
            with self.assertRaises(ProjectError):
                self.write(first['active'])

    def test_broken_workspace_binding_or_switch_never_falls_back_or_enables_nexus_write(self):
        good = consult(self.app, self.untracked)
        self.assertEqual(good['status'], 'consulted')
        bound = self.base / 'damaged'
        project = self.bind(bound)
        path = bound / '.maven/project.json'
        path.write_text('{')
        for damage in ('malformed', 'orphaned store', 'switching'):
            with self.subTest(damage=damage):
                if damage == 'orphaned store':
                    path.unlink()
                    project.inventory()
                if damage == 'switching':
                    shutil.rmtree(bound / '.maven')
                    save_private(self.home, barrier_path(self.home, bound).relative_to(self.home), {'reviewed': True})
                result = consult(self.app, bound)
                self.assertEqual((result['status'], result['error']), ('unavailable', 'project_unavailable'))
                self.assertIsNone(result['active'])
                with self.assertRaises(ProjectError):
                    self.write(good['active'], cwd=bound, kind='preference', data=preference('nexus', 'Never write this'))
        self.assertEqual(self.nexus.inventory()['items'], [])

    def test_missing_invalid_undeliverable_and_foreign_cohort_home_leave_valid_project_usable(self):
        self.seed(self.project, 'brief', {'purpose': 'Project available'})
        for condition in ('missing', 'invalid', 'damaged store', 'other epoch'):
            with self.subTest(condition=condition):
                self.choose_home(self.nexus)
                if condition == 'missing':
                    (self.home / 'home.json').unlink()
                elif condition == 'invalid':
                    (self.home / 'home.json').write_text('{')
                elif condition == 'damaged store':
                    self.nexus.inventory()
                    self.nexus.path.chmod(0o644)
                else:
                    self.nexus.path.chmod(0o600)
                    foreign = {'recovery_epoch': uid()}
                    self.bind(self.nexus.root, self.nexus.project_id, **foreign)
                    self.choose_home(self.nexus, **foreign)
                result = consult(self.app, self.project.root)
                self.assertEqual((result['status'], result['error']), ('partial', 'nexus_unavailable'))
                self.assertIsNone(result['nexus'])
                self.assertEqual(result['active']['brief']['data']['purpose'], 'Project available')
                self.assertTrue(self.write(result['active'])['changed'])
                if condition != 'other epoch':
                    self.assertEqual(consult(self.app, self.untracked)['status'], 'unavailable')

    def test_learned_replica_markers_prevent_stale_capture_and_resurrection(self):
        key = self.seed(self.nexus, 'setting', preference('nexus', 'Remove this old preference', category='preference'))
        view = consult(self.app, self.project.root)
        replica = Replica(self.home, self.ident['instance_id'])
        with replica.locked():
            replica.remember([{'kind': 'project_record', 'project_id': self.nexus.project_id, 'id': key}])
        with self.assertRaises(StaleView):
            self.write(view['nexus'], record_id=key, kind='preference', data=preference('nexus', 'Revive'))
        after = consult(self.app, self.project.root)
        self.assertEqual(after['nexus']['preferences'], [])
        with self.assertRaisesRegex(ValueError, 'erased'):
            self.write(after['nexus'], record_id=key, kind='preference', data=preference('nexus', 'Revive'))
        with replica.locked():
            replica.remember([{'kind': 'project', 'id': self.project.project_id}])
        self.assertEqual(consult(self.app, self.project.root)['status'], 'unavailable')
        with self.assertRaisesRegex(SetupPending, 'retired'):
            self.write(after['nexus'], kind='preference', data=preference('nexus', 'Do not capture'))
