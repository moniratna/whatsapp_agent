# Skill update operations

## Scope and ownership

The skill update pipeline changes local Markdown files only. It never commits or pushes Git changes and does not deploy updated files. The release owner is responsible for preserving locally activated files across deployments. Assign one application on-call owner for candidate storage and database recovery, and one skill owner for content review and rollback decisions before enabling any production stage.

The existing `/api/operations/skill-review-runs` and skill improvement proposal endpoints remain a separate human-review workflow. Do not use their aggregate proposal records to populate the automated candidate queues. Keep those endpoints enabled and behavior-compatible through the transition; deprecation requires a separately approved migration plan.

The customer selection/extraction/batch lane is driven by `python -m app.workers.skill_updates`. It queues bounded scheduled runs, resumes stable selection cursors, applies stage gates, and advances ready per-type batches. The reviewer self-update lane remains a separate worker. Before any live rollout, exercise both workers against an isolated staging database and persistent private storage; do not treat a local demo as production launch approval.

## Feature flags and rollout order

All flags default to `false`. `SKILL_UPDATES_ENABLED` is the master gate. `SKILL_UPDATE_BATCH_SIZE` defaults to 50; a smaller local demo value such as 3 is supported, but should not be used for production quality evaluation. `SKILL_UPDATE_SCHEDULE_MINUTES` controls the scheduler interval. Enable stages only in this order, first in an isolated staging environment:

1. `SKILL_UPDATES_ENABLED=true` with all stage flags false. Confirm a private, persistent `SKILL_UPDATE_STORAGE_DIR` is mounted outside the repository and temporary directories.
2. `SKILL_UPDATE_EXTRACTION_ENABLED=true` while `SKILL_UPDATE_CANDIDATE_WRITES_ENABLED=false`. Compare shadow extraction outcomes with operator-reviewed examples. This stores only bounded outcomes and opaque evidence references in the run record; it does not create candidate Markdown files.
3. `SKILL_UPDATE_CANDIDATE_WRITES_ENABLED=true`. Verify candidate counts, redaction, hash matching, per-type sequence growth, permissions, and retention.
4. `SKILL_UPDATE_CONSOLIDATION_ENABLED=true`. Use synthetic batches first. Confirm exact configured-size claims, manifest hashes, second-review accounting, base hashes, and regression evidence.
5. `SKILL_UPDATE_AUTO_MERGE_ENABLED=true` only after forced partial-write failure, crash recovery, rollback, and manifest-only cleanup all pass in staging.
6. `SKILL_REVIEWER_UPDATES_ENABLED=true` independently, only after the fixed-suite reviewer comparison and individual rollback rehearsal pass.

The settings validator rejects skipping prerequisites. Turn off a later stage first, then its prerequisites. Disabling a flag stops new work at that boundary; it does not discard queued candidates or reset in-flight state. For a clean stop, disable extraction first, let already started jobs reach a durable state, then disable writes, consolidation, activation, and reviewer updates in reverse order.

## Status and metrics

Read-only admins can inspect `/api/operations/skill-updates`. The endpoint returns feature flag state, grouped counts for candidates, batches, active updates, reviewer runs and evaluations, plus recent failed or stuck items and sanitized machine error codes. It does not return candidate Markdown, transcript snapshots, evidence bodies, backup contents, or credentials. It is suitable for polling by the monitoring system; alert rules should use sustained conditions rather than one sample.

Recommended alerts:

- Any `rollback_required` active update or unexpected active-file hash conflict: page the skill owner; stop activation.
- A batch or reviewer run remaining in `updating_skills`, `applying`, or `cleanup_pending` beyond its maximum lease/recovery window: alert the on-call owner.
- Any parked retry, candidate/manifest/backup hash mismatch, missing artifact, or cleanup failure: alert and preserve the storage volume for investigation.
- Growing pending candidate age, repeated extraction rejection, repeated reviewer evaluation failure, or reviewer no-change ratio changes: create a quality review item, not an automatic merge.
- A batch with an applied update but no reviewer run is recoverable by replaying the customer activation coordinator; the unique batch constraint makes enqueue idempotent.

## Routine checks

1. Query `/api/operations/skill-updates` with an admin, supervisor, or read-only principal. Check each stage flag against the approved rollout and review queue counts by candidate type/status.
2. Confirm candidate and backup roots are mounted, owner-only (`0700` directories and `0600` files), writable by the service account, and excluded from web serving. Do not copy candidate or snapshot contents into logs or tickets.
3. Check that pending candidate volume is below the storage budget and each source type has its own independent queue. A source type with fewer than `SKILL_UPDATE_BATCH_SIZE` valid customer candidates should wait; do not combine source types to fill a batch.
4. Confirm the latest successful update has a matching `skill_active_updates` or `reviewer_update_runs` audit record, verified active hash, retained backup, and expected cleanup status.
5. Before deployment, preserve active skill file hashes and contents under the deployment's approved configuration management procedure. The database and candidate volume are not a substitute for carrying active skill files across releases.

