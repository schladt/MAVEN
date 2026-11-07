---
name: maven-checkpoint
description: Preserve this bound MAVEN session's durable work and inspect an exact checkpoint for owner approval without closing the session.
---

# Checkpoint the bound session

Use the installed lifecycle commands and the absolute root and MAVEN session UUID retained by maven-start-work for **this** harness session. In OMP the owner invokes `/skill:maven-checkpoint`. Do not derive the root from a changed cwd, select the newest local session, reuse a peer's UUID, or start a replacement session. If the binding is missing or ambiguous, establish the exact owner-selected binding before proceeding. Cloud memory, not the OMP transcript or cached instructions, is durable authority.

A request to checkpoint authorizes preparation within the existing task scope, **not** an unseen commit/push candidate. Do not infer exact approval from invoking this skill. Keep private receipts and summaries out of tracked files. Never run raw Git publication to bypass the managed workflow.

## Prepare truthful memory and quiescence

1. Inspect `maven-workflow status --root ROOT --session SESSION --json`. Preserve existing reservations and approval/unknown-outcome receipts. Do not overwrite a pending approval with a fresh summary or inspection; use the explicit cancellation/recovery paths below.
2. Refresh current applicable authority with `maven-workflow refresh-context --root ROOT --session SESSION --json` when the session permits it. Read the full returned instructions, dependencies, handover, tasks, blockers and applicable references, including all ranges of truncated captured output. A failed authority load blocks work; do not use a cached body as fallback.
3. Finish authorized durable project records before freezing writers. Record actual results, checks actually exercised, unfinished tasks, blockers and next steps through the existing memory APIs with the same root/session, real provenance, expected revisions and stable request keys. Use `maven-memory record put PRIVATE_RECORD_FILE --root ROOT --session SESSION` for permitted ordinary records; managed session/checkpoint records belong to the lifecycle commands. Keep actual returned `UUID:REVISION` references, never invented references or a claim that local text was saved to cloud.
4. Stop relevant repository and session-memory writes, including delegated writers, before attesting `--writers-quiescent`. Coordinate affected peers rather than claiming their writers stopped. This flag is a factual assertion, not an instruction to stop background jobs or permission to kill them. Unregistered writers can still race the workflow.

Begin inspection to reserve the checkout and learn which peers must acknowledge a pause:

```sh
maven-checkpoint --root ROOT --session SESSION --writers-quiescent --json
```

A peer/freshness block may be expected at this stage, but it is not success. Preserve the returned reservation. Each affected peer must actually stop its writes and acknowledge that exact reservation under its own UUID:

```sh
maven-workflow pause --root ROOT --session PEER_SESSION \
  --reservation RESERVATION --writers-quiescent --json
```

Do not attest on a peer's behalf without establishing its actual quiescence. A peer with a pending cloud journal must explicitly recover it before pause. Once all required writers are paused, summarize and inspect the current candidate:

```sh
maven-checkpoint --root ROOT --session SESSION --writers-quiescent \
  --summary SUMMARY --memory-ref RECORD_UUID:REVISION --json
```

Repeat `--memory-ref` for each relevant durable work reference; omit it only when there are genuinely no additional records to reference. The summary must truthfully describe this candidate and reservation, not a stale earlier state. If the first inspection already returned an approval candidate, do not overwrite it: review that exact candidate, or explicitly cancel the unexecuted approval before replacing its summary.

## Review before action-specific apply

Read the complete report and show the owner the exact `proposed.approval_code`, action, candidate paths/digest, current branch/base HEAD, accepted remote/destination, commit message, configured check results, durable memory references and proposed commit/push effects. Do not attribute every shared-checkout change to this session. Run only the project-configured checks through the workflow. Existing local commits must not be silently published; a destination/HEAD mismatch needs a separate owner decision, not reset, rebase, force-push or invented receipts.

Only after the owner approves this **exact current checkpoint candidate**, and writers remain quiescent:

```sh
maven-checkpoint --root ROOT --session SESSION --writers-quiescent \
  --apply APPROVAL_CODE --json
```

Do not add `--summary` or `--memory-ref` to apply. Changed bytes, HEAD, destination, policy, authority or references invalidate approval and require fresh review. A stop-work code cannot be used for checkpoint, or vice versa. An approval code is a candidate identifier, not proof that the owner approved it.

No-Git or unchanged work can finish a memory checkpoint during inspection without a Git approval candidate. Report `git_outcome` accurately: `not_applicable` or `unchanged` is not a commit/push. Confirm `checkpointed` and actual durable receipts, not merely exit 0 or `ready`. Exit 3/`approval_required` still needs review; exit 2/`blocked` stops affected work; exit 5/`unsynchronized` can mean an external operation already occurred; exit 4 is not success. A checkpoint retains this session's binding and active state; it does not close peers or log out shared credentials.

## Recover without losing evidence

For read-only local inspection use `maven-workflow status --root ROOT --session SESSION --json`. Preserve the original UUID, request keys, candidate and runtime receipts after interruption or uncertainty. Never delete locks/journals, mint another session, or retry a commit/push blindly.

For an explicitly cancelled **unexecuted** approval, use `maven-workflow cancel --root ROOT --session SESSION --approval-only --json`. Omitting `--approval-only` cancels the session instead. Cancellation never proves an external mutation failed and refuses possibly executed receipts.

After establishing actual writer quiescence and selecting explicit recovery, use `maven-workflow recover --root ROOT --session SESSION --writers-quiescent --json`. This can replay the original pending cloud mutation even without `--apply`; it is not a read-only status command. If status reports interrupted writer tokens, confirm those writers stopped and pass the **complete exact** token set with repeated `--writer-token TOKEN`. Never infer writer death from age. Only for the retained exact owner-approved operation use recovery with `--apply APPROVAL_CODE`; do not invent fresh approval or switch actions. Read the recovery result before taking another action.

After the reservation is released, peers resume explicitly with `maven-workflow resume --root ROOT --session PEER_SESSION --json`. Do not resume them while a reservation or unresolved receipt blocks them. Closing a CLI invocation only closes its client. Do not use logout as routine checkpoint cleanup: shared access can be needed by peers in other projects. Authentication/identity/integrity failures may request shared-access cleanup; inspect `local_access_cleanup.verified`, issues and affected peers, and require the owner's explicit login before resuming. Transient transport failures preserve state; they do not authorize login, identity switching or discarding work.
