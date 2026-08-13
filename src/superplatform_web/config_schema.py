"""Dynamic configuration schema generation.

Walks the merged :class:`Config` value tree and produces a self-describing
schema (types, defaults, descriptions, editability, sections) so the
frontend can render a config editor with zero per-key code.

Descriptions are extracted from inline YAML comments in the base config
files (``default.yaml`` / ``exchanges.yaml``) using ruamel.yaml round-trip
mode, so editing the YAML is the only thing needed to change what the
Settings page shows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from superplatform.runtime.config import Config

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # config_schema.py → project root
_BASE_FILES = (
    Path(_PROJECT_ROOT) / "config" / "default.yaml",
    Path(_PROJECT_ROOT) / "config" / "exchanges.yaml",
)

# Section labels matched against the dotted key prefix (most specific first).
_SECTION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("evaluation.cost.", "成本假设"),
    ("evaluation.ic_decay.", "IC 衰减"),
    ("evaluation.ic.", "IC 计算"),
    ("evaluation.forward_bias.", "前视检查"),
    ("evaluation.oos_", "样本外区间"),
    ("evaluation.", "评估"),
    ("data.cache.", "数据缓存"),
    ("data.validation.", "数据校验"),
    ("data.symbols.", "标的池"),
    ("data.", "数据"),
    ("live.paper.", "模拟盘"),
    ("live.risk.", "风控"),
    ("live.reconnect.", "重连"),
    ("live.database.", "数据库"),
    ("live.", "实盘"),
    ("visualization.", "可视化"),
    ("exchanges.", "交易所"),
    ("project.", "项目"),
    ("defaults.", "默认值"),
)

# Human-readable labels for well-known keys; others are humanized from the key.
_LABELS: dict[str, str] = {
    "defaults.exchange": "默认交易所",
    "defaults.market": "默认市场",
    "project.name": "项目名称",
    "project.version": "项目版本",
    "data.timezone": "时区",
    "data.cache_dir": "缓存目录",
    "data.output_format": "输出格式",
    "data.max_concurrent_requests": "最大并发请求数",
    "data.cache.enabled": "启用本地缓存",
    "data.cache.path": "缓存数据库路径",
    "data.symbols.perpetual": "默认永续标的池",
    "data.symbols.spot": "默认现货标的池",
    "data.validation.outlier_method": "异常值方法",
    "data.validation.outlier_threshold": "异常值阈值",
    "data.validation.max_missing_pct": "最大缺失率 (%)",
    "evaluation.sample_start": "样本内开始",
    "evaluation.sample_end": "样本内结束",
    "evaluation.oos_start": "样本外开始",
    "evaluation.oos_end": "样本外结束",
    "evaluation.layers": "分层数",
    "evaluation.rolling_window": "滚动窗口 (日)",
    "evaluation.rolling_step": "滚动步长 (日)",
    "evaluation.cost.maker_fee_bps": "Maker 手续费 (bps)",
    "evaluation.cost.taker_fee_bps": "Taker 手续费 (bps)",
    "evaluation.cost.slippage_bps": "滑点 (bps)",
    "evaluation.ic.method": "IC 计算方法",
    "evaluation.ic.min_stocks_per_period": "每期最少标的数",
    "evaluation.ic_decay.max_horizon": "最大衰减期数",
    "evaluation.forward_bias.n_cutoffs": "截断次数",
    "evaluation.forward_bias.tolerance": "容差",
    "evaluation.forward_bias.groups": "审计粒度",
    "evaluation.cpu_workers": "计算线程数",
    "exchanges.binance.enabled": "启用 Binance",
    "exchanges.binance.proxy": "Binance 代理地址",
    "exchanges.binance.default_market_type": "默认市场类型",
    "exchanges.binance.max_klines_per_request": "单次最大 K 线数",
    "exchanges.okx.enabled": "启用 OKX",
    "live.tick_interval_seconds": "Tick 间隔 (秒)",
    "live.staleness_threshold_seconds": "数据陈旧阈值 (秒)",
    "live.reconnect.max_retries": "最大重连次数",
    "live.reconnect.base_delay_seconds": "重连基础延迟 (秒)",
    "live.reconnect.max_delay_seconds": "重连最大延迟 (秒)",
    "live.reconnect.backoff_multiplier": "重连退避倍数",
    "live.heartbeat_interval_seconds": "心跳间隔 (秒)",
    "live.heartbeat_timeout_seconds": "心跳超时 (秒)",
    "live.paper.initial_capital_usdt": "初始资金 (USDT)",
    "live.paper.maker_fee_bps": "Maker 手续费 (bps)",
    "live.paper.taker_fee_bps": "Taker 手续费 (bps)",
    "live.risk.max_leverage": "最大杠杆",
    "live.risk.max_position_notional_per_symbol": "单标的最大名义仓位",
    "live.risk.max_order_notional": "单笔最大名义",
    "live.risk.maintenance_margin_rate": "维持保证金率",
    "live.database.path": "实盘数据库路径",
    "visualization.theme": "图表主题",
    "visualization.output_dir": "报告输出目录",
}

# Enum hints so the frontend can render an NSelect instead of a free input.
_ENUMS: dict[str, list[str]] = {
    "defaults.exchange": ["binance", "synthetic", "okx", "bybit"],
    "defaults.market": ["perpetual", "spot", "coin_futures"],
    "data.timezone": ["UTC"],
    "data.output_format": ["parquet", "csv"],
    "data.validation.outlier_method": ["mad", "zscore"],
    "evaluation.ic.method": ["pearson", "spearman"],
    "evaluation.forward_bias.groups": ["representative", "all"],
    "exchanges.binance.default_market_type": ["perpetual", "spot"],
}

# Integer bounds hint for number inputs.
_BOUNDS: dict[str, tuple[int, int]] = {
    "evaluation.layers": (2, 10),
    "evaluation.ic_decay.max_horizon": (1, 100),
    "evaluation.ic.min_stocks_per_period": (2, 500),
    "evaluation.forward_bias.n_cutoffs": (1, 50),
    "evaluation.cpu_workers": (0, 64),
    "data.max_concurrent_requests": (1, 64),
    "live.tick_interval_seconds": (1, 3600),
}

# Keys stored as ISO date strings, editable via a date picker.
_DATE_KEYS: frozenset[str] = frozenset({
    "evaluation.sample_start",
    "evaluation.sample_end",
    "evaluation.oos_start",
    "evaluation.oos_end",
})

# Keys that are write-protected (hardcoded / governance-locked, never web-editable).
_READONLY_PREFIXES: tuple[str, ...] = ("project.",)
_READONLY_KEYS: frozenset[str] = frozenset({"evaluation.oos_start", "evaluation.oos_end"})

# Top-level sections managed via their own CRUD APIs, hidden from the
# generic Settings page.
_EXCLUDED_TOP_LEVEL: frozenset[str] = frozenset({"factors", "strategies", "factor_groups", "symbol_groups"})


def _humanize(key: str) -> str:
    part = key.split(".")[-1]
    return part.replace("_", " ").capitalize()


def _section(key: str) -> str:
    for prefix, label in _SECTION_PREFIXES:
        if key.startswith(prefix):
            return label
    return "其他"


def _value_type(key: str, value: Any) -> str:
    if key in _DATE_KEYS and isinstance(value, str):
        return "date"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return "str"


def _is_simple(value: Any) -> bool:
    """True when the value can be edited by a scalar/primitive control."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        # Simple lists (symbols, frequencies) are editable; lists of dicts are not.
        return all(_is_simple(item) for item in value) and not any(
            isinstance(item, dict) for item in value
        )
    return False


