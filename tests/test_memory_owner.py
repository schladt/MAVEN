"""Owner-memory contracts and opt-in real PostgreSQL transaction regressions.

Set MAVEN_MEMORY_TEST_DSN to a disposable PostgreSQL database with NO maven
schema. The SQL test creates its schema inside a transaction and rolls back;
it refuses an existing schema. No AWS calls, SQL emulation, or production data.
"""

import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from tools.memory import admin, service, store, validation


def record(kind="note", data=None):
    return {"request_key": str(uuid4()), "record_id": str(uuid4()), "expected_revision": 0,
            "kind": kind, "classification": "Sensitive", "data_scope": "owner",
            "data": data if data is not None else {"title": "Fixture", "body_markdown": "Exact source\n", "category": "reference"},
            "source": {"origin": "fixture/source:0-13", "excerpt": "Exact source\n"},
            "change_reason": "Import exact fixture source", "tags": ["fixture"]}


class OwnerValidationTests(unittest.TestCase):
    def test_every_structured_kind_accepts_sourced_owner_data(self):
        entity = str(uuid4())
        session = str(uuid4())
        cases = {
            "entity": {"entity_type": "person", "name": "Fixture person", "aliases": ["Fixture"]},
            "fact": {"subject_id": entity, "predicate": "preference", "value": {"text": "fixture"}},
            "relationship": {"subject_id": entity, "predicate": "knows", "object_id": str(uuid4())},
            "task": {"title": "Fixture", "description": "", "state": "open"},
            "decision": {"title": "Fixture", "body_markdown": "Rationale", "state": "accepted"},
            "risk": {"title": "Fixture", "body_markdown": "Evidence", "state": "open"},
            "note": {"title": "Fixture", "body_markdown": "Exact source\n", "category": "reference"},
            "session": {"state": "active", "summary": "", "workflow": "work"},
            "operation": {"session_id": session, "name": "refresh", "state": "started", "summary": ""},
            "setting": {"key": "fixture.region", "value": "region", "scope": "global"},
            "checkpoint": {"session_id": session, "candidate_digest": "a" * 64, "git_base": "b" * 40,
                           "memory_refs": [{"record_id": entity, "revision": 1}], "state": "prepared"},
        }
        for kind, data in cases.items():
            with self.subTest(kind=kind):
                body = record(kind, data)
                before = copy.deepcopy(body)
                self.assertIs(validation.validate("/v1/memory/write", body), body)
                self.assertEqual(body, before)

    def test_note_is_lossless_bounded_and_cannot_become_authority(self):
        body = record()
        body["data"]["body_markdown"] = "é" * 65536
        validation.validate("/v1/memory/write", body)
        body["data"]["body_markdown"] += "é"
        with self.assertRaises(validation.MemoryError):
            validation.validate("/v1/memory/write", body)
        for extra in ("authority_state", "operative", "requires"):
            body = record()
            body["data"][extra] = "active"
            with self.assertRaises(validation.MemoryError):
                validation.validate("/v1/memory/write", body)

    def test_bad_enums_and_types_have_bounded_invalid_request(self):
        for field in ("kind", "data_scope"):
            for bad in ([], {}, None, 2):
                body = record()
                body[field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(validation.MemoryError) as error:
                    validation.validate("/v1/memory/write", body)
                self.assertEqual(error.exception.status, "invalid_request")
        for bad in ([], {}, None, True, "not-a-state"):
            body = record("session", {"state": bad, "summary": ""})
            with self.assertRaises(validation.MemoryError):
                validation.validate("/v1/memory/write", body)

    def test_secret_values_rejected_and_safe_references_allowed(self):
        body = record("setting", {"key": "fixture.secret-location", "scope": "global", "value": None,
                                  "secret_ref": {"provider": "ssm", "name": "/fixture/credential"}})
        validation.validate("/v1/memory/write", body)
        for forbidden in ({"password": "fixture"}, {"access_token": "fixture"},
                          "-----BEGIN " + "PRIVATE KEY-----", "AKIA" + "X" * 16,
                          "Bearer " + "x" * 32):
            body["data"]["value"] = forbidden
            with self.assertRaises(validation.MemoryError) as error:
                validation.validate("/v1/memory/write", body)
            self.assertEqual(str(error.exception), "invalid_request")
            self.assertEqual(error.exception.metadata, {})

    def test_settings_checkpoint_and_claim_require_exact_scope_and_fence(self):
        body = record("setting", {"key": "fixture", "value": False, "scope": "project"})
        with self.assertRaises(validation.MemoryError):
            validation.validate("/v1/memory/write", body)
        body["project_id"] = str(uuid4())
        validation.validate("/v1/memory/write", body)
        claim = {"request_key": str(uuid4()), "record_id": str(uuid4()), "expected_revision": 1,
                 "session_id": str(uuid4()), "action": "claim", "lease_seconds": 30,
                 "source": {"origin": "fixture", "excerpt": ""}, "change_reason": "Claim fixture"}
        validation.validate("/v1/memory/claim", claim)
        claim["action"] = "complete"
        with self.assertRaises(validation.MemoryError):
            validation.validate("/v1/memory/claim", claim)
        del claim["lease_seconds"]
        claim["fence"] = 1
        validation.validate("/v1/memory/claim", claim)
        claim["fence"] = True
        with self.assertRaises(validation.MemoryError):
            validation.validate("/v1/memory/claim", claim)

    def test_named_context_root_requires_a_valid_skill_slug(self):
        request = {"skill_slug": "fixture-skill", "workflow": "different-workflow", "operation": "start-work"}
        validation.validate("/v1/memory/context", request)
        for value in ("", "Fixture Skill", "../skill", "x" * 81, [], None, True):
            with self.subTest(value=value), self.assertRaises(validation.MemoryError) as error:
                validation.validate("/v1/memory/context", {**request, "skill_slug": value})
            self.assertEqual(error.exception.status, "invalid_request")

    def test_directives_and_dependencies_preserve_legacy_candidate_hashes(self):
        body = {"request_key": str(uuid4()), "proposal_id": str(uuid4()), "page_id": str(uuid4()),
                "expected_revision": 0, "classification": "Sensitive", "data_scope": "owner",
                "candidate": {"slug": "fixture", "page_kind": "directive", "title": "Fixture",
                              "body_markdown": "Exact body", "scope": {}, "authority_state": "active"},
                "source": {"origin": "fixture", "excerpt": ""}, "change_reason": "Fixture"}
        original_hash = validation.digest(body["candidate"])
        validation.validate("/v1/proposals/create", body)
        self.assertEqual(validation.digest(body["candidate"]), original_hash)
        self.assertNotIn("requires", body["candidate"])
        body["candidate"]["requires"] = ["fixture-helper"]
        validation.validate("/v1/proposals/create", body)
        self.assertNotEqual(validation.digest(body["candidate"]), original_hash)
        body["candidate"]["requires"] *= 2
        with self.assertRaises(validation.MemoryError):
            validation.validate("/v1/proposals/create", body)

    def test_handler_protocol_is_transport_metadata_not_record_data(self):
        config = {"client_role_arn": "arn:aws:iam::123456789012:role/fixture-client",
                  "approval_role_arn": "arn:aws:iam::123456789012:role/fixture-approval"}
        body = record()
        event = {"rawPath": "/v1/memory/write", "body": json.dumps(body),
                 "headers": {"x-maven-memory-protocol": "2"},
                 "requestContext": {"http": {"method": "POST"}, "authorizer": {"iam": {
                     "userArn": "arn:aws:sts::123456789012:assumed-role/fixture-client/test"}}}}
        storage = Mock()
        storage.perform.return_value = {"record_id": body["record_id"], "revision": 1}
        with patch.object(service, "configuration", return_value=config), patch.object(service, "make_store", return_value=storage):
            result = service.handler(event, None)
            self.assertEqual(result["statusCode"], 200)
            self.assertEqual(storage.perform.call_args.kwargs, {"client_protocol": 2})
            event["headers"]["x-maven-memory-protocol"] = "3"
            self.assertEqual(service.handler(event, None)["statusCode"], 409)
            self.assertEqual(storage.perform.call_count, 1)


class JsonResultTransportTests(unittest.TestCase):
    @staticmethod
    def wire_rows(document):
        checksum = hashlib.sha256(document).hexdigest()
        return [[{"longValue": offset + 1},
                 {"stringValue": base64.b64encode(document[offset:offset + store.JSON_CHUNK_BYTES]).decode("ascii")},
                 {"longValue": len(document)}, {"stringValue": checksum}]
                for offset in range(0, len(document), store.JSON_CHUNK_BYTES)]

    def adapter(self, rows):
        client = Mock()
        client.execute_statement.return_value = {"records": rows}
        return store.DataAPI(client, {"cluster_arn": "fixture", "app_secret_arn": "fixture", "database": "fixture"}), client

    def test_large_utf8_json_reassembled_from_small_rows_in_one_request(self):
        value = {"data": {"body_markdown": "é" * 65536}, "source": {"origin": "fixture", "excerpt": "Exact source"}}
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        rows = self.wire_rows(raw)
        self.assertGreater(len(raw), 65536)
        self.assertTrue(all(len(json.dumps(row).encode("ascii")) < 8192 for row in rows))
        database, client = self.adapter(rows)
        result = database.json_scalar("CAST(:value AS jsonb)", {"value": raw.decode()}, "fixture-tx")
        self.assertEqual(json.loads(result), value)
        client.execute_statement.assert_called_once()
        self.assertEqual(client.execute_statement.call_args.kwargs["transactionId"], "fixture-tx")

    def test_maximum_document_stays_below_total_data_api_response_limit(self):
        raw = b'"' + b"x" * (store.MAX_JSON_RESULT_BYTES - 2) + b'"'
        rows = self.wire_rows(raw)
        self.assertLess(len(json.dumps({"records": rows}).encode("ascii")), 1048576)
        database, _ = self.adapter(rows)
        self.assertEqual(database.json_scalar("'fixture'::jsonb").encode(), raw)

    def test_missing_reordered_corrupt_and_inconsistent_rows_fail_closed(self):
        original = self.wire_rows(json.dumps({"body": "é" * 9000}, ensure_ascii=False).encode())
        variants = [original[:-1], list(reversed(original)), original + original[-1:]]
        for column, replacement in [(0, {"longValue": 7}), (1, {"stringValue": "!"}),
                                    (2, {"longValue": 1}), (3, {"stringValue": "0" * 64})]:
            rows = copy.deepcopy(original)
            rows[1][column] = replacement
            variants.append(rows)
        variants.append(self.wire_rows(b'{"body":"\xff"}'))
        for rows in variants:
            database, _ = self.adapter(rows)
            with self.subTest(rows=len(rows)), self.assertRaises(validation.MemoryError) as error:
                database.json_scalar("'fixture'::jsonb")
            self.assertEqual(error.exception.status, "temporarily_unavailable")
            self.assertEqual(error.exception.metadata, {})

    def test_oversized_authority_context_fails_without_partial_results(self):
        database, _ = self.adapter([[{"longValue": 1}, {"isNull": True},
                                    {"longValue": store.MAX_JSON_RESULT_BYTES + 1},
                                    {"stringValue": "a" * 64}]])
        with self.assertRaises(validation.MemoryError) as error:
            database.json_scalar("'fixture'::jsonb", overflow_status="context_incomplete")
        self.assertEqual(error.exception.status, "context_incomplete")
        self.assertEqual(error.exception.http_status, 409)


SQL_HELPERS = r"""
CREATE FUNCTION pg_temp.assert_equal(actual jsonb, expected jsonb, label text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'fixture assertion: %',label; END IF; END $$;
CREATE FUNCTION pg_temp.write(request jsonb, action text DEFAULT 'write') RETURNS jsonb LANGUAGE sql AS $$
 SELECT maven.memory_write(action,request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),'fixture-owner')
$$;
CREATE FUNCTION pg_temp.request(kind text,data jsonb) RETURNS jsonb LANGUAGE sql AS $$
 SELECT jsonb_build_object('request_key',gen_random_uuid(),'record_id',gen_random_uuid(),'expected_revision',0,
   'kind',kind,'classification','Sensitive','data_scope','owner','data',data,
   'source',jsonb_build_object('origin','fixture/source','excerpt','exact fixture source'),'change_reason','Fixture write')
$$;
CREATE FUNCTION pg_temp.publish(candidate jsonb,scope_value text DEFAULT 'owner') RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE request jsonb; result jsonb; challenge jsonb; proposal uuid:=gen_random_uuid(); page uuid:=gen_random_uuid(); hash text;
BEGIN
 hash:=encode(sha256(convert_to(candidate::text,'UTF8')),'hex');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',proposal,'page_id',page,'expected_revision',0,
   'classification','Sensitive','data_scope',scope_value,'candidate',candidate,
   'source',jsonb_build_object('origin','fixture','excerpt','exact fixture'),'change_reason','Fixture publication');
 result:=maven.instruction_write('create',request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),hash,'fixture-owner');
 PERFORM pg_temp.assert_equal(result->'status',NULL,'proposal created');
 challenge:=maven.instruction_challenge(proposal,'fixture-approver');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',proposal,'challenge_id',challenge->'challenge_id',
   'candidate_hash',hash,'expected_revision',0,'approval_source',jsonb_build_object('origin','fixture approval','excerpt','Approve exact fixture'));
 result:=maven.instruction_write('resolve',request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),NULL,'fixture-approver');
 PERFORM pg_temp.assert_equal(result->'revision','1','proposal approved');
 RETURN page;
END $$;
"""

SQL_LEGACY = r"""
INSERT INTO maven.service_state(singleton,instance_id,recovery_epoch,schema_version,data_scope,state,app_secret_arn,app_secret_version)
 VALUES(true,gen_random_uuid(),gen_random_uuid(),1,'synthetic_and_owner_instructions','ready','fixture-app','fixture-version');
DO $$
DECLARE request jsonb; candidate jsonb; result jsonb; challenge jsonb; hash text; proposal uuid:=gen_random_uuid();
BEGIN
 request:=jsonb_build_object('request_key',gen_random_uuid(),'record_id',gen_random_uuid(),'action','create_fact',
   'classification','Sensitive','data_scope','synthetic','fact',jsonb_build_object('predicate','legacy','value_type','string','value','Synthetic historical fixture'),
   'source',jsonb_build_object('origin','fixture legacy source','excerpt','exact old evidence'),'change_reason','Historical fixture');
 result:=maven.change_fact(request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),'fixture-actor');
 IF result->>'revision'<>'1' THEN RAISE EXCEPTION 'legacy fixture rejected'; END IF;
 candidate:=jsonb_build_object('slug','fixture-legacy-skill','page_kind','skill','title','Legacy owner skill',
   'body_markdown','Exact original owner skill body','scope','{}'::jsonb,'authority_state','active');
 hash:=encode(sha256(convert_to(candidate::text,'UTF8')),'hex');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',proposal,'page_id',gen_random_uuid(),'expected_revision',0,
   'classification','Sensitive','data_scope','owner','candidate',candidate,
   'source',jsonb_build_object('origin','fixture original skill','excerpt','exact source'),'change_reason','Legacy publication');
 result:=maven.instruction_write('create',request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),hash,'fixture-owner');
 IF result ? 'status' THEN RAISE EXCEPTION 'legacy proposal rejected'; END IF;
 challenge:=maven.instruction_challenge(proposal,'fixture-approver');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',proposal,'challenge_id',challenge->'challenge_id',
   'candidate_hash',hash,'expected_revision',0,'approval_source',jsonb_build_object('origin','fixture approval','excerpt','Approve exact fixture'));
 result:=maven.instruction_write('resolve',request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),NULL,'fixture-approver');
 IF result->>'revision'<>'1' THEN RAISE EXCEPTION 'legacy approval rejected'; END IF;
END $$;
CREATE TEMP TABLE fixture_legacy_versions AS SELECT record_id,revision,snapshot,content_hash FROM maven.record_versions;
CREATE TEMP TABLE fixture_legacy_page AS SELECT maven.instruction_read('{"slug":"fixture-legacy-skill"}') AS snapshot;
"""

SQL_PENDING_SKILL_BUNDLE = r"""
CREATE TEMP TABLE fixture_pending_bundle(proposal uuid, page uuid, hash text);
DO $$
DECLARE request jsonb; candidate jsonb; result jsonb;
BEGIN
 candidate:=jsonb_build_object('slug','invalid-historical-bundle','page_kind','skill','title','Pending fixture',
   'body_markdown','Exact pending fixture','scope','{}'::jsonb,'authority_state','active','bundle','{}'::jsonb);
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',gen_random_uuid(),'page_id',gen_random_uuid(),
   'expected_revision',0,'classification','Sensitive','data_scope','owner','candidate',candidate,
   'source',jsonb_build_object('origin','fixture','excerpt','exact fixture'),'change_reason','Pending fixture');
 result:=maven.instruction_write('create',request,repeat('a',64),repeat('b',64),'fixture-owner');
 IF result ? 'status' THEN RAISE EXCEPTION 'pre-migration pending fixture failed'; END IF;
 INSERT INTO fixture_pending_bundle VALUES((request->>'proposal_id')::uuid,(request->>'page_id')::uuid,repeat('b',64));
END $$;
"""

SQL_SKILL_BUNDLES = r"""
DO $$
DECLARE candidate jsonb; bundle jsonb; bad jsonb; request jsonb; result jsonb; challenge jsonb;
  pending record; proposal uuid:=gen_random_uuid(); project uuid:=gen_random_uuid(); hash text;
BEGIN
 bundle:=jsonb_build_object('version','1.0.0','sha256',repeat('a',64),'size_bytes',123,
   'manifest_sha256',repeat('b',64),'load_policy','on-demand');
 candidate:=jsonb_build_object('slug','bundle-skill','page_kind','skill','title','Bundle fixture',
   'body_markdown','Exact approved bundle fixture','scope',jsonb_build_object('project',project),'authority_state','active','bundle',bundle);
 FOR bad IN SELECT value FROM jsonb_array_elements(jsonb_build_array(
   'null'::jsonb,'{}'::jsonb,bundle-'version',bundle || '{"version":"../x"}',
   bundle || '{"sha256":"bad"}',bundle || '{"manifest_sha256":"bad"}',
   bundle || '{"size_bytes":true}',bundle || '{"size_bytes":1.5}',bundle || '{"size_bytes":0}',
   bundle || '{"size_bytes":268435457}',bundle || '{"load_policy":"unknown"}',
   bundle || '{"bucket":"not-allowed"}',bundle || '{"key":"not-allowed"}',bundle || '{"url":"not-allowed"}'
 )) LOOP
   request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',gen_random_uuid(),'page_id',gen_random_uuid(),
     'expected_revision',0,'classification','Sensitive','data_scope','owner','candidate',candidate || jsonb_build_object('bundle',bad),
     'source',jsonb_build_object('origin','fixture','excerpt','exact fixture'),'change_reason','Invalid fixture');
   BEGIN
     PERFORM maven.instruction_write('create',request,repeat('a',64),repeat('b',64),'fixture-owner');
     RAISE EXCEPTION 'invalid bundle accepted';
   EXCEPTION WHEN invalid_parameter_value THEN NULL;
   END;
   IF EXISTS (SELECT 1 FROM maven.instruction_proposals WHERE id=(request->>'proposal_id')::uuid) THEN
     RAISE EXCEPTION 'invalid bundle persisted';
   END IF;
 END LOOP;
 BEGIN
   PERFORM pg_temp.publish(candidate || '{"page_kind":"procedure"}');
   RAISE EXCEPTION 'procedure bundle accepted';
 EXCEPTION WHEN invalid_parameter_value THEN NULL;
 END;
 BEGIN
   PERFORM pg_temp.publish(candidate || '{"page_kind":"directive"}');
   RAISE EXCEPTION 'directive bundle accepted';
 EXCEPTION WHEN invalid_parameter_value THEN NULL;
 END;
 SELECT * INTO pending FROM fixture_pending_bundle;
 challenge:=maven.instruction_challenge(pending.proposal,'fixture-approver');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',pending.proposal,
   'challenge_id',challenge->'challenge_id','candidate_hash',pending.hash,'expected_revision',0,
   'approval_source',jsonb_build_object('origin','fixture approval','excerpt','Approve exact fixture'));
 BEGIN
   PERFORM maven.instruction_write('resolve',request,repeat('a',64),NULL,'fixture-approver');
   RAISE EXCEPTION 'invalid historical pending bundle published';
 EXCEPTION WHEN invalid_parameter_value THEN NULL;
 END;
 IF EXISTS (SELECT 1 FROM maven.records WHERE id=pending.page) THEN
   RAISE EXCEPTION 'failed publication left a record';
 END IF;
 hash:=encode(sha256(convert_to(candidate::text,'UTF8')),'hex');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',proposal,'page_id',gen_random_uuid(),
   'expected_revision',0,'classification','Sensitive','data_scope','owner','candidate',candidate,
   'source',jsonb_build_object('origin','fixture','excerpt','exact fixture'),'change_reason','Exact bundle fixture');
 result:=maven.instruction_write('create',request,repeat('a',64),hash,'fixture-owner');
 PERFORM pg_temp.assert_equal(maven.instruction_read(jsonb_build_object('proposal_id',proposal))->'bundle',bundle,'draft metadata exact');
 challenge:=maven.instruction_challenge(proposal,'fixture-approver');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',proposal,'challenge_id',challenge->'challenge_id',
   'candidate_hash',repeat('c',64),'expected_revision',0,
   'approval_source',jsonb_build_object('origin','fixture approval','excerpt','Approve exact fixture'));
 PERFORM pg_temp.assert_equal(maven.instruction_write('resolve',request,repeat('a',64),NULL,'fixture-approver')->'status',
   '"approval_invalid"','approval cannot replace bundle candidate hash');
 request:=request || jsonb_build_object('candidate_hash',hash);
 result:=maven.instruction_write('resolve',request,repeat('b',64),NULL,'fixture-approver');
 PERFORM pg_temp.assert_equal(result->'revision','1','valid bundle published');
 PERFORM pg_temp.assert_equal(maven.instruction_read('{"slug":"bundle-skill"}')->'bundle',bundle,'published bundle exact');
 PERFORM pg_temp.assert_equal(maven.instruction_read('{"slug":"bundle-skill"}')->'content_hash',to_jsonb(hash),'candidate hash unchanged');
 PERFORM pg_temp.assert_equal((SELECT page->'bundle' FROM jsonb_array_elements(maven.instruction_index('{"page_kind":"skill"}')->'pages') page
   WHERE page->>'slug'='bundle-skill'),bundle,'index includes exact bundle');
 PERFORM pg_temp.assert_equal((SELECT page->'bundle' FROM jsonb_array_elements(maven.memory_context(jsonb_build_object(
   'project_id',project,'skill_slug','bundle-skill'))->'pages') page WHERE page->>'slug'='bundle-skill'),bundle,'context includes exact bundle');
END $$;
"""

SQL_NAMED_SKILL_CONTEXT = r"""
DO $$
DECLARE project uuid:=gen_random_uuid(); request jsonb; candidate jsonb; result jsonb; helper uuid; root uuid;
BEGIN
 request:=jsonb_build_object('project_id',project,'workflow','different-workflow','operation','start-work','skill_slug','root-skill');
 candidate:=jsonb_build_object('slug','root-leaf','page_kind','procedure','title','Root selection fixture',
   'body_markdown','Approved fixture body','scope',jsonb_build_object('project',project),'authority_state','active');
 PERFORM pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','root-helper','requires',jsonb_build_array('root-leaf'));
 helper:=pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','root-skill','page_kind','skill','requires',jsonb_build_array('root-helper'));
 root:=pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','root-directive','page_kind','directive','requires','[]'::jsonb);
 PERFORM pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','root-workflow','page_kind','procedure',
   'scope',jsonb_build_object('project',project,'workflow','different-workflow'));
 PERFORM pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','root-operation',
   'scope',jsonb_build_object('project',project,'operation','start-work'));
 PERFORM pg_temp.publish(candidate);
 result:=maven.memory_context(request);
 PERFORM pg_temp.assert_equal((SELECT jsonb_agg(p->>'slug' ORDER BY p->>'slug') FROM jsonb_array_elements(result->'pages') p),
   '["root-directive","root-helper","root-leaf","root-operation","root-skill","root-workflow"]',
   'named root, transitive dependencies and both caller selectors retained');
 result:=maven.memory_context(request-'skill_slug');
 PERFORM pg_temp.assert_equal((SELECT jsonb_agg(p->>'slug' ORDER BY p->>'slug') FROM jsonb_array_elements(result->'pages') p),
   '["root-directive","root-operation","root-workflow"]','ordinary context does not load unselected skills');
 PERFORM pg_temp.assert_equal(maven.memory_context(request || '{"skill_slug":"missing-root"}')->'status',
   '"page_unavailable"','unknown named root fails without partial context');
 PERFORM pg_temp.assert_equal(maven.memory_context(request || '{"skill_slug":"root-workflow"}')->'status',
   '"page_unavailable"','procedure cannot satisfy named skill root');
 PERFORM pg_temp.assert_equal(maven.memory_context(request || jsonb_build_object('project_id',gen_random_uuid()))->'status',
   '"page_unavailable"','named root cannot cross project scope');
 candidate:=candidate || jsonb_build_object('slug','scoped-root','page_kind','skill',
   'scope',jsonb_build_object('project',project,'workflow','restricted-workflow','operation','restricted-operation'));
 PERFORM pg_temp.publish(candidate);
 PERFORM pg_temp.assert_equal(maven.memory_context(request || '{"skill_slug":"scoped-root"}')->'status',
   '"page_unavailable"','named root cannot override workflow scope');
 PERFORM pg_temp.assert_equal(maven.memory_context(request || '{"skill_slug":"scoped-root","workflow":"restricted-workflow"}')->'status',
   '"page_unavailable"','named root cannot override operation scope');
 result:=maven.memory_context(request || '{"skill_slug":"scoped-root","workflow":"restricted-workflow","operation":"restricted-operation"}');
 PERFORM pg_temp.assert_equal((SELECT jsonb_agg(p->>'slug' ORDER BY p->>'slug') FROM jsonb_array_elements(result->'pages') p),
   '["root-directive","scoped-root"]','matching scoped skill loads');
 candidate:=candidate || jsonb_build_object('slug','synthetic-root','scope',jsonb_build_object('project',project));
 PERFORM pg_temp.publish(candidate,'synthetic');
 PERFORM pg_temp.assert_equal(maven.memory_context(request || '{"skill_slug":"synthetic-root"}')->'status',
   '"page_unavailable"','synthetic skill cannot satisfy owner root');
 UPDATE maven.pages SET authority_state='revoked' WHERE id=root;
 PERFORM pg_temp.assert_equal(maven.memory_context(request)->'status','"page_unavailable"','revoked named root fails closed');
 UPDATE maven.pages SET authority_state='active' WHERE id=root;
 UPDATE maven.pages SET authority_state='revoked' WHERE id=helper;
 PERFORM pg_temp.assert_equal(maven.memory_context(request)->'status','"dependency_unavailable"','named root still requires active dependencies');
 UPDATE maven.pages SET authority_state='active' WHERE id=helper;
 candidate:=candidate || jsonb_build_object('slug','missing-dependency-root','requires',jsonb_build_array('absent-helper'));
 PERFORM pg_temp.publish(candidate);
 PERFORM pg_temp.assert_equal(maven.memory_context(request || '{"skill_slug":"missing-dependency-root"}')->'status',
   '"dependency_unavailable"','named root still requires complete dependencies');
 candidate:=candidate || jsonb_build_object('slug','cyclic-root','requires',jsonb_build_array('cyclic-helper'));
 PERFORM pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','cyclic-helper','page_kind','procedure','requires',jsonb_build_array('cyclic-root'));
 PERFORM pg_temp.publish(candidate);
 PERFORM pg_temp.assert_equal(maven.memory_context(request || '{"skill_slug":"cyclic-root"}')->'status',
   '"dependency_conflict"','named root still rejects dependency cycles');
END $$;
"""

SQL_BEHAVIOR = r"""
DO $$
DECLARE request jsonb; result jsonb; original jsonb; current_value jsonb; session_request jsonb; other_session jsonb;
  task_request jsonb; claim_request jsonb; checkpoint_request jsonb; subject jsonb; person uuid; helper uuid; page uuid;
  candidate jsonb:=jsonb_build_object('slug','fixture-directive','page_kind','directive','title','Fixture directive',
    'body_markdown','Exact fixture instruction','scope','{}'::jsonb,'authority_state','active');
BEGIN
 PERFORM pg_temp.assert_equal(maven.instruction_read('{"slug":"fixture-legacy-skill"}'),(SELECT snapshot FROM fixture_legacy_page),'legacy owner page unchanged');
 IF EXISTS (SELECT 1 FROM fixture_legacy_versions old LEFT JOIN maven.record_versions v USING(record_id,revision)
   WHERE v.record_id IS NULL OR v.snapshot IS DISTINCT FROM old.snapshot OR v.content_hash IS DISTINCT FROM old.content_hash) THEN
   RAISE EXCEPTION 'legacy history changed';
 END IF;
 request:=pg_temp.request('note',jsonb_build_object('title','Fixture','body_markdown',repeat('é',65536),'category','reference'));
 result:=pg_temp.write(request); PERFORM pg_temp.assert_equal(result->'revision','1','create owner note');
 PERFORM pg_temp.assert_equal(pg_temp.write(request)->'replayed','true','same request replays');
 original:=maven.memory_read(jsonb_build_object('record_id',request->'record_id'));
 PERFORM pg_temp.assert_equal(original->'data',request->'data','full note read');
 PERFORM pg_temp.assert_equal(original->'source',request->'source','exact provenance');
 PERFORM pg_temp.assert_equal(original->'data_scope','"owner"','owner never synthetic');
 PERFORM pg_temp.assert_equal(original->'operative','false','note never authority');
 PERFORM pg_temp.assert_equal(maven.memory_lookup((request->>'request_key')::uuid)->'record_id',request->'record_id','receipt lookup');
 PERFORM pg_temp.assert_equal(pg_temp.write(request || jsonb_build_object('change_reason','Different'))->'status','"idempotency_conflict"','key conflict');
 PERFORM pg_temp.assert_equal(pg_temp.write(request || jsonb_build_object('request_key',gen_random_uuid()))->'status','"revision_conflict"','stale revision');
 PERFORM pg_temp.assert_equal(maven.change_fact(jsonb_build_object('request_key',request->'request_key'),repeat('a',64),'fixture')->'status','"idempotency_conflict"','legacy route shares key domain');
 PERFORM pg_temp.assert_equal(maven.instruction_write('create',jsonb_build_object('request_key',request->'request_key','proposal_id',gen_random_uuid()),repeat('a',64),repeat('b',64),'fixture')->'status','"idempotency_conflict"','instruction route shares key domain');
 request:=request || jsonb_build_object('request_key',gen_random_uuid(),'expected_revision',1,'data',jsonb_build_object('title','Corrected','body_markdown','corrected','category','reference'));
 result:=pg_temp.write(request); PERFORM pg_temp.assert_equal(result->'revision','2','revision advances');
 PERFORM pg_temp.assert_equal(maven.memory_read(jsonb_build_object('record_id',request->'record_id','revision',1))->'data',original->'data','historical note exact');
 PERFORM pg_temp.assert_equal(maven.memory_search('{"kind":"note","query":"corrected","limit":1}')->'records'->0->'record_id',request->'record_id','literal bounded search');
 PERFORM pg_temp.assert_equal(maven.memory_context('{}')->'pages','[]','notes excluded context');
 subject:=pg_temp.request('entity','{"entity_type":"person","name":"Fixture person","aliases":[]}');
 PERFORM pg_temp.write(subject); person:=(subject->>'record_id')::uuid;
 result:=pg_temp.write(pg_temp.request('fact',jsonb_build_object('subject_id',person,'predicate','fixture','value','owner fact')));
 PERFORM pg_temp.assert_equal(result->'revision','1','typed owner fact');
 result:=pg_temp.write(pg_temp.request('relationship',jsonb_build_object('subject_id',person,'predicate','knows','object_id',gen_random_uuid())));
 PERFORM pg_temp.assert_equal(result->'status','"reference_unavailable"','dangling entity blocked');
 result:=pg_temp.write(pg_temp.request('note','{"title":"Second fixture","body_markdown":"more","category":"reference"}'));
 result:=maven.memory_search('{"kind":"note","limit":1}');
 PERFORM pg_temp.assert_equal(to_jsonb(jsonb_array_length(result->'records')),'1','bounded search page');
 IF result->>'next_after_id' IS NULL THEN RAISE EXCEPTION 'pagination cursor absent'; END IF;
 current_value:=maven.memory_search(jsonb_build_object('kind','note','limit',1,'after_id',result->'next_after_id'));
 PERFORM pg_temp.assert_equal(current_value->'next_after_id','null','last search cursor');
 IF current_value->'records'->0->'record_id'=result->'records'->0->'record_id' THEN RAISE EXCEPTION 'pagination repeated record'; END IF;
 session_request:=pg_temp.request('session','{"state":"active","summary":"Fixture session"}'); PERFORM pg_temp.write(session_request);
 other_session:=pg_temp.request('session','{"state":"active","summary":"Second fixture session"}'); PERFORM pg_temp.write(other_session);
 task_request:=pg_temp.request('task','{"title":"Fixture task","description":"","state":"open"}'); PERFORM pg_temp.write(task_request);
 claim_request:=jsonb_build_object('request_key',gen_random_uuid(),'record_id',task_request->'record_id','expected_revision',1,
   'session_id',session_request->'record_id','action','claim','lease_seconds',30,'source',task_request->'source','change_reason','Claim fixture');
 result:=pg_temp.write(claim_request,'claim'); PERFORM pg_temp.assert_equal(result->'fence','1','first fence');
 PERFORM pg_temp.assert_equal(result->'revision','2','claim versions task');
 PERFORM pg_temp.assert_equal(pg_temp.write(claim_request || jsonb_build_object('request_key',gen_random_uuid(),'expected_revision',2,'session_id',other_session->'record_id'),'claim')->'status','"claim_conflict"','live rival refused');
 PERFORM pg_temp.assert_equal(pg_temp.write(task_request || jsonb_build_object('request_key',gen_random_uuid(),'expected_revision',2))->'status','"claim_conflict"','write cannot bypass claim');
 UPDATE maven.memory_items SET claim_expires_at=clock_timestamp()-interval '1 second' WHERE id=(task_request->>'record_id')::uuid;
 claim_request:=(claim_request-'lease_seconds') || jsonb_build_object('request_key',gen_random_uuid(),'expected_revision',2,'action','complete','fence',1);
 PERFORM pg_temp.assert_equal(pg_temp.write(claim_request,'claim')->'status','"claim_conflict"','expired completion rejected');
 result:=pg_temp.write((claim_request-'fence') || jsonb_build_object('request_key',gen_random_uuid(),'session_id',other_session->'record_id','action','claim','lease_seconds',30),'claim');
 PERFORM pg_temp.assert_equal(result->'fence','2','reclaim fences old owner');
 PERFORM pg_temp.assert_equal(pg_temp.write(claim_request || jsonb_build_object('request_key',gen_random_uuid(),'expected_revision',3),'claim')->'status','"claim_conflict"','stale fence rejected');
 result:=pg_temp.write(claim_request || jsonb_build_object('request_key',gen_random_uuid(),'expected_revision',3,'session_id',other_session->'record_id','fence',2),'claim');
 PERFORM pg_temp.assert_equal(result->'state','"completed"','current owner completes');
 PERFORM pg_temp.assert_equal(maven.memory_context('{}')->'tasks','[]','terminal task omitted');
 checkpoint_request:=pg_temp.request('checkpoint',jsonb_build_object('session_id',session_request->'record_id','candidate_digest',repeat('a',64),
   'git_base',repeat('b',40),'memory_refs',jsonb_build_array(jsonb_build_object('record_id',request->'record_id','revision',2)),'state','prepared'));
 PERFORM pg_temp.assert_equal(pg_temp.write(checkpoint_request)->'revision','1','checkpoint references exact version');
 checkpoint_request:=checkpoint_request || jsonb_build_object('request_key',gen_random_uuid(),'expected_revision',1,'data',
   checkpoint_request->'data' || jsonb_build_object('state','committed','final_commit',repeat('c',40)));
 PERFORM pg_temp.assert_equal(pg_temp.write(checkpoint_request)->'revision','2','checkpoint final commit');
 candidate:=candidate || jsonb_build_object('slug','fixture-unused','page_kind','skill'); PERFORM pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','fixture-helper','scope',jsonb_build_object('workflow','other-workflow')); helper:=pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','fixture-directive','page_kind','directive','scope','{}'::jsonb,'requires',jsonb_build_array('fixture-helper')); page:=pg_temp.publish(candidate);
 result:=maven.memory_context('{}'); PERFORM pg_temp.assert_equal(to_jsonb(jsonb_array_length(result->'pages')),'2','directive plus reusable dependency only');
 PERFORM pg_temp.assert_equal(maven.instruction_read('{"slug":"fixture-directive"}')->'requires','["fixture-helper"]','dependency metadata preserved');
 candidate:=candidate || jsonb_build_object('slug','fixture-synthetic','requires','[]'::jsonb); PERFORM pg_temp.publish(candidate,'synthetic');
 PERFORM pg_temp.assert_equal(to_jsonb(jsonb_array_length(maven.memory_context('{}')->'pages')),'2','synthetic authority excluded');
 UPDATE maven.pages SET authority_state='revoked' WHERE id=helper;
 PERFORM pg_temp.assert_equal(maven.memory_context('{}')->'status','"dependency_unavailable"','revoked dependency fails closed');
 UPDATE maven.pages SET authority_state='active' WHERE id=helper;
 candidate:=candidate || jsonb_build_object('slug','fixture-cycle-a','requires',jsonb_build_array('fixture-cycle-b')); PERFORM pg_temp.publish(candidate);
 candidate:=candidate || jsonb_build_object('slug','fixture-cycle-b','requires',jsonb_build_array('fixture-cycle-a')); PERFORM pg_temp.publish(candidate);
 PERFORM pg_temp.assert_equal(maven.memory_context('{}')->'status','"dependency_conflict"','cycles fail closed');
 UPDATE maven.service_state SET minimum_client_protocol=2 WHERE singleton;
 PERFORM set_config('maven.client_protocol','1',true);
 PERFORM pg_temp.assert_equal(pg_temp.write(pg_temp.request('note','{"title":"Old client","body_markdown":"blocked","category":"fixture"}'))->'status','"client_incompatible"','obsolete writer refused');
 PERFORM set_config('maven.client_protocol','2',true);
 PERFORM pg_temp.assert_equal(pg_temp.write(pg_temp.request('note','{"title":"New client","body_markdown":"accepted","category":"fixture"}'))->'revision','1','current writer accepted');
END $$;
SET CONSTRAINTS ALL IMMEDIATE;
"""

SQL_TRANSPORT_ASSERTIONS = r"""
DO $$
DECLARE reconstructed bytea; expected jsonb;
BEGIN
 SELECT string_agg(decode(chunk,'base64'),''::bytea ORDER BY position) INTO reconstructed FROM fixture_wire;
 IF octet_length(reconstructed)<=65536 OR (SELECT count(*) FROM fixture_wire)<2 THEN
   RAISE EXCEPTION 'fixture did not exceed Data API row limit';
 END IF;
 IF EXISTS (SELECT 1 FROM fixture_wire WHERE octet_length(chunk)>8192 OR size<>octet_length(reconstructed)
   OR digest<>encode(sha256(reconstructed),'hex')) THEN RAISE EXCEPTION 'invalid transport metadata'; END IF;
 expected:=maven.memory_read(jsonb_build_object('record_id',(SELECT id FROM maven.memory_items WHERE data->>'title'='Corrected'),'revision',1));
 PERFORM pg_temp.assert_equal(convert_from(reconstructed,'UTF8')::jsonb,expected,'chunked SQL read preserves full record');
 IF (SELECT last_value FROM fixture_evaluation)<>1 THEN RAISE EXCEPTION 'result evaluated more than once'; END IF;
END $$;
"""

SQL_APPLICATION_STATE_LOCK = r"""
CREATE ROLE maven_state_guard_fixture;
GRANT USAGE ON SCHEMA maven TO maven_state_guard_fixture;
GRANT SELECT ON maven.service_state TO maven_state_guard_fixture;
GRANT EXECUTE ON FUNCTION maven.lock_service_state() TO maven_state_guard_fixture;
SET LOCAL ROLE maven_state_guard_fixture;
DO $$
BEGIN
 BEGIN
   PERFORM 1 FROM maven.service_state WHERE singleton FOR SHARE;
   RAISE EXCEPTION 'fixture unexpectedly has direct row-lock privileges';
 EXCEPTION WHEN insufficient_privilege THEN NULL;
 END;
 IF maven.lock_service_state()->>'data_scope' IS DISTINCT FROM 'owner_memory' THEN
   RAISE EXCEPTION 'application cannot lock and read authoritative service state';
 END IF;
 BEGIN
   UPDATE maven.service_state SET state='maintenance' WHERE false;
   RAISE EXCEPTION 'application unexpectedly has direct state-write privileges';
 EXCEPTION WHEN insufficient_privilege THEN NULL;
 END;
END $$;
RESET ROLE;
"""


@unittest.skipUnless(os.environ.get("MAVEN_MEMORY_TEST_DSN") and shutil.which("psql"),
                     "requires explicit disposable PostgreSQL DSN and psql")
class OwnerPostgreSQLTests(unittest.TestCase):
    def test_real_sql_history_claims_dependencies_and_protocol(self):
        statements = ["BEGIN;", "DO $$ BEGIN IF to_regnamespace('maven') IS NOT NULL THEN RAISE EXCEPTION 'refuse existing maven schema'; END IF; END $$;",
                      "CREATE SCHEMA maven;"]
        for index, (_, _, migration) in enumerate(admin.migrations()):
            statements.extend(statement + ";" for statement in migration)
            if index == 4:
                statements.append(SQL_LEGACY)
                statements.append("SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED;")
            if index == 8:
                statements.append("SELECT set_config('maven.client_protocol','2',true);")
                statements.append(SQL_PENDING_SKILL_BUNDLE)
        read_expression = ("maven.memory_read(jsonb_build_object('record_id',"
                           "(SELECT id FROM maven.memory_items WHERE data->>'title'='Corrected'),'revision',1))")
        once_expression = "jsonb_build_object('sequence',nextval('fixture_evaluation'),'body',repeat('x',70000))"
        statements.extend([
            "SELECT set_config('maven.client_protocol','2',true);", SQL_HELPERS,
            "SAVEPOINT skill_bundles;", SQL_SKILL_BUNDLES,
            "ROLLBACK TO SAVEPOINT skill_bundles; RELEASE SAVEPOINT skill_bundles;",
            "SAVEPOINT named_skill_context;", SQL_NAMED_SKILL_CONTEXT,
            "ROLLBACK TO SAVEPOINT named_skill_context; RELEASE SAVEPOINT named_skill_context;", SQL_BEHAVIOR,
            "CREATE TEMP TABLE fixture_wire AS " + store.json_result_sql(read_expression) + ";",
            "CREATE TEMP SEQUENCE fixture_evaluation;",
            "CREATE TEMP TABLE fixture_once AS " + store.json_result_sql(once_expression) + ";",
            SQL_TRANSPORT_ASSERTIONS, SQL_APPLICATION_STATE_LOCK, "ROLLBACK;"])
        env = {**os.environ, "PGDATABASE": os.environ["MAVEN_MEMORY_TEST_DSN"]}
        completed = subprocess.run([shutil.which("psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1"],
                                   input="\n".join(statements), text=True, capture_output=True,
                                   env=env, timeout=90, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
