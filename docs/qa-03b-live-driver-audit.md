# QA-03B offline live-driver independent audit

Status: Phase A threat model complete; Phase B is not started because no David
candidate commit is present in the local repository.

Audit owner: Lucy

Contract base: `7372402dfdf1a3152accb7a9665974c842e29603`

Candidate under audit: `AWAITING_DAVID_COMMIT`

Acceptance contract: `docs/qa-03b-acceptance-matrix.md`

## Scope and safety boundary

This audit owns only this document. It does not authorize changes to the live
driver, its tests, the smoke contract, runtime configuration, migrations, or
application code.

- Never read, source, create, edit, copy, or print `.env`.
- Do not contact Supabase, Telegram, Meta, another remote service, or a local
  application runtime.
- Do not run Docker, migrations, ETL, model evaluation, browser automation,
  content generation, or a real publisher.
- Review an immutable commit object supplied by David, never a moving
  worktree.
- Run tests with dotenv loading and every remote integration flag explicitly
  disabled in an otherwise empty process environment.
- Treat every unproved or ambiguous assertion as blocked. Offline success is
  not remote acceptance evidence.

## Trust boundaries and assets

The driver crosses five untrusted boundaries: application HTTP, Auth admin and
session APIs, direct PostgREST, direct Storage, and persisted worker state
observed across a process restart. External response status, headers, bodies,
identifiers, redirects, and exception text are untrusted.

The assets to protect are:

- pre-existing Auth identities, application rows, and Storage objects;
- owner isolation between newly created owners A and B;
- exact cleanup authority and the integrity of the created-target ledger;
- access tokens, credentials, raw identifiers, object names, signed URLs, and
  response bodies;
- Q/R/U job identity, attempt monotonicity, idempotency, and side-effect
  uniqueness across restart; and
- the truthfulness of typed observations and the final PASS/BLOCKED label.

## `SmokeDriver` contract map

All raw identifiers below are process-memory-only. Every successful create
must be registered in `FixtureLedger` before another fallible operation. Every
external failure must become a stable typed code without retaining remote
exception text or a raw response object.

