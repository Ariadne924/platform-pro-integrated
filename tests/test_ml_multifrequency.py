from __future__ import annotations

import pandas as pd

from superplatform.ml.multifrequency import fuse_factor_panels


def _panel(frequency: str, values: list[float]) -> pd.DataFrame:
    timestamps = pd.date_range(
        "2024-01-01", periods=len(values), freq=frequency, tz="UTC"
    )
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "symbol": "BTC",
            "factor_name": "momentum",
            "factor_value": values,
            "close": 100.0,
            "ret_1": 0.01,
            "ret_5": 0.05,
            "ret_10": 0.10,
            "ret_20": 0.20,
        }
    )


def test_multi_frequency_features_are_backward_aligned_and_namespaced() -> None:
    daily = _panel("D", [1.0, 2.0, 3.0])
    hourly = _panel("12h", [10.0, 20.0, 30.0, 40.0, 50.0])
    result = fuse_factor_panels(
        {"1d": daily, "1h": hourly},
        base_frequency="1d",
    )
    assert result.metadata["future_timestamp_violations"] == 0
    assert set(result.panel["factor_name"]) == {"momentum", "momentum@1h"}
    fused = result.panel[result.panel["factor_name"].eq("momentum@1h")]
    assert fused.sort_values("timestamp")["factor_value"].tolist() == [10.0, 30.0, 50.0]
    assert set(result.panel["ret_1"]) == {0.01}


def test_multi_frequency_labels_always_come_from_base_panel() -> None:
    daily = _panel("D", [1.0, 2.0, 3.0])
    hourly = _panel("12h", [10.0, 20.0, 30.0, 40.0, 50.0])
    hourly["ret_1"] = 999.0
    result = fuse_factor_panels(
        {"1d": daily, "1h": hourly},
        base_frequency="1d",
    )
    assert set(result.panel["ret_1"]) == {0.01}
