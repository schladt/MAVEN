"""Explicit one-secret Home catalog writes, independent of memory session capture."""
from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import sys
import uuid

from tools.aws.secret_store import DEFAULT_STREAM_LIMIT, MAX_VALUE_BYTES, SecretStore, valid_name, _operation
from tools.aws.secrets import StorageRejected, StoreError, open_input
from tools.coordination import private_lock
from tools.home import load_home
from tools.memory.boundary import private_path, read_private, save_private
from tools.memory.client import MemoryClientError
from tools.memory.environment import configured_client, load_environment
from tools.memory.validation import digest, source as validate_source, validate

_MANAGED_METADATA = {'store_id', 'provider', 'storage_operation', 'storage_name', 'version', 'domain', 'service'}


class SecretCatalog:
    def __init__(self, home, client=None, store=None):
        self.home = Path(home).expanduser().absolute()
        self.values = load_environment(self.home / '.env')
        selected = load_home(self.home)
        if not selected or selected.get('invalid'):
            raise StoreError('select a valid MAVEN Home before using its secret catalog')
        for field in ('instance_id', 'recovery_epoch'):
            if not selected.get(field) or self.values.get('MAVEN_MEMORY_' + field.upper()) != selected[field]:
                raise StoreError('Home secret catalog does not match the selected instance and recovery epoch')
        self.recovery_epoch = selected['recovery_epoch']
        self.home_id = selected['project_id']
        self.instance_id = selected['instance_id']
        self.store = store or SecretStore(self.values)
        self.client = client or configured_client(self.home / '.env', values=self.values)
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self.client.close()

    @staticmethod
    def _result(response):
        if not isinstance(response, dict) or response.get('status') != 'ready' or not isinstance(response.get('data'), dict):
            raise StoreError('secret catalog service returned no verified response')
        return response['data']

    def lookup(self, name, *, revision=None):
        valid_name(name)
        request = {'project_id': self.home_id, 'name': name}
        if revision is not None:
            request['revision'] = revision
        data = self._result(self.client.invoke('/v1/memory/secret_lookup', request))
        if 'record' not in data:
            raise StoreError('secret catalog lookup response is incomplete')
        record = data['record']
        if record is not None and (not isinstance(record, dict) or record.get('kind') != 'setting'
                or record.get('data_scope') != 'owner' or record.get('project_id') != self.home_id
                or record.get('project_archived') is True or record.get('data', {}).get('key') != 'secret:' + name
                or record.get('data', {}).get('scope') != 'project'
                or revision is not None and record.get('revision') != revision):
            raise StoreError('secret catalog scope or revision does not match the request')
        return record

    def _path(self, operation_id):
        return 'runtime/secrets/operations/' + _operation(operation_id) + '.json'

    def _save(self, state):
        save_private(self.home, self._path(state['operation_id']), state)

    def _load(self, operation_id):
        state = read_private(self.home, self._path(operation_id))
        if (state.get('schema') != 1 or state.get('operation_id') != operation_id
                or state.get('recovery_epoch') != self.recovery_epoch
                or state.get('home_id') != self.home_id or state.get('instance_id') != self.instance_id
                or state.get('store_id') != self.store.store_id):
            raise StoreError('secret operation belongs to a different Home, instance or store')
        return state

    @staticmethod
    def _status(state):
        result = {key: state[key] for key in ('operation_id', 'state', 'home_id', 'store_id', 'name')}
        if state.get('receipt'):
            result.update(storage='stored', storage_receipt=state['receipt'])
        else:
            result['storage'] = 'failed' if state['state'] == 'storage_failed' else 'outcome_unknown'
        result['catalog'] = 'saved' if state['state'] == 'completed' else 'pending'
        if state.get('catalog_ref'):
            result['catalog_ref'] = state['catalog_ref']
        if state.get('error'):
            result['error'] = state['error']
        if state.get('conflict'):
            result['conflict'] = state['conflict']
        if state['state'] != 'completed':
            result['next_action'] = (
                'Inspect the failed storage operation; no catalog was written.'
                if state['state'] == 'storage_failed' else
                'Review conflict.current and storage_receipt. To deliberately repoint the catalog, use resolve_catalog '
                'with this operation_id, the reviewed expected_revision, and the actual owner source; no secret is re-uploaded.'
                if state['state'] == 'catalog_conflict' else
                'Retry catalog only with this operation_id; storage is never re-uploaded.')
        return result

    @staticmethod
    def _input(source_path, from_stdin):
        if from_stdin:
            if source_path is not None or sys.stdin.isatty():
                raise StoreError('stdin requires a binary pipe and no source path')
            return nullcontext(sys.stdin.buffer)
        if not source_path:
            raise StoreError('provide an explicit owner-private input file or CLI stdin')
        return open_input(source_path, private=True)

    def _rotation_prior(self, prior, name, provider):
        old_meta = prior['data']['value']
        old_ref = prior['data'].get('secret_ref', {})
        if old_ref.get('provider') != provider:
            raise StoreError('rotation must preserve the catalog provider')
        if isinstance(old_meta, dict) and old_meta.get('store_id', self.store.store_id) != self.store.store_id:
            raise StoreError('catalog entry belongs to a different secret store')
        if provider == 'ssm' and old_ref.get('name', '').split(':')[0] != self.store.parameter(name):
            raise StoreError('catalog parameter does not match the selected store/name')
        if provider == 'encrypted-file' and old_ref.get('store_id') != self.store.store_id:
            raise StoreError('migrate the existing file catalog reference before rotation')

    def store_and_catalog(self, name, *, source_path=None, from_stdin=False, provider='ssm', replace=False,
                          operation_id=None, domain=None, service=None, source, max_bytes=DEFAULT_STREAM_LIMIT):
        valid_name(name)
        validate_source(source)
        if provider not in ('ssm', 'encrypted-file') or type(replace) is not bool:
            raise StoreError('select ssm or encrypted-file and an explicit replace choice')
        for label in (domain, service):
            if label is not None:
                valid_name(label)
        operation_id = _operation(operation_id)
        intent = {'name': name, 'provider': provider, 'replace': replace, 'domain': domain,
                  'service': service, 'source': source, 'max_bytes': max_bytes}
        with private_lock(self.home, 'runtime/secrets/' + operation_id + '.lock'):
            if private_path(self.home, self._path(operation_id)).exists():
                state = self._load(operation_id)
                if state['intent'] != intent:
                    raise StoreError('operation_id is bound to the original store-and-catalog request')
                return self._status(state)
            prior = self.lookup(name)
            if bool(prior) != replace:
                raise StoreError('use replace only for an existing catalog entry; creation never overwrites one')
            if prior:
                self._rotation_prior(prior, name, provider)
            physical = (name[:30] + '.v' + uuid.UUID(operation_id).hex
                        if provider == 'encrypted-file' and replace else name)
            state = {'schema': 1, 'operation_id': operation_id, 'home_id': self.home_id,
                     'instance_id': self.instance_id, 'store_id': self.store.store_id,
                     'recovery_epoch': self.recovery_epoch,
                     'name': name, 'physical_name': physical, 'intent': intent, 'prior': prior,
                     'state': 'storing', 'catalog_attempt': 0}
            # Verify current skill authority before storing anything. This path fetches
            # instructions only, never ordinary Home memory or a session/handover.
            self._authority()
            with self._input(source_path, from_stdin) as stream:
                self._save(state)
                try:
                    if provider == 'ssm':
                        value = stream.read(MAX_VALUE_BYTES + 1)
                        receipt = self.store.put(name, value, replace=replace, operation_id=operation_id)
                    else:
                        receipt = self.store.put_file(physical, stream, max_bytes=max_bytes, operation_id=operation_id)
                except Exception as error:
                    code = getattr(error, 'response', {}).get('Error', {}).get('Code')
                    definitive = isinstance(error, StorageRejected) or code in {
                        'ParameterAlreadyExists', 'ParameterNotFound', 'AccessDenied',
                        'AccessDeniedException', 'ValidationException', 'PreconditionFailed'}
                    state.update(state='storage_failed' if definitive else 'storage_uncertain',
                                 error='storage_rejected' if definitive else 'storage_outcome_unknown')
                    self._save(state)
                    return self._status(state)
            state.update(receipt=receipt, state='catalog_pending')
            self._save(state)
            return self._catalog(state)

    def _authority(self):
        result = self.client.load_instruction('maven-secrets', context={
            'project_id': self.home_id, 'instructions_only': True})
        generation = result.get('context', {}).get('authority_generation')
        if type(generation) is not int or generation < 0:
            raise StoreError('current secret-store authority is unavailable')
        return generation

    def retry_catalog(self, operation_id):
        operation_id = _operation(operation_id)
        with private_lock(self.home, 'runtime/secrets/' + operation_id + '.lock'):
            state = self._load(operation_id)
            if state['state'] in ('completed', 'storage_failed'):
                return self._status(state)
            if not state.get('receipt'):
                receipt = self.store.reconcile(state['physical_name'], state['intent']['provider'], operation_id)
                if receipt is None:
                    state.update(state='storage_uncertain', error='storage_outcome_unknown')
                    self._save(state)
                    return self._status(state)
                state.update(receipt=receipt, state='catalog_pending')
                state.pop('error', None)
                self._save(state)
            return self._catalog(state)

    def resolve_catalog(self, operation_id, *, expected_revision, source):
        """Explicitly review a definitive CAS conflict; reuse storage, not rejected approval."""
        validate_source(source)
        if type(expected_revision) is not int or expected_revision < 0:
            raise StoreError('expected_revision must be the exact reviewed catalog revision')
        operation_id = _operation(operation_id)
        with private_lock(self.home, 'runtime/secrets/' + operation_id + '.lock'):
            state = self._load(operation_id)
            resolution = {'expected_revision': expected_revision, 'source': source}
            if state.get('resolution') == resolution and state['state'] != 'catalog_conflict':
                return self._status(state) if state['state'] == 'completed' else self._catalog(state)
            if state['state'] != 'catalog_conflict' or not state.get('receipt') or not state.get('catalog_request'):
                raise StoreError('retry the original catalog request to establish a definitive revision conflict first')
            current = self.lookup(state['name'])
            reviewed = state['conflict']['current']
            if current != reviewed or expected_revision != (current['revision'] if current else 0):
                raise StoreError('catalog changed since conflict review; retry_catalog and review the current record')
            if current:
                self._rotation_prior(current, state['name'], state['intent']['provider'])
            state.setdefault('rejected_catalog_requests', []).append(state.pop('catalog_request'))
            state.setdefault('catalog_resolutions', []).append(resolution)
            state.update(resolution=resolution, catalog_prior=current, state='catalog_pending',
                         catalog_attempt=state['catalog_attempt'] + 1)
            state.pop('conflict', None)
            state.pop('error', None)
            self._save(state)
            return self._catalog(state)

    def _request(self, state, generation):
        receipt, prior = state['receipt'], state.get('catalog_prior', state['prior'])
        if receipt.get('store_id') != self.store.store_id or receipt.get('operation_id') != state['operation_id']:
            raise StoreError('storage receipt does not match the original operation')
        metadata = {'store_id': receipt['store_id'], 'provider': receipt['provider'],
                    'storage_operation': receipt['operation_id'], 'storage_name': receipt['name']}
        if 'version' in receipt:
            metadata['version'] = receipt['version']
        previous = prior['data']['value'] if prior else None
        if isinstance(previous, dict):
            legacy = {key: value for key, value in previous.items()
                      if key not in _MANAGED_METADATA and key != 'previous_metadata'}
            if 'previous_metadata' in previous:
                metadata['previous_metadata'] = previous['previous_metadata']
            if legacy:
                metadata['previous_metadata'] = {'original': metadata.get('previous_metadata'), **legacy}
        elif previous is not None:
            metadata['previous_metadata'] = previous
        for field in ('domain', 'service'):
            value = state['intent'][field]
            if value is None and isinstance(previous, dict):
                value = previous.get(field)
            if value is not None:
                metadata[field] = value
        tags = list(prior.get('tags', [])) if prior else []
        for tag in ('secret', 'secret-store:' + ('ssm' if receipt['provider'] == 'ssm' else 'file')):
            if tag not in tags:
                tags.append(tag)
        for field in ('domain', 'service'):
            if field in metadata:
                tags = [tag for tag in tags if not tag.startswith(field + ':')]
                tags.append(field + ':' + metadata[field])
        return {'request_key': str(uuid.uuid5(uuid.UUID(state['operation_id']),
                                            'catalog:' + str(state['catalog_attempt']))),
                'record_id': prior['record_id'] if prior else str(uuid.uuid5(uuid.UUID(self.home_id), 'secret:' + state['name'])),
                'expected_revision': prior['revision'] if prior else 0, 'kind': 'setting',
                'classification': 'Sensitive', 'data_scope': 'owner', 'project_id': self.home_id,
                'data': {'key': 'secret:' + state['name'], 'scope': 'project', 'value': metadata,
                         'secret_ref': receipt['secret_ref']}, 'tags': tags,
                'source': state.get('resolution', {}).get('source', state['intent']['source']),
                'change_reason': 'Explicit store-and-catalog: verified storage receipt', 'authority_generation': generation}

    def _catalog(self, state):
        try:
            if 'catalog_request' not in state:
                request = self._request(state, self._authority())
                validate('/v1/memory/secret_catalog', request)
                state['catalog_request'] = request
                self._save(state)
            request = state['catalog_request']
            result = self._result(self.client.invoke('/v1/memory/secret_catalog', request))
            if (result.get('record_id') != request['record_id'] or result.get('request_key') != request['request_key']
                    or result.get('revision') != request['expected_revision'] + 1):
                raise StoreError('catalog receipt does not match the original request')
            record = self.lookup(state['name'], revision=result['revision'])
            if (record is None or record.get('record_id') != request['record_id']
                    or record.get('data') != request['data'] or record.get('tags') != request['tags']):
                raise StoreError('catalog receipt could not be verified at its exact revision')
        except Exception as error:
            code = getattr(error, 'code', None) or getattr(error, 'status', None) or 'catalog_unavailable'
            state.update(state='catalog_pending', error=code)
            if code == 'revision_conflict' and 'catalog_request' in state:
                try:
                    current = self.lookup(state['name'])
                except Exception:
                    pass  # Keep the exact rejected request; retry can retrieve the review later.
                else:
                    state.update(state='catalog_conflict', conflict={'current': current,
                        'expected_revision': current['revision'] if current else 0,
                        'rejected_request_key': state['catalog_request']['request_key']})
            if code == 'context_stale' and 'catalog_request' in state:
                # Definitively rejected, not an uncertain write: refresh authority but
                # retain the original pointer CAS and storage receipt. Never re-upload.
                state.setdefault('rejected_catalog_requests', []).append(state.pop('catalog_request'))
                state['catalog_attempt'] += 1
            self._save(state)
            return self._status(state)
        state.update(state='completed', catalog_ref={'record_id': result['record_id'], 'revision': result['revision']})
        state.pop('error', None)
        state.pop('conflict', None)
        self._save(state)
        return self._status(state)


