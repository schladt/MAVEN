import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools.memory.boundary import save_private
from tools.memory.validation import request_limit
from tools.reconcile_local import LocalStore, StaleView
from tools.reconcile_records import (
    MAX_BATCH, MAX_ENVELOPE_BYTES, brief_id, canonical, make_change, profile_id,
)
from tools.reconcile_redaction import Replica
from tools.taxonomy_conversion import ConversionError, apply_mapping, main, scan, split_id


def uid():
    return str(uuid4())


def version(kind='note', data=None, previous=None, key=None):
    return make_change(previous, kind, data if data is not None else {'text': 'fixture note'},
                       'fixture correction', record_id=previous['record']['id'] if previous else key or uid())


def entity(name):
    return {'kind': 'entity', 'data': {'type': 'fixture', 'name': name}}


def ref(name, state='active'):
    return {'kind': 'reference', 'data': {'name': name, 'state': state, 'read_when': 'On request',
                                          'as_of': '2026-11-27', 'body': 'Reviewed background.'}}


def task():
    return {'kind': 'task', 'data': {'title': 'Measure', 'state': 'open', 'added_on': '2026-11-27'}}


def plan(key):
    return {'kind': 'plan', 'data': {'task_id': key, 'state': 'proposed', 'steps': ['Measure'],
                                     'completion_check': 'Read the instrument.'}}


def retype(key, spec):
    return {'op': 'retype', 'id': key, **spec}


class TaxonomyConversionTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project = uid()
        self.original = LocalStore(self.root, self.project)
        self.private = self.root / 'reviewer'
        self.private.mkdir(mode=0o700)
        self.db = self.private / 'snapshot.sqlite3'  # Does not encode the project ID.

    def copy(self, *envelopes):
        token = self.original.inventory()['token']
        for start in range(0, len(envelopes), MAX_BATCH):
            token = self.original.apply(list(envelopes[start:start + MAX_BATCH]), token, strict_current=False)['token']
        # An actual consistent SQLite copy; the conversion never receives the original root.
        self.db.touch(mode=0o600)
        with contextlib.closing(sqlite3.connect(self.original.path.as_uri() + '?mode=ro', uri=True)) as src:
            with contextlib.closing(sqlite3.connect(self.db)) as dst:
                src.backup(dst)
        return LocalStore.open_copy(self.db, self.project)

    def mapping(self, *rows):
        return {'project_id': self.project, 'rows': list(rows)}

    def command(self, *args, module=False, project=None, db=None, workspace=None):
        target = ['--workspace', str(workspace)] if workspace is not None else ['--database', str(db or self.db)]
        argv = [*target, '--project', project or self.project, *map(str, args)]
        if module:
            result = subprocess.run([sys.executable, '-m', 'tools.taxonomy_conversion', *argv],
                                    capture_output=True, text=True, check=False,
                                    env={**os.environ, 'HOME': str(self.private / 'not-a-home')})
            return result.returncode, result.stdout, result.stderr
        out, err = StringIO(), StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_module_cli_on_populated_sqlite_copy_preserves_ids_histories_and_erases_only_named_source(self):
        parent = version(previous=version(data={'text': 'Earlier private source'}), data={'text': 'Current private source'})
        parent_id = parent['record']['id']
        left = version('fact', {'text': 'Private moved wording'})
        forget = version(data={'text': 'UNIQUE erased wording for copy test'})
        left_id, forget_id = left['record']['id'], forget['record']['id']
        survivor = version(**entity('Earlier name'))
        survivor = version(previous=survivor, **entity('Current name'))
        survivor_id = survivor['record']['id']
        old_task = version('task', {'title': 'Measure', 'state': 'open'})
        decision = version('decision', {'title': 'Retain', 'state': 'accepted'})
        outcome = version('outcome', {'title': 'Measured', 'text': 'Old shape'})
        old = [parent, left, forget, survivor, old_task, decision, outcome]
        copy = self.copy(*old)
        initial = copy.inventory()['token']
        child_ref = {**ref('Private background'), 'key': 'topic'}
        child_skill = {'kind': 'skill', 'data': {'name': 'Release', 'when_to_use': 'At release', 'path': 'SKILL.md'},
                       'key': 'procedure'}
        final = {'id': survivor_id, **entity('Owner supplied exact survivor')}
        mapping = self.mapping({'op': 'split', 'id': parent_id, **entity('Primary'),
                                'children': [child_ref, child_skill, {**entity('Child'), 'key': 'item'}]},
                               {'op': 'merge', 'id': left_id, 'survivor': final},
                               {'op': 'merge', 'id': forget_id, 'survivor': final, 'source': 'ERASE ' + forget_id},
                               retype(old_task['record']['id'], task()),
                               retype(decision['record']['id'], {'kind': 'decision', 'data': {
                                   'title': 'Retain', 'rationale': 'Owner supplied', 'state': 'accepted'}}),
                               retype(outcome['record']['id'], {'kind': 'outcome', 'data': {
                                   'title': 'Measured', 'observed': 'Owner supplied', 'occurred_on': '2026-11-26'}}))
        path = self.private / 'mapping.json'
        path.write_text(json.dumps(mapping), encoding='utf-8')
        path.chmod(0o600)
        code, report, err = self.command('scan', module=True)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual({f['id'] for f in json.loads(report)['findings']},
                         {env['record']['id'] for env in old if env != survivor})
        for field in ('task.added_on', 'decision.rationale', 'outcome.occurred_on'):
            self.assertIn(field, report)
        self.assertEqual(self.command('check')[0], 1)
        code, report, err = self.command('apply', path, '--dry-run', '--expected-token', initial, module=True)
        self.assertEqual((code, err), (0, ''))
        predicted = json.loads(report)
        self.assertEqual(copy.inventory()['token'], initial)
        code, report, err = self.command('apply', path, module=True)
        self.assertEqual((code, err), (0, ''))
        applied = json.loads(report)
        self.assertEqual(applied['changes'], predicted['changes'])
        for private in ('Earlier private source', 'Private moved wording', 'UNIQUE erased', 'Owner supplied'):
            self.assertNotIn(private, report + canonical(predicted))
        token = applied['token']
        self.assertEqual(self.command('check', module=True)[0], 0)
        for env in old:
            key = env['record']['id']
            after = copy.lookup(key, token)
            if key == forget_id:
                self.assertTrue(after['record']['erased'])
                self.assertEqual(set(after['record']), {'id', 'written_at', 'erased'})
                self.assertEqual(after['history'], [])
            else:
                self.assertEqual(len(after['history']), len(env['history']) + 1)
                self.assertIn(env['record'], [v['record'] for v in after['history']])
                for entry in env['history']:
                    self.assertIn(entry, after['history'])
                self.assertTrue(all(v['record']['id'] == key for v in after['history']))
        self.assertEqual(copy.lookup(survivor_id, token)['record']['data'], final['data'])
        crossref = copy.lookup(left_id, token)['record']['data']
        self.assertEqual((crossref['state'], crossref['survivor_id']), ('retired', survivor_id))
        for key, field in (('topic', 'body'), ('procedure', 'when_to_use'), ('item', 'source_id')):
            child = copy.lookup(split_id(parent_id, key), token)
            self.assertEqual(child['history'], [])
            self.assertIn(parent_id, child['record']['data'][field])
        self.assertNotIn(b'UNIQUE erased wording', self.db.read_bytes())
        self.assertEqual({p.name for p in self.private.iterdir()}, {'mapping.json', 'snapshot.sqlite3'})
        again = apply_mapping(copy, mapping)
        self.assertEqual((again['changes'], again['token']), ([], token))
        self.assertEqual(self.original.read([forget_id], initial), [forget])

    def test_all_converted_active_cards_appear_in_context_across_write_batches(self):
        notes = [version() for _ in range(MAX_BATCH + 1)]
        store = self.copy(*notes)
        rows = [retype(env['record']['id'], ref(f'Topic {n}')) for n, env in enumerate(notes)]
        apply_mapping(store, self.mapping(*rows))
        self.assertEqual(scan(store)['findings'], [])
        cards = store.context_view(active=True, nexus=False)['references']
        self.assertCountEqual(cards, [{'id': row['id'], 'name': row['data']['name'],
                                      'read_when': row['data']['read_when'], 'origin': 'project'} for row in rows])

    def test_resume_does_not_replay_historical_payload_or_erase_source_after_survivor_edit(self):
        source, survivor, primary = version(), version(), version()
        key, dest, other = (env['record']['id'] for env in (source, survivor, primary))
        store = self.copy(source, survivor, primary)
        exact = entity('Reviewed survivor')
        retype_row = retype(other, entity('Reviewed primary'))
        first = apply_mapping(store, self.mapping(retype(dest, exact), retype_row))
        newer = [version(previous=env, **entity('Intervening edit')) for env in store.read([dest, other], first['token'])]
        token = store.apply(newer, first['token'], strict_current=True)['token']
        merge = {'op': 'merge', 'id': key, 'survivor': {'id': dest, **exact}, 'source': 'ERASE ' + key}
        for row in (merge, retype_row):
            with self.assertRaisesRegex(ConversionError, 'historical'):
                apply_mapping(store, self.mapping(row))
        self.assertEqual(store.read([dest, other], token), newer)
        self.assertEqual(store.lookup(key, token), source)

    def test_interrupted_batches_resume_without_overwriting_an_interleaved_unrelated_edit(self):
        entries = [version() for _ in range(MAX_BATCH + 7)]
        unrelated = version(**entity('Unrelated before'))
        store = self.copy(*entries, unrelated)
        mapping = self.mapping(*(retype(e['record']['id'], entity('Classified')) for e in entries))
        before = store.inventory()['token']
        apply = store.apply
        batch_sizes = []

        def other_writer(changes, expected, **kw):
            batch_sizes.append(len(changes))
            if len(batch_sizes) == 2:
                apply([version(previous=unrelated, **entity('Unrelated after'))], expected, strict_current=True)
            return apply(changes, expected, **kw)

        with patch.object(store, 'apply', side_effect=other_writer), self.assertRaises(StaleView):
            apply_mapping(store, mapping, expected_token=before)
        self.assertEqual(batch_sizes, [MAX_BATCH, 7])
        with self.assertRaises(StaleView):
            apply_mapping(store, mapping, expected_token=before, dry_run=True)
        result = apply_mapping(store, mapping)
        self.assertEqual((len(result['changes']), len(result['unchanged'])), (7, MAX_BATCH))
        self.assertEqual(store.lookup(unrelated['record']['id'], result['token'])['record']['data']['name'],
                         'Unrelated after')
        for e in entries:
            self.assertEqual(len(store.lookup(e['record']['id'], result['token'])['history']), 1)
        self.assertEqual(scan(store)['findings'], [])

    def test_batch_byte_limit_counts_utf8_and_preflights_all_oversize_histories_before_any_write(self):
        entries = [version(data={'text': '\U00020000' * 70000}) for _ in range(5)]
        store = self.copy(*entries)
        valid = self.mapping(*(retype(env['record']['id'], entity('Small')) for env in entries))
        too_large = {**entity('Sensitive content'), 'data': {**entity('Large')['data'], 'text': 'x' * 280000}}
        rows = valid['rows'][:3] + [retype(e['record']['id'], too_large) for e in entries[3:]]
        before = store.inventory()['token']
        with self.assertRaises(ConversionError) as err:
            apply_mapping(store, self.mapping(*rows))
        for e in entries[3:]:
            self.assertIn(e['record']['id'], str(err.exception))
        self.assertIn(str(MAX_ENVELOPE_BYTES), str(err.exception))
        self.assertEqual(store.inventory()['token'], before)
        with patch.object(store, 'apply', wraps=store.apply) as apply:
            report = apply_mapping(store, valid)
        self.assertEqual(report['batches'], 2)
        for call in apply.call_args_list:
            self.assertLess(len(canonical(call.args[0]).encode()), request_limit('/v1/reconcile/apply'))

    def test_dependency_order_across_batches_promotes_task_before_plans(self):
        oldtask = version()
        task_id = oldtask['record']['id']
        plans = [version() for _ in range(MAX_BATCH + 2)]
        store = self.copy(oldtask, *plans)
        mapping = self.mapping(*(retype(p['record']['id'], plan(task_id)) for p in plans), retype(task_id, task()))
        self.assertEqual(apply_mapping(store, mapping)['batches'], 2)
        with self.assertRaisesRegex(ConversionError, 'task kind cannot change'):
            apply_mapping(store, self.mapping(retype(task_id, entity('Not a task'))), dry_run=True)
        apply_mapping(store, self.mapping('ERASE ' + task_id))
        self.assertEqual({f['id'] for f in scan(store)['findings']}, {p['record']['id'] for p in plans})

    def test_merge_erasure_with_cyclic_task_precondition_is_one_survivor_first_transaction(self):
        survivor = version(**task())
        key = survivor['record']['id']
        source = version(**plan(key))
        source_id = source['record']['id']
        store = self.copy(survivor, source)
        mapping = self.mapping({'op': 'merge', 'id': source_id, 'survivor': {'id': key, **entity('Done')},
                                'source': 'ERASE ' + source_id})
        with patch.object(store, 'apply', wraps=store.apply) as apply:
            apply_mapping(store, mapping)
        self.assertEqual([e['record']['id'] for e in apply.call_args.args[0]], [key, source_id])
        self.assertEqual(scan(store)['findings'], [])

    def test_brief_reference_pointers_need_explicit_update_files_and_read_first_survive(self):
        old = version()
        key = old['record']['id']
        data = {'purpose': 'Fixture', 'read_first': [key], 'consult_when_relevant': [
            {'link': 'record:' + key}, {'link': 'manual.pdf'}]}
        brief = version('brief', data, key=brief_id(self.project))
        store = self.copy(old, brief)
        one = retype(key, ref('Topic'))
        with self.assertRaisesRegex(ConversionError, 'consult_when_relevant'):
            apply_mapping(store, self.mapping(one))
        corrected = {**data, 'consult_when_relevant': [{'link': 'manual.pdf'}]}
        report = apply_mapping(store, self.mapping(one, retype(brief_id(self.project), {'kind': 'brief',
                                                                                     'data': corrected})))
        self.assertEqual(store.lookup(brief_id(self.project), report['token'])['record']['data'], corrected)

    def test_merge_explicit_retype_and_split_collision_never_clobber_unrelated_id(self):
        source, survivor = version(), version()
        key, dest = source['record']['id'], survivor['record']['id']
        collision = version(key=split_id(key, 'taken'), **entity('Unrelated record'))
        store = self.copy(source, survivor, collision)
        before = store.inventory()['token']
        with self.assertRaisesRegex(ConversionError, 'split child ID exists'):
            apply_mapping(store, self.mapping({'op': 'split', 'id': key, **entity('Primary'),
                                               'children': [{'key': 'taken', **entity('New record')}]}))
        self.assertEqual(store.lookup(collision['record']['id'], before), collision)
        mapping = self.mapping({'op': 'merge', 'id': key, 'survivor': {'id': dest, **entity('Exact')},
                                'source': {'kind': 'risk', 'data': {'title': 'Still uncertain', 'state': 'open'}}})
        token = apply_mapping(store, mapping)['token']
        self.assertEqual(store.lookup(key, token)['record']['kind'], 'risk')

    def test_invalid_mapping_rows_never_leak_private_text_or_apply_otherwise_valid_prefix(self):
        source, survivor = version(), version()
        key, dest = source['record']['id'], survivor['record']['id']
        store = self.copy(source, survivor)
        before = store.inventory()['token']
        sensitive = 'private text must not escape'
        merge = {'op': 'merge', 'id': key, 'survivor': {'id': dest, **entity(sensitive)}}
        for rows in ([retype(key, entity(sensitive)), 'ERASE ' + uid()], [retype(key, entity(sensitive))] * 2,
                     ['ERASE  ' + key], [{'op': 'erase', 'id': key}], [{**merge, 'source': 'ERASE ' + dest}],
                     [{**merge, 'source': 'duplicate'}], [merge, retype(dest, entity(sensitive))],
                     [{**merge, 'id': dest}], [{'op': 'merge', 'id': key, 'survivor': {'id': dest}}],
                     [retype(key, {'kind': 'profile', 'data': {'body': sensitive}})],
                     [retype(key, {'kind': 'action', 'data': {'text': sensitive}})],
                     [retype(key, {'kind': 'outcome', 'data': {'title': sensitive}})],
                     [retype(key, plan(uid()))], [{**retype(key, entity(sensitive)), sensitive: 'unexpected'}],
                     [{'op': 'split', 'id': key, **entity(sensitive), 'children': []}],
                     [{'op': 'split', 'id': key, **entity(sensitive), 'children': [{'key': 'k', **entity('a')}] * 2}]):
            with self.subTest(rows=rows), self.assertRaises(ConversionError) as err:
                apply_mapping(store, self.mapping(*rows))
            self.assertNotIn(sensitive, str(err.exception))
        self.assertEqual(store.inventory()['token'], before)
        path = self.private / 'map.json'
        path.write_text('{"rows":[],"rows":[],"' + sensitive + '":null}')
        code, out, err = self.command('apply', path)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('repeats a JSON field', err)
        self.assertNotIn(sensitive, err)

    def bind_workspace(self):
        home = self.private / 'installation'
        home.mkdir(mode=0o700)
        self.enterContext(patch.dict(os.environ, MAVEN_HOME=str(home)))
        instance = uid()
        save_private(self.root, '.maven/project.json', {
            'schema': 2, 'root': str(self.root), 'instance_id': instance,
            'project_id': self.project, 'recovery_epoch': uid()})
        return Replica(home, instance)

    def test_bound_workspace_cli_converts_and_checks_selected_project(self):
        self.bind_workspace()
        legacy = version()
        key = legacy['record']['id']
        self.original.apply([legacy], self.original.orient()['token'], strict_current=False)
        path = self.private / 'mapping.json'
        path.write_text(json.dumps(self.mapping(retype(key, task()))))
        code, out, err = self.command('scan', workspace=self.root, module=True)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual([row['id'] for row in json.loads(out)['findings']], [key])
        code, out, err = self.command('apply', path, workspace=self.root, module=True)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['changes'], [key])
        code, out, err = self.command('check', workspace=self.root, module=True)
        self.assertEqual((code, err, json.loads(out)['findings']), (0, '', []))
        result = self.original.lookup(key, json.loads(out)['token'])
        self.assertEqual(result['record']['data'], task()['data'])
        self.assertEqual(result['history'][0]['record'], legacy['record'])

    def test_workspace_apply_refuses_wrong_project_and_missing_or_broken_binding(self):
        self.bind_workspace()
        legacy = version()
        key = legacy['record']['id']
        token = self.original.apply([legacy], self.original.orient()['token'], strict_current=False)['token']
        mapping = self.private / 'mapping.json'
        binding = self.root / '.maven/project.json'
        for case in ('wrong UUID', 'broken binding', 'missing binding'):
            with self.subTest(case=case):
                project = uid() if case == 'wrong UUID' else self.project
                if case == 'broken binding':
                    binding.write_text('{')
                elif case == 'missing binding':
                    binding.unlink()
                mapping.write_text(json.dumps({'project_id': project, 'rows': ['ERASE ' + key]}))
                code, out, err = self.command('apply', mapping, workspace=self.root, project=project)
                self.assertEqual((code, out), (2, ''))
                self.assertIn('binding', json.loads(err)['error'])
                self.assertEqual(self.original.lookup(key, token), legacy)

    def test_workspace_apply_enforces_remembered_erasure_before_reading_mapping_targets(self):
        replica = self.bind_workspace()
        legacy = version()
        key = legacy['record']['id']
        self.original.apply([legacy], self.original.orient()['token'], strict_current=False)
        with replica.locked():
            replica.remember([{'kind': 'project_record', 'id': key, 'project_id': self.project}])
        path = self.private / 'mapping.json'
        path.write_text(json.dumps(self.mapping(retype(key, task()))))
        code, out, err = self.command('apply', path, workspace=self.root)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('erased', json.loads(err)['error'])
        after = self.original.lookup(key, self.original.orient()['token'])
        self.assertTrue(after['record']['erased'])
        self.assertEqual(after['history'], [])

    def test_cli_requires_exactly_one_target(self):
        for target in ([], ['--workspace', str(self.root), '--database', str(self.db)]):
            with self.subTest(target=target), contextlib.redirect_stderr(StringIO()), self.assertRaises(SystemExit) as exc:
                main([*target, '--project', self.project, 'scan'])
            self.assertEqual(exc.exception.code, 2)

    def test_copy_selection_never_initializes_missing_or_empty_files_or_accepts_wrong_project(self):
        self.copy(version())
        missing = self.private / 'absent.sqlite3'
        self.assertEqual(self.command('scan', db=missing)[0], 2)
        self.assertFalse(missing.exists())
        missing.touch(mode=0o600)
        self.assertEqual(self.command('scan', db=missing)[0], 2)
        self.assertEqual(missing.read_bytes(), b'')
        self.assertEqual(self.command('scan', project=uid())[0], 2)
        linked = self.private / 'link.sqlite3'
        linked.symlink_to(self.db)
        self.assertEqual(self.command('scan', db=linked)[0], 2)
        self.db.chmod(0o644)
        self.assertEqual(self.command('scan')[0], 2)

    def test_profile_at_reserved_id_cannot_be_authored_by_conversion_even_for_named_project(self):
        key = profile_id(self.project)
        store = self.copy(version(key=key))
        with self.assertRaisesRegex(ConversionError, 'configured Home'):
            apply_mapping(store, self.mapping(retype(key, {'kind': 'profile', 'data': {'body': 'Not configured'}})))
