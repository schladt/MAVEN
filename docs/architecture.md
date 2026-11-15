# Architecture and storage

MAVEN is an agent-operated memory system, not a model provider. A configured harness
launches the installed Python MCP process over standard **stdio** and owns its
connection lifetime. No manual server startup, custom launcher, listening port or
permanent service is needed. OMP is optional. A future Streamable HTTP adapter can
reuse domain contracts; a public endpoint or mobile application is not included.

## Layers

1. **Harness guidance:** intent routing, visible initialization, context handoff,
   decisions and native background subagents where available; foreground fallback.
2. **MCP adapter:** official Python SDK transport/discovery, typed tools, sanitized
   results and cancellation. Stdout is protocol-only; diagnostics go to stderr.
3. **Shared controllers:** explicit selection, resumable interactions, maintenance
   assignments, cutoffs, per-effect progress and verified receipts.
4. **Domain services:** the existing cloud client, validation, authority, memory
   lifecycle, optional Git publication, setup and pinned uninstall recovery.
5. **AWS durable records and private local evidence:** one authoritative memory store,
   not an alternative local database of instructions.

MCP does not provide a model loop, spawn subagents or guarantee startup hooks,
transcripts, notifications or approval dialogs. Those are harness capabilities.
Deterministic code handles I/O, identity, revision checks, coordination and recovery;
the harness agent interprets and reconciles semantic changes. No MAVEN-owned model
runtime, model credential or immortal worker conversation is required.

## Storage and identity

| Location | Purpose | Authority |
| --- | --- | --- |
| Instance `venv/`, installed resources | Executables, bootstrap guidance and proposed authority | Code, never live owner policy |
| Instance `.env` | Explicit endpoint, named profiles, identity and integration settings | Private connection settings, not credentials or memory |
| Project `.maven/project.json` | Canonical root/cloud project and optional Git configuration | Explicit local selection |
| Home `home.json` (optional, 0600) | Per-machine pointer to the owner-configured MAVEN Home project | Explicit owner opt-in via `maven_admin` kind `home` |
| Private `runtime/start-choices.jsonl`, `runtime/active/` | Local start-choice log and active line for status lines | Local only, never sent to the cloud |
| Private project/session runtime | Assignment generations, cutoff manifests, journals and receipts | Pending-work/recovery evidence, not cloud-saved memory |
| Private administration runtime | Persisted reviews, interactions and recovery references | Exact operation evidence, not blanket approval |
| Project `.maven/sensitive/` | Approved preserved originals | Private artifacts, not instructions |
| Selected AWS service | Versioned records, instructions, projects, tasks, sessions and handovers | Durable MAVEN memory |

`MAVEN_HOME` selects the installation (default `~/.maven`). Every operation resolves
home/instance, project where applicable, and work session explicitly. No global
current-project/session pointer, newest-receipt inference or cwd rebinding is allowed.
The configured MAVEN Home is the one explicit, owner-chosen root; outside an enabled
project the owner picks Start Home, Create Project or Read Only at each session start.
Transport identity is distinct from durable session identity. New/forked sessions get
new identities; explicit resume retains the original. Workers inherit scoped work
from the originating session with distinct operation/writer identity.

## Scope, concurrency and completion

Default retrieval combines universal and current-project records. Universal holds only
preferences, instructions and general lessons, never customer- or project-specific
detail; owner-level tasks and people/organization awareness belong to Home. Explicit universal,
project and owner-wide consultation remain available. Project scopes organize the
owner's knowledge; actual AWS/instance identity controls access. Scope promotion uses
source-linked representations retaining original project/revision and caveats. Scope
correction changes default visibility without erasing history. Neither makes reference
knowledge into an active instruction.

A bounded maintenance assignment records source evidence, allowed scope, uncertainty,
generation and intended work. Conflicting work is serialized, stale generations are
rejected, and cloud expected revisions/task fences protect against concurrent writers.
The session's pending-request journal must not be overwritten by another worker.
Multi-record results retain verified per-effect progress and a completion manifest;
partial effects are not an atomic transaction claim.

