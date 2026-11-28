import unittest
from uuid import UUID, uuid4

from tools.reconcile_records import (
    KINDS, MAX_ENVELOPE_BYTES, TaxonomyError, accept_change,
    brief_id, canonical, make_change, profile_id, tombstone, validate_current_data, validate_envelope,
    validate_project_envelope,
)


TASK_ID = "148d25cd-8212-4ee6-8dc9-382ed80391ad"
V2 = {
    "profile": {"body": "Owner background."},
    "brief": {"purpose": "Maintain the catalog."},
    "preference": {"title": "Time zone", "summary": "Use UTC.", "scope": "project"},
    "skill": {"name": "Release", "when_to_use": "After approval", "path": "ops/SKILL.md"},
    "reference": {"name": "Lab", "read_when": "Planning lab work", "state": "active",
                  "as_of": "2024-02-29", "body": "Equipment list."},
    "task": {"title": "Measure", "state": "open", "added_on": "2024-02-29"},
    "decision": {"title": "Use UTC", "state": "accepted", "rationale": "Independent of zone."},
    "risk": {"title": "Missing evidence", "state": "open"},
    "plan": {"task_id": TASK_ID, "steps": ["Measure", "Record"], "state": "proposed",
             "completion_check": "Results in the catalog."},
    "entity": {"type": "instrument", "name": "Test meter"},
    "outcome": {"title": "Calibration observed", "occurred_on": "2024-02-29", "observed": "Within limits."},
}
REMOVED = {"action", "cheatsheet", "fact", "lesson", "link", "note", "question", "relationship",
           "research", "setting"}


def envelope(kind, data, key, history=()):
    return {"record": {"id": key, "written_at": "2026-11-27T00:00:00Z", "kind": kind,
                       "data": data}, "history": list(history)}


