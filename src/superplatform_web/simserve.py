"""sim_platform API 形状 → superplatform 核心服务 的共享接线层（05）。

死规矩落实：本模块只做「调用 01/02/03/04 的服务/CLI + 形状映射」，
不重算任何评估指标（IC/评级/六查一律来自 04 的 CLI 或核心运行时）。

- 符号映射：sim UI 用 ``BTC/USDT``，superplatform 数据层用 ``BTCUSDT``；
- 取数：经 ``superplatform_web.state.store``（01 的 DuckDB 缓存，同进程复用，
  遵守 DuckDB 单进程写锁——web 进程内共享同一 Store，不开第二进程）；
- 因子/策略清单：02 的双文件注册中心（ensure_scanned 增量热插拔）；
- 评级/指标/六查：04 的服务层（RatingService/FactorMetricsService/
  BiasControlService，PROGRESS_04.md 留给 05 的接口），进程内调用并串行化；
  04 未就位时抛 ``Service04Unavailable``，路由如实返回 503，不给假数据。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import superplatform_web.state as _state

log = logging.getLogger("superplatform_web.simserve")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ── 符号映射 ────────────────────────────────────────────────────────


def ui_symbol(core: str) -> str:
    """数据层符号 → UI 符号：BTCUSDT → BTC/USDT。"""
    if "/" in core:
        return core
    for quote in ("USDT", "USDC", "USD"):
        if core.endswith(quote):
            return f"{core[: -len(quote)]}/{quote}"
    return core


def core_symbol(ui: str) -> str:
    """UI 符号 → 数据层符号：BTC/USDT → BTCUSDT。"""
    return ui.replace("/", "").strip().upper()


# ── Store / provider 表解析 ─────────────────────────────────────────


def get_store():
    """01 的 DuckDB 缓存 Store；未启用（tests 等场景）返回 None。"""
    return _state.store


def provider_table_for(data_type: str) -> str:
    """按 config 默认 exchange/market 解析 provider 缓存表名（pv_*）。"""
    from superplatform.data.store import provider_table

    provider_id = _state.resolve_provider_for_data_type(
        _state.default_exchange(), _state.default_market(), data_type
    )
    return provider_table(provider_id)


def kline_table() -> str:
    return provider_table_for("kline")


def funding_table() -> str:
    return provider_table_for("funding_rate")


def oi_table() -> str:
    return provider_table_for("open_interest")


def perp_symbols() -> list[str]:
    """config 研究池标的（数据层格式 BTCUSDT）。"""
    raw = _state.config.get("data.symbols.perpetual") or []
    return [str(s) for s in raw]


def cached_kline_symbols(frequency: str = "1m") -> list[str]:
    """研究池里该频率在缓存中确有数据的标的（真实覆盖，不凭空列）。"""
    store = get_store()
    if store is None:
        return []
    out = []
    for sym in perp_symbols():
        try:
            info = store.series_range(kline_table(), sym, frequency)
        except Exception:
            continue
        if info.get("count", 0) > 0:
            out.append(sym)
    return out


# ── 02 双文件注册中心 ───────────────────────────────────────────────


def factor_registry():
    """02 的双文件因子注册中心（每次调用做 mtime 增量扫描=热插拔入口）。"""
    from superplatform.factors.dual_registry import DualFactorRegistry

    reg = DualFactorRegistry.get_instance()
    reg.ensure_scanned()
    return reg


def strategy_registry():
    from superplatform.strategy.dual_registry import DualStrategyRegistry

    reg = DualStrategyRegistry.get_instance()
    reg.ensure_scanned()
    return reg


def compute_factor_series(factor_id: str, symbol_core: str, limit: int) -> list[dict]:
    """调 02 注册的因子 compute 算指定标的的最近时序（真实计算，非指标重算）。

    数据取因子 MD 声明频率的缓存 K 线；无缓存返回空列表（如实）。
    """
    reg = factor_registry()
    rec = reg.get_record(factor_id)
    if rec is None:
        return []
    store = get_store()
    if store is None:
        return []
    frequency = rec.frequency or "1d"
    df = store.query_series(
        kline_table(), symbol_core, frequency, limit=max(limit * 4, 2000),
        order="DESC",
    )
    if df.empty:
        return []
    df = df.sort_values("timestamp").reset_index(drop=True)
    from superplatform.factors.registry import FactorRegistry

    factor = FactorRegistry.get_instance().get(factor_id)
    result = factor.compute({"kline": {ui_symbol(symbol_core): df}})
    values = getattr(result, "values", result)
    if values is None or len(values) == 0:
        return []
    tail = values.tail(limit)
    rows = []
    for row in tail.itertuples(index=False):
        ts = getattr(row, "timestamp")
        val = getattr(row, "value")
        if val is None or val != val:  # NaN
            continue
        rows.append({"ts": _ts_iso(ts), "value": float(val)})
    return rows


# ── 时间工具 ────────────────────────────────────────────────────────


def _ts_iso(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(float(val), tz=timezone.utc).isoformat()
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc).isoformat()
        return val.astimezone(timezone.utc).isoformat()
    # pandas Timestamp 等
    try:
        import pandas as pd

        ts = pd.Timestamp(val)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.isoformat()
    except Exception:
        return str(val)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 04 服务适配层（rating / metrics / bias-check）────────────────────
#
# 04 已交付 Python 服务层（见 PROGRESS_04.md「留给 05 的服务层接口」）：
#   RatingService.rate_factor(factor_id, days, horizon, symbols, refresh)
#   RatingService.leaderboard(ids, days, horizon, refresh, compute_limit)
#   FactorMetricsService.factor_metrics(factor_id, force) / qualification_summary /
#     correlation_matrix / metrics_csv / qualification_csv / correlation_csv
#   BiasControlService.run(scope, factor_ids, run_id, on_progress) / report_data /
#     report / eval_store.latest_run_id()
# 三个服务非线程安全（DuckDB 单写者）：全部调用经 _S04_LOCK 串行化；
# 复用 web 进程的 _state.store（同进程多连接是 DuckDB 支持用法），
# 不开子进程（会撞 01 缓存的单进程写锁）。


class Service04Unavailable(RuntimeError):
    """04 的评级/指标/偏差服务未就位或未初始化。"""


_S04_LOCK = threading.Lock()
_services04: Optional[tuple] = None


def services04() -> tuple:
    """(RatingService, FactorMetricsService, BiasControlService) 惰性单例。

    复用 web 进程已初始化的 config/providers/store；store 未启用（如 tests）
    或服务构造失败时抛 Service04Unavailable——路由如实映射 503。
    """
    global _services04
    if _services04 is not None:
        return _services04
    with _S04_LOCK:
        if _services04 is not None:
            return _services04
        if _state.store is None:
            raise Service04Unavailable("数据缓存未启用（data.cache.enabled=false）")
        try:
            from superplatform.evaluation.bias import BiasControlService
            from superplatform.evaluation.factor_metrics import FactorMetricsService
            from superplatform.evaluation.rating import RatingService
        except ImportError as exc:
            raise Service04Unavailable(f"04 的评估服务模块未就位: {exc}") from exc
        cache = _state.config.get("data.cache.path", "data/cache.duckdb")
        try:
            rating = RatingService(
                _state.config, _state.providers, cache_path=cache, store=_state.store
            )
            metrics = FactorMetricsService(
                _state.config, _state.providers, cache_path=cache, store=_state.store
            )
            bias = BiasControlService(
                _state.config, _state.providers, cache_path=cache, store=_state.store
            )
        except Exception as exc:
            raise Service04Unavailable(
                f"04 评估服务初始化失败: {type(exc).__name__}: {exc}"
            ) from exc
        _services04 = (rating, metrics, bias)
        return _services04


def call04(fn, *args, **kwargs):
    """串行调用 04 服务（DuckDB 单写者，服务非线程安全）。"""
    with _S04_LOCK:
        return fn(*args, **kwargs)


# ── 因子检查状态登记（attest/uncheck/override 的簿记，非指标）────────
#
# 落 data/（运行时产物，gitignored）JSON 文件；只记状态簿记与人工解封，
# 六查结果来自 04 bias-check 的运行记录（见 _RunManager）。


class _CheckStateStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"statuses": {}, "overrides": {}}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("检查状态文件损坏，重置为空: %s", self._path)
            self._data = {"statuses": {}, "overrides": {}}
        self._data.setdefault("statuses", {})
        self._data.setdefault("overrides", {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path)

    @staticmethod
    def _files_md5(rec) -> Optional[str]:
        """因子 MD+impl 内容哈希（hash_changed 判定用）；文件缺失返回 None。"""
        h = hashlib.md5()
        try:
            for p in (rec.md_path, rec.impl_path):
                if p is None:
                    continue
                h.update(Path(p).read_bytes())
            return h.hexdigest()
        except OSError:
            return None

    def get_status(self, factor_id: str, rec=None) -> dict[str, Any]:
        with self._lock:
            row = dict(self._data["statuses"].get(factor_id) or {})
        row.setdefault("status", "unchecked")
        row.setdefault("source", None)
        row.setdefault("attested_by", None)
        row.setdefault("attested_at", None)
        row.setdefault("updated_at", None)
        row.setdefault("metrics_compute", None)
        row["registered"] = rec is not None
        if rec is not None:
            current = self._files_md5(rec)
            row["hash_changed"] = bool(
                row.get("status") == "checked"
                and row.get("file_md5")
                and current
                and row["file_md5"] != current
            )
        else:
            row["hash_changed"] = False
        return row

    def list_statuses(self, records: dict[str, Any]) -> dict[str, Any]:
        return {fid: self.get_status(fid, rec) for fid, rec in records.items()}

    def attest(self, factor_id: str, name: str, rec) -> dict[str, Any]:
        with self._lock:
            row = self._data["statuses"].setdefault(factor_id, {})
            row.update({
                "status": "checked",
                "source": "manual",
                "attested_by": name,
                "attested_at": utcnow_iso(),
                "updated_at": utcnow_iso(),
                "file_md5": self._files_md5(rec),
            })
            self._save()
        return self.get_status(factor_id, rec)

    def uncheck(self, factor_id: str, rec) -> dict[str, Any]:
        with self._lock:
            row = self._data["statuses"].setdefault(factor_id, {})
            row.update({
                "status": "unchecked",
                "source": None,
                "attested_by": None,
                "attested_at": None,
                "updated_at": utcnow_iso(),
                "file_md5": self._files_md5(rec),
            })
            self._save()
        return self.get_status(factor_id, rec)

    def mark_auto_checked(self, factor_id: str) -> None:
        with self._lock:
            rec = factor_registry().get_record(factor_id)
            row = self._data["statuses"].setdefault(factor_id, {})
            row.update({
                "status": "checked",
                "source": "auto",
                "updated_at": utcnow_iso(),
                "file_md5": self._files_md5(rec) if rec else None,
            })
            self._save()

    def set_checking(self, factor_id: str, on: bool) -> None:
        with self._lock:
            row = self._data["statuses"].setdefault(factor_id, {})
            if on:
                row["status"] = "checking"
                row["updated_at"] = utcnow_iso()
            elif row.get("status") == "checking":
                row["status"] = "unchecked"
            self._save()

    # ── 人工解封 ──
    def list_overrides(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data["overrides"]))

    def set_override(self, factor_id: str, check_name: str, operator: str,
                     reason: Optional[str], original_status: str) -> dict[str, Any]:
        with self._lock:
            entry = self._data["overrides"].setdefault(factor_id, {})
            entry[check_name] = {
                "operator": operator,
                "reason": reason,
                "original_status": original_status,
                "created_at": utcnow_iso(),
            }
            self._save()
            return dict(entry[check_name])

    def revoke_override(self, factor_id: str, check_name: str) -> bool:
        with self._lock:
            entry = self._data["overrides"].get(factor_id) or {}
            existed = check_name in entry
            entry.pop(check_name, None)
            if not entry:
                self._data["overrides"].pop(factor_id, None)
            self._save()
            return existed


_check_state = _CheckStateStore(PROJECT_ROOT / "data" / "web_factor_check.json")


def check_state() -> _CheckStateStore:
    return _check_state


# ── 偏差检查批次（run）管理：后台线程串行调 04 bias-check CLI ─────────
#
# DuckDB 单进程写锁 → 批次串行执行（同进程多连接合法但仍串行，避免
# 与 live/回测争抢）。run 记录只在内存；完成后把六查结果回写
# data/web_bias_results.json 供 overview/detail 读取（真实结果留档）。


class BiasRun:
    def __init__(self, scope: str, factor_ids: list[str]):
        self.run_id = uuid.uuid4().hex[:12]
        self.scope = scope
        self.factor_ids = list(factor_ids)
        self.status = "RUNNING"  # RUNNING/PASS/FAIL/ERROR/CANCELLED
        self.started_at = utcnow_iso()
        self.finished_at: Optional[str] = None
        self.progress = {
            "completed": 0,
            "total": len(factor_ids),
            "passed": 0,
            "failed": 0,
            "not_checked": 0,
            "current_factor": None,
            "latest_error": None,
        }
        self.results: dict[str, Any] = {}
        self.cancel_requested = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scope": self.scope,
            "factor_ids": self.factor_ids,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": self.progress["total"],
            "progress": dict(self.progress),
        }


class _RunManager:
    def __init__(self, results_path: Path):
        self._runs: dict[str, BiasRun] = {}
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()  # 同时只允许一个批次执行
        self._results_path = results_path
        self._stored_results: dict[str, Any] = self._load_results()

    def _load_results(self) -> dict[str, Any]:
        try:
            if self._results_path.exists():
                return json.loads(self._results_path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("偏差结果文件损坏，重置: %s", self._results_path)
        return {}

    def _save_results(self) -> None:
        self._results_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._results_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._stored_results, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        tmp.replace(self._results_path)

    def stored_result(self, factor_id: str) -> Optional[dict[str, Any]]:
        return self._stored_results.get(factor_id)

    def start(self, scope: str, factor_ids: list[str]) -> BiasRun:
        # 预检：04 偏差服务未就位则如实失败（路由映射 503）
        services04()
        if not self._worker_lock.acquire(blocking=False):
            raise ValueError("另一个偏差检查批次正在运行，请等待完成后再发起")
        run = BiasRun(scope, factor_ids)
        with self._lock:
            self._runs[run.run_id] = run
        thread = threading.Thread(target=self._execute, args=(run,), daemon=True)
        thread.start()
        return run

    def _execute(self, run: BiasRun) -> None:
        any_fail = False
        try:
            _, _, bias = services04()
            for fid in run.factor_ids:
                if run.cancel_requested:
                    run.status = "CANCELLED"
                    return
                run.progress["current_factor"] = fid
                check_state().set_checking(fid, True)
                try:
                    # 逐因子调 04 BiasControlService.run（同步、逐因子落库；
                    # 与 UI 交互粒度一致，取消在因子间生效）。注意：多重检验的
                    # BH 家族因此是单因子家族（family_size=1），与 04 CLI 的
                    # 整批次家族口径不同，结果 payload 内如实携带 family_size。
                    ret = call04(bias.run, run.scope, [fid], run.run_id, None)
                    result = (ret.get("results") or [{}])[0]
                    detail = self._normalize_check_result(fid, result, run.scope, run.run_id)
                except Service04Unavailable:
                    raise
                except Exception as exc:  # 单因子失败不拖垮整批
                    detail = {
                        "factor_id": fid,
                        "overall_status": "ERROR",
                        "failure_reason": f"{type(exc).__name__}: {exc}",
                        "checks": {},
                        "out_of_sample": {},
                        "checked_at": utcnow_iso(),
                        "run_id": run.run_id,
                    }
                run.results[fid] = detail
                self._stored_results[fid] = detail
                self._save_results()
                run.progress["completed"] += 1
                status = str(detail.get("overall_status") or "").upper()
                if status == "PASS":
                    run.progress["passed"] += 1
                    check_state().mark_auto_checked(fid)
                else:
                    any_fail = True
                    if status in ("FAIL", "ERROR"):
                        run.progress["failed"] += 1
                    else:
                        run.progress["not_checked"] += 1
                    check_state().set_checking(fid, False)
            run.status = "FAIL" if any_fail else "PASS"
        except Service04Unavailable as exc:
            run.status = "ERROR"
            run.progress["latest_error"] = str(exc)
        except Exception as exc:
            run.status = "ERROR"
            run.progress["latest_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            run.progress["current_factor"] = None
            run.finished_at = utcnow_iso()
            self._worker_lock.release()

    @staticmethod
    def _normalize_check_result(fid: str, raw: Any, scope: str, run_id: str) -> dict[str, Any]:
        """把 04 bias 结果规整成六查详情形状（result: factor_id/overall_status/
        checks/oos/failure_reason/checked_at——见 04 BiasControlService.run）。"""
        if not isinstance(raw, dict):
            return {
                "factor_id": fid,
                "overall_status": "ERROR",
                "failure_reason": "04 bias-check 结果不是 dict",
                "checks": {},
                "out_of_sample": {},
                "checked_at": utcnow_iso(),
                "run_id": run_id,
            }
        checks = raw.get("checks") or {}
        normalized_checks = {}
        for key in ("lookahead", "full_sample", "multiple_testing", "overfit", "cost"):
            block = checks.get(key)
            if isinstance(block, str):
                block = {"status": block}
            normalized_checks[key] = block or {"status": "NOT_CHECKED"}
        oos = raw.get("oos") or raw.get("out_of_sample") or {}
        if isinstance(oos, str):
            oos = {"status": oos}
        overall = raw.get("overall_status") or raw.get("status") or "NOT_CHECKED"
        return {
            "factor_id": fid,
            "overall_status": str(overall).upper(),
            "scope": raw.get("scope") or scope,
            "run_id": run_id,
            "checks": normalized_checks,
            "out_of_sample": oos,
            "failure_reason": raw.get("failure_reason"),
            "checked_at": _ts_iso(raw.get("checked_at")) or utcnow_iso(),
            "raw": raw,
        }

    def get(self, run_id: str) -> Optional[BiasRun]:
        with self._lock:
            return self._runs.get(run_id)

    def latest(self) -> Optional[BiasRun]:
        with self._lock:
            if not self._runs:
                return None
            return max(self._runs.values(), key=lambda r: r.started_at)

    def cancel(self, run_id: str) -> BiasRun:
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.cancel_requested = True
        if run.status == "RUNNING":
            run.status = "CANCELLED"
            run.finished_at = utcnow_iso()
        return run


_run_manager = _RunManager(PROJECT_ROOT / "data" / "web_bias_results.json")


def run_manager() -> _RunManager:
    return _run_manager
