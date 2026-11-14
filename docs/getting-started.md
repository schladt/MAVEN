# Getting started

Install/connect the selected instance once, then ask your assistant to enable MAVEN
for an explicitly chosen folder. Project enablement is not part of installation.
Supported sessions initialize visibly through native guidance and stdio MCP. No manually
assembled UUIDs, JSON requests or lifecycle commands are needed for ordinary work.

Start with the [README prerequisites](../README.md#before-you-start). A source
checkout does not supply a configured virtual environment, private `.env`, or your
project's `.maven` directory. The installer creates the private installation home;
project state is created separately through explicit harness enablement. Installing
tools in a checkout does not register that checkout as a project.

## Guided first-time setup

From the cloned or extracted source folder:

```sh
python3 install.py
```

The full-screen Textual interface starts with your goal: **connect existing memory**,
**create a new instance**, or **Uninstall**. An existing installation shows its saved, unverified
instance/account/region/profiles and offers **check or repair**, **update local
software**, or **set up a separate instance**. Check/repair does not reinstall packages
or automatically redeploy. A separate instance requires an unused private home.
Software-only installation and optional Google/certificate settings are under
**Advanced options**. The private installation home defaults to `~/.maven`.

Python 3.11+ with `venv` is required. If UI dependencies are missing, a brief initial
prompt requests consent to download them into a temporary private runtime; system
Python is never modified.

Use Tab/Shift+Tab to move focus, arrows to select, Space to toggle checkboxes and
Enter or the mouse to activate controls. Back revises unsubmitted choices; it never
silently undoes external effects. Reviews explain what changes and why, with complete
artifacts under expandable technical details. The current access purpose, selected
profile and account remain visible. At narrow widths the step sidebar is hidden;
use at least an 80-column, 24-row terminal.
Ctrl+Q or Ctrl+C requests a safe pause. The current action may need to finish first;
already-submitted AWS work can continue remotely. Closing is not rollback and does
not leave a persistent MAVEN background worker.

The installer checks Git and an AWS CLI with `aws login` support and gives
installation guidance; it never silently installs system packages. Under Advanced,
choose **Install software without connecting to cloud memory** when AWS prerequisites
are unavailable. Git is unnecessary for a no-Git project obtained through an archive.

The local installation creates `venv/`, `.env` when absent, private runtime/login
cache directories and an `activate` shell snippet under the selected home. Afterward:

```sh
source "$HOME/.maven/activate"
```

Use your selected home's `activate` path if different. This sets `MAVEN_HOME` and
PATH; it is **not** the private `.env`. Never source `.env`.

### Pick the connection path

**Existing instance:** leave the connection override blank for the guided path.
If this private home already has a connection, setup reuses it. Otherwise:

1. Select maintenance access, sign in if needed, and confirm the actual AWS account
   and principal.
2. MAVEN searches CloudFormation in the selected region, reading all result pages.
   Choose the intended stack explicitly, even if only one was found. The list shows
   stack names, instance IDs and status; only ready stacks with valid MAVEN outputs
   from the selected account are offered.
3. Select daily-use access, then review the complete connection before binding memory.

Discovery is read-only and requires `cloudformation:DescribeStacks` permission to
enumerate stacks in the selected account/region. Missing access or no eligible stacks
produces a recoverable blocker, never automatic deployment or selection. Check the
account, region, permissions and deployment status, then resume the same operation.
The chosen stack ARN is saved as the locator to avoid silently selecting a replacement
with the same name; connection review and binding re-read that exact stack.

You can still supply an exact stack name or an absolute path to another configured
installation's `.env` as an optional override. Importing settings does not import
credentials. If MAVEN has not been deployed, choose **Create a new MAVEN instance**
using a separate unused private home.

The wizard derives locators from the imported file or actual stack outputs and asks
you to confirm instance and principal identities. Neither method redeploys,
initializes an existing database, or activates replacement authority. Existing
authority must already be available; missing authority is a blocker, not permission
to reset it.

**New instance:** sign in and confirm the discovered account/principal, then provide
the stack/instance name, notification email and two comma-separated availability zones
(for example, `us-east-1b, us-east-1c`; surrounding spaces are accepted). Select
daily-use and maintenance access explicitly. Review the limitations and costs below.
The instance name automatically follows a compatible stack name until you override
it. Stack names that do not meet the instance's 2–32-character lowercase naming
rules require a separate instance name. The form suggests `us-east-1a, us-east-1b`
and Aurora PostgreSQL `18.6`; both remain editable. These are suggestions, **not
verified account/region availability**. Confirm zone availability and engine
support for Data API and auto-pause before approving deployment. Saved answers
remain preserved when revisiting a checkpoint.
**Prepare deployment review** requests local package preparation and a CloudFormation
change-set preview; it does not deploy billable resources. The next approval binds
billable execution to the exact generated package and change-set digest. Database
initialization remains a separate consequential approval.

Starter instructions are prepared locally without an extra confirmation. **One
collection approval replaces the eighteen per-page submission/activation approvals**:
it authorizes uploading and activating all nine exact instruction pages, in dependency
order. The review explains what “authority” means, lists page titles, and provides
complete contents under **Technical details**. Progress reports the current page
and completed count. This is not an all-or-nothing cloud transaction: completed
pages remain active after a pause or failure. Resume preserves the approval and
original request identifiers; changed contents or connections cannot reuse it.
Older individual starter prompts advance to one collection review without replaying
already completed pages. Unknown outcomes always reconcile the original request.

The default flow removes duplicate new-instance region confirmation, separate
preview-creation approval, local starter-preparation approval and per-page approvals.
Actual AWS identity confirmation, billable execution, SQL initialization/migration,
the starter collection, and changed local harness files retain meaningful approvals.
Expandable technical details provide deeper inspection without a second granular
review mode. Destructive removal confirmations remain separate.

Current-step context is inside the bordered panel; its overflow is scrollable in
small terminals. A bottom status bar distinguishes **Waiting…**, **Awaiting input**,
pause requested, paused, failed and complete. Declining an approval preserves
the operation rather than claiming completion or rollback.

**Daily-use access** operates memory. **Maintenance access** manages CloudFormation
or reads required stack metadata; it is requested only when needed. These purposes
can use identities in the same AWS account. A configured existing connection does
not require maintenance access just to verify memory.

Use a named `aws login` profile, not root or static access keys. A profile is a local
label, not an access grant. Saved/imported account and principal details are labelled
as expected, not verified. On a first install they may be unknown until sign-in;
setup then explicitly confirms the actual STS account and principal before changes.
An identity differing from saved bindings is blocked, not silently substituted.
Choose **Sign in**, manual instructions, Back or pause; login is never launched
without that choice. A failed login offers manual recovery, not a readiness claim.

MAVEN keeps its own private AWS sign-in caches inside the installation home
(`runtime/aws-login-cache` for daily use, `runtime/aws-deploy-login-cache` for
maintenance). A plain `aws login --profile …` writes the AWS default cache, which MAVEN
does not read. When a sign-in expires, use the exact command MAVEN shows (in the
installer's **Sign in** screen, a blocked tool result, or `maven_status`), for example:

```sh
AWS_LOGIN_CACHE_DIRECTORY=~/.maven/runtime/aws-login-cache aws login --profile maven-interactive --region us-east-1
```

Read-only access preflights distinguish **Passed**, **Missing access**, and
**Could not verify**. Missing access blocks the affected phase with remediation and
retry of the same operation. Unknown checks remain disclosed in reviews, not relabelled
as success. IAM simulation covers representative actions, not every resource
condition, organization control, quota or dependency; role assumption and reads
cannot prove write permissions. No test resources are created by these probes.

Importing `.env` does not copy credentials or reuse another machine's cache paths.
Choose owner-private local login caches, separate for daily-use and maintenance.
See [AWS command-line sign-in](https://docs.aws.amazon.com/signin/latest/userguide/command-line-sign-in.html)
for account/identity prerequisites and login permissions.

For interactive AWS sign-in, the TUI temporarily restores the normal terminal and
resumes afterward. Authentication output is not captured into the progress log.
The actual returned principal is verified before any subsequent cloud action.

### Optional harness connection and project enablement

After verified cloud access and complete approved authority, optionally configure
OMP, Claude Code, Codex, Copilot or Gemini. Select harnesses directly, then approve
one exact local file-change review that also confirms affected harnesses are stopped.
Identical already-configured integrations require no repeat approval or rewrite.
Skip leaves their files unchanged. Conflicting or modified files are preserved;
partial completed writes remain recorded in selective-uninstall ownership receipts.
If an older install left `venv/bin/maven-mcp` group-writable (`0775`), this same
review can repair that exact installer-owned executable to `0755` and update only
its ownership entry. Already manually-corrected launchers can have their stale
ownership entry reconciled. Modified, unowned, linked or otherwise unsafe targets
remain untouched. The conflict screen offers local retry without cloud setup.
New installs use a safe package-install umask and record safe launcher permissions.

Completion distinguishes **cloud verified**, **harness files written/skipped/conflict**,
**restart needed**, and **harness MCP connection not yet verified**.
Open the actual project in a new selected-harness session and ask:
**“Enable MAVEN for this project.”** Review the root, instance and scope before approval.
The installer never chooses a project folder or binds its source checkout.

Project enablement can select an existing cloud project or create a named one with
genuine source evidence. Memory needs no Git configuration; optional Git assistance
is separate. Project selection does not commit, push, pull or switch branches.

### Resume instance setup

After activating the installed commands, resume interrupted or declined setup with:

```sh
maven-setup
```

For a separate home:

```sh
maven-setup --home /absolute/path/to/private-home
```

Use your actual installation directory. Keep `runtime/setup`, review files and
pending receipts intact. Resume retains the exact operation, requests and approvals,
and reports its saved phase. Back only revises safe, unsubmitted choices.
Unknown outcomes must be reconciled before retrying mutations; existing infrastructure
must never be reinitialized to recover onboarding.

Older installers could stop with `KeyError` after signing in when an existing-instance
connection was left blank without saved settings. Pause with Ctrl+Q and restart
the updated installer, then choose **Resume unfinished setup**. The same operation
now reconfirms maintenance access and searches AWS for the missing target, followed
by explicit stack selection and connection review. Historical approvals and receipts
remain intact. Do not delete settings or receipts to recover.

`maven-setup` opens the same setup screens and reads existing receipts. Guided
installation continues directly into cloud setup; if paused, activate that home and
run `maven-setup` when ready. `--mode existing|new` selects the task without asking
again. **Completed, verified setup resumes optional harness configuration by default**,
including an explicit `--operation` for that completed record. No new operation or
cloud checks are started when its exact saved settings still match. The installer
offers **Continue / configure coding harnesses** for that state.

Starting a new check/repair after completion is explicit: choose that task or use
`--fresh-operation`. This never discards an unfinished or unverified operation.
If an older installer already started another connection check, it remains unfinished
and resumes its current checkpoint—not the earlier completed starter installation.
If recorded readiness lacks a recoverable exact binding or settings changed,
restore the original settings or explicitly request a fresh check; readiness is
not inferred from today's settings.

To abandon the current unfinished setup, rerun `python3 install.py` and choose
**Abandon unfinished setup and start over**. Review the exact operation and type its
displayed confirmation prefix. Declining leaves it unchanged. Approved abandonment:

- Preserves the original operation, setup state and private `.env` contents in
  `runtime/administration/restart-<original-operation-UUID>.json`.
- Leaves original request artifacts, approvals, receipts, installed software, AWS
  login caches and cloud resources intact. The old operation becomes read-only.
- Clears active connection settings and starts a distinct setup with fresh choices,
  approvals and an isolated artifact directory. Projects using this home need a new
  completed connection before memory works again.

Stop/recover registered MAVEN sessions and release reservations first. Setup locks
prevent abandoning a currently running local setup. If local tools were removed,
the installer offers its normal reviewed software-only restoration before restart.
An interrupted restart resumes its journaled replacement operation; it never resets
new settings again after a completed restart.

**Abandonment is not cloud rollback:** submitted AWS work may continue and resources
may still incur charges. Unknown effects remain unresolved. Reconnect to existing
resources or manage them separately; do not duplicate an unresolved deployment.
The recovery CLI is `maven-setup --home PATH --restart-operation UUID`, using the
actual home and original operation ID. Unlike `--fresh-operation`, this is an explicit
reviewed abandonment; it cannot be combined with `--mode` or `--operation`.

Instance completion requires verified daily-use cloud access and complete applicable
authority. Project enablement is separate. For an interrupted project operation,
preserve its `.maven/runtime` receipts and resume its original `maven-project setup`
choices. Legacy installer project receipts are retained, not executed or deleted
by the redesigned installer; unresolved submitted effects require reconciliation.

The `maven_admin` Back action exposes the same safe navigation to harnesses: it
returns a fresh editable interaction while retaining historical approvals and
receipts. It refuses to cross submitted or unresolved effects.

Conversational setup preserves a durable operation identity. The agent records a
response, then advances one explicit phase; recording consent alone does not execute
the next external action. `maven-setup --operation UUID` resumes that exact saved
operation (use its actual returned identity). Reviews retain the full digest and real
owner source evidence, not invented TUI approval provenance.

For a later reviewed configuration or migration, the assistant can begin a new
`maven_admin` setup operation after the previous setup completed with verified
instance readiness. The old wizard state and receipts are preserved, and new
preparation artifacts are isolated. Ordinary new operations cannot take over unresolved,
cancelled or unverifiable setup. Use original receipts for recovery, or explicitly
review abandonment through the installer; abandonment does not resolve cloud effects.
External-prerequisite screens show the command, required environment and exact resume
command inside the review modal. Decline to pause safely while completing the handoff.

### First use and everyday work

Configure the selected harness to launch the installed `maven-mcp` stdio entrypoint
and load its thin startup/intent guidance. See the [capability matrix](integrations.md#harness-capability-matrix)
for configuration formats and limitations; availability is not permission to capture.

Ask **“Enable MAVEN for this project.”** Confirm the root, instance, project and
authorized scope. Later sessions reuse that project selection, establish a distinct
work-session binding and load complete current authority, handover, tasks and blockers.
The visible initializing/ready/blocked messages report actual readiness, not merely
server startup. Where startup guidance cannot run reliably, ask **“Initialize MAVEN.”**

Ask to save or consult memory, reconcile a plan, checkpoint, pause/resume or wrap up.
Routine maintenance stays quiet and uses native background reasoning where available,
otherwise foreground reconciliation. Explicit saves confirm verified cloud receipts.
Universal/current-project retrieval is the default; other-project consultation is
explicit. Ordinary checkpoints have a cutoff and exact revisions, not Git effects.
“Checkpoint and wait” synchronizes; “resume” catches up from available discussion,
while “resume fresh” does not. See [workflow](workflow.md) for these boundaries.

After restarting a harness, the assistant checks `maven_status` before claiming a
connection; an unbound process lists the active registered sessions for the folder and
resumes only the exact one. Outside an enabled project, see the start choice below.

**Optional keep-warm:** set `MAVEN_KEEP_WARM=1` in the private `.env` (or the MCP
process environment) to have an initialized MCP process ping the instruction index every
240 seconds, keeping Aurora from auto-pausing (5 minutes idle) during active sessions.
It is off by default: each ping is a billed Lambda/Data API request, and a database kept
awake accrues compute charges for as long as the session stays bound. Leave it off when
cold-start latency is acceptable.

Additional projects only need their own explicit enablement, not another instance
deployment. A new/forked session gets its own identity; explicit resume preserves the
original. Cwd changes, transport reconnection and newest receipts never select a new
project/session implicitly. Missing access blocks dependent work, not unrelated chat.

### MAVEN Home and the start choice

At the start of each session the assistant calls `maven_status` with its workspace
folder. Inside an enabled project it initializes that project as before. Anywhere
else it asks one line, every time:

```text
MAVEN: no project here — Start Home, Create Project, or Read Only?
```

- **Start Home** initializes your MAVEN Home (offered only when Home is configured).
- **Create Project** runs normal enablement for a folder; `$HOME`, `/`, `~/.maven` and
  the MAVEN checkout are refused.
- **Read Only** (also the result of not answering) creates no session and captures
  nothing. When you ask, the assistant consults universal knowledge, and Home if you
  want it, read-only.

The answer is recorded locally only. Subagents and maintenance workers inherit the
choice and never ask. Nothing is captured without a project or Home choice, and a cwd
change never rebinds a session. The assistant prints the active line, such as
`MAVEN: Home (<label>)`, `MAVEN: project <name>` or `MAVEN: read-only`, at start and
whenever it changes.

**MAVEN Home** is an optional, per-machine project for your life, personal and work
tasks, and awareness of people and organizations (for example tag `client:<slug>`).
Set it up, change it or disable it with **“Set up or change MAVEN Home”** in the
installer, or ask the assistant (`maven_admin` kind `home`). Disabling leaves cloud
data untouched. Home is the one explicit, owner-configured exception to MAVEN never
choosing a root by itself. A Home session also starts with a short **project index**:
up to 100 non-archived projects' names and last activity, with no project details,
so the assistant knows what exists and which project to open. Archived projects
appear only when you explicitly request archived-history discovery.

To use the same Home knowledge on another machine, select the **same existing cloud
project ID** during Home setup. Names are labels, not identity. For a folder already
bound to the wrong project, ask **“Switch this folder to my existing Home project.”**
The project/Home administration form has an explicit **switch** mode; it reviews
old/new identities and local preservation before changing anything. Finish local
sessions first. Old cloud data and other machines remain unchanged; no cleanup or
merge is implied. The project index's `is_current_context` flag identifies the
current view, not a globally unique Home project.

Optional startup adapters supply the genuine native conversation identity. MAVEN
resumes that exact open session, keeps paused mode, or creates a distinct session for
a new conversation. Stale/unfinished peers offer recovery, new work without takeover,
or Read Only; no peer is automatically closed. Ask **“Review MAVEN recovery”** for
diagnosis without replaying writes.

OMP's optional exit guard offers wrap-up/exit-anyway/stay for an open mapped session.
It waits for verified MAVEN closure before automatically exiting. Other supported
harness end hooks are best-effort notices, not exit prevention. Forced termination
can bypass every hook; next-start recovery remains necessary.

Scoping: **universal** holds preferences, instructions and general lessons only;
nothing specific to one customer or project, because universal records appear in every
project. **Home** holds the owner-level material above. **Project** holds that
project's detail. When unsure, the assistant chooses Home, not universal.

### Installer options and harnesses

`python3 install.py --local-only` installs local tools without invoking cloud setup.
Use it for a package-only installation; downloads still require network access or an
appropriate package cache, and it does not establish cloud readiness. `--home PATH`
chooses another private home, `--google` installs
optional client libraries without granting Google account access, and `--harness NAME`
selects a harness (repeat for several). `--skill-dir PATH` explicitly selects a custom
destination. Running harnesses do not block interactive skill installation: they load
the change after a restart, and a harness that saves its own configuration while running
can overwrite the MAVEN entry, so confirm it is listed afterward. A noninteractive
`--local-only` install still requires `--harness-stopped` when selecting harness files.
Conflicting files are preserved; interactive setup can continue without changing them.
Start a new harness afterward to discover the generic skills.

The installed generic skills are `maven` (intent front door), `maven-maintenance`
(bounded semantic worker) and `maven-update` (updates MAVEN itself; see
[Existing-instance upgrade](#existing-instance-upgrade)). An installer-owned skill
you have not edited is upgraded when MAVEN updates; edited or shared copies are kept.
Native startup guidance and an owned MCP configuration
entry complement them. They are bootstrap routing, not cached cloud authority.
Unrelated user configuration is preserved; conflicts or modified owned content require
review rather than overwrite. Stage changes and close affected harnesses before
applying them; a running assistant cannot truthfully attest it has stopped.

Custom profiles need an explicit destination. Client configuration, trust, startup,
background execution and transcript access differ; consult the
[integration matrix](integrations.md#harness-capability-matrix). No native OMP extension
is required. Hosted clients need separately provisioned access and are not covered by
local stdio installation.

To stage selected integrations without live writes, use `--stage-harnesses PATH`.
For custom profiles, pair `--harness-config NAME=ABSOLUTE_FILE` with
`--harness-context NAME=ABSOLUTE_FILE` and an explicit `--skill-dir PATH`. Actual
integration changes still require stopped-harness approval. Normal installation
includes `ui,mcp`; optional Google dependencies remain separate.

Native startup/shutdown integration is enabled by default for selected harnesses.
Uncheck **Install native MAVEN startup and shutdown integration** or pass
`--no-lifecycle-hooks` to keep only portable startup guidance and explicit wrap-up.
Custom profile overrides require that flag until an explicit native hook destination
is supported; the installer will not guess or alter the wrong profile. Staging
includes lifecycle files when enabled. Restart the selected harness after applying.

### Optional AWS Agent Toolkit

During AWS sign-in, you may be offered AWS skills and an AWS MCP server connection.
This is AWS's optional Agent Toolkit, not MAVEN. You can install it separately or
decline it without blocking MAVEN setup. Fetching/listing skills such as Bedrock or
billing does not itself deploy or enable those services.

AWS installs skills globally for the selected agents. OMP supports Claude-format
skills, but current upstream OMP requires opting into foreign user-level discovery
through `enabledProviders`; the installed version and settings determine whether
Claude's skills are picked up. Start a new OMP session afterward. AWS detecting
Claude but not OMP does not establish incompatibility, and skill discovery alone
does not establish an MCP connection.

Skills influence agent behavior; MCP tools can read or change AWS resources where
permissions allow. Use a least-privileged identity, not root or a broad deployment
identity. MAVEN's approval gates do not automatically govern separate AWS MCP calls.
See [AWS toolkit setup](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/aws-cli.html)
and [OMP skill discovery](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md).

### VPNs and corporate certificates

Ask your administrator for a trusted PEM CA bundle if your network requires one.
Pass its actual path explicitly, for example:

```sh
python3 install.py --ca-bundle /absolute/path/to/approved-ca-bundle.pem
```

The installer validates the bundle and sets `AWS_CA_BUNDLE`, `SSL_CERT_FILE` and
`REQUESTS_CA_BUNDLE` for its children. An explicitly selected bundle is recorded in
a new private `activate` snippet for later sessions, never in `.env`. Existing
activation snippets are preserved; if the CA selection differs, the installer prints
the required exports instead. Use those commands before resuming setup. The PEM bundle
must include the trusted roots needed by your network; obtain it from your administrator.
No corporate roots ship in MAVEN, and TLS verification stays enabled. Do not accept
an unverified certificate merely to get past an error.

## Advanced manual reference

Conversation through MCP is the normal operating path. The remaining sections retain
CLI/TUI administration and recovery references, not routine memory prerequisites.
Use the installed release's help for exact argument contracts. All shell variables below
mean actual owner-reviewed values: define them from private settings and command results.
MAVEN provides no preconfigured account, endpoint, credentials or owner authority.

### Deployment limitations and costs

Edit the private `.env` as literal settings (mode `0600`); do not source it as a shell
script. Uncomment only selected fields. Keep credentials in their designated provider
stores, not in this file. Use the packaged `environment.example` comments to identify
the complete supported configuration.

The current infrastructure template deliberately supports **us-east-1**, two explicitly
reviewed availability zones, and an explicit same-account human IAM user principal.
It is not a generic cross-account, any-region or federated-role deployment template.
Its defaults are Aurora PostgreSQL 18.6 and an ARM64 Lambda `python3.15` runtime.
Verify their actual regional availability and required Data API/auto-pause support
before planning or approving deployment; offline packaging does not establish that
these versions are offered. Do not substitute an arbitrary principal or broaden trust
just to pass a check.

Configure these owner-selected groups:

- Routine login: `MAVEN_AWS_PROFILE`, `MAVEN_AWS_EXPECTED_PRINCIPAL`,
  `MAVEN_AWS_LOGIN_CACHE`, `MAVEN_AWS_REGION`.
- Separate setup login: `MAVEN_DEPLOY_PROFILE`, `MAVEN_DEPLOY_EXPECTED_PRINCIPAL`,
  `MAVEN_DEPLOY_LOGIN_CACHE`. Keep login-cache directories private (`0700`) and
  separate. Authenticate these profiles explicitly using your approved AWS login
  method before manual cloud commands; there is no silent login or default-profile fallback.
- Deployment: `MAVEN_STACK_NAME`, `MAVEN_INSTANCE_NAME`, `MAVEN_ACCOUNT_ID`,
  `MAVEN_CALLER_PRINCIPAL_ARN`, `MAVEN_NOTIFICATION_EMAIL`,
  `MAVEN_AVAILABILITY_ZONES` (two comma-separated selected zones), and optionally
  `MAVEN_ENGINE_VERSION`.

The owner/admin must review the exact required deployment permissions and runtime,
admin and approval-role trust. Deployment permissions are not runtime permissions.
Review the generated policy/permissions artifacts rather than granting broad
administrator access by default. An explicit transport CA override, if needed, is
private process configuration; never ship corporate roots or disable TLS verification.

**Costs and retention:** this is an AWS deployment, not a free hosted service. Review
Aurora serverless compute, database/storage/backups, Secrets Manager, S3 object and
version storage, Lambda requests, logs, data transfer and other resources in the
rendered template using current provider pricing. The template permits zero minimum
capacity with auto-pause, but that is not a zero-cost guarantee. Budget notifications
are not spending caps and tagging/reporting may undercount or lag. Retained resources,
old versions and deletion protection mean deleting the stack is not full teardown;
resource disposal requires separate review and approval.

### Prepare, review and apply an exact deployment

Choose a private output directory that does not already contain an installation
package. Preparation is offline:

```sh
maven-memory install prepare --output "$PACKAGE_DIRECTORY"
```

Review its manifest, rendered template, parameters, permission details, runtime code
and digests. Resolve every required input. Preparation is not evidence that the engine,
account, zone or permissions are available in AWS.

After owner approval to create a CloudFormation change set:

```sh
maven-memory install plan --package "$PACKAGE_DIRECTORY" \
  --review "$PRIVATE_REVIEW_FILE" --type CREATE
```

This command contacts AWS and creates a change set; it does **not** execute it. Review
the exact resource changes, capabilities, role trust, target, parameters, costs and
returned `review_sha256`. After separate explicit execution approval:

```sh
maven-memory install apply --package "$PACKAGE_DIRECTORY" \
  --review "$PRIVATE_REVIEW_FILE" --confirm-review "$REVIEW_SHA256"
```

Apply rejects changed review/package content, executes the exact reviewed change set,
reconciles its result and writes confirmed connection outputs to the private `.env`.
Do not manually invent output ARNs. The `--budget` options on plan/apply are bounded
**wait durations in seconds**, not monetary budgets. Preserve `.pending` and review
receipts after timeouts; an unknown outcome is not permission to create another stack.

For an existing approved stack, `maven-memory install connect --confirm-instance
"$INSTANCE_ID"` validates its ready outputs before connecting. Updates use `--type
UPDATE` and retain the existing instance/recovery identifiers. Reinspect every update;
old approval does not cover changed artifacts.

After approval to initialize the selected database/schema:

```sh
maven-memory install init --confirm-instance "$INSTANCE_ID"
```

The caller binding is established by the reviewed IAM principal and role trust;
initialization establishes the service schema/state, not a new arbitrary identity.
Use `install migrate --confirm-instance "$INSTANCE_ID"` only for a separately reviewed
migration to an existing deployment. Initialization does not activate starter authority.

### Review and activate a complete starter authority

Starter preparation is offline and creates new proposal/page UUIDs, bodies and a
manifest with dependency-safe `activation_order`:

```sh
maven-memory starter prepare --output "$STARTER_DIRECTORY" \
  --source-origin "$OWNER_SOURCE" --source-excerpt "$OWNER_REQUEST"
```

Supply genuine owner source evidence, not the literal example variable names. Read
all proposed bodies. The catalog contains the core directive and memory, setup,
connection, skill-loading, workflow, secrets, workspace and optional Google skills.
They are proposals only; even the generic root directive requires owner activation.
Google authority does not enable Google access or make it a core prerequisite.

For **each selected page in dependency order**, review its complete generated JSON
and body, then submit the proposal and obtain the exact owner-approval challenge:

```sh
maven-memory instruction draft "$STARTER_DIRECTORY/$SLUG.json"
maven-memory instruction challenge --proposal-id "$PROPOSAL_ID"
```

Use the actual proposal ID from the manifest/response. Save the complete challenge JSON
in an owner-private file (mode `0600`) without editing its identity, hash or revision.
The challenge requires the separate approval role and expires after ten minutes.
After the owner reviews the exact candidate and supplies approval evidence:

```sh
maven-memory starter approval --draft "$STARTER_DIRECTORY/$SLUG.json" \
  --challenge "$CHALLENGE_FILE" --output "$APPROVAL_FILE" \
  --approval-origin "$OWNER_APPROVAL_SOURCE" --approval-excerpt "$OWNER_APPROVAL"
maven-memory instruction activate "$APPROVAL_FILE" --confirm-candidate "$CANDIDATE_HASH"
maven-memory instruction read --slug "$SLUG"
```

The builder validates the matching challenge/draft, current candidate hash, revision
and expiry; it neither submits nor activates. Use the candidate hash from the actual
challenge. Never manufacture approval text, reuse an expired challenge or automatically
approve the entire catalog. Activate every selected page's dependencies first. If a
write outcome is unknown, reconcile the exact request key with `maven-memory lookup
--request-key "$REQUEST_KEY"` (add `--approval` for activation) before repeating it.

### Select the project manually

Use `maven-project setup --root "$PROJECT_ROOT"` for interactive project onboarding.
For an agent that has obtained the owner choices, the shared project controller safely
generates identities and preserves an existing selection. `maven-project init`
remains a low-level explicit-ID API, not the ordinary first-use path. Manual starts
also require an explicit root and complete authority; retain the returned MAVEN
session UUID, distinct from any optional harness identity.

Review [Security and privacy](security.md) before sharing retrieved context with an
external model. Configure [optional integrations](integrations.md) only when needed;
neither Google nor a secrets-file bucket is required to start core memory work.

## Existing-instance upgrade

Local code/templates do not update live AWS resources, schema or active authority by
themselves. The guided update applies them as one resumable setup operation in mode
`update` (`maven-setup --mode update`, **Update MAVEN** in `python3 install.py`, the
`maven-update` skill, or `maven_admin` begin kind `setup` with `{"mode": "update"}`):

| Stage | What it compares | What it does when different |
| --- | --- | --- |
| Local | Installed package files (`RECORD`) and installer-owned harness skills vs the checkout | Hands off the installer command; restart harnesses, then resume. Edited or shared skills are preserved |
| Schema | `maven.schema_migrations` vs the bundled ordered migrations (admin role, read-only) | Separately reviewed forward migration; a non-prefix history is reported, not bypassed |
| Service | Deployed template vs the current template built from the live stack's parameters (maintenance access) | Reviewed `UPDATE` change set; removal/replacement of RDS, Secrets Manager, S3 or KMS resources is refused before review; reconnects from the updated outputs |
| Instructions | Active page content hashes vs the bundled catalog | Optional; one collection approval proposes and activates new and changed pages at their current revisions |

Each stage that is already current passes without a question. A change set is created
only to review it; execution needs approval. First inspect pending publication, setup
and removal receipts. Resolve incompatible operations through their original supported
recovery implementation; never reinterpret an old publication checkpoint as a new
memory checkpoint or discard unknown outcomes. Historical record provenance remains
intact. Reload active authority after instruction changes. Schema approval is not
policy approval; installing templates is neither.

The new contract is schema 2 / client protocol 3 with `012_agentic_memory.sql`.
Ordinary schema-1 use is rejected. Both `maven-memory migrate --confirm-instance
"$INSTANCE_ID"` and `maven-memory install migrate --confirm-instance "$INSTANCE_ID"`
accept a schema-1 binding solely for the separately approved administrator migration.
They do not rewrite it to claim runtime readiness. SQL migration alone is not service
deployment: deploy the matching reviewed service and reconnect from actual upgraded
stack outputs.
See [architecture](architecture.md#rollout-and-authority) for version/cutover boundaries.

## Uninstall and retirement

In conversation, ask **“Plan removal of this project's MAVEN integration only”**,
**“Remove this local installation”**, or **“Retire this cloud instance.”** The shared
controller presents persisted full reviews and choices, bound to interaction revisions
and exact targets. Inspect is read-only; apply/resume can change external state.
Approve each selected scope separately. Login, secure input, stopped-harness attestation
and self-removal can require a protected terminal/browser continuation. The process
being removed must not replace the pinned recovery worker with in-process deletion.

Run `python3 install.py` and choose **Uninstall** from either the first-time or
existing-installation menu. Local discovery checks `~/.maven`, `MAVEN_HOME`, the
installer-selected `--home`, and `~/.maven-installations/installations.json`.
It does not recursively search the disk or contact AWS. Listed paths are candidates;
ownership and exact inventories are checked separately before removal.

For a custom location, choose **Advanced — browse or enter an installation path**.
Browse folders (including hidden directories), move to a parent, or enter an exact
path. Files, symlink paths and filesystem root cannot be selected as removal homes.
Back returns without changes. Confirm the chosen home to open the existing protected
removal dashboard; this does not install/update MAVEN or approve any removal scope.
New plans prefill that home. Existing recovery plans retain their original targets.

The standalone `maven-uninstall` and `python3 -m tools.uninstall` commands still open
the same dashboard with installed `[ui]` dependencies. Choose a listed operation or
open a custom `plan.json`; start a new plan to select independent scopes and review
their exact inventories. Dashboard discovery and inspection read local receipts,
not live AWS state.

The protected uninstaller stages existing dependencies offline; it does not download
packages. The unified installer's initial UI bootstrap may separately request consent
to download missing UI dependencies before task selection. When available, the selected
installation's Python supplies its existing dependencies for the protected dashboard.
Otherwise the current UI interpreter is used. Cloud operations still require AWS
packages and explicitly selected logins. Noninteractive `plan`, `apply`, `resume`,
and `status` remain dependency-free for local removal with Python 3.11+.

### Select independent scopes

`plan` inspects targets and writes private review/recovery files; it does not remove
the selected targets. All paths and identifiers below mean your actual selections.

| Selection passed to `maven-uninstall plan` | Effect |
| --- | --- |
| `--project "$PROJECT_ROOT"` | Detach that checkout; retain cloud memory |
| `--machine --home "$MAVEN_HOME"` | Remove attributable tools/integration, then delete that whole installation folder (settings, sign-in caches, setup and session records); leave project bindings and cloud resources |
| `--cloud --home "$MAVEN_HOME"` | Retire the configured cloud instance; leave local installations and project files |

Combine selectors to request more than one scope. Repeat `--project` or machine
`--home` to select several targets. `--project-home` and `--cloud-home` explicitly
choose different installations for their cloud operations; otherwise the first
selected home is used. Overlapping installation homes require separate plans.
Machine `--all-homes` covers current-user homes discovered from `~/.maven`,
`MAVEN_HOME`, explicit paths, and the private installer registry
`~/.maven-installations/installations.json`. It is not a filesystem-wide search or
permission to remove another OS user's installation.

For example, prepare local removal without contacting AWS:

```sh
maven-uninstall plan --project "$PROJECT_ROOT" --machine --home "$MAVEN_HOME" \
  --offline --output "$PRIVATE_OPERATION_DIRECTORY"
```

The operation directory must be outside selected project/installation roots and
export destinations. The default is `~/.maven-uninstall/<UUID>`. It contains
`plan.json`, `journal.json`, and a hash-bound `runner/` copy of the implementation.
Keep the whole directory private and intact; it is needed after installed tools
have been removed. Review the entire plan, its targets, inventories, warnings,
retained artifacts, and `plan_digest`.

The interactive launcher stages a disposable private UI outside installation homes,
so the screen survives removal of its original environment. Worker execution uses
the exact hash-bound runner and offline copies of installed dependencies in
`ui-runtime/` beside the plan. Those worker copies remain for recovery; the disposable
launcher copy is removed after the TUI closes. No AWS credentials are copied into
either dependency snapshot. Keep the entire operation directory private.

### Approve, inspect, and resume

In the TUI, review the complete plan, then confirm each selected destructive target
using only its first six characters (the full identity if shorter). The full target
is shown, and the prefix never selects a target or substitutes for backend identity
checks. Purge and administrative abandonment retain separate confirmations.
Changed plans invalidate all collected approvals. The TUI still asks whether affected
writers stopped; running assistant harnesses only produce a warning, because a harness
that saves its own configuration can write a removed MAVEN entry back.

Registered local sessions with no in-flight writers do not block planning: the plan
warns, and the barrier detaches their registrations and retains the exact entries as
private evidence. A session with in-flight writers still blocks; recover it, or use
`--offline` after confirming those writers stopped.

Ctrl+Q/Ctrl+C stops dispatching new actions at the next saved action boundary, then
closes the UI without a persistent background worker. In-flight remote operations
may continue; resume reconciles the same plan. Do not delete its journal or start a
replacement operation to bypass an uncertain outcome.

Existing recovery snapshots created before the TUI worker protocol remain immutable:
the dashboard can inspect them and shows their original CLI recovery command rather
than replacing the approved implementation.

For the CLI, stop affected local writers and close affected assistant harnesses before
applying. These flags are truthful attestations, not commands that stop processes:

```sh
maven-uninstall apply --plan "$PRIVATE_OPERATION_DIRECTORY/plan.json" \
  --confirm "$PLAN_DIGEST" --writers-quiescent --harnesses-stopped

maven-uninstall status --plan "$PRIVATE_OPERATION_DIRECTORY/plan.json"

maven-uninstall resume --plan "$PRIVATE_OPERATION_DIRECTORY/plan.json" \
  --confirm "$PLAN_DIGEST" --writers-quiescent --harnesses-stopped
```

Use the exact digest from the reviewed plan. Purge additionally requires
`--confirm-purge "$INSTANCE_ID"`; administrative abandonment additionally requires
`--confirm-abandon "$INSTANCE_ID"`. Repeat those confirmations on resume.
Changed identities/content require review, not an edited plan or deleted journal.

`status` reads local receipts without contacting AWS and reports each selected
scope separately. A completed scope is not a claim that retained artifacts are
gone. Exit 2/`blocked` may mean an external effect occurred or deletion is pending;
inspect the recorded action before resuming. Exit 3 indicates a saved worker pause;
exit 130 indicates an interactive pause/interruption. There is no automatic rollback.

The returned `recovery_command` uses a verified Python interpreter outside selected
installation homes. Use that command after self-removal; to resume, replace its
`status` arguments with the approved `resume` arguments above. The recovery runner
itself remains dependency-free for local cleanup. CLI-only cloud recovery needs the
cloud-capable interpreter, configuration and selected login until cloud actions
finish; the TUI also retains its verified dependency snapshot. Combined plans perform
cloud work before removing installed tools.

### Project memory and local originals

Ask **“Archive cloud project …”** or **“Reactivate cloud project …”** in your harness.
The assistant resolves the exact project ID and uses `maven_admin` kind
`project_lifecycle`, with `action` (`archive` or `reactivate`) and `project_id`.
No local folder, temporary project binding or uninstall is required.

The review identifies the AWS account, region, instance, administration identity,
project name/ID, current state and all-machines impact. Actual stopped-writer
confirmation and exact approval are required; active sessions, live claims and
unresolved operations block the transition. Original receipts survive interruption.
No folder, local binding, Home selection or cloud resource is removed.

Archival preserves history but hides the project and its contents from normal
discovery, reads and Home's project list, and blocks new project work. Ask explicitly
to consult archived history; reactivation is a separate reviewed action. Neither
action restores forgotten/erased content or erases already-loaded conversations.

Existing installations need **Update MAVEN**, including approved database migration
`017_project_archive_visibility.sql` and service/validator deployment. Direct archive
refuses to proceed without the migration; it never migrates implicitly.

The existing uninstall options `--archive-project` and `--reactivate-project` remain
available for their separately reviewed removal workflows and preserved plans.
Reactivation restores cloud eligibility, not local attachment; select the project
again through project setup when needed.

Project removal preserves originals, receipts, and ignore protection by default.
Machine removal deletes the whole installation folder as its final step, after
tools, caches and integrations are removed. The folder is kept, with a warning,
only when no installer ownership receipt proves it is MAVEN's, it is your home
directory, it contains the source checkout or a registered project, another
installation may use a sign-in cache inside it, or it holds a `.git`, mounted or
foreign-owned entry. Anything added or changed after review stops the deletion.
Without `--export`, nothing is archived; the TUI asks before continuing.
`--export "$PRIVATE_EXPORT_DIRECTORY"` produces verified private ZIP archives first,
moving resolved originals/settings only after verification. Archives contain
`inventory.json` and `data/` entries; sign-in caches, locks and detached session
records are not archived. Project recovery evidence and a minimal detached project
binding remain in place; the report distinguishes copies from moves.
Credential caches are not included in recovery exports. Do not remove ignore rules
while protected `.maven` artifacts remain.

`--offline` permits local detachment when cloud access or packages are unavailable.
After actual local writers stop, it archives exact selected local session/lease
evidence and invalidates those local registrations. It does **not** close cloud
sessions, archive cloud projects, or recover unsaved work. Offline mode cannot
perform cloud removal, archival, or reactivation.

### Machine ownership and legacy installations

New installs record artifact ownership in `<home>/installation.json`; project
integration ownership is separate from the existing project configuration schema.
Uninstall preserves modified, shared, or uncertain artifacts and never deletes a
source checkout. Dedicated installer-owned local caches can be removed only when
no unselected installation is known to need them. External/shared caches and
general AWS configuration remain; local deletion does not claim provider token
revocation.

Older installations without ownership receipts are preserved unless you explicitly
add `--adopt-legacy` to a machine plan. Adoption verifies a dedicated, non-editable
MAVEN virtual environment and inventories its exact contents for approval. It does
not claim ownership of arbitrary old skills or unrelated directories.

Completed machine removal marks the installation inactive and removes its discovery
registration while retaining recovery evidence. Explicit reinstallation can clear
only a matching completed-removal barrier; pending removal must be reconciled first.

### Cloud retirement, purge, and retained charges

Cloud planning verifies the actual account, region, instance, stack, resources, and
selected administrative identities. Use a separately selected deployment login
whose permissions survive stack deletion, plus the configured database-admin role
for the write barrier. Permissions are not granted automatically. Missing access
blocks the operation; no default/root or administrator fallback is used.

Default `--cloud-mode retire` establishes persistent maintenance, drains service
writes, creates and checks a final database snapshot, and preserves required
versioned resources, secrets, encryption dependencies, deployed runtime code, and
recovery metadata before removing running infrastructure. The recovery manifest is
saved locally and in the recovery bucket. Snapshot availability and manifest/object
verification are **not** proof of a successful restore.

`--cloud-mode purge` requires the separate instance confirmation and removes only
reviewed instance resources. It handles retained infrastructure, deletion
protection, object versions, and delete markers rather than assuming stack deletion
is sufficient. Unselected backups and encryption keys remain. Secrets Manager
deletion uses a seven-day recovery window and remains pending until absence is
confirmed; it is not reported as immediate erasure.

Clean session closure is the default. `--abandon-sessions` is a separate reviewed
choice for unreachable clients. It preserves unfinished session/request evidence,
blocks new cloud writes across machines, and makes no normal-closure or unsaved-work
recovery claim. Bootstrap/migrate cannot silently clear this retirement barrier.

Optional resources outside the stack are preserved unless individually selected:

- `--optional-resource s3:BUCKET`
- `--optional-resource ssm:/absolute/prefix/`
- `--optional-resource secret:SECRET_ARN`
- `--retained-resource ARN` for an exact retained S3 bucket, cluster snapshot,
  automated-backup set, or supported retired secret.

Dedicated external resources need exact instance/exclusive ownership evidence
(`MavenInstanceId`, `MavenExclusive=true`, `DataScope=owner_memory`) and dependency
checks. Tags are not permission to misclassify shared storage. Uncertain resources
stay protected. Retirement records ownership for its proven retained resources so
a later individually reviewed purge can identify them after the stack is gone.
Wrapping keys cannot be selected for removal while retained encrypted-file storage
still needs them. Removing stored Google credentials does not delete Google content,
accounts, or prove grant revocation.

Review all retained resources and charges. Retention locks, permission failures,
concurrent changes, and asynchronous provider deletion may leave work pending.
Preserve the original operation directory and resume; do not treat a timeout as
permission to start a replacement operation.

### Verification limits

Verification stays local: focused temporary-file checks, simulated provider/package
boundaries, headless Textual interactions and real-terminal smoke checks. No cloud
resources or separate testing environment are required. Local removal/recovery checks
are not proof of live IAM permissions, PostgreSQL concurrency, AWS teardown or restoration.
