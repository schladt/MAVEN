"""Wire contracts for session-free, project-scoped memory. Links are just strings."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID, uuid5

from tools.memory.validation import MemoryError as CredentialError, reject_secrets

MAX_BATCH = 50
MAX_ENVELOPE_BYTES = 512 * 1024
KINDS = frozenset("decision task risk action outcome note reference plan research lesson question cheatsheet link entity fact relationship setting skill brief".split())
STATES = {
    "decision": {"proposed", "accepted", "superseded"},
    "risk": {"open", "mitigated", "closed"},
    "task": {"open", "in_progress", "blocked", "completed", "cancelled"},
}
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|\+00:00)\Z", re.ASCII)
_SECRET_FIELDS = {"secret", "secrets", "secret_ref", "secret_adapter", "credential", "credentials",
                  "plaintext_secret", "secret_value", "secret_arn"}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def record_uuid(value) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("expected canonical UUID")
    return value


def brief_id(project_id: str) -> str:
    return str(uuid5(UUID(record_uuid(project_id)), "orientation-brief"))


def _text(value, name, maximum=None):
    if not isinstance(value, str) or not value.strip() or maximum is not None and len(value) > maximum:
        raise ValueError(f"invalid {name}")


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
            raise ValueError("skill path must be a project-relative SKILL.md path")
    elif set(data) == {"name", "when_to_use", "steps", "verification"}:
        if not isinstance(data["steps"], list) or not data["steps"]:
            raise ValueError("skill steps must be a nonempty list")
        for step in data["steps"]:
            _text(step, "skill step")
        _text(data["verification"], "skill verification")
    else:
        raise ValueError("skill requires exactly one procedure: steps and verification, or path")


def _record(value, *, live=False):
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
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(data, dict) or not data:
        raise ValueError("expected a project knowledge kind and nonempty data object")
    if kind in STATES and (not isinstance(data.get("state"), str) or data["state"] not in STATES[kind]):
        raise ValueError(f"{kind} requires state in {', '.join(sorted(STATES[kind]))}")
    if kind == "skill":
        _skill(data)
    if kind == "setting" and isinstance(data.get("key"), str):
        # A key/value setting must not bypass credential-named field checks.
        setting = {data["key"]: None}
        _json(setting)
        reject_secrets(setting)


def validate_envelope(envelope) -> None:
    if not isinstance(envelope, dict) or set(envelope) != {"record", "history"}:
        raise ValueError("expected a record and its history")
    current, history = envelope["record"], envelope["history"]
    if not isinstance(history, list):
        raise ValueError("expected history list")
    _json(envelope)
    if len(canonical(envelope).encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise ValueError("record with history exceeds transport size limit")
    try:
        reject_secrets(envelope)
        _record(current)
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


def validate_project_envelope(envelope, project_id) -> None:
    validate_envelope(envelope)
    is_brief_id = envelope["record"]["id"] == brief_id(project_id)
    for record in chain((envelope["record"],), (entry["record"] for entry in envelope["history"])):
        if not record.get("erased") and (record["kind"] == "brief") != is_brief_id:
            raise ValueError("the project brief has one reserved record ID")


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
