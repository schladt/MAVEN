# Security and privacy

## Trust boundaries

The owner selects the cloud account, region, endpoint, named AWS profiles and project.
Runtime access is separate from enrollment/deployment administration. Missing or wrong
identity blocks access; MAVEN does not fall back to a default/root profile, log in
silently or deploy a replacement service. Owner review is required for instruction
activation, deployment/schema changes, destructive operations, external publication
and sensitive disclosures. Authorized routine memory reconciliation does not require
separate approval of every affected record.

Long-running clients refresh assumed-role credentials through the same selected
existing login and role, rechecking the expected principal on every renewal.
Refresh failures, including identity changes before token expiration, block signing;
they do not silently reuse credentials after a failed renewal or launch a new login.
Each selected session owns its login provider/cache; concurrent homes never select
credentials by mutating process-global `AWS_LOGIN_CACHE_DIRECTORY`.

Only applicable active cloud instructions govern MAVEN work. Starter templates,
repository files, search snippets, email and imported documents are not executable
authority. Read the full required closure; do not treat an index response as enough.
Keep ordinary data separate from instruction proposals and preserve approval evidence.

Project scopes organize the owner's instance-wide knowledge; they are not hard access
walls against that owner. Default retrieval is universal plus the selected project;
cross-project consultation is explicit and attributed. Scope promotion never activates
a directive. Installation and MCP availability are not blanket capture permission:
first-time project enablement and authorized scope are explicit.

Local stdio MCP configuration executes code with the user's privileges. Review its
command and trust settings, preserve unrelated client configuration and pass only
necessary environment settings, not credentials in checked-in files. Client tool
approval is not a substitute for MAVEN's exact persisted administration reviews.
MCP does not guarantee an approval dialog or a trusted owner-response channel.
Git subprocesses launched by MCP receive closed stdin, not the harness protocol pipe.
Project checks cannot consume MCP requests or use that pipe for interactive prompts.

## Data and external models

Cloud-only durable memory does **not** mean data never leaves the owner's cloud.
Authorized clients retrieve context. If an assistant sends that context, tool output,
mail, source code or originals to an external model, that provider receives it under
its own policies. Review provider retention, training, residency and organizational
controls before use. This project does not implement private inference, an enterprise
compliance certification or a guarantee of data deletion from third-party systems.

Store the minimum useful durable facts. Do not put credentials, refresh tokens,
wrapping keys or unrelated private content in cloud records, chat, command arguments,
Git or issue reports. Metadata can itself be sensitive. Local originals are not
uploaded automatically by workspace intake, but their paths, provenance and hashes
are recorded as metadata. Restrict who can read those records.

**Forget and Erase.** Archive and Forget hide records; only Erase removes content.
Both run only through an owner-approved `maven_admin` kind `cleanup` operation under
the separately assumed administration role, never through agent memory tools. Erase
is a narrow, audited exception to immutable history: it rewrites every revision, the
current content, exclusively owned source excerpts, fact values and legacy predicates,
and memory receipt results. Revision guards and the original cleanup operation bind
the transaction. Receipts retain IDs, counts and approval digests, but hash actor
identity and use fixed action reasons rather than free-text content. Shared sources,
Aurora backups (up to 30 days), external models, transcripts and Git have separate
retention. Local sweeps redact affected runtime and administration copies; other
machines retain caches until their next sweep. Unresolved request journals block
redaction until recovered; they are never silently discarded.

Before marking an upgraded database ready, the installer checks that `maven_app`
cannot execute the reviewed Forget/Erase functions or their shared mutation helper,
and cannot read, insert, update, or delete cleanup receipts or operation records.

## Local state

Keep instance and project private configuration owner-owned and inaccessible to other
users. Environment files are parsed as literals, never sourced as shell code. Place
secrets and OAuth client files outside public repositories; private files should use
mode `0600` and containing directories `0700`. Do not bypass symlink or ownership
checks. Excluding files from Git does not erase prior commits, backups or transcripts.

