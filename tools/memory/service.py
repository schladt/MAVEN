"""IAM memory HTTP API. Payloads and provider errors are never logged."""

import base64
import binascii
import json
import os
import time

from .store import Store, safe_provider_error, sdk_config
from .validation import (APPROVAL_ROUTES, CLIENT_PROTOCOL, MemoryError, ROUTES, authorized_actor,
                         decode_json, request_limit, uuid_string, validate)


def configuration():
    names = {"cluster_arn": "MEMORY_CLUSTER_ARN", "app_secret_arn": "MEMORY_SECRET_ARN",
             "database": "MEMORY_DATABASE", "journal_bucket": "MEMORY_JOURNAL_BUCKET",
             "instance_id": "MEMORY_INSTANCE_ID", "recovery_epoch": "MEMORY_RECOVERY_EPOCH",
             "client_role_arn": "MEMORY_CLIENT_ROLE_ARN",
             "approval_role_arn": "MEMORY_APPROVAL_ROLE_ARN"}
    try:
        config = {key: os.environ[name] for key, name in names.items()}
        uuid_string(config["instance_id"])
        uuid_string(config["recovery_epoch"])
        if any(not value for value in config.values()):
            raise ValueError
        if config["approval_role_arn"] == config["client_role_arn"]:
            raise ValueError
    except (KeyError, ValueError, MemoryError):
        raise MemoryError("context_stale") from None
    return config


def make_store(config, deadline):
    import boto3
    return Store(config, boto3.client("rds-data", config=sdk_config()),
                 boto3.client("s3", config=sdk_config()), deadline=deadline)


def response(status_code, body):
    return {"statusCode": status_code,
            "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
            "isBase64Encoded": False}


def route_actor(event, route, config):
    client_role = config["client_role_arn"]
    approval_role = config.get("approval_role_arn")
    if route in APPROVAL_ROUTES:
        if not approval_role or approval_role == client_role:
            raise MemoryError("not_authorized", 403)
        return authorized_actor(event, approval_role)
    if route == "/v1/operations/lookup":
        try:
            return authorized_actor(event, client_role)
        except MemoryError:
            if not approval_role or approval_role == client_role:
                raise MemoryError("not_authorized", 403) from None
            return authorized_actor(event, approval_role)
    return authorized_actor(event, client_role)


def handler(event, context):
    """API Gateway v2 handler; only exact POST routes touch AWS."""
    request_key = None
    try:
        config = configuration()
        try:
            method = event["requestContext"]["http"]["method"]
            path = event["rawPath"]
        except (KeyError, TypeError):
            raise MemoryError("invalid_request", 422) from None
        if method != "POST" or path not in ROUTES:
            raise MemoryError("invalid_request", 422)
        actor = route_actor(event, path, config)
        if event.get("rawQueryString"):
            raise MemoryError("invalid_request", 422)
        body = event.get("body")
        maximum = request_limit(path)
        encoded_maximum = 4 * ((maximum + 2) // 3)
        if not isinstance(body, str) or len(body) > encoded_maximum:
            raise MemoryError("invalid_request", 422)
        if event.get("isBase64Encoded", False):
            try:
                body = base64.b64decode(body, validate=True).decode("utf-8")
            except (ValueError, UnicodeError, binascii.Error):
                raise MemoryError("invalid_request", 422) from None
        try:
            if len(body.encode("utf-8")) > maximum:
                raise MemoryError("invalid_request", 422)
        except UnicodeError:
            raise MemoryError("invalid_request", 422) from None
        request = validate(path, decode_json(body))
        headers = event.get("headers") or {}
        protocol = headers.get("x-maven-memory-protocol", "1")
        if not isinstance(protocol, str) or protocol != str(CLIENT_PROTOCOL):
            raise MemoryError("client_incompatible", 409)
        request_key = request.get("request_key")
        remaining = context.get_remaining_time_in_millis() / 1000 if context is not None else 25
        deadline = time.monotonic() + max(0, min(25, remaining) - 1)
        result = make_store(config, deadline).perform(path, request, actor, client_protocol=int(protocol))
        return response(200, {"status": "ready", "data": result})
    except MemoryError as error:
        result = {"status": error.status, **error.metadata}
        if request_key is not None:
            result["request_key"] = request_key
        return response(error.http_status, result)
    except Exception as error:
        safe = safe_provider_error(error)
        result = {"status": safe.status}
        if request_key is not None:
            result["request_key"] = request_key
        return response(safe.http_status, result)
