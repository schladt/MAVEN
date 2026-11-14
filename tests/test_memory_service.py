"""Offline boundary regressions, not an emulation of PostgreSQL or AWS."""

import base64
from io import BytesIO
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from tools.memory import admin, service, store, validation

REAL_MAKE_STORE = service.make_store

INSTANCE = "10000000-0000-4000-8000-000000000001"
EPOCH = "10000000-0000-4000-8000-000000000002"
RECORD = "10000000-0000-4000-8000-000000000003"
KEY = "10000000-0000-4000-8000-000000000004"
ROLE = "arn:aws:iam::123456789012:role/memory-client"
ACTOR = "arn:aws:sts::123456789012:assumed-role/memory-client/test"
APPROVAL_ROLE = "arn:aws:iam::123456789012:role/memory-approval"
APPROVAL_ACTOR = "arn:aws:sts::123456789012:assumed-role/memory-approval/test"
CONFIG = {"cluster_arn": "arn:aws:rds:us-east-1:123456789012:cluster:synthetic",
          "app_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:app",
          "admin_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:admin",
          "database": "maven", "journal_bucket": "synthetic-memory-test",
          "instance_id": INSTANCE, "recovery_epoch": EPOCH, "client_role_arn": ROLE,
          "admin_role_arn": "arn:aws:iam::123456789012:role/memory-admin",
          "approval_role_arn": APPROVAL_ROLE,
          "region": "us-east-1", "endpoint": "https://example.invalid", "schema_version": 2}


def change():
    return {"request_key": KEY, "action": "create_fact", "record_id": RECORD,
            "classification": "Sensitive", "data_scope": "synthetic",
            "fact": {"predicate": "fixture", "value_type": "string", "value": "synthetic fact"},
            "source": {"origin": "synthetic fixture", "excerpt": "synthetic evidence"},
            "change_reason": "synthetic test"}


def proposal():
    return {"request_key": KEY, "proposal_id": EPOCH, "page_id": RECORD,
            "expected_revision": 0, "classification": "Sensitive", "data_scope": "synthetic",
            "candidate": {"slug": "synthetic-procedure", "page_kind": "procedure",
                          "title": "Synthetic procedure", "body_markdown": "# Synthetic\n\nExample",
                          "scope": {}, "authority_state": "active"},
            "source": {"origin": "synthetic fixture", "excerpt": ""},
            "change_reason": "synthetic test"}


def resolution():
    return {"request_key": KEY, "proposal_id": EPOCH, "challenge_id": INSTANCE,
            "candidate_hash": validation.digest(proposal()["candidate"]), "expected_revision": 0,
            "approval_source": {"origin": "synthetic owner approval", "excerpt": "Approve exact candidate"}}


def event(body=None, path="/v1/records/change"):
    return {"rawPath": path, "rawQueryString": "", "body": json.dumps(change() if body is None else body),
            "headers": {"x-maven-memory-protocol": str(validation.CLIENT_PROTOCOL)},
            "requestContext": {"http": {"method": "POST"},
                               "authorizer": {"iam": {"userArn": ACTOR}}}}


def scalar(value):
    return {"records": [[{"stringValue": json.dumps(value)}]]}


def control_response(state="ready"):
    return {"Body": BytesIO(json.dumps(store.control_document(CONFIG, state)).encode()), "ETag": '"v1"'}


class ValidationTests(unittest.TestCase):
    def test_typed_values_and_stable_canonical_hash(self):
        for kind, value in [("string", "é"), ("number", 2.5), ("boolean", False),
                            ("object", {"a": 1}), ("array", [None]), ("null", None)]:
            request = change()
            request["fact"].update(value_type=kind, value=value)
            self.assertEqual(validation.validate_change(request), request)
            reverse = dict(reversed(list(request.items())))
            self.assertEqual(validation.digest(request), validation.digest(reverse))

    def test_owner_facts_are_rejected_for_create_and_correction(self):
        for action in ("create_fact", "correct_fact"):
            body = change()
            body.update(action=action, data_scope="owner")
            if action == "correct_fact":
                body["expected_revision"] = 1
            with self.subTest(action=action), self.assertRaises(validation.MemoryError):
                validation.validate_change(body)

    def test_rejects_scope_unknown_fields_bad_types_and_revision(self):
        variants = []
        for key, value in [("classification", "Secret"), ("data_scope", "personal"),
                           ("extra", True), ("expected_revision", 1)]:
            body = change()
            body[key] = value
            variants.append(body)
        for value in [True, 0, -1, 1.5, None]:
            body = change()
            body.update(action="correct_fact", expected_revision=value)
            variants.append(body)
        for kind, value in [("number", True), ("number", float("nan")),
                            ("number", float("inf")), ("object", []), ("null", "null"),
                            ("string", "\x00"), ("string", "x" * 9000)]:
            body = change()
            body["fact"].update(value_type=kind, value=value)
            variants.append(body)
        for body in variants:
            with self.subTest(body=repr(body)[:80]), self.assertRaises(validation.MemoryError) as error:
                validation.validate_change(body)
            self.assertEqual(error.exception.status, "invalid_request")

    def test_duplicate_json_keys_and_nonfinite_numbers_rejected(self):
        for body in ['{"record_id":1,"record_id":2}', '{"value":NaN}', '{"value":Infinity}']:
            with self.assertRaises(validation.MemoryError):
                validation.decode_json(body)


