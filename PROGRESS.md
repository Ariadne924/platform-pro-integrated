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

---

# 04 阶段详情（PROGRESS_04.md 并入）

# PROGRESS_04.md — 04 评级与偏差控制（已完成，2026-08-13）

理解的目标：移植 sim_platform 的 `app/factors/rating.py`（S~D 评级）、
`app/bias_checkers.py`（六查）、`app/factor_metrics.py`（开发集指标 + 合格判定 +
相关性矩阵），接 exchangia 内核的数据（01 的 DuckDB 缓存，经 DataProvider.fetch）
与 02 的双文件因子，产出 `rating` / `metrics` / `bias-check` 三个 CLI 子命令，
指标在 04 新模块内唯一权威实现，CLI 只调用不自算。

## 移植口径与拍板（与源项目的差异，逐条记录）

- **数据源**：源项目检查器从 `factor_value` 在线缓存表读因子历史值；本平台无落库
  因子值历史（03 离线评估在内存计算），所有检查/指标/评级统一走源项目「落库为空
  时的重算回退」口径——按注册实现 + K 线重算（含 lookback warmup），payload 里
  recomputed/source 如实标注。取数从 `store.range_klines` 换成
  `KlineFetcher`（同步包装异步 `provider.fetch`，进程内单事件循环，逐 symbol
  失败返回空帧不拖垮整批，与 03 pipeline `_safe_fetch` 同语义）。
- **频率泛化**：源项目只有 1m/1d 两档；本平台按因子 MD 声明频率取 bar 宽
  （1m~1w 九档，MOM-001=1h）。源配置 `*_1m_*` 键在这里叫 `*_intraday_*`
  （语义=非 1d 因子按自身频率的 bar 粒度），horizon/滚动窗口单位均为因子频率 bar。
  信号回测年化按频率换算（1h→8760，经 `_BARS_PER_YEAR`，与 03 `periods_per_year` 一致）。
- **insufficient 不出级**：源项目在「全部标的样本不足」时聚合层放占位 grade="D"；
  按本阶段死规矩（样本不足返回 insufficient，不给假数字）改为
  `status="insufficient"`、`grade=None`。横截面/funding/OI/mark_price 依赖因子
  `status="not_supported"`（多标的 config 因子 required_symbols>1 同此）。
- **横截面帧未移植**：双文件协议无横截面声明；多标的 config 因子在记录加载时标
  `cross_sectional=True`，逐标的历史重算对它不成立，各检查如实 BLOCKED/
  not_supported，不给假数字。
- **相关性矩阵封顶 + 串行**：因子数封顶 `metrics_corr_max_factors=200`（超出按
  factor_id 排序截断并 `truncated=true` 标注，满足「分块或封顶」规模约束）；源项目的
  spawn 子进程池未移植（本平台因子库当前规模串行足够；批量提速靠跨因子共享 K 线
  帧缓存 + DuckDB 结果缓存的增量重算）。
- **缓存按 (factor_id, 数据版本) 键控**：新表 `eval_metrics_cache` /
  `eval_rating_cache` / `eval_corr_cache` / `eval_bias_runs` / `eval_bias_results` /
  `eval_oos_lock`，与 01 数据缓存同库（data/cache.duckdb，同进程多连接为 DuckDB
  支持用法，已实测；跨进程仍单写者）。数据版本键 = 因子消费的 provider 缓存表逐
  symbol 首末日期+行数的 sha256（metrics 末日期按开发集右端封顶，rating 不封顶）。
  **写键在计算之后重取**（计算期间的增量拉取会改变数据版本，以写时状态为准，
  后续读取才能命中——首版写成预取键导致评级榜缓存集体失效，已修并复验）。
- **偏差语义按拍的板**：lookahead/full_sample/overfit 的 PASS 是真判定；
  multiple_testing/cost/out_of_sample 的 PASS 仅表示「可计算」，payload 带 note，
  显著性以 `significant_after_correction` 为准。六查 = 开发集五查 + 样本外
  （scope=development 时样本外显示 LOCKED；scope=locked_oos 加跑样本外，
  每因子只允许成功一次，eval_oos_lock 持久化，重复执行拒跑标 LOCKED）。
- **多重检验家族**：CLI 一批次的全部因子即 BH 家族（family_size 如实进 payload；
  单因子批次 family_size=1，adjusted=p）。
