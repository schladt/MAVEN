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

The full-screen Textual interface starts with your goal: **connect existing memory**
or **create a new instance**. An existing installation shows its saved, unverified
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

**Existing instance:** choose one of these connection methods:

1. **Import settings from another installation’s `.env` file**, usually
   `~/.maven/.env`. This imports private connection settings, not credentials.
2. **Find the existing deployment in AWS**, using its CloudFormation stack—the
   named group of resources for that instance—and a separately selected identity
   permitted to read its details.

The wizard derives locators from the imported file or actual stack outputs and asks
you to confirm instance and principal identities. Neither method redeploys,
initializes an existing database, or activates replacement authority. Existing
authority must already be available; missing authority is a blocker, not permission
to reset it.

**New instance:** sign in and confirm the discovered account/principal, then provide
the stack/instance name, notification email and two availability zones. Select
daily-use and maintenance access explicitly. Review the limitations and costs below.
The wizard displays the generated package and complete change-set review, asks
permission to create a change set, and binds execution to the exact review digest.
Database initialization and each starter authority proposal/activation have separate
reviews. Changed candidates require fresh approval. Routine terminal approvals use
buttons and record the actual selection; destructive confirmations remain separate.
Read the complete artifacts and candidate bodies before approving.
Declining any stage leaves setup pending, not complete.

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
OMP, Claude Code, Codex, Copilot or Gemini. Review exact local MCP, startup-guidance
and skill changes, then confirm affected harnesses are stopped before any writes.
Skip leaves their files unchanged. Conflicting or modified files are preserved;
partial completed writes remain recorded in selective-uninstall ownership receipts.

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

`maven-setup` opens the same setup screens and reads existing receipts. Guided
installation continues directly into cloud setup; if paused, activate that home and
run `maven-setup` when ready. `--mode existing|new` selects the task without asking
again. After verified completion, a separately reviewed check/repair can start a
new operation; `--fresh-operation` never discards an unfinished or unverified one.

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
preparation artifacts are isolated. Unresolved, cancelled, or unverifiable previous
operations must be reconciled through their original identities, not replaced.
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

Additional projects only need their own explicit enablement, not another instance
deployment. A new/forked session gets its own identity; explicit resume preserves the
original. Cwd changes, transport reconnection and newest receipts never select a new
project/session implicitly. Missing access blocks dependent work, not unrelated chat.

### Installer options and harnesses

`python3 install.py --local-only` installs local tools without invoking cloud setup.
Use it for a package-only installation; downloads still require network access or an
appropriate package cache, and it does not establish cloud readiness. `--home PATH`
chooses another private home, `--google` installs
optional client libraries without granting Google account access, and `--harness NAME`
selects a harness (repeat for several). `--skill-dir PATH` explicitly selects a custom
destination. Close affected harnesses before confirming skill installation; a
noninteractive `--local-only` install also requires `--harness-stopped` when selecting
harness files. Conflicting files are preserved; interactive setup can continue without
changing them. Start a new harness afterward to discover the generic skills.

The installed generic skills are `maven` (intent front door) and `maven-maintenance`
(bounded semantic worker). Native startup guidance and an owned MCP configuration
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

Local code/templates do not update live AWS resources, schema or active authority.
First inspect pending publication, setup and removal receipts. Resolve incompatible
operations through their original supported recovery implementation; never reinterpret
an old publication checkpoint as a new memory checkpoint or discard unknown outcomes.

Separately review the matching service/schema upgrade and forward migration before
using changed memory contracts. An incompatible deployed schema must be reported,
not bypassed. Historical record provenance remains intact. After rollout, separately
review proposed instruction bodies and dependencies, request exact activation
challenges, and activate only with real owner approval. Reload active authority.
Schema approval is not policy approval; installing templates is neither.

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

The installed `maven-uninstall` opens a full-screen operation dashboard. From this
checkout, `python3 -m tools.uninstall` opens the same interface when run with the
installed `[ui]` dependencies. Dashboard discovery and inspection use local receipts,
not live AWS queries. Choose a listed operation or open a custom `plan.json` path;
start a new plan to select independent scopes and review their exact inventories.

The TUI never downloads dependencies during uninstall. Activate the installed
environment first. For an older manual installation, install this checkout's `[ui]`
extra into that existing virtual environment before using the TUI. The noninteractive
`plan`, `apply`, `resume`, and `status` interfaces remain dependency-free for local
removal with Python 3.11+; cloud operations still need AWS packages and selected logins.

### Select independent scopes

`plan` inspects targets and writes private review/recovery files; it does not remove
the selected targets. All paths and identifiers below mean your actual selections.

| Selection passed to `maven-uninstall plan` | Effect |
| --- | --- |
| `--project "$PROJECT_ROOT"` | Detach that checkout; retain cloud memory |
| `--machine --home "$MAVEN_HOME"` | Remove attributable tools/integration for that installation; leave project bindings and cloud resources |
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
Changed plans invalidate all collected approvals.

Ctrl+Q/Ctrl+C stops dispatching new actions at the next saved action boundary, then
closes the UI without a persistent background worker. In-flight remote operations
may continue; resume reconciles the same plan. Do not delete its journal or start a
replacement operation to bypass an uncertain outcome.

Existing recovery snapshots created before the TUI worker protocol remain immutable:
the dashboard can inspect them and shows their original CLI recovery command rather
than replacing the approved implementation.

Stop affected local writers and close affected assistant harnesses before applying.
These flags are truthful attestations, not commands that stop processes:

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

`--archive-project` explicitly archives the cloud project, affecting **all**
checkouts using that project, not only the selected folder. History stays readable;
new project work is blocked. Clean closure/reconciliation is required for archival.
Archive/reactivate requires explicit migration using current tooling:

```sh
maven-memory install migrate --confirm-instance "$INSTANCE_ID"
maven-uninstall plan --reactivate-project "$PROJECT_ROOT" \
  --home "$MAVEN_HOME" --output "$PRIVATE_OPERATION_DIRECTORY"
```

Review migration separately before running it; uninstall never migrates implicitly.
Reactivation uses the current binding or one unambiguous, digest-verified preserved
`detached-project-*.json` binding. It restores cloud eligibility, not local attachment;
select the project again through `maven-project setup` when ready.

Local removal preserves originals, receipts, and ignore protection by default.
`--export "$PRIVATE_EXPORT_DIRECTORY"` produces verified private ZIP archives,
moving resolved originals/settings only after verification. Archives contain
`inventory.json` and `data/` entries. Unresolved recovery evidence and a minimal
detached project binding remain in place; the report distinguishes copies from moves.
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
