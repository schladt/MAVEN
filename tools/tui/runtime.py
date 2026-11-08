"""Offline, verified runtime staging for UI self-removal and pinned workers.

The interactive process runs from a disposable private copy outside installation
homes. Worker dependencies persist beside the exact reviewed recovery runner.
Neither path downloads packages or changes the system interpreter.
"""
from __future__ import annotations

from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tools.interaction import checkpoint
from tools.uninstall.common import UninstallError, checked_path, file_digest, private_directory, read_private, save_private


_UI_START = """import sys
sys.dont_write_bytecode = True
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from tools.tui.uninstall import main
raise SystemExit(main())
"""
_WORKER_START = """import runpy, sys
sys.dont_write_bytecode = True
runner, libraries = sys.argv[1:3]
sys.path[:0] = [str(__import__('pathlib').Path(runner).parent), libraries]
sys.argv = [runner, *sys.argv[3:]]
runpy.run_path(runner, run_name='__main__')
"""


def _environment():
    environment = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _matching_python(homes=(), preferred=None):
    from tools.uninstall.cli import _external_python
    executable = _external_python(homes, preferred)
    result = subprocess.run([executable, "-I", "-c",
                             "import json,sys; print(json.dumps([list(sys.version_info[:2]), sys.implementation.cache_tag]))"],
                            capture_output=True, text=True, check=True, timeout=15)
    expected = [list(sys.version_info[:2]), sys.implementation.cache_tag]
    if json.loads(result.stdout) != expected:
        raise UninstallError("offline recovery runtime needs an external interpreter with the same Python minor version and ABI")
    return executable


def _distributions():
    # packaging is an explicit UI dependency; core CLI recovery never imports it.
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    queue = [Requirement("textual>=2.1,<3"), Requirement("packaging>=24,<27")]
    for name in ("boto3[crt]", "aws-encryption-sdk[MPL]", "requests"):
        requirement = Requirement(name)
        try:
            metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            continue  # A Textual-only installation can still remove local scopes.
        queue.append(requirement)
    seen, distributions = set(), {}
    while queue:
        requirement = queue.pop()
        name = canonicalize_name(requirement.name)
        key = (name, tuple(sorted(requirement.extras)))
        if key in seen:
            continue
        seen.add(key)
        try:
            distribution = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            raise UninstallError("installed UI/cloud dependency is missing: " + requirement.name) from None
        if requirement.specifier and not requirement.specifier.contains(distribution.version):
            raise UninstallError("installed dependency version is incompatible: " + requirement.name)
        distributions[name] = distribution
        extras = requirement.extras | {""}
        for value in distribution.requires or ():
            child = Requirement(value)
            if child.marker is None or any(child.marker.evaluate({"extra": extra}) for extra in extras):
                queue.append(child)
    return distributions


def _copy_file(source, target):
    before = file_digest(source)
    if target.exists():
        if file_digest(target) != before:
            raise UninstallError("conflicting installed dependency files; preserve the current runtime")
        return before
    private_directory(target.parent)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output, source.open("rb") as stream:
        shutil.copyfileobj(stream, output)
        output.flush()
        os.fsync(output.fileno())
    if file_digest(target) != before or file_digest(source) != before:
        raise UninstallError("installed dependency changed while staging; nothing was authorized for removal")
    return before


