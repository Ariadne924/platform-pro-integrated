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
