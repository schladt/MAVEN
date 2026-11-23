"""Offline workspace context and conditional, destination-bound capture."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from tools.coordination import ensure_available, project_binding_lock
from tools.project import ProjectError, instance_home, load_project, require_binding
from tools.reconcile import local_write, select_project
from tools.reconcile_local import LocalStore, StaleView
from tools.reconcile_records import digest
from tools.reconcile_redaction import Replica


class LocalApplication:
    """Open selected stores under binding and replica protection."""

    def __init__(self, *, home=None, env=None):
        self.home = (Path(home).expanduser().resolve() if home else
                     Path(env).expanduser().resolve().parent if env else instance_home())
        self.env = Path(env).expanduser().resolve() if env else self.home / '.env'
        if self.env.parent != self.home:
            raise ProjectError('environment must belong to selected instance home')
        self._unbound_client = None

    def close(self):
        if self._unbound_client is not None:
            self._unbound_client.close()
            self._unbound_client = None

    @contextmanager
    def local(self, cwd, *, project_id=None, use_home=False, delivering=False):
        selected = select_project(cwd, self.home, use_home=use_home)
        if not selected or project_id is not None and project_id != selected['project_id']:
            raise ProjectError('workspace does not select the requested project')
        root = Path(selected['root'])
        binding = load_project(root)
        replica = Replica(self.home, binding['instance_id'])
        with replica.locked(), project_binding_lock(self.home, root):
            ensure_available(self.home, root)
            if selected != select_project(cwd, self.home, use_home=use_home) or load_project(root) != binding:
                raise ProjectError('workspace selection changed')
            if self.env.exists() or self.env.is_symlink():
                from tools.memory.environment import load_environment
                values = load_environment(self.env)
                context = {k: values.get('MAVEN_MEMORY_' + k.upper()) for k in ('instance_id', 'recovery_epoch')}
                if not delivering or context['instance_id'] != binding['instance_id']:
                    require_binding(binding, context)
                elif context['recovery_epoch'] != binding['recovery_epoch']:
                    self.close()  # Never deliver markers using a cached client from an earlier epoch.
            store = LocalStore(root, selected['project_id'])
            store.selection = selected
            store.binding = binding
            store.replica = replica
            replica.protect(store, delivering=delivering)
            yield store


def _cohort(binding):
    return binding['instance_id'], binding['recovery_epoch']


@contextmanager
def _destination(app, cwd, tracked, selected, *, active, cohort=None):
    # Select tracked Home through the workspace, not via an independently mutable Home pointer.
    via_home = selected != tracked
    with app.local(cwd, project_id=selected['project_id'], use_home=via_home) as store:
        if store.selection != selected or via_home and select_project(cwd, app.home) != tracked:
            raise ProjectError('workspace selection changed')
        binding = store.binding
        identity = _cohort(binding)
        if cohort is not None and identity != cohort:
            raise ProjectError('Home does not belong to the active instance and recovery epoch')
        # The SQLite token alone is insufficient: restored/copied worktrees can share it,
        # and a preferences-only Nexus view cannot stand in for an active project's brief.
        scope = digest(['workspace-context-v1', str(app.home), str(store.root), store.project_id, identity,
                        selected['mode'], active, tracked['root'] if tracked else str(Path(cwd).resolve()),
                        tracked['project_id'] if tracked else None])
        yield store, scope, identity


def _view(app, cwd, tracked, selected, *, active, cohort=None):
    with _destination(app, cwd, tracked, selected, active=active, cohort=cohort) as (store, scope, identity):
        nexus = selected['mode'] == 'home'
        view = store.context_view(active=active, nexus=nexus)
        return {**selected, **view, 'token': scope + '.' + view['token'], 'use_home': nexus}, identity


def consult(app, cwd):
    """Load one workspace context; the caller retains it until a refresh is needed."""
    result = {'status': 'unavailable', 'active': None, 'nexus': None, 'cloud_checked': False}
    error = 'project_unavailable'
    try:
        tracked = select_project(cwd, app.home)
        if tracked is None:
            error = 'nexus_unavailable'
        selected = tracked or select_project(cwd, app.home, use_home=True)
        active, identity = _view(app, cwd, tracked, selected, active=True)
        result['active'] = active
        if selected['mode'] == 'home':
            nexus = 'same_as_active'
        else:
            error = 'nexus_unavailable'
            selected = select_project(cwd, app.home, use_home=True)
            nexus, _ = _view(app, cwd, tracked, selected, active=False, cohort=identity)
        return {**result, 'status': 'consulted', 'nexus': nexus}
    except Exception:
        # A damaged binding, guard, or unreadable store never enables a fallback. Do not
        # reflect file names or exception text across the workspace context boundary.
        return {**result, 'status': 'partial' if result['active'] is not None else 'unavailable', 'error': error}


def capture(app, cwd, *, project_id, use_home, expected_token, reason, kind=None, data=None, record_id=None, erase=False):
    """Capture at the explicitly selected destination; the caller obtains the owner's preference scope."""
    if type(use_home) is not bool or type(erase) is not bool:
        raise ValueError('invalid capture selection')
    if erase:
        if not record_id or kind is not None or data is not None:
            raise ValueError('erasure requires an existing exact ID and no kind or data')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise ValueError('erasure requires a reason')
    elif not isinstance(data, dict):
        raise ValueError('invalid capture selection or data')
    tracked = select_project(cwd, app.home)  # Never equate a broken binding with an untracked directory.
    selected = select_project(cwd, app.home, use_home=True) if use_home else tracked
    if not selected or selected['project_id'] != project_id:
        raise ProjectError('workspace does not select the requested project')
    active = tracked is None or tracked == selected
    preference = not erase and kind == 'setting' and data.get('category') == 'preference'
    if preference:
        scope = data.get('scope')
        if scope not in ('project', 'nexus') or not all(
                isinstance(data.get(key), str) and data[key].strip() for key in ('title', 'summary')):
            raise ValueError('a preference requires its owner-selected scope, title and summary')
        if (scope == 'nexus' and not use_home) or (scope == 'project' and not active):
            raise ProjectError('preference scope does not match the selected destination')
    elif not active:
        raise ProjectError('ordinary capture belongs to the active project')

    cohort = None
    if not active:
        # Even an explicit Nexus preference cannot escape a retired/inaccessible workspace.
        with _destination(app, cwd, tracked, tracked, active=True) as (_, _, identity):
            cohort = identity
    with _destination(app, cwd, tracked, selected, active=active, cohort=cohort) as (store, binding, _):
        prefix = binding + '.'
        if not isinstance(expected_token, str) or not expected_token.startswith(prefix):
            raise StaleView('workspace context is not bound to this destination; reload context')
        if record_id and preference:
            previous = store.lookup(record_id, expected_token[len(prefix):])
            if previous is not None:
                old = previous['record']
                if old.get('erased'):
                    raise ValueError('erased records cannot be restored')
                fields = old.get('data', {})
                if old.get('kind') == 'setting' and fields.get('category') == 'preference':
                    if fields.get('scope') != data['scope']:
                        raise ProjectError('a correction cannot change preference scope')
                elif not active:
                    raise ProjectError('only a Nexus preference may be corrected across project boundaries')
        result = local_write(store, expected_token[len(prefix):], kind, data, reason, record_id=record_id, erase=erase)
        return {**result, 'changed': bool(result['changed']), 'token': prefix + result['token']}
