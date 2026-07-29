# QA-03B remote acceptance contract

Status: independent acceptance contract for the remote closure harness.

Source baseline: `7cf2b0cbb956b2f6a9e2e795b3e64f85befc0076`.

This document defines what the harness must prove. It is not evidence that a
remote gate passed. Any implementation or route change after the source
baseline requires this mapping to be reviewed again before the harness runs.

## Non-negotiable boundary

- Do not read, source, create, edit, copy, print, or otherwise use `.env`.
- Run only in an approved process environment with dotenv loading disabled.
- Use exactly one loopback application replica, MockPublisher, and disabled
  browser publishing.
- Do not run migrations, ETL, model evaluation, content generation, a real
  publisher, Meta/browser automation, or a setup wizard.
- Do not mutate a target until both prerequisite gates below pass.
- Use two newly created Auth owners and marker-scoped fixtures. Never adopt,
  modify, or delete a pre-existing row, object, or Auth identity after a
  collision.
- Keep every raw target identifier only in process memory. Persist only the
  sanitized evidence described below.

Any unmet assertion makes the overall result `BLOCKED`, not a partial pass.

## Evidence classes

| Evidence class | Current disposition | Remote requirement |
|---|---|---|
| Queued job with a new Worker/MockPublisher instance | `LOCAL PASS` | Must also cross a real process restart |
| Retry job with a new Worker/MockPublisher instance | `LOCAL PASS` | Must also cross a real process restart |
| Stale running attempt is finalized | `LOCAL PASS` | Seed and verify the persisted state remotely |
| Persisted maximum controls the next `attempt_no` | `LOCAL PASS` | Verify on the remote database |
| `(publish_job_id, attempt_no)` database uniqueness | `LOCAL PASS` by model/migration review | Verify no duplicate numbers in remote results |
| Job and attempt recovery transaction rolls back atomically | `LOCAL PASS` | Offline injected-failure proof is sufficient; do not inject a remote failure |
| Crash after MockPublisher side effect leaves an intent fence | `LOCAL PASS` | Actual crash injection is not required remotely |
| Fresh publisher fails closed for a fenced unknown outcome | `LOCAL PASS` | Seed the fenced stale state, restart, and verify it remotely |
| Static configuration and loopback readiness | No current remote pass | Mandatory remote evidence |
| Auth mapping, exact HTTP statuses, RLS, and private Storage | Prior evidence is incomplete for QA-03B closure | Mandatory remote evidence |
| Live queued/retry restart and duplicate checks | No current remote pass | Mandatory remote evidence |
| Exact-target cleanup predicates and final zero checks | No current auditable predicate evidence | Mandatory remote evidence |

Local proof does not substitute for a row marked mandatory remote.

## Gate A: prerequisites before mutation

### Static configuration

Run the sanitized configuration preflight in the exact runtime process
environment. It must:

- exit `0`;
- report overall `ready`;
- report source `process_environment_only`;
- report `remote_access: false`; and
- report every check as `pass` / `valid`: database connection, Supabase
  origin, project alignment, server secret key, Auth public key, Supabase
  Auth, Storage backend, Storage buckets, publisher, and browser publisher.

Evidence may contain check names and states only. A missing, invalid, unsafe,
or unavailable check aborts before mutation.

### Loopback runtime

Prove exactly one approved application replica, then run the loopback
preflight. It must:

- call only the configured `127.0.0.1` application port;
- return HTTP `200` from `/health/live`;
- return HTTP `200` from `/health/ready`;
- report database/schema, Storage, and scheduler as `ready`; and
- report no stale running work and no scheduler degradation before fixtures
  are created.

Do not use a Compose command that can implicitly read `.env`. If the exact
runtime or target cannot be established without `.env`, stop with
`BLOCKED_BEFORE_MUTATION`.

## Gate B: Auth and owner mapping

Create exactly two new marker-scoped Auth users, A and B. For each create:

- record `created: true` under a sanitized alias;
- resolve the Auth identity to exactly one `public.users` row;
- prove the two application owner IDs are distinct; and
- prove neither identity or owner row existed before the run.

With Auth enabled:

| Request | Expected status | Additional predicate |
|---|---:|---|
| Protected application request without a session | `401` | `WWW-Authenticate: Bearer`; no response secret |
| Protected request with A's valid session | Route-specific success | Request owner resolves to A |
| Protected request with B's valid session | Route-specific success | Request owner resolves to B |
| Valid Auth identity with no application mapping, if tested | `403` | No Auth UUID in the response |

Never persist an email, password, access token, refresh token, cookie, Auth
UUID, authorization header, or raw application owner ID.

## Gate C: User A happy path

All authenticated JSON mutations use A's Bearer token. A supplied `user_id`
must equal the owner resolved from that token.

