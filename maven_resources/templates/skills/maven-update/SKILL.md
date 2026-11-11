---
name: maven-update
description: Update an installed MAVEN to its checkout's version, locally and in the cloud — software, harness skills, database schema, service stack and owner instructions — through one resumable, separately reviewed operation.
---

# Update MAVEN

Use when the owner asks to update, upgrade or refresh MAVEN itself, or after the MAVEN checkout changed. This is not memory work: it changes the installation and the selected cloud instance, so every effect goes through `maven_admin` reviews with the owner's actual answers. Never invent approvals, run AWS sign-in yourself, or search the filesystem for MAVEN source.

## 0. Choose the version

The update brings the installation to the MAVEN checkout recorded by the installer (or an explicit absolute `checkout`). If the owner wants the newest published MAVEN, update that checkout first and only with consent: confirm `git -C <checkout> status` is clean, then `git -C <checkout> pull --ff-only`. Never stash, reset, discard or merge the owner's local changes; if the pull cannot fast-forward, stop and report.

## 1. Begin or resume

Call `maven_admin` with `action="begin"`, `kind="setup"`, `parameters={"mode": "update"}` (add `"checkout": "<absolute path>"` only when the owner chose a different checkout). Retain the returned `operation_id` and tell the owner it, because step 2 needs a harness restart. If begin reports an unresolved setup operation, resume that exact operation ID with `action="status"`/`"advance"` instead of starting another. Each later call uses `action="respond"` for a pending interaction, otherwise `action="advance"`.

## 2. Local software and harness skills

The first stage compares the installed software and installer-owned skills with the checkout.

- A form `Update local MAVEN software` shows the differences; relay them and ask whether to update or continue without it.
- A `local_update` handoff gives the exact installer command. It must run in a separate terminal because it replaces the software this MCP server runs from and rewrites this harness's MAVEN skills and configuration. Give the owner the command; they choose **Update local MAVEN software** and review its changes there. Then the harness must be restarted so it loads the new MAVEN; afterwards continue the same operation ID. If advance says the process is still running old software, the restart has not happened yet.
- Skills the owner modified, or that another installation shares, are preserved and reported, not overwritten.

## 3. Sign-in and reviews

- `aws_login` handoffs: give the owner the command to run privately (everyday access first; maintenance access before the service stage), then advance. Identity confirmations are reviews like any other.
- Reviews: show the owner the complete review (title, description and the decisive content: SQL file names for the schema, every changed resource and its replacement flag for the service, page titles and differences for instructions). Respond only with their real decision: `{approved, review_digest, source: {origin, excerpt}, confirmations}`. Declining pauses the operation; it never skips a stage silently.
- Stages, in order: **schema** (pending ordered migrations, applied under maintenance mode), **service** (a CloudFormation change set built from the live stack's own deployment inputs; refused before review if it would remove or replace a database, secret, bucket or key), **instructions** (bundled pages that are new or differ from the active ones; the form shows exact differences and warns that an update replaces owner edits). Current stages pass without questions.
- Error `warming`: the Aurora database is resuming from auto-pause. Wait about 30 seconds and advance again; repeat for up to 10 minutes before reporting it.
- `uncertain` state: an effect may have reached AWS. Advance the same operation to reconcile it; never begin a replacement.

## 4. Finish

The operation completes after readiness verification. Report each stage's verified outcome from the receipts — local (updated, skipped or current), schema (migrated or current), service (updated with its change set, or current), instructions (activated pages, kept, or current) — plus anything preserved and why. After instruction changes, reload authority with `maven_context`. Remind the owner to restart other harnesses that use MAVEN.
