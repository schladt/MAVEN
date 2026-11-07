# Getting started

Install/connect the cloud instance once, optionally defer project setup, then open
OMP in the intended project and invoke `/skill:maven-start-work`. First use guides
your choices in chat; later sessions reuse that project's selection and load cloud
memory. You do not need to generate new UUIDs or assemble lifecycle commands.

Start with the [README prerequisites](../README.md#before-you-start). A source
checkout does not supply a configured virtual environment, private `.env`, or your
project's `.maven` directory. The installer creates them in the selected locations;
you do not need to prepare them manually. Installing tools in a checkout does not
register that checkout as a project.

## Guided first-time setup

From the cloned or extracted source folder:

```sh
python3 install.py
```

The default is interactive: choose the private installation home (default
`~/.maven`), optional harness integrations and optional Google dependencies, then
follow the cloud/project wizard. Python 3.11+ with `venv` is required. The installer
checks Git and an AWS CLI with `aws login` support and gives installation guidance;
it never silently installs system packages. Git is unnecessary for a no-Git project
when you obtained the source through an archive.

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

**New instance:** provide the AWS account, stack/instance name, notification email,
two availability zones and explicit routine and deployment identities. Review the
limitations and costs below. The wizard prepares real artifacts, asks permission to
create a change set, requires the actual review digest before execution, and separately
asks approval for database initialization and exact starter authority candidates.
Read the complete generated artifacts and candidate bodies before approving them.
Declining any stage leaves setup pending, not complete.

Use a named `aws login` profile and confirm the actual STS principal; never use root
for routine access. A profile is a local label, not an access key. The wizard can
create/authenticate that selected CLI profile through normal interactive AWS sign-in
after your approval. Saved static-key profiles and implicit credential chains are
not accepted by guided setup. Metadata/deployment access is selected separately and
never becomes a silent fallback. Importing another installation's `.env` does not
copy credentials or reuse another machine's cache paths: choose a local private cache
folder (Enter accepts the created default). An existing approved local cache can be
selected explicitly. See [AWS command-line sign-in](https://docs.aws.amazon.com/signin/latest/userguide/command-line-sign-in.html)
for account/identity prerequisites and login permissions.

### Optional project setup

After verified instance access and complete approved authority, the wizard offers
an optional project step; you can defer it and configure the project in OMP later.
Instance readiness and project readiness are distinct. To set up now, select and
confirm the **project folder** separately. Filesystem `/` is not permitted; an
ambiguous/subdirectory root needs an explicit choice rather than silent rebinding.
The MAVEN source checkout is a development project only if you explicitly choose it.

Choose an existing cloud project or authorize a named new one with genuine source
evidence. Confirm the detected Git checkout root, remote, actual push URL and current
branch, or explicitly choose no-Git. Setup creates `.maven/project.json`, runtime
directories and ignore rules without committing, pushing, pulling or switching
branches. New project/request UUIDs are generated and durably journaled for you
before mutation. Private project state is mode `0700`/`0600` and ignored before
personal configuration is written.

### Resume instance setup

After activating the installed commands, resume interrupted or declined setup with:

```sh
maven-setup
```

For a separate home or a different suggested project folder:

```sh
maven-setup --home /absolute/path/to/private-home --project-root /absolute/path/to/project
```

Those paths must be your actual selected directories. `--project-root` is a prompt
default, not authorization to bind that folder. Keep the home, `runtime/setup` artifacts,
review files and pending request receipts intact. The wizard resumes the same requests;
there is no reset/redeploy flag. Unknown outcomes must be reconciled before retrying
mutations. Existing infrastructure must never be reinitialized to recover onboarding.

Instance completion requires verified routine cloud access and complete applicable
authority. A declined optional project step does not invent project readiness.
For an interrupted project operation, preserve the selected root's `.maven/runtime`
setup receipts and repeat its original `maven-project setup` choices. The command
reconciles the original IDs before an explicitly resumed mutation. Legacy
instance-wizard pending/saved project requests retain their original IDs and receipts;
do not discard them or create a second project to get past an uncertain response.

### First use and everyday work in OMP

Make the selected home's installed commands available to OMP, install/discover the
generic skills safely as described below, and open OMP in the actual project folder:

```text
/skill:maven-start-work
```

On first use, the assistant confirms the root and asks whether to select an existing
cloud project or create a named one, and whether to use the confirmed Git destination
or no-Git. It obtains real owner source evidence for creation, then uses the
noninteractive project setup interface with those choices. It does not guess answers
or ask you to invent new UUIDs. For a configured project it preserves and validates
the existing selection, reads complete cloud instructions/dependencies and project
handover/tasks/blockers, and starts this session. Missing authority blocks work;
having no previous handover is normal for a new project.

Each new OMP session has its own MAVEN session UUID, including parallel sessions in
one checkout. The assistant retains that root/project/MAVEN binding across cwd
changes. Repeating start in the same chat checks/refreshes its existing binding
rather than creating another session. Resuming a prior session is explicit, never
inferred from the newest file or another transcript. OMP's own session ID is only an
optional association when reliably exposed; the assistant omits it otherwise and
never invents one. Cloud memory, not OMP history, preserves work between sessions.

Use `/skill:maven-checkpoint` to preserve progress and `/skill:maven-stop-work` to
close only the current session. These coordinate writers, save truthful summaries
and references, and require explicit approval of the exact publication candidate.
Invoking a skill is not blanket commit/push approval. No-Git projects get memory-only
closure, and credentials shared by active peers are not logged out by one stop.
See [Project workflow](workflow.md) for the full approval and recovery contract.

For another project, open OMP in its explicitly chosen folder and use the same
start skill. Do not rerun instance provisioning or use the instance wizard's first
saved project as an additional-project pointer. An owner who prefers interactive
terminal project setup can use `maven-project setup --root /absolute/path/to/project`;
the normal in-harness path does not require that extra terminal step.

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

The installed generic skills are maven-setup, maven-connect, maven-skill-use,
maven-start-work, maven-checkpoint and maven-stop-work. Existing older copies are
conflicts too; the installer does not overwrite them as an upgrade. If integration
is deferred, stage the generic skills in a separate explicit `--skill-dir PATH`,
review differences, and arrange an owner-controlled cutover only after affected
harnesses stop. Never replace active instructions from within a running session.
No MCP server, daemon or auto-start hook is needed for these lifecycle skills.

| Harness | Default user skill root |
| --- | --- |
| Codex | `~/.agents/skills` |
| Claude Code | `~/.claude/skills` |
| OMP | `~/.omp/agent/skills` |
| Copilot CLI | `~/.copilot/skills` |
| Gemini CLI | `~/.gemini/skills` |
| Antigravity | `~/.gemini/config/skills` |

Custom profiles need an explicit destination; these are local installation targets,
not a guarantee of runtime behavior for every harness version. Cloud-hosted clients
require separately provisioned skills and credentials.

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

The guided installation and in-harness skills above are the normal entrypoints.
The remaining sections document underlying commands for deliberate manual
administration, not extra prerequisites for routine work. All shell variables below
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
For an agent that has already obtained every owner choice, the noninteractive
interface in [Project workflow](workflow.md#manual-project-setup-reference) generates
new identities safely and preserves an existing selection. `maven-project init`
remains a low-level explicit-ID API, not the ordinary first-use path. Manual starts
also require an explicit root and complete authority; retain the returned MAVEN
session UUID, distinct from any optional harness identity.

Review [Security and privacy](security.md) before sharing retrieved context with an
external model. Configure [optional integrations](integrations.md) only when needed;
neither Google nor a secrets-file bucket is required to start core memory work.
