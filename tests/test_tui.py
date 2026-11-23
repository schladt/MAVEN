"""Local terminal approval/pause boundaries; no cloud or provisioned environments."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from textual.widgets import Collapsible, Input, OptionList, Static, TextArea

from tools.interaction import Field
from tools.tui.app import ChooseScreen, FormScreen, ReviewScreen, WorkflowApp
from tools.tui.administration import present


class TuiBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_environment_migration_requires_terminal_approval(self):
        from tools.administration import AdminOperations
        from tools.memory.environment import load_environment
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            home.mkdir(mode=0o700)
            environment = home / '.env'
            original = 'MAVEN_AWS_REGION="us-east-1"\nMAVEN_GOOGLE_PARAMETER_PREFIX="/synthetic/google/"\n'
            environment.write_text(original)
            environment.chmod(0o600)
            controller = AdminOperations(home)
            operation = controller.begin('environment_migration', {})
            identifier = operation['operation_id']
            app = WorkflowApp('Local environment migration', [],
                              lambda ui: present(ui, home, operation_id=identifier))
            async with app.run_test(size=(110, 38)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, ReviewScreen)
                self.assertEqual(environment.read_text(), original)
                self.assertFalse(controller.status(identifier)['approvals'])
                await pilot.click('#accept')
                await pilot.pause()
                completed = controller.status(identifier)
                self.assertEqual(completed['state'], 'completed')
                self.assertEqual(load_environment(environment), {'MAVEN_AWS_REGION': 'us-east-1'})
                self.assertEqual(len(completed['approvals']), 1)
                self.assertEqual(completed['outcome']['preserved_locator'], '/synthetic/google/')
                await pilot.press('ctrl+q')

    async def test_activity_tracks_work_input_and_completion_on_ui_thread(self):
        for size in ((80, 24), (130, 45)):
            with self.subTest(size=size):
                release, finish = threading.Event(), threading.Event()
                answers, update_threads = [], []

                def operation(ui):
                    ui.stage('Submitting request')
                    if not release.wait(10):
                        raise RuntimeError('test did not release request')
                    answers.append(ui.form('Choose profile', [Field('profile', 'Profile', 'owner')]))
                    ui.stage('Waiting for completion')
                    if not finish.wait(10):
                        raise RuntimeError('test did not finish request')

                app = WorkflowApp('Activity lifecycle', ['Starting'], operation)
                show_activity = app.show_activity

                def record_activity(*args, **kwargs):
                    update_threads.append(threading.current_thread())
                    return show_activity(*args, **kwargs)

                try:
                    with patch.object(app, 'show_activity', side_effect=record_activity):
                        async with app.run_test(size=size) as pilot:
                            await pilot.pause()
                            self.assertEqual(app.activity_state, 'working')
                            self.assertIn('Waiting… Submitting request', app.activity_text)
                            self.assertTrue(app.screen.query_one('#busy').display)
                            self.assertTrue(app.screen.query_one('#status').is_on_screen)
                            self.assertGreaterEqual(app.screen.query_one('#status').region.y,
                                                    app.screen.query_one('#events').region.bottom)
                            release.set()
                            await pilot.pause()
                            self.assertIsInstance(app.screen, FormScreen)
                            self.assertEqual(app.activity_state, 'input')
                            self.assertIn('Awaiting input — Choose profile',
                                          str(app.screen.query_one('#dialog-status', Static).render()))
                            self.assertFalse(app._main_screen.query_one('#busy').display)
                            await pilot.click('#submit')
                            await pilot.pause()
                            self.assertEqual(answers, [{'profile': 'owner'}])
                            self.assertEqual(app.activity_state, 'working')
                            self.assertIn('Waiting for completion', app.activity_text)
                            self.assertTrue(app.screen.query_one('#busy').display)
                            finish.set()
                            await pilot.pause()
                            self.assertTrue(app.finished)
                            self.assertEqual(app.activity_state, 'complete')
                            self.assertFalse(app.screen.query_one('#busy').display)
                            self.assertTrue(all(thread is threading.main_thread() for thread in update_threads))
                            await pilot.press('ctrl+q')
                finally:
                    release.set()
                    finish.set()

    async def test_terminal_status_distinguishes_failure_pause_and_completion(self):
        for result, state in ((0, 'complete'), (2, 'failed'), (3, 'paused'), (130, 'paused'),
                              ('exception', 'failed'), ({'status': 'conflict'}, 'failed'),
                              ({'state': 'partial', 'recovery': 'Restore the original directory.'}, 'failed')):
            with self.subTest(result=result):
                def operation(ui):
                    if result == 'exception':
                        raise ValueError('private exception material')
                    return result

                app = WorkflowApp('Terminal status', [], operation)
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    self.assertTrue(app.finished)
                    self.assertEqual(app.activity_state, state)
                    status = app.screen.query_one('#status', Static)
                    self.assertTrue(str(status.render()).startswith(state.capitalize()))
                    self.assertNotIn('private exception material', str(status.render()))
                    if isinstance(result, dict) and 'recovery' in result:
                        self.assertIn(result['recovery'], str(status.render()))
                    elif state == 'failed':
                        self.assertIn('without making changes', str(status.render()))
                    self.assertTrue(status.is_on_screen)
                    self.assertFalse(app.screen.query_one('#busy').display)
                    await pilot.pause()
                    self.assertEqual(app.activity_state, state)
                    await pilot.press('ctrl+q')

    async def test_external_authentication_hands_off_terminal_without_busy_indicator(self):
        result = Mock(returncode=0)
        answers = []

        def operation(ui):
            answers.append(ui.external(['local-auth-placeholder'], check=False))

        app = WorkflowApp('External authentication', ['Authenticate'], operation)

        def authenticate(*args, **kwargs):
            self.assertIs(threading.current_thread(), threading.main_thread())
            self.assertEqual(app.activity_state, 'input')
            self.assertIn('External authentication', app.activity_text)
            self.assertFalse(app._main_screen.query_one('#busy').display)
            return result

        terminal = Mock()
        with patch.object(app, 'suspend') as suspend, patch('tools.tui.app.open', create=True) as opened, \
                patch('tools.tui.app.os.tcgetpgrp', return_value=os.getpgrp()), \
                patch('tools.tui.app.subprocess.run', side_effect=authenticate) as run:
            opened.return_value.__enter__.return_value = terminal
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                suspend.assert_called_once_with()
                opened.assert_called_once_with('/dev/tty', 'r+b', buffering=0)
                run.assert_called_once_with(['local-auth-placeholder'], stdin=terminal, stdout=terminal,
                                            stderr=terminal, check=False)
                self.assertEqual(answers, [result])
                self.assertEqual(app.activity_state, 'complete')
                self.assertFalse(app.screen.query_one('#busy').display)
                await pilot.press('ctrl+q')

    async def test_instance_name_default_preserves_overrides_and_rejects_incompatible_names(self):
        from tools.setup_common import provision_fields
        answers = []

        def operation(ui):
            answers.append(ui.form('Choose new instance settings', provision_fields()))
            answers.append(ui.form('Revisit saved settings', provision_fields(answers[0])))

        app = WorkflowApp('Instance defaults', ['New instance'], operation)
        async with app.run_test(size=(110, 42)) as pilot:
            await pilot.pause()
            stack = app.screen.query_one('#field-0', Input)
            instance = app.screen.query_one('#field-1', Input)
            stack.value = 'my-memory'
            await pilot.pause()
            self.assertEqual(instance.value, 'my-memory')
            stack.value = 'My-CloudFormation-Stack'
            await pilot.pause()
            self.assertEqual(instance.value, '')
            instance.value = 'INVALID'
            app.screen.query_one('#field-2', Input).value = 'owner@example.com'
            await pilot.click('#submit')
            await pilot.pause()
            self.assertEqual(answers, [])
            self.assertTrue(app.screen.query_one('#error-1', Static).display)
            instance.value = 'custom-memory'
            await pilot.pause()
            stack.value = 'different-stack'
            await pilot.pause()
            self.assertEqual(instance.value, 'custom-memory')
            stack.value = 'custom-memory'
            await pilot.pause()
            stack.value = 'final-stack'
            await pilot.pause()
            self.assertEqual(instance.value, 'custom-memory')
            await pilot.click('#submit')
            await pilot.pause()
            self.assertEqual(answers[0]['MAVEN_INSTANCE_NAME'], 'custom-memory')
            self.assertEqual(app.screen.query_one('#field-1', Input).value, 'custom-memory')
            app.screen.query_one('#field-0', Input).value = 'another-stack'
            await pilot.pause()
            self.assertEqual(app.screen.query_one('#field-1', Input).value, 'custom-memory')
            await pilot.press('ctrl+q')

    async def test_help_context_and_complete_details_survive_compact_navigation(self):
        context = ('Task: Connect existing memory\nAccess purpose: Everyday memory access\n'
                   'AWS account: 123456789012\nProfile: owner | not verified\n'
                   'Principal: arn:aws:iam::123456789012:user/owner\nTarget: instance-123\n'
                   'Checkpoint: identity inspection\nReceipts: /private/operations/receipt.json')
        artifact = '{"exact_instance": "instance-123", "complete": "' + 'retained ' * 80 + '"}'
        answers = []

        def operation(ui):
            ui.set_context(context)
            answers.append(ui.form('Choose AWS access', [
                Field('profile', 'AWS profile', 'owner', help='Use the profile for everyday memory access.')],
                description='Choose an account before signing in. This only saves your selection.',
                details=artifact, back=True))
            answers.append(ui.review('Review exact selection', 'No deployment is approved here.',
                                     identity='instance-123', details=artifact, back=True))
            answers.append(ui.choose('Next action', [('Finish', 'finish')], description='Keep all receipts.'))

        app = WorkflowApp('Context and details', ['Choose access'], operation)
        async with app.run_test(size=(130, 45)) as pilot:
            await pilot.pause()
            self.assertIsInstance(app.screen, FormScreen)
            self.assertTrue(app.screen.query_one('.field-help', Static).is_on_screen)
            panel = app.screen.query_one('#dialog')
            self.assertEqual(str(panel.query_one('#dialog-context', Static).render()), context)
            self.assertTrue(panel.query_one('.context-scroll').is_on_screen)
            self.assertEqual(app.screen.query_one('#technical-body', TextArea).text, artifact)
            self.assertTrue(app.screen.query_one('#technical-details', Collapsible).collapsed)
            self.assertFalse(app.screen.query_one('#technical-body', TextArea).is_on_screen)
            await pilot.click('#technical-details CollapsibleTitle')
            await pilot.pause()
            self.assertFalse(app.screen.query_one('#technical-details', Collapsible).collapsed)
            self.assertTrue(app.screen.query_one('#technical-body', TextArea).is_on_screen)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertEqual(str(app.screen.query_one('#dialog-context', Static).render()), context)
            self.assertTrue(app.screen.query_one('#dialog-context', Static).is_on_screen)
            context_scroll = app.screen.query_one('.context-scroll')
            self.assertGreater(context_scroll.max_scroll_y, 0)
            context_scroll.focus()
            await pilot.press('end')
            await pilot.pause()
            self.assertGreater(context_scroll.scroll_y, 0)
            self.assertTrue(app.screen.query_one('#submit').is_on_screen)
            await pilot.click('#submit')
            await pilot.pause()
            self.assertIsInstance(app.screen, ReviewScreen)
            self.assertTrue(app.screen.query_one('.target', Static).is_on_screen)
            self.assertTrue(app.screen.query_one('#back').is_on_screen)
            self.assertTrue(app.screen.query_one('#decline').is_on_screen)
            self.assertEqual(str(app.screen.query_one('#dialog-context', Static).render()), context)
            await pilot.click('#back')
            await pilot.pause()
            self.assertIsInstance(app.screen, ChooseScreen)
            self.assertEqual(str(app.screen.query_one('#dialog #dialog-context', Static).render()), context)
            self.assertTrue(app.screen.query_one('#choices').is_on_screen)
            self.assertIn('Awaiting input', str(app.screen.query_one('#dialog-status', Static).render()))
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(answers, [{'profile': 'owner'}, None, 'finish'])
            self.assertEqual(str(app.screen.query_one('#activity #context', Static).render()), context)
            self.assertGreaterEqual(app.screen.query_one('#status').region.y,
                                    app.screen.query_one('#events').region.bottom)
            self.assertTrue(app.screen.query_one('#context', Static).is_on_screen)
            self.assertEqual(str(app.screen.query_one('#context', Static).render()), context)
            await pilot.press('ctrl+q')

    async def test_typed_aws_login_requires_choice_and_then_actual_identity_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            command = ['aws', 'login', '--profile', 'selected-owner', '--region', 'us-east-1']
            environment = {'AWS_LOGIN_CACHE_DIRECTORY': str(Path(directory).resolve() / 'private cache')}
            blocked = {
                'operation_id': identifier, 'state': 'blocked', 'step': 'identity_inspect', 'revision': 7,
                'pending_interaction': None, 'next_actions': ['advance', 'back'],
                'presentation': {'title': 'Check everyday AWS identity', 'purpose': 'Everyday memory access',
                                 'account': '123456789012', 'profile': 'selected-owner', 'verified': False},
                'handoff': {'type': 'aws_login', 'reason': 'Sign in privately.', 'command': command,
                            'environment': environment},
            }
            controller = Mock(home=Path(directory))
            controller.status.return_value = blocked
            controller.advance.return_value = {
                'operation_id': identifier, 'state': 'completed', 'step': 'done',
                'pending_interaction': None, 'outcome': 'Ready',
                'presentation': {'account': '123456789012', 'profile': 'selected-owner', 'verified': True,
                                 'principal': 'arn:aws:iam::123456789012:user/actual-owner'},
            }
            with patch('tools.tui.administration.AdminOperations', return_value=controller):
                app = WorkflowApp('Sign in', ['Choose access'], lambda ui: present(ui, Path(directory), operation_id=identifier))
                with patch.object(app, 'external', return_value=Mock(returncode=0)) as external:
                    async with app.run_test(size=(80, 24)) as pilot:
                        await pilot.pause()
                        self.assertIsInstance(app.screen, ChooseScreen)
                        self.assertIn('not verified', str(app.screen.query_one('#dialog-context', Static).render()))
                        external.assert_not_called()
                        controller.advance.assert_not_called()
                        await pilot.press('enter')
                        await pilot.pause()
                        external.assert_called_once_with(command, {'env': {**os.environ, **environment}, 'check': False})
                        # Status 0 inspects the actual identity without an intermediate screen.
                        controller.advance.assert_called_once_with(identifier, expected_revision=7)
                        self.assertIn('actual-owner', str(app.screen.query_one('#context', Static).render()))
                        controller.respond.assert_not_called()
                        await pilot.press('ctrl+q')

    async def test_new_profile_headless_signin_shows_and_launches_the_same_scoped_command(self):
        import shlex
        from tools.administration import AdminOperations
        from tools.setup import Wizard

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home, cache = root / 'home', root / 'cache with spaces'
            env = {'AWS_CONFIG_FILE': str(root / 'absent-config'),
                   'AWS_SHARED_CREDENTIALS_FILE': str(root / 'absent-credentials')}
            controller = AdminOperations(home)
            identifier = controller.begin('setup', {'mode': 'existing'})['operation_id']
            Wizard(home).settings({'MAVEN_STACK_NAME': 'test-stack', 'MAVEN_AWS_REGION': 'us-east-1'})
            # Resume the blocked identity inspection saved by a pre-fix installer.
            state = controller._load(identifier)
            state.update(step='identity_inspect', state='blocked', pending_interaction=None,
                         error='selected_login_configuration_unavailable')
            state['answers']['identity'] = {'profile': 'fresh-owner', 'cache': str(cache)}
            controller._save(state)
            app = WorkflowApp('Existing stack', [], lambda ui: present(ui, home, operation_id=identifier))
            identity = {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/test-owner'}
            with patch.dict(os.environ, env), patch.object(app, 'external', return_value=Mock(returncode=0)) as launch:
                async with app.run_test(size=(100, 34)) as pilot:
                    await pilot.pause()
                    await pilot.click('#accept')  # Retry the stopped inspection, not the login.
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ChooseScreen)
                    expected = ['aws', 'login', '--profile', 'fresh-owner', '--region', 'us-east-1', '--remote']
                    shown = app.screen.query_one('#choice-details', TextArea).text
                    self.assertIn(shlex.join(['env', 'AWS_LOGIN_CACHE_DIRECTORY=' + str(cache), *expected]), shown)
                    self.assertIn('Create profile', {v: k for k, v in app.screen.choices}['remote'])
                    launch.assert_not_called()
                    with patch('tools.setup_common.inspect_login_identity', return_value=identity) as inspect:
                        await pilot.press('down', 'enter')
                        await pilot.pause()
                    launch.assert_called_once_with(expected, {'env': {**os.environ, 'AWS_LOGIN_CACHE_DIRECTORY': str(cache)},
                                                              'check': False})
                    inspect.assert_called_once()
                    review = controller.status(identifier)['pending_interaction']
                    self.assertEqual(review['review']['identity'], identity)
                    self.assertEqual(review['key'], 'identity_approval')
                    await pilot.press('ctrl+q')

    async def test_rejected_migration_shows_safe_diagnostics_without_auto_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            controller = Mock(home=Path(directory))
            controller.status.return_value = {
                'operation_id': identifier, 'state': 'blocked', 'step': 'migrate',
                'error': 'migration_rejected',
                'error_detail': 'Migration: 020_secret_adapter_grants.sql; statement: 10; SQLSTATE: 42601; rollback confirmed',
                'pending_interaction': None, 'next_actions': ['advance'], 'receipts': []}
            with patch('tools.tui.administration.AdminOperations', return_value=controller):
                app = WorkflowApp('Update MAVEN', ['Database upgrade'],
                                  lambda ui: present(ui, Path(directory), operation_id=identifier))
                async with app.run_test(size=(110, 38)) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ReviewScreen)
                    displayed = '\n'.join(widget.text for widget in app.screen.query(TextArea))
                    self.assertIn('020_secret_adapter_grants.sql', displayed)
                    self.assertIn('42601', displayed)
                    self.assertIn('rollback confirmed', displayed)
                    self.assertIn('not a service outage', displayed)
                    controller.database_status.assert_not_called()
                    controller.advance.assert_not_called()
                    await pilot.press('ctrl+q')

    async def test_paused_database_is_awaited_then_the_same_step_retried(self):
        for statuses, retried in ((['warming', 'warming', 'awake'], True), (['not_authorized'], False)):
            with self.subTest(statuses=statuses), tempfile.TemporaryDirectory() as directory:
                identifier = str(uuid4())
                controller = Mock(home=Path(directory))
                controller.status.return_value = {
                    'operation_id': identifier, 'state': 'blocked', 'step': 'verify', 'error': 'warming',
                    'read_only_retry': True, 'pending_interaction': None, 'next_actions': ['advance'], 'receipts': []}
                controller.database_status.side_effect = statuses
                controller.advance.return_value = {
                    'operation_id': identifier, 'state': 'cancelled', 'step': 'done', 'pending_interaction': None}
                clock = Mock(now=0.0)
                fake_time = Mock(monotonic=lambda: clock.now)
                fake_time.sleep.side_effect = lambda seconds: setattr(clock, 'now', clock.now + seconds)
                with patch('tools.tui.administration.AdminOperations', return_value=controller), \
                        patch('tools.tui.administration.time', fake_time), \
                        patch('tools.memory.client.time', fake_time):
                    app = WorkflowApp('Setup', ['Setup'], lambda ui: present(ui, Path(directory), operation_id=identifier))
                    async with app.run_test(size=(80, 24)) as pilot:
                        await pilot.pause(0.5)
                        self.assertEqual(controller.database_status.call_count, len(statuses))
                        if retried:
                            controller.advance.assert_called_once_with(identifier)
                            self.assertNotIsInstance(app.screen, ReviewScreen)
                            self.assertGreaterEqual(clock.now, 2 * 5)  # Checks are spaced by the wake interval.
                        else:
                            controller.advance.assert_not_called()
                            self.assertIsInstance(app.screen, ReviewScreen)
                            self.assertIn('Administration stopped', app.screen.heading)
                        await pilot.press('ctrl+q')

    async def test_aws_login_manual_back_and_pause_never_launch_a_command(self):
        for action in ('manual', 'back', 'pause'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                identifier = str(uuid4())
                state = {
                    'operation_id': identifier, 'state': 'blocked', 'step': 'identity_inspect',
                    'pending_interaction': None, 'next_actions': ['advance', 'back'],
                    'handoff': {'type': 'aws_login', 'reason': 'Authenticate selected access.',
                                'command': ['aws', 'login', '--profile', 'owner'], 'environment': {}},
                }
                controller = Mock(home=Path(directory))
                controller.status.return_value = state
                controller.back.return_value = {
                    'operation_id': identifier, 'state': 'awaiting_input', 'step': 'identity',
                    'pending_interaction': {'kind': 'form', 'title': 'Change AWS selection',
                                            'fields': [{'name': 'profile', 'label': 'Profile'}]},
                }
                with patch('tools.tui.administration.AdminOperations', return_value=controller):
                    app = WorkflowApp('Sign in', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                    with patch.object(app, 'external') as external:
                        async with app.run_test(size=(100, 34)) as pilot:
                            await pilot.pause()
                            options = app.screen.query_one('#choices', OptionList)
                            options.highlighted = [value for _, value in app.screen.choices].index(action)
                            await pilot.press('enter')
                            await pilot.pause()
                            if action == 'manual':
                                self.assertIsInstance(app.screen, ReviewScreen)
                                self.assertIn('--profile owner', app.screen.query_one('#review-body', TextArea).text)
                            elif action == 'back':
                                controller.back.assert_called_once_with(identifier)
                                self.assertIsInstance(app.screen, FormScreen)
                            else:
                                self.assertTrue(app.finished)
                                self.assertEqual(app.exit_code, 130)
                            external.assert_not_called()
                            controller.advance.assert_not_called()
                            controller.respond.assert_not_called()
                            await pilot.press('ctrl+q')

    async def test_failed_signin_offers_manual_fallback_without_identity_claim(self):
        for failure in (OSError('unavailable terminal'), Mock(returncode=9)):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                identifier = str(uuid4())
                state = {
                    'operation_id': identifier, 'state': 'blocked', 'step': 'identity_inspect',
                    'pending_interaction': None, 'next_actions': ['advance', 'back'],
                    'handoff': {'type': 'aws_login', 'reason': 'Authenticate selected access.',
                                'command': ['aws', 'login', '--profile', 'owner', '--region', 'us-east-1'],
                                'environment': {'AWS_LOGIN_CACHE_DIRECTORY': str(Path(directory).resolve() / 'cache')}},
                }
                controller = Mock(home=Path(directory))
                controller.status.return_value = state
                with patch('tools.tui.administration.AdminOperations', return_value=controller):
                    app = WorkflowApp('Sign in', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                    with patch.object(app, 'external', side_effect=failure if isinstance(failure, Exception) else None,
                                      return_value=failure) as external:
                        async with app.run_test(size=(80, 24)) as pilot:
                            await pilot.pause()
                            await pilot.press('enter')
                            await pilot.pause()
                            self.assertIsInstance(app.screen, ChooseScreen)
                            self.assertIn('not verified', app.screen.query_one('#choice-details', TextArea).text)
                            external.assert_called_once()
                            controller.advance.assert_not_called()
                            controller.respond.assert_not_called()
                            app.screen.query_one('#choices', OptionList).highlighted = [
                                value for _, value in app.screen.choices].index('manual')
                            await pilot.press('enter')
                            await pilot.pause()
                            self.assertIsInstance(app.screen, ReviewScreen)
                            self.assertIn('--profile owner', app.screen.query_one('#review-body', TextArea).text)
                            await pilot.press('ctrl+q')

    async def test_review_back_uses_controller_and_decline_does_not_mutate(self):
        for back in (False, True):
            with self.subTest(back=back), tempfile.TemporaryDirectory() as directory:
                identifier = str(uuid4())
                question = {'kind': 'review', 'title': 'Review existing instance', 'review': {'instance_id': 'exact-instance'},
                            'review_digest': 'exact-review-digest', 'interaction_id': 'exact-interaction', 'revision': 7}
                state = {'operation_id': identifier, 'state': 'awaiting_decision', 'step': 'connect_review',
                         'pending_interaction': question, 'next_actions': ['respond', 'back']}
                controller = Mock(home=Path(directory))
                controller.status.return_value = state
                controller.back.return_value = {
                    'operation_id': identifier, 'state': 'awaiting_input', 'step': 'identity',
                    'pending_interaction': {'kind': 'form', 'title': 'Change AWS selection',
                                            'fields': [{'name': 'profile', 'label': 'Profile'}]},
                }
                with patch('tools.tui.administration.AdminOperations', return_value=controller):
                    app = WorkflowApp('Review', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                    async with app.run_test(size=(100, 34)) as pilot:
                        await pilot.pause()
                        await pilot.click('#back' if back else '#decline')
                        await pilot.pause()
                        controller.respond.assert_not_called()
                        controller.advance.assert_not_called()
                        if back:
                            controller.back.assert_called_once_with(identifier)
                            self.assertIsInstance(app.screen, FormScreen)
                        else:
                            controller.back.assert_not_called()
                            self.assertEqual(app.exit_code, 130)
                        await pilot.press('ctrl+q')

    async def test_single_approval_records_actual_terminal_event_with_exact_review_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            review = {'instance_id': 'exact-instance', 'action': 'connect'}
            question = {'kind': 'review', 'title': 'Review existing instance', 'review': review,
                        'review_digest': 'exact-review-digest', 'interaction_id': 'exact-interaction', 'revision': 7,
                        'description': 'Connect the exact existing instance without deployment.',
                        'details': 'No cloud resources will be created.'}
            state = {'operation_id': identifier, 'state': 'awaiting_decision', 'step': 'connect_review',
                     'pending_interaction': question, 'next_actions': ['respond', 'back']}
            controller = Mock(home=Path(directory))
            controller.status.return_value = state
            controller.respond.return_value = {
                'operation_id': identifier, 'state': 'completed', 'step': 'done',
                'pending_interaction': None, 'outcome': 'Ready',
            }
            with patch('tools.tui.administration.AdminOperations', return_value=controller):
                app = WorkflowApp('Review', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                async with app.run_test(size=(100, 34)) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ReviewScreen)
                    controller.respond.assert_not_called()
                    await pilot.click('#accept')
                    await pilot.pause()
                    self.assertTrue(app.finished)
                    self.assertEqual(len(app.screen_stack), 1)
                    controller.respond.assert_called_once()
                    operation, interaction, revision, response = controller.respond.call_args.args
                    self.assertEqual((operation, interaction, revision), (identifier, 'exact-interaction', 7))
                    self.assertIs(response['approved'], True)
                    self.assertEqual(response['review_digest'], question['review_digest'])
                    source = response['source']
                    self.assertEqual(source['origin'], 'owner MAVEN administration TUI')
                    self.assertIn(question['title'], source['excerpt'])
                    self.assertIn(question['review_digest'], source['excerpt'])
                    self.assertEqual(question['review'], review)
                    await pilot.press('ctrl+q')

    async def test_terminal_approval_still_requires_separate_destructive_target_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            question = {
                'kind': 'review', 'title': 'Purge exact selected instance',
                'review': {'instance_id': 'exact-instance', 'action': 'purge'},
                'review_digest': 'exact-review-digest', 'interaction_id': 'exact-interaction', 'revision': 7,
                'confirmations': {'purge': 'exact-instance'},
            }
            controller = Mock(home=Path(directory))
            controller.status.return_value = {
                'operation_id': identifier, 'state': 'awaiting_decision', 'step': 'removal_plan',
                'pending_interaction': question, 'next_actions': ['respond'],
            }
            controller.respond.return_value = {
                'operation_id': identifier, 'state': 'completed', 'step': 'done',
                'pending_interaction': None, 'outcome': 'Ready',
            }
            with patch('tools.tui.administration.AdminOperations', return_value=controller):
                app = WorkflowApp('Destructive review', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    await pilot.click('#accept')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, FormScreen)
                    controller.respond.assert_not_called()
                    self.assertIn('exact-instance', str(app.screen.query_one('.field-help', Static).render()))
                    app.screen.query_one('#field-0', Input).value = 'exact-instance'
                    await pilot.click('#submit')
                    await pilot.pause()
                    controller.respond.assert_called_once()
                    response = controller.respond.call_args.args[3]
                    self.assertEqual(response['confirmations'], {'purge': 'exact-instance'})
                    self.assertEqual(response['review_digest'], question['review_digest'])
                    self.assertIn(question['review_digest'], response['source']['excerpt'])
                    await pilot.press('ctrl+q')

    async def test_form_back_and_quit_preserve_original_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            question = {'kind': 'form', 'title': 'Choose AWS access',
                        'fields': [{'name': 'profile', 'label': 'Profile', 'default': 'owner',
                                    'help': 'Choose everyday access.'}]}
            state = {'operation_id': identifier, 'state': 'awaiting_input', 'step': 'identity',
                     'pending_interaction': question, 'next_actions': ['respond', 'back']}
            controller = Mock(home=Path(directory))
            controller.status.return_value = state
            controller.back.return_value = state
            with patch('tools.tui.administration.AdminOperations', return_value=controller):
                app = WorkflowApp('Selection', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    await pilot.click('#back')
                    await pilot.pause()
                    controller.back.assert_called_once_with(identifier)
                    self.assertIsInstance(app.screen, FormScreen)
                    await pilot.press('ctrl+q')
                    await pilot.pause()
                    self.assertEqual(app.exit_code, 130)
                    controller.respond.assert_not_called()
                    controller.advance.assert_not_called()
                    self.assertEqual(state['operation_id'], identifier)
                    self.assertIs(state['pending_interaction'], question)

    async def test_secrets_repair_uses_bound_review_without_claiming_memory_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            review = {'settings': {'MAVEN_SECRETS_BUCKET': 'exact-private-store'},
                      'policy_principal': 'arn:aws:iam::123456789012:user/daily',
                      'permission_checks': 'Read only; no canary writes. Write access remains unverified.',
                      'retention': 'Independent store is retained when memory service is removed.'}
            question = {'kind': 'review', 'key': 'secrets_review', 'title': 'Review exact secrets binding',
                        'review': review, 'review_digest': 'a' * 64,
                        'interaction_id': str(uuid4()), 'revision': 3, 'confirmations': {}}
            controller = Mock(home=Path(directory))
            controller.status.return_value = {'operation_id': identifier, 'kind': 'repair',
                'state': 'awaiting_decision', 'step': 'secrets_prepare',
                'pending_interaction': question, 'next_actions': ['respond']}
            controller.respond.return_value = {'operation_id': identifier, 'kind': 'repair',
                'state': 'completed', 'step': 'secrets_apply', 'pending_interaction': None,
                'outcome': {'secrets_ready': True, 'write_access_verified': False}}
            with patch('tools.tui.administration.AdminOperations', return_value=controller), \
                    patch('tools.tui.harness.finish_setup') as finish:
                app = WorkflowApp('Secrets Repair', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                async with app.run_test(size=(100, 34)) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ReviewScreen)
                    controller.respond.assert_not_called()
                    await pilot.click('#accept')
                    await pilot.pause()
                    response = controller.respond.call_args.args[3]
                    self.assertEqual(response['review_digest'], question['review_digest'])
                    self.assertIn(question['review_digest'], response['source']['excerpt'])
                    finish.assert_not_called()
                    controller.advance.assert_not_called()
                    await pilot.press('ctrl+q')

    async def test_completed_setup_finishes_harness_without_project_forms(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            identifier = str(uuid4())
            outcome = {'instance_ready': True, 'home': str(home), 'account': '123456789012',
                       'region': 'us-east-1', 'instance_id': 'exact-instance', 'profile': 'owner'}
            controller = Mock(home=Path(directory))
            controller.status.return_value = {
                'operation_id': identifier, 'kind': 'setup', 'state': 'awaiting_input', 'step': 'optional_project',
                'pending_interaction': {'kind': 'form', 'key': 'optional_project', 'title': 'Obsolete project prompt'},
                'next_actions': ['advance'],
            }
            controller.advance.return_value = {
                'operation_id': identifier, 'kind': 'setup', 'state': 'completed', 'step': 'verify',
                'pending_interaction': None, 'outcome': outcome,
            }
            with patch('tools.tui.administration.AdminOperations', return_value=controller), \
                    patch('tools.tui.harness.finish_setup', return_value={}) as finish:
                app = WorkflowApp('Finish setup', [], lambda ui: present(ui, home, operation_id=identifier))
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    self.assertTrue(app.finished)
                    self.assertEqual(len(app.screen_stack), 1)
                    controller.advance.assert_called_once_with(identifier)
                    controller.respond.assert_not_called()
                    finish.assert_called_once_with(app.interaction, home, outcome)
                    await pilot.press('ctrl+q')

    async def test_administration_handoff_is_visible_and_only_explicit_resume_advances(self):
        for state, resume in (('blocked', False), ('uncertain', True)):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / 'private home'
                identifier = str(uuid4())
                command = ['aws', 'login', '--profile', 'selected-owner']
                cache = str(home / 'login cache')
                error = 'Selected identity needs external authentication.'
                reason = 'Authenticate privately, then return to this same operation.'
                recovery = 'Do not replace the retained request receipt.'
                result = {
                    'operation_id': identifier, 'state': state, 'step': 'identity_inspect',
                    'pending_interaction': None, 'error': error, 'recovery': recovery,
                    'handoff': {'reason': reason, 'command': command,
                                'environment': {'AWS_LOGIN_CACHE_DIRECTORY': cache}},
                }
                controller = Mock(home=Path(directory))
                controller.status.return_value = result
                controller.advance.return_value = {
                    'operation_id': identifier, 'state': 'completed', 'step': 'done',
                    'pending_interaction': None, 'outcome': 'Ready',
                }
                with patch('tools.tui.administration.AdminOperations', return_value=controller), \
                        patch('tools.tui.app.subprocess.run') as external:
                    app = WorkflowApp('Administration', ['identity_inspect', 'done'],
                                      lambda ui: present(ui, home, operation_id=identifier))
                    async with app.run_test(size=(100, 34)) as pilot:
                        await pilot.pause()
                        self.assertIsInstance(app.screen, ReviewScreen)
                        body = app.screen.query_one('#review-body', TextArea)
                        self.assertTrue(body.is_on_screen)
                        self.assertTrue(body.read_only)
                        # These are the actual handoff values in the active modal,
                        # not a snapshot of incidental presenter wording.
                        for value in (error, reason, recovery, ' '.join(command),
                                      'AWS_LOGIN_CACHE_DIRECTORY', cache, str(home), identifier):
                            self.assertIn(value, body.text)
                        self.assertIn('--operation ' + identifier, body.text)
                        self.assertIn('--home', body.text)
                        self.assertIn('maven-setup', body.text)
                        controller.advance.assert_not_called()
                        await pilot.click('#accept' if resume else '#decline')
                        await pilot.pause()
                        if resume:
                            controller.advance.assert_called_once_with(identifier)
                            self.assertEqual(app.exit_code, 0)
                        else:
                            controller.advance.assert_not_called()
                            self.assertEqual(app.exit_code, 130)
                        controller.respond.assert_not_called()
                        external.assert_not_called()
                        self.assertEqual(result['state'], state)
                        await pilot.press('ctrl+q')

    async def test_local_update_handoff_does_not_request_aws_sign_in(self):
        with tempfile.TemporaryDirectory() as directory:
            home, identifier = Path(directory), str(uuid4())
            command = ['python3', '/checkout/install.py', '--home', str(home)]
            reason = 'Install the selected checkout, then restart this process.'
            controller = Mock(home=Path(directory))
            controller.status.return_value = {
                'operation_id': identifier, 'state': 'blocked', 'step': 'update_local_wait',
                'pending_interaction': None,
                'handoff': {'type': 'local_update', 'reason': reason,
                            'command': command, 'environment': {}}}
            with patch('tools.tui.administration.AdminOperations', return_value=controller):
                app = WorkflowApp('Update MAVEN', ['update_local_wait'],
                                  lambda ui: present(ui, home, operation_id=identifier))
                async with app.run_test(size=(100, 34)) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ReviewScreen)
                    body = app.screen.query_one('#review-body', TextArea).text
                    self.assertIn(reason, body)
                    self.assertIn(' '.join(command), body)
                    self.assertNotIn('sign-in', body.casefold())
                    await pilot.press('ctrl+q')
                    controller.advance.assert_not_called()

    async def test_destructive_action_requires_exact_six_character_or_short_identity(self):
        for identity in ('abcdef-long-target-identity', 'abc'):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / identity
                target.write_text('preserve until approved')

                def operation(ui):
                    ui.set_context('Task: Remove selected target\nAccess: Owner-approved removal\n'
                                   'AWS: 123456789012 | Profile: owner | inspected\n'
                                   'Principal: arn:aws:iam::123456789012:user/owner')
                    if ui.review('Remove selected target', str(target), identity=identity, destructive=True):
                        target.unlink()
                    return 0

                app = WorkflowApp('Approval boundary', ['Review'], operation)
                async with app.run_test(size=(100, 34)) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ReviewScreen)
                    app.screen.query_one('#confirmation', Input).value = 'wrong'
                    await pilot.click('#accept')
                    await pilot.pause()
                    self.assertTrue(target.exists())
                    await pilot.resize_terminal(80, 24)
                    await pilot.pause()
                    for selector in ('#dialog-context', '.target', '#confirmation', '#accept'):
                        widget = app.screen.query_one(selector)
                        self.assertTrue(widget.is_on_screen, selector)
                        self.assertLessEqual(widget.region.bottom, 23, selector)
                    self.assertIn(identity, str(app.screen.query_one('.target', Static).render()))
                    await pilot.resize_terminal(100, 34)
                    app.screen.query_one('#confirmation', Input).value = identity[:6]
                    await pilot.click('#accept')
                    await pilot.pause()
                    self.assertFalse(target.exists())
                    self.assertEqual(app.exit_code, 0)
                    await pilot.press('ctrl+q')

    async def test_quit_waits_for_current_action_and_prevents_the_next_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt'
            entered, release = threading.Event(), threading.Event()

            def operation(ui):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release the current action')
                receipt.write_text('first action durably complete')
                ui.checkpoint()
                receipt.write_text('unapproved subsequent action')

            app = WorkflowApp('Pause boundary', ['Execute'], operation)
            try:
                async with app.run_test(size=(100, 34)) as pilot:
                    await pilot.pause()
                    self.assertTrue(entered.is_set())
                    await pilot.press('ctrl+q')
                    self.assertFalse(app.finished)
                    self.assertEqual(app.activity_state, 'pause_requested')
                    self.assertIn('waiting for a safe checkpoint', str(app.screen.query_one('#status', Static).render()))
                    self.assertTrue(app.screen.query_one('#busy').display)
                    release.set()
                    await pilot.pause()
                    self.assertEqual(receipt.read_text(), 'first action durably complete')
                    self.assertEqual(app.exit_code, 130)
                    self.assertEqual(app.activity_state, 'paused')
                    self.assertTrue(app.activity_text.startswith('Paused'))
            finally:
                release.set()

    async def test_awake_probe_never_automatically_replays_a_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            controller = Mock(home=Path(directory))
            controller.status.return_value = {
                'operation_id': identifier, 'kind': 'update', 'state': 'uncertain',
                'step': 'migrate', 'error': 'warming', 'pending_interaction': None,
                'next_actions': ['advance'], 'receipts': []}
            controller.database_status.return_value = 'awake'
            with patch('tools.tui.administration.AdminOperations', return_value=controller):
                app = WorkflowApp('Update', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                async with app.run_test(size=(100, 34)) as pilot:
                    await pilot.pause()
                    controller.database_status.assert_called_once()
                    controller.advance.assert_not_called()
                    self.assertIsInstance(app.screen, ReviewScreen)
                    displayed = '\n'.join(widget.text for widget in app.screen.query(TextArea))
                    self.assertIn('tools.tui.administration', displayed)
                    self.assertNotIn('maven-setup', displayed)
                    self.assertNotIn('aws login', displayed)
                    await pilot.press('ctrl+q')

    async def test_child_failure_keeps_exit_status_but_not_arguments_or_output(self):
        import subprocess
        def operation(ui):
            raise subprocess.CalledProcessError(17, ['child', 'private-argument'],
                                                output='private-output', stderr='private-error')
        app = WorkflowApp('Child failure', [], operation)
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            self.assertEqual(app.exit_code, 2)
            self.assertIn('status 17', app.activity_text)
            self.assertNotIn('private-', app.activity_text)
            self.assertIn('not proof of an authentication failure', app.activity_text)
            await pilot.press('ctrl+q')

    async def test_rejected_form_reason_is_visible_on_the_current_form(self):
        from tools.administration import AdminInputError
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            question = {'kind': 'form', 'title': 'Select access', 'interaction_id': 'selection',
                        'revision': 1, 'fields': [{'name': 'profile', 'label': 'Named profile', 'default': 'owner'}]}
            result = {'kind': 'home', 'operation_id': identifier, 'state': 'waiting',
                      'step': 'identity', 'pending_interaction': question,
                      'next_actions': ['respond'], 'receipts': []}
            controller = Mock(home=Path(directory))
            controller.status.return_value = result
            controller.respond.side_effect = AdminInputError('Invalid answer for profile: select a named login profile.')
            with patch('tools.tui.administration.AdminOperations', return_value=controller):
                app = WorkflowApp('Selected access', [], lambda ui: present(ui, Path(directory), operation_id=identifier))
                async with app.run_test(size=(100, 34)) as pilot:
                    await pilot.pause()
                    await pilot.click('#submit')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, FormScreen)
                    self.assertIn('Invalid answer for profile', app.screen.description)
                    controller.advance.assert_not_called()
                    await pilot.press('ctrl+q')



class UiAvailabilityTests(unittest.TestCase):
    def test_textual_supported_lower_bound_and_prereleases(self):
        from tools.tui.bootstrap import ui_available
        for version, supported in (('2.0.0', False), ('2.1.0', True), ('2.9.0', True),
                                   ('3.0.0', False), ('2.1.0rc1', False)):
            with self.subTest(version=version), \
                    patch('tools.tui.bootstrap.util.find_spec', return_value=object()), \
                    patch('tools.tui.bootstrap.metadata.version',
                          side_effect=lambda name: version if name == 'textual' else '25.0'):
                self.assertEqual(ui_available(), supported)

    def test_auth_command_is_executable_with_exact_cache_profile_and_region(self):
        import shlex
        from tools.tui.administration import _instructions
        result = {'kind': 'home', 'step': 'identity_inspect', 'state': 'blocked',
                  'error': 'selected_login_unavailable_or_expired',
                  'handoff': {'type': 'aws_login', 'reason': 'Selected login expired.',
                              'command': ['aws', 'login', '--profile', 'owner profile', '--region', 'us-east-1'],
                              'environment': {'AWS_LOGIN_CACHE_DIRECTORY': '/private/selected cache'}}}
        rendered = _instructions(result, Path('/private/home space'), 'retained-operation')
        command = rendered.split('External command:\n', 1)[1].split('\n', 1)[0]
        self.assertEqual(shlex.split(command), ['env', 'AWS_LOGIN_CACHE_DIRECTORY=/private/selected cache',
                         'aws', 'login', '--profile', 'owner profile', '--region', 'us-east-1'])
        self.assertIn('tools.tui.administration', rendered)
        self.assertNotIn('maven-setup', rendered)



if __name__ == '__main__':
    unittest.main()
