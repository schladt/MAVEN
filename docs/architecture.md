# Architecture and storage

The harness runs the agent; its local `maven-mcp` process provides deterministic helpers over stdio. There is no MAVEN model loop or mandatory background service. The [reconcile procedure](../maven_resources/templates/skills/maven/references/reconcile.md) tells the agent how to reason about records. The helpers select the project, read inventories, perform conditional writes and verify the result. They do not decide what a proposal means or grant permission to act on it.

## Storage and identity

| Location | What it contains |
| --- | --- |
| Installation home (`MAVEN_HOME`, default `~/.maven`) | Installed software, selected instance settings and private administrative reviews. AWS sign-in caches are separate from `.env`. |
| `<root>/.maven/project.json` | Reviewed folder and project binding; schema 2 pins the signed cloud identity. Schema 1 does not authorize reconciliation. |
| Installation `home.json` | Reviewed, per-machine Nexus selection. Supplies global preferences alongside a tracked project and serves as the turn destination in genuinely untracked directories. Never a recovery path for a broken binding. |
| `<root>/.maven/reconcile/<project-id>.sqlite3` | Durable local project records and history, including the orientation brief and skill index derived from records. Exclude this private directory from Git. |
| Selected AWS instance | Project records and their histories accessed through the signed service. Access to the deployed reconcile capability requires matching service and schema; the agent does not connect directly to the database. |

Stable project and record IDs distinguish projects and versions of the same record across machines. They do not identify a running assistant. No per-machine sync bookmark, global current conversation, peer lease or exit receipt is required.

## Consistency

Each record has a write timestamp; an unchanged version retains it. Its event date, if relevant, belongs in the content. Conditional writes depend on observed **whole-project views**, including records outside the proposed change set. The cloud checks its view in the transaction that publishes a batch; the local store also checks its view before changing records. Concurrent additions under a different ID therefore matter, and equal timestamps do not resolve disagreements.

The two stores are **not** one distributed transaction. On interruption, keep confirmed effects and read actual state before continuing; report convergence only after verification. Records missing from one side are not deletions. A terminal erasure marker retains only record ID, erased status and write time; the prior managed wording is removed. Ordinary corrections append history instead. See [Security](security.md) for the limits of erasure.

## Rollout and authority

Installing a skill, enabling a folder, updating local software, migrating the database and deploying the matching signed service are distinct effects. None migrates legacy records, deletes data or grants approval for an external action. An existing schema-1 **folder binding** needs an explicit reviewed project switch, independently of any service upgrade. Do not bypass a blocked cloud check with another account, a direct database call or credentials found in the environment.

Project skill records describe work; they are not signed grants. Cloud policy activation and consumption of a secret grant remain separately authorized. Code, local exercises and simulated storage are not proof of live deployment, migration or interoperability. See [installation and update](getting-started.md#existing-instance-upgrade) and [integration boundaries](integrations.md).
