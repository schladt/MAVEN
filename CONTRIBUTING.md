# Contributing

Keep MAVEN generic, owner-controlled and usable from an installed package outside
its source checkout. Discuss changes to authentication, instruction authority,
publication, durable records or concurrency before changing their contracts.

## Local development

For first-time use, follow the [guided installer](README.md#install-and-set-up):
`python3 install.py` creates its own private environment and offers instance setup.
The project step is optional: enable an explicitly chosen folder in your supported
assistant. Configured projects retain their selection; new work sessions get distinct
MAVEN identities. Conversation through stdio MCP is the normal path; CLI/TUI remain
recovery surfaces. Memory checkpoints and closure never implicitly publish source.

No preexisting `.venv` or project `.maven` directory is required. The MAVEN source
checkout is not automatically a development project: explicitly choose/approve that
root before registration. Filesystem `/` is never valid. For local package
installation without the cloud wizard, use `python3 install.py --local-only`.
The development environment below is a separate, optional contributor workflow.

Use the existing Python 3.11+ environment with the required optional extras. Install
`.[mcp,ui]` for MCP/UI development, and select focused existing tests for the affected
behavior. Do not create a separate test environment or deploy cloud resources merely
to verify changes. Core recovery must not import MCP, UI or model dependencies.

Include the `google` extra only to exercise optional Google behavior. Those tests
use synthetic accounts and mocked services; no live Google access is required.
Keep a separate private `MAVEN_HOME` for any explicitly authorized manual work.
The repository must not contain your environment file, credentials, runtime state,
project receipts or downloaded originals.

For onboarding changes, keep smoke checks small: install into a disposable home,
verify native skill discovery, defer project setup, then configure/revisit two
disposable project roots. Exercise two sessions in one root and a session in the
other, including handover isolation and mismatched-resume rejection. Simulate cloud
boundaries for this local smoke; it is not proof of live AWS deployment or an
end-to-end model-driven chat.

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

- Preserve explicit project/session selection and complete cloud-authority loading.
- Keep bootstrap guidance intent-based and thin over shared controllers via MCP.
  Bind root and returned MAVEN identity to the work session across cwd changes;
  no global current-session file, guessed harness identity or implicit resume.
- Preserve non-Git memory, explicit scope selection, source-linked promotion,
  assignment supersession, checkpoint cutoffs and both resume modes. Native background
  reasoning is a harness capability, not an MCP or deterministic-worker feature.
- Keep service/schema rollout and exact active-policy approval separate from local
  implementation. Preserve historical checkpoints and pending recovery implementations.
- Keep instance setup separate from additional-project setup. Noninteractive
  creation needs explicit owner choices and genuine source evidence, and generated
  IDs must be journaled before mutation. Preserve legacy pending-request identities.
- Edit only generic source templates, never an active installation's skills or
  private cloud instruction bodies. Existing skill conflicts remain protected;
  stage/review separately and activate only after affected harnesses stop. Template
  changes are not approval to activate cloud authority.
- Keep runtime credentials distinct from setup/admin credentials. Never add ambient
  profile fallback, silent login, relaxed TLS verification or unreviewed activation.
- Update callers, relevant behavioral tests and the focused public documentation
  together. Prefer a small boundary test over a new test framework.
- Never make tests deploy, send mail, share files, log in, publish or access an owner's
  live cloud. Live exercises require separate owner approval and exact targets.
- Check installed resource access as well as source-checkout behavior. Core must
  remain usable without optional Google packages.
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