class TaxonomyGrammarTests(unittest.TestCase):
    def setUp(self):
        self.key = str(uuid4())
        self.project = str(uuid4())

    def test_all_eleven_kinds_validate_in_both_modes(self):
        for kind, data in V2.items():
            with self.subTest(kind=kind):
                key = {"brief": brief_id(self.project), "profile": profile_id(self.project)}.get(kind, self.key)
                value = envelope(kind, data, key)
                validate_current_data(kind, data)
                for strict in (False, True):
                    validate_project_envelope(value, self.project, strict_current=strict)

    def test_legacy_kinds_cannot_be_new_currents_even_on_duplicate_write(self):
        self.assertEqual(KINDS, REMOVED | (V2.keys() - {"preference", "profile"}))
        for kind in REMOVED:
            with self.subTest(kind=kind):
                value = envelope(kind, {"description": "Legacy"}, self.key)
                validate_envelope(value)
                same = make_change(value, kind, value["record"]["data"], "duplicate", record_id=self.key)
                with self.assertRaises(TaxonomyError) as err:
                    validate_envelope(same, strict_current=True)
                self.assertIn("kind", str(err.exception))
                for allowed in V2:
                    self.assertIn(allowed, str(err.exception))

    def test_each_required_field_is_named_on_rejection(self):
        for kind, fields in V2.items():
            if kind == "brief":  # Required: a nonempty object, not a particular field.
                continue
            for field in fields:
                data = {key: value for key, value in fields.items() if key != field}
                with self.subTest(kind=kind, missing=field), self.assertRaisesRegex(TaxonomyError, field):
                    validate_current_data(kind, data)
        with self.assertRaisesRegex(TaxonomyError, "brief.data"):
            validate_current_data("brief", {})
        with self.assertRaisesRegex(TaxonomyError, "task.data"):
            validate_current_data("task", [])

    def test_fields_require_nonblank_text_and_plan_requires_ordered_text_steps(self):
        for kind, field in (("preference", "summary"), ("decision", "rationale"), ("task", "title"),
                            ("risk", "title"), ("plan", "completion_check"), ("outcome", "observed"),
                            ("entity", "name"), ("entity", "type"), ("profile", "body"),
                            ("reference", "read_when")):
            for bad in (" \t\n", 1, ["text"], None):
                with self.subTest(kind=kind, field=field, bad=bad), self.assertRaisesRegex(TaxonomyError, field):
                    validate_current_data(kind, {**V2[kind], field: bad})
        for bad in ([], "do work", {"1": "work"}, ["first", " "], [True]):
            with self.subTest(bad=bad), self.assertRaisesRegex(TaxonomyError, "steps"):
                validate_current_data("plan", {**V2["plan"], "steps": bad})
        for bad in (None, [], "unresolved label", TASK_ID.upper(), "{" + TASK_ID + "}"):
            with self.subTest(bad=bad), self.assertRaisesRegex(TaxonomyError, "task_id.*canonical UUID"):
                validate_current_data("plan", {**V2["plan"], "task_id": bad})
        with self.assertRaisesRegex(TaxonomyError, "read_when.*line"):
            validate_current_data("reference", {**V2["reference"], "read_when": "One\nTwo"})

    def test_exact_state_and_preference_scope_choices(self):
        choices = {"decision": ("proposed", "accepted", "superseded"),
                   "risk": ("open", "mitigated", "closed"),
                   "task": ("open", "in_progress", "blocked", "completed", "cancelled"),
                   "plan": ("proposed", "approved", "done", "superseded"),
                   "reference": ("active", "retired"), "preference": ("project", "nexus")}
        for kind, allowed in choices.items():
            field = "scope" if kind == "preference" else "state"
            for val in allowed:
                with self.subTest(kind=kind, val=val):
                    validate_current_data(kind, {**V2[kind], field: val})
            for bad in ("archived", [], None):
                with self.subTest(kind=kind, bad=bad), self.assertRaisesRegex(TaxonomyError, field):
                    validate_current_data(kind, {**V2[kind], field: bad})
        with self.assertRaisesRegex(TaxonomyError, "category"):
            validate_current_data("preference", {**V2["preference"], "category": "preference"})

    def test_dates_are_exact_ascii_calendar_dates(self):
        for kind, field in (("reference", "as_of"), ("task", "added_on"), ("outcome", "occurred_on")):
            for bad in ("2023-02-29", "2024-04-31", "0000-01-01", "2024-2-29", "20240229",
                        "2024-W09-4", "２０２４-02-29", "2024-02-29T00:00:00Z", "2024-02-29\n", None):
                with self.subTest(kind=kind, bad=bad), self.assertRaisesRegex(TaxonomyError, field + ".*YYYY-MM-DD"):
                    validate_current_data(kind, {**V2[kind], field: bad})

    def test_unicode_limits_are_code_points_and_closed_fields_cannot_bypass_them(self):
        # Astral characters occupy four UTF-8 bytes; a combining mark is a separate code point.
        for kind, field, limit in (("profile", "body", 7000), ("reference", "body", 7000),
                                   ("reference", "name", 120), ("reference", "read_when", 400)):
            data = {**V2[kind], field: "\U0001f332" * (limit - 2) + "e\u0301"}
            with self.subTest(kind=kind, field=field):
                validate_envelope(envelope(kind, data, self.key), strict_current=True)
                with self.assertRaisesRegex(TaxonomyError, rf"{field}.*{limit}.*{limit + 1}"):
                    validate_current_data(kind, {**data, field: data[field] + "x"})
        for kind in ("profile", "reference"):
            with self.subTest(kind=kind), self.assertRaisesRegex(TaxonomyError, "allows only") as err:
                validate_current_data(kind, {**V2[kind], "private extra key": "private extra value"})
            self.assertNotIn("private extra", str(err.exception))
        # Long plan text and catalog entries are subject only to the envelope cap.
        validate_current_data("plan", {**V2["plan"], "steps": ["a" * 7001]})
        validate_current_data("entity", {**V2["entity"], "body": "x" * 8000, "metadata": [1, 2]})

    def test_mixed_history_preserves_frozen_v1_and_valid_v2(self):
        old_data = (("action", {"state": "started"}), ("setting", {"key": "status", "value": "planned"}),
                    ("reference", {"text": "legacy card"}), ("plan", {"state": "archived"}),
                    ("task", {"state": "blocked"}), ("decision", {"state": "accepted"}),
                    ("risk", {"state": "open"}), ("outcome", {"summary": "old observation"}),
                    ("entity", {"description": "catalog entry"}))
        history = []
        for kind, data in old_data:
            value = envelope(kind, data, self.key)
            validate_envelope(value)
            with self.subTest(kind=kind), self.assertRaises(TaxonomyError):
                validate_envelope(value, strict_current=True)
            history.append({"reason": "prior wording", "record": value["record"]})
        preference = envelope("preference", V2["preference"], self.key, history)
        current = make_change(preference, "outcome", V2["outcome"], "observed result", record_id=self.key)
        validate_project_envelope(current, self.project, strict_current=True)
        accept_change(preference, current)
        with self.assertRaisesRegex(ValueError, "discard managed history"):
            accept_change(preference, {**current, "history": current["history"][-1:]})
        for bad in (("preference", {"summary": "lacks scope"}), ("task", {"state": "invented"}),
                    ("skill", {"description": "no procedure"}), ("unknown", {"text": "unknown"})):
            entry = {"reason": "not valid in either grammar", "record": envelope(*bad, self.key)["record"]}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_envelope({**current, "history": [entry]}, strict_current=True)

    def test_profile_identity_rejects_impossible_currents_and_history(self):
        key = profile_id(self.project)
        self.assertEqual(str(UUID(key)), key)
        self.assertEqual(key, profile_id(self.project))
        self.assertNotIn(key, (brief_id(self.project), profile_id(str(uuid4()))))
        first = envelope("profile", V2["profile"], key)
        updated = make_change(first, "profile", {"body": "Changed background"}, "update", record_id=key)
        validate_project_envelope(updated, self.project, strict_current=True)
        for use_key, current_kind, past_kind in ((self.key, "profile", None), (key, "preference", None),
                                                 (key, "profile", "preference"), (self.key, "entity", "profile")):
            history = [] if past_kind is None else [
                {"record": envelope(past_kind, V2.get(past_kind, {"text": "old"}), use_key)["record"],
                 "reason": "retyped"}]
            for strict in (False, True):
                with self.subTest(key=use_key, kind=current_kind, past=past_kind, strict=strict), \
                        self.assertRaisesRegex(TaxonomyError, "profile.*reserved"):
                    validate_project_envelope(envelope(current_kind, V2[current_kind], use_key, history),
                                              self.project, strict_current=strict)

    def test_profile_id_collision_remains_readable_and_converts_losslessly(self):
        key = profile_id(self.project)
        legacy = envelope("note", {"text": "V1 could use this UUID."}, key)
        validate_project_envelope(legacy, self.project)
        shared = make_change(legacy, "entity", V2["entity"], "catalogued", record_id=key)
        validate_project_envelope(shared, self.project)
        # Even v2-shaped data from a shared kind isn't a valid NEW occupant of the slot.
        with self.assertRaisesRegex(TaxonomyError, "profile.*reserved"):
            validate_project_envelope(shared, self.project, strict_current=True)
        profile = make_change(shared, "profile", V2["profile"], "converted", record_id=key)
        validate_project_envelope(profile, self.project, strict_current=True)
        accept_change(shared, profile)
        self.assertCountEqual([item["record"] for item in profile["history"]],
                              [legacy["record"], shared["record"]])

    def test_tombstone_is_content_free_terminal_and_not_a_historical_version(self):
        key = profile_id(self.project)
        profile = envelope("profile", V2["profile"], key)
        marker = tombstone(profile, record_id=key)
        validate_project_envelope(marker, self.project, strict_current=True)
        accept_change(profile, marker)
        with self.assertRaisesRegex(ValueError, "cannot be restored"):
            accept_change(marker, profile)
        for outer, inner in ((marker, profile), (profile, marker)):
            with self.assertRaises(ValueError):
                validate_envelope({**outer, "history": [{"record": inner["record"], "reason": "invalid"}]},
                                  strict_current=True)

    def test_envelope_size_limit_reports_bytes_not_content(self):
        data = {**V2["entity"], "body": "private wording"}
        value = envelope("entity", data, self.key)
        data["body"] += "x" * (MAX_ENVELOPE_BYTES - len(canonical(value).encode("utf-8")))
        validate_envelope(value, strict_current=True)
        data["body"] += "é"
        with self.assertRaises(TaxonomyError) as err:
            validate_envelope(value, strict_current=True)
        self.assertIn(f"limit: {MAX_ENVELOPE_BYTES} bytes", str(err.exception))
        self.assertIn(f"actual: {MAX_ENVELOPE_BYTES + 2} bytes", str(err.exception))
        self.assertNotIn("private wording", str(err.exception))

    def test_wire_security_applies_to_legacy_and_new_history(self):
        value = envelope("entity", V2["entity"], self.key)
        for kind, data in (("setting", {"key": "api-key", "value": "must not leak"}),
                           ("preference", {**V2["preference"], "nested": [{"secret_ref": "must not leak"}]}),
                           ("entity", {**V2["entity"], "nested": {"authorization": "must not leak"}}),
                           ("note", {"text": "-----BEGIN PRIVATE KEY-----"})):
            entry = {"record": envelope(kind, data, self.key)["record"], "reason": "previous"}
            with self.subTest(kind=kind), self.assertRaises(ValueError) as err:
                validate_envelope({**value, "history": [entry]}, strict_current=True)
            self.assertNotIn("must not leak", str(err.exception))
        other = {"record": envelope("note", {"text": "earlier"}, str(uuid4()))["record"], "reason": "prior"}
        duplicate = {"record": value["record"], "reason": "prior"}
        for history in ([other], [duplicate, duplicate]):
            with self.assertRaises(ValueError):
                validate_envelope({**value, "history": history}, strict_current=True)

    def test_diagnostics_do_not_echo_private_values_or_user_supplied_field_names(self):
        private = "private input not for diagnostics"
        for kind, data in ((private, {}), ([], {}), ("plan", {**V2["plan"], "task_id": private}),
                           ("reference", {**V2["reference"], private: private}),
                           ("task", {**V2["task"], "state": private}),
                           ("skill", {**V2["skill"], "path": private}),
                           ("outcome", {**V2["outcome"], "occurred_on": private})):
            with self.subTest(kind=kind), self.assertRaises(TaxonomyError) as err:
                validate_current_data(kind, data)
            self.assertNotIn(private, str(err.exception))
