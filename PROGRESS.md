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

---

# 01 阶段详情（PROGRESS_01.md 并入）

# PROGRESS_01.md — 01 数据层与回填(已完成,2026-08-13)

理解的目标:给 superplatform 打通 Binance 历史数据回填(vision 归档路线,
本机 fapi/api 直连超时)+ 只读校验报告;机制 + 小集(BTC/ETH 永续 +
BTC/ETH 现货)验收;40 标的全量回填命令就绪但不本次跑。

## 环境/源端实测结论(全部 curl 取证,2026-08-13)

- `fapi.binance.com` / `api.binance.com` 直连 **000 超时**(curl exit 非零);
  `data.binance.vision` 200 可用 → 回填全程 vision 归档,零 REST。
- vision UM 永续归档起点(**比任务书假设的 2019-09-25 晚**,各家族一致):
  klines(1m/1h/1d,日+月)/aggTrades 均自 **2019-12-31** 起;
  fundingRate 月归档自 **2020-01** 起;metrics(OI)日归档自 **2020-09-01**
  起(BTCUSDT;ETHUSDT 自 2021-12-01),metrics 无月归档。关键取证:
  ```
  GET 404  data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2019-09-08.zip
  GET 404  .../BTCUSDT-1m-2019-12-30.zip   (12-16~12-30 逐日全 404)
  GET 206  .../BTCUSDT-1m-2019-12-31.zip   (首行 1577750400000 = 2019-12-31T00:00Z, 1440 行)
  GET 404  .../monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2019-12.zip
  GET 206  .../monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2020-01.zip
  GET 404  .../daily/metrics/BTCUSDT/BTCUSDT-metrics-2020-08-31.zip
  GET 206  .../daily/metrics/BTCUSDT/BTCUSDT-metrics-2020-09-01.zip
  GET 200  data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2019-01.zip  (现货 2019-01 起可用)
  ```
- **归档时间戳单位不统一**(重要坑):spot 月归档自 2025-01 起 open_time
  为**微秒**(16 位,如 1735689600000000),UM 永续 2026-07 月归档仍为毫秒
  (13 位);metrics create_time 是字符串。基类 `parse_kline_archive` 固定
  `unit="ms"`,微秒值溢出回绕成垃圾时间戳,会在区间裁剪时静默丢光。
- **归档发布有滞后**:当日/前一日日归档可能尚未上架(2026-08-12 的 UM
  日 kline/metrics 404,2026-08-11 200;现货 08-12 已有)。基类
  `_earliest_archive_date` 用单日 HEAD 判"有无任何归档"并缓存结论,
  单日 404 会把 None 固化 → 整条序列静默全空。
- fundingRate 只有月归档 → **当月 funding 区间内不可得**(latest
  停在 2026-07-31 16:00Z,次月 1 日月归档发布后由增量自动补上)。

## 实现(新增/改动文件)

- **`src/superplatform/data/backfill.py`(新增)**:回填核心。
  - 三个 vision-only 薄源(`_VisionKLineSource`/`_VisionFundingRateSource`/
    `_VisionOpenInterestSource`),provider_id 与运行时标准 provider 一致
    (`binance-perp-kline`/`binance-spot-kline`/`binance-perp-funding-rate`/
    `binance-perp-open-interest`)→ 缓存表/书签与运行时、validate-report
    完全对齐;现货/永续分 provider 分表,结构上不混。
  - `_BackfillVisionClient(BinanceVisionClient)` 子类覆盖(network/ 是
    只读地界,不改源码):① `_EARLIEST_POSSIBLE` 放宽到 2017(基类下界
    2019-09-01 是为永续 metrics 写的,现货 2019-01 起的数据会被钳掉);
    ② `_earliest_archive_date` 高端单日 404 向前回退 ≤14 天、None 不缓存;
    ③ kline 归档解析按数量级嗅探 ms/µs/ns;④ `_download_url` 对 5xx
    重试(实测 ETHUSDT metrics 单日 503 曾整批带走 OI 拉取)。
  - sub-daily kline **按月分块、时间倒序**(先跑最新月,让最早归档二分
    搜索在有数据的近端落定;否则 None 缓存会毒化整列),每块落库一次,
    断点续跑;funding/OI/1d 全区间单次(几千行)。
  - **稠密覆盖判定** `_chunk_coverage`(窗口内行数 + empty_ranges 覆盖
    bar 数 vs 期望 bar 数,容差 8):DataCache 的 min/max 跨度看不到缓存
    内部空洞——首轮回填曾因 µs 解析 bug 留下现货 2025-01→2026-07 大洞,
    min/max 判定把它误标"已覆盖",靠报告的 missing_pct 才暴露。分块路径
    因此直调源 + `DataCache.cache_segment` 写穿,不经过 CachingProvider;
    非分块序列仍走 CachingProvider 增量。
  - 死规矩落实:时间戳全程 UTC-aware;永续请求起点早于 2019-09-25 钳位
    并把前缀记为 empty_range(已验证为空,不报错);现货起点 2019-01-01
    (config `data.backfill` 段可调)。
- **`src/superplatform/data/store/__init__.py`(改动,新增 2 个只读方法)**:
  `count_series_range` / `empty_ranges_between`,供稠密判定。
