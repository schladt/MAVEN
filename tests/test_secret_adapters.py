"""Offline declarative adapter contracts, execution safety and authorization tests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools import secret_adapters as adapters
from tools.memory import adapter_contracts as contracts
from tools.memory.client import MemoryClientError


HOME = '0c7e20d8-1c43-4ca8-b9df-a3472cefa618'
CATALOG_ID = '8bd0d6c3-a28e-474b-9f4f-645be4be80fd'
SOURCE = {'origin': 'owner-message:adapter-review', 'excerpt': 'Approve this exact fixture adapter.'}
SECRET = b'offline-only-fixture-secret-472192'


def definition(code='import json; print(json.dumps({"ok": True}))', *, mode='stdin'):
    arguments = [{'literal': '-c'}, {'literal': code}]
    if mode == 'fd':
        arguments.append({'template': '{secret_fd}'})
    return {'kind': 'secret-adapter', 'version': 1, 'executable': sys.executable,
            'arguments': arguments, 'parameters': {}, 'input': {'mode': mode},
            'timeout_seconds': 2, 'max_output_bytes': 4096, 'results': {'ok': {'type': 'boolean'}}}


def invocation(**changes):
    return {'home_id': HOME, 'invocation_id': str(uuid4()), 'adapter': 'secret-adapter-fixture',
            'adapter_revision': 1, 'secret_name': 'fixture.test',
            'secret_ref': {'provider': 'ssm', 'name': '/fixture/test:1'},
            'purpose': 'offline regression', 'operation': 'check', 'destination': 'fixture.invalid',
            'parameters': {}, 'catalog_record_id': CATALOG_ID, 'catalog_revision': 1,
            'store_id': 'a' * 64, **changes}


def candidate(document=None):
    return {'slug': 'secret-adapter-fixture', 'page_kind': 'procedure', 'title': 'Fixture adapter',
            'body_markdown': json.dumps(document or definition()), 'scope': {'project': HOME},
            'authority_state': 'active', 'requires': ['maven-secrets']}


def ready(data, **extra):
    return {'status': 'ready', 'data': data, **extra}


def policy(document=None, revision=1):
    return {'home_id': HOME, 'slug': 'secret-adapter-fixture', 'page_id': str(uuid4()),
            'revision': revision, 'document': document or definition()}


def catalog_record():
    return {'record_id': CATALOG_ID, 'revision': 1, 'project_id': HOME,
            'data': {'key': 'secret:fixture.test', 'scope': 'project',
                     'value': {'store_id': 'a' * 64},
                     'secret_ref': {'provider': 'ssm', 'name': '/fixture/test:1'}}}


class AdapterContractTests(unittest.TestCase):
    def test_declarative_literals_and_named_templates_only(self):
        data = definition()
        data['parameters'] = {'region': {'max_length': 20, 'choices': ['east']}}
        data['arguments'].append({'template': '--region={region}'})
        self.assertIs(contracts.validate_definition(data), data)
        for bad in ('{secret}', '{region.__class__}', '{region[0]}', '{region!r}', '{region:>100}', '{}', '{'):
            changed = copy.deepcopy(data)
            changed['arguments'].append({'template': bad})
            with self.subTest(bad=bad), self.assertRaises(contracts.AdapterError):
                contracts.validate_definition(changed)
        for extra in ('shell', 'environment', 'cwd', 'script'):
            with self.subTest(extra=extra), self.assertRaises(contracts.AdapterError):
                contracts.validate_definition({**data, extra: 'forbidden'})
        changed = copy.deepcopy(data)
        changed['arguments'].append({'literal': 'one', 'template': '{region}'})
        with self.assertRaises(contracts.AdapterError):
            contracts.validate_definition(changed)

    def test_relative_programs_and_unbounded_limits_rejected(self):
        for key, value in [('executable', 'python'), ('executable', '/bin/../bin/python'),
                           ('timeout_seconds', 0), ('timeout_seconds', 301), ('timeout_seconds', True),
                           ('max_output_bytes', 1048577), ('max_output_bytes', -1)]:
            with self.subTest(key=key, value=value), self.assertRaises(contracts.AdapterError):
                contracts.validate_definition({**definition(), key: value})
        for mode in ('argv', 'env', 'file'):
            with self.assertRaises(contracts.AdapterError):
                contracts.validate_definition({**definition(), 'input': {'mode': mode}})
        with self.assertRaises(contracts.AdapterError):
            contracts.validate_definition({**definition(), 'input': {'mode': 'fd'}})

    def test_home_procedure_scope_dependency_and_reserved_kind(self):
        self.assertIsNotNone(contracts.validate_adapter_candidate(candidate()))
        for change in ({'page_kind': 'skill'}, {'scope': {}}, {'requires': []},
                       {'scope': {'project': HOME, 'operation': 'check'}}, {'slug': 'secret-grant-fixture'}):
            with self.subTest(change=change), self.assertRaises(contracts.AdapterError):
                contracts.validate_adapter_candidate({**candidate(), **change})
        self.assertIsNone(contracts.validate_adapter_candidate({'slug': 'ordinary-procedure'}))

    def test_digest_binds_every_invocation_field_and_is_order_independent(self):
        request = invocation()
        digest = contracts.invocation_digest(request)
        self.assertEqual(digest, contracts.invocation_digest(dict(reversed(list(request.items())))))
        for change in ({'purpose': 'other'}, {'operation': 'other'}, {'destination': 'other.invalid'},
                       {'adapter_revision': 2}, {'catalog_revision': 2}, {'store_id': 'b' * 64},
                       {'invocation_id': str(uuid4())}, {'secret_ref': {'provider': 'ssm', 'name': '/fixture/test:2'}}):
            self.assertNotEqual(digest, contracts.invocation_digest({**request, **change}))
        with self.assertRaises(contracts.AdapterError):
            contracts.validate_adapter_request(contracts.CONSUME_ROUTE, {
                'invocation': request, 'invocation_digest': '0' * 64,
                'grant': 'secret-grant-fixture', 'grant_revision': 1})

    def test_single_use_grant_requires_exact_digest_and_timezone(self):
        controller = adapters.SecretAdapters(HOME, client=Mock())
        request = invocation()
        grant = controller.grant_document(request, mode='single-use', expires_at='2030-01-01T00:00:00Z')
        self.assertEqual(grant['invocation_digest'], contracts.invocation_digest(request))
        self.assertEqual(grant['secret_ref'], request['secret_ref'])
        self.assertEqual(grant['destination'], request['destination'])
        missing = {key: value for key, value in grant.items() if key != 'invocation_digest'}
        with self.assertRaises(contracts.AdapterError):
            contracts.validate_grant(missing)
        with self.assertRaises(contracts.AdapterError):
            contracts.validate_grant({**grant, 'mode': 'standing'})
        with self.assertRaises(contracts.AdapterError):
            contracts.validate_grant({**grant, 'expires_at': '2030-01-01T00:00:00'})


@unittest.skipUnless(os.name == 'posix', 'private FD and process-group runner is POSIX')
class AdapterRunnerTests(unittest.TestCase):
    def test_stdin_only_declared_results_and_no_inherited_environment(self):
        code = ('import os,sys,json; s=sys.stdin.buffer.read(); '
                'print(json.dumps({"ok": bool(s) and "MAVEN_TEST_CREDENTIAL" not in os.environ, '
                '"raw": s.decode()})); print(s.decode(),file=sys.stderr)')
        with patch.dict(os.environ, {'MAVEN_TEST_CREDENTIAL': 'must-not-inherit'}):
            result = adapters.run_adapter(definition(code), invocation(), SECRET)
        self.assertEqual(result, {'status': 'completed', 'results': {'ok': True}})
        self.assertNotIn(SECRET.decode(), repr(result))

    def test_private_fd_delivery_does_not_put_credential_in_argv(self):
        code = ('import os,sys,json; s=os.read(int(sys.argv[1]),4096); '
                'print(json.dumps({"ok": bool(s) and s.decode() not in repr(sys.argv) and sys.stdin.read()==""}))')
        self.assertTrue(adapters.run_adapter(definition(code, mode='fd'), invocation(), SECRET)['results']['ok'])

    def test_shell_metacharacters_are_literal(self):
        data = definition('import json,sys; print(json.dumps({"ok":sys.argv[1]=="$(touch /tmp/never);*"}))')
        data['arguments'].append({'literal': '$(touch /tmp/never);*'})
        self.assertTrue(adapters.run_adapter(data, invocation(), SECRET)['results']['ok'])

    def test_credential_argument_denied_before_launch(self):
        data = definition()
        data['arguments'].append({'literal': SECRET.decode()})
        with patch.object(adapters.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(contracts.AdapterError, '^credential_argument_forbidden$'):
                adapters.run_adapter(data, invocation(), SECRET)
            spawn.assert_not_called()

    def test_declared_secret_output_and_encoded_secret_output_are_denied(self):
        for expression in ('s.decode()', 'base64.b64encode(s).decode()', 's.hex()'):
            data = definition('import sys,json,base64; s=sys.stdin.buffer.read(); print(json.dumps({"value":' + expression + '}))')
            data['results'] = {'value': {'type': 'string', 'max_length': 256}}
            with self.subTest(expression=expression), self.assertRaisesRegex(contracts.AdapterError, '^unsafe_adapter_result$'):
                adapters.run_adapter(data, invocation(), SECRET)

    def test_wrong_type_duplicate_json_and_undeclared_output_are_safe(self):
        for code in ('print("not json")', 'print(\'{"ok":true,"ok":false}\')',
                     'print(\'{"ok":"true"}\')', 'print(\'{"other":true}\')'):
            with self.subTest(code=code), self.assertRaisesRegex(contracts.AdapterError, '^unsafe_adapter_result$'):
                adapters.run_adapter(definition(code), invocation(), SECRET)
        data = definition('import sys; print(sys.stdin.read())')
        data['results'] = {}
        self.assertEqual(adapters.run_adapter(data, invocation(), SECRET)['results'], {})

    def test_nonzero_error_never_contains_stdout_or_stderr(self):
        code = 'import sys; s=sys.stdin.read(); print(s); print(s,file=sys.stderr); sys.exit(7)'
        with self.assertRaisesRegex(contracts.AdapterError, '^adapter_failed$') as error:
            adapters.run_adapter(definition(code), invocation(), SECRET)
        self.assertNotIn(SECRET.decode(), str(error.exception))

    def test_combined_output_ceiling_and_runtime_ceiling(self):
        for channel in ('sys.stdout', 'sys.stderr'):
            data = definition(f'import sys; {channel}.write("x"*100000)')
            with self.subTest(channel=channel), self.assertRaisesRegex(contracts.AdapterError, '^adapter_output_limit$'):
                adapters.run_adapter(data, invocation(), SECRET)
        started = time.monotonic()
        data = definition('import time; time.sleep(20)')
        data['timeout_seconds'] = 1
        with self.assertRaisesRegex(contracts.AdapterError, '^adapter_timeout$'):
            adapters.run_adapter(data, invocation(), SECRET)
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(Path('/proc').exists(), 'Linux process state check')
    def test_timeout_kills_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'pid'
            code = ('import os,time; pid=os.fork(); '
                    f'open({str(marker)!r},"w").write(str(pid)) if pid else None; time.sleep(20)')
            data = definition(code)
            data['timeout_seconds'] = 1
            try:
                with self.assertRaisesRegex(contracts.AdapterError, '^adapter_timeout$'):
                    adapters.run_adapter(data, invocation(), SECRET)
                pid = int(marker.read_text())
                state = Path(f'/proc/{pid}/stat')
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        if state.read_text().split()[2] == 'Z':
                            break
                    except FileNotFoundError:
                        break
                    time.sleep(0.01)
                else:
                    self.fail('adapter descendant survived timeout cleanup')
            finally:
                if marker.exists():
                    pid = int(marker.read_text())
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class AdapterControllerTests(unittest.TestCase):
    def controller(self, client=None):
        catalog = Mock(home_id=HOME)
        catalog.lookup.return_value = catalog_record()
        secret_store = Mock(store_id='a' * 64)
        secret_store.read_secret.return_value = SECRET
        return adapters.SecretAdapters(HOME, client=client or Mock(), catalog=catalog, store=secret_store)

    def test_prepare_reads_exact_named_catalog_and_has_no_secret_bytes(self):
        client = Mock()
        client.invoke.return_value = ready(policy())
        controller = self.controller(client)
        result = controller.prepare(adapter='secret-adapter-fixture', secret_name='fixture.test',
                                    purpose='offline regression', operation='check', destination='fixture.invalid', parameters={})
        controller.catalog.lookup.assert_called_once_with('fixture.test')
        controller.store.read_secret.assert_not_called()
        self.assertEqual(result['invocation']['catalog_record_id'], CATALOG_ID)
        self.assertNotIn(SECRET.decode(), json.dumps(result))

    def test_cloud_unavailable_or_reconciled_consumption_never_reads_secret(self):
        for second in (RuntimeError('sensitive provider detail'), ready({'consumed': True}, reconciled=True)):
            client = Mock()
            client.invoke.side_effect = [ready(policy()), second]
            controller = self.controller(client)
            with self.assertRaisesRegex(contracts.AdapterError, '^adapter_authority_unavailable$'):
                controller.execute(invocation=invocation(), grant='secret-grant-fixture', grant_revision=1)
            controller.store.read_secret.assert_not_called()
        client = Mock()
        client.invoke.side_effect = RuntimeError('offline')
        controller = self.controller(client)
        with self.assertRaises(contracts.AdapterError):
            controller.execute(invocation=invocation(), grant='secret-grant-fixture', grant_revision=1)
        controller.catalog.lookup.assert_not_called()
        controller.store.read_secret.assert_not_called()

    def test_consumed_invocation_cannot_retry_after_uncertain_network(self):
        request = invocation()
        seen = set()
        def transport(route, body):
            if route == contracts.POLICY_ROUTE:
                return ready(policy())
            identity = body['invocation']['invocation_id']
            if identity in seen:
                raise RuntimeError('claim_conflict')
            seen.add(identity)
            raise RuntimeError('response lost after commit')
        client = Mock()
        client.invoke.side_effect = transport
        controller = self.controller(client)
        for _ in range(2):
            with self.assertRaises(contracts.AdapterError):
                controller.execute(invocation=request, grant='secret-grant-fixture', grant_revision=1)
        self.assertEqual(seen, {request['invocation_id']})
        controller.store.read_secret.assert_not_called()

    def test_exact_consumption_precedes_read_and_catalog_rotation_fails_closed(self):
        request = invocation()
        authorization = {'consumed': True, 'invocation_id': request['invocation_id'],
                         'invocation_digest': contracts.invocation_digest(request)}
        client = Mock()
        client.invoke.side_effect = [ready(policy()), ready(authorization)]
        controller = self.controller(client)
        rotated = catalog_record()
        rotated['revision'] = 2
        controller.catalog.lookup.return_value = rotated
        with self.assertRaisesRegex(contracts.AdapterError, '^adapter_catalog_changed$'):
            controller.execute(invocation=request, grant='secret-grant-fixture', grant_revision=1)
        controller.store.read_secret.assert_not_called()
        self.assertEqual(client.invoke.call_args_list[1].args[0], contracts.CONSUME_ROUTE)

    def test_revision_change_blocks_before_consume(self):
        client = Mock()
        client.invoke.return_value = ready(policy(revision=2))
        controller = self.controller(client)
        with self.assertRaisesRegex(contracts.AdapterError, '^adapter_revision_changed$'):
            controller.execute(invocation=invocation(), grant='secret-grant-fixture', grant_revision=1)
        self.assertEqual(client.invoke.call_count, 1)
        controller.store.read_secret.assert_not_called()

    def test_success_launches_once_after_exact_authorization(self):
        request = invocation()
        client = Mock()
        client.invoke.side_effect = [ready(policy()), ready({'consumed': True,
            'invocation_id': request['invocation_id'], 'invocation_digest': contracts.invocation_digest(request)})]
        controller = self.controller(client)
        with patch.object(adapters, 'run_adapter', return_value={'status': 'completed', 'results': {'ok': True}}) as run:
            result = controller.execute(invocation=request, grant='secret-grant-fixture', grant_revision=1)
        run.assert_called_once_with(definition(), request, SECRET)
        self.assertEqual(result['results'], {'ok': True})
        self.assertNotIn(SECRET.decode(), repr(client.invoke.call_args_list))

    @staticmethod
    def create_receipt(route, request):
        assert route == '/v1/proposals/create'
        return ready({'proposal_id': request['proposal_id'], 'page_id': request['page_id'],
                      'expected_revision': request['expected_revision'], 'request_key': request['request_key'],
                      'candidate_hash': hashlib.sha256(contracts._canonical(request['candidate']).encode()).hexdigest()})

    def test_draft_review_and_exact_owner_approval_use_publication_protocol(self):
        client, approval = Mock(), Mock()
        client.invoke.side_effect = self.create_receipt
        controller = adapters.SecretAdapters(HOME, client=client, approval_client=approval)
        draft = controller.draft(slug='secret-adapter-fixture', title='Fixture adapter',
                                 document=definition(), owner_source=SOURCE)
        create_route, create = client.invoke.call_args.args
        self.assertEqual(create_route, '/v1/proposals/create')
        self.assertEqual(create['source'], SOURCE)
        self.assertEqual(create['candidate']['scope'], {'project': HOME})
        approval.invoke.assert_not_called()
        page = {**candidate(), 'proposal_id': draft['proposal_id'], 'page_id': draft['page_id'],
                'revision': 0, 'data_scope': 'owner', 'content_hash': draft['candidate_hash'],
                'candidate_authority_state': 'active'}
        client.read_full.return_value = ready(page)
        challenge = {'proposal_id': draft['proposal_id'], 'challenge_id': str(uuid4()), 'candidate_hash': draft['candidate_hash'],
                     'expected_revision': 0, 'expires_at': '2030-01-01T00:00:00Z'}
        approval.invoke.return_value = ready(challenge)
        review = controller.review(draft['proposal_id'])
        self.assertEqual(json.loads(review['review']['body_markdown']), definition())
        with self.assertRaisesRegex(contracts.AdapterError, '^owner_approval_required$'):
            controller.approve(**{key: challenge[key] for key in ('proposal_id', 'challenge_id', 'candidate_hash', 'expected_revision')},
                               approved=False, owner_source=SOURCE)
        approval.invoke.side_effect = lambda route, request: ready({
            'page_id': draft['page_id'], 'revision': 1, 'proposal_id': draft['proposal_id'],
            'request_key': request['request_key']})
        controller.approve(**{key: challenge[key] for key in ('proposal_id', 'challenge_id', 'candidate_hash', 'expected_revision')},
                           approved=True, owner_source=SOURCE)
        resolve_route, resolve = approval.invoke.call_args.args
        self.assertEqual(resolve_route, '/v1/proposals/resolve')
        self.assertEqual(resolve['approval_source'], SOURCE)
        self.assertEqual(resolve['candidate_hash'], draft['candidate_hash'])

    def test_revocation_requires_conditional_existing_revision(self):
        client = Mock()
        client.invoke.side_effect = self.create_receipt
        controller = adapters.SecretAdapters(HOME, client=client)
        with self.assertRaises(contracts.AdapterError):
            controller.draft(slug='secret-adapter-fixture', title='Revoke', document=definition(),
                             owner_source=SOURCE, revoke=True)
        controller.draft(slug='secret-adapter-fixture', title='Revoke', document=definition(), owner_source=SOURCE,
                         page_id=str(uuid4()), expected_revision=3, revoke=True)
        request = client.invoke.call_args.args[1]
        self.assertEqual(request['expected_revision'], 3)
        self.assertEqual(request['candidate']['authority_state'], 'revoked')

    def test_rejected_draft_reports_failure_and_preserves_request_for_corrected_service(self):
        arguments = {'slug': 'secret-adapter-fixture', 'title': 'Fixture adapter',
                     'document': definition(), 'owner_source': SOURCE}
        for as_response in (False, True):
            with self.subTest(as_response=as_response):
                client = Mock()
                if as_response:
                    client.invoke.return_value = {'status': 'database_statement_rejected'}
                else:
                    client.invoke.side_effect = MemoryClientError('database_statement_rejected', detail='SQLSTATE: 42725')
                controller = adapters.SecretAdapters(HOME, client=client)
                failed = controller.draft(**arguments)
                self.assertEqual((failed['status'], failed['error']), ('failed', 'database_statement_rejected'))
                original = client.invoke.call_args.args[1]
                self.assertEqual(failed['request'], original)
                client.invoke.assert_called_once()
                client.invoke.side_effect = self.create_receipt
                recovered = controller.draft(**arguments)
                self.assertEqual(recovered['status'], 'draft')
                self.assertEqual(recovered['request_key'], failed['request_key'])
                self.assertEqual(client.invoke.call_args.args[1], original)

    def test_rejected_approval_is_not_a_pending_or_successful_publication(self):
        client, approval = Mock(), Mock()
        proposal, page_id = str(uuid4()), str(uuid4())
        client.read_full.return_value = ready({
            **candidate(), 'proposal_id': proposal, 'page_id': page_id, 'revision': 0,
            'data_scope': 'owner', 'content_hash': 'b' * 64, 'candidate_authority_state': 'active'})
        approval.invoke.side_effect = MemoryClientError('database_statement_rejected')
        result = adapters.SecretAdapters(HOME, client=client, approval_client=approval).approve(
            proposal_id=proposal, challenge_id=str(uuid4()), candidate_hash='b' * 64,
            expected_revision=0, owner_source=SOURCE, approved=True)
        self.assertEqual((result['status'], result['error']), ('failed', 'database_statement_rejected'))
        self.assertEqual(result['request'], approval.invoke.call_args.args[1])
        approval.invoke.assert_called_once()

    def test_unknown_draft_preserves_exact_request_across_controller_restart(self):
        client = Mock()
        client.invoke.side_effect = RuntimeError('lost write response')
        arguments = {'slug': 'secret-adapter-fixture', 'title': 'Fixture adapter',
                     'document': definition(), 'owner_source': SOURCE}
        pending = adapters.SecretAdapters(HOME, client=client).draft(**arguments)
        self.assertEqual(pending['status'], 'outcome_unknown')
        original = client.invoke.call_args.args[1]
        self.assertEqual(pending['request'], original)
        self.assertEqual(pending['request_key'], original['request_key'])
        def recovered(route, request):
            self.assertEqual(request, original)
            return {**self.create_receipt(route, request), 'reconciled': True}
        client.invoke.side_effect = recovered
        result = adapters.SecretAdapters(HOME, client=client).draft(**arguments)
        self.assertEqual(result['status'], 'draft')
        self.assertEqual(result['proposal_id'], pending['proposal_id'])
        self.assertEqual(result['page_id'], pending['page_id'])
        self.assertEqual(result['request_key'], pending['request_key'])

    def test_unknown_approval_reuses_exact_review_receipt_without_retargeting(self):
        client, approval = Mock(), Mock()
        proposal, page_id, challenge = str(uuid4()), str(uuid4()), str(uuid4())
        page = {**candidate(), 'proposal_id': proposal, 'page_id': page_id, 'revision': 0,
                'data_scope': 'owner', 'content_hash': 'b' * 64, 'candidate_authority_state': 'active'}
        client.read_full.return_value = ready(page)
        arguments = {'proposal_id': proposal, 'challenge_id': challenge, 'candidate_hash': 'b' * 64,
                     'expected_revision': 0, 'owner_source': SOURCE, 'approved': True}
        approval.invoke.side_effect = RuntimeError('lost publication response')
        pending = adapters.SecretAdapters(HOME, client=client, approval_client=approval).approve(**arguments)
        self.assertEqual(pending['status'], 'outcome_unknown')
        original = approval.invoke.call_args.args[1]
        self.assertEqual(pending['request'], original)
        def recovered(route, request):
            self.assertEqual(route, '/v1/proposals/resolve')
            self.assertEqual(request, original)
            return ready({'page_id': page_id, 'revision': 1, 'proposal_id': proposal,
                          'request_key': request['request_key'], 'replayed': True}, reconciled=True)
        approval.invoke.side_effect = recovered
        controller = adapters.SecretAdapters(HOME, client=client, approval_client=approval)
        result = controller.approve(**arguments)
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['request_key'], pending['request_key'])
        before = approval.invoke.call_count
        with self.assertRaisesRegex(contracts.AdapterError, '^adapter_review_changed$'):
            controller.approve(**{**arguments, 'candidate_hash': 'c' * 64})
        self.assertEqual(approval.invoke.call_count, before)

    def test_recovered_receipt_for_wrong_request_remains_unknown(self):
        client = Mock()
        def wrong_receipt(route, request):
            response = self.create_receipt(route, request)
            response['data']['request_key'] = str(uuid4())
            return {**response, 'reconciled': True}
        client.invoke.side_effect = wrong_receipt
        result = adapters.SecretAdapters(HOME, client=client).draft(
            slug='secret-adapter-fixture', title='Fixture adapter', document=definition(), owner_source=SOURCE)
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertEqual(result['request_key'], result['request']['request_key'])


SQL_ADAPTER_HELPERS = r"""
CREATE FUNCTION pg_temp.adapter_assert(actual jsonb, expected jsonb) RETURNS void LANGUAGE plpgsql AS $$
BEGIN IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'adapter SQL assertion failed: %', actual; END IF; END $$;
CREATE FUNCTION pg_temp.adapter_write(request jsonb) RETURNS void LANGUAGE plpgsql AS $$
DECLARE result jsonb;
BEGIN
 result:=maven.memory_write('write',request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),'fixture-owner');
 PERFORM pg_temp.adapter_assert(result->'status',NULL);
