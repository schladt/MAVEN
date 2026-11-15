"""Pure server-side contracts for owner-approved Home secret adapter procedures."""
from datetime import datetime
import hashlib
import json
import os
import re
import string
import uuid

POLICY_ROUTE = '/v1/memory/secret_adapter_policy'
CONSUME_ROUTE = '/v1/memory/secret_adapter_consume'
ADAPTER_ROUTES = frozenset({POLICY_ROUTE, CONSUME_ROUTE})
ADAPTER_WRITE_ROUTES = frozenset({CONSUME_ROUTE})
MAX_SECRET_BYTES = 64 * 1024 * 1024
MAX_POLICY_BYTES = 65536
_SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
_FIELD = re.compile(r'[a-z][a-z0-9_]{0,47}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_FORMATTER = string.Formatter()


class AdapterError(Exception):
    """Content-free error safe to return to a model."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code='invalid_adapter'):
    raise AdapterError(code) from None


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        _fail()


def _text(value, maximum=2048):
    try:
        valid = isinstance(value, str) and value.strip() and '\x00' not in value and len(value.encode('utf-8')) <= maximum
    except UnicodeError:
        valid = False
    if not valid:
        _fail()
    return value


def _slug(value):
    if not isinstance(value, str) or len(value) > 80 or not _SLUG.fullmatch(value):
        _fail()
    return value


def _uuid(value):
    try:
        if str(uuid.UUID(value)) != value:
            _fail()
    except (ValueError, TypeError, AttributeError):
        _fail()
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _canonical(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        result.encode('utf-8')
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _fail())
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _source(value):
    _fields(value, {'origin', 'excerpt'})
    _text(value['origin'], 160)
    _text(value['excerpt'], 2000)


def _ref(value):
    # Keep provider validation shared with the catalog/API instead of interpreting refs.
    from .validation import validate_secret_ref
    try:
        validate_secret_ref(value)
    except Exception:
        _fail()


def _templates(argument, names):
    _fields(argument, (), {'literal', 'template'})
    if len(argument) != 1:
        _fail()
    value = next(iter(argument.values()))
    try:
        valid = isinstance(value, str) and '\x00' not in value and len(value.encode('utf-8')) <= 4096
    except UnicodeError:
        valid = False
    if not valid:
        _fail()
    if 'template' in argument:
        try:
            for _, field, spec, conversion in _FORMATTER.parse(value):
                if field is not None and (field not in names or spec or conversion):
                    _fail()
        except ValueError:
            _fail()


def validate_definition(value):
    _fields(value, {'kind', 'version', 'executable', 'arguments', 'parameters', 'input',
                    'timeout_seconds', 'max_output_bytes', 'results'})
    if value['kind'] != 'secret-adapter' or type(value['version']) is not int or value['version'] != 1:
        _fail()
    executable = _text(value['executable'], 4096)
    if not os.path.isabs(executable) or os.path.normpath(executable) != executable:
        _fail()
    parameters = value['parameters']
    if not isinstance(parameters, dict) or len(parameters) > 32:
        _fail()
    for key, rule in parameters.items():
        if not isinstance(key, str) or not _FIELD.fullmatch(key) or key in {'secret_fd', 'purpose', 'operation', 'destination'}:
            _fail()
        _fields(rule, {'max_length'}, {'choices'})
        _integer(rule['max_length'], 1, 2048)
        if 'choices' in rule:
            if not isinstance(rule['choices'], list) or not 1 <= len(rule['choices']) <= 64:
                _fail()
            for choice in rule['choices']:
                _text(choice, rule['max_length'])
    _fields(value['input'], {'mode'})
    if value['input']['mode'] not in ('stdin', 'fd'):
        _fail()
    names = set(parameters) | {'purpose', 'operation', 'destination'}
    if value['input']['mode'] == 'fd':
        names.add('secret_fd')
    if not isinstance(value['arguments'], list) or len(value['arguments']) > 128:
        _fail()
    fd_used = False
    for argument in value['arguments']:
        _templates(argument, names)
        fd_used |= any(field == 'secret_fd' for _, field, _, _ in _FORMATTER.parse(argument.get('template', '')))
    if value['input']['mode'] == 'fd' and not fd_used:
        _fail()
    _integer(value['timeout_seconds'], 1, 300)
    _integer(value['max_output_bytes'], 1, 1048576)
    if not isinstance(value['results'], dict) or len(value['results']) > 32:
        _fail()
    for name, rule in value['results'].items():
        if not isinstance(name, str) or not _FIELD.fullmatch(name):
            _fail()
        _fields(rule, {'type'}, {'max_length', 'choices'})
        if rule['type'] not in ('string', 'integer', 'boolean'):
            _fail()
        if rule['type'] == 'string':
            _integer(rule.get('max_length'), 1, 4096)
        elif 'max_length' in rule:
            _fail()
        if 'choices' in rule:
            if not isinstance(rule['choices'], list) or not 1 <= len(rule['choices']) <= 64:
                _fail()
            for choice in rule['choices']:
                _result_value(choice, {k: v for k, v in rule.items() if k != 'choices'})
    if len(_canonical(value).encode()) > MAX_POLICY_BYTES:
        _fail()
    return value


def _binding(value):
    _slug(value['adapter'])
    if not value['adapter'].startswith('secret-adapter-'):
        _fail()
    _integer(value['adapter_revision'], 1, 2147483647)
    _text(value['secret_name'], 160)
    _ref(value['secret_ref'])
    _uuid(value['catalog_record_id'])
    _integer(value['catalog_revision'], 1, 2147483647)
    if not isinstance(value['store_id'], str) or not _DIGEST.fullmatch(value['store_id']):
        _fail()
    for key in ('purpose', 'operation', 'destination'):
        _text(value[key], 1024)
    if not isinstance(value['parameters'], dict) or len(value['parameters']) > 32:
        _fail()
    for key, item in value['parameters'].items():
        if not isinstance(key, str) or not _FIELD.fullmatch(key):
            _fail()
        _text(item, 2048)


BINDING_FIELDS = {'adapter', 'adapter_revision', 'secret_name', 'secret_ref', 'purpose',
                  'operation', 'destination', 'parameters', 'catalog_record_id',
                  'catalog_revision', 'store_id'}


def validate_invocation(value):
    _fields(value, BINDING_FIELDS | {'home_id', 'invocation_id'})
    _uuid(value['home_id'])
    _uuid(value['invocation_id'])
    _binding(value)
    return value


def invocation_digest(value):
    validate_invocation(value)
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def validate_grant(value):
    _fields(value, BINDING_FIELDS | {'kind', 'version', 'mode', 'expires_at'}, {'invocation_digest'})
    if value['kind'] != 'secret-grant' or type(value['version']) is not int or value['version'] != 1:
        _fail()
    _binding(value)
    if value['mode'] not in ('standing', 'single-use'):
        _fail()
    if value['mode'] == 'single-use':
        if not isinstance(value.get('invocation_digest'), str) or not _DIGEST.fullmatch(value['invocation_digest']):
            _fail()
    elif 'invocation_digest' in value:
        _fail()
    _text(value['expires_at'], 32)
    try:
        expiry = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None:
            _fail()
    except ValueError:
        _fail()
    return value


def validate_adapter_candidate(candidate):
    """Call from server proposal-create validation, after ordinary candidate validation.

    Reserved slugs cannot be used to publish unvalidated adapter/grant JSON. Returning
    None for other procedures leaves existing authority semantics unchanged.
    """
    slug = candidate.get('slug', '')
    if not slug.startswith(('secret-adapter-', 'secret-grant-')):
        return None
    if (candidate.get('page_kind') != 'procedure' or candidate.get('requires') != ['maven-secrets']
            or set(candidate.get('scope', {})) != {'project'}):
        _fail()
    _uuid(candidate['scope']['project'])
    _text(candidate.get('body_markdown'), MAX_POLICY_BYTES)
    document = _json(candidate['body_markdown'])
    (validate_definition if slug.startswith('secret-adapter-') else validate_grant)(document)
    return document


def validate_adapter_request(route, value):
    """API validator: AdapterError must become MemoryError('invalid_request')."""
    if route == POLICY_ROUTE:
        _fields(value, {'home_id', 'slug'})
        _uuid(value['home_id'])
        _slug(value['slug'])
        if not value['slug'].startswith(('secret-adapter-', 'secret-grant-')):
            _fail()
    elif route == CONSUME_ROUTE:
        _fields(value, {'invocation', 'invocation_digest', 'grant', 'grant_revision'})
        validate_invocation(value['invocation'])
        if value['invocation_digest'] != invocation_digest(value['invocation']):
            _fail()
        _slug(value['grant'])
        if not value['grant'].startswith('secret-grant-'):
            _fail()
        _integer(value['grant_revision'], 1, 2147483647)
    else:
        _fail()
    return value


def _result_value(value, rule):
    expected = {'string': str, 'integer': int, 'boolean': bool}[rule['type']]
    if type(value) is not expected:
        _fail('unsafe_adapter_result')
    if isinstance(value, str):
        try:
            valid = '\x00' not in value and len(value.encode()) <= rule['max_length']
        except UnicodeError:
            valid = False
        if not valid:
            _fail('unsafe_adapter_result')
    if type(value) is int and not -(2**53) < value < 2**53:
        _fail('unsafe_adapter_result')
    if 'choices' in rule and value not in rule['choices']:
        _fail('unsafe_adapter_result')
