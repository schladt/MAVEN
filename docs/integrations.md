# Harness and optional service integrations

## Harness capability matrix

The baseline is a client-launched local **stdio** MCP process, using the installed
`maven-mcp` executable and explicit `MAVEN_HOME`. Prefer an absolute executable path
so client PATH differences cannot select another installation. Do not put credentials
in MCP configuration. Preserve unrelated entries and use the selected profile/custom
destination. No public endpoint, listening port or mandatory OMP plugin is needed.

The installer uses the same skill and short global pointer in all five clients. Native hooks back that policy where the host exposes events. An installed file, successful contract test or MCP handshake alone does **not** prove turn enforcement in a live client.

| Client | Config / standing guidance | Local turn integration and limits |
| --- | --- | --- |
| [OMP](https://omp.sh/docs/mcp) | `~/.omp/agent/mcp.json` / `AGENTS.md` | `extensions/maven-turn.js` supplies preferences before tools, observes tool results, and checks the reply at stop. Restart with the reviewed extension enabled. |
| [Claude Code](https://code.claude.com/docs/en/hooks) | `~/.claude.json` / `CLAUDE.md` | `~/.claude/settings.json`: `UserPromptSubmit`, `PostToolUse`, `Stop`. The prompt hook can also fire for synthetic events without owner attribution. After opting out, use a **new conversation** to resume; a synthetic “on” prompt must not re-enable capture. |
| [Codex](https://developers.openai.com/codex/hooks/) | `~/.codex/config.toml` / `AGENTS.override.md` or `AGENTS.md` | `~/.codex/hooks.json`: prompt, tool and stop hooks. Review/trust definitions in `/hooks`; disabled or untrusted hooks do not run. |
| [Copilot CLI](https://docs.github.com/en/copilot/reference/hooks-reference) | `~/.copilot/mcp-config.json` / `copilot-instructions.md` | User-profile `hooks/maven-turn.json`. Prompt-hook output is not model context. A pre-tool guard supplies the current key and requires keyed MCP refresh before consequential tools. Stop can report the receipt in the timeline, not rewrite an answer. |
| [Gemini CLI](https://geminicli.com/docs/hooks/reference/) | `~/.gemini/settings.json` / `GEMINI.md` or selected custom context | `BeforeAgent`, `AfterTool`, `AfterAgent`. Requires enabled hooks; delegated/custom tools may not expose file effects. |

Each client reads local state, not AWS. The agent classifies semantic changes and shell/delegated effects; hooks see only supported events, not an operating-system write audit. They cannot force every model or interrupted response to include a line in already-delivered text. A normal reply should append the receipt; the adapter can ask once for completion or display the verified status separately. When native signals are absent, the pointer can guide the agent but cannot enforce timing, opt-out retention after context loss or footer delivery. Never infer success from missing events.

**Qualification:** OMP 19.1.0 was exercised offline in an isolated live client with a deterministic provider, real stdio MCP and temporary SQLite stores. A commit-message-only prompt saw the Nexus preference before the first model request, left Git and both inventories unchanged and produced one verified footer. The other four clients were exercised with native event fixtures through the real hook subprocesses and SQLite; their actual binaries, trust settings and rendered UI were not tested. On each installed profile, restart, request a commit message in a different tracked project, and check the preference, unchanged Git state and final local status before treating that profile as verified.

The optional Claude status line identifies the **local** selected project, not a live cloud connection. During a reviewed integration update, retire only exact, unmodified installer-owned legacy hooks and skills; preserve edited, shared or unowned files for owner review.

### Installation and update boundaries

The normal local installation includes the official Python MCP SDK on Python 3.11+. The installer tracks its MCP entries, guidance blocks and all five files in the `maven` skill bundle for safe removal. Stage/review conflicts
instead of replacing unrelated configuration. Approve exact writes and acknowledge
running-harness configuration risk separately from any required writer quiescence.

Use `--stage-harnesses PATH` to inspect a staged home layout without changing live
configuration. Custom profiles use paired `--harness-config NAME=ABSOLUTE_FILE` and
`--harness-context NAME=ABSOLUTE_FILE`, `--skill-dir PATH` and, for native integration in a custom profile, `--harness-adapter NAME=ABSOLUTE_PATH`. In a headless `--local-only` run the installer first prints the changes and review digest; rerun the same selection with `--approve-harness-review SHA256` using the displayed value. Do not substitute a digest from a different plan. Supported native targets are OMP, Claude Code,
Codex, Copilot CLI and Gemini CLI, not every client accepting a similar skill format.

Codex integration uses an existing nonempty `AGENTS.override.md` when present,
otherwise `AGENTS.md`. If Gemini's custom `context.fileName` excludes `GEMINI.md`,
select the context path explicitly. Identical preexisting integration entries are
referenced, not adopted as installer-owned. Selective removal preserves unrelated
content. Legacy CLI skills are retired only when their bytes match the prior
installer inventory and ownership is unshared; modified, unowned or shared copies
block cutover and require separate review rather than deletion.

First-time project enablement is explicit. Installing turn integration does not configure a Home, authorize cloud changes or grant secret access. The local update report inventories guidance, skills, MCP and adapters and reports edited/shared/conflicting targets without silently replacing them. Verify the effective trust/profile after restarting. See [Getting started](getting-started.md).

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

### Home secret catalog

The installed [secrets procedure](../maven_resources/templates/skills/maven/references/secrets.md) describes explicit credential requests. It does not grant access. The secret catalog is separate from ordinary Home or project memory and is not copied by reconcile.

`maven_secrets` provides `list`, `lookup`, `store_and_catalog`, `retry_catalog` and `resolve_catalog`.
`list` returns paged names/metadata only. `lookup(name=...)` uses the narrow exact
Home secret-setting route, not general memory search. Explicit `store_and_catalog`
reads an owner-private `source_path` and genuine request `source`; never supply a
plaintext value in tool arguments. Choose `provider="ssm"` or `"encrypted-file"`.
It may write exactly that one Home catalog setting without changing the selected memory project, searching other Home records or capturing discussion. For a private terminal handoff:

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

Reusing consumed authorization returns `invocation_consumed`: prepare a new invocation and obtain any required fresh approval; do not replay the old attempt. Other explicit authorization denials return
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

Raw stdout/stderr is withheld from the agent and transcript; only defined nonsecret
results are returned. This is not a sandbox against misuse by a trusted executable.
Approve the actual executable, inputs, destination and effects, not merely its name.
Executable-path approval is not binary attestation or a guarantee of plaintext
zeroization. The bounded consumer input limit is separate from streaming file restore.


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
