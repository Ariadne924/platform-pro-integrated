"""FastAPI application — Superplatform Web dashboard.

Run with:
    uv run superplatform-web --port 8000
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import superplatform_web.state as _state
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.factors.registry import FactorRegistry
from superplatform.strategy.registry import StrategyRegistry
from superplatform_web.experiments import ExperimentStore
from superplatform_web.routes.config import router as config_router
from superplatform_web.routes.data import router as data_router
from superplatform_web.routes.evaluate_steps import router as evaluate_router
from superplatform_web.routes.evaluation import router as evaluation_router
from superplatform_web.routes.factors import router as factors_router
from superplatform_web.routes.introspect import router as introspect_router
from superplatform_web.routes.live import router as live_router
from superplatform_web.routes.market_v1 import router as market_v1_router
from superplatform_web.routes.strategies import router as strategies_router
from superplatform_web.routes.symbols import router as symbols_router

# ── Project root (where config/ lives) ─────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # src/superplatform_web/app.py → project root

# Experiments store path (monkeypatched by tests to isolate from the live DB).
_EXPERIMENTS_PATH = _PROJECT_ROOT / "data" / "research_experiments.duckdb"


async def _universe_sync_if_stale() -> None:
    """Background universe refresh; sync_universe_if_stale never raises."""
    from superplatform_web.universe import (
        prime_vision_from_universe,
        sync_universe_if_stale,
    )

    # Prime vision earliest-dates from the stored universe before (re)checking
    # staleness, so even a "not stale" startup skips archive HEAD probes.
    prime_vision_from_universe()
    await sync_universe_if_stale()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load config, register providers, discover factors & strategies."""
    from dotenv import load_dotenv
    load_dotenv()  # .env (gitignored) → os.environ; existing env vars win

    _state.reload_config()
    _state.reapply_providers()
    app.state.config = _state.config
    app.state.providers = _state.providers
    app.state.experiments = ExperimentStore(_EXPERIMENTS_PATH)
    app.state.evaluation_cache = _state.evaluation_cache

    FactorRegistry.get_instance().auto_discover()
    FactorInstanceRegistry.get_instance().build_from_config(
        _state.config, FactorRegistry.get_instance()
    )
    StrategyRegistry.get_instance().auto_discover()

    # Fire-and-forget universe sync. sync_universe_if_stale swallows all
    # errors and skips when the cache store is disabled — the store guard
    # also keeps the network call out of every TestClient test (store=None).
    if _state.store is not None:
        asyncio.create_task(_universe_sync_if_stale())

    yield
    # Shutdown: close all DuckDB connections.
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    if hasattr(app.state, "experiments") and app.state.experiments is not None:
        app.state.experiments.close()


app = FastAPI(title="Superplatform Web", version="0.1.0", lifespan=lifespan)

app.include_router(factors_router)
app.include_router(strategies_router)
app.include_router(live_router)
app.include_router(market_v1_router)
app.include_router(config_router)
app.include_router(data_router)
app.include_router(introspect_router)
app.include_router(evaluate_router)
app.include_router(evaluation_router)
app.include_router(symbols_router)

# ── Health ──────────────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Error mapping ────────────────────────────────────────────────────
# Translate common ccxt / network errors into readable HTTP responses
# so the frontend can display them without a raw traceback.

@app.exception_handler(Exception)
async def _map_errors(_request, exc):
    import asyncio as _aio

    from fastapi.responses import JSONResponse

    msg = str(exc) or type(exc).__name__
    # DNS / proxy / connection
    if isinstance(exc, (ConnectionError, TimeoutError, _aio.TimeoutError)):
        return JSONResponse(status_code=502, content={
            "error": "网络不可达",
            "detail": f"连接交易所失败。请检查代理设置或网络。原始错误: {msg}",
        })
    # ccxt errors
    if type(exc).__module__.startswith("ccxt"):
        status = 429 if "DDoS" in type(exc).__name__ or "RateLimit" in type(exc).__name__ else 502
        return JSONResponse(status_code=status, content={
            "error": "交易所请求失败",
            "detail": f"{msg}。请稍后重试。",
        })
    # DNS resolution
    if "getaddrinfo" in msg.lower() or "name resolution" in msg.lower():
        return JSONResponse(status_code=502, content={
            "error": "DNS 解析失败",
            "detail": f"无法解析交易所域名。请检查代理或 DNS 设置。原始错误: {msg}",
        })
    # Pass through — let FastAPI's default handler deal with it
    raise exc


# ── Static files (frontend) ────────────────────────────────────────
# Serve the built Vue SPA from frontend/dist/. In development you run the
# Vite dev server (:5173) and proxy /api to this app, so the mount is only
# used for the production build. If dist/ is absent the app still boots and
# serves an empty dir — build the frontend to get the dashboard.
_dist = _PROJECT_ROOT / "frontend" / "dist"
_dist.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
