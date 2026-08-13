"""一键启动开发环境：FastAPI 后端 (:8000) + Vite 前端 (:5173)。

用法（仓库根目录）：
    uv run dev.py

后端由 uvicorn 提供；前端是 Vite dev server，已在 vite.config.ts 里配置
/api → http://127.0.0.1:8000 代理。Ctrl+C 会同时停止两者。

某个服务若已在运行则自动跳过启动，不重复占用端口。注意：后端的数据文件
（data/research_experiments.duckdb）同一时间只允许一个进程打开，所以不要
和另一个已经占着 :8000 的后端一起跑，否则新起的后端会启动失败。
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8000
FRONTEND_PORT = 5173


def _port_open(host: str, port: int) -> bool:
    """端口已被监听 → True。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _spawn(cmd: list[str], cwd: Path) -> subprocess.Popen:
    """启动子进程；Windows 上 pnpm/vite 是 .cmd 批处理，需要经 cmd 解析。"""
    if os.name == "nt":
        return subprocess.Popen(["cmd", "/c", *cmd], cwd=str(cwd))
    return subprocess.Popen(cmd, cwd=str(cwd))


def main() -> int:
    procs: list[subprocess.Popen] = []

    def _stop(*_args):
        # 子进程与本脚本共享终端，Ctrl+C 会同时送达；terminate 只是兜底。
        for p in procs:
            try:
                p.terminate()
            except (ProcessLookupError, OSError):
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if _port_open(BACKEND_HOST, BACKEND_PORT):
        print(f"[dev] backend already running at http://{BACKEND_HOST}:{BACKEND_PORT} - skipping")
    else:
        print(f"[dev] starting backend  http://{BACKEND_HOST}:{BACKEND_PORT}")
        procs.append(_spawn(
            [sys.executable, "-m", "uvicorn", "superplatform_web.app:app",
             "--host", BACKEND_HOST, "--port", str(BACKEND_PORT)],
            ROOT,
        ))

    if _port_open(BACKEND_HOST, FRONTEND_PORT):
        print(f"[dev] frontend already running at http://{BACKEND_HOST}:{FRONTEND_PORT} - skipping")
    else:
        print(f"[dev] starting frontend http://{BACKEND_HOST}:{FRONTEND_PORT} (/api -> :{BACKEND_PORT})")
        procs.append(_spawn(["pnpm", "dev"], FRONTEND))

    # 任一进程退出后其余照常；全部退出则脚本结束。
    while procs:
        procs = [p for p in procs if p.poll() is None]
        if not procs:
            break
        time.sleep(0.5)

    print("[dev] both processes exited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
