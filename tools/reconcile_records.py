"""Wire contracts for session-free, project-scoped memory. Links are just strings."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from itertools import chain
from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID, uuid5

from tools.memory.validation import MemoryError as CredentialError, reject_secrets

MAX_BATCH = 50
MAX_ENVELOPE_BYTES = 512 * 1024
REDACTION_KINDS = frozenset("record source page project project_record proposal challenge publication instruction_receipt operation memory_receipt consumption cleanup lifecycle citation skill_bundle recovery_object".split())
REDACTION_TIMESTAMP = "1970-01-01T00:00:00Z"
# Frozen v1 grammar: used for unconverted current versions and preserved history, not new writes.
KINDS = frozenset("decision task risk action outcome note reference plan research lesson question cheatsheet link entity fact relationship setting skill brief".split())
V2_KINDS = frozenset("profile brief preference skill reference task decision risk plan entity outcome".split())
_BODY_LIMIT = 7000
STATES = {
    "decision": {"proposed", "accepted", "superseded"},
    "risk": {"open", "mitigated", "closed"},
    "task": {"open", "in_progress", "blocked", "completed", "cancelled"},
}
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|\+00:00)\Z", re.ASCII)
_SECRET_FIELDS = {"secret", "secrets", "secret_ref", "secret_adapter", "credential", "credentials",
                  "plaintext_secret", "secret_value", "secret_arn"}


class TaxonomyError(ValueError):
    """A structural rule safe to show to the author; never include supplied content."""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def record_uuid(value) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("expected canonical UUID")
    return value


def redaction_target(value, *, project_id=None):
    """A delivery marker never carries content, original digests, or storage locators."""
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or value["kind"] not in REDACTION_KINDS:
        raise ValueError("invalid redaction target kind")
    key = record_uuid(value.get("id"))
    kind = value["kind"]
    if kind == "project_record":
        scope = record_uuid(value.get("project_id"))
        if set(value) != {"kind", "id", "project_id"} or project_id is not None and scope != project_id:
            raise ValueError("invalid scoped redaction target")
    elif set(value) != {"kind", "id"} or kind == "project" and project_id is not None and key != project_id:
        raise ValueError("invalid redaction target")
    return value


def brief_id(project_id: str) -> str:
    return str(uuid5(UUID(record_uuid(project_id)), "orientation-brief"))


def profile_id(project_id: str) -> str:
    return str(uuid5(UUID(record_uuid(project_id)), "owner-profile"))


def _text(value, name, maximum=None):
    if isinstance(value, str) and maximum is not None and len(value) > maximum:
        raise TaxonomyError(f"{name} exceeds {maximum} Unicode code points (actual: {len(value)})")
    if not isinstance(value, str) or not value.strip():
        raise TaxonomyError(f"{name} must be nonempty text")


def _json(value, depth=0):
    if depth > 48:
        raise ValueError("record nesting is too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or "\0" in key:
                raise ValueError("record keys must be ordinary JSON strings")
            if key.lower().replace("-", "_") in _SECRET_FIELDS:
                raise ValueError("secrets are not ordinary project records")
            _json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json(item, depth + 1)
    elif isinstance(value, str):
        if "\0" in value:
            raise ValueError("record text cannot contain NUL")
    elif value is not None and type(value) not in (int, bool, float):
        raise ValueError("unsupported JSON value")


def _skill(data):
    _text(data.get("name"), "skill name", 120)
    _text(data.get("when_to_use"), "skill when_to_use", 400)
    if set(data) == {"name", "when_to_use", "path"}:
        path = data["path"]
        _text(path, "skill path", 1024)
        if (PureWindowsPath(path).drive or PurePosixPath(path).name != "SKILL.md"
                or any(part in ("", ".", "..") for part in path.split("/"))
                or path.startswith("~") or any(char in path for char in "\\:")
                or any(ord(char) < 32 for char in path)):
            raise TaxonomyError("skill path must be a project-relative SKILL.md path")
    elif set(data) == {"name", "when_to_use", "steps", "verification"}:
        if not isinstance(data["steps"], list) or not data["steps"]:
            raise TaxonomyError("skill steps must be a nonempty list")
        for step in data["steps"]:
            _text(step, "skill step")
        _text(data["verification"], "skill verification")
    else:
        raise TaxonomyError("skill requires exactly one procedure: steps and verification, or path")


def _one_of(value, field, choices):
    if not isinstance(value, str) or value not in choices:
        raise TaxonomyError(f"{field} must be one of {', '.join(sorted(choices))}")


def _date(value, field):
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            date.fromisoformat(value)
            return
        except ValueError:
            pass
    raise TaxonomyError(f"{field} must be a calendar date in YYYY-MM-DD format")


def validate_current_data(kind, data) -> None:
    """Check v2 authoring rules only. The full envelope also needs wire and identity checks."""
    _one_of(kind, "kind", V2_KINDS)
    if not isinstance(data, dict):
        raise TaxonomyError(f"{kind}.data must be an object")

    if kind in ("task", "decision", "risk", "outcome", "preference"):
        _text(data.get("title"), f"{kind}.title")
    if kind in STATES:
        _one_of(data.get("state"), f"{kind}.state", STATES[kind])

    if kind == "profile":
        _text(data.get("body"), "profile.body", _BODY_LIMIT)
        if data.keys() != {"body"}:
            raise TaxonomyError("profile.data allows only body")
    elif kind == "brief":
        if not data:
            raise TaxonomyError("brief.data must be a nonempty object")
    elif kind == "preference":
        _text(data.get("summary"), "preference.summary")
        _one_of(data.get("scope"), "preference.scope", ("nexus", "project"))
        if "category" in data:
            raise TaxonomyError("preference.category is not used; omit category")
    elif kind == "skill":
        _skill(data)
    elif kind == "reference":
        _text(data.get("name"), "reference.name", 120)
        _text(data.get("read_when"), "reference.read_when", 400)
        if data["read_when"].splitlines() != [data["read_when"]]:
            raise TaxonomyError("reference.read_when must be a single line")
        _one_of(data.get("state"), "reference.state", ("active", "retired"))
        _date(data.get("as_of"), "reference.as_of")
        _text(data.get("body"), "reference.body", _BODY_LIMIT)
        if data.keys() != {"name", "read_when", "state", "as_of", "body"}:
            raise TaxonomyError("reference.data allows only name, read_when, state, as_of, body")
    elif kind == "task":
        _date(data.get("added_on"), "task.added_on")
    elif kind == "decision":
        _text(data.get("rationale"), "decision.rationale")
    elif kind == "plan":
        try:
            record_uuid(data.get("task_id"))
        except ValueError:
            raise TaxonomyError("plan.task_id must be a canonical UUID") from None
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise TaxonomyError("plan.steps must be a nonempty ordered list of steps")
        for step in steps:
            _text(step, "plan.steps item")
        _one_of(data.get("state"), "plan.state", ("proposed", "approved", "done", "superseded"))
        _text(data.get("completion_check"), "plan.completion_check")
    elif kind == "entity":
        _text(data.get("type"), "entity.type")
        _text(data.get("name"), "entity.name")
        if data["type"] == "workspace-metadata":
            # The entity name now carries the governed key, formerly setting.key.
            # Apply the same credential-name policy without changing frozen v1 entities.
            try:
                key = {data["name"]: None}
                _json(key)
                reject_secrets(key)
            except (ValueError, CredentialError):
                raise TaxonomyError("entity.name cannot be a credential field for workspace metadata") from None
    elif kind == "outcome":
        _date(data.get("occurred_on"), "outcome.occurred_on")
        _text(data.get("observed"), "outcome.observed")


def _record(value, *, live=False, strict_current=False):
    if not isinstance(value, dict):
        raise ValueError("expected record object")
    record_uuid(value.get("id"))
    stamp = value.get("written_at")
    if not isinstance(stamp, str) or not _TIMESTAMP.fullmatch(stamp):
        raise ValueError("written_at must be a UTC ISO8601 timestamp")
    datetime.fromisoformat(stamp)
    if not live and set(value) == {"id", "written_at", "erased"} and value["erased"] is True:
        return
    if set(value) != {"id", "written_at", "kind", "data"}:
        raise ValueError("invalid live record fields")
    kind, data = value["kind"], value["data"]
    # For shared kinds every v2 shape also satisfies v1. Unconverted currents and
    # history read the frozen grammar; v2-only kinds have no permissive legacy form.
    if strict_current or not isinstance(kind, str) or kind not in KINDS:
        validate_current_data(kind, data)
        return
    if not isinstance(data, dict) or not data:
        raise ValueError("expected a project knowledge kind and nonempty data object")
    if kind in STATES:
        _one_of(data.get("state"), f"{kind}.state", STATES[kind])
    if kind == "skill":
        _skill(data)
    if kind == "setting" and isinstance(data.get("key"), str):
        # A key/value setting must not bypass credential-named field checks.
        setting = {data["key"]: None}
        _json(setting)
        reject_secrets(setting)


def validate_envelope(envelope, *, strict_current=False) -> None:
    if not isinstance(envelope, dict) or set(envelope) != {"record", "history"}:
        raise ValueError("expected a record and its history")
    current, history = envelope["record"], envelope["history"]
    if not isinstance(history, list):
        raise ValueError("expected history list")
    _json(envelope)
    size = len(canonical(envelope).encode("utf-8"))
    if size > MAX_ENVELOPE_BYTES:
        raise TaxonomyError(f"record with history exceeds transport size limit "
                            f"(limit: {MAX_ENVELOPE_BYTES} bytes, actual: {size} bytes)")
    try:
        reject_secrets(envelope)
        _record(current, strict_current=strict_current)
        if current.get("erased") and history:
            raise ValueError("an erased record cannot retain history")
        seen = set()
        for entry in history:
            if not isinstance(entry, dict) or set(entry) != {"record", "reason"}:
                raise ValueError("invalid history fields")
            _text(entry["reason"], "change reason", 512)
            _record(entry["record"], live=True)
            if entry["record"]["id"] != current["id"]:
                raise ValueError("history belongs to another record")
            key = digest(entry)
            if key in seen:
                raise ValueError("duplicate history entry")
            seen.add(key)
    except CredentialError as exc:
        raise ValueError("credential material is not ordinary project memory") from exc


def validate_project_envelope(envelope, project_id, *, strict_current=False) -> None:
    validate_envelope(envelope, strict_current=strict_current)
    current = envelope["record"]
    is_brief_id = current["id"] == brief_id(project_id)
    is_profile_id = current["id"] == profile_id(project_id)
    for record in chain((current,), (entry["record"] for entry in envelope["history"])):
        if record.get("erased"):
            continue
        if (record["kind"] == "brief") != is_brief_id:
            raise TaxonomyError("the project brief has one reserved record ID")
        if (record["kind"] == "profile") != is_profile_id:
            # V1 hadn't reserved the profile ID. Its live records remain readable
            # there and can become history when the slot is converted to a profile.
            if is_profile_id and record["kind"] in KINDS and not (strict_current and record is current):
                continue
            raise TaxonomyError("the owner profile has one reserved record ID")


def accept_change(previous, incoming) -> None:
    """Fence regression inside a store's CAS transaction, after wire validation."""
    if previous is None:
        return
    current, target = previous["record"], incoming["record"]
    if current["id"] != target["id"]:
        raise ValueError("record IDs differ")
    if target.get("erased"):
        return
    if current.get("erased"):
        raise ValueError("an erased record cannot be restored")
    saved = {digest(entry) for entry in incoming["history"]}
    if any(digest(entry) not in saved for entry in previous["history"]):
        raise ValueError("change would discard managed history")
    old = digest(current)
    if digest(target) != old and not any(digest(entry["record"]) == old for entry in incoming["history"]):
        raise ValueError("replacement must preserve prior wording and reason")


