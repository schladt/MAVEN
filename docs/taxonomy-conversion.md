# Convert a project’s records

`maven-taxonomy` (from a checkout: `python -m tools.taxonomy_conversion`) accepts exactly one target: `--workspace DIR` for a bound project on this device, or `--database COPY` for a standalone review copy. Both require `--project UUID` and support `scan`, `apply`, and `check`. Neither contacts the cloud. The owner approves one mapping table per project in conversation; no approval artifact, backup, or preliminary dry run is required.

## Convert a bound project after installing the update

1. Start with **one up-to-date device per project**. For Home, use its configured project directory; for another project, use that project's bound directory. Set `dir` to this directory, `project` to its bound UUID, and `mapping` to the private mapping file for that project. If preparing the table, `maven-taxonomy --workspace "$dir" --project "$project" scan` lists what needs conversion.
2. After the owner approves the table, run:

   ```sh
   maven-taxonomy --workspace "$dir" --project "$project" apply "$mapping" &&
     maven-taxonomy --workspace "$dir" --project "$project" check
   ```

   A successful `check` means this **local** store has no findings. If it reports unresolved records, use the listed IDs and reasons to correct the mapping. If interrupted, inspect the current state and rerun; do not replace the database with a review copy.
3. **Only when the owner requests reconciliation**, publish the reviewed conversion and verify that the local and cloud stores match.
4. On each other updated device, **reconcile first** on the owner's request. Take the converted cloud version for an unchanged old ancestor; an erasure always wins. Do not replay the first device's entire table. If that device has divergent or local-only old records, scan its workspace and convert only those IDs with data that preserves its offline edits. Run `check`, then finish the requested reconcile and verify. If nothing remains to convert, no `apply` is needed.

`--workspace` uses the normal project binding, locks, and per-batch write token. Opening the workspace enforces erasures already known to this installation, even for `scan`, `check`, or `--dry-run`. Those commands do not apply mapping changes. `--database` is for review only: it does not consult the installation's erasure records.

## Select a consistent copy

A normal source has layout `PROJECT_ROOT/.maven/reconcile/PROJECT_UUID.sqlite3`. Obtain the canonical, lowercase UUID from that project's known binding (`.maven/project.json`, `project_id`). The review copy can have any filename in a directory owned by the reviewer, mode `0700`. Its existing regular database file must be owned by the reviewer, mode `0600`, with no hard links or symlinks. Keep it somewhere private and out of version control. Do **not** point `--database` at the live store. The command checks the project identity inside SQLite; a matching filename does not count. It will not initialize an absent file, empty database, or missing project.

For example, set `project` and `source_root` for the selected input. This uses SQLite's backup API rather than copying a live database with `cp`; it opens the source with `mode=ro`, never for writing. It creates only an exclusive, private destination. If backup fails, discard that review directory rather than using the interrupted copy.

```sh
umask 077
project=11111111-1111-4111-8111-111111111111
source_root=/path/to/selected/project
review=$(mktemp -d)
copy="$review/snapshot.sqlite3"
python - "$source_root/.maven/reconcile/$project.sqlite3" "$copy" <<'PY'
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
source, dest = (Path(arg).absolute() for arg in sys.argv[1:])
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
    os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
    with closing(sqlite3.connect(dest.as_uri() + '?mode=rw', uri=True)) as dst:
        src.backup(dst)
PY
```

## Commands on a review copy

```sh
python -m tools.taxonomy_conversion --database "$copy" --project "$project" scan
python -m tools.taxonomy_conversion --database "$copy" --project "$project" apply "$review/mapping.json" --dry-run
python -m tools.taxonomy_conversion --database "$copy" --project "$project" apply "$review/mapping.json"
python -m tools.taxonomy_conversion --database "$copy" --project "$project" check
```

- `scan` returns JSON with `project_id`, local `token`, total original and child IDs including erasure markers (`total`), and `findings`: ID, current kind, and failed rules. It reports removed kinds, missing v2 fields, bad dates/limits, orphan plan links, and reference IDs still in the brief's `consult_when_relevant`. It does not classify records or print their bodies or histories. Inspect necessary wording privately when preparing the table. Missing task `added_on`, decision `rationale`, or outcome `occurred_on`/`observed` must come from evidence, not generated defaults.
- `check` performs the same scan. Exit status **0** means clean; **1** means findings. `scan` exits 0 even with findings. Exit **2** means invalid input or a failed operation; a JSON error goes to stderr for conversion/store errors. No completion is implied on error.
- `apply` reports `dry_run`, `project_id`, `token`, planned/applied `batches`, changed record IDs (`changes`), and IDs already at the requested result (`unchanged`). No payloads, history, row keys or old text are printed. `--dry-run` performs the same preflight without applying the mapping. Optionally pass `--expected-token "$token"` **after** the `apply` subcommand to refuse a different current view. Without it, the tool reads the current token and checks it on every subsequent batch. The token is not part of the mapping and is not an approval.
- Mapping files contain exact record data: keep them private (e.g. under `$review` with mode `0600`). Treat derived tables, any manually captured content and the original snapshot as sensitive too. Applying an erasure to the copy does not erase a live store, other copies, past backups or the mapping. Do not keep a new copy of the erased text in working notes.

## Mapping format

UTF-8 JSON: one object containing **only** `project_id` and `rows` (an array). UUIDs are exact canonical lowercase strings. Object members must be unique; unrelated or misspelled fields fail. Each row has exactly one of the four forms below. This example uses fictitious, pre-existing source and survivor IDs; replace them and the proposed data with the reviewed table for the selected copy.

