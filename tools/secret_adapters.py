"""Owner-approved Home secret procedures; no credentials enter tool arguments/results.

Definitions and grants are published instruction pages, never ordinary memory settings.
All execution authorization is fetched/consumed in the cloud immediately before reading
secret bytes. A consumed attempt is never replayable, including after transport failure.
The runner is not an OS sandbox: approval trusts the executable and its declared
operation/destination, not just its output schema.
"""
from __future__ import annotations

import base64
import hashlib
import os
import selectors
import signal
import subprocess
import tempfile
import time
import uuid
from .memory.adapter_contracts import (
    AdapterError, ADAPTER_ROUTES, ADAPTER_WRITE_ROUTES, BINDING_FIELDS, CONSUME_ROUTE,
    POLICY_ROUTE, MAX_SECRET_BYTES, _canonical, _fail, _integer, _json, _result_value,
    _slug, _source, _text, _uuid, invocation_digest, validate_adapter_candidate,
    validate_adapter_request, validate_definition, validate_grant, validate_invocation,
)
from .memory.client import DEFINITIVE_MUTATION_ERRORS

_PUBLICATION_REJECTIONS = DEFINITIVE_MUTATION_ERRORS | {
    'proposal_conflict', 'approval_invalid', 'authentication_required',
    'client_incompatible', 'proposal_unavailable',
}



def _secret_tokens(secret):
    return (secret, base64.b64encode(secret), base64.urlsafe_b64encode(secret), secret.hex().encode())


def _contains_secret(raw, tokens):
    return any(token and token in raw for token in tokens)


def _safe_results(raw, definition, secret):
    if not definition['results']:
        return {}
    try:
        document = _json(raw)
        if not isinstance(document, dict):
            _fail()
        result = {}
        tokens = _secret_tokens(secret)
        for name, rule in definition['results'].items():
            if name not in document:
                _fail()
            value = document[name]
            _result_value(value, rule)
            rendered = value.encode() if isinstance(value, str) else _canonical(value).encode()
            if _contains_secret(rendered, tokens) or _contains_secret(name.encode(), tokens):
                _fail()
            result[name] = value
        if _contains_secret(_canonical(result).encode(), tokens):
            _fail()
        return result
    except (AdapterError, UnicodeError):
        _fail('unsafe_adapter_result')


def _arguments(definition, invocation, fd=None):
    parameters = invocation['parameters']
    if parameters.keys() != definition['parameters'].keys():
        _fail('invalid_adapter_parameters')
    for name, rule in definition['parameters'].items():
        _text(parameters[name], rule['max_length'])
        if 'choices' in rule and parameters[name] not in rule['choices']:
            _fail('invalid_adapter_parameters')
    substitutions = {**parameters, **{key: invocation[key] for key in ('purpose', 'operation', 'destination')}}
    if fd is not None:
        substitutions['secret_fd'] = str(fd)
    return [definition['executable']] + [arg['literal'] if 'literal' in arg else arg['template'].format_map(substitutions)
                                        for arg in definition['arguments']]


