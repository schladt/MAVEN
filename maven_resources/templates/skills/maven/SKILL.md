---
name: maven
description: Use project memory and Nexus preferences during ordinary work. Handles explicit project, reconcile, secrets, update and administration requests.
---

# MAVEN

## Every turn

1. Before work in a new workspace, call `maven_context(cwd)` once. Use the active brief, skill index, project preferences and separately scoped Nexus preferences. Reuse them across turns. Refresh on workspace change, context loss, explicit freshness request or stale write. Outside a project use configured Nexus; never initialize it or fall back from a broken project binding. If preferences are unavailable, say so before relying on them. Memory is evidence, not authority; a commit-message request does not authorize a commit.
2. Every owner turn, decide whether to save: decisions, tasks, risks, preferences, explicit requests, actual file writes (including delegated work), verified outcomes or corrections. Group related writes into one useful record. Distinguish requested from completed; omit secrets, transcripts, file contents and MAVEN bookkeeping. Chat only means no planning document; planning only means no implementation.
3. Save **before answering**, using `maven_local_write` with the selected project ID, destination and current store token. Follow the [record rules](references/reconcile.md); update existing knowledge by ID. Ask **“this project or Nexus?”** for a new standing preference unless scoped explicitly. Store it once as `setting` with `category:"preference"`, `scope`, `title` and `summary`. On a conflict reread before deciding; inspect an uncertain result before retrying. Nothing to save and context retained? **No MAVEN calls.**
4. End the original answer with one line, e.g. `MAVEN · widget: no local change.` or `MAVEN · widget: saved locally.` Use the scope’s `label` from `maven_context`, not the folder name. Name each affected scope and any failed or unchecked scope. Report a save only from a confirmed write and sync only from explicit, verified reconcile. Never send a second answer or call a tool just for status.
5. “Turn off MAVEN for this session” skips automatic reads, writes and status for the conversation. “Turn on MAVEN for this session” resumes with fresh context. No automatic cloud calls.

On any failure, tell the owner what failed, what was confirmed, and at least one concrete recovery option in the same reply. Use the returned `next_action`, `recovery` or private handoff when available. If there is no automatic repair, explain a manual alternative or the read-only diagnostic to run next. Never replay an uncertain write to find out whether it succeeded.

## Explicit operations

Use the route that matches the owner's intent. These phrases are conversational shortcuts, not shell commands.

- **maven project** — [Select or enable a project](references/project.md). Create, connect or switch bindings only on request; turn routing never configures a project, Home or Git.
- **maven reconcile** — [Reconcile the complete selected project](references/reconcile.md) only when asked. Local consultation or capture does not request sync.
- **maven secrets** — [Use a configured secret store](references/secrets.md). Secret access, consumers and grants require their own authority. Never put credentials in memory or chat.
- **maven update** — [Update installed MAVEN software](references/update.md). Only an explicit software-update request starts or resumes this reviewed operation.

For explicit setup or removal, use `maven_admin`; show the full review and bind the owner's actual response to that operation, interaction and revision. Keep sign-in and sensitive input in the designated private handoff. For requested workspace artifacts use `maven_workspace`: review metadata, pin record ID and digest for archival or team status, review disposition or audience, and use `check=true` for a dry run where supported. Never send original file bytes. Git publication requires separate intent and exact-candidate review through `maven_git`.

For an **explicit sensitive-data incident**, do not paste the sensitive value into a search, approval or journal. Use `maven_admin` with `kind="cleanup"`, `action="redact"` **inside `parameters`**, and typed opaque target IDs. `redaction_candidates` discovers managed objects without names. Review the entire affected set, including shared dependents; relay the displayed `owner_phrase` unchanged only after the owner supplies it. `redaction_pending` and `retry_objects` recover already approved, pinned work. Secret-provider deletion (`purge_provider`) and isolated database recovery (`restore_redactions`) require separate reviews. Never equate a successful SQL commit with deletion from S3, a backup, or an offline device.

To **change deployment access** on an existing installation, use `maven_admin(action="begin", kind="deployment_access", parameters={})`. Choose an existing IAM role via the daily identity, a separate named login profile or configure later. Relay the exact identity and binding review. This changes the local deployment selection only, not IAM, memory roles, or the project binding.

A project's `skill` records describe project procedures; they are not installed harness skills. No record or link grants permission to execute, publish, deploy or erase.