- **`src/superplatform/data/cache.py`(改动,新增 1 个方法)**:
  `DataCache.cache_segment` — 把 get_or_fetch 内部的"记内部空洞 + upsert"
  写路径公开给分块路径复用。
- **`src/superplatform/data/validation_report.py`(改动,纯增量)**:
  SeriesAudit 增加 `expected_bars/missing_bars/missing_pct`;`audit_series`
  与 `generate_validation_report` 增加 `max_missing_pct`(默认 10,对应
  config `data.validation.max_missing_pct`),缺失占比超阈值 → 显式 warning
  → WARN;报告新增「逐序列覆盖(earliest / latest / missing_pct)」全量表
  (PASS 序列也列出),非通过序列详情含缺失占比行;JSON 留痕同步带新字段。
  既有渲染/判定逻辑未动(test_validation_report 全绿)。
- **`src/superplatform/runtime/cli.py`(改动,只加不碰 02 区域)**:
  新增 `backfill` 子命令(在 validate-report 块后);`validate-report` 增加
  `--max-missing-pct`(默认读 config);文件尾补 `if __name__ == "__main__":
  main()`(此前 `python -m superplatform.runtime.cli` 静默无输出;console
  script 已按 00 注记用 `pip install -e . --no-deps` 刷新为 `superplatform`)。
- **`tools/backfill.py` / `tools/validate_report.py`(新增)**:薄入口,
  等价 `superplatform backfill|validate-report`;backfill 的 docstring 即
  全量回填 README(范围/量级/断点续跑)。
- **`config/default.yaml`(改动)**:新增 `data.backfill` 段
  (perpetual_start/spot_start/kline_frequencies/oi_frequency/chunk_months)。
- **`.gitignore`(改动,1 行锚定)**:`data/` → `/data/`。原规则会忽略
  **任意层级**的 data/ 目录,导致 `src/superplatform/data/` 整个数据层
  (00 搬运的 + 01 新增的)从未被 git 跟踪(`git ls-files src/superplatform/data/`
  为空);锚定后根目录运行时产物 `data/cache.duckdb` 仍忽略,源码目录恢复可见。

## 任务 1 回填命令 — 验收

命令(小集,config 默认边界:永续 2019-09-25、现货 2019-01-01,end=now):
```
.venv/Scripts/python.exe -m superplatform.runtime.cli backfill --symbols BTCUSDT,ETHUSDT --market both --cache data/cache.duckdb
```
run4(终态)输出尾部:
```
  [skip] binance-spot-kline · ETHUSDT · 1m · 2019-02-01~2019-03-01: 已覆盖(缺 0/40320)
  [skip] binance-spot-kline · ETHUSDT · 1m · 2019-01-01~2019-02-01: 已覆盖(缺 0/44640)
完成: 1722144 行 / 12 条序列。
exit=0
```
DuckDB 终态(12 条序列,共 14,987,885 行):
```
== pv_binance_perp_kline
BTCUSDT 1d   2416  2019-12-31 → 2026-08-11
BTCUSDT 1m   3479040  2019-12-31 → 2026-08-11
ETHUSDT 1d   2416  2019-12-31 → 2026-08-11
ETHUSDT 1m   3479040  2019-12-31 → 2026-08-11
== pv_binance_spot_kline
BTCUSDT 1d   2781  2019-01-01 → 2026-08-12
BTCUSDT 1m   4000551  2019-01-01 → 2026-08-12
ETHUSDT 1d   2781  2019-01-01 → 2026-08-12
ETHUSDT 1m   4000550  2019-01-01 → 2026-08-12
== pv_binance_perp_funding_rate
BTCUSDT/ETHUSDT 8h  7212  2020-01-01 → 2026-07-31   (月归档,当月次月补)
== pv_binance_perp_open_interest
BTCUSDT 1d  2171  2020-09-01 → 2026-08-11            (metrics 源端最早)
ETHUSDT 1d  1715  2021-12-01 → 2026-08-11            (ETH metrics 源端最早)
```
- 时间戳 UTC:DuckDB 读回 dtype `datetime64[us, UTC]`;报告「时区(UTC) 12/12」。
- 现货/永续不混:分 provider 分表(`pv_binance_perp_kline` /
  `pv_binance_spot_kline`),小集两市场同名符号各自落各自表。
- 永续早于 2019-09-25 按空处理不报错(冒烟库实测):
  ```
  backfill --symbols BTCUSDT --market perpetual --start 2019-01-01 --end 2020-03-01 --data-type funding_rate
    [note] binance-perp-funding-rate: 请求起点 2019-01-01 早于数据边界 2019-09-25,前缀按已验证为空记录
    [ok] binance-perp-funding-rate · BTCUSDT · 8h · 全区间: 180 rows
  empty_ranges: (pv_binance_perp_funding_rate, BTCUSDT, 8h, 2019-01-01 → 2019-09-25)
  ```
- **永续 earliest = 2019-12-31,未达「≤ 2019-09-26」**:vision 源端
  2019-09~2019-12-30 无任何 UM 永续归档(curl 证据见上),直连 API 又不通,
  机制已按可得数据验证。详见 `BLOCKED_01.md`。

## 任务 2 校验报告 — 验收

