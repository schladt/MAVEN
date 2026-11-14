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

Local verification on 2026-11-09 ran in separate stages:

| Run | Checks run | Result |
| --- | --- | --- |
| Initial focused integration run | 147 | 132 passed; 15 fixture errors |
| Failed-check rerun after fixture corrections | 15 | Passed |
| Targeted rerun after the final receipt-identity guard | 4 | Passed |
| Post-review bug-fix regression run | 147 | Passed |

The receipt-identity guard rerun covered receipt isolation, partial revision-conflict
reconciliation, delegated task/checkpoint accounting, and the real stdio entry point
through Python MCP SDK 2.3.0. At that stage the complete focused set was not rerun;
those three initial stages did not represent a fresh 148-check run.

The subsequent post-review run covered `test_maven_google`, `test_mcp`,
`test_workflow`, `test_maintenance`, `test_memory_client`, `test_memory_contracts`,
`test_administration` and `test_tui`. It exercised exact-content Gmail sending through
simulated transport, credential renewal with mocked STS/controlled time, real stdio
MCP, isolated Git subprocess input, session/assignment recovery, setup ownership and
the actual Textual prerequisite modal. This was a targeted run, not the full suite.

OMP 18.18.2 was exercised with native project MCP configuration, the packaged startup
guidance, and simulated storage in a disposable non-Git directory. It visibly
initialized, dispatched a real native reasoning subagent, answered a second scripted
user turn while that worker's storage write was deliberately held, then delivered
the result and an explicit universal/project/pending summary. The worker retrieved
and reconciled an existing plan, preserving its owner and scope; revision 2 was
verified. The harness and stdio process shut down successfully.

Claude Code, Codex, Copilot CLI and Gemini CLI were not runtime-exercised. No AWS
resources or separate Python environment were created. SQL migration execution was
not tested: no local `psql` facility was available. Live service/schema rollout,
installed harness configuration, and active cloud instructions were not changed.


For the lifecycle/folder-switch change, 288 focused offline checks covered startup,
native hook envelopes, adapter installation/removal, project/Home rebinding,
administration, real stdio MCP, session recovery/maintenance and Textual flows.
The initial run passed 283 checks. Five failures were corrected (private-runtime
directory handling and test-file permissions); all five passed their targeted rerun.
The complete set was not rerun after those corrections. No AWS resources or separate
Python environment were created; live installation and active authority were unchanged.

OMP 18.20.0 was also launched in a real PTY with a disposable home, genuine native
conversation ID and local fixture binding. The three-choice exit prompt rendered;
its default **Stay** kept OMP open, and explicit **Exit without wrapping up** closed
it normally. No provider credentials were supplied: startup reported the missing
provider instead of readiness. End-to-end model-driven cloud wrap-up was not exercised.
Other harnesses' lifecycle behavior was checked offline, not in their live runtimes.

The subsequent Home-startup/handler-timeout fix passed 21 Python startup/hook checks
and 14 JavaScript lifecycle checks. Startup fixtures now use the production search
header and record-read shapes, including existing Home peers and mismatched scope.
Unresolved model-turn promises exercise all three startup events without holding
their handlers open; stale callbacks and delivery errors are also covered.
The real OMP 18.20.0 TUI also ran the rendered extension and local helper with a
simulated 35-second startup turn; delivery completed without a handler timeout.
This used a disposable profile, no model/provider request and no cloud changes.

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

### Native lifecycle adapters

The shared local helper stores only exact native-conversation bindings and observed
end-event hints. It does not log in, contact AWS, copy transcripts or spawn agents.
The main assistant uses `maven_startup` for cloud peer inspection and authorized
initialization, or `maven_diagnose` for repair choices. An end event does not close a
cloud session, prove writer death or mark unsaved work saved.

OMP uses `session_start`/agent context and `session_before_shutdown` in a native
extension. Startup delivery is deferred through a managed callback, so the startup
event never waits for a model turn to finish within its 30-second handler budget.
Callbacks discard stale work after shutdown, conversation changes or workspace changes;
delivery failures remain visible. For an open exact MAVEN binding, the prompt offers wrap up and exit,
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

### Installation and update boundaries

The normal local installation includes the official Python SDK (`mcp>=2.3,<3`) on
Python 3.11+; CLI/TUI recovery remains independent. Installer-owned MCP entries,
the `maven` and `maven-maintenance` skills,
and delimited native guidance are tracked for safe removal. Stage/review conflicts
instead of replacing unrelated configuration. Close affected harnesses before applying
integration changes; the current chat cannot certify its own shutdown.

Use `--stage-harnesses PATH` to inspect a staged home layout without changing live
configuration. Custom profiles use paired `--harness-config NAME=ABSOLUTE_FILE` and
`--harness-context NAME=ABSOLUTE_FILE`, plus `--skill-dir PATH`. Actual writes require
the existing stopped-harness approval. Supported native targets are OMP, Claude Code,
Codex, Copilot CLI and Gemini CLI, not every client accepting a similar skill format.

