"""Generate a non-mutating Supabase cutover and rollback checklist.

This command never links a project, pushes a migration, resets a database, or
changes application configuration. It only writes a local sign-off document.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from laplace.db import REQUIRED_SUPABASE_REVISION


def render_cutover_checklist(*, environment: str, generated_at: datetime) -> str:
    stamp = generated_at.astimezone(UTC).isoformat()
    return f"""# Arya_Tool Supabase cutover checklist

- Environment: `{environment}`
- Required migration revision: `{REQUIRED_SUPABASE_REVISION}`
- Generated at (UTC): `{stamp}`
- Operator: ____________________
- Change window: ____________________

## Preflight

- [ ] Confirm no credential, URL, access key, or browser profile is in this document.
- [ ] Create a recoverable SQLite backup and a separate media directory backup.
- [ ] Record backup paths, hashes, timestamps, and restore-test result.
- [ ] Run the SQLite ETL command in dry-run mode and archive JSON + Markdown reports.
- [ ] Confirm all core tables exist and orphan foreign-key count is zero.
- [ ] Confirm local media count, byte total, and missing-file count.
- [ ] Confirm artifact metadata count, object count, byte total, and kind totals.
- [ ] Review the migration dry-run against the intended Supabase project.
- [ ] Confirm database schema health reports revision {REQUIRED_SUPABASE_REVISION} or newer.
- [ ] Confirm private `arya-media` and `arya-artifacts` buckets and owner policies.
- [ ] Complete a staging rollback drill and attach evidence.

## Write freeze and final ETL

- [ ] Announce the write-freeze window and name the operator who can abort it.
- [ ] Stop write API mutations, Telegram mutations, scheduler, and publish worker.
- [ ] Verify no running or awaiting-confirmation write job remains.
- [ ] Take the final SQLite and media backups; do not delete earlier backups.
- [ ] Run final ETL once and archive both reconciliation formats.
- [ ] Run ETL a second time and confirm zero duplicate rows and objects.
- [ ] Compare row counts, IDs, statuses, affiliate totals/currencies, media hashes,
      artifact metadata, timestamps, and JSON fields.

## Cutover smoke gates

- [ ] Change runtime database/storage configuration through the approved secret process.
- [ ] Restart the application and confirm database + Storage health.
- [ ] Test Auth owner mapping and a negative cross-owner request.
- [ ] Test Content Studio, scheduling, mock worker, analytics, and signed artifact URL.
- [ ] Confirm retry/restart does not duplicate publish jobs or artifacts.
- [ ] Re-enable write traffic only after every smoke gate passes.

## Rollback triggers

- [ ] Define the maximum acceptable downtime and reconciliation mismatch.
- [ ] Roll back on schema/revision failure, owner isolation failure, missing objects,
      incorrect totals, or repeated scheduler/worker errors.
- [ ] Freeze writes before rollback.
- [ ] Export and reconcile data created on Supabase after cutover.
- [ ] Restore the previous database/storage configuration through the secret process.
- [ ] Restart and smoke-test the SQLite path before reopening writes.
- [ ] Preserve Supabase and local evidence; do not reset, unlink, or delete either source.

## Sign-off

- Cutover result: PASS / ABORT / ROLLED BACK
- Reconciliation report: ____________________
- Smoke-test evidence: ____________________
- Rollback evidence (if used): ____________________
- Operator signature and UTC timestamp: ____________________
"""


def write_cutover_checklist(
    destination: str | Path,
    *,
    environment: str,
    generated_at: datetime | None = None,
) -> Path:
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_cutover_checklist(
        environment=environment,
        generated_at=generated_at or datetime.now(UTC),
    )
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    except FileExistsError as exc:
        raise RuntimeError(
            "Checklist destination already exists; choose a new path"
        ) from exc
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write a local, non-mutating Supabase cutover checklist",
    )
    parser.add_argument("--output", required=True, help="New Markdown output path")
    parser.add_argument(
        "--environment",
        default="staging",
        choices=("staging", "production"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        path = write_cutover_checklist(
            args.output,
            environment=args.environment,
        )
    except (OSError, RuntimeError) as exc:
        print(f"Checklist was not written: {exc}")
        return 1
    print(f"Checklist written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
