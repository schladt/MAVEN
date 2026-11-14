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

The full-screen Textual installer starts with a task: connect existing memory,
create a new instance, or **Uninstall**. For an existing installation it shows saved, **unverified**
account/instance settings and offers check/repair, local software update, or a
separate installation. Software-only installation and optional Google/certificate
settings live under **Advanced options**. The private home defaults to `~/.maven`.
If UI dependencies are missing, a bootstrap prompt requests permission to download
them into a temporary private runtime; system Python is never modified.

After verified cloud setup, optionally review local integration for OMP, Claude Code,
Codex, Copilot or Gemini. Running harnesses do not block the review; restart them
afterward and confirm MAVEN is listed, since a harness that saves its own configuration
while running can overwrite the entry. Conflicts are preserved. Files written, cloud
readiness and an actual harness connection are reported separately. A written
configuration is **not** a verified MCP connection.
Older installer-owned MCP launchers with group-writable permissions can be repaired
to `0755` within the local file-change review. Modified or unowned legacy skills
are preserved and require separate resolution; restarting cloud setup does not
fix those local conflicts. Use **Retry local harness configuration** instead.

Use Tab/Shift+Tab, arrow keys, Space and Enter, or the mouse. Ctrl+Q/Ctrl+C requests
a safe pause; it does not undo completed work or cancel an AWS operation already
submitted. AWS sign-in temporarily takes over the terminal, then returns to setup.
Back revises unsubmitted choices without undoing completed actions. Plain-language
reviews explain purpose, changes and consequences; expandable technical details
retain complete artifacts and review digests. Destructive confirmations remain separate.
The active step's account, access and target details stay inside the bordered panel.
A bottom status bar distinguishes background work, awaiting input, pause, failure
and completion. The activity indicator is spaced apart from the status text.

The wizard offers two paths:

- **Connect an existing MAVEN instance:** leave the connection override blank.
  Saved connections are reused; otherwise confirm maintenance AWS access and choose
  from MAVEN stacks discovered in that account/region. Then select daily-use access
  and review the exact connection. An explicit stack name or configured installation's
  `.env` path remains an optional override. This path never redeploys, initializes
  the database, or replaces active authority.
