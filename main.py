from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status

from app.config import Settings, load_settings
from app.schemas import (
    BattleActionResultV1,
    CombatEventBatch,
    DirectorModifiers,
    PromoteRequest,
    RollbackRequest,
    SnapshotResponse,
)
from app.security import WriteGuard
from app.store import AIStore


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("roblox-ai")


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or load_settings()
    store = AIStore(configured)
    guard = WriteGuard(configured)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        store.initialize()
        LOGGER.info(
            "Roblox AI service %s ready (database=%s, writes=%s)",
            configured.service_version,
            store.engine.dialect.name,
            "enabled" if configured.api_key else "disabled",
        )
        if not configured.persistent_database:
            LOGGER.warning(
                "SQLite activo: en Koyeb los datos se pierden al recrear la instancia "
                "salvo que SQLITE_PATH apunte a un volumen persistente"
            )
        try:
            yield
        finally:
            store.engine.dispose()

    app = FastAPI(
        title="Roblox Zombie AI Learning Service",
        version=configured.service_version,
        lifespan=lifespan,
    )
    app.state.settings = configured
    app.state.store = store

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "status": "online",
            "version": configured.service_version,
            "schema_version": configured.schema_version,
            "writes_enabled": configured.api_key is not None,
            "persistent_database": configured.persistent_database,
        }

    @app.get("/health")
    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok", "version": configured.service_version}

    @app.get("/health/ready")
    def health_ready() -> dict[str, str]:
        if not store.ready():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database unavailable",
            )
        return {"status": "ready", "database": store.engine.dialect.name}

    @app.get("/api/v2/snapshot", response_model=SnapshotResponse)
    def get_snapshot_v2() -> dict[str, Any]:
        return store.get_snapshot()

    @app.post("/api/v2/events/batch")
    def report_batch(
        batch: CombatEventBatch,
        _access: None = Depends(guard),
    ) -> dict[str, int | str]:
        if len(batch.events) > configured.max_batch_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"El maximo configurado es {configured.max_batch_size} eventos",
            )
        result = store.process_batch(batch.model_dump(mode="json"))
        return {"status": "accepted", **result}

    # These routes keep deployed Roblox servers working during the v2 migration.
    @app.get("/api/v1/zombie-brain-weights")
    def get_snapshot_v1() -> dict[str, Any]:
        snapshot = store.get_snapshot()
        return {
            "version": snapshot["version"],
            "timestamp": snapshot["timestamp"],
            "message": "validated production snapshot",
            "director_modifiers": snapshot["director_modifiers"],
            "zombies": snapshot["zombies"],
        }

    @app.post("/api/v1/report-zombie-action")
    def report_action_v1(
        action: BattleActionResultV1,
        request: Request,
        _access: None = Depends(guard),
    ) -> dict[str, int | str]:
        batch = {
            "client_version": request.headers.get("X-AI-Client-Schema", "v1-compat"),
            "universe_id": request.headers.get("X-Roblox-Universe-Id", "unknown"),
            "place_id": request.headers.get("X-Roblox-Place-Id", "unknown"),
            "job_id": request.headers.get("X-Roblox-Job-Id", "v1-compat"),
            "events": [{"event_id": str(uuid4()), **action.model_dump(mode="json")}],
        }
        result = store.process_batch(batch)
        return {"status": "accepted", "server_version": configured.service_version, **result}

    @app.get("/api/v1/backup-db")
    def retired_public_backup() -> None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="El backup publico fue retirado; usa el export autenticado",
        )

    @app.get("/api/v2/admin/candidate")
    def get_candidate(_access: None = Depends(guard)) -> dict[str, Any]:
        return store.get_candidate()

    @app.post("/api/v2/admin/promote")
    def promote(
        request: PromoteRequest,
        _access: None = Depends(guard),
    ) -> dict[str, Any]:
        try:
            return {"status": "promoted", **store.promote(**request.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/api/v2/admin/rollback")
    def rollback(
        request: RollbackRequest,
        _access: None = Depends(guard),
    ) -> dict[str, str]:
        try:
            return store.rollback(request.version, request.note)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Version no encontrada",
            ) from exc

    @app.put("/api/v2/admin/runtime-config")
    def runtime_config(
        modifiers: DirectorModifiers,
        _access: None = Depends(guard),
    ) -> dict[str, Any]:
        return {
            "status": "updated",
            "director_modifiers": store.update_modifiers(modifiers.model_dump()),
        }

    @app.get("/api/v2/admin/export")
    def export_state(_access: None = Depends(guard)) -> dict[str, Any]:
        return store.export_state()

    @app.get("/api/v2/admin/storage")
    def storage_status(_access: None = Depends(guard)) -> dict[str, Any]:
        return store.get_storage_status()

    return app


app = create_app()
