"""Local terminal approval/pause boundaries; no cloud or provisioned environments."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from textual.widgets import Input, TextArea

from tools.tui.app import ReviewScreen, WorkflowApp
from tools.tui.administration import present


class TuiBoundaryTests(unittest.IsolatedAsyncioTestCase):
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
                controller = Mock()
                controller.status.return_value = result
                controller.advance.return_value = {
                    'operation_id': identifier, 'state': 'completed', 'step': 'done',
                    'pending_interaction': None, 'outcome': 'Ready',
                }
                with patch('tools.tui.administration.AdminOperations', return_value=controller):
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
                        self.assertEqual(result['state'], state)
                        await pilot.press('ctrl+q')

    async def test_destructive_action_requires_exact_six_character_or_short_identity(self):
        for identity in ('abcdef-long-target-identity', 'abc'):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / identity
                target.write_text('preserve until approved')

                def operation(ui):
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
                    release.set()
                    await pilot.pause()
                    self.assertEqual(receipt.read_text(), 'first action durably complete')
                    self.assertEqual(app.exit_code, 130)
            finally:
                release.set()


if __name__ == '__main__':
    unittest.main()
