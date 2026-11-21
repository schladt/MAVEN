# Update MAVEN

Only an explicit request to update installed MAVEN software starts or resumes this operation. “Update this brief” is a local memory edit. Editing a checkout does not authorize installation or cloud changes. Use `maven_admin` reviews and the owner's actual answers; never invent approvals, run AWS sign-in yourself, or search the filesystem for MAVEN source.

## 0. Choose the version

The update brings the installation to the MAVEN checkout recorded by the installer (or an explicit absolute `checkout`). If the owner wants the newest published MAVEN, update that checkout first and only with consent: confirm `git -C <checkout> status` is clean, then `git -C <checkout> pull --ff-only`. Never stash, reset, discard or merge the owner's local changes; if the pull cannot fast-forward, stop and report.

## 1. Begin or resume

Call `maven_admin` with `action="begin"`, `kind="setup"`, `parameters={"mode": "update"}` (add `"checkout": "<absolute path>"` only when the owner chose a different checkout). Retain the returned `operation_id` and tell the owner it, because step 2 needs a harness restart. If begin reports an unresolved setup operation, resume that exact operation ID with `action="status"`/`"advance"` instead of starting another. Each later call uses `action="respond"` for a pending interaction, otherwise `action="advance"`.

## 2. Local software and harness skills

The first stage compares the installed software and installer-owned skills with the checkout.

- A form `Update local MAVEN software` shows the differences; relay them and ask whether to update or continue without it.
- A `local_update` handoff gives the exact installer command. It must run in a separate terminal because it replaces the software this MCP server runs from and rewrites this harness's MAVEN skills and configuration. Give the owner the command; they choose **Update local MAVEN software** and review its changes there. Then the harness must be restarted so it loads the new MAVEN; afterwards continue the same operation ID. If advance says the process is still running old software, the restart has not happened yet.
- Skills the owner modified, or that another installation shares, are preserved and reported, not overwritten.
- Older installers may have printed the retired `--no-lifecycle-hooks` option. If the checkout rejects it, omit that option only; this version never installs lifecycle hooks. Keep the same selected home, recorded destinations and operation ID.

## 3. Sign-in and reviews

- For owner-requested administration pass `interactive_login=true` when beginning, responding or advancing. An `aws_login` handoff can also be opened with `maven_admin(action="login", operation_id=…, revision=…)`. Show the purpose and selected profile; only the owner may press a key or complete sign-in. If no terminal opens, give them the returned `terminal_command` or exact `sign_in_command`. After authentication check status/receipts of the **same** operation. A sign-in does not authorize an effect or its replay.
- Reviews: show the complete review, including SQL file names for the schema and every changed resource and replacement flag for the service. Respond only with the owner's real decision: `{approved, review_digest, source: {origin, excerpt}, confirmations}`. Declining pauses the operation; it does not silently skip an effect.
- After local software, check the **database schema**, then the **service**. Apply approved ordered schema migrations under maintenance mode. The service change set uses the live stack's deployment inputs and refuses changes that remove or replace a database, secret, bucket or key. Current stages pass without changes. This operation is not authorized to import or delete any legacy memory.
- Error `warming`: the Aurora database is resuming from auto-pause. Use the returned bounded read-only retry path when available. It spaces probes but is not a hard transport deadline. Never retry an uncertain mutation as a readiness probe; reconcile its original receipt.
- `uncertain` state: an effect may have reached AWS. Advance the same operation to reconcile it; never begin a replacement.

## 4. Finish

Report the verified outcomes from the operation receipts: local software and harness integration, schema, service and final readiness. Distinguish skipped or preserved items from changes applied. Remind the owner to restart affected harnesses.
