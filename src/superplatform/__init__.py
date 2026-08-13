"""Superplatform — Quantitative Trading Research Framework.

Layered architecture (bottom-up):
    network      — Exchange API adapters, rate limiting, raw data ingestion
    data         — Unified data schemas, hot-swappable providers, validation
    factors      — Factor computation plugins with unified registry
    strategy     — Trading strategies consuming factors
    evaluation   — IC analysis, forward-bias checks, cost sensitivity
    visualization — Single-factor reports, dashboard (consumes evaluation output)
    runtime      — Orchestration, config loading, CLI (top)
"""

__version__ = "0.1.0"


def main() -> None:
    from superplatform.runtime.cli import main as _main

    _main()
