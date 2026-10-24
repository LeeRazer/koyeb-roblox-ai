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
    PlatformPromoteRequest,
    ProjectCreate,
    ProjectKeyCreate,
    RollbackRequest,
    SnapshotResponse,
)
from app.security import AdminGuard, IdentityRateLimiter, WriteGuard
from app.store import AIStore
from app.platform_store import PlatformStore


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("roblox-ai")


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or load_settings()
    store = AIStore(configured)
    platform = PlatformStore(store, configured)
    guard = WriteGuard(configured)
    admin_guard = AdminGuard(configured)
    platform_limiter = IdentityRateLimiter(configured.write_rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        store.initialize()
        platform.initialize()
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
        if configured.api_key and not configured.admin_api_key:
            LOGGER.warning(
                "ADMIN_API_KEY no configurada: las rutas admin usan temporalmente AI_API_KEY"
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
    app.state.platform_store = platform

    def platform_access(request: Request) -> dict[str, Any]:
        auth = platform.authenticate(request.headers.get("X-API-Key", ""))
        if auth is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Project API key invalida o revocada",
            )
        platform_limiter.check(auth["key_id"])
        return auth

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

    # v3 is an additive, opt-in public-platform contract. Existing Roblox
    # servers remain on v1/v2 until the new SDK passes staging and canary gates.
    @app.post("/api/v3/admin/projects")
    def create_platform_project(
        payload: ProjectCreate,
        _access: None = Depends(admin_guard),
    ) -> dict[str, Any]:
        try:
            return platform.create_project(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/api/v3/admin/projects")
    def list_platform_projects(
        _access: None = Depends(admin_guard),
    ) -> list[dict[str, Any]]:
        return platform.list_projects()

    @app.post("/api/v3/admin/projects/{project_id}/keys")
    def create_platform_key(
        project_id: str,
        payload: ProjectKeyCreate,
        _access: None = Depends(admin_guard),
    ) -> dict[str, Any]:
        try:
            return platform.create_key(project_id, payload.label)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proyecto no encontrado") from exc

    @app.delete("/api/v3/admin/projects/{project_id}/keys/{key_id}")
    def revoke_platform_key(
        project_id: str,
        key_id: str,
        _access: None = Depends(admin_guard),
    ) -> dict[str, str]:
        if not platform.revoke_key(project_id, key_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clave no encontrada")
        return {"status": "revoked"}

    @app.get("/api/v3/snapshot")
    def get_platform_snapshot(
        request: Request,
        auth: dict[str, Any] = Depends(platform_access),
    ) -> dict[str, Any]:
        universe_id = request.headers.get("X-Roblox-Universe-Id", "")
        place_id = request.headers.get("X-Roblox-Place-Id", "")
        if not platform.authorize_game(auth, universe_id, place_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Universe o Place no autorizado para este proyecto",
            )
        return platform.get_snapshot(auth["project"]["project_id"])

    @app.post("/api/v3/events/batch")
    def report_platform_batch(
        batch: CombatEventBatch,
        auth: dict[str, Any] = Depends(platform_access),
    ) -> dict[str, Any]:
        if len(batch.events) > configured.max_batch_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"El maximo configurado es {configured.max_batch_size} eventos",
            )
        if not platform.authorize_game(auth, batch.universe_id, batch.place_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Universe o Place no autorizado para este proyecto",
            )
        result = platform.process_batch(
            auth["project"]["project_id"],
            batch.model_dump(mode="json"),
        )
        return {"status": "accepted", **result}

    @app.get("/api/v3/admin/projects/{project_id}/candidate")
    def get_platform_candidate(
        project_id: str,
        _access: None = Depends(admin_guard),
    ) -> dict[str, Any]:
        return platform.get_candidate(project_id)

    @app.post("/api/v3/admin/projects/{project_id}/promote")
    def promote_platform_candidate(
        project_id: str,
        payload: PlatformPromoteRequest,
        _access: None = Depends(admin_guard),
    ) -> dict[str, Any]:
        try:
            return {
                "status": "promoted",
                **platform.promote(project_id, **payload.model_dump()),
            }
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/api/v3/admin/projects/{project_id}/evaluation")
    def evaluate_platform_candidate(
        project_id: str,
        minimum_samples_per_action: int = 20,
        _access: None = Depends(admin_guard),
    ) -> dict[str, Any]:
        if minimum_samples_per_action < 1 or minimum_samples_per_action > 1_000_000:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="minimum_samples_per_action fuera de rango",
            )
        return platform.evaluate(project_id, minimum_samples_per_action)

    @app.post("/api/v3/admin/projects/{project_id}/rollback")
    def rollback_platform_candidate(
        project_id: str,
        payload: RollbackRequest,
        _access: None = Depends(admin_guard),
    ) -> dict[str, str]:
        try:
            return platform.rollback(project_id, payload.version, payload.note)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Version no encontrada",
            ) from exc

    @app.put("/api/v3/admin/projects/{project_id}/runtime-config")
    def update_platform_runtime_config(
        project_id: str,
        modifiers: DirectorModifiers,
        _access: None = Depends(admin_guard),
    ) -> dict[str, Any]:
        try:
            return {
                "status": "updated",
                "director_modifiers": platform.update_modifiers(
                    project_id,
                    modifiers.model_dump(),
                ),
            }
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Proyecto no encontrado",
            ) from exc

    @app.get("/api/v3/admin/projects/{project_id}/storage")
    def get_platform_storage(
        project_id: str,
        _access: None = Depends(admin_guard),
    ) -> dict[str, Any]:
        return platform.storage_status(project_id)

    return app


app = create_app()
