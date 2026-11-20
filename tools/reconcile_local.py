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
    MAX_BATCH, accept_change, brief_id, canonical, record_uuid, summarize, validate_project_envelope,
)


class StaleView(ValueError):
    """The supplied token does not identify this store's current project view."""


def _directory(path, *, private):
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


def _private_database(path):
    # Never let SQLite create a file with the process umask. A rollback journal
    # created by SQLite inherits the database's private mode. Refuse planted
    # links, including journal/WAL sidecars, before asking SQLite to open it.
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            info = path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        _file_info(info)
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

    @contextmanager
    def _transaction(self, *, write=False):
        _directory(self.path.parent.parent, private=False)
        _directory(self.path.parent, private=True)
        _private_database(self.path)
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("PRAGMA secure_delete=ON")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ValueError("unsupported local memory journal mode")
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
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
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
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

    def inventory(self) -> dict:
        with self._transaction() as (db, token):
            items = [json.loads(item) for item, in db.execute("SELECT item FROM records ORDER BY id")]
            return {"token": token, "items": items}

    @staticmethod
    def _lookup(db, record_id):
        row = db.execute("SELECT record, history FROM records WHERE id=?", (record_id,)).fetchone()
        return _decode(row) if row is not None else None

    def lookup(self, record_id: str, expected_token: str) -> dict | None:
        record_uuid(record_id)
        with self._transaction() as (db, token):
            self._guard(token, expected_token)
            return self._lookup(db, record_id)

    def read(self, ids: list[str], expected_token: str) -> list[dict]:
        _batch(ids)
        for key in ids:
            record_uuid(key)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate record ID")
        with self._transaction() as (db, token):
            self._guard(token, expected_token)
            result = []
            for key in ids:
                envelope = self._lookup(db, key)
                if envelope is None:
                    raise ValueError("requested record not found")
                result.append(envelope)
            return result

    def apply(self, changes: list[dict], expected_token: str) -> dict:
        _batch(changes)
        encoded = {}
        for envelope in changes:
            validate_project_envelope(envelope, self.project_id)
            key = envelope["record"]["id"]
            if key in encoded:
                raise ValueError("duplicate record ID")
            encoded[key] = (canonical(envelope["record"]), canonical(envelope["history"]), envelope)
        with self._transaction(write=True) as (db, token):
            self._guard(token, expected_token)
            changed = []
            for key, (record, history, envelope) in encoded.items():
                row = db.execute("SELECT record, history FROM records WHERE id=?", (key,)).fetchone()
                if row == (record, history):
                    continue
                accept_change(_decode(row) if row else None, envelope)
                item = summarize(envelope)
                db.execute("INSERT INTO records(id, kind, item, record, history) VALUES (?, ?, ?, ?, ?) "
                           "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, item=excluded.item, "
                           "record=excluded.record, history=excluded.history",
                           (key, item["kind"], canonical(item), record, history))
                changed.append(key)
            if changed:
                token = str(uuid4())
                db.execute("UPDATE project SET token=? WHERE id=?", (token, self.project_id))
            return {"token": token, "changed": changed}

    def orient(self) -> dict:
        with self._transaction() as (db, token):
            row = db.execute("SELECT record FROM records WHERE id=? AND kind='brief'", (self.brief_id,)).fetchone()
            skills = []
            for item, in db.execute("SELECT item FROM records WHERE kind='skill' ORDER BY id"):
                index = json.loads(item)
                skills.append({key: index[key] for key in ("id", "name", "when_to_use")})
            return {"source": "local", "project_id": self.project_id, "token": token, "brief_id": self.brief_id,
                    "brief": json.loads(row[0]) if row else None, "skills": skills}
