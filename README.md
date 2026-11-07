# MAVEN

MAVEN gives AI coding assistants owner-controlled cloud memory: approved instructions,
project state, tasks and handovers that survive individual assistant sessions. You
choose the AWS instance, identities and project folder. MAVEN is not a hosted model
or a free cloud service.

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

The interactive installer asks where to install (default `~/.maven`), which assistant
harnesses to integrate, and whether to include optional Google dependencies. It
creates a private home, login-cache/runtime directories and its own `venv`, installs
the commands, and opens the setup wizard. Harness choices are `codex`, `claude`,
`omp`, `copilot`, `gemini`, `antigravity`, or none. Close affected harnesses before
approving skill installation; existing conflicting skills are never overwritten.
These are installation targets, not a claim that every harness has been runtime-tested.

The wizard offers two paths:

- **Connect an existing MAVEN instance:** import a private nonsecret environment file
  from a configured installation, or select an existing CloudFormation stack. Confirm
  the actual instance and routine AWS principal. This path does not redeploy,
  initialize the database, or replace active authority.
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

Next, explicitly select the folder you want to work in and an existing cloud project
or a new project with real source evidence. The installer checkout is not automatically
your project. Confirm the Git remote and branch, or choose **no Git** for memory-only
work. Setup creates private `.maven/project.json` and runtime state in that folder;
it does not commit, push, pull, or switch branches.

## Start work or resume setup

At the default installation location, enable the installed commands in your shell:

```sh
source "$HOME/.maven/activate"
```

Use your actual selected home's `activate` path if you changed it. Source this file
in each new shell (or add that line to your shell's startup file); never source `.env`.
Setup prints the actual paths and a first-use command for your selected project.
Open a terminal in that project and,
for example when using Codex, run:

```sh
maven-start-work --root "$PWD" --harness codex
```

Use your actual harness name. Keep the returned session UUID for checkpoint/stop;
[Project workflow](docs/workflow.md) explains those commands and approval gates.
A start requires verified cloud access and complete applicable approved authority.

If setup stops, you decline an approval, or login expires, preserve the private home
and resume with:

```sh
maven-setup
```

Reruns preserve configuration and pending review/request identities. An unknown cloud
outcome must be reconciled using the same artifacts, not bypassed by starting a new
installation. Setup is not complete until cloud access, authority and project
configuration have been verified; a local package installation alone is not readiness.

## Options and safety

- `python3 install.py --local-only` installs local tools without running cloud setup.
- `--home PATH`, `--harness NAME` (repeatable), `--skill-dir PATH`, and `--google`
  select installation options. Google is optional and requires separate consent;
  core memory does not need a Google account.
- For noninteractive native skill installation, close the affected harnesses and pass
  `--local-only --harness NAME --harness-stopped`. Without that confirmation, their
  files are not changed.
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

For detailed installation, recovery and manual commands, see [Getting started](docs/getting-started.md).
Also see [Architecture](docs/architecture.md), [Security](docs/security.md),
[Optional integrations](docs/integrations.md), and [Contributing](CONTRIBUTING.md).

## License

[MIT](LICENSE). Third-party dependencies retain their own licenses and notices.