- **overfit 证据**：config `bias_control.parameter_search` 只录了 MOM-001
  （frozen=true, retuned_on_oos=false——02 创建以来 window=20 未变，git 可查）；
  无证据因子如实 BLOCKED，不伪造 PASS。
- **rating 方向语义**：解析双文件 MD `output.direction` 文本（含「看空」→bearish），
  缺省 bullish_high=True；评级一律用 |RankIC|/|ICIR|/|Sharpe|，方向只影响回测持仓符号。

## 改动/新增文件

- 新增 `src/superplatform/evaluation/bias.py`：状态常量、`_json_safe`/`_scanable_source`
  等移植件、`EvalFactorRecord`（双文件+config 双通道适配）、`KlineFetcher`、
  `EvalCacheStore`（DuckDB 缓存）、`BiasCheckRunner`（六查移植）、
  `BiasControlService`（同步批次 + md/json/csv 报告导出 + OOS 一次性锁）。
- 新增 `src/superplatform/evaluation/factor_metrics.py`：`FactorMetricsCalculator`
  （IC/RankIC/ICIR/衰减/分层/滚动/换手/相关性矩阵，唯一权威实现）+
  `FactorMetricsService`（缓存、合格判定六项、qualification 汇总、CSV 导出）。
- 新增 `src/superplatform/evaluation/rating.py`：评级纯函数逐行移植
  （grade_of/_ic_metrics/_signal_backtest/compute_factor_metrics）+
  `RatingService`（单因子评级/评级榜/缓存）。
- 改动 `src/superplatform/runtime/cli.py`（纯增量）：`rating` / `metrics` /
  `bias-check` 三个子命令；`_print_json` 用 ensure_ascii=True（GBK 控制台重定向
  仍是合法 JSON）。既有子命令逻辑未动。
- 改动 `config/default.yaml`：末尾新增 `bias_control:` 段（窗口/阈值/合格判定/
  parameter_search，不改任何已有配置）。

## 验收记录（全部为实际命令输出）

### 任务 1：`rating --factor MOM-001 --json`（全研究池 40 标的，近 30 天 1h）

```
.venv/Scripts/python.exe -m superplatform.runtime.cli rating --factor MOM-001 --json
status = ok | grade = S
  rank_ic_mean: -0.09966481904131114      ic_mean: -0.10211436441986625
  icir: -1.0278369884142948               sharpe: -3.3224798390148513
  total_return_pct: -9.8765               max_drawdown_pct: -18.5329
  win_rate: 0.4379                        n_samples: 25680
  n_symbols: 40 / n_symbols_ok: 36        coverage: 0.9989
  last_ts: 2026-08-13T06:00:00+00:00
insufficient symbols: [PEPEUSDT, SHIBUSDT, FLOKIUSDT, BONKUSDT]   # 源端 Invalid symbol，如实剔除
```
（|RankIC|=0.0997≥0.05、|ICIR|=1.03≥0.5、|Sharpe|=3.32≥1.5 → S；动量在近 30 天
窗口呈反转（IC 为负），评级用绝对值，notes 里如实说明口径。BTC/ETH 双子集冒烟：
grade=A，per_symbol S/B。）

### 任务 1：`rating --leaderboard`（无 ids 只读缓存）

```
rating --factor MOM-001 --refresh   → exit=0（按数据版本键落 eval_rating_cache）
rating --leaderboard
Factor ID      Grade  Status            RankIC     ICIR   Sharpe   Samples
MOM-001        S      ok               -0.0997   -1.028    -3.32     25680
LAH-002        -      not_evaluated ...（反向验证临时因子，验完已删）
...（96 decorator factory + config 实例均 not_evaluated，绝不触发重算）
共 99 个：rated=1 insufficient=0 not_supported=0 not_evaluated=98（本次计算 0）
```

### 任务 2：`metrics --factor MOM-001 --json`（开发集 2021→2024，36 标的，987,579 样本）

