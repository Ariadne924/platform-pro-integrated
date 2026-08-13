"""策略 MD 协议解析器 + 校验器（移植自 sim_platform app/strategies/protocol.py）。

MD 文档是双文件策略通道的唯一事实来源，结构为：YAML frontmatter + 正文固定
7 个章节。本模块逐条实现 10 条校验规则，失败时返回
（规则编号, 字段名, 描述）三元组（ValidationIssue 数据类）。

与 sim_platform 原版的关键差异（冻结接口，见 task/README_任务书.md）：
- impl 入口函数签名为 exchangia 约定：
      generate(factor_results: dict[factor_name, dict[symbol, FactorResult]],
               **params) -> StrategySignal | DataFrame(timestamp, symbol, position)
  即 sim_platform 的 generate(ctx) -> list[TargetOrder] 约定在此不适用；
  返回 DataFrame 时由注册中心包装成 StrategySignal（positions 含
  timestamp/symbol/position 三列）。
- implementation 相对路径先按仓库根解析；找不到时回退到 MD 所在插件根
  （strategies/ 的上级目录，对 imports/strategies/ 同样成立）。

对外接口：
- parse_md(path)   -> StrategyDoc    解析 MD 文档（容错，不抛异常）
- validate(md_path, seen_ids) -> list[ValidationIssue]   逐条校验，空列表 = 通过
- load_entry_function(impl_path, entry) -> callable      加载实现文件入口函数

说明：
- seen_ids 由调用方（注册中心）维护，用于规则 3 的全库唯一性检查；
  本函数只读不写，调用方在注册成功后自行 add。
- 规则 8 的可导入性检查使用带 mtime 的唯一模块名加载，避免热重载时命中
  importlib 缓存导致旧代码残留（与因子协议一致）。
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger("superplatform.strategy.protocol")

# 仓库根目录（superplatform_G1/），用于解析 frontmatter 中的相对路径
BASE_DIR = Path(__file__).resolve().parents[3]

# -------------------------------------------------------------------
# 协议常量
# -------------------------------------------------------------------

# 规则 2：frontmatter 必填字段
REQUIRED_FIELDS = [
    "strategy_id", "name", "version", "status", "symbols",
    "params", "max_leverage", "implementation", "created_at",
]

# 规则 3：strategy_id 格式
STRATEGY_ID_PATTERN = re.compile(r"^[A-Z]{2,8}-\d{3}$")

# 规则 4：name 必须为 snake_case
SNAKE_CASE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# 规则 5：status 枚举
STATUS_ENUM = {"draft", "active", "deprecated"}

# 规则 7：max_leverage 允许范围
MAX_LEVERAGE_MIN = 1
MAX_LEVERAGE_MAX = 20

# 规则 9：正文 7 个章节标题（序号 + 标题前缀，校验时容忍标题后的额外文字）
EXPECTED_SECTIONS: list[tuple[int, str]] = [
    (1, "策略概述"),
    (2, "逻辑与信号定义"),
    (3, "参数说明"),
    (4, "执行规则"),
    (5, "风控约束"),
    (6, "回测与有效性记录"),
    (7, "变更日志"),
]

# 章节标题行正则：「## 1. 策略概述」/「## 1.策略概述（补充说明）」均可匹配
_SECTION_HEADING_RE = re.compile(r"^##\s*(\d+)\s*[.、]\s*(.+?)\s*$", re.MULTILINE)

# 规则 10：逻辑与信号定义章节的 $$...$$ LaTeX 公式块
_LATEX_BLOCK_RE = re.compile(r"\$\$.+?\$\$", re.DOTALL)

# 默认入口函数名（frontmatter 未声明 entry 时使用）
DEFAULT_ENTRY = "generate"


# -------------------------------------------------------------------
# 数据结构
# -------------------------------------------------------------------

@dataclass
class ValidationIssue:
    """单条校验失败记录：(规则编号, 字段名, 描述)。"""

    rule_no: int     # 策略协议的规则编号（1~10）
    field: str       # 相关字段名（正文类问题用 "body"/章节名等）
    message: str     # 人类可读的失败描述

    def __str__(self) -> str:
        return f"规则{self.rule_no} | 字段[{self.field}] | {self.message}"


@dataclass
class StrategyDoc:
    """parse_md 的解析结果（容错：frontmatter 缺失/解析失败不抛异常）。"""

    path: Path
    meta: Optional[dict[str, Any]] = None        # frontmatter 解析结果；失败为 None
    meta_error: Optional[str] = None             # frontmatter 缺失/YAML 解析错误描述
    body: str = ""                               # 正文（frontmatter 之后）
    # 章节列表：(序号, 标题文字, 章节内容)，按文中出现顺序
    sections: list[tuple[int, str, str]] = field(default_factory=list)

    def section_content(self, number: int) -> str:
        """按序号取章节内容，不存在返回空串。"""
        for num, _title, content in self.sections:
            if num == number:
                return content
        return ""


# -------------------------------------------------------------------
# 解析
# -------------------------------------------------------------------

def parse_md(path: str | Path) -> StrategyDoc:
    """解析策略 MD 文档，返回 StrategyDoc（容错，不抛异常）。

    frontmatter 缺失或 YAML 不可解析时 meta=None 且 meta_error 记录原因，
    由 validate() 映射为规则 1 失败。
    """
    path = Path(path)
    doc = StrategyDoc(path=path)
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        doc.meta_error = f"文件读取失败: {e}"
        return doc

    # 提取 frontmatter：文件须以 --- 开头，并以单独的 --- 行结束
    fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", raw, re.DOTALL)
    if not fm_match:
        doc.meta_error = "frontmatter 缺失（文件须以 --- 开头并以 --- 行结束）"
        doc.body = raw
        doc.sections = _extract_sections(raw)
        return doc

    fm_text, body = fm_match.group(1), fm_match.group(2)
    doc.body = body
    try:
        meta = yaml.safe_load(fm_text)
        if not isinstance(meta, dict):
            doc.meta_error = "frontmatter YAML 解析结果不是键值对映射"
        else:
            doc.meta = meta
    except yaml.YAMLError as e:
        doc.meta_error = f"frontmatter YAML 解析失败: {e}"

    doc.sections = _extract_sections(body)
    return doc


def _extract_sections(body: str) -> list[tuple[int, str, str]]:
    """从正文提取编号章节：返回 (序号, 标题, 内容) 列表（按出现顺序）。"""
    matches = list(_SECTION_HEADING_RE.finditer(body))
    sections: list[tuple[int, str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((int(m.group(1)), m.group(2).strip(), body[start:end]))
    return sections


# -------------------------------------------------------------------
# 实现文件加载（供规则 8 与注册中心共用）
# -------------------------------------------------------------------

def load_entry_function(impl_path: str | Path, entry: str = DEFAULT_ENTRY) -> Callable:
    """加载实现文件并返回入口函数。

    使用带文件 mtime 的唯一模块名加载，确保热重载（文件修改后再次加载）
    拿到的是新代码而非 importlib 缓存。加载失败或入口缺失时抛异常，
    由调用方捕获并映射为校验/执行错误。
    """
    impl_path = Path(impl_path)
    mtime_ns = impl_path.stat().st_mtime_ns
    module_name = f"superplatform_strategy_impl_{impl_path.stem}_{mtime_ns}"
    # 同名旧模块（同 mtime 重复加载场景）先清理，保证语义一致
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, impl_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为实现文件创建模块 spec: {impl_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    fn = getattr(module, entry, None)
    if not callable(fn):
        raise AttributeError(f"实现文件缺少可调用入口函数 '{entry}': {impl_path}")
    return fn


def _resolve_impl_path(impl_decl: str, md_path: str | Path | None = None) -> Path:
    """将 frontmatter 中的 implementation 相对路径解析为绝对路径。

    优先按仓库根（BASE_DIR）解析；若该文件不存在且给出了 md_path，
    回退到 MD 所在的插件根（strategies/ 的上级目录）解析——
    内置 strategies/X.md 声明 strategies/impl/x.py 与
    imports/strategies/X.md 声明 imports/strategies/impl/x.py 均可命中。
    """
    p = Path(str(impl_decl))
    if p.is_absolute():
        return p
    primary = (BASE_DIR / p).resolve()
    if primary.is_file() or md_path is None:
        return primary
    plugin_root = Path(md_path).resolve().parent.parent
    fallback = (plugin_root / p).resolve()
    return fallback if fallback.is_file() else primary


# -------------------------------------------------------------------
# 校验（10 条规则逐条实现）
# -------------------------------------------------------------------

def validate(md_path: str | Path, seen_ids: Optional[set[str]] = None) -> list[ValidationIssue]:
    """校验单个策略 MD 文档，返回失败列表（空列表 = 全部通过）。

    :param md_path: 策略 MD 文件路径
    :param seen_ids: 已注册/已见 strategy_id 集合（规则 3 唯一性检查用）；
                     为 None 时跳过唯一性检查
    """
    if seen_ids is None:
        seen_ids = set()
    issues: list[ValidationIssue] = []
    doc = parse_md(md_path)

    # ---- 规则 1：frontmatter 存在且 YAML 可解析 ----
    if doc.meta is None:
        issues.append(ValidationIssue(1, "frontmatter", doc.meta_error or "frontmatter 解析失败"))
        # frontmatter 不可用则规则 2~8 均无法检查，继续检查正文类规则（9/10）
        issues.extend(_validate_body(doc))
        return issues

    meta = doc.meta

    # ---- 规则 2：必填字段齐全 ----
    missing = [f for f in REQUIRED_FIELDS if f not in meta or meta[f] is None]
    if missing:
        issues.append(ValidationIssue(
            2, ",".join(missing), f"必填字段缺失: {missing}"
        ))
        # 关键字段缺失时后续字段级规则无法可靠检查，继续正文类规则后返回
        issues.extend(_validate_body(doc))
        return issues

    # ---- 规则 3：strategy_id 格式 + 全库唯一 ----
    strategy_id = str(meta.get("strategy_id", ""))
    if not STRATEGY_ID_PATTERN.match(strategy_id):
        issues.append(ValidationIssue(
            3, "strategy_id",
            f"strategy_id '{strategy_id}' 不匹配 ^[A-Z]{{2,8}}-\\d{{3}}$",
        ))
    elif strategy_id in seen_ids:
        issues.append(ValidationIssue(
            3, "strategy_id", f"strategy_id '{strategy_id}' 在全库中不唯一（重复注册）"
        ))

    # ---- 规则 4：name 为 snake_case，且实现文件名必须为 <name>.py ----
    name = str(meta.get("name", ""))
    if not SNAKE_CASE_PATTERN.match(name):
        issues.append(ValidationIssue(
            4, "name", f"name '{name}' 不是合法的 snake_case"
        ))
    impl_decl = str(meta.get("implementation", ""))
    impl_file = Path(impl_decl).name
    if SNAKE_CASE_PATTERN.match(name) and impl_file != f"{name}.py":
        issues.append(ValidationIssue(
            4, "implementation",
            f"实现文件名必须为 <name>.py（期望 '{name}.py'，实际 '{impl_file}'）",
        ))

    # ---- 规则 5：status 枚举 ----
    status = str(meta.get("status", ""))
    if status not in STATUS_ENUM:
        issues.append(ValidationIssue(
            5, "status", f"status '{status}' 不在枚举 {sorted(STATUS_ENUM)} 内"
        ))

    # ---- 规则 6：symbols 为非空列表且元素均为字符串 ----
    symbols = meta.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        issues.append(ValidationIssue(
            6, "symbols", f"symbols 必须为非空列表，实际: {symbols!r}"
        ))
    else:
        bad = [x for x in symbols if not isinstance(x, str) or not x.strip()]
        if bad:
            issues.append(ValidationIssue(
                6, "symbols", f"symbols 含非字符串或空元素 {bad!r}"
            ))

    # ---- 规则 7：max_leverage 为 1~20 的数 ----
    max_leverage = meta.get("max_leverage")
    if (isinstance(max_leverage, bool)
            or not isinstance(max_leverage, (int, float))
            or not (MAX_LEVERAGE_MIN <= max_leverage <= MAX_LEVERAGE_MAX)):
        issues.append(ValidationIssue(
            7, "max_leverage",
            f"max_leverage '{max_leverage}' 不是 {MAX_LEVERAGE_MIN}~{MAX_LEVERAGE_MAX} 的数"
        ))

    # ---- 规则 8：implementation 真实存在、可导入、含 entry 函数 ----
    entry = str(meta.get("entry", DEFAULT_ENTRY))
    impl_path = _resolve_impl_path(impl_decl, md_path)
    parts = {part.lower() for part in impl_path.parts}
    if not {"strategies", "impl"}.issubset(parts):
        issues.append(ValidationIssue(
            8, "implementation", f"implementation 必须指向 strategies/impl/ 目录下: {impl_decl}"
        ))
    elif impl_path.suffix != ".py" or not impl_path.is_file():
        issues.append(ValidationIssue(
            8, "implementation", f"实现文件不存在或不是 .py 文件: {impl_path}"
        ))
    else:
        try:
            load_entry_function(impl_path, entry)
        except Exception as e:
            issues.append(ValidationIssue(
                8, "implementation", f"实现文件不可导入或缺少入口 '{entry}': {e}"
            ))

    # ---- 规则 9 / 10：正文章节 ----
    issues.extend(_validate_body(doc))
    return issues


def _validate_body(doc: StrategyDoc) -> list[ValidationIssue]:
    """正文类校验：规则 9（7 章节齐全且顺序正确）与规则 10（$$ 公式块）。"""
    issues: list[ValidationIssue] = []

    # ---- 规则 9：7 个章节标题齐全且顺序正确（容忍标题后额外文字）----
    headings = [(num, title) for num, title, _c in doc.sections]
    expected_pos = 0          # 在已识别标题中期待匹配的 EXPECTED_SECTIONS 下标
    last_seen_no = 0          # 上一个成功匹配的章节序号（用于顺序判定）
    matched_numbers: list[int] = []
    for num, title in headings:
        if expected_pos >= len(EXPECTED_SECTIONS):
            break
        exp_no, exp_title = EXPECTED_SECTIONS[expected_pos]
        # 序号一致、标题以期望文字开头（容忍「## 1. 策略概述（补充）」），且顺序递增
        if num == exp_no and title.startswith(exp_title) and num > last_seen_no:
            matched_numbers.append(num)
            last_seen_no = num
            expected_pos += 1
    if len(matched_numbers) < len(EXPECTED_SECTIONS):
        missing = [
            f"{no}. {t}" for no, t in EXPECTED_SECTIONS[expected_pos:]
        ]
        issues.append(ValidationIssue(
            9, "body",
            f"正文章节不齐全或顺序错误，缺失/无法按序匹配: {missing}",
        ))

    # ---- 规则 10：「逻辑与信号定义」章节至少包含一个 $$...$$ 公式块 ----
    signal_content = doc.section_content(2)
    if not _LATEX_BLOCK_RE.search(signal_content):
        issues.append(ValidationIssue(
            10, "逻辑与信号定义", "「逻辑与信号定义」章节缺少 $$...$$ LaTeX 公式块"
        ))
    return issues
