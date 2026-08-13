"""Tests for factor groups — kind-dispatched factor selectors.

Unit tests cover config parsing and resolution with a fresh registry (never the
singleton, so real factor discovery is untouched). Web tests exercise
``GET /api/factors/groups`` against the app with an isolated temp config.
"""

import pytest
import yaml

import superplatform_web.state as _state
from superplatform.factors.base import Factor, FactorCategory
from superplatform.factors.factor_groups import (
    FactorGroup,
    factor_groups,
    resolve_group,
    supported_kinds,
)
from superplatform.factors.registry import FactorRegistry
from superplatform.runtime.config import Config
from superplatform_web.app import app

# ── Unit helpers ─────────────────────────────────────────────────────

def _make_stub(name: str, category: FactorCategory):
    def compute(self, data, **params):
        return None  # never invoked by these tests

    return type(
        f"_Stub_{name}",
        (Factor,),
        {
            "name": name,
            "category": category,
            "required_data": ["kline"],
            "description": "stub",
            "compute": compute,
        },
    )


def _fresh_registry(*stubs) -> FactorRegistry:
    reg = FactorRegistry()
    for stub in stubs:
        reg.register(stub())
    return reg


# ── Config parsing ───────────────────────────────────────────────────

def test_parse_list_group():
    cfg = Config({
        "factor_groups": {
            "g": {"kind": "list", "description": "d", "factors": ["a", "b"]},
        }
    })
    groups = factor_groups(cfg)
    assert len(groups) == 1
    g = groups[0]
    assert g.name == "g"
    assert g.kind == "list"
    assert g.description == "d"
    assert g.factors == ["a", "b"]


def test_parse_absent_config_returns_empty():
    assert factor_groups(Config({})) == []
    assert factor_groups(Config({"factor_groups": None})) == []


def test_parse_non_mapping_section_raises():
    with pytest.raises(ValueError, match="factor_groups"):
        factor_groups(Config({"factor_groups": "nope"}))


def test_parse_non_mapping_entry_raises():
    with pytest.raises(ValueError, match="g1"):
        factor_groups(Config({"factor_groups": {"g1": "nope"}}))


def test_parse_unknown_kind_raises_with_supported():
    with pytest.raises(ValueError) as exc:
        factor_groups(Config({"factor_groups": {"g": {"kind": "bogus", "factors": ["a"]}}}))
    assert "bogus" in str(exc.value)
    assert "list" in str(exc.value)
    assert "category" in str(exc.value)


def test_parse_missing_kind_raises():
    with pytest.raises(ValueError, match="kind"):
        factor_groups(Config({"factor_groups": {"g": {"factors": ["a"]}}}))


def test_supported_kinds():
    assert supported_kinds() == ["category", "list"]


# ── Resolution ───────────────────────────────────────────────────────

def test_resolve_list_dedup_order_and_unknown():
    reg = _fresh_registry(
        _make_stub("a", FactorCategory.MOMENTUM_REVERSAL),
        _make_stub("b", FactorCategory.VOLATILITY),
    )
    group = FactorGroup(name="g", kind="list", factors=["b", "a", "b", "ghost"])
    res = resolve_group(group, reg)
    assert res.factors == ["b", "a"]  # deduped, declaration order preserved
    assert res.unknown == ["ghost"]


def test_resolve_list_missing_factors_raises():
    reg = _fresh_registry()
    with pytest.raises(ValueError, match="factors"):
        resolve_group(FactorGroup(name="g", kind="list"), reg)


def test_resolve_list_non_string_raises():
    reg = _fresh_registry()
    with pytest.raises(ValueError, match="字符串"):
        resolve_group(FactorGroup(name="g", kind="list", factors=["a", 42]), reg)


