"""superplatform 最小启动壳（00 阶段）。

- 导入 superplatform_web 的 FastAPI app；
- 自动扫描 `src/superplatform_web/routes/*.py`，把尚未注册的 router 全部 include
  （已显式 include 的路由按 (path, method) 去重，不重复注册）；
- 静态托管项目根 `web/`（空目录亦可，挂在既有静态挂载之前）；
- uvicorn 起 :8000。

05 阶段会在此壳上增强（页面、API 映射）。本阶段不写任何业务路由。
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.routing import Mount  # noqa: E402

from superplatform_web.app import app  # noqa: E402


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


def main() -> None:
    import uvicorn

    included = auto_include_routes()
    mount_web()
    print(f"auto-included routes: {included or '(none new)'}")
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