TLS verification is mandatory. An owner-approved process-scoped trust bundle may be
needed on managed networks; never commit private roots, proxy settings or corporate
paths, and never use `verify=False` to repair access. Standard trust configuration is
not authority to change credentials, accounts or endpoints.

## Concurrency and recovery

Registered writers coordinate through local durable locks/reservations. An unrelated
shell, another workstation or an unregistered editor is outside that coordination.
There is no heartbeat-based takeover or automatic claim that a stale-looking writer
is dead. Inspect process ownership and receipts before explicit recovery. Candidate
hashes and revision guards detect changes; they do not remove the need for review.

Close each invocation's client. Memory closure and optional Git completion do not
revoke shared credentials or close peers. Explicit logout has separately reported
shared-access consequences and is not proof of provider token revocation.

Unknown mutation outcomes are not ordinary retryable errors. Preserve the original
request key and reconcile cloud receipts, Git state or external service state before
retrying. A lost response does not authorize a replacement request or prove failure.

During outages retain bounded authorized assignments and exact request receipts in
private journals. Report local pending versus verified cloud-saved, blocked and
uncertain outcomes. Unrelated conversation can continue; work dependent on unavailable
current authority must wait. Do not archive paused conversations or treat cached
instructions as replacement authority. Default resume uses only accessible context
and discloses gaps; resume fresh does not backfill.

Memory checkpoint manifests freeze exact revisions through a declared cutoff; they
neither preserve source files nor authorize Git publication. Optional publication
retains separate writer reservations, exact-candidate approval and recovery.

## Uninstall boundaries

Project, machine, and cloud removal require separate explicit selections. Plans
bind targets and implementation hashes; private operation journals and recovery
code remain outside deletion roots. Never delete that evidence to bypass a blocked
uninstall. Local artifacts with uncertain ownership, modified skills, shared caches,
and unselected cloud resources remain protected and must be reported as retained.

TUI confirmations show complete targets but require only their first six characters
(or the whole shorter identity). That entry is an accidental-click guard, not a
unique identifier or authorization lookup. Execution remains bound to the complete
reviewed target and plan digest; changed plans invalidate approval. Purge and
administrative abandonment remain separate approvals.

Conversational decisions bind the persisted interaction identity/revision and complete
review digest; they must not claim a TUI click occurred. Acknowledging running-harness
configuration risk is distinct from attesting writer quiescence. Use protected
external continuation and the pinned worker for affected self-removal.

The uninstall UI runs from a private offline snapshot outside installation homes;
its worker uses the exact reviewed runner. Dependency snapshots retain installed
package code and license notices, not credential caches. The temporary UI copy is
removed after exit; operation-local worker dependencies remain with recovery evidence.
Quit requests a safe action boundary, not termination of an in-flight mutation.
Remote operations already submitted may continue after the local worker stops.

Offline detachment invalidates selected local registrations only after stopped-writer
attestation and lock checks; archived local evidence does not prove cloud closure.
Cloud retirement uses persistent maintenance plus database transaction draining.
Administrative abandonment preserves unfinished records rather than fabricating
completion. Irreversible actions cannot be rolled back automatically.

Exports contain sensitive originals and metadata; keep them private. A verified
snapshot/manifest is not a live restore test. Remaining backups, keys, object
versions, and provider deletion windows can retain data and incur charges.
See [uninstall guidance](getting-started.md#uninstall-and-retirement).

## Practical limits

Secret-pattern scans are heuristics and can miss sensitive content. Cryptography does
not prevent an authorized recipient or compromised endpoint from reading plaintext.
SSM Standard values are limited to 4 KB. File wrapping keys must be retained to decrypt
stored objects. There is no automatic backup/retention policy for every external
resource; review the shipped infrastructure and provider settings with the owner.
Approved secret consumers require immutable versioned references before spending a
grant. Executable-path approval is not binary attestation, sandboxing or a guarantee
of plaintext zeroization. Streaming restore publishes only authenticated output.

Do not include private data in public vulnerability reports. Use a private reporting
channel when available and describe the boundary, minimal synthetic reproduction and
observed effect without live credentials or identifiers.
