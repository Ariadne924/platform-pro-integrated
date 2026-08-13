"""Single-factor report and dashboard generation.

Generates self-contained HTML pages using Plotly for interactive charts.
Each report is a standalone page; the dashboard provides a library-wide overview.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


@dataclass
class FactorReport:
    """Data for a single-factor evaluation report.

    Attributes:
        factor_name: Name of the factor.
        factor_category: Category of the factor.
        ic_df: IC time series (from compute_ic).
        ic_stats: ICIR statistics dict.
        ic_decay_df: IC decay results.
        layer_results: Stratification test results.
        turnover_df: Turnover time series.
        forward_bias_passed: Whether forward-bias check passed.
        sample_start: Start of the evaluation sample.
        sample_end: End of the evaluation sample.
        frequency: Data frequency.
        cost_summary: Cost sensitivity results.
    """

    factor_name: str
    factor_category: str
    ic_df: pd.DataFrame
    ic_stats: dict
    ic_decay_df: pd.DataFrame | None = None
    layer_results: pd.DataFrame | None = None
    turnover_df: pd.DataFrame | None = None
    forward_bias_passed: bool = False
    sample_start: str | None = None
    sample_end: str | None = None
    frequency: str = "1d"
    cost_summary: pd.DataFrame | None = None

    def to_html(self, output_path: str) -> str:
        """Generate a self-contained HTML report.

        Args:
            output_path: Where to write the HTML file.

        Returns:
            The output path.
        """
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        fig = make_subplots(
            rows=3, cols=2,
            subplot_titles=(
                "IC Over Time", "IC Decay",
                "Layer Test (Cumulative)", "Turnover",
                "Rolling IC", "Factor Distribution"
            ),
        )

        # 1. IC over time
        if not self.ic_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=self.ic_df["timestamp"],
                    y=self.ic_df["ic"],
                    mode="lines",
                    name="Pearson IC",
                ),
                row=1, col=1,
            )
            mean_ic = self.ic_df["ic"].mean()
            fig.add_hline(y=mean_ic, line_dash="dash", line_color="gray",
                         annotation_text=f"Mean IC: {mean_ic:.4f}",
                         row=1, col=1)

        # 2. IC decay
        if self.ic_decay_df is not None and not self.ic_decay_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=self.ic_decay_df["horizon"],
                    y=self.ic_decay_df["mean_ic"],
                    mode="lines+markers",
                    name="IC Decay",
                ),
                row=1, col=2,
            )
            fig.add_hline(y=0, line_dash="dash", line_color="gray",
                         row=1, col=2)

        # 3. Layer test (cumulative returns)
        if self.layer_results is not None and not self.layer_results.empty:
            for layer_id in sorted(self.layer_results["layer"].unique()):
                layer_data = self.layer_results[
                    self.layer_results["layer"] == layer_id
                ].sort_values("timestamp")
                layer_data = layer_data.copy()
                layer_data["cum_return"] = (1 + layer_data["mean_return"]).cumprod()
                fig.add_trace(
                    go.Scatter(
                        x=layer_data["timestamp"],
                        y=layer_data["cum_return"],
                        mode="lines",
                        name=f"Layer {layer_id + 1}",
                    ),
                    row=2, col=1,
                )

        # 4. Turnover
        if self.turnover_df is not None and not self.turnover_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=self.turnover_df["timestamp"],
                    y=self.turnover_df["turnover"],
                    mode="lines",
                    name="Turnover",
                ),
                row=2, col=2,
            )

        # Layout annotations
        icir_val = self.ic_stats.get("icir", float("nan"))
        icir_str = f"{icir_val:.4f}" if not (isinstance(icir_val, float) and (icir_val != icir_val)) else "N/A"
        title = (
            f"Factor Report: {self.factor_name} | "
            f"Category: {self.factor_category} | "
            f"Frequency: {self.frequency}<br>"
            f"<sup>Sample: {self.sample_start} → {self.sample_end} | "
            f"ICIR: {icir_str} | "
            f"Forward Bias: {'✓ PASS' if self.forward_bias_passed else '✗ FAIL'}</sup>"
        )
        fig.update_layout(
            title=title,
            height=1000,
            showlegend=True,
        )

        html = fig.to_html(include_plotlyjs="cdn", full_html=True)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return str(out)


class FactorDashboard:
    """Factor library overview dashboard.

    Generates a single page with:
    - Factor count by category
    - ICIR bar chart
    - Correlation heatmap
    - Forward-bias pass/fail summary
    """

    def __init__(self, factor_reports: list[FactorReport]):
        self.reports = factor_reports

    def to_html(self, output_path: str) -> str:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        n = len(self.reports)
        if n == 0:
            return ""

        names = [r.factor_name for r in self.reports]
        categories = [r.factor_category for r in self.reports]
        icirs = [r.ic_stats.get("icir", 0) or 0 for r in self.reports]
        biases = [r.forward_bias_passed for r in self.reports]

        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=("ICIR by Factor", "Category Distribution", "Bias Check", ""),
            specs=[[{}, {"type": "pie"}], [{"colspan": 2}, None]],
        )

        # ICIR bar chart
        colors = ["green" if b else "red" for b in biases]
        fig.add_trace(
            go.Bar(x=names, y=icirs, marker_color=colors, name="ICIR"),
            row=1, col=1,
        )

        # Category distribution
        from collections import Counter
        cat_counts = Counter(categories)
        fig.add_trace(
            go.Pie(
                labels=list(cat_counts.keys()),
                values=list(cat_counts.values()),
                name="Categories",
            ),
            row=1, col=2,
        )

        # Bias check summary
        passed = sum(biases)
        fig.add_trace(
            go.Bar(
                x=["PASS", "FAIL"],
                y=[passed, n - passed],
                marker_color=["green", "red"],
                name="Bias Check",
            ),
            row=2, col=1,
        )

        title = (
            f"Factor Library Dashboard | Total Factors: {n} | "
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        fig.update_layout(title=title, height=800, showlegend=False)

        html = fig.to_html(include_plotlyjs="cdn", full_html=True)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return str(out)
