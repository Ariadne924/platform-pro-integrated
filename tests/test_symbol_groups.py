"""Tests for symbol groups — kind-dispatched symbol selectors.

Unit tests cover config parsing and resolution with a fresh registry (never the
singleton, so real factor discovery is untouched). Web tests exercise
``GET /api/symbols/groups`` + ``/top`` and user-group CRUD against the app with
an isolated temp config. ``fetch_tickers`` (the binance 24h-ticker call) is
stubbed in every web test so no network request ever leaves the process.
"""

import yaml
import pytest

import superplatform_web.state as _state
from superplatform.factors.symbol_groups import (
    SymbolGroup,
    UniverseContext,
    resolve_group,
    supported_kinds,
    symbol_groups,
)
from superplatform.runtime.config import Config
from superplatform_web.app import app


# ── Unit: config parsing ─────────────────────────────────────────────

def test_parse_list_group():
    cfg = Config({
        "symbol_groups": {
            "g": {"kind": "list", "description": "d", "symbols": ["BTCUSDT", "ETHUSDT"]},
        }
    })
    groups = symbol_groups(cfg)
    assert len(groups) == 1
    g = groups[0]
    assert g.name == "g"
    assert g.kind == "list"
    assert g.description == "d"
    assert g.symbols == ["BTCUSDT", "ETHUSDT"]


def test_parse_top_n_group():
    cfg = Config({
        "symbol_groups": {
            "g": {"kind": "top_n", "n": 10, "description": "d"},
        }
    })
    g = symbol_groups(cfg)[0]
    assert g.kind == "top_n"
    assert g.n == 10


def test_parse_absent_config_returns_empty():
    assert symbol_groups(Config({})) == []
    assert symbol_groups(Config({"symbol_groups": None})) == []


def test_parse_non_mapping_section_raises():
    with pytest.raises(ValueError, match="symbol_groups"):
        symbol_groups(Config({"symbol_groups": "nope"}))


def test_parse_non_mapping_entry_raises():
    with pytest.raises(ValueError, match="g1"):
        symbol_groups(Config({"symbol_groups": {"g1": "nope"}}))


def test_parse_unknown_kind_raises_with_supported():
    with pytest.raises(ValueError) as exc:
        symbol_groups(Config({"symbol_groups": {"g": {"kind": "bogus", "symbols": ["A"]}}}))
    assert "bogus" in str(exc.value)
    assert "list" in str(exc.value)
    assert "top_n" in str(exc.value)


def test_parse_missing_kind_raises():
    with pytest.raises(ValueError, match="kind"):
        symbol_groups(Config({"symbol_groups": {"g": {"symbols": ["A"]}}}))


def test_supported_kinds():
    assert supported_kinds() == ["list", "top_n"]


# ── Unit: resolution ─────────────────────────────────────────────────

def test_resolve_list_dedup_order_and_unknown():
    ctx = UniverseContext(active={"BTCUSDT", "ETHUSDT"})
    group = SymbolGroup(name="g", kind="list", symbols=["ETHUSDT", "BTCUSDT", "ETHUSDT", "GHOST"])
    res = resolve_group(group, ctx)
    assert res.symbols == ["ETHUSDT", "BTCUSDT"]  # deduped, declaration order preserved
    assert res.unknown == ["GHOST"]


def test_resolve_list_missing_symbols_raises():
    with pytest.raises(ValueError, match="symbols"):
        resolve_group(SymbolGroup(name="g", kind="list"), UniverseContext(active=set()))


def test_resolve_list_non_string_raises():
    with pytest.raises(ValueError, match="字符串"):
        resolve_group(
            SymbolGroup(name="g", kind="list", symbols=["A", 42]),
            UniverseContext(active=set()),
        )


def test_resolve_top_n_orders_by_volume_and_caps():
    ctx = UniverseContext(
        active={"A", "B", "C"},
        quote_volume={"A": 100.0, "B": 50.0, "C": 200.0},
    )
    res = resolve_group(SymbolGroup(name="g", kind="top_n", n=2), ctx)
    assert res.symbols == ["C", "A"]  # desc volume, capped at [:2]
    assert res.unknown == []


def test_resolve_top_n_excludes_unknown_volume_symbols():
    ctx = UniverseContext(active={"A", "B"}, quote_volume={"A": 100.0, "GHOST": 50.0})
    res = resolve_group(SymbolGroup(name="g", kind="top_n", n=5), ctx)
    assert res.symbols == ["A"]
    assert res.unknown == ["GHOST"]


def test_resolve_top_n_invalid_n_raises():
    for bad in (0, -3, "5", None):
        with pytest.raises(ValueError, match="n"):
            resolve_group(
                SymbolGroup(name="g", kind="top_n", n=bad),
                UniverseContext(active=set(), quote_volume={"A": 1.0}),
            )


