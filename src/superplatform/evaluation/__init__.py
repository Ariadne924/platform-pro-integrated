"""Evaluation layer — factor assessment, backtesting, bias detection, and reporting.

Submodules
----------
``backtest``    Quantile-layer backtest, long-short NAV, turnover.
``correlation`` Panel-based (canonical) and dict-based factor correlation.
``cost_analysis`` Cost-assumption scenarios and sensitivity.
``experiment``  Standalone experiment runner (governance, regression guard, full deliverable).
``forward_bias`` Forward-looking bias checker class.
``ic``          Single-factor IC / RankIC / ICIR / IC decay (pipeline helper).
``metrics``     Canonical multi-factor IC, preprocessing, winsorization, z-score.
``qc``          Quality-control checks (missing, duplicates, extremes, forward bias).
``report``      Matplotlib plots and deterministic Markdown reporting.
``returns``     Perpetual-contract total-return construction.
``rolling``     Rolling-window IC/ICIR from raw data (pipeline helper).
``stability``   Calendar-time rolling stability from IC timeseries (canonical).
``stratification`` Cross-sectional quantile layer test.
``turnover``    Layer turnover between consecutive periods.
"""

from superplatform.evaluation.backtest import (
    BacktestResult,
    assign_quantiles,
    compute_decile_returns,
    compute_long_short_returns,
    compute_nav,
    compute_turnover,
    run_backtest,
    run_layer_backtest,
    write_backtest_outputs,
)
from superplatform.evaluation.correlation import (
    compute_factor_correlations,
    factor_correlation_from_dict,
    factor_correlation_matrix,
)
from superplatform.evaluation.cost_analysis import (
    CostAssumptions,
    cost_sensitivity,
)
from superplatform.evaluation.forward_bias import (
    ForwardBiasChecker,
    ForwardBiasReport,
)
from superplatform.evaluation.ic import (
    compute_ic_decay,
    compute_icir,
    compute_rankic,
)
from superplatform.evaluation.metrics import (
    compute_ic,
    compute_ic_ir,
    compute_rank_ic,
    evaluate_factor,
    preprocess_factor_panel,
    winsorize,
    zscore,
)
from superplatform.evaluation.qc import (
    check_forward_bias,
    run_qc,
)
from superplatform.evaluation.report import (
    generate_plots,
    plot_correlation_heatmap,
    plot_ic_series,
    plot_layer_nav,
    write_evaluation_report,
)
from superplatform.evaluation.returns import (
    construct_perpetual_returns,
)
from superplatform.evaluation.rolling import (
    rolling_icir,
)
from superplatform.evaluation.stability import (
    compute_rolling_stability,
    rolling_stability,
)
from superplatform.evaluation.stratification import (
    layer_summary,
    layer_test,
)
from superplatform.evaluation.turnover import (
    mean_turnover,
)

__all__ = [
    # backtest
    "BacktestResult",
    "assign_quantiles",
    "compute_decile_returns",
    "compute_long_short_returns",
    "compute_nav",
    "compute_turnover",
    "run_backtest",
    "run_layer_backtest",
    "write_backtest_outputs",
    # correlation
    "compute_factor_correlations",
    "factor_correlation_from_dict",
    "factor_correlation_matrix",
    # cost_analysis
    "CostAssumptions",
    "cost_sensitivity",
    # forward_bias
    "ForwardBiasChecker",
    "ForwardBiasReport",
    # ic (pipeline helpers)
    "compute_ic_decay",
    "compute_icir",
    "compute_rankic",
    # metrics (canonical)
    "compute_ic",
    "compute_ic_ir",
    "compute_rank_ic",
    "evaluate_factor",
    "preprocess_factor_panel",
    "winsorize",
    "zscore",
    # qc
    "check_forward_bias",
    "run_qc",
    # report
    "generate_plots",
    "plot_correlation_heatmap",
    "plot_ic_series",
    "plot_layer_nav",
    "write_evaluation_report",
    # returns
    "construct_perpetual_returns",
    # rolling (pipeline helpers)
    "rolling_icir",
    # stability (canonical)
    "compute_rolling_stability",
    "rolling_stability",
    # stratification
    "layer_summary",
    "layer_test",
    # turnover
    "mean_turnover",
]
