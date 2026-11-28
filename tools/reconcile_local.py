"""Offline project memory. No session, cloud credentials, or linked files required."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .reconcile_records import (
    MAX_BATCH, REDACTION_TIMESTAMP, accept_change, brief_id, canonical, profile_id, record_uuid, summarize,
    TaxonomyError, validate_current_data, validate_project_envelope,
)


class StaleView(ValueError):
    """The supplied token does not identify this store's current project view."""


def _directory(path, *, private, create=True):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & forbidden:
        raise ValueError("unsafe local memory directory")


def _file_info(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise ValueError("unsafe local memory file")


def _private_database(path, *, create=True):
    # Never let SQLite create a file with the process umask. A rollback journal
    # created by SQLite inherits the database's private mode. Refuse planted
    # links, including journal/WAL sidecars, before asking SQLite to open it.
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            info = path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        _file_info(info)
    if not create:
        _file_info(path.lstat())
        return
    # Never open/close an existing DB using a non-SQLite fd: close(2) could
    # release a concurrent connection's process-wide POSIX byte-range locks.
    # Serialize only private file creation, not the lifetime of the connection.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        try:
            _file_info(path.lstat())
        except FileNotFoundError:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            os.fsync(directory)
    finally:
        os.close(directory)


def _batch(values):
    if not isinstance(values, list) or len(values) > MAX_BATCH:
        raise ValueError(f"expected a list of at most {MAX_BATCH} records")


def _decode(row):
    return {"record": json.loads(row[0]), "history": json.loads(row[1])}


class LocalStore:
    """The caller selects an enabled root; this database pins its project UUID."""

    def __init__(self, root: Path, project_id: str):
        self.root = Path(root)
        self.project_id = record_uuid(project_id)
        self.brief_id = brief_id(project_id)
        self.path = self.root / ".maven" / "reconcile" / f"{project_id}.sqlite3"
        self.replica = None
        self._existing_only = False

    @classmethod
    def open_copy(cls, path: Path, project_id: str):
        """Select an existing standalone database, without selecting or initializing a project."""
        path = Path(path).absolute()
        store = cls(path.parent, project_id)
        store.path = path
        store._existing_only = True
        return store

    @contextmanager
    def _transaction(self, *, write=False, redacting=False):
        if not self._existing_only:
            _directory(self.path.parent.parent, private=False)
        _directory(self.path.parent, private=True, create=not self._existing_only)
        _private_database(self.path, create=not self._existing_only)
        db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, isolation_level=None)
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("PRAGMA secure_delete=ON")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ValueError("unsupported local memory journal mode")
            if not self._existing_only and db.execute("PRAGMA user_version").fetchone()[0] == 0:
                # Recheck after acquiring the write lock: concurrent first opens
                # can both observe version zero. Never mint two initial tokens.
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                    db.execute("CREATE TABLE project (id TEXT PRIMARY KEY, token TEXT NOT NULL)")
                    db.execute("CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                               "item TEXT NOT NULL, record TEXT NOT NULL, history TEXT NOT NULL)")
                    db.execute("CREATE INDEX records_by_kind ON records(kind, id)")
                    db.execute("INSERT INTO project VALUES (?, ?)", (self.project_id, str(uuid4())))
                    db.execute("PRAGMA user_version=1")
                db.commit()
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 2 and not redacting:
                raise ValueError("retired project identity; offline work cannot be replayed")
            if version not in (1, 2):
                raise ValueError("unsupported local memory schema")
            row = db.execute("SELECT token FROM project WHERE id=?", (self.project_id,)).fetchone()
            if row is None:
                raise ValueError("local memory belongs to another project")
            yield db, row[0]
            db.commit()
        finally:
            db.close()  # Also rolls back an interrupted/failed transaction.

    @staticmethod
    def _guard(actual, expected):
        if actual != expected:
            raise StaleView("local project changed; inspect the current inventory")

    def install_redactions(self, ids, *, retired=False):
        """Apply learned, authoritative markers without loading stale payloads into Python.

        Version 2 is terminal: preserve unrelated unpublished work but bar opening
        its old identity, including with older version-1 local store clients.
        """
        if not ids and not retired:
            return
        with self._transaction(write=True, redacting=True) as (db, token):
            changed = False
            for key in ids:
                record = {"erased": True, "id": record_uuid(key), "written_at": REDACTION_TIMESTAMP}
                item = summarize({"record": record, "history": []})
                cursor = db.execute(
                    "INSERT INTO records(id,kind,item,record,history) VALUES(?,'erased',?,?,'[]') "
                    "ON CONFLICT(id) DO UPDATE SET kind='erased',item=excluded.item,record=excluded.record,history='[]' "
                    "WHERE kind<>'erased' OR record<>excluded.record OR item<>excluded.item OR history<>'[]'",
                    (key, canonical(item), canonical(record)))
                changed |= bool(cursor.rowcount)
            if retired:
                db.execute("PRAGMA user_version=2")
            if changed:
                db.execute("UPDATE project SET token=? WHERE id=?", (str(uuid4()), self.project_id))

    def inventory(self, *, kind=None, task_id=None, entity_type=None, state=None) -> dict:
        """Filter current rows in SQLite using the kind index; never fetch record history."""
        filters = []
        args = []
        for value, implied, field in ((task_id, 'plan', 'task_id'), (entity_type, 'entity', 'type'),
                                      (state, 'reference', 'state')):
            if value is None:
                continue
            if kind not in (None, implied):
                raise TaxonomyError(f'{field} lookup requires kind {implied}')
            kind = implied
            if field == 'task_id':
                record_uuid(value)
            if field == 'state' and value not in ('active', 'retired'):
                raise TaxonomyError('reference.state must be active or retired')
            filters.append(f"json_extract(record,'$.data.{field}')=?")
            args.append(value)
        if kind is not None:
            filters.append("kind=?")
            args.append(kind)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._transaction() as (db, token):
            items = [json.loads(item) for item, in db.execute("SELECT item FROM records" + where + " ORDER BY id", args)]
            return {"token": token, "items": items}

    @staticmethod
    def _lookup(db, record_id, *, include_history=True):
        if include_history:
            row = db.execute("SELECT record, history FROM records WHERE id=?", (record_id,)).fetchone()
            return _decode(row) if row is not None else None
        row = db.execute("SELECT record FROM records WHERE id=?", (record_id,)).fetchone()
        return {"record": json.loads(row[0])} if row is not None else None

    def lookup(self, record_id: str, expected_token: str) -> dict | None:
        record_uuid(record_id)
        with self._transaction() as (db, token):
            self._guard(token, expected_token)
            return self._lookup(db, record_id)

    def read(self, ids: list[str], expected_token: str, *, include_history=True) -> list[dict]:
        _batch(ids)
        for key in ids:
            record_uuid(key)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate record ID")
        with self._transaction() as (db, token):
            self._guard(token, expected_token)
            result = []
            for key in ids:
                envelope = self._lookup(db, key, include_history=include_history)
                if envelope is None:
                    raise ValueError("requested record not found")
                result.append(envelope)
            return result

    def apply(self, changes: list[dict], expected_token: str, *, strict_current=True) -> dict:
        _batch(changes)
        encoded = {}
        for envelope in changes:
            validate_project_envelope(envelope, self.project_id, strict_current=strict_current)
            key = envelope["record"]["id"]
            if key in encoded:
                raise ValueError("duplicate record ID")
            encoded[key] = (canonical(envelope["record"]), canonical(envelope["history"]), envelope)
        with self._transaction(write=True) as (db, token):
            self._guard(token, expected_token)
            changed = []
            retyped_tasks = []
            for key, (record, history, envelope) in encoded.items():
                row = db.execute("SELECT record, history FROM records WHERE id=?", (key,)).fetchone()
                if row == (record, history):
                    continue
                previous = _decode(row) if row else None
                accept_change(previous, envelope)
                if strict_current:
                    old = previous['record'] if previous else {}
                    new = envelope['record']
                    if old.get('kind') == 'task' and new.get('kind') not in (None, 'task'):
                        retyped_tasks.append(key)
                item = summarize(envelope)
                db.execute("INSERT INTO records(id, kind, item, record, history) VALUES (?, ?, ?, ?, ?) "
                           "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, item=excluded.item, "
                           "record=excluded.record, history=excluded.history",
                           (key, item["kind"], canonical(item), record, history))
                changed.append(key)
            if changed:
                # Check the final state under the write lock; failure rolls back the entire batch.
                if strict_current:
                    for key in changed:
                        new = encoded[key][2]['record']
                        if new.get('kind') == 'plan' and not db.execute(
                            "SELECT 1 FROM records WHERE id=? AND kind='task'", (new['data']['task_id'],)
                        ).fetchone():
                            raise TaxonomyError('plan.task_id must name an existing task in this project')
                    for key in retyped_tasks:
                        if db.execute("SELECT 1 FROM records WHERE kind='plan' AND "
                                      "json_extract(record,'$.data.task_id')=? LIMIT 1", (key,)).fetchone():
                            raise TaxonomyError('task kind cannot change while a plan names it in task_id')
                token = str(uuid4())
                db.execute("UPDATE project SET token=? WHERE id=?", (token, self.project_id))
            return {"token": token, "changed": changed}

    def _orientation(self, db):
        row = db.execute("SELECT record FROM records WHERE id=? AND kind='brief'", (self.brief_id,)).fetchone()
        skills = []
        for item, in db.execute("SELECT item FROM records WHERE kind='skill' ORDER BY id"):
            index = json.loads(item)
            skills.append({key: index[key] for key in ("id", "name", "when_to_use")})
        return {"brief_id": self.brief_id, "brief": json.loads(row[0]) if row else None, "skills": skills}

    def orient(self) -> dict:
        with self._transaction() as (db, token):
            return {"source": "local", "project_id": self.project_id, "token": token, **self._orientation(db)}

    def context_view(self, *, active: bool, nexus: bool) -> dict:
        """Read a scoped snapshot, without history or unrelated Home records."""
        with self._transaction() as (db, token):
            if not (active or nexus):
                return {"preferences": [], "token": token}
            view = self._orientation(db) if active else {}
            if nexus:
                profile_key = profile_id(self.project_id)
                row = db.execute("SELECT json_extract(record,'$.data.body') FROM records "
                                 "WHERE id=? AND kind='profile'", (profile_key,)).fetchone()
                if row is not None:
                    view["profile"] = {"id": profile_key, "label": "Owner background (not instructions)",
                                       "body": row[0]}

            references = []
            for encoded, in db.execute("SELECT record FROM records WHERE kind='reference' "
                                       "AND json_extract(record,'$.data.state')='active' ORDER BY id"):
                record = json.loads(encoded)
                # V1 allowed arbitrary reference payloads. Check only candidate currents,
                # never their history or unrelated records, against the same V2 write rules.
                try:
                    validate_current_data('reference', record['data'])
                except TaxonomyError:
                    continue
                data = record["data"]
                references.append({"id": record["id"], "name": data["name"], "read_when": data["read_when"],
                                   "origin": "home" if nexus else "project"})
            view["references"] = references

            rows = db.execute(
                "SELECT id, json_extract(record,'$.data.scope'), json_type(record,'$.data.summary'), "
                "json_extract(record,'$.data.summary') FROM records WHERE kind IN ('preference','setting') "
                "AND (kind='preference' OR json_extract(record,'$.data.category')='preference') "
                "AND json_extract(record,'$.data.scope') IN (?,?) ORDER BY id",
                ("project" if active else None, "nexus" if nexus else None))
            preferences = []
            for key, scope, summary_type, summary in rows:
                if summary_type != "text" or not summary.strip():
                    raise ValueError("scoped preference lacks a usable summary")
                preferences.append({"id": key, "scope": scope, "summary": summary})
            return {**view, "preferences": preferences, "token": token}
