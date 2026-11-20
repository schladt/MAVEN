"""Strict sourced memory and approved instruction contracts; no inferred authority."""

import hashlib
import json
import math
import re
from uuid import UUID

MAX_REQUEST_BYTES = 8192
MAX_PROPOSAL_REQUEST_BYTES = 1048576
MAX_BODY_BYTES = 131072
MAX_SKILL_DESCRIPTION = 500
MAX_MEMORY_REQUEST_BYTES = 196608
MAX_SEARCH_TERMS = 8
MAX_SEARCH_OFFSET = 1000
MAX_DETAIL = 200
MEMORY_KINDS = {"entity", "fact", "relationship", "task", "decision", "risk",
                "note", "session", "operation", "setting", "checkpoint"}
MEMORY_ROUTES = {"/v1/memory/" + action for action in ("write", "read", "search", "context",
                                                       "secret_lookup", "secret_catalog",
                                                       "secret_adapter_policy", "secret_adapter_consume")}
RECONCILE_ROUTES = {"/v1/reconcile/" + action for action in ("status", "inventory", "read", "apply")}
APPROVAL_ROUTES = {"/v1/proposals/challenge", "/v1/proposals/resolve"}
MUTATION_ROUTES = {"/v1/proposals/create", "/v1/proposals/resolve", "/v1/reconcile/apply",
                   "/v1/memory/write", "/v1/memory/secret_catalog"}
ROUTES = ({"/v1/records/query", "/v1/operations/lookup", "/v1/proposals/create", "/v1/pages/index",
           "/v1/pages/read"} | APPROVAL_ROUTES | MEMORY_ROUTES | RECONCILE_ROUTES)

# Version 2 requires migration 012 and memory-native checkpoint/scope contracts.
SCHEMA_VERSION = 2
# Version 4 requires migration 013: one dispatcher call per request and ranked retrieval.
CLIENT_PROTOCOL = 4



def request_limit(route):
    if route == "/v1/reconcile/apply":
        return 1024 * 1024
    if route in ("/v1/memory/write", "/v1/memory/secret_catalog", "/v1/memory/secret_adapter_consume"):
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
        if isinstance(metadata.get("detail"), str) and metadata["detail"]:
            self.metadata["detail"] = metadata["detail"][:MAX_DETAIL]


def invalid(detail=None):
    """Details name fields and kinds only; caller values are never reflected."""
    raise MemoryError("invalid_request", 422, detail=detail)


def _names(keys):
    names = sorted(key for key in keys if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key))
    hidden = len(keys) - len(names)
    return ", ".join(names + ([f"{hidden} unsupported name(s)"] if hidden else []))


def uuid_string(value, *, name="value"):
    if not isinstance(value, str):
        invalid(f"{name} must be a lowercase canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        invalid(f"{name} must be a lowercase canonical UUID")
    if str(parsed) != value:
        invalid(f"{name} must be a lowercase canonical UUID")
    return value


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        invalid("request is not serializable JSON")


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid("request repeats a JSON field")
        result[key] = value
    return result


def decode_json(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda _: invalid("request contains a non-finite number"))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        invalid("request is not valid JSON")


def fields(value, required, optional=(), *, name="request"):
    if not isinstance(value, dict):
        invalid(f"{name} must be an object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        problems = ([f"requires {_names(missing)}"] if missing else []) + (
            [f"does not accept {_names(extra)}"] if extra else [])
        invalid(f"{name} {'; '.join(problems)}; allowed: {', '.join(sorted({*required, *optional}))}")


def text(value, maximum, *, empty=False, name="value"):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        invalid(f"{name} must be a string of at most {maximum} characters")
    if not empty and not value.strip():
        invalid(f"{name} must not be empty")


def revision(value, *, name="revision"):
    if type(value) is not int or not 1 <= value <= 2147483647:
        invalid(f"{name} must be an integer from 1 to 2147483647")


def _json_value(value, depth=0, maximum=MAX_REQUEST_BYTES):
    if depth > 32:
        invalid("request nests deeper than 32 levels")
    if isinstance(value, str):
        text(value, maximum, empty=True, name="request string")
    elif value is None or type(value) is bool:
        pass
    elif type(value) in (int, float):
        if isinstance(value, float) and not math.isfinite(value):
            invalid("request contains a non-finite number")
    elif isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1, maximum)
    elif isinstance(value, dict):
        for key, item in value.items():
            text(key, MAX_REQUEST_BYTES, empty=True, name="request field name")
            _json_value(item, depth + 1, maximum)
    else:
        invalid("request contains an unsupported JSON value")