def combine_history(current, parents, reason) -> dict:
    """Compose a lossless, order-independent envelope around the chosen version."""
    _text(reason, "change reason", 512)
    entries = {}
    version = digest(current)
    for parent in parents:
        validate_envelope(parent)
        old = parent["record"]
        if old["id"] != current["id"]:
            raise ValueError("cannot combine histories from different records")
        if current.get("erased"):
            continue
        if old.get("erased"):
            raise ValueError("an erased record cannot be restored")
        for entry in parent["history"]:
            entries[digest(entry)] = entry
        if digest(old) != version:
            entry = {"record": old, "reason": reason}
            entries[digest(entry)] = entry
    ordered = sorted(entries, key=lambda key: (entries[key]["record"]["written_at"], key))
    result = {"record": current, "history": [entries[key] for key in ordered]}
    validate_envelope(result)
    return result


def _now(previous):
    now = datetime.now(timezone.utc)
    if previous is not None and now == datetime.fromisoformat(previous["record"]["written_at"]):
        now += timedelta(microseconds=1)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def make_change(previous, kind, data, reason, *, record_id) -> dict:
    record_uuid(record_id)
    _text(reason, "change reason", 512)
    _json(data)
    if previous is not None:
        validate_envelope(previous)
        old = previous["record"]
        if old["id"] != record_id or old.get("erased"):
            raise ValueError("record is erased or has a different ID")
        if old["kind"] == kind and canonical(old["data"]) == canonical(data):
            return previous
    record = {"id": record_id, "written_at": _now(previous), "kind": kind, "data": data}
    return combine_history(record, [previous] if previous else [], reason)


