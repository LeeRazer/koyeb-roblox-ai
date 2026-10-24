from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import re
import secrets
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .defaults import DEFAULT_DIRECTOR_MODIFIERS, default_zombies, normalize_action
from .store import AIStore, metadata


projects = Table(
    "platform_projects",
    metadata,
    Column("project_id", String(36), primary_key=True),
    Column("slug", String(50), nullable=False, unique=True),
    Column("name", String(100), nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("universe_id", String(32), nullable=False, index=True),
    Column("place_ids", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)


project_api_keys = Table(
    "platform_api_keys",
    metadata,
    Column("key_id", String(36), primary_key=True),
    Column(
        "project_id",
        String(36),
        ForeignKey("platform_projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("key_prefix", String(20), nullable=False, unique=True),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("label", String(80), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("revoked_at", Float, nullable=True),
)


project_candidate_weights = Table(
    "platform_candidate_weights",
    metadata,
    Column("project_id", String(36), primary_key=True),
    Column("enemy_type", String(64), primary_key=True),
    Column("tactics", JSON, nullable=False),
    Column("q_values", JSON, nullable=False),
    Column("total_battles", Integer, nullable=False, default=0),
    Column("last_updated", Float, nullable=False),
)


project_events = Table(
    "platform_battle_events",
    metadata,
    Column("project_id", String(36), primary_key=True),
    Column("event_id", String(36), primary_key=True),
    Column("enemy_type", String(64), nullable=False),
    Column("action_name", String(64), nullable=False),
    Column("reward", Float, nullable=False),
    Column("normalized_reward", Float, nullable=False),
    Column("player_dashed", Boolean, nullable=False),
    Column("player_health_lost", Float, nullable=False),
    Column("universe_id", String(32), nullable=False),
    Column("place_id", String(32), nullable=False),
    Column("job_id", String(128), nullable=False),
    Column("client_version", String(32), nullable=False),
    Column("context", JSON, nullable=False),
    Column("received_at", Float, nullable=False, index=True),
)


project_daily_stats = Table(
    "platform_daily_action_stats",
    metadata,
    Column("project_id", String(36), primary_key=True),
    Column("day_utc", String(10), primary_key=True),
    Column("enemy_type", String(64), primary_key=True),
    Column("action_name", String(64), primary_key=True),
    Column("difficulty", String(16), primary_key=True),
    Column("event_count", Integer, nullable=False, default=0),
    Column("success_count", Integer, nullable=False, default=0),
    Column("dashed_count", Integer, nullable=False, default=0),
    Column("reward_sum", Float, nullable=False, default=0),
    Column("damage_sum", Float, nullable=False, default=0),
    Column("updated_at", Float, nullable=False),
)


project_runtime_state = Table(
    "platform_runtime_state",
    metadata,
    Column("project_id", String(36), primary_key=True),
    Column("active_version", String(64), nullable=False),
    Column("candidate_revision", Integer, nullable=False, default=0),
    Column("director_modifiers", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
)


project_model_versions = Table(
    "platform_model_versions",
    metadata,
    Column("project_id", String(36), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("note", String(250), nullable=False),
    Column("pinned", Boolean, nullable=False, default=False),
    Column("created_at", Float, nullable=False),
    Column("promoted_at", Float, nullable=True),
)


project_audit = Table(
    "platform_admin_audit",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", String(36), nullable=False, index=True),
    Column("action", String(32), nullable=False),
    Column("details", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "project")[:50].rstrip("-")


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


class PlatformStore:
    """Isolated project registry and v3 learning store.


    The platform tables are additive and do not alter the live v1/v2 tables.
    """

    def __init__(self, legacy_store: AIStore, settings: Settings):
        self.legacy_store = legacy_store
        self.settings = settings
        self.engine = legacy_store.engine
        self.is_postgresql = legacy_store.is_postgresql
        self._last_prune_monotonic = 0.0

    def initialize(self) -> None:
        # AIStore.initialize calls create_all on the shared metadata. Calling it
        # again is harmless and guarantees these tables exist in isolated tests.
        metadata.create_all(self.engine)
        self.prune_raw_events()

    def _dialect_insert(self, table: Table):
        return postgresql_insert(table) if self.is_postgresql else sqlite_insert(table)

    def _candidate_snapshot(
        self,
        connection: Connection,
        project_id: str,
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            select(project_candidate_weights).where(
                project_candidate_weights.c.project_id == project_id
            )
        ).mappings().all()
        return {
            row["enemy_type"]: {
                "tactics": deepcopy(row["tactics"]),
                "q_values": deepcopy(row["q_values"]),
                "state_biases": deepcopy(row["q_values"]),
                "total_battles": int(row["total_battles"]),
            }
            for row in rows
        }

    def _new_key(self, connection: Connection, project_id: str, label: str) -> dict[str, Any]:
        raw_key = "zai_live_" + secrets.token_urlsafe(32)
        key_id = str(uuid4())
        now = time.time()
        connection.execute(
            insert(project_api_keys).values(
                key_id=key_id,
                project_id=project_id,
                key_prefix=raw_key[:20],
                key_hash=_key_hash(raw_key),
                label=label,
                created_at=now,
                revoked_at=None,
            )
        )
        return {
            "key_id": key_id,
            "label": label,
            "api_key": raw_key,
            "created_at": now,
        }

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(uuid4())
        slug = payload.get("slug") or _slugify(payload["name"])
        now = time.time()
        initial_version = "1-r0"
        try:
            with self.legacy_store.transaction() as connection:
                connection.execute(
                    insert(projects).values(
                        project_id=project_id,
                        slug=slug,
                        name=payload["name"],
                        status="active",
                        universe_id=payload["universe_id"],
                        place_ids=payload.get("place_ids", []),
                        created_at=now,
                    )
                )
                for enemy_type, data in default_zombies().items():
                    connection.execute(
                        insert(project_candidate_weights).values(
                            project_id=project_id,
                            enemy_type=enemy_type,
                            tactics=data["tactics"],
                            q_values=data["q_values"],
                            total_battles=0,
                            last_updated=now,
                        )
                    )
                snapshot = self._candidate_snapshot(connection, project_id)
                connection.execute(
                    insert(project_runtime_state).values(
                        project_id=project_id,
                        active_version=initial_version,
                        candidate_revision=0,
                        director_modifiers=deepcopy(DEFAULT_DIRECTOR_MODIFIERS),
                        updated_at=now,
                    )
                )
                connection.execute(
                    insert(project_model_versions).values(
                        project_id=project_id,
                        version=initial_version,
                        status="production",
                        snapshot={"zombies": snapshot},
                        note="initial validated defaults",
                        pinned=True,
                        created_at=now,
                        promoted_at=now,
                    )
                )
                key = self._new_key(connection, project_id, "primary")
                connection.execute(
                    insert(project_audit).values(
                        project_id=project_id,
                        action="project_created",
                        details={"slug": slug, "universe_id": payload["universe_id"]},
                        created_at=now,
                    )
                )
        except IntegrityError as exc:
            raise ValueError("El slug del proyecto ya existe") from exc

        return {
            "project": {
                "project_id": project_id,
                "slug": slug,
                "name": payload["name"],
                "status": "active",
                "universe_id": payload["universe_id"],
                "place_ids": payload.get("place_ids", []),
                "created_at": now,
            },
            "key": key,
        }

    def list_projects(self) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(projects).order_by(projects.c.created_at)
                ).mappings().all()
            ]

    def create_key(self, project_id: str, label: str) -> dict[str, Any]:
        with self.legacy_store.transaction() as connection:
            exists = connection.execute(
                select(projects.c.project_id).where(projects.c.project_id == project_id)
            ).scalar_one_or_none()
            if exists is None:
                raise KeyError(project_id)
            key = self._new_key(connection, project_id, label)
            connection.execute(
                insert(project_audit).values(
                    project_id=project_id,
                    action="key_created",
                    details={"key_id": key["key_id"], "label": label},
                    created_at=time.time(),
                )
            )
            return key

    def revoke_key(self, project_id: str, key_id: str) -> bool:
        now = time.time()
        with self.legacy_store.transaction() as connection:
            result = connection.execute(
                update(project_api_keys)
                .where(
                    project_api_keys.c.project_id == project_id,
                    project_api_keys.c.key_id == key_id,
                    project_api_keys.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            if result.rowcount != 1:
                return False
            connection.execute(
                insert(project_audit).values(
                    project_id=project_id,
                    action="key_revoked",
                    details={"key_id": key_id},
                    created_at=now,
                )
            )
        return True

    def authenticate(self, raw_key: str) -> dict[str, Any] | None:
        if not raw_key.startswith("zai_live_") or len(raw_key) < 32:
            return None
        prefix = raw_key[:20]
        with self.engine.connect() as connection:
            key_row = connection.execute(
                select(project_api_keys).where(
                    project_api_keys.c.key_prefix == prefix,
                    project_api_keys.c.revoked_at.is_(None),
                )
            ).mappings().first()
            if key_row is None or not hmac.compare_digest(
                key_row["key_hash"], _key_hash(raw_key)
            ):
                return None
            project = connection.execute(
                select(projects).where(
                    projects.c.project_id == key_row["project_id"],
                    projects.c.status == "active",
                )
            ).mappings().first()
            if project is None:
                return None
            return {"project": dict(project), "key_id": key_row["key_id"]}

    @staticmethod
    def authorize_game(auth: dict[str, Any], universe_id: str, place_id: str) -> bool:
        project = auth["project"]
        if universe_id != project["universe_id"]:
            return False
        allowed_places = project["place_ids"] or []
        return not allowed_places or place_id in allowed_places

    def get_snapshot(self, project_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            state = connection.execute(
                select(project_runtime_state).where(
                    project_runtime_state.c.project_id == project_id
                )
            ).mappings().one()
            version = connection.execute(
                select(project_model_versions).where(
                    project_model_versions.c.project_id == project_id,
                    project_model_versions.c.version == state["active_version"],
                )
            ).mappings().one()
            return {
                "schema_version": 3,
                "project_id": project_id,
                "version": version["version"],
                "timestamp": int(max(version["promoted_at"] or 0, state["updated_at"])),
                "director_modifiers": deepcopy(state["director_modifiers"]),
                "zombies": deepcopy(version["snapshot"])["zombies"],
            }

    def _insert_event(self, connection: Connection, values: dict[str, Any]) -> bool:
        statement = self._dialect_insert(project_events).values(**values).on_conflict_do_nothing(
            index_elements=[project_events.c.project_id, project_events.c.event_id]
        )
        return connection.execute(statement).rowcount == 1

    def _aggregate(
        self,
        connection: Connection,
        project_id: str,
        event: dict[str, Any],
        action_name: str,
        normalized_reward: float,
        received_at: float,
    ) -> None:
        values = {
            "project_id": project_id,
            "day_utc": time.strftime("%Y-%m-%d", time.gmtime(received_at)),
            "enemy_type": event["enemy_type"],
            "action_name": action_name,
            "difficulty": event.get("difficulty") or "Unknown",
            "event_count": 1,
            "success_count": 1 if normalized_reward > 0 else 0,
            "dashed_count": 1 if event.get("player_dashed") else 0,
            "reward_sum": normalized_reward,
            "damage_sum": float(event.get("player_health_lost", 0)),
            "updated_at": received_at,
        }
        key = [
            project_daily_stats.c.project_id,
            project_daily_stats.c.day_utc,
            project_daily_stats.c.enemy_type,
            project_daily_stats.c.action_name,
            project_daily_stats.c.difficulty,
        ]
        statement = self._dialect_insert(project_daily_stats).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=key,
            set_={
                "event_count": project_daily_stats.c.event_count + 1,
                "success_count": project_daily_stats.c.success_count + values["success_count"],
                "dashed_count": project_daily_stats.c.dashed_count + values["dashed_count"],
                "reward_sum": project_daily_stats.c.reward_sum + normalized_reward,
                "damage_sum": project_daily_stats.c.damage_sum + values["damage_sum"],
                "updated_at": received_at,
            },
        )
        connection.execute(statement)

    def process_batch(self, project_id: str, batch: dict[str, Any]) -> dict[str, int]:
        accepted = 0
        duplicates = 0
        with self.legacy_store.transaction() as connection:
            state_query = select(project_runtime_state).where(
                project_runtime_state.c.project_id == project_id
            )
            if self.is_postgresql:
                state_query = state_query.with_for_update()
            state = connection.execute(state_query).mappings().one()

            for event in batch["events"]:
                enemy_type = event["enemy_type"]
                action_name = normalize_action(enemy_type, event["action_name"])
                if action_name is None:
                    continue
                reward = float(event["reward"])
                normalized_reward = _clamp(reward / 4.0, -1.0, 1.0)
                received_at = time.time()
                values = {
                    "project_id": project_id,
                    "event_id": str(event["event_id"]),
                    "enemy_type": enemy_type,
                    "action_name": action_name,
                    "reward": reward,
                    "normalized_reward": normalized_reward,
                    "player_dashed": bool(event.get("player_dashed", False)),
                    "player_health_lost": float(event.get("player_health_lost", 0)),
                    "universe_id": batch["universe_id"],
                    "place_id": batch["place_id"],
                    "job_id": batch["job_id"],
                    "client_version": batch["client_version"],
                    "context": {
                        "encounter_id": event.get("encounter_id"),
                        "sequence_number": event.get("sequence_number"),
                        "occurred_at": event.get("occurred_at"),
                        "difficulty": event.get("difficulty"),
                        "room_number": event.get("room_number"),
                        "decision_context": event.get("decision_context"),
                    },
                    "received_at": received_at,
                }
                if not self._insert_event(connection, values):
                    duplicates += 1
                    continue

                self._aggregate(
                    connection,
                    project_id,
                    event,
                    action_name,
                    normalized_reward,
                    received_at,
                )
                weight_query = select(project_candidate_weights).where(
                    project_candidate_weights.c.project_id == project_id,
                    project_candidate_weights.c.enemy_type == enemy_type,
                )
                if self.is_postgresql:
                    weight_query = weight_query.with_for_update()
                weight = connection.execute(weight_query).mappings().one()
                q_values = dict(weight["q_values"])
                current_q = float(q_values.get(action_name, 0))
                q_values[action_name] = round(
                    _clamp(
                        current_q
                        + self.settings.learning_rate * (normalized_reward - current_q),
                        -1,
                        1,
                    ),
                    6,
                )
                connection.execute(
                    update(project_candidate_weights)
                    .where(
                        project_candidate_weights.c.project_id == project_id,
                        project_candidate_weights.c.enemy_type == enemy_type,
                    )
                    .values(
                        q_values=q_values,
                        total_battles=int(weight["total_battles"]) + 1,
                        last_updated=received_at,
                    )
                )
                accepted += 1

            revision = int(state["candidate_revision"]) + accepted
            if accepted:
                connection.execute(
                    update(project_runtime_state)
                    .where(project_runtime_state.c.project_id == project_id)
                    .values(candidate_revision=revision)
                )

        if time.monotonic() - self._last_prune_monotonic >= self.settings.prune_interval_seconds:
            self.prune_raw_events()
        return {"accepted": accepted, "duplicates": duplicates, "candidate_revision": revision}

    def get_candidate(self, project_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            state = connection.execute(
                select(project_runtime_state).where(
                    project_runtime_state.c.project_id == project_id
                )
            ).mappings().one()
            zombies = self._candidate_snapshot(connection, project_id)
            return {
                "project_id": project_id,
                "candidate_revision": int(state["candidate_revision"]),
                "active_version": state["active_version"],
                "total_events": sum(item["total_battles"] for item in zombies.values()),
                "zombies": zombies,
            }

    def promote(
        self,
        project_id: str,
        note: str,
        minimum_total_events: int,
        force: bool,
    ) -> dict[str, Any]:
        with self.legacy_store.transaction() as connection:
            state_query = select(project_runtime_state).where(
                project_runtime_state.c.project_id == project_id
            )
            if self.is_postgresql:
                state_query = state_query.with_for_update()
            state = connection.execute(state_query).mappings().one()
            zombies = self._candidate_snapshot(connection, project_id)
            total_events = sum(item["total_battles"] for item in zombies.values())
            if total_events < minimum_total_events and not force:
                raise ValueError(
                    f"Se requieren {minimum_total_events} eventos; existen {total_events}"
                )
            version = f"1-r{state['candidate_revision']}"
            now = time.time()
            connection.execute(
                update(project_model_versions)
                .where(
                    project_model_versions.c.project_id == project_id,
                    project_model_versions.c.status == "production",
                )
                .values(status="archived")
            )
            statement = self._dialect_insert(project_model_versions).values(
                project_id=project_id,
                version=version,
                status="production",
                snapshot={"zombies": zombies},
                note=note,
                pinned=False,
                created_at=now,
                promoted_at=now,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    project_model_versions.c.project_id,
                    project_model_versions.c.version,
                ],
                set_={"status": "production", "note": note, "promoted_at": now},
            )
            connection.execute(statement)
            connection.execute(
                update(project_runtime_state)
                .where(project_runtime_state.c.project_id == project_id)
                .values(active_version=version, updated_at=now)
            )
            connection.execute(
                insert(project_audit).values(
                    project_id=project_id,
                    action="promote",
                    details={"version": version, "events": total_events, "note": note},
                    created_at=now,
                )
            )
            self._prune_archived_versions(connection, project_id)
        return {"version": version, "total_events": total_events}

    def _prune_archived_versions(self, connection: Connection, project_id: str) -> int:
        stale_versions = connection.execute(
            select(project_model_versions.c.version)
            .where(
                project_model_versions.c.project_id == project_id,
                project_model_versions.c.status == "archived",
                project_model_versions.c.pinned.is_(False),
            )
            .order_by(project_model_versions.c.created_at.desc())
            .offset(self.settings.max_archived_versions)
        ).scalars().all()
        if not stale_versions:
            return 0
        result = connection.execute(
            delete(project_model_versions).where(
                project_model_versions.c.project_id == project_id,
                project_model_versions.c.version.in_(stale_versions),
            )
        )
        return max(0, int(result.rowcount or 0))

    def rollback(self, project_id: str, version: str, note: str) -> dict[str, str]:
        with self.legacy_store.transaction() as connection:
            target = connection.execute(
                select(project_model_versions).where(
                    project_model_versions.c.project_id == project_id,
                    project_model_versions.c.version == version,
                )
            ).mappings().first()
            if target is None:
                raise KeyError(version)
            now = time.time()
            connection.execute(
                update(project_model_versions)
                .where(
                    project_model_versions.c.project_id == project_id,
                    project_model_versions.c.status == "production",
                )
                .values(status="archived")
            )
            connection.execute(
                update(project_model_versions)
                .where(
                    project_model_versions.c.project_id == project_id,
                    project_model_versions.c.version == version,
                )
                .values(status="production", promoted_at=now)
            )
            connection.execute(
                update(project_runtime_state)
                .where(project_runtime_state.c.project_id == project_id)
                .values(active_version=version, updated_at=now)
            )
            connection.execute(
                insert(project_audit).values(
                    project_id=project_id,
                    action="rollback",
                    details={"version": version, "note": note},
                    created_at=now,
                )
            )
        return {"project_id": project_id, "version": version, "status": "production"}

    def update_modifiers(
        self,
        project_id: str,
        modifiers: dict[str, float | int],
    ) -> dict[str, float | int]:
        now = time.time()
        with self.legacy_store.transaction() as connection:
            result = connection.execute(
                update(project_runtime_state)
                .where(project_runtime_state.c.project_id == project_id)
                .values(director_modifiers=modifiers, updated_at=now)
            )
            if result.rowcount != 1:
                raise KeyError(project_id)
            connection.execute(
                insert(project_audit).values(
                    project_id=project_id,
                    action="runtime_config",
                    details={"director_modifiers": modifiers},
                    created_at=now,
                )
            )
        return modifiers

    def prune_raw_events(self, now: float | None = None) -> int:
        current = now or time.time()
        cutoff = current - self.settings.raw_event_retention_hours * 3_600
        with self.legacy_store.transaction() as connection:
            result = connection.execute(
                delete(project_events).where(project_events.c.received_at < cutoff)
            )
        self._last_prune_monotonic = time.monotonic()
        return max(0, int(result.rowcount or 0))

    def storage_status(self, project_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            raw_events = int(
                connection.execute(
                    select(func.count()).select_from(project_events).where(
                        project_events.c.project_id == project_id
                    )
                ).scalar_one()
            )
            aggregate_events = int(
                connection.execute(
                    select(func.coalesce(func.sum(project_daily_stats.c.event_count), 0)).where(
                        project_daily_stats.c.project_id == project_id
                    )
                ).scalar_one()
            )
        return {
            "project_id": project_id,
            "raw_event_count": raw_events,
            "aggregated_event_count": aggregate_events,
            "raw_event_retention_hours": self.settings.raw_event_retention_hours,
        }
