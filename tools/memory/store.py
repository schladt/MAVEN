"""Parameterized Data API operations with explicit, bounded transactions."""

import base64
import binascii
from contextlib import contextmanager
import hashlib
import json
import time

from .validation import (MEMORY_ROUTES, MemoryError, MUTATION_ROUTES, SCHEMA_VERSION, canonical, decode_json, digest,
                         fields, uuid_string)

CONTROL_KEYS = {"instance_id", "recovery_epoch", "schema_version", "state", "data_scope"}
ROLLOUT_SCOPE = "owner_memory"
JSON_CHUNK_BYTES = 4096
MAX_JSON_RESULT_BYTES = 655360
# Reads may reuse a ready control document this long; mutations always fetch it fresh.
CONTROL_TTL_SECONDS = 5


def json_result_sql(expression):
    """One snapshot/evaluation, ASCII rows below 8 KiB, response below 1 MiB."""
    return f"""
WITH payload AS MATERIALIZED (
    SELECT convert_to(({expression})::text, 'UTF8') AS bytes
), document AS MATERIALIZED (
    SELECT bytes, octet_length(bytes) AS size,
           encode(sha256(bytes), 'hex') AS digest FROM payload
)
SELECT part.position,
       CASE WHEN document.size <= {MAX_JSON_RESULT_BYTES}
            THEN replace(encode(substring(document.bytes FROM part.position FOR {JSON_CHUNK_BYTES}),
                                'base64'), chr(10), '') ELSE NULL END AS chunk,
       document.size, document.digest
FROM document CROSS JOIN LATERAL
    generate_series(1, CASE WHEN document.size <= {MAX_JSON_RESULT_BYTES}
                           THEN document.size ELSE 1 END, {JSON_CHUNK_BYTES}) AS part(position)
ORDER BY part.position
"""


def sdk_config():
    from botocore.config import Config
    return Config(connect_timeout=1, read_timeout=6,
                  retries={"total_max_attempts": 1, "mode": "standard"})


def parameter(name, value):
    if value is None:
        data = {"isNull": True}
    elif type(value) is int:
        data = {"longValue": value}
    elif type(value) is bool:
        data = {"booleanValue": value}
    else:
        data = {"stringValue": str(value)}
    return {"name": name, "value": data}


def safe_provider_error(error):
    code = getattr(error, "response", {}).get("Error", {}).get("Code")
    message = getattr(error, "response", {}).get("Error", {}).get("Message", "")
    if code == "DatabaseErrorException" and "project_archived" in message and "55000" in message:
        return MemoryError("project_archived", 409)
    return MemoryError("warming" if code == "DatabaseResumingException"
                       else "temporarily_unavailable")


def control_document(config, state):
    return {"instance_id": config["instance_id"], "recovery_epoch": config["recovery_epoch"],
            "schema_version": SCHEMA_VERSION, "state": state, "data_scope": ROLLOUT_SCOPE}


def parse_control(raw, config, *, allow_maintenance=False, allow_legacy=False):
    try:
        document = decode_json(raw)
        fields(document, CONTROL_KEYS, {"uninstall"})
        uuid_string(document["instance_id"])
        uuid_string(document["recovery_epoch"])
        expected = control_document(config, document["state"])
        if "uninstall" in document:
            marker = document["uninstall"]
            if (document["state"] != "maintenance" or not isinstance(marker, str)
                    or len(marker) != 64 or any(char not in "0123456789abcdef" for char in marker)):
                raise ValueError
            expected["uninstall"] = marker
        if allow_legacy and document["data_scope"] in {"synthetic", "synthetic_and_owner_instructions"}:
            expected["data_scope"] = document["data_scope"]
        if allow_legacy and document["schema_version"] == 1:
            expected["schema_version"] = 1
        if document != expected or type(document["schema_version"]) is not int:
            raise ValueError
        if document["state"] not in {"ready", "maintenance"}:
            raise ValueError
    except (MemoryError, ValueError, KeyError, TypeError):
        raise MemoryError("context_stale") from None
    if document["state"] != "ready" and not allow_maintenance:
        raise MemoryError("context_stale")
    return document


