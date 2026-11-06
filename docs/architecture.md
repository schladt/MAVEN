# Architecture and storage

MAVEN separates installed code, private local operating state, and authoritative
cloud records. It is a CLI/client system, not a daemon or model provider.

| Location | Purpose | Authority |
| --- | --- | --- |
| Instance `venv/` and installed `maven_resources` | Executables, policy, infrastructure, generic starter proposals and bootstrap skills | Code and reviewed defaults; not active owner instructions |
| Instance `.env` | Literal private endpoint, named profile, identity and integration settings | Connection configuration, not durable project memory |
| Project `.maven/project.json` | Explicit cloud project UUID, canonical root and accepted Git destination/checks | Local project selection |
| Project `.maven/runtime/sessions/<UUID>/` | Session-specific receipts and reconciliation evidence | Local evidence, not an alternate handover store |
| Instance/project runtime coordination files | Locks, registered writers and durable reservations | Local concurrency control; no timeout takeover |
| Project `.maven/sensitive/` | Owner-approved preserved originals | Private artifacts, never automatically operative instructions |
| Owner-selected cloud service | Versioned instructions, projects, tasks, blockers, sessions and handovers | Durable MAVEN authority |

The default instance is `~/.maven`. `MAVEN_HOME` explicitly selects another instance;
installation with `--home` and subsequent use must select the same location. Code
never uses its own installed directory as a project root. There is no global current
project. Lifecycle commands require an explicit root, and later actions bind to the
returned MAVEN session UUID. A harness name/session ID is optional additional identity,
not a replacement for the MAVEN UUID.

The cloud service enforces scoped access, instruction approval, revision guards and
request-key reconciliation. Instructions have distinct proposal and active states.
An index discovers a page; its complete current body and dependency closure are
required before use. The packaged authority catalog is a dependency-complete proposal
set authored for a new owner. It contains no live owner credentials or permissions.

The workflow coordinates cooperating writers sharing a checkout. A checkpoint obtains
one repository reservation, requires peer quiescence, snapshots an exact candidate and
revalidates before mutation. Different sessions keep separate receipts. No automatic
file-to-session ownership is inferred. An arbitrary editor can still change files;
revalidation detects candidate changes but is not an operating-system write sandbox.

Cloud authority remains authoritative when local processes end. Local originals and
receipts may be necessary to recover a partial operation, so do not remove `.maven`
or the instance runtime directory to clear an unexplained failure. A lost cloud
connection is not permission to continue from a stale transcript or cached instruction.

Optional Google modules are imported only by Google callers. Core project, memory,
secrets, workspace and workflow commands do not require Google consent or packages.