| Method | Required inputs and preconditions | Required output | Authorized side effects and cleanup targets | Failure and retained-data rules |
|---|---|---|---|---|
| `run_initial_flow` | Opaque 16+ character marker, empty/in-progress ledger, live ephemeral tagger, already validated Gate A | Complete `InitialFlowObservations`: exact HTTP matrix, six authorized and ten denied Storage checks, 21 exact RLS checks, two-owner Auth mapping, and three-event analytics | Create exactly two Auth users and app users; one happy account, media row/object, product, post, job/attempt, three events, and artifact row/object; perform happy publish with MockPublisher; no other creates | Register each confirmed new target immediately. A duplicate, conflict, pre-existing match, missing ID, malformed body, or ambiguous 2xx must block and must not confer delete authority. Discard bodies, tokens, URLs, cookies, IDs, names, and headers after deriving typed state |
| `prepare_restart_fixtures` | Initial flow validated; same ledger and tagger; raw A-owned identities still only in memory | Immutable pre-restart `RestartSnapshot` for Q/R/U, analytics, bucket privacy, media/artifact tags, and database uniqueness | Create three separate active mock accounts and posts, Q/R/U jobs, R attempts 1/2, and U attempt 1; Q has no attempt yet; seed exact queued/retry/stale-fenced state and retain the supplied ledger for later Q1/R3 registration | Register every row immediately. Do not pre-register fabricated future attempt IDs, run a process restart, invoke a real publisher, or infer targets by marker/prefix. Missing, duplicate, cross-owner, or misnumbered fixtures block |
| `resume_after_restart` | Accepted real restart boundary and post-restart runtime preflight; same live tagger and captured raw fixture identities | First post-restart `RestartSnapshot` with Q published once, R attempt 3 published once, and U failed/unknown without replay | Read exact captured jobs, attempts, objects, analytics, privacy, and uniqueness state only; worker side effects belong to the restarted application, not this method | Never reselect by marker, owner, ordering, or "latest" row. Job and idempotency tags must match the pre-restart snapshot. No raw result, error text, or remote ID may survive extraction |
| `settled_snapshot` | Same identities and tagger after `resume_after_restart` | A second `RestartSnapshot` exactly equal to the first post-restart snapshot | Read-only settling probe; no new row, attempt, object, event, publisher call, or cleanup action | A timeout, changing snapshot, identity swap, duplicate side effect, or extra attempt blocks. It must not manufacture equality by reusing the previous object |
| `finish_flow` | Accepted restart validation; retained exact A artifact identity/session; artifact remained active through cross-owner and restart checks | Complete `FinishFlowObservations`: API delete `204`, metadata deleted, object absent, reconciliation counts zero | Delete exactly the captured artifact through A's API, then read exact metadata/object and owner-scoped reconciliation | No direct early object delete, no B session, no broader reconciliation-derived delete. Redirect, misleading 204, residue, or owner mismatch blocks |
| `delete_exact` | One ledger-owned `ExactCleanupTarget` with its validated selector and provenance | Integer affected count; normally exactly `1`, while an already API-deleted artifact object is not deleted again | One equality-bound row/Auth/object delete only; bounded retry may repeat the identical target | No list/prefix/owner/marker/date/table authority, no filter broadening, and no string-built selector injection. Failure text is discarded; independent targets must still be attempted |
| `remaining_exact` | The same exact target, including full bucket plus object name for Storage | Exact remaining count, only `0` or `1` is acceptable | Read-only primary-key/Auth/object existence probe | No marker/prefix/list inference and no cached answer. Unknown or multiple matches block and cannot be reported as zero |
| `post_cleanup_verification` | Marker used only after all exact-ledger deletes have been attempted | `PostCleanupVerification` covering all 13 marker scopes, unchanged sentinel, private buckets, healthy runtime, and one replica | Read-only marker residue counts, sentinel comparison, bucket privacy, and final health/replica probes | Marker-wide or Storage list results are verification only and never deletion authority. Unavailable/unknown is not zero. Persist only counts, booleans, states, and approved tags |

The cleanup methods are inherited through `CleanupExecutor` and are part of
the `SmokeDriver` contract even though they are declared separately in
`laplace/ops/qa03b_smoke.py`.

## Adversarial threat and probe matrix

