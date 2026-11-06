# Project and session workflow

## Bind an explicit project

After cloud initialization, select an existing cloud project and its root:

```sh
maven-project init --root "$PROJECT_ROOT" --project-id "$PROJECT_ID" \
  --remote origin --accepted-url "$REPOSITORY_URL" --branch "$BRANCH"
maven-project show --root "$PROJECT_ROOT"
```

For a project without Git, use `--no-git` instead of the three Git destination flags.
The accepted URL and selected remote must describe the owner's intended repository.
Destination URLs must not embed credentials, signed queries or fragments. HTTP(S)
userinfo and URL passwords are rejected; use an owner-managed SSH or credential
mechanism instead. Ordinary HTTPS, SSH usernames/SCP syntax, explicit local paths
and file URLs are supported.
The workflow uses the current branch; there is no hard-coded canonical `main` branch
or automatic branch switch. Register optional project checks with repeated
`--check '["program", "argument"]'`; MAVEN's own test suite is not an implicit check
for every unrelated project.
Before first publication, the configured remote branch must already exist and match
local HEAD. Establish that destination through a separately approved Git operation;
the workflow does not create a missing remote branch or silently publish local history.

To create a new cloud project through initialization, additionally use `--create`,
`--name`, `--source-origin`, `--source-excerpt` and `--request-key`. Supply real owner
authorization and a fresh canonical UUID request key, then keep that key for any
reconciliation. Do not create a second project to repair an uncertain first outcome.
Existing project files are retained rather than silently rebound.

## Start or resume

```sh
maven-start-work --root "$PROJECT_ROOT" --harness codex --harness-session "$HARNESS_SESSION"
# Resume only a specific, previously selected session:
maven-start-work --root "$PROJECT_ROOT" --resume "$SESSION_ID"
```

Use the returned MAVEN UUID as `SESSION_ID`; a harness session ID is only an optional
association. Start loads the project context, previous completed handover, tasks,
blockers and peer sessions. No previous handover is normal for a fresh project.
Missing access or authority is not the same as an empty project. Complete all required
instruction reads before work. Start does not pull, merge, rebase, reset or switch
branches, and external commits do not require invented MAVEN receipts.

Older completed sessions without a completion timestamp are ordered by their cloud
record timestamp, not by their randomly assigned session UUID.

Keep cloud session records current as work proceeds. Use `maven-workflow
refresh-context --help` and `maven-workflow session-summary --help` for explicit
context refresh and summary management; those operations target the selected session,
not a global current project.
Project-bound record writes use `maven-memory record put "$PRIVATE_RECORD_FILE"
--root "$PROJECT_ROOT" --session "$SESSION_ID"`; claims similarly require both
selectors and a matching session in the request. Use managed lifecycle commands for
project session/checkpoint records, not raw record writes. `maven-memory skill list`
reads the cloud catalog without a local cache; `skill fetch --slug "$SLUG" --root
"$PROJECT_ROOT" --session "$SESSION_ID"` uses the selected session's private cache.

## Inspect, quiesce and approve

Stop active writes before attesting `--writers-quiescent`. It means the relevant
cooperating writers are actually quiescent, not that every process or the whole cloud
instance has been stopped.

```sh
maven-checkpoint --root "$PROJECT_ROOT" --session "$SESSION_ID" --writers-quiescent
```

Inspection reserves the repository and returns the reservation and any peers that
must acknowledge a pause. Each affected peer stops its work and explicitly pauses:

```sh
maven-workflow pause --root "$PROJECT_ROOT" --session "$PEER_SESSION_ID" \
  --reservation "$RESERVATION_ID" --writers-quiescent
```

Then the requester repeats inspection with its truthful summary and exact durable
memory reference as required by the response:

```sh
maven-checkpoint --root "$PROJECT_ROOT" --session "$SESSION_ID" --writers-quiescent \
  --summary "$SUMMARY" --memory-ref "$RECORD_ID:$REVISION"
```

Review the returned candidate, selected current branch, destination, existing local
commits, checks and proposed effects. Only after explicit approval of that exact
candidate, apply the returned code without rewriting its summary or references:

```sh
maven-checkpoint --root "$PROJECT_ROOT" --session "$SESSION_ID" \
  --writers-quiescent --apply "$APPROVAL_CODE"
```

Do not combine `--apply` with `--summary` or `--memory-ref`.
Changing bytes, HEAD, memory reference or destination invalidates approval and requires
reinspection. Do not silently publish pre-existing local commits. If the workflow
blocks on them, reconcile their disposition with the owner rather than resetting,
rebasing or force-pushing.

A checkpoint keeps the requester active. To close only the requesting session, use
`maven-stop-work` with the same inspect/review/apply sequence instead. A no-Git project
can close durable memory honestly without claiming a commit or push. Completion is
supported by cloud and Git receipts, not by a local success-shaped summary alone.

## Shared checkout and recovery

All cooperating write paths must use the session registry. New writers are rejected
while a repository reservation is active. Per-session receipts prevent one session
from overwriting another's state; they do not attribute shared files to particular
sessions. Peer HEAD advancement is reconciled without discarding session memory.

There is no automatic stale-lock takeover. Inspect `maven-workflow cancel --help`,
`resume --help` and `recover --help`, and the blocked command's report before choosing
an explicit recovery action. Preserve unknown-outcome receipts and exact request keys.
`maven-workflow status --root "$PROJECT_ROOT" --session "$SESSION_ID"` exposes current
reservation and writer tokens. Recovery of crashed writers requires the exact complete
token set with repeated `--writer-token TOKEN` and truthful `--writers-quiescent`.
Every `recover` invocation requires `--writers-quiescent`, even without `--apply`,
because exact cloud reconciliation can replay a pending mutation. `status` is the
read-only local inspection command. A peer's pending journal must be recovered before
it can acknowledge quiescence.
`cancel --approval-only` releases an approval reservation while retaining the session;
it is not a claim that a previously attempted external operation failed.
Do not clear locks, delete runtime directories, reset Git or retry publication blindly.
Cancel/release and peer resume are explicit steps, not proof that an attempted external
mutation failed. An unregistered editor or a different host can still race the workflow;
revalidation is necessary but cannot make arbitrary concurrent edits safe.

Closing an invocation releases its client. Stopping one project session must not log
out credentials still shared by active peers, including peers in another project.
Last-session cleanup can log out shared local access. Explicit
`maven-workflow logout --root "$PROJECT_ROOT" --session "$SESSION_ID"` removes the
selected shared local login/cache access and reports affected peer sessions, marking
them paused/access-revoked locally. It neither closes their cloud memory sessions nor
claims provider-side token revocation. Resume only after the owner's explicit fresh
login; no automatic renewal occurs. Authentication, identity, and validated-response
integrity failures also request shared-access cleanup and report affected sessions.
Transient transport failures preserve session state and receipts for explicit recovery.