```
status=PARTIAL（36/40 标的可算，4 个源端无效标的如实进 warnings）
window: 2021-01-01T00:00:00Z → 2024-12-31T23:59:59Z   horizons: [1,6,24,72,168]（1h bar）
ic: -0.00591   rank_ic: -0.02932   icir: -1.12937   turnover: 0.06980
p_value: 7.95e-187   decay_ratio: 0.9636
ic_decay: h=1 rank_ic=-0.0293 / h=6 -0.0283 / h=24 -0.0429 / h=72 -0.0030 / h=168 -0.0006
quantile buckets mean_return: [0.000283, -0.000166, 0.000042, 0.000133, 0.000275]
rolling: mean=-0.0391 std=0.0346 positive_ratio=0.1253 count=5737
qualification: qualified=false, reasons=[分层不单调, 多空差(-0.0000)过小]
缓存：重跑 cache_hit=True，payload 逐字段一致（15s→秒级的差距主要在装配）
导出：metrics --factor MOM-001 --output reports/MOM-001_metrics.csv（带 BOM 长表）
qualification 汇总：metrics --qualification-summary --output reports/qualification_summary.csv
  → total=99 evaluated=1 qualified=0 unqualified=1 not_evaluated=98
相关性矩阵：metrics --correlation-matrix --ids MOM-001,LAH-002 --output reports/corr_probe.csv
  → 2×2 Spearman，off-diagonal 0.0427，对角 1.0（LAH-002 清理后该矩阵缓存行已删）
```

### 任务 2：`bias-check --factor MOM-001 --scope development`（六查批次 + 报告导出）

```
.venv/Scripts/python.exe -m superplatform.runtime.cli bias-check --factor MOM-001 --scope development --output reports/
Bias check run: 24db8f9650c0  scope=development
Factor ID      Overall    Look    Full    MT      Overfit  Cost    OOS
MOM-001        PASS       PASS    PASS    PASS    PASS     PASS    LOCKED
Summary: total=1 PASS=1 FAIL=0 BLOCKED=0 ERROR=0 LOCKED=0
Report: reports\bias_control_24db8f9650c0.md / .json     （约 12s，exit=0）

明细（真判定证据）：
  lookahead: PASS  compared=19580  max_abs_diff=0.0  tol=3.03e-08
    2022-07-01 PASS compared=5500 | 2023-07-01 PASS compared=6380 | 2024-07-01 PASS compared=7700
  full_sample: PASS  violations=[]（tokenize 剔除注释/字符串后扫描）
  multiple_testing: PASS  p=2.21e-46 adj_p=2.21e-46 significant=True samples=986715 family_size=1
  overfit: PASS（config parameter_search 声明 frozen=true 且未在样本外重调）
  cost: PASS gross=+0.4015 net(最高档)=-0.9839 turnover=0.1069 samples=987543
    逐档 net：0bps -0.9371 / 2bps -0.9569 / 5bps -0.9719 / 10bps -0.9839（1h 高换手信号扣费即死，真实）
  oos: LOCKED can_run_once=true（development scope 不跑样本外）
```

### 反向验证（红→绿）

① 空数据 rating 必须 insufficient 而非 S 级（RED）：
```
rating --factor MOM-001 --symbols NOSUCHUSDT --json
  → exit=2
  status = insufficient | grade = None | aggregate.insufficient = True
  per_symbol: [(NOSUCHUSDT, insufficient=True, reason='K线数据为空')]
```
GREEN 侧：上方 MOM-001 全池 grade=S（ok）。

② 含前视的因子六查前视项必须 FAIL（RED）：
临时因子 LAH-002（imports/factors/，z-score 均值/标准差用整段样本计算=前视）：
```
bias-check --factor LAH-002 --scope development
  → exit=1（硬门槛）
  LAH-002  Overall=FAIL  Look=FAIL  Full=FAIL  MT=PASS  Overfit=BLOCKED  Cost=PASS  OOS=LOCKED
  lookahead 明细：compared=21360  max_abs_diff=3.5259  tolerance=7.42e-07
    2022-07-01 FAIL max_diff=1.2619 | 2023-07-01 FAIL max_diff=3.1969 | 2024-07-01 FAIL max_diff=3.5259
  full_sample: FAIL（historical_values_changed=true；overfit BLOCKED=无冻结证据，如实）
```
GREEN 侧：MOM-001 lookahead PASS max_diff=0.0（见上）。

③ 样本外一次性锁定（locked_oos 语义验证，LAH-002 限 BTCUSDT）：
```
首跑：bias-check --factor LAH-002 --symbols BTCUSDT --scope locked_oos
  → OOS=PASS（complete=true, samples=4526, gross=-0.4522, net=-0.6864；PASS=可计算/跑完）
二跑：同命令 → Overall=LOCKED，OOS=LOCKED
  「锁定样本外已运行过一次，一次性锁定不允许重复执行」
```