def run_adapter(definition, invocation, secret):
    """Execute one already-authorized attempt. Never expose raw child diagnostics.

    Empty environment; private empty cwd; no shell, inherited descriptors or credential
    argv. Pipes are drained incrementally under a combined stdout/stderr byte ceiling.
    The session/process group is killed even on success, including surviving children.
    Programs deliberately escaping their process group require external OS sandboxing.
    """
    validate_definition(definition)
    validate_invocation(invocation)
    if not isinstance(secret, bytes) or not 1 <= len(secret) <= MAX_SECRET_BYTES:
        _fail('invalid_secret_size')
    child = None
    read_fd = write_fd = None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    try:
        read_fd, write_fd = os.pipe()
        argv = _arguments(definition, invocation, read_fd if definition['input']['mode'] == 'fd' else None)
        tokens = _secret_tokens(secret)
        if any(_contains_secret(arg.encode(), tokens) for arg in argv):
            _fail('credential_argument_forbidden')
        with tempfile.TemporaryDirectory(prefix='maven-adapter-') as directory:
            try:
                child = subprocess.Popen(argv, stdin=read_fd if definition['input']['mode'] == 'stdin' else subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=directory, env={},
                                         close_fds=True, pass_fds=(read_fd,) if definition['input']['mode'] == 'fd' else (),
                                         start_new_session=True)
                os.close(read_fd)
                read_fd = None
                for stream, label in ((child.stdout, 'stdout'), (child.stderr, 'stderr')):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, label)
                os.set_blocking(write_fd, False)
                selector.register(write_fd, selectors.EVENT_WRITE, 'input')
                offset = total = 0
                view = memoryview(secret)
                deadline = time.monotonic() + definition['timeout_seconds']
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _fail('adapter_timeout')
                    for key, _ in selector.select(min(remaining, 0.1)):
                        if key.data == 'input':
                            try:
                                offset += os.write(write_fd, view[offset:offset + 65536])
                            except BrokenPipeError:
                                offset = len(secret)
                            except BlockingIOError:
                                continue
                            if offset == len(secret):
                                selector.unregister(write_fd)
                                os.close(write_fd)
                                write_fd = None
                        else:
                            try:
                                chunk = os.read(key.fd, 65536)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                                continue
                            total += len(chunk)
                            if total > definition['max_output_bytes']:
                                _fail('adapter_output_limit')
                            if key.data == 'stdout':
                                stdout.extend(chunk)
                try:
                    code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    _fail('adapter_timeout')
                if code != 0:
                    _fail('adapter_failed')
            finally:
                if child is not None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                    for stream in (child.stdout, child.stderr):
                        stream.close()
        return {'status': 'completed', 'results': _safe_results(stdout, definition, secret)}
    except (OSError, ValueError, KeyError, UnicodeError, subprocess.SubprocessError):
        _fail('adapter_execution_unavailable')
    finally:
        selector.close()
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        stdout.clear()