| ID | Threat | Offline probe | Required fail-closed result |
|---|---|---|---|
| T01 | Import or constructor performs network, mutation, dotenv load, client discovery, or environment-dependent setup | Import the exact candidate in a subprocess with socket/connect and HTTP transports set to raise; instantiate with inert injected fakes; inspect import graph for `load_dotenv`, `get_settings`, default live clients, and module-level calls | Zero calls and zero created targets; no live CLI or implicit client |
| T02 | A create succeeds remotely but ledger registration is delayed until later parsing/probing | Make the fake return a valid create ID, then raise on the very next operation; inspect the ledger received by cleanup | The exact created target is already registered and cleanup is attempted |
| T03 | Ambiguous, duplicate, conflicting, or pre-existing create is adopted | Return 2xx with missing/multiple IDs, `duplicate=true`, conflict, mismatched owner, repeated ID, or a preexisting lookup | BLOCKED; do not register the ambiguous/pre-existing target and never delete it |
| T04 | Misleading HTTP success or trusted response body forges state | Return expected status with wrong owner/state/hash/count, or disagree with an independent exact read | BLOCKED; typed observation is derived from independently checked state |
| T05 | Driver can return hard-coded accepted dataclasses without making probes | Use call-recording fakes and return plausible payloads in the wrong sequence or omit a required endpoint | BLOCKED; every expected endpoint and exact state probe must be called once in the required phase |
| T06 | Owner A/B confusion or invalid B token makes isolation checks vacuous | Swap sessions/owner IDs, make tags collide at the presentation layer, or give B an invalid/anonymous session | BLOCKED before isolation PASS; prove raw A/B identities are distinct and both sessions are valid/mapped |
| T07 | RLS test is vacuous because the selected A row does not exist or the predicate is broad | Remove the exact A row, change `id=eq.<captured>` to owner/marker/prefix, omit `Prefer: return=representation`, or return empty without an A before/after read | BLOCKED; exact existing A row, exact equality filter, `200 []`, zero affected, and unchanged A hash are all required |
| T08 | Storage redirect or auto-follow discloses payload/signed URL while the final code looks denied | Return 301/302/307/308 with a secret-bearing `Location`, or follow to a 200 payload for a public/B request | BLOCKED on any disclosure; denial probes do not auto-follow, and raw `Location`/body is discarded |
| T09 | Storage denial is normalized to generic 4xx or owner hash is not re-read | Return distinct 3xx/4xx/5xx statuses and mutate the A object behind a denied response | Preserve the exact numeric status but BLOCK unless non-2xx, no disclosure, and A SHA-256 is unchanged |
| T10 | Restart snapshot silently swaps jobs or idempotency identity | Reorder rows, add same-marker decoys, return another owner's job, or change job/idempotency tag after restart | BLOCKED; every Q/R/U exact identity and tag is stable, while the application instance tag changes |
| T11 | Retry numbering trusts stale `attempt_count` instead of persisted maximum | Seed R with counter 1 and terminal attempts 1/2; make a candidate try attempt 2 again | BLOCKED; only attempt 3 may be created and the unique constraint must remain present |
| T12 | Fenced unknown outcome is replayed | Seed U running/stale with `side_effect_started=true`, then expose a publisher fake that records calls | Zero publisher calls; U becomes failed/unknown/manual with no attempt 2 or remote result |
| T13 | Settled snapshot is forged by returning/caching the prior snapshot | Mutate the backing fake between first and settled reads and assert fresh read calls | BLOCKED; the method performs independent exact reads and equality reflects the backing state |
| T14 | Cleanup selector is broadened after failure | Reject the first exact delete and expose list, prefix, wildcard, owner, marker, `LIKE`, recursive, unfiltered, or string-injected alternatives | Continue with other exact ledger targets, retain the identical selector on bounded retry, report residue/unknown, final BLOCKED |
| T15 | Cleanup stops on the first exception or interrupt | Raise ordinary exceptions, `KeyboardInterrupt`, and a non-`Exception` `BaseException` on different targets | All independent targets and final verification are attempted; fatal interruption is re-raised only after cleanup attempts |
| T16 | Storage listing becomes deletion authority | Return attacker-controlled objects from a bucket listing | No listed object is deleted unless its exact bucket/name was already in the created ledger |
| T17 | Artifact is deleted too early or direct cleanup contradicts API provenance | Make cross-owner/restart checks require the active artifact; leave residue after apparent API 204 | API delete occurs only in `finish_flow`; residue blocks and may only be removed as the same exact target with blocked audit evidence |
| T18 | Raw data survives in attributes, exceptions, logs, snapshots, repr, or evidence | Seed every external field with sentinel URL/token/email/UUID/object/path/body values; force every error path; scan stdout/stderr, exception args, object repr/`__dict__`, and public evidence | No sentinel survives. Only stable codes, exact statuses, counts, booleans, SHA-256, and ephemeral 16-hex HMAC tags are retained |
| T19 | Ephemeral tag key or raw IDs remain usable after the run | Inspect driver/evidence after `run_smoke` and attempt another tag operation | Key is zeroized; tagger rejects reuse; evidence has no raw identifiers or unsalted identifier hashes |
| T20 | A partial or cleanup-failed run claims PASS | Fail each method and each cleanup/verification step in turn, then attempt forged `HarnessEvidence(result="passed")` | Stable BLOCKED/FAILED code as appropriate; PASS is impossible unless all stages and exact cleanup are complete |
| T21 | Invalid Q/R/U pre-restart state is discovered only after the application has already restarted | Return a malformed, ambiguous, wrong-owner, or incomplete pre-restart snapshot and record whether the checkpoint is entered | Driver rejects invalid fixture construction before the restart checkpoint; no unsafe restart proceeds merely because later transition validation would block |

