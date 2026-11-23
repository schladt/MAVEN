"""User-visible receipts across independent hook and MCP calls, on real local stores."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools.home import save_home
from tools.memory.boundary import BoundaryError, save_private
from tools.reconcile_local import LocalStore
from tools.turn_memory import LocalApplication
from tools import turn_runtime as t


class TurnReceipts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.home = base / 'installation'
        self.home.mkdir(mode=0o700)
        self.app = LocalApplication(home=self.home)
        self.cwd, self.nexus = base / 'widgets', base / 'personal'
        ident = {k: str(uuid4()) for k in ('instance_id', 'recovery_epoch')}
        for root in (self.cwd, self.nexus):
            root.mkdir(mode=0o700)
            key = str(uuid4())
            save_private(root, '.maven/project.json', {'schema': 2, 'root': str(root), 'project_id': key, **ident})
            if root == self.nexus:
                self.nid = key
            else:
                self.pid = key
        save_home(self.home, root=self.nexus, project_id=self.nid, **ident, label='Nexus',
                  consent={'origin': 'test', 'excerpt': 'Select Home'})
        self.event = (self.home, 'omp', 'opaque-user-session', self.cwd)

    def test_write_cannot_be_dismissed_and_one_record_covers_a_batch(self):
        initial = LocalStore(self.cwd, self.pid).inventory()
        key = t.begin(*self.event)['turn_key']
        self.assertEqual(t.finish(self.app, self.cwd, key)['status'], 'complete')
        self.assertEqual(LocalStore(self.cwd, self.pid).inventory(), initial)
        t.observe(*self.event, write=True)
        t.observe(*self.event, opaque=True)
        self.assertEqual(t.finish(self.app, self.cwd, key)['status'], 'incomplete')
        self.assertTrue(t.end(*self.event)['request_capture'])
        self.assertFalse(t.end(*self.event)['request_capture'])  # Never hold the terminal open.
        t.save(self.app, self.cwd, key, project_id=self.pid, kind='action', data={'summary': 'Adjusted widget parser'},
               reason='Observed writes', covers_files=True)
        receipt = t.finish(self.app, self.cwd, key)
        self.assertEqual(receipt['footer'], 'MAVEN · widgets: updated locally; Nexus: consulted, no update.')
        self.assertEqual(t.end(*self.event)['footer'], receipt['footer'])
        self.assertEqual(len(LocalStore(self.cwd, self.pid).inventory()['items']), 1)
        t.observe(*self.event, write=True)
        self.assertEqual(t.end(*self.event)['status'], 'incomplete')
        persisted = next((self.home / 'runtime/turns').glob('*.json')).read_text()
        for raw in ('Adjusted widget parser', str(self.cwd), str(self.nexus), 'opaque-user-session', 'Nexus', 'Observed writes'):
            self.assertNotIn(raw, persisted)

    def test_conversation_opt_out_survives_new_process_not_new_conversation(self):
        key = t.begin(*self.event)['turn_key']
        self.assertEqual(t.control('  Turn off MAVEN for this session. '), (True, False))
        self.assertFalse(any(t.control('Say "turn off MAVEN for this session".')))
        self.assertEqual(t.begin(*self.event, disable=True)['status'], 'off')
        with patch.object(t, 'consult', side_effect=AssertionError('should not read')):
            self.assertEqual(t.begin(*self.event)['status'], 'off')
            t.observe(*self.event, write=True, turn_key=key)
            self.assertEqual(t.end(*self.event)['footer'], '')
            with self.assertRaises(BoundaryError):
                t.finish(self.app, self.cwd, key)
        self.assertEqual(t.begin(self.home, 'omp', 'independent', self.cwd)['status'], 'consulted')
        self.assertNotEqual(t.begin(*self.event, enable=True)['turn_key'], key)

    def test_cross_scope_and_late_event_cannot_claim_new_turn(self):
        old = t.begin(*self.event, turn_id='old')['turn_key']
        key = t.begin(*self.event, turn_id='current')['turn_key']
        with self.assertRaises(BoundaryError):
            t.refresh(self.app, self.cwd, old)
        with self.assertRaises(BoundaryError):
            t.finish(self.app, self.nexus, key)
        t.observe(*self.event, write=True, turn_id='old')
        self.assertFalse(t.end(*self.event, turn_id='old')['request_capture'])
        with self.assertRaises(BoundaryError):
            t.save(self.app, self.cwd, key, project_id=self.nid, kind='task', data={'state': 'open'}, reason='wrong store')
        receipt = t.finish(self.app, self.cwd, key, awaiting_scope=True)
        self.assertEqual(receipt['status'], 'complete')
        self.assertIn('preference scope pending', receipt['footer'])
        t.save(self.app, self.cwd, key, project_id=self.nid, kind='setting',
               data={'category': 'preference', 'scope': 'nexus', 'summary': 'Never auto-publish'}, reason='Owner selected Nexus')
        self.assertIn('widgets: consulted, no update; Nexus: updated locally', t.finish(self.app, self.cwd, key)['footer'])
        self.assertEqual(LocalStore(self.cwd, self.pid).inventory()['items'], [])

    def test_lost_receipt_after_sql_commit_is_not_confirmed_or_replayed(self):
        key = t.begin(*self.event)['turn_key']
        persist = t._save
        def kill_after_commit(home, slot, state):
            if state['counts']:
                raise OSError('fixture crash')
            persist(home, slot, state)
        with patch.object(t, '_save', side_effect=kill_after_commit), self.assertRaises(OSError):
            t.save(self.app, self.cwd, key, project_id=self.pid, kind='outcome', data={'summary': 'Parsed fixtures'}, reason='Verified')
        self.assertEqual(len(LocalStore(self.cwd, self.pid).inventory()['items']), 1)
        final = t.end(*self.event)
        self.assertEqual(final['status'], 'failed')
        self.assertIn('unverified', final['footer'])
        self.assertNotIn('updated locally', final['footer'])
        self.assertFalse(final['request_capture'])

    def test_copilot_denies_mutation_until_its_own_context_was_served(self):
        key = t.begin(self.home, 'copilot', 'A', self.cwd)['turn_key']
        other = t.begin(self.home, 'copilot', 'B', self.cwd)['turn_key']
        self.assertEqual(t.copilot_guard(self.home, 'A', self.cwd)['turn_key'], key)
        self.assertTrue(t.copilot_guard(self.home, 'A', self.cwd)['deny'])
        t.refresh(self.app, self.cwd, other)
        self.assertTrue(t.copilot_guard(self.home, 'A', self.cwd)['deny'])
        view = json.loads(t.refresh(self.app, self.cwd, key)['text'].splitlines()[-1])
        self.assertNotIn('root', view['nexus'])
        self.assertFalse(t.copilot_guard(self.home, 'A', self.cwd)['deny'])
        t.begin(self.home, 'copilot', 'A', self.cwd, disable=True)
        self.assertTrue(t.copilot_guard(self.home, 'A', self.cwd)['off'])

    def test_duplicate_cloud_uuid_still_routes_by_physical_scope(self):
        # Two worktrees legitimately bound to one cloud project have independent SQLite state.
        path = self.cwd / '.maven/project.json'
        binding = json.loads(path.read_text())
        save_private(self.cwd, '.maven/project.json', {**binding, 'project_id': self.nid})
        r = t.begin(*self.event)
        self.assertIsInstance(r['context']['nexus'], dict)
        key = r['turn_key']
        t.save(self.app, self.cwd, key, project_id=self.nid, kind='action', data={'summary':'Project change'}, reason='Observed')
        self.assertIn('widgets: updated locally; Nexus: consulted, no update', t.finish(self.app, self.cwd, key)['footer'])
        self.assertEqual(LocalStore(self.nexus, self.nid).inventory()['items'], [])
        t.save(self.app, self.cwd, key, project_id=self.nid, kind='setting',
               data={'category':'preference', 'scope':'nexus', 'summary':'Check first'}, reason='Owner selected Nexus')
        self.assertEqual([r['kind'] for r in LocalStore(self.cwd, self.nid).inventory()['items']], ['action'])
        self.assertEqual([r['kind'] for r in LocalStore(self.nexus, self.nid).inventory()['items']], ['setting'])

    def test_nexus_preference_cannot_replace_unrelated_home_knowledge(self):
        first = t.begin(self.home, 'omp', 'personal-work', self.nexus)['turn_key']
        prior = t.save(self.app, self.nexus, first, project_id=self.nid, kind='note',
                       data={'summary': 'Private unrelated Home note'}, reason='Selected Home')
        key = t.begin(*self.event)['turn_key']
        with self.assertRaises(ValueError):
            t.save(self.app, self.cwd, key, project_id=self.nid, record_id=prior['id'], kind='setting',
                   data={'category':'preference','scope':'nexus','summary':'Wrong target'}, reason='Not a preference')
        self.assertEqual([r['kind'] for r in LocalStore(self.nexus, self.nid).inventory()['items']], ['note'])

    def test_native_wire_events_reach_the_same_store_as_model_finish(self):
        # Real bridge subprocesses and stores, synthetic client events. No native client or model login.
        from tools.turn_native import NATIVE_EVENTS
        source = str(Path(__file__).resolve().parents[1])
        launch = 'import sys; sys.path.insert(0,sys.argv.pop(1)); from tools.turn_hook import main; sys.exit(main())'
        def hook(name, phase, event):
            result = subprocess.run([sys.executable, '-I', '-B', '-c', launch, source, name, phase,
                                     '--home', str(self.home)], input=json.dumps(event), capture_output=True,
                                    text=True, cwd=self.cwd, check=True, timeout=15)
            return json.loads(result.stdout)

        first = t.begin(self.home, 'omp', 'home-prefs', self.nexus)['turn_key']
        t.save(self.app, self.nexus, first, project_id=self.nid, kind='setting',
               data={'category':'preference','scope':'nexus','summary':'No implicit Git commits'}, reason='Selected Nexus')
        expected = {
            'claude': {'tool_name':'Write','tool_response':{'type':'create','filePath':'parser.py'}},
            'codex': {'tool_name':'apply_patch','tool_response':
                      'Exit code: 0\nWall time: 0.1 seconds\nOutput:\nSuccess. Updated the following files:\nA parser.py\n'},
            'gemini': {'tool_name':'write_file','tool_input':{'file_path':'parser.py'},
                       'tool_response':{'llmContent':'OK','returnDisplay':'Updated'}},
            'copilot': {'toolName':'create','toolArgs':{'path':'parser.py'},'toolResult':{'resultType':'success'}},
        }
        for name, (submit, post, stop) in NATIVE_EVENTS.items():
            with self.subTest(client=name):
                base = {'cwd':str(self.cwd), 'sessionId' if name == 'copilot' else 'session_id':'wire-client'}
                if name == 'codex':
                    base['turn_id'] = 'actual-native-turn'
                intro = hook(name, 'begin', {**base, 'hook_event_name':submit,'prompt':'Update the parser'})
                if name == 'copilot':
                    guard = hook(name, 'guard', {**base, 'toolName':'bash'})
                    self.assertEqual(guard['permissionDecision'], 'deny')
                    key = guard['permissionDecisionReason'].split('turn_key=\"')[1].split('\"')[0]
                    text = t.refresh(self.app, self.cwd, key)['text']
                    self.assertEqual(hook(name, 'guard', {**base, 'toolName':'bash'}), {})
                else:
                    text = intro['hookSpecificOutput']['additionalContext']
                view = json.loads(text.splitlines()[-1])
                self.assertEqual(view['nexus']['preferences'][0]['summary'], 'No implicit Git commits')
                key = view['turn_key']
                hook(name, 'observe', {**base, 'hook_event_name':post, **expected[name]})
                self.assertEqual(t.finish(self.app, self.cwd, key)['status'], 'incomplete')
                t.save(self.app, self.cwd, key, project_id=self.pid, kind='action', data={'summary':f'Updated parser in {name}'},
                       reason='Observed write', covers_files=True)
                footer = t.finish(self.app, self.cwd, key)['footer']
                shown = hook(name, 'end', {**base, 'hook_event_name':stop, 'stopReason':'end_turn'})
                self.assertEqual(shown.get('systemMessage', shown.get('message')), footer)
        self.assertEqual(len(LocalStore(self.cwd, self.pid).inventory()['items']), len(NATIVE_EVENTS))
