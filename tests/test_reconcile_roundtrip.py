"""Coordinator acceptance scenarios. Each endpoint persists to its own real project store."""

from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

from tools.reconcile import apply_review, inspect_views, local_write, read_details
from tools.reconcile_local import LocalStore, StaleView
from tools.reconcile_records import make_change


def key(number):
    return str(UUID(int=number))


def observation(title):
    return {"title": title, "observed": title, "occurred_on": "2026-11-27"}


def capture(store, record_id, kind, data, reason="Observed during project work"):
    return local_write(store, store.inventory()["token"], kind, data, reason, record_id=record_id)


def saved(store, record_id):
    return store.read([record_id], store.inventory()["token"])[0]


def put_at(store, record_id, kind, data, timestamp):
    # Import a version with a known original timestamp, without changing the clock.
    store.apply([{"record": {"id": record_id, "kind": kind, "data": data, "written_at": timestamp},
                  "history": []}], store.inventory()["token"], strict_current=False)


def reviewed(view):
    return {name: view[name] for name in ("local_token", "cloud_token")}


def take(record_id, side, reason):
    return {"id": record_id, "take": side, "reason": reason}


class AtCloudCommit(LocalStore):
    """A second handle to the *same* real cloud DB, with a deterministic transport boundary."""

    def __init__(self, store, *, before=None, after=None):
        super().__init__(store.root, store.project_id)
        self.before, self.after = before, after

    def apply(self, changes, expected_token):
        if self.before:
            self.before()
        receipt = super().apply(changes, expected_token)
        if self.after:
            self.after()
        return receipt


class ReconcileRoundtripTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local, self.cloud = (LocalStore(Path(tmp.name) / name, key(1000))
                                  for name in ("worktree", "independent"))
        for store in (self.local, self.cloud):
            store.root.mkdir(mode=0o700)

    def inspect(self, **options):
        return inspect_views(self.local, self.cloud, **options)

    def apply(self, view, *choices):
        return apply_review(self.local, self.cloud, list(choices), **reviewed(view))

    def details(self, view, *ids, include_history=False):
        result = read_details(self.local, self.cloud, list(ids), include_history=include_history, **reviewed(view))
        return {item["id"]: item for item in result["records"]}

    def test_paged_project_context_survives_selective_bidirectional_apply(self):
        accepted, proposal, risk, action, outcome = map(key, range(1, 6))
        for store in (self.local, self.cloud):
            put_at(store, accepted, "decision", {"state": "accepted", "title": "Keep SQLite for launch",
                   "source": "Signed launch criteria"}, "2020-01-01T00:00:00Z")
        capture(self.local, proposal, "decision", {"state": "proposed", "title": "Move reporting at launch",
                                                   "rationale": "Separate reporting load from release traffic"})
        capture(self.cloud, risk, "risk", {"state": "open", "title": "PostgreSQL lacks launch certification"})
        capture(self.local, action, "outcome", observation("Archived the build manifest"))
        capture(self.cloud, outcome, "outcome", observation("Verified build manifest checksum"))
        before = self.inspect(limit=2)
        self.assertEqual((before["status"], before["total"], before["different"]), ("review_required", 5, 4))
        items = list(before["items"])
        page = before
        while page["next_after_id"]:
            page = self.inspect(limit=2, after_id=page["next_after_id"], **reviewed(before))
            items.extend(page["items"])
            self.assertLessEqual(len(items), 5, "pagination repeated inventory items")
        ids = [item["both"]["id"] if "both" in item else item["id"] for item in items]
        self.assertCountEqual(ids, [accepted, proposal, risk, action, outcome])
        index = dict(zip(ids, items))
        self.assertEqual(index[accepted]["both"]["state"], "accepted")
        self.assertIn("SQLite", index[accepted]["both"]["summary"])
        self.assertEqual(index[proposal]["local"]["state"], "proposed")
        self.assertEqual(index[risk]["cloud"]["state"], "open")
        details = self.details(before, accepted, proposal, risk)
        self.assertEqual(details[accepted]["cloud"]["record"]["data"]["source"], "Signed launch criteria")
        self.assertIsNone(details[proposal]["cloud"])
        self.assertIsNone(details[risk]["local"])

        # Leave the launch question to the owner. Nothing about it blocks these observations.
        receipt = self.apply(before, take(action, "local", "Observed action"), take(outcome, "cloud", "Test result"))
        self.assertEqual(receipt["status"], "applied")
        self.assertTrue(receipt["verify_required"])
        after = self.inspect(verify=True)
        self.assertEqual((after["status"], after["different"]), ("review_required", 2))
        actual = self.details(after, accepted, proposal, risk, action, outcome)
        for record_id in (accepted, proposal, risk):
            self.assertEqual(actual[record_id], details[record_id])
        for record_id in (action, outcome):
            self.assertIsNotNone(actual[record_id]["local"])
            self.assertEqual(actual[record_id]["local"], actual[record_id]["cloud"])
            self.assertNotIn("history", actual[record_id]["local"])

    def test_equal_write_times_do_not_choose_between_contradictions(self):
        choice = key(1)
        stamp = "2025-12-01T12:00:00Z"
        put_at(self.local, choice, "decision", {"state": "proposed", "title": "Release to production",
                                                "rationale": "The candidate passed QA"}, stamp)
        put_at(self.cloud, choice, "decision", {"state": "accepted", "title": "Hold release for legal signoff",
                                                "rationale": "Legal has not approved release"}, stamp)
        before = self.inspect()
        self.assertEqual((before["status"], before["different"]), ("review_required", 1))
        versions = self.details(before, choice)[choice]
        self.assertEqual(versions["local"]["record"]["written_at"], versions["cloud"]["record"]["written_at"])
        self.assertNotEqual(versions["local"]["record"]["data"], versions["cloud"]["record"]["data"])
        why = "The legal hold is signed; the release remains a proposal"
        self.apply(before, take(choice, "cloud", why))
        verified = self.inspect(verify=True)
        self.assertEqual(verified["status"], "synced")
        for store in (self.local, self.cloud):
            result = saved(store, choice)
            self.assertEqual(result["record"], versions["cloud"]["record"])
            self.assertEqual(result["history"], [{"record": versions["local"]["record"], "reason": why}])
        # An ordinary repeat does not mint a write, timestamp, or history entry.
        self.assertEqual(self.inspect(), verified)
        self.apply(verified, take(choice, "cloud", "Reviewed again"))
        self.assertEqual(self.inspect(verify=True), verified)

    def test_only_an_unchanged_offline_ancestor_can_take_the_cloud_conversion(self):
        rid = key(1)
        old = make_change(None, 'note', {'text': 'old'}, 'fixture', record_id=rid)
        converted = make_change(old, 'entity', {'name': 'Alpha', 'type': 'project'}, 'convert', record_id=rid)
        self.local.apply([old], self.local.orient()['token'], strict_current=False)
        self.cloud.apply([converted], self.cloud.orient()['token'])
        review = self.inspect()
        self.assertNotIn('history', self.details(review, rid)[rid]['cloud'])
        self.assertEqual(self.details(review, rid, include_history=True)[rid]['cloud'], converted)
        self.apply(review, take(rid, 'local', 'Local version was already converted'))
        self.assertEqual(saved(self.local, rid), converted)
        self.assertEqual(saved(self.cloud, rid), converted)
        divergent = make_change(converted, 'note', {'text': 'unpublished edit'}, 'old client', record_id=rid)
        self.local.apply([divergent], self.local.orient()['token'], strict_current=False)
        local_only = make_change(None, 'note', {'text': 'offline only'}, 'old client', record_id=key(2))
        self.local.apply([local_only], self.local.orient()['token'], strict_current=False)
        view = self.inspect()
        for target, side in ((rid, 'cloud'), (key(2), 'local')):
            with self.assertRaisesRegex(ValueError, 'convert it locally'):
                self.apply(view, take(target, side, 'Reviewed offline version'))
        self.assertEqual(self.inspect(), view)

    def test_invalid_authored_merge_leaves_both_stores_unchanged(self):
        rid = key(1)
        capture(self.local, rid, 'outcome', observation('Build passed'))
        view = self.inspect()
        for kind, data, field in [('note', {'text': 'old kind'}, 'kind'),
                                  ('outcome', {'title': 'Unverified'}, 'occurred_on')]:
            with self.assertRaisesRegex(ValueError, field):
                self.apply(view, {**take(rid, 'merge', 'Review'), 'kind': kind, 'data': data})
        self.assertEqual(self.inspect(), view)

    def test_verify_never_reports_a_pair_of_snapshots_that_did_not_coexist(self):
        observed = key(1)
        capture(self.local, observed, "outcome", observation("Artifact passed verification"))
        original = saved(self.local, observed)
        local, cloud = self.local, self.cloud

        class BetweenSnapshots(LocalStore):
            def inventory(inner):
                capture(local, observed, "outcome", observation("Later verification failed"))
                cloud.apply([original], cloud.inventory()["token"])
                return super().inventory()

        # Old local and new cloud are identical; the two stores have never actually agreed.
        with self.assertRaises(StaleView):
            inspect_views(local, BetweenSnapshots(cloud.root, cloud.project_id), verify=True)
        current = self.inspect(verify=True)
        self.assertEqual((current["status"], current["different"]), ("review_required", 1))

    def test_cloud_addition_at_commit_invalidates_review_of_another_id(self):
        ready, withdrawn = key(1), key(2)
        capture(self.local, ready, "task", {"state": "completed", "title": "Clear release for launch", "added_on": "2026-11-27"})
        before = self.inspect()

        def revoke_approval():
            capture(self.cloud, withdrawn, "risk", {"state": "open", "title": "Legal withdrew release approval"})

        endpoint = AtCloudCommit(self.cloud, before=revoke_approval)
        with self.assertRaises(StaleView):
            apply_review(self.local, endpoint, [take(ready, "local", "Launch appeared clear")], **reviewed(before))
        with self.assertRaises(StaleView):
            self.details(before, ready)
        with self.assertRaises(StaleView):
            self.inspect(limit=1, after_id=ready, **reviewed(before))
        fresh = self.inspect()
        actual = self.details(fresh, ready, withdrawn)
        self.assertIsNone(actual[ready]["cloud"])
        self.assertIsNone(actual[withdrawn]["local"])
        # New evidence affects a different ID: revise the conclusion, not just the token.
        self.apply(fresh, take(withdrawn, "cloud", "Legal notice"),
                   {**take(ready, "merge", "Approval was withdrawn"), "kind": "task",
                    "data": {"state": "blocked", "title": "Await renewed approval", "source": withdrawn, "added_on": "2026-11-27"}})
        self.assertEqual(self.inspect(verify=True)["status"], "synced")
        self.assertEqual(saved(self.cloud, ready)["record"]["data"]["state"], "blocked")

    def test_local_edit_after_cloud_commit_survives_partial_apply(self):
        release = key(1)
        capture(self.local, release, "outcome", observation("Image built"))
        capture(self.cloud, release, "outcome", observation("Image signed"))
        before = self.inspect()
        reason = "Keep both verified observations"
        merge = {**take(release, "merge", reason), "kind": "outcome", "data": observation("Image built and signed")}

        def add_local_evidence():
            capture(self.local, release, "outcome", observation("Image failed local verification"))

        endpoint = AtCloudCommit(self.cloud, after=add_local_evidence)
        with self.assertRaises(StaleView):
            apply_review(self.local, endpoint, [merge], **reviewed(before))
        fresh = self.inspect()
        versions = self.details(fresh, release)[release]
        self.assertEqual(fresh["status"], "review_required")
        self.assertEqual(versions["local"]["record"]["data"]["title"], "Image failed local verification")
        self.assertEqual(versions["cloud"]["record"]["data"], merge["data"])
        self.apply(fresh, {**merge, "data": observation("Image built and signed; local verification failed")})
        self.assertEqual(self.inspect(verify=True)["status"], "synced")
        history = saved(self.local, release)["history"]
        prior_titles = {revision["record"]["data"]["title"] for revision in history}
        self.assertEqual(prior_titles, {"Image built", "Image signed", "Image built and signed",
                                        "Image failed local verification"})

    def test_new_local_id_after_cloud_commit_invalidates_even_an_uncontested_download(self):
        evidence, ready, risk = key(1), key(2), key(3)
        capture(self.local, evidence, "outcome", observation("Submitted launch checklist"))
        capture(self.cloud, ready, "decision", {"state": "accepted", "title": "Launch may proceed",
                                               "rationale": "Release checklist completed"})
        before = self.inspect()

        def discover_risk():
            capture(self.local, risk, "risk", {"state": "open", "title": "New blocking safety finding"})

        choices = [take(evidence, "local", "Observed submission"), take(ready, "cloud", "Release decision")]
        with self.assertRaises(StaleView):
            apply_review(self.local, AtCloudCommit(self.cloud, after=discover_risk), choices, **reviewed(before))
        fresh = self.inspect()
        self.assertEqual((fresh["status"], fresh["different"]), ("review_required", 2))
        actual = self.details(fresh, evidence, ready, risk)
        self.assertEqual(actual[evidence]["cloud"], actual[evidence]["local"])  # First-side commit was durable.
        self.assertIsNone(actual[ready]["local"])  # Now needs review against the new risk.
        self.assertEqual(actual[risk]["local"]["record"]["data"]["state"], "open")
        self.assertIsNone(actual[risk]["cloud"])

    def test_lost_cloud_reply_recognizes_existing_merge_on_fresh_review(self):
        for i, retry_with in enumerate(("cloud", "merge"), 1):
            with self.subTest(retry_with=retry_with):
                self._recover_existing_merge(key(i), retry_with)

    def _recover_existing_merge(self, report, retry_with):
        capture(self.local, report, "outcome", observation("Windows tested"))
        capture(self.cloud, report, "outcome", observation("Linux tested"))
        before = self.inspect()
        merge = {**take(report, "merge", "Combine observed platforms"), "kind": "outcome",
                 "data": observation("Tested on Windows and Linux")}

        def lose_reply():
            raise ConnectionError("lost reply after durable cloud commit")

        with self.assertRaises(ConnectionError):
            apply_review(self.local, AtCloudCommit(self.cloud, after=lose_reply), [merge], **reviewed(before))
        fresh = self.inspect()
        versions = self.details(fresh, report, include_history=True)[report]
        confirmed = versions["cloud"]
        self.assertEqual(fresh["different"], 1)
        self.assertEqual(confirmed["record"]["data"], merge["data"])
        self.assertEqual(len(confirmed["history"]), 2)
        self.assertNotEqual(versions["local"]["record"], confirmed["record"])
        # Review the actual versions; reaching the same conclusion is not another edit.
        retry = take(report, retry_with, "Confirmed the saved platform evidence")
        if retry_with == "merge":
            retry.update(kind=merge["kind"], data=merge["data"])
        self.apply(fresh, retry)
        verified = self.inspect(verify=True)
        self.assertEqual(verified["status"], "synced")
        self.assertEqual(verified["cloud_token"], fresh["cloud_token"])
        self.assertEqual(saved(self.local, report), confirmed)
        self.assertEqual(saved(self.cloud, report), confirmed)

    def test_erasure_suppresses_history_and_beats_a_later_edit_in_either_direction(self):
        for i, (eraser, offline, erased_side) in enumerate(((self.local, self.cloud, "local"),
                                                            (self.cloud, self.local, "cloud")), 1):
            with self.subTest(erased_side=erased_side):
                other = "cloud" if erased_side == "local" else "local"
                target = key(i)
                words = ("private initial wording", "private revised wording", "private later offline edit")
                capture(eraser, target, "outcome", observation(words[0]))
                capture(eraser, target, "outcome", observation(words[1]))
                self.apply(self.inspect(), take(target, erased_side, "Share confirmed note"))
                old = saved(offline, target)
                local_write(eraser, eraser.inventory()["token"], None, None, "Owner requested erasure",
                            record_id=target, erase=True)
                marker = saved(eraser, target)
                later = make_change(old, "note", {"title": words[2]}, "Edited offline", record_id=target)
                stamp = datetime.fromisoformat(marker["record"]["written_at"]) + timedelta(days=1)
                later["record"]["written_at"] = stamp.isoformat()
                offline.apply([later], offline.inventory()["token"], strict_current=False)

                before = self.inspect()
                versions = self.details(before, target)[target]
                self.assertIsNone(versions[other])
                self.assertEqual(versions[erased_side], {'record': marker['record']})
                for word in words:
                    self.assertNotIn(word, json.dumps([before, versions]))
                # Even choosing the live (newer) side cannot restore the erased wording.
                self.apply(before, take(target, other, "Compare latest offline copy"))
                self.assertEqual(self.inspect(verify=True)["status"], "synced")
                for store in (self.local, self.cloud):
                    actual = saved(store, target)
                    self.assertEqual(actual, marker)
                    self.assertEqual(actual["history"], [])
                    self.assertEqual(set(actual["record"]), {"id", "erased", "written_at"})
                    self.assertIs(actual["record"]["erased"], True)

    def test_new_decision_can_correct_an_unchanged_brief_and_skill_without_editing_git(self):
        runbook, git_skill, decision = key(1), key(2), key(3)
        path = "release/SKILL.md"
        repo_file = self.local.root / path
        repo_file.parent.mkdir(parents=True)
        git_body = b"Private repository procedure: promote the staging image.\n"
        repo_file.write_bytes(git_body)
        owned = {"name": "Verify release", "when_to_use": "Before release", "steps": ["Inspect staging"],
                 "verification": "Staging matches the candidate"}
        linked = {"name": "Promote image", "when_to_use": "After approval", "path": path}
        capture(self.local, runbook, "skill", owned)
        capture(self.local, git_skill, "skill", linked)
        outline = {"purpose": "Release orientation", "current": "Verify on staging",
                   "skills": [{"id": rid, "name": data["name"], "when_to_use": data["when_to_use"]}
                              for rid, data in ((runbook, owned), (git_skill, linked))]}
        brief = capture(self.local, None, "brief", outline)["id"]
        self.apply(self.inspect(), *(take(rid, "local", "Initial project knowledge")
                                     for rid in (runbook, git_skill, brief)))
        capture(self.cloud, decision, "decision", {"state": "accepted", "title": "Retire staging",
                "rationale": "Signed verification replaces the staging approval",
                "source": "Owner approved signed candidate verification; promotion requires separate approval"})

        before = self.inspect()
        self.assertEqual(before["different"], 1)
        context = self.details(before, decision, brief, runbook, git_skill)
        for rid in (brief, runbook, git_skill):
            self.assertEqual(context[rid]["local"], context[rid]["cloud"])
        self.assertEqual(context[decision]["cloud"]["record"]["data"]["state"], "accepted")
        self.assertEqual(context[runbook]["local"]["record"]["data"]["steps"], ["Inspect staging"])
        self.assertEqual(context[git_skill]["local"]["record"]["data"], linked)
        corrected = {**owned, "steps": ["Verify candidate signature"], "verification": "Signature is valid"}
        self.apply(before, take(decision, "cloud", "Established owner decision"),
                   {**take(runbook, "merge", "Retired staging; use approved verification"),
                    "kind": "skill", "data": corrected},
                   {**take(brief, "merge", "Flag repository-owned procedure for owner edit"), "kind": "brief",
                    "data": {**outline, "current": "Verify the signed candidate", "authority": decision,
                             "open_question": {"id": git_skill, "text": "Owner must revise promotion procedure"}}})
        self.assertEqual(self.inspect(verify=True)["status"], "synced")
        for store in (self.local, self.cloud):
            orientation = store.orient()
            self.assertEqual(orientation["brief"]["id"], brief)
            refs = orientation["brief"]["data"]["skills"]
            self.assertCountEqual(refs, orientation["skills"])
            self.assertEqual(orientation["brief"]["data"]["authority"], decision)
            records = store.read([ref["id"] for ref in refs], orientation["token"])
            sources = {entry["record"]["id"]: entry["record"]["data"] for entry in records}
            self.assertEqual(sources[runbook], corrected)
            self.assertEqual(sources[git_skill], linked)
            self.assertNotIn(git_body.decode().strip(), json.dumps([records, orientation]))
        self.assertEqual(repo_file.read_bytes(), git_body)
        self.assertFalse((self.cloud.root / path).exists())
