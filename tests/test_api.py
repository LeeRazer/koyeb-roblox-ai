from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import Settings
from main import create_app


API_KEY = "test-key-that-is-not-used-in-production"


def make_client(tmp_path: Path, *, api_key: str | None = API_KEY) -> TestClient:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = (tmp_path / "test.db").as_posix()
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{database}",
        api_key=api_key,
        write_rate_limit_per_minute=1_000,
        max_batch_size=100,
        learning_rate=0.12,
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
