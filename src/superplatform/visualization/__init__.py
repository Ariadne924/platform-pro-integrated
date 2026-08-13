"""Visualization layer — single-factor reports and dashboard.

Generates:
- Single-factor report pages (IC chart, decay, layer test, turnover)
- Factor library overview dashboard
- Correlation heatmap

All output is self-contained HTML with plotly for interactivity.
"""

from superplatform.visualization.reports import FactorDashboard, FactorReport

__all__ = ["FactorReport", "FactorDashboard"]
