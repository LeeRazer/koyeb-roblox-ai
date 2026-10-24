# Zombie Intelligence Platform — Implementation Plan

## Objective

Turn the current private learning bridge into a public Roblox zombie-intelligence
platform without destabilizing the live game. The product will provide a
server-side SDK, tactical runtime, Director integration, telemetry ingestion,
safe model training, evaluation, versioning, and per-game isolation.

## Non-negotiable safety rules

1. Existing `/api/v1` and `/api/v2` contracts remain available until every live
   Roblox server has migrated.
2. Production policies never learn directly. Events update a candidate; only a
   validated promotion changes the snapshot consumed by games.
3. All database changes are additive first. Destructive cleanup happens only
   after a full rollback window and verified backups.
4. New Roblox behavior is guarded by a feature flag and defaults off.
5. HTTP is never used per frame. Roblox downloads compact snapshots and performs
   inference locally on the game server.
6. A bad tenant, exploit, old client, or invalid event must not affect another
   game or the global base policy.

## Target architecture

```text
Roblox server SDK
  -> authenticated batch ingestion
  -> validation / deduplication / abuse controls
  -> short-lived raw event store
  -> permanent daily aggregates + feature statistics
  -> candidate trainer
  -> offline evaluator and safety gates
  -> model registry (candidate / canary / production / archived)
  -> compact versioned snapshot
  -> Roblox local Utility AI + team coordinator + Director
```

PostgreSQL stores projects, keys, configuration, aggregates, and model metadata.
Raw training payloads are retained briefly. Large offline datasets eventually
move to compressed object storage rather than consuming transactional database
space.

## Phases

### Phase 0 — Stable private baseline (complete)

- Persistent PostgreSQL support.
- Authenticated writes.
- Idempotent v2 batches.
- Candidate/production separation.
- Manual promotion and rollback.
- Stable snapshots and legacy v1 compatibility.

Exit gate: API v4 healthy in Koyeb and existing Roblox bridge still works.

### Phase 1 — Data quality and bounded storage (complete on feature branch)

- Aggregate every accepted event by UTC day, enemy, action, and difficulty.
- Retain raw events for a configurable short period.
- Preserve aggregates and learned weights permanently.
- Expose authenticated storage diagnostics.
- Add retention and restart tests.

Exit gate: repeated cleanup cannot change candidate weights or production
snapshots; database growth is bounded under a sustained synthetic workload.

### Phase 2 — Multi-game tenancy and key management (foundation complete on feature branch)

- Add projects, environments, allowed Universe/Place IDs, SDK versions, quotas,
  and per-project model state.
- Store only hashes of generated developer keys; support rotation and revocation.
- Separate platform-admin credentials from Roblox ingestion credentials.
- Introduce `/api/v3` while v1/v2 adapt to a private legacy project.
- Enforce row isolation in every query and test cross-tenant access failures.

Exit gate: two test projects cannot read, train, promote, or roll back each
other's policies.

### Phase 3 — Advanced telemetry schema (contract started on feature branch)

- Record decision context, not raw player identity: distance bucket, line of
  sight, health/stamina bucket, cooldowns, room phase, team pressure, nearby
  allies, target isolation, difficulty, and chosen action.
- Add encounter and decision IDs for trajectories.
- Validate state/action capabilities against each project's registered schema.
- Reject impossible values, obsolete game versions, replayed events, and
  unauthorized universes.

Exit gate: telemetry is reproducible, bounded, privacy-safe, and useful for
offline evaluation.

### Phase 4 — Trainer, evaluation, and safe rollout (evaluator foundation started)

- Replace the single moving average with confidence-aware action statistics and
  contextual policies.
- Evaluate candidates against holdout encounters and fixed simulation fixtures.
- Require minimum samples and improvement thresholds.
- Support canary rollout, automatic regression detection, and rollback.
- Keep hard gameplay constraints outside learned policy control.

Exit gate: no candidate can reach production without passing deterministic and
statistical safety gates.

### Phase 5 — Roblox SDK v1

- Package sensing, Utility AI, tactical movement, target selection, special
  coordination, Director adapters, batching, caching, and DataStore fallback.
- Provide capability adapters so games can map custom enemies/actions without
  editing core SDK code.
- Add Studio diagnostics, an integration wizard, version negotiation, and a
  kill switch.

Exit gate: install into a clean test place, configure one enemy, lose Internet,
recover, and upgrade without editing SDK internals.

### Phase 6 — Dashboard and private beta

- Project/key management, allowed universes, telemetry health, policy comparison,
  promotion, rollback, quotas, and audit history.
- Onboard a small set of external Roblox developers.
- Publish data-use, retention, security, and incident-response documentation.

Exit gate: external developers can self-serve without access to another
developer's data or platform-admin operations.

## Initial data-retention policy

- Raw accepted events: 72 hours by default.
- Daily aggregates: permanent.
- Production/candidate weights: permanent.
- Archived policy versions: latest 20 plus every explicitly pinned release.
- Audit records: one year initially.

These values remain configurable per deployment and can be tightened before the
public beta.

## Rollout sequence

1. Build and test on `feature/public-ai-platform`.
2. Deploy a staging service and staging database.
3. Replay sanitized fixtures and load tests.
4. Merge additive backend changes while Roblox continues using v1.
5. Observe one full rollback window.
6. Enable the new SDK only in Studio/private servers.
7. Canary a small percentage of production servers.
8. Expand gradually; keep the old protocol until active legacy servers age out.

## Immediate implementation order

1. Phase 1 bounded storage and diagnostics.
2. Database migration framework and multi-tenant schema.
3. v3 authentication and project isolation.
4. Rich event contract and updated Roblox SDK behind a disabled flag.
5. Evaluator and canary promotion workflow.
