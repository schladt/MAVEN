"""Root-scoped onboarding receipts and boundaries; no real cloud operations."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.memory.client import MemoryClientError
from tools.interaction import using
from tools.project import ProjectError, load_project
from tools.project_setup import _selection, setup_project
from tools.setup_common import SetupPending, private_directory, read_private, save_json


class ProjectSetupTests(unittest.TestCase):
    def args(self, root, env, **changes):
        values = dict(root=root, env=env, non_interactive=True, create=True, project_id=None,
                      name='Owner project', source_origin='Owner chat approval',
                      source_excerpt='Create this project with memory-only lifecycle.',
                      no_git=True, remote=None, accepted_url=None, branch=None, check=[])
        values.update(changes)
        return argparse.Namespace(**values)

    def cloud(self, stack):
        config = {'instance_id': str(uuid4()), 'recovery_epoch': str(uuid4()),
                  'region': 'us-east-1', 'endpoint': 'https://example.execute-api.us-east-1.amazonaws.com'}
        client = Mock()
        client.invoke.return_value = {'data': {'kind': 'entity', 'data_scope': 'owner',
                                               'data': {'entity_type': 'project'}}}
        stack.enter_context(patch('tools.memory.environment.load_environment', return_value={}))
        stack.enter_context(patch('tools.memory.environment.memory_config', return_value=config))
        stack.enter_context(patch('tools.memory.environment.configured_client', return_value=client))
        stack.enter_context(patch('tools.project_setup.verify_authority', return_value=7))
        return client, config

    def test_independent_roots_and_configured_rerun_never_duplicate_records(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            parent = Path(directory).resolve()
            client, _ = self.cloud(stack)
            ids = []
            for name in ('one', 'two'):
                root = parent / name
                root.mkdir()
                args = self.args(root, parent / '.env')
                result = setup_project(args)
                ids.append(result['project']['project_id'])
                before = (root / '.maven' / 'project.json').read_bytes()
                setup_project(args)
                setup_project(self.args(root, parent / '.env', create=False, name=None,
                                        source_origin=None, source_excerpt=None, no_git=False))
                self.assertEqual((root / '.maven' / 'project.json').read_bytes(), before)
                receipt = root / '.maven' / 'runtime' / 'project-setup' / 'state.json'
                self.assertTrue(read_private(receipt)['complete'])
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)
                self.assertIn('/.maven/', (root / '.gitignore').read_text().splitlines())
            self.assertNotEqual(*ids)
            writes = [call for call in client.invoke.call_args_list if call.args[0] == '/v1/memory/write']
            self.assertEqual(len(writes), 2)

    def test_unknown_outcome_preserves_ids_and_checks_identity_before_resume(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory).resolve()
            client, config = self.cloud(stack)
            args = self.args(root, root / '.env')
            def unknown(route, body):
                if route == '/v1/memory/write':
                    raise MemoryClientError('outcome_unknown', request_key=body['request_key'])
                return {'data': {}}
            client.invoke.side_effect = unknown
            with self.assertRaises(MemoryClientError):
                setup_project(args)
            receipt = root / '.maven' / 'runtime' / 'project-setup' / 'state.json'
            saved = read_private(receipt)
            self.assertTrue(saved['submitted'])
            self.assertFalse((root / '.maven' / 'project.json').exists())
            original = config['instance_id']
            config['instance_id'] = str(uuid4())
            client.reset_mock()
            with self.assertRaises(SetupPending):
                setup_project(args)
            client.invoke.assert_not_called()
            config['instance_id'] = original
            client.invoke.side_effect = None
            client.invoke.return_value = {'data': {'kind': 'entity', 'data_scope': 'owner',
                                                   'data': {'entity_type': 'project'}}}
            setup_project(args)
            calls = client.invoke.call_args_list
            self.assertEqual(calls[0].args, ('/v1/operations/lookup', {'request_key': saved['selection']['request_key']}))
            self.assertEqual(calls[1].args[1]['request_key'], saved['selection']['request_key'])
            self.assertEqual(load_project(root)['project_id'], saved['selection']['project_id'])

    def test_root_missing_choices_and_git_subdirectory_fail_before_cloud_write(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory).resolve()
            client, _ = self.cloud(stack)
            cases = [self.args(Path('/'), root / '.env'),
                     self.args(root, root / '.env', create=False),
                     self.args(root, root / '.env', no_git=False),
                     self.args(root, root / '.env', source_excerpt=None)]
            for args in cases:
                with self.subTest(args=args), self.assertRaises(ProjectError):
                    setup_project(args)
            git = self.args(root, root / '.env', no_git=False, remote='origin',
                            accepted_url='git@example.org:owner/project.git', branch='main')
            with patch('tools.project._git', return_value=str(root.parent)), self.assertRaises(ProjectError):
                setup_project(git)
            client.invoke.assert_not_called()

    def test_legacy_receipt_is_adopted_exactly_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            parent = Path(directory).resolve()
            root = parent / 'project'
            root.mkdir()
            home = parent / 'home'
            private_directory(home / 'runtime' / 'setup')
            client, config = self.cloud(stack)
            args = self.args(root, home / '.env')
            legacy = {key: value for key, value in vars(args).items()
                      if key not in ('env', 'non_interactive')}
            legacy.update(root=str(root), project_id=str(uuid4()), request_key=str(uuid4()), existing=False)
            state_path = home / 'runtime' / 'setup' / 'state.json'
            save_json(state_path, {'schema': 1, 'instance_id': config['instance_id'],
                                   'project': legacy, 'submitted_requests': [legacy['request_key']]})
            with self.assertRaises(SetupPending):
                setup_project(args)
            client.invoke.assert_not_called()
            args.non_interactive = False
            with patch('tools.project_setup.approve'), patch('tools.project_setup.ask') as ask:
                setup_project(args)
            ask.assert_not_called()
            calls = client.invoke.call_args_list
            self.assertEqual(calls[0].args, ('/v1/operations/lookup', {'request_key': legacy['request_key']}))
            self.assertEqual(load_project(root)['project_id'], legacy['project_id'])
            self.assertEqual(read_private(state_path)['project'], legacy)
            second = parent / 'second'
            second.mkdir()
            setup_project(self.args(second, home / '.env'))
            self.assertNotEqual(load_project(second)['project_id'], legacy['project_id'])

    def test_interactive_selection_reviews_complete_provenance_and_stable_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = self.args(root, root / '.env', non_interactive=False)
            ui = Mock()
            ui.form.side_effect = [
                {'mode': 'create', 'lifecycle': 'no-git'},
                {'name': 'Actual owner project', 'source_origin': 'Owner setup review',
                 'source_excerpt': 'Track the explicitly selected project.', 'checks': '["python", "-V"]'},
            ]
            ui.review.return_value = True
            with using(ui):
                selected = _selection(args, root)
            reviewed = json.loads(ui.review.call_args.args[1])
            self.assertEqual(reviewed, selected)
            self.assertEqual(ui.review.call_args.kwargs['identity'], selected['project_id'])
            self.assertTrue(selected['request_key'])
            self.assertEqual(selected['source_excerpt'], 'Track the explicitly selected project.')
            self.assertEqual(selected['check'], ['["python", "-V"]'])
            self.assertFalse((root / '.maven').exists())


if __name__ == '__main__':
    unittest.main()
