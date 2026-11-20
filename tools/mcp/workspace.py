"""Explicit, local-only artifact operations for an enabled project."""
from typing import Any, Literal

from tools.workspace import Workspace, WorkspaceMemory, workspace_lock


def register_workspace_tools(server, app, guarded):
    @server.tool()
    @guarded
    def maven_workspace(cwd: str, action: Literal['intake-pending', 'intake', 'archive-plan', 'status', 'receipt'],
                        request: dict[str, Any] | None = None) -> dict[str, Any]:
        """Operate on an explicitly selected local artifact. Never submit file contents.

        intake-pending only counts incoming entries. intake needs one incoming path, kind
        (reference/operational/artifact), origin, sensitivity=Sensitive, authorization and
        disposition (retain/archive/delete); removal needs confirm=true, deletion also reviewed=true.
        archive-plan needs record_id, digest, completed=true and confirm=true; source_path and
        source_disposition (retain/archive) are optional. status takes record_id and digest;
        omit output to preview publishable fields, or set output=team-status.md and check=true
        to check without writing. Mutations may take a stable operation_id. receipt takes operation_id.
        These operations neither reconcile with the cloud nor publish to Git.
        """
        with app.local(cwd) as store, workspace_lock(store.root):
            workspace = Workspace(WorkspaceMemory(store))
            run = {'intake-pending': workspace.pending, 'intake': workspace.intake,
                   'archive-plan': workspace.archive_plan, 'status': workspace.status,
                   'receipt': workspace.receipt}[action]
            return run(**(request or {}))