def test_resolve_category():
    reg = _fresh_registry(
        _make_stub("a", FactorCategory.MOMENTUM_REVERSAL),
        _make_stub("b", FactorCategory.VOLATILITY),
        _make_stub("c", FactorCategory.MOMENTUM_REVERSAL),
    )
    res = resolve_group(FactorGroup(name="g", kind="category", category="momentum_reversal"), reg)
    assert sorted(res.factors) == ["a", "c"]
    assert res.unknown == []


def test_resolve_category_invalid_raises():
    reg = _fresh_registry(_make_stub("a", FactorCategory.MOMENTUM_REVERSAL))
    with pytest.raises(ValueError, match="category"):
        resolve_group(FactorGroup(name="g", kind="category", category="nope"), reg)


def test_resolve_category_empty_is_legal():
    reg = _fresh_registry(_make_stub("a", FactorCategory.MOMENTUM_REVERSAL))
    res = resolve_group(FactorGroup(name="g", kind="category", category="volatility"), reg)
    assert res.factors == []
    assert res.unknown == []


# ── Web endpoint ─────────────────────────────────────────────────────

@pytest.fixture
def _isolated_state(tmp_path, monkeypatch):
    """Point config + experiments DB at temp files so tests never touch real ones."""
    monkeypatch.setattr(_state, "_CONFIG_FILES", (
        str(tmp_path / "default.yaml"),
        str(tmp_path / "exchanges.yaml"),
        str(tmp_path / "factors.yaml"),
        str(tmp_path / "user_groups.yaml"),
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
    _state.reload_config()
    yield
    if _state.store is not None:
        _state.store.close()
        _state.store = None
    _state.providers.clear()


def test_groups_endpoint_resolves(tmp_path, _isolated_state):
    """A `kind: list` group resolves to real registered factors with counts."""
    (tmp_path / "factors.yaml").write_text(yaml.safe_dump({
        "factor_groups": {
            "momentum_study": {
                "kind": "list",
                "factors": ["momentum", "short_term_reversal", "rsi", "ghost"],
            },
        }
    }), encoding="utf-8")
    FactorRegistry.get_instance().auto_discover()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        body = c.get("/api/factors/groups")
        assert body.status_code == 200, body.text
        groups = body.json()
        assert len(groups) == 1
        g = groups[0]
        assert g["name"] == "momentum_study"
        assert g["kind"] == "list"
        assert g["count"] == 3
        assert set(g["factors"]) == {"momentum", "short_term_reversal", "rsi"}
        assert g["unknown"] == ["ghost"]
        assert g["available_count"] >= 1
        assert isinstance(g["unavailable"], list)


def test_groups_endpoint_unknown_kind_422(tmp_path, _isolated_state):
    (tmp_path / "factors.yaml").write_text(yaml.safe_dump({
        "factor_groups": {"g": {"kind": "bogus", "factors": ["momentum"]}},
    }), encoding="utf-8")
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/api/factors/groups")
        assert r.status_code == 422
        assert "bogus" in r.json()["detail"]


def test_groups_endpoint_absent_ok(tmp_path, _isolated_state):
    """No `factor_groups:` section → empty list, not an error."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/api/factors/groups")
        assert r.status_code == 200, r.text
        assert r.json() == []


# ── User group persistence (POST / DELETE /groups) ───────────────────

def test_groups_post_saves_dedups_and_survives_reload(tmp_path, _isolated_state):
    """POST persists to user_groups.yaml (deletable, deduped) and survives reload."""
    FactorRegistry.get_instance().auto_discover()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/factors/groups", json={
            "name": " my_set ",  # surrounding whitespace is stripped
            "factors": ["momentum", "rsi", "momentum"],
        })
        assert r.status_code == 200, r.text
        by_name = {g["name"]: g for g in r.json()}
        assert "my_set" in by_name
        g = by_name["my_set"]
        assert g["kind"] == "list"
        assert g["deletable"] is True
        assert g["factors"] == ["momentum", "rsi"]  # deduped, order kept
        assert g["count"] == 2

    # Persisted on disk under `factor_groups:`.
    written = yaml.safe_load((tmp_path / "user_groups.yaml").read_text(encoding="utf-8"))
    assert "my_set" in written["factor_groups"]
    assert written["factor_groups"]["my_set"]["kind"] == "list"
    assert written["factor_groups"]["my_set"]["factors"] == ["momentum", "rsi"]

    # A config reload (restart-equivalent) still sees the user group.
    _state.reload_config()
    with TestClient(app) as c:
        r = c.get("/api/factors/groups")
        names = {g["name"] for g in r.json()}
        assert "my_set" in names


def test_groups_post_unknown_factor_422(tmp_path, _isolated_state):
    FactorRegistry.get_instance().auto_discover()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/factors/groups", json={"name": "g", "factors": ["momentum", "not_a_factor"]})
        assert r.status_code == 422
        assert "not_a_factor" in r.json()["detail"]
    assert not (tmp_path / "user_groups.yaml").exists()  # nothing written


def test_groups_post_empty_name_422(tmp_path, _isolated_state):
    FactorRegistry.get_instance().auto_discover()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/factors/groups", json={"name": "   ", "factors": ["momentum"]})
        assert r.status_code == 422
        assert "分组名" in r.json()["detail"]


def test_groups_post_conflicts_with_preconfigured_409(tmp_path, _isolated_state):
    """A name matching a preconfigured group is rejected — no silent overwrite."""
    (tmp_path / "factors.yaml").write_text(yaml.safe_dump({
        "factor_groups": {
            "momentum_study": {"kind": "list", "factors": ["momentum", "rsi"]},
        },
    }), encoding="utf-8")
    FactorRegistry.get_instance().auto_discover()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        # Preconfigured groups are not deletable.
        r = c.get("/api/factors/groups")
        assert next(g for g in r.json() if g["name"] == "momentum_study")["deletable"] is False

        r = c.post("/api/factors/groups", json={"name": "momentum_study", "factors": ["momentum"]})
        assert r.status_code == 409
        assert "预配置" in r.json()["detail"]
    assert not (tmp_path / "user_groups.yaml").exists()


def test_groups_post_overwrites_existing_user_group(tmp_path, _isolated_state):
    """Re-saving an existing user group replaces its factors."""
    FactorRegistry.get_instance().auto_discover()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/api/factors/groups", json={"name": "g", "factors": ["momentum"]})
        assert r.status_code == 200, r.text
        r = c.post("/api/factors/groups", json={"name": "g", "factors": ["rsi"]})
        assert r.status_code == 200, r.text
        by_name = {g["name"]: g for g in r.json()}
        assert by_name["g"]["factors"] == ["rsi"]
        assert by_name["g"]["count"] == 1


def test_groups_delete_user_group(tmp_path, _isolated_state):
    FactorRegistry.get_instance().auto_discover()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        c.post("/api/factors/groups", json={"name": "g", "factors": ["momentum"]})
        r = c.delete("/api/factors/groups/g")
        assert r.status_code == 200, r.text
        assert "g" not in {g["name"] for g in r.json()}
    # File no longer references the group (and may be gone entirely).
    user_file = tmp_path / "user_groups.yaml"
    if user_file.exists():
        written = yaml.safe_load(user_file.read_text(encoding="utf-8")) or {}
        assert "g" not in (written.get("factor_groups") or {})


def test_groups_delete_preconfigured_404(tmp_path, _isolated_state):
    """Preconfigured groups (declared in factors.yaml) are never deletable."""
    (tmp_path / "factors.yaml").write_text(yaml.safe_dump({
        "factor_groups": {
            "momentum_study": {"kind": "list", "factors": ["momentum", "rsi"]},
        },
    }), encoding="utf-8")
    FactorRegistry.get_instance().auto_discover()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.delete("/api/factors/groups/momentum_study")
        assert r.status_code == 404
        assert "不是用户保存的分组" in r.json()["detail"]
