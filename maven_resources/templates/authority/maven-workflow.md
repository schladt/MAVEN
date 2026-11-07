# Explicit multi-session project lifecycle

Normal OMP use is `/skill:maven-start-work`, `/skill:maven-checkpoint` and `/skill:maven-stop-work`; these are thin entrypoints to the installed CLI, not alternate authority. Confirm the absolute project root, reject `/`, and ask about ambiguous or subdirectory roots rather than silently rebinding. Never auto-use the MAVEN installation checkout. Read `maven-project show --root ROOT`; if absent, obtain the owner's project/create and Git/no-Git choices in chat before `maven-project setup --root ROOT --non-interactive`. New UUIDs are generated and durably journaled by setup; preserve original pending receipts. Additional projects are independent of instance setup's saved first project.

Use `maven-start-work --root ROOT --harness NAME` once per new harness session. Record the returned MAVEN session UUID and retain root/project/session binding in that session across cwd changes. Add `--harness-session ID` only when the current harness reliably exposes it; omission is supported and the MAVEN UUID is distinct. Never invent an OMP identity or reuse another chat's binding. Repeated invocation in a bound chat checks status and refreshes context, not another new start. Resume only the exact explicitly requested session. No global current-session file exists, and a transcript is not persistent memory.

Read the prior completed handover, active tasks, blockers, peer sessions and full applicable instructions returned or referenced by start. No handover is normal for a new project. Starting must not pull, merge, rebase, reset or switch branches. Preserve existing dirty work and unrelated local commits.

Keep cloud session memory current while working. All cooperating writers must use the session registry. Before checkpoint or stop, pause registered peer writers, obtain the repository reservation, inspect the exact candidate and give the owner its current-branch destination and change summary. Run only project-configured checks. Do not attribute shared checkout files to a session automatically.

Commit and push only after approval of the exact current action-specific candidate. Asking to checkpoint or stop is not approval of an unseen proposal. Revalidation must catch changed bytes or HEAD; changed candidates require new review. Never silently publish pre-existing local commits. Unknown commit/push outcomes require reconciliation using saved receipts, not blind retry or destructive Git repair. Cancel and recovery preserve evidence and require explicit decisions.

Checkpoint leaves the requesting session active. Stop closes only that session after durable closure; no-Git projects receive memory-only closure without a Git success claim. One stop must not log out shared credentials used by active peer sessions. Report the distinction between invocation-client closure and last-session credential cleanup. Local coordination cannot prevent arbitrary unregistered editor or shell writes.

## Concrete operating sequence

Variables below are actual owner-selected values or values returned by the service,
never literal example identities. `ROOT` is the selected project root; `SESSION` is
the MAVEN UUID. Keep every JSON report and pending receipt private.

```sh
maven-start-work --root "$ROOT" --harness "$HARNESS" --json
maven-workflow status --root "$ROOT" --session "$SESSION" --json
maven-workflow refresh-context --root "$ROOT" --session "$SESSION" --json
```

Add `--harness-session "$HARNESS_SESSION"` only if that identity is reliably exposed
by the current harness. These are manual reference commands, not a requirement for
the owner to assemble identifiers outside the harness.

Stop all relevant writes before attesting quiescence. Begin inspection to obtain the
reservation. A peer-related block retains coordination state; inspect `status` for the
reservation and exact peers rather than creating another session or deleting locks:

```sh
maven-checkpoint --root "$ROOT" --session "$SESSION" --writers-quiescent --json
maven-workflow status --root "$ROOT" --session "$SESSION" --json
```

Each listed peer stops its own writes and acknowledges the exact reservation:

```sh
maven-workflow pause --root "$ROOT" --session "$PEER_SESSION" \
  --reservation "$RESERVATION" --writers-quiescent --json
```

With every required peer paused, record a truthful summary and exact durable record
references, then inspect. Repeat `--memory-ref` for additional current references.
Do not invent a reference or write a replacement summary over a pending approval.

```sh
maven-workflow session-summary --root "$ROOT" --session "$SESSION" \
  --summary "$SUMMARY" --memory-ref "$RECORD_ID:$REVISION" --writers-quiescent --json
maven-checkpoint --root "$ROOT" --session "$SESSION" --writers-quiescent --json
```

Review all candidate paths/digests, current branch, accepted destination, checks,
memory references and proposed effects. Only after the owner approves the exact
returned `proposed.approval_code`, apply without summary/reference flags:

```sh
maven-checkpoint --root "$ROOT" --session "$SESSION" \
  --writers-quiescent --apply "$APPROVAL_CODE" --json
```

Use `maven-stop-work` instead of `maven-checkpoint` in both inspection and apply when
the owner intends to close this session. Do not apply a checkpoint code with stop or
vice versa. No-Git/unchanged candidates can complete memory closure during inspection;
read `git_outcome` and never claim publication when none occurred.

Interpret the report, not merely process completion: exit 0 covers `ready`,
`checkpointed`, `closed` or `cancelled`; `ready` alone does not mean publication.
Exit 3/`approval_required` means review is still required. Exit 2/`blocked` means stop
the affected work and inspect issues. Exit 5/`unsynchronized` may mean a commit or push
already occurred; preserve receipts and reconcile. Exit 4 is an error, not success.

## Explicit cancellation and recovery

For an unexecuted approval, cancel only that approval to retain the active session.
Cancellation without `--approval-only` cancels the selected session instead; it never
proves a remote mutation failed. Cancellation refuses possibly executed receipts.

```sh
maven-workflow cancel --root "$ROOT" --session "$SESSION" --approval-only --json
maven-workflow recover --root "$ROOT" --session "$SESSION" --writers-quiescent --json
```

Every recovery requires truthful `--writers-quiescent`, even without `--apply`:
it can replay the exact pending cloud request and restore receipt state. Use `status`
for read-only local inspection. A peer's pending cloud journal blocks pause/quiescence
until explicitly recovered. After reviewing the retained candidate and explicit owner
authorization, retry only its exact approved operation:

```sh
maven-workflow recover --root "$ROOT" --session "$SESSION" \
  --apply "$APPROVAL_CODE" --writers-quiescent --json
```

If `status` shows crashed writer tokens, establish that those writers truly stopped,
then supply the exact complete token set with repeated `--writer-token "$TOKEN"` to
`recover`, together with `--writers-quiescent`. Never infer death from age or clear
unknown tokens. Resume peers only after the reservation is released:

```sh
maven-workflow resume --root "$ROOT" --session "$PEER_SESSION" --json
```

Explicit `maven-workflow logout --root "$ROOT" --session "$SESSION"` affects shared
local access and reports peers paused locally; it does not close their cloud sessions
or revoke provider-issued tokens. Require fresh owner login before resuming; never
renew or switch identities silently.

Authentication, identity, and validated-response integrity failures request cleanup
of the selected shared local access and pause affected peers. Inspect
`local_access_cleanup.verified` and its issues; an attempted logout is not proof of
cleanup. Preserve session records and pending receipts. Ordinary transient transport
failure does not itself authorize discarding work or renewing credentials.
