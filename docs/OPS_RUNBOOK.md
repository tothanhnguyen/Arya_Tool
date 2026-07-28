# Operations Runbook

## Operating boundary

Arya_Tool currently runs the web server, both schedulers, the publish worker,
and the optional Telegram bot in one process. Run exactly one application
replica. Multiple replicas can run duplicate in-process schedulers even though
individual publish claims are idempotent.

Keep the real social publisher disabled during the Supabase cutover:

- `LAPLACE_SOCIAL_PUBLISHER` remains `mock`.
- `LAPLACE_SOCIAL_BROWSER_PUBLISHER` remains `false`.
- Facebook and Instagram browser profiles stay on the host. They are not part
  of a database dump or Storage export and should not be copied into a
  container or cloud backup.

The Docker healthcheck is liveness only. A container reported as `healthy` can
still be unready because its database, schema, Storage, or scheduler gate is
failing.

## Sanitized preflight

Run the static preflight in the final runtime environment before opening
traffic:

```bash
python -m laplace.ops.preflight config
```

The command reads process environment variables only. It does not load a
dotenv file, contact Supabase, or print configuration values. It reports only
stable check names with `valid`, `missing`, `invalid`, or `unsafe` states and
exits with status 2 when blocked.

For Docker Compose, run it in a one-off container so it checks the same
environment without starting the application or schedulers:

```bash
docker compose run --rm --no-deps arya_tool \
  python -m laplace.ops.preflight config
```

The Supabase gate requires:

- PostgreSQL through psycopg on the direct/default port or Session pooler port
  5432, with SSL enabled; transaction pooler port 6543 is rejected.
- A valid Supabase API origin, a server secret key, and a distinct publishable
  or legacy anon key.
- Matching database and API project references when standard Supabase cloud
  hostnames make both references available.
- Supabase Auth and Supabase media storage enabled.
- The two migrated private bucket names.
- Mock publisher enabled and browser publishing disabled.

This check validates shape and safe modes, not whether credentials work or the
remote schema exists. Startup and readiness provide those online gates.

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

After the process starts, run the loopback-only runtime gate:

```bash
python -m laplace.ops.preflight runtime
```

Use `--port` only if the application uses a different local port. The command
itself calls only `127.0.0.1` and discards response details. The application's
readiness handler intentionally checks its configured database and Storage, so
this runtime step does cause the running app to contact those dependencies. It
reports sanitized states for liveness, aggregate readiness, database/schema,
Storage, and scheduler and exits with status 2 unless every gate is ready.

## Startup and restart

Before starting:

1. Confirm SDB-02 recorded all required migrations and its Postgres integration
   gate passed.
2. Confirm only one Arya_Tool process will run.
3. Run the static configuration preflight.
4. Confirm the SQLite rollback backup, media backup, Supabase database backup,
   and private Storage export locations are recorded and recoverable.
5. Confirm writes remain frozen until runtime and smoke gates pass.

For Docker Compose:

```bash
docker compose build arya_tool
docker compose run --rm --no-deps arya_tool \
  python -m laplace.ops.preflight config
docker compose up -d --no-build arya_tool
python -m laplace.ops.preflight runtime
```

Use graceful stop before a planned restart:

```bash
docker compose stop -t 15 arya_tool
docker compose up -d --force-recreate arya_tool
```

`docker compose restart` does not reload changed environment configuration.
Use `up --force-recreate` after changing runtime secrets or backend selection
through the approved secret process. Never edit backend configuration while
the old process is still accepting writes.

Startup fails closed when the database is unavailable, any required table or
column is missing, or the migration revision is older than the application
requirement. Do not work around this by creating tables from SQLAlchemy or
editing production in Supabase Studio.

## Post-start smoke gates

Keep writes frozen while performing these checks in order:

1. Static configuration preflight passes.
2. `/health/live` and every component in `/health/ready` pass.
3. A human Supabase Auth session resolves to the intended local owner.
4. An unauthenticated request is rejected and a second test owner cannot read
   or mutate the first owner's account, post, media, job, event, or artifact.
5. The mock flow completes: media upload, product, draft, approval, scheduling,
   MockPublisher result, and analytics.
6. Private media and artifact upload/download use signed access and never a
   public object URL.
7. Restart with a queued/retry fixture and confirm no duplicate publish job,
   attempt, artifact, or affiliate event appears.
8. Logs and HTTP bodies contain no database URL, key, token, object path, or
   authorization header.

Archive only sanitized status, timestamps, record counts, hashes, and test
identifiers. Do not archive sessions, request headers, URLs containing
credentials, or browser profiles. Reopen writes only after every gate passes.

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

## Backup schedule and restore drill

Choose explicit recovery objectives before production. A conservative initial
schedule is:

- Daily logical Postgres dump with hash and manifest.
- Daily private Storage export followed by metadata/object reconciliation.
- An encrypted off-host copy after each successful backup.
- Daily backups retained for 14 days and one weekly backup retained for 8
  weeks, subject to data policy and available quota.
- A staging restore drill before cutover and monthly afterward.

Managed Supabase backups do not replace logical dumps or Storage exports.
Record the project, operator, tool version, start/end UTC timestamps, object
counts, byte totals, hashes, and verification result without recording a URL
or credential.

Restore only into an isolated staging target:

1. Freeze staging writes and confirm the target is not production.
2. Verify dump/export hashes and manifests before import.
3. Restore the database, then restore private Storage objects.
4. Apply no schema repair by hand; migrations remain the schema source of
   truth.
5. Reconcile all 18 table counts, foreign keys, affiliate totals/currencies,
   job states, artifact rows, and Storage object hashes.
6. Start one staging instance and pass static preflight, runtime preflight,
   Auth isolation, mock publish, restart/idempotency, and analytics gates.
7. Record measured restore time and data-loss window against the chosen RTO
   and RPO.

Any mismatch makes the drill fail. Preserve evidence and investigate; do not
delete or overwrite the source backup.

## Rollback

Rollback is mandatory on schema/revision failure, owner-isolation failure,
missing private objects, reconciliation mismatch, repeated scheduler/worker
errors, or readiness that does not recover inside the agreed change window.

1. Keep traffic closed or freeze writes again.
2. Stop the single application process gracefully.
3. Record the Supabase high-water timestamp and export/reconcile all data
   created after cutover. Do not silently discard it.
4. Restore the previous database and storage selection through the approved
   secret process.
5. Recreate the process so it receives the restored configuration.
6. Verify the restored SQLite backup, start one process, run the runtime health
   gate, then smoke API access, MockPublisher, scheduler, and analytics. The
   static Supabase configuration preflight is not a SQLite rollback gate.
7. Reopen writes only after reconciliation and operator sign-off.

Keep SQLite/media and Supabase data intact until the stabilization window
ends. Never reset, unlink, repair migration history, or delete buckets as part
of rollback.

## Monitoring and alerts

Collect the readiness status and numeric scheduler indicators at a fixed
interval. Alert immediately on database/schema or Storage unavailability,
scheduler stopped, stale running jobs, or repeated readiness HTTP 503. Alert
on sustained backlog, retry count, or lag at thresholds matched to the posting
schedule; the application defaults are 100 queued/retry jobs, 20 retry jobs,
300 seconds lag, and 600 seconds for stale running work.

Also monitor process restarts, disk space for local rollback/report data,
database connections and quota, Storage capacity/egress, backup age, last
successful restore drill, and Supabase project pause state. A liveness alert
means the process itself is down; a readiness alert means it must stay out of
traffic even if Docker still calls it healthy.