def tombstone(previous, *, record_id) -> dict:
    record_uuid(record_id)
    if previous is not None:
        validate_envelope(previous)
        if previous["record"]["id"] != record_id:
            raise ValueError("record IDs differ")
        if previous["record"].get("erased"):
            return previous
    return {"record": {"id": record_id, "written_at": _now(previous), "erased": True}, "history": []}


def _snippet(value, limit):
    return " ".join(value.split())[:limit]


def summarize(envelope) -> dict:
    record = envelope["record"]
    item = {"id": record["id"], "written_at": record["written_at"], "digest": digest(envelope)}
    if record.get("erased"):
        return dict(item, kind="erased", summary="Erased", erased=True)
    data, kind = record["data"], record["kind"]
    if kind == "skill":
        return dict(item, kind=kind, name=data["name"], when_to_use=data["when_to_use"],
                    summary=_snippet(f"{data['name']}: {data['when_to_use']}", 240))
    if isinstance(data.get("state"), str):
        item["state"] = _snippet(data["state"], 80)
    parts = []
    for key in ("title", "name", "summary", "purpose", "description", "body", "body_markdown", "text", "question", "key", "predicate", "value"):
        value = data.get(key)
        if isinstance(value, str) and value.strip() and value not in parts:
            parts.append(value)
            if sum(map(len, parts)) >= 240:
                break
    summary = " — ".join(parts) if parts else canonical(data)
    return dict(item, kind=kind, summary=_snippet(summary, 240))