def _sync_directories(root):
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for path in [*sorted(directories, key=lambda item: len(item.parts), reverse=True), root, root.parent]:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _verify_runtime(root, plan_digest=None):
    private_directory(root)
    document = read_private(root / "manifest.json")
    if (document.get("schema") != 1 or document.get("plan_digest") != plan_digest
            or document.get("python") != [list(sys.version_info[:2]), sys.implementation.cache_tag]
            or not isinstance(document.get("files"), dict)):
        raise UninstallError("staged runtime identity or Python ABI changed; preserve the recovery artifacts")
    actual = set()
    site = root / "site"
    for path in site.rglob("*"):
        if path.is_symlink():
            raise UninstallError("staged runtime contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(site).as_posix())
    if actual != set(document["files"]):
        raise UninstallError("staged runtime file set changed")
    for name, expected in document["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or file_digest(site / relative) != expected:
            raise UninstallError("staged runtime file changed")
    return site


def _stage_dependencies(destination, *, plan_digest=None):
    if destination.exists() or destination.is_symlink():
        return _verify_runtime(destination, plan_digest)
    private_directory(destination.parent)
    temporary = Path(tempfile.mkdtemp(prefix=".ui-staging-", dir=destination.parent))
    try:
        site = private_directory(temporary / "site")
        manifest, versions = {}, {}
        for name, distribution in _distributions().items():
            versions[name] = distribution.version
            files = distribution.files
            if files is None:
                raise UninstallError("installed dependency has no file inventory: " + name)
            for item in files:
                relative = Path(item)
                # Console scripts and bytecode are not needed by the isolated
                # interpreter. Never copy external paths or executable .pth hooks.
                if (relative.is_absolute() or ".." in relative.parts or "__pycache__" in relative.parts
                        or relative.suffix in {".pyc", ".pth"}):
                    continue
                source = Path(distribution.locate_file(item)).absolute()
                if not source.exists():
                    raise UninstallError("installed dependency inventory is incomplete: " + name)
                manifest[relative.as_posix()] = _copy_file(source, site / relative)
                checkpoint()
        save_private(temporary / "manifest.json", {
            "schema": 1, "plan_digest": plan_digest,
            "python": [list(sys.version_info[:2]), sys.implementation.cache_tag],
            "distributions": versions, "files": manifest,
        })
        _sync_directories(temporary)
        temporary.rename(destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return destination / "site"
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def launch_uninstaller():
    """Launch a temporary protected UI, including when its installation is removed."""
    from tools.tui.bootstrap import run_terminal, ui_available
    from tools.uninstall.cli import _snapshot, _verify_runner
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The uninstall TUI requires a terminal; use plan/apply/resume/status for noninteractive recovery.", file=sys.stderr)
        return 2
    if not ui_available():
        print("The terminal interface needs the MAVEN [ui] dependencies. Activate the installed MAVEN environment "
              "or install its [ui] extra. Offline plan/apply/resume/status remain available without Textual; "
              "uninstall never downloads dependencies automatically.", file=sys.stderr)
        return 2
    python = _matching_python()
    # Cleanup belongs to this stdlib launcher, not to the process deleting its
    # original environment. The UI and every imported dependency live elsewhere.
    with tempfile.TemporaryDirectory(prefix="maven-uninstall-ui-") as temporary:
        root = Path(temporary).resolve()
        print("Preparing a private offline UI runtime outside the installation; it is removed when the TUI closes.")
        manifest = _snapshot(root / "runner")
        site = _stage_dependencies(root / "ui-runtime")
        _verify_runner({"runner": manifest}, root)
        _verify_runtime(root / "ui-runtime")
        environment = _environment()
        environment["MAVEN_TUI_RUNTIME_ROOT"] = str(root)
        return run_terminal([python, "-I", "-B", "-c", _UI_START,
                             str(root / "runner"), str(site)], env=environment)


def worker_command(plan_path, arguments: list[str]):
    """Return an isolated command for the *exact* reviewed implementation."""
    from tools.uninstall.cli import _load_plan, _verify_runner
    path, plan = _load_plan(plan_path)
    if plan.get("tui_worker_protocol") != 1:
        raise UninstallError("this original recovery snapshot predates TUI workers; use its exact CLI recovery command")
    _verify_runner(plan, path.parent)
    protected = Path(__file__).resolve().parents[2]
    roots = [Path(home) for home in plan["machine_homes"]]
    active_root = os.environ.get("MAVEN_TUI_RUNTIME_ROOT")
    if active_root and protected.is_relative_to(Path(active_root)):
        active = checked_path(active_root)
        if path.is_relative_to(active) or any(
                root.is_relative_to(active) or active.is_relative_to(root) for root in roots):
            raise UninstallError("recovery and removal targets must be separate from the disposable UI runtime")
    if any(location == root or location.is_relative_to(root)
           for root in roots for location in (protected, Path(sys.prefix).absolute())):
        raise UninstallError("the active TUI runtime is inside a removal target; relaunch the protected uninstall dashboard")
    python = _matching_python(plan["machine_homes"], plan["recovery_python"])
    site = _stage_dependencies(path.parent / "ui-runtime", plan_digest=plan["plan_digest"])
    _verify_runner(plan, path.parent)
    _verify_runtime(path.parent / "ui-runtime", plan["plan_digest"])
    return ([python, "-I", "-B", "-c", _WORKER_START,
             str(path.parent / "runner/run.py"), str(site), *arguments], _environment())
