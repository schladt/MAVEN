import copy
import hashlib
import json
import unittest
from uuid import uuid4

from tools.reconcile_records import (
    KINDS, MAX_ENVELOPE_BYTES, accept_change, brief_id, combine_history, digest, make_change,
    summarize, tombstone, validate_envelope, validate_project_envelope,
)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.id = str(uuid4())
        self.project = str(uuid4())

    def new(self, kind="note", data=None):
        return make_change(None, kind, {"text": "project notes"} if data is None else data,
                           "first observation", record_id=self.id)

    def test_timestamp_describes_write_not_event_and_copies_do_not_rewrite(self):
        data = {"text": "Deployed café", "event_date": "2020-01-01"}
        first = self.new("action", data)
        self.assertEqual(first["record"].keys(), {"id", "kind", "written_at", "data"})
        self.assertTrue(first["record"]["written_at"].endswith("Z"))
        first["record"]["written_at"] = "2026-01-01T00:00:00+00:00"
        same = make_change(first, "action", data, "duplicate observation", record_id=self.id)
        self.assertEqual(same, first)
        self.assertEqual(digest(first), hashlib.sha256(json.dumps(first, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest())
        changed = make_change(first, "outcome", {"summary": "Verified HTTP response"},
                              "observed successful probe", record_id=self.id)
        self.assertNotEqual(changed["record"]["written_at"], first["record"]["written_at"])
        self.assertEqual(changed["history"], [{"record": first["record"], "reason": "observed successful probe"}])
        self.assertEqual(summarize(changed)["summary"], "Verified HTTP response")

    def test_supported_kinds_and_states(self):
        samples = {
            "decision": {"state": "proposed", "title": "Replace storage"},
            "task": {"state": "blocked", "title": "Tune SQLite"},
            "risk": {"state": "mitigated", "title": "Approval withdrawn"},
            "skill": {"name": "Test", "when_to_use": "Check a build", "path": "SKILL.md"},
        }
        for kind in KINDS:
            with self.subTest(kind=kind):
                record = self.new(kind, samples.get(kind, {"description": "Keep project detail"}))
                self.assertEqual(summarize(record)["kind"], kind)
                if kind in ("decision", "task", "risk"):
                    self.assertEqual(summarize(record)["state"], samples[kind]["state"])
                    with self.assertRaises(ValueError):
                        self.new(kind, {"title": "No evidence of a state"})
        for kind in ("session", "checkpoint", "claim", "secret", "operation", "instruction"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.new(kind)

    def test_skill_has_exactly_one_procedure_and_never_requires_the_file(self):
        name = {"name": "Release", "when_to_use": "After signed approval"}
        procedure = {"steps": ["Build", "Run the acceptance suite"], "verification": "Read its result"}
        link = {"path": "ops/release/SKILL.md"}
        self.new("skill", {**name, **link})
        self.new("skill", {**name, **procedure})
        invalid = [{}, {**procedure, **link}, {"steps": []}, {"steps": ["build"]},
                   {"steps": ["build"], "verification": " "}, {"verification": "pass"},
                   *({"path": p} for p in ["../SKILL.md", "/etc/SKILL.md", "C:/SKILL.md",
                                          "https://example.org/SKILL.md", "ops\\SKILL.md", "README.md"])]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.new("skill", {**name, **fields})

    def test_single_brief_identity_is_reserved_including_historical_records(self):
        data = {"purpose": "project context"}
        with self.assertRaises(ValueError):
            validate_project_envelope(self.new("brief", data), self.project)
        key = brief_id(self.project)
        self.assertEqual(key, brief_id(self.project))
        self.assertNotEqual(key, brief_id(str(uuid4())))
        brief = make_change(None, "brief", data, "seed", record_id=key)
        validate_project_envelope(brief, self.project)
        with self.assertRaises(ValueError):
            validate_project_envelope(make_change(brief, "note", data, "demote", record_id=key), self.project)
        forged = self.new()
        forged["history"].append({"reason": "legacy", "record": self.new("brief", data)["record"]})
        with self.assertRaises(ValueError):
            validate_project_envelope(forged, self.project)

    def test_equal_timestamps_different_content_and_extra_history(self):
        base = self.new()
        a = make_change(base, "note", {"text": "Local"}, "local reason", record_id=self.id)
        b = make_change(base, "note", {"text": "Cloud"}, "cloud reason", record_id=self.id)
        b["record"]["written_at"] = a["record"]["written_at"]
        self.assertNotEqual(digest(a), digest(b))
        merged = combine_history(a["record"], [a, b], "retain local observation")
        self.assertEqual(merged, combine_history(a["record"], [b, a, a], "retain local observation"))
        for parent in [a, b]:
            accept_change(parent, merged)
        self.assertCountEqual([e["reason"] for e in merged["history"]],
                              ["local reason", "cloud reason", "retain local observation"])
        self.assertEqual(merged, combine_history(merged["record"], [merged, a], "no change"))
        self.assertNotEqual(digest(merged), digest(a))  # history participates in inventory equality
        with self.assertRaises(ValueError):
            accept_change(merged, a)
        with self.assertRaises(ValueError):
            accept_change(a, b)

    def test_wire_validation_fails_closed(self):
        first = self.new()
        marker = tombstone(first, record_id=self.id)
        mismatched_id = {**first["record"], "id": str(uuid4())}
        invalid = [None, {}, {**first, "scope": "universal"},
                   {**first, "history": [{"record": mismatched_id, "reason": "different subject"}]},
                   {**first, "history": [{"record": marker["record"], "reason": "erased"}]},
                   {**marker, "history": [{"record": first["record"], "reason": "content leak"}]},
                   {**marker, "record": {**marker["record"], "erased": 1}},
                   {**marker, "record": {**marker["record"], "kind": "note"}},
                   {**first, "record": {**first["record"], "written_at": "2025-01-01T00:00:00"}},
                   {**first, "record": {**first["record"], "written_at": "2025-02-30T00:00:00Z"}},
                   {**first, "record": {**first["record"], "id": "{" + self.id + "}"}},
                   {**first, "record": {**first["record"], "data": {"text": "a" * MAX_ENVELOPE_BYTES}}}]
        for value in invalid:
            with self.assertRaises(ValueError):
                validate_envelope(value)
        for data in [{"api_key": "forbidden"}, {"nested": [{"secret_ref": "forbidden"}]},
                     {"title": "-----BEGIN PRIVATE KEY-----"}, {"text": float("nan")}, {"text": "\0"}]:
            record = copy.deepcopy(first)
            record["record"]["data"] = data
            with self.assertRaises(ValueError):
                validate_envelope(record)
        with self.assertRaises(ValueError):
            self.new("setting", {"key": "password", "value": "sensitive"})
        forged = {**first, "history": [{"record": first["record"], "reason": "-----BEGIN PRIVATE KEY-----"}]}
        with self.assertRaises(ValueError):
            validate_envelope(forged)
