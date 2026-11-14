# Project and session workflow

## Enable once, initialize visibly

Ask your assistant to **enable MAVEN for this project**. Confirm the absolute folder,
selected instance and existing cloud project, or authorize a named new project with
real source evidence. Installation is not permission to capture unrelated chats.
The installation checkout is not automatically a project; `/` is not a valid root.
No Git repository, remote, branch or explicit no-Git choice is needed for memory.

Outside an enabled project, each session starts with one question, asked every time:
**Start Home**, **Create Project** or **Read Only** (no answer means Read Only; nothing
is captured). The answer is recorded locally only; subagents inherit it and never ask.
The assistant prints the active line (`MAVEN: Home (<label>)`, `MAVEN: project <name>`,
`MAVEN: read-only`) at start and on any change. See
[MAVEN Home and the start choice](getting-started.md#maven-home-and-the-start-choice).

For an enabled project, supported harness guidance initializes subsequent sessions:

```text
MAVEN: initializing for <project>...
MAVEN: ready — project context loaded; automatic memory updates enabled.
```

Readiness requires the actual binding, current complete applicable instructions and
dependencies, prior handover, tasks and blockers. A new project's missing handover is
normal; unavailable authority is not. Paused sessions announce that mode. Failures
show `MAVEN: initialization blocked — <reason>.` Tool discovery or stderr logs do
not establish readiness. Where automatic startup is unavailable, ask **initialize
MAVEN**; the [capability matrix](integrations.md#harness-capability-matrix) discloses
this foreground fallback.

Each new/forked work session has a distinct MAVEN identity. The assistant retains
its explicit root/project/session binding across cwd changes and compaction, then
reloads current authority. Resume selects the original session explicitly, never the
newest receipt. A transport process is not a work session. Harness IDs are associated
only when reliably exposed. Tree navigation does not undo writes or revive approvals.

### Startup and recovery decisions

Optional native adapters provide the real harness conversation ID to `maven_startup`.
First inspect; then continue an unambiguous enabled-project selection. Inspection
does not create a cloud session. An exact native conversation binding can resume
automatically, including paused mode; a fresh conversation gets a separate session.
Closed sessions stay closed. Continuing old work means a new session with the
previous handover, not adopting another conversation's session.

When no project is enabled, use the Home/project/Read Only choice above. A Git root
alone does not enable MAVEN. Native hook context is routing information, never proof
that current cloud authority has loaded. If hooks are unavailable or disabled,
first-turn guidance runs the same workflow; if it is also unavailable, initialize
explicitly rather than claiming automatic readiness.

Startup lists up to 100 project-scoped session headers, reads each listed session's
current record, and reports incomplete pagination explicitly. Search results do not
contain session bodies; peer state, revision and the `recorded_at` timestamp come from
the record read. Old record timestamps are only a `possibly_stale` warning; initialized
project context also includes peers' checkpoint activity.
An observed prior end event without a verified finish is unfinished-work evidence,
not proof of worker death. Offer review recovery, start new leaving peers alone, or
Read Only. Never automatically close a peer, steal its task claims, or replay writes.
Same-conversation pending/closing work retains its binding for exact recovery;
use a separate conversation if the owner wants new work without recovering it.

### Switch an already-bound folder

Ask **“Switch this folder to the existing project …”**. The assistant begins
`maven_admin` kind `project` with the exact `root`, then selects form mode `switch`,
the destination `project_id`, and an empty `name`. Home administration has the same
explicit mode and its separate Home replacement consent.

The review pins the old/new project IDs, instance, local settings, preservation
path and Home effect. Finish local sessions and resolve writers/unknown operations
first. MAVEN archives the entire old `.maven` under a reviewed private sibling
`.maven-preserved-<operation UUID>/.maven`, then installs the new binding; ordinary
project files and Git configuration stay in place. A persistent barrier blocks new
work during interrupted cutover. Resume the original reviewed operation rather
than manually editing configuration or deleting locks.

The old cloud project, records and Home tag are not deleted, merged or archived.
Other machines are unchanged. If this folder was this machine's Home, the reviewed
switch updates that local selection too. Cloud cleanup is a separate owner decision.
The Home index uses `is_current_context` to identify the viewed project; it does not
claim that only one project is Home across every machine.

### Archive or reactivate a cloud project

Ask **“Archive cloud project …”**. The assistant resolves its exact ID and begins
`maven_admin` kind `project_lifecycle` with
`{"action": "archive", "project_id": "<cloud UUID>"}`. No local root, temporary
binding or uninstall is involved. Review the account, region, instance, selected
administration identity, project name/ID and state. Approve only that exact review
and confirm affected writers are quiescent; unresolved cloud work blocks application.
This affects the project on every machine but changes no local binding or Home setting.

The project and its contents disappear from normal search/read, Home's project index,
startup context and instruction discovery. Explicit archived-history requests use
`include_archived=true` on `maven_search`/`maven_read`; results carry
`project_archived` and remain reference data, never authority or permission to write.
Forget/Erase and source visibility still apply. Existing conversations, exports and
private recovery evidence are not erased by archival.

Reactivation uses the same administration kind with `action="reactivate"` and a new
exact review. It restores visibility and work eligibility, not forgotten/erased
content or local attachment. Existing installations must first complete the reviewed
database migration `017_project_archive_visibility.sql` and service update.



## Everyday memory

Within the enabled scope, the assistant maintains meaningful decisions, plans, tasks,
blockers, verified outcomes and handovers automatically. It preserves uncertainty and
distinguishes proposals from accepted decisions; it does not archive the transcript
or indiscriminately upload source files. Routine reconciliations need no approval for
each record and stay quiet on success.

| Ask | Result |
| --- | --- |
| “Remember this decision.” | Explicit save; confirmation only after verified cloud receipt, otherwise pending/blocked/uncertain |
| “What did we decide?” | Applicable universal plus current-project memory, with source attribution |
| “Consult the other project's approach.” | Explicit cross-project consultation, not an automatic merge of decisions |
| “Reconcile the plan with these changes.” | Bounded semantic assignment that reads current records and reconciles affected tasks/plan |
| “That lesson applies everywhere.” / “Keep this project-specific.” | Source-linked scope correction retaining history and project caveats |
| “MAVEN status.” | Binding, readiness, maintenance mode, pending assignments, uncertain requests and last verified relevant results |
| “Clean up my memory.” | Read-only clean-up review (`maven_admin` kind `cleanup`, action `review`): candidates grouped by suggested Forget, Erase or Archive; nothing changes until the owner chooses per item |

The owner controls knowledge throughout the selected instance. Scopes organize
retrieval, not hard owner access walls. Default consultation includes universal and
current-project records; other projects require explicit intent. The assistant may
promote general lessons without a per-record approval, retaining source record/revision
and distinguishing general knowledge from local choices. Promotion never activates
an instruction. Complete governing instructions are loaded; other universal knowledge
is retrieved when relevant, not dumped into every turn.

Where the harness supports real background subagents, a scoped maintenance agent
retrieves, interprets, selects scope, reconciles and submits changes while unrelated
conversation continues. Otherwise the foreground assistant performs that reasoning.
MCP represents assignments and results; it does not spawn models. A deterministic
network worker or an immediate queued reply is not proof of background reasoning.
Assignments preserve actual source evidence and session ownership; revision checks,
task fences and generations prevent stale workers from replacing newer intent.
Partial multi-record work retains per-effect receipts, not a false atomic-success claim.

## Checkpoint and wrap up

**“Make a checkpoint”** establishes an explicit point-in-time cutoff. The assistant
announces preparation, completes included assignments and saves a coherent handover
with exact record/revision references. New discussion belongs to subsequent maintenance,
not the checkpoint being prepared. Later record changes do not rewrite its meaning.
Ordinary checkpoints are nonblocking where the harness can deliver later completion;
foreground-only clients disclose that limitation. Pending, conflicted or uncertain
included work prevents a saved claim.

**“Checkpoint and wait”** requests synchronization through that same defined boundary.
Each tool wait is bounded (20 seconds by default, at most 120); if included work is
unfinished it returns pending, not saved. The assistant retains the checkpoint identity,
finishes included semantic work through a native worker or foreground fallback, then
advances that same checkpoint. Waiting alone does not launch a reasoning worker.
**“Wrap up this session”** reconciles required work, saves the handover/checkpoint and
closes only this session once cloud persistence is verified and no closure-blocking
memory operations remain. Unpublished code can be reported as unfinished work without
blocking memory closure. Neither operation backs up source files, commits, pushes,
activates instructions, closes a peer session or revokes shared credentials.

At checkpoint completion, explicit wrap-up or a requested summary, show **universal
updates/promotions**, **project updates**, and **pending work**. Do not interrupt after
every routine successful write. Explicit saves, checkpoint progress/completion,
mode changes, conflicts, failures and requests for owner decisions remain visible.

### Exiting the harness

The optional OMP adapter offers **Wrap up and exit**, **Exit without wrapping up**,
or **Stay** for an open exact-conversation binding. It cancels the immediate exit
before asking the agent to pause new automatic maintenance, finish accepted work,
checkpoint and close. It checks the exact closure receipt, not the assistant's text,
before exiting. Failure leaves the harness open; report pending work and paused mode.

OMP's hook does not cover forced exit, OS signals, restart, or programmatic shutdown;
a repeated quit can bypass the pending dialog. Claude Code, Codex, Copilot CLI and
Gemini CLI end hooks cannot veto exit. They record only best-effort local evidence,
never a false successful wrap-up. Missing end events are expected after crashes.
Use the next startup's recovery workflow; previously verified cloud memory remains
saved even when final reconciliation or handover is unfinished.


## Pause and resume

**“Pause automatic memory updates”** stops new automatic assignments in this session,
not other projects or sessions. Active work pauses at safe boundaries where supported;
already-submitted writes may finish. Preserve previously accepted assignments and
receipts. Do not build a separate paused transcript archive or backlog.

Explicit save, consult and checkpoint requests remain available while paused, and
required instruction checks remain in force. **“Resume”** reviews available paused
discussion and creates bounded catch-up work. Report missing context honestly; do not
claim complete reconstruction. **“Resume fresh”** resumes without looking back.
Both mode changes are visible and survive normal session restoration.

## Outages and recovery

Report degraded state and distinguish **pending locally**, **cloud-saved**, **blocked**
and **uncertain**. Private local journals retain bounded authorized assignments and
original request identities; they are evidence, never alternate durable memory.
Continue unrelated conversation, but wait for operations requiring unavailable current
memory or authority. Cached instructions do not replace current authority.

On recovery, retrieve current records before semantic reconciliation. Reconcile an
uncertain submitted write with its original request key, not a new duplicate request.
Read-only status does not replay mutations; explicit recovery may. Preserve private
runtime directories and receipts. Do not silently log in, change identities, elevate
permissions, clear locks or discard evidence. Cancellation stops future work at safe
boundaries and cannot roll back completed external effects.

**“Review MAVEN recovery”** uses `maven_diagnose` for local state and repair choices;
`maven_startup(action="inspect")` also checks sessions in the selected cloud project.
Repair is specific: renew the selected login, reconcile an original request/checkpoint,
finish its original closure, close an owner-confirmed stopped peer, or review a folder
switch/local integration change. Read-only diagnosis never changes cloud records.
Unknown writes require real relevant-worker quiescence before `maven_recover`.
Neither a timestamp nor an end-hook event is that attestation.


## Optional Git publication

Ask explicitly for Git assistance when source publication is intended. Configure and
confirm the actual current branch, remote and accepted push URL separately from memory
enablement. Do not embed credentials in URLs. The assistant reviews exact candidate
paths/digests, HEAD, destination, pre-existing commits, configured checks and proposed
effects. Commit/push requires approval of that exact action-specific candidate;
changed bytes, HEAD or destination require a new review. A memory-checkpoint request
is never publication approval. MAVEN does not silently pull, merge, rebase, reset,
switch branches or publish pre-existing local commits.

Publication retains cooperating-writer registration, repository reservations, truthful
quiescence and peer acknowledgements. These are optional Git protections, not a freeze
required to save a note. Local coordination does not sandbox arbitrary editors or
other hosts. Unknown commit/push outcomes retain their original candidate and receipts;
reconcile explicitly rather than retry blindly or repair Git destructively. A previous
publication receipt must be recovered with its original supported path before unsafe
cutover; it cannot be relabeled as a memory-native checkpoint.

## Recovery interfaces and removal

Normal work uses conversation and discovered typed MCP tools, not owner-assembled
UUIDs, JSON or approval hashes. CLI/TUI remain available for deliberate recovery and
secure handoffs. Inspect installed help for the exact release's arguments:
`maven-workflow` is the memory lifecycle; `maven-git` is optional publication.
Their pending receipts and approval meanings are not interchangeable.

Stopping work is not uninstalling MAVEN. [Removal](getting-started.md#uninstall-and-retirement)
selects project, local-machine and cloud scopes independently, preserving original
recovery evidence and reporting retained resources. Never start another session to
bypass retirement or removal barriers.

## MCP operation reference

The assistant discovers typed schemas rather than asking the owner to assemble these
arguments. The shipped tool groups are:

| Intent | Tools and important boundaries |
| --- | --- |
| Selection and current context | `maven_status(cwd=…, harness=…, harness_session=…)`, `maven_startup` (`inspect`, `continue`, explicit `new`; `home=true` only on Home choice), `maven_project`, `maven_initialize`, `maven_start` (`read_only`/`off`), `maven_context`; no guessed conversation IDs or implicit capture |
| Inspection and consultation | `maven_status(summary=true)`, `maven_instructions`, `maven_search`, `maven_read`; default `scope="context"`, explicit `project`, `universal` or `all` as intended. Search is ranked with `rank`/`snippet` (page with `offset`/`next_offset` for queries, `after_id` otherwise; `limit` ≤ 20; `include_archived` for obsolete records). `unbound=true` is a read-only universal consultation without a session, used only on explicit owner intent; `include_home=true` adds the configured Home |
| Mutations and scope | `maven_save`, `maven_scope`, `maven_claim`; real source origin/excerpt, expected revisions and actual task fences |
| Semantic assignments | `maven_maintenance` actions `enqueue`, `claim`, `apply`, `revise`, `complete`, `release`, `status`; preserve operation identity and worker token. Status/apply/revise return summaries unless `detail=true`; at most 50 changes per apply; an apply that returns `continue=true` is continued with the same token and no changes |
| Session boundaries | `maven_checkpoint`, `maven_pause`, `maven_resume`, `maven_finish`; finish uses a verified checkpoint covering accepted work. `maven_close_peer(session_id, revision, source, reason)` marks another session of this project abandoned (exact `revision` from `active_peers`) only after the owner confirms it no longer runs; never this session, refused while it holds live task claims, and that machine's local files are untouched |
| Recovery and administration | `maven_diagnose`, `maven_recover`, `maven_admin`; diagnosis does not replay mutations, recovery may; project/Home form mode `switch` is an exact reviewed folder rebind, not cloud cleanup |
| Optional publication | `maven_git` actions `configure`, `inspect`, `apply`, `status`, `recover`, `cancel`, `pause`; exact reviewed candidate/owner source and actual cooperating-writer quiescence for publication |

`maven_checkpoint` captures the cutoff and may return pending. After completing
included assignments, call it with the original `checkpoint_id` and `advance=true`;
do not create a replacement boundary. `wait=true` has the bounded wait described
above. `maven_resume` defaults to catch-up using available `discussion`, real `source`
and an honest `context_complete`; `fresh=true` skips look-back.
`nothing_to_record=true` completes catch-up inline when the paused discussion held
nothing worth saving. `maven_admin` responses omit choice checkpoints, prior responses,
navigation and retired interactions unless `detail=true`.

After a harness restart, call `maven_status` before saying connected. Its `rebind_hint`
contains an exact `binding` only when the supplied genuine harness/conversation identity
matches retained evidence; otherwise local sessions are peers, not a resume selection.
Say whether memory state came from this conversation or was re-read from MAVEN.
Client/customer engagement details stay
project-scoped even when the default prefers universal. Offer to turn standing owner
policies into project or universal instructions through the instruction review flow.
Mark obsolete ordinary records with tag `archived` (or `data.state` `archived`);
cloud projects use the separate lifecycle operation above, never a tag workaround.

**Clean-up** is offered on the owner's request, never automatically. The read-only
review lists stale open tasks, duplicates, archived and superseded records, oversized
notes and universal records tagged `client:*`, each with a suggested action.
Ordinary record archival is a normal `maven_save` revision adding `archived`.
Archived cloud projects and their contents are omitted from this default review.
**Forget** (hidden from search,
startup and every read; reversible with `restore`) and **Erase** (every revision,
current content, owned source excerpts, fact values and receipt results rewritten to a
content-free tombstone; irreversible) run only through an owner-approved `maven_admin`
kind `cleanup` operation with at most 200 record ids and a reason. Its exact review
lists every record and version count; approval writes an immutable receipt with the
review digest and no content. Erase caveats: Aurora automated backups keep old content
up to 30 days; other machines keep any locally cached copy until they next run a MAVEN
clean-up. Every clean-up first sweeps this machine's local runtime (every registered
project and the Home folder) for erased records and reports each file changed; an
unresolved request for an erased record blocks the sweep until it is recovered.

An interrupted closure retains its original checkpoint and finish intent. Restore
the exact binding even if initialization reports that the session is closing or
already closed. Reconcile any pending request with `maven_recover` after actual
worker quiescence, then retry `maven_finish` with the original `checkpoint_id`.
An unready connection may finish only that persisted closure; it cannot initiate
a different closure, reopen the session, or resume ordinary memory writes.

Worker handoff preserves blocked/applied reconciliation phases. After a definitive
write rejection, inspect current records and revise the same assignment; its failure
evidence is retained while the rejected request no longer blocks the session journal.
Unknown outcomes retain their fences until exact-request recovery proves the result.
Owner working-context assembly excludes synthetic records without weakening exact-read
scope checks.

A worker with its own MCP connection initializes with the originating `root` and
`session_id`, `resume=true`, and its actual `assignment_id`. Preserve the originating
binding's actual harness metadata rather than inventing another identity. A worker
sharing the main connection does not rebind it. Assignment-specific connections cannot
take over session lifecycle/admin operations. Workers claim and apply their assigned
change set, not independent explicit saves.

Task transitions are checkpoint-tracked. `maven_claim` accepts an optional
`operation_id` for the primary assistant. Workers instead submit a `TaskChange`
through maintenance `apply`/`revise`: `kind="task"`, `task_action` (`claim`, `release`
or `complete`), exact `record_id`, `expected_revision`, `scope`, and applicable
`lease_seconds`, `fence` or `request_key`. Preserve actual returned fences across
phases; discussion alone does not establish ownership or completion.
Apply the claim phase first and observe its returned fence. Only after that phase
is fully verified, revise the assignment for the next phase, apply it with the actual
fence/current revision, then complete the assignment. Primary `maven_claim` operations
also participate in the accepted-work cutoff, not a separate untracked task path.

Public raw-record and workspace memory writes use the same maintenance sequencing,
so checkpoint boundaries account for them too. Manifests exceeding 100 references
are stored as bounded exact-reference chains; subsequent boundaries link the prior
checkpoint. This storage detail does not change the declared cutoff, historical
revision meaning or the requirement to verify all included work before completion.

Optional Git setup uses `maven_git(action="configure")` with `configuration`
containing the selected `remote`, `accepted_url` and `branch`. It returns the full
persisted review and candidate digest. Only after actual owner review, repeat
configure with `approval` containing `approved=true`, that exact `candidate`, and
real owner `source` (`origin`, `excerpt`). Publication uses the current memory
session, not a replacement identity. Git recovery accepts exact `writer_tokens`
when recovering established stopped writers; never invent tokens or infer death.

When publication waits for a peer, that peer calls `maven_git(action="pause",
reservation_id=<exact current reservation>, writers_quiescent=true)` only after
its relevant writers have actually stopped. This acknowledgement remains available
after restoring a binding whose initialization is blocked by the reservation.
`maven_pause` controls automatic memory updates and does not acknowledge Git reservations.
Cancelling a publication approval leaves ordinary memory usable and preserves the
cancelled receipt; it does not publish or close the memory session.

`maven_scope(record_id, scope, reason, preview=true)` returns a non-mutating
`classification_plan` for the primary assistant or an assigned worker; preview
does not require source evidence. A worker applies its returned changes within the
originating assignment. Direct classification (`preview=false`) is primary-only
and requires real source evidence.

`maven_admin` supports `begin`, `respond`, `advance` and read-only `status`. Respond
records the actual owner response against the persisted interaction ID/revision and
full review digest; a separate advance performs the next explicit phase. Recovery
requires actual relevant workers to have stopped, never an age-based inference.
