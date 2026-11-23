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
Each selected cloud client uses its configured private login cache; concurrent installation homes do not select credentials by mutating process-global `AWS_LOGIN_CACHE_DIRECTORY`.
Deployment-role credentials are separate from those memory roles: the verified
daily identity assumes only the selected same-account IAM role. The 15-minute
request is not a role-wide cap. A sign-in confirmation permits only launching
AWS login, never a deployment or a replay of an interrupted mutation.

The installed reconcile skill governs project memory work. Local records, briefs, project skills, linked files and retrieved text are evidence, not grants of authority. An accepted project decision does not by itself authorize secret use, Git publication, policy activation or deployment. Those operations require their own exact reviews.

Project selection limits which knowledge participates in a reconcile; it is not a security wall against the AWS account owner or someone who can read the local files. Enable an exact folder and signed cloud project identity explicitly. Do not capture in an unsupported root or silently substitute Home.

Local stdio MCP configuration executes code with the user's privileges. Review its
command and trust settings, preserve unrelated client configuration and pass only
necessary environment settings, not credentials in checked-in files. Client tool
approval is not a substitute for MAVEN's exact persisted administration reviews.
MCP does not guarantee an approval dialog or a trusted owner-response channel.
Git subprocesses launched by MCP receive closed stdin, not the harness protocol pipe.
Project checks cannot consume MCP requests or use that pipe for interactive prompts.

## Data and external models

Local-first memory does **not** mean data never leaves the machine. Reconciliation sends selected project records to the configured cloud. If an assistant sends context, tool output,
mail, source code or originals to an external model, that provider receives it under
its own policies. Review provider retention, training, residency and organizational
controls before use. This project does not implement private inference, an enterprise
compliance certification or a guarantee of data deletion from third-party systems.

