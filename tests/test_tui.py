"""Local terminal approval/pause boundaries; no cloud or provisioned environments."""
import tempfile
import threading
import unittest
from pathlib import Path

from textual.widgets import Input

from tools.tui.app import ReviewScreen, WorkflowApp


class TuiBoundaryTests(unittest.IsolatedAsyncioTestCase):
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
