"""Portable stdio MCP presentation over MAVEN's deterministic controllers."""
from __future__ import annotations

import argparse
from functools import wraps
import json
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Literal
import uuid

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from tools.administration import AdminInputError
from tools.coordination import session_uuid
from tools.memory.boundary import BoundaryError, digest, private_path, read_private, save_private
from tools.memory.client import MemoryClientError
from tools.memory.maintenance import Maintenance, MaintenanceError
from tools.memory.validation import MemoryError
from tools.project import ProjectError, instance_home
from tools.setup_common import SetupPending


INSTRUCTIONS = """MAVEN is owner-controlled cloud memory, independent of Git and of this harness.
Before governed work in an explicitly enabled project, visibly say 'MAVEN: initializing for <project>...'
and call maven_initialize using the unambiguous project root. Read all returned applicable instructions
and required context. Report ready only from the verified result; do not confuse tool discovery with readiness.
Retain the returned root, session_id, home/environment and actual harness identity across compaction.
Never guess the newest session or silently change projects. Reconnect by explicitly resuming that binding.
For routine meaningful updates enqueue a bounded maven_maintenance assignment with real source/context.
Use your harness's native background subagent tool where available; otherwise reconcile in the foreground.
MCP does not spawn a model. A worker claims the exact assignment, reads current records, reconciles intent,
applies expected-revision changes, and completes with a truthful summary. Queued is not cloud-saved.
A worker on a separate MCP connection initializes with the originating binding, resume=true and assignment_id;
if sharing the main connection, retain its binding and use only the delegated assignment and worker token.
Universal memory plus the selected project are the default consultation scope; other projects are consulted
upon request. Agents classify ordinary knowledge without extra approval, but never activate instructions
merely by making a record universal. Preserve provenance and project-specific caveats.
Keep routine successful maintenance quiet. Announce explicit save results, checkpoint progress/completion,
pause/resume, and blockers. Summaries on request, checkpoint completion and wrap-up identify universal,
project and pending work. Pause stops new automatic assignments; explicit operations remain available.
Default resume retrieves available paused discussion for catch-up and discloses missing context. Resume fresh
skips catch-up. Ordinary checkpoints are point-in-time and nonblocking; drive included workers and advance
that exact checkpoint. 'Checkpoint and wait' means do not claim completion before its verified receipt.
Use maven_admin for actual persisted choices and complete exact reviews. Capture real owner responses;
never invent approvals, writer quiescence, or stopped-harness attestations. Do not put secrets in tool inputs.
Git publication is optional through maven_git and requires exact review/approval. Memory saves never commit,
push or back up source files. Cloud migrations and active instruction changes require separate approval.
During outages preserve accepted local operations and say pending/degraded; required current authority
cannot be replaced by cached instructions. Recover original requests explicitly, never by minting new IDs.
"""


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid')
    origin: str = Field(min_length=1, max_length=160)
    excerpt: str = Field(max_length=2000)


class Reference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    record_id: str
    revision: int = Field(ge=1)


