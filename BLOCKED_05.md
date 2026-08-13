# BLOCKED_05.md — 05 UI 四页受阻/受限项

## 1. 无独立服务导致的如实 503/501（设计内降级，非缺陷）

sim_platform 有而 superplatform 没有的服务，对应端点如实返回错误，前端显示
错误提示而非假数据：

- `/api/signals/*`（全部 503）：本平台无独立信号引擎——交易信号由双文件策略
  直接产出（03 消费层）。主界面「信号规则」面板因此不可用。
- `/api/cleaning/config`（GET/POST 503）：sim 的数据清洗管道未移植。
- `/api/backtest/factor`（501）：sim 的单标的因子买卖回测引擎未移植；
  因子效果走探索页评级/指标（04 服务）或 CLI `superplatform evaluate`。
  策略回测 `/api/backtest/strategy` 已实现（03 run_strategy 真实回测）。
- `/api/pystrategies*`：本平台无纯 Python 策略通道（只有 MD+impl 双文件），
  列表如实返回空、详情 404；`strategy_py` 上传如实 400 拒绝。

## 2. 相关性矩阵全库首算耗时长（记录在案）

`GET /api/admin/bias-control/correlation-matrix` 不带 ids 时按 04 服务对全库
（99 个在册因子 × 研究池标的）逐因子重算 + 日频网格 Spearman，首次计算
耗时可达数分钟~数十分钟（04 实现封顶 200 因子，算完落 DuckDB 缓存，之后
cache_hit 秒回）。期间 04 串行锁被占用，rating/metrics 端点会排队——
DuckDB 单写者的固有约束，与 03/04 的备注一致。

## 3. 主界面「因子最新值」列显示 `--`（如实体现在 PROGRESS_05）

本平台无常驻因子值计算（03 评估按需触发、不落 factor_value 表），
`/api/state` 的 `factor_values` 与因子列表的 `latest_values` 如实为空，
前端显示 `--`。因子时序图走 `/api/factors/{id}/series` 按需真实计算，不受影响。

## 4. live 持仓方向语义沿用 03 既有行为

模拟盘 DEM-001 的多单按 exchangia 消费层语义落成 spot 持仓（category=spot），
与 sim 的 perp 持仓展示略有差异；是 03 记录的既有语义，未改。

其余无阻塞。