def test_resolve_top_n_empty_volume_is_empty():
    res = resolve_group(
        SymbolGroup(name="g", kind="top_n", n=3),
        UniverseContext(active={"A", "B"}),
    )
    assert res.symbols == []
    assert res.unknown == []


# ── Web: isolated app state ──────────────────────────────────────────

@pytest.fixture
def _isolated_state(tmp_path, monkeypatch):
    """Point config + experiments DB at temp files so tests never touch real ones."""
    monkeypatch.setattr(_state, "_CONFIG_FILES", (
        str(tmp_path / "default.yaml"),
        str(tmp_path / "exchanges.yaml"),
        str(tmp_path / "factors.yaml"),
        str(tmp_path / "user_groups.yaml"),
        str(tmp_path / "user_symbol_groups.yaml"),
        str(tmp_path / "settings.yaml"),
    ))
    monkeypatch.setattr("superplatform_web.app._EXPERIMENTS_PATH", tmp_path / "experiments.duckdb")

    (tmp_path / "default.yaml").write_text(yaml.safe_dump({
        "defaults": {"exchange": "synthetic", "market": "perpetual"},
        "data": {"symbols": {"perpetual": ["BTCUSDT"]}},
    }), encoding="utf-8")
    (tmp_path / "exchanges.yaml").write_text("", encoding="utf-8")
    (tmp_path / "factors.yaml").write_text("", encoding="utf-8")

    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()
    _state.live_runtime = None
    import superplatform_web.routes.live as _live_routes
    _live_routes.live_runtime = None  # live_start rebinds this module global
    _state.reload_config()
    yield
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()
    _state.live_runtime = None
    _live_routes.live_runtime = None


def _with_symbol_groups(tmp_path, groups: dict) -> None:
    """Write ``symbol_groups:`` into default.yaml (preconfigured-group source)."""
    (tmp_path / "default.yaml").write_text(yaml.safe_dump({
        "defaults": {"exchange": "synthetic", "market": "perpetual"},
        "data": {"symbols": {"perpetual": ["BTCUSDT"]}},
        "symbol_groups": groups,
    }), encoding="utf-8")


def _stub_tickers(monkeypatch, tickers: dict, *, module: str = "superplatform_web.routes.symbols"):
    """Stand in for the cached 24h-ticker fetch (no network)."""
    async def fake():
        return tickers

    monkeypatch.setattr(f"{module}.fetch_tickers", fake)


# ── Web: GET /groups ─────────────────────────────────────────────────

