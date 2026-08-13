# PROGRESS.md — superplatform_G1 进度

## 任务 0（00 地基·基线核对）

理解的目标：以 exchangia 为骨架搬进本目录并改名 superplatform，补 requirements.txt 与最小 run.py，pytest 全绿。
顺序：00 →（01∥02）→ 03 →（04∥05），一次一任务，commit 前跑 pytest。
最大风险：①改名全局替换误伤字符串；②Binance API 本机不通（fapi/spot 均超时），回填只能靠 vision 归档；③源目录零改动需哈希自证。

- 2026-08-13 环境实测：Python 3.13.11（≥3.11 达标）、无 uv、PyPI 通、fapi/spot API 超时、data.binance.vision 通。
- 源目录哈希清单：文件数 299，清单存仓库根 `SOURCE_exchangia.sha256`（299 行全量贴进本文件不利于阅读，按「建议可换更好路」规则改为独立清单文件入库，本文件记录其 sha256 与生成/校验命令，效力等同）：
  - 清单 sha256：`7cc0c46510d2e98cf7c13b8a2877012c09f2670995569e6f7f0a13d614740a6b`
  - 生成：`cd <源> && find . -type f | sort | xargs sha256sum > <repo>/SOURCE_exchangia.sha256`
  - 校验：`cd <源> && sha256sum -c <repo>/SOURCE_exchangia.sha256`
  - 源目录（本会话实际使用）：`F:/量化/superplatform_G1/exchangia-master/exchangia-master`（用户指定的本目录内只读副本）
- pytest 基线（2026-08-13，源目录实测）：**381 passed, 0 skipped**（46.17s）；`python -c "import exchangia"` 可导入。依赖装在本仓库 `.venv`（Python 3.13.11，pandas 3.0.5 实测兼容，无需降版）。

## 00 地基 — 已完成（2026-08-13）

### 任务 1 搬运+改名+requirements.txt ✅
- 复制 pyproject.toml/config/src/tests/dev.py/README.md（README 被 pyproject `readme` 引用，缺它无法构建，故一并复制）；`src/exchangia→src/superplatform`、`src/exchangia_web→src/superplatform_web`，144 个文件全局替换 `exchangia→superplatform`/`Exchangia→Superplatform`，替换后全树 `grep -ri exchangia` 零残留；`requires-python` 提至 `>=3.11`；22+8 依赖逐项转写 `requirements.txt`。
- 唯一适配性补丁：补空目录 `frontend/dist/.gitkeep`（app.py 对 `frontend/dist` 做 `mkdir(exist_ok=True)`，未复制的父目录会导致 4 个 web 测试收集失败；不改任何被测逻辑）。
- 验收输出：`python -m pytest tests/ -q` → **381 passed, 0 skipped**（27.94s，主 venv）= 基线 381；干净 venv（`.venv-check`，装完即删）`pip install -r requirements.txt` 全装齐 → pytest **381 passed**（35.99s）。
- 反向验证：临时把 `test_data_frequency_h8` 的 `"8h"` 改 `"4h"` → `1 failed`；还原 → `4 passed`。红→绿证据齐。

### 任务 2 最小 run.py 壳 ✅
- `run.py`：导入 `superplatform_web.app:app`，`auto_include_routes()` 扫 `routes/*.py`（按 `_IncludedRouter.original_router` 身份去重，已注册 9 模块不重复挂，新增模块自动注册——探针实测 `['tmp_probe']`），`web/` 挂在既有静态挂载之前，uvicorn :8000。
- 验收输出：`python run.py` 后台起 → `GET /` = 404（web/ 为空，服务在监听）、`GET /api/health` = `{"status":"ok"}`。
- 反向验证：routes/ 放语法错误文件 `tmp_bad_syntax.py` → 启动崩溃并点名该文件 line 1 SyntaxError；删除后重启 `GET /` = 404 恢复。红→绿证据齐。

### 硬指标 2 源目录零改动 ✅
- `sha256sum -c SOURCE_exchangia.sha256`：299 文件全 OK 无不一致（基线 pytest 用 `PYTHONDONTWRITEBYTECODE=1` 防新文件落入源树）。

### BLOCKED
- 无（00 阶段）。已记录风险：Binance fapi/spot 直连 API 本机超时，仅 data.binance.vision 归档通——影响 01 回填与 03 testnet 联调，到时按实处理。