def fetch_control(s3, config, *, allow_maintenance=False, allow_legacy=False):
    result = s3.get_object(Bucket=config["journal_bucket"], Key="control.json")
    body = result["Body"]
    try:
        raw = body.read(4097)
    finally:
        body.close()
    if len(raw) > 4096:
        raise MemoryError("context_stale")
    return parse_control(raw, config, allow_maintenance=allow_maintenance,
                         allow_legacy=allow_legacy), result.get("ETag")


class DataAPI:
    def __init__(self, client, config, *, admin=False, deadline=None):
        self.client = client
        self.base = {"resourceArn": config["cluster_arn"],
                     "secretArn": config["admin_secret_arn" if admin else "app_secret_arn"],
                     "database": config["database"]}
        self.deadline = deadline

    def budget(self, reserve=7):
        if self.deadline is not None and self.deadline - time.monotonic() < reserve:
            raise MemoryError("temporarily_unavailable")

    def execute(self, sql, values=None, transaction=None):
        self.budget()
        args = dict(self.base, sql=sql,
                    parameters=[parameter(key, value) for key, value in (values or {}).items()],
                    includeResultMetadata=False, continueAfterTimeout=False)
        if transaction is not None:
            args["transactionId"] = transaction
        return self.client.execute_statement(**args)

    def scalar(self, sql, values=None, transaction=None):
        result = self.execute(sql, values, transaction)
        rows = result.get("records", [])
        if not rows:
            return None
        field = rows[0][0]
        if field.get("isNull"):
            return None
        for name in ("stringValue", "longValue", "booleanValue", "doubleValue"):
            if name in field:
                return field[name]
        raise MemoryError("temporarily_unavailable")

    def json_scalar(self, expression, values=None, transaction=None, *,
                    overflow_status="temporarily_unavailable"):
        """Reassemble one bounded JSON value without Data API's 64 KiB row limit."""
        rows = self.execute(json_result_sql(expression), values, transaction).get("records", [])
        try:
            if not rows or len(rows) > (MAX_JSON_RESULT_BYTES + JSON_CHUNK_BYTES - 1) // JSON_CHUNK_BYTES:
                raise ValueError
            total = rows[0][2]["longValue"]
            checksum = rows[0][3]["stringValue"]
            if type(total) is not int or total < 1 or not isinstance(checksum, str) or len(checksum) != 64:
                raise ValueError
            if total > MAX_JSON_RESULT_BYTES:
                raise MemoryError(overflow_status, 409 if overflow_status == "context_incomplete" else 503)
            if len(rows) != (total + JSON_CHUNK_BYTES - 1) // JSON_CHUNK_BYTES:
                raise ValueError
            document = bytearray()
            for row in rows:
                if (len(row) != 4 or row[0].get("longValue") != len(document) + 1
                        or row[2].get("longValue") != total or row[3].get("stringValue") != checksum):
                    raise ValueError
                encoded = row[1]["stringValue"]
                if not isinstance(encoded, str) or len(encoded) > 4 * ((JSON_CHUNK_BYTES + 2) // 3):
                    raise ValueError
                chunk = base64.b64decode(encoded, validate=True)
                if len(chunk) != min(JSON_CHUNK_BYTES, total - len(document)):
                    raise ValueError
                document.extend(chunk)
            if len(document) != total or hashlib.sha256(document).hexdigest() != checksum:
                raise ValueError
            return document.decode("utf-8")
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error, UnicodeError):
            raise MemoryError("temporarily_unavailable") from None

    @contextmanager
    def transaction(self, *, timeouts=True):
        """Explicit timeouts unless the application role's session defaults already bound statements."""
        self.budget()
        tx = self.client.begin_transaction(**self.base)["transactionId"]
        committed = False
        try:
            if timeouts:
                self.execute("SELECT set_config('statement_timeout', '5s', true), "
                             "set_config('lock_timeout', '1s', true)", transaction=tx)
            yield tx
            self.budget()
            self.client.commit_transaction(resourceArn=self.base["resourceArn"],
                                           secretArn=self.base["secretArn"], transactionId=tx)
            committed = True
        finally:
            if not committed:
                try:
                    if self.deadline is None or self.deadline - time.monotonic() >= 7:
                        self.client.rollback_transaction(resourceArn=self.base["resourceArn"],
                                                         secretArn=self.base["secretArn"], transactionId=tx)
                except Exception:
                    pass  # Original failure is authoritative; SDK diagnostics stay private.