def test_groups_endpoint_resolves(tmp_path, _isolated_state, monkeypatch):
    """List + top_n groups resolve against stubbed tickers with counts."""
    _with_symbol_groups(tmp_path, {
        "core_two": {"kind": "list", "description": "核心双标的",
                     "symbols": ["BTCUSDT", "ETHUSDT", "GHOST"]},
        "top10": {"kind": "top_n", "description": "Top", "n": 2},
    })
    _stub_tickers(monkeypatch, {"BTCUSDT": 100.0, "ETHUSDT": 50.0, "SOLUSDT": 200.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        body = c.get("/api/symbols/groups")
        assert body.status_code == 200, body.text
        by_name = {g["name"]: g for g in body.json()}
        assert set(by_name) == {"core_two", "top10"}

        core = by_name["core_two"]
        assert core["kind"] == "list"
        assert core["symbols"] == ["BTCUSDT", "ETHUSDT"]
        assert core["unknown"] == ["GHOST"]
        assert core["count"] == 2
        assert core["available_count"] == 2  # symbols resolve vs active; no 2nd filter
        assert core["deletable"] is False  # preconfigured

        top = by_name["top10"]
        assert top["kind"] == "top_n"
        assert top["symbols"] == ["SOLUSDT", "BTCUSDT"]  # desc volume, capped at 2
        assert top["count"] == 2
        assert top["unknown"] == []


def test_groups_endpoint_unknown_kind_422(tmp_path, _isolated_state, monkeypatch):
    _with_symbol_groups(tmp_path, {"g": {"kind": "bogus", "symbols": ["BTCUSDT"]}})
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/api/symbols/groups")
        assert r.status_code == 422
        assert "bogus" in r.json()["detail"]


def test_groups_endpoint_absent_ok(tmp_path, _isolated_state, monkeypatch):
    """No `symbol_groups:` section → empty list, not an error."""
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/api/symbols/groups")
        assert r.status_code == 200, r.text
        assert r.json() == []


# ── Web: GET /top (dynamic Top-N) ────────────────────────────────────

def test_top_endpoint_returns_top_n(tmp_path, _isolated_state, monkeypatch):
    _stub_tickers(monkeypatch, {"A": 1.0, "B": 3.0, "C": 2.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/api/symbols/top?n=2")
        assert r.status_code == 200, r.text
        assert r.json() == {"n": 2, "symbols": ["B", "C"], "count": 2}


def test_top_endpoint_empty_tickers_is_empty(tmp_path, _isolated_state, monkeypatch):
    _stub_tickers(monkeypatch, {})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/api/symbols/top?n=5")
        assert r.status_code == 200, r.text
        assert r.json() == {"n": 5, "symbols": [], "count": 0}


# ── Web: user-group persistence (POST / DELETE) ──────────────────────

def test_groups_post_saves_dedups_and_survives_reload(tmp_path, _isolated_state, monkeypatch):
    """POST persists to user_symbol_groups.yaml (deletable, deduped) and survives reload."""
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0, "ETHUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/symbols/groups", json={
            "name": " my_set ",  # surrounding whitespace is stripped
            "symbols": ["BTCUSDT", "ETHUSDT", "BTCUSDT"],
        })
        assert r.status_code == 200, r.text
        by_name = {g["name"]: g for g in r.json()}
        assert "my_set" in by_name
        g = by_name["my_set"]
        assert g["kind"] == "list"
        assert g["deletable"] is True
        assert g["symbols"] == ["BTCUSDT", "ETHUSDT"]  # deduped, order kept
        assert g["count"] == 2

    # Persisted on disk under `symbol_groups:`.
    written = yaml.safe_load((tmp_path / "user_symbol_groups.yaml").read_text(encoding="utf-8"))
    assert "my_set" in written["symbol_groups"]
    assert written["symbol_groups"]["my_set"]["kind"] == "list"
    assert written["symbol_groups"]["my_set"]["symbols"] == ["BTCUSDT", "ETHUSDT"]

    # A config reload (restart-equivalent) still sees the user group.
    _state.reload_config()
    with TestClient(app) as c:
        r = c.get("/api/symbols/groups")
        names = {g["name"] for g in r.json()}
        assert "my_set" in names


def test_groups_post_unknown_symbol_422(tmp_path, _isolated_state, monkeypatch):
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/symbols/groups", json={"name": "g", "symbols": ["BTCUSDT", "DELISTED"]})
        assert r.status_code == 422
        assert "DELISTED" in r.json()["detail"]
    assert not (tmp_path / "user_symbol_groups.yaml").exists()  # nothing written


def test_groups_post_empty_name_422(tmp_path, _isolated_state, monkeypatch):
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/symbols/groups", json={"name": "   ", "symbols": ["BTCUSDT"]})
        assert r.status_code == 422
        assert "分组名" in r.json()["detail"]


def test_groups_post_conflicts_with_preconfigured_409(tmp_path, _isolated_state, monkeypatch):
    """A name matching a preconfigured group is rejected — no silent overwrite."""
    _with_symbol_groups(tmp_path, {
        "core_two": {"kind": "list", "symbols": ["BTCUSDT", "ETHUSDT"]},
    })
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0, "ETHUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        # Preconfigured groups are not deletable.
        r = c.get("/api/symbols/groups")
        assert next(g for g in r.json() if g["name"] == "core_two")["deletable"] is False

        r = c.post("/api/symbols/groups", json={"name": "core_two", "symbols": ["BTCUSDT"]})
        assert r.status_code == 409
        assert "预配置" in r.json()["detail"]
    assert not (tmp_path / "user_symbol_groups.yaml").exists()


def test_groups_post_overwrites_existing_user_group(tmp_path, _isolated_state, monkeypatch):
    """Re-saving an existing user group replaces its symbols."""
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0, "ETHUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/symbols/groups", json={"name": "g", "symbols": ["BTCUSDT"]})
        assert r.status_code == 200, r.text
        r = c.post("/api/symbols/groups", json={"name": "g", "symbols": ["ETHUSDT"]})
        assert r.status_code == 200, r.text
        by_name = {g["name"]: g for g in r.json()}
        assert by_name["g"]["symbols"] == ["ETHUSDT"]
        assert by_name["g"]["count"] == 1


def test_groups_delete_user_group(tmp_path, _isolated_state, monkeypatch):
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        c.post("/api/symbols/groups", json={"name": "g", "symbols": ["BTCUSDT"]})
        r = c.delete("/api/symbols/groups/g")
        assert r.status_code == 200, r.text
        assert "g" not in {g["name"] for g in r.json()}
    # File no longer references the group (and may be gone entirely).
    user_file = tmp_path / "user_symbol_groups.yaml"
    if user_file.exists():
        written = yaml.safe_load(user_file.read_text(encoding="utf-8")) or {}
        assert "g" not in (written.get("symbol_groups") or {})


def test_groups_delete_preconfigured_404(tmp_path, _isolated_state, monkeypatch):
    """Preconfigured groups (declared in default.yaml) are never deletable."""
    _with_symbol_groups(tmp_path, {
        "core_two": {"kind": "list", "symbols": ["BTCUSDT", "ETHUSDT"]},
    })
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0, "ETHUSDT": 1.0})
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.delete("/api/symbols/groups/core_two")
        assert r.status_code == 404
        assert "不是用户保存的分组" in r.json()["detail"]


