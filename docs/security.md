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
