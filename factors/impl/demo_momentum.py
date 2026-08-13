"""MOM-001 demo_momentum：N 根K线简单动量（双文件示例因子）。

因子文档（唯一事实来源）：factors/MOM-001_demo_momentum.md
冻结接口（exchangia 多标的约定）：
    compute(data: dict[data_type, dict[symbol, DataFrame]], **params)
    -> DataFrame(timestamp, value)
约束：纯函数；禁止网络 IO；禁止随机性；禁止读写全局状态。
"""

import pandas as pd


def compute(data: dict, **params) -> pd.DataFrame:
    """计算简单动量：close / close.shift(window) - 1。

    :param data: 数据类型 → {标的: DataFrame} 两层字典；单标的因子，
                 K线 DataFrame 含 timestamp/close 等列
    :param params: window（回看窗口，K线根数，默认 20）
    :return: 含 timestamp/value 两列的 DataFrame（头部 window 根为 NaN）
    """
    window = int(params.get("window", 20))
    kline = list(data["kline"].values())[0]
    result = pd.DataFrame({"timestamp": kline["timestamp"]})
    result["value"] = kline["close"] / kline["close"].shift(window) - 1
    return result
