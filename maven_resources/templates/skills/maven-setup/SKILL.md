---
name: maven-setup
description: Install or connect an explicitly owner-selected MAVEN memory instance with private settings and reviewed deployment artifacts.
---

# Set up MAVEN

This is a bootstrap procedure, not deployment authorization. The owner must select the instance, routine identity and separate deployment identity. Never auto-login, elevate, use root credentials, guess resource identifiers, or replace active harness instructions.

## Install local commands

From a reviewed public checkout run `python3 install.py` with Python 3.11 or newer. It installs into `~/.maven/venv` and creates `~/.maven/.env` only when absent. Add `~/.maven/venv/bin` to PATH. An explicit `MAVEN_HOME` selects another instance home. `--google` includes optional Google dependencies. No source-checkout virtual environment is required afterward. The installer reports missing Git and AWS CLI without installing them: Git is optional for memory-only projects, while an AWS CLI supporting login is needed for the owner interactive login flow. Local package installation does not claim cloud readiness.

Bootstrap discovery is opt-in: `--harness codex`, `--harness claude`, `--harness omp`, `--harness copilot`, `--harness gemini`, or `--harness antigravity`; repeat for multiple harnesses. These select documented default user skill roots: `~/.agents/skills`, `~/.claude/skills`, `~/.omp/agent/skills`, `~/.copilot/skills`, `~/.gemini/skills`, and `~/.gemini/config/skills`, respectively. Copilot installation targets local discovery, not remote cloud sessions. Custom profiles or configuration roots require an explicit `--skill-dir PATH`. Existing differing skills cause a refusal, never replacement. Use a separate skill directory to stage generic skills, review them, and activate only after the affected harness session has ended. Starting a new harness discovers installed skills. Do not modify an active harness's authority.

## Select private configuration

The installed environment template lists accepted keys. Keep the instance `.env` owner-owned with mode `0600`, with only actual owner-selected literal settings. Never source it as shell code or store passwords, access keys or tokens in it. Login caches are separate owner-private directories. `--env PATH` selects another private settings file. Projects are never selected globally: each explicit project root has private `.maven/project.json`.

Use existing normal owner login only. If login is missing or expired, stop and ask the owner to perform the normal interactive AWS login for the configured profile and cache. Do not capture credentials, switch identities, or silently log in.

## Review before cloud mutation

1. `maven-memory install prepare --output PRIVATE_PACKAGE_DIR` builds exact runtime, template and SQL artifacts offline from private settings.
2. `maven-memory install plan --package PRIVATE_PACKAGE_DIR --review PRIVATE_REVIEW_FILE --type CREATE` creates a change set, not a deployment. Use UPDATE only for an explicitly reviewed existing stack. Review package hashes, IAM, costs, retention, parameters and the complete change-set review file.
3. Only after exact owner approval, `maven-memory install apply --package PRIVATE_PACKAGE_DIR --review PRIVATE_REVIEW_FILE --confirm-review REVIEW_SHA256` executes that exact reviewed change set and records actual output locators. An unknown outcome requires reconciling the same pending request, never making up new request IDs.
4. `maven-memory install init --confirm-instance INSTANCE_UUID` explicitly initializes SQL/admin control; successful stack creation alone is not readiness. Existing schema upgrades use the reviewed migrate command instead.

For existing infrastructure use `maven-memory install connect --confirm-instance INSTANCE_UUID`; do not provision a replacement. Preserve existing instance and recovery identity.

## Initialize a real project

Select an existing cloud project entity with `maven-project init --root ROOT --project-id UUID --remote REMOTE --accepted-url URL --branch BRANCH`. All Git destination values are explicit and must match the selected checkout. Use `--no-git` instead for a memory-only project. `--check '["command", "argument"]'` registers an optional project-specific check without shell evaluation.

A fresh project uses the same command plus `--create --name NAME --source-origin ORIGIN --source-excerpt EXCERPT --request-key UUID`. Supply real owner provenance and retain the exact project and request UUIDs to reconcile unknown outcomes. Initialization validates the cloud project without requiring an existing global project pointer. Existing differing project settings are preserved and rejected rather than overwritten. `maven-project show --root ROOT` reads the current selection without cloud access.

Generic starter authority is draft material, not active owner authority. Use the installed onboarding commands' help, review every starter and complete dependencies, then obtain exact owner approval through the cloud proposal/challenge/activation flow. A blank deployment never grants permission to work without applicable approved cloud authority. Finish with maven-connect.
