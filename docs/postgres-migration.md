# Postgres Migration Notes

The current service uses SQLite for a single-process deployment, but the storage layer is intentionally organized around repository-style methods in `archershub/storage.py` so it can be replaced later.

## Tables to preserve

- `users`
- `credentials`
- `jobs`
- `pending_actions`
- `snapshots`
- `scheduler_state`
- `job_runtime`
- `user_runtime`
- `registration_codes`

## Migration approach

1. Keep the bot, scheduler, and admin code calling `SQLiteStorage`-style methods rather than raw SQL.
2. Introduce a second storage implementation with the same public methods and dataclass return shapes.
3. Move JSON columns to `JSONB` in Postgres for `section_filters`, `priority_sections`, pending-action details, and snapshots.
4. Replace `INSERT ... ON CONFLICT` statements with Postgres equivalents while preserving idempotent scheduler writes.
5. Keep `job_runtime.next_retry_at` and scheduler timestamps in UTC `TIMESTAMPTZ`.

## Operational cutover

1. Export SQLite data table by table.
2. Import users, credentials, jobs, and runtime tables before re-enabling the scheduler.
3. Migrate snapshots and pending actions last because they are disposable but useful for continuity.
4. Point `ARCHERSHUB_DB`-style configuration at the new storage backend only after a dry-run admin health check succeeds.