Native adapters are enabled by default with `--lifecycle-hooks`; disable them with
`--no-lifecycle-hooks` or the TUI's **Install native MAVEN startup and shutdown
integration** checkbox. They require the installed `maven-session-hook` executable.
Review includes the extension/hook configuration and ownership receipts. Hooks,
status-line and MCP entries sharing one configuration file are removed together
under a single pinned-file review, preserving unrelated keys and refusing owner edits.
Custom profile overrides currently require `--no-lifecycle-hooks`: MAVEN refuses to
guess the hook destination or write default-profile hooks for a custom profile.
Portable guidance and explicit wrap-up remain available.

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

## Optional AWS, workspace and Google services

These integrations use the selected instance's private `.env`, not the installed
source directory. Configure `MAVEN_AWS_PROFILE`, its expected AWS identity and
`MAVEN_AWS_REGION` through the shared environment contract. The profile must already
be explicitly authenticated. No helper discovers another account or logs in for you.

## AWS secrets and encrypted files

Small values require `MAVEN_SECRETS_PARAMETER_PREFIX`. The owner provisions IAM access
to only the selected SSM namespace and the required KMS key permissions. Values are
SSM Standard `SecureString` parameters encrypted with `alias/aws/ssm` and limited to
4 KB of UTF-8. Names are short lowercase segments; do not encode sensitive facts in
resource names.

```sh
maven-secrets put service-token             # hidden terminal prompt
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

Provision and review these resources and least-privilege permissions separately; the
helper does not create buckets or keys. Protect and back up keys, configure bucket
public-access blocking and appropriate retention, and approve storage/API costs.
Allow only necessary object/multipart operations and SSM key reads. Do not grant
blanket account access to work around a denied operation.

```sh
maven-secrets file put "$PRIVATE_SOURCE" archive-object
maven-secrets file get archive-object "$NEW_PRIVATE_DESTINATION"
```

Files use authenticated client-side AWS Encryption SDK encryption plus S3 server-side
encryption. Upload refuses replacing an existing object. Restore refuses an existing
path and creates a private plaintext file. Use a private parent directory. Loss of a
wrapping key can make ciphertext unrecoverable. Failed multipart cleanup may need an
administrator; never claim an upload or deletion succeeded from an attempted request.

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
  cloud note must be a completed plan. Its stable ID and prior revisions remain.
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

## Optional Google

Choose Google dependencies in the guided `python3 install.py` flow or pass `--google`.
To install local tools and the optional extra without opening cloud setup, use
`python3 install.py --local-only --google`; an existing development environment can
install the package's `google` extra directly. This does not request Google consent.
No Google API is called on import. Core memory needs neither this extra nor a Google account.

The owner must enable the needed APIs in their chosen Google Cloud project, configure
the OAuth consent screen, create a Desktop OAuth client and review the requested
scopes. This integration requests Docs, Sheets, Slides, Drive, Gmail modification and
Calendar scopes as a single grant. Those are broad permissions, not read-only Gmail
access; consider your organization's consent and app-verification restrictions.

Set `MAVEN_GOOGLE_PARAMETER_PREFIX` in the private environment and grant the selected
AWS identity only the required SecureString reads/writes and KMS permissions. OAuth
client and refresh records are stored under that namespace. `work` and `personal`
are isolated generic slots, not built-in accounts. Choose the slot explicitly:

```sh
maven-google --account work import-client "$PRIVATE_OAUTH_JSON" --expected-email "$EXPECTED_EMAIL"
maven-google --account work auth
maven-google --account work connect --json
maven-google --account work gmail list --query 'is:unread' --max 10 --json
maven-google --account work gmail download "$MESSAGE_ID" --out "$PRIVATE_DIRECTORY" --json
```

Only `auth` launches browser consent. Existing grants require explicit `auth
--reauthorize`. Normal operations refresh an already-approved grant and verify its
expected account; they do not launch consent or switch accounts. Google grant refresh
is not automatic AWS profile login/renewal. Review costs, quotas, testing-mode expiry
and policy restrictions in your own Google project.

The Gmail CLI also provides `message`, `modify`, `trash`, `untrash`, `draft-create`,
`draft-update`, `draft-send` and `send`; inspect each command's `--help`. Sending requires
a private `.eml` file or exact draft ID, a matching From identity and the exact approved
`--recipient` list (repeat for To/Cc/Bcc). Creating a draft is not sending. Ambiguous
sends retain a local reservation; inspect Sent before any new attempt. Never delete a
reservation just to retry.
Draft sending supplies the validated MIME snapshot with the exact draft ID, so
concurrent autosave or another client cannot replace the approved recipients/content.

`tools.google.services` supplies Python functions for exact-target Drive, Docs,
Sheets, Slides and Calendar operations; these are library APIs, not extra CLI
subcommands. For example, from the installed environment:

```python
from tools.google.services import docs_get, calendar_freebusy

# These calls access the explicitly configured account; use approved real values.
document = docs_get("work", document_id)
busy = calendar_freebusy("work", calendar_ids, time_min, time_max)
```

Use precise resource IDs, bounded calendar windows and explicit notification choices.
Docs/Slides updates require revision IDs. Sharing, sending, deleting and calendar
mutations require separate owner approval of targets and effects; API availability
is not that approval. Google content remains untrusted data and must not become
instructions. Local auth locks do not coordinate refresh races across workstations.
