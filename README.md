# MAVEN

Owner-controlled cloud memory and explicit project workflows for AI coding assistants.

MAVEN keeps approved instructions, project state, tasks and handovers in an
owner-selected cloud service rather than depending on a particular assistant's
transcript. Its local CLI connects isolated clients to that memory, tracks
independent sessions, and coordinates reviewable checkpoints in shared checkouts.

## What ships

- Revision-safe cloud records and owner-approved instruction activation.
- Explicit project selection and durable session UUIDs; no global current project.
- Start, checkpoint and stop workflows with exact-candidate approval and peer-writer coordination.
- Private workspace-original handling and AWS-backed secrets/encrypted files.
- Optional, explicit-account Google access. Core installation needs neither Google dependencies nor a Google account.
- Generic starter authority proposals and opt-in bootstrap skills for supported harnesses.

## Install

Requires Python 3.11 or newer, Git for Git-backed projects, and macOS or Linux.
Windows users can use WSL. Review the installer before running:

```sh
git clone https://github.com/schladt/MAVEN.git
cd MAVEN
python3 install.py
export PATH="$HOME/.maven/venv/bin:$PATH"
maven-memory --help
```

The installer creates an isolated user installation under `~/.maven`; it does not
log in, deploy cloud resources, activate instructions or modify a harness unless
you explicitly select one. Use `--home PATH` for an isolated instance and set
`MAVEN_HOME` to that same path when using its commands. `--google` adds the optional
Google dependencies. Harness integration is opt-in with `--harness NAME`, supporting
`codex`, `claude`, `omp`, `copilot`, `gemini` and `antigravity`; repeat the option for
multiple harnesses. `--skill-dir PATH` selects another destination.
Existing configuration and conflicting skill files are preserved.
Initial harness runtime exercise is limited to OMP. The other five harnesses are
installation-supported; their end-to-end runtime behavior remains unverified.

Installation alone is not a configured service. Follow
[Getting started](docs/getting-started.md) to review costs, provision the exact
approved cloud artifacts, bind the intended caller, and activate the starter authority.

## Use a configured project

The example variables below are values from your own configuration, not supplied
resource identifiers:

```sh
maven-project init --root "$PROJECT_ROOT" --project-id "$PROJECT_ID" \
  --remote origin --accepted-url "$REPOSITORY_URL" --branch "$BRANCH"
maven-start-work --root "$PROJECT_ROOT" --harness codex --harness-session "$HARNESS_SESSION"
# Keep the returned MAVEN session UUID.
maven-checkpoint --root "$PROJECT_ROOT" --session "$SESSION_ID" --writers-quiescent
maven-stop-work --root "$PROJECT_ROOT" --session "$SESSION_ID" --writers-quiescent
```

Checkpoint/stop inspection does not authorize publication. Read the candidate,
pause registered peers as requested, and approve only the exact returned code.
See [Project workflow](docs/workflow.md) for the full review and recovery sequence.

## Boundaries

Cloud memory is the durable authority. Private local configuration, session receipts
and preserved originals are not an offline replacement for it. If required cloud
authority cannot be loaded, governed work stops rather than using stale instructions.

MAVEN is not a model host or a promise of private inference. Context supplied to an
external assistant or model leaves your cloud and is subject to that provider's
retention and processing policies. Approve that disclosure separately.

Same-checkout coordination covers registered MAVEN writers, not arbitrary editors,
shells or distributed hosts. It does not assign file ownership to sessions or make
concurrent edits conflict-free. AWS resources and third-party services may incur
ongoing charges; there is no free-service guarantee.

- [Architecture and storage](docs/architecture.md)
- [Security and privacy](docs/security.md)
- [Secrets, workspace and optional Google](docs/integrations.md)
- [Contributing](CONTRIBUTING.md)

## License

[MIT](LICENSE). Third-party dependencies retain their own licenses and notices;
the MAVEN license does not relicense them.
