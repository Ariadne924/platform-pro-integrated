---
strategy_id: PYS-101
name: trend_following_donchian
version: 2.2.1
status: active
symbols: [BTCUSDT]
params:
  channel: 120
  er_period: 120
  er_long: 0.15
  er_short: 0.15
  gross_target: 0.10
max_leverage: 1
engine_frequency: 1d
data_dependencies:
  - id: btc_4h
    exchange: binance
    market_type: spot
    data_type: kline
    symbol: BTCUSDT
    frequency: 4h
    layer: gold
    required_fields: [open, high, low, close, volume]
    closed_only: true
    group: primary
    align: intersect
implementation: strategies/impl/trend_following_donchian.py
entry: generate
created_at: 2026-08-26
---

## 1. 策略概述

BTC 现货 4h 趋势跟踪。以 Donchian 通道突破作为方向开关，并以效率比
（Efficiency Ratio, ER）过滤震荡行情：只有在效率比足够高时才对通道突破
建仓。信号在每根已完成 4h K 线收盘后产生，汇总为日频目标仓位。

## 2. 逻辑与信号定义

通道：

$$U_t = \max_{i \in [t-\text{channel}, t-1]} high_i,\qquad
L_t = \min_{i \in [t-\text{channel}, t-1]} low_i$$

效率比：

$$ER_t = \frac{|close_t - close_{t-\text{er\_period}}|}{\sum_{i=t-\text{er\_period}}^{t} |close_i - close_{i-1}|}$$

状态机（仅用已收盘数据，无前视）：

- 空仓时：`close_t > U_t 且 ER_t ≥ er_long` → 做多（+1）；
  `close_t < L_t 且 ER_t ≥ er_short` → 做空（-1）；
- 持仓时：`close_t < L_t` 平多、`close_t > U_t` 平空。

日频目标仓位：取当日最后一根 4h 仓位 × `gross_target`，`t` 日信号
`t+1` 日执行（成本、滑点与风控由统一引擎处理）。

## 3. 参数说明

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| channel | 120 | Donchian 通道根数（4h，20 日） |
| er_period | 120 | 效率比窗口根数（4h） |
| er_long | 0.15 | 做多所需最低效率比 |
| er_short | 0.15 | 做空所需最低效率比 |
| gross_target | 0.10 | 目标名义仓位（10%） |

## 4. 执行规则

- 只消费已闭合（`is_closed`）的 Gold 4h K 线；
- 信号时间戳 = 当日最后一根 4h 桶收盘时刻（下一日 UTC 00:00），与统一
  引擎 `position.shift(1)` 对齐，杜绝前视；
- 多空对称，`long_only=false`；缺失 K 线不填零、不参与信号。

## 5. 风控约束

- `gross_target` 上限 10%（`0 < gross_target ≤ 1` 校验）；
- 杠杆上限 1（`max_leverage: 1`）；
- 止损/熔断/敞口封顶等由统一回测引擎与消费层负责，不在策略内重复。

## 6. 回测与有效性记录

- 数据依赖：Binance spot BTCUSDT，Gold 4h（源 1m），仅已闭合；
- 信号频率 4h、引擎频率 1d；
- 有效性记录见 Task-2 统一回测引擎验收与组内 PYS-101 审计（v2.2.x）。

## 7. 变更日志

- 2026-08-26：移植进平台双文件协议，新增 `data_dependencies` 声明与
  `generate(bundle)` 适配入口；逻辑与原始 `generate_positions` 一致。