清理（imports/ + eval 库行）：
```
rm imports/factors/LAH-002_lookahead_zscore.md imports/factors/impl/lookahead_zscore.py
DELETE: eval_bias_results 3 行 / eval_oos_lock 1 行 / 相关 runs 3 行 / eval_corr_cache 清空
新进程注册中心：LAH-002 after cleanup: None | MOM-001 still registered: True
留存：eval_bias_runs/results 仅 MOM-001 的 development 批次 24db8f9650c0
```

### not_supported 分支（funding/OI/横截面依赖，不给假数字）

```
构造 EvalFactorRecord(data_types=["funding_rate"]) → status=not_supported, grade=None
  note: 依赖输入 funding_rate 无本地时序数据，暂不支持评级
构造 cross_sectional=True → status=not_supported（横截面/多标的因子暂不支持单标的时序评级）
```

## pytest 与地界自查

```
.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
381 passed, 1 warning in 35.36s        # = 基线 381 / 0 skipped

git status --short（仅 04 地界；其余 M/?? 为并行 05 的地界，未碰）：
 M config/default.yaml                 # 末尾新增 bias_control 段
 M src/superplatform/runtime/cli.py    # 仅新增 rating/metrics/bias-check 子命令
?? src/superplatform/evaluation/bias.py
?? src/superplatform/evaluation/factor_metrics.py
?? src/superplatform/evaluation/rating.py
（05 地界：pyproject.toml / requirements.txt / run.py / src/superplatform_web/** / web/）
```

未碰 tests/、01（data/、tools/）、02（factors/、strategies/、协议与注册中心）、
03（runtime 其余文件）地界；未写任何 HTTP 路由；无 skip/todo/mock/|| true；
未 commit（交总控）。imports/ 临时 LAH-002 已删（02 对 imports 的约定是运行时产物，
不入库）。reports/ 产物（bias_control_*.md/json、MOM-001_metrics.csv、
qualification_summary.csv）为运行时输出，.gitignore 覆盖。

## 留给 05 的服务层接口（Python 调用，全部返回 JSON 可序列化 dict）

```python
from superplatform.runtime.config import Config
from superplatform.runtime.cli import _setup_providers        # (providers, store)；store 用毕 close()
from superplatform.evaluation.rating import RatingService
from superplatform.evaluation.factor_metrics import FactorMetricsService
from superplatform.evaluation.bias import BiasControlService

config = Config.load("config/default.yaml", "config/exchanges.yaml", "config/factors.yaml")
providers, store = _setup_providers(config)
cache = config.get("data.cache.path", "data/cache.duckdb")

rating = RatingService(config, providers, cache_path=cache, store=store, symbols=None)
metrics = FactorMetricsService(config, providers, cache_path=cache, store=store, symbols=None)
bias = BiasControlService(config, providers, cache_path=cache, store=store, symbols=None)
# symbols=None → 默认 config data.symbols.perpetual 研究池；传 list[str] 覆盖。
# 三个服务非线程安全（DuckDB 单写者）：API 映射需串行化（如单 worker 线程 +
# asyncio.to_thread），不要与 live/回填进程并发开 data/cache.duckdb。

# ① 评级（单因子）：因子未注册返回 None；
#    payload["status"]: ok（aggregate.grade ∈ S~D）/ insufficient / not_supported
rating.rate_factor("MOM-001", days=None, horizon=None, symbols=None, refresh=False) -> dict | None
# ② 评级榜：ids=None 只读缓存（not_evaluated 不重算）；ids 子集 ≤compute_limit 同步计算
rating.leaderboard(ids=None, days=None, horizon=None, refresh=False, compute_limit=20) -> dict
# ③ 开发集指标（IC/RankIC/ICIR/衰减/分层/换手/滚动 + p_value/decay_ratio/qualification 块）
metrics.factor_metrics("MOM-001", force=False) -> dict | None
metrics.factor_metrics_many(["MOM-001", ...], on_progress=None) -> dict[str, dict | None]
# ④ 合格判定汇总（只读缓存；refresh=True 限量补算 ≤refresh_limit 个）
metrics.qualification_summary(refresh=False, refresh_limit=20) -> dict
metrics.qualification_state("MOM-001") -> bool | None          # True/False/未评估
# ⑤ 相关性矩阵（日频网格 Spearman；ids=None 全库，封顶 metrics_corr_max_factors）
metrics.correlation_matrix(factor_ids=None) -> dict
# ⑥ 六查批次：scope=development|locked_oos；同步执行、逐因子落库，返回 run 摘要
bias.run("development", ["MOM-001"], run_id=None, on_progress=None) -> dict
bias.report_data(run_id) -> dict | None                        # 批次全量明细
bias.report(run_id, "md"|"json"|"csv") -> (content, filename)  # 报告导出（csv 带 BOM）
bias.eval_store.latest_run_id() -> str | None                  # 最近批次
# CSV 导出辅助：FactorMetricsService.metrics_csv / qualification_csv / correlation_csv
```

