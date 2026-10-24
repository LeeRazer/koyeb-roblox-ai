from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .defaults import ENEMY_TYPES, normalize_action


FiniteReward = Annotated[float, Field(ge=-5.0, le=5.0, allow_inf_nan=False)]
FiniteDamage = Annotated[float, Field(ge=0.0, le=10_000.0, allow_inf_nan=False)]
ActionName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_ -]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class BattleActionResultV1(StrictModel):
    enemy_type: str = Field(min_length=1, max_length=64)
    action_name: ActionName
    reward: FiniteReward
    player_dashed: bool = False
    player_health_lost: FiniteDamage = 0.0

    @field_validator("enemy_type")
    @classmethod
    def known_enemy(cls, value: str) -> str:
        if value not in ENEMY_TYPES:
            raise ValueError("enemy_type no registrado")
        return value

    @model_validator(mode="after")
    def known_action(self) -> "BattleActionResultV1":
        if normalize_action(self.enemy_type, self.action_name) is None:
            raise ValueError("action_name no registrado para este enemigo")
        return self


class CombatEvent(BattleActionResultV1):
    event_id: UUID
    occurred_at: int | None = Field(default=None, ge=0, le=4_102_444_800)
    difficulty: Literal["Easy", "Normal", "Hard", "Nightmare", "Endless"] | None = None
    room_number: int | None = Field(default=None, ge=0, le=10_000)


class CombatEventBatch(StrictModel):
    client_version: str = Field(min_length=1, max_length=32)
    universe_id: str = Field(min_length=1, max_length=32)
    place_id: str = Field(min_length=1, max_length=32)
    job_id: str = Field(min_length=1, max_length=128)
    events: list[CombatEvent] = Field(min_length=1, max_length=500)


class DirectorModifiers(StrictModel):
    horde_multiplier: float = Field(default=1.0, ge=0.50, le=1.75, allow_inf_nan=False)
    special_cap_bonus: int = Field(default=0, ge=-2, le=3)
    stress_accumulation_rate: float = Field(default=1.0, ge=0.50, le=2.0, allow_inf_nan=False)
    interval_speed: float = Field(default=1.0, ge=0.50, le=2.0, allow_inf_nan=False)


class PromoteRequest(StrictModel):
    note: str = Field(default="manual promotion", min_length=1, max_length=250)
    minimum_total_events: int = Field(default=20, ge=0, le=1_000_000_000)
    force: bool = False


class RollbackRequest(StrictModel):
    version: str = Field(min_length=1, max_length=64)
    note: str = Field(default="manual rollback", min_length=1, max_length=250)


class SnapshotResponse(StrictModel):
    schema_version: int
    version: str
    timestamp: int
    director_modifiers: dict[str, float | int]
    zombies: dict[str, dict[str, Any]]