```json
{
  "project_id": "11111111-1111-4111-8111-111111111111",
  "rows": [
    {
      "op": "retype", "id": "22222222-2222-4222-8222-222222222222",
      "kind": "task", "data": {"title": "Measure", "state": "open", "added_on": "2026-11-27"}
    },
    {
      "op": "split", "id": "33333333-3333-4333-8333-333333333333",
      "kind": "entity", "data": {"type": "instrument", "name": "Bench meter"},
      "children": [
        {"key": "calibration", "kind": "reference", "data": {
          "name": "Meter calibration", "read_when": "Before using this meter",
          "state": "active", "as_of": "2026-11-27", "body": "Consult the calibration sheet."
        }}
      ]
    },
    {
      "op": "merge", "id": "44444444-4444-4444-8444-444444444444",
      "survivor": {"id": "55555555-5555-4555-8555-555555555555", "kind": "entity",
                   "data": {"type": "instrument", "name": "Fixture A", "location": "Lab"}}
    },
    "ERASE 66666666-6666-4666-8666-666666666666"
  ]
}
```

1. **Retype**: `op`, existing `id`, strict `kind`, complete `data`. Replaces the current version **at this ID**. This also corrects the fields of an unchanged kind. Prior versions and change reasons remain on that ID. It is a replacement object, not a patch.
2. **Split**: `op`, existing `id`, primary `kind` and `data`, and a nonempty `children` list. The primary remains at its original ID with its history. A child has `key` (unique nonblank stable text within this split), `kind`, and `data`. Its ID is UUIDv5 with namespace the source UUID and UTF-8 name `taxonomy-split:` followed by the exact key. Reordering children never changes their IDs. A new child starts with empty history. Open-field kinds gain `source_id` equal to the primary ID. For closed `reference` data the tool appends `\n\nSource record: <ID>` to `body`; for `skill` it appends ` Source record: <ID>` to `when_to_use`. Supply the unadorned fields in the table. The resulting fields, **including the link**, must fit their normal limits. A conflicting `source_id` or different content at a derived child ID is an error, not permission to overwrite it.
3. **Merge**: `op`, existing source `id`, `survivor` (`id`, strict `kind`, exact complete `data`) and optional `source`. The survivor must already exist at a **different** ID. Its prior history stays on the survivor; no source history is grafted. By default, the source becomes an `entity` with `{"type":"cross-reference","name":"Merged record","state":"retired","survivor_id":"<survivor UUID>"}`; it is not an active reference, and its complete history stays on the source. To choose a different lossless retype, supply `"source":{"kind":...,"data":...}`. To erase after saving the survivor, use **only** `"source":"ERASE <exact source UUID>"`. A survivor needed by several merge rows must specify identical kind and data in every row; it is written once before its sources are disposed. All other overlapping destinations, including merge chains, are rejected rather than resolved by row order.
4. **Erase**: the entire row is the JSON string `"ERASE <ID>"`: uppercase, one ASCII space, exact existing UUID, no trailing characters. The current record becomes the existing content-free terminal marker with no history. Already erased exact IDs are unchanged. A label like `duplicate`, a title match, a missing row, or a merge alone **never** erases. Erasure wins; no mapping can restore a terminal marker.

The tool will not author a `profile`, regardless of project UUID or filename; owner profile content is written separately through the configured Home path. It never supplies a date, invents an observation from a started action, or guesses a classification. Workspace key/value settings belong in `workspace-metadata` entities at their existing IDs, not preferences; operation receipts belong in `workspace-operation` entities and archived non-task plans in `workspace-archived-plan` entities. Keep `read_first` and file/document links in the brief. If a `consult_when_relevant` entry points to a record becoming a reference, include the corrected brief as an ordinary retype row; the tool does not rewrite an unrelated brief behind the reviewer's back. It recognizes a direct UUID/`record:<UUID>` string or such a value at `id`, `record_id`, or `link` in an entry.

## Limits, retries and certification

All proposed versions are preflighted before the first batch: strict v2 fields and reserved IDs, Unicode limits, secret restrictions, and the **512 KiB UTF-8 envelope including all preserved history**. Oversized proposals are identified by record ID and sizes, never truncated. Reduce proposed wording, summarize into multiple records with a split, or deliberately maintain a private file and point to it from a reviewed record. Old data stays on the same ID even when split, so a near-full original envelope can still be too large to revise. No erasure is inferred as a workaround.

Batching respects 50 changes, the 1 MiB apply request budget, and task/plan dependencies. These are transaction limits, not limits on the number of records in a project. A task may be introduced in the same transaction as a plan; a live plan prevents retyping its task away. An explicit erasure is not blocked by old plans. `check` will report surviving orphan plans for a session to resolve. Partial mappings may leave unrelated legacy records unresolved: only `check` certifies the entire project.

Each batch commits atomically, but **the mapping as a whole is not one transaction**. On interruption or stale-token failure, leave the selected store in place. Inspect intervening edits, then rerun the same mapping without a stale token. Current exact matches are skipped, including already-created children and already-erased exact IDs. If the mapped wording is now historical rather than current, the tool refuses to overwrite the intervening edit or erase a merge source while its survivor differs. Resolve the conflicting row against the current record. No journal, digest gate or approval step is involved. Use a fresh `check` after the last batch.

Focused repository check: `python -m unittest discover -s tests -p 'test_taxonomy_conversion.py'` (includes a real populated synthetic SQLite backup, module CLI round trip, interrupted batches, history and marker assertions).
