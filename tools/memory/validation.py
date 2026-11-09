"""Strict sourced memory and approved instruction contracts; no inferred authority."""

import hashlib
import json
import math
import re
from uuid import UUID

MAX_REQUEST_BYTES = 8192
MAX_PROPOSAL_REQUEST_BYTES = 1048576
MAX_BODY_BYTES = 131072
MAX_MEMORY_REQUEST_BYTES = 196608
MEMORY_KINDS = {"entity", "fact", "relationship", "task", "decision", "risk",
                "note", "session", "operation", "setting", "checkpoint"}
MEMORY_ROUTES = {"/v1/memory/" + action for action in ("write", "read", "search", "context", "claim")}
APPROVAL_ROUTES = {"/v1/proposals/challenge", "/v1/proposals/resolve"}
MUTATION_ROUTES = {"/v1/records/change", "/v1/proposals/create", "/v1/proposals/resolve",
                   "/v1/memory/write", "/v1/memory/claim"}
ROUTES = {"/v1/records/change", "/v1/records/query", "/v1/operations/lookup",
          "/v1/proposals/create", "/v1/pages/index", "/v1/pages/read"} | APPROVAL_ROUTES | MEMORY_ROUTES

# Version 2 requires migration 012 and memory-native checkpoint/scope contracts.
SCHEMA_VERSION = 2
CLIENT_PROTOCOL = 3


def request_limit(route):
    if route == "/v1/memory/write":
        return MAX_MEMORY_REQUEST_BYTES
    return MAX_PROPOSAL_REQUEST_BYTES if route == "/v1/proposals/create" else MAX_REQUEST_BYTES


class MemoryError(Exception):
    """Only these bounded codes/metadata may cross the service boundary."""

    def __init__(self, status, http_status=503, **metadata):
        super().__init__(status)
        self.status = status
        self.http_status = http_status
        self.metadata = {key: value for key, value in metadata.items()
                         if key in {"current_revision", "request_key"}}


def invalid():
    raise MemoryError("invalid_request", 422)


def uuid_string(value):
    if not isinstance(value, str):
        invalid()
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        invalid()
    if str(parsed) != value:
        invalid()
    return value


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        invalid()


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid()
        result[key] = value
    return result


def decode_json(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda _: invalid())
    except (ValueError, TypeError, RecursionError, UnicodeError):
        invalid()


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        invalid()
    if value.keys() - set(required) - set(optional):
        invalid()


