from collections import defaultdict, deque
import hmac
import threading
import time

from fastapi import HTTPException, Request, status

from .config import Settings


class WriteGuard:
    """Authenticate and rate-limit state-changing requests."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def __call__(self, request: Request) -> None:
        if not self.settings.api_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Las escrituras estan deshabilitadas hasta configurar AI_API_KEY",
            )

        supplied = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied, self.settings.api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key invalida",
            )

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            window = self._requests[client]
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.settings.write_rate_limit_per_minute:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Demasiadas escrituras; reintenta mas tarde",
                )
            window.append(now)


class AdminGuard(WriteGuard):
    """Use a separate platform-admin secret when one is configured."""

    def __call__(self, request: Request) -> None:
        if not self.settings.admin_api_key:
            return super().__call__(request)

        admin_key = self.settings.admin_api_key
        supplied = request.headers.get("X-Admin-Key", "")
        if not hmac.compare_digest(supplied, admin_key or ""):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Admin API key invalida",
            )

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            window = self._requests[client]
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.settings.write_rate_limit_per_minute:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Demasiadas operaciones administrativas",
                )
            window.append(now)


class IdentityRateLimiter:
    """Sliding-window limiter for authenticated project identities."""

    def __init__(self, limit_per_minute: int):
        self.limit_per_minute = limit_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, identity: str) -> None:
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            window = self._requests[identity]
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.limit_per_minute:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Cuota temporal del proyecto excedida",
                )
            window.append(now)