已实测（本阶段验收进程内调用）：rate_factor / leaderboard / factor_metrics /
qualification_summary / report_data 全部返回可 json.dumps 的 dict，缓存命中
cache_hit=True。

## 已知限制（记录在案，不阻塞）

- PEPEUSDT/SHIBUSDT/FLOKIUSDT/BONKUSDT 四个研究池标的在数据源端 Invalid symbol，
  逐 symbol 剔除并如实进 warnings（metrics status=PARTIAL 即因此）。
- config 通道（decorator）因子无 impl 路径：full_sample 静态扫描对其如实 BLOCKED；
  评级/指标可用（同一冻结 compute 接口），leaderboard 默认只读缓存不自动计算。
- 评级是近 N 天窗口快照，与开发集 metrics 口径不同（MOM-001 近 30 天 S 级、
  开发集 RankIC -0.029 且不合格）——两者用途不同，payload notes 均如实标注。
- 评估类进程与 live 不可并发（DuckDB 单写者），05 起后台任务需串行化。

---

# 05 阶段详情（PROGRESS_05.md 并入）

# PROGRESS_05.md — 05 UI 四页（已完成，2026-08-13）

理解的目标：把 sim_platform 的四个原生 JS+ECharts 页面接进 superplatform，
后端按 sim 的 API 形状提供数据：index 看行情/净值/持仓（01 缓存 + 03 live），
explorer 看因子/评级（02 注册中心 + 04 rating），bias-control 跑六查
（04 BiasControlService）。所有 HTTP 路由 + 静态托管 + run.py 增强归本阶段；
路由只调用服务，不重算指标。

## 关键拍板（与任务书/源项目的差异，逐条记录）

- **路由遮蔽修复（run.py 的核心增强）**：app.py 导入时即挂 `Mount("/", static)`
  （frontend/dist），`app.include_router` 只能追加路由——后注册的 API 路由会被
  静态挂载全部遮蔽成 404（探针 `/api/simprobe` 实测：include 成功但 404）。
  run.py 新增 `mounts_to_tail()`：所有 Mount 挪到路由表末尾（相对顺序保持
  web/ → frontend/dist），API 路由先匹配。只改 run.py，app.py 不动，tests 零影响。
  探针红→绿：挪前 `probe: 404 {"detail":"Not Found"}` → 挪后 `probe: 200 {"probe":"ok"}`。
- **三处 API 路径避让**（web/API_DIFF.md A 类）：sim 源页的 `GET /api/factors`、
  `GET /api/strategies`、`DELETE /api/factors/{id}` 与 exchangia 既有冻结路由
  撞名且 shape 不同（tests/test_web_introspect.py:245 等冻结了旧形状），前端
  改调 `/api/registry/factors`、`/api/registry/strategies`、
  `DELETE /api/registry/factors/{id}`，响应保持 sim 形状。这是四页 HTML 与源
  仅有的内容差异（另有 4 处 display:none 隐藏策略工厂入口，任务书明确指令）。
- **04 接线用服务层不用 CLI 子进程**：04 交付了 Python 服务接口
  （PROGRESS_04.md 末尾），且 DuckDB 单进程写锁使子进程方案不可行
  （web 进程持有 data/cache.duckdb）。`simserve.services04()` 惰性构造
  RatingService/FactorMetricsService/BiasControlService（复用 _state 的
  config/providers/store），全部调用经 `_S04_LOCK` 串行化（04 服务非线程安全）。
- **sim 有而本平台没有的服务：如实 503/501**（不返回假空数据）：signals
  （无独立信号引擎）、cleaning（未移植）、backtest/factor（无 sim 的因子买卖
  回测引擎）、pystrategies（无双文件制外通道）。前端对这些有设计的错误展示。
- **检查状态登记/人工解封是本层簿记**：attest/uncheck/override 状态落
  `data/web_factor_check.json`（运行时产物，gitignored）；六查结果经
  `data/web_bias_results.json` 留档 + 04 的 eval_bias_* 表（UI 批次与 04
  共享 run_id，报告由 04 report 生成，重启后可导出）。