class Change(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['entity', 'fact', 'relationship', 'task', 'decision', 'risk', 'note', 'setting']
    data: dict[str, Any]
    scope: Literal['project', 'universal'] = 'project'
    record_id: str | None = None
    expected_revision: int = Field(default=0, ge=0)
    request_key: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(default='Reconcile authorized memory', min_length=1, max_length=240)


class TaskChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['task'] = 'task'
    task_action: Literal['claim', 'release', 'complete']
    record_id: str
    expected_revision: int = Field(ge=1)
    scope: Literal['project', 'universal'] = 'project'
    lease_seconds: int | None = Field(default=None, ge=30, le=3600)
    fence: int | None = Field(default=None, ge=1)
    request_key: str | None = None
    reason: str = Field(default='Reconcile authorized task', min_length=1, max_length=240)


class GitConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    remote: str = Field(min_length=1)
    accepted_url: str = Field(min_length=1)
    branch: str = Field(min_length=1)


class OwnerApproval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved: bool
    candidate: str
    source: Source


class MavenApplication:
    """One process-local binding, restored only by explicit identity, not cwd or recency."""
    def __init__(self, *, home=None, env=None, service_factory=None, admin_factory=None):
        self.home = (Path(home).expanduser().resolve() if home else
                     Path(env).expanduser().resolve().parent if env else instance_home())
        self.env = Path(env).expanduser().resolve() if env else self.home / '.env'
        if self.env.parent != self.home:
            raise ProjectError('environment and home must select the same memory instance directory')
        self.service_factory = service_factory
        self.admin_factory = admin_factory
        self.service = None
        self.maintenance = None
        self.assignment_id = None
        self.binding = None
        self.initialized = False
        self._resume_initialization = False
        self._lock = threading.RLock()

    def initialize(self, root, *, harness='mcp', harness_session=None, session_id=None,
                   resume=False, assignment_id=None):
        from tools.memory.lifecycle import SessionService
        root = Path(root).expanduser().resolve(strict=True)
        session_id = session_uuid(session_id) if session_id else None
        assignment_id = session_uuid(assignment_id) if assignment_id else None
        with self._lock:
            if self.assignment_id is not None and assignment_id != self.assignment_id:
                raise MaintenanceError('a delegated connection cannot change its originating assignment')
            if assignment_id is not None and self.binding is None:
                self.assignment_id = assignment_id
            if self.binding:
                if (self.binding['root'] != str(root)
                        or session_id is not None and self.binding['session_id'] != session_id
                        or assignment_id != self.assignment_id
                        or harness != self.binding['harness']
                        or harness_session != self.binding['harness_session']):
                    raise MaintenanceError('this MCP process is already bound; use the exact original binding')
            else:
                if resume and not session_id:
                    raise MaintenanceError('resume requires the exact original session identity')
                if assignment_id and (not resume or not session_id):
                    raise MaintenanceError('a delegated worker requires its originating session and assignment')
                identifier = session_id or str(uuid.uuid4())
                path = f'.maven/runtime/sessions/{identifier}/mcp-binding.json'
                stored = read_private(root, path) if private_path(root, path).exists() else None
                if stored is not None and not resume:
                    raise MaintenanceError('restoring this binding requires explicit resume')
                if assignment_id and (stored is None or not stored.get('initialized')):
                    raise MaintenanceError('a worker cannot initialize or replace its originating session')
                selection = {'root': str(root), 'session_id': identifier,
                             'home': str(self.home), 'env': str(self.env),
                             'harness': harness, 'harness_session': harness_session}
                if stored and any(stored.get(key) != value for key, value in selection.items()):
                    raise MaintenanceError('selection differs from the original persisted MCP binding')
                factory = self.service_factory or SessionService
                service = factory(root, identifier, env=self.env, harness=harness,
                                  harness_session=harness_session)
                maintenance = Maintenance(service)
                try:
                    if stored and stored.get('project_id') != service.project_id:
                        raise MaintenanceError('project differs from the original persisted MCP binding')
                    if assignment_id:
                        maintenance.status(assignment_id)
                except Exception:
                    service.close()
                    raise
                self.service, self.maintenance = service, maintenance
                self.assignment_id = assignment_id
                self.binding = {**selection, 'project_id': service.project_id}
                self._resume_initialization = (resume if stored is None else
                                               stored.get('initialized', False) or stored.get('resume_required', False))
                if not assignment_id and stored is None:
                    save_private(root, path, {**self.binding, 'initialized': False,
                                             'resume_required': resume})
            self.initialized = False
            if self.assignment_id:
                result = self.service.context()
            else:
                result = self.service.initialize(resume=self._resume_initialization)
            return self._ready(result)

    def _ready(self, result):
        if not isinstance(result, dict) or result.get('status') != 'ready':
            return {**(result if isinstance(result, dict) else {'status': 'blocked'}),
                    'binding': dict(self.binding),
                    'message': 'MAVEN: initialization blocked; inspect the preserved session before continuing.'}
        state = self.maintenance.status()
        if self.assignment_id is None:
            save_private(self.service.root,
                         f'.maven/runtime/sessions/{self.service.session_id}/mcp-binding.json',
                         {**self.binding, 'initialized': True})
        self.initialized = True
        self._resume_initialization = True
        return {'status': 'ready', 'binding': dict(self.binding), 'memory': result,
                'automatic_updates': state['automatic_updates'], 'pending_count': state['pending_count'],
                'assignment_id': self.assignment_id,
                'message': 'MAVEN: ready — project context loaded; automatic memory updates ' +
                           ('enabled.' if state['automatic_updates'] else 'paused.')}

    def bound(self, *, require_ready=True):
        if self.service is None or require_ready and not self.initialized:
            raise MaintenanceError('initialize the explicitly selected project successfully first')
        return self.service

    def primary(self):
        if self.assignment_id is not None:
            raise MaintenanceError('delegated memory workers cannot change session lifecycle or administration')

    def assignment(self, operation_id):
        if self.assignment_id and operation_id != self.assignment_id:
            raise MaintenanceError('this worker connection is limited to its delegated assignment')

    def consultation(self, scope, project_id):
        if self.assignment_id and (scope == 'all' or project_id not in (None, self.service.project_id)):
            raise MaintenanceError('delegated consultation is limited to universal and originating-project memory')

    def admin(self):
        self.primary()
        from tools.administration import AdminOperations
        return (self.admin_factory or AdminOperations)(self.home)

    def close(self):
        if self.service is not None:
            self.service.close()

class MavenMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        try:
            result = await super().call_tool(name, arguments, context)
        except Exception:
            # SDK validation errors can include complete input values; never echo them.
            payload = {'status': 'error', 'error': 'invalid_or_unavailable_tool_call',
                       'cloud_saved': False}
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))],
                                  structured_content=payload, is_error=True)
        if isinstance(result, CallToolResult) and isinstance(result.structured_content, dict):
            status = result.structured_content.get('status')
            result.is_error = result.is_error or status in {
                'error', 'blocked', 'uncertain', 'failed', 'unsynchronized'}
        return result