class InstructionValidationTests(unittest.TestCase):
    def test_valid_requests_preserve_exact_input(self):
        for route, body in [
                ("/v1/proposals/create", proposal()),
                ("/v1/proposals/challenge", {"proposal_id": EPOCH}),
                ("/v1/proposals/resolve", resolution()),
                ("/v1/pages/index", {}),
                ("/v1/pages/index", {"page_kind": "skill", "limit": 10, "after_slug": "example"}),
                ("/v1/pages/read", {"slug": "example", "offset": 4096, "expected_revision": 2}),
                ("/v1/pages/read", {"slug": "example", "revision": 1}),
                ("/v1/pages/read", {"proposal_id": EPOCH})]:
            with self.subTest(route=route, body=body):
                self.assertIs(validation.validate(route, body), body)

    def test_owner_scope_is_request_metadata_not_candidate_content(self):
        synthetic = proposal()
        owner = proposal()
        owner["data_scope"] = "owner"
        owner["candidate"]["page_kind"] = "skill"
        synthetic["candidate"]["page_kind"] = "skill"
        self.assertIs(validation.validate_create(owner), owner)
        self.assertEqual(validation.digest(owner["candidate"]), validation.digest(synthetic["candidate"]))
        self.assertNotEqual(validation.digest(owner), validation.digest(synthetic))
        owner["candidate"]["data_scope"] = "owner"
        with self.assertRaises(validation.MemoryError):
            validation.validate_create(owner)

    def test_owner_pages_still_reject_secret_classification(self):
        for classification in ("Secret", "Restricted", None):
            body = proposal()
            body.update(data_scope="owner", classification=classification)
            with self.subTest(classification=classification), self.assertRaises(validation.MemoryError):
                validation.validate_create(body)

    def test_candidate_and_proposal_fields_are_strict(self):
        for key, value in [("slug", "Bad_Slug"), ("slug", "x" * 81),
                           ("page_kind", "reference"), ("title", " "),
                           ("scope", {"unknown": "scope"}), ("scope", {"project": ""}),
                           ("scope", {"workflow": 1}), ("authority_state", "draft"),
                           ("body_markdown", ""), ("body_markdown", "\ud800")]:
            body = proposal()
            body["candidate"][key] = value
            with self.subTest(key=key, value=repr(value)), self.assertRaises(validation.MemoryError):
                validation.validate_create(body)
        for key, value in [("approved", True), ("classification", "Secret"),
                           ("data_scope", "personal"), ("expected_revision", True),
                           ("expected_revision", -1), ("proposal_id", "not-a-uuid")]:
            body = proposal()
            body[key] = value
            with self.subTest(key=key), self.assertRaises(validation.MemoryError):
                validation.validate_create(body)

    def test_body_limit_is_utf8_bytes_not_characters(self):
        body = proposal()
        body["candidate"]["body_markdown"] = "😀" * (validation.MAX_BODY_BYTES // 4)
        self.assertIs(validation.validate_create(body), body)
        body["candidate"]["body_markdown"] += "a"
        with self.assertRaises(validation.MemoryError):
            validation.validate_create(body)
        body["candidate"]["body_markdown"] = "a" * validation.MAX_BODY_BYTES
        self.assertIs(validation.validate_create(body), body)

    def test_bundle_metadata_is_exact_skill_only_and_hash_bound(self):
        body = proposal()
        body["candidate"]["page_kind"] = "skill"
        legacy_hash = validation.digest(body["candidate"])
        self.assertIs(validation.validate_create(body), body)
        self.assertNotIn("bundle", body["candidate"])
        self.assertEqual(validation.digest(body["candidate"]), legacy_hash)
        bundle = {"version": "1.0.0", "sha256": "a" * 64, "size_bytes": 123,
                  "manifest_sha256": "b" * 64, "load_policy": "on-demand"}
        body["candidate"]["bundle"] = dict(bundle)
        self.assertIs(validation.validate_create(body), body)
        self.assertEqual(body["candidate"]["bundle"], bundle)
        self.assertNotEqual(validation.digest(body["candidate"]), legacy_hash)
        for field, bad in [
            ("version", ""), ("version", "../1"), ("version", "é"), ("version", "x" * 81),
            ("sha256", "A" * 64), ("sha256", 3), ("manifest_sha256", "b" * 63),
            ("size_bytes", True), ("size_bytes", 1.0), ("size_bytes", 0),
            ("size_bytes", 268435457), ("load_policy", "sometimes"),
            ("bucket", "unapproved"), ("key", "unapproved"), ("url", "https://invalid.example"),
        ]:
            body["candidate"]["bundle"] = {**bundle, field: bad}
            with self.subTest(field=field, bad=bad), self.assertRaises(validation.MemoryError):
                validation.validate_create(body)
        for field in bundle:
            body["candidate"]["bundle"] = {key: value for key, value in bundle.items() if key != field}
            with self.subTest(missing=field), self.assertRaises(validation.MemoryError):
                validation.validate_create(body)
        body["candidate"]["bundle"] = bundle
        for kind in ("procedure", "directive"):
            body["candidate"]["page_kind"] = kind
            with self.subTest(kind=kind), self.assertRaises(validation.MemoryError):
                validation.validate_create(body)

    def test_approval_requires_exact_binding_and_nonblank_evidence(self):
        for key, value in [("candidate_hash", "f" * 63), ("candidate_hash", "g" * 64),
                           ("candidate_hash", True), ("expected_revision", False),
                           ("approved", True), ("approval_source", {"origin": "owner", "excerpt": ""})]:
            body = resolution()
            body[key] = value
            with self.subTest(key=key), self.assertRaises(validation.MemoryError):
                validation.validate_resolve(body)
        body = resolution()
        del body["challenge_id"]
        with self.assertRaises(validation.MemoryError):
            validation.validate_resolve(body)
        with self.assertRaises(validation.MemoryError):
            validation.validate_challenge({"proposal_id": EPOCH, "approved": True})

    def test_discovery_and_retrieval_reject_ambiguous_selectors(self):
        for body in [{}, {"slug": "example", "proposal_id": EPOCH},
                     {"proposal_id": EPOCH, "revision": 1},
                     {"proposal_id": EPOCH, "expected_revision": 1},
                     {"slug": "example", "revision": 1, "expected_revision": 1},
                     {"slug": "example", "offset": True}, {"slug": "example", "offset": -1},
                     {"slug": "example", "revision": 0}, {"slug": "example", "extra": 1}]:
            with self.subTest(body=body), self.assertRaises(validation.MemoryError):
                validation.validate_read(body)
        for body in [{"limit": True}, {"limit": 0}, {"limit": 11}, {"page_kind": "note"},
                     {"after_slug": "Not-valid"}, {"scope": {}}]:
            with self.subTest(body=body), self.assertRaises(validation.MemoryError):
                validation.validate_index(body)

    def test_nonproposal_requests_keep_original_size_limit(self):
        body = change()
        body["fact"]["value"] = "a" * validation.MAX_REQUEST_BYTES
        with self.assertRaises(validation.MemoryError):
            validation.validate_change(body)
        self.assertEqual(validation.request_limit("/v1/pages/read"), 8192)
        self.assertEqual(validation.request_limit("/v1/proposals/create"), 1048576)



class HandlerTests(unittest.TestCase):
    def setUp(self):
        config_patch = patch.object(service, "configuration", return_value=CONFIG)
        self.addCleanup(config_patch.stop)
        config_patch.start()
        store_patch = patch.object(service, "make_store")
        self.addCleanup(store_patch.stop)
        self.factory = store_patch.start()

    def test_unknown_route_and_unauthorized_do_not_touch_aws(self):
        request = event(path="/v1/arbitrary")
        self.assertEqual(service.handler(request, None)["statusCode"], 422)
        request = event()
        request["requestContext"].pop("authorizer")
        request["headers"] = {"userArn": ACTOR}
        self.assertEqual(service.handler(request, None)["statusCode"], 403)
        request = event()
        request["requestContext"]["authorizer"]["iam"]["userArn"] = ACTOR.replace("123456789012", "999999999999")
        self.assertEqual(service.handler(request, None)["statusCode"], 403)
        self.factory.assert_not_called()

    def test_invalid_input_does_not_touch_aws(self):
        body = change()
        body["data_scope"] = "personal"
        self.assertEqual(service.handler(event(body), None)["statusCode"], 422)
        self.factory.assert_not_called()

    def test_owner_scope_only_reaches_instruction_creation(self):
        self.factory.return_value.perform.return_value = ({}, 7)
        body = proposal()
        body["data_scope"] = "owner"
        self.assertEqual(service.handler(event(body, "/v1/proposals/create"), None)["statusCode"], 200)
        self.factory.return_value.perform.assert_called_once_with("/v1/proposals/create", body, ACTOR,
                                                                  client_protocol=validation.CLIENT_PROTOCOL)
        self.factory.reset_mock()
        body = change()
        body["data_scope"] = "owner"
        self.assertEqual(service.handler(event(body), None)["statusCode"], 422)
        body = proposal()
        body.update(data_scope="owner", classification="Secret")
        self.assertEqual(service.handler(event(body, "/v1/proposals/create"), None)["statusCode"], 422)
        self.factory.assert_not_called()


    def test_safe_ambiguous_error_keeps_key(self):
        self.factory.return_value.perform.side_effect = RuntimeError("private SQL and parameters")
        result = service.handler(event(), None)
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(json.loads(result["body"]), {"status": "temporarily_unavailable", "request_key": KEY})

    def test_warming_is_only_reported_for_provider_resume(self):
        error = RuntimeError("private provider detail")
        error.response = {"Error": {"Code": "DatabaseResumingException"}}
        self.factory.return_value.perform.side_effect = error
        result = service.handler(event(), None)
        self.assertEqual(json.loads(result["body"])["status"], "warming")

    def test_approval_routes_require_separate_owner_role(self):
        for path, body in [("/v1/proposals/challenge", {"proposal_id": EPOCH}),
                           ("/v1/proposals/resolve", resolution())]:
            with self.subTest(path=path):
                request = event(body, path)
                request["headers"] = {"userArn": APPROVAL_ACTOR}
                self.assertEqual(service.handler(request, None)["statusCode"], 403)
                self.factory.assert_not_called()
        self.factory.return_value.perform.return_value = ({}, 7)
        for path, body in [("/v1/proposals/challenge", {"proposal_id": EPOCH}),
                           ("/v1/proposals/resolve", resolution())]:
            request = event(body, path)
            request["requestContext"]["authorizer"]["iam"]["userArn"] = APPROVAL_ACTOR
            self.assertEqual(service.handler(request, None)["statusCode"], 200)
            self.factory.return_value.perform.assert_called_with(path, body, APPROVAL_ACTOR,
                                                                 client_protocol=validation.CLIENT_PROTOCOL)

    def test_lookup_allows_either_role_but_ordinary_routes_require_client(self):
        self.factory.return_value.perform.return_value = ({}, 7)
        for actor in (ACTOR, APPROVAL_ACTOR):
            request = event({"request_key": KEY}, "/v1/operations/lookup")
            request["requestContext"]["authorizer"]["iam"]["userArn"] = actor
            self.assertEqual(service.handler(request, None)["statusCode"], 200)
        for path, body in [("/v1/records/change", change()),
                           ("/v1/records/query", {"record_id": RECORD}),
                           ("/v1/proposals/create", proposal()), ("/v1/pages/index", {}),
                           ("/v1/pages/read", {"slug": "example"})]:
            request = event(body, path)
            request["requestContext"]["authorizer"]["iam"]["userArn"] = APPROVAL_ACTOR
            self.factory.reset_mock()
            self.assertEqual(service.handler(request, None)["statusCode"], 403)
            self.factory.assert_not_called()

    def test_missing_or_shared_approval_role_fails_closed(self):
        request = event(resolution(), "/v1/proposals/resolve")
        for approval_role in (None, ROLE):
            config = {**CONFIG, "approval_role_arn": approval_role}
            with patch.object(service, "configuration", return_value=config):
                self.assertEqual(service.handler(request, None)["statusCode"], 403)
        self.factory.assert_not_called()

    def test_large_utf8_proposal_accepts_plain_and_base64_body(self):
        body = proposal()
        body["candidate"]["body_markdown"] = "😀" * (validation.MAX_BODY_BYTES // 4)
        self.factory.return_value.perform.return_value = ({}, 7)
        for encoded in (False, True):
            request = event(body, "/v1/proposals/create")
            if encoded:
                request["body"] = base64.b64encode(request["body"].encode()).decode()
                request["isBase64Encoded"] = True
            self.assertEqual(service.handler(request, None)["statusCode"], 200)
            self.factory.return_value.perform.assert_called_with("/v1/proposals/create", body, ACTOR,
                                                                 client_protocol=validation.CLIENT_PROTOCOL)

    def test_raw_request_size_and_encoding_fail_closed(self):
        for path, maximum in [("/v1/records/change", 8192),
                              ("/v1/proposals/create", validation.MAX_PROPOSAL_REQUEST_BYTES)]:
            for encoded in (False, True):
                request = event(path=path)
                request["body"] = " " * (maximum + 1)
                if encoded:
                    request["body"] = base64.b64encode(request["body"].encode()).decode()
                    request["isBase64Encoded"] = True
                self.assertEqual(service.handler(request, None)["statusCode"], 422)
        for body in ("\ud800", '{"a":1,"a":2}'):
            request = event()
            request["body"] = body
            self.assertEqual(service.handler(request, None)["statusCode"], 422)
        request = event()
        request.update(body="!!!", isBase64Encoded=True)
        self.assertEqual(service.handler(request, None)["statusCode"], 422)
        self.factory.assert_not_called()

    def test_instruction_errors_are_sanitized_and_keep_mutation_key(self):
        for code, status in [("proposal_unavailable", 404), ("page_unavailable", 404),
                             ("proposal_conflict", 409), ("approval_invalid", 409),
                             ("revision_conflict", 409), ("idempotency_conflict", 409)]:
            self.factory.return_value.perform.side_effect = validation.MemoryError(code, status, private="secret")
            request = event(resolution(), "/v1/proposals/resolve")
            request["requestContext"]["authorizer"]["iam"]["userArn"] = APPROVAL_ACTOR
            result = service.handler(request, None)
            self.assertEqual(result["statusCode"], status)
            self.assertEqual(json.loads(result["body"]), {"status": code, "request_key": KEY})

    def test_success_carries_generation_and_validation_detail_names_fields(self):
        self.factory.return_value.perform.return_value = ({"records": []}, 11)
        result = service.handler(event({"query": "ble"}, "/v1/memory/search"), None)
        self.assertEqual(json.loads(result["body"]),
                         {"status": "ready", "data": {"records": []}, "authority_generation": 11})
        self.factory.reset_mock()
        body = {"query": "private-value", "after_id": RECORD}
        result = service.handler(event(body, "/v1/memory/search"), None)
        detail = json.loads(result["body"])["detail"]
        self.assertEqual(result["statusCode"], 422)
        self.assertIn("after_id", detail)
        self.assertNotIn("private-value", detail)
        self.factory.assert_not_called()

    def test_archive_read_rejects_non_boolean_opt_in(self):
        for value in (None, 0, 1, "true", [], {}):
            self.factory.reset_mock()
            result = service.handler(event({"record_id": RECORD, "include_archived": value}, "/v1/memory/read"), None)
            self.assertEqual(result["statusCode"], 422)
            self.factory.assert_not_called()

    def test_archived_project_context_is_a_conflict(self):
        with self.assertRaises(validation.MemoryError) as error:
            store.route_result({"status": "project_archived"})
        self.assertEqual((error.exception.status, error.exception.http_status), ("project_archived", 409))

    def test_new_memory_routes_are_client_role_only(self):
        self.factory.return_value.perform.return_value = ({"revisions": {}}, 3)
        for path, body in [("/v1/memory/project_context", {"project_id": RECORD, "session_id": KEY}),
                           ("/v1/memory/revisions", {"record_ids": [RECORD]})]:
            with self.subTest(path=path):
                self.assertEqual(service.handler(event(body, path), None)["statusCode"], 200)
                request = event(body, path)
                request["requestContext"]["authorizer"]["iam"]["userArn"] = APPROVAL_ACTOR
                self.assertEqual(service.handler(request, None)["statusCode"], 403)

    def test_container_reuses_sdk_clients(self):
        import boto3
        service._CLIENTS.clear()
        self.addCleanup(service._CLIENTS.clear)
        with patch.object(boto3, "client") as client:
            first, second = REAL_MAKE_STORE(CONFIG, None), REAL_MAKE_STORE(CONFIG, None)
        self.assertEqual(sorted(call.args[0] for call in client.call_args_list), ["rds-data", "s3"])
        self.assertIs(first.db.client, second.db.client)
        self.assertIs(first.s3, second.s3)
        self.assertIs(first.cache, service.CONTROL_CACHE)


def wire_rows(document):
    raw = json.dumps(document).encode()
    checksum = hashlib.sha256(raw).hexdigest()
    return {"records": [[{"longValue": offset + 1},
                         {"stringValue": base64.b64encode(raw[offset:offset + store.JSON_CHUNK_BYTES]).decode("ascii")},
                         {"longValue": len(raw)}, {"stringValue": checksum}]
                        for offset in range(0, len(raw), store.JSON_CHUNK_BYTES)]}


def envelope(result, *, state="ready", generation=5):
    return {"service_state": store.control_document(CONFIG, state), "authority_generation": generation,
            "result": result}


class DataAPIContractTests(unittest.TestCase):
    def setUp(self):
        self.data = Mock()
        self.data.begin_transaction.return_value = {"transactionId": "tx"}
        self.s3 = Mock()
        self.s3.get_object.side_effect = lambda **_: control_response()
        self.cache = store.ControlCache()
        self.storage = store.Store(CONFIG, self.data, self.s3, control_cache=self.cache)

    def script_result(self, result):
        """Legacy /v1/records/* and lookup path: explicit transaction, unchanged statements."""
        self.data.execute_statement.side_effect = [
            {}, scalar(envelope(None)), {}, {"records": [[{"booleanValue": True}]]}, scalar(result)]

    def dispatch(self, result, **kwargs):
        self.data.execute_statement.side_effect = [wire_rows(envelope(result, **kwargs))]

    def parameters(self):
        return {item["name"]: item["value"].get("stringValue", item["value"].get("longValue"))
                for item in self.data.execute_statement.call_args.kwargs["parameters"]}

    def test_owner_scope_and_candidate_hash_cross_database_boundary_unchanged(self):
        request = proposal()
        request["data_scope"] = "owner"
        request["candidate"]["page_kind"] = "skill"
        request["candidate"]["bundle"] = {
            "version": "1.0.0", "sha256": "a" * 64, "size_bytes": 123,
            "manifest_sha256": "b" * 64, "load_policy": "always",
        }
        self.dispatch({"proposal_id": EPOCH, "state": "pending"})
        self.assertEqual(self.storage.perform("/v1/proposals/create", request, ACTOR),
                         ({"proposal_id": EPOCH, "state": "pending"}, 5))
        parameters = self.parameters()
        self.assertIn("maven.memory_request(", self.data.execute_statement.call_args.kwargs["sql"])
        self.assertEqual(parameters["route"], "/v1/proposals/create")
        self.assertEqual(json.loads(parameters["request"]), request)
        self.assertEqual(parameters["candidate_hash"], validation.digest(request["candidate"]))
        self.assertEqual(parameters["request_hash"], validation.digest(request))
        self.assertEqual(parameters["protocol"], 1)

    def test_read_is_one_data_api_call_and_reuses_control_briefly(self):
        # Before migration 013 a read was begin, set_config, lock_service_state, set_config,
        # memory_protocol_allowed, route statement and commit: seven Data API calls.
        for _ in range(2):
            self.dispatch({"records": [], "next_after_id": None, "next_offset": None}, generation=9)
            result, generation = self.storage.perform("/v1/memory/search", {"query": "ble"}, ACTOR,
                                                      client_protocol=4)
            self.assertEqual(generation, 9)
        self.assertEqual(self.data.execute_statement.call_count, 2)
        self.data.begin_transaction.assert_not_called()
        self.data.commit_transaction.assert_not_called()
        self.assertNotIn("transactionId", self.data.execute_statement.call_args.kwargs)
        self.assertEqual(self.s3.get_object.call_count, 1)
        self.assertIsNone(self.parameters()["request_hash"])
        self.cache.fetched_at -= store.CONTROL_TTL_SECONDS + 1
        self.dispatch({"records": []})
        self.storage.perform("/v1/memory/search", {}, ACTOR)
        self.assertEqual(self.s3.get_object.call_count, 2)

    def test_mutation_is_one_statement_inside_rollbackable_transaction(self):
        self.cache.put(CONFIG, store.control_document(CONFIG, "ready"))
        self.dispatch({"record_id": RECORD, "revision": 1, "request_key": KEY, "replayed": False})
        result, generation = self.storage.perform("/v1/memory/write", {"request_key": KEY}, ACTOR)
        self.assertEqual((result["revision"], generation), (1, 5))
        self.assertEqual(self.data.execute_statement.call_count, 1)
        self.assertEqual(self.data.execute_statement.call_args.kwargs["transactionId"], "tx")
        self.data.begin_transaction.assert_called_once()
        self.data.commit_transaction.assert_called_once()
        # Mutations never trust the cache: control is fetched before and after the statement.
        self.assertEqual(self.s3.get_object.call_count, 2)
        self.dispatch({"record_id": RECORD, "revision": 1})
        self.s3.get_object.side_effect = [control_response(), control_response("maintenance")]
        with self.assertRaises(validation.MemoryError) as error:
            self.storage.perform("/v1/memory/write", {"request_key": KEY}, ACTOR)
        self.assertEqual(error.exception.status, "context_stale")
        self.assertEqual(self.data.commit_transaction.call_count, 1)
        self.data.rollback_transaction.assert_called_once()
        self.assertIsNone(self.cache.get(CONFIG))

    def test_database_state_mismatch_is_stale_and_clears_cache(self):
        for document in [envelope({"records": []}, state="maintenance"),
                         {**envelope({"records": []}), "service_state": {
                             **store.control_document(CONFIG, "ready"), "recovery_epoch": RECORD}},
                         {**envelope({"records": []}), "service_state": None}]:
            self.data.execute_statement.side_effect = [wire_rows(document)]
            with self.subTest(document=document["service_state"]), self.assertRaises(validation.MemoryError) as error:
                self.storage.perform("/v1/memory/search", {}, ACTOR)
            self.assertEqual(error.exception.status, "context_stale")
            self.assertIsNone(self.cache.get(CONFIG))

    def test_route_statuses_map_exactly_and_unknown_is_ambiguous(self):
        for result, status, http in [({"status": "client_incompatible"}, "client_incompatible", 409),
                                     ({"status": "record_unavailable"}, "record_unavailable", 404),
                                     ({"status": "invalid_request"}, "invalid_request", 422),
                                     ({"status": "surprise"}, "temporarily_unavailable", 503),
                                     (None, "temporarily_unavailable", 503)]:
            self.dispatch(result)
            with self.subTest(result=result), self.assertRaises(validation.MemoryError) as error:
                self.storage.perform("/v1/memory/read", {"record_id": RECORD}, ACTOR)
            self.assertEqual((error.exception.status, error.exception.http_status), (status, http))
        self.dispatch({"status": "revision_conflict", "current_revision": 3})
        with self.assertRaises(validation.MemoryError) as error:
            self.storage.perform("/v1/memory/write", {"request_key": KEY}, ACTOR)
        self.assertEqual(error.exception.metadata, {"current_revision": 3})
        self.data.commit_transaction.assert_not_called()

    def test_legacy_routes_keep_explicit_transaction_and_report_generation(self):
        self.script_result({"record_id": RECORD, "revision": 1, "request_key": KEY, "replayed": False})
        result, generation = self.storage.perform("/v1/records/change", change(), ACTOR)
        self.assertEqual((result["revision"], generation), (1, 5))
        self.assertIn("maven.memory_service_state()",
                      self.data.execute_statement.call_args_list[1].kwargs["sql"])
        self.data.commit_transaction.assert_called_once()
        # The pre-013 per-request sequence, still used only by /v1/records/* and lookup: 7 Data API calls.
        calls = (self.data.begin_transaction.call_count + self.data.execute_statement.call_count
                 + self.data.commit_transaction.call_count)
        self.assertEqual(calls, 7)

    def test_legacy_rollout_is_admin_only_and_unknown_modes_fail_closed(self):
        legacy = {**store.control_document(CONFIG, "ready"), "data_scope": "synthetic"}
        with self.assertRaises(validation.MemoryError):
            store.parse_control(json.dumps(legacy), CONFIG)
        self.assertEqual(store.parse_control(json.dumps(legacy), CONFIG, allow_legacy=True), legacy)
        for mode in ("owner", "personal", "anything"):
            document = {**legacy, "data_scope": mode}
            with self.subTest(mode=mode), self.assertRaises(validation.MemoryError):
                store.parse_control(json.dumps(document), CONFIG, allow_legacy=True)


    def test_maintenance_before_commit_rolls_back(self):
        self.script_result({"record_id": RECORD, "revision": 1, "request_key": KEY, "replayed": False})
        self.s3.get_object.side_effect = [control_response(), control_response("maintenance")]
        with self.assertRaises(validation.MemoryError) as error:
            self.storage.perform("/v1/records/change", change(), ACTOR)
        self.assertEqual(error.exception.status, "context_stale")
        self.data.commit_transaction.assert_not_called()
        self.data.rollback_transaction.assert_called_once()

    def test_initial_maintenance_or_epoch_mismatch_never_wakes_database(self):
        for document in [store.control_document(CONFIG, "maintenance"),
                         {**store.control_document(CONFIG, "ready"), "recovery_epoch": RECORD}]:
            self.s3.get_object.side_effect = None
            self.s3.get_object.return_value = {"Body": BytesIO(json.dumps(document).encode())}
            with self.assertRaises(validation.MemoryError):
                self.storage.perform("/v1/records/query", {"record_id": RECORD}, ACTOR)
        self.data.begin_transaction.assert_not_called()


    def test_revision_conflict_and_ambiguous_commit_do_not_retry(self):
        self.script_result({"status": "revision_conflict", "current_revision": 2})
        with self.assertRaises(validation.MemoryError) as error:
            self.storage.perform("/v1/records/change", change(), ACTOR)
        self.assertEqual(error.exception.metadata, {"current_revision": 2})
        self.data.commit_transaction.assert_not_called()
        self.script_result({"record_id": RECORD, "revision": 1, "request_key": KEY, "replayed": False})
        self.data.commit_transaction.side_effect = RuntimeError("lost response")
        with self.assertRaises(RuntimeError):
            self.storage.perform("/v1/records/change", change(), ACTOR)
        self.assertEqual(self.data.commit_transaction.call_count, 1)


class AdminContractTests(unittest.TestCase):

    def test_error_context_logging_blocks_bootstrap_before_credential_use(self):
        settings = {"statement": "none", "error_statement": "panic",
                    "parameters": "0", "error_parameters": "0", "audit": "none"}
        session = Mock()
        session.client.return_value.get_caller_identity.return_value = {
            "Arn": "arn:aws:sts::123456789012:assumed-role/memory-admin/test"}
        for verbosity in ("default", "verbose"):
            with self.subTest(verbosity=verbosity):
                db = Mock()
                db.scalar.return_value = json.dumps({**settings, "error_verbosity": verbosity})
                with patch.object(admin, "DataAPI", return_value=db):
                    with self.assertRaises(validation.MemoryError) as error:
                        admin.bootstrap(CONFIG, session)
                self.assertEqual(error.exception.status, "bootstrap_refused")
                session.client.return_value.get_secret_value.assert_not_called()
                session.client.return_value.put_object.assert_not_called()

    def test_exact_repeat_does_not_fetch_or_rotate_password(self):
        db = Mock()
        db.transaction.return_value.__enter__ = Mock(return_value="tx")
        db.transaction.return_value.__exit__ = Mock(return_value=False)
        hashes = {name: sha for name, sha, _ in admin.migrations()}
        state = {**store.control_document(CONFIG, "maintenance"),
                 "app_secret_arn": CONFIG["app_secret_arn"], "app_secret_version": "v1"}
        db.scalar.side_effect = [True, True, json.dumps(hashes), json.dumps(state)]
        secrets = Mock()
        admin._install(db, CONFIG, secrets, store.control_document(CONFIG, "maintenance"))
        secrets.get_secret_value.assert_not_called()
        db.execute.assert_called_once()

    def test_ordered_four_migration_upgrade_accepts_legacy_scope_without_password(self):
        db = Mock()
        db.transaction.return_value.__enter__ = Mock(return_value="tx")
        db.transaction.return_value.__exit__ = Mock(return_value=False)
        bundle = admin.migrations()
        installed = {name: sha for name, sha, _ in bundle[:4]}
        state = {**store.control_document(CONFIG, "ready"), "data_scope": "synthetic", "schema_version": 1,
                 "app_secret_arn": CONFIG["app_secret_arn"], "app_secret_version": "v1"}
        db.scalar.side_effect = [True, True, json.dumps(installed), json.dumps(state)]
        secrets = Mock()
        admin._install(db, CONFIG, secrets, store.control_document(CONFIG, "maintenance"), upgrade=True)
        secrets.get_secret_value.assert_not_called()
        registrations = []
        calls = db.execute.call_args_list
        for index, call in enumerate(calls):
            if call.args[0].startswith("INSERT INTO maven.schema_migrations"):
                name, sha, statements = bundle[4 + len(registrations)]
                self.assertEqual(call.args[1], {"name": name, "sha": sha})
                self.assertEqual([item.args[0] for item in calls[index - len(statements):index]], statements)
                registrations.append(name)
        self.assertEqual(registrations, [name for name, _, _ in bundle[4:]])

    def test_legacy_database_is_not_accepted_as_a_complete_bundle(self):
        db = Mock()
        db.transaction.return_value.__enter__ = Mock(return_value="tx")
        db.transaction.return_value.__exit__ = Mock(return_value=False)
        state = {**store.control_document(CONFIG, "ready"), "data_scope": "synthetic",
                 "app_secret_arn": CONFIG["app_secret_arn"], "app_secret_version": "v1"}
        db.scalar.side_effect = [
            True, True, json.dumps({name: sha for name, sha, _ in admin.migrations()}), json.dumps(state)]
        with self.assertRaises(validation.MemoryError):
            admin._install(db, CONFIG, Mock(), store.control_document(CONFIG, "maintenance"), upgrade=True)


    def test_migration_drift_is_refused(self):
        db = Mock()
        db.transaction.return_value.__enter__ = Mock(return_value="tx")
        db.transaction.return_value.__exit__ = Mock(return_value=False)
        db.scalar.side_effect = [True, True, json.dumps({"001_foundation.sql": "wrong"})]
        secrets = Mock()
        with self.assertRaises(validation.MemoryError) as error:
            admin._install(db, CONFIG, secrets, store.control_document(CONFIG, "maintenance"))
        self.assertEqual(error.exception.status, "bootstrap_refused")
        secrets.get_secret_value.assert_not_called()

    def test_upgrade_replaces_legacy_ready_or_maintenance_control_before_install(self):
        for initial_state in ("ready", "maintenance"):
            with self.subTest(initial_state=initial_state):
                legacy = {**store.control_document(CONFIG, initial_state), "data_scope": "synthetic"}
                session = Mock()
                session.client.return_value.get_caller_identity.return_value = {
                    "Arn": "arn:aws:sts::123456789012:assumed-role/memory-admin/test"}
                s3 = session.client.return_value
                s3.get_object.side_effect = [
                    control_response("maintenance"), control_response("maintenance"), control_response()]
                db = Mock()
                db.transaction.return_value.__enter__ = Mock(return_value="tx")
                db.transaction.return_value.__exit__ = Mock(return_value=False)
                db.scalar.return_value = json.dumps({
                    **store.control_document(CONFIG, "maintenance"),
                    "app_secret_arn": CONFIG["app_secret_arn"], "app_secret_version": "v1"})

                def install(database, config, secrets, control, *, upgrade):
                    self.assertTrue(upgrade)
                    self.assertEqual(control, store.control_document(CONFIG, "maintenance"))
                    publication = s3.put_object.call_args.kwargs
                    self.assertEqual(json.loads(publication["Body"]), control)
                    self.assertEqual(publication["IfMatch"], '"legacy"')

                with patch.object(admin, "DataAPI", return_value=db), \
                        patch.object(admin, "_check_logging"), \
                        patch.object(admin, "_control", return_value=(legacy, '"legacy"')), \
                        patch.object(admin, "_install", side_effect=install) as installer, \
                        patch.object(admin, "_verify_application"):
                    result = admin.migrate(CONFIG, session)
                installer.assert_called_once()
                self.assertEqual(result["data_scope"], store.ROLLOUT_SCOPE)
                self.assertEqual(json.loads(s3.put_object.call_args.kwargs["Body"]),
                                 store.control_document(CONFIG, "ready"))
                s3.get_secret_value.assert_not_called()

    def test_control_publication_requires_an_explicit_precondition(self):
        s3 = Mock()
        with self.assertRaises(validation.MemoryError):
            admin._put_control(s3, CONFIG, "ready")
        s3.put_object.assert_not_called()

    def test_control_initializes_without_listing_and_preserves_existing_state(self):
        from botocore.exceptions import ClientError

        body = None
        s3 = Mock()

        def get_object(**kwargs):
            if body is None:
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
            return {"Body": BytesIO(body), "ETag": '"version"'}

        def put_object(**kwargs):
            nonlocal body
            if body is not None:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            body = kwargs["Body"]

        s3.get_object.side_effect = get_object
        s3.put_object.side_effect = put_object
        initialized, _ = admin._control(s3, CONFIG)
        self.assertEqual(initialized, store.control_document(CONFIG, "maintenance"))
        body = validation.canonical(store.control_document(CONFIG, "ready")).encode()
        existing, _ = admin._control(s3, CONFIG)
        self.assertEqual(existing, store.control_document(CONFIG, "ready"))


if __name__ == "__main__":
    unittest.main()