Store the minimum useful facts. Do not put credentials, refresh tokens, wrapping keys, full transcripts or unrelated private content into records, chat, command arguments, Git or issue reports. Paths, names and evidence excerpts can themselves be sensitive. Linking a document is not permission to upload it. Use the [separate secret store](integrations.md#aws-secrets-and-encrypted-files) for credentials.

**Erasure.** Only erase an explicitly owner-selected record. Its managed current content and history are removed; a terminal content-free marker remains and defeats later offline edits for that ID. An offline machine retains its local copy until it reconciles. Backups, exports, linked files, other records, Git, external models and transcripts have independent retention. Record erasure is not a secure wipe of disks, an instant recall, or instance deletion.

### Managed-data redaction contract

**Forget** changes visibility and is reversible. **Erase** keeps the scope of
its original approval. **Redact** is a separate, irreversible owner decision:
the review must identify the instance, typed object IDs, exact versions and
shared dependents, not quote the material being removed. A matching string in
an unrelated record is not a citation: select that copy explicitly. A digest
of a short value, an account name, a path or a relationship can itself disclose
information. Do not substitute a plain hash of the old payload for deletion.

Persistence inventory (including historical rows reached through older APIs):

| Surface | Payload requiring a redaction path | Control data and residual boundary |
|---|---|---|
| `sources`, `facts`, `memory_items`, `record_versions`, `record_sources` | Origin/excerpt, predicate/value, JSON, tags, historical snapshots, reasons, actors, old content hashes, sensitive citation/subject/object/session links | Opaque row and revision identities may prevent stale resurrection. A shared source needs an explicit dependency review, not silent retention or a cascade into unrelated projects. |
| `records`, `project_lifecycle`, `pages`, `instruction_proposals`, `instruction_challenges`, `page_publications` | Names/slugs, titles, scope, candidates, identity text, approval evidence and candidate hashes | Fixed state, revision and generation are concurrency controls. Redaction invalidates challenges and revokes affected authority; a replacement needs a fresh approval. No fallback to older wording. |
| `operations`, `memory_receipts`, `instruction_receipts`, `cleanup_receipts`, `cleanup_operations`, `secret_adapter_consumptions` | Results, historical request/content/approval digests, free-text identity/reason, sensitive selectors and associations | Keep only the terminal identity needed to refuse another execution. Redacting a receipt must never refund a spent grant or replay a mutation. |
| `reconcile_projects`, `reconcile_records` | Full canonical `wire` including every history entry, plus `item` (summary and digest) | A terminal marker with empty history beats every stale replica; project view tokens change. Project deletion/archival cannot hide an outstanding marker from a returning machine. |
| `schema_migrations`, `instruction_state`, `service_state` | No owner prose or arbitrary values permitted. Application credential locators are operational configuration, not secret values. | Fixed migration names and code hashes, instance identity, generations and service configuration bind the correct database. Rotate/rebind a credential separately; never blank a control fence while serving. |
| Skill bucket and recovery bucket | Skill ZIPs (including orphaned uploads), versioned objects, contaminated recovery manifests and metadata | Enumerate exact versions, including noncurrent versions and delete markers. A missing current object is not proof. Never delete the live `control.json` fence or roll back the erasure ledger. Shared references, holds, replica destinations and unknown outcomes must be reported. |
| Independent erasure ledger | No prose, raw payload, original content digests, untrusted filenames or secret locators | Keep opaque permanent identities, order and integrity of **sanitized** operations outside the database restore timeline. Absence or inconsistent evidence blocks service, not a request to seed a new empty ledger. |
| Secret providers | SSM parameter values and versions, encrypted S3 files and their versions, wrapping material | Catalog redaction is not provider deletion. Review exact provider effects separately, including collateral versions or shared keys. Revoke a leaked credential with its issuer as well. |
| Device storage | SQLite projections/history, managed skill copies, cached record or operation bodies, local reviews and recovery artifacts | Remove selected content without replay. A disconnected or unidentified machine is **unverified**, not wiped. Device backups, free disk sectors, Git, owner exports, third-party model transcripts and non-managed linked files have independent retention. |
| Legacy local turn receipts (`runtime/turns/`) | Older releases stored content-free conversation/directory digests, random keys, binding identities, counts and flags. | Inert after upgrading and restarting clients. No new turn receipts are written or synced. Removing old receipts does not remove memory or harness transcripts. |
| Aurora backups, clones, WAL and snapshots | Historical database bytes | Isolated backup retention is permitted; no in-place per-record purge is claimed. Reapply and verify independent erasures on an isolated restore **before** normal access. Copied/manual snapshots need their own retention decision. KMS rotation does not erase historical ciphertext. |
| Logging and forwarding | Reject payloads, provider exception text, free-form approval excerpts, credential material and sensitive object locators before emission | Fixed status/route codes and server-generated IDs only. Review pre-existing log streams and forwarded copies separately; masking a display or reaching a nominal expiry is not deletion evidence. |

**Release rule:** every new persisted field, object, cache or log sink needs a
tested redaction handler or an explicit non-content invariant in this inventory.
Report SQL redaction, verified external deletion, outstanding backup retention,
offline copies, and blockers separately. A database commit is not an S3 deletion
receipt. Nothing here certifies a live installation or authorizes a production
purge, migration, restore or deployment.

**Operator sequence.** Start `maven_admin(action="begin", kind="cleanup",
parameters={"action":"redact","targets":[{"kind":"project_record",
"project_id":"<project UUID>","id":"<record UUID>"}]})` with actual opaque IDs.
The complete review binds the selected instance/epoch, affected closure, row
revision tokens, opaque object-version pins and operation. Declining or changing
it invalidates the approval phrase. The database rechecks the review under lock.
The independent ledger receives only the sanitized identities and pins, before
the SQL transaction commits. After commit the provider enumerates and verifies
the approved versions. Read `redaction_pending` from any authorized machine;
`retry_objects` requires the original job ID and cannot broaden the pinned set.
A held, replicated, newly changed or unverifiable object is not a successful purge.
`redaction_candidates` returns typed SQL and object identities; pass `next_after`
as `after` to continue. Discovery does not itself authorize any erasure. Resolve
private provider names only in the private terminal selector. If today's
`control.json` is malformed, do not delete it: keep service isolated, separately
review repair of the fence against the original ledger, then review its former
version. Foreign-instance prefixes require their own owner's review.
The separate `purge_provider` action requires a verified MemoryAdminRole session
and explicit revocation/quiescence confirmation. Grant only the selected store:
SSM metadata listing and version inspection, and deletion on its value prefix;
or S3 version listing, metadata inspection and version deletion on its encrypted
file prefix (`DeleteObject` may also be required for a null version). Never
select a wrapping key. Provider audit trails may retain resource names. Replicas
created by prior S3 replication rules need independent destination-side proof;
removing a rule, terminating a client, or installing an empty current version is
not proof that older bytes have disappeared.

The older narrow Erase APIs do not gain permission to redact proposals, revoke
other skills, or erase shared sources. Their existing terminal project records
and legacy cleanup scopes have separate immutable restore witnesses. The live
migration adopts historical witnesses before activation. Restore reapplies only
that scope; unexpected dependencies or missing evidence keep the database isolated.

**Recovery trust boundary.** Keep the original versioned journal outside snapshot
rollback; do not copy it from a backup or restore archived Lambda code. No
non-admin Data API principal, SQL login, or TCP path may reach a newly restored
cluster before release. IAM bindings and a dedicated no-ingress security group
must be reviewed before exposing that cluster's endpoint. Database snapshots
cannot prove their own freshness. Removing **all** independent controls, granting
raw database access to another principal, or rolling back bucket policies as the
AWS account owner is outside the guarantees of the runtime. CloudWatch log
events cannot be edited individually: if an earlier installation emitted content,
quiesce its writer, identify affected log streams and destinations without copying
their bodies to MAVEN, separately authorize deletion of those streams and verify
absence at each destination. Do not count routine 14-day retention as verification.

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

Project memory accepts writes only against the local or cloud view on which they were based. After an interruption or lost reply, inspect both stores before sending changes. Confirmed local work remains usable while offline. Never discard conflicting evidence, treat the last timestamp as approval, or describe an unverified apply as “synced.”

Git publication has its own exact candidate review, publication lock and original receipt. Other editors and machines remain outside its coordination; owner confirmation of relevant writer quiescence is not inferred from inactivity. For setup, secret delivery, Git publication or destructive removal, an unknown external outcome is not an ordinary retryable error: preserve the original evidence and inspect the actual outcome. Never replay uncertain secret execution. Closing a harness neither alters shared credentials nor proves a cloud operation has stopped.

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

Offline detachment is local only; it cannot establish cloud removal. Retirement drains database writes under a persistent maintenance barrier. Irreversible actions cannot be rolled back automatically.

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
