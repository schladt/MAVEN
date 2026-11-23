# MAVEN

MAVEN gives coding assistants project memory they can read and update offline. In an enabled folder the assistant opens a local orientation brief, records useful knowledge as work happens, and uses **maven reconcile** to bring that project's local and AWS copies into agreement. Close the harness whenever you like: no work session to finish and no network call required on exit. A local save does not mean another machine has received it.

## Before you start

- macOS, Linux, or Windows through WSL (run the installer inside WSL).
- Python 3.11+ with `venv` and pip. Git is useful for cloning; neither a source archive nor a non-Git project needs a repository to use memory.
- For cloud setup: AWS CLI v2 with `aws login` support (2.32.0+), an AWS account, network access, and an explicitly selected named identity. See [AWS CLI installation](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html). MAVEN is not a hosted model or a free cloud service.

## Install and set up

```sh
git clone https://github.com/schladt/MAVEN.git
cd MAVEN
python3 install.py
```

Alternatively, extract the source archive and run `python3 install.py` there. No existing virtual environment or `.env` is required. The guided installer connects to an existing instance or prepares a separately reviewed deployment. The private installation defaults to `~/.maven`. Use `--local-only` to install software without cloud setup; this neither enables a project nor establishes cloud readiness.

Review the exact harness integration, restart the affected harness, and confirm it lists MAVEN. Writing a configuration file isn't a verified MCP connection. [Harness configuration](docs/integrations.md#harness-capability-matrix) covers OMP, Claude Code, Codex, Copilot CLI and Gemini CLI.

Open the intended folder and ask **“Enable MAVEN for this project.”** Review the absolute root, instance and cloud project ID. The source checkout isn't enabled by installation. A schema-1 binding requires a reviewed switch; don't edit it in place. With turn capture enabled, untracked folders use configured Nexus Home for local memory without enabling those folders. A damaged binding never redirects project work into Home.

## Working with project memory

| Ask the assistant | What happens |
| --- | --- |
| “What should I know before working on this?” | Reads the local brief: accepted decisions, open questions, verified outcomes, project skills and where to read more. No claim of fresh cloud knowledge. |
| “Remember why we chose this.” | Updates the relevant local record, keeping evidence and distinguishing a proposal from an accepted decision. |
| “Reconcile this project.” | Compares both inventories, reconciles compatible work, and verifies what reached both sides. A disputed decision is reported separately; unrelated work can still converge. |

The [MAVEN skill](maven_resources/templates/skills/maven/SKILL.md) loads local project context and Nexus preferences once, saves qualifying work before answering, and ends each enabled reply with one status line. A no-change follow-up needs no MAVEN call. The [reconcile procedure](maven_resources/templates/skills/maven/references/reconcile.md) syncs knowledge **only on request**, not source files, transcripts or secrets. **Git publication, secret use, deployment and policy activation require separate approvals.**

## Setup, update and removal

- [Getting started](docs/getting-started.md): cloud identities, cost and permissions, private sign-in, installer options, and [resuming interrupted setup](docs/getting-started.md#resume-instance-setup).
- [Updating MAVEN](docs/getting-started.md#existing-instance-upgrade): separately review local software, cloud schema and service changes. This checkout is not evidence of installation, live rollout, Home migration or legacy deletion.
- [Uninstall and retirement](docs/getting-started.md#uninstall-and-retirement): choose project, machine and cloud scopes independently. Inspect exact targets, retention and any remaining charges; preserve the pinned recovery plan.
- [Project workflow and optional Git](docs/workflow.md) · [Architecture](docs/architecture.md) · [Secrets and harnesses](docs/integrations.md) · [Security and privacy](docs/security.md) · [Contributing](CONTRIBUTING.md).

## License

[MIT](LICENSE). Third-party dependencies retain their own licenses and notices.
