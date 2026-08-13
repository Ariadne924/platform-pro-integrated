---
# 示例双文件因子：N 根K线简单动量（demo 用，够验收即可）
factor_id: MOM-001
name: demo_momentum
category: momentum
version: 1.0.0
status: active
frequency: 1h
lookback_bars: 21
inputs: [close]
params:
  window: 20
output:
  type: float
  description: "过去 window 根K线的简单收益率"
  direction: "值越大越看多"
implementation: factors/impl/demo_momentum.py
entry: compute
data_sources: [binance_kline]
tags: [示例, 动量]
author: superplatform
created_at: 2026-08-13
updated_at: 2026-08-13
references:
  - "Jegadeesh & Titman (1993), Returns to Buying Winners and Selling Losers"
---

# MOM-001 demo_momentum：简单价格动量

## 1. 因子概述

过去 N 根K线的简单收益率。直觉：短期趋势延续（动量效应），
涨的继续涨、跌的继续跌。

## 2. 数学定义

$$
MOM_t = \frac{P_t}{P_{t-N}} - 1, \quad N = \mathrm{window} = 20
$$

其中 $P_t$ 为第 t 根K线收盘价。

## 3. 输入与参数

| 字段 | 含义 | 默认值 | 取值约束 |
| --- | --- | --- | --- |
| close | 收盘价 | - | > 0 |

| 参数 | 含义 | 默认值 | 取值约束 |
| --- | --- | --- | --- |
| window | 动量回看窗口（K线根数） | 20 | 正整数 |

## 4. 计算步骤

1. 取 data["kline"] 中单个标的的K线 DataFrame（单标的因子）；
2. 读取 close 列，计算 close / close.shift(window) - 1；
3. 组装 timestamp/value 两列返回。

## 5. 输出与解释

输出为无量纲收益率，中性值 0；> 0 表示过去 window 根K线上涨，
< 0 表示下跌。绝对值越大趋势越强。

## 6. 数据依赖与频率

依赖K线收盘价（kline.close）；声明频率 1h；lookback_bars = window + 1
（shift 需要前 window 根 + 当前根）。

## 7. 边界条件与异常处理

- 数据不足 window + 1 根时，头部值为 NaN（shift 自然产生），不插值；
- close ≤ 0 视为脏数据，不特殊处理（上游 KLineSchema 已校验 > 0）。

## 8. 适用范围与已知局限

适用于流动性好的主流标的；震荡市动量信号反复打脸；
对跳空与插针敏感，未做异常值裁剪。

## 9. 有效性检验记录

未检验（demo 因子，仅用于双文件通道验收）。

| 检验项 | 结果 | 日期 |
| --- | --- | --- |
| IC / RankIC | 未检验 | - |
| 分层回测 | 未检验 | - |

## 10. 变更日志

| 版本 | 日期 | 变更内容 |
| --- | --- | --- |
| 1.0.0 | 2026-08-13 | 初始版本（02 阶段示例因子） |
