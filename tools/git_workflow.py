#!/usr/bin/env python3
"""Explicit optional Git publication with exact approved candidate semantics."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import uuid
from typing import Any
from urllib.parse import urlsplit

SCHEMA = 1

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_APPROVAL_REQUIRED = 3
EXIT_ERROR = 4
EXIT_UNSYNCHRONIZED = 5
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
APPROVAL_CODE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)-"
    r"(?P<fingerprint>[0-9A-F]{4}-[0-9A-F]{4})$"
)


class WorkflowFailure(Exception):
    """Expected safe workflow stop."""

    def __init__(self, issue: str):
        super().__init__(issue)
        self.issue = issue


def git_environment():
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE'):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
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


def git_path(root: Path, name: str) -> Path:
    value = git(root, "rev-parse", "--git-path", name).stdout.strip()
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()

def other_git_locks(root: Path) -> list[str]:
    git_dir_value = git(root, "rev-parse", "--git-dir").stdout.strip()
    git_dir = Path(git_dir_value)
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    found: list[str] = []
    for candidate in git_dir.rglob("*.lock"):
        if candidate.name == "maven-workflow.lock":
            continue
        try:
            found.append(str(candidate.relative_to(git_dir)))
        except ValueError:
            found.append(candidate.name)
    return sorted(found)


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

def approval_code(proposal: dict[str, Any]) -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = canonical_digest(proposal)[:8].upper()
    fingerprint = "-".join(digest[index : index + 4] for index in range(0, len(digest), 4))
    return f"{timestamp}-{fingerprint}"


def approval_code_matches(proposal: dict[str, Any], code: str) -> bool:
    match = APPROVAL_CODE_RE.fullmatch(code)
    if match is None:
        return False
    try:
        dt.datetime.strptime(match.group("timestamp"), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    digest = canonical_digest(proposal)[:8].upper()
    expected = "-".join(digest[index : index + 4] for index in range(0, len(digest), 4))
    return match.group("fingerprint") == expected


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

def checkpoint_matches_proposal(root: Path, checkpoint_sha: str, proposal: dict[str, Any]) -> bool:
    parent = git(root, "rev-parse", f"{checkpoint_sha}^", check=False)
    if parent.returncode != 0 or parent.stdout.strip() != proposal["base_head"]:
        return False
    if changed_between(root, proposal["base_head"], checkpoint_sha) != proposal["paths"]:
        return False
    message = git(root, "log", "-1", "--format=%B", checkpoint_sha, check=False)
    if message.returncode != 0 or message.stdout.rstrip("\n") != proposal["commit_message"]:
        return False
    return canonical_digest(commit_snapshot(root, checkpoint_sha, proposal["paths"])) == proposal["candidate_digest"]

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
            raise WorkflowFailure("checkpoint tree contains ambiguous path entries")
        metadata, actual = entries[0].split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if actual != os.fsencode(path) or kind != b"blob":
            raise WorkflowFailure("checkpoint tree contains an unsupported object")
        data = git(root, "cat-file", "blob", object_id.decode(), text=False).stdout
        if mode == b"120000":
            result[path] = "symlink:120000:" + os.fsdecode(data)
        elif mode in (b"100644", b"100755"):
            result[path] = f"file:{mode.decode()}:" + hashlib.sha256(data).hexdigest()
        else:
            raise WorkflowFailure("checkpoint tree contains an unsupported mode")
    return result


def load_policy(root, project):
    from maven_resources import resource_path
    policy = load_json(resource_path('config/workflow.json'), 'installed workflow policy')
    if policy.get('schema') != SCHEMA or policy.get('memory', {}).get('mode') != 'cloud':
        raise WorkflowFailure('installed workflow policy is invalid')
    policy['project_id'] = project['project_id']
    policy['git_enabled'] = project.get('git_enabled', project.get('remote') is not None)
    policy['project_checks'] = project['checks']
    policy['repository'] = {
        'branch_policy': 'explicit-current', 'remote': project['remote'],
        'branch': project['branch'], 'accepted_urls': [project['accepted_url']]}
    return policy


def workflow_digest():
    from tools.memory import boundary, client, environment
    from tools import coordination, project
    paths = [Path(__file__), *(Path(module.__file__) for module in (boundary, client, environment, coordination, project))]
    return canonical_digest({path.name: sha256_file(path) for path in paths})


def receipt_path(session_id):
    from tools.coordination import session_uuid
    return f'.maven/runtime/sessions/{session_uuid(session_id)}/approval.json'


def receipt(root, report):
    from tools.memory.boundary import save_private
    save_private(root, receipt_path(report['session_id']), report)


def load_receipt(root, session_id):
    from tools.memory.boundary import read_private
    value = read_private(root, receipt_path(session_id))
    if value.get('schema') != SCHEMA or value.get('session_id') != session_id or value.get('root') != str(root):
        raise WorkflowFailure('approval receipt project or session binding is invalid')
    return value


def maybe_receipt(root, session_id):
    from tools.memory.boundary import private_path
    return load_receipt(root, session_id) if private_path(root, receipt_path(session_id)).exists() else None


def base_report(action, root, session_id):
    return {'schema': SCHEMA, 'action': action, 'root': str(root), 'session_id': session_id,
            'status': 'blocked', 'issues': [], 'warnings': [], 'changed_paths': [], 'checks': []}


def head(root, policy):
    return git(root, 'rev-parse', 'HEAD').stdout.strip() if policy['git_enabled'] else None


def candidate_paths(root):
    entries = parse_status(root)
    return sorted({path for entry in entries if entry['kind'] != '!'
                   for path in (entry['path'], entry.get('original', entry['path']))})


def inspect_git(root, policy):
    if not policy['git_enabled']:
        return {'git_enabled': False, 'head': None, 'changed_paths': []}
    if Path(git(root, 'rev-parse', '--show-toplevel').stdout.strip()).resolve() != root:
        raise WorkflowFailure('selected project root is not the exact Git checkout root')
    branch = current_branch(root)
    remote, url = validate_remote(root, policy)
    if branch != policy['repository']['branch']:
        raise WorkflowFailure('current branch differs from the explicitly configured destination branch')
    return {'git_enabled': True, 'branch': branch, 'remote': remote, 'remote_url': url,
            'head': head(root, policy), 'upstream': upstream_ref(root), 'changed_paths': candidate_paths(root),
            'remote_sha': remote_branch_sha(root, remote, branch)}


def parse_refs(values):
    result = []
    for value in values:
        try:
            identifier, revision = value.rsplit(':', 1)
            item = {'record_id': str(uuid.UUID(identifier)), 'revision': int(revision)}
            if item['revision'] < 1:
                raise ValueError
        except (ValueError, AttributeError):
            raise WorkflowFailure('memory reference must be UUID:positive-revision') from None
        if item not in result:
            result.append(item)
    return result


def ensure_no_pending(memory):
    from tools.memory.boundary import private_path
    if private_path(memory.root, memory.pending_path).exists():
        raise WorkflowFailure('cloud outcome is unresolved; explicitly recover the exact stored request first')


def start_work(root, policy, memory, registry, *, resume=False):
    report = base_report('start', root, memory.session_id)
    # Inspection never merges, pulls, resets, switches, or requires MAVEN provenance for external commits.
    report.update(inspect_git(root, policy))
    if resume:
        ensure_no_pending(memory)
        stored = maybe_receipt(root, memory.session_id)
        if stored and stored['status'] in ('preparing', 'approval_required', 'applying', 'unsynchronized', 'synchronized', 'memory_ready'):
            raise WorkflowFailure('a checkpoint receipt is still pending; apply, cancel or recover it before resume')
        record = memory.session()
        meta = memory.metadata(record)
        if memory.harness is not None and memory.harness != meta.get('harness'):
            raise WorkflowFailure('resume harness does not match the bound session')
        if memory.harness_session is not None and memory.harness_session != meta.get('harness_session'):
            raise WorkflowFailure('resume harness identity does not match the bound session')
        # Activation refuses a live reservation, even if it belongs to this requester.
        registry.activate(root, memory.session_id)
        try:
            memory.reconcile(report['head'])
        except Exception:
            registry.pause(root, memory.session_id)
            raise
        report['memory'] = memory.refresh()
    else:
        report['memory'] = memory.start(report['head'])
        registry.activate(root, memory.session_id)
    report['local_peers'] = registry.peers(root, memory.session_id)
    report['status'] = 'ready'
    report['next_action'] = 'Use cloud authority and current tasks. Startup inspected but did not synchronize Git.'
    return report


def summarize(root, policy, memory, registry, summary, refs):
    reservation = registry.reserve(root, memory.session_id)
    registry.quiescent(root, memory.session_id, reservation['reservation_id'])
    current = inspect_git(root, policy)
    paths = current['changed_paths']
    candidate = candidate_digest(root, paths) if policy['git_enabled'] else canonical_digest({})
    ref = memory.summarize(summary=summary, refs=refs, git_base=current['head'],
                           candidate_digest=candidate, reservation_id=reservation['reservation_id'])
    # A writer outside the registry cannot be trusted to honor the pause; compare again.
    if policy['git_enabled'] and (candidate_paths(root) != paths or head(root, policy) != current['head']
                                  or candidate_digest(root, paths) != candidate):
        raise WorkflowFailure('repository changed during summary; obtain a fresh summary after writers stop')
    return {'session': ref, 'reservation': reservation}


def checkpoint_issues(root, policy):
    issues = validate_structure(root, policy)
    entries = parse_status(root)
    if other_git_locks(root):
        issues.append('repository has active Git lock files')
    if any(entry['kind'] == 'u' or 'U' in entry['xy'] for entry in entries):
        issues.append('worktree contains unresolved conflicts')
    if index_paths(root):
        issues.append('index must be unmodified; preserve existing staging explicitly')
    paths = candidate_paths(root)
    issues.extend(scan_secrets(root, paths, policy))
    if any(existing_excluded_path(root, path, policy) for path in paths):
        issues.append('candidate includes private or excluded paths')
    return issues


def inspect_checkpoint(root, policy, memory, registry, *, action, message=None):
    report = base_report(action, root, memory.session_id)
    reservation = registry.reserve(root, memory.session_id)
    report['reservation'] = reservation
    registry.quiescent(root, memory.session_id, reservation['reservation_id'])
    ensure_no_pending(memory)
    prior = maybe_receipt(root, memory.session_id)
    if prior and prior['status'] in ('preparing', 'approval_required', 'applying', 'unsynchronized', 'synchronized', 'memory_ready'):
        raise WorkflowFailure('existing approval must be applied, explicitly cancelled, or recovered; it cannot be overwritten')
    report.update(inspect_git(root, policy))
    paths = report['changed_paths']
    report['control_plane_changes'] = [path for path in paths if is_control_plane(path, policy)]
    issues = memory.freshness_issues(root, paths, git_enabled=policy['git_enabled'],
                                     reservation_id=reservation['reservation_id'])
    if policy['git_enabled']:
        issues.extend(checkpoint_issues(root, policy))
        if report['remote_sha'] != report['head']:
            issues.append('destination must already equal HEAD; pre-existing local commits are never published')
    if issues:
        report['issues'] = sorted(set(issues))
        return report
    if not policy['git_enabled'] or not paths:
        report['status'] = 'memory_ready'
        report['git_outcome'] = 'not_applicable' if not policy['git_enabled'] else 'unchanged'
        return report
    message = message or f"checkpoint: preserve {report['branch']} work state"
    maximum = policy['checkpoint']['max_commit_message_chars']
    if not message.strip() or '\n' in message or '\r' in message or len(message) > maximum:
        raise WorkflowFailure(f'commit message must be one nonempty line of at most {maximum} characters')
    before = candidate_digest(root, paths)
    checks = run_project_checks(root, policy)
    report['checks'] = checks
    if (candidate_paths(root) != paths or candidate_digest(root, paths) != before
            or head(root, policy) != report['head'] or index_paths(root)):
        raise WorkflowFailure('repository changed during inspection; no approval was created')
    registry.quiescent(root, memory.session_id, reservation['reservation_id'])
    proposal = {
        'action': action, 'session_id': memory.session_id, 'project_id': memory.project_id,
        'reservation_id': reservation['reservation_id'], 'root': str(root), 'paths': paths,
        'commit_message': message, 'remote': report['remote'], 'branch': report['branch'],
        'remote_url': report['remote_url'], 'base_head': report['head'],
        'expected_remote_sha': report['remote_sha'], 'candidate_digest': before,
        'policy_digest': canonical_digest(policy), 'workflow_digest': workflow_digest(),
        'checks': checks, 'integration_ready': all(check['passed'] for check in checks),
        'would_push_existing_commits': False,
    }
    # Save the planned checkpoint identity before its cloud mutation so crash recovery can bind it.
    proposal['memory'] = {'record_id': str(uuid.uuid5(uuid.UUID(reservation['reservation_id']), 'checkpoint')),
                          'revision': 1}
    report.update(status='preparing', proposed=proposal)
    receipt(root, report)
    proposal['memory'] = memory.prepare_checkpoint(root, paths, report['head'], reservation['reservation_id'])
    proposal['approval_code'] = approval_code(proposal)
    report.update(status='approval_required', proposed=proposal)
    report['next_action'] = f"Review exact proposal, then maven-{action} --root ROOT --session {memory.session_id} --writers-quiescent --apply {proposal['approval_code']}"
    receipt(root, report)
    return report


def proposal_from_receipt(stored, code):
    proposal = stored.get('proposed')
    if stored.get('status') not in ('approval_required', 'applying', 'unsynchronized', 'synchronized'):
        raise WorkflowFailure('no applicable exact approval receipt is available')
    if not isinstance(proposal, dict) or proposal.get('approval_code') != code:
        raise WorkflowFailure('approval code does not match this session receipt')
    unsigned = dict(proposal)
    unsigned.pop('approval_code', None)
    if not approval_code_matches(unsigned, code):
        raise WorkflowFailure('approval fingerprint does not match the complete proposal')
    paths = proposal.get('paths')
    if (not isinstance(paths, list) or not paths or paths != sorted(set(paths))
            or any(not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts
                   or path.startswith('.git/') or path == '.git' for path in paths)):
        raise WorkflowFailure('approval contains invalid candidate paths')
    if (proposal.get('session_id') != stored['session_id'] or proposal.get('root') != stored['root']
            or proposal.get('action') != stored['action']
            or proposal.get('would_push_existing_commits') is not False
            or proposal.get('expected_remote_sha') != proposal.get('base_head')):
        raise WorkflowFailure('approval session, action or publication binding is invalid')
    return proposal


def validate_approval(root, policy, memory, registry, proposal, *, committed=False):
    ensure_no_pending(memory)
    registry.quiescent(root, memory.session_id, proposal['reservation_id'])
    if not policy['git_enabled']:
        raise WorkflowFailure('Git approval cannot be used for a memory-only project')
    if canonical_digest(policy) != proposal['policy_digest'] or workflow_digest() != proposal['workflow_digest']:
        raise WorkflowFailure('installed workflow, project destination or policy changed after approval')
    current = inspect_git(root, policy)
    if (current['branch'] != proposal['branch'] or current['remote'] != proposal['remote']
            or current['remote_url'] != proposal['remote_url']):
        raise WorkflowFailure('explicit Git destination changed after approval')
    memory.verify_checkpoint(proposal, resume=committed)
    issues = checkpoint_issues(root, policy)
    if committed:
        if candidate_paths(root):
            issues.append('checkpoint recovery requires a clean worktree and index')
    else:
        if current['head'] != proposal['base_head'] or current['changed_paths'] != proposal['paths']:
            issues.append('branch base or complete candidate path set changed after approval')
        if candidate_digest(root, proposal['paths']) != proposal['candidate_digest']:
            issues.append('candidate bytes or modes changed after approval')
        issues.extend(memory.freshness_issues(root, proposal['paths'], reservation_id=proposal['reservation_id']))
        if current['remote_sha'] != proposal['expected_remote_sha']:
            issues.append('remote advanced after approval; no checkpoint will be created')
    if issues:
        raise WorkflowFailure('; '.join(sorted(set(issues))))


def git_with_index(root, index, *arguments, input_data=None):
    env = git_environment()
    env['GIT_INDEX_FILE'] = str(index)
    try:
        completed = subprocess.run(['git', *arguments], cwd=root, env=env, capture_output=True,
                                   input=input_data, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkflowFailure('isolated checkpoint index command failed') from None
    if completed.returncode:
        raise WorkflowFailure('isolated checkpoint index could not reproduce approved content')
    return completed.stdout.decode().strip()


def create_checkpoint(root, policy, memory, registry, report, proposal):
    """Build an isolated index, then atomically advance only the approved branch base.

    The ordinary index is locked for the final transition. A pre-transition durable
    receipt records the exact object so a crash never authorizes a guessed commit.
    """
    import shutil
    checks = run_project_checks(root, policy)
    if checks != proposal['checks']:
        raise WorkflowFailure('project check results changed; cancel and obtain a new approval')
    validate_approval(root, policy, memory, registry, proposal)
    index = git_path(root, 'index')
    lock_path = index.with_name(index.name + '.lock')
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise WorkflowFailure('Git index became locked; no checkpoint was created') from None
    try:
        with tempfile.TemporaryDirectory(prefix='maven-checkpoint-', dir=index.parent) as directory:
            isolated = Path(directory) / 'index'
            git_with_index(root, isolated, 'read-tree', proposal['base_head'])
            git_with_index(root, isolated, '--literal-pathspecs', 'add', '-A',
                           '--pathspec-from-file=-', '--pathspec-file-nul',
                           input_data=b'\0'.join(os.fsencode(path) for path in proposal['paths']) + b'\0')
            tree = git_with_index(root, isolated, 'write-tree')
            commit = git(root, '-c', 'core.hooksPath=/dev/null', 'commit-tree', tree,
                         '-p', proposal['base_head'], '-m', proposal['commit_message']).stdout.strip()
            if not checkpoint_matches_proposal(root, commit, proposal):
                raise WorkflowFailure('isolated commit bytes or modes differ from exact approval')
            if (current_branch(root) != proposal['branch'] or head(root, policy) != proposal['base_head']
                    or candidate_paths(root) != proposal['paths'] or index_paths(root)
                    or candidate_digest(root, proposal['paths']) != proposal['candidate_digest']):
                raise WorkflowFailure('repository changed before checkpoint installation')
            registry.quiescent(root, memory.session_id, proposal['reservation_id'])
            memory.verify_checkpoint(proposal)
            lock_info = os.fstat(descriptor)
            report.update(status='applying', checkpoint_sha=commit,
                          index_lock_identity=[lock_info.st_dev, lock_info.st_ino],
                          index_base_tree=git(root, 'rev-parse', proposal['base_head'] + '^{tree}').stdout.strip())
            receipt(root, report)
            with os.fdopen(descriptor, 'wb') as target, isolated.open('rb') as source:
                descriptor = None
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            git(root, '-c', 'core.hooksPath=/dev/null', 'update-ref',
                f"refs/heads/{proposal['branch']}", commit, proposal['base_head'])
            os.replace(lock_path, index)
            report.update(status='unsynchronized', head=commit)
            receipt(root, report)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)
    return report['checkpoint_sha']


def finish_checkpoint_push(root, policy, memory, registry, report, proposal):
    commit = report['checkpoint_sha']
    if head(root, policy) != commit or not checkpoint_matches_proposal(root, commit, proposal):
        raise WorkflowFailure('local checkpoint no longer equals exact approved commit; preserve receipt')
    validate_approval(root, policy, memory, registry, proposal, committed=True)
    memory.link_checkpoint(proposal, commit, None)
    remote_sha = remote_branch_sha(root, proposal['remote'], proposal['branch'])
    if remote_sha != commit:
        if remote_sha != proposal['expected_remote_sha']:
            raise WorkflowFailure('remote branch advanced; checkpoint preserved, force push is prohibited')
        registry.quiescent(root, memory.session_id, proposal['reservation_id'])
        if head(root, policy) != commit or current_branch(root) != proposal['branch'] or candidate_paths(root):
            raise WorkflowFailure('checkout changed before publication; preserve the checkpoint and new work')
        # Push the immutable approved object, never a possibly concurrently changed HEAD.
        result = git(root, '-c', 'core.hooksPath=/dev/null', '-c', 'push.followTags=false',
                     'push', '--porcelain', proposal['remote'],
                     f"{commit}:refs/heads/{proposal['branch']}", check=False)
        if result.returncode:
            raise WorkflowFailure('push failed or rejected; retain exact receipt for explicit recovery')
        remote_sha = remote_branch_sha(root, proposal['remote'], proposal['branch'])
    if remote_sha != commit:
        raise WorkflowFailure('remote SHA is not the approved checkpoint; no synchronization claim')
    if head(root, policy) != commit or current_branch(root) != proposal['branch'] or candidate_paths(root):
        raise WorkflowFailure('checkout changed during publication; do not close the session or discard new work')
    memory.link_checkpoint(proposal, commit, remote_sha)
    report.update(status='synchronized', remote_sha=remote_sha)
    receipt(root, report)
    return report


def apply_checkpoint(root, policy, memory, registry, *, action, code):
    report = load_receipt(root, memory.session_id)
    proposal = proposal_from_receipt(report, code)
    if proposal['action'] != action:
        raise WorkflowFailure('approval action differs; use the lifecycle command that created it')
    if report['status'] == 'applying':
        raise WorkflowFailure('interrupted commit installation requires explicit recover first')
    if report['status'] == 'approval_required':
        validate_approval(root, policy, memory, registry, proposal)
        create_checkpoint(root, policy, memory, registry, report, proposal)
    return finish_checkpoint_push(root, policy, memory, registry, report, proposal)


def complete_boundary(root, policy, memory, registry, report):
    close = report['action'] == 'stop-work'
    proposal = report.get('proposed')
    registry.quiescent(root, memory.session_id, report['reservation']['reservation_id'])
    if proposal:
        memory.verify_checkpoint(proposal, resume=True)
    if report['status'] == 'memory_ready':
        session = memory.session(closed=True)
        if session['data']['state'] == 'active' and memory.metadata(session).get('phase') != 'checkpointed':
            issues = memory.freshness_issues(root, [], git_enabled=policy['git_enabled'],
                                             reservation_id=report['reservation']['reservation_id'])
            if policy['git_enabled'] and candidate_paths(root):
                issues.append('worktree changed before memory-only finalization')
            if issues:
                raise WorkflowFailure('; '.join(issues))
        report['memory_checkpoint'] = memory.memory_checkpoint(report['reservation']['reservation_id'])
        receipt(root, report)
    report['session'] = memory.complete(report['status'], close=close,
                                        checkpoint=proposal['memory'] if proposal else report.get('memory_checkpoint'))
    report['status'] = 'closed' if close else 'checkpointed'
    receipt(root, report)
    if close:
        report['local_access_cleanup'] = registry.close(root, memory.session_id, logout_cache)
    else:
        registry.release(root, memory.session_id, activate=True)
    return report


def logout_cache(item):
    """Release a cache only while the registry proves no remaining session uses it."""
    cache = Path(item['cache'])
    try:
        if (not cache.is_absolute() or cache.is_symlink()
                or cache.exists() and (cache.stat().st_uid != os.getuid() or cache.stat().st_mode & 0o077)):
            raise OSError
        env = os.environ.copy()
        env['AWS_LOGIN_CACHE_DIRECTORY'] = str(cache)
        for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                    'AWS_SECURITY_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE'):
            env.pop(key, None)
        result = subprocess.run(['aws', 'logout', '--profile', item['profile']], env=env,
                                capture_output=True, timeout=30, check=False)
        if result.returncode or cache.exists() and any(cache.iterdir()):
            return ['last-session AWS logout or cache-absence verification failed']
    except (OSError, subprocess.SubprocessError):
        return ['last-session local credential cleanup failed; explicit recovery is required']
    return []


def cancel_work(root, policy, memory, registry, *, approval_only=False):
    ensure_no_pending(memory)
    stored = maybe_receipt(root, memory.session_id)
    if stored and stored['status'] in ('applying', 'unsynchronized', 'synchronized', 'memory_ready'):
        raise WorkflowFailure('commit or publication may exist; recover exact receipt instead of cancelling it')
    if stored and stored.get('proposed') and stored['status'] in ('preparing', 'approval_required'):
        proposal = stored['proposed']
        if head(root, policy) != proposal['base_head']:
            raise WorkflowFailure('HEAD changed since approval; recover before cancelling')
        memory.abort_checkpoint(proposal)
        stored['status'] = 'approval_cancelled'
        receipt(root, stored)
    if approval_only:
        memory.reconcile(head(root, policy))
        registry.release(root, memory.session_id, activate=True)
        return {**base_report('cancel', root, memory.session_id), 'status': 'ready',
                'next_action': 'Approval cancelled; session remains active and work memory is retained.'}
    record = memory.cancel() if memory.restore_pointer() is not None else None
    report = {**base_report('cancel', root, memory.session_id), 'status': 'cancelled', 'session': record}
    receipt(root, report)
    report['local_access_cleanup'] = registry.close(root, memory.session_id, logout_cache)
    return report


def recover_work(root, policy, memory, registry, *, code=None, writer_tokens=None):
    report = base_report('recover', root, memory.session_id)
    if writer_tokens is not None:
        registry.recover_writers(root, memory.session_id, writer_tokens)
    report['reconciled_request'] = memory.recover()
    stored = maybe_receipt(root, memory.session_id)
    if stored and stored['status'] == 'memory_ready':
        return complete_boundary(root, policy, memory, registry, stored)
    if not stored:
        report.update(status='ready', coordination=registry.inspect(root, memory.session_id))
        report['next_action'] = 'Exact cloud request reconciled. Explicitly resume or cancel this session.'
        return report
    if stored['status'] in ('closed', 'cancelled'):
        report['local_access_cleanup'] = registry.close(root, memory.session_id, logout_cache)
        report['status'] = stored['status']
        return report
    if stored['status'] == 'preparing':
        # The checkpoint mutation may have completed before the approval code was saved.
        proposal = stored['proposed']
        from tools.memory.client import MemoryClientError
        try:
            memory.verify_checkpoint(proposal)
        except MemoryClientError as error:
            if error.code != 'record_unavailable':
                raise
            registry.quiescent(root, memory.session_id, proposal['reservation_id'])
            memory.prepare_checkpoint(root, proposal['paths'], proposal['base_head'], proposal['reservation_id'])
            memory.verify_checkpoint(proposal)
        proposal['approval_code'] = approval_code(proposal)
        stored['status'] = 'approval_required'
        receipt(root, stored)
        return stored
    if stored['status'] == 'checkpointed':
        registry.release(root, memory.session_id, activate=True)
        return stored
    if not code:
        report.update(status='ready', approval_status=stored['status'], coordination=registry.inspect(root, memory.session_id))
        report['next_action'] = 'Review retained receipt; use recover --apply CODE to retry only that exact approval.'
        return report
    proposal = proposal_from_receipt(stored, code)
    if stored['status'] == 'applying':
        registry.quiescent(root, memory.session_id, proposal['reservation_id'])
        lock_path = git_path(root, 'index').with_name('index.lock')
        if lock_path.exists():
            info = lock_path.lstat()
            if lock_path.is_symlink() or [info.st_dev, info.st_ino] != stored.get('index_lock_identity'):
                raise WorkflowFailure('unknown index lock must be preserved; inspect concurrent Git activity')
            lock_path.unlink()
        commit = stored['checkpoint_sha']
        actual_head = head(root, policy)
        if actual_head == proposal['base_head']:
            stored['status'] = 'approval_required'
            stored.pop('checkpoint_sha', None)
            receipt(root, stored)
        elif actual_head == commit and checkpoint_matches_proposal(root, commit, proposal):
            # A crash can occur after atomic ref update but before index replacement.
            # Only restore the exact recorded pre-transition index, never unknown staging.
            if index_paths(root):
                if git(root, 'write-tree').stdout.strip() != stored.get('index_base_tree'):
                    raise WorkflowFailure('index differs from recorded transition; preserve it for manual reconciliation')
                git(root, 'read-tree', commit)
            stored['status'] = 'unsynchronized'
            receipt(root, stored)
        else:
            raise WorkflowFailure('interrupted commit has an unexpected HEAD; preserve all receipts')
    result = apply_checkpoint(root, policy, memory, registry, action=proposal['action'], code=code)
    return complete_boundary(root, policy, memory, registry, result)


def parser(action=None):
    from tools.project import default_environment
    value = argparse.ArgumentParser(description=__doc__)
    if action is None:
        commands = value.add_subparsers(dest='action', required=True)
        parsers = [(name, commands.add_parser(name)) for name in
                   ('start', 'checkpoint', 'stop-work', 'refresh-context', 'session-summary', 'pause', 'resume', 'cancel', 'recover', 'status', 'logout')]
    else:
        value.set_defaults(action=action)
        parsers = [(action, value)]
    for name, command in parsers:
        command.add_argument('--root', type=Path, required=True)
        command.add_argument('--env', type=Path, default=default_environment())
        command.add_argument('--json', action='store_true')
        if name == 'start':
            command.add_argument('--resume', metavar='SESSION_UUID')
            command.add_argument('--harness')
            command.add_argument('--harness-session')
        else:
            command.add_argument('--session', required=True, metavar='SESSION_UUID')
        if name in ('checkpoint', 'stop-work', 'session-summary', 'pause', 'recover'):
            command.add_argument('--writers-quiescent', action='store_true',
                                 help='attest relevant repository and session-memory writers have stopped')
        if name in ('checkpoint', 'stop-work', 'session-summary'):
            command.add_argument('--summary', required=name == 'session-summary')
            command.add_argument('--memory-ref', action='append', default=[], metavar='UUID:REVISION')
        if name in ('checkpoint', 'stop-work'):
            mode = command.add_mutually_exclusive_group()
            mode.add_argument('--message')
            mode.add_argument('--apply', metavar='APPROVAL_CODE')
        if name == 'pause':
            command.add_argument('--reservation', metavar='RESERVATION_UUID')
        if name == 'cancel':
            command.add_argument('--approval-only', action='store_true')
        if name == 'recover':
            command.add_argument('--apply', metavar='APPROVAL_CODE')
            command.add_argument('--writer-token', action='append', default=None,
                                 help='exact interrupted writer token; requires explicit writer-quiescence attestation')
    return value


def status_exit(status):
    if status in ('ready', 'closed', 'checkpointed', 'cancelled'):
        return EXIT_OK
    if status == 'approval_required':
        return EXIT_APPROVAL_REQUIRED
    if status == 'unsynchronized':
        return EXIT_UNSYNCHRONIZED
    return EXIT_BLOCKED if status == 'blocked' else EXIT_ERROR


def render_human(report):
    # Human mode retains the same complete context/receipts as JSON; no hidden approval fields.
    return 'MAVEN WORKFLOW ' + report['action'] + ' ' + report['status'] + '\n' + json.dumps(report, indent=2, sort_keys=True)


def _main(argv=None, *, action=None):
    from tools.project import load_project, project_values
    from tools.memory.boundary import BoundaryError, MemoryBoundary
    from tools.memory.client import MemoryClientError
    from tools.memory.environment import configured_client, load_environment
    from tools.coordination import SessionRegistry, session_lock, session_uuid
    args = parser(action).parse_args(argv)
    root = args.root.expanduser().resolve()
    identifier = getattr(args, 'session', None) or getattr(args, 'resume', None) or str(uuid.uuid4())
    report = base_report(args.action, root, identifier)
    client = None
    registry = SessionRegistry(args.env.expanduser().resolve().parent)
    try:
        session_uuid(identifier)
        project = load_project(root)
        if not project['git_enabled'] and args.action not in ('status', 'recover', 'cancel', 'logout'):
            raise WorkflowFailure('optional Git is not configured; use maven-project git after enabling memory')
        values = project_values(load_environment(args.env), project)
        policy = load_policy(root, project)
        if args.action in ('checkpoint', 'stop-work', 'session-summary', 'pause') and not args.writers_quiescent:
            raise WorkflowFailure('explicit relevant-writer quiescence attestation is required')
        if args.action == 'recover' and not args.writers_quiescent:
            raise WorkflowFailure('exact-request recovery requires explicit relevant-writer quiescence attestation')
        if args.action in ('checkpoint', 'stop-work') and args.apply and (args.summary or args.memory_ref):
            raise WorkflowFailure('apply cannot rewrite the approved session summary or memory references')
        with session_lock(root, identifier):
            if args.action == 'start' and not args.resume:
                if args.harness_session and not args.harness:
                    raise WorkflowFailure('--harness-session requires explicit --harness')
                registry.register(root, project['project_id'], identifier, values,
                                  harness=args.harness, harness_session=args.harness_session)
            else:
                try:
                    registered = registry.inspect(root, identifier)['session']
                except BoundaryError:
                    terminal = maybe_receipt(root, identifier)
                    if args.action not in ('status', 'recover') or not terminal or terminal['status'] not in ('closed', 'cancelled'):
                        raise
                    registered = None
                if registered is not None and (
                        registered['project_id'] != project['project_id'] or registered['caches'] != registry.caches(values)):
                    raise WorkflowFailure('selected project or shared credential binding differs from this session')
            if args.action in ('checkpoint', 'stop-work', 'session-summary'):
                from tools.memory.boundary import private_path
                if private_path(root, f'.maven/runtime/sessions/{identifier}/lifecycle.json').exists():
                    from tools.memory.lifecycle import SessionService
                    from tools.memory.maintenance import Maintenance
                    bound = SessionService(root, identifier, env=args.env)
                    if Maintenance(bound).status()['pending_count']:
                        raise WorkflowFailure('complete or resolve accepted memory maintenance before optional Git publication')
                registry.use_git(root, identifier)
            stored_receipt = maybe_receipt(root, identifier)
            if args.action == 'status':
                report.update(status='ready', coordination=registry.inspect(root, identifier) if registered else None,
                              peers=registry.peers(root, identifier), approval=stored_receipt)
            elif args.action == 'pause':
                report.update(status='ready', coordination=registry.pause(root, identifier, args.reservation))
            elif args.action == 'logout':
                report.update(status='ready', local_access_cleanup=registry.revoke(root, identifier, logout_cache))
            elif (args.action == 'recover' and stored_receipt
                  and stored_receipt['status'] in ('closed', 'cancelled') and not registry.pending(root, identifier)):
                # Terminal cloud read-back was persisted before cleanup; local logout needs no fresh login.
                report.update(status=stored_receipt['status'],
                              local_access_cleanup=registry.close(root, identifier, logout_cache))
            else:
                client = configured_client(args.env, values=values)
                memory = MemoryBoundary(client, root, project['project_id'], identifier,
                                        harness=getattr(args, 'harness', None),
                                        harness_session=getattr(args, 'harness_session', None))
                if args.action in ('start', 'resume'):
                    report = start_work(root, policy, memory, registry,
                                        resume=args.action == 'resume' or bool(args.resume))
                elif args.action == 'refresh-context':
                    report.update(status='ready', memory=memory.refresh(), peers=registry.peers(root, identifier))
                elif args.action == 'session-summary':
                    ensure_no_pending(memory)
                    stored = maybe_receipt(root, identifier)
                    if stored and stored['status'] in ('preparing', 'approval_required', 'applying', 'unsynchronized', 'synchronized', 'memory_ready'):
                        raise WorkflowFailure('cancel or recover pending approval before replacing its summary')
                    report.update(status='ready', **summarize(root, policy, memory, registry,
                                                             args.summary, parse_refs(args.memory_ref)))
                elif args.action in ('checkpoint', 'stop-work'):
                    if args.apply:
                        report = apply_checkpoint(root, policy, memory, registry, action=args.action, code=args.apply)
                    else:
                        if args.summary:
                            ensure_no_pending(memory)
                            stored = maybe_receipt(root, identifier)
                            if stored and stored['status'] in ('preparing', 'approval_required', 'applying', 'unsynchronized', 'synchronized', 'memory_ready'):
                                raise WorkflowFailure('pending approval cannot be overwritten by a new summary')
                            summarize(root, policy, memory, registry, args.summary, parse_refs(args.memory_ref))
                        report = inspect_checkpoint(root, policy, memory, registry, action=args.action, message=args.message)
                    if report['status'] in ('memory_ready', 'synchronized'):
                        # Durable terminal intent also covers a crash after a memory-only close.
                        receipt(root, report)
                        report = complete_boundary(root, policy, memory, registry, report)
                elif args.action == 'cancel':
                    report = cancel_work(root, policy, memory, registry, approval_only=args.approval_only)
                elif args.action == 'recover':
                    report = recover_work(root, policy, memory, registry, code=args.apply, writer_tokens=args.writer_token)
    except (WorkflowFailure, BoundaryError, MemoryClientError, OSError, ValueError) as error:
        # Never overwrite an approval or uncertain receipt with a generic error report.
        report['status'] = 'blocked'
        report.setdefault('issues', []).append(str(error))
        report['next_action'] = 'Inspect this session status; preserve receipts and use explicit cancel or recover.'
        with contextlib.suppress(Exception):
            report['coordination'] = registry.inspect(root, identifier)
            stored = maybe_receipt(root, identifier)
            if stored and stored['status'] in ('applying', 'unsynchronized', 'synchronized'):
                report['status'] = 'unsynchronized'
                report['retained_approval'] = stored
        if isinstance(error, MemoryClientError) and error.code in {
                'unexpected_login_identity', 'authentication_required', 'not_authorized',
                'selected_login_unavailable_or_expired', 'selected_login_or_role_unavailable',
                'invalid_service_response', 'invalid_transport_ca_bundle',
                'login_cache_must_be_owner_private'}:
            try:
                report['local_access_cleanup'] = registry.revoke(root, identifier, logout_cache)
                report['coordination'] = registry.inspect(root, identifier)
                report['next_action'] = (
                    'Shared local access cleanup was requested after a security failure; inspect its verified result. '
                    'Preserve receipts; resolve the failure and explicitly log in before resuming affected sessions.')
            except (BoundaryError, OSError) as cleanup_error:
                report['issues'].append('security cleanup could not be verified: ' + str(cleanup_error))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                report.setdefault('issues', []).append('invocation memory client could not be closed')
                report['status'] = 'blocked'
    cleanup = report.get('local_access_cleanup')
    if cleanup and cleanup['issues']:
        report['issues'].extend(cleanup['issues'])
        report['status'] = 'blocked'
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_human(report))
    return status_exit(report['status'])


def start_main(argv=None):
    return _main(argv, action='start')


def checkpoint_main(argv=None):
    return _main(argv, action='checkpoint')


def stop_main(argv=None):
    return _main(argv, action='stop-work')


def main(argv=None):
    return _main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