def test_groups_write_blocked_while_session_running(tmp_path, _isolated_state, monkeypatch):
    """Symbol-group writes are rejected while a live session is running."""
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0})
    _state.live_runtime = object()  # pretend a session is live
    try:
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            r = c.post("/api/symbols/groups", json={"name": "g", "symbols": ["BTCUSDT"]})
            assert r.status_code == 409
            assert "运行中" in r.json()["detail"]
            r = c.delete("/api/symbols/groups/g")
            assert r.status_code == 409
    finally:
        _state.live_runtime = None


# ── Live start: per-session symbol threading ─────────────────────────

class _StubBroker:
    name = "stub-broker"


class _StubLiveRuntime:
    """Records the kwargs live_start passes; `start` completes immediately."""

    def __init__(self, config, providers, broker, consumer=None, symbols=None):
        self.config = config
        self.providers = providers
        self.broker = broker
        self.consumer = consumer
        self.symbols = symbols

    def setup(self, strategy_name=None):
        self.strategy_name = strategy_name

    async def start(self):
        pass


def _stub_live_runtime(monkeypatch):
    """Stub build_broker + LiveRuntime in the live route so no broker is built."""
    captured: dict = {}

    def fake_build_broker(config, adapter=None, symbols=None):
        captured["broker_symbols"] = symbols
        return _StubBroker()

    def fake_runtime(config, providers, broker, consumer=None, symbols=None):
        captured["runtime_symbols"] = symbols
        return _StubLiveRuntime(config, providers, broker, consumer, symbols)

    monkeypatch.setattr("superplatform_web.routes.live.build_broker", fake_build_broker)
    monkeypatch.setattr("superplatform_web.routes.live.LiveRuntime", fake_runtime)
    return captured


def test_live_start_passes_symbols_to_broker_and_runtime(tmp_path, _isolated_state, monkeypatch):
    """The session selection reaches both build_broker and LiveRuntime."""
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0, "ETHUSDT": 1.0},
                  module="superplatform_web.routes.live")
    captured = _stub_live_runtime(monkeypatch)
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/live/start", json={"strategy": "momentum_demo",
                                            "symbols": ["BTCUSDT", "ETHUSDT"]})
        assert r.status_code == 200, r.text
        assert r.json()["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert captured["broker_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert captured["runtime_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_live_start_rejects_unknown_symbol(tmp_path, _isolated_state, monkeypatch):
    _stub_tickers(monkeypatch, {"BTCUSDT": 1.0}, module="superplatform_web.routes.live")
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/live/start", json={"strategy": "momentum_demo",
                                            "symbols": ["BTCUSDT", "DELISTED"]})
        assert r.status_code == 422
        assert "DELISTED" in r.json()["detail"]


def test_live_start_offline_allows_any_symbol(tmp_path, _isolated_state, monkeypatch):
    """No tickers + no stored universe → unknown-symbol validation is skipped."""
    _stub_tickers(monkeypatch, {}, module="superplatform_web.routes.live")
    captured = _stub_live_runtime(monkeypatch)
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/live/start", json={"strategy": "momentum_demo",
                                            "symbols": ["WHATEVER"]})
        assert r.status_code == 200, r.text
    assert captured["broker_symbols"] == ["WHATEVER"]


# ── Broker factory: per-session override ─────────────────────────────

def test_factory_testnet_accepts_session_symbols_override(monkeypatch):
    """symbols=... supplies the subscription list even when live.symbols is absent."""
    from superplatform.network.brokers import BinanceBroker, build_broker
    from superplatform.runtime.config import Config

    cfg = Config({"live": {
        "broker": "binance-testnet",
        "binance_testnet": {"api_key_env": "TN_KEY", "api_secret_env": "TN_SECRET"},
    }})
    monkeypatch.setenv("TN_KEY", "k")
    monkeypatch.setenv("TN_SECRET", "s")
    b = build_broker(cfg, symbols=["BTCUSDT"])
    assert isinstance(b, BinanceBroker)
    assert b._symbols == {"BTCUSDT"}
