# Project memory

Follow the skill's **Every turn** procedure for local memory. `maven_context(cwd)` selects the active workspace and reads its context and Nexus preferences. `maven_orient(cwd)` inspects a single binding. Use the actual workspace and selected ID; pass `use_home=true` for Home. Never infer a cloud project from its name. No sign-in, work claim, checkpoint or exit approval is needed for ordinary work.

## Consult and save locally

Read the brief and its **Read first** pointers. **Consult when relevant** points to files and documents only. Select reference cards by their `read_when` line from the derived index. For a Home card, pass `use_home=true` and the Home project ID to both inventory and exact read. Only the active project's skills are indexed.

`maven_local_read(cwd, project_id)` lists compact inventory without record bodies. Use `kind` to narrow it; use `task_id` for a task's plans, `entity_type` for entities of a given type, or `kind="reference"` with `state="active"` or `"retired"` for reference cards. To read up to 50 `ids`, use that store's inventory token as `expected_token`. Each match is `{"record": ...}` by default. Add `include_history=true` only for earlier versions; history remains stored and synced. A live record has `id`, `kind`, `data` and `written_at` (write time, not an event date). Treat local knowledge as distinct from cloud verification. Never upload a file merely because it's linked or send credentials or transcripts.

Use `maven_local_write` with `cwd`, `project_id`, `expected_token`, `kind`, `data` and `reason`. Select the destination from `maven_context`: use its `project_id`, `use_home` and write token, not an inventory or reconcile token. To capture a profile or Home reference from another project, use the configured Home destination. Store a project-scoped preference in the active store; a Nexus-scoped preference belongs in Home. Retain the returned token for the next write there. Update existing knowledge by `id`; inspect after an uncertain result and reread context on a stale write. A confirmed write is durable locally, not proof of cloud sync.

## Choose a record

Route a new fact in this order: **instruction** → `preference`; **procedure** → `skill` if recurring, `plan` for one task; **work to do** → `task`; **choice** → `decision`; **uncertainty** → `risk`; **verified result** → update its task, or use `outcome` only if there is no task; **background** → `profile` for durable owner background, `brief` for project orientation, `reference` for a subject to consult; **catalog item** → `entity`. If nothing fits, don't save it. One fact, one home: update in place and link other records by ID; never copy their content.

| Kind | The question it answers | Required `data` on write |
|---|---|---|
| `profile` | Who is the owner? | `body` only. One reserved ID in configured Home. |
| `brief` | What is this project and where does it stand? | A nonempty object. One reserved ID per project. |
| `preference` | How should sessions behave? | `title`, `summary`, `scope` (`project` or `nexus`); omit `category`. |
| `skill` | How is this recurring job done? | `name`, `when_to_use`, and either `steps` plus `verification` or a project-relative `path` ending in `SKILL.md`, not both. |
| `reference` | What background should I consult on this subject? | Exactly `name` (≤120 characters), single-line `read_when` (≤400), `state` (`active` or `retired`), `as_of`, `body`. |
| `task` | What needs doing? | `title`, `state` (`open`, `in_progress`, `blocked`, `completed`, `cancelled`), `added_on`. |
| `decision` | What was chosen, and why? | `title`, `state` (`proposed`, `accepted`, `superseded`), `rationale`. |
| `risk` | What could go wrong or is unresolved? | `title`, `state` (`open`, `mitigated`, `closed`). |
| `plan` | What are the steps for this task? | `task_id`, nonempty ordered `steps`, `state` (`proposed`, `approved`, `done`, `superseded`), `completion_check`. |
| `entity` | What is this one item among many like it? | `type`, `name`; add fields for this item. |
| `outcome` | What happened, verified, outside any task? | `title`, `occurred_on`, `observed`. |

