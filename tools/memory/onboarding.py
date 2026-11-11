"""Offline new-owner proposals and exact approval requests; never contacts AWS."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from maven_resources import resource_path

from .client import MemoryClientError, read_json
from .validation import digest, fields, source, validate_create, validate_resolve


def _private_write(path: Path, document: dict) -> None:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise MemoryClientError('starter_path_unsafe')
    path = path.parent.resolve() / path.name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(document, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def catalog() -> tuple[list[dict], dict[str, str]]:
    """Bundled authority pages in dependency (activation) order, with exact candidates and source hashes."""
    catalog_path = resource_path('templates/authority/catalog.json')
    document = read_json(catalog_path)
    fields(document, {'schema', 'pages'})
    if type(document['schema']) is not int or document['schema'] != 1 or not isinstance(document['pages'], list):
        raise MemoryClientError('starter_catalog_invalid')
    pages = {}
    hashes = {'catalog.json': hashlib.sha256(catalog_path.read_bytes()).hexdigest()}
    for page in document['pages']:
        fields(page, {'slug', 'page_kind', 'title', 'requires'})
        from .validation import slug
        slug(page['slug'])
        if page['slug'] in pages:
            raise MemoryClientError('starter_catalog_invalid')
        body_path = resource_path('templates/authority/' + page['slug'] + '.md')
        raw = body_path.read_bytes()
        pages[page['slug']] = {**page, 'body_markdown': raw.decode('utf-8'),
                               'scope': {}, 'authority_state': 'active'}
        hashes[body_path.name] = hashlib.sha256(raw).hexdigest()
    if 'maven' not in pages or pages['maven']['page_kind'] != 'directive':
        raise MemoryClientError('starter_catalog_invalid')
    ordered, visiting, visited = [], set(), set()

    def visit(name):
        if name not in pages or name in visiting:
            raise MemoryClientError('starter_dependency_invalid')
        if name in visited:
            return
        visiting.add(name)
        for dependency in pages[name]['requires']:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in pages:
        visit(name)
    return [pages[name] for name in ordered], hashes


def write_drafts(destination: Path, drafts: list[dict], hashes: dict[str, str]) -> dict:
    """Write ordered proposal drafts and their manifest; refuses an existing destination."""
    destination = Path(destination).expanduser().absolute()
    if destination.is_symlink():
        raise MemoryClientError('starter_path_unsafe')
    destination = destination.parent.resolve() / destination.name
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    ordered = [draft['candidate']['slug'] for draft in drafts]
    manifest = {
        'schema': 1, 'status': 'review_required', 'authority_activated': False,
        'source_sha256': hashes, 'activation_order': ordered,
        'pages': [{
            'slug': draft['candidate']['slug'], 'file': draft['candidate']['slug'] + '.json',
            'proposal_id': draft['proposal_id'], 'page_id': draft['page_id'],
            'request_key': draft['request_key'], 'candidate_hash': digest(draft['candidate']),
        } for draft in drafts],
    }
    for draft in drafts:
        _private_write(destination / (draft['candidate']['slug'] + '.json'), draft)
    _private_write(destination / 'manifest.json', manifest)
    return {'status': 'review_required', 'path': str(destination),
            'authority_activated': False, 'activation_order': ordered}


def draft(candidate: dict, evidence: dict, *, page_id: str | None = None, expected_revision: int = 0,
          reason: str = 'Propose reviewed generic starter authority') -> dict:
    request = {
        'request_key': str(uuid.uuid4()), 'proposal_id': str(uuid.uuid4()),
        'page_id': page_id or str(uuid.uuid4()), 'expected_revision': expected_revision,
        'classification': 'Sensitive', 'data_scope': 'owner',
        'candidate': dict(candidate), 'source': dict(evidence), 'change_reason': reason,
    }
    validate_create(request)
    return request


def prepare(destination: Path, *, source_origin: str, source_excerpt: str) -> dict:
    """Generate a complete dependency-ordered starter set, without submitting it."""
    evidence = {'origin': source_origin, 'excerpt': source_excerpt}
    source(evidence, approval=True)
    candidates, hashes = catalog()
    return write_drafts(destination, [draft(candidate, evidence) for candidate in candidates], hashes)


def approval(draft_path: Path, challenge_path: Path, destination: Path, *,
             approval_origin: str, approval_excerpt: str) -> dict:
    """Bind owner-supplied approval provenance to one actual cloud challenge."""
    draft = validate_create(read_json(draft_path, maximum=1024 * 1024))
    response = read_json(challenge_path)
    if response.get('status') != 'ready' or not isinstance(response.get('data'), dict):
        raise MemoryClientError('starter_challenge_invalid')
    challenge = response['data']
    expected = {'proposal_id': draft['proposal_id'], 'page_id': draft['page_id'],
                'expected_revision': draft['expected_revision'], 'candidate_hash': digest(draft['candidate'])}
    if any(challenge.get(key) != value for key, value in expected.items()):
        raise MemoryClientError('candidate_confirmation_mismatch')
    try:
        expiry = datetime.fromisoformat(challenge['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError('expired')
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MemoryClientError('starter_challenge_expired_or_invalid') from None
    request = validate_resolve({
        'request_key': str(uuid.uuid4()), 'proposal_id': draft['proposal_id'],
        'challenge_id': challenge.get('challenge_id'), 'candidate_hash': expected['candidate_hash'],
        'expected_revision': draft['expected_revision'],
        'approval_source': {'origin': approval_origin, 'excerpt': approval_excerpt},
    })
    _private_write(destination, request)
    return {'status': 'approval_prepared', 'path': str(destination),
            'request_key': request['request_key'], 'candidate_hash': request['candidate_hash'],
            'authority_activated': False}
