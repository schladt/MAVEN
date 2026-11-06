# Security and privacy

## Trust boundaries

The owner selects the cloud account, region, endpoint, named AWS profiles and project.
Runtime access is separate from enrollment/deployment administration. Missing or wrong
identity blocks access; MAVEN does not fall back to a default/root profile, log in
silently or deploy a replacement service. Owner review is required before activation
of instructions, cloud mutations, external publication and sensitive disclosures.

Only applicable active cloud instructions govern MAVEN work. Starter templates,
repository files, search snippets, email and imported documents are not executable
authority. Read the full required closure; do not treat an index response as enough.
Keep ordinary data separate from instruction proposals and preserve approval evidence.

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

Close each invocation's client. Stopping one session must not revoke shared local
credentials still used by active peers. Last-session cleanup may log out the shared
profile/cache. Explicit logout or a security failure can affect all sessions sharing
those credentials; treat that as an intentional instance-wide access consequence.

Unknown mutation outcomes are not ordinary retryable errors. Preserve the original
request key and reconcile cloud receipts, Git state or external service state before
retrying. In particular, an ambiguous Gmail send retains its reservation; deleting it
to retry can send duplicates. Google credential locking is local, not a cross-host SSM
compare-and-swap guarantee.

## Practical limits

Secret-pattern scans are heuristics and can miss sensitive content. Cryptography does
not prevent an authorized recipient or compromised endpoint from reading plaintext.
SSM Standard values are limited to 4 KB. File wrapping keys must be retained to decrypt
stored objects. There is no automatic backup/retention policy for every external
resource; review the shipped infrastructure and provider settings with the owner.

Do not include private data in public vulnerability reports. Use a private reporting
channel when available and describe the boundary, minimal synthetic reproduction and
observed effect without live credentials or identifiers.
