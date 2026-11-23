"""Project-scoped helpers for the reconcile skill. No session, background loop or sync cursor."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import stat
from uuid import UUID, uuid4

from tools.project import ProjectError, load_project, project_label
from tools.reconcile_records import brief_id, combine_history, digest, make_change, tombstone
from tools.reconcile_local import StaleView


def select_project(cwd: str | Path, home: Path, *, use_home=False) -> dict | None:
    from tools.home import load_home

    folder = Path(cwd).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise ProjectError('supply an absolute workspace directory')
    folder = folder.resolve()
    selected = load_home(home) if (home / 'home.json').exists() or (home / 'home.json').is_symlink() else None
    if use_home:
        if not selected or 'invalid' in selected:
            raise ProjectError('Home is not configured for this installation')
        root = Path(selected['root'])
    else:
        from tools.project_switch import barrier_path
        switching = home / 'runtime/project-switches'
        root = None
        for candidate in (folder, *folder.parents):
            if switching.exists() and barrier_path(home, candidate).exists():
                raise ProjectError('folder switch in progress; do not select an enclosing project')
            binding = candidate / '.maven/project.json'
            try:
                mode = binding.parent.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise ProjectError('project configuration directory is unsafe or a symlink')
            try:
                binding_mode = binding.lstat().st_mode
            except FileNotFoundError:
                # A removed binding beside an existing record store is not an
                # untracked folder. Do not redirect its writes to an ancestor/Home.
                orphan = binding.parent / 'reconcile'
                if orphan.exists() or orphan.is_symlink():
                    raise ProjectError('local memory is missing its project binding')
            else:
                if not stat.S_ISREG(binding_mode):
                    raise ProjectError('project binding must be a regular file and not a symlink')
                root = candidate
                break
        if root is None:
            return None
    project = load_project(root)
    if project['schema'] != 2:
        raise ProjectError('legacy binding; review a project switch before using sessionless memory')
    is_home = bool(selected and selected.get('root') == str(root) and
                   all(selected.get(key) == project[key] for key in ('project_id', 'instance_id', 'recovery_epoch')))
    if use_home and not is_home:
        raise ProjectError('configured Home binding changed')
    return {'root': str(root), 'project_id': project['project_id'], 'mode': 'home' if is_home else 'project',
            'label': selected['label'] if is_home else project_label(root, project)}


def _by_id(inventory):
    return {item['id']: item for item in inventory['items']}


def local_write(store, expected_token, kind, data, reason, *, record_id=None, erase=False):
    if kind == 'brief' and not record_id:
        record_id = brief_id(store.project_id)
    if erase and (not record_id or kind is not None or data is not None):
        raise ValueError('erasure requires an existing exact ID and no kind or data')
    record_id = record_id or str(uuid4())
    prior = store.lookup(record_id, expected_token)
    if erase and prior is None:
        raise ValueError('unknown local record; read the affected record before erasing')
    if prior and prior['record'].get('erased') and not erase:
        raise ValueError('erased records cannot be restored')
    entry = (tombstone(prior, record_id=record_id) if erase else
             make_change(prior, kind, data, reason, record_id=record_id))
    result = store.apply([entry], expected_token)
    return {'status': 'local', 'id': record_id, **result, 'cloud_checked': False}


def _views(local, cloud, local_token=None, cloud_token=None):
    # The signed marker-only route remains available even when ordinary project
    # access is forbidden. Never read/disclose either live inventory or upload a
    # reviewed payload before durably accepting this delivery.
    if local.replica is not None:
        local.replica.receive(local, cloud)
    left = local.inventory()
    if local_token is not None and left['token'] != local_token:
        raise StaleView('local project changed; inspect again')
    right = cloud.inventory()
    if cloud_token is not None and right['token'] != cloud_token:
        raise StaleView('cloud project changed; inspect again')
    # A matching local token on both sides of the cloud read proves that the snapshots coexisted.
    local.read([], left['token'])
    return left, right


def inspect_views(local, cloud, *, verify=False, after_id=None, limit=100, local_token=None, cloud_token=None):
    if not 1 <= limit <= 200 or (after_id and not (local_token and cloud_token)):
        raise ValueError('invalid page or missing pinned view')
    left, right = _views(local, cloud, local_token, cloud_token)
    a, b = _by_id(left), _by_id(right)
    keys = sorted(a.keys() | b.keys())
    def same(key):
        return key in a and key in b and a[key]['digest'] == b[key]['digest']
    different = sum(not same(key) for key in keys)
    result = {'project_id': local.project_id, 'local_token': left['token'], 'cloud_token': right['token'],
              'total': len(keys), 'different': different,
              'status': 'synced' if not different else 'review_required'}
    if not different or verify:
        return result
    if after_id is not None and after_id not in a and after_id not in b:
        raise ValueError('unknown inventory page boundary')
    remaining = [key for key in keys if after_id is None or key > after_id]
    def item(key):
        if same(key):
            return {'both': a[key]}
        # A marker bars disclosure of the stale counterpart, including its summary.
        if a.get(key, {}).get('kind') == 'erased' or b.get(key, {}).get('kind') == 'erased':
            return {'id': key, 'erased': True}
        return {'id': key, 'local': a.get(key), 'cloud': b.get(key)}
    return {**result, 'items': [item(key) for key in remaining[:limit]],
            'next_after_id': remaining[limit - 1] if len(remaining) > limit else None}


def _details(local, cloud, ids, left, right):
    if not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids) or any(str(UUID(key)) != key for key in ids):
        raise ValueError('supply 1–50 distinct canonical record IDs')
    a, b = _by_id(left), _by_id(right)
    if any(key not in a and key not in b for key in ids):
        raise ValueError('unknown record in reviewed project')
    erased = {key for key in ids if a.get(key, {}).get('kind') == 'erased' or b.get(key, {}).get('kind') == 'erased'}
    def fetch(store, index, token):
        wanted = [key for key in ids if key in index and (key not in erased or index[key]['kind'] == 'erased')]
        records = store.read(wanted, token) if wanted else []
        by_id = {e['record']['id']: e for e in records}
        if by_id.keys() != set(wanted) or any(digest(e) != index[k]['digest'] for k, e in by_id.items()):
            raise StaleView('incomplete record snapshot')
        return by_id
    return fetch(local, a, left['token']), fetch(cloud, b, right['token'])


def read_details(local, cloud, ids, local_token, cloud_token):
    left, right = _views(local, cloud, local_token, cloud_token)
    a, b = _details(local, cloud, ids, left, right)
    return {'status': 'review', 'records': [{'id': key, 'local': a.get(key), 'cloud': b.get(key)} for key in ids]}


def _covers(entry, parents):
    """A previously accepted write may already contain the complete intended merge."""
    versions = {digest(h['record']) for h in entry['history']} | {digest(entry['record'])}
    entries = {digest(h) for h in entry['history']}
    return all(digest(p['record']) in versions and all(digest(h) in entries for h in p['history'])
               for p in parents)


def apply_review(local, cloud, choices, local_token, cloud_token):
    """Publish explicit semantic decisions, never pick the newer timestamp for the agent."""
    if not choices or not local_token or not cloud_token:
        raise ValueError('apply requires choices and both observed tokens')
    ids = [choice['id'] for choice in choices]
    left, right = _views(local, cloud, local_token, cloud_token)
    local_entries, cloud_entries = _details(local, cloud, ids, left, right)
    a, b = _by_id(left), _by_id(right)
    to_cloud, to_local = [], []
    for choice in choices:
        key, take = choice['id'], choice['take']
        parents = [entries[key] for entries in (local_entries, cloud_entries) if key in entries]
        markers = [entry for entry in parents if entry['record'].get('erased')]
        if markers:
            entry = min(markers, key=lambda e: (datetime.fromisoformat(e['record']['written_at']), e['record']['written_at']))
        else:
            if take == 'merge':
                matches = [p for p in parents if p['record']['kind'] == choice['kind']
                           and digest(p['record']['data']) == digest(choice['data'])]
                source = next((p for p in matches if _covers(p, parents)), matches[0] if matches else None)
                if source is None:
                    source = make_change(parents[0], choice['kind'], choice['data'], choice['reason'], record_id=key)
            elif take in ('local', 'cloud'):
                source = (local_entries if take == 'local' else cloud_entries).get(key)
                if source is None:
                    raise ValueError('chosen side has no such record')
            else:
                raise ValueError('choose local, cloud, merge, or propagate an existing erasure')
            entry = source if _covers(source, parents) else combine_history(source['record'], parents, choice['reason'])
        hashed = digest(entry)
        if a.get(key, {}).get('digest') != hashed:
            to_local.append(entry)
        if b.get(key, {}).get('digest') != hashed:
            to_cloud.append(entry)
    # CAS even when there is nothing to upload: a new unrelated cloud record invalidates this review.
    local.read([], local_token)
    result = cloud.apply(to_cloud, cloud_token)
    local_result = local.apply(to_local, local_token)
    return {'status': 'applied', 'verify_required': True, 'local_token': local_result['token'],
            'cloud_token': result['token'], 'ids': ids}