class SecretAdapters:
    """Controller for explicit Home binding, approved publication and local execution.

    client: daily MemoryClient; approval_client: separately authorized approval role.
    catalog: SecretCatalog for this Home; store: SecretStore for the selected instance.
    No local policy cache is accepted. Sources are actual owner excerpts supplied by the
    caller, never synthesized by this controller. review() must be displayed verbatim.
    """

    def __init__(self, home_id, *, client, approval_client=None, catalog=None, store=None):
        self.home_id = _uuid(home_id)
        if catalog is not None and catalog.home_id != self.home_id:
            _fail('adapter_home_mismatch')
        self.client = client
        self.approval_client = approval_client
        self.catalog = catalog
        self.store = store

    def _identity(self, operation, payload):
        return str(uuid.uuid5(uuid.UUID(self.home_id), _canonical({'operation': operation, 'payload': payload})))

    @staticmethod
    def _publication_status(route, request, *, error=None):
        return {'status': 'failed' if error else 'outcome_unknown',
                **({'error': error} if error else {}),
                'route': route, 'request_key': request['request_key'],
                'proposal_id': request['proposal_id'],
                'request': _json(_canonical(request)),
                'recovery': (
                    'The service rejected this request; nothing was published by this attempt. '
                    'Correct the reported cause, then retry the identical request.'
                    if error else
                    'Publication is not confirmed. Preserve and reconcile this original request; '
                    'do not review or approve it as an existing draft, mint new IDs, or change the request.')}

    @staticmethod
    def _call(client, route, request):
        if client is None:
            _fail('adapter_authority_unavailable')
        publication = route in ('/v1/proposals/create', '/v1/proposals/resolve')
        try:
            result = client.invoke(route, request)
        except Exception as error:
            if publication:
                code = getattr(error, 'code', None)
                return SecretAdapters._publication_status(
                    route, request, error=code if code in _PUBLICATION_REJECTIONS else None)
            _fail('adapter_authority_unavailable')
        if (not isinstance(result, dict) or result.get('status') != 'ready'
                or not isinstance(result.get('data'), dict)):
            if publication:
                code = result.get('status') if isinstance(result, dict) else None
                return SecretAdapters._publication_status(
                    route, request, error=code if code in _PUBLICATION_REJECTIONS else None)
            _fail('adapter_authority_unavailable')
        # A recovered publication receipt is useful. A recovered consumption can
        # never authorize launching the executable again.
        if not publication and (result.get('reconciled') or result['data'].get('replayed')):
            _fail('adapter_authority_unavailable')
        return result['data']

    def policy(self, slug):
        request = {'home_id': self.home_id, 'slug': slug}
        validate_adapter_request(POLICY_ROUTE, request)
        data = self._call(self.client, POLICY_ROUTE, request)
        if data.get('slug') != slug or data.get('home_id') != self.home_id:
            _fail('adapter_authority_unavailable')
        _integer(data.get('revision'), 1, 2147483647)
        (validate_definition if slug.startswith('secret-adapter-') else validate_grant)(data.get('document'))
        return data

    def draft(self, *, slug, title, document, owner_source, page_id=None, expected_revision=0, revoke=False):
        """Persist an exact idempotent proposal; distinguish rejection from unknown outcome."""
        _slug(slug)
        _text(title, 160)
        _source(owner_source)
        _integer(expected_revision, 0, 2147483646)
        if type(revoke) is not bool or expected_revision and page_id is None or revoke and not expected_revision:
            _fail()
        if not slug.startswith(('secret-adapter-', 'secret-grant-')):
            _fail()
        candidate = {'slug': slug, 'page_kind': 'procedure', 'title': title, 'body_markdown': _canonical(document),
                     'scope': {'project': self.home_id}, 'authority_state': 'revoked' if revoke else 'active',
                     'requires': ['maven-secrets']}
        validate_adapter_candidate(candidate)
        page_id = _uuid(page_id) if page_id else self._identity('secret-procedure-page', {'slug': slug})
        request = {'page_id': page_id, 'expected_revision': expected_revision,
                   'classification': 'Sensitive', 'data_scope': 'owner', 'candidate': candidate,
                   'source': owner_source, 'change_reason': 'Owner-requested secret procedure publication'}
        proposal_id = self._identity('secret-procedure-proposal', request)
        request['proposal_id'] = proposal_id
        request['request_key'] = self._identity('secret-procedure-create', request)
        route = '/v1/proposals/create'
        data = self._call(self.client, route, request)
        candidate_hash = hashlib.sha256(_canonical(candidate).encode()).hexdigest()
        expected = {'proposal_id': proposal_id, 'page_id': page_id, 'expected_revision': expected_revision,
                    'candidate_hash': candidate_hash, 'request_key': request['request_key']}
        if data.get('status') in ('failed', 'outcome_unknown'):
            return {**data, 'page_id': page_id, 'candidate_hash': candidate_hash}
        if any(data.get(key) != value for key, value in expected.items()):
            return {**self._publication_status(route, request), 'page_id': page_id, 'candidate_hash': candidate_hash}
        return {'status': 'draft', **expected, 'candidate': candidate}

    def _draft(self, proposal_id):
        _uuid(proposal_id)
        try:
            response = self.client.read_full({'proposal_id': proposal_id})
            page = response['data']
        except Exception:
            _fail('adapter_authority_unavailable')
        if (page.get('proposal_id') != proposal_id or page.get('revision') != 0
                or page.get('data_scope') != 'owner' or page.get('scope') != {'project': self.home_id}):
            _fail('adapter_authority_unavailable')
        if validate_adapter_candidate(page) is None:
            _fail()
        return page

    def review(self, proposal_id):
        page = self._draft(proposal_id)
        challenge = self._call(self.approval_client, '/v1/proposals/challenge', {'proposal_id': proposal_id})
        if challenge.get('candidate_hash') != page.get('content_hash'):
            _fail('adapter_authority_unavailable')
        return {'status': 'owner_review_required', **challenge,
                'review': {key: page[key] for key in ('slug', 'page_kind', 'title', 'body_markdown', 'scope', 'requires')},
                'authority_state': page['candidate_authority_state'],
                'warning': 'Approval trusts this executable and exact operation/destination. Raw output is withheld; this is not an OS sandbox.'}

    def approve(self, *, proposal_id, challenge_id, candidate_hash, expected_revision, owner_source, approved):
        if approved is not True:
            _fail('owner_approval_required')
        _source(owner_source)
        _uuid(proposal_id)
        _uuid(challenge_id)
        _integer(expected_revision, 0, 2147483646)
        page = self._draft(proposal_id)
        if page.get('content_hash') != candidate_hash:
            _fail('adapter_review_changed')
        request = {'proposal_id': proposal_id, 'challenge_id': challenge_id,
                   'candidate_hash': candidate_hash, 'expected_revision': expected_revision,
                   'approval_source': owner_source}
        request['request_key'] = self._identity('secret-procedure-resolve', request)
        route = '/v1/proposals/resolve'
        data = self._call(self.approval_client, route, request)
        expected = {'proposal_id': proposal_id, 'page_id': page['page_id'],
                    'revision': expected_revision + 1, 'request_key': request['request_key']}
        if data.get('status') in ('failed', 'outcome_unknown'):
            return {**data, 'page_id': page['page_id'], 'candidate_hash': candidate_hash}
        if any(data.get(key) != value for key, value in expected.items()):
            return {**self._publication_status(route, request), 'page_id': page['page_id'], 'candidate_hash': candidate_hash}
        return data

    def prepare(self, *, adapter, secret_name, purpose, operation, destination, parameters):
        """Resolve exact catalog version and produce a nonsecret reviewable invocation."""
        policy = self.policy(adapter)
        if self.catalog is None or self.store is None:
            _fail('adapter_catalog_unavailable')
        try:
            record = self.catalog.lookup(secret_name)
            reference = record['data']['secret_ref']
        except Exception:
            _fail('adapter_catalog_unavailable')
        invocation = {'home_id': self.home_id, 'invocation_id': str(uuid.uuid4()), 'adapter': adapter,
                      'adapter_revision': policy['revision'], 'secret_name': secret_name, 'secret_ref': reference,
                      'purpose': purpose, 'operation': operation, 'destination': destination, 'parameters': parameters,
                      'catalog_record_id': record['record_id'], 'catalog_revision': record['revision'],
                      'store_id': self.store.store_id}
        validate_invocation(invocation)
        _arguments(policy['document'], invocation, 3 if policy['document']['input']['mode'] == 'fd' else None)
        return {'invocation': invocation, 'invocation_digest': invocation_digest(invocation)}

    def grant_document(self, invocation, *, mode, expires_at):
        validate_invocation(invocation)
        if invocation['home_id'] != self.home_id:
            _fail()
        document = {'kind': 'secret-grant', 'version': 1, 'mode': mode, 'expires_at': expires_at,
                    **{key: invocation[key] for key in BINDING_FIELDS}}
        if mode == 'single-use':
            document['invocation_digest'] = invocation_digest(invocation)
        return validate_grant(document)

    def execute(self, *, invocation, grant, grant_revision):
        validate_invocation(invocation)
        if invocation['home_id'] != self.home_id or self.store is None or self.catalog is None:
            _fail('adapter_authority_unavailable')
        policy = self.policy(invocation['adapter'])
        if policy['revision'] != invocation['adapter_revision']:
            _fail('adapter_revision_changed')
        _arguments(policy['document'], invocation, 3 if policy['document']['input']['mode'] == 'fd' else None)
        request = {'invocation': invocation, 'invocation_digest': invocation_digest(invocation),
                   'grant': grant, 'grant_revision': grant_revision}
        validate_adapter_request(CONSUME_ROUTE, request)
        authorization = self._call(self.client, CONSUME_ROUTE, request)
        if (authorization.get('consumed') is not True or authorization.get('invocation_id') != invocation['invocation_id']
                or authorization.get('invocation_digest') != request['invocation_digest']):
            _fail('adapter_authority_unavailable')
        # Once consumed, any failure means this attempt is spent. No receipt replays,
        # process retries, cached grants or recovered outcomes can launch it again.
        try:
            record = self.catalog.lookup(invocation['secret_name'])
            if (record['data']['secret_ref'] != invocation['secret_ref']
                    or record['record_id'] != invocation['catalog_record_id']
                    or record['revision'] != invocation['catalog_revision']
                    or self.store.store_id != invocation['store_id']):
                _fail('adapter_catalog_changed')
            secret = self.store.read_secret(invocation['secret_ref'])
        except AdapterError:
            raise
        except Exception:
            _fail('adapter_secret_unavailable')
        try:
            return run_adapter(policy['document'], invocation, secret)
        finally:
            del secret