def nonnegative_integer(value, *, name="value"):
    if type(value) is not int or not 0 <= value <= 2147483647:
        invalid(f"{name} must be an integer from 0 to 2147483647")


def slug(value, *, name="slug"):
    text(value, 80, name=name)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        invalid(f"{name} must be lowercase words joined by single hyphens")


def page_kind(value):
    if value not in ("skill", "procedure", "directive"):
        invalid("page_kind must be one of directive, procedure, skill")


def source(value, *, approval=False, name="source"):
    fields(value, {"origin", "excerpt"}, name=name)
    text(value["origin"], 160, name=f"{name}.origin")
    text(value["excerpt"], 2000, empty=not approval, name=f"{name}.excerpt")


def validate_bundle(value):
    """Validate exact approved resource metadata without normalizing its hash input."""
    fields(value, {"version", "sha256", "size_bytes", "manifest_sha256", "load_policy"}, name="candidate.bundle")
    if (not isinstance(value["version"], str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value["version"])):
        invalid("candidate.bundle.version must be 1-80 letters, digits, dots, underscores or hyphens")
    for key in ("sha256", "manifest_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            invalid(f"candidate.bundle.{key} must be 64 lowercase hex characters")
    if type(value["size_bytes"]) is not int or not 1 <= value["size_bytes"] <= 268435456:
        invalid("candidate.bundle.size_bytes must be an integer from 1 to 268435456")
    if value["load_policy"] not in ("always", "on-demand"):
        invalid("candidate.bundle.load_policy must be always or on-demand")
    return value


def _instruction(route, value):
    if route == "/v1/proposals/create":
        fields(value, {"request_key", "proposal_id", "page_id", "expected_revision",
                       "classification", "data_scope", "candidate", "source", "change_reason"})
        for key in ("request_key", "proposal_id", "page_id"):
            uuid_string(value[key], name=key)
        nonnegative_integer(value["expected_revision"], name="expected_revision")
        if value["classification"] != "Sensitive":
            invalid("classification must be Sensitive")
        if value["data_scope"] not in ("synthetic", "owner"):
            invalid("data_scope must be owner or synthetic")
        candidate = value["candidate"]
        fields(candidate, {"slug", "page_kind", "title", "body_markdown", "scope", "authority_state"},
               {"requires", "bundle", "description"}, name="candidate")
        if "requires" in candidate:
            string_list(candidate["requires"], 32, 80, validator=slug, name="candidate.requires")
            if candidate["slug"] in candidate["requires"]:
                invalid("candidate.requires must not include the page itself")
        reject_secrets(value)
        slug(candidate["slug"], name="candidate.slug")
        page_kind(candidate["page_kind"])
        if "description" in candidate:
            if candidate["page_kind"] != "skill":
                invalid("candidate.description is only allowed for skill pages")
            text(candidate["description"], MAX_SKILL_DESCRIPTION, name="candidate.description")
        if "bundle" in candidate:
            if candidate["page_kind"] != "skill":
                invalid("candidate.bundle is only allowed for skill pages")
            validate_bundle(candidate["bundle"])
        text(candidate["title"], 160, name="candidate.title")
        text(candidate["body_markdown"], MAX_BODY_BYTES, name="candidate.body_markdown")
        if len(candidate["body_markdown"].encode("utf-8")) > MAX_BODY_BYTES:
            invalid(f"candidate.body_markdown exceeds {MAX_BODY_BYTES} UTF-8 bytes")
        fields(candidate["scope"], (), {"project", "workflow", "operation"}, name="candidate.scope")
        for key, scope_value in candidate["scope"].items():
            text(scope_value, 80, name=f"candidate.scope.{key}")
        if candidate["authority_state"] not in ("active", "revoked"):
            invalid("candidate.authority_state must be active or revoked")
        source(value["source"])
        text(value["change_reason"], 240, name="change_reason")
        from .adapter_contracts import AdapterError, validate_adapter_candidate
        try:
            validate_adapter_candidate(candidate)
        except AdapterError:
            invalid("adapter authority candidate does not satisfy its declarative policy contract")
    elif route == "/v1/proposals/challenge":
        fields(value, {"proposal_id"})
        uuid_string(value["proposal_id"], name="proposal_id")
    elif route == "/v1/proposals/resolve":
        fields(value, {"request_key", "proposal_id", "challenge_id", "candidate_hash",
                       "expected_revision", "approval_source"})
        for key in ("request_key", "proposal_id", "challenge_id"):
            uuid_string(value[key], name=key)
        text(value["candidate_hash"], 64, name="candidate_hash")
        if not re.fullmatch(r"[0-9a-f]{64}", value["candidate_hash"]):
            invalid("candidate_hash must be 64 lowercase hex characters")
        nonnegative_integer(value["expected_revision"], name="expected_revision")
        source(value["approval_source"], approval=True, name="approval_source")
        reject_secrets(value)
    elif route == "/v1/pages/index":
        fields(value, (), {"page_kind", "after_slug", "limit", "scope", "project_id"})
        if "scope" in value:
            if value["scope"] not in ("universal", "context"):
                invalid("scope must be universal or context")
            if (value["scope"] == "context") != ("project_id" in value):
                invalid("context scope requires project_id; universal scope excludes it")
        elif "project_id" in value:
            invalid("project_id requires context scope")
        if "project_id" in value:
            uuid_string(value["project_id"], name="project_id")
        if "page_kind" in value:
            page_kind(value["page_kind"])
        if "after_slug" in value:
            slug(value["after_slug"], name="after_slug")
        if "limit" in value and (type(value["limit"]) is not int or not 1 <= value["limit"] <= 10):
            invalid("limit must be an integer from 1 to 10")
    else:
        fields(value, (), {"slug", "proposal_id", "offset", "revision", "expected_revision"})
        if ("slug" in value) == ("proposal_id" in value):
            invalid("exactly one of slug or proposal_id is required")
        if "proposal_id" in value:
            uuid_string(value["proposal_id"], name="proposal_id")
            if "revision" in value or "expected_revision" in value:
                invalid("proposal reads do not accept revision or expected_revision")
        else:
            slug(value["slug"])
            if "revision" in value and "expected_revision" in value:
                invalid("revision and expected_revision are mutually exclusive")
            for key in ("revision", "expected_revision"):
                if key in value:
                    revision(value[key], name=key)
        if "offset" in value:
            nonnegative_integer(value["offset"], name="offset")


def string_list(value, count, maximum, *, validator=None, name="list"):
    if not isinstance(value, list) or len(value) > count:
        invalid(f"{name} must be a list of at most {count} strings")
    for item in value:
        text(item, maximum, name=f"{name} item")
        if validator:
            validator(item, name=f"{name} item")
    if len(set(value)) != len(value):
        invalid(f"{name} must not repeat values")


def reject_secrets(value):
    """Reject credential-shaped material without reflecting it in diagnostics."""
    secret_keys = {"password", "passwd", "secret_access_key", "aws_secret_access_key",
                   "access_token", "refresh_token", "id_token", "api_key", "private_key",
                   "client_secret", "authorization", "session_token", "aws_session_token"}
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[- ]", "_", key.lower()) in secret_keys:
                invalid("credential-named fields are not accepted; store a secret_ref instead")
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
            invalid("credential-shaped text is not accepted")


def _memory_data(kind, data, request):
    """Only project identity administration and the separate secret catalog still use this shape."""
    if kind == "entity":
        fields(data, {"entity_type", "name", "aliases"}, name="project entity")
        if data["entity_type"] != "project" or "project_id" in request:
            invalid("expected an unscoped project identity")
        text(data["name"], 240, name="data.name")
        string_list(data["aliases"], 32, 240, name="data.aliases")
    elif kind == "setting":
        fields(data, {"key", "value", "scope", "secret_ref"}, name="secret catalog setting")
        text(data["key"], 160, name="data.key")
        if data["scope"] != "project" or "project_id" not in request:
            invalid("secret catalog settings require a project")
        validate_secret_ref(data["secret_ref"])
    else:
        invalid("ordinary project writes require reconciliation")
    if len(canonical(data).encode("utf-8")) > 147456:
        invalid("data exceeds 147456 bytes")


def _memory_kind(value):
    if not isinstance(value, str) or value not in MEMORY_KINDS:
        invalid(f"kind must be one of {', '.join(sorted(MEMORY_KINDS))}")


def validate_secret_ref(value):
    from .secrets_contract import validate_secret_ref as check
    return check(value)


def _memory(route, value):
    action = route.rsplit("/", 1)[1]
    if action == "write":
        fields(value, {"request_key", "record_id", "expected_revision", "kind", "classification",
                       "data_scope", "data", "source", "change_reason"}, {"project_id", "tags", "authority_generation"})
        uuid_string(value["request_key"], name="request_key")
        uuid_string(value["record_id"], name="record_id")
        nonnegative_integer(value["expected_revision"], name="expected_revision")
        if "authority_generation" in value:
            nonnegative_integer(value["authority_generation"], name="authority_generation")
        _memory_kind(value["kind"])
        if value["classification"] != "Sensitive":
            invalid("classification must be Sensitive")
        if value["data_scope"] not in ("owner", "synthetic"):
            invalid("data_scope must be owner or synthetic")
        if "project_id" in value:
            uuid_string(value["project_id"], name="project_id")
        if "tags" in value:
            string_list(value["tags"], 20, 80, name="tags")
        source(value["source"])
        text(value["change_reason"], 240, name="change_reason")
        reject_secrets(value)
        _memory_data(value["kind"], value["data"], value)
    elif action == "read":
        fields(value, {"record_id"}, {"revision", "include_archived"})
        uuid_string(value["record_id"], name="record_id")
        if "revision" in value:
            revision(value["revision"])
        if "include_archived" in value and type(value["include_archived"]) is not bool:
            invalid("include_archived must be a boolean")
    elif action == "search":
        fields(value, (), {"kind", "project_id", "scope", "query", "tag", "subject_id", "state", "after_id",
                           "limit", "include_archived", "offset"})
        if "scope" in value:
            if value["scope"] not in ("project", "universal", "context", "all"):
                invalid("scope must be one of all, context, project, universal")
            if value["scope"] == "project" and "project_id" not in value:
                invalid("scope project requires project_id")
            if value["scope"] in ("universal", "all") and "project_id" in value:
                invalid(f"scope {value['scope']} does not accept project_id")
        if "kind" in value:
            _memory_kind(value["kind"])
        for key in ("project_id", "subject_id", "after_id"):
            if key in value:
                uuid_string(value[key], name=key)
        for key, maximum in (("query", 160), ("tag", 80), ("state", 32)):
            if key in value:
                text(value[key], maximum, name=key)
        if "query" in value and len(value["query"].split()) > MAX_SEARCH_TERMS:
            invalid(f"query allows at most {MAX_SEARCH_TERMS} words")
        if "limit" in value and (type(value["limit"]) is not int or not 1 <= value["limit"] <= 20):
            invalid("limit must be an integer from 1 to 20")
        if "include_archived" in value and type(value["include_archived"]) is not bool:
            invalid("include_archived must be a boolean")
        if "offset" in value:
            if "query" not in value:
                invalid("offset pages ranked results and requires query")
            if type(value["offset"]) is not int or not 0 <= value["offset"] <= MAX_SEARCH_OFFSET:
                invalid(f"offset must be an integer from 0 to {MAX_SEARCH_OFFSET}")
        if "query" in value and "after_id" in value:
            invalid("after_id pages listings without query; use offset with query")
    elif action == "context":
        fields(value, (), {"project_id", "workflow", "operation", "skill_slug", "instructions_only"})
        if "instructions_only" in value and type(value["instructions_only"]) is not bool:
            invalid("instructions_only must be a boolean")
        if "project_id" in value:
            uuid_string(value["project_id"], name="project_id")
        for key in ("workflow", "operation"):
            if key in value:
                text(value[key], 80, name=key)
        if "skill_slug" in value:
            slug(value["skill_slug"], name="skill_slug")


def validate(route, value):
    """Validate without touching AWS, returning the same request object."""
    if route not in ROUTES:
        invalid("unknown route")
    maximum = request_limit(route)
    if route not in RECONCILE_ROUTES:
        _json_value(value, maximum=maximum)
    try:
        if len(canonical(value).encode("utf-8")) > maximum:
            invalid(f"request exceeds {maximum} bytes")
    except UnicodeError:
        invalid("request is not valid UTF-8")
    if route in RECONCILE_ROUTES:
        from .reconcile_transport import validate_request
        validate_request(route, value)
    elif route in ("/v1/memory/secret_lookup", "/v1/memory/secret_catalog"):
        from .secrets_contract import validate_secret_request
        validate_secret_request(route, value)
    elif route in ("/v1/memory/secret_adapter_policy", "/v1/memory/secret_adapter_consume"):
        from .adapter_contracts import AdapterError, validate_adapter_request
        try:
            validate_adapter_request(route, value)
        except AdapterError:
            invalid("adapter request does not satisfy its declarative policy contract")
    elif route in MEMORY_ROUTES:
        _memory(route, value)
        if route == "/v1/memory/write" and (value["kind"] != "entity" or value["data_scope"] != "owner"):
            invalid("only owner project identity administration is supported here")
    elif route == "/v1/operations/lookup":
        fields(value, {"request_key"})
        uuid_string(value["request_key"], name="request_key")
    elif route == "/v1/records/query":
        fields(value, {"record_id"}, {"revision"})
        uuid_string(value["record_id"], name="record_id")
        if "revision" in value:
            revision(value["revision"])
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
