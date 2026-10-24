from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import logging
import math
import time
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    event,
    func,
    inspect,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

from .config import Settings
from .defaults import DEFAULT_DIRECTOR_MODIFIERS, default_zombies, normalize_action


LOGGER = logging.getLogger(__name__)
metadata = MetaData()


candidate_weights = Table(
    "ai_candidate_weights",
    metadata,
    Column("enemy_type", String(64), primary_key=True),
    Column("tactics", JSON, nullable=False),
    Column("q_values", JSON, nullable=False),
    Column("total_battles", Integer, nullable=False, default=0),
    Column("last_updated", Float, nullable=False),
)


battle_events = Table(
    "ai_battle_events",
    metadata,
    Column("event_id", String(36), primary_key=True),
    Column("enemy_type", String(64), nullable=False, index=True),
    Column("action_name", String(64), nullable=False, index=True),
    Column("raw_action_name", String(64), nullable=False),
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


daily_action_stats = Table(
    "ai_daily_action_stats",
    metadata,
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


runtime_state = Table(
    "ai_runtime_state",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("active_version", String(64), nullable=False),
    Column("candidate_revision", Integer, nullable=False, default=0),
    Column("director_modifiers", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
)


model_versions = Table(
    "ai_model_versions",
    metadata,
    Column("version", String(64), primary_key=True),
    Column("status", String(16), nullable=False, index=True),
    Column("snapshot", JSON, nullable=False),
    Column("note", String(250), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("promoted_at", Float, nullable=True),
)


admin_audit = Table(
    "ai_admin_audit",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("action", String(32), nullable=False),
    Column("details", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


class AIStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._last_prune_monotonic = 0.0
        engine_options: dict[str, Any] = {"pool_pre_ping": True, "future": True}
        if settings.database_url.startswith("sqlite:"):
            engine_options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if settings.database_url in {"sqlite://", "sqlite:///:memory:"}:
                engine_options["poolclass"] = StaticPool

        self.engine = create_engine(settings.database_url, **engine_options)
        self.is_sqlite = self.engine.dialect.name == "sqlite"
        self.is_postgresql = self.engine.dialect.name == "postgresql"
        if not (self.is_sqlite or self.is_postgresql):
            raise RuntimeError(f"Motor de base de datos no soportado: {self.engine.dialect.name}")

        if self.is_sqlite:
            self._configure_sqlite()

    def _configure_sqlite(self) -> None:
        @event.listens_for(self.engine, "connect")
        def sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        connection = self.engine.connect()
        transaction = None
        try:
            if self.is_sqlite:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                transaction = connection.begin()
            yield connection
            if transaction is not None:
                transaction.commit()
            else:
                connection.commit()
        except Exception:
            if transaction is not None:
                transaction.rollback()
            else:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        metadata.create_all(self.engine)
        with self.transaction() as connection:
            now = time.time()
            for enemy_type, data in default_zombies().items():
                self._insert_ignore(
                    connection,
                    candidate_weights,
                    {
                        "enemy_type": enemy_type,
                        "tactics": data["tactics"],
                        "q_values": data["q_values"],
                        "total_battles": 0,
                        "last_updated": now,
                    },
                )

            self._migrate_legacy_if_present(connection)

            state_created = self._insert_ignore(
                connection,
                runtime_state,
                {
                    "id": 1,
                    "active_version": f"{self.settings.service_version}-r0",
                    "candidate_revision": 0,
                    "director_modifiers": deepcopy(DEFAULT_DIRECTOR_MODIFIERS),
                    "updated_at": now,
                },
            )

            if state_created:
                initial_version = f"{self.settings.service_version}-r0"
                self._insert_ignore(
                    connection,
                    model_versions,
                    {
                        "version": initial_version,
                        "status": "production",
                        "snapshot": {"zombies": self._candidate_snapshot(connection)},
                        "note": "initial validated defaults",
                        "created_at": now,
                        "promoted_at": now,
                    },
                )

            self._prune_raw_events(connection, now)

    def _insert_ignore(
        self,
        connection: Connection,
        table: Table,
        values: dict[str, Any],
    ) -> bool:
        if self.is_postgresql:
            statement = postgresql_insert(table).values(**values).on_conflict_do_nothing()
        else:
            statement = sqlite_insert(table).values(**values).on_conflict_do_nothing()
        return connection.execute(statement).rowcount == 1

    def _migrate_legacy_if_present(self, connection: Connection) -> None:
        if not inspect(self.engine).has_table("zombie_weights"):
            return
        try:
            rows = connection.execute(
                text(
                    "SELECT enemy_type, tactics_json, q_values_json, "
                    "total_battles FROM zombie_weights"
                )
            ).mappings()
            for row in rows:
                enemy_type = row["enemy_type"]
                existing = connection.execute(
                    select(candidate_weights).where(candidate_weights.c.enemy_type == enemy_type)
                ).mappings().first()
                legacy_battles = max(0, int(row["total_battles"] or 0))
                if not existing or existing["total_battles"] > 0 or legacy_battles <= 0:
                    continue

                tactics = json.loads(row["tactics_json"] or "{}")
                legacy_q = json.loads(row["q_values_json"] or "{}")
                normalized: dict[str, list[float]] = {}
                for legacy_action, raw_value in legacy_q.items():
                    action = normalize_action(enemy_type, legacy_action)
                    if action and isinstance(raw_value, (int, float)) and math.isfinite(raw_value):
                        normalized.setdefault(action, []).append(_clamp(float(raw_value) / 15.0, -1, 1))
                q_values = dict(existing["q_values"])
                for action, values in normalized.items():
                    q_values[action] = sum(values) / len(values)

                connection.execute(
                    update(candidate_weights)
                    .where(candidate_weights.c.enemy_type == enemy_type)
                    .values(
                        tactics=tactics if isinstance(tactics, dict) else existing["tactics"],
                        q_values=q_values,
                        total_battles=legacy_battles,
                        last_updated=time.time(),
                    )
                )
        except Exception:
            LOGGER.exception("No se pudo migrar la tabla v3; se conservaron los defaults v4")

    def _candidate_snapshot(self, connection: Connection) -> dict[str, dict[str, Any]]:
        rows = connection.execute(select(candidate_weights)).mappings().all()
        return {
            row["enemy_type"]: {
                "tactics": deepcopy(row["tactics"]),
                "q_values": deepcopy(row["q_values"]),
                "state_biases": deepcopy(row["q_values"]),
                "total_battles": int(row["total_battles"]),
            }
            for row in rows
        }

    def ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(select(1)).scalar_one()
            return True
        except Exception:
            LOGGER.exception("Database readiness check failed")
            return False

    def get_snapshot(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            state = connection.execute(
                select(runtime_state).where(runtime_state.c.id == 1)
            ).mappings().one()
            version = connection.execute(
                select(model_versions).where(
                    model_versions.c.version == state["active_version"]
                )
            ).mappings().one()
            snapshot = deepcopy(version["snapshot"])
            return {
                "schema_version": self.settings.schema_version,
                "version": version["version"],
                "timestamp": int(max(version["promoted_at"] or 0, state["updated_at"])),
                "director_modifiers": deepcopy(state["director_modifiers"]),
                "zombies": snapshot["zombies"],
            }

    def _insert_event_if_new(self, connection: Connection, values: dict[str, Any]) -> bool:
        if self.is_postgresql:
            statement = postgresql_insert(battle_events).values(**values).on_conflict_do_nothing(
                index_elements=[battle_events.c.event_id]
            )
        else:
            statement = sqlite_insert(battle_events).values(**values).on_conflict_do_nothing(
                index_elements=[battle_events.c.event_id]
            )
        result = connection.execute(statement)
        return result.rowcount == 1

    def _record_daily_aggregate(
        self,
        connection: Connection,
        *,
        enemy_type: str,
        action_name: str,
        difficulty: str,
        reward: float,
        damage: float,
        player_dashed: bool,
        received_at: float,
    ) -> None:
        key_columns = [
            daily_action_stats.c.day_utc,
            daily_action_stats.c.enemy_type,
            daily_action_stats.c.action_name,
            daily_action_stats.c.difficulty,
        ]
        values = {
            "day_utc": time.strftime("%Y-%m-%d", time.gmtime(received_at)),
            "enemy_type": enemy_type,
            "action_name": action_name,
            "difficulty": difficulty,
            "event_count": 1,
            "success_count": 1 if reward > 0 else 0,
            "dashed_count": 1 if player_dashed else 0,
            "reward_sum": reward,
            "damage_sum": damage,
            "updated_at": received_at,
        }
        if self.is_postgresql:
            statement = postgresql_insert(daily_action_stats).values(**values)
        else:
            statement = sqlite_insert(daily_action_stats).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=key_columns,
            set_={
                "event_count": daily_action_stats.c.event_count + 1,
                "success_count": daily_action_stats.c.success_count + values["success_count"],
                "dashed_count": daily_action_stats.c.dashed_count + values["dashed_count"],
                "reward_sum": daily_action_stats.c.reward_sum + reward,
                "damage_sum": daily_action_stats.c.damage_sum + damage,
                "updated_at": received_at,
            },
        )
        connection.execute(statement)

    def _prune_raw_events(self, connection: Connection, now: float) -> int:
        cutoff = now - self.settings.raw_event_retention_hours * 3_600
        result = connection.execute(
            delete(battle_events).where(battle_events.c.received_at < cutoff)
        )
        deleted_count = max(0, int(result.rowcount or 0))
        self._last_prune_monotonic = time.monotonic()
        if deleted_count:
            connection.execute(
                insert(admin_audit).values(
                    action="retention_prune",
                    details={"deleted_events": deleted_count, "cutoff": cutoff},
                    created_at=now,
                )
            )
        return deleted_count

    def prune_raw_events(self, now: float | None = None) -> int:
        """Delete expired raw rows without touching aggregates or learned policy."""
        with self.transaction() as connection:
            return self._prune_raw_events(connection, now or time.time())

    def process_batch(self, batch: dict[str, Any]) -> dict[str, int]:
        accepted = 0
        duplicates = 0
        with self.transaction() as connection:
            state_statement = select(runtime_state).where(runtime_state.c.id == 1)
            if self.is_postgresql:
                state_statement = state_statement.with_for_update()
            state = connection.execute(state_statement).mappings().one()

            for event_data in batch["events"]:
                enemy_type = event_data["enemy_type"]
                raw_action = event_data["action_name"]
                action_name = normalize_action(enemy_type, raw_action)
                if action_name is None:
                    continue

                reward = float(event_data["reward"])
                normalized_reward = _clamp(reward / 4.0, -1.0, 1.0)
                received_at = time.time()
                damage = float(event_data.get("player_health_lost", 0))
                player_dashed = bool(event_data.get("player_dashed", False))
                inserted = self._insert_event_if_new(
                    connection,
                    {
                        "event_id": str(event_data["event_id"]),
                        "enemy_type": enemy_type,
                        "action_name": action_name,
                        "raw_action_name": raw_action,
                        "reward": reward,
                        "normalized_reward": normalized_reward,
                        "player_dashed": player_dashed,
                        "player_health_lost": damage,
                        "universe_id": batch["universe_id"],
                        "place_id": batch["place_id"],
                        "job_id": batch["job_id"],
                        "client_version": batch["client_version"],
                        "context": {
                            "occurred_at": event_data.get("occurred_at"),
                            "difficulty": event_data.get("difficulty"),
                            "room_number": event_data.get("room_number"),
                        },
                        "received_at": received_at,
                    },
                )
                if not inserted:
                    duplicates += 1
                    continue

                self._record_daily_aggregate(
                    connection,
                    enemy_type=enemy_type,
                    action_name=action_name,
                    difficulty=event_data.get("difficulty") or "Unknown",
                    reward=normalized_reward,
                    damage=damage,
                    player_dashed=player_dashed,
                    received_at=received_at,
                )

                weight_statement = select(candidate_weights).where(
                    candidate_weights.c.enemy_type == enemy_type
                )
                if self.is_postgresql:
                    weight_statement = weight_statement.with_for_update()
                weight = connection.execute(weight_statement).mappings().one()
                q_values = dict(weight["q_values"])
                current_q = float(q_values.get(action_name, 0.0))
                new_q = current_q + self.settings.learning_rate * (
                    normalized_reward - current_q
                )
                q_values[action_name] = round(_clamp(new_q, -1, 1), 6)
                connection.execute(
                    update(candidate_weights)
                    .where(candidate_weights.c.enemy_type == enemy_type)
                    .values(
                        q_values=q_values,
                        total_battles=int(weight["total_battles"]) + 1,
                        last_updated=time.time(),
                    )
                )
                accepted += 1

            if accepted:
                connection.execute(
                    update(runtime_state)
                    .where(runtime_state.c.id == 1)
                    .values(candidate_revision=int(state["candidate_revision"]) + accepted)
                )

            revision = int(state["candidate_revision"]) + accepted
            if (
                time.monotonic() - self._last_prune_monotonic
                >= self.settings.prune_interval_seconds
            ):
                self._prune_raw_events(connection, time.time())
        return {"accepted": accepted, "duplicates": duplicates, "candidate_revision": revision}

    def get_storage_status(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            raw_event_count = int(
                connection.execute(select(func.count()).select_from(battle_events)).scalar_one()
            )
            oldest_raw_event = connection.execute(
                select(func.min(battle_events.c.received_at))
            ).scalar_one()
            aggregate_rows = int(
                connection.execute(select(func.count()).select_from(daily_action_stats)).scalar_one()
            )
            aggregated_events = int(
                connection.execute(
                    select(func.coalesce(func.sum(daily_action_stats.c.event_count), 0))
                ).scalar_one()
            )
            version_count = int(
                connection.execute(select(func.count()).select_from(model_versions)).scalar_one()
            )
            if self.is_postgresql:
                database_bytes = int(
                    connection.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
                )
            else:
                page_count = int(connection.exec_driver_sql("PRAGMA page_count").scalar_one())
                page_size = int(connection.exec_driver_sql("PRAGMA page_size").scalar_one())
                database_bytes = page_count * page_size

        return {
            "database": self.engine.dialect.name,
            "database_bytes": database_bytes,
            "raw_event_count": raw_event_count,
            "oldest_raw_event": oldest_raw_event,
            "raw_event_retention_hours": self.settings.raw_event_retention_hours,
            "daily_aggregate_rows": aggregate_rows,
            "aggregated_event_count": aggregated_events,
            "model_version_count": version_count,
        }

    def get_candidate(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            state = connection.execute(
                select(runtime_state).where(runtime_state.c.id == 1)
            ).mappings().one()
            zombies = self._candidate_snapshot(connection)
            return {
                "candidate_revision": int(state["candidate_revision"]),
                "active_version": state["active_version"],
                "total_events": sum(z["total_battles"] for z in zombies.values()),
                "zombies": zombies,
            }

    def promote(self, note: str, minimum_total_events: int, force: bool) -> dict[str, Any]:
        with self.transaction() as connection:
            state_statement = select(runtime_state).where(runtime_state.c.id == 1)
            if self.is_postgresql:
                state_statement = state_statement.with_for_update()
            state = connection.execute(state_statement).mappings().one()
            zombies = self._candidate_snapshot(connection)
            total_events = sum(z["total_battles"] for z in zombies.values())
            if total_events < minimum_total_events and not force:
                raise ValueError(
                    f"Se requieren {minimum_total_events} eventos; existen {total_events}"
                )

            version_name = f"{self.settings.service_version}-r{state['candidate_revision']}"
            now = time.time()
            existing = connection.execute(
                select(model_versions.c.version).where(model_versions.c.version == version_name)
            ).scalar_one_or_none()
            connection.execute(update(model_versions).values(status="archived").where(
                model_versions.c.status == "production"
            ))
            if existing is None:
                connection.execute(
                    insert(model_versions).values(
                        version=version_name,
                        status="production",
                        snapshot={"zombies": zombies},
                        note=note,
                        created_at=now,
                        promoted_at=now,
                    )
                )
            else:
                connection.execute(
                    update(model_versions)
                    .where(model_versions.c.version == version_name)
                    .values(status="production", promoted_at=now, note=note)
                )
            connection.execute(
                update(runtime_state)
                .where(runtime_state.c.id == 1)
                .values(active_version=version_name, updated_at=now)
            )
            connection.execute(
                insert(admin_audit).values(
                    action="promote",
                    details={"version": version_name, "note": note, "events": total_events},
                    created_at=now,
                )
            )
            return {"version": version_name, "total_events": total_events}

    def rollback(self, version_name: str, note: str) -> dict[str, str]:
        with self.transaction() as connection:
            target = connection.execute(
                select(model_versions).where(model_versions.c.version == version_name)
            ).mappings().first()
            if target is None:
                raise KeyError(version_name)
            now = time.time()
            connection.execute(update(model_versions).values(status="archived").where(
                model_versions.c.status == "production"
            ))
            connection.execute(
                update(model_versions)
                .where(model_versions.c.version == version_name)
                .values(status="production", promoted_at=now)
            )
            connection.execute(
                update(runtime_state)
                .where(runtime_state.c.id == 1)
                .values(active_version=version_name, updated_at=now)
            )
            connection.execute(
                insert(admin_audit).values(
                    action="rollback",
                    details={"version": version_name, "note": note},
                    created_at=now,
                )
            )
        return {"version": version_name, "status": "production"}

    def update_modifiers(self, modifiers: dict[str, float | int]) -> dict[str, float | int]:
        now = time.time()
        with self.transaction() as connection:
            connection.execute(
                update(runtime_state)
                .where(runtime_state.c.id == 1)
                .values(director_modifiers=modifiers, updated_at=now)
            )
            connection.execute(
                insert(admin_audit).values(
                    action="runtime_config",
                    details={"director_modifiers": modifiers},
                    created_at=now,
                )
            )
        return modifiers

    def export_state(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            versions = [dict(row) for row in connection.execute(
                select(model_versions).order_by(model_versions.c.created_at.desc())
            ).mappings().all()]
            event_count = connection.execute(
                select(func.count()).select_from(battle_events)
            ).scalar_one()
        return {
            "snapshot": self.get_snapshot(),
            "candidate": self.get_candidate(),
            "versions": versions,
            "event_count": int(event_count),
            "storage": self.get_storage_status(),
        }