QUERY = """
SELECT jsonb_build_object('record_id', r.id, 'revision', v.revision,
    'fact', v.snapshot, 'source', sv.snapshot, 'change_reason', v.change_reason,
    'recorded_at', v.recorded_at)::text
FROM maven.records r
JOIN maven.facts f ON f.id = r.id
JOIN maven.record_versions v ON v.record_id = r.id
    AND v.revision = COALESCE(CAST(:revision AS integer), r.revision)
JOIN maven.record_sources c ON c.record_id = v.record_id AND c.revision = v.revision
JOIN maven.records s ON s.id = c.source_id
JOIN maven.record_versions sv ON sv.record_id = c.source_id AND sv.revision = c.source_revision
WHERE r.id = CAST(:record_id AS uuid) AND r.kind = 'fact'
  AND r.visibility_state = 'visible' AND r.archived_at IS NULL
  AND s.visibility_state = 'visible' AND s.archived_at IS NULL
LIMIT 1
"""

LOOKUP = """
SELECT jsonb_build_object('record_id', o.record_id, 'revision', o.revision,
    'request_key', o.request_key, 'replayed', true)::text
FROM maven.operations o
JOIN maven.records r ON r.id = o.record_id
JOIN maven.record_sources c ON c.record_id = o.record_id AND c.revision = o.revision
JOIN maven.records s ON s.id = c.source_id
WHERE o.request_key = CAST(:request_key AS uuid)
  AND r.visibility_state = 'visible' AND r.archived_at IS NULL
  AND s.visibility_state = 'visible' AND s.archived_at IS NULL
LIMIT 1
"""

INSTRUCTION_LOOKUP = """
SELECT o.result || jsonb_build_object('replayed', true)
FROM maven.instruction_receipts o
WHERE o.request_key = CAST(:request_key AS uuid) AND maven.instruction_visible(o.proposal_id)
"""


# Ordinary memory, page and proposal routes are one maven.memory_request statement.
DISPATCHED_ROUTES = MEMORY_ROUTES | {"/v1/proposals/create", "/v1/proposals/resolve",
                                     "/v1/proposals/challenge", "/v1/pages/index", "/v1/pages/read"}
WRITE_ROUTES = MUTATION_ROUTES | {"/v1/proposals/challenge"}
ROUTE_STATUSES = {"revision_conflict", "idempotency_conflict", "record_unavailable",
                  "proposal_unavailable", "page_unavailable", "proposal_conflict",
                  "approval_invalid", "invalid_request", "claim_conflict", "setting_conflict",
                  "reference_unavailable", "session_unavailable", "client_incompatible",
                  "context_incomplete", "dependency_unavailable", "dependency_conflict", "context_stale"}
DISPATCH = ("maven.memory_request(:route, CAST(:request AS jsonb), :request_hash, :candidate_hash, "
            ":actor, CAST(:protocol AS integer))")


def route_result(result):
    """Map exact route outcomes; anything unrecognized is an ambiguous provider failure."""
    if not isinstance(result, dict):
        raise MemoryError("temporarily_unavailable")
    if "status" in result:
        code = result["status"]
        if code not in ROUTE_STATUSES:
            raise MemoryError("temporarily_unavailable")
        status = 404 if code.endswith("_unavailable") else 422 if code == "invalid_request" else 409
        raise MemoryError(code, status, **{k: v for k, v in result.items() if k == "current_revision"})
    return result