def text(value, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        invalid()
    if not empty and not value.strip():
        invalid()


def revision(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        invalid()


def _json_value(value, depth=0, maximum=MAX_REQUEST_BYTES):
    if depth > 32:
        invalid()
    if isinstance(value, str):
        text(value, maximum, empty=True)
    elif value is None or type(value) is bool:
        pass
    elif type(value) in (int, float):
        if isinstance(value, float) and not math.isfinite(value):
            invalid()
    elif isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1, maximum)
    elif isinstance(value, dict):
        for key, item in value.items():
            text(key, MAX_REQUEST_BYTES, empty=True)
            _json_value(item, depth + 1, maximum)
    else:
        invalid()


def nonnegative_integer(value):
    if type(value) is not int or not 0 <= value <= 2147483647:
        invalid()


def slug(value):
    text(value, 80)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        invalid()


def page_kind(value):
    if value not in ("skill", "procedure", "directive"):
        invalid()


def source(value, *, approval=False):
    fields(value, {"origin", "excerpt"})
    text(value["origin"], 160)
    text(value["excerpt"], 2000, empty=not approval)


def validate_bundle(value):
    """Validate exact approved resource metadata without normalizing its hash input."""
    fields(value, {"version", "sha256", "size_bytes", "manifest_sha256", "load_policy"})
    if (not isinstance(value["version"], str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value["version"])):
        invalid()
    for key in ("sha256", "manifest_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            invalid()
    if type(value["size_bytes"]) is not int or not 1 <= value["size_bytes"] <= 268435456:
        invalid()
    if value["load_policy"] not in ("always", "on-demand"):
        invalid()
    return value


def _instruction(route, value):
    if route == "/v1/proposals/create":
        fields(value, {"request_key", "proposal_id", "page_id", "expected_revision",
                       "classification", "data_scope", "candidate", "source", "change_reason"})
        for key in ("request_key", "proposal_id", "page_id"):
            uuid_string(value[key])
        nonnegative_integer(value["expected_revision"])
        if value["classification"] != "Sensitive" or value["data_scope"] not in ("synthetic", "owner"):
            invalid()
        candidate = value["candidate"]
        fields(candidate, {"slug", "page_kind", "title", "body_markdown", "scope", "authority_state"}, {"requires", "bundle"})
        if "requires" in candidate:
            string_list(candidate["requires"], 32, 80, validator=slug)
            if candidate["slug"] in candidate["requires"]:
                invalid()
        reject_secrets(value)
        slug(candidate["slug"])
        page_kind(candidate["page_kind"])
        if "bundle" in candidate:
            if candidate["page_kind"] != "skill":
                invalid()
            validate_bundle(candidate["bundle"])
        text(candidate["title"], 160)
        text(candidate["body_markdown"], MAX_BODY_BYTES)
        if len(candidate["body_markdown"].encode("utf-8")) > MAX_BODY_BYTES:
            invalid()
        fields(candidate["scope"], (), {"project", "workflow", "operation"})
        for scope_value in candidate["scope"].values():
            text(scope_value, 80)
        if candidate["authority_state"] not in ("active", "revoked"):
            invalid()
        source(value["source"])
        text(value["change_reason"], 240)
    elif route == "/v1/proposals/challenge":
        fields(value, {"proposal_id"})
        uuid_string(value["proposal_id"])
    elif route == "/v1/proposals/resolve":
        fields(value, {"request_key", "proposal_id", "challenge_id", "candidate_hash",
                       "expected_revision", "approval_source"})
        for key in ("request_key", "proposal_id", "challenge_id"):
            uuid_string(value[key])
        text(value["candidate_hash"], 64)
        if not re.fullmatch(r"[0-9a-f]{64}", value["candidate_hash"]):
            invalid()
        nonnegative_integer(value["expected_revision"])
        source(value["approval_source"], approval=True)
    elif route == "/v1/pages/index":
        fields(value, (), {"page_kind", "after_slug", "limit"})
        if "page_kind" in value:
            page_kind(value["page_kind"])
        if "after_slug" in value:
            slug(value["after_slug"])
        if "limit" in value and (type(value["limit"]) is not int or not 1 <= value["limit"] <= 10):
            invalid()
    else:
        fields(value, (), {"slug", "proposal_id", "offset", "revision", "expected_revision"})
        if ("slug" in value) == ("proposal_id" in value):
            invalid()
        if "proposal_id" in value:
            uuid_string(value["proposal_id"])
            if "revision" in value or "expected_revision" in value:
                invalid()
        else:
            slug(value["slug"])
            if "revision" in value and "expected_revision" in value:
                invalid()
            for key in ("revision", "expected_revision"):
                if key in value:
                    revision(value[key])
        if "offset" in value:
            nonnegative_integer(value["offset"])


def string_list(value, count, maximum, *, validator=None):
    if not isinstance(value, list) or len(value) > count:
        invalid()
    for item in value:
        text(item, maximum)
        if validator:
            validator(item)
    if len(set(value)) != len(value):
        invalid()


def reject_secrets(value):
    """Reject credential-shaped material without reflecting it in diagnostics."""
    secret_keys = {"password", "passwd", "secret_access_key", "aws_secret_access_key",
                   "access_token", "refresh_token", "id_token", "api_key", "private_key",
                   "client_secret", "authorization", "session_token", "aws_session_token"}
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[- ]", "_", key.lower()) in secret_keys:
                invalid()
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str):
        if re.search(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
                     r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
                     r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})|"
                     r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}|"
                     r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", value):
            invalid()