- **run.py 默认自动启动模拟盘 live 会话**（DEM-001 + BTCUSDT,ETHUSDT，
  config live.broker=simulated，**adapter=None 合成价格源**——本机 fapi 生产域
  直连超时（BLOCKED_01/03），传真实 adapter 会让 _hook_data 每 tick 卡 30s
  超时且 STALE 不下单；合成源即 03 CLI live 的默认行为，撮合/权益/持仓全是
  真实计算）。经 lifespan 包装实现，只在 run.py 进程生效，tests 的 TestClient
  不受影响；会话失败只记日志不拖垮服务。`--no-autolive` 关闭。
- **策略回测映射 03 run_strategy**：amount 仅用于把归一化净值换算 USDT 展示；
  本平台是权重调仓模型，无逐笔成交价 → positions/transactions 如实空数组
  （前端据此隐藏明细表），汇总数字全部真实。buy_time/sell_time 经
  dual_factor_defaults 传给运行时。
- **新依赖 2 个**（00 地界文件，已加最小一行并在此备案）：python-multipart
  （sim 上传接口的 multipart 表单必需，缺了 FastAPI 路由注册即炸）、openpyxl
  （/api/factors/export 的 xlsx 导出）。已入 requirements.txt + pyproject.toml。
- **符号映射**：UI 用 `BTC/USDT`，数据层用 `BTCUSDT`，路由层双向换算
  （字段映射类适配）。K 线 period 聚合用 pandas resample（图表展示层聚合），
  均线叠加复制 sim app/indicators.py（展示逻辑，非评估指标）。
- **相关性矩阵默认因子集收窄**：sim 口径是「落库因子值的全部因子」；本平台
  无落库因子值，对应物取 eval_metrics_cache 已评估因子（实测全库 99 因子重算
  超过 10 分钟被超时杀掉，收窄后秒回）。未评估因子进 excluded 如实标注，
  payload 带 note 说明口径。

## 验收记录（全部为实际命令输出）

### 任务 1：四页 200（真实 run.py 服务）

```
.venv/Scripts/python.exe -u run.py --port 8000
auto-included routes: ['sim_admin','sim_bias','sim_market','sim_misc',
                       'sim_rating','sim_registry','sim_state','sim_trading']
GET /                -> 200
GET /explorer.html   -> 200
GET /bias-control.html -> 200
GET /about.html      -> 200
GET /explorer        -> 307（重定向 /explorer.html，导航栏用它）
```

AI 策略工厂入口已隐藏（index/explorer/about 共 4 处按钮 display:none，
见 API_DIFF.md C 类；页面 DOM 实证见下方 Chrome 取证）。

### 任务 2：API 接线（TestClient + 真实服务双层验证）

**index 页 — K线（01 缓存）**：
```
GET /api/market/klines?symbol=BTC/USDT&limit=5
  -> 200 count=5 last={'ts':'2026-08-11T23:59:00+00:00','open':63560.0,...}（缓存尾部）
GET ...&start=2026-08-01&end=2026-08-11&limit=5000&ma=all
  -> 200 period=5m（自动降采样）count=2880
     ma keys=['MA5','MA10','MA20','MA30','MA60','MA83','MA_W','MA_M']
GET /api/market/tickers -> 200 {'symbols':{'BTC/USDT':{'last_price':63572.0,
  'change_24h_pct':...,'funding_rate':...,'open_interest':...},'ETH/USDT':{...}}}
```

**index 页 — 净值/持仓/账户（03 live，autolive 合成源模拟盘）**：
```
run.py 启动 ~50s 后：
/api/state:  ticks=5 running=True stale=False
             account={'equity':99990.0,'wallet_balance':49990.0,'margin_used':50000.0,...}
             positions_n=1  tickers=['BTC/USDT','ETH/USDT']
/api/trading/equity?limit=10 -> rows=5 last={'ts':'2026-08-13T07:42:10Z','equity':99990.0,...}
/api/trading/orders?limit=5  -> orders=5（filled，真实撮合含拒单路径）
```
（对照：autolive 误传真实 adapter 时 fapi 超时 → tick STALE、零订单零净值点；
改 adapter=None 后恢复——证明净值/持仓确实来自 live 会话而非编造的静态值。）

