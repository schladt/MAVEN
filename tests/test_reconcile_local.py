import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import uuid4

from tools.reconcile_local import LocalStore, StaleView
from tools.reconcile_records import brief_id, make_change, summarize, tombstone


def note(text, key=None):
    return make_change(None, "note", {"text": text}, "observed", record_id=key or str(uuid4()))


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = str(uuid4())
        self.store = LocalStore(self.root, self.project)

    def test_offline_orientation_discovers_skills_not_procedures_or_link_content(self):
        first = self.store.orient()
        self.assertEqual(first, {"source": "local", "project_id": self.project, "token": first["token"],
                                 "brief_id": brief_id(self.project), "brief": None, "skills": []})
        path = self.root / "ops" / "SKILL.md"
        path.parent.mkdir()
        path.write_text("linked procedure text is never imported")
        link = make_change(None, "skill", {"name": "Ship", "when_to_use": "After approval", "path": "ops/SKILL.md"},
                           "described procedure", record_id=str(uuid4()))
        owned = make_change(None, "skill", {"name": "Check", "when_to_use": "After deploy", "steps": ["probe endpoint"],
                                           "verification": "Read response"}, "described check", record_id=str(uuid4()))
        brief = make_change(None, "brief", {"purpose": "Analytics", "current": "Built, not deployed",
                            "proposals": ["Database change"], "read_first": [{"link": "README.md", "answers": "Setup",
                            "status": "current"}], "approval_boundaries": "Deployment requires owner approval"},
                            "orient agents", record_id=first["brief_id"])
        saved = self.store.apply([owned, brief, link], first["token"])
        fresh = LocalStore(self.root, self.project)
        view = fresh.orient()
        self.assertEqual(view["token"], saved["token"])
        self.assertEqual(view["brief"], brief["record"])
        self.assertCountEqual(view["skills"], [{"id": v["record"]["id"], "name": v["record"]["data"]["name"],
                              "when_to_use": v["record"]["data"]["when_to_use"]} for v in [owned, link]])
        self.assertNotIn("probe endpoint", json.dumps(view))
        self.assertIn("probe endpoint", json.dumps(fresh.read([owned["record"]["id"]], saved["token"])))
        self.assertNotIn(b"linked procedure text", fresh.path.read_bytes())
        self.assertEqual(path.read_text(), "linked procedure text is never imported")
        self.assertEqual(fresh.path.stat().st_mode & 0o077, 0)
        self.assertEqual(fresh.path.parent.stat().st_mode & 0o077, 0)

    def test_offline_edits_and_selective_retry_do_not_hide_unresolved_records(self):
        key = str(uuid4())
        old = note("Old governing decision evidence", key)
        second = LocalStore(self.root, self.project)
        observed = self.store.apply([old], self.store.inventory()["token"])["token"]
        actual_work = make_change(old, "note", {"text": "Owner clarified evidence"}, "owner clarification", record_id=key)
        new_risk = make_change(None, "risk", {"title": "Legal withdrew approval", "state": "open"}, "notification",
                               record_id=str(uuid4()))
        saved = second.apply([actual_work, new_risk], observed)
        unrelated = note("Verified unrelated work")
        with self.assertRaises(StaleView):
            self.store.apply([unrelated], observed)
        with self.assertRaises(StaleView):
            self.store.read([key], observed)
        self.assertCountEqual(self.store.inventory()["items"], list(map(summarize, [actual_work, new_risk])))
        after = self.store.apply([unrelated], saved["token"])
        self.assertEqual(after["changed"], [unrelated["record"]["id"]])
        self.assertEqual(second.read([key, new_risk["record"]["id"]], after["token"]), [actual_work, new_risk])
        for bad in [[key, key], [str(uuid4()), key]]:
            with self.assertRaises(ValueError):
                second.read(bad, after["token"])
        # Losing the response does not generate another timestamp, token or history entry.
        retry = self.store.apply([actual_work, unrelated], second.inventory()["token"])
        self.assertEqual(retry, {"token": after["token"], "changed": []})
        self.assertEqual(self.store.apply([], after["token"]), retry)

    def test_lookup_guards_both_present_and_missing_records(self):
        token = self.store.orient()["token"]
        first = note("keep")
        key = first["record"]["id"]
        absent = str(uuid4())
        self.assertIsNone(self.store.lookup(key, token))
        current = self.store.apply([first], token)["token"]
        self.assertEqual(self.store.lookup(key, current), first)
        self.assertIsNone(self.store.lookup(absent, current))
        for record_id in [key, absent]:
            with self.assertRaises(StaleView):
                self.store.lookup(record_id, token)
        with self.assertRaises(StaleView):
            self.store.read([], token)
        self.assertEqual(self.store.read([], current), [])

    def test_interleaved_new_ids_cannot_both_publish_against_one_project_view(self):
        token = self.store.inventory()["token"]
        barrier = Barrier(2)
        def writer(text):
            peer = LocalStore(self.root, self.project)
            addition = note(text)
            barrier.wait()
            try:
                return peer.apply([addition], token)
            except StaleView:
                return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(writer, ["local writer one", "local writer two"]))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(len(self.store.inventory()["items"]), 1)

    def test_erasure_physically_purges_managed_content_and_atomically_fences_stale_imports(self):
        original = note("managed wording to scrub A" * 500)
        key = original["record"]["id"]
        correction = make_change(original, "note", {"text": "managed wording to scrub B" * 500},
                                 "managed reason to scrub", record_id=key)
        token = self.store.apply([correction], self.store.inventory()["token"])["token"]
        self.assertIn(b"managed wording to scrub A", self.store.path.read_bytes())
        marker = tombstone(correction, record_id=key)
        token = self.store.apply([marker], token)["token"]
        returned = LocalStore(self.root, self.project)
        self.assertEqual(returned.read([key], token), [marker])
        self.assertEqual(marker, tombstone(marker, record_id=key))
        self.assertEqual(set(marker["record"]), {"id", "written_at", "erased"})
        self.assertEqual(marker["history"], [])
        self.assertEqual(returned.inventory()["items"][0]["kind"], "erased")
        for file in self.store.path.parent.iterdir():
            self.assertNotIn(b"managed wording to scrub", file.read_bytes())
            self.assertNotIn(b"managed reason to scrub", file.read_bytes())
        # Even a newer offline live version loses. The entire failed batch rolls back.
        late = make_change(correction, "note", {"text": "offline correction"}, "offline edit", record_id=key)
        late["record"]["written_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "erased"):
            returned.apply([note("rolled back"), late], token)
        self.assertEqual(returned.inventory(), {"token": token, "items": [summarize(marker)]})
        with self.assertRaisesRegex(ValueError, "erased"):
            make_change(marker, "note", {"text": "no"}, "attempted resurrection", record_id=key)

    def test_correction_preserves_prior_wording_without_touching_other_records(self):
        old, unrelated = note("old"), note("separate")
        key = old["record"]["id"]
        merged = make_change(old, "note", {"text": "wrong merge"}, "merged evidence", record_id=key)
        token = self.store.apply([merged, unrelated], self.store.inventory()["token"])["token"]
        with self.assertRaisesRegex(ValueError, "history"):
            self.store.apply([old], token)
        undone = make_change(merged, "note", old["record"]["data"], "owner corrected mistaken merge", record_id=key)
        after = self.store.apply([undone], token)
        self.assertEqual(after["changed"], [key])
        self.assertEqual(self.store.read([unrelated["record"]["id"], key], after["token"]), [unrelated, undone])
        self.assertNotEqual(undone["record"]["written_at"], old["record"]["written_at"])

    def test_other_project_and_legacy_runtime_do_not_supply_memory(self):
        runtime = self.root / ".maven" / "runtime"
        runtime.mkdir(parents=True)
        receipt = runtime / "sessions.json"
        receipt.write_text("not project memory")
        token = self.store.inventory()["token"]
        self.store.apply([note("only first project")], token)
        switched = LocalStore(self.root, str(uuid4()))
        self.assertEqual(switched.inventory()["items"], [])
        self.assertNotEqual(switched.inventory()["token"], token)
        self.assertEqual(receipt.read_text(), "not project memory")
        self.store.path.replace(switched.path)
        with self.assertRaisesRegex(ValueError, "another project"):
            switched.orient()

    def test_rejects_symlinked_private_paths_and_unsafe_permissions(self):
        outside = self.root / "outside"
        outside.mkdir()
        hidden = self.root / ".maven"
        hidden.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            self.store.orient()
        self.assertEqual(list(outside.iterdir()), [])
        hidden.unlink()
        self.store.orient()
        sidecar = self.store.path.with_name(self.store.path.name + "-journal")
        target = outside / "unrelated"
        target.write_text("untouched")
        sidecar.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            self.store.orient()
        sidecar.unlink()
        self.store.path.unlink()
        self.store.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            self.store.orient()
        self.assertEqual(target.read_text(), "untouched")
        self.store.path.unlink()
        self.store.path.touch(mode=0o600)
        os.chmod(self.store.path, 0o644)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.store.inventory()

    def test_ungraceful_process_exit_does_not_require_session_recovery(self):
        script = """\
import os, sys
from pathlib import Path
from tools.reconcile_local import LocalStore
from tools.reconcile_records import make_change
store = LocalStore(Path(sys.argv[1]), sys.argv[2])
value = make_change(None, 'note', {'text':'saved before exit'}, 'observed', record_id=sys.argv[3])
store.apply([value], store.inventory()['token'])
os._exit(0)
"""
        key = str(uuid4())
        subprocess.run([sys.executable, "-c", script, str(self.root), self.project, key], check=True)
        inventory = self.store.inventory()
        self.assertEqual(inventory["items"][0]["summary"], "saved before exit")
        self.assertEqual(self.store.read([key], inventory["token"])[0]["history"], [])