| Step | Request | Expected status | Required result |
|---|---|---:|---|
| Mock account | `POST /api/social/accounts/mock` | `201` | New active `auth_type=mock` account |
| Media | `POST /api/social/media?user_id=<A>` | `201` | `duplicate=false`, Supabase backend, expected payload SHA-256 |
| Product | `POST /api/social/products` | `201` | New active product owned by A |
| Draft | `POST /api/social/content/drafts` | `201` | New draft linked to A's media and product |
| Approval | `POST /api/social/content/{post_id}/approve` | `200` | Post becomes `approved` |
| Scheduling | `POST /api/social/jobs` | `201` | New `queued` job and `already_existed=false` |
| Mock publish | Scheduler/worker, not HTTP | N/A | One published job, one successful attempt, one non-null remote result |
| View event | `POST /api/social/events` | `201` | One owner-scoped view |
| Click event | `POST /api/social/events` | `201` | One owner-scoped click |
| Commission event | `POST /api/social/events` | `201` | One owner-scoped commission |
| Artifact upload | `POST /api/artifacts` | `201` | Active metadata and expected payload SHA-256 |
| Artifact download | `GET /api/artifacts/{artifact_id}/download` | `200` | Body SHA-256 matches upload |
| Artifact signed URL | `POST /api/artifacts/{artifact_id}/signed-url` | `200` | Positive bounded expiry; do not persist URL |
| Artifact reconciliation | `GET /api/artifacts/reconciliation` | `200` | One active row and one verified object before delete |
| Artifact delete | `DELETE /api/artifacts/{artifact_id}` | `204` | Exact object removed; metadata becomes `deleted` |

The harness must capture each numeric status separately. Phrases such as
"all creates passed" or "cross-owner requests were denied" are insufficient.
Do not call `/api/social/content/generate`; the draft must use the
non-model `/content/drafts` route.

Keep the artifact active through the cross-owner checks and restart snapshot.
Execute its required HTTP `204` delete after the post-restart duplicate check
and before direct metadata cleanup. Gate C defines the status contract, not an
earlier execution order.

### Analytics reconciliation

Before restart, record sanitized counts of exactly three marker events:

- view count `1`;
- click count `1`;
- commission count `1`; and
- the expected commission amount and currency.

After restart, the same three event IDs and totals must remain unchanged.

## Gate D: private media and artifacts

For A's media object:

- authenticated download returns HTTP `200` and the upload SHA-256;
- signing returns HTTP `200`;
- the signed download returns HTTP `200` and the same SHA-256; and
- the public-object endpoint must not return a success status.

For A's artifact, the application API statuses are fixed in Gate C. Its
download and signed download hashes must match the uploaded artifact.

Raw Supabase Storage denial codes can vary with the deployed Storage API
version. The harness must still record the exact numeric status for every
public and cross-owner request; the acceptance predicate is non-2xx, no
payload disclosure, and unchanged owner object hash. Do not normalize these
observations to a generic `4xx`.

Both configured buckets must remain private before and after the run.

## Gate E: cross-owner contract

Run every request with B's valid Bearer token. Capture each status
individually and verify the referenced A resource remains unchanged.

### Application API

| Request by B | Expected status | Required invariant |
|---|---:|---|
| `POST /api/social/accounts/mock` with `user_id=A` | `403` | No account created |
| `POST /api/social/media?user_id=A` | `403` | No row or object created |
| `POST /api/social/products` with `user_id=A` | `403` | No product created |
| `POST /api/social/content/drafts` with `user_id=A` | `403` | No draft created |
| `POST /api/social/content/{A_post}/approve` with `user_id=B` | `409` | A's post unchanged |
| `POST /api/social/jobs` with `user_id=A` | `403` | No job created |
| `POST /api/social/jobs/{A_job}/cancel` with `user_id=B` | `409` | A's job unchanged |
| `POST /api/social/events` with `user_id=A` | `403` | No event created |
| `GET /api/artifacts/{A_artifact}/download` | `404` | No body or metadata disclosure |
| `POST /api/artifacts/{A_artifact}/signed-url` | `404` | No signed URL returned |
| `DELETE /api/artifacts/{A_artifact}` | `404` | A's object and metadata unchanged |
| `GET /api/artifacts/reconciliation` | `200` | Zero A artifacts visible to B |

The `409` results for foreign social post/job actions are the current
tool-to-API mapping; do not rewrite them as `403` or `404` in evidence.

### Direct PostgREST RLS

Use an equality filter on one exact A row and
`Prefer: return=representation` for mutation checks:

| Operation by B | Expected status/body | Required invariant |
|---|---|---|
| `GET` exact A row | `200` and `[]` | No A columns disclosed |
| `PATCH` exact A row | `200` and `[]` | A row hash unchanged |
| `DELETE` exact A row | `200` and `[]` | A row still exists and hash is unchanged |

Exercise representative ownership roots and dependents: account, media,
product, post, publish job, affiliate event, and artifact. The filter must be
`id=eq.<one captured ID>`, never an owner-wide or marker-prefix filter.

### Direct Storage

With B's session, attempt authenticated read, sign, update, and delete against
each exact A object. For every request:

- persist the exact numeric HTTP status but not its body;
- require a non-2xx result;
- require no signed URL or payload bytes; and
- re-read as A and prove the object SHA-256 is unchanged.

## Gate F: restart fixtures

Use separate active mock accounts for the queued, retry, and fenced fixtures
so account cooldown and daily limits cannot defer another fixture.

Create all fixture rows before shutdown and capture an immutable sanitized
pre-restart snapshot.

### Queued fixture Q

- one marker-scoped job with `status=queued`;
- `attempt_count=0`;
- zero attempts;
- a due time that occurs only while the application is down or after the new
  process starts; and
- one stable idempotency-key tag.

### Retry fixture R

- one marker-scoped job with `status=retry`;
- two persisted terminal `retryable_error` attempts numbered `1` and `2`;
- both prior attempts have `finished_at` and no side-effect fence;
- deliberately set `attempt_count=1` while persisted maximum is `2`;
- set `next_retry_at` due only in the restarted process; and
- preserve one stable idempotency-key tag across every attempt.

This fixture proves that the next number comes from the persisted maximum,
not only the possibly stale counter.

### Fenced fixture U

- one marker-scoped job with `status=running`, no remote result, and a
  `updated_at` older than the ten-minute startup recovery cutoff;
- one running attempt numbered `1`;
- persisted response metadata contains `side_effect_started=true`; and
- no actual external publisher is invoked to prepare this fixture.

### Restart boundary

Prove a real process boundary:

1. exactly one replica is live before shutdown;
2. the pre-restart fixture transaction is committed;
3. graceful stop exits successfully;
4. loopback liveness is unavailable while stopped;
5. a new process starts with MockPublisher and dotenv loading disabled;
6. exactly one replica is live afterward; and
7. loopback liveness/readiness return HTTP `200` after processing settles.

Do not persist a PID or container ID. Command exit states, timestamps, replica
counts, and sanitized instance tags are sufficient.

### Post-restart assertions

| Fixture | Required final state |
|---|---|
| Q | Same job; `published`; `attempt_count=1`; attempts exactly `[1:published]`; one remote-result tag |
| R | Same job; `published`; `attempt_count=3`; attempts exactly `[1:retryable_error, 2:retryable_error, 3:published]`; one remote-result tag |
| U | Same job; `failed`; `next_retry_at=null`; attempt exactly `[1:unknown]`; `finished_at` set; reconciliation required; no attempt `2`; no remote result |

For Q and R:

- the job count remains one per fixture;
- attempt numbers are strictly increasing and unique;
- the database unique constraint remains present;
- every request/idempotency tag is stable for its job;
- exactly one attempt is `published`;
- exactly one distinct remote-result tag exists; and
- no extra media object, artifact, or affiliate event appears.

For U, error type and message must be stable sanitized values equivalent to
`publish_outcome_unknown` and manual reconciliation. Automatic replay is a
failure.

## Gate G: exact-target cleanup

### Target ledger

On every successful create, push an in-memory cleanup record containing:

- resource type and table, if applicable;
- the exact returned primary key or Auth UUID;
- exact Storage bucket plus complete object name;
- whether this run created the resource; and
- a sanitized correlation tag.

The correlation tag should be a truncated HMAC-SHA256 over resource type and
raw identifier using an ephemeral per-run key. Erase the key after the run.
Do not use an unsalted hash of a small numeric ID.

Only `created=true` records may be deleted. A duplicate, conflict, ambiguous
response, or pre-existing target must never be added as an owned cleanup
target.

### Allowed predicate shapes

Evidence must preserve predicate shape and affected count without the bound
value:

- application row: `table.primary_key = :one_bound_id`;
- publish attempt: `publish_attempts.id = :one_bound_id`;
- Storage object: `bucket = :one_bucket AND name = :one_complete_name`;
- Auth user: admin delete of one exact captured Auth UUID.

An equality-bound list is allowed only when the evidence also records its
exact cardinality and every member came from the in-memory created ledger.

Forbidden cleanup shapes include:

- wildcard, glob, regex, `LIKE`, prefix, folder, or recursive object deletion;
- owner-wide, marker-wide, date-wide, table-wide, or unfiltered deletion;
- a Storage list result used as authority to delete uncaptured objects;
- deleting by email, display name, external ID, filename, or partial path; and
- changing to a broader predicate after an exact delete fails.