## Offline proof versus mandatory remote evidence

| Assertion class | Offline audit disposition | Mandatory operator evidence |
|---|---|---|
| Dependency injection, no import/constructor side effects, no hidden dotenv, no live CLI | Fully provable from exact source plus call-blocking tests | Recheck packaged/runtime entrypoint before any authorized run |
| Response parsing, stable error mapping, raw-data disposal, typed observation construction | Fully provable with adversarial fake transports | Redaction scan of the authorized run's structured evidence and captured logs |
| Immediate ledger registration, collision refusal, equality-only cleanup, continuation after failure | Fully provable with stateful fakes and exact call traces | Exact predicates, affected counts, per-target zero lookups, residue counts, and unchanged sentinel |
| HTTP status and route coverage | Request construction and extraction are provable offline | Every Gate C/E numeric status from the real loopback application |
| Auth mapping and owner separation | Branching and validation are provable offline | Two new Auth identities mapping one-to-one to distinct remote app owners |
| PostgREST RLS | Equality request shape and fail-closed parsing are provable offline | Real B-session `200 []` reads/updates/deletes plus unchanged exact A rows |
| Storage privacy/isolation | Redirect policy, status preservation, hash logic, and no-disclosure handling are provable offline | Real public/B denials, A hash re-reads, signed downloads, and both real buckets private |
| Q/R/U snapshot logic, attempt ordering, identity stability, duplicate detection | Fixture requests and transition validation are provable offline | Committed remote fixtures across a real stop/down/start boundary and settled state |
| Recovery transaction rollback and intent-fence behavior | Existing injected-failure source tests are sufficient; do not inject a remote failure/crash | Real seeded fenced stale state must fail closed after restart; no remote crash injection required |
| Static/runtime preflight and final health | Exact report validation is provable offline | Process-environment-only configuration, one approved replica, loopback liveness/readiness before and after |
| Final PASS | Not available from an offline audit | All acceptance-matrix rows must pass in one separately authorized remote run |

## Phase B immutable-commit procedure

Phase B starts only after David supplies a full commit SHA. The following gates
must all pass before a verdict:

1. Resolve the SHA as a commit and record it verbatim.
2. Prove `7372402` is an ancestor and inspect
   `git diff --find-renames 7372402..<candidate>`.
3. Refuse a moving worktree as evidence. Test an archive of the exact commit in
   a fresh temporary directory after confirming the commit does not track a
   file named `.env`.
4. Confirm the diff is limited to
   `laplace/ops/qa03b_live_driver.py`,
   `tests/test_qa03b_live_driver.py`, and only the minimum contract change in
   `laplace/ops/qa03b_smoke.py`. Any other file requires explicit owner review.
5. Run all commands below with an empty inherited environment and explicit
   offline flags. Do not invoke setup, Docker, migrations, or a runtime.
6. Review source and tests against every threat T01-T20. A test that only
   asserts a preconstructed accepted dataclass is not evidence that the driver
   derived it truthfully.

The required process prefix for every Python/Ruff command is:

```text
env -i PATH=/usr/bin:/bin \
  PYTHONPATH=. \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_ADDOPTS="-p no:cacheprovider" \
  LAPLACE_DISABLE_ENV_FILE=1 \
  LAPLACE_RUN_POSTGRES_INTEGRATION=0 \
  LAPLACE_RUN_POSTGRES_RLS_INTEGRATION=0 \
  LAPLACE_SOCIAL_PUBLISHER=mock \
  LAPLACE_SOCIAL_BROWSER_PUBLISHER=false \
  LAPLACE_LLM_PROVIDER=mock
```

