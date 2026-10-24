# API v3 — Multi-project preview

API v3 is additive and disabled from the live Roblox game until staging and
canary validation are complete. v1/v2 remain supported.

## Credentials

- Platform administration uses `X-Admin-Key` with `ADMIN_API_KEY`.
- A project key starts with `zai_live_`, is returned only when created, and is
  stored as a SHA-256 digest in PostgreSQL.
- Roblox sends its project key in `X-API-Key` from a server-only ModuleScript.
- Every project declares an allowed Universe ID and optional Place ID allowlist.

If `ADMIN_API_KEY` is absent, admin routes temporarily fall back to the legacy
`AI_API_KEY`. This compatibility mode is not suitable for a public deployment.

## Platform administration

### Create a project

`POST /api/v3/admin/projects`

```json
{
  "name": "Example Game",
  "slug": "example-game",
  "universe_id": "123456",
  "place_ids": ["234567"]
}
```

The response includes a project key exactly once. Store it securely.

### Other administration routes

- `GET /api/v3/admin/projects`
- `POST /api/v3/admin/projects/{project_id}/keys`
- `DELETE /api/v3/admin/projects/{project_id}/keys/{key_id}`
- `GET /api/v3/admin/projects/{project_id}/candidate`
- `POST /api/v3/admin/projects/{project_id}/promote`
- `POST /api/v3/admin/projects/{project_id}/rollback`
- `PUT /api/v3/admin/projects/{project_id}/runtime-config`
- `GET /api/v3/admin/projects/{project_id}/storage`

## Roblox routes

### Fetch snapshot

`GET /api/v3/snapshot`

Required headers:

```text
X-API-Key: zai_live_...
X-Roblox-Universe-Id: 123456
X-Roblox-Place-Id: 234567
```

### Report decisions

`POST /api/v3/events/batch`

The body keeps the v2 envelope and supports optional decision context:

```json
{
  "client_version": "roblox-sdk-3",
  "universe_id": "123456",
  "place_id": "234567",
  "job_id": "server-job-id",
  "events": [
    {
      "event_id": "053f75f0-3a14-4b74-b9cf-3887e44e2f03",
      "encounter_id": "45d727f8-64c1-41e4-a2f1-8ffd42628eb9",
      "sequence_number": 4,
      "enemy_type": "ZombittCarcelero",
      "action_name": "AttackGrab",
      "reward": 2.5,
      "player_dashed": false,
      "player_health_lost": 18,
      "difficulty": "Normal",
      "room_number": 3,
      "decision_context": {
        "distance_to_target": 24.5,
        "has_line_of_sight": true,
        "self_health_ratio": 0.72,
        "target_health_ratio": 0.48,
        "target_stamina_ratio": 0.15,
        "ability_ready": true,
        "nearby_allies": 5,
        "nearby_specials": 1,
        "target_isolated": true,
        "team_stress": 0.81,
        "room_phase": "combat",
        "previous_action": "Chase",
        "outcome_latency_ms": 920
      }
    }
  ]
}
```

Fields are bounded and unknown fields are rejected. Repeating the same
`event_id` in the same project is idempotent.

## Isolation and retention

- Candidate weights, production snapshots, events, aggregates, versions, and
  keys are scoped by project.
- A key cannot submit events for a different Universe/Place allowlist.
- Raw events expire after the configured retention period.
- Daily aggregates and learned policy survive raw-event deletion.
- Project keys can be revoked immediately without changing other projects.