## Retry and replay

Transient customer activation failures use bounded exponential backoff (five total attempts, one-minute initial delay, six-hour cap). Permanent validation/policy failures are parked and are not automatically retried. The batch record retains attempt timestamps, sanitized error codes, and next-attempt time. `BatchRetryCoordinator.begin_retry` claims only a due, non-parked batch and assigns a lease; rerun proposal validation before activation after any base-hash conflict.

The customer worker is independently runnable with `python -m app.workers.skill_updates` when `SKILL_UPDATES_ENABLED=true`. It requires `SKILL_UPDATE_STORAGE_DIR`; extraction, candidate writes, consolidation, and activation remain controlled by their individual stage flags. The reviewer worker is independently runnable with `python -m app.workers.reviewer_updates`. It polls queued and interrupted reviewer runs only when `SKILL_REVIEWER_UPDATES_ENABLED=true`. The shared active-file lock serializes recovery across worker processes. Replaying a queued, evaluating, applying, or cleanup-pending run is idempotent; successful cleanup removes only manifest-listed, hash-matching candidate files after the update is durable.

Do not edit database statuses or delete candidate files by hand. To replay the customer lane, invoke the existing batch service with the existing batch ID and its current lease owner, or allow the bounded retry coordinator to claim it when due. If the lease has expired, use the owning worker's lease-recovery path before replay. Do not extend a retry ceiling or change a permanent failure to transient to force progress.

## Hash mismatch and missing artifact response

1. Disable `SKILL_UPDATE_AUTO_MERGE_ENABLED` and `SKILL_REVIEWER_UPDATES_ENABLED`; leave extraction and candidate cleanup controls otherwise unchanged until the state is understood.
2. Preserve the database, candidate volume, active skill directory, and logs. Take protected copies and record their hashes; do not overwrite a mismatching file.
3. Inspect the sanitized status endpoint, batch/run row, manifest hash, update/evaluation record, and artifact metadata. Read private artifact bytes only from a restricted operator session.
4. If a source candidate or manifest hash differs, do not clean any source in that batch. Reconcile only from a verified manifest and the exact recorded candidate hashes. Missing candidates require an operator investigation; do not fabricate replacements.
5. If an active file differs from both recorded base and proposed hashes, do not restore automatically. Mark the update for operator review and compare against the durable backup and release source. Record the decision and resulting hash in the incident record.
6. After repair, run the isolated failure/replay checks and full second review again before re-enabling activation.

## Rollback

Rollback restores the exact stored pre-update bytes from the verified backup. Use `SkillActivationService.rollback(batch_id)` for customer updates and `ReviewerUpdateLane.rollback(run_id)` for reviewer updates. Each operation confirms that the current active hash matches the recorded applied hash; this prevents overwriting a later human or release edit. If the active hash differs, stop and reconcile manually. For a multi-skill batch, restore only the targets listed in that update record. Preserve evaluation and batch audit metadata; rollback does not rewind or delete the audit trail.

Reviewer rollback uses the corresponding `reviewer_update_runs.backup_relative_path` and `backup_sha256`, and restores only `skills/skill_reviewer.md`. Keep the reviewer evaluation record and the candidate outcome. After rollback, verify the restored hash equals the recorded base hash and reload all active skills through the explicit loader.

## Staging rehearsal and release checklist

Run the isolated test suite and migration cycle before rehearsal:

```sh
uv run pytest -q tests/test_skill_update_selection.py tests/test_skill_update_extraction.py \
  tests/test_skill_update_batches.py tests/test_skill_update_proposals.py \
  tests/test_skill_update_activation.py tests/test_skill_update_reviewer_lane.py
uv run alembic upgrade head
uv run alembic downgrade cd25f0e914a1
uv run alembic upgrade head
```

In staging, use a disposable database and copied skill directory. Rehearse shadow extraction, one valid candidate, a batch at the configured size, a multi-target activation, failure on the second file replacement, crash recovery after a file replacement, a tampered candidate cleanup block, reviewer no-change, reviewer pass, reviewer regression rejection, and reviewer crash recovery. Verify the active hashes and candidate counts after each scenario. Do not use production customer transcripts as fixtures.

Release approval requires a named owner, protected persistent storage, successful migration and downgrade rehearsal, documented backup restore, alert delivery, a completed staging runbook, and separately approved flags. This repository change does not enable any flag or claim that an external staging environment has been exercised.
