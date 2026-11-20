"""Optional, owner-reviewed Git publication. No memory writes or cloud client."""
from __future__ import annotations

from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from tools import git_workflow as git
from tools.project import load_project, validate_accepted_url, validate_git
from tools.uninstall.common import read_private, save_private


class Configuration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    remote: str = Field(min_length=1)
    accepted_url: str = Field(min_length=1)
    branch: str = Field(min_length=1)


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid')
    origin: str = Field(min_length=1, max_length=160)
    excerpt: str = Field(min_length=1, max_length=2000)


class Approval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved: StrictBool
    candidate: str = Field(min_length=64, max_length=64)
    source: Source


def configure(root, project, configuration, approval):
    from tools.project import configure_git_locked
    target = git.publication_directory(root) / 'configuration.json'
    git.require_no_pending(root)
    if approval is None:
        if configuration is None:
            raise git.WorkflowFailure('select an exact Git destination to review')
        selected = configuration.model_dump()
        validate_accepted_url(selected['accepted_url'])
        if any(value.startswith('-') or any(ord(char) < 32 for char in value) for value in selected.values()):
            raise git.WorkflowFailure('invalid Git destination')
        configured = {**project, **selected, 'git_enabled': True}
        validate_git(root, configured)
        git.validate_remote(root, git.load_policy(configured))
        review = {'operation_id': str(uuid.uuid4()), 'project_before': project, 'configuration': selected}
        saved = {'status': 'approval_required', 'review': review, 'candidate': git.canonical_digest(review)}
        save_private(target, saved)
        return saved
    saved = read_private(target)
    review = saved['review']
    approved = approval.model_dump()
    git.validate_owner(approved, saved['candidate'])
    if (saved['candidate'] != git.canonical_digest(review)
            or configuration is not None and configuration.model_dump() != review['configuration']):
        raise git.WorkflowFailure('configuration differs from the retained exact review')
    expected = review['project_before']
    configured = {**expected, **review['configuration'], 'git_enabled': True}
    if project != expected and not (project == configured and saved.get('approval') == approved):
        raise git.WorkflowFailure('project binding or Git configuration changed after review')
    validate_git(root, configured)
    git.validate_remote(root, git.load_policy(configured))
    save_private(target, {**saved, 'approval': approved})
    result = configure_git_locked(root, **review['configuration'], expected_project=project)
    if load_project(root) != configured:
        raise git.WorkflowFailure('Git configuration read-back differs from approval')
    return {**result, 'candidate': saved['candidate']}


def register_git_tools(server, app, guarded):
    @server.tool()
    @guarded
    def maven_git(cwd: str, action: Literal['configure', 'inspect', 'apply', 'status', 'recover', 'cancel'],
                  message: str | None = None, approval: Approval | None = None,
                  writers_quiescent: bool = False, configuration: Configuration | None = None,
                  candidate: str | None = None) -> dict[str, Any]:
        """Optional Git only; never part of memory reconciliation. Explicitly select the enabled project by cwd.

        Configure reviews an exact remote, URL and branch; apply that review with configure and owner approval.
        Inspect reviews the complete changed candidate, exact commit message, destination, base and checks.
        Apply requires the returned candidate and actual owner words in approval. Inspect, apply, cancel and
        mutating recovery require all relevant repository writers stopped. Status and recover without approval
        only observe the retained receipt. Recover with the original approval retries only that candidate;
        cancel takes the exact candidate and cannot discard a possibly published commit.
        """
        with app.local(cwd) as store:
            if action == 'configure':
                if message is not None or candidate is not None:
                    raise git.WorkflowFailure('unexpected Git configuration arguments')
                with git.publication_lock(store.root):
                    project = git.publication_project(store.root, app.home)
                    if project['project_id'] != store.project_id:
                        raise git.WorkflowFailure('project binding changed')
                    return configure(store.root, project, configuration, approval)
            if configuration is not None:
                raise git.WorkflowFailure('configuration belongs only to configure')
            return git.bound_publication(store.root, action, writers_quiescent=writers_quiescent,
                                         message=message, approval=approval.model_dump() if approval else None,
                                         candidate=candidate, home=app.home, project_id=store.project_id)
