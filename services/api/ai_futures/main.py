import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from ai_futures.ai import register_ai_routes
from ai_futures.alerts import run_alerts
from ai_futures.auth import require_backend_token
from ai_futures.backtest_api import register_backtest_routes
from ai_futures.bots import register_bot_routes
from ai_futures.config import Settings, get_settings
from ai_futures.db import Database
from ai_futures.exchange_credentials import register_credential_routes
from ai_futures.forecast_api import register_forecast_routes
from ai_futures.health import dependencies
from ai_futures.market import register_market_routes
from ai_futures.models import AppState
from ai_futures.paper import register_paper_routes
from ai_futures.schemas import AppStateResponse, utc_now
from ai_futures.state import seed_state

logger = logging.getLogger("ai_futures.api")


def error_response(request: Request, status: int, code: str, message: str, retryable=False):
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "details": {},
            "request_id": str(request.state.request_id),
            "retryable": retryable,
        },
    )


def create_app(settings: Settings | None = None, database=None, redis=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or get_settings()
        app.state.database = database or Database(app.state.settings)
        app.state.redis = redis or Redis.from_url(
            app.state.settings.redis_url.get_secret_value(),
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        alert_task = None
        try:
            await seed_state(app.state.database, app.state.settings.config_version)
            stop_alerts = asyncio.Event()
            alert_task = asyncio.create_task(
                run_alerts(stop_alerts, app.state.database, app.state.settings)
            )
            yield
        finally:
            if alert_task is not None:
                stop_alerts.set()
                await alert_task
            await app.state.redis.aclose()
            await app.state.database.close()

    app = FastAPI(title="AI Futures — Paper backend", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        request.state.request_id = uuid4()
        credential_path = request.url.path.startswith("/api/v1/exchange-credentials") or (
            request.url.path.startswith("/api/v1/bots/")
            and request.url.path.endswith("/live-credential")
        )
        if credential_path or request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                require_backend_token(request)
            except HTTPException:
                response = error_response(request, 401, "AUTH_REQUIRED", "Backend token required")
                response.headers["WWW-Authenticate"] = "Bearer"
                response.headers["X-Request-ID"] = str(request.state.request_id)
                if credential_path:
                    response.headers["Cache-Control"] = "no-store"
                return response
        try:
            response = await call_next(request)
        except Exception:
            logger.error("request_failed request_id=%s", request.state.request_id)
            response = error_response(request, 500, "INTERNAL_ERROR", "Request failed", True)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        if credential_path:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, exc.status_code, "HTTP_ERROR", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response(request, 422, "VALIDATION_ERROR", "Invalid request")

    @app.get("/api/v1/health/live")
    async def live():
        return {"status": "ok", "service": "api", "occurred_at": utc_now()}

    @app.get("/api/v1/health")
    @app.get("/api/v1/health/ready")
    async def ready(request: Request):
        checks = await dependencies(app.state.database, app.state.redis, app.state.settings)
        healthy = all(value == "ok" for value in checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                "mode": "paper",
                "dependencies": checks,
                "request_id": str(request.state.request_id),
                "occurred_at": utc_now().isoformat(),
            },
        )

    @app.get("/api/v1/state", response_model=AppStateResponse)
    async def state():
        async with app.state.database.sessions() as session:
            value = await session.get(AppState, 1)
            if value is None:
                raise HTTPException(503, "State not initialized")
            return value

    register_market_routes(app)
    register_paper_routes(app)
    register_bot_routes(app)
    register_credential_routes(app)
    register_ai_routes(app)
    register_backtest_routes(app)
    register_forecast_routes(app)
    return app


app = create_app()