This is required because `tests/conftest.py` calls `get_settings()` and normal
`get_settings()` behavior otherwise permits reading the repository `.env`.

## Audit gate matrix

| Gate | Exact check | Pass condition | Current result |
|---|---|---|---|
| C0 candidate pin | `git cat-file -e <candidate>^{commit}`; ancestor and diff inspection | Full immutable SHA exists and is descended from `7372402` | `WAITING` |
| C1 ownership | Candidate name-only diff | Only David-owned files plus a minimal justified smoke-contract change | `WAITING` |
| C2 no implicit I/O | Static import graph plus blocked socket/HTTP/Auth/PostgREST/Storage fakes on import and construction | No network, dotenv, filesystem mutation, remote discovery, or implicit client | `WAITING` |
| C3 driver adversarial tests | `tests/test_qa03b_live_driver.py` | All required and negative driver cases pass offline | `WAITING` |
| C4 harness contract | `tests/test_qa03b_smoke.py` | Existing fail-closed QA-03B tests pass unchanged | `WAITING` |
| C5 Auth | `tests/test_owner_auth_integration.py tests/test_supabase_auth.py` | Offline Auth and owner mapping regressions pass | `WAITING` |
| C6 social API/RLS | `tests/test_social_api.py tests/test_supabase_db.py` | API contract and offline database/RLS source tests pass; integration tests remain disabled/skipped | `WAITING` |
| C7 artifact/Storage | `tests/test_artifact_api.py tests/test_artifact_storage.py tests/test_social_storage.py` | Artifact and private Storage regressions pass with fakes/local storage only | `WAITING` |
| C8 worker/scheduler | `tests/test_social_worker.py tests/test_social_scheduler.py tests/test_scheduler_stream.py` | Recovery, attempt, fence, worker, and scheduler regressions pass | `WAITING` |
| C9 preflight | `tests/test_ops_preflight.py` | Process-only and loopback preflight validation tests pass without a runtime | `WAITING` |
| C10 full offline regression | `pytest -q` with the required empty-environment prefix | All offline tests pass; only explicitly disabled remote integration tests may skip | `WAITING` |
| C11 static quality | Ruff check and format check on candidate files; `git diff --check 7372402..<candidate>` | All commands exit zero | `WAITING` |
| C12 threat closure | Manual source/test trace against T01-T21 | No open High or Medium finding; every assertion has derivation evidence | `WAITING` |

The focused pytest invocation for C3-C9 is:

```text
python -m pytest -q \
  tests/test_qa03b_live_driver.py \
  tests/test_qa03b_smoke.py \
  tests/test_owner_auth_integration.py \
  tests/test_supabase_auth.py \
  tests/test_social_api.py \
  tests/test_supabase_db.py \
  tests/test_artifact_api.py \
  tests/test_artifact_storage.py \
  tests/test_social_storage.py \
  tests/test_social_worker.py \
  tests/test_social_scheduler.py \
  tests/test_scheduler_stream.py \
  tests/test_social_models.py \
  tests/test_social_publishers.py \
  tests/test_social_publisher_factory.py \
  tests/test_ops_preflight.py
```

The static-quality targets are:

```text
ruff check \
  laplace/ops/qa03b_live_driver.py \
  laplace/ops/qa03b_smoke.py \
  tests/test_qa03b_live_driver.py \
  tests/test_qa03b_smoke.py

ruff format --check \
  laplace/ops/qa03b_live_driver.py \
  laplace/ops/qa03b_smoke.py \
  tests/test_qa03b_live_driver.py \
  tests/test_qa03b_smoke.py
```

## Verdict

`BLOCKED_BEFORE_PHASE_B`.

Phase A is ready for candidate review, but there is no local commit or ref
descended from `7372402`. This is not an implementation finding. Phase B has
no evidentiary result until David supplies an immutable commit SHA and every
C0-C12 gate is completed against that exact commit.
