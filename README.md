# superplatform

本地量化平台：**exchangia 分层内核 + sim_platform 双文件热插拔协议 + 原生 JS/ECharts 五页 UI** 的合成体。数据从 2019 至今完整回填（币安 vision 归档），能研究（评估/评级/偏差控制六查/机器学习滚动验证），也能 10 秒起一个本地模拟盘。

- 数据层：Binance USDT-M 永续 + 现货，kline(1m/1d) + funding_rate + open_interest，DuckDB 缓存、增量回填、UTC 强校验。
- 因子/策略：MD+impl 双文件协议（因子 12 条 / 策略 10 条校验），`imports/` 落文件 10 秒内热注册、删 MD 即注销；注册表 mtime 增量 diff，支撑数千因子。
- 研究：IC/RankIC/ICIR/衰减/分层/换手 HTML 报告、前视硬门槛、S~D 评级 + 评级榜、偏差控制六查 + 合格判定，缓存按 (因子 × 数据版本) 键控。
- 机器学习：Gold 因子面板、Expanding Walk-Forward（Purge/Embargo）、牛熊震荡状态评估、逐模型自动回测、统一策略排行与风险优先评分；当前定位为研究框架，不直接实盘下单。
- 交易：策略出仓位权重、消费层转订单；回测（Sharpe/MaxDD）、`live` 模拟盘（敞口 100% 封顶、反转拆单、超资金拒单）、Binance testnet（key 只读环境变量）。
- UI：`/`（行情 K线/净值/持仓）、`/explorer.html`（因子库/评级榜）、`/bias-control.html`（六查/合格判定/相关性矩阵）、`/ml.html`（机器学习研究）、`/about.html`，原生 JS + ECharts。

## 快速开始

需要 Python ≥ 3.11（3.13 实测通过）。

```bash
python -m venv .venv
# Windows Git Bash:
.venv/Scripts/python.exe -m pip install -r requirements.txt
# 或 Linux/macOS: .venv/bin/pip install -r requirements.txt

python run.py        # http://localhost:8000 （自带 DEM-001 模拟盘会话）
```

五页：`/`、`/explorer.html`、`/bias-control.html`、`/ml.html`、`/about.html`。

## CLI 一览

```bash
superplatform backfill --symbols BTCUSDT,ETHUSDT --market both   # 回填（全量: --all）
superplatform validate-report                                    # 数据校验报告（earliest/latest/missing_pct）
superplatform factors list --page-size 20 --filter mom           # 因子清单（分页/过滤）
superplatform evaluate --factor MOM-001                          # 评估 HTML 报告（前视不过不交付）
superplatform check --factor MOM-001                             # 前视检查
superplatform backtest --strategy DEM-001                        # 回测 Sharpe/MaxDD
superplatform live --strategy DEM-001 --ticks 5 --interval 1     # 模拟盘跑 5 tick 打印状态退出
superplatform rating --factor MOM-001 --json                     # S~D 评级
superplatform rating --leaderboard --json                        # 评级榜
superplatform metrics --factor MOM-001 --json                    # 开发集深度指标
superplatform bias-check --factor MOM-001 --scope development    # 偏差控制六查 + 报告导出
```

console script 安装：`pip install -e . --no-deps`（pyproject 已声明 `superplatform` / `superplatform-web`）。

## 双文件因子/策略

K 线数据采用 Bronze/Silver/Gold 单向分层，版本化接口、质量标记和转换血缘见
[`docs/K线数据分层与接口.md`](docs/K线数据分层与接口.md)。

机器学习研究框架的训练协议、评分口径、接口和当前边界见
[`docs/机器学习研究框架.md`](docs/机器学习研究框架.md)。

策略可在 MD frontmatter 显式声明数据依赖（`data_dependencies`），平台据此
一次解析策略的全部数据集合，见
[`docs/策略数据依赖与接口.md`](docs/策略数据依赖与接口.md)：

```http
GET  /api/v1/strategies/{strategy_id}/data-requirements   # 声明 + 精确 Provider 解析
POST /api/v1/strategies/{strategy_id}/data/resolve        # 一次解析多数据依赖集合
```

内置数据依赖策略：`strategies/PYS-101_trend_following.md`（Binance spot
BTCUSDT，Gold 4h 趋势跟踪），可直接 `superplatform backtest --strategy PYS-101`
或 Web 触发回测。

格式规范：[`docs/因子格式说明.md`](docs/因子格式说明.md)、[`docs/策略格式说明.md`](docs/策略格式说明.md)（含校验规则一览与最小示例）。`factors/TEMPLATE.md`、`strategies/TEMPLATE.md` 是协议权威模板。一个插件 = 一份 MD（唯一事实来源）+ 一个 impl .py：

- 因子 impl：`compute(data: dict[data_type, dict[symbol, DataFrame]], **params) -> FactorResult`，`FactorResult.values` 含 `timestamp/value` 两列。
- 策略 impl：`generate_signals(factor_results) -> StrategySignal`，positions 列 `timestamp/symbol/position`。

内置示例：`factors/MOM-001_demo_momentum.md` + `factors/impl/demo_momentum.py`（因子）、`strategies/DEM-001_demo_threshold.md` + `strategies/impl/demo_threshold.py`（策略）。用户导入放 `imports/factors/`、`imports/strategies/`（MD 与 impl 成对），也可在 Web 因子库页上传。

## 数据回填

```bash
superplatform backfill --all     # 40 永续 + BTC/ETH 现货，2019→now，1m+1d+funding+OI
```

约 1.5 亿行 / 数小时，断点续跑（empty_range 书签），细节见 `tools/backfill.py` docstring。数据走 data.binance.vision 归档；校验报告 `superplatform validate-report` 输出每 symbol 覆盖与缺失占比。

## 目录结构

```
src/superplatform/        # 内核：data / factors / strategy / evaluation / consumption / ml / runtime / network
src/superplatform_web/    # FastAPI app + routes/（sim 形状 API → 内核服务映射）
web/                      # 五页原生 JS + ECharts（含机器学习研究工作台）
factors/ strategies/      # 内置双文件插件 + TEMPLATE.md
imports/                  # 用户导入热插拔目录
tools/                    # backfill.py / validate_report.py
tests/                    # pytest 自动化测试
PROGRESS.md               # 全阶段建造与验收记录（含红→绿反向验证证据）
BLOCKED.md                # 受阻项与取证（如：vision 永续归档自 2019-12-31 起，2019-09 段源端不存在）
```

## 测试

```bash
python -m pytest tests/ -q
```

## 已知限制

- 本仓库数据回填依赖 data.binance.vision；UM 永续归档最早 2019-12-31（2019-09 段源端不存在，取证见 `BLOCKED.md`）。
- testnet 需自备 `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`（只从环境变量读）。
- DuckDB 单写者：`run.py` 服务运行时不要并发跑评估类 CLI。
- 机器学习任务当前是单进程内存队列；服务重启后任务状态不会保留，GPU/分布式训练仅预留扩展接口。

## License

MIT，见 [LICENSE](LICENSE)。

## 致谢

本项目由两个开源项目合成而来，感谢其开发者的工作：

- **exchangia**：分层内核（数据/因子/策略/评估/消费/运行时/交易商）与多标的因子计算接口。[github.com/Exchangia/exchangia](https://github.com/Exchangia/exchangia)
- **sim_platform**：MD+impl 双文件因子/策略协议、评级与偏差控制算法、原生 JS+ECharts 四页 UI 与 API 形状。[github.com/JPGroupC/sim_platform](https://github.com/JPGroupC/sim_platform)
