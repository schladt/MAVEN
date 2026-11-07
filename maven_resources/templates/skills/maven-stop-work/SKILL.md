---
name: maven-stop-work
description: Close only this bound MAVEN session with a durable handover, exact publication approval when needed, and peer-aware access cleanup.
---

# Stop this session's work

In OMP the owner invokes `/skill:maven-stop-work`. Use installed commands with the exact absolute root and MAVEN UUID bound by maven-start-work to **this** harness session, even after cwd changes. Do not reuse a peer's session, infer the newest session, or create a new one to close the old one. Missing/ambiguous binding requires establishing the owner's exact intended session first. This procedure closes that session, not the entire project, every harness, or all cloud access.

Read maven-checkpoint for the shared writer coordination, complete authority loading, durable memory and recovery requirements, then perform the **stop-work** sequence below. Do not run checkpoint first or reuse its approval code. Asking to stop is not approval of an unseen commit/push candidate. Never bypass managed approval with raw Git, publish pre-existing local commits, or claim completion from a local summary alone.

## Prepare the handover and reserve

Inspect `maven-workflow status --root ROOT --session SESSION --json`. If approval or unknown-outcome receipts exist, preserve them and handle them explicitly before changing a summary. Do not overwrite or delete them. Refresh context with `maven-workflow refresh-context --root ROOT --session SESSION --json` when permitted; read the entire current instruction bodies, dependencies, tasks, blockers, prior handover and applicable references. Read all ranges of captured output when truncated; no cached instructions or OMP transcript substitutes for cloud authority or durable memory.

Save authorized factual work/task/blocker records with real provenance, revision guards and stable requests through existing session-bound memory APIs. A handover states completed work, actual checks/results, unresolved risks, pending operations and next steps honestly. Retain returned exact durable `UUID:REVISION` references. Use lifecycle commands for managed session/checkpoint records, not raw record writes. Finish these writes before quiescing; references must match the final candidate.

Stop the requester and delegated writers, then truthfully attest quiescence to begin inspection and obtain the reservation/required peer list:

```sh
maven-stop-work --root ROOT --session SESSION --writers-quiescent --json
```

Coordinate each affected peer to stop its actual repository and session-memory writes and acknowledge the exact reservation under its own session:

```sh
maven-workflow pause --root ROOT --session PEER_SESSION \
  --reservation RESERVATION --writers-quiescent --json
```

Do not claim peers stopped merely because a pause was requested. Recover any pending peer journal explicitly before acknowledging quiescence. With all required peers paused and no pending approval to overwrite, supply the truthful final handover and current durable references:

```sh
maven-stop-work --root ROOT --session SESSION --writers-quiescent \
  --summary SUMMARY --memory-ref RECORD_UUID:REVISION --json
```

Repeat `--memory-ref` for additional real references; omit only if none exist. A stale summary requires a fresh one under the current reservation. If an earlier inspection already returned an exact candidate, review it or explicitly cancel that unexecuted approval before changing memory. Checkpoint and stop inspection are not read-only: no-Git/unchanged work can complete durable memory closure during inspection.

## Approve only the exact stop candidate

Read and present the complete returned candidate: action `stop-work`, `proposed.approval_code`, paths/digest, branch/base HEAD, accepted remote/destination, existing commits, configured checks, commit message, durable memory references and proposed effects. Shared files are not automatically attributable to this session. Destination/HEAD mismatch blocks publication; reconcile pre-existing commits with the owner separately without destructive Git repair. Only project-configured checks run through the workflow.

After explicit owner approval of this exact candidate, with writers still quiescent:

```sh
maven-stop-work --root ROOT --session SESSION --writers-quiescent \
  --apply APPROVAL_CODE --json
```

Never combine apply with summary/reference flags. A checkpoint code cannot authorize stop, or vice versa. Changed candidate, HEAD, policy, destination, authority or references require new review; the returned code itself is not owner approval.

Confirm `closed` and actual durable cloud/Git receipts. `ready` or exit 0 alone is not proof of closure/publication; `approval_required`/exit 3 still needs review; `blocked`/exit 2 or error/exit 4 is not success; `unsynchronized`/exit 5 can mean Git already changed. Read `git_outcome`: `not_applicable` and `unchanged` must not be described as commit/push. If cloud closure is confirmed but cleanup fails, report those separate facts, not a fully successful stop or an invented rollback. Retire this harness session's active binding only on verified closure; retain exact identifiers for cleanup/recovery evidence. Later work starts a new session unless explicitly resuming an eligible active one.

## Preserve uncertain outcomes and peers

Use `maven-workflow status --root ROOT --session SESSION --json` for read-only local inspection. An explicitly unexecuted approval can be cancelled with `maven-workflow cancel --root ROOT --session SESSION --approval-only --json`; without that flag cancellation targets the session. Cancellation is not evidence that a remote operation failed and refuses possibly executed receipts.

For explicit recovery, establish actual quiescence and run `maven-workflow recover --root ROOT --session SESSION --writers-quiescent --json`. It may replay the exact pending cloud request even without `--apply`. Supply repeated `--writer-token TOKEN` for the complete exact interrupted-writer token set reported by status only after confirming those writers stopped. If recovery needs continuation of a retained exact approved operation, use `--apply APPROVAL_CODE` on recover only with that approval; never mint fresh requests or switch its action. Preserve all original UUIDs, receipts and request keys. Never delete runtime evidence/locks, infer dead writers from age, reset Git or retry publication blindly.

After reservation release, peers resume explicitly using `maven-workflow resume --root ROOT --session PEER_SESSION --json`; a stop closes only the requester. Inspect `local_access_cleanup`, its `verified` field, issues and affected peers. The workflow must retain shared credentials while active peers need them, including another project's peers; last-session cleanup may log out local access. Do not force logout to make a stop look clean. Closing an invocation only releases its client.

Explicit `maven-workflow logout --root ROOT --session SESSION --json` is a separate shared-access action: it can pause/access-revoke other local sessions, not close their cloud sessions or revoke provider-issued tokens. Explain that effect and obtain the applicable authorization before using it. Authentication/identity/integrity failures can also request cleanup; report its actual verified result. Transient transport failures preserve receipts. After access removal or expiry, only the owner's explicit normal login permits resumption; never auto-renew, elevate or switch identities.
