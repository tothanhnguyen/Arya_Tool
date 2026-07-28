# Operations Runbook

## Health endpoints

The application exposes two separate signals after the health router is
included by the application factory:

- `GET /health/live` checks only that the web process can answer.
- `GET /health/ready` checks database connectivity and schema, configured
  storage, and scheduler state.

Readiness returns HTTP 200 only when every check is ready. HTTP 503 means the
process stays live but should not receive production traffic. Responses use
stable codes and numeric indicators only. They do not include connection
URLs, credentials, exception text, bucket names, paths, or object names.

Scheduler indicators are snapshots:

- `backlog`: queued and retry jobs.
- `retry`: jobs currently waiting for retry.
- `lag_seconds`: age of the oldest due job.
- `stale_running`: running jobs older than the recovery threshold.

External monitoring should alert on readiness HTTP 503 and track changes in
these counters. A single snapshot cannot calculate a retry growth rate.

## SQLite backup

The backup helper uses SQLite's online backup API, so it includes a consistent
snapshot even when WAL mode is active. It validates the copied database,
calculates SHA-256, writes a JSON manifest, and refuses to overwrite either
output file.

```bash
python -m laplace.ops.backup sqlite \
  arya-tool.db \
  backups/arya-tool-2026-07-28.sqlite
```

Verify before retention, transfer, or restore:

```bash
python -m laplace.ops.backup verify-sqlite \
  backups/arya-tool-2026-07-28.sqlite
```

The manifest is stored beside the backup with the suffix
`.manifest.json`. Keep both files together. The helper creates private files,
but the backup location still needs filesystem encryption, access control,
off-host replication, and a retention policy.

Restore drill:

1. Stop write traffic and both schedulers.
2. Verify the backup and manifest.
3. Preserve the current database under a new name; do not overwrite it.
4. Restore into a separate path and run SQLite integrity checks.
5. Start a staging instance against the restored path.
6. Reconcile row counts, social jobs, affiliate totals, and media references.
7. Record the operator, timestamp, hashes, and drill result.

Database backup protects metadata only. Local media and Supabase Storage
objects require a separate backup and reconciliation.

Artifact upload persistence failures deliberately leave a content-addressed
object for later reconciliation instead of deleting it immediately. Immediate
compensation can race another idempotent upload that has already verified the
same object and is about to commit metadata. Treat unreferenced-object cleanup
as a delayed, audited operation.

## Supabase backup plan

The planning command prints an inert JSON plan. It never launches the
Supabase CLI and never contacts or mutates a remote project.

```bash
python -m laplace.ops.backup plan-supabase \
  backups/supabase-database.dump \
  backups/supabase-storage
```

The database command in the plan uses a linked project's existing local CLI
authentication without placing a password, URL, or secret key in argv.
Review and execute it manually in the approved environment. The plan marks
Storage as a separate read-only export because a logical database dump does
not contain private Storage objects.

Before any production backup:

1. Confirm the linked project and operator access out of band.
2. Confirm output destinations do not already exist.
3. Run the logical database dump as a read-only operation.
4. Export every private Storage bucket with an approved read-only tool.
5. Hash the database dump and Storage export.
6. Reconcile metadata rows against exported objects.
7. Test restore on staging without changing the linked production project.

Never use `supabase db reset --linked` for backup or restore.
