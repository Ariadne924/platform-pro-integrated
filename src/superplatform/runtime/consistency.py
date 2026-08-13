"""Provider-Broker consistency check.

Runtime-layer utility that traces a Strategy's upstream data sources and
verifies they are consistent with the downstream Consumer's target exchange.

Rationale:
  superplatform decouples data sources from execution destinations. You can
  fetch klines from OKX and send orders to Binance. This is a feature
  for cross-exchange strategies, but a footgun for misconfiguration.

  This check makes mismatches VISIBLE rather than silent.

Trace path:
  Strategy → used_factors → Factor.required_data → provider_id → exchange_name
"""

import logging

from superplatform.consumption.base import ConsumerConfig, Strictness
from superplatform.factors.registry import FactorRegistry
from superplatform.factors.resolve import resolve_factor

logger = logging.getLogger(__name__)


class ConsistencyError(Exception):
    """Raised when strictness=STRICT and a provider-broker mismatch is found."""


def _provider_to_exchange(
    provider_id: str,
    mapping: dict[str, str] | None = None,
) -> str:
    """Map a provider ID to its exchange name.

    Default convention: the first '-' segment is the exchange.
        'binance-perp-kline' → 'binance'
        'synthetic-kline'    → 'synthetic'
        'okx-spot-kline'     → 'okx'

    An explicit `mapping` dict overrides the convention.
    """
    if mapping and provider_id in mapping:
        return mapping[provider_id]
    return provider_id.split("-", 1)[0]


def trace_provider_exchanges(
    strategy_name: str,
    factor_registry: FactorRegistry,
    factor_to_providers: dict[str, dict[str, str]],
    # factor_name → {data_type: provider_id}
    provider_exchange_mapping: dict[str, str] | None = None,
) -> set[str]:
    """Trace a strategy back to its source exchanges.

    Args:
        strategy_name: Name of the strategy (must be registered).
        factor_registry: Auto-discovered registry with all Factor classes.
        factor_to_providers: Per-factor provider assignment from factors.yaml.
            e.g. {'rsi_14': {'kline': 'binance-perp-kline'}}
        provider_exchange_mapping: Optional provider_id → exchange_name map.
            Falls back to the '{exchange}-...' naming convention.

    Returns:
        Set of exchange names that the strategy's data comes from.
        e.g. {'binance'} or {'binance', 'okx'}.

    Raises:
        KeyError: If a factor or data_type can't be resolved.
    """
    strategy = None
    from superplatform.strategy.registry import StrategyRegistry
    strategy_reg = StrategyRegistry.get_instance()
    if strategy_name in strategy_reg.list_all():
        strategy = strategy_reg.get(strategy_name)

    if strategy is None:
        raise KeyError(f"Strategy '{strategy_name}' not found in registry")

    factor_names = getattr(strategy, "used_factors", [])
    if not factor_names:
        logger.debug(
            "Strategy '{}' declares no used_factors — skipping consistency check",
            strategy_name,
        )
        return set()

    exchanges: set[str] = set()
    for factor_name in factor_names:
        # Resolve instances too (the consistency trace spans both layers).
        factor = resolve_factor(factor_name, factory_registry=factor_registry)
        for data_type in factor.required_data:
            # Which provider serves this (factor, data_type)?
            if factor_name not in factor_to_providers:
                raise KeyError(
                    f"Factor '{factor_name}' has no provider config in factors.yaml "
                    f"(needed for data_type '{data_type}')"
                )
            provider_map = factor_to_providers[factor_name]
            if data_type not in provider_map:
                raise KeyError(
                    f"Factor '{factor_name}' requires '{data_type}' but "
                    f"factors.yaml only maps: {list(provider_map.keys())}"
                )
            provider_id = provider_map[data_type]
            exchange = _provider_to_exchange(provider_id, provider_exchange_mapping)
            exchanges.add(exchange)

    return exchanges


def check_consistency(
    strategy_name: str,
    consumer: ConsumerConfig,
    factor_registry: FactorRegistry,
    factor_to_providers: dict[str, dict[str, str]],
    provider_exchange_mapping: dict[str, str] | None = None,
) -> None:
    """Verify that a strategy's upstream exchanges match the consumer's target.

    Behavior depends on `consumer.strictness`:
      STRICT → raise ConsistencyError on mismatch
      WARN   → log warning, continue
      SILENT → no-op

    Args:
        strategy_name: Strategy being executed.
        consumer: ConsumerConfig with target_exchange and strictness policy.
        factor_registry: Factor registry with all discovered factors.
        factor_to_providers: Config mapping factor → data_type → provider_id.
        provider_exchange_mapping: Optional override for provider_id → exchange.

    Raises:
        ConsistencyError: If strictness=STRICT and exchanges don't match.
        KeyError: If a factor, data_type, or provider can't be resolved.
    """
    if consumer.strictness == Strictness.SILENT:
        return

    source_exchanges = trace_provider_exchanges(
        strategy_name=strategy_name,
        factor_registry=factor_registry,
        factor_to_providers=factor_to_providers,
        provider_exchange_mapping=provider_exchange_mapping,
    )

    if not source_exchanges:
        return  # strategy has no factors (edge case)

    target = consumer.target_exchange
    mismatch = source_exchanges - {target}

    if not mismatch:
        logger.info(
            "Consistency OK: strategy '{}' sources={}, consumer='{}' target={}",
            strategy_name, source_exchanges, consumer.consumer_id, target,
        )
        return

    msg = (
        f"Data/Execution mismatch for strategy '{strategy_name}': "
        f"factor data from {mismatch}, but consumer '{consumer.consumer_id}' "
        f"executes on '{target}'"
    )

    if consumer.strictness == Strictness.STRICT:
        raise ConsistencyError(msg)
    else:  # WARN
        logger.warning(msg)