Read-only service calls take a shared session lock, so parallel searches and reads are
fine; writes take the exclusive lock. Both wait briefly; a lock that stays held returns
`session_busy` with retry guidance. Errors distinguish availability (warming, transport,
rate limits, login) reported as memory unavailable, from definitive rejections carrying
a bounded `detail` that names the offending field or kind, never its value. Ranked search
matches every query term at word boundaries in title, tags or body and returns snippets;
records tagged `archived` or with `data.state` `archived` are excluded unless requested.
Project lifecycle archival is separate from record tags. Migration `017` excludes
archived project entities and owned records before search ranking/pagination, and
from default exact reads, Home discovery, context and instruction routes. Explicit
`include_archived` on memory search/read permits historical reference only; it never
overrides Forget/Erase or enables project work. Archive/reactivate transitions advance
authority generation so cached instruction context must be revalidated.

Memory checkpoints record the session, explicit accepted-work cutoff, exact
record/revision manifest and truthful handover. Included work must finish before saved
completion. New work stays outside that immutable boundary. Memory does not reserve
or publish source files. Optional Git publication separately retains repository
reservations, exact-candidate approval and recovery; arbitrary external writers remain
outside its local coordination.

Operation results distinguish pending/working, awaiting decision, paused, blocked,
uncertain, completed and definitive failure/cancellation. They retain operation identity,
selection, review revisions/digests, safe next actions and verified record receipts as
applicable. Local acceptance is not cloud completion. Inspection is separate from
reconciliation that may replay an external write. Cancellation is not rollback.

## Secrets and approved consumers

Configured Read Only/Home/project modes can discover current approved skills and
load a named skill with its dependencies, without initializing or capturing.
Read Only resolves universal authority; bound requests retain their project scope.
Off/unconfigured access fails closed.

The explicit store holds SSM SecureStrings and streamed encrypted S3 files; memory
holds nonsecret catalog references, never values. Narrow Home lookup/catalog routes
allow only an exact secret setting, including an explicitly requested write from
Read Only or another project without rebinding or general Home search. Storage and
catalog commit are separate: pinned storage receipts survive catalog failure, and
retry only reconciles the catalog. File rotation creates immutable objects and
compare-and-swaps the logical pointer; SSM references pin versions.

Owner-defined declarative adapters and grants are approved cloud authority, not
catalog settings. Single-use consumption is atomic across hosts; standing grants
retain reviewed constraints. Secret delivery uses stdin/private FDs, with raw output
withheld and fail-closed policy checks. The trusted executable remains a security
boundary: this runner is not a sandbox against its misuse.

## Rollout and authority

Local package installation does not deploy service/schema changes or activate policy.
Before live cutover, separately review and apply the matching cloud service/schema
upgrade; incompatible deployed schemas must block rather than bypass validation.
Historical Git checkpoints retain their shape/provenance. Recover incompatible pending
publication/setup/removal operations through their original supported implementation
before cutover; never reinterpret old receipts.

The current contract retains schema version **2** and client protocol **4**.
The memory-native foundation is migration `012_agentic_memory.sql`.
Ordinary clients reject schema 1. A separately reviewed upgrade accepts the old schema only for that
upgrade path; SQL migration alone does not deploy the matching service. Reconnect
using actual reviewed schema-2 stack outputs afterward; no installed configuration
is silently rewritten. New checkpoints use `prepared` → `saved` or `aborted`,
with immutable cutoff, exact references and summary; historical optional-Git shapes
remain distinct.

Then separately review proposed instruction bodies, dependencies and exact activation
challenges. Activate through the existing owner-approval process and reload complete
current authority. Packaged templates and catalog changes never auto-update active
cloud instructions. Schema rollout approval is not instruction activation approval.

With affected harness sessions stopped, installer update first replaces local
software/harness files and completes any required reviewed environment migration.
It then applies additive SQL **018 → 019 → 020**, deploys the matching service,
offers exact bundled-authority reviews, and verifies the required store/readiness.
Keep new MCP sessions disabled until the complete update succeeds; file installation
and feature activation are distinct. Start a fresh harness afterward to initialize
against current authority; the installer does not restart existing MCP connections.
Before hosts receive the removed Google runtime, check live instruction dependencies
and separately revoke its cloud skill with owner approval. Existing Google
credentials are not deleted; Magic Workspace remains independent.

Local implementation or simulated-cloud checks are not live AWS/SQL/harness proof.
See [integration capabilities](integrations.md#harness-capability-matrix) and
[workflow](workflow.md) for the user-visible contract. Core recovery remains independent
of MCP/UI/model dependencies; Google is no longer a core runtime.