END $$;
CREATE FUNCTION pg_temp.adapter_publish(candidate jsonb, page uuid, base integer DEFAULT 0) RETURNS void LANGUAGE plpgsql AS $$
DECLARE request jsonb; result jsonb; challenge jsonb; proposal uuid:=gen_random_uuid(); hash text;
BEGIN
 hash:=encode(sha256(convert_to(candidate::text,'UTF8')),'hex');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',proposal,'page_id',page,'expected_revision',base,
   'classification','Sensitive','data_scope','owner','candidate',candidate,
   'source',jsonb_build_object('origin','fixture','excerpt','exact fixture'),'change_reason','Fixture publication');
 result:=maven.instruction_write('create',request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),hash,'fixture-owner');
 PERFORM pg_temp.adapter_assert(result->'status',NULL);
 challenge:=maven.instruction_challenge(proposal,'fixture-approver');
 request:=jsonb_build_object('request_key',gen_random_uuid(),'proposal_id',proposal,'challenge_id',challenge->'challenge_id',
   'candidate_hash',hash,'expected_revision',base,'approval_source',jsonb_build_object('origin','fixture','excerpt','Approve exact fixture'));
 result:=maven.instruction_write('resolve',request,encode(sha256(convert_to(request::text,'UTF8')),'hex'),NULL,'fixture-approver');
 PERFORM pg_temp.adapter_assert(result->'revision',to_jsonb(base+1));
