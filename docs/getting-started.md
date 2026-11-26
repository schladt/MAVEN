# Getting started

Install/connect the selected instance, then enable the intended folder. The installer creates the private installation home; it never automatically registers its source checkout. See the [prerequisites](../README.md#before-you-start). Once a folder is enabled, reading its brief and saving local work do not require AWS to be online.

## Guided first-time setup

From the cloned or extracted source folder:

```sh
python3 install.py
```

The full-screen Textual interface starts with a short menu. Without an installation it
offers **Install** (connect existing memory, create a new instance, or software only)
and **Remove**. An existing installation shows its saved, unverified
instance/account/region/profiles and offers **Install** (a separate instance or
software only, in an unused private home), **Update** (everything, or local software
only), **Remove**, and **Settings and repair** (connection check/repair, secrets,
deployment access, MAVEN Home, harnesses, certificate and staging settings), plus
**Resume unfinished setup** when one is pending. Check/repair does not reinstall packages
or automatically redeploy. The private installation home defaults to `~/.maven`.

Python 3.11+ with `venv` is required. If UI dependencies are missing, a brief initial
prompt requests consent to download them into a temporary private runtime; system
Python is never modified.

**Optional secrets-store selection**

Ordinary project memory does not depend on a secrets store or Git configuration. When secrets are wanted, import reviewed locators, enter existing settings, or approve the exact private S3 bucket and generated SSM wrapping key.
Prefixes are namespaces, not resources to discover or adopt by name. The review pins
identity, resources and effects; plaintext never belongs in reviews, journals or chat.

Read-only verification checks the selected resources/access; it does **not** prove
write permissions. Resume retains the original create operation and verifies its
outcome, never regenerating an existing wrapping key. Keep keys separately protected
from ciphertext. The store can be configured before selecting MAVEN Home; Home is
required only for the secret catalog.
SSM metadata listing requires `ssm:DescribeParameters` on `Resource: "*"`, because
AWS cannot scope that action to a prefix. The proposed daily-use policy discloses
this account-wide metadata permission; MAVEN filters its requests to the configured
store. Value access and writes remain scoped to the exact store, and no policy is
attached automatically.


Check/Repair supports the same integration-only import and exact review. Updates
preserve existing integration settings and keys. Missing settings require explicit
repair, not silent replacement; start-over separately reviews retain/rebind choices.
See [secret usage](integrations.md#aws-secrets-and-encrypted-files) for metadata-only
discovery, private-file store-and-catalog and approved declarative consumers.

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

1. Verify the daily-use login, then select deployment access for discovery.
   Confirm the source and the actual same-account deployment identity.
2. MAVEN searches CloudFormation in the selected region, reading all result pages.
   Choose the intended stack explicitly, even if only one was found. The list shows
   stack names, instance IDs and status; only ready stacks with valid MAVEN outputs
   from the selected account are offered.
3. Review the complete connection before binding memory.

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

The wizard derives locators from the imported file or actual stack outputs and asks you to confirm the instance and principal identities. Connecting to an existing instance is not permission to redeploy, initialize a database or change policy. A missing reconcile capability requires a separately reviewed update.

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

Verify the selected instance through its signed service. The local project workflow does not require activating cloud instructions. Installing a skill, approving a database migration or seeing a ready stack grants no authority to execute external actions.

Current-step context is inside the bordered panel; its overflow is scrollable in
small terminals. A bottom status bar distinguishes **Waiting…**, **Awaiting input**,
pause requested, paused, failed and complete. Declining an approval preserves
the operation rather than claiming completion or rollback.

**Daily-use access** operates memory. **Deployment access** manages infrastructure
or reads stack metadata. Choose an existing, same-account IAM role assumed from
the verified daily login, a separate login profile, or configure deployment access
later when the selected work does not need it. Existing installations retain
their separate profile until an explicit change. Changing deployment access
does not create IAM roles, change policies, switch projects or alter the three
MAVEN memory roles.

Use a named `aws login` profile, not root or static access keys. A profile is a local
label, not an access grant. Saved/imported account and principal details are labelled
as expected, not verified. On a first install they may be unknown until sign-in;
setup then explicitly confirms the actual STS account and principal before changes.
An identity differing from saved bindings is blocked, not silently substituted.
On a new machine, enter the profile name you want to create. You do not need an
existing AWS profile or a MAVEN `.env` from another machine. Choose **Create profile
and sign in** with a browser on this machine or **on another device (SSH/headless)**.
The latter runs `aws login --remote` in the installer terminal: open the AWS link
on your browser device, then paste the code back into that terminal, not into chat.
The installer shows both exact commands and only launches the one you select.
A failed login offers manual recovery, not a readiness claim.

MAVEN keeps the daily AWS login in its selected private cache. A separate deployment
profile has its own private cache; role mode instead assumes the exact deployment
role from the daily identity, requesting 900-second credentials held in process.
The IAM trust policy, not MAVEN, determines whether fresh MFA is required. Because
`aws login` otherwise reuses the browser's existing console session (which carries no
fresh MFA), every role-mode elevation sign-in MAVEN shows or runs forces a new console
sign-in: it sets `BROWSER` to a private/incognito window of the default browser
(Chrome, Chromium, Brave, Edge or Firefox on Linux; Chrome, Chromium, Brave or Edge on
macOS). Otherwise, including over SSH, it uses `aws login --remote`; open that URL in a
private window. A denied elevation (`deployment_elevation_denied`) always returns this
sign-in handoff.
A plain `aws login --profile …` writes to the AWS default cache, not this installation's.
If a user-controlled login terminal is unavailable, run the exact manual command
returned for the selected profile, cache and region; for example:

```sh
AWS_LOGIN_CACHE_DIRECTORY=~/.maven/runtime/aws-login-cache aws login --profile maven-interactive --region us-east-1
```

Permission failures, network failures and an unfinished local-software handoff are
not sign-in requests. Their screens retain the original operation's resume command.

Read-only access preflights distinguish **Passed**, **Missing access**, and
**Could not verify**. Missing access blocks the affected phase with remediation and
retry of the same operation. Unknown checks remain disclosed in reviews, not relabelled
as success. IAM simulation covers representative actions, not every resource
condition, organization control, quota or dependency; role assumption and reads
cannot prove write permissions. No test resources are created by these probes.

Importing `.env` does not copy credentials or reuse another machine's cache paths.
For a separate deployment profile choose a different owner-private cache. To change
an existing installation, select **Settings and repair → Change deployment access** or run
`maven-setup --home "$HOME/.maven" --deployment-access` for the same reviewed flow.
The live preflight verifies the identity and reads CloudFormation; it does not
create test resources or prove write permissions.
See [AWS command-line sign-in](https://docs.aws.amazon.com/signin/latest/userguide/command-line-sign-in.html)
for account/identity prerequisites and login permissions.

For interactive AWS sign-in, the TUI temporarily restores the normal terminal and
resumes afterward. Authentication output is not captured into the progress log.
The actual returned principal is verified before any subsequent cloud action.

### Optional harness connection and project enablement

After verified cloud access, optionally configure OMP, Claude Code, Codex, Copilot CLI or Gemini CLI. Approve the exact local file changes and
acknowledge the risk of changing running harness configuration; this is not a claim
that writers stopped. Actual writer quiescence is checked separately where required.
Identical integrations need no rewrite. Conflicting targets are preserved without
blocking independent safe targets; receipts retain each completed write.
If an older install left `venv/bin/maven-mcp` group-writable (`0775`), this same
review can repair that exact installer-owned executable to `0755` and update only
its ownership entry. Already manually-corrected launchers can have their stale
ownership entry reconciled. Modified, unowned, linked or otherwise unsafe targets
remain untouched. The conflict screen offers local retry without cloud setup.
New installs use a safe package-install umask and record safe launcher permissions.

Completion distinguishes **cloud verified**, **harness files written/skipped/conflict**,
**restart needed**, and **harness MCP connection not yet verified**.
Open the actual project folder in the selected harness and ask **“Enable MAVEN for this project.”** Review the root, instance and cloud project ID before approval.
The installer never chooses a project folder or binds its source checkout.

Project enablement can select an existing cloud project or create a named one with
genuine source evidence. Memory needs no Git configuration; optional Git assistance
is separate. Project selection does not commit, push, pull or switch branches.

New bindings use `project.json` schema 2 to pin the signed service's instance ID and recovery epoch with the project ID. To change destinations, or if the folder has an older schema-1 binding, ask **“Switch this folder to the existing project …”** and review the full old/new identities, preservation path and any Home change. Use the reviewed switch even if the destination project ID is unchanged. Never update the schema number by hand.

For a Home switch, the replacement review lists other tagged Home projects across all discovery pages. If discovery cannot finish or a candidate changes while it is being read, the switch does not proceed.

Switch preserves the old `.maven` directory, including unsynced local knowledge, at the reviewed private sibling path. Resolve outstanding external operations first. If interrupted, resume the same administrative operation. No cloud project is merged or erased. Other machines' selections do not change.

To rename an existing cloud project, review its exact ID and old/new name. Renaming changes the label for other machines too; it is not a new project or a local folder rename. Creating a cloud project without rebinding the current folder is likewise separate from switching that folder to it. Resume the original creation or rename operation after a lost reply; verify the actual cloud entity before reporting completion.

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

Completed verified setup can continue local harness configuration without repeating cloud approvals if its recorded settings still match. To request a fresh connection check after completion choose **Settings and repair → Check or repair the connection**. Do not discard an unfinished operation or infer current AWS availability from a saved readiness receipt.

To abandon the current unfinished setup, rerun `python3 install.py` and choose
**Settings and repair → Start over**. Review the exact operation and type its
displayed confirmation prefix. Declining leaves it unchanged. Approved abandonment:

- Preserves the original operation, setup state and private `.env` contents in
  `runtime/administration/restart-<original-operation-UUID>.json`.
- Leaves original request artifacts, approvals, receipts, installed software, AWS
  login caches and cloud resources intact. The old operation becomes read-only.
- Retains the exact `.env` bytes and selectors as **unverified**, starting a distinct
  setup with fresh approvals and an isolated artifact directory. It does not silently
  switch identity, delete the connection or require AWS access just to restart locally.

Setup locks prevent abandoning an actively running setup. If local tools were removed, use the reviewed software-only restoration. An interrupted restart resumes its journaled replacement operation without repeating completed work.

**Abandonment is not cloud rollback:** submitted AWS work may continue and resources
may still incur charges. Unknown effects remain unresolved. Reconnect to existing
resources or manage them separately; do not duplicate an unresolved deployment.
The recovery CLI is `maven-setup --home PATH --restart-operation UUID`, using the
actual home and original operation ID. Unlike `--fresh-operation`, this is an explicit
reviewed abandonment; it cannot be combined with `--mode` or `--operation`.

Instance readiness checks the signed reconcile capability. Project and Home selections require separate review. For an interrupted setup, binding change, or external handoff, resume its original operation. The assistant presents the complete review and records your actual response against its identity and revision; recording approval is not execution. A changed target or artifact requires a fresh review.

### First use and everyday work

The agent loads local preferences on first use, reuses them, and reports whether MAVEN changed on each enabled turn. Ask “Remember this decision” to save evidence locally or “Reconcile this project” to compare with the cloud. See the [project workflow](workflow.md) and [client capabilities](integrations.md#harness-capability-matrix).

### MAVEN Home

Home is an optional, per-machine selection for personal knowledge and cross-project preferences. Use the installer's **Set up or change MAVEN Home** or explicitly ask the assistant to configure it. To share Home, select the same cloud project ID on each machine; names alone do not establish identity. Outside tracked roots, local turn capture defaults to this configured Home. Missing or damaged configuration is reported, not repaired implicitly. Disabling the pointer is not deletion.

### Installer options and harnesses

`python3 install.py --local-only` installs local tools without invoking cloud setup.
Use it for a package-only installation; downloads still require network access or an
appropriate package cache, and it does not establish cloud readiness. `--home PATH`
chooses another private home, and `--harness NAME`
selects a harness (repeat for several). `--skill-dir PATH` explicitly selects a custom
destination. Prefer closing the affected harnesses before updating them. Only pass
`--harness-stopped` if they are actually stopped. Running clients keep their loaded
skills and can overwrite configuration when they save; restart them and verify
discovery after the update. Conflicting files are preserved and reported.
Start a new harness afterward to discover the `maven` skill.

The installer ships one [MAVEN skill](../maven_resources/templates/skills/maven/SKILL.md) with four local procedures: **maven project**, **maven reconcile**, **maven secrets** and **maven update**. Ask in plain language; these are not shell commands. Edited or shared copies are preserved. A client-launched stdio MCP entry and standing guidance complement the skill. Review conflicts rather than overwriting unrelated configuration. See the [integration matrix](integrations.md#harness-capability-matrix); hosted clients are not covered by local stdio installation.

To stage selected integrations without live writes, use `--stage-harnesses PATH`.
For custom profiles, pair `--harness-config NAME=ABSOLUTE_FILE` with
`--harness-context NAME=ABSOLUTE_FILE` and `--skill-dir PATH`. A headless `--local-only` change first prints the planned writes and review digest. Review, then repeat with `--approve-harness-review SHA256` using that digest. Restart affected clients to load the new guidance and tools.

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

## Cloud setup reference

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
- Deployment authentication: `MAVEN_DEPLOY_MODE` is `profile`, `role` or `deferred`.
  An absent mode retains existing profile behavior. Separate-profile mode uses
  `MAVEN_DEPLOY_PROFILE`, `MAVEN_DEPLOY_EXPECTED_PRINCIPAL`, and `MAVEN_DEPLOY_LOGIN_CACHE`.
  Role mode uses `MAVEN_DEPLOY_ROLE_ARN` and the verified daily source login;
  it does not try the old deployment profile if elevation is denied. Keep login
  caches owner-private (`0700`). Configure deployment access through reviewed setup.
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

## Existing-instance upgrade

Ask **“Update MAVEN”** or choose **Update MAVEN** in `python3 install.py`. The [update procedure](../maven_resources/templates/skills/maven/references/update.md) describes the resumable operation. Review the selected checkout and installation, run any local software handoff in a separate terminal, then restart the affected harness and resume the same operation. Installation updates preserve modified or shared skills rather than replacing them silently.

For cloud activation, review the exact ordered database migrations (including `025_project_reconciliation.sql`) and the matching service change set against the selected instance. Updates that remove or replace a database, secret, bucket or key are refused. Do not clear a maintenance barrier or mark an uncertain database operation complete; preserve its journal, inspect the outcome, and obtain fresh approval for changed SQL. Confirm the signed service reports the expected instance, recovery epoch and `project_reconcile_v1` capability before reporting cloud readiness. A package, local check or installed harness entry proves none of this.

This is a software and infrastructure update, **not a legacy memory import or purge**. Any one-time Home migration, verification, cleanup of legacy clients and destructive removal need separate exact-target approval; they are not part of ordinary reconcile. Secure grants and cloud policy activation remain independent. For the separately reviewed migration of a retired Google environment locator, see [integrations](integrations.md#google-removal-and-independent-integrations).

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
packages. CLI cloud recovery retains the existing cloud dependencies without requiring
Textual; local `plan`, `apply`, `resume` and `status` need only Python 3.11+.
The unified installer's UI bootstrap separately asks before downloading missing UI
dependencies. Cloud operations still require the explicitly selected login.

### Select independent scopes

`plan` inspects targets and writes private review/recovery files; it does not remove
the selected targets. All paths and identifiers below mean your actual selections.

| Selection passed to `maven-uninstall plan` | Effect |
| --- | --- |
| `--project "$PROJECT_ROOT"` | Detach that checkout; retain cloud memory |
| `--machine --home "$MAVEN_HOME"` | Remove attributable tools/integration, then the reviewed installation folder including private settings and caches; leave project folders and cloud resources |
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
Before registry detachment or home purge, recovery evidence is saved outside the
selected roots, even without an optional export. Configuration and unfinished
installation intents remain private; AWS login caches are never copied.

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
Changed plans invalidate approvals. Writer quiescence and running-harness configuration
risk are separate acknowledgements: a harness may later write removed entries back,
but accepting that risk is not an assertion that its writers stopped.

Ctrl+Q/Ctrl+C stops dispatching new actions at the next saved action boundary, then
closes the UI without a persistent background worker. In-flight remote operations
may continue; resume reconciles the same plan. Do not delete its journal or start a
replacement operation to bypass an uncertain outcome.

Existing recovery snapshots created before the TUI worker protocol remain immutable:
the dashboard can inspect them and shows their original CLI recovery command rather
than replacing the approved implementation.

For new plans, stop affected writers and acknowledge running-harness configuration
risk. Frozen older plans retain their original truthful `--harnesses-stopped` contract:

```sh
maven-uninstall apply --plan "$PRIVATE_OPERATION_DIRECTORY/plan.json" \
  --confirm "$PLAN_DIGEST" --writers-quiescent --harness-config-risk

maven-uninstall status --plan "$PRIVATE_OPERATION_DIRECTORY/plan.json"

maven-uninstall resume --plan "$PRIVATE_OPERATION_DIRECTORY/plan.json" \
  --confirm "$PLAN_DIGEST" --writers-quiescent --harness-config-risk
```

Use the exact digest from the reviewed plan. Purge additionally requires
`--confirm-purge "$INSTANCE_ID"`; administrative abandonment additionally requires
`--confirm-abandon "$INSTANCE_ID"`. Repeat those confirmations on resume.
Changed identities/content require review, not an edited plan or deleted journal.

`status` reads local receipts without contacting AWS and reports each scope separately.
Exit 2 means blocked; exit 3 means a saved worker pause; exit 4/`pending-provider`
means submitted provider work has not reached verified absence. Exit 130 means an
interactive pause. Local completion never implies cloud completion or rollback.

The `recovery_command` uses a verified Python outside selected homes. Preserve its
runner, dependency snapshot and selected configuration. A fresh selected login may
be needed after home removal; caches are not exported. Combined plans may finish
independent local actions while cloud deletion remains pending, but only after
external recovery prerequisites are preserved. Resume the original operation.

### Project memory and local originals

Detaching a project is not erasing a record or reconciling offline changes. Before removal, review the local project records and protected artifacts in the actual inventory. Preserve any needed unsynced knowledge privately; a cloud snapshot cannot contain work that never reached the cloud. Archiving or reactivating a cloud project, if separately selected in a removal plan, does not move or erase its local files.

Project removal preserves originals, receipts, and ignore protection by default.
Machine removal deletes the whole installation folder as its final step, after
tools, caches and integrations are removed. The folder is kept, with a warning,
only when no installer ownership receipt proves it is MAVEN's, it is your home
directory, it contains the source checkout or a registered project, another
installation may use a sign-in cache inside it, or it holds a `.git`, mounted or
foreign-owned entry. Anything added or changed after review stops the deletion.
Without `--export`, no optional originals archive is made; required recovery evidence is still preserved.
`--export "$PRIVATE_EXPORT_DIRECTORY"` produces verified private ZIP archives first,
moving resolved originals/settings only after verification. Archives contain
`inventory.json` and `data/` entries. Inspect the report to distinguish copies from moves and items not selected for export.
Credential caches are not included in recovery exports. Do not remove ignore rules
while protected `.maven` artifacts remain.

`--offline` permits local detachment after affected writers have stopped. It cannot remove cloud resources, archive or reactivate a cloud project, or verify that local work ever reached AWS.

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
Do not serve that snapshot by redeploying its archived Lambda. Restore into
isolation with no normal credentials or network access; retain the **original**
recovery bucket, including its independent erasure ledger. Use the matching
current maintenance tooling to replay and verify every erasure before binding
the restored physical cluster for normal use. If the original ledger is missing,
stale or unverifiable, leave the restore isolated. See [security](security.md).

`--cloud-mode purge` requires the separate instance confirmation and removes only
reviewed instance resources. It handles retained infrastructure, deletion
protection, object versions, and delete markers rather than assuming stack deletion
is sufficient. Unselected backups and encryption keys remain. Secrets Manager
deletion uses a seven-day recovery window and remains pending until absence is
confirmed; it is not reported as immediate erasure.
`--wait-seconds` is bounded from 0 to 300; a seven-day provider window remains
`pending-provider`, not a failed deletion or a reason to start another operation.

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

Temporary-file, simulated-provider and isolated database checks exercise different boundaries. None establishes that an installation has been updated, a cloud resource has been removed, a snapshot restores successfully, or all harness versions interoperate.
