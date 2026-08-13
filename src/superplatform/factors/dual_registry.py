"""双文件因子注册中心：扫描 / 校验 / 注册 / 热插拔（移植自 sim_platform app/factors/registry.py）。

职责：
- 同时扫描内置 `factors/` 与导入 `imports/factors/` 两个插件目录
  （MD + impl/<name>.py 成对），校验通过后把 impl 的 compute 包装成
  exchangia 的 `Factor` 并注册进 `FactorRegistry`（与 decorator 通道并存）；
- MD 是唯一事实来源：孤立 impl/*.py 一律不注册；
- 内置与 imports 同 factor_id 冲突时内置优先并告警；
- `check_and_reload()` 用 mtime 快照做增量 diff——只重扫新增/修改/删除的
  文件，不全量重算（数千因子规模下每 tick 的开销 = 目录 glob + stat）；
- 校验失败的 MD 不注册，失败原因（规则编号 + 字段名）保留在内存供
  `list_factors()` / CLI 展示。

冻结接口：impl 入口 `compute(data: dict[data_type, dict[symbol, DataFrame]],
**params)`，返回 `FactorResult` 或含 timestamp/value 两列的 DataFrame
（Series 与单列 DataFrame 自动归一到 timestamp/value）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from superplatform.factors import protocol
from superplatform.factors.base import Factor, FactorCategory, FactorResult
from superplatform.factors.protocol import ValidationIssue
from superplatform.factors.registry import FactorRegistry

logger = logging.getLogger("superplatform.factors.dual_registry")

# 扫描时忽略的模板文件名
TEMPLATE_NAME = "TEMPLATE.md"

# 双文件插件目录：内置优先于导入
SOURCE_BUILTIN = "builtin"
SOURCE_IMPORTS = "imports"

# MD category 枚举（12 类）→ exchangia FactorCategory（5 类）
_CATEGORY_MAP: dict[str, FactorCategory] = {
    "momentum": FactorCategory.MOMENTUM_REVERSAL,
    "reversal": FactorCategory.MOMENTUM_REVERSAL,
    "technical": FactorCategory.MOMENTUM_REVERSAL,
    "volatility": FactorCategory.VOLATILITY,
    "volume": FactorCategory.VOLUME_LIQUIDITY,
    "microstructure": FactorCategory.MICROSTRUCTURE,
    "basis_funding": FactorCategory.CRYPTO_SPECIFIC,
    "onchain": FactorCategory.CRYPTO_SPECIFIC,
    "sentiment": FactorCategory.CRYPTO_SPECIFIC,
    "cross_asset": FactorCategory.CRYPTO_SPECIFIC,
    "ml_feature": FactorCategory.CRYPTO_SPECIFIC,
    "other": FactorCategory.CRYPTO_SPECIFIC,
}

# inputs 字段 → exchangia required_data（数据类型）
_INPUT_DATA_TYPE: dict[str, str] = {
    "open": "kline", "high": "kline", "low": "kline", "close": "kline",
    "volume": "kline", "quote_volume": "kline", "trades": "kline",
    "taker_buy_volume": "kline", "vwap": "kline",
    "funding_rate": "funding_rate",
    "open_interest": "open_interest",
    "mark_price": "mark_price",
}


@dataclass
class DualFactorRecord:
    """单个双文件因子的注册记录（内存态）。"""

    factor_id: str
    name: str
    category: str                       # MD 原始 category（12 类枚举）
    version: str
    status: str                         # draft / active / deprecated
    frequency: str
    lookback_bars: int
    inputs: list[str]
    params: dict[str, Any]
    md_path: Path
    impl_path: Path
    source: str                         # builtin / imports
    entry: str = "compute"
    author: Optional[str] = None
    compute_fn: Optional[Callable] = None
    registered_ts: Optional[datetime] = None


@dataclass
class FailedRecord:
    """校验失败的 MD 记录（仅内存）。"""

    md_path: Path
    source: str
    issues: list[ValidationIssue]


def _coerce_values(result: Any) -> pd.DataFrame:
    """把 impl 返回值归一为含 timestamp/value 两列的 DataFrame。"""
    if isinstance(result, pd.Series):
        df = result.to_frame("value")
        df.insert(0, "timestamp", df.index)
        return df.reset_index(drop=True)
    if isinstance(result, pd.DataFrame):
        if {"timestamp", "value"}.issubset(result.columns):
            return result
        if len(result.columns) == 1:
            df = result.rename(columns={result.columns[0]: "value"})
            df.insert(0, "timestamp", df.index)
            return df.reset_index(drop=True)
    raise ValueError(
        "双文件因子 compute 必须返回 FactorResult、含 timestamp/value 列的 "
        f"DataFrame 或 Series，实际: {type(result).__name__}"
    )


def _build_factor(rec: DualFactorRecord) -> Factor:
    """把 impl 的 compute 包装成 exchangia 的 Factor 实例。"""
    fn = rec.compute_fn
    base_params = dict(rec.params)
    category = _CATEGORY_MAP[rec.category]
    required_data = sorted({_INPUT_DATA_TYPE[x] for x in rec.inputs})
    factor_id = rec.factor_id

    def compute(self, data, **params):
        merged = {**base_params, **params}
        result = fn(data, **merged)
        if isinstance(result, FactorResult):
            return result
        return FactorResult(
            name=factor_id,
            category=category,
            values=_coerce_values(result),
            metadata={
                "params": merged,
                "source": rec.source,
                "md_path": str(rec.md_path),
                "factor_name": rec.name,
            },
        )

    cls = type(
        f"DualFactor_{factor_id.replace('-', '_')}",
        (Factor,),
        {
            "compute": compute,
            "name": factor_id,
            "category": category,
            "description": f"双文件因子 {factor_id} ({rec.name})，MD: {rec.md_path.name}",
            "version": rec.version,
            "required_data": required_data,
            "required_symbols": None,
        },
    )
    return cls()


class DualFactorRegistry:
    """双文件因子注册中心（线程安全，与 decorator 版 FactorRegistry 并存）。

    校验通过的因子以 factor_id 为名注册进 `FactorRegistry.get_instance()`，
    因此 `FactorRegistry.get_instance().get("MOM-001").compute(data)` 与
    decorator 因子走同一条消费路径。
    """

    _instance: Optional["DualFactorRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        builtin_dir: str | Path | None = None,
        imports_dir: str | Path | None = None,
    ) -> None:
        base = protocol.BASE_DIR
        # 扫描顺序即优先级：内置在前，导入在后
        self.dirs: list[tuple[str, Path]] = [
            (SOURCE_BUILTIN, Path(builtin_dir) if builtin_dir else base / "factors"),
            (SOURCE_IMPORTS, Path(imports_dir) if imports_dir else base / "imports" / "factors"),
        ]
        self._lock = threading.RLock()
        # factor_id -> DualFactorRecord（仅校验通过的因子）
        self._factors: dict[str, DualFactorRecord] = {}
        # md_path（字符串） -> FailedRecord（校验失败的 MD）
        self._failed: dict[str, FailedRecord] = {}
        # md_path（字符串） -> 冲突说明（imports 与内置同 ID，被内置压制）
        self._conflicts: dict[str, str] = {}
        # 文件 mtime 快照：路径字符串 -> mtime_ns（覆盖 .md 与 impl/*.py）
        self._file_mtimes: dict[str, int] = {}
        self._scanned = False

    @classmethod
    def get_instance(cls) -> "DualFactorRegistry":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ---------------------------------------------------------------
    # 全量扫描与注册
    # ---------------------------------------------------------------
    def scan_all(self) -> dict[str, Any]:
        """全量扫描内置 + imports 插件目录，校验并注册，返回摘要统计。"""
        with self._lock:
            for rec in self._factors.values():
                FactorRegistry.get_instance().unregister(rec.factor_id)
            self._factors.clear()
            self._failed.clear()
            self._conflicts.clear()
            seen_ids: set[str] = set()
            md_files: list[tuple[Path, str]] = []
            for source, d in self.dirs:
                for md_path in self._list_md_files(d):
                    md_files.append((md_path, source))
            for md_path, source in md_files:
                self._register_one(md_path, source, seen_ids)
            self._file_mtimes = self._snapshot_mtimes()
            self._scanned = True
            summary = {
                "scanned": len(md_files),
                "registered": len(self._factors),
                "failed": len(self._failed),
                "conflicts": len(self._conflicts),
                "active": sum(1 for r in self._factors.values() if r.status == "active"),
                "draft": sum(1 for r in self._factors.values() if r.status == "draft"),
                "deprecated": sum(1 for r in self._factors.values() if r.status == "deprecated"),
            }
            logger.info(
                "双文件因子全量扫描完成: 扫描 %d | 注册 %d（active=%d draft=%d deprecated=%d）"
                " | 失败 %d | 冲突 %d",
                summary["scanned"], summary["registered"], summary["active"],
                summary["draft"], summary["deprecated"], summary["failed"],
                summary["conflicts"],
            )
            for fid, rec in self._factors.items():
                logger.info("  [注册] %s %s (%s/%s, %s)",
                            fid, rec.name, rec.category, rec.status, rec.source)
            for path, fr in self._failed.items():
                for issue in fr.issues:
                    logger.warning("  [拒绝] %s -> %s", Path(path).name, issue)
            return summary

    def force_reload(self) -> dict[str, Any]:
        """强制全量重载。"""
        logger.info("收到强制重载请求，执行全量扫描")
        return self.scan_all()

    def ensure_scanned(self) -> None:
        """首次调用全量扫描，之后每次调用做 mtime 增量 diff（热插拔入口）。"""
        if not self._scanned:
            self.scan_all()
        else:
            self.check_and_reload()

    # ---------------------------------------------------------------
    # 增量热重载（mtime diff，只重扫变更文件）
    # ---------------------------------------------------------------
    def check_and_reload(self) -> bool:
        """对比 mtime 快照，新增/修改/删除文件时增量重载。返回是否有变更。"""
        with self._lock:
            if not self._scanned:
                self.scan_all()
                return True
            current = self._snapshot_mtimes()
            if current == self._file_mtimes:
                return False
            old = self._file_mtimes
            added = [p for p in current if p not in old]
            removed = [p for p in old if p not in current]
            changed = [p for p in current if p in old and current[p] != old[p]]

            seen_ids = set(self._factors.keys())
            dirty = False

            # 1) 删除的 .md -> 注销对应因子
            for p in removed:
                if p.endswith(".md"):
                    dirty |= self._unregister_by_md(Path(p))

            # 2) 新增/修改的 .md -> 重新校验注册（同 factor_id 先视为更新）
            for p in added + changed:
                if not p.endswith(".md"):
                    continue
                md_path = Path(p)
                if md_path.name == TEMPLATE_NAME:
                    continue
                # 修改场景：先注销旧记录再重新注册（seen_ids 需先移除旧 id）
                old_rec = self._find_by_md(md_path)
                if old_rec is not None:
                    seen_ids.discard(old_rec.factor_id)
                    self._unregister(old_rec.factor_id)
                self._failed.pop(str(md_path), None)
                self._conflicts.pop(str(md_path), None)
                self._register_one(md_path, self._source_of(md_path), seen_ids)
                dirty = True

            # 3) 修改的 impl/*.py -> 重载依赖它的因子（模块按 mtime 重新加载）
            for p in changed:
                if p.endswith(".py"):
                    for rec in list(self._factors.values()):
                        if str(rec.impl_path) == p:
                            seen_ids.discard(rec.factor_id)
                            self._unregister(rec.factor_id)
                            self._register_one(rec.md_path, rec.source, seen_ids)
                            dirty = True
                            logger.info("实现文件变更，因子已热重载: %s (%s)",
                                        rec.factor_id, Path(p).name)

            # 4) 新增/删除的孤立 impl .py：不改变注册集（孤立 .py 一律忽略），仅记日志
            for p in added:
                if p.endswith(".py"):
                    logger.info("检测到新增实现文件（孤立 .py 不注册，需配套 MD）: %s",
                                Path(p).name)
            for p in removed:
                if p.endswith(".py"):
                    # 实现文件被删除：对应因子下次重注册会校验失败，这里主动注销
                    for rec in list(self._factors.values()):
                        if str(rec.impl_path) == p:
                            self._unregister(rec.factor_id)
                            self._failed[str(rec.md_path)] = FailedRecord(
                                md_path=rec.md_path,
                                source=rec.source,
                                issues=[ValidationIssue(
                                    10, "implementation",
                                    f"实现文件已删除: {p}",
                                )],
                            )
                            dirty = True
                            logger.warning("实现文件已删除，因子注销: %s (%s)",
                                           rec.factor_id, Path(p).name)

            self._file_mtimes = current
            if dirty:
                self._sync_into_factor_registry()
                logger.info("双文件因子目录变更已增量重载: 新增 %d | 删除 %d | 修改 %d",
                            len(added), len(removed), len(changed))
            return dirty

    # ---------------------------------------------------------------
    # 查询接口
    # ---------------------------------------------------------------
    def get_record(self, factor_id: str) -> Optional[DualFactorRecord]:
        with self._lock:
            return self._factors.get(factor_id)

    def list_factors(self) -> list[dict[str, Any]]:
        """返回全部双文件因子（含校验失败与冲突的 MD）的状态列表。"""
        with self._lock:
            rows: list[dict[str, Any]] = []
            for rec in sorted(self._factors.values(), key=lambda r: r.factor_id):
                rows.append({
                    "factor_id": rec.factor_id,
                    "name": rec.name,
                    "category": rec.category,
                    "version": rec.version,
                    "status": rec.status,
                    "frequency": rec.frequency,
                    "lookback_bars": rec.lookback_bars,
                    "author": rec.author,
                    "source": rec.source,
                    "registered": True,
                    "validation_errors": [],
                    "md_path": str(rec.md_path),
                    "impl_path": str(rec.impl_path),
                    "registered_ts": rec.registered_ts.isoformat() if rec.registered_ts else None,
                })
            for path, fr in sorted(self._failed.items()):
                rows.append({
                    "factor_id": None,
                    "name": Path(path).stem,
                    "category": None,
                    "version": None,
                    "status": "invalid",
                    "frequency": None,
                    "lookback_bars": None,
                    "author": None,
                    "source": fr.source,
                    "registered": False,
                    "validation_errors": [
                        {"rule_no": i.rule_no, "field": i.field, "message": i.message}
                        for i in fr.issues
                    ],
                    "md_path": path,
                    "impl_path": None,
                    "registered_ts": None,
                })
            for path, msg in sorted(self._conflicts.items()):
                rows.append({
                    "factor_id": None,
                    "name": Path(path).stem,
                    "category": None,
                    "version": None,
                    "status": "conflict",
                    "frequency": None,
                    "lookback_bars": None,
                    "author": None,
                    "source": SOURCE_IMPORTS,
                    "registered": False,
                    "validation_errors": [],
                    "conflict": msg,
                    "md_path": path,
                    "impl_path": None,
                    "registered_ts": None,
                })
            return rows

    # ---------------------------------------------------------------
    # 内部：单文件注册 / 注销
    # ---------------------------------------------------------------
    def _register_one(self, md_path: Path, source: str, seen_ids: set[str]) -> None:
        """校验并注册单个 MD（调用方需持有锁）。"""
        # 反向冲突前置处理：内置文件后入场时，先释放同 ID 的 imports 记录，
        # 否则规则 3 的唯一性检查会把内置文件误判为重复注册
        if source == SOURCE_BUILTIN:
            pre = protocol.parse_md(md_path)
            if pre.meta:
                cand = str(pre.meta.get("factor_id", ""))
                holder = self._factors.get(cand)
                if holder is not None and holder.source == SOURCE_IMPORTS:
                    logger.warning(
                        "ID 冲突，内置优先: 内置 %s 压制 imports %s（factor_id=%s）",
                        md_path.name, holder.md_path.name, cand,
                    )
                    seen_ids.discard(cand)
                    self._unregister(cand)
                    self._conflicts[str(holder.md_path)] = (
                        f"factor_id '{cand}' 与内置因子冲突，内置优先 "
                        f"（内置: {md_path.name}，被压制: {holder.md_path.name}）"
                    )
        issues = protocol.validate(md_path, seen_ids)
        if issues:
            # 内置优先冲突：唯一失败项是规则 3 重复，且在册记录来自内置、
            # 当前文件来自 imports —— 不算坏文件，记为冲突并告警
            if (source == SOURCE_IMPORTS
                    and all(i.rule_no == 3 for i in issues)
                    and any("不唯一" in i.message for i in issues)):
                doc = protocol.parse_md(md_path)
                dup_id = str((doc.meta or {}).get("factor_id", ""))
                holder = self._factors.get(dup_id)
                if holder is not None and holder.source == SOURCE_BUILTIN:
                    msg = (f"factor_id '{dup_id}' 与内置因子冲突，内置优先 "
                           f"（内置: {holder.md_path.name}，被压制: {md_path.name}）")
                    self._conflicts[str(md_path)] = msg
                    logger.warning("ID 冲突，内置优先: %s", msg)
                    return
            self._failed[str(md_path)] = FailedRecord(
                md_path=md_path, source=source, issues=issues,
            )
            return
        doc = protocol.parse_md(md_path)
        assert doc.meta is not None  # 校验通过则 meta 必然可用
        meta = doc.meta
        factor_id = str(meta["factor_id"])

        # 同 source 重复 ID（规则 3 已被 seen_ids 拦住），防御性兜底
        existing = self._factors.get(factor_id)
        if existing is not None and existing.md_path != md_path:
            self._unregister(factor_id)

        entry = str(meta.get("entry", protocol.DEFAULT_ENTRY))
        impl_path = protocol._resolve_impl_path(str(meta["implementation"]), md_path)
        try:
            compute_fn = protocol.load_entry_function(impl_path, entry)
        except Exception as e:
            # 理论上规则 10 已拦截，防御性兜底
            self._failed[str(md_path)] = FailedRecord(
                md_path=md_path,
                source=source,
                issues=[ValidationIssue(10, "implementation", f"入口加载失败: {e}")],
            )
            return
        rec = DualFactorRecord(
            factor_id=factor_id,
            name=str(meta["name"]),
            category=str(meta["category"]),
            version=str(meta["version"]),
            status=str(meta["status"]),
            frequency=str(meta["frequency"]),
            lookback_bars=int(meta["lookback_bars"]),
            inputs=[str(x) for x in meta["inputs"]],
            params=dict(meta.get("params") or {}),
            md_path=md_path,
            impl_path=impl_path,
            source=source,
            entry=entry,
            compute_fn=compute_fn,
            registered_ts=datetime.now(timezone.utc),
            author=meta.get("author"),
        )
        self._factors[factor_id] = rec
        seen_ids.add(factor_id)
        self._failed.pop(str(md_path), None)
        self._conflicts.pop(str(md_path), None)
        FactorRegistry.get_instance().register(_build_factor(rec))

    def _unregister(self, factor_id: str) -> bool:
        """按 factor_id 注销（调用方需持有锁）。"""
        rec = self._factors.pop(factor_id, None)
        if rec is not None:
            self._failed.pop(str(rec.md_path), None)
            FactorRegistry.get_instance().unregister(factor_id)
            return True
        return False

    def _unregister_by_md(self, md_path: Path) -> bool:
        """按 MD 路径注销（文件删除场景；调用方需持有锁）。"""
        rec = self._find_by_md(md_path)
        dirty = False
        if rec is not None:
            self._unregister(rec.factor_id)
            dirty = True
        if str(md_path) in self._failed:
            self._failed.pop(str(md_path), None)
            dirty = True
        if str(md_path) in self._conflicts:
            self._conflicts.pop(str(md_path), None)
            dirty = True
        return dirty

    def _find_by_md(self, md_path: Path) -> Optional[DualFactorRecord]:
        for rec in self._factors.values():
            if rec.md_path == md_path:
                return rec
        return None

    def _source_of(self, md_path: Path) -> str:
        """按路径归属判定来源（builtin / imports）。"""
        resolved = md_path.resolve()
        for source, d in self.dirs:
            try:
                resolved.relative_to(d.resolve())
                return source
            except ValueError:
                continue
        return SOURCE_IMPORTS

    def _sync_into_factor_registry(self) -> None:
        """保证全部在册双文件因子都挂在 FactorRegistry 里。

         decorator 通道的 FactorRegistry.reload() 会清空 _factors/_instances，
        这里在每次增量重载后补齐缺失项，避免双文件因子被误清后丢失。
        """
        reg = FactorRegistry.get_instance()
        for rec in self._factors.values():
            if rec.factor_id not in reg.list_all():
                reg.register(_build_factor(rec))

    # ---------------------------------------------------------------
    # 内部：目录快照
    # ---------------------------------------------------------------
    def _list_md_files(self, factors_dir: Path) -> list[Path]:
        """列出插件目录内全部 MD（忽略 TEMPLATE.md），按文件名排序保证稳定顺序。"""
        if not factors_dir.is_dir():
            return []
        return sorted(
            p for p in factors_dir.glob("*.md") if p.name != TEMPLATE_NAME
        )

    def _snapshot_mtimes(self) -> dict[str, int]:
        """对全部插件目录的 *.md 与 impl/*.py 建立 mtime 快照。"""
        snap: dict[str, int] = {}
        try:
            for _source, factors_dir in self.dirs:
                if not factors_dir.is_dir():
                    continue
                for p in factors_dir.glob("*.md"):
                    snap[str(p)] = p.stat().st_mtime_ns
                impl_dir = factors_dir / "impl"
                if impl_dir.is_dir():
                    for p in impl_dir.glob("*.py"):
                        if p.name == "__init__.py":
                            continue
                        snap[str(p)] = p.stat().st_mtime_ns
        except OSError as e:
            logger.warning("双文件因子目录快照失败: %s", e)
        return snap
