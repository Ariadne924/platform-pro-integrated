# superplatform

量化交易研究框架：一条从交易所原始行情到因子研究报告的完整管线，带 Web 研究仪表盘。

## 快速开始

需要 Python >= 3.10，依赖用 uv 管理。

```bash
# 装 uv（如果还没装）
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 进入项目，创建 venv，装依赖（含 dev）
cd superplatform
uv venv
uv pip install -e ".[dev]"

# 一键启动 Web 开发环境（后端 :8000 + 前端 :5173）
uv run dev.py
```

浏览器打开 http://localhost:5173 进入研究仪表盘（前端 `/api` 已代理到 :8000）。
`uv run dev.py` 的详细说明见[开发说明](#开发说明)；前端依赖首次需
`cd frontend && pnpm install`。

CLI 快速上手：

```bash
# 列出已注册因子
uv run superplatform factors list

# 评估单个因子，生成 HTML 报告到 reports/
uv run superplatform evaluate --factor momentum

# 跑所有在 config 里配了的因子，生成总览看板
uv run superplatform dashboard

# 前视偏差检查
uv run superplatform check --factor momentum

# 数据校验
uv run superplatform validate --input data/some_kline.parquet --schema kline

# G1 数据校验报告(审计 data/cache.duckdb 全库,生成 markdown + JSON 留痕)
# 干净环境缓存缺失时会自动按 config 抓取数据集;--fetch 强制(增量)补拉
uv run superplatform validate-report --cache data/cache.duckdb --output reports/data_validation_report.md
uv run superplatform validate-report --fetch    # 先抓取配置的数据集,再审计
```

## 功能总览

整体分层自底向上，每层只依赖下层，上层不关心数据的真实来源。

### 网络层 -- `network/`

对接交易所 API，把不同交易所的原始数据格式翻译成统一格式。上层不关心数据来自 Binance 还是 OKX。

`ExchangeAdapter` 抽象基类，定义了 6 个 `fetch_*` 方法和 3 个 `subscribe_*` 方法。离线模式下调 `fetch_*` 拿 DataFrame 走人。在线模式下 Runtime 持有 DataFrame，创建线程调 `subscribe_*`，网络层在该线程里持续更新 Runtime 的 DataFrame。DataFrame 所有权始终在 Runtime，网络层只有写权限。

`RateLimiter` 做 token-bucket + sliding-window 限频。`MarketType` 枚举区分 spot / perpetual / coin_futures，从网络层就打上标记，防止上游混用。

### 数据层 -- `data/`

把数据从哪来封装掉。上层只认 data_type（kline、funding_rate 之类），不认交易所。

统一 Schema：每种数据类型一个 Pydantic model，定了列名和 numpy dtype。有 KLine、Trade、OrderBook、FundingRate、OpenInterest、Basis 六种。`DataProvider` 只有一个方法 `fetch(symbol, frequency, start, end) → DataFrame`。数据校验不是 provider 的职责，Runtime 拿回 DataFrame 后自己调 `validators.py`。

`DataProviderRegistry` 热插拔，按 `{source}-{data_type}` 命名（如 `binance-kline`），支持按 data_type 查询。

`validators.py` 做接口契约 enforcement：列名对不对、dtype 对不对、时间戳是不是 UTC、有没有 spot/perp 混用、有没有缺失时间段。防的是网络层实现的 bug 静默污染上游，不是防数据本身错误。

`validation_report.py` 提供 G1 数据校验报告：只读审计 `data/cache.duckdb` 中每条
(symbol, frequency) 序列,跑完整校验套件(UTC、Schema、频率一致性、缺失区间、
异常值、重复时间戳、空值、增量更新书签),产出 Markdown 报告与同名 JSON 留痕。
报告按判定分组(每表只列 `WARN`/`PASS` 符号清单,判定唯一时折叠为「全部」),
仅展开**非通过**序列的详情以减噪;完整逐序列数据在 JSON 留痕里。
报告开头有「检查项通过情况」表,逐项给通过计数(如 `时区(UTC) 122/122`)——
审查者一眼可见每项校验都跑过、过了多少;不适用项如实标注。
另有「已知数据源限制」固定说明:经直接访问数据源归档核实为空值/缺口的
情况(如 funding_rate.mark_price 在 2023-10-31 前源不提供该字段、OI 缺口日
源端每日归档缺失或截断)在报告开头给出根因,并标注「经核实数/失败总数」,
审查者不必猜测为何空着。有异常值被标出时还会附一句方法论说明:异常检测
基于 MAD,检出的统计极端值多为真实市场波动,不代表数据一定有问题。
用 `uv run superplatform validate-report` 一键生成,覆盖 G1 三项硬性检查。
干净环境缓存缺失时,该命令会自动按 `config/factors.yaml` 推导数据集并通过
provider + 缓存层把数据抓进库后再审计(`--fetch` 可强制增量补拉)。

### 因子层 -- `factors/`

每个因子实现 `compute(data, **params) → FactorResult`。

`data` 的结构是 `list[dict[str, DataFrame]]`，list 里每个元素是一个 symbol，dict 的 key 是 data_type（`"kline"` 之类）。因子用索引区分 symbol（`data[0]` vs `data[1]`），不出现具体的币种名。`required_data` 声明需要哪些 data_type，`required_symbols` 声明需要几个 symbol（None = 任意数量）。

用 `@factor` 装饰器定义因子，自动注册到 `FactorRegistry`。`FactorRegistry.auto_discover()` 递归扫描 `factors/defs/`，放个新文件进去就能被发现。五个分类：momentum_reversal / volatility / volume_liquidity / microstructure / crypto_specific。

`@factor(...)` 可以带 `params_schema` 声明参数的类型 / 默认值 / 描述 / 范围，web 端据此自动生成参数配置和评估表单控件，改参数不用动代码。

### 策略层 -- `strategy/`

抽象基类 `Strategy`，核心方法 `generate_signals(factor_data) → DataFrame`，输出列 timestamp, symbol, position。

### 评估层 -- `evaluation/`

消费因子值和 forward returns，产出评估指标。所有函数无状态，接收 DataFrame 返回指标。

`compute_ic` / `compute_rankic` 算截面 Pearson/Spearman IC。`compute_icir` 算 mean(IC)/std(IC)、正向比例、t-stat。`compute_ic_decay` 算多期 IC 衰减。`layer_test` 按因子值分 5 层计算各层收益。`compute_turnover` 算层间换手率。`factor_correlation_matrix` 算因子间相关性。`rolling_stability` 在滚动窗口上重算 ICIR。`ForwardBiasChecker` 渐进截断数据重算，验证历史因子值不变，硬门槛，全部因子必须通过。`cost_sensitivity` 在多组手续费+滑点假设下评估扣除成本后的净收益。

评估层依赖一个前提：输入 DataFrame 里已经有 forward return 列。这个列由 Runtime 通过 `utils/forward_returns.py` 计算，因为是未来信息，只能在评估阶段用，不能进因子计算。

### 可视化层 -- `visualization/`

在评估层之上、运行层之下。不做计算，只做渲染。

`FactorReport` 生成单因子 HTML 报告，Plotly 多面板，标题标注样本区间、ICIR、前视检查结果。`FactorDashboard` 生成因子库总览（ICIR 柱状图、分类分布饼图、前视检查通过率）。

### 运行层 -- `runtime/`

唯一持有数据、控制线程的层。其他层都是无状态的转换器。

`OfflineRuntime` 是离线研究管线：读 config、创建 provider、拉数据、校验、组装 data、算因子、算 forward return、评估、前视检查、生成报告。`Config` 做 YAML 加载和 deep merge。CLI 提供 `evaluate`、`dashboard`、`factors list` 等命令。

## Web 前端

`frontend/` 是 Vue 3 + Vite + TypeScript 重建的仪表盘（替代旧的单文件 `index.html`），
通过 FastAPI 挂载生产构建产物、开发时由 Vite dev server 代理 `/api` 到后端。

### 开发模式

推荐用根目录的 `uv run dev.py` 一键启动（后端 :8000 + 前端 :5173，详见[开发说明](#开发说明)）。
也可以手动分两个终端：

```bash
# 终端 1：启动后端 API（:8000）
uv run superplatform-web --port 8000

# 终端 2：启动 Vite dev server（:5173，/api 代理到 :8000）
cd frontend && pnpm install && pnpm run dev
```

浏览器打开 http://localhost:5173 。后端 `config/*.yaml` 改动即时反映到「系统设置」页。

### 生产模式

```bash
cd frontend && pnpm run build        # 产物写入 frontend/dist/
uv run superplatform-web --port 8000     # 直接由 FastAPI 托管 SPA
```

浏览器打开 http://localhost:8000 。`dist/` 不存在时应用仍可启动（空目录），构建后即生效。

### 两大可插拔机制

1. **配置驱动表单**：后端 `default.yaml` 经 `config_schema.py` 递归生成 schema，
   「系统设置」页的 `DynamicForm` 按 type 分发控件。改 YAML 即出现新配置项，前后端零改动。
2. **仪表盘 Widget 注册**：每个 widget 是一个 `.vue` 文件 + `widgets/registry.ts` 中的一行
   注册（含列位置、顺序、条件显隐 `predicate`）。`DashboardView` 用 `useWidgetRegistry()`
   过滤排序后渲染，新增 widget 不碰任何已有组件。

## 开发说明

### 环境搭建

需要 Python >= 3.10。用 uv 管理 venv 和依赖。

```bash
# 装 uv（如果还没装）
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 进入项目，创建 venv
cd superplatform
uv venv

# 装依赖（含 dev）
uv pip install -e ".[dev]"

# 确认环境正确
uv run pytest tests/ -v
```

### 一键启动开发环境：`uv run dev.py`

仓库根目录的 `dev.py` 同时启动后端（FastAPI + uvicorn，:8000）和前端
（Vite dev server，:5173，`/api` 已代理到 :8000）。

```bash
cd superplatform
uv run dev.py
```

- **某个服务已在运行时自动跳过**，不重复占端口（比如你只想连一个已经跑着的后端时）。
- **Ctrl+C 同时停止两个服务**。
- 前端首次运行前需要先 `cd frontend && pnpm install`。
- **注意**：后端的数据文件 `data/research_experiments.duckdb` 同一时间只允许一个
  进程打开。不要和另一个已经占着 :8000 的后端一起跑，否则新起的后端会启动失败。

### 日常开发

```bash
uv run pytest tests/ -v
uv run superplatform --help
uv run superplatform factors list
```

## 配置文件

`config/default.yaml` 放样本区间、频率、评估参数、成本假设的默认值。`config/exchanges.yaml` 放交易所端点、限频参数，API key 从环境变量读不写文件。`config/factors.yaml` 放每个因子的 symbol 列表、provider 映射、参数覆盖，因子代码不硬编码这些。

## 目录

```
superplatform/
├── config/                 # YAML 配置
├── src/superplatform/
│   ├── network/            # 交易所 API 适配 + 限频
│   │   └── binance/        #   Binance 实现
│   ├── data/               # 统一 Schema + Provider + 校验
│   │   └── providers/      #   具体数据源实现
│   ├── factors/            # 因子定义 + 注册
│   │   └── defs/           #   因子代码（5 个分类目录）
│   ├── strategy/           # 策略基类
│   ├── evaluation/         # 评估指标 + 前视检查
│   ├── visualization/      # Plotly 报告生成
│   ├── runtime/            # 管线编排 + CLI + 配置
│   └── utils/              # 时间工具、forward return、日志
├── src/superplatform_web/      # FastAPI 后端（schema 动态化、因子/策略 CRUD、自省、评估步骤）
├── frontend/               # Vue 3 + Vite + TS 前端（DynamicForm + Widget 系统）
├── dev.py                  # 一键启动开发环境（uv run dev.py）
├── tests/                  # 测试
├── notebooks/              # Jupyter
├── reports/                # 生成的报告（gitignore）
└── extern/                 # 外部参考仓库
```

## 第三阶段一键评估

使用根目录脚本生成完整评估交付物：

~~~bash
python run_evaluation.py --config config/config.yaml
~~~

默认读取 data/evaluation_panel.csv。生产模式下，如果输入文件不存在会报错并以非 0
状态码退出。仅在显式传入 `--demo` 时，才会使用固定随机种子生成可复现的 demo 面板：

~~~bash
python run_evaluation.py --config config/config.yaml --demo
~~~

真实研究时，将 input.panel_path 指向已经完成 QC 的 CSV 或 Parquet 面板。

面板至少包含：

~~~text
timestamp, available_ts, entry_ts, exit_ts, is_eligible, eligibility_reason, exchange, market_type, settlement_asset, funding_included, symbol, factor_name, factor_value, ret_1, ret_5, ret_10
~~~

其中 timestamp、available_ts、entry_ts、exit_ts 必须显式带 UTC 时区。
必须满足 timestamp <= available_ts < entry_ts < exit_ts；并且
exit_ts - entry_ts 等于配置中的 horizon bars × bar_interval。ret_1、ret_5、ret_10
必须是按因子时点构造的未来简单收益，exit_ts 对应当前配置选择的 return_col。
默认配置仅评估 is_eligible=true 的动态标的池；上市时长、流动性和数据完整性
门槛应由上游数据层计算，并随输入快照保存。

`market.exchange`、`market.market_type` 和 `market.settlement_asset` 是强制样本过滤条件，
默认选择 Binance USDT 永续。两个市场必须以独立配置、独立运行目录评估；Binance 现货配置
应移除 `settlement_asset`，并设置 `market_type: spot` 和 `allow_short: false`，不会生成可交易
的多空结论。`perpetual` 默认要求每行给出 `funding_included=true`，以审计其未来收益已包含
资金费。该标记用于审计输入口径；永续评估会从按 bar 对齐的 `close` 和
`funding_rate` 构造未来收益，并将价格收益与 funding 收益相加。
主口径对每个时点/因子截面做 winsorize，保留 `factor_value_raw`、`factor_value_eval` 和
`is_outlier`，其开关及截断分位点都记录在配置与 `qc_result.json`。
`evaluated_panel.csv` 同时保留规范化的 `is_eligible` 和可多标签的
`eligibility_reason`；原因标签记录 eligibility 输入来源及上游提供的已有原因。
`evaluation_report.md` 的“样本过滤统计”表汇总过滤前、资格通过、最终选中和原因计数。

每次运行生成 outputs/{run_date}/，包含：

~~~text
decile_returns.csv
long_short_returns.csv
long_short_nav.csv
turnover.csv
ic_timeseries.csv
rank_ic_timeseries.csv
stability.csv
corr_pearson.csv
corr_spearman.csv
ic_timeseries.png
layer_nav.png
corr_pearson.png
corr_spearman.png
evaluation_report.md
failed_tasks.csv
evaluation.log
resolved_config.yaml
run_manifest.json
input_panel_snapshot.csv
evaluated_panel.csv
layer_assignment_log.csv
qc_result.json
~~~

可以通过命令行固定运行目录：

~~~bash
python run_evaluation.py --config config/config.yaml --run-date 20240131
~~~

主流程固定随机种子，记录完整日志，并将失败任务写入
failed_tasks.csv、evaluation_report.md 和 run_manifest.json。单个任务失败时，
其他独立任务仍会继续执行。

## 实验治理

`config/config.yaml` 的 `experiment` 段定义 `experiment_id`、`in_sample` 和
`out_of_sample`（均为闭区间 UTC 时间戳）。同一 `experiment_id` 第一次记录了 OOS
区间后，后续运行不能修改或移除该区间；需要新的区间时必须使用新的实验 ID。

每次运行会对收益口径、市场、预处理、分层、稳定性、相关性、成本、样本资格和 IS/OOS
窗口计算 `params_hash`，并与同一实验 ID 的历史 manifest 比对。hash 变化会写入
`governance_warnings` 并记录日志；设置
`experiment.fail_fast_on_hash_change: true` 时，运行会在加载数据前失败退出。