def main(argv=None):
    import argparse
    from tools.project import instance_home
    parser = argparse.ArgumentParser(description='Explicit store-and-catalog; no memory session is created')
    parser.add_argument('--home', type=Path, default=instance_home())
    commands = parser.add_subparsers(dest='action', required=True)
    store = commands.add_parser('store-and-catalog')
    store.add_argument('name')
    inputs = store.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--input', dest='source_path', type=Path)
    inputs.add_argument('--stdin', action='store_true', dest='from_stdin')
    store.add_argument('--provider', choices=('ssm', 'encrypted-file'), default='ssm')
    store.add_argument('--replace', action='store_true')
    store.add_argument('--operation-id')
    store.add_argument('--domain')
    store.add_argument('--service')
    store.add_argument('--max-bytes', type=int, default=DEFAULT_STREAM_LIMIT)
    retry = commands.add_parser('retry-catalog')
    retry.add_argument('operation_id')
    resolve = commands.add_parser('resolve-catalog')
    resolve.add_argument('operation_id')
    resolve.add_argument('--expected-revision', type=int, required=True)
    resolve.add_argument('--source-origin', required=True)
    resolve.add_argument('--source-excerpt', required=True)
    lookup = commands.add_parser('lookup')
    lookup.add_argument('name')
    args = parser.parse_args(argv)
    controller = None
    try:
        controller = SecretCatalog(args.home)
        if args.action == 'lookup':
            result = {'record': controller.lookup(args.name), 'home_id': controller.home_id}
        elif args.action == 'retry-catalog':
            result = controller.retry_catalog(args.operation_id)
        elif args.action == 'resolve-catalog':
            result = controller.resolve_catalog(args.operation_id, expected_revision=args.expected_revision,
                source={'origin': args.source_origin, 'excerpt': args.source_excerpt})
        else:
            options = vars(args).copy()
            options.pop('action')
            options.pop('home')
            result = controller.store_and_catalog(**options, source={
                'origin': 'maven-secret-catalog', 'excerpt': 'Explicit store-and-catalog command for ' + args.name})
        print(json.dumps(result))
        return 0 if result.get('state', 'completed') == 'completed' else 2
    except Exception as error:
        code = getattr(error, 'code', None) or getattr(error, 'status', None) or 'secret_catalog_unavailable'
        print(json.dumps({'error': code}), file=sys.stderr)
        return 2
    finally:
        if controller is not None:
            controller.close()
