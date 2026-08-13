"""IC (Information Coefficient) computation.

IC = corr(factor_value_t, forward_return_{t+1})
RankIC = Spearman rank correlation of the same.
ICIR = mean(IC) / std(IC) over the evaluation period.

All IC functions work on a DataFrame with columns:
    timestamp, symbol, factor_value, forward_return
"""

import numpy as np
import pandas as pd
from scipy import stats


def _pearson(x, y) -> float:
    """Pearson correlation of two 1-D arrays; NaN on zero variance.

    numpy implementation of the standard formula — scipy's pearsonr has a
    per-call overhead that dominates on panels with thousands of tiny
    cross-sections.
    """
    with np.errstate(invalid="ignore", divide="ignore"):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        x = x - x.mean()
        y = y - y.mean()
        denom = np.sqrt(np.dot(x, x) * np.dot(y, y))
        if denom == 0:
            return float("nan")
        return float(np.dot(x, y) / denom)


def _wide_pearson(f: np.ndarray, r: np.ndarray, min_valid: int):
    """Row-wise Pearson correlation on wide matrices f, r (rows=periods).

    NaN in either matrix excludes that symbol from that period's correlation,
    mirroring the ``factor.notna() & returns.notna()`` mask of the loop version.
    Returns ``(ic, n_stocks)`` arrays; periods with fewer than ``min_valid``
    valid symbols get ``ic = nan`` and are dropped by the caller. Vectorizing
    the per-period loop this way avoids ~O(n_periods) Python-groupby overhead,
    which dominated runtime on long panels.
    """
    valid = np.isfinite(f) & np.isfinite(r)
    cnt = valid.sum(axis=1)
    keep = cnt >= min_valid
    fs = np.where(valid, f, 0.0)
    rs = np.where(valid, r, 0.0)
    n = np.where(keep, cnt, 1)
    fmean = np.where(keep, fs.sum(axis=1) / n, 0.0)
    rmean = np.where(keep, rs.sum(axis=1) / n, 0.0)
    fc = np.where(valid, fs - fmean[:, None], 0.0)
    rc = np.where(valid, rs - rmean[:, None], 0.0)
    num = (fc * rc).sum(axis=1)
    den = np.sqrt((fc * fc).sum(axis=1) * (rc * rc).sum(axis=1))
    with np.errstate(invalid="ignore", divide="ignore"):
        ic = np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)
    ic[~keep] = np.nan
    return ic, cnt


def _ic_by_loop(work, group_col, factor_col, forward_return_col, min_stocks, *, ic_col, rank):
    """Per-period loop reference path, kept for degenerate panels that contain
    duplicate (timestamp, symbol) rows and therefore cannot be pivoted."""
    results = []
    for ts, group in work.groupby(group_col, sort=True):
        factor = group[factor_col]
        returns = group[forward_return_col]
        mask = factor.notna() & returns.notna()
        if mask.sum() < min_stocks:
            continue
        if rank:
            ic = _pearson(
                stats.rankdata(factor[mask].to_numpy()),
                stats.rankdata(returns[mask].to_numpy()),
            )
        else:
            ic = _pearson(factor[mask].to_numpy(), returns[mask].to_numpy())
        results.append({"timestamp": ts, ic_col: ic, "n_stocks": int(mask.sum())})
    return pd.DataFrame(results)


def compute_ic(
    df: pd.DataFrame,
    factor_col: str = "factor_value",
    forward_return_col: str = "forward_return",
    group_col: str = "timestamp",
    symbol_col: str = "symbol",
    min_stocks: int = 2,
) -> pd.DataFrame:
    """Compute cross-sectional Pearson IC per period.

    Returns DataFrame with columns: timestamp, ic, n_stocks
    """
    work = df[[group_col, symbol_col, factor_col, forward_return_col]].copy()
    work[factor_col] = pd.to_numeric(work[factor_col], errors="coerce")
    work[forward_return_col] = pd.to_numeric(work[forward_return_col], errors="coerce")

    if work.empty or work.duplicated([group_col, symbol_col]).any():
        return _ic_by_loop(
            work, group_col, factor_col, forward_return_col, min_stocks, ic_col="ic", rank=False
        )

    wide_f = work.pivot(index=group_col, columns=symbol_col, values=factor_col)
    wide_r = work.pivot(index=group_col, columns=symbol_col, values=forward_return_col)
    ic, cnt = _wide_pearson(wide_f.to_numpy(dtype=float), wide_r.to_numpy(dtype=float), min_stocks)
    keep = cnt >= min_stocks
    return pd.DataFrame({
        "timestamp": wide_f.index[keep],
        "ic": ic[keep],
        "n_stocks": cnt[keep].astype(int),
    })


