"""双文件因子/策略（02 通道）→ 运行时（03）的接线层。

02 的 `DualFactorRegistry` / `DualStrategyRegistry` 把 MD+impl 校验后包装成
exchangia 的 Factor/Strategy 并注册进 `FactorRegistry` / `StrategyRegistry`
单例。本模块只负责运行侧的三件接线事，不改 02 的任何文件：

1. 扫描时机：双文件注册中心是惰性单例——CLI/运行时首次用到时
   `ensure_scanned()`（首扫全量，之后每次调用只做 mtime 增量 diff，
   这正是热插拔入口；长驻的 LiveRuntime 每 tick 调一次）；
2. 双文件因子没有 config 条目：`dual_factor_entry` 按 MD 记录
   （frequency / lookback）合成一个最小评估配置，symbols 默认取研究池
   `data.symbols.perpetual`（截面 IC 需要
   `evaluation.ic.min_stocks_per_period` 以上的标的数）；
3. 双文件策略的 `used_factors` 为空（impl 按 MD params 里的因子 ID
   自取输入）：`dual_strategy_factor_ids` 从策略 MD params 中识别
   「取值是已注册因子 ID」的参数，推导运行时要先算的因子列表。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from superplatform.factors.dual_registry import DualFactorRegistry
from superplatform.factors.instance_registry import FactorInstanceRegistry
from superplatform.strategy.base import Strategy
from superplatform.strategy.dual_registry import DualStrategyRegistry
from superplatform.strategy.registry import StrategyRegistry

if TYPE_CHECKING:
    from superplatform.runtime.config import Config


def scan_dual_registries() -> None:
    """对两个双文件注册中心各做一次 ensure_scanned（首扫全量/之后增量）。"""
    DualFactorRegistry.get_instance().ensure_scanned()
    DualStrategyRegistry.get_instance().ensure_scanned()


def dual_factor_entry(
    factor_id: str,
    config: Config,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为双文件因子合成最小评估配置；非双文件因子返回 {}。

    返回的条目与 config ``factors.<name>`` 同构（symbols / frequency /
    start / end），pipeline 的取数与分组逻辑原样适用。MD params 已由
    02 的包装层合并进 compute，无需在这里重复下发。

    ``overrides`` 可携带 symbols / start / end（CLI --symbols/--start/--end），
    缺省时 symbols 取研究池 ``data.symbols.perpetual``，start/end 回落到
    ``evaluation.sample_start`` / ``evaluation.sample_end``（由 pipeline 处理）。
    """
    dual = DualFactorRegistry.get_instance()
    dual.ensure_scanned()
    rec = dual.get_record(factor_id)
    if rec is None:
        return {}
    overrides = {k: v for k, v in (overrides or {}).items() if v}
    symbols = overrides.pop("symbols", None) or config.get("data.symbols.perpetual") or ["BTCUSDT"]
    entry: dict[str, Any] = {
        "symbols": list(symbols),
        "frequency": rec.frequency or "1d",
    }
    for key in ("start", "end"):
        if overrides.get(key):
            entry[key] = overrides[key]
    return entry


def dual_strategy_factor_ids(strategy_id: str) -> list[str]:
    """推导双文件策略的信号因子列表（非双文件策略返回 []）。

    规则：策略 MD params 中，取值是字符串且命中已注册双文件因子 ID
    或因子实例名的参数，视为信号因子引用（如 DEM-001 的
    ``params.factor: MOM-001``）。保持参数声明顺序、去重。
    """
    srec = DualStrategyRegistry.get_instance().get_record(strategy_id)
    if srec is None:
        return []
    factors = DualFactorRegistry.get_instance()
    instances = FactorInstanceRegistry.get_instance()
    ids: list[str] = []
    for value in srec.params.values():
        if not isinstance(value, str):
            continue
        if factors.get_record(value) is not None or instances.has(value):
            if value not in ids:
                ids.append(value)
    return ids


def resolve_strategy_ex(strategy_name: str) -> tuple[Strategy, list[str], bool]:
    """解析策略：decorator 通道优先，未注册时回退双文件通道。

    返回 ``(strategy, used_factors, is_dual)``：

    - decorator / config 策略：与原逻辑一致，``used_factors`` 取策略声明，
      ``is_dual=False``（调用方继续做实例治理校验）；
    - 双文件策略：``used_factors`` 由 ``dual_strategy_factor_ids`` 推导，
      ``is_dual=True``（因子经 02 协议校验注册，不走实例治理）。

    未注册的名字先触发一次双文件增量扫描再重试，仍找不到时抛
    ``KeyError``（与原 ``StrategyRegistry.get`` 行为一致）。
    """
    registry = StrategyRegistry.get_instance()
    try:
        strategy = registry.get(strategy_name)
    except KeyError:
        DualStrategyRegistry.get_instance().ensure_scanned()
        strategy = registry.get(strategy_name)
    srec = DualStrategyRegistry.get_instance().get_record(strategy_name)
    if srec is None:
        return strategy, list(strategy.used_factors), False
    DualFactorRegistry.get_instance().ensure_scanned()
    return strategy, dual_strategy_factor_ids(strategy_name), True


# 各频率每年的 bar 数（加密市场 7×24），用于回测指标年化。
_PERIODS_PER_YEAR = {
    "1m": 525_600,
    "5m": 105_120,
    "15m": 35_040,
    "30m": 17_520,
    "1h": 8_760,
    "4h": 2_190,
    "8h": 1_095,
    "1d": 365,
    "1w": 52,
}


def periods_per_year(frequency: str | None) -> int:
    """按 K 线频率给年化周期的 bar 数；缺省/未知按 1d=365（历史默认）。"""
    return _PERIODS_PER_YEAR.get(str(frequency or "1d"), 365)