- **Create a new MAVEN instance:** confirm the account discovered after sign-in,
  then provide the stack/instance name, notification email and two availability zones.
  The instance name defaults to a compatible stack name and remains editable.
  Suggested zones (`us-east-1a, us-east-1b`) and PostgreSQL version (`18.6`) are
  editable, not verified AWS availability. Spaces around comma-separated zones
  are accepted; confirm actual regional support before deployment.
  Select maintenance access separately. **Prepare deployment review** requests a
  change-set preview without deploying resources. Review costs and approve exact
  billable execution, database initialization, then **one approval to upload and
  activate all nine starter instruction pages**. Complete artifacts remain available
  in expandable technical details. The current deployment template is restricted
  to `us-east-1` and a same-account human IAM user; see
  [deployment limitations](docs/getting-started.md#deployment-limitations-and-costs).
  Deployment is not permission to activate instructions.

**Daily-use access** operates memory; **maintenance access** manages infrastructure
or reads necessary stack metadata. These are purposes, not necessarily different AWS
accounts. Existing configured connections do not require a maintenance login.
Named `aws login` profiles are local labels, not proof of identity or permissions.
Setup displays known expected account/principal details before login, explicitly
marks unknown values, then verifies and asks you to confirm the actual identity.
Saved-account mismatches block progress; root is never a daily-use fallback.
Read-only preflights report **Passed**, **Missing access**, or **Could not verify**
with limitations and remediation. They do not grant permissions or guarantee deployment.

After verified setup, **Continue / configure coding harnesses** resumes local
configuration without repeating cloud approvals. Saved readiness must still match
the exact local settings; AWS is not rechecked by this step. Explicit connection
check/repair remains a separate task. Existing unfinished checks retain their own
checkpoints and receipts.

AWS sign-in may also offer AWS skills and an AWS MCP connection. These are optional
and separate from MAVEN; see [the AWS toolkit note](docs/getting-started.md#optional-aws-agent-toolkit).

Project enablement happens **in your harness, not the installer**. Open the actual
project and ask **“Enable MAVEN for this project.”** Review the proposed root,
instance and scope before approving. Memory needs no repository, remote or branch;
Git assistance is separate. The installation checkout is never automatically bound.

## Work with your assistant

The configured client launches the installed `maven-mcp` process with the selected
private home, not embedded credentials. Installed `maven` front-door guidance,
`maven-maintenance` worker guidance and `maven-update` self-update guidance route
intent without CLI rituals.

Ask **“Enable MAVEN for this project.”** Confirm scope and selection. Subsequent
supported sessions visibly initialize, then report ready only after binding and
complete current authority/context load, or a precise blocked reason. Clients without
reliable automatic startup use **“Initialize MAVEN”** as the foreground fallback.
Installation/tool availability does not authorize capturing unrelated chats.

Optional native lifecycle hooks start the same portable workflow: **checking →
initializing → ready**, or a clear selection/recovery/sign-in blocker. Exact native
conversation bindings resume with their pause state intact; new conversations get
new sessions. Other sessions are peers, never candidates to adopt by recency.
Stale or unfinished peers prompt **review recovery**, **start new and leave them
alone**, or **Read Only**. Staleness is not proof that a writer stopped.

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
- After a harness restart the assistant checks `maven_status` before saying it is
  connected and resumes only your exact session. Ask **“What instructions do you
  have?”** to list active cloud instructions (`maven_instructions`). Answers say
  whether they came from this conversation or were re-read from MAVEN.
- Search is ranked with snippets; records tagged or stated `archived` drop out of
  default results. Universal memory holds only preferences, instructions and general
  lessons; customer- and project-specific detail never goes there. Standing policies
  you state can be offered as instructions for future sessions.
- Outside an enabled project, each session asks **Start Home, Create Project, or Read
  Only?** every time; no answer means Read Only, which creates no session and captures
  nothing (ask **“use what MAVEN knows”** to consult read-only). The assistant prints
  the active line (for example `MAVEN: read-only`) at start and on any change. Optional
  **MAVEN Home** holds your personal and work tasks and people/organization awareness;
  set it up with the installer's **“Set up or change MAVEN Home”**. See
  [MAVEN Home and the start choice](docs/getting-started.md#maven-home-and-the-start-choice).
- **“Wrap up”** closes this memory session after verified required writes, not other
  sessions or shared credentials. Summaries separate universal/project changes and
  pending work. Outages preserve private pending evidence, never a false saved claim.
- With optional OMP lifecycle integration, ordinary exit offers **Wrap up and exit**,
  **Exit without wrapping up**, or **Stay** when this conversation has an open MAVEN
  session. Only verified closure permits automatic exit. Other harness shutdown
  hooks are best-effort, not exit vetoes; forced termination cannot guarantee wrap-up.
- Ask **“Review MAVEN recovery”** for read-only diagnosis before choosing a specific
  repair. Original pending requests are preserved, not blindly retried.
- Ask **“Switch this folder to an existing MAVEN project”** to review an explicit
  rebinding after local sessions are finished. MAVEN preserves the old runtime and
  leaves old cloud records and other machines untouched; cleanup is separate.

Home is a per-machine selection of a cloud project plus a local folder. To share
Home across machines, select the same cloud project ID, not merely the same name.
The project index's `is_current_context` flag identifies the project being viewed,
not every machine's Home selection.

See [Project workflow](docs/workflow.md) for readiness, checkpoints, outage recovery
and exact-candidate safety for explicitly selected Git publication.

## Resume instance setup

If setup stops, you decline an approval, or login expires, preserve the private home
and resume with:

```sh
maven-setup
```

Reruns preserve configuration and pending review/request identities. To start over,
choose **Abandon unfinished setup and start over** in `python3 install.py`. After exact
confirmation, it archives the old operation and settings, clears the active connection,
and opens fresh choices. Software, sign-in caches and cloud resources remain.
**This does not cancel AWS work or stop charges.** Unknown outcomes still require
reconciliation; do not duplicate an unresolved deployment. See
[setup recovery](docs/getting-started.md#resume-instance-setup) for restart receipts.
Instance readiness still requires verified cloud access and complete applicable
authority; project enablement remains a separate harness task.

## Uninstall selected scopes

Run `python3 install.py` and choose **Uninstall**. It checks `~/.maven`, `MAVEN_HOME`,
the selected `--home`, and registered installations. Choose **Advanced — browse or
enter an installation path** for custom locations. Discovery is local and read-only;
the chosen home is carried into the protected removal dashboard with no scope preselected.

The standalone `maven-uninstall` and `python3 -m tools.uninstall` entrypoints remain
available for recovery with the `[ui]` dependencies. Inspect/resume a saved operation
or start a new guided plan; existing plans retain their original targets.
**Project, machine, and cloud removal are independent choices**; selecting one
never implicitly selects the others.

You can also ask the assistant to plan removal in conversation. Review the exact
independent scopes and retained resources. The running harness cannot attest that
it is stopped: affected integration/self-removal requires a protected external
continuation using the pinned recovery implementation.

Project memory and protected project artifacts are retained by default. Machine removal
deletes the whole installation folder, including settings, after archiving them if you
give an archive directory; it keeps the folder only when it cannot be proven MAVEN's. Cloud removal
defaults to recoverable retirement; permanent purge requires separate approval.
Every operation has an exact reviewed plan and an external recovery directory.
The TUI stays available through self-removal using an offline private runtime copy.
Noninteractive `plan`, `apply`, `resume`, and `status` remain available; local offline
detachment and recovery require neither Textual nor AWS packages or login.

Stop affected MAVEN writers before applying removal; running harnesses only produce a
warning. Idle registered sessions are detached with retained evidence. See
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
- Native lifecycle adapters are enabled by default for selected harnesses and shown
  in the local integration review. Use `--no-lifecycle-hooks` (or uncheck the TUI
  option) for guidance-only startup and explicit wrap-up. No harness is mandatory.
- Stage integrations without live configuration writes using `--stage-harnesses PATH`.
  Custom profiles use paired `--harness-config NAME=ABSOLUTE_FILE` and
  `--harness-context NAME=ABSOLUTE_FILE`, plus an explicit `--skill-dir PATH`.
  Review staged changes and conflicts before an owner-controlled stopped-harness cutover.
  Custom profile overrides currently require `--no-lifecycle-hooks`; the installer
  refuses to guess a native hook location or write to the default profile instead.
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

## Update MAVEN

Ask your assistant to **"Update MAVEN"** (the `maven-update` skill), or run
`python3 install.py` and choose **Update MAVEN**. One resumable operation brings an
installation to its checkout's version, reviewing each part only where it differs:

1. **Local software and harness skills** — the installer runs in a separate terminal
   because it replaces the running software; restart harnesses afterward. Skills you
   edited, or that another installation shares, are preserved.
2. **Database schema** — pending ordered migrations, under maintenance mode.
3. **Service stack** — a CloudFormation change set built from the live stack's own
   deployment inputs. A change that would remove or replace the database, a secret,
   a bucket or a key is refused before review.
4. **Instructions** — bundled pages that are new or differ from the active ones, with
   exact differences; updating replaces your edits to those pages, so it is optional.

To get the newest published MAVEN, update the checkout first (`git pull --ff-only`).
Local installation alone is not live rollout, and templates never replace active
policy without approval. Documentation is not proof of runtime coverage of all five
clients, live migration, deployment or activation.

For detailed installation, recovery and manual commands, see [Getting started](docs/getting-started.md).
Also see [Architecture](docs/architecture.md), [Security](docs/security.md),
[Optional integrations](docs/integrations.md), and [Contributing](CONTRIBUTING.md).

## License

[MIT](LICENSE). Third-party dependencies retain their own licenses and notices.
