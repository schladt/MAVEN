"""Owner-reviewed local selection of an enabled project as MAVEN Home."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

from tools.setup_common import SetupPending, private_directory, save_json
from tools.uninstall.common import read_private

HOME_FILE = 'home.json'


class HomeError(ValueError):
    """An invalid Home selection; the message names the problem without secrets."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checkout(home: Path) -> Path | None:
    from tools.uninstall.local import MANIFEST
    manifest = home / MANIFEST
    if not manifest.exists():
        return None
    try:
        source = read_private(manifest).get('source')
    except (SetupPending, OSError, ValueError) as error:
        raise HomeError('Installation ownership metadata is unreadable; inspect it before choosing a Home folder') from error
    return Path(source).resolve() if source else None


def _inside(path: Path, parent: Path | None) -> bool:
    return parent is not None and (path == parent or parent in path.parents)


def validate_root(home: Path, root: Path) -> Path:
    """A Home root is an absolute folder outside the MAVEN home and checkout."""
    home = Path(home).absolute()
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise HomeError('Home folder must be an absolute path')
    root = root.resolve()
    if not root.is_dir():
        raise HomeError('Home folder does not exist')
    if root in (Path('/'), Path.home().resolve()):
        raise HomeError('Home folder cannot be / or the user home directory itself')
    if _inside(root, home.resolve()) or _inside(root, _checkout(home)):
        raise HomeError('Home folder cannot be inside the MAVEN installation or its source checkout')
    return root


def load_home(home: Path) -> dict | None:
    """The validated Home selection, None when not configured, or {'invalid': reason}."""
    from tools.project import ProjectError
    home = Path(home).absolute()
    path = home / HOME_FILE
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink():
            raise HomeError('home.json must not be a symlink')
        value = read_private(path)
        _validate_selection(home, value)
    except (HomeError, ProjectError, SetupPending) as error:
        return {'invalid': str(error)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'invalid': 'home.json is unreadable or malformed'}
    return value


def _validate_selection(home: Path, value: dict) -> None:
    from tools.project import load_project
    if value.get('schema') != 1 or not isinstance(value.get('label'), str) or not value['label'].strip():
        raise HomeError('home.json has an unsupported schema')
    root = validate_root(home, Path(value['root']))
    if str(root) != value['root']:
        raise HomeError('home.json root must be its resolved absolute path')
    project = load_project(root)
    if project['project_id'] != value.get('project_id'):
        raise HomeError('Home folder belongs to a different MAVEN project')
    if project['schema'] != 2 or project['instance_id'] != value.get('instance_id'):
        raise HomeError('Home belongs to a different MAVEN instance')
    if project['recovery_epoch'] != value.get('recovery_epoch'):
        raise HomeError('Home belongs to a different MAVEN recovery epoch')
    consent = value.get('consent')
    if (not isinstance(consent, dict) or not str(consent.get('origin', '')).strip()
            or not isinstance(consent.get('excerpt'), str) or not consent['excerpt'].strip()):
        raise HomeError('home.json lacks the recorded owner consent')


def save_home(home: Path, *, root: Path, project_id: str, instance_id: str, label: str,
              consent: dict, enabled_at: str | None = None, recovery_epoch: str | None = None) -> dict:
    from tools.project import load_project
    home = Path(home).absolute()
    root = validate_root(home, root)
    record = {'schema': 1, 'root': str(root), 'project_id': str(uuid.UUID(project_id)),
              'recovery_epoch': recovery_epoch if recovery_epoch is not None else load_project(root).get('recovery_epoch'),
              'instance_id': instance_id, 'label': label.strip(), 'enabled_at': enabled_at or _now(),
              'consent': {'origin': consent['origin'], 'excerpt': consent['excerpt']}}
    if not record['label']:
        raise HomeError('Home label is required')
    _validate_selection(home, record)
    private_directory(home)
    save_json(home / HOME_FILE, record)
    return record


def remove_home(home: Path) -> bool:
    path = Path(home).absolute() / HOME_FILE
    if not path.exists() and not path.is_symlink():
        return False
    path.unlink()
    return True


