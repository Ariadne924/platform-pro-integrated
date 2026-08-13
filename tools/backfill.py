"""历史数据回填入口 — `python tools/backfill.py ...` 等价 `superplatform backfill ...`。

实现本体在 src/superplatform/data/backfill.py(vision-only 源 + DataCache
增量缓存);CLI 注册在 src/superplatform/runtime/cli.py 的 backfill 子命令。
本文件只是薄入口,便于不配 console script 也能跑。

## 全量回填 README(任务 3 交付,本阶段不跑全量)

范围:40 个 USDT-M 永续(config data.symbols.perpetual)+ BTC/ETH 现货;
时间 2019→now——永续边界 2019-09-25(币安永续上线,更早按空处理不报错;
注意 vision 归档实际自 2019-12-31 起,fundingRate 自 2020-01,OI metrics
自 2020-09,源端再早没有);数据类型 kline(1m+1d)+ funding_rate + open_interest。

命令:
    python tools/backfill.py --all                      # 全类型全标的
    python tools/backfill.py --all --data-type kline --kline-frequencies 1m
    python tools/backfill.py --symbols BTCUSDT,ETHUSDT --market both

预计量级:1m 约 1.5 亿行(40 永续 × ~350 万 + 2 现货 × ~400 万),归档
下载 ~5GB,首次全量数小时;1d/funding/OI 合计仅数十万行,分钟级。DuckDB
缓存约数 GB。

断点续跑:重复执行同一条命令即可。已覆盖区间按缓存与 empty_ranges 书签
跳过,只补缺口与最新尾部;sub-daily kline 按月分块逐块落库,中断不丢
已落库分块。vision 归档 T+1 发布,最新一天的数据下次运行时自动补上。
"""

import sys

from superplatform.runtime.cli import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "backfill", *sys.argv[1:]]
    main()
