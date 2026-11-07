# Contributing

Keep MAVEN generic, owner-controlled and usable from an installed package outside
its source checkout. Discuss changes to authentication, instruction authority,
publication, durable records or concurrency before changing their contracts.

## Local development

For first-time use, follow the [guided installer](README.md#install-and-set-up):
`python3 install.py` creates its own private environment and offers cloud/project
setup. No preexisting `.venv` or project `.maven` directory is required. For local
package installation without the cloud wizard, use `python3 install.py --local-only`.
The development environment below is a separate, optional contributor workflow.

Use a virtual environment with Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests
```

Install `-e '.[google]'` instead to exercise the optional Google tests. Those tests
use synthetic accounts and mocked services; no live Google access is required.
Keep a separate private `MAVEN_HOME` for any explicitly authorized manual work.
The repository must not contain your environment file, credentials, runtime state,
project receipts or downloaded originals.

## Change requirements

- Preserve explicit project/session selection and complete cloud-authority loading.
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
