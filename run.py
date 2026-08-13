"""superplatform 启动入口（00 最小壳 → 05 增强）。

- 导入 superplatform_web 的 FastAPI app；
- 自动扫描 `src/superplatform_web/routes/*.py`，把尚未注册的 router 全部 include
  （已显式 include 的路由按 `_IncludedRouter.original_router` 身份去重）；
- 静态托管项目根 `web/`（sim_platform 四页），挂在既有静态挂载之前；
- **05 增强**：
  - 所有 Mount（静态托管）挪到路由表末尾——app.include_router 只能追加路由，
    若不挪动，app.py 末尾既有的 `Mount("/", static)` 会先匹配一切路径，
    把自动 include 的 API 路由全部遮蔽成 404（探针实测）；
  - `--host/--port` 参数；
  - 默认自动启动一个模拟盘 live 会话（config `live.broker`=simulated），
    让主界面净值/持仓/账户开箱即有真实数据；`--no-autolive` 关闭，
    `--live-strategy/--live-symbols` 覆盖默认。自动会话失败只记日志，不影响服务。
- uvicorn 起服务（默认 127.0.0.1:8000）。
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.routing import Mount  # noqa: E402

from superplatform_web.app import app  # noqa: E402

# 默认自动模拟盘会话参数（可用 CLI 覆盖）
_AUTOLIVE_STRATEGY = "DEM-001"
_AUTOLIVE_SYMBOLS = ["BTCUSDT", "ETHUSDT"]


def auto_include_routes() -> list[str]:
    """扫描 routes/ 下所有 .py 模块并 include 其 `router`，返回新挂上的模块名。

    语法错误等导入异常会直接抛出并点名文件——证明自动 include 真生效。
    """
    import superplatform_web.routes as routes_pkg

    # FastAPI 新版 include_router 以 _IncludedRouter 包装入 app.routes，
    # 其 original_router 即被注册的 router 对象——按身份去重，避免重复注册。
    already = {
        id(r.original_router)
        for r in app.routes
        if type(r).__name__ == "_IncludedRouter"
    }
    included: list[str] = []
    for mod_info in pkgutil.iter_modules(routes_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"superplatform_web.routes.{mod_info.name}")
        router = getattr(module, "router", None)
        if router is None or id(router) in already:
            continue
        app.include_router(router)
        included.append(mod_info.name)
    return included


def mount_web() -> None:
    """把项目根 web/ 挂到既有静态挂载之前（空目录则 / 返回 404，服务仍在监听）。"""
    web_dir = PROJECT_ROOT / "web"
    web_dir.mkdir(exist_ok=True)
    mount = next(
        (r for r in app.routes if getattr(r, "name", "") == "static"),
        None,
    )
    idx = app.routes.index(mount) if mount is not None else len(app.routes)
    app.routes.insert(
        idx, Mount("/", app=StaticFiles(directory=str(web_dir), html=True), name="web")
    )


def mounts_to_tail() -> None:
    """把所有 Mount 挪到路由表末尾（保持相对顺序），防止静态挂载遮蔽 API 路由。

    Starlette 按注册顺序匹配，`Mount("/")` 匹配任意路径；app.py 在导入时就
    挂了 frontend/dist 的 Mount("/")，之后 include 的 API 路由若不挪动将永远
    匹配不到（05 探针 `/api/simprobe` 实测 404）。挪动后：API 路由先匹配，
    web/ 静态页兜底，frontend/dist 最后（web/ 没有的路径才轮到它）。
    """
    routes = app.router.routes
    mounts = [r for r in routes if isinstance(r, Mount)]
    others = [r for r in routes if not isinstance(r, Mount)]
    routes[:] = others + mounts


def install_autolive(
    strategy: str = _AUTOLIVE_STRATEGY,
    symbols: list[str] | None = None,
) -> None:
    """包装 app 的 lifespan：服务启动后自动跑一个模拟盘 live 会话。

    只在 run.py 进程内生效（不改 app.py，tests 的 TestClient 不受影响）。
    会话对象写进 `superplatform_web.state.live_runtime`，/api/live/* 与
    /api/state、/api/trading/* 随即读到同一个真实会话。
    """
    import asyncio
    import logging
    from contextlib import asynccontextmanager

    log = logging.getLogger("run.autolive")
    symbols = symbols or list(_AUTOLIVE_SYMBOLS)
    base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(app_):
        import superplatform_web.state as _state

        task = None

        async def _run() -> None:
            try:
                from superplatform.consumption.base import ConsumerConfig
                from superplatform.network.brokers import build_broker
                from superplatform.runtime.live import LiveRuntime

                # adapter=None → SimulatedBroker 用合成价格源（同 03 CLI live
                # 的默认行为）。本机 fapi 生产域直连超时（BLOCKED_01/03），
                # 传真实 adapter 会让 _hook_data 每 tick 卡 30s 超时且价格
                # STALE 不下单；合成源离线可跑，撮合/权益/持仓全是真实计算。
                broker = build_broker(_state.config, adapter=None, symbols=symbols)
                live = LiveRuntime(
                    _state.config,
                    _state.providers,
                    broker,
                    consumer=ConsumerConfig.backtest(),
                    symbols=symbols,
                )
                live.setup(strategy_name=strategy)
                _state.live_runtime = live
                log.info(
                    "autolive 已启动: strategy=%s broker=%s symbols=%s",
                    strategy, broker.name, symbols,
                )
                await live.start()  # 阻塞直到 stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("autolive 会话启动/运行失败（服务继续）")

        async with base_lifespan(app_):
            task = asyncio.create_task(_run())
            try:
                yield
            finally:
                live = _state.live_runtime
                if live is not None:
                    try:
                        await live.stop()
                    except Exception:
                        log.exception("autolive 停止失败")
                    _state.live_runtime = None
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    app.router.lifespan_context = _lifespan


def main() -> None:
    parser = argparse.ArgumentParser(description="superplatform Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--no-autolive",
        action="store_true",
        help="不自动启动模拟盘 live 会话（净值/持仓端点将返回「未初始化」）",
    )
    parser.add_argument("--live-strategy", default=_AUTOLIVE_STRATEGY)
    parser.add_argument(
        "--live-symbols",
        default=",".join(_AUTOLIVE_SYMBOLS),
        help="自动会话标的，逗号分隔（默认 BTCUSDT,ETHUSDT——01 已回填缓存）",
    )
    args = parser.parse_args()

    import uvicorn

    included = auto_include_routes()
    mount_web()
    mounts_to_tail()
    if not args.no_autolive:
        install_autolive(
            strategy=args.live_strategy,
            symbols=[s.strip() for s in args.live_symbols.split(",") if s.strip()],
        )
    print(f"auto-included routes: {included or '(none new)'}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
