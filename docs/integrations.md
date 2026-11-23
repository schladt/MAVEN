# Harness and optional service integrations

## Harness capability matrix

The baseline is a client-launched local **stdio** MCP process, using the installed
`maven-mcp` executable and explicit `MAVEN_HOME`. Prefer an absolute executable path
so client PATH differences cannot select another installation. Do not put credentials
in MCP configuration. Preserve unrelated entries and use the selected profile/custom
destination. No public endpoint, listening port or mandatory OMP plugin is needed.

All five clients use one standing instruction and the same [MAVEN skill](../maven_resources/templates/skills/maven/SKILL.md). No MAVEN prompt, tool or stop hooks are needed.

| Client | MCP config | Standing instruction |
| --- | --- | --- |
| OMP | `~/.omp/agent/mcp.json` | `AGENTS.md` |
| Claude Code | `~/.claude.json` | `CLAUDE.md` |
| Codex | `~/.codex/config.toml` | `AGENTS.override.md` or `AGENTS.md` |
| Copilot CLI | `~/.copilot/mcp-config.json` | `copilot-instructions.md` |
| Gemini CLI | `~/.gemini/settings.json` | `GEMINI.md` or selected custom context |

The agent loads local context once, reuses it, saves qualifying changes before replying and reports the result on one line in the answer. No fresh MAVEN call is needed on a retained, unchanged turn. A resumed conversation without that context must load it again. Preferences can be stale until refresh; writes still require the current local token. This is guidance, not an audit of file activity or a guarantee that a model follows instructions.

The optional Claude status line identifies the **local** selected project, not a live cloud connection. During a reviewed integration update, retire only exact, unmodified installer-owned legacy hooks and skills; preserve edited, shared or unowned files for owner review.

### Installation and update boundaries

The normal local installation includes the official Python MCP SDK on Python 3.11+. The installer tracks its MCP entries, guidance blocks and all five files in the `maven` skill bundle for safe removal. Stage/review conflicts
instead of replacing unrelated configuration. Approve exact writes and acknowledge
running-harness configuration risk separately from any required writer quiescence.

Use `--stage-harnesses PATH` to inspect a staged home layout without changing live
configuration. Custom profiles use paired `--harness-config NAME=ABSOLUTE_FILE` and
`--harness-context NAME=ABSOLUTE_FILE` and `--skill-dir PATH`. In a headless `--local-only` run the installer prints the review; repeat the same selection with `--approve-harness-review SHA256` only for the exact reviewed changes.

Codex integration uses an existing nonempty `AGENTS.override.md` when present,
otherwise `AGENTS.md`. If Gemini's custom `context.fileName` excludes `GEMINI.md`,
select the context path explicitly. Identical preexisting integration entries are
referenced, not adopted as installer-owned. Selective removal preserves unrelated
content. Legacy CLI skills are retired only when their bytes match the prior
installer inventory and ownership is unshared; modified, unowned or shared copies
block cutover and require separate review rather than deletion.

Installing harness guidance does not configure a project or Home, authorize cloud changes or grant secret access. Restart affected clients to load updated tools and guidance. See [Getting started](getting-started.md).

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