END $$;
"""


@unittest.skipUnless(os.environ.get('MAVEN_MEMORY_TEST_DSN') and shutil.which('psql'),
                     'requires explicit disposable PostgreSQL DSN and psql')
class AdapterPostgreSQLTests(unittest.TestCase):
    def test_real_publication_catalog_binding_consumption_replay_expiry_and_revocation(self):
        from tools.memory import admin
        def literal(value):
            return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'::jsonb"
        def write_record(kind, data, record_id, *, tags=None, project=None, revision=0):
            body = {'request_key': str(uuid4()), 'record_id': record_id, 'expected_revision': revision,
                    'kind': kind, 'classification': 'Sensitive', 'data_scope': 'owner', 'data': data,
                    'source': SOURCE, 'change_reason': 'Offline adapter regression', 'tags': tags or []}
            if project:
                body['project_id'] = project
            return 'SELECT pg_temp.adapter_write(' + literal(body) + ');'
        def publish(document, page_id, revision=0):
            return f"SELECT pg_temp.adapter_publish({literal(document)},'{page_id}'::uuid,{revision});"
        statements = ['BEGIN;',
            "DO $$ BEGIN IF to_regnamespace('maven') IS NOT NULL THEN RAISE EXCEPTION 'refuse existing maven schema'; END IF; END $$;",
            'CREATE SCHEMA maven;']
        for _, _, migration in admin.migrations():
            statements.extend(statement + ';' for statement in migration)
        statements.extend([
            "INSERT INTO maven.service_state(singleton,instance_id,recovery_epoch,schema_version,data_scope,state,app_secret_arn,app_secret_version) "
            "VALUES(true,gen_random_uuid(),gen_random_uuid(),2,'owner_memory','ready','fixture-app','fixture-version');",
            "SELECT set_config('maven.client_protocol','4',true);", SQL_ADAPTER_HELPERS,
            write_record('entity', {'entity_type': 'project', 'name': 'Home', 'aliases': []}, HOME),
            write_record('setting', catalog_record()['data'], CATALOG_ID, project=HOME)])
        skill = {'slug': 'maven-secrets', 'page_kind': 'skill', 'title': 'Secrets', 'body_markdown': 'Fixture authority',
                 'scope': {}, 'authority_state': 'active', 'requires': ['fixture-secret-middle']}
        leaf = {**skill, 'slug': 'fixture-secret-leaf', 'requires': []}
        middle = {**skill, 'slug': 'fixture-secret-middle', 'requires': ['fixture-secret-leaf']}
        leaf_id = str(uuid4())
        statements.extend([publish(leaf, leaf_id), publish(middle, str(uuid4())),
                           publish(skill, str(uuid4())), publish(candidate(), str(uuid4()))])
        controller = adapters.SecretAdapters(HOME, client=Mock())
        attempt = invocation()
        grant = controller.grant_document(attempt, mode='single-use', expires_at='2099-01-01T00:00:00Z')
        grant_page_id = str(uuid4())
        grant_candidate = {**candidate(), 'slug': 'secret-grant-fixture', 'body_markdown': json.dumps(grant)}
        statements.append(publish(grant_candidate, grant_page_id))
        def consume(value, slug='secret-grant-fixture'):
            request = {'invocation': value, 'invocation_digest': contracts.invocation_digest(value),
                       'grant': slug, 'grant_revision': 1}
            return f"maven.secret_adapter_consume({literal(request)},'fixture-owner')"
        # Python and PostgreSQL must calculate the same exact invocation digest.
        statements.append(f"SELECT pg_temp.adapter_assert(to_jsonb(encode(sha256(convert_to(maven.secret_adapter_canonical({literal(attempt)}),'UTF8')),'hex')),"
                          f"{literal(contracts.invocation_digest(attempt))});")
        statements.append(f"SELECT pg_temp.adapter_assert({consume(attempt)}->'consumed','true');")
        statements.append(f"SELECT pg_temp.adapter_assert({consume(attempt)}->'status','\"claim_conflict\"');")
        different = {**attempt, 'invocation_id': str(uuid4())}
        statements.append(f"SELECT pg_temp.adapter_assert({consume(different)}->'status','\"not_authorized\"');")
        changed = {**attempt, 'destination': 'unapproved.invalid'}
        statements.append(f"SELECT pg_temp.adapter_assert({consume(changed)}->'status','\"not_authorized\"');")
        standing = controller.grant_document(different, mode='standing', expires_at='2099-01-01T00:00:00Z')
        standing_candidate = {**candidate(), 'slug': 'secret-grant-standing', 'body_markdown': json.dumps(standing)}
        standing_id = str(uuid4())
        statements.append(publish(standing_candidate, standing_id))
        statements.append(f"SELECT pg_temp.adapter_assert({consume(different, 'secret-grant-standing')}->'consumed','true');")
        # Revoking a transitive dependency leaves the immediate maven-secrets skill
        # active, but both fresh lookup and consumption must nevertheless fail.
        statements.append('SAVEPOINT revoked_transitive_dependency;')
        statements.append(publish({**leaf, 'authority_state': 'revoked'}, leaf_id, 1))
        lookup = {'home_id': HOME, 'slug': 'secret-adapter-fixture'}
        statements.append(f"SELECT pg_temp.adapter_assert(maven.secret_adapter_policy({literal(lookup)})->'status','\"dependency_unavailable\"');")
        unspent = {**attempt, 'invocation_id': str(uuid4())}
        statements.append(f"SELECT pg_temp.adapter_assert({consume(unspent, 'secret-grant-standing')}->'status','\"reference_unavailable\"');")
        statements.append(f"SELECT pg_temp.adapter_assert(to_jsonb((SELECT count(*) FROM maven.secret_adapter_consumptions WHERE invocation_id='{unspent['invocation_id']}'::uuid)),'0');")
        statements.append('ROLLBACK TO SAVEPOINT revoked_transitive_dependency; RELEASE SAVEPOINT revoked_transitive_dependency;')
        expired = {**standing, 'expires_at': '2000-01-01T00:00:00Z'}
        statements.append(publish({**candidate(), 'slug': 'secret-grant-expired', 'body_markdown': json.dumps(expired)}, str(uuid4())))
        another = {**attempt, 'invocation_id': str(uuid4())}
        statements.append(f"SELECT pg_temp.adapter_assert({consume(another, 'secret-grant-expired')}->'status','\"not_authorized\"');")
        statements.append(publish({**standing_candidate, 'authority_state': 'revoked'}, standing_id, 1))
        statements.append(f"SELECT pg_temp.adapter_assert({consume(another, 'secret-grant-standing')}->'status','\"reference_unavailable\"');")
        # Rotate the exact catalog record; even still-approved grants cannot use its old revision.
        rotated = catalog_record()['data']
        rotated['secret_ref'] = {'provider': 'ssm', 'name': '/fixture/test:2'}
        statements.append(write_record('setting', rotated, CATALOG_ID, project=HOME, revision=1))
        statements.append(f"SELECT pg_temp.adapter_assert({consume(attempt)}->'status','\"reference_unavailable\"');")
        statements.append('SET CONSTRAINTS ALL IMMEDIATE; ROLLBACK;')
        completed = subprocess.run([shutil.which('psql'), '-X', '-q', '-v', 'ON_ERROR_STOP=1'],
                                   input='\n'.join(statements), text=True, capture_output=True,
                                   env={**os.environ, 'PGDATABASE': os.environ['MAVEN_MEMORY_TEST_DSN']},
                                   timeout=90, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == '__main__':
    unittest.main()