命令:
```
.venv/Scripts/python.exe -m superplatform.runtime.cli validate-report --cache data/cache.duckdb --output reports/data_validation_report.md
```
输出:`Verdict: WARN`(WARN 全部来自 MAD 异常值标记与源端空洞书签,
非数据错误;0 FAIL)。逐序列覆盖表(每 symbol earliest/latest/missing_pct):

| Provider | Symbol | 频率 | 行数 | Earliest | Latest | 缺失% | 判定 |
| --- | --- | --- | ---: | --- | --- | ---: | --- |
| binance-perp-funding-rate | BTCUSDT | 8h | 7212 | 2020-01-01 | 2026-07-31 | 0.00 | WARN |
| binance-perp-funding-rate | ETHUSDT | 8h | 7212 | 2020-01-01 | 2026-07-31 | 0.00 | WARN |
| binance-perp-kline | BTCUSDT | 1d | 2416 | 2019-12-31 | 2026-08-11 | 0.00 | PASS |
| binance-perp-kline | BTCUSDT | 1m | 3479040 | 2019-12-31 | 2026-08-11 | 0.00 | WARN |
| binance-perp-kline | ETHUSDT | 1d | 2416 | 2019-12-31 | 2026-08-11 | 0.00 | PASS |
| binance-perp-kline | ETHUSDT | 1m | 3479040 | 2019-12-31 | 2026-08-11 | 0.00 | WARN |
| binance-perp-open-interest | BTCUSDT | 1d | 2171 | 2020-09-01 | 2026-08-11 | 0.00 | PASS |
| binance-perp-open-interest | ETHUSDT | 1d | 1715 | 2021-12-01 | 2026-08-11 | 0.00 | PASS |
| binance-spot-kline | BTCUSDT | 1d | 2781 | 2019-01-01 | 2026-08-12 | 0.00 | WARN |
| binance-spot-kline | BTCUSDT | 1m | 4000551 | 2019-01-01 | 2026-08-12 | 0.10 | WARN |
| binance-spot-kline | ETHUSDT | 1d | 2781 | 2019-01-01 | 2026-08-12 | 0.00 | PASS |
| binance-spot-kline | ETHUSDT | 1m | 4000550 | 2019-01-01 | 2026-08-12 | 0.10 | WARN |

**小集每 symbol missing_pct ≤ 10% 达标**(最大 0.10%,为币安维护停机等
源端空洞,已记 empty_ranges 书签)。报告只读打开缓存,不改数据。
WARN 明细:funding 91 个 MAD 异常值(2020 牛市 0.1%+ 资金费率,真实市场
值);spot 1m 22 个缺失区间(累计 4089 bar = 0.10%,如 2019-03-12 停机)。

## 任务 3 全量回填清单(交付,不本次跑)

- `superplatform backfill --help` 已显示全量用法(验收实测 exit=0,
  epilog 含全量命令/范围/量级/断点续跑;`tools/backfill.py` docstring 同内容)。
- 全量命令:`superplatform backfill --all`(或 `--symbols-file <文件>`)。
  范围:config `data.symbols` 的 40 个 USDT-M 永续 + BTC/ETH 现货;
  时间边界 `data.backfill.perpetual_start=2019-09-25` /
  `spot_start=2019-01-01`(源端实际起点见上方实测);kline 1m+1d +
  funding_rate + open_interest(1d 重采样)。
- 预计量级(按小集实测外推):1m 约 1.5 亿行(40 永续 × ~350 万 +
  2 现货 × ~400 万),月归档下载 ~5GB,首次全量数小时;1d/funding/OI
  合计数十万行(OI 为日归档,40 标的约 8.6 万次请求,是慢头)。
- 断点续跑:重复执行同一条命令即可——稠密覆盖判定跳过已覆盖块
  (`[skip] ... 已覆盖(缺 0/44640)`),只补缺口与最新尾部;单块失败
  不拖垮整批,exit=1 汇总失败序列,重跑补上(run2→run3 实证:
  ETHUSDT OI 单日 503 → 重跑补齐,exit=0)。

## 反向验证(红→绿)

① naive 时间戳 → validators 必须报 UTC 错:
```
== RED: naive(无时区) ==
check_utc.is_utc = False | tz = None
full_validation_report.utc_check.is_utc = False
audit_series.status = FAIL | hard_failures = ['时间戳非 UTC-aware(tz=None)']
superplatform validate --input naive_kline.parquet --schema kline
  → utc_check: {'is_utc': False, 'tz': None, ...}
== GREEN: UTC-aware ==
check_utc.is_utc = True; audit_series.status = PASS
superplatform validate(utc_kline.parquet) → {'is_utc': True, 'tz': 'UTC', ...}
```
② >10% 缺口 → 报告必须标 WARN(100 根 1d 挖掉 50 根 = 50% 缺口):
```
verdict = WARN
| binance-perp-kline | BTCUSDT | 1d | 50 | 2024-01-01 | 2024-04-09 | 50.00 | WARN |
JSON: status WARN, missing_pct 50.0,
warnings: ['1 个缺失区间, 累计 1224.0 小时', '缺失占比 50.00% 超过阈值 10%']
```
GREEN 侧:上方小集报告 missing_pct 全 ≤ 0.10% ≤ 10%。