**explorer 页 — 因子清单/预览/评级（02 + 04）**：
```
GET /api/registry/factors -> 200 MOM-001 active（02 双文件清单）
GET /api/admin/overview   -> 200 counts={factors:1,strategies:1,pystrategies:0}
GET /api/admin/factors/MOM-001/md   -> 200 content len=1625
GET /api/admin/strategies/DEM-001/impl -> 200 content len=1423
GET /api/admin/factors/MOM-001/rating?days=30 -> 200
     status=active aggregate.grade=S（04 RatingService 真数据；rank_ic_mean=-0.0997 等）
GET /api/admin/factors/ratings/leaderboard -> 200 entries[0]={MOM-001 grade:S computed:true}
GET /api/admin/bias-control/factors/MOM-001/metrics -> 200
     status=PARTIAL ic=-0.00591 rank_ic=-0.02932 qualification.qualified=false
GET /api/admin/bias-control/qualification -> 200
     summary={total:98,evaluated:1,qualified:0,unqualified:1,not_evaluated:97}
GET /api/admin/bias-control/correlation-matrix -> 200 factor_ids=['MOM-001']
```

**bias-control 页 — 六查批次（04 BiasControlService）**：
```
POST /api/factors/MOM-001/run-check -> 202 run_id=d44d36b02f69
轮询 GET /api/admin/bias-control/runs/d44d36b02f69（3s 间隔）
  -> status PASS, progress={completed:1,total:1,passed:1,failed:0}
GET /api/admin/bias-control/factors/MOM-001 -> 200 overall=PASS
  checks={lookahead:PASS, full_sample:PASS, multiple_testing:PASS, overfit:PASS, cost:PASS}
  oos=LOCKED（development scope 不跑样本外，与 04 语义一致）
GET .../runs/d44d36b02f69/report?format=md|csv|json -> 200（439/193/5458 字节，04 生成）
GET /api/admin/bias-control/overview -> summary={total:1,checked:1,pass:1,fail:0,...}
检查状态：run-check 后 /api/factors/MOM-001/check-status -> {status:checked,source:auto}
attest -> {checked, manual}；override PASS 项 -> 400（仅 FAIL/BLOCKED/ERROR 可解封，如实拒）；
uncheck -> unchecked。
```

**上传 → 热插拔 → 热拔（02 通道）**：
```
POST /api/upload/factor（MD+impl 成对，UPL-001）
  -> 200 validation.registered=True factor_id=UPL-001 errors=[]
GET /api/factors/UPL-001/series?symbol=BTC/USDT&limit=3 -> 200 count=3（真实计算）
DELETE /api/registry/factors/UPL-001 -> 200 deleted=True（文件移 imports/factor_trash/）
GET /api/registry/factors -> UPL-001 消失、MOM-001 仍在
DELETE /api/registry/factors/MOM-001 -> 403（内置因子不从 UI 下架，如实拒）
（验后已清理 factor_trash；imports/ 恢复如初）
```

**策略回测（03 run_strategy）**：
```
POST /api/backtest/strategy {DEM-001, md, 2025-01-01→2025-03-01, 10000}
  -> 200 total_return_pct=-25.48 final_equity=7452.41（Sharpe -1.06，真实计算）
     txt 报告随行返回；positions/transactions 如实空数组（权重调仓无逐笔价）
POST /api/backtest/factor -> 501（无 sim 因子买卖回测引擎，如实拒并指引替代）
```

**如实错误面（无服务不造假）**：/api/signals/rules -> 503；/api/cleaning/config -> 503；
/api/pystrategies -> 200 空列表；/api/factors/export -> 200 xlsx 5110 字节；
/api/factors/MOM-001/self-check-package -> 200 zip 2718 字节。

### 反向验证（红→绿，Chrome 无头 DOM 取证）

- **GREEN**（后端正常，`chrome --headless --dump-dom http://127.0.0.1:8000/`）：
  DOM 含 `<span class="dot ok"></span>API 正常`；ECharts canvas 已渲染（canvas count: 1）。
