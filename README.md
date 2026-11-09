# MAVEN

MAVEN gives AI coding assistants owner-controlled cloud memory: approved instructions,
project state, tasks and handovers that survive individual assistant sessions. You
choose the AWS instance, identities and project folder. MAVEN is not a hosted model
or a free cloud service.

**Normal use:** install/connect your selected instance, enable memory for an explicit
project, then ask your assistant to remember, consult, checkpoint or wrap up. The
harness launches MAVEN over standard MCP stdio; no manual server or session-ID
assembly is required. OMP, Claude Code, Codex, Copilot CLI and Gemini CLI have
[documented integration paths](docs/integrations.md#harness-capability-matrix).
Memory works without Git; publication is optional and separately approved.

## Before you start

- **macOS or Linux**, or **Windows through WSL**. Run all commands inside WSL, not
  native Windows Python.
- **Python 3.11 or newer**, with `venv` and pip support.
- **Git** to clone this repository and for Git-backed projects. A downloaded source
  archive and a no-Git project are also supported.
- **AWS CLI v2 with `aws login` support** (2.32.0 or newer), an AWS account, and an
  owner-approved named profile/identity. Update older CLI versions using the
  [AWS installation instructions](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
  Setup needs network access to Python package downloads and the selected AWS services.

Useful starting commands:

| System | Python and Git |
| --- | --- |
| macOS with [Homebrew](https://brew.sh/) | `brew install python git awscli` |
| Ubuntu/Debian or Ubuntu in WSL | `sudo apt update && sudo apt install python3 python3-venv python3-pip git` |
| Fedora | `sudo dnf install python3 python3-pip git` |

Check that `python3 --version` is at least 3.11; older Linux releases may need a
newer supported Python package. Install the current AWS CLI v2 separately on Linux;
distribution packages may be too old for `aws login`. See
[WSL installation](https://learn.microsoft.com/en-us/windows/wsl/install) if needed.
The installer explains missing prerequisites; it does not install system packages
or grant AWS permissions.

## Install and set up

```sh
git clone https://github.com/schladt/MAVEN.git
cd MAVEN
python3 install.py
```

Alternatively, download and extract this repository's source archive, open a terminal
in the extracted folder, and run `python3 install.py`. You do **not** need an existing
virtual environment, private `.env`, or project `.maven` directory.

The full-screen Textual installer asks where to install (default `~/.maven`), which
assistant harnesses to integrate, and whether to include optional Google dependencies.
If UI dependencies are missing, a brief bootstrap prompt asks permission to download
them into a temporary private runtime; system Python is never modified. The installer
creates a private home, login-cache/runtime directories and its own `venv`, installs
the commands with MCP/UI dependencies, and opens setup. Harness choices are
`codex`, `claude`, `omp`, `copilot`, `gemini`, or none. Close affected harnesses before
approving integration changes; conflicting skills/configuration are preserved.
These are installation targets, not a claim that every harness has been runtime-tested.

Use Tab/Shift+Tab, arrow keys, Space and Enter, or the mouse. Ctrl+Q/Ctrl+C requests
a safe pause; it does not undo completed work or cancel an AWS operation already
submitted. AWS sign-in temporarily takes over the terminal, then returns to setup.
Reviews show full artifacts and identities. Destructive approvals require only the
first six characters of the displayed target identity (the entire identity if shorter);
the backend still receives the complete reviewed identity and digest.

The wizard offers two paths:

- **Connect an existing MAVEN instance:** choose **Import settings from another
  installation’s `.env` file** (usually `~/.maven/.env`), or **Find the existing
  deployment in AWS** using its CloudFormation stack—the named group of AWS
  resources for that instance. Confirm the actual instance and routine AWS principal.
  This path does not redeploy, initialize the database, or replace active authority.
- **Create a new MAVEN instance:** supply your account, stack/instance name,
  notification email, two availability zones and a separately selected deployment
  identity. Review costs and generated artifacts, then separately approve the change
  set, its exact execution digest, database initialization and each starter authority
  candidate. The current deployment template is restricted to `us-east-1` and a
  same-account human IAM user; see [deployment limitations](docs/getting-started.md#deployment-limitations-and-costs)
  before choosing this path. Deployment is not permission to activate instructions.

Normal work uses the explicitly selected routine identity, **never root or an implicit
administrator fallback**. Guided setup uses named `aws login` profiles, not static
access keys. A profile is a local label such as `maven`; if it is new or expired, the
wizard can open the normal AWS sign-in flow after you approve. You need an existing
AWS identity with the required access; the installer does not create your AWS account
or grant administrator permissions. Setup/deployment access is selected separately.

AWS sign-in may also offer AWS skills and an AWS MCP connection. These are optional
and separate from MAVEN; see [the AWS toolkit note](docs/getting-started.md#optional-aws-agent-toolkit).

Project setup is optional during installation. Confirm an explicit folder and an
existing cloud project or creation with real source evidence. Memory enablement
needs no repository, remote, branch or explicit no-Git choice. Optional Git setup
is separate. Private `.maven` selection/runtime files belong to that folder; setup
never commits, pushes, pulls or switches branches. The installation checkout is
not automatically a project, and `/` cannot be a project root.

## Work with your assistant

The configured client launches the installed `maven-mcp` process with the selected
private home, not embedded credentials. Installed `maven` front-door guidance and
`maven-maintenance` worker guidance route intent without CLI rituals.

Ask **“Enable MAVEN for this project.”** Confirm scope and selection. Subsequent
supported sessions visibly initialize, then report ready only after binding and
complete current authority/context load, or a precise blocked reason. Clients without
reliable automatic startup use **“Initialize MAVEN”** as the foreground fallback.
Installation/tool availability does not authorize capturing unrelated chats.

- Meaningful decisions, tasks, plans and outcomes are reconciled automatically.
  Real harness-native background subagents do this where supported; otherwise the
  foreground assistant does. MCP itself does not spawn agents.
- Ask **“Remember this”**, **“Consult memory”**, or **“Reconcile the plan.”** Explicit
  saves confirm verified cloud receipts; routine successes stay quiet.
- Default retrieval includes universal and current-project knowledge. Ask to consult
  other projects or correct scope; provenance remains intact.
- **“Make a checkpoint”** saves a point-in-time handover and exact revisions through
  a declared cutoff. **“Checkpoint and wait”** requests synchronization. Neither
  backs up source files, commits or pushes.
- **“Pause automatic updates”** affects this session only; explicit operations remain
  available. **“Resume”** catches up from available discussion; **“Resume fresh”**
  does not look back. Missing context is disclosed.
- **“Wrap up”** closes this memory session after verified required writes, not other
  sessions or shared credentials. Summaries separate universal/project changes and
  pending work. Outages preserve private pending evidence, never a false saved claim.

See [Project workflow](docs/workflow.md) for readiness, checkpoints, outage recovery
and exact-candidate safety for explicitly selected Git publication.

## Resume instance setup

If setup stops, you decline an approval, or login expires, preserve the private home
and resume with:

```sh
maven-setup
```

Reruns preserve configuration and pending review/request identities. An unknown cloud
outcome must be reconciled using the same artifacts, not bypassed by starting a new
installation. Instance readiness requires verified cloud access and complete
applicable authority; project readiness is reported separately if you defer that
optional step. A local package installation alone is not readiness.

## Uninstall selected scopes

Run `maven-uninstall` for the full-screen operation dashboard, or
`python3 -m tools.uninstall` from this checkout using an interpreter with the `[ui]`
dependencies. Inspect or resume a saved operation, or start a new guided plan.
**Project, machine, and cloud removal are independent choices**; selecting one
never implicitly selects the others.

You can also ask the assistant to plan removal in conversation. Review the exact
independent scopes and retained resources. The running harness cannot attest that
it is stopped: affected integration/self-removal requires a protected external
continuation using the pinned recovery implementation.

Project memory and protected local artifacts are retained by default. Cloud removal
defaults to recoverable retirement; permanent purge requires separate approval.
Every operation has an exact reviewed plan and an external recovery directory.
The TUI stays available through self-removal using an offline private runtime copy.
Noninteractive `plan`, `apply`, `resume`, and `status` remain available; local offline
detachment and recovery require neither Textual nor AWS packages or login.

Close affected harnesses and writers before applying removal. See
[Uninstall and retirement](docs/getting-started.md#uninstall-and-retirement) for
scope selection, legacy installations, export, retained charges, and recovery.

## Options and safety

- `python3 install.py --local-only` installs local tools without running cloud setup.
- `--home PATH`, `--harness NAME` (repeatable), `--skill-dir PATH`, and `--google`
  select installation options. Google is optional and requires separate consent;
  core memory does not need a Google account.
- For noninteractive native skill installation, close the affected harnesses and pass
  `--local-only --harness NAME --harness-stopped`. Without that confirmation, their
  files are not changed.
- Stage integrations without live configuration writes using `--stage-harnesses PATH`.
  Custom profiles use paired `--harness-config NAME=ABSOLUTE_FILE` and
  `--harness-context NAME=ABSOLUTE_FILE`, plus an explicit `--skill-dir PATH`.
  Review staged changes and conflicts before an owner-controlled stopped-harness cutover.
- Behind a VPN or corporate TLS proxy, pass `--ca-bundle PATH` with a trusted PEM
  bundle supplied by your owner/administrator. A new private `activate` snippet retains
  this explicit selection; existing snippets are preserved and any required exports
  are printed. Never disable certificate verification or auto-trust a certificate.
- Private configuration is mode `0600`; private state directories are `0700`.
  Keep `.env`, login caches, review artifacts and project runtime state out of Git.
  Configuration contains identity selections and locators, not credentials or tokens.
- AWS resources can incur continuing charges, including retained storage after stack
  deletion. Review costs and retention before approving cloud actions.
- Cloud memory is authoritative, not an offline instruction cache. Sharing retrieved
  context with an external model is a separate disclosure subject to that provider's
  policies; MAVEN does not promise private inference.

## Upgrade boundaries

Local installation is not live rollout. Existing instances need a separately reviewed
matching service/schema upgrade before changed memory contracts can be used. Recover
incompatible pending operations through their original path first. Then review and
activate changed cloud instructions separately; templates are proposals and never
replace active policy automatically. Documentation is not proof of runtime coverage
of all five clients, live migration, deployment or activation.

For detailed installation, recovery and manual commands, see [Getting started](docs/getting-started.md).
Also see [Architecture](docs/architecture.md), [Security](docs/security.md),
[Optional integrations](docs/integrations.md), and [Contributing](CONTRIBUTING.md).

## License

[MIT](LICENSE). Third-party dependencies retain their own licenses and notices.