③ 增量/自愈(分块路径,冒烟库实证):
```
run A(3 个月块):[ok] ×3, 共 131040 行
run B(重跑):  [skip] ×3 已覆盖(缺 0/xxxxx), 共 0 行
挖洞(DELETE 2024-02-10 全天 1440 行)后 run C:
  [skip] 2024-03 块 / [ok] 2024-02 块: 41760 rows / [skip] 2024-01 块
  → 只补被挖的块,其余不重拉
```

## 规矩自查

- pytest:`381 passed, 1 warning in 31.90s`(= 00 基线 381,0 skipped)。
  命令:`.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider`。
- git 地界(未 commit,交总控):
  ```
  改动: .gitignore(锚定 /data/) | config/default.yaml(data.backfill 段)
        | src/superplatform/runtime/cli.py(backfill 子命令 + validate-report
          --max-missing-pct + __main__ 守卫;factors list 区域是 02 的,未碰)
  新增(untracked): src/superplatform/data/(整个数据层,含 00 搬运件——
        此前被 gitignore 误伤从未跟踪) | tools/(backfill.py, validate_report.py)
  ```
  `src/superplatform/strategy/registry.py` 的 M 与 `factors/`、`imports/`、
  `strategies/`、`PROGRESS_02.md`、`BLOCKED_02.md` 等是并行 02 的地界。
- 未写任何 HTTP 路由;未碰 tests/、validators 阈值(max_missing_pct=10
  只被读取未被修改);无 skip/todo/mock/|| true。
- 源目录零改动:全程只读引用 exchangia 副本,未写。

## 遗留/备注

- funding 当月数据次月补(源端只发月归档);perp/spot kline 最新 1~2 天
  随归档发布滞后自动补(日归档 T+1,UM 偶发滞后更久,高端回退已覆盖)。
- `superplatform validate --input *.csv` 在 pandas 3 下读 CSV 时间戳为
  Arrow 字符串会 TypeError(00 搬运的既有问题,与 01 无关;parquet 正常)。
  未修(非本地界/非本任务),记录在案。

---

# 02 阶段详情（PROGRESS_02.md 并入）

# PROGRESS_02.md — 02 双文件热插拔（已完成，2026-08-13）

理解的目标：把 sim_platform 的 MD+impl 双文件因子/策略协议移植进 superplatform，
同时扫内置 `factors/`、`strategies/` 与导入 `imports/factors/`、`imports/strategies/`，
mtime 增量热插拔；impl 包装成 exchangia 的 Factor/Strategy，CLI `factors list` 分页/过滤。

## 交付物

- `src/superplatform/factors/protocol.py`：因子 12 条规则逐条移植；
  impl 约定改为冻结接口 `compute(data: dict[data_type, dict[symbol, DataFrame]], **params)`
  （返回 FactorResult 或 timestamp/value DataFrame/Series）；
  `_resolve_impl_path` 增加「MD 所在插件根」回退（imports/factors/ 下的相对声明可命中）。
- `src/superplatform/strategy/protocol.py`：策略 10 条规则逐条移植；
  impl 约定 `generate(factor_results, **params)` 返回 StrategySignal 或
  timestamp/symbol/position DataFrame。
- `src/superplatform/factors/dual_registry.py`：`DualFactorRegistry`——双目录扫描、
  mtime 快照增量 diff（只重扫变更文件）、孤立 py 不注册、内置优先冲突告警、
  校验通过即包装成 Factor 注册进 `FactorRegistry.get_instance()`（与 decorator 通道并存），
  MD category(12 类)→FactorCategory(5 类)、inputs→required_data 映射。
- `src/superplatform/strategy/dual_registry.py`：`DualStrategyRegistry`，同构，
  注册进 `StrategyRegistry.get_instance()`。
- `src/superplatform/strategy/registry.py`：补 `unregister()`（热拔需要，decorator 通道语义不变）。
- `src/superplatform/runtime/cli.py`（仅 factors list 区域）：`cmd_factors_list` 接双文件
  registry（`ensure_scanned()` = 首扫全量、之后每次 mtime 增量），新增
  `--config`（修既有 AttributeError bug：函数用了 args.config 但 parser 没定义）、
  `--page/--page-size/--filter/--category/--status/--source`；双文件通道分页展示，
  invalid 行展开「规则编号+字段」，conflict 行展示压制关系。
- `factors/TEMPLATE.md`、`strategies/TEMPLATE.md`：按新冻结接口改写的模板（扫描时忽略）。
- 示例：`factors/MOM-001_demo_momentum.md` + `factors/impl/demo_momentum.py`（N 根动量）、
  `strategies/DEM-001_demo_threshold.md` + `strategies/impl/demo_threshold.py`（阈值跟随）。
- `imports/{factors,strategies}/impl/` 目录就位（.gitkeep）。

## 验收记录（全部为实际命令输出）

### 任务 1：协议校验报「规则编号+字段」

- 绿：`protocol.validate("factors/MOM-001_demo_momentum.md")` → `[]`
- 红（反向验证：合规 MD 仅删「数学定义」$$ 公式块）→
  `['规则12 | 字段[数学定义] | 「数学定义」章节缺少 $$...$$ LaTeX 公式块']`，断言仅 1 条且 rule_no==12