def compute_rankic(
    df: pd.DataFrame,
    factor_col: str = "factor_value",
    forward_return_col: str = "forward_return",
    group_col: str = "timestamp",
    symbol_col: str = "symbol",
    min_stocks: int = 5,
) -> pd.DataFrame:
    """Compute cross-sectional Rank IC (Spearman) per period.

    ``min_stocks`` must match the one passed to :func:`compute_ic` for the same
    panel, or the two series cover different date ranges (the classic symptom
    is RankIC plotting from sample start while IC starts much later).

    Returns DataFrame with columns: timestamp, rank_ic, n_stocks
    """
    work = df[[group_col, symbol_col, factor_col, forward_return_col]].copy()
    work[factor_col] = pd.to_numeric(work[factor_col], errors="coerce")
    work[forward_return_col] = pd.to_numeric(work[forward_return_col], errors="coerce")

    if work.empty or work.duplicated([group_col, symbol_col]).any():
        return _ic_by_loop(
            work, group_col, factor_col, forward_return_col, min_stocks, ic_col="rank_ic", rank=True
        )

    wide_f = work.pivot(index=group_col, columns=symbol_col, values=factor_col)
    wide_r = work.pivot(index=group_col, columns=symbol_col, values=forward_return_col)
    # Spearman = Pearson on ranked values. A symbol whose factor or return is
    # NaN must be excluded from *both* rankings (same joint mask as the loop),
    # so mask before ranking rather than after.
    valid = np.isfinite(wide_f) & np.isfinite(wide_r)
    f = wide_f.where(valid).rank(axis=1, method="average").to_numpy(dtype=float)
    r = wide_r.where(valid).rank(axis=1, method="average").to_numpy(dtype=float)
    ic, cnt = _wide_pearson(f, r, min_stocks)
    keep = cnt >= min_stocks
    return pd.DataFrame({
        "timestamp": wide_f.index[keep],
        "rank_ic": ic[keep],
        "n_stocks": cnt[keep].astype(int),
    })


def compute_icir(ic_series: pd.Series) -> dict:
    """Compute ICIR = mean(IC) / std(IC) and related statistics.

    Returns dict with: icir, mean_ic, std_ic, ic_positive_ratio, ic_ir_tstat
    """
    ic = ic_series.dropna()
    if len(ic) < 2:
        return {
            "icir": np.nan,
            "mean_ic": np.nan,
            "std_ic": np.nan,
            "ic_positive_ratio": np.nan,
            "ic_ir_tstat": np.nan,
        }
    mean_ic = ic.mean()
    std_ic = ic.std()
    icir = mean_ic / std_ic if std_ic > 0 else np.nan
    return {
        "icir": icir,
        "mean_ic": mean_ic,
        "std_ic": std_ic,
        "ic_positive_ratio": (ic > 0).mean(),
        "ic_ir_tstat": icir * np.sqrt(len(ic)) if not np.isnan(icir) else np.nan,
    }


def compute_ic_decay(
    df: pd.DataFrame,
    factor_col: str = "factor_value",
    forward_return_cols: list[str] | None = None,
    max_lag: int = 20,
    group_col: str = "timestamp",
    symbol_col: str = "symbol",
    min_stocks: int = 2,
) -> pd.DataFrame:
    """Compute IC decay — IC for increasingly distant forward returns.

    If forward_return_cols is a list like ['ret_t1','ret_t2',...,'ret_t20'],
    compute IC for each horizon. Otherwise, assumes a single forward_return_col
    and shifts are computed internally.

    Returns DataFrame with columns: horizon, mean_ic, icir
    """
    if forward_return_cols is None:
        forward_return_cols = [f"forward_return_t{i}" for i in range(1, max_lag + 1)]
    present = [col for col in forward_return_cols if col in df.columns]
    if not present:
        return pd.DataFrame()

    # Numeric conversion once, then each horizon's IC is computed from a
    # single vectorized wide-matrix pass (previously a per-period × per-horizon
    # Python loop calling _pearson ~O(periods × horizons) times, which
    # dominated runtime on long panels).
    work = df[[group_col, symbol_col, factor_col, *present]].copy()
    for col in (factor_col, *present):
        work[col] = pd.to_numeric(work[col], errors="coerce")

    per_horizon: dict[str, list[float]] = {col: [] for col in present}

    if work.empty or work.duplicated([group_col, symbol_col]).any():
        # Degenerate panel with duplicate (timestamp, symbol) rows cannot be
        # pivoted — fall back to the per-period loop.
        for _, group in work.groupby(group_col, sort=True):
            factor = group[factor_col].to_numpy(dtype=float)
            for col in present:
                returns = group[col].to_numpy(dtype=float)
                mask = ~(np.isnan(factor) | np.isnan(returns))
                if mask.sum() < min_stocks:
                    continue
                per_horizon[col].append(_pearson(factor[mask], returns[mask]))
    else:
        wide_f = work.pivot(index=group_col, columns=symbol_col, values=factor_col).to_numpy(dtype=float)
        for col in present:
            wide_r = work.pivot(index=group_col, columns=symbol_col, values=col).to_numpy(dtype=float)
            ic, cnt = _wide_pearson(wide_f, wide_r, min_stocks)
            keep = cnt >= min_stocks
            per_horizon[col].extend(ic[keep].tolist())

    results = []
    for col in present:
        ic_series = pd.Series(per_horizon[col], dtype=float)
        if ic_series.empty:
            continue
        stats_result = compute_icir(ic_series)
        horizon = int(col.split("_t")[-1]) if "_t" in col else 0
        results.append({
            "horizon": horizon,
            "mean_ic": stats_result["mean_ic"],
            "icir": stats_result["icir"],
            "ic_positive_ratio": stats_result["ic_positive_ratio"],
        })
    return pd.DataFrame(results)
