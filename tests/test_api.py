from __future__ import annotations

from pathlib import Path
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.config import Settings
from app.store import battle_events
from app.platform_store import project_api_keys
from main import create_app


API_KEY = "test-key-that-is-not-used-in-production"


def make_client(
    tmp_path: Path,
    *,
    api_key: str | None = API_KEY,
    admin_api_key: str | None = None,
) -> TestClient:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = (tmp_path / "test.db").as_posix()
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{database}",
        api_key=api_key,
        write_rate_limit_per_minute=1_000,
        max_batch_size=100,
        learning_rate=0.12,
        admin_api_key=admin_api_key,
    )
    return TestClient(create_app(settings))


def event_batch(event_id: str, reward: float = 4.0) -> dict:
    return {
        "client_version": "test-2",
        "universe_id": "123",
        "place_id": "456",
        "job_id": "job-test",
        "events": [
            {
                "event_id": event_id,
                "enemy_type": "ZombittCarcelero",
                "action_name": "AttackGrab",
                "reward": reward,
                "player_dashed": False,
                "player_health_lost": 10,
                "difficulty": "Normal",
                "room_number": 2,
            }
        ],
    }


def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def test_snapshot_is_stable_and_v1_compatible(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = client.get("/api/v2/snapshot")
        second = client.get("/api/v2/snapshot")
        legacy = client.get("/api/v1/zombie-brain-weights")

        assert first.status_code == 200
        assert first.json()["timestamp"] == second.json()["timestamp"]
        assert first.json()["version"] == "4.0.0-r0"
        assert legacy.status_code == 200
        assert legacy.json()["zombies"] == first.json()["zombies"]


def test_writes_require_key_and_can_be_disabled(tmp_path: Path) -> None:
    event = event_batch(str(uuid4()))
    with make_client(tmp_path / "enabled") as client:
        assert client.post("/api/v2/events/batch", json=event).status_code == 401
        assert client.post(
            "/api/v2/events/batch",
            json=event,
            headers={"X-API-Key": "wrong"},
        ).status_code == 401

    with make_client(tmp_path / "disabled", api_key=None) as client:
        assert client.post("/api/v2/events/batch", json=event).status_code == 503


def test_events_are_idempotent_and_candidate_isolated(tmp_path: Path) -> None:
    event_id = str(uuid4())
    with make_client(tmp_path) as client:
        production_before = client.get("/api/v2/snapshot").json()

        accepted = client.post(
            "/api/v2/events/batch", json=event_batch(event_id), headers=auth()
        )
        duplicate = client.post(
            "/api/v2/events/batch", json=event_batch(event_id), headers=auth()
        )
        candidate = client.get("/api/v2/admin/candidate", headers=auth()).json()
        production_after = client.get("/api/v2/snapshot").json()

        assert accepted.json()["accepted"] == 1
        assert duplicate.json()["accepted"] == 0
        assert duplicate.json()["duplicates"] == 1
        assert candidate["candidate_revision"] == 1
        assert candidate["zombies"]["ZombittCarcelero"]["total_battles"] == 1
        assert production_after == production_before


def test_promote_and_rollback(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post(
            "/api/v2/events/batch",
            json=event_batch(str(uuid4())),
            headers=auth(),
        )
        promoted = client.post(
            "/api/v2/admin/promote",
            json={"note": "validated test", "minimum_total_events": 1},
            headers=auth(),
        )
        snapshot = client.get("/api/v2/snapshot").json()

        assert promoted.status_code == 200
        assert promoted.json()["version"] == "4.0.0-r1"
        assert snapshot["version"] == "4.0.0-r1"
        assert snapshot["zombies"]["ZombittCarcelero"]["total_battles"] == 1

        rolled_back = client.post(
            "/api/v2/admin/rollback",
            json={"version": "4.0.0-r0", "note": "test rollback"},
            headers=auth(),
        )
        assert rolled_back.status_code == 200
        assert client.get("/api/v2/snapshot").json()["version"] == "4.0.0-r0"


def test_runtime_config_validation_and_retired_backup(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        updated = client.put(
            "/api/v2/admin/runtime-config",
            json={
                "horde_multiplier": 1.2,
                "special_cap_bonus": 1,
                "stress_accumulation_rate": 1.1,
                "interval_speed": 0.9,
            },
            headers=auth(),
        )
        invalid = client.put(
            "/api/v2/admin/runtime-config",
            json={"horde_multiplier": 99},
            headers=auth(),
        )

        assert updated.status_code == 200
        assert client.get("/api/v2/snapshot").json()["director_modifiers"][
            "horde_multiplier"
        ] == 1.2
        assert invalid.status_code == 422
        assert client.get("/api/v1/backup-db").status_code == 410


def test_unknown_action_is_rejected(tmp_path: Path) -> None:
    payload = event_batch(str(uuid4()))
    payload["events"][0]["action_name"] = "not-a-real-state"
    with make_client(tmp_path) as client:
        response = client.post("/api/v2/events/batch", json=payload, headers=auth())
        assert response.status_code == 422


def test_raw_retention_preserves_aggregates_and_learning(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for _ in range(2):
            response = client.post(
                "/api/v2/events/batch",
                json=event_batch(str(uuid4())),
                headers=auth(),
            )
            assert response.status_code == 200

        before = client.get("/api/v2/admin/storage", headers=auth()).json()
        assert before["raw_event_count"] == 2
        assert before["aggregated_event_count"] == 2
        assert before["daily_aggregate_rows"] == 1

        store = client.app.state.store
        with store.transaction() as connection:
            connection.execute(
                update(battle_events).values(received_at=time.time() - 4 * 24 * 3_600)
            )
        assert store.prune_raw_events() == 2

        after = client.get("/api/v2/admin/storage", headers=auth()).json()
        candidate = client.get("/api/v2/admin/candidate", headers=auth()).json()
        assert after["raw_event_count"] == 0
        assert after["aggregated_event_count"] == 2
        assert candidate["zombies"]["ZombittCarcelero"]["total_battles"] == 2


def test_v3_projects_are_isolated_and_keys_can_be_revoked(tmp_path: Path) -> None:
    admin_key = "separate-platform-admin-key"
    admin_headers = {"X-Admin-Key": admin_key}
    with make_client(tmp_path, admin_api_key=admin_key) as client:
        assert client.get("/api/v3/admin/projects", headers=auth()).status_code == 401

        first = client.post(
            "/api/v3/admin/projects",
            json={
                "name": "First Game",
                "slug": "first-game",
                "universe_id": "1001",
                "place_ids": ["2001"],
            },
            headers=admin_headers,
        )
        second = client.post(
            "/api/v3/admin/projects",
            json={
                "name": "Second Game",
                "slug": "second-game",
                "universe_id": "1002",
                "place_ids": ["2002"],
            },
            headers=admin_headers,
        )
        assert first.status_code == 200
        assert second.status_code == 200

        first_data = first.json()
        second_data = second.json()
        first_key = first_data["key"]["api_key"]
        second_key = second_data["key"]["api_key"]
        first_project = first_data["project"]["project_id"]
        second_project = second_data["project"]["project_id"]

        with client.app.state.store.engine.connect() as connection:
            stored_key = connection.execute(
                select(project_api_keys).where(
                    project_api_keys.c.key_id == first_data["key"]["key_id"]
                )
            ).mappings().one()
        assert stored_key["key_hash"] != first_key
        assert first_key not in stored_key.values()

        first_game_headers = {
            "X-API-Key": first_key,
            "X-Roblox-Universe-Id": "1001",
            "X-Roblox-Place-Id": "2001",
        }
        assert client.get("/api/v3/snapshot", headers=first_game_headers).status_code == 200
        assert client.get(
            "/api/v3/snapshot",
            headers={
                "X-API-Key": first_key,
                "X-Roblox-Universe-Id": "1001",
                "X-Roblox-Place-Id": "9999",
            },
        ).status_code == 403

        payload = event_batch(str(uuid4()))
        payload["universe_id"] = "1001"
        payload["place_id"] = "2001"
        learned = client.post(
            "/api/v3/events/batch",
            json=payload,
            headers={"X-API-Key": first_key},
        )
        assert learned.status_code == 200
        assert learned.json()["accepted"] == 1

        opposite_payload = event_batch(str(uuid4()), reward=-4.0)
        opposite_payload["universe_id"] = "1001"
        opposite_payload["place_id"] = "2001"
        opposite_payload["events"][0]["player_dashed"] = True
        opposite_payload["events"][0]["player_health_lost"] = 6
        learned_opposite = client.post(
            "/api/v3/events/batch",
            json=opposite_payload,
            headers={"X-API-Key": first_key},
        )
        assert learned_opposite.status_code == 200
        assert learned_opposite.json()["accepted"] == 1

        first_candidate = client.get(
            f"/api/v3/admin/projects/{first_project}/candidate",
            headers=admin_headers,
        ).json()
        second_candidate = client.get(
            f"/api/v3/admin/projects/{second_project}/candidate",
            headers=admin_headers,
        ).json()
        assert first_candidate["total_events"] == 2
        assert second_candidate["total_events"] == 0

        blocked_promotion = client.post(
            f"/api/v3/admin/projects/{first_project}/promote",
            json={
                "note": "must not pass without evidence",
                "minimum_total_events": 1,
                "minimum_samples_per_action": 3,
                "minimum_eligible_actions": 1,
            },
            headers=admin_headers,
        )
        assert blocked_promotion.status_code == 409
        assert "Evidencia insuficiente" in blocked_promotion.json()["detail"]
        assert client.get("/api/v3/snapshot", headers=first_game_headers).json()[
            "version"
        ] == "1-r0"

        promoted = client.post(
            f"/api/v3/admin/projects/{first_project}/promote",
            json={
                "note": "isolated test",
                "minimum_total_events": 1,
                "minimum_samples_per_action": 2,
                "minimum_eligible_actions": 1,
            },
            headers=admin_headers,
        )
        assert promoted.status_code == 200
        assert promoted.json()["version"] == "1-r2"
        assert client.get("/api/v3/snapshot", headers=first_game_headers).json()[
            "version"
        ] == "1-r2"

        evaluation = client.get(
            f"/api/v3/admin/projects/{first_project}/evaluation",
            params={"minimum_samples_per_action": 2},
            headers=admin_headers,
        ).json()
        assert evaluation["total_samples"] == 2
        assert evaluation["eligible_actions"] == 1
        action_evaluation = evaluation["actions"][0]
        assert action_evaluation["sample_count"] == 2
        assert action_evaluation["reward_mean"] == 0
        assert action_evaluation["reward_variance"] == 2
        assert action_evaluation["standard_error"] == 1
        assert action_evaluation["lower_95_bound"] == -1.96
        assert action_evaluation["success_rate"] == 0.5
        assert action_evaluation["dash_rate"] == 0.5
        assert action_evaluation["damage_mean"] == 8

        configured = client.put(
            f"/api/v3/admin/projects/{first_project}/runtime-config",
            json={
                "horde_multiplier": 1.1,
                "special_cap_bonus": 1,
                "stress_accumulation_rate": 1.0,
                "interval_speed": 0.9,
            },
            headers=admin_headers,
        )
        assert configured.status_code == 200
        assert client.get("/api/v3/snapshot", headers=first_game_headers).json()[
            "director_modifiers"
        ]["horde_multiplier"] == 1.1

        rolled_back = client.post(
            f"/api/v3/admin/projects/{first_project}/rollback",
            json={"version": "1-r0", "note": "isolation rollback"},
            headers=admin_headers,
        )
        assert rolled_back.status_code == 200
        assert client.get("/api/v3/snapshot", headers=first_game_headers).json()[
            "version"
        ] == "1-r0"

        wrong_tenant_payload = event_batch(str(uuid4()))
        wrong_tenant_payload["universe_id"] = "1001"
        wrong_tenant_payload["place_id"] = "2001"
        assert client.post(
            "/api/v3/events/batch",
            json=wrong_tenant_payload,
            headers={"X-API-Key": second_key},
        ).status_code == 403

        key_id = first_data["key"]["key_id"]
        revoked = client.delete(
            f"/api/v3/admin/projects/{first_project}/keys/{key_id}",
            headers=admin_headers,
        )
        assert revoked.status_code == 200
        assert client.get("/api/v3/snapshot", headers=first_game_headers).status_code == 401
