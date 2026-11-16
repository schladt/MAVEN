"""Pure metadata contracts shared by the local secret controller and cloud service."""
import re

from .validation import fields, invalid, nonnegative_integer, revision, text, uuid_string

SECRET_ROUTES = {'/v1/memory/secret_lookup', '/v1/memory/secret_catalog'}
SECRET_MUTATIONS = {'/v1/memory/secret_catalog'}


def secret_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', value):
        invalid('secret name must be a lowercase namespace segment of at most 64 characters')
    return value


def validate_secret_ref(value):
    if not isinstance(value, dict):
        invalid('data.secret_ref must be an object')
    provider = value.get('provider')
    if provider in ('ssm', 'secretsmanager'):
        fields(value, {'provider', 'name'}, name='data.secret_ref')
        text(value['name'], 512, name='data.secret_ref.name')
        if not re.fullmatch(r'[A-Za-z0-9_./:@+=-]+', value['name']):
            invalid('data.secret_ref.name contains unsupported characters')
    elif provider == 'encrypted-file':
        fields(value, {'provider', 'name', 'bucket', 'key_id', 'store_id'}, {'version_id'}, name='data.secret_ref')
        if not isinstance(value['bucket'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', value['bucket']):
            invalid('data.secret_ref.bucket must be a valid bucket name')
        if not isinstance(value['key_id'], str) or not re.fullmatch(r'[0-9a-f]{16}', value['key_id']):
            invalid('data.secret_ref.key_id must be a 16-character wrapping key identifier')
        if not isinstance(value['store_id'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['store_id']):
            invalid('data.secret_ref.store_id must be a store binding digest')
        text(value['name'], 512, name='data.secret_ref.name')
        if (not re.fullmatch(r'(?:[A-Za-z0-9_.-]+/)+[a-z0-9][a-z0-9_.-]{0,63}', value['name'])
                or any(part in ('.', '..') for part in value['name'].split('/'))):
            invalid('data.secret_ref.name must be a relative immutable object key')
        if 'version_id' in value:
            text(value['version_id'], 1024, name='data.secret_ref.version_id')
            if not re.fullmatch(r'[A-Za-z0-9_.+/=-]+', value['version_id']):
                invalid('data.secret_ref.version_id contains unsupported characters')
    else:
        invalid('data.secret_ref.provider must be ssm, secretsmanager or encrypted-file')
    return value


def validate_consumer_ref(value):
    """The executable store's compatibility gate; ordinary memory keeps legacy locators."""
    from .adapter_contracts import AdapterError
    validate_secret_ref(value)
    if value['provider'] not in ('ssm', 'encrypted-file'):
        raise AdapterError('unsupported_secret_provider')
    if value['provider'] == 'ssm' and not re.fullmatch(
            r'/(?:[A-Za-z0-9_.-]+/)*[a-z0-9][a-z0-9_.-]{0,63}:[1-9][0-9]*', value['name']):
        raise AdapterError('secret_reference_migration_required')
    return value


def validate_secret_request(route, value):
    from .validation import _memory
    if route == '/v1/memory/secret_lookup':
        fields(value, {'project_id', 'name'}, {'revision'})
        uuid_string(value['project_id'], name='project_id')
        secret_name(value['name'])
        if 'revision' in value:
            revision(value['revision'])
        return value
    if route != '/v1/memory/secret_catalog':
        invalid('unsupported secret catalog route')
    _memory('/v1/memory/write', value)
    if value['kind'] != 'setting' or 'project_id' not in value or value['data'].get('scope') != 'project':
        invalid('secret catalog writes require exactly one project setting')
    if 'authority_generation' not in value:
        invalid('secret catalog writes require current authority_generation')
    key = value['data']['key']
    if not key.startswith('secret:'):
        invalid('secret catalog key must start with secret:')
    name = secret_name(key[7:])
    if 'secret_ref' not in value['data']:
        invalid('secret catalog writes require a typed secret_ref')
    reference = validate_secret_ref(value['data']['secret_ref'])
    metadata = value['data']['value']
    fields(metadata, {'store_id', 'provider', 'storage_operation', 'storage_name'},
           {'version', 'domain', 'service', 'previous_metadata'}, name='secret catalog metadata')
    if not isinstance(metadata['store_id'], str) or not re.fullmatch(r'[0-9a-f]{64}', metadata['store_id']):
        invalid('secret catalog metadata requires an exact store binding digest')
    if metadata['provider'] not in ('ssm', 'encrypted-file') or metadata['provider'] != reference['provider']:
        invalid('secret catalog provider must match its reference')
    uuid_string(metadata['storage_operation'], name='storage_operation')
    secret_name(metadata['storage_name'])
    for field in ('domain', 'service'):
        if field in metadata:
            secret_name(metadata[field])
    if reference['provider'] == 'ssm':
        revision(metadata.get('version'), name='version')
        if not reference['name'].endswith('/' + metadata['storage_name'] + ':' + str(metadata['version'])):
            invalid('secret catalog SSM reference must pin the verified stored version')
    elif reference['store_id'] != metadata['store_id'] or not reference['name'].endswith('/' + metadata['storage_name']):
        invalid('secret catalog file reference must match its verified store and object')
    if metadata['provider'] == 'ssm' and metadata['storage_name'] != name:
        invalid('SSM catalog name must match its stored name')
    return value
