# Harness and optional service integrations

## Harness capability matrix

The baseline is a client-launched local **stdio** MCP process, using the installed
`maven-mcp` executable and explicit `MAVEN_HOME`. Prefer an absolute executable path
so client PATH differences cannot select another installation. Do not put credentials
in MCP configuration. Preserve unrelated entries and use the selected profile/custom
destination. No public endpoint, listening port or mandatory OMP plugin is needed.

The matrix records **documented support**. Runtime verification is limited to the
specific local proof below; it is not a claim of interoperability with every client,
version, enterprise policy, or optional feature.

| Client | Documented stdio configuration and environment | Startup, decisions and trust | Semantic maintenance baseline |
| --- | --- | --- | --- |
| [OMP](https://omp.sh/docs/mcp) | `.omp/mcp.json`, `~/.omp/agent/mcp.json`, or named-profile MCP config; `command`, `args`, `env`, optional `cwd`; review precedence | `AGENTS.md` fallback; optional native extension supplies exact session identity and startup context. OMP 18.20.0+ supports the cancellable ordinary-exit prompt described below | Use available native background tasks only when exposed and responsive; otherwise foreground |
| [Claude Code](https://code.claude.com/docs/en/mcp) | `mcpServers` in selected local/user configuration or project `.mcp.json`; stdio command/args/env | `CLAUDE.md` fallback; optional [SessionStart/SessionEnd hooks](https://code.claude.com/docs/en/hooks). End is advisory, not an exit veto. Workspace trust can block loading | [Native subagents](https://code.claude.com/docs/en/sub-agents) documented, including background configuration; confirm worker tool access/permissions, else foreground |
| [Codex](https://developers.openai.com/codex/mcp) | `[mcp_servers.<name>]` in selected `config.toml`; command/args/env and supported environment controls | `AGENTS.md` fallback; optional [SessionStart/SessionEnd hooks](https://developers.openai.com/codex/hooks) in supported versions. End is advisory; trust/tool approval remains separate from MAVEN exact reviews | Foreground is portable baseline; use native agents only if the installed version exposes verified background execution |
| [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) | `~/.copilot/mcp-config.json`, `mcpServers`, local/stdio command/args/env; PATH inherited, other needed variables explicitly configured | `copilot-instructions.md` fallback; optional [sessionStart/sessionEnd hooks](https://docs.github.com/en/copilot/reference/hooks-reference). End output cannot veto exit. Use user configuration for non-Git projects | Foreground fallback; do not infer nonblocking background execution from tool discovery or custom-agent support |
| [Gemini CLI](https://geminicli.com/docs/tools/mcp-server/) | `settings.json` `mcpServers`: command/args/env/cwd; sensitive inherited variables may be filtered | `GEMINI.md` fallback; optional [SessionStart/SessionEnd hooks](https://geminicli.com/docs/hooks/reference/). End is best-effort. Keep confirmations enabled; `trust: true` bypasses them | Foreground fallback unless installed native background capabilities are actually available and verified |

Gemini's official documentation currently notes its transition to Antigravity CLI
for some account tiers. A legacy skill destination is not proof of Antigravity MCP
compatibility; confirm the actual product/version before choosing an integration.

### Recorded local verification

Recorded runtime exercises are limited to OMP 18.18.2 and 18.20.0 with disposable
profiles and simulated storage. They covered stdio startup, native background-worker
delivery, and exit-prompt behavior—not live AWS or end-to-end model-driven cloud wrap-up.

Claude Code, Codex, Copilot CLI and Gemini CLI have documented configuration paths
and offline adapter checks, not verified live-runtime interoperability. Offline
fixtures do not establish deployed SQL behavior, live IAM permissions, cloud rollout,
or active-instruction correctness.

### Optional capabilities and honest fallbacks

| Capability | Required portable behavior | Optional enhancement / limitation |
| --- | --- | --- |
| Startup | Native guidance invokes the shared inspection/initialization workflow; print checking, then initializing and ready/blocked based on actual results | Optional hooks inject exact-conversation startup context; they do not load cloud authority or create a session themselves. Missing/disabled hooks retain first-turn fallback |
| Session identity | Preserve returned MAVEN binding in accessible session context/private evidence | Associate a native harness ID only when reliably exposed; never infer from PID, cwd or newest receipt |
| Background reasoning | Foreground agent reads, reasons and reconciles bounded assignments | Native subagent may work while another user turn proceeds; must receive scoped context and access to MAVEN tools. MCP cannot launch it |
| Catch-up | Resume from accessible paused discussion and disclose missing portions | Harness transcript APIs are optional, not a standard MCP facility; resume fresh avoids backfill |
| Progress / notifications | Visible chat for initialization, explicit saves/checkpoints, mode changes and meaningful blockers | Native progress/status delivery where supported; routine successful updates stay quiet |
| Owner decisions | Present full persisted review and bind the actual response to its identity/revision | Native dialogs/elicitation are optional; never forge consent or treat tool permission as exact target approval |
| Shutdown | Journal effects before mutation; preserve uncertain outcomes and finish explicitly | OMP's optional ordinary-exit guard can cancel before wrap-up. Other clients' end hooks are advisory. Forced exit/EOF/signals guarantee neither grace nor background completion |

The [MCP transport specification](https://modelcontextprotocol.io/specification/latest/basic/transports)
defines client-launched stdio and transport-independent protocol semantics, not
subagents, startup policy, a permanent model runtime or uniform approval APIs.
Instructions and current context still come from the selected AWS instance.

Native standing-context references:
[OMP context files](https://omp.sh/docs/context-files),
[Claude memory files](https://code.claude.com/docs/en/memory),
[Codex AGENTS.md](https://developers.openai.com/codex/guides/agents-md),
[Copilot custom instructions](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions),
and [Gemini GEMINI.md](https://geminicli.com/docs/cli/gemini-md/).
MAVEN always installs first-eligible-turn guidance; optional lifecycle adapters add
native events. Neither guarantees that authority is initialized before the first
user input; readiness still requires the real MCP result and complete context.

Status commands pin the selected `MAVEN_HOME`, including paths with spaces. Active
status keys include both harness and native conversation ID; a folder fallback is
explicitly a **last folder hint (not started here)**, not proof of this chat's binding.
The optional terminal UI supports Textual `>=2.1,<3`.

### Native lifecycle adapters

The shared local helper stores only exact native-conversation bindings and observed
end-event hints. It does not log in, contact AWS, copy transcripts or spawn agents.
The main assistant uses `maven_startup` for cloud peer inspection and authorized
initialization, or `maven_diagnose` for repair choices. An end event does not close a
cloud session, prove writer death or mark unsaved work saved.

OMP uses `session_start`/agent context and `session_before_shutdown` in a native
extension. Startup delivery is deferred through a managed callback, so the startup
event never waits for a model turn to finish within its 30-second handler budget.
Callbacks recheck the live session-manager cwd and conversation identity after awaits,
and fence queued work after workspace changes or shutdown. Delivery failures remain
visible. For an open exact MAVEN binding, the prompt offers wrap up and exit,
exit without wrapping up, or stay. Wrap-up cancels immediate shutdown, lets the
agent reconcile accepted work, then checks the exact finish receipt before exiting.
Failure leaves the harness open. OMP 18.20.0+ documents this cancellable event; older
versions retain first-turn guidance and explicit wrap-up.
Forced exits, signals, restart and programmatic `ctx.shutdown()` bypass the guard;
a second quit can bypass a pending prompt. It is a convenience, not crash recovery.

Claude Code and Codex use `SessionStart` additional context with genuine `session_id`;
Copilot uses `sessionStart` and `sessionId`; Gemini uses its own `SessionStart` output
shape. Their end events are advisory/best-effort, never modeled as an OMP-equivalent
veto. Hook timeout, policy or unsupported versions can suppress delivery. Startup
still checks retained evidence and offers recovery when an end event was missed.
Resume only the exact recorded conversation; stale peers are not interchangeable.

An MCP process that never successfully initialized may recover through an explicit
`maven_startup(action="new")` in the same enabled root, project, instance and harness.
This also permits attaching the real native conversation ID after an earlier attempt
had none. `inspect` can diagnose that failed process binding under the supplied native
identity without adopting it; `continue` still requires the original identity.
Replacement requires a verified existing cloud project and an exact existence check
including archived state proving the old session unavailable—not absence from a
bounded search page. Authentication,
transport and uncertain read failures remain blockers. An unavailable project is
reported as a local folder association with `project_check.cloud_verified=false`;
this does not establish deletion or archival and never creates or rebinds a project.
Verified cloud project names, including renames, label startup's active project
without renaming its folder; an unverified folder-name fallback is explicitly marked.

Previously initialized/resumed sessions, cloud sessions, pending requests, receipts,
checkpoints, closure, assignments, writers or uncertain registrations prevent this
replacement. Peers are never closed, adopted or marked stopped. The failed binding
is fenced with `superseded_by`; `failed-start-recovery.json` preserves its original
binding, registration and native-conversation reservation. Only its exact successor
may replace that reservation. Existing runtime evidence and definitive rejected
initial-write request identities remain intact; no old request is replayed.
After a process restart, select the exact failed attempt with
`maven_startup(action="new", failed_start_session_id=…)`. Its durable journal resumes
interrupted cutover at the original boundary; it does not adopt a discovered peer.
Folder switching recognizes that exact fenced failed-start receipt as a terminal
local attempt, not a closed cloud session. Its replacement must either have verified
closure or independently satisfy the reviewed never-initialized proof below.
Pending work or incomplete retirement still prevents switching.

The official reviewed folder switch can also preserve a never-initialized attempt
when its old cloud project is unavailable. It requires the exact original MCP
binding with `initialized:false` and `resume_required:false`, no retained work or
uncertain effects, and either no registration yet or only its exact memory-only
`starting` registration without writers or reservations. An exact session read
with `include_archived:true` must return `record_unavailable`; search absence,
timeouts, authentication failures and other outages never establish this proof.
Initialized, abandoned, unknown and session-receipt-only runtimes cannot use this
never-initialized exception.

The owner's review pins each original binding, registry and native-conversation
record, retained-file fingerprints and cloud absence proof. Applying that review
revalidates the originals under the exclusive project gate and native-pointer locks.
The durable switch journal and sibling preservation archive retain the original
IDs, request evidence and owner source across interruption. Only after successful
runtime cutover are the reviewed starting registrations and their exact native
conversation pointers removed; unrelated registrations and conversations are never
cleared. The switch barrier remains until cleanup is durable. Resume uses the same
journal, not a new operation or inferred closure. Fresh native startup then selects
the new project; an old direct session service remains fenced by its original
root/project binding. No cloud session is closed or peer automatically declared stopped.
Before preserving private evidence beside a root, every enclosing Git worktree must
already exclude the destination. Otherwise the switch refuses before moving data;
it does not edit parent repositories' ignore rules.

A separate, explicit owner-stopped historical path is available on project/Home
switch administration operations through `parameters.stopped_session_ids`: an exact,
duplicate-free list of canonical session UUIDs, not a root-wide bypass. It applies
only to unregistered historical runtimes with matching `session.json` pointers,
no pending requests, no unfinished maintenance/checkpoints or closure, no active
publication, and only definitive rejected requests. Rejection receipts must verify
either the current request-internal digest or the legacy outer `pending_digest`
against the exact canonical request; missing, changed or mixed hashes are refused.
Unknown evidence and MCP-bound runtimes cannot use this path. The full review pins every retained file fingerprint,
pointer and original registration absence. The owner must separately confirm the
exact IDs, that those sessions **and their workers** have stopped, and preservation
without cloud closure, with a truthful source. Generic approval alone is insufficient.
Apply rechecks the same guards and fingerprints, including after interruption.
The archive and journal retain originals unchanged and label them `stopped-by-owner`,
not verified closed or cloud-absent. No cloud request is replayed and no registered
session or unrelated native pointer is removed by this historical path.


### Installation and update boundaries

The normal local installation includes the official Python SDK (`mcp>=2.3,<3`) on
Python 3.11+; CLI/TUI recovery remains independent. Installer-owned MCP entries,
the `maven` and `maven-maintenance` skills,
and delimited native guidance are tracked for safe removal. Stage/review conflicts
instead of replacing unrelated configuration. Approve exact writes and acknowledge
running-harness configuration risk separately from any required writer quiescence.

Use `--stage-harnesses PATH` to inspect a staged home layout without changing live
configuration. Custom profiles use paired `--harness-config NAME=ABSOLUTE_FILE` and
`--harness-context NAME=ABSOLUTE_FILE`, plus `--skill-dir PATH`. Actual writes require
the exact local-change review. Supported native targets are OMP, Claude Code,
Codex, Copilot CLI and Gemini CLI, not every client accepting a similar skill format.

Native adapters are enabled by default with `--lifecycle-hooks`; disable them with
`--no-lifecycle-hooks` or the TUI's **Install native MAVEN startup and shutdown
integration** checkbox. They require the installed `maven-session-hook` executable.
Review includes the extension/hook configuration and ownership receipts. Hooks,
status-line and MCP entries sharing one configuration file are removed together
under a single pinned-file review, preserving unrelated keys and refusing owner edits.
Updates reuse exact recorded custom hook/configuration destinations. Ambiguous legacy
targets are refused rather than guessed or replaced with default-profile paths.
Conflicts preserve the affected target without blocking independent safe writes.

Codex integration uses an existing nonempty `AGENTS.override.md` when present,
otherwise `AGENTS.md`. If Gemini's custom `context.fileName` excludes `GEMINI.md`,
select the context path explicitly. Identical preexisting integration entries are
referenced, not adopted as installer-owned. Selective removal preserves unrelated
content. Legacy CLI skills are retired only when their bytes match the prior
installer inventory and ownership is unshared; modified, unowned or shared copies
block cutover and require separate review rather than deletion.

First-time project enablement is explicit. Startup guidance does not authorize
unrelated capture and is not active cloud authority. Follow [getting started](getting-started.md)
for existing-instance schema rollout and separately approved policy activation.

## Configured AWS and workspace services

These integrations use the selected instance's private `.env`, not the installed
source directory. Configure `MAVEN_AWS_PROFILE`, its expected AWS identity and
`MAVEN_AWS_REGION` through the shared environment contract. The profile must already
be explicitly authenticated. No helper discovers another account or logs in for you.

## AWS secrets and encrypted files

Secrets setup is optional and separately reviewed: import existing settings, enter
selected locators, or approve the exact private bucket and generated wrapping key.
Repair/update preserve selected keys; read-only verification does not prove write
permissions. Core memory does not depend on this store. Catalog operations require Home.

Small values use `MAVEN_SECRETS_PARAMETER_PREFIX`: SSM Standard `SecureString`
parameters encrypted with `alias/aws/ssm`, limited to 4 KB of UTF-8. Grant only the
selected namespace and necessary KMS permissions. Names are nonsecret lowercase
segments; dotted domains organize metadata, not access control.

```sh
maven-secrets put service-token             # hidden terminal prompt
maven-secrets list --domain example.service --limit 20  # metadata only
maven-secrets check service-token           # hidden comparison prompt
maven-secrets replace service-token         # intentional existing-value update
maven-secrets reveal service-token --tty    # explicit terminal-only disclosure
maven-secrets use-stdin service-token -- trusted-program
```

`put`, `replace` and `check` also accept `--stdin` from a trusted pipe. Avoid shell
history, tracing and programs that log stdin. `use-stdin` passes plaintext to the
chosen program: the program is part of the trust boundary.

Encrypted-file operations additionally require:

- `MAVEN_SECRETS_BUCKET`: an owner-provisioned private S3 bucket.
- `MAVEN_SECRETS_OBJECT_PREFIX`: a nonempty relative object prefix ending in `/`.
- `MAVEN_SECRETS_KEY_ID`: the selected 16-character lowercase hexadecimal identifier.
- `MAVEN_SECRETS_FILE_KEY_PREFIX`: an absolute SSM prefix ending in `/`; the selected
  key parameter stores the base64 representation of a securely generated 32-byte
  wrapping key as a `SecureString`.

The setup controller owns reviewed provisioning; the low-level helper does not
create buckets or keys. Protect and back up keys, review public-access blocking,
retention and costs, and grant only necessary object/multipart operations and SSM
key reads. Never broaden account access to work around a denial.

```sh
maven-secrets file put "$PRIVATE_SOURCE" archive-object
maven-secrets file put archive-stream --stdin --max-bytes 1073741824 --json < "$PRIVATE_SOURCE"
maven-secrets file get archive-object "$NEW_PRIVATE_DESTINATION"
```

Files use authenticated client-side AWS Encryption SDK encryption and inherit the
bucket's reviewed server-side encryption policy, including KMS defaults. Upload
refuses replacement. Restore streams into a private temporary file and publishes
only after authentication, without overwriting an existing destination. Keep the
parent private and retain historical wrapping keys. Failed cleanup is not success.

Pipe input is bounded (default 64 GiB), encrypted as a stream without plaintext
spooling. Receipts contain metadata, not values. `list` pages both providers using
`--cursor`; `--domain` matches a dotted namespace, not a substring. These low-level
commands never update Home.

### Approved skills and the Home secret catalog

In configured **Read Only**, **Home**, or **project** mode, `maven_instructions`
discovers approved skills; `maven_context(skill_slug="maven-secrets")` loads the full
approved skill and governing dependencies. Read Only uses universal authority;
bound Home/project loads use their actual scope. Discovery does not load authority.
Off and unconfigured modes provide no skill access. Named loading creates no session
and captures no discussion.

`maven_secrets` provides `list`, `lookup`, `store_and_catalog`, `retry_catalog` and `resolve_catalog`.
`list` returns paged names/metadata only. `lookup(name=...)` uses the narrow exact
Home secret-setting route, not general memory search. Explicit `store_and_catalog`
reads an owner-private `source_path` and genuine request `source`; never supply a
plaintext value in tool arguments. Choose `provider="ssm"` or `"encrypted-file"`.
It may write exactly that one Home catalog setting from Read Only or another
project, without rebinding, opening a Home session, general search or capture.

The standalone equivalent needs no memory session:

```sh
maven-secret-catalog store-and-catalog example.service.token --input "$PRIVATE_SOURCE" --domain example.service
maven-secret-catalog lookup example.service.token
maven-secret-catalog retry-catalog "$OPERATION_ID"
```

The CLI also accepts `--stdin`, `--provider encrypted-file`, `--max-bytes`, and
intentional `--replace`. Catalog references pin SSM versions and immutable encrypted
objects. File rotation creates a new physical object and compare-and-swaps the
logical catalog pointer against its prior revision; it never overwrites ciphertext.
SSM storage and catalog compare-and-swap are separate effects, not an atomic provider
credential rotation. Storage success with `catalog_pending` is not complete catalog
success: retain the operation ID and retry **only the catalog**. Never upload again
to repair that outcome, discard uncertain receipts, or silently choose another store.
`resolve_catalog` accepts the original operation ID, current expected catalog revision
and real owner source after a conflict; it performs only a new catalog CAS, not another
upload. Confirmed storage failure, uncertain storage and catalog-pending are distinct.
Approved consumers preflight supported immutable SSM versions/encrypted-file references
before spending a grant. Legacy references require explicit replacement and a new grant.

### Owner-defined secret adapters

`maven_secret_adapter` runs declarative owner-defined consumers without adding
MAVEN code per adapter. Definitions specify the executable, argument templates,
stdin/private-FD secret delivery and explicitly nonsecret results. Never place
secrets in argv or environment. Definitions and grants are cloud-approved authority,
not ordinary editable settings.

Use `policy` to read, `draft`/`review`/`approve` for exact proposal challenges,
`prepare` for the exact invocation, and `grant_document` for a standing or single-use
grant candidate. A draft or grant document is not approval: present the complete
review and record the real owner's response before `execute`. Standing grants stay
within their reviewed constraints; single-use grants pin invocation/digest and expiry
and are consumed atomically across hosts. Missing/stale policy fails closed.
Never rerun after uncertain consumption or execution.

Reusing consumed authorization returns `invocation_consumed`, with guidance to
prepare a new invocation and obtain any required fresh approval—not inspect session
status or replay the old attempt. Other explicit authorization denials return
`adapter_not_authorized`. The service does not distinguish grant expiry from binding
mismatches in that response, so the client does not invent a `grant_expired` diagnosis.
Both refusals stop before secret retrieval or program execution.

Publication results distinguish `draft` (verified proposal receipt), `failed`
(definitive rejection with a bounded error code), and `outcome_unknown` (no verified
receipt). An unknown outcome is not a queued or existing draft: do not proceed to
review/approval. Preserve the returned request and IDs for reconciliation or an
identical retry after the cause is corrected. A `database_statement_rejected` error
requires correcting the database/service implementation or its access rules; it is
not automatically retried as a transient outage. A failed lookup after a lost write
response still leaves the original write outcome unknown.

Migration `021_secret_adapter_scope_guard.sql` repairs the adapter/grant proposal
scope guard without rewriting deployed migration 020. Roll it out through installer
**Update MAVEN**, including the matching service/client changes and a fresh MCP
session; then retry the original adapter request unchanged.

Raw stdout/stderr is withheld from the agent and transcript; only defined nonsecret
results are returned. This is not a sandbox against misuse by a trusted executable.
Approve the actual executable, inputs, destination and effects, not merely its name.
Executable-path approval is not binary attestation or a guarantee of plaintext
zeroization. The bounded consumer input limit is separate from streaming file restore.


## Workspace originals and metadata

The selected project needs an active MAVEN session. All output is JSON:

```sh
maven-workspace --root "$PROJECT_ROOT" --session "$SESSION_ID" intake-pending
maven-workspace --root "$PROJECT_ROOT" --session "$SESSION_ID" intake --request "$PRIVATE_REQUEST"
```

`--env PATH` explicitly selects a different private environment. A mutation request
must be an owner-owned private regular JSON file. For intake, use fields such as:

```json
{
  "path": ".maven/incoming/source.txt",
  "kind": "reference",
  "origin": "Owner-selected source",
  "sensitivity": "Sensitive",
  "authorization": "Owner approved this classification and local preservation",
  "disposition": "retain"
}
```

Replace the provenance and authorization with actual owner decisions. Supported kinds
are `reference`, `operational` and `artifact`. `archive` or `delete` needs `confirm:
true`; `delete` also needs `reviewed: true`. Both remove the incoming source **after
preserving a private verified original**; neither is secure erasure. Raw content is
not uploaded by intake. The cloud receives metadata and operation receipts.

Other actions accept these request fields:

- `archive-plan`: `record_id`, `revision`, `confirm`, `completed`; optional
  `source_path` and `source_disposition` (`retain` or `archive`). The exact current
  cloud note must be a completed plan. Its stable ID, category and prior revisions remain; `archived` is the standard tag.
- `override`: `identifier`, `slug`, `revision`, `content_hash`, `operation` (`record`
  or `revoke`) and `confirm`. This records metadata about an already-approved
  directive; it cannot grant authority.
- `status`: `record_id`, `revision`, optional `output: "team-status.md"`, optional
  `check: true`. The cloud `project-status` setting must explicitly enable external
  status and provide all publishable sections: Progress, Milestones, Blockers, Next
  Steps and Client Actions. Internal fields never substitute for publishable data.

Review status readership manually; identifier warnings and secret-pattern scans are
not proof that content is safe to publish. Mutations serialize the selected session's
pending cloud journal and retain registered-writer safeguards where applicable.
Optional Git publication reservations must not be confused with memory-checkpoint cutoffs.
Intake, archive and status publication journal whole operations. Retry with the same
`operation_id`; the local `receipt` command inspects evidence without a cloud client.
The kernel-held `cloud-workspace-v2.lock` is never unlinked to take over. Legacy lock
recovery requires `recover-lock --writers-quiescent` and preserves original evidence.
First output publication refuses overwrite; replacement of an existing reviewed
output and source removal rely on cooperating writers, not a filesystem sandbox.

## Google removal and independent integrations

Google OAuth, Gmail/Workspace clients, CLI and installation extra are no longer
part of MAVEN core. Removing this runtime does not delete stored Google credentials:
they remain ordinary, untouched SSM values. It neither revokes Google grants nor
changes the independent Magic Workspace skill.

Before upgrading any host to the removed runtime, inspect live dependencies and
obtain owner approval to revoke the existing cloud Google skill. Bundled template
removal is not live revocation. Run `maven-setup --home "$MAVEN_HOME" --migrate-environment`
for the one-time local-only reviewed migration of `MAVEN_GOOGLE_PARAMETER_PREFIX`.
Guided setup presents the same separate persisted review before strict settings loading.
The selected home's `.env` loses only the retired locator; imported `.env` files are
unchanged and use a sanitized private snapshot. Receipts under
`runtime/environment-migrations/` preserve the locator, source/output digests and exact
review. Resume its original operation after interruption. No cloud authority or
credential is changed by this migration.
Reimporting changed source bytes requires a new digest-bound review while preserving
prior receipts; an unfinished migration retains its original source and operation.