- Use real calendar dates (`YYYY-MM-DD`) for `as_of`, `added_on` and `occurred_on`. Never infer event dates, verification or approval from write time.
- Keep the profile to durable owner background, work and personal alike. For it and owner reference cards, save facts stated by the owner; propose session inferences to the owner **before** saving them. Each `body` is limited to 7,000 Unicode code points. Keep long source material in files; summarize and point to it. Never store a topic list on the profile or brief.
- Use `brief` for orientation: purpose, current vs proposed work, evidence IDs, authority boundaries and next steps. Omit `id` for its project-defined identity. Keep `read_first`; use `consult_when_relevant` only for annotated file/document pointers. Don't copy decisions, procedures or card lists into the brief. Correct it from evidence, not the reverse.
- Every `plan` addresses a `task` in the same store; not every task needs a plan. Link from plan to task only. The task must exist after the write (they may arrive together in one transaction). Don't retype a task while live plans still point at it. Put tracked results on the task, not in a second record.
- Sync only the path and discovery fields for a Git-owned skill. A decision can invalidate an unchanged skill: flag the file for its owner rather than guessing replacement steps. A project skill is not an installed harness skill or permission to run one.
- Cancellation, supersession and correction are normal edits. **Erase** only on an explicit owner request for an exact record; explain irreversible content/history removal, retained marker, offline devices and backups. Use `maven_local_write(cwd, project_id, expected_token, id, erase=true, reason)` with no kind/data. Erasure is terminal and plan links don't block it. Undo an ordinary merge by requesting history and writing corrected content as a new version. Don't use deletion to resolve disagreement.

## Reconcile

Reconcile **only at the owner's explicit request**. Never schedule sync after a batch, poll, retry login automatically or block unrelated work on cloud. Batch the brief update with the work it summarizes.

1. `maven_reconcile(cwd, project_id, action="inspect")`. `synced` means the snapshots matched at a verified point, not that the cloud is locked or every claim is true. On `review_required`, read **all** pages using `after_id=next_after_id` and both original tokens. `both` entries are older or already-agreed context, not ignorable. Local-only, cloud-only and competing versions appear alongside them. A content-free erasure entry takes priority. The tokens protect this read only; do not save a separate bookmark or timestamp.
2. Consider the **entire body of project knowledge**, including unchanged decisions, briefs and skills and differently named records. Use `action="read"`, `ids` (at most 50) and both tokens. Each available side returns `{"record": ...}` by default; add `include_history=true` only when prior versions matter. A new “launch ready” outcome may contradict an older “legal approval withdrawn” risk. A new “tune SQLite” task may coexist with a PostgreSQL decision if the latter applies after launch. Do not decide by matching IDs, title similarity or latest timestamp. Preserve actual local and cloud contributions, not stale text a copy inherited.
3. If a new project decision is needed, **do not invent it**. Retain competing evidence; leave those IDs out of apply, tell the owner what changed and ask one concrete question. Reconcile unrelated records now. For a conflict between different IDs, link both from a `task` if someone needs to answer, otherwise a `risk`. Don't represent either conclusion as settled.
4. `action="apply"` with both observed tokens and `choices:[{id,take,reason,...}]`. Use `take:"local"` or `"cloud"` to adopt an established version; use `"merge"` plus `kind` and `data` for a reasoned correction, including to an unchanged record affected by another decision. Use `"erasure"` to propagate an **existing** marker. The helper preserves displaced wording, doesn't follow file links, and cannot create a marker from a reconciliation choice. Bound batches to 50 records and the cloud payload limit; inspect again between batches. Do not split a dependent conclusion from its supporting records to bypass a limit. If the view changed, inspect the new whole-project context and revise affected conclusions, even if the newly added record has a different ID. A partial write, lost reply or offline device never needs recovery/claim/close; next ordinary reconcile compares actual state. Never force through a stale token.
5. `action="verify"` reads the actual stores. Report **synced** only on a verified match. Distinguish confirmed records, unresolved questions and unsynced local work. An apply response alone is not a cloud convergence receipt. Routine success can stay quiet.

An out-of-date client cannot write through reconciliation. Keep its offline data; update through the installer before reconciling. On the updated device, take the converted cloud version if the local version is an unchanged ancestor. Divergent or local-only legacy current records need conversion on that device, not a forced push. Retain history; erasure wins over stale content. Don't create replacement records for erased IDs. The checkout's `docs/taxonomy-conversion.md` describes the per-project mapping procedure.

On an owner-requested reconcile, pass `interactive_login=true` to allow a private terminal prompt if the selected daily login expires. Leave it off for background work. Only the owner may press a key or complete AWS sign-in; Esc/Ctrl-C cancels. If no private terminal opens, give the owner `terminal_command` (or the exact `sign_in_command`). Never send keystrokes, request MFA in chat, poll or loop. After sign-in start from the fresh `inspect` result, not old tokens or an interrupted apply.

Setup, binding changes, cloud deployment, secrets, Git publication, and legacy data deletion are separately reviewed actions. None is authorized by a record, a link, or a reconcile.