class ControlCache:
    """Per-container ready control document; only reads may reuse it, briefly."""

    def __init__(self, ttl=CONTROL_TTL_SECONDS, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self.clear()

    @staticmethod
    def _key(config):
        return config["journal_bucket"], config["instance_id"], config["recovery_epoch"]

    def get(self, config):
        if self.document is None or self.key != self._key(config) or self.clock() - self.fetched_at > self.ttl:
            return None
        return dict(self.document)

    def put(self, config, document):
        self.key, self.document, self.fetched_at = self._key(config), dict(document), self.clock()

    def clear(self):
        self.key = self.document = self.fetched_at = None


class Store:
    def __init__(self, config, data_client, s3_client, *, deadline=None, control_cache=None):
        self.config = config
        self.db = DataAPI(data_client, config, deadline=deadline)
        self.s3 = s3_client
        self.cache = control_cache

    def control(self, *, fresh=True):
        if not fresh and self.cache is not None:
            cached = self.cache.get(self.config)
            if cached is not None:
                return cached
        self.db.budget()
        try:
            document = fetch_control(self.s3, self.config)[0]
        except Exception:
            if self.cache is not None:
                self.cache.clear()
            raise
        if self.cache is not None:
            self.cache.put(self.config, document)
        return document

    def envelope(self, raw, expected):
        """Trust a result only under the expected locked service state; return its generation."""
        try:
            value = json.loads(raw) if raw is not None else None
            if not isinstance(value, dict) or not {"service_state", "authority_generation"} <= value.keys():
                raise ValueError
        except ValueError:
            raise MemoryError("temporarily_unavailable") from None
        try:
            if value["service_state"] is None or parse_control(canonical(value["service_state"]), self.config) != expected:
                raise MemoryError("context_stale")
        except MemoryError:
            if self.cache is not None:
                self.cache.clear()
            raise MemoryError("context_stale") from None
        generation = value["authority_generation"]
        if type(generation) is not int or generation < 0:
            raise MemoryError("temporarily_unavailable")
        return value, generation

    def perform(self, route, request, actor, *, client_protocol=1):
        """Return (result, authority_generation) for one validated request."""
        mutation = route in WRITE_ROUTES
        expected = self.control(fresh=mutation)
        if route not in DISPATCHED_ROUTES:
            return self._legacy(route, request, actor, expected, client_protocol, mutation)
        values = {"route": route, "request": canonical(request),
                  "request_hash": digest(request) if mutation else None,
                  "candidate_hash": digest(request["candidate"]) if "candidate" in request else None,
                  "actor": actor, "protocol": client_protocol}
        overflow = "context_incomplete" if route == "/v1/memory/context" else "temporarily_unavailable"
        if not mutation:
            # One autocommitted statement; the application role's session timeouts bound it.
            raw = self.db.json_scalar(DISPATCH, values, overflow_status=overflow)
            value, generation = self.envelope(raw, expected)
            return route_result(value.get("result")), generation
        # Mutations keep an explicit transaction so a changed control document rolls them back.
        with self.db.transaction(timeouts=False) as tx:
            raw = self.db.json_scalar(DISPATCH, values, tx, overflow_status=overflow)
            value, generation = self.envelope(raw, expected)
            result = route_result(value.get("result"))
            if self.control() != expected:
                raise MemoryError("context_stale")
        return result, generation

    def _legacy(self, route, request, actor, expected, client_protocol, mutation):
        with self.db.transaction() as tx:
            _, generation = self.envelope(
                self.db.scalar("SELECT maven.memory_service_state()::text", transaction=tx), expected)
            self.db.execute("SELECT set_config('maven.client_protocol', :protocol, true)",
                            {"protocol": str(client_protocol)}, tx)
            if not self.db.scalar("SELECT maven.memory_protocol_allowed()", transaction=tx):
                raise MemoryError("client_incompatible", 409)
            if route == "/v1/records/change":
                raw = self.db.scalar("SELECT maven.change_fact(CAST(:request AS jsonb), :hash, :actor)::text",
                                     {"request": canonical(request), "hash": digest(request), "actor": actor}, tx)
            elif route == "/v1/records/query":
                raw = self.db.scalar(QUERY, {"record_id": request["record_id"],
                                             "revision": request.get("revision")}, tx)
                if raw is None:
                    raise MemoryError("record_unavailable", 404)
            else:
                raw = self.db.scalar(LOOKUP, {"request_key": request["request_key"]}, tx)
                if raw is None:
                    raw = self.db.scalar(INSTRUCTION_LOOKUP, {"request_key": request["request_key"]}, tx)
                if raw is None:
                    raw = self.db.scalar("SELECT maven.memory_lookup(CAST(:request_key AS uuid))::text",
                                         {"request_key": request["request_key"]}, tx)
                if raw is None:
                    raise MemoryError("operation_unavailable", 404)
            result = route_result(json.loads(raw))
            if mutation and self.control() != expected:
                raise MemoryError("context_stale")
        return result, generation
