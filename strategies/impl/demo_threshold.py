"""DEM-001 demo_threshold：动量阈值跟随策略（双文件示例策略）。

策略文档（唯一事实来源）：strategies/DEM-001_demo_threshold.md
冻结接口（exchangia 约定）：
    generate(factor_results: dict[factor_name, dict[symbol, FactorResult]],
             **params) -> DataFrame(timestamp, symbol, position)
约束：纯函数；禁止网络 IO；禁止随机性；禁止读写全局状态。
"""

import pandas as pd


def generate(factor_results: dict, **params) -> pd.DataFrame:
    """把动量因子值映射为目标仓位 {+1, -1, 0}。

    :param factor_results: 因子名 → {标的: FactorResult} 两层字典；
                           FactorResult.values 含 timestamp/value 两列
    :param params: factor（信号因子 ID，默认 MOM-001）、
                   threshold（信号阈值 θ，默认 0.0）
    :return: 含 timestamp/symbol/position 三列的 DataFrame
    """
    factor_id = str(params.get("factor", "MOM-001"))
    threshold = float(params.get("threshold", 0.0))

    rows: list[pd.DataFrame] = []
    for symbol, result in (factor_results.get(factor_id) or {}).items():
        values = result.values
        v = values["value"].fillna(0.0)
        position = pd.Series(0.0, index=values.index)
        position[v > threshold] = 1.0
        position[v < -threshold] = -1.0
        frame = pd.DataFrame({
            "timestamp": values["timestamp"],
            "symbol": symbol,
            "position": position,
        })
        rows.append(frame)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "symbol", "position"])
    return pd.concat(rows, ignore_index=True)
