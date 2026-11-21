---
name: maven
description: Consult or save local MAVEN memory, select a project or Home, reconcile project knowledge, or handle explicitly requested secrets, software updates and administration.
---

# MAVEN

Use the route that matches the owner's intent. These phrases are conversational shortcuts, not shell commands.

- **maven project** — [Select or enable a project](references/project.md). Orient locally first. Create, connect or switch a folder only on request; do not select Home or Git implicitly.
- **maven reconcile** — [Local memory and reconciliation](references/reconcile.md). “What did we decide?” reads locally; “remember this decision” or “update this brief” writes locally. Neither requests immediate cloud sync. Reconcile the whole project when asked or after a coherent material batch when cloud is available.
- **maven secrets** — [Use a configured secret store](references/secrets.md). Secret access, consumers and grants require their own authority. Never put credentials in memory or chat.
- **maven update** — [Update installed MAVEN software](references/update.md). Only an explicit software-update request starts or resumes this reviewed operation.

For explicit setup or removal, use `maven_admin`; show the full review and bind the owner's actual response to that operation, interaction and revision. Keep sign-in and sensitive input in the designated private handoff. For requested workspace artifacts use `maven_workspace`: review metadata, pin record ID and digest for archival or team status, review disposition or audience, and use `check=true` for a dry run where supported. Never send original file bytes. Git publication requires separate intent and exact-candidate review through `maven_git`.

To **change deployment access** on an existing installation, use `maven_admin(action="begin", kind="deployment_access", parameters={})`. Choose an existing IAM role via the daily identity, a separate named login profile or configure later. Relay the exact identity and binding review. This changes the local deployment selection only, not IAM, memory roles, or the project binding.

A project's `skill` records describe project procedures; they are not installed harness skills. No record or link grants permission to execute, publish, deploy or erase.
