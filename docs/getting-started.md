# Getting started

This guide distinguishes local installation, cloud preparation, cloud mutation and
instruction activation. Do not run the later stages merely because installation
succeeded. All shell variables denote your own reviewed values; MAVEN supplies no
preconfigured account, endpoint, credentials or active owner authority.

## 1. Install local tools

From the reviewed source checkout:

```sh
python3 install.py
export PATH="$HOME/.maven/venv/bin:$PATH"
maven-memory --help
```

Requires Python 3.11+ and macOS/Linux (or WSL). Git is needed for Git projects;
the AWS CLI is needed for the owner's interactive AWS login. The installer reports
missing external tools without installing them or blocking local package setup.
It creates an isolated environment and a private configuration template at
`~/.maven/.env`. It does not execute AWS login, deploy or activate cloud instructions.
Use `--home PATH` and set `MAVEN_HOME` to that path for a separate instance. No global
current project is created.

Optional `--harness NAME` installs generic bootstrap skills for `codex`, `claude`,
`omp`, `copilot`, `gemini` or `antigravity`; repeat it to select multiple harnesses.
Only the selected harnesses' documented user skill roots are modified. Copilot here
means the local CLI; cloud-hosted agents need separately provisioned skills.
The installer refuses conflicting existing files. For a custom active harness profile,
select its skill directory with `--skill-dir PATH`; the installer does not guess
custom profile locations. `--google` installs optional Google dependencies but does
not request Google consent. Harness instructions explain connection only, not a
hidden local replacement for owner cloud authority.

Default skill locations follow the harness documentation:

| Harness | User skill root |
| --- | --- |
| [Codex](https://developers.openai.com/codex/skills) | `~/.agents/skills` |
| [Claude Code](https://code.claude.com/docs/en/skills) | `~/.claude/skills` |
| [OMP](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md) | `~/.omp/agent/skills` |
| [Copilot CLI](https://docs.github.com/en/copilot/concepts/customizations/agent-skills) | `~/.copilot/skills` |
| [Gemini CLI](https://geminicli.com/docs/cli/skills/) | `~/.gemini/skills` |
| [Antigravity](https://antigravity.google/docs/skills) | `~/.gemini/config/skills` |

Custom profile overrides require an explicit `--skill-dir` instead of assuming these
default locations. Installing bootstrap skills does not configure cloud-hosted clients
or grant them credentials.

Initial runtime exercise is limited to OMP. Codex, Claude Code, Copilot CLI, Gemini CLI
and Antigravity have supported installation targets, but their end-to-end runtime
behavior remains unverified. A documented skill location is not evidence that a
particular harness version loaded or executed the workflow correctly.

## 2. Choose identity, infrastructure and costs

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
  method before cloud commands; there is no automatic login or default-profile fallback.
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

## 3. Prepare, review and apply the exact deployment

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

## 4. Review and activate a complete starter authority

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

## 5. Select the project and begin

Bind an existing cloud project or create a new owner-approved project using the
explicit `maven-project init` interface in [Project workflow](workflow.md). Start with
an explicit root, keep the returned session UUID, and load the complete applicable
cloud authority. A new project need not have a previous handover.

Review [Security and privacy](security.md) before sharing retrieved context with an
external model. Configure [optional integrations](integrations.md) only when needed;
neither Google nor a secrets-file bucket is required to start core memory work.
