# Contributing

Keep MAVEN generic, owner-controlled and usable from an installed package outside
its source checkout. Discuss changes to authentication, instruction authority,
publication, durable records or concurrency before changing their contracts.

## Local development

For first-time use, follow the [guided installer](README.md#install-and-set-up):
`python3 install.py` creates its own private environment and offers instance setup.
Project enablement is separate: review the intended root and cloud identity in the harness. Work from the local brief and use the [reconcile procedure](maven_resources/templates/skills/maven/references/reconcile.md) for synchronization. Memory work never implicitly publishes source.

No preexisting `.venv` or project `.maven` directory is required. The MAVEN source
checkout is not automatically a development project: explicitly choose/approve that
root before registration. Filesystem `/` is never valid. For local package
installation without the cloud wizard, use `python3 install.py --local-only`.
The development environment below is a separate, optional contributor workflow.

Use the existing Python 3.11+ environment with the required optional extras. Install
`.[mcp,ui]` for MCP/UI development, and select focused existing tests for the affected
behavior. Do not create a separate test environment or deploy cloud resources merely
to verify changes. Core recovery must not import MCP, UI or model dependencies.

The installed-wheel regression in `test_packaging_project` builds offline without
build isolation. Its test interpreter needs pip and the build requirements from
`pyproject.toml` (`setuptools>=77` and `wheel`); the runtime itself does not.

Keep a separate private `MAVEN_HOME` for any explicitly authorized manual work.
The repository must not contain your environment file, credentials, runtime state,
project receipts or downloaded originals.

For onboarding changes, use a disposable installation and explicitly selected test roots. Exercise project isolation, explicit Home, no capture in an unsupported folder, and rejection of legacy or mismatched bindings. Simulate cloud boundaries; this is not proof of live AWS or end-to-end model-driven operation.

For uninstall changes, use the existing interpreter and focused offline checks:

```sh
python3 -B -m unittest discover -s tests -p 'test_uninstall*.py'
```

These use temporary filesystem fixtures and mocked provider responses, not a new
virtual environment or live AWS resources. Keep verification narrow: scope isolation,
preservation/export, exact approval, and uncertain-outcome recovery. Do not claim
live teardown, SQL concurrency, or successful restoration from these tests.

For TUI/controller changes, reuse the installed MAVEN virtual environment with the
`[ui]` extra rather than creating a separate test environment:

```sh
PYTHONPATH=tests "$MAVEN_HOME/venv/bin/python" -B -m unittest \
  test_tui test_uninstall_cli test_installer test_setup test_project_setup
```

Use temporary filesystem fixtures and simulated package/provider results. A brief
real-terminal check should cover navigation, resizing and safe quit; do not deploy
or delete cloud resources to verify presentation. Core local recovery must still
work using system Python without importing Textual or AWS packages.

## Change requirements

- Preserve offline orientation and durable local capture. Do not add a work-session lifecycle, implicit project selection or cloud dependency on harness open/exit.
- Keep semantic reasoning in the installed `maven` skill’s reconcile procedure. Compare whole-project views, fetch details selectively, guard writes on both sides, and verify convergence. Preserve competing evidence and never resurrect erased content.
- Keep record ownership clear: a project skill carries steps **or** a repository link; a link does not authorize reading secrets, uploading the file, running code or publishing.
- Keep service/schema rollout and sensitive external approvals separate from local implementation. Preserve pending setup, Git and removal recovery evidence.
- Keep instance setup separate from additional-project setup. Noninteractive
  creation needs explicit owner choices and genuine source evidence, and generated
  IDs must be journaled before mutation. Preserve legacy pending-request identities.
- Edit only generic source templates, never an active installation's skills or
  private cloud instruction bodies. Existing skill conflicts remain protected;
  stage and review local changes separately. Templates do not activate cloud policy.
- Keep runtime credentials distinct from setup/admin credentials. Never add ambient
  profile fallback, silent login, relaxed TLS verification or unreviewed activation.
- Update callers, relevant behavioral tests and the focused public documentation
  together. Prefer a small boundary test over a new test framework.
- Never make tests deploy, send mail, share files, log in, publish or access an owner's
  live cloud. Live exercises require separate owner approval and exact targets.
- Check installed resource access as well as source-checkout behavior. Core recovery
  must remain usable without optional MCP or UI packages.
- Preserve revision guards, request keys and unknown-outcome receipts. Do not solve
  races by ignoring errors, deleting evidence or resetting unrelated Git state.
- Use only invented fixtures and reserved example domains. Do not paste private
  instructions, source histories, customer data, real resource IDs or token-shaped
  values into issues, tests, logs or documentation.

For MCP changes, use the real stdio adapter/SDK client with simulated storage and the
existing environment. Verify discovery, representative calls and shutdown without
printing non-protocol text to stdout. Record exact harness/runtime combinations
actually exercised; documented configuration is not a runtime interoperability test.
SQL migration runtime validation and live-cloud deployment are separate evidence.

Describe the behavior changed, checks actually run, and any untested external
assumptions in your contribution. Do not claim a successful deployment or workflow
from a mocked test alone. For suspected security issues, avoid public exploit data
or secrets; use the repository host's private security reporting channel if enabled,
or contact a maintainer privately before disclosing sensitive details.

## Licensing

Contributions must be yours to license under [MIT](LICENSE), or have compatible
terms and retained attribution. Do not copy third-party implementation code without
reviewing its license. Dependencies installed by the package manager retain their
own licenses; preserve required notices when redistributing their code or binaries.
