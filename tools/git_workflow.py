#!/usr/bin/env python3
"""Explicit optional Git publication with exact approved candidate semantics."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import stat
import uuid
from typing import Any
from urllib.parse import urlsplit

from tools.project import ProjectError, instance_home, load_project, require_binding
from tools.recovery import DEFAULT_NEXT_ACTION
from tools.uninstall.common import UninstallError, private_directory, read_private, save_private

SCHEMA = 1

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_APPROVAL_REQUIRED = 3
EXIT_UNCERTAIN = 5
SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{24,}\b")),
    (
        "credential-assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9+/_.=-]{16,}"
        ),
    ),
)


class WorkflowFailure(ProjectError):
    """Safe publication stop; subprocess output and credential values are excluded."""


def git_environment():
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE'):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
    return env


def run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    text: bool = True,
    timeout: int = 120,
    input_data: str | bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    env = git_environment()
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=text,
            env=env,
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL if input_data is None else None,
            input=input_data,
        )
    except subprocess.TimeoutExpired:
        raise WorkflowFailure(f"command timed out: {args[0]}") from None
    if check and result.returncode != 0:
        raise WorkflowFailure(f"command failed: {args[0]} {args[1] if len(args) > 1 else ''}".rstrip())
    return result


def git(
    root: Path,
    *args: str,
    check: bool = True,
    text: bool = True,
    input_data: str | bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    return run(["git", *args], cwd=root, check=check, text=text, input_data=input_data)


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WorkflowFailure(f"missing {label}: {path}") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise WorkflowFailure(f"invalid {label}: {path}") from None
    if not isinstance(value, dict):
        raise WorkflowFailure(f"invalid {label}: expected JSON object")
    return value

def normalize_remote(value: str, root: Path | None = None) -> str | None:
    from tools.project import validate_accepted_url
    from urllib.parse import unquote
    try:
        value = validate_accepted_url(value)
    except ValueError:
        return None
    if value.startswith(('https://', 'http://', 'ssh://', 'file://')):
        parsed = urlsplit(value)
        if parsed.scheme == 'file':
            return str(Path(unquote(parsed.path)).resolve())
        user = (parsed.username + '@') if parsed.username else ''
        port = parsed.port or {'https': 443, 'http': 80, 'ssh': 22}[parsed.scheme]
        return f'{parsed.scheme}://{user}{parsed.hostname}:{port}{parsed.path}'
    path = Path(value).expanduser()
    if path.is_absolute() or value.startswith(('./', '../', '~/')):
        return str((path if path.is_absolute() else (root or Path.cwd()) / path).resolve())
    return 'scp://' + value


def validate_remote(root: Path, policy: dict[str, Any]) -> tuple[str, str]:
    repository = policy["repository"]
    remote = repository["remote"]
    result = git(root, "remote", "get-url", remote, check=False)
    if result.returncode != 0:
        raise WorkflowFailure(f"required Git remote is missing: {remote}")
    actual = normalize_remote(result.stdout.strip(), root)
    accepted = {normalize_remote(str(item), root) for item in repository["accepted_urls"]}
    if actual is None:
        raise WorkflowFailure("Git remote URL contains embedded credentials or is unsupported")
    if actual not in accepted:
        raise WorkflowFailure("Git remote does not match the workflow policy")
    push = git(root, "remote", "get-url", "--push", "--all", remote, check=False)
    push_urls = push.stdout.splitlines()
    if push.returncode or len(push_urls) != 1 or normalize_remote(push_urls[0], root) != actual:
        raise WorkflowFailure("Git push destination differs from the single approved remote")
    return remote, actual


def index_file(root: Path) -> Path:
    value = Path(git(root, 'rev-parse', '--git-path', 'index').stdout.strip())
    path = value if value.is_absolute() else root / value
    if path.resolve() != path.absolute() or not path.is_file():
        raise WorkflowFailure('Git index must be a regular file without symlink components')
    return path


def busy_repository(root):
    """An in-progress merge, sequencer or ordinary Git lock is not a quiescent checkout."""
    directory = Path(git(root, 'rev-parse', '--absolute-git-dir').stdout.strip())
    if any((directory / name).exists() for name in
           ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer')):
        return True
    return next(directory.rglob('*.lock'), None) is not None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_ignored(root: Path, relative: str) -> bool:
    return git(root, "check-ignore", "--no-index", "-q", "--", relative, check=False).returncode == 0


def path_matches(path: str, patterns: list[str]) -> bool:
    pure = PurePosixPath(path)
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(pure.name, pattern) for pattern in patterns)


def excluded_path(path: str, policy: dict[str, Any]) -> bool:
    if path_matches(path, list(policy.get("allowed_excluded_globs", []))):
        return False
    if any(path.startswith(prefix) for prefix in policy.get("excluded_prefixes", [])):
        return True
    if "/__pycache__/" in f"/{path}/" or path.startswith("__pycache__/"):
        return True
    return path_matches(path, list(policy.get("excluded_globs", [])))


def existing_excluded_path(root: Path, path: str, policy: dict[str, Any]) -> bool:
    if not excluded_path(path, policy):
        return False
    candidate = root / path
    return candidate.exists() or candidate.is_symlink()


def tracked_files(root: Path) -> list[str]:
    raw = git(root, "ls-files", "-z", text=False).stdout
    return sorted(part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part)


def validate_structure(root: Path, policy: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    links = policy.get("discovery_links", {})
    if not isinstance(links, dict):
        issues.append("workflow policy discovery_links must be an object")
    else:
        for relative, expected in sorted(links.items()):
            path = root / relative
            if not path.is_symlink():
                issues.append(f"skill discovery link is missing or not a symlink: {relative}")
                continue
            if os.readlink(path) != expected:
                issues.append(f"skill discovery link has the wrong target: {relative}")
            if not path.resolve().is_dir():
                issues.append(f"skill discovery target is missing: {relative}")

    for relative in policy.get("ignored_probes", []):
        if not check_ignored(root, relative):
            issues.append(f"required excluded path is Git-eligible: {relative}")
    for relative in policy.get("eligible_probes", []):
        if check_ignored(root, relative):
            issues.append(f"required durable path is ignored: {relative}")

    for relative in tracked_files(root):
        if existing_excluded_path(root, relative, policy):
            issues.append(f"excluded path is tracked: {relative}")

    return sorted(set(issues))


def parse_status(root: Path) -> list[dict[str, str]]:
    raw = git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all", text=False).stdout
    records = raw.split(b"\0")
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(records):
        raw_record = records[index]
        index += 1
        if not raw_record:
            continue
        record = raw_record.decode("utf-8", "surrogateescape")
        kind = record[0]
        if kind == "1":
            fields = record.split(" ", 8)
            if len(fields) != 9:
                raise WorkflowFailure("could not parse Git worktree status")
            entries.append({"kind": kind, "xy": fields[1], "path": fields[8]})
        elif kind == "2":
            fields = record.split(" ", 9)
            if len(fields) != 10 or index >= len(records):
                raise WorkflowFailure("could not parse renamed Git path")
            original = records[index].decode("utf-8", "surrogateescape")
            index += 1
            entries.append({"kind": kind, "xy": fields[1], "path": fields[9], "original": original})
        elif kind == "u":
            fields = record.split(" ", 10)
            if len(fields) != 11:
                raise WorkflowFailure("could not parse unmerged Git path")
            entries.append({"kind": kind, "xy": fields[1], "path": fields[10]})
        elif kind in ("?", "!"):
            entries.append({"kind": kind, "xy": kind, "path": record[2:]})
        elif kind == "#":
            continue
        else:
            raise WorkflowFailure("could not parse Git worktree status")
    return entries


def current_branch(root: Path) -> str:
    branch = git(root, "branch", "--show-current").stdout.strip()
    if not branch:
        raise WorkflowFailure("detached HEAD is not supported")
    return branch


def upstream_ref(root: Path) -> str | None:
    result = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None






def changed_between(root: Path, left: str, right: str) -> list[str]:
    raw = git(root, "diff", "--no-renames", "--name-only", "-z", f"{left}..{right}", text=False).stdout
    return sorted(part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part)


def is_control_plane(path: str, policy: dict[str, Any]) -> bool:
    for protected in policy.get("control_plane_paths", []):
        if protected.endswith("/") and path.startswith(protected):
            return True
        if path == protected:
            return True
    return False


def secret_name(path: str, policy: dict[str, Any]) -> bool:
    config = policy.get("secret_scan", {})
    if path_matches(path, list(config.get("allowed_name_globs", []))):
        return False
    return path_matches(path, list(config.get("forbidden_name_globs", [])))


def scan_secrets(root: Path, paths: list[str], policy: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    maximum = int(policy.get("secret_scan", {}).get("max_file_bytes", 5 * 1024 * 1024))
    discovery = set(policy.get("discovery_links", {}).keys())
    for relative in paths:
        path = root / relative
        if secret_name(relative, policy):
            findings.append(f"secret-bearing filename is not eligible: {relative}")
            continue
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            if relative not in discovery:
                findings.append(f"unreviewed symlink is not eligible: {relative}")
            continue
        if not path.is_file():
            findings.append(f"non-file candidate requires review: {relative}")
            continue
        try:
            size = path.stat().st_size
        except OSError:
            findings.append(f"candidate cannot be inspected: {relative}")
            continue
        if size > maximum:
            findings.append(f"candidate exceeds secret-scan size limit: {relative}")
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(f"candidate cannot be inspected: {relative}")
            continue
        if b"\0" in data:
            findings.append(f"binary candidate requires explicit review: {relative}")
            continue
        text = data.decode("utf-8", "replace")
        for category, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"possible {category} in candidate: {relative}")
                break
    return sorted(set(findings))

def snapshot(root: Path, paths: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in paths:
        path = root / relative
        if path.is_symlink():
            result[relative] = "symlink:120000:" + os.readlink(path)
        elif path.is_file():
            executable = bool(path.stat().st_mode & 0o111)
            mode = "100755" if executable else "100644"
            result[relative] = f"file:{mode}:" + sha256_file(path)
        elif path.exists():
            result[relative] = "other"
        else:
            result[relative] = "missing"
    return result

def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()



def candidate_digest(root: Path, paths: list[str]) -> str:
    return canonical_digest(snapshot(root, paths))






def remote_branch_sha(root: Path, remote: str, branch: str) -> str | None:
    reference = f"refs/heads/{branch}"
    result = git(root, "ls-remote", "--heads", remote, reference, check=False)
    if result.returncode != 0:
        raise WorkflowFailure("could not verify the remote branch state")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise WorkflowFailure("remote branch lookup returned an ambiguous result")
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != reference or re.fullmatch(r"[0-9a-fA-F]{40,64}", fields[0]) is None:
        raise WorkflowFailure("could not parse the remote branch state")
    return fields[0].lower()




def index_paths(root: Path) -> list[str]:
    raw = git(root, "diff", "--cached", "--no-renames", "--name-only", "-z", text=False).stdout
    return sorted(part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part)


def run_project_checks(root: Path, policy: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    checks = policy.get("project_checks", [])
    if not isinstance(checks, list):
        raise WorkflowFailure("workflow policy project_checks must be a list")
    for command in checks:
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise WorkflowFailure("workflow policy contains an invalid project check")
        completed = run(command, cwd=root, check=False, timeout=300)
        results.append({"command": command, "returncode": completed.returncode, "passed": completed.returncode == 0})
    return results

def commit_matches_proposal(root: Path, commit: str, proposal: dict[str, Any]) -> bool:
    parent = git(root, "log", "-1", "--format=%P", commit, check=False)
    if parent.returncode or parent.stdout.strip() != proposal["base_head"]:
        return False
    if changed_between(root, proposal["base_head"], commit) != proposal["paths"]:
        return False
    message = git(root, "log", "-1", "--format=%B", commit, check=False)
    if message.returncode != 0 or message.stdout.rstrip("\n") != proposal["commit_message"]:
        return False
    return canonical_digest(commit_snapshot(root, commit, proposal["paths"])) == proposal["candidate_digest"]

def commit_snapshot(root: Path, commit: str, paths: list[str]) -> dict[str, str]:
    """Compare immutable commit bytes and modes, not only its paths and message."""
    result = {}
    for path in paths:
        raw = git(root, "--literal-pathspecs", "ls-tree", "-z", commit, "--", path, text=False).stdout
        if not raw:
            result[path] = "missing"
            continue
        entries = raw.rstrip(b"\0").split(b"\0")
        if len(entries) != 1:
            raise WorkflowFailure("commit tree contains ambiguous path entries")
        metadata, actual = entries[0].split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if actual != os.fsencode(path) or kind != b"blob":
            raise WorkflowFailure("commit tree contains an unsupported object")
        data = git(root, "cat-file", "blob", object_id.decode(), text=False).stdout
        if mode == b"120000":
            result[path] = "symlink:120000:" + os.fsdecode(data)
        elif mode in (b"100644", b"100755"):
            result[path] = f"file:{mode.decode()}:" + hashlib.sha256(data).hexdigest()
        else:
            raise WorkflowFailure("commit tree contains an unsupported mode")
    return result


def load_policy(project):
    from maven_resources import resource_path
    policy = load_json(resource_path('config/workflow.json'), 'installed workflow policy')
    if policy.get('schema') != SCHEMA:
        raise WorkflowFailure('installed workflow policy is invalid')
    policy['project_id'] = project['project_id']
    policy['instance_id'] = project['instance_id']
    policy['recovery_epoch'] = project['recovery_epoch']
    policy['project_checks'] = project['checks']
    policy['repository'] = {
        'branch_policy': 'explicit-current', 'remote': project['remote'],
        'branch': project['branch'], 'accepted_urls': [project['accepted_url']]}
    return policy


def workflow_digest():
    from tools import project
    paths = [Path(__file__), Path(project.__file__), Path(__file__).parent / 'mcp' / 'publication.py']
    return canonical_digest({path.name: sha256_file(path) for path in paths})


def publication_directory(root):
    return root / '.maven' / 'git-publication'


@contextlib.contextmanager
def publication_lock(root):
    """Serialize only explicit Git operations; no process or harness registration."""
    directory = private_directory(publication_directory(root))
    descriptor = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise WorkflowFailure('Git publication lock must be an owner-private regular file')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkflowFailure('another Git operation is in progress') from None
        yield
    finally:
        os.close(descriptor)


def receipt(root, report, **changes):
    # Advance in-memory status only after the durable write. An error saving a
    # terminal read-back must still be reported as an uncertain operation.
    save_private(publication_directory(root) / 'receipt.json', {**report, **changes})
    report.update(changes)


def load_receipt(root):
    path = publication_directory(root) / 'receipt.json'
    if not path.exists() and not path.is_symlink():
        return None
    report = read_private(path)
    if (not isinstance(report, dict) or report.get('schema') != SCHEMA
            or report.get('root') != str(root) or report.get('status') not in
            ('approval_required', 'installing', 'unverified', 'published', 'cancelled')):
        raise WorkflowFailure('invalid Git publication receipt; preserve it for inspection')
    return report


def require_no_pending(root):
    prior = load_receipt(root)
    if prior and prior['status'] not in ('published', 'cancelled'):
        raise WorkflowFailure('resolve the existing Git publication before replacing its review or destination')




def head(root):
    return git(root, 'rev-parse', 'HEAD').stdout.strip()


def candidate_paths(root):
    entries = parse_status(root)
    return sorted({path for entry in entries if entry['kind'] != '!'
                   for path in (entry['path'], entry.get('original', entry['path']))})


def inspect_git(root, policy):
    if Path(git(root, 'rev-parse', '--show-toplevel').stdout.strip()).resolve() != root:
        raise WorkflowFailure('selected project root is not the exact Git checkout root')
    branch = current_branch(root)
    remote, url = validate_remote(root, policy)
    if branch != policy['repository']['branch']:
        raise WorkflowFailure('current branch differs from the explicitly configured destination branch')
    return {'git_enabled': True, 'branch': branch, 'remote': remote, 'remote_url': url,
            'head': head(root), 'upstream': upstream_ref(root), 'changed_paths': candidate_paths(root),
            'remote_sha': remote_branch_sha(root, remote, branch)}



def publication_issues(root, policy):
    issues = validate_structure(root, policy)
    entries = parse_status(root)
    if busy_repository(root):
        issues.append('repository has an active Git operation or lock')
    if any(entry['kind'] == 'u' or 'U' in entry['xy'] for entry in entries):
        issues.append('worktree contains unresolved conflicts')
    if index_paths(root):
        issues.append('index must be unmodified; preserve existing staging explicitly')
    paths = candidate_paths(root)
    issues.extend(scan_secrets(root, paths, policy))
    if any(existing_excluded_path(root, path, policy) for path in paths):
        issues.append('candidate includes private or excluded paths')
    return issues


def inspect_publication(root, policy, message):
    require_no_pending(root)
    report = {'schema': SCHEMA, 'root': str(root), 'project_id': policy['project_id'],
              **inspect_git(root, policy)}
    paths = report['changed_paths']
    before = candidate_digest(root, paths)
    report['control_plane_changes'] = [path for path in paths if is_control_plane(path, policy)]
    issues = publication_issues(root, policy)
    if report['remote_sha'] != report['head']:
        issues.append('destination must already equal HEAD; pre-existing local commits are never published')
    if issues:
        report['status'] = 'blocked'
        report['issues'] = sorted(set(issues))
        report['next_action'] = ('Review the listed issues without discarding local work or staging. '
                                 'Resolve them before requesting a fresh Git inspection.')
        return report
    if not paths:
        return {**report, 'status': 'unchanged'}
    if not isinstance(message, str):
        raise WorkflowFailure('supply the exact commit message for owner review')
    maximum = policy['max_commit_message_chars']
    if (not message.strip() or any(ord(char) < 32 for char in message) or len(message) > maximum
            or any(pattern.search(message) for _, pattern in SECRET_PATTERNS)):
        raise WorkflowFailure(f'commit message must be one nonempty line of at most {maximum} characters')
    checks = run_project_checks(root, policy)
    report['checks'] = checks
    report['check_policy'] = ('Exact owner approval may publish failing checks; rerun results must '
                              'equal these reviewed results. Approval is not integration readiness.')
    report['failed_checks'] = [check for check in checks if not check['passed']]
    if (candidate_paths(root) != paths or candidate_digest(root, paths) != before
            or head(root) != report['head'] or index_paths(root)):
        raise WorkflowFailure('repository changed during inspection; no approval was created')
    proposal = {
        'operation_id': str(uuid.uuid4()), 'project_id': policy['project_id'],
        'root': str(root), 'paths': paths,
        'commit_message': message, 'remote': report['remote'], 'branch': report['branch'],
        'remote_url': report['remote_url'], 'base_head': report['head'],
        'expected_remote_sha': report['remote_sha'], 'candidate_digest': before,
        'policy_digest': canonical_digest(policy), 'workflow_digest': workflow_digest(),
        'checks': checks, 'integration_ready': all(check['passed'] for check in checks),
        'would_push_existing_commits': False,
    }
    report.update(status='approval_required', proposed=proposal, candidate=canonical_digest(proposal))
    report['next_action'] = ('Review the exact changes, destination, message and check results. '
                             'Only explicit owner approval of this candidate permits publication.')
    receipt(root, report)
    return report


def review_candidate(stored, candidate):
    if not stored:
        raise WorkflowFailure('no Git publication receipt is available')
    proposal = stored.get('proposed')
    if (not isinstance(proposal, dict) or stored.get('candidate') != candidate
            or canonical_digest(proposal) != candidate or proposal.get('root') != stored['root']
            or proposal.get('project_id') != stored['project_id']
            or proposal.get('would_push_existing_commits') is not False
            or proposal.get('expected_remote_sha') != proposal.get('base_head')):
        raise WorkflowFailure('candidate does not match the exact retained publication')
    paths = proposal.get('paths')
    if (not isinstance(paths, list) or not paths
            or any(not isinstance(path, str) or not path or PurePosixPath(path).as_posix() != path
                   or path.startswith('/') or '..' in PurePosixPath(path).parts
                   or '.git' in (part.casefold() for part in PurePosixPath(path).parts) for path in paths)
            or paths != sorted(set(paths))):
        raise WorkflowFailure('Git approval contains invalid candidate paths')
    return proposal


def validate_approval(root, policy, proposal, *, committed=False):
    if canonical_digest(policy) != proposal['policy_digest'] or workflow_digest() != proposal['workflow_digest']:
        raise WorkflowFailure('installed workflow, project destination or policy changed after approval')
    current = inspect_git(root, policy)
    if (current['branch'] != proposal['branch'] or current['remote'] != proposal['remote']
            or current['remote_url'] != proposal['remote_url']):
        raise WorkflowFailure('explicit Git destination changed after approval')
    issues = publication_issues(root, policy)
    if committed:
        if current['changed_paths']:
            issues.append('publication recovery requires a clean worktree and index')
    else:
        if current['head'] != proposal['base_head'] or current['changed_paths'] != proposal['paths']:
            issues.append('branch base or complete candidate path set changed after approval')
        if candidate_digest(root, proposal['paths']) != proposal['candidate_digest']:
            issues.append('candidate bytes or modes changed after approval')
        if current['remote_sha'] != proposal['expected_remote_sha']:
            issues.append('remote advanced after approval; no commit will be created')
    if issues:
        raise WorkflowFailure('; '.join(sorted(set(issues))))


def git_with_index(root, index, *arguments, input_data=None):
    env = git_environment()
    env['GIT_INDEX_FILE'] = str(index)
    try:
        completed = subprocess.run(['git', *arguments], cwd=root, env=env, capture_output=True,
                                   stdin=subprocess.DEVNULL if input_data is None else None,
                                   input=input_data, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkflowFailure('isolated publication index command failed') from None
    if completed.returncode:
        raise WorkflowFailure('isolated publication index could not reproduce approved content')
    return completed.stdout.decode().strip()


def install_commit(root, policy, report, proposal):
    """Build an isolated index, then atomically advance only the approved branch base.

    The ordinary index is locked for the final transition. A pre-transition durable
    receipt records the exact object so a crash never authorizes a guessed commit.
    """
    import shutil
    checks = run_project_checks(root, policy)
    if checks != proposal['checks']:
        raise WorkflowFailure('project check results changed; cancel and obtain a new approval')
    validate_approval(root, policy, proposal)
    index = index_file(root)
    lock_path = index.with_name(index.name + '.lock')
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise WorkflowFailure('Git index became locked; no commit was created') from None
    info = os.fstat(descriptor)
    lock_identity = [info.st_dev, info.st_ino]
    try:
        with tempfile.TemporaryDirectory(prefix='maven-publication-', dir=index.parent) as directory:
            isolated = Path(directory) / 'index'
            git_with_index(root, isolated, 'read-tree', proposal['base_head'])
            git_with_index(root, isolated, '--literal-pathspecs', 'add', '-A',
                           '--pathspec-from-file=-', '--pathspec-file-nul',
                           input_data=b'\0'.join(os.fsencode(path) for path in proposal['paths']) + b'\0')
            tree = git_with_index(root, isolated, 'write-tree')
            commit = git(root, '-c', 'core.hooksPath=/dev/null', 'commit-tree', tree,
                         '-p', proposal['base_head'], '-m', proposal['commit_message']).stdout.strip()
            if not commit_matches_proposal(root, commit, proposal):
                raise WorkflowFailure('isolated commit bytes or modes differ from exact approval')
            if (current_branch(root) != proposal['branch'] or head(root) != proposal['base_head']
                    or candidate_paths(root) != proposal['paths'] or index_paths(root)
                    or candidate_digest(root, proposal['paths']) != proposal['candidate_digest']):
                raise WorkflowFailure('repository changed before commit installation')
            if validate_remote(root, policy) != (proposal['remote'], proposal['remote_url']):
                raise WorkflowFailure('Git destination changed during commit preparation')
            receipt(root, report, status='installing', commit_sha=commit, index_lock_identity=lock_identity,
                    base_index_sha=sha256_file(index), approved_index_sha=sha256_file(isolated))
            with os.fdopen(descriptor, 'wb') as target, isolated.open('rb') as source:
                descriptor = None
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            git(root, '-c', 'core.hooksPath=/dev/null', 'update-ref',
                f"refs/heads/{proposal['branch']}", commit, proposal['base_head'])
            os.replace(lock_path, index)
            receipt(root, report, status='unverified', head=commit)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if report['status'] != 'installing':
            release_index_lock(lock_path, lock_identity)


def publish_commit(root, policy, report, proposal):
    commit = report['commit_sha']
    if head(root) != commit or not commit_matches_proposal(root, commit, proposal):
        raise WorkflowFailure('local HEAD no longer equals the approved commit; preserve receipt')
    validate_approval(root, policy, proposal, committed=True)
    remote_sha = remote_branch_sha(root, proposal['remote'], proposal['branch'])
    if remote_sha != commit:
        if remote_sha != proposal['expected_remote_sha']:
            raise WorkflowFailure('remote branch advanced; commit preserved, history replacement is prohibited')
        if head(root) != commit or current_branch(root) != proposal['branch'] or candidate_paths(root):
            raise WorkflowFailure('checkout changed before publication; preserve the commit and new work')
        # Push the immutable approved object, never a possibly concurrently changed HEAD.
        reference = f"refs/heads/{proposal['branch']}"
        # The single-parent object is already verified as a direct fast-forward from
        # this exact base. CAS also rejects a concurrent deletion or rewind.
        # A raw URL may be subject to unrelated pushInsteadOf rules even when
        # the selected named remote has an explicit push URL. Use exactly the
        # single push destination validated for the named remote.
        destination, url = validate_remote(root, policy)
        if (destination, url) != (proposal['remote'], proposal['remote_url']):
            raise WorkflowFailure('Git destination changed before publication')
        result = git(root, '-c', 'core.hooksPath=/dev/null', '-c', 'push.followTags=false',
                     '-c', f'remote.{destination}.mirror=false', '-c', 'push.pushOption=',
                     'push', '--porcelain', '--recurse-submodules=no',
                     f"--force-with-lease={reference}:{proposal['expected_remote_sha']}",
                     '--', destination, f"{commit}:{reference}", check=False)
        if result.returncode:
            raise WorkflowFailure('push failed or rejected; retain exact receipt for explicit recovery')
        remote_sha = remote_branch_sha(root, proposal['remote'], proposal['branch'])
    if validate_remote(root, policy) != (proposal['remote'], proposal['remote_url']):
        raise WorkflowFailure('Git destination changed during publication; preserve the receipt')
    if remote_sha != commit:
        raise WorkflowFailure('remote SHA is not the approved commit; publication is unverified')
    if head(root) != commit or current_branch(root) != proposal['branch'] or candidate_paths(root):
        raise WorkflowFailure('checkout changed during publication; preserve the receipt and new work')
    receipt(root, report, status='published', remote_sha=remote_sha)
    return report


def release_index_lock(path, identity):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or [info.st_dev, info.st_ino] != identity:
        raise WorkflowFailure('unknown Git index lock must be preserved')
    path.unlink()


def recover_installation(root, policy, report, proposal):
    if (canonical_digest(policy) != proposal['policy_digest'] or workflow_digest() != proposal['workflow_digest']
            or current_branch(root) != proposal['branch']):
        raise WorkflowFailure('branch, workflow or destination differs from the interrupted installation')
    commit = report['commit_sha']
    actual = head(root)
    if actual not in (proposal['base_head'], commit) or not commit_matches_proposal(root, commit, proposal):
        raise WorkflowFailure('unexpected interrupted commit or HEAD; preserve the receipt')
    index = index_file(root)
    lock_path = index.with_name(index.name + '.lock')
    if actual == commit and index_paths(root):
        if sha256_file(index) != report['base_index_sha']:
            raise WorkflowFailure('index differs from recorded transition; preserve staging')
        # Finish the exact interrupted rename, never rebuild or clear unknown staging.
        try:
            info = lock_path.lstat()
        except FileNotFoundError:
            raise WorkflowFailure('interrupted index lock missing; restore the index explicitly') from None
        if (not stat.S_ISREG(info.st_mode) or [info.st_dev, info.st_ino] != report['index_lock_identity']
                or sha256_file(lock_path) != report['approved_index_sha']):
            raise WorkflowFailure('interrupted index differs from the approved tree; preserve staging')
        os.replace(lock_path, index)
    else:
        release_index_lock(lock_path, report['index_lock_identity'])
    receipt(root, report, status='approval_required' if actual == proposal['base_head'] else 'unverified')


def validate_owner(approval, candidate):
    from tools.memory.validation import MemoryError, reject_secrets, source
    if (not isinstance(approval, dict) or set(approval) != {'approved', 'candidate', 'source'}
            or approval['approved'] is not True or approval['candidate'] != candidate):
        raise WorkflowFailure('actual owner approval of this exact candidate is required')
    try:
        source(approval['source'], approval=True)
        reject_secrets(approval['source'])
        if any(pattern.search(value) for value in approval['source'].values() for _, pattern in SECRET_PATTERNS):
            raise WorkflowFailure('owner approval must not contain credentials')
    except MemoryError:
        raise WorkflowFailure('owner approval requires a non-secret origin and owner excerpt') from None


def publication_project(root, home):
    from tools.memory.client import MemoryClientError
    from tools.memory.environment import load_environment
    project = load_project(root)
    try:
        values = load_environment(home / '.env')
    except MemoryClientError:
        raise WorkflowFailure('Git requires the selected installation’s private project binding settings') from None
    require_binding(project, {key: values.get('MAVEN_MEMORY_' + key.upper())
                              for key in ('instance_id', 'recovery_epoch')})
    return project


def publication(root, action, *, home=None, **options):
    from tools.coordination import ensure_available, project_binding_lock
    from tools.reconcile import select_project
    root = Path(root).expanduser().resolve(strict=True)
    home = home or instance_home()
    with project_binding_lock(home, root):
        ensure_available(home, root)
        selection = select_project(root, home)
        if not selection or Path(selection['root']) != root:
            raise WorkflowFailure('--root must be the exact enabled project checkout')
        return bound_publication(root, action, home=home, project_id=selection['project_id'], **options)


def bound_publication(root, action, *, writers_quiescent=False, message=None, approval=None,
                      candidate=None, home, project_id):
    """Caller holds the project binding gate; serialize only this project's publication."""
    if action not in ('inspect', 'apply', 'recover', 'status', 'cancel'):
        raise WorkflowFailure('unknown Git publication action')
    if (message is not None and action != 'inspect' or approval is not None and action not in ('apply', 'recover')
            or candidate is not None and action != 'cancel'):
        raise WorkflowFailure('unexpected arguments for the selected Git operation')
    if action not in ('status', 'recover') or approval is not None:
        if writers_quiescent is not True:
            raise WorkflowFailure('attest all relevant repository writers have stopped')
    with publication_lock(root):
        project = publication_project(root, home)
        if project['project_id'] != project_id:
            raise WorkflowFailure('selected project changed before Git operation')
        if not project['git_enabled']:
            raise WorkflowFailure('optional Git publication is not configured in the selected project')
        policy = load_policy(project)
        if action == 'inspect':
            return inspect_publication(root, policy, message)
        stored = load_receipt(root)
        if stored and stored.get('project_id') != project['project_id']:
            raise WorkflowFailure('publication receipt belongs to a different project')
        if action == 'status' or action == 'recover' and approval is None:
            result = {'status': 'observed', 'publication': stored}
            if stored and stored['status'] in ('installing', 'unverified'):
                result['next_action'] = 'Compare the retained commit with the local and remote branch before recovering this exact approval.'
            return result
        code = (approval or {}).get('candidate') if action != 'cancel' else candidate
        proposal = review_candidate(stored, code)
        if action == 'cancel':
            if stored['status'] != 'approval_required':
                raise WorkflowFailure('only an unapplied review can be cancelled; preserve uncertain outcomes')
            receipt(root, stored, status='cancelled')
            return stored
        validate_owner(approval, code)
        if stored.get('approval', approval) != approval:
            raise WorkflowFailure('owner evidence differs from the original approval')
        if action == 'recover' and stored['status'] == 'published':
            return {**stored, 'replayed': True, 'message': 'Retained verified result; no new push was attempted.'}
        if stored['status'] in ('cancelled', 'published'):
            raise WorkflowFailure('this publication is already terminal; inspect status')
        if action == 'apply' and stored['status'] != 'approval_required':
            raise WorkflowFailure('interrupted Git publication requires explicit recovery')
        try:
            if stored['status'] == 'installing':
                recover_installation(root, policy, stored, proposal)
            if stored['status'] == 'approval_required':
                validate_approval(root, policy, proposal)
                stored['approval'] = approval
                install_commit(root, policy, stored, proposal)
            return publish_commit(root, policy, stored, proposal)
        except Exception as error:
            if stored['status'] not in ('installing', 'unverified'):
                raise
            issue = str(error) if isinstance(error, (WorkflowFailure, UninstallError)) else 'Git publication could not be verified.'
            return {'status': 'uncertain', 'candidate': code, 'issue': issue,
                    'next_action': 'Inspect the retained publication; recover only this exact approval.'}