- 红（章节 5 标题改 5.1）→ `规则11 | 字段[body] | 正文章节不齐全或顺序错误，缺失/无法按序匹配: ['5. 输出与解释', ...]`
- 策略侧：合规 DEM-001 → `[]`；仅删公式块 → `['规则10 | 字段[逻辑与信号定义] | ...']`（仅 1 条）

### 任务 2：热插拔（进程内增量 diff）

脚本：先 scan_all 建快照 → 写入 REV-001（因子）/DEM-002（策略）双文件 → `check_and_reload()` 计时：

```
基线扫描: 1 因子 / 1 策略
热插检测: 因子 changed=True 策略 changed=True 耗时 0.046s（要求 <10s）
REV-001 compute 尾部: [{'timestamp': Timestamp('2026-01-02 04:00:00'), 'value': -0.04065040650406515}, ...]
DEM-002 positions 尾部: [{'timestamp': ..., 'symbol': 'BTC/USDT', 'position': -1.0}, ...]
删 impl 后: registered_record = None | list 状态 = invalid | 错误 = [{'rule_no': 10, 'field': 'implementation', 'message': '实现文件已删除: ...demo_reversal.py'}]
FactorRegistry.get('REV-001') -> "Factor 'REV-001' not registered"
删 MD 后 REV-001 相关行数: 0
冲突: 在册 MOM-001 source = builtin | conflict 行 = factor_id 'MOM-001' 与内置因子冲突，内置优先 （内置: MOM-001_demo_momentum.md，被压制: MOM-001_copy.md）
ALL HOT-PLUG CHECKS PASSED
```

- `registry.get("REV-001").compute({"kline": {"BTC/USDT": df}})` 出数，末值与手算
  `-(129/124-1)` 误差 <1e-12；FactorResult.values 含 timestamp/value 两列。
- 删 impl 留 MD → 不注册（invalid + 规则 10）；删 MD → 从 list 完全消失。
- imports 放同 ID（MOM-001）MD → WARNING 日志 + 在册仍为 builtin + list 出 conflict 行；
  删除后 conflict 行消失。

### 任务 2：CLI（每次调用 = 新进程，落文件后即见，远小于 10s）

- 落 REV-001 到 imports/factors/ 后 `factors list --filter rev`：

```
== 双文件通道（factors/ + imports/factors/）== 共 1 个，第 1/1 页（每页 50）
Factor ID    Name                     Category         Status      Freq   Src      Version
------------------------------------------------------------------------------------------
REV-001      demo_reversal            momentum         active      1h     imports  1.0.0
```

- 分页：`--page-size 1 --page 2` → `共 2 个，第 2/2 页（每页 1）`，第二页为 REV-001。
- 反向验证（删 MD 后 CLI 再查）→ `共 0 个`（REV-001 消失）。
- 坏 MD（缺公式块）`--status invalid` → 列表出 `- BAD-001_no_formula ... invalid` 行并展开
  `-> 规则12 | 字段[数学定义] | 「数学定义」章节缺少 $$...$$ LaTeX 公式块`。
- 既有 decorator/config 通道输出保留（96 factory + 6 instance），双文件实例不重复出现。

### pytest

```
.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
381 passed, 1 warning in 29.64s        # 基线 381 / 0 skipped，维持
```

### 边界自查

```
git diff --name-only        → src/superplatform/runtime/cli.py（hunk 仅 cmd_factors_list 与 p_list 两处）
                              src/superplatform/strategy/registry.py（仅补 unregister）
git status --porcelain 新增 → factors/、strategies/、src/.../factors/{protocol,dual_registry}.py、
                              src/.../strategy/{protocol,dual_registry}.py
```

未碰 tests/、01 地界（data/、tools/）、03 地界（runtime 其余文件），未写任何 HTTP 路由。

## 拍板与偏差记录

- **imports/ 不入库**：`.gitignore` 第 15 行有 `imports/`（00 定为运行时产物）。imports 属用户
  导入数据，不入库语义合理；注册中心对缺失目录按空库处理，CLI/运行时不受影响。
  .gitignore 不在 02 界限内，未动；若总控想把 imports 示例入库，删该行即可。
- **CLI 入口**：包未 pip 安装，验收用 `PYTHONPATH=src python -c "sys.argv=[...]; main()"` 调
  真实 `main()`；未给 cli.py 加 `__main__` guard（超出 factors list 区域，避免与 01 同文件冲突）。
- **config 未动**：扫描目录取默认值 `factors/`、`strategies/`、`imports/...`（构造器可覆盖），
  不写 config/default.yaml，避免与 01 的配置段冲突。
- **mtime 增量口径**：长驻进程内 `ensure_scanned()/check_and_reload()` 只重扫变更文件
  （实测 0.046s 含 4 文件新增）；CLI 每次新进程首扫为全量（进程无先验快照，属固有性质，
  数千 MD 的全量 = glob + YAML 解析，03 运行时每 tick 走的是增量路径）。
- 例子的 factor_id 用 MOM/DEM/REV 前缀（协议允许 2~8 大写字符），与 sim_platform 编号不冲突。

## 风险/留给 03+

- 03 运行时在每个 tick 调 `DualFactorRegistry.get_instance().ensure_scanned()`（策略侧同理）
  即可获得热插拔；decorator 通道 `FactorRegistry.reload()` 会清表，双文件侧已在每次增量后
  `_sync_into_factor_registry()` 自愈。