def _is_editable(key: str, value: Any) -> bool:
    if key.startswith(_READONLY_PREFIXES) or key in _READONLY_KEYS:
        return False
    return _is_simple(value)


def _make_field(key: str, value: Any, comments: dict[str, str]) -> dict:
    vtype = _value_type(key, value)
    field: dict[str, Any] = {
        "key": key,
        "label": _LABELS.get(key) or _humanize(key),
        "type": vtype,
        "default": value,
        "description": comments.get(key, ""),
        "section": _section(key),
        "editable": _is_editable(key, value),
    }
    if vtype in ("int", "number") and key in _BOUNDS:
        field["min"], field["max"] = _BOUNDS[key]
    if key in _ENUMS:
        field["enum"] = _ENUMS[key]
    return field


# ── YAML comment extraction (descriptions) ──────────────────────────


def _extract_comments() -> dict[str, str]:
    """Map dotted key → inline comment from the base config files."""
    comments: dict[str, str] = {}
    rt = YAML(typ="rt")
    for path in _BASE_FILES:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            node = rt.load(f)
        _walk_comments(node, "", comments)
    return comments


def _walk_comments(node: Any, prefix: str, out: dict[str, str]) -> None:
    if not isinstance(node, dict):
        return
    ca = node.ca
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        comment = ""
        if ca is not None and ca.items and key in ca.items:
            slots = ca.items[key]
            # slot 2 = end-of-line comment (inline, after the value);
            # slot 0 = comment on its own line above the key.
            token = (slots[2] if len(slots) > 2 and slots[2] else slots[0]) if slots else None
            if token and token.value:
                comment = token.value.lstrip("#").strip()
        if isinstance(value, dict):
            _walk_comments(value, dotted, out)
        elif comment:
            out[dotted] = comment


# ── Tree building ───────────────────────────────────────────────────


def _build_tree(data: dict, prefix: str, comments: dict[str, str]) -> list[dict]:
    nodes: list[dict] = []
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            children = _build_tree(value, dotted, comments)
            if not children:
                continue
            nodes.append({
                "key": dotted,
                "label": _LABELS.get(dotted) or _humanize(key),
                "type": "object",
                "description": comments.get(dotted, ""),
                "children": children,
            })
        else:
            nodes.append(_make_field(dotted, value, comments))
    return nodes


def build_schema(config: Config) -> dict:
    """Build the full config schema from the current merged configuration.

    Returns ``{"fields": [...], "sections": [...]}``. ``fields`` is a flat
    list of every editable/visible leaf (handy for lookups and value
    binding); ``sections`` is the nested tree consumed by DynamicForm.
    """
    data = config.to_dict()
    comments = _extract_comments()
    fields: list[dict] = []

    def collect(node: dict, prefix: str) -> None:
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                collect(value, dotted)
            else:
                fields.append(_make_field(dotted, value, comments))

    # Flatten every leaf (for /values + validation) — including factor/strategy
    # config which is managed elsewhere but still validatable.
    collect(data, "")

    visible = {k: v for k, v in data.items() if k not in _EXCLUDED_TOP_LEVEL}
    sections = _build_tree(visible, "", comments)
    return {"fields": fields, "sections": sections}


def flatten_values(data: dict, prefix: str = "", out: dict | None = None) -> dict:
    """Flatten a nested config dict into ``dotted.key → value``."""
    out = {} if out is None else out
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flatten_values(value, dotted, out)
        else:
            out[dotted] = value
    return out