### Foreign-key-safe order

Use exact targets in this order:

1. delete the active artifact through A's API and require HTTP `204`;
2. affiliate events;
3. publish attempts;
4. publish jobs;
5. content-generation rows if and only if unexpectedly created, which also
   makes the run blocked;
6. social posts;
7. affiliate products;
8. social accounts;
9. exact media and artifact Storage objects not already removed;
10. media assets and artifact metadata rows;
11. any other exact user-owned rows created by the run;
12. application user rows; and
13. the two exact Auth users.

Each expected existing target must report affected count `1`. An idempotent
second verification may report `0`; it must be labeled verification, not the
primary deletion.

### Post-cleanup proof

For every ledger entry:

- exact primary-key lookup returns zero rows;
- exact Auth-user lookup returns absent;
- exact Storage-object lookup returns absent; and
- the sanitized correlation tag matches create, use, delete, and verify steps.

Also prove:

- zero remaining rows for the run's marker in every touched table;
- zero remaining test objects in both buckets;
- zero remaining test Auth users;
- both buckets are still private;
- a pre-run non-test sentinel/count/hash is unchanged;
- final liveness and readiness are HTTP `200`; and
- exactly one application replica remains.

Final zero counts do not replace the exact predicate and affected-count
evidence.

## Abort and cleanup behavior

- Before the first mutation, any failed prerequisite exits immediately with
  no cleanup needed and explicit zero-created/zero-deleted counts.
- After the first successful create, all work runs under `try/finally`; every
  later assertion, timeout, restart failure, or interrupt enters exact-target
  cleanup.
- Cleanup uses the ledger in reverse dependency order and continues across
  independent exact targets after recording a sanitized failure.
- A failed exact delete may be retried a bounded number of times against the
  same exact target. It must never broaden its predicate.
- If the target is unavailable, report cleanup state as unknown and the run as
  blocked. Do not claim zero residue.
- If any created target remains, report a sanitized residue count and
  correlation tag, stop, and require operator reconciliation.
- Do not stop the final healthy runtime until cleanup and final verification
  complete unless safety requires an emergency stop.

## Evidence redaction contract

The persisted evidence may contain:

- source commit SHA and sanitized run evidence ID;
- timestamps and durations;
- command exit codes;
- exact HTTP status numbers;
- route templates without bound IDs;
- predicate templates without bound values;
- booleans, states, counts, attempt numbers, and affected counts;
- payload SHA-256 values; and
- ephemeral HMAC correlation tags.

The persisted evidence must not contain:

- `.env` contents or evidence derived by reading `.env`;
- database or Supabase URLs, connection strings, hostnames, or project refs;
- secret, publishable, anon, API, or service-role keys;
- access/refresh tokens, cookies, passwords, emails, authorization headers,
  JWT claims, or signed URLs;
- full Auth UUIDs, raw numeric row IDs, idempotency keys, remote post IDs,
  external account IDs, or marker values;
- complete Storage object names, local media paths, filenames containing a
  marker, or response bodies that may contain them;
- PIDs, container IDs, browser profiles, or secret-bearing logs; or
- exception text from remote services.

Before persisting evidence, scan both structured output and captured logs for
the forbidden field classes. A redaction finding blocks acceptance even if
all functional assertions passed.

## Final acceptance matrix

| Gate | Pass condition | Evidence location |
|---|---|---|
| Prerequisites | Static and loopback gates pass before mutation | Sanitized command/check records |
| Auth mapping | Two new Auth identities map one-to-one to distinct owners | Counts and correlation tags |
| HTTP contract | Every Gate C and Gate E request has its exact status | Per-request status table |
| Owner isolation | B sees/mutates none of A's resources | Statuses, empty results, unchanged hashes |
| Private Storage | Authorized hashes match; public/B access denied; buckets private | Exact status and hash records |
| Happy publish | One job, one published attempt, three analytics events | Sanitized state/count snapshot |
| Live restart | Real stop/down/start boundary with Q and R processed once | Boundary and before/after snapshots |
| Attempt monotonicity | Q `[1]`; R `[1,2,3]`; no duplicates | Attempt state table |
| Intent fence | U becomes failed/unknown/manual, no replay | Before/after U snapshot |
| Duplicate protection | Stable counts/tags for jobs, side effects, artifacts, events | Before/after reconciliation |
| Cleanup | Equality predicates, affected counts, exact post-zero checks | Cleanup ledger report |
| Redaction | Forbidden-field scan is empty | Scanner category counts only |
| Final health | One replica; liveness/readiness HTTP `200` | Final preflight record |

QA-03B is `PASS` only when every row above passes in one authorized run.
