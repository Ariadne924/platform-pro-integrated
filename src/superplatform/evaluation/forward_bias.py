"""Forward-looking bias checker.

This is the HARD GATE for Task 1 — every factor MUST pass this check.

The checker:
1. Takes the factor computation pipeline
2. Truncates data at progressively earlier cutoff dates
3. Re-computes factor values
4. Verifies that historical factor values are identical (not contaminated by future data)

If a factor's historical values CHANGE when future data is removed,
it has a forward-looking bias and MUST be fixed.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class ForwardBiasReport:
    """Result of a forward-bias check for one factor.

    Attributes:
        factor_name: Name of the factor checked.
        passed: True if no forward-looking bias detected.
        n_cutoffs: Number of truncation points tested.
        n_mismatches: Number of cutoffs where historical values diverged.
        max_abs_diff: Maximum absolute difference in historical values.
        details: Per-cutoff details for audit trail.
    """

    factor_name: str
    passed: bool
    n_cutoffs: int
    n_mismatches: int
    max_abs_diff: float
    details: list[dict] = field(default_factory=list)


class ForwardBiasChecker:
    """Automated forward-looking bias detection.

    Usage:
        checker = ForwardBiasChecker(n_cutoffs=5)
        report = checker.check(
            factor_name="momentum",
            compute_fn=lambda df: my_factor_compute(df),
            data=full_dataframe,
            date_col="timestamp",
        )
        assert report.passed, f"Forward bias in {report.factor_name}!"
    """

    def __init__(self, n_cutoffs: int = 5, tolerance: float = 1e-12):
        """
        Args:
            n_cutoffs: Number of progressively earlier cutoffs to test.
            tolerance: Numerical tolerance for comparison (floating point).
        """
        self.n_cutoffs = n_cutoffs
        self.tolerance = tolerance

    def check(
        self,
        factor_name: str,
        compute_fn: Callable[[pd.DataFrame], pd.DataFrame],
        data: pd.DataFrame,
        date_col: str = "timestamp",
        id_col: str | None = "symbol",
        baseline: pd.DataFrame | pd.Series | None = None,
    ) -> ForwardBiasReport:
        """Run the forward-bias check.

        Args:
            factor_name: Name of the factor.
            compute_fn: Function that takes a DataFrame and returns factor values.
                The returned DataFrame must have columns [date_col, id_col, 'value']
                or be indexable to get factor values.
            data: Full dataset (must be sorted by date_col ascending).
            date_col: Name of the timestamp column.
            id_col: Name of the asset identifier column.
            baseline: Optional pre-computed full-sample factor values (the raw
                ``compute_fn`` output, or an already-extracted Series). When
                provided, the checker skips the redundant full-sample recompute
                and compares every truncated recomputation directly against it.
                Callers that already computed the factor on the full sample
                (the normal pipeline flow) pass those values to avoid one full
                factor compute per audit.

        Returns:
            ForwardBiasReport with pass/fail and detailed audit trail.
        """
        data = data.sort_values(date_col).reset_index(drop=True)
        unique_dates = data[date_col].drop_duplicates().sort_values()
        if len(unique_dates) < self.n_cutoffs + 2:
            raise ValueError(
                f"Need at least {self.n_cutoffs + 2} unique dates, "
                f"got {len(unique_dates)}"
            )

        # Full-sample computation as baseline. Reuse the caller's when supplied
        # (it is the same computation, already done by the main factor pass).
        if baseline is None:
            full_result = compute_fn(data)
            full_values = self._extract_values(full_result, date_col, id_col)
        elif isinstance(baseline, pd.Series):
            full_values = baseline
        else:
            full_values = self._extract_values(baseline, date_col, id_col)

        # Progressive truncation
        date_step = max(1, len(unique_dates) // (self.n_cutoffs + 1))
        details = []
        n_mismatches = 0
        max_abs_diff = 0.0

        for i in range(1, self.n_cutoffs + 1):
            cutoff_idx = len(unique_dates) - i * date_step
            cutoff_date = unique_dates.iloc[cutoff_idx]
            truncated = data[data[date_col] <= cutoff_date].copy()
            truncated_result = compute_fn(truncated)
            truncated_values = self._extract_values(truncated_result, date_col, id_col)

            # Compare overlapping timestamps
            common_keys = full_values.index.intersection(truncated_values.index)
            if len(common_keys) == 0:
                detail = {
                    "cutoff_date": cutoff_date,
                    "common_points": 0,
                    "max_abs_diff": 0.0,
                    "note": "No overlapping data",
                }
                details.append(detail)
                continue

            diff = (full_values.loc[common_keys] - truncated_values.loc[common_keys]).abs()
            max_diff = diff.max()
            max_abs_diff = max(max_abs_diff, max_diff)
            is_mismatch = max_diff > self.tolerance

            if is_mismatch:
                n_mismatches += 1

            details.append({
                "cutoff_date": str(cutoff_date),
                "common_points": len(common_keys),
                "max_abs_diff": float(max_diff),
                "is_mismatch": bool(is_mismatch),
            })

        passed = bool(n_mismatches == 0)
        return ForwardBiasReport(
            factor_name=factor_name,
            passed=passed,
            n_cutoffs=self.n_cutoffs,
            n_mismatches=int(n_mismatches),
            max_abs_diff=float(max_abs_diff),
            details=details,
        )

    @staticmethod
    def _extract_values(
        result: pd.DataFrame,
        date_col: str,
        id_col: str | None,
    ) -> pd.Series:
        """Extract factor values as a Series indexed by (date, [id])."""
        if "value" in result.columns:
            val_col = "value"
        else:
            numeric_cols = result.select_dtypes(include=[np.number]).columns
            non_meta = [c for c in numeric_cols if c not in (date_col, id_col)]
            val_col = non_meta[0] if non_meta else result.columns[-1]

        keys = [date_col]
        if id_col and id_col in result.columns:
            keys.append(id_col)
        return result.set_index(keys)[val_col]
