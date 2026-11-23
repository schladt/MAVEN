# Project workflow

For ordinary work, follow **Every turn** in the [installed MAVEN skill](../maven_resources/templates/skills/maven/SKILL.md). No separate start, checkpoint, finish or recovery ritual. The local reply receipt does not close a session or synchronize a project.

## Choose the project

Enable the intended root through the reviewed project setup. Selection follows the nearest enabled project, not the repository name, the last conversation, or another machine's choice. A schema-2 binding records the reviewed signed cloud identity. A schema-1 binding needs an explicit, reviewed switch, not an automatic upgrade or a hand edit. To share a project across machines, select the same instance and cloud project ID on each; local paths can differ.

Outside an enabled project, use configured **Nexus Home** for local turn capture. Inside a valid project, consult its brief and preferences plus Nexus preferences before acting; project work stays in the project. If either binding is damaged, report the affected scope. Never redirect a broken project to Home. “Turn off MAVEN for this session” disables automatic memory for this conversation only.

## What to save

Record what the next agent needs to avoid repeating work or a mistake. Keep an accepted decision distinct from a suggestion, an attempted action distinct from an observed outcome, and “implemented” distinct from “tested” and “deployed.” Update an existing record rather than duplicating it. Use one orientation brief with evidence links, annotated **Read first** and **Consult when relevant** references, open questions, and the next useful step. Do not log every exchange.

Each reply ends with the actual local consultation/capture status by project. File changes qualify even when the owner never mentioned memory; summarize them, don't store their bytes. No-change decisions create no project record. For a new standing preference ask whether it belongs in the project or Nexus unless the owner already specified. Reconcile only when explicitly requested.

A project skill describes **when to use a procedure** and either carries steps and a verification method or points to the repository-owned `SKILL.md`. The latter syncs only its description and link; Git carries the file. A decision can invalidate an unchanged procedure. Flag the file for its owner rather than silently rewriting it or inventing a replacement.

## Reconcile and interrupt

“Reconcile this project” compares the whole selected project, including unchanged decisions and records present on only one side. A fresh task and an older, differently named risk can conflict. Preserve compatible evidence. Ask the owner only where a new decision is needed, and finish unrelated changes. A timestamp says **when a version was written**, not which contributor is right.

**Saved locally** means the local write completed. **Synced** means both stores were verified to match at the reported point; it does not settle an open question or guarantee no later edits. If either view changes before a write, reread and reconsider the affected conclusions. If a reply is lost, inspect actual state on the next reconcile rather than submitting a duplicate. An outage does not undo confirmed local writes.

An absent record is not a deletion. Cancel or supersede it explicitly. A bad merge can be corrected from history as a new write without rolling back the project. Explicit, owner-requested erasure removes the selected record's managed content and history; its content-free marker defeats stale offline edits at reconciliation. See [privacy and retention](security.md#data-and-external-models).

## Optional Git publication

Memory does not need Git. Only ask to configure or publish Git when that external effect is intended. Review the actual current branch, remote and accepted push URL; never embed credentials in the URL. Publication requires the destination already to equal the reviewed HEAD. Before publishing, review the exact candidate: paths and bytes, commit message, HEAD and destination, configured checks and their results, and proposed effects. Actual owner approval and confirmation that relevant writers have stopped apply to **that candidate only**. Changed bytes, branch, destination or check results invalidate the review. Approving a candidate with failed checks does not make it integration-ready. A saved decision, a reconcile or a previously approved push grants no new publication approval.

Git's local publication lock coordinates participating operations, not arbitrary editors or other machines. Do not infer quiescence. Do not silently pull, rebase, reset, switch branches or publish pre-existing commits. After an uncertain commit or push, inspect the original receipt and exact repository and remote state rather than retrying blindly. Never claim a failed or unrun check passed. Publication uncertainty is not a reason to prevent offline memory work or harness exit.

For installation, reconnection or removal see [Getting started](getting-started.md).
