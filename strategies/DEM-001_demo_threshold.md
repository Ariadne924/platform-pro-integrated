---
# 示例双文件策略：动量阈值跟随（demo 用，够验收即可）
strategy_id: DEM-001
name: demo_threshold
version: 1.0.0
status: active
symbols: ["BTC/USDT"]
params:
  factor: MOM-001
  threshold: 0.0
max_leverage: 2
risk_limits:
  max_position_notional_per_symbol: null
  max_order_notional: null
implementation: strategies/impl/demo_threshold.py
entry: generate
description: "动量因子值超过阈值做多、低于负阈值做空、之间平仓"
tags: [示例, 动量]
author: superplatform
created_at: 2026-08-13
updated_at: 2026-08-13
---

# DEM-001 demo_threshold：动量阈值跟随策略

## 1. 策略概述

跟随型动量策略：以上示例因子 MOM-001 的输出为信号源，
动量超过阈值做多、低于负阈值做空、回落到阈值带内平仓。
直觉：趋势延续时持单，趋势熄火时离场。

## 2. 逻辑与信号定义

设动量因子值为 $m_t$，阈值为 $\theta \geq 0$，目标仓位：

$$
position_t = \begin{cases}
+1, & m_t > \theta \\
-1, & m_t < -\theta \\
0, & |m_t| \leq \theta
\end{cases}
$$

## 3. 参数说明

| 参数 | 含义 | 默认值 | 取值约束 |
| --- | --- | --- | --- |
| factor | 信号因子 ID | MOM-001 | 已注册双文件因子 |
| threshold | 信号阈值 θ | 0.0 | ≥ 0 |

## 4. 执行规则

输入 factor_results["MOM-001"] 的各标的 FactorResult
（values 含 timestamp/value）；逐标的把 value 映射为
position ∈ {+1, -1, 0}（NaN 记 0），输出含
timestamp/symbol/position 三列的目标仓位表。

## 5. 风控约束

策略级杠杆上限 2；无额外名义仓位覆盖（risk_limits 全 null，
跟随全局 config 上限）。

## 6. 回测与有效性记录

未回测（demo 策略，仅用于双文件通道验收）。

| 检验项 | 结果 | 日期 |
| --- | --- | --- |
| 历史回测 | 未回测 | - |

## 7. 变更日志

| 版本 | 日期 | 变更内容 |
| --- | --- | --- |
| 1.0.0 | 2026-08-13 | 初始版本（02 阶段示例策略） |