def _memory_data(kind, data, request):
    schemas = {
        "entity": ({"entity_type", "name", "aliases"}, set()),
        "fact": ({"subject_id", "predicate", "value"}, set()),
        "relationship": ({"subject_id", "predicate", "object_id"}, set()),
        "task": ({"title", "description", "state"}, {"subject_id"}),
        "decision": ({"title", "body_markdown", "state"}, {"subject_id"}),
        "risk": ({"title", "body_markdown", "state"}, {"subject_id"}),
        "note": ({"title", "body_markdown", "category"}, set()),
        "session": ({"state", "summary"}, {"workflow", "operation"}),
        "operation": ({"session_id", "name", "state", "summary"}, set()),
        "setting": ({"key", "value", "scope"}, {"secret_ref"}),
        "checkpoint": (({"session_id", "cutoff", "memory_refs", "state"}, {"summary"})
                       if isinstance(data, dict) and "cutoff" in data else
                       ({"session_id", "candidate_digest", "git_base", "memory_refs", "state"},
                        {"final_commit", "summary"})),
    }
    fields(data, *schemas[kind])
    if len(canonical(data).encode("utf-8")) > 147456:
        invalid()
    for key, maximum in {"title": 240, "name": 240, "description": 16000, "summary": 16000,
                         "predicate": 80, "category": 80, "workflow": 80, "operation": 80, "key": 160}.items():
        if key in data:
            text(data[key], maximum, empty=key in {"description", "summary"})
    for key in ("subject_id", "object_id", "session_id"):
        if key in data:
            uuid_string(data[key])
    if "body_markdown" in data:
        text(data["body_markdown"], MAX_BODY_BYTES, empty=True)
        if len(data["body_markdown"].encode("utf-8")) > MAX_BODY_BYTES:
            invalid()
    states = {"task": {"open", "in_progress", "blocked", "completed", "cancelled"},
              "decision": {"proposed", "accepted", "superseded"}, "risk": {"open", "mitigated", "closed"},
              "session": {"active", "closed", "abandoned"},
              "operation": {"started", "completed", "failed", "blocked"},
              "checkpoint": ({"prepared", "saved", "aborted"} if "cutoff" in data
                             else {"prepared", "committed", "aborted"})}
    if kind in states and (not isinstance(data["state"], str) or data["state"] not in states[kind]):
        invalid()
    if kind == "entity":
        if data["entity_type"] not in ("person", "organization", "project", "topic"):
            invalid()
        string_list(data["aliases"], 32, 240)
    if kind == "operation":
        text(data["name"], 160)
    if kind == "setting":
        if data["scope"] not in ("global", "project"):
            invalid()
        if (data["scope"] == "project") != ("project_id" in request):
            invalid()
        if "secret_ref" in data:
            fields(data["secret_ref"], {"provider", "name"})
            if data["secret_ref"]["provider"] not in ("ssm", "secretsmanager"):
                invalid()
            text(data["secret_ref"]["name"], 512)
            if not re.fullmatch(r"[A-Za-z0-9_./:@+=-]+", data["secret_ref"]["name"]):
                invalid()
    if kind == "checkpoint":
        if "cutoff" in data:
            nonnegative_integer(data["cutoff"])
        else:
            if not isinstance(data["candidate_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["candidate_digest"]):
                invalid()
            for key in ("git_base", "final_commit"):
                if key in data and (not isinstance(data[key], str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", data[key])):
                    invalid()
            if (data["state"] == "committed") != ("final_commit" in data):
                invalid()
        refs = data["memory_refs"]
        if not isinstance(refs, list) or len(refs) > 100:
            invalid()
        for ref in refs:
            fields(ref, {"record_id", "revision"})
            uuid_string(ref["record_id"])
            revision(ref["revision"])
        if len({(ref["record_id"], ref["revision"]) for ref in refs}) != len(refs):
            invalid()


def _memory(route, value):
    action = route.rsplit("/", 1)[1]
    if action == "write":
        fields(value, {"request_key", "record_id", "expected_revision", "kind", "classification",
                       "data_scope", "data", "source", "change_reason"}, {"project_id", "tags"})
        uuid_string(value["request_key"])
        uuid_string(value["record_id"])
        nonnegative_integer(value["expected_revision"])
        if (not isinstance(value["kind"], str) or value["kind"] not in MEMORY_KINDS
                or value["classification"] != "Sensitive" or value["data_scope"] not in ("owner", "synthetic")):
            invalid()
        if "project_id" in value:
            uuid_string(value["project_id"])
        if "tags" in value:
            string_list(value["tags"], 20, 80)
        source(value["source"])
        text(value["change_reason"], 240)
        reject_secrets(value)
        _memory_data(value["kind"], value["data"], value)
    elif action == "read":
        fields(value, {"record_id"}, {"revision"})
        uuid_string(value["record_id"])
        if "revision" in value:
            revision(value["revision"])
    elif action == "search":
        fields(value, (), {"kind", "project_id", "scope", "query", "tag", "subject_id", "state", "after_id", "limit"})
        if "scope" in value:
            if value["scope"] not in ("project", "universal", "context", "all"):
                invalid()
            if value["scope"] == "project" and "project_id" not in value:
                invalid()
            if value["scope"] in ("universal", "all") and "project_id" in value:
                invalid()
        if "kind" in value and (not isinstance(value["kind"], str) or value["kind"] not in MEMORY_KINDS):
            invalid()
        for key in ("project_id", "subject_id", "after_id"):
            if key in value:
                uuid_string(value[key])
        for key, maximum in (("query", 160), ("tag", 80), ("state", 32)):
            if key in value:
                text(value[key], maximum)
        if "limit" in value and (type(value["limit"]) is not int or not 1 <= value["limit"] <= 20):
            invalid()
    elif action == "context":
        fields(value, (), {"project_id", "workflow", "operation", "skill_slug"})
        if "project_id" in value:
            uuid_string(value["project_id"])
        for key in ("workflow", "operation"):
            if key in value:
                text(value[key], 80)
        if "skill_slug" in value:
            slug(value["skill_slug"])
    else:
        fields(value, {"request_key", "record_id", "expected_revision", "session_id", "action", "source", "change_reason"},
               {"lease_seconds", "fence"})
        for key in ("request_key", "record_id", "session_id"):
            uuid_string(value[key])
        revision(value["expected_revision"])
        source(value["source"])
        text(value["change_reason"], 240)
        reject_secrets(value)
        if value["action"] == "claim":
            if "fence" in value or type(value.get("lease_seconds")) is not int or not 30 <= value["lease_seconds"] <= 3600:
                invalid()
        elif value["action"] in ("release", "complete"):
            if "lease_seconds" in value:
                invalid()
            revision(value.get("fence"))
        else:
            invalid()

def validate(route, value):
    """Validate without touching AWS, returning the same request object."""
    if route not in ROUTES:
        invalid()
    maximum = request_limit(route)
    _json_value(value, maximum=maximum)
    try:
        if len(canonical(value).encode("utf-8")) > maximum:
            invalid()
    except UnicodeError:
        invalid()
    if route in MEMORY_ROUTES:
        _memory(route, value)
    elif route == "/v1/operations/lookup":
        fields(value, {"request_key"})
        uuid_string(value["request_key"])
    elif route == "/v1/records/query":
        fields(value, {"record_id"}, {"revision"})
        uuid_string(value["record_id"])
        if "revision" in value:
            revision(value["revision"])
    elif route == "/v1/records/change":
        fields(value, {"request_key", "action", "record_id", "classification",
                       "data_scope", "fact", "source", "change_reason"},
               {"expected_revision"})
        uuid_string(value["request_key"])
        uuid_string(value["record_id"])
        if value["classification"] != "Sensitive" or value["data_scope"] != "synthetic":
            invalid()
        if value["action"] == "correct_fact":
            revision(value.get("expected_revision"))
        elif value["action"] != "create_fact" or "expected_revision" in value:
            invalid()
        fact = value["fact"]
        fields(fact, {"predicate", "value_type", "value"})
        text(fact["predicate"], 80)
        types = {"string": str, "number": (int, float), "boolean": bool,
                 "object": dict, "array": list, "null": type(None)}
        kind = fact["value_type"]
        if not isinstance(kind, str) or kind not in types:
            invalid()
        if not isinstance(fact["value"], types[kind]):
            invalid()
        if kind == "number" and type(fact["value"]) is bool:
            invalid()
        fields(value["source"], {"origin", "excerpt"})
        text(value["source"]["origin"], 160)
        text(value["source"]["excerpt"], 2000, empty=True)
        text(value["change_reason"], 240)
    else:
        _instruction(route, value)
    return value


def authorized_actor(event, role_arn):
    """Trust only API Gateway's IAM authorizer, never a caller header."""
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/(.+)", role_arn or "")
    try:
        actor = event["requestContext"]["authorizer"]["iam"]["userArn"]
    except (KeyError, TypeError):
        raise MemoryError("not_authorized", 403) from None
    if not match or not isinstance(actor, str):
        raise MemoryError("not_authorized", 403)
    account, role_path = match.groups()
    role_name = role_path.rsplit("/", 1)[-1]
    pattern = rf"arn:aws:sts::{account}:assumed-role/{re.escape(role_name)}/[\w+=,.@-]+"
    if not re.fullmatch(pattern, actor):
        raise MemoryError("not_authorized", 403)
    return actor


def validate_change(body):
    return validate("/v1/records/change", body)


def validate_query(body):
    return validate("/v1/records/query", body)


def validate_lookup(body):
    return validate("/v1/operations/lookup", body)


def validate_create(body):
    return validate("/v1/proposals/create", body)


def validate_challenge(body):
    return validate("/v1/proposals/challenge", body)


def validate_resolve(body):
    return validate("/v1/proposals/resolve", body)


def validate_index(body):
    return validate("/v1/pages/index", body)


def validate_read(body):
    return validate("/v1/pages/read", body)