def create_server(application=None):
    app = application or MavenApplication()
    server = MavenMCPServer('MAVEN', instructions=INSTRUCTIONS, version='0.1.0', log_level='WARNING')
    read_hint = ToolAnnotations(read_only_hint=True, open_world_hint=True)

    def guarded(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except MemoryClientError as error:
                return {'status': 'uncertain' if error.code == 'outcome_unknown' else 'blocked',
                        'error': error.code, 'request_key': error.request_key,
                        'binding': app.binding, 'cloud_saved': False,
                        'message': 'MAVEN: memory unavailable or blocked; preserve pending operations and inspect status.'}
            except MaintenanceError as error:
                return {'status': 'blocked', 'error': str(error), 'binding': app.binding, 'cloud_saved': False}
            except (BoundaryError, ProjectError):
                return {'status': 'blocked', 'error': 'selection_or_operation_blocked',
                        'binding': app.binding, 'cloud_saved': False,
                        'next_action': 'Inspect the exact session status and retained operation evidence.'}
            except MemoryError as error:
                return {'status': 'blocked', 'error': error.status, 'cloud_saved': False}
            except AdminInputError as error:
                return {'status': 'error', 'error': 'invalid_request', 'message': str(error),
                        'binding': app.binding, 'cloud_saved': False}
            except SetupPending as error:
                return {'status': 'blocked', 'error': 'setup_pending', 'message': str(error),
                        'binding': app.binding, 'cloud_saved': False}
            except (ValueError, TypeError, KeyError, OSError):
                return {'status': 'error', 'error': 'invalid_or_unavailable_operation',
                        'binding': app.binding, 'cloud_saved': False}
            except Exception:
                # Do not reflect SDK/process error strings which can contain credentials.
                return {'status': 'error', 'error': 'operation_failed', 'binding': app.binding,
                        'cloud_saved': False, 'next_action': 'Inspect retained operation evidence before retrying.'}
        return invoke

    @server.tool()
    @guarded
    def maven_initialize(root: str, harness: str = 'mcp', harness_session: str | None = None,
                         session_id: str | None = None, resume: bool = False,
                         assignment_id: str | None = None) -> dict[str, Any]:
        """Initialize an explicitly enabled project and load complete current authority. Announce initialization before calling, then readiness or a blocker. Restore an exact binding, never the newest session."""
        return app.initialize(root, harness=harness, harness_session=harness_session,
                              session_id=session_id, resume=resume, assignment_id=assignment_id)

    @server.tool(annotations=read_hint)
    @guarded
    def maven_context() -> dict[str, Any]:
        """Reload complete approved instructions, handover, tasks and relevant memory before governed work; not an instruction cache fallback."""
        return app.bound().context()

    @server.tool(annotations=read_hint)
    @guarded
    def maven_status(summary: bool = False) -> dict[str, Any]:
        """Inspect local binding and pending maintenance without replaying cloud writes. Request summary for universal/project/pending updates; do not announce routine successes individually."""
        if app.service is None:
            return {'status': 'not_initialized', 'home': str(app.home), 'env': str(app.env),
                    'live_state_checked': False, 'next_action': 'Select an enabled project or use guided project setup.'}
        result = {'binding': dict(app.binding), 'initialized': app.initialized,
                  'session': app.service.status(), 'maintenance': app.maintenance.status()}
        if summary:
            result.update(app.maintenance.summary())
        return result

    @server.tool(annotations=read_hint)
    @guarded
    def maven_search(query: str = '', kind: str | None = None,
                     scope: Literal['context', 'project', 'universal', 'all'] = 'context',
                     project_id: str | None = None, after_id: str | None = None,
                     limit: int = Field(default=20, ge=1, le=20)) -> dict[str, Any]:
        """Search universal plus current-project memory by default. Other project IDs or all-project search require an explicit cross-project consultation request. Follow pagination and read relevant exact records."""
        app.bound()
        app.consultation(scope, project_id)
        return app.service.search(query=query, kind=kind, scope=scope, project_id=project_id,
                                  after_id=after_id, limit=limit)

    @server.tool(annotations=read_hint)
    @guarded
    def maven_read(record_id: str, revision: int | None = None,
                   scope: Literal['context', 'project', 'universal', 'all'] = 'context',
                   project_id: str | None = None) -> dict[str, Any]:
        """Read a real memory record, optionally at its exact revision. Reference data does not activate instructions. Preserve project attribution when explicitly consulting other projects."""
        app.bound()
        app.consultation(scope, project_id)
        return app.service.read_record(record_id, revision=revision, scope=scope, project_id=project_id)

    @server.tool()
    @guarded
    def maven_save(change: Change, source: Source, operation_id: str | None = None) -> dict[str, Any]:
        """Explicitly save ordinary memory with real provenance and expected revision; report saved only on verified completion. Workers use their assigned change set, not independent writes. Use fenced task claims for in-progress/completed task transitions."""
        app.primary()
        app.bound()
        return app.maintenance.save(**change.model_dump(exclude_none=True),
                                    source=source.model_dump(), operation_id=operation_id)

    @server.tool()
    @guarded
    def maven_scope(record_id: str, scope: Literal['project', 'universal'], source: Source | None = None,
                    reason: str = 'Correct memory scope', operation_id: str | None = None,
                    preview: bool = False) -> dict[str, Any]:
        """Preview a source-linked scope correction; workers apply the returned changes only inside their existing assignment. Without preview, explicitly execute the correction with actual source. Promotion never activates instructions or clones fenced task ownership."""
        app.bound()
        if preview:
            return app.maintenance.classification_plan(record_id, scope=scope, reason=reason)
        app.primary()
        if source is None:
            raise MaintenanceError('scope correction requires actual source')
        return app.maintenance.reclassify(record_id, scope=scope, source=source.model_dump(),
                                          reason=reason, operation_id=operation_id)

    @server.tool()
    @guarded
    def maven_maintenance(action: Literal['enqueue', 'claim', 'apply', 'revise', 'complete', 'release', 'status'],
                          operation_id: str | None = None, intent: str | None = None,
                          source: Source | None = None, context: str = '', workstream: str = 'project',
                          automatic: bool = True, supersede: bool = False, worker: str | None = None,
                          worker_token: str | None = None, changes: list[Change | TaskChange] | None = None,
                          summary: str | None = None, no_changes: bool = False,
                          worker_stopped: bool = False) -> dict[str, Any]:
        """Manage bounded semantic memory work; queued is not cloud-saved. Claim, read current records, apply exact revisions, then complete. Task changes use task_action and real lease fences: apply claim, read returned task/fence, revise the next phase, apply, then complete the assignment. Revise preserves verified earlier effects. Supersede only genuinely replaced workstream intent."""
        app.bound()
        if action == 'enqueue':
            app.primary()
            if intent is None or source is None:
                raise MaintenanceError('enqueue requires intent and actual source')
            return app.maintenance.enqueue(intent, source=source.model_dump(), context=context,
                                           workstream=workstream, automatic=automatic,
                                           operation_id=operation_id, supersede=supersede)
        if operation_id is None:
            raise MaintenanceError('this maintenance action requires its original operation identity')
        app.assignment(operation_id)
        if action == 'status':
            return app.maintenance.status(operation_id)
        if action == 'claim':
            if worker is None:
                raise MaintenanceError('claim requires an actual worker label')
            return app.maintenance.claim(operation_id, worker=worker)
        if action == 'apply':
            return app.maintenance.apply(operation_id, worker_token=worker_token,
                                         changes=[item.model_dump(exclude_none=True) for item in changes]
                                         if changes is not None else None)
        if action == 'revise':
            if changes is None:
                raise MaintenanceError('revise requires a reread and reconciled change set')
            return app.maintenance.revise(operation_id, worker_token=worker_token,
                                          changes=[item.model_dump(exclude_none=True) for item in changes])
        if action == 'complete':
            if summary is None:
                raise MaintenanceError('completion requires a truthful summary')
            return app.maintenance.complete(operation_id, worker_token=worker_token,
                                            summary=summary, no_changes=no_changes)
        return app.maintenance.release(operation_id, worker_token=worker_token, worker_stopped=worker_stopped)

    @server.tool()
    @guarded
    def maven_claim(record_id: str, action: Literal['claim', 'release', 'complete'],
                    expected_revision: int, source: Source, lease_seconds: int | None = None,
                    fence: int | None = None, request_key: str | None = None,
                    operation_id: str | None = None) -> dict[str, Any]:
        """Claim, release or complete a task inside checkpoint-tracked maintenance using actual lease fences and expected revision. Workers submit TaskChange through their assigned maintenance operation."""
        app.primary()
        app.bound()
        return app.maintenance.claim_task(record_id, action=action, expected_revision=expected_revision,
                                          source=source.model_dump(), lease_seconds=lease_seconds,
                                          fence=fence, request_key=request_key, operation_id=operation_id)

    @server.tool()
    @guarded
    def maven_checkpoint(summary: str | None = None, refs: list[Reference] | None = None,
                         checkpoint_id: str | None = None, advance: bool = False,
                         wait: bool = False, wait_seconds: int = Field(default=20, ge=0, le=120)) -> dict[str, Any]:
        """Capture a point-in-time memory boundary, never a Git commit or source backup. Run pending included maintenance then advance the same checkpoint. Wait is an explicit bounded synchronization request; pending is not success."""
        app.primary()
        app.bound()
        if advance:
            if checkpoint_id is None:
                raise MaintenanceError('advance requires the original checkpoint identity')
            return app.maintenance.advance_checkpoint(checkpoint_id)
        if summary is None:
            raise MaintenanceError('checkpoint requires a truthful boundary summary')
        return app.maintenance.checkpoint(summary=summary, refs=[item.model_dump() for item in refs or []],
                                          checkpoint_id=checkpoint_id, wait=wait, wait_seconds=wait_seconds)

    @server.tool()
    @guarded
    def maven_pause(anchor: str | None = None) -> dict[str, Any]:
        """Pause new automatic maintenance in this session. Keep explicit operations and required instruction checks. Anchor references available harness history, not a copied transcript; submitted writes may finish."""
        app.primary()
        app.bound()
        return app.maintenance.pause(anchor=anchor)

    @server.tool()
    @guarded
    def maven_resume(fresh: bool = False, discussion: str | None = None,
                     source: Source | None = None, context_complete: bool = False) -> dict[str, Any]:
        """Resume with catch-up by default: retrieve available paused discussion and supply its real source. Disclose missing history. Explicit resume fresh skips look-back and creates no transcript backlog."""
        app.primary()
        app.bound()
        return app.maintenance.resume(fresh=fresh, discussion=discussion,
                                       source=source.model_dump() if source else None,
                                       context_complete=context_complete)

    @server.tool()
    @guarded
    def maven_finish(checkpoint_id: str) -> dict[str, Any]:
        """Close only this memory session after its verified checkpoint. An interrupted original closure may be finished after reconnect without reopening the session. No Git publication or shared-credential logout."""
        app.primary()
        app.bound(require_ready=False)
        return app.maintenance.finish(checkpoint_id, recover_only=not app.initialized)

    @server.tool()
    @guarded
    def maven_recover(operation_id: str | None = None, checkpoint_id: str | None = None,
                      workers_stopped: bool = False) -> dict[str, Any]:
        """Explicitly reconcile original receipts after establishing actual writer quiescence. This can replay an original cloud mutation; use status for read-only diagnosis. Never infer worker death from age."""
        app.primary()
        app.bound(require_ready=False)
        if workers_stopped is not True:
            raise MaintenanceError('recovery requires actual relevant-writer quiescence')
        if operation_id and checkpoint_id:
            raise MaintenanceError('select one exact recovery target')
        if operation_id:
            return app.maintenance.recover(operation_id, worker_stopped=True)
        if checkpoint_id:
            return app.maintenance.recover_checkpoint(checkpoint_id, worker_stopped=True)
        return app.service.recover()

    @server.tool()
    @guarded
    def maven_admin(action: Literal['begin', 'respond', 'advance', 'back', 'status'],
                     kind: Literal['setup', 'project', 'install', 'uninstall', 'recovery'] | None = None,
                     parameters: dict[str, Any] | None = None, operation_id: str | None = None,
                     interaction_id: str | None = None, revision: int | None = None,
                     response: dict[str, Any] | None = None) -> dict[str, Any]:
        """Drive real resumable administration. Present persisted questions/full exact reviews, capture actual owner responses, then explicitly advance. Back revises unsubmitted choices only, never rolls back external effects. Project/machine/cloud removal are independent. Login, secrets and affected-harness shutdown use secure external handoffs.

        respond: pass the pending_interaction's interaction_id and revision. A form response is an object with exactly its field names. A review response is {approved: bool, review_digest: <the review's digest>, source: {origin, excerpt} describing the owner's actual answer, confirmations: <the review's confirmations, usually {}>}. Invalid input returns error invalid_request with a message naming the problem.

        Updating an existing installation: begin kind "setup" with parameters {mode: "update"} (optional absolute "checkout"). It checks local software and harness skills, then the database schema, service stack and bundled instructions, asking for review only where something differs."""
        admin = app.admin()
        if action == 'begin':
            if kind is None:
                raise MaintenanceError('administration begin requires an operation kind')
            return admin.begin(kind, parameters or {})
        if operation_id is None:
            raise MaintenanceError('administration requires the original operation identity')
        if action == 'status':
            return admin.status(operation_id)
        if action == 'advance':
            return admin.advance(operation_id)
        if action == 'back':
            return admin.back(operation_id)
        if interaction_id is None or revision is None or response is None:
            raise MaintenanceError('respond requires exact interaction identity, revision and owner response')
        return admin.respond(operation_id, interaction_id, revision, response)

    @server.tool()
    @guarded
    def maven_project(parameters: dict[str, Any]) -> dict[str, Any]:
        """Begin explicit first-time project enablement or selection through persisted owner choices; parameters is {root: absolute project folder}. Memory does not require Git. Never use the installation checkout as an implicit project.

        This only begins the operation. Answer its pending_interaction and advance it with maven_admin (kind "project") using the returned operation_id."""
        return app.admin().begin('project', parameters)

    @server.tool()
    @guarded
    def maven_git(action: Literal['configure', 'inspect', 'apply', 'status', 'recover', 'cancel', 'pause'],
                   summary: str | None = None, memory_refs: list[Reference] | None = None,
                   message: str | None = None, approval: OwnerApproval | None = None,
                   writers_quiescent: bool = False, configuration: GitConfiguration | None = None,
                   writer_tokens: list[str] | None = None,
                   reservation_id: str | None = None) -> dict[str, Any]:
        """Optional Git workflow in this exact memory session. Configure and publish only with exact owner approval. Pause acknowledges the peer's exact reservation_id after actual writer quiescence; memory pause is separate. Recovery retains original request and writer identities."""
        app.primary()
        service = app.bound(require_ready=action not in ('status', 'recover', 'cancel', 'pause'))
        if action == 'pause':
            if reservation_id is None:
                raise MaintenanceError('Git pause requires the exact current reservation identity')
            reservation_id = session_uuid(reservation_id)
        elif reservation_id is not None:
            raise MaintenanceError('reservation identity belongs only to Git pause')
        if action == 'configure':
            from tools.project import configure_git, load_project, validate_accepted_url, validate_git
            from tools.memory.validation import reject_secrets, source as validate_source
            review_path = f'.maven/runtime/sessions/{service.session_id}/git-config-review.json'
            if approval is None:
                if configuration is None:
                    raise MaintenanceError('Git configuration requires an exact remote, accepted URL and branch')
                candidate = configuration.model_dump()
                candidate['accepted_url'] = validate_accepted_url(candidate['accepted_url'])
                project = load_project(service.root)
                validate_git(service.root, {**project, **candidate, 'git_enabled': True})
                review = {'binding': dict(app.binding), 'project_before': project,
                          'configuration': candidate}
                saved = {'status': 'approval_required', 'review': review, 'candidate': digest(review),
                         'message': 'Review the complete Git destination; approve only with the actual owner response.'}
                save_private(service.root, review_path, saved)
                return saved
            stored = read_private(service.root, review_path)
            review = stored['review']
            current = load_project(service.root)
            configured = {**review['project_before'], **review['configuration'], 'git_enabled': True}
            same_approved_retry = (stored.get('approval') == approval.model_dump() and current == configured)
            if (not approval.approved or not approval.source.excerpt.strip()
                    or approval.candidate != stored['candidate'] or digest(review) != approval.candidate
                    or review['binding'] != app.binding
                    or review['project_before'] != current and not same_approved_retry
                    or configuration is not None and configuration.model_dump() != review['configuration']):
                raise MaintenanceError('Git configuration approval differs from the exact current review')
            validate_source(approval.source.model_dump())
            reject_secrets(approval.source.model_dump())
            save_private(service.root, review_path, {**stored, 'approval': approval.model_dump()})
            result = configure_git(service.root, **review['configuration'], env=app.env)
            service.project = result['project']
            return {**result, 'binding': dict(app.binding), 'candidate': approval.candidate}
        if configuration is not None:
            raise MaintenanceError('configuration belongs only to the configure action')
        if action != 'status' and not writers_quiescent:
            raise MaintenanceError('Git publication requires actual relevant-writer quiescence')
        if action != 'inspect' and (summary is not None or memory_refs or message is not None):
            raise MaintenanceError('only inspection can create publication summary or candidate inputs')
        if action not in ('apply', 'recover') and approval is not None:
            raise MaintenanceError('publication approval belongs only to apply or recover')
        if action != 'recover' and writer_tokens:
            raise MaintenanceError('interrupted writer tokens belong only to exact recovery')
        command = [sys.executable, '-m', 'tools.git_workflow',
                   {'inspect': 'checkpoint', 'apply': 'checkpoint'}.get(action, action),
                   '--root', str(service.root), '--session', service.session_id,
                   '--env', str(app.env), '--json']
        if action in ('inspect', 'apply', 'recover', 'pause'):
            for token in writer_tokens or []:
                command.extend(['--writer-token', token])
            command.append('--writers-quiescent')
        if action == 'pause':
            command.extend(['--reservation', reservation_id])
        if action == 'inspect':
            if summary is not None:
                command.extend(['--summary', summary])
            if message is not None:
                command.extend(['--message', message])
            for ref in memory_refs or []:
                command.extend(['--memory-ref', f'{ref.record_id}:{ref.revision}'])
        if action == 'apply' or action == 'recover' and approval is not None:
            if approval is None or not approval.approved or not approval.source.excerpt.strip():
                raise MaintenanceError('actual owner approval of the exact publication candidate is required')
            from tools.memory.validation import reject_secrets, source as validate_source
            validate_source(approval.source.model_dump())
            reject_secrets(approval.source.model_dump())
            from tools.git_workflow import load_receipt, proposal_from_receipt
            proposal = proposal_from_receipt(load_receipt(service.root, service.session_id), approval.candidate)
            save_private(service.root, f'.maven/runtime/sessions/{service.session_id}/git-owner-approval.json',
                         {'candidate': approval.candidate, 'proposal': proposal,
                          'source': approval.source.model_dump(), 'approved': True})
            command.extend(['--apply', approval.candidate])
        if action == 'cancel':
            command.append('--approval-only')
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            return {'status': 'error', 'error': 'git_workflow_failed', 'exit_code': completed.returncode}
        if not isinstance(result, dict) or not isinstance(result.get('status'), str):
            return {'status': 'error', 'error': 'invalid_git_workflow_result',
                    'exit_code': completed.returncode}
        # The CLI's exception reports may embed subprocess or transport diagnostics.
        if result.get('issues'):
            result['issues'] = ['Git workflow blocked; inspect retained receipts and exact destination.']
        if completed.returncode and result['status'] in ('ready', 'checkpointed', 'closed', 'cancelled'):
            result['status'] = 'error'
            result['error'] = 'git_workflow_failed'
        result['exit_code'] = completed.returncode
        return result

    return server


def main(argv=None, *, application_factory=MavenApplication):
    parser = argparse.ArgumentParser(description='MAVEN stdio MCP server; launched by a configured harness.')
    parser.add_argument('--home', type=Path)
    parser.add_argument('--env', type=Path)
    args = parser.parse_args(argv)
    app = application_factory(home=args.home, env=args.env)
    try:
        create_server(app).run(transport='stdio')
    finally:
        app.close()


if __name__ == '__main__':
    main()
