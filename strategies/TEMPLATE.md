---
# ============================================================
# 策略文档模板（TEMPLATE.md）—— superplatform 双文件策略通道
# 本文件为模板，注册中心扫描时会被忽略，不参与注册。
# 使用方式：复制本文件为 strategies/<STRATEGY-ID>_<name>.md（内置）或
# imports/strategies/<STRATEGY-ID>_<name>.md（用户导入），
# 并在同目录 impl/ 下放置实现文件 <name>.py。
#
# 双文件协议要点（与因子协议同构）：
# - MD 文档是唯一事实来源：MD 校验通过且实现文件存在、入口函数可导入，
#   策略才会注册；孤立的 impl/*.py 一律忽略；
# - 校验共 10 条规则，失败时报告（规则编号 + 字段名 + 描述）；
# - 内置与 imports 同 strategy_id 冲突时内置优先并告警；
# - status 语义：draft 注册但不执行；active 参与执行；deprecated 仅展示；
# - 实现文件必须是纯函数：禁止网络 IO、禁止随机性、禁止读写全局状态。
#
# 冻结接口（impl 入口签名，exchangia 约定）：
#     def generate(factor_results: dict[factor_name, dict[symbol, FactorResult]],
#                  **params) -> StrategySignal | DataFrame
#   factor_results 为「因子名 → {标的: FactorResult}」两层字典；
#   返回 DataFrame 时必须含 timestamp/symbol/position 三列，
#   由注册中心自动包装成 StrategySignal（position 为目标仓位权重/方向，
#   多 > 0、空 < 0、平仓 0）。
# ============================================================

# 策略唯一标识，格式 ^[A-Z]{2,8}-\d{3}$，全库唯一（规则 3）
strategy_id: XXX-000
# 策略名，snake_case；实现文件名必须为 <name>.py（规则 4）
name: template_strategy
# 语义化版本号
version: 1.0.0
# 状态：draft（注册但不执行）/ active（参与执行）/ deprecated（仅展示）（规则 5）
status: draft
# 交易标的列表（非空字符串列表）；["*"] 表示 config 中全部标的（规则 6）
symbols: ["BTC/USDT"]
# 参数表（字典，作为 **params 原样传给 generate(factor_results, **params)）
params:
  threshold: 0.0
# 策略级杠杆上限，1~20 的数（规则 7）
max_leverage: 5
# 策略级风控覆盖（可空）：null 表示使用全局 config 上限
risk_limits:
  max_position_notional_per_symbol: null
  max_order_notional: null
# 实现文件路径：内置写 strategies/impl/<name>.py，导入写
# imports/strategies/impl/<name>.py；可导入且含 entry 函数（规则 8）
implementation: strategies/impl/template_strategy.py
# 入口函数名（缺省 generate）
entry: generate
# 一句话描述
description: "一句话说明策略做什么"
tags: [示例]
author: quant
created_at: 2026-01-01
updated_at: 2026-01-01
---

# XXX-000 template_strategy：策略标题

<!-- 正文固定 7 个章节，标题齐全且顺序正确（规则 9）；
     「逻辑与信号定义」章节至少包含一个 $$...$$ 公式块（规则 10）。 -->

## 1. 策略概述

（一句话定义 + 策略类型 + 经济学/行为学直觉）

## 2. 逻辑与信号定义

（开仓/平仓信号的精确定义，至少一个 $$...$$ 公式块）

$$
position_t = \mathbb{1}\{x_t > \theta\} - \mathbb{1}\{x_t < -\theta\}
$$

## 3. 参数说明

（表格：参数/含义/默认值/取值约束）

| 参数 | 含义 | 默认值 | 取值约束 |
| --- | --- | --- | --- |
| threshold | 信号阈值 | 0.0 | 实数 |

## 4. 执行规则

（因子输入、信号生成频率、目标仓位约定；说明 factor_results 输入与
StrategySignal 输出）

## 5. 风控约束

（策略级杠杆上限、名义仓位/单笔订单上限、与全局风控的叠加关系）

## 6. 回测与有效性记录

（初始填"未回测"；预留收益/回撤/胜率/夏普栏位）

| 检验项 | 结果 | 日期 |
| --- | --- | --- |
| 历史回测 | 未回测 | - |

## 7. 变更日志

| 版本 | 日期 | 变更内容 |
| --- | --- | --- |
| 1.0.0 | 2026-01-01 | 初始版本 |
