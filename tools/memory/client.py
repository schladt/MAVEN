"""Explicit-profile, authenticated cloud memory client."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import random
import re
import time
import uuid

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import Session as BotocoreSession
import requests

from .validation import CLIENT_PROTOCOL, MUTATION_ROUTES, ROUTES, SCHEMA_VERSION

REGION = 'us-east-1'
MAX_RESPONSE = 256 * 1024
CONFIG_KEYS = {'cluster_arn', 'app_secret_arn', 'admin_secret_arn', 'database',
               'journal_bucket', 'instance_id', 'recovery_epoch', 'region',
               'client_role_arn', 'admin_role_arn', 'approval_role_arn', 'endpoint', 'schema_version'}
OPTIONAL_CONFIG_KEYS = {'skill_bucket'}
DEFINITIVE_MUTATION_ERRORS = frozenset({
    'revision_conflict', 'invalid_request', 'not_authorized', 'claim_conflict',
    'project_archived', 'idempotency_conflict', 'setting_conflict',
    'reference_unavailable', 'session_unavailable',
})


class MemoryClientError(Exception):
    """A deliberately content-free error, safe to return through agent tools."""
    def __init__(self, code: str, *, request_key: str | None = None):
        super().__init__(code)
        self.code = code
        self.request_key = request_key


def read_json(path: Path, *, maximum: int = MAX_RESPONSE) -> dict:
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError('size')
        def reject_constant(value):
            raise ValueError('nonfinite')
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        body = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
        if not isinstance(body, dict):
            raise ValueError('object')
        return body
    except (OSError, ValueError, RecursionError):
        raise MemoryClientError('invalid_json_file') from None


def validate_config(config: dict) -> dict:
    try:
        if (not CONFIG_KEYS <= config.keys() or config.keys() - CONFIG_KEYS - OPTIONAL_CONFIG_KEYS
                or config['region'] != REGION or config['database'] != 'maven'):
            raise ValueError('keys')
        if type(config['schema_version']) is not int or config['schema_version'] != SCHEMA_VERSION:
            raise ValueError('version')
        for key in ['instance_id', 'recovery_epoch']:
            if str(uuid.UUID(config[key])) != config[key]:
                raise ValueError('uuid')
        if not re.fullmatch(r'https://[a-z0-9]+\.execute-api\.us-east-1\.amazonaws\.com', config['endpoint']):
            raise ValueError('endpoint')
        role = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]+)', config['client_role_arn'])
        if not role:
            raise ValueError('role')
        account = role[1]
        for key in ('admin_role_arn', 'approval_role_arn'):
            if not re.fullmatch(rf'arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+', config[key]):
                raise ValueError('role')
        if len({config[key] for key in ('client_role_arn', 'admin_role_arn', 'approval_role_arn')}) != 3:
            raise ValueError('separation')
        if not re.fullmatch(rf'arn:aws:rds:{REGION}:{account}:cluster:[a-zA-Z0-9-]+', config['cluster_arn']):
            raise ValueError('cluster')
        for key in ['app_secret_arn', 'admin_secret_arn']:
            if not re.fullmatch(rf'arn:aws:secretsmanager:{REGION}:{account}:secret:[A-Za-z0-9/_+=.@-]+', config[key]):
                raise ValueError('secret')
        if config['app_secret_arn'] == config['admin_secret_arn']:
            raise ValueError('separation')
        for key in ('journal_bucket', 'skill_bucket'):
            if key in config and not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', config[key]):
                raise ValueError('bucket')
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MemoryClientError('invalid_configuration') from None
    return dict(config)


def _credential_time():
    return datetime.now(timezone.utc)


def selected_session(config: dict, *, admin: bool = False, approval: bool = False, environment: dict | None = None):
    """Assume only the selected role, renewing through the same verified human login."""
    if admin and approval:
        raise MemoryClientError('invalid_role_selection')
    for name in ('boto3', 'botocore', 'urllib3'):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        from .environment import load_environment, selected_aws_session, transport_ca_bundle
        values = dict(load_environment() if environment is None else environment)
        region = config['region']
        purpose = 'admin' if admin else 'approval' if approval else 'client'
        role = config[f'{purpose}_role_arn']
        sdk_config = Config(connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1})

        def refresh():
            try:
                source = selected_aws_session(values, region=region)
                sts = source.client('sts', config=sdk_config, verify=transport_ca_bundle())
                try:
                    temporary = sts.assume_role(RoleArn=role, RoleSessionName=f'maven-memory-{purpose}',
                                                DurationSeconds=3600)['Credentials']
                finally:
                    sts.close()
                expiration = temporary['Expiration']
                if (not isinstance(expiration, datetime) or expiration.tzinfo is None
                        or expiration <= _credential_time()):
                    raise ValueError('expiration')
                metadata = {'access_key': temporary['AccessKeyId'],
                            'secret_key': temporary['SecretAccessKey'],
                            'token': temporary['SessionToken'], 'expiry_time': expiration.isoformat()}
                if any(not isinstance(value, str) or not value for value in metadata.values()):
                    raise ValueError('credentials')
                return metadata
            except (BotoCoreError, ClientError, KeyError, OSError, TypeError, ValueError, AttributeError):
                raise MemoryClientError('selected_login_or_role_unavailable') from None

        initial = refresh()
        credentials = RefreshableCredentials(
            access_key=initial['access_key'], secret_key=initial['secret_key'], token=initial['token'],
            expiry_time=datetime.fromisoformat(initial['expiry_time']), refresh_using=refresh,
            method='assume-role', time_fetcher=_credential_time,
            # Identity or login failure must fail closed even during early renewal.
            advisory_timeout=900, mandatory_timeout=900)
        sdk_session = BotocoreSession()
        sdk_session._credentials = credentials
        return boto3.Session(botocore_session=sdk_session, region_name=region)
    except (BotoCoreError, ClientError, KeyError, OSError, TypeError, ValueError):
        raise MemoryClientError('selected_login_or_role_unavailable') from None


class MemoryClient:
    def __init__(self, config: dict, session):
        self.config = validate_config(config)
        self.session = session
        self.http = requests.Session()
        self.http.trust_env = False  # Do not forward signed requests through an ambient proxy.
        from .environment import transport_ca_bundle
        self.http.verify = transport_ca_bundle()

    def close(self):
        self.http.close()
        self.session = None

    def _post(self, route: str, body: dict, remaining: float) -> tuple[int, dict]:
        url = self.config['endpoint'] + route
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        try:
            credentials = self.session.get_credentials().get_frozen_credentials()
            request = AWSRequest(method='POST', url=url, data=raw,
                                 headers={'Content-Type': 'application/json', 'x-maven-memory-protocol': str(CLIENT_PROTOCOL)})
            SigV4Auth(credentials, 'execute-api', REGION).add_auth(request)
            timeout = max(0.1, min(27.0, remaining / 2))
            with self.http.post(url, data=raw, headers=dict(request.headers), timeout=(min(3.0, timeout), timeout),
                                allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403):
                    return response.status_code, {'status': 'authentication_required' if response.status_code == 401 else 'not_authorized'}
                if response.status_code == 429:
                    return 429, {'status': 'rate_limited'}
                content = bytearray()
                for chunk in response.iter_content(8192):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise MemoryClientError('invalid_service_response')
                try:
                    result = json.loads(content)
                except (ValueError, RecursionError):
                    raise MemoryClientError('service_response_unavailable') from None
                if not isinstance(result, dict) or not isinstance(result.get('status'), str):
                    raise MemoryClientError('service_response_unavailable')
                return response.status_code, result
        except (requests.RequestException, BotoCoreError, AttributeError):
            raise MemoryClientError('transport_unavailable') from None

    def invoke(self, route: str, body: dict, *, budget: float = 90.0) -> dict:
        if route not in ROUTES:
            raise MemoryClientError('unsupported_operation')
        deadline = time.monotonic() + budget
        request_key = body.get('request_key') if route in MUTATION_ROUTES else None
        uncertain = False
        delay = 0.5
        while time.monotonic() < deadline:
            current_route = '/v1/operations/lookup' if uncertain else route
            current_body = {'request_key': request_key} if uncertain else body
            try:
                status, result = self._post(current_route, current_body, deadline-time.monotonic())
            except MemoryClientError as error:
                if error.code not in ('transport_unavailable', 'service_response_unavailable'):
                    raise MemoryClientError(error.code, request_key=request_key) from None
                if request_key:
                    uncertain = True
                else:
                    raise
            else:
                code = result['status']
                if status == 200 and code == 'ready' and isinstance(result.get('data'), dict):
                    return result
                if uncertain and status == 404:
                    # Absence may race an in-flight commit. Preserve the key for explicit same-request retry.
                    raise MemoryClientError('outcome_unknown', request_key=request_key)
                if request_key and ((status >= 500 and code not in ('warming', 'context_stale')) or status == 200):
                    uncertain = True
                elif status not in (429, 503) or code not in ('warming', 'temporarily_unavailable', 'rate_limited'):
                    allowed = DEFINITIVE_MUTATION_ERRORS | {
                        'record_unavailable', 'operation_unavailable', 'proposal_unavailable',
                        'page_unavailable', 'proposal_conflict', 'approval_invalid',
                        'authentication_required', 'context_stale', 'client_incompatible',
                        'dependency_unavailable', 'dependency_conflict', 'context_incomplete'}
                    raise MemoryClientError(code if code in allowed else 'service_failure', request_key=request_key)
            remaining = deadline-time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, random.uniform(delay/2, delay)))
            delay = min(5.0, delay*2)
        raise MemoryClientError('outcome_unknown' if uncertain else 'temporarily_unavailable', request_key=request_key)

    def read_full(self, body: dict, *, budget: float = 90.0) -> dict:
        """Assemble a complete instruction, pinning current reads and verifying its digest."""
        from .validation import validate_read
        request = dict(validate_read(body))
        if request.get('offset', 0) != 0:
            raise MemoryClientError('invalid_request')
        deadline = time.monotonic() + budget
        chunks = []
        first = None
        offset = 0
        byte_count = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryClientError('temporarily_unavailable')
            response = self.invoke('/v1/pages/read', {**request, 'offset': offset}, budget=remaining)
            data = response.get('data')
            if not isinstance(data, dict):
                raise MemoryClientError('invalid_service_response')
            chunk = data.get('body_markdown')
            total = data.get('total_characters')
            revision = data.get('revision')
            digest = data.get('body_sha256')
            if (not isinstance(chunk, str) or len(chunk) > 4096
                    or type(total) is not int or not 1 <= total <= 131072
                    or type(data.get('offset')) is not int or data['offset'] != offset
                    or type(revision) is not int or revision < 0
                    or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
                raise MemoryClientError('invalid_service_response')
            if first is None:
                if ('slug' in request and (data.get('slug') != request['slug'] or revision < 1)
                        or 'proposal_id' in request and (data.get('proposal_id') != request['proposal_id'] or revision != 0)):
                    raise MemoryClientError('invalid_service_response')
                requested_revision = request.get('revision', request.get('expected_revision'))
                if requested_revision is not None and revision != requested_revision:
                    raise MemoryClientError('revision_conflict')
                first = dict(data)
                if 'slug' in request and 'revision' not in request:
                    request['expected_revision'] = revision
            else:
                if revision != first['revision']:
                    raise MemoryClientError('revision_conflict')
                changing = {'body_markdown', 'offset', 'next_offset', 'authority_generation'}
                if ({key: value for key, value in data.items() if key not in changing}
                        != {key: value for key, value in first.items() if key not in changing}):
                    raise MemoryClientError('invalid_service_response')
            try:
                byte_count += len(chunk.encode('utf-8'))
            except UnicodeError:
                raise MemoryClientError('invalid_service_response') from None
            end = offset + len(chunk)
            next_offset = data.get('next_offset')
            if (not chunk or end > total or byte_count > 131072 or 'next_offset' not in data
                    or (end == total and next_offset is not None)
                    or (end < total and (type(next_offset) is not int or next_offset != end))):
                raise MemoryClientError('invalid_service_response')
            chunks.append(chunk)
            if next_offset is None:
                break
            offset = next_offset
        full_body = ''.join(chunks)
        if hashlib.sha256(full_body.encode('utf-8')).hexdigest() != first['body_sha256']:
            raise MemoryClientError('invalid_service_response')
        # The owner reviews state/scope/title as well as the body. A draft's
        # delivery state is not its proposed active/revoked state.
        from .validation import digest as candidate_digest
        try:
            candidate = {key: first[key] for key in ('slug', 'page_kind', 'title', 'scope')}
            candidate.update(body_markdown=full_body, authority_state=first['candidate_authority_state'])
            if 'requires' in first:
                candidate['requires'] = first['requires']
            if 'bundle' in first:
                candidate['bundle'] = first['bundle']
            if candidate_digest(candidate) != first['content_hash']:
                raise MemoryClientError('invalid_service_response')
        except (KeyError, ValueError, TypeError):
            raise MemoryClientError('invalid_service_response') from None
        return {**response, 'data': {**first, 'body_markdown': full_body, 'offset': 0, 'next_offset': None}}

    def load_instruction(self, slug: str, *, synthetic: bool = False, budget: float = 90.0,
                         context: dict | None = None) -> dict:
        """Load current skill with all applicable authority; synthetic mode is test-only."""
        if not synthetic:
            selectors = {} if context is None else dict(context)
            selectors['skill_slug'] = slug
            selectors.setdefault('workflow', slug)
            selectors.setdefault('operation', slug)
            response = self.load_context(selectors, budget=budget)
            selected = next((page for page in response['data']['instructions'] if page['slug'] == slug), None)
            if selected is None or selected.get('page_kind') != 'skill' or selected.get('data_scope') != 'owner':
                raise MemoryClientError('instruction_not_loadable')
            return {'status': 'ready', 'data': selected, 'context': response['data'], 'load_mode': 'owner_skill'}
        deadline = time.monotonic() + budget
        response = self.read_full({'slug': slug}, budget=budget)
        data = response['data']

        def require_skill(page):
            if (page.get('page_kind') != 'skill' or page.get('authority_state') != 'active'
                    or page.get('candidate_authority_state') != 'active'
                    or page.get('operative') is not True or page.get('data_scope') != 'synthetic'):
                raise MemoryClientError('instruction_not_loadable')

        require_skill(data)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MemoryClientError('temporarily_unavailable')
        # One current read, never a historical selector: a revocation or replacement
        # during assembly must fail rather than release the previously fetched body.
        current = self.invoke('/v1/pages/read', {
            'slug': slug, 'expected_revision': data['revision'], 'offset': 0,
        }, budget=remaining).get('data')
        if not isinstance(current, dict):
            raise MemoryClientError('invalid_service_response')
        if current.get('revision') != data['revision']:
            raise MemoryClientError('revision_conflict')
        require_skill(current)
        changing = {'body_markdown', 'offset', 'next_offset', 'authority_generation'}
        if ({key: value for key, value in current.items() if key not in changing}
                != {key: value for key, value in data.items() if key not in changing}):
            raise MemoryClientError('invalid_service_response')
        chunk = current.get('body_markdown')
        if (not isinstance(chunk, str) or not 1 <= len(chunk) <= 4096
                or current.get('offset') != 0 or type(current.get('offset')) is not int
                or chunk != data['body_markdown'][:len(chunk)]
                or 'next_offset' not in current
                or (len(chunk) == data['total_characters'] and current['next_offset'] is not None)
                or (len(chunk) < data['total_characters']
                    and (type(current['next_offset']) is not int or current['next_offset'] != len(chunk)))):
            raise MemoryClientError('invalid_service_response')
        return {**response, 'data': {**data, 'authority_generation': current.get('authority_generation')},
                'load_mode': 'synthetic_test_only'}

    def load_context(self, body: dict | None = None, *, budget: float = 90.0) -> dict:
        """Load complete applicable approved authority, rejecting mixed generations."""
        request = {} if body is None else dict(body)
        deadline = time.monotonic() + budget

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise MemoryClientError('temporarily_unavailable')
            return value

        response = self.invoke('/v1/memory/context', request, budget=remaining())
        context = response['data']
        generation = context.get('authority_generation')
        pages = context.get('pages')
        if (type(generation) is not int or generation < 0 or not isinstance(pages, list)
                or len(pages) > 100 or type(context.get('minimum_client_protocol')) is not int
                or context['minimum_client_protocol'] != CLIENT_PROTOCOL):
            raise MemoryClientError('invalid_service_response')
        catalog = {}
        for page in pages:
            if (not isinstance(page, dict) or not isinstance(page.get('slug'), str)
                    or page['slug'] in catalog or type(page.get('revision')) is not int
                    or page['revision'] < 1):
                raise MemoryClientError('invalid_service_response')
            if page.get('data_scope') != 'owner':
                raise MemoryClientError('instruction_not_loadable')
            catalog[page['slug']] = page
        ordered, visiting, visited = [], set(), set()

        def visit(slug):
            if slug not in catalog:
                raise MemoryClientError('dependency_unavailable')
            if slug in visiting:
                raise MemoryClientError('dependency_conflict')
            if slug in visited:
                return
            visiting.add(slug)
            dependencies = catalog[slug].get('requires', [])
            if (not isinstance(dependencies, list) or len(dependencies) > 32
                    or any(not isinstance(item, str) for item in dependencies)
                    or len(set(dependencies)) != len(dependencies)):
                raise MemoryClientError('invalid_service_response')
            for dependency in dependencies:
                visit(dependency)
            visiting.remove(slug)
            visited.add(slug)
            ordered.append(slug)

        for slug in catalog:
            visit(slug)
        instructions = []
        for slug in ordered:
            metadata = catalog[slug]
            page = self.read_full({'slug': slug, 'expected_revision': metadata['revision']},
                                  budget=remaining())['data']
            if page.get('authority_generation') != generation:
                raise MemoryClientError('context_stale')
            if (page.get('authority_state') != 'active' or page.get('candidate_authority_state') != 'active'
                    or page.get('operative') is not True or page.get('data_scope') != 'owner'
                    or page.get('page_kind') not in ('directive', 'skill', 'procedure')):
                raise MemoryClientError('instruction_not_loadable')
            if (any(page.get(key) != value for key, value in metadata.items())
                    or ('bundle' in page) != ('bundle' in metadata)):
                raise MemoryClientError('context_stale')
            instructions.append(page)
        current = self.invoke('/v1/memory/context', request, budget=remaining())['data']
        if (current.get('authority_generation') != generation or current.get('pages') != pages
                or current.get('minimum_client_protocol') != context['minimum_client_protocol']):
            raise MemoryClientError('context_stale')
        return {**response, 'data': {**current, 'instructions': instructions}}