- impl 加载会在插件 impl/ 目录写 `__pycache__`（.gitignore 已覆盖，快照只 glob *.md/*.py 不受影响）。

---

# 03 阶段详情（PROGRESS_03.md 并入）

# PROGRESS_03.md — 03 核心运行时（已完成，2026-08-13）

理解的目标：把 exchangia 的评估/回测/实时模拟盘/testnet 接上 02 的双文件因子/策略与
01 的 DuckDB 数据，打通「数据 → 双文件因子 → 策略 → 回测/模拟盘 → 下单」链路。
策略只出仓位权重、消费层转订单；总敞口 100% 封顶；方向反转拆单；前视 FAIL 不交付；
testnet key 只读环境变量、缺 key 报错退出不降级。

## 接线设计（一个改动点一句话）

- 02 的双文件注册中心把校验通过的因子/策略以 ID 为名注册进
  `FactorRegistry` / `StrategyRegistry` 单例——运行时的消费入口不变，变的只有
  「config 条目从哪来」和「策略的信号因子列表从哪来」。
- 双文件因子没有 config 条目 → 新增 `runtime/dual.py::dual_factor_entry` 按 MD 记录
  合成最小评估配置（frequency 取 MD 声明；symbols 默认研究池
  `data.symbols.perpetual` 40 标的——截面 IC 需要 ≥20 标的/期；start/end 回落
  `evaluation.sample_*`；params 已由 02 包装层并入 compute，不重复下发）。
- 双文件策略 `used_factors=[]`（impl 按 MD params 里的因子 ID 自取输入）→
  `dual.py::dual_strategy_factor_ids` 从策略 MD params 识别「取值是已注册因子
  ID/实例名」的参数（DEM-001 的 `params.factor: MOM-001` → `["MOM-001"]`）。
- `dual.py::resolve_strategy_ex`：decorator 通道优先，KeyError 时触发双文件增量
  扫描再重试；双文件策略跳过实例治理校验（因子已经 02 协议校验注册）。
- 扫描时机（PROGRESS_02 留给 03 的集成点）：`dual_factor_entry` /
  `resolve_strategy_ex` 内按需 `ensure_scanned()`；LiveRuntime 注册
  `_hook_hot_reload` 每 tick 调两个注册中心的 `ensure_scanned()`（首扫后每次仅
  mtime diff）。decorator-only 的旧测试路径不触发任何双文件扫描（懒触发），
  注册表单例不被污染（381 基线守住的关键）。

## 改动文件（全部在 03 地界）

- **新增 `src/superplatform/runtime/dual.py`**：上述接线层 +
  `periods_per_year`（1d=365 … 1h=8760，回测年化按 K 线频率）。
- **`src/superplatform/runtime/pipeline.py`**：
  - `OfflineRuntime(..., *, dual_factor_defaults=None)`（新 keyword 参数，默认 None，
    web/测试的旧调用零影响）；
  - 新 `_factor_config_entry(name)`：config 条目优先，缺失时回退双文件合成条目；
    `_evaluate_factor` 与 `run_strategy` 的取数/一致性检查统一走它（原
    `factor_entry` 直调仅这两处+evaluate_grid 未动）；
  - `run_strategy` 改用 `resolve_strategy_ex`；回测年化 `periods_per_year` 按首个
    信号因子的频率（1d→365，与既有行为一致；双文件 1h→8760）。
- **`src/superplatform/runtime/live.py`**：
  - `setup()` 用 `resolve_strategy_ex`（双文件策略可启动）；信号因子列表存
    `self._active_factor_names`；新增 `_hook_hot_reload`（每 tick 增量扫描）；
  - `_hook_factors` / `_hook_strategy` 的因子清单改用 `_active_factor_names`
    （修复：双文件策略 `used_factors=[]` 导致 live 永远不出信号）；
  - `_hook_factors` 的 config 门槛放行已注册双文件因子。
- **`src/superplatform/runtime/cli.py`**：
  - `evaluate`/`check`/`backtest` 加 `--symbols/--start/--end`（仅对双文件通道
    生效，有 config 条目的因子/策略行为不变）；`check` 前视 FAIL 时 exit 1（硬门槛）；
  - `_run_deliver` 硬门槛：前视 FAIL 的因子不交付（打印名单），全 FAIL 不产出；
  - `live` 加 `--ticks N`（跑满 N tick 打印权益/持仓退出，优先于 --duration）、
    `--interval`、`--symbols`、`--broker`（后三个只改内存 config 不回写文件）；
    `build_broker` 的 RuntimeError → `SystemExit` 报错退出（缺 key 不降级模拟盘）；
    结束后打印逐持仓明细。既有 backfill/factors list/validate-report 逻辑未动。

## 验收记录（全部为实际命令输出）

### 任务 1：`evaluate --factor MOM-001`（双文件因子，全默认）

```
.venv/Scripts/python.exe -m superplatform.runtime.cli evaluate --factor MOM-001 --output reports/
# 40 标的（data.symbols.perpetual）× 1h × 2021-01-01→2025-06-30，
# 01 缓存未覆盖的 1h 由 CachingProvider 走 vision 归档补齐（机制同 01，非直连 API），
# 全程约 2.5 分钟，此后落缓存。
============================================================
Factor: MOM-001
  ICIR:        -0.0345
  Mean IC:     -0.0118
  IC > 0:      48.67%
  Layers:      5
  Avg Turnover: 0.3339
  Forward Bias: PASS
  Cost scenarios: 1
  Report: reports//MOM-001_report.html
```

报告截面齐全（grep reports/MOM-001_report.html）：`IC Over Time` / `IC Decay` /
`Layer Test (Cumulative)` / `Turnover` / `Rolling IC` / `ICIR: -0.0345` 均在，
标题含 Forward Bias PASS。RankIC 在 `PipelineResult.rank_ic_df`（40 标截面）；
ICIR/IC/分层/换手见上。

### 任务 1：`check --factor MOM-001` 通过

```
.venv/Scripts/python.exe -m superplatform.runtime.cli check --factor MOM-001
  → exit=0；日志尾行: MOM-001: Forward Bias — PASS
```

### 任务 1 反向验证：含未来数据的因子必须 FAIL（红→绿）

临时因子 `imports/factors/LAH-001_lookahead_zscore.md` + `impl/lookahead_zscore.py`
（z-score 的均值/标准差用全样本计算 = 前视），验完已删：

```
check --factor LAH-001 --start 2024-01-01 --end 2025-06-30
  → LAH-001: Forward Bias — FAIL
    This factor has forward-looking bias and MUST be fixed!
  → exit=1（硬门槛）
evaluate --factor LAH-001 --start 2024-01-01 --end 2025-06-30 --deliver
  → LAH-001: ICIR=-0.0343, bias=FAIL
    Forward Bias: FAIL
    Forward Bias FAIL — 不交付: LAH-001
    No deliverable factor passed the forward-bias gate.   （exit=0 但零交付）
```

清理后（新进程，mtime 增量扫描）：`LAH-001 after cleanup: None` /
`MOM-001 still registered: True`。GREEN 侧即上方 MOM-001 的 PASS。

### 任务 2：`backtest --strategy DEM-001`（双文件策略）

```
.venv/Scripts/python.exe -m superplatform.runtime.cli backtest --strategy DEM-001
  → exit=0（缓存已暖，秒级）
Strategy: DEM-001
  Sharpe:       -0.87
  Total Return: -98.39%
  Annual Return:-60.11%
  Annual Vol:   69.15%
  Max Drawdown: -99.23%
  Win Rate:     46.87%
  Avg Return:   -0.0078%
```

（demo 阈值策略在 1h 动量上稳定亏钱属预期——指标为真实计算，年化按 1h=8760。）

### 任务 2：`live --ticks 5`（SimulatedBroker 模拟盘）

```
.venv/Scripts/python.exe -m superplatform.runtime.cli live --strategy DEM-001 --ticks 5 --interval 1 --symbols BTCUSDT,ETHUSDT
  → exit=0；无 Traceback（grep 验证）
Live trading started: strategy=DEM-001, broker=simulated-synthetic
Running 5 ticks (interval=1.0s).
[tick 5] BTCUSDT=59587.73, ETHUSDT=57899.60 | 0.03s
Order placed: BTCUSDT buy 0.8391 filled
Order rejected: ETHUSDT buy — Insufficient balance: need 50025.00, wallet 49990.00
Final: equity=99990.00 wallet=49990.00
Positions:
  BTCUSDT:spot: qty=0.839099 entry=59587.73 mark=59587.73 upnl=0.00
Orders: 5, Trades: 5
```

5 tick 后自动退出；权益非零（100000 → 99990，taker fee）；持仓逐行打印。
同时可见消费层死规矩在工作：2 标的各 50%（总敞口 100% 封顶 → 等权 1/N），
首单锁定保证金后次单余额不足被拒（拒单路径的真实演示）。

### 任务 2 反向验证：超资金订单必须被拒

```
SimulatedBroker(initial_capital=100_000), price=50000:
  buy 100 BTC（名义 5,000,000 > max_order_notional 100,000）
  → order=None | "Order notional 5000000 > max 100000"     （拒）
对照 buy 0.1 BTC → status=filled                              （放）
REVERSE-CHECK OK: oversized order rejected
```

方向反转拆单（死规矩）：

```
持 0.5 BTC 多头 + 目标 position=-1.0 → generate_orders:
  close  qty=0.5000      （先平多）
  short  qty=2.0000      （再开空）
SPLIT-ORDER OK: long->short 拆成 close + short
```

### 任务 3：testnet

```
无 key:
  live --strategy DEM-001 --ticks 1 --broker binance-testnet
  → exit=1
  → "live 启动失败: binance-testnet requires API keys in environment variables:
     BINANCE_TESTNET_API_KEY, BINANCE_TESTNET_API_SECRET"
  （报错退出，非静默降级为模拟盘；broker 构建先于 DuckDB/数据层初始化）
有 key（dummy）连通性:
  curl https://testnet.binancefuture.com/fapi/v1/ping → 200 (0.58s)
  BINANCE_TESTNET_API_KEY=dummy ... build_broker() 成功（broker=binance-testnet，
  key 仅从环境变量读）→ broker._fetch_account() 收到 Binance 服务端
  ClientError (401, -2014, 'API-key format invalid.')
  —— 即网络通、签名请求路由正确，仅凭据无效。真 key 全流程未验，见 BLOCKED_03.md。
```

## pytest 与地界自查

```
.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
381 passed, 1 warning in 33.68s        # = 基线 381 / 0 skipped

git status --short
 M src/superplatform/runtime/cli.py
 M src/superplatform/runtime/live.py
 M src/superplatform/runtime/pipeline.py
?? src/superplatform/runtime/dual.py
```

全在 03 地界（runtime/**）。未碰 tests/、config 阈值、01 地界（data/、tools/）、
02 地界（factors/、strategies/、imports/ 仅临时放 LAH-001 反向验证，验完即删）、
未写任何 HTTP 路由；无 skip/todo/mock/|| true。未 commit（交总控）。

## 拍板与已知限制

- **evaluate 默认全研究池 + MD 频率**：双文件因子评估默认 symbols 取
  `data.symbols.perpetual`（40 标的，满足 min_stocks=20 的截面 IC 门槛），
  频率取 MD 声明（MOM-001=1h）。首次评估的 1h 数据由 CachingProvider 走 vision
  归档补齐并入缓存（与 01 回填同机制、同表同 provider_id，不是直连 API）。
- **回测年化按频率**：`run_strategy` 现在按首个信号因子的频率给
  `periods_per_year`（1d=365 与旧行为一致；1h=8760）。config 里全部既有因子
  都是 1d，旧测试数值不变。
- **live 单标的因子的共享值（搬运件既有行为，未改）**：`_hook_factors` 对整个
  symbol buffer 只调一次 `factor.compute` 并把同一 FactorResult 挂到每个 symbol
  下；MOM-001 这类「取 values()[0]」的单标的 impl 在 live 里会让所有标的拿到
  第一个标的的因子值（demo 可接受）。离线评估/回测无此问题（按组逐标的计算）。
  05/后续若要 live per-symbol 精度，改 `_hook_factors` 的分组调用即可。
- **live 持仓方向**：`generate_orders` 以 buy/close/short 表达（spot+perp 混合
  语义是 exchangia 消费层原样），DEM-001 的多单落成 spot 持仓属既有语义。
- reports/ 产物（MOM-001_report.html 等）为运行时输出，.gitignore 覆盖不入库。

## 留给 04/05 的集成说明（可调用入口签名）

```python
# 离线评估（双文件因子直接用 factor_id；decorator/config 因子照旧）
from superplatform.runtime.pipeline import OfflineRuntime, PipelineResult
runtime = OfflineRuntime(config, provider_registry,
                         progress=None,                  # 可选进度回调 fn(dict)
                         dual_factor_defaults=None)      # {"symbols": [...], "start": ..., "end": ...}
results: list[PipelineResult] = await runtime.run(
    ["MOM-001"], output_dir="reports", skip_report=False, lightweight=False)
# PipelineResult: factor_name/per_symbol/cross_section/ic_df/ic_stats/rank_ic_df/
#   rank_ic_stats/ic_decay_df/layer_results/turnover_df/rolling_df/
#   forward_bias_passed/forward_bias_reports/cost_summary/validation_reports

# 策略回测（双文件策略直接用 strategy_id）
result = await runtime.run_strategy("DEM-001", output_dir="reports",
                                    consumer=ConsumerConfig.backtest())
# result: {"signal": StrategySignal, "backtest": BacktestResult,
#          "factor_results": dict[factor, dict[group, FactorResult]]}
# BacktestResult: equity/trades/total_return/annual_return/annual_vol/sharpe/
#   max_drawdown/win_rate/avg_return/liquidated_at

# 实时模拟盘 / testnet
from superplatform.runtime.live import LiveRuntime
from superplatform.network.brokers import build_broker     # live.broker 决定实现；缺 key 抛 RuntimeError
broker = build_broker(config, adapter=None, symbols=None)
live = LiveRuntime(config, provider_registry, broker,
                   consumer=ConsumerConfig.backtest(),    # 或 Strictness 变体
                   limits=None, symbols=None)             # symbols=会话级标的覆盖
live.setup(strategy_name="DEM-001")                       # 双文件策略直接可用
await live.start()                                        # 阻塞直到 stop()
await live.stop()
live.state                                                # AccountState 本地镜像（equity()/positions）
live.scheduler.snapshot()                                 # tick_no/prices/stale 等
live.scheduler.register_hook(hook)                        # 自定义每 tick 钩子（如 N tick 自停）

# 双文件接线工具（04 评级若消费双文件因子可复用）
from superplatform.runtime.dual import (
    scan_dual_registries,          # 两注册中心各一次增量扫描（热插拔入口）
    dual_factor_entry,             # (factor_id, config, overrides) -> 合成 config 条目
    dual_strategy_factor_ids,      # strategy_id -> [信号因子 ID]
    resolve_strategy_ex,           # name -> (strategy, used_factors, is_dual)
    periods_per_year,              # 频率 -> 年化 bar 数
)
```

注意：DuckDB 缓存单进程写锁——同一时刻只能一个进程打开 `data/cache.duckdb`
（评估/回测/live 并发跑会撞 `IO Error: Cannot open file`），05 的 API 映射
若起后台任务需串行化或复用同一进程内的 store。