- **RED**（/api/state 不可达：用 python http.server 静态伺服 web/，/api/* 全部 404）：
  DOM `<span id="health"><span class="dot err"></span>API 异常</span>`；
  `#acc-equity` 等账户位保持 `--`——显示错误而非假数据。
  前端代码路径：pollState catch → console.error('poll state failed') + setHealth(false)。
- **闭环**：kill 服务进程后 `curl /api/state` -> 000 连接被拒绝（curl exit 7）。

### 硬指标 2：四页与源逐字节比对

```
sha256sum 比对（源 = sim_platform-main/.../cryptopaperlab/web/）：
index.html DIFF(3处) explorer.html DIFF(2处) bias-control.html DIFF(1处)
about.html DIFF(1处) lab.png IDENTICAL
diff 逐行输出 = 恰为 API_DIFF.md 记录的 7 处（3 处 API 路径避让 + 4 处
display:none 隐藏策略工厂），无其他差异。
```

### pytest 与地界

```
.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
381 passed, 1 warning in 41.43s        # = 基线 381 / 0 skipped

git status --short（05 地界）：
 M pyproject.toml / requirements.txt   # 各加 2 行依赖（python-multipart/openpyxl）
 M run.py                              # 增强（mounts_to_tail/参数/autolive）
?? src/superplatform_web/simserve.py / ma_overlays.py / routes/sim_*.py（8 个路由模块）
?? web/                                 # 四页 + lab.png + API_DIFF.md
?? PROGRESS_05.md / BLOCKED_05.md
（config/default.yaml、runtime/cli.py、evaluation/* 是 04 地界，未碰）
```

未碰 tests/、01/02/03 地界源码；04 地界只 import 调用其服务；无 skip/todo/
mock/|| true；未 commit（交总控）。

## 留给后续/总控的备注

- 04 联调状态：**已完成**。rating/leaderboard/metrics/qualification/
  correlation-matrix/bias-check 全部经服务层接真（MOM-001 评级 S、开发集
  指标、六查批次 PASS 均实测，见上）。无待联调端点。
- UI 批次的六查按逐因子调 04 run（取消在因子间生效）；多重检验 BH 家族
  因此是单因子家族（family_size=1），与 04 CLI 整批次家族口径不同，
  结果 payload 内如实携带 family_size。
- 相关性矩阵默认集收窄为已评估因子（口径见上方拍板与 BLOCKED_05.md #2）。
- autolive 用合成价格源是离线环境的既定行为（同 03 CLI）；网络恢复后
  手动 POST /api/live/start 会用真实 adapter（00 既有代码路径，未改）。
- 运行 `python run.py` 即得完整演示：四页 200、K线/净值/持仓/评级/六查全真。

---

# 终验（2026-08-13，总控执行）

## 源目录零改动
- exchangia 源：`sha256sum -c SOURCE_exchangia.sha256` → 299 文件全 OK、0 不一致。
- sim_platform 源：`find -newermt "2026-08-13 11:20"` → 0 文件在会话开始后被改动。

## 清单逐项（硬指标）
- 00：pytest 381 passed/0 skipped ≥ 基线 381；requirements.txt 干净 venv 装全且全绿；run.py 起服务（/ 404 监听中、/api/health 200）。✅
- 01：backfill 小集 12 序列 14,987,885 行进 DuckDB；validate-report missing_pct ≤ 0.10% ≤ 10%；永续 earliest=2019-12-31（源端最早，2019-09-26 子项 BLOCKED 有 curl 取证）。✅（1 子项源端受阻）
- 02：imports 双文件 2s 热注册（<10s）、compute 出数；坏 MD 报「规则12 | 字段[数学定义]」；删 impl 不注册；删 MD 注销；factors list 分页/过滤。✅
- 03：evaluate/check MOM-001 PASS；backtest DEM-001 出 Sharpe/MaxDD；live --ticks 5 打印 equity=99990 无异常退出；超资金拒单、反转拆单、testnet 无 key exit=1 不降级。✅
- 04：rating MOM-001 出 S 级真实指标；leaderboard/metrics/qualification/相关性矩阵真实返回；六查批次 PASS 并导出 md/json；空数据 insufficient、前视因子 FAIL（红→绿齐）。✅
- 05：四页 200；/api/state、klines、registry、rating、leaderboard 全真数据；UI 触发六查批次 PASS（run 47e0e47c569c）；四页与源差异恰为 web/API_DIFF.md 记录的 7 处（3 API 路径避让 + 4 处隐藏 AI 工厂入口）；停 /api/state 前端显示异常而非假数据（无头 Chrome 证据）。✅

## 环境备忘
- 依赖新增 2 项（05）：python-multipart、openpyxl（pyproject + requirements 双份）。
- DuckDB 单写者：run.py 服务进程持有 data/cache.duckdb 时，CLI 评估类命令会撞锁——先停服务再跑 CLI。
- 全量回填（40 永续+现货 2019→now）命令就绪：`superplatform backfill --all`（约 1.5 亿行/数小时，断点续跑，见 tools/backfill.py docstring）。