def main(argv=None):
    from tools.memory.boundary import BoundaryError
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('inspect', 'status', 'apply', 'recover', 'cancel'):
        command = commands.add_parser(action)
        command.add_argument('--root', type=Path, required=True)
        if action != 'status':
            command.add_argument('--writers-quiescent', action='store_true')
        if action == 'inspect':
            command.add_argument('--message', required=True)
        if action in ('apply', 'recover', 'cancel'):
            command.add_argument('--candidate', required=action != 'recover')
        if action in ('apply', 'recover'):
            command.add_argument('--approval-origin', required=action == 'apply')
            command.add_argument('--approval-excerpt', required=action == 'apply')
    args = parser.parse_args(argv)
    candidate = getattr(args, 'candidate', None)
    approval = None
    try:
        if args.action in ('apply', 'recover') and (candidate or args.approval_origin or args.approval_excerpt):
            approval = {'approved': True, 'candidate': candidate,
                        'source': {'origin': args.approval_origin, 'excerpt': args.approval_excerpt}}
        result = publication(args.root, args.action, writers_quiescent=getattr(args, 'writers_quiescent', False),
                             candidate=candidate if args.action == 'cancel' else None,
                             message=getattr(args, 'message', None), approval=approval)
    except (ProjectError, UninstallError, BoundaryError) as error:
        result = {'status': 'blocked', 'issue': str(error), 'next_action': DEFAULT_NEXT_ACTION}
    except KeyboardInterrupt:
        result = {'status': 'interrupted', 'issue': 'Git workflow interrupted.', 'next_action': DEFAULT_NEXT_ACTION}
    except Exception:
        result = {'status': 'blocked', 'issue': 'Git workflow could not finish.', 'next_action': DEFAULT_NEXT_ACTION}
    print(json.dumps(result, indent=2, sort_keys=True))
    return {'blocked': EXIT_BLOCKED, 'approval_required': EXIT_APPROVAL_REQUIRED,
            'uncertain': EXIT_UNCERTAIN, 'interrupted': 130}.get(result['status'], EXIT_OK)


if __name__ == '__main__':
    raise SystemExit(main())
