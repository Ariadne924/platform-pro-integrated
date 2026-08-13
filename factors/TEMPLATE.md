---
# ============================================================
# 因子文档模板（TEMPLATE.md）—— superplatform 双文件因子通道
# 本文件为模板，注册中心扫描时会被忽略，不参与注册。
# 使用方式：复制本文件为 factors/<FACTOR-ID>_<name>.md（内置）或
# imports/factors/<FACTOR-ID>_<name>.md（用户导入，与内置分开存放），
# 并在同目录 impl/ 下放置实现文件 <name>.py。
#
# 双文件协议要点：
# - MD 文档是唯一事实来源：MD 校验通过且实现文件存在、入口函数可导入，
#   因子才会注册；孤立的 impl/*.py 一律忽略；
# - 校验共 12 条规则，失败时报告（规则编号 + 字段名 + 描述）；
# - 内置与 imports 同 factor_id 冲突时内置优先并告警；
# - status 语义：draft 注册但不计算；active 参与计算；deprecated 仅展示；
# - 实现文件必须是纯函数：禁止网络 IO、禁止随机性、禁止读写全局状态。
#
# 冻结接口（impl 入口签名，exchangia 多标的约定）：
#     def compute(data: dict[data_type, dict[symbol, DataFrame]], **params)
#         -> FactorResult | DataFrame(timestamp, value) | Series
#   data 为「数据类型 → {标的: DataFrame}」两层字典；单标的因子取
#   list(data["kline"].values())[0]；K线 DataFrame 含 timestamp/open/high/
#   low/close/volume 等列。返回 DataFrame/Series 时由注册中心自动包装成
#   FactorResult（values 含 timestamp/value 两列）。
# ============================================================

# 因子唯一标识，格式 ^[A-Z][A-Z0-9]{1,7}-\d{3}$，全库唯一（规则 3）
factor_id: XXX-000
# 因子名，snake_case；实现文件名必须为 <name>.py（规则 4）
name: template_factor
# 分类枚举：momentum / reversal / volatility / volume / technical /
# microstructure / basis_funding / onchain / sentiment / cross_asset /
# ml_feature / other（规则 5）
category: other
# 语义化版本号
version: 1.0.0
# 状态：draft（注册但不计算）/ active（参与计算）/ deprecated（仅展示）（规则 6）
status: draft
# 计算频率：tick / 1m / 5m / 1h / 4h / 1d（规则 7）
frequency: 1m
# 回看窗口（正整数，基于 1m 主K线）（规则 8）
lookback_bars: 60
# 输入字段，子集 ⊆ {open, high, low, close, volume, quote_volume, trades,
# taker_buy_volume, vwap, funding_rate, open_interest, mark_price}（规则 9）
inputs: [close]
# 参数表（字典，作为 **params 原样传给 compute(data, **params)）
params:
  window: 60
# 输出说明
output:
  type: float
  description: "一句话说明输出含义"
  direction: "值越大越看多"
# 实现文件路径：内置写 factors/impl/<name>.py，导入写
# imports/factors/impl/<name>.py；可导入且含 entry 函数（规则 10）
implementation: factors/impl/template_factor.py
# 入口函数名（缺省 compute）
entry: compute
data_sources: [binance_kline]
tags: [示例]
author: quant
created_at: 2026-01-01
updated_at: 2026-01-01
references:
  - "参考文献条目"
---

# XXX-000 template_factor：因子标题

<!-- 正文固定 10 个章节，标题齐全且顺序正确（规则 11）；
     「数学定义」章节至少包含一个 $$...$$ 公式块（规则 12）。 -->

## 1. 因子概述

（一句话定义 + 经济学/行为学直觉）

## 2. 数学定义

$$
F_t = \frac{1}{N} \sum_{i=0}^{N-1} x_{t-i}, \quad N = 60
$$

## 3. 输入与参数

（表格：字段/含义/默认值/取值约束）

| 字段 | 含义 | 默认值 | 取值约束 |
| --- | --- | --- | --- |
| close | 收盘价 | - | > 0 |

| 参数 | 含义 | 默认值 | 取值约束 |
| --- | --- | --- | --- |
| window | 滚动窗口 | 60 | 正整数 |

## 4. 计算步骤

（编号步骤，与代码实现一一对应）

1. 步骤一；
2. 步骤二。

## 5. 输出与解释

（取值范围、方向约定、中性值）

## 6. 数据依赖与频率

（数据源、计算频率、降频与 forward-fill 行为）

## 7. 边界条件与异常处理

（数据不足 lookback、缺失值、除零的处理方式）

## 8. 适用范围与已知局限

（适用的市场状态、品种范围与已知失效场景）

## 9. 有效性检验记录

未检验。

| 检验项 | 结果 | 日期 |
| --- | --- | --- |
| IC / RankIC | 未检验 | - |
| 分层回测 | 未检验 | - |

## 10. 变更日志

| 版本 | 日期 | 变更内容 |
| --- | --- | --- |
| 1.0.0 | 2026-01-01 | 初始版本 |
