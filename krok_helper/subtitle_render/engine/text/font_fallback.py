"""Cluster-level glyph coverage + fallback resolution（Qt 权威，双后端共享）。

问题：请求字体缺字时，CPU（Qt）走 Qt 自己的回退链选字，GPU（DirectWrite）
走 C++ 侧的候选链——两条链各自维护、各自写死候选族名，缺字场景 CPU/GPU
观感分叉（2026-10-11 实测 Segoe UI@600 承载汉字差 5-7px）。

做法：**让 Qt 的解析结果成为唯一决策**。本模块按文本簇（grapheme cluster）
检查请求字体覆盖，并对缺字簇用 ``QTextLayout`` 的 glyph runs 读出 Qt 实际
用哪个字体渲染——这正是 CPU 画面的事实，随 IR 下发给 GPU 执行，GPU 不再
自行按脚本猜族名（也不再有「前一个字符用过某字体」的历史偏置）。

覆盖判定（按簇，不看单个首字形）：

* 跳过变体选择符（U+FE00-FE0F / U+E0100-E01EF）、组合符/格式符/控制符
  （Unicode 类别 Mn/Me/Cf/Cc）、空白——这些码点没有独立可见字形是正常的；
* 簇内其余**基础码点**必须都有非零 glyph 才算覆盖。

线程约定：所有 Qt 调用（``cluster_coverage`` / ``resolve_fallback_family``）
只在 GUI 线程的预热里跑；渲染线程只查 ``lookup_fallback`` 这张纯 dict 表
（与 font_capabilities 的预热约定一致，见 preview_async 注释）。
"""

from __future__ import annotations

import threading
import unicodedata
from dataclasses import dataclass

from PyQt6.QtCore import QTextBoundaryFinder
from PyQt6.QtGui import QFont, QRawFont, QTextLayout, QTextOption

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    canonical_family,
    capabilities_generation,
    clear_capabilities_cache,
)
from krok_helper.subtitle_render.engine.text.font_weight import build_weight_font

# ---------------------------------------------------------------------------
# 文本簇
# ---------------------------------------------------------------------------

_VARIATION_SELECTORS = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
_OPTIONAL_CATEGORIES = frozenset({"Mn", "Me", "Cf", "Cc"})

# 回退表键： (代际, 规范族名, 字重, 斜体, 文本簇)
_FALLBACK_TABLE: dict[tuple, str | None] = {}
_FALLBACK_MAX = 65536
_TABLE_LOCK = threading.RLock()

# 簇覆盖/回退解析缓存（GUI 线程预热用），键同回退表去掉代际。
_RESOLUTION_CACHE: dict[tuple, tuple[str, ...]] = {}
_RAW_FONT_CACHE: dict[tuple, QRawFont] = {}
_RAW_FONT_CACHE_MAX = 256
_SLOT_FONT_CACHE: dict[tuple, QFont] = {}


def clear_font_fallback_cache() -> None:
    """字体装卸 / 文本变动后清空（随 capability 代际一起失效）。"""
    with _TABLE_LOCK:
        _FALLBACK_TABLE.clear()
        _RESOLUTION_CACHE.clear()
        _RAW_FONT_CACHE.clear()
        _SLOT_FONT_CACHE.clear()


def _is_variation_selector(code: int) -> bool:
    return any(low <= code <= high for low, high in _VARIATION_SELECTORS)


def _is_optional_scalar(char: str) -> bool:
    """组合符/格式符/控制符/空白/变体选择符——无独立可见字形是正常的。"""
    code = ord(char)
    if _is_variation_selector(code):
        return True
    if char.isspace():
        return True
    return unicodedata.category(char) in _OPTIONAL_CATEGORIES


def grapheme_clusters(text: str) -> tuple[str, ...]:
    """按 Unicode 字素簇切分（Qt 的边界扫描器，与渲染侧同源）。"""
    if not text:
        return ()
    finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, text)
    clusters: list[str] = []
    start = 0
    position = finder.toNextBoundary()
    while position != -1:
        clusters.append(text[start:position])
        start = position
        position = finder.toNextBoundary()
    if start < len(text):
        clusters.append(text[start:])
    return tuple(clusters)


# ---------------------------------------------------------------------------
# 覆盖检查
# ---------------------------------------------------------------------------


def _slot_key(family: str, weight: int, italic: bool) -> tuple:
    return (canonical_family(str(family)), int(weight), bool(italic))


def slot_font(family: str, weight: int, italic: bool) -> QFont:
    """槽位 QFont（缓存）：覆盖检查与回退解析共用同一个已建引擎的实例。"""
    key = _slot_key(family, weight, italic)
    cached = _SLOT_FONT_CACHE.get(key)
    if cached is not None:
        return cached
    font = build_weight_font(str(family), 64, int(weight), italic=bool(italic))
    if len(_SLOT_FONT_CACHE) >= _RAW_FONT_CACHE_MAX:
        _SLOT_FONT_CACHE.clear()
        _RAW_FONT_CACHE.clear()
    _SLOT_FONT_CACHE[key] = font
    return font


def slot_raw_font(family: str, weight: int, italic: bool) -> QRawFont:
    """请求槽位的实际 face（与渲染同口径构造 QFont 后取 QRawFont）。

    每个 (族, 字重, 斜体) 只建一次引擎：逐字覆盖检查复用同一 QRawFont，
    避免逐字付 ``QRawFont.fromFont`` 的引擎创建成本。
    """
    key = _slot_key(family, weight, italic)
    cached = _RAW_FONT_CACHE.get(key)
    if cached is not None:
        return cached
    raw = QRawFont.fromFont(slot_font(family, weight, italic))
    if len(_RAW_FONT_CACHE) >= _RAW_FONT_CACHE_MAX:
        _RAW_FONT_CACHE.clear()
    _RAW_FONT_CACHE[key] = raw
    return raw


def _scalar_covered(raw: QRawFont, char: str) -> bool:
    try:
        glyphs = list(raw.glyphIndexesForString(char))
    except (RuntimeError, TypeError, ValueError):
        return False
    return any(int(glyph) != 0 for glyph in glyphs)


def cluster_covered(raw: QRawFont, cluster: str) -> bool:
    """簇是否被该 face 覆盖（只看必需码点，跳过组合符/变体选择符/控制符）。"""
    required = [char for char in cluster if not _is_optional_scalar(char)]
    if not required:
        return True
    return all(_scalar_covered(raw, char) for char in required)


def missing_clusters(family: str, weight: int, italic: bool, text: str) -> tuple[str, ...]:
    """文本中请求字体覆盖不到的簇（GUI 线程预热用）。"""
    if not text:
        return ()
    raw = slot_raw_font(family, weight, italic)
    return tuple(
        cluster
        for cluster in grapheme_clusters(text)
        if not cluster_covered(raw, cluster)
    )


# ---------------------------------------------------------------------------
# 回退解析（Qt 权威）
# ---------------------------------------------------------------------------


def _layout_runs(font: QFont, text: str) -> list[tuple[str, str | None]]:
    """Qt 实际用来画 ``text`` 的 (族名, style 名) 列表（含回退 face）。"""
    layout = QTextLayout(text, font)
    layout.setTextOption(QTextOption())
    layout.beginLayout()
    line = layout.createLine()
    line.setLineWidth(1_000_000)
    layout.endLayout()
    runs: list[tuple[str, str | None]] = []
    for run in line.glyphRuns():
        raw = run.rawFont()
        runs.append((str(raw.familyName()), str(raw.styleName()) or None))
    return runs


def _layout_run_families(font: QFont, text: str) -> list[tuple[str, int]]:
    """``(族名, 覆盖的码点数)`` 列表（按 run 顺序）。

    此 Qt 构建的 ``QGlyphRun.stringIndexes()`` 返回空表，只能按字形数回推
    每个 run 覆盖的字符数；调用方必须校验码点总数一致，否则放弃批量映射。
    """
    layout = QTextLayout(text, font)
    layout.setTextOption(QTextOption())
    layout.beginLayout()
    line = layout.createLine()
    line.setLineWidth(1_000_000)
    layout.endLayout()
    return [
        (str(run.rawFont().familyName()), len(list(run.glyphIndexes())))
        for run in line.glyphRuns()
    ]


def _fallback_families_from_layout(
    font: QFont, requested: str, clusters: "list[str] | tuple[str, ...]"
) -> dict[str, str | None]:
    """一次 QTextLayout 解析多个簇的回退族名（逐簇返回，None = 无回退）。

    逐簇单建 QTextLayout 实测每簇 ~11ms（塑形触发回退查询），缺字多的工程
    （拉丁字体请求 + 汉字歌词）冷预热近 1s；批量化后一次塑形覆盖全部缺字
    簇，簇间用细空白（U+2009）连接。只有**单码点簇**参与批量（码点↔字形
    一一对应才可回推归属）；组合符簇 / 代理对簇退回逐簇解析。
    """
    if not clusters:
        return {}
    result: dict[str, str | None] = {cluster: None for cluster in clusters}
    batched = [
        cluster
        for cluster in clusters
        if len(cluster) == 1 and not (0xD800 <= ord(cluster) <= 0xDFFF)
    ]
    others = [cluster for cluster in clusters if cluster not in set(batched)]

    if batched:
        joiner = "\u2009"
        text = joiner.join(batched)
        runs = _layout_run_families(font, text)
        total_codepoints = len(batched) * 2 - 1  # 簇 + 连接符
        if sum(count for _family, count in runs) == total_codepoints:
            cursor = 0
            for family, count in runs:
                if family and family.casefold() != requested.casefold():
                    for step in range(count):
                        # 偶数步 = 簇本身，奇数步 = 连接符。
                        cluster_index = cursor + step
                        if cluster_index % 2 == 0:
                            result[batched[cluster_index // 2]] = family
                cursor += count
        else:
            # 码点 ↔ 字形不是一一对应（连字/分解）：整批退回逐簇解析。
            others = batched + others
            for cluster in batched:
                result[cluster] = None

    for cluster in others:
        if not cluster:
            continue
        for family, _count in _layout_run_families(font, cluster):
            if family and family.casefold() != requested.casefold():
                result[cluster] = family
                break
    return result


def resolve_fallback_families(
    family: str, weight: int, italic: bool, clusters: "list[str] | tuple[str, ...]"
) -> dict[str, str | None]:
    """批量版 :func:`resolve_fallback_family`（GUI 线程预热用）。"""
    unique = list(dict.fromkeys(cluster for cluster in clusters if cluster))
    if not unique:
        return {}
    requested = canonical_family(str(family))
    return _fallback_families_from_layout(
        slot_font(family, weight, italic), requested, unique
    )


def resolve_fallback_family(family: str, weight: int, italic: bool, cluster: str) -> str | None:
    """缺字簇的回退族名：Qt 实际选中的那个字体（GUI 线程预热用）。

    返回 None 表示 Qt 也没有可用字体（真缺字）或该簇本就无需回退。
    """
    if not cluster:
        return None
    requested = canonical_family(str(family))
    return _fallback_families_from_layout(
        slot_font(family, weight, italic), requested, [cluster]
    ).get(cluster)


def prewarm_fallback(
    family: str,
    weight: int,
    italic: bool,
    texts: "list[str] | tuple[str, ...]",
) -> int:
    """GUI 线程预热：为一批文本算好回退族名，写进纯 dict 表。返回写入条目数。

    覆盖的文本不写表（渲染线程查表未命中即视为「无需回退」，native 侧保持
    请求字体）——避免把「已覆盖」和「尚未预热」混为一谈。
    """
    cache_key_base = _slot_key(family, weight, italic)
    written = 0
    pending_missing: dict[str, str] = {}
    for text in texts:
        if not text:
            continue
        key = (cache_key_base, text)
        with _TABLE_LOCK:
            if key in _RESOLUTION_CACHE:
                continue
        missing = missing_clusters(family, weight, italic, text)
        with _TABLE_LOCK:
            if len(_RESOLUTION_CACHE) >= _FALLBACK_MAX:
                _RESOLUTION_CACHE.clear()
                _FALLBACK_TABLE.clear()
            _RESOLUTION_CACHE[key] = missing
        for cluster in missing:
            pending_missing.setdefault(cluster, text)
    if not pending_missing:
        return written
    # 批量解析：一次 QTextLayout 覆盖全部缺字簇（逐簇建布局实测每簇 ~11ms）。
    resolved_by_cluster = resolve_fallback_families(
        family, weight, italic, list(pending_missing)
    )
    generation = capabilities_generation()
    for cluster, text in pending_missing.items():
        family_hit = resolved_by_cluster.get(cluster)
        if not family_hit:
            continue
        with _TABLE_LOCK:
            _FALLBACK_TABLE[(generation,) + (cache_key_base, text)] = family_hit
        written += 1
    return written


def lookup_fallback(family: str, weight: int, italic: bool, text: str) -> str | None:
    """渲染线程：查该 (槽位, 文本) 的回退族名；纯 dict，不碰 Qt。

    未预热 / 已覆盖 / 字体库已换代 → None（native 侧回退系统字体回退）。
    """
    if not text:
        return None
    key = (
        capabilities_generation(),
        _slot_key(family, weight, italic),
        text,
    )
    with _TABLE_LOCK:
        return _FALLBACK_TABLE.get(key)


def missing_clusters_lookup(
    family: str, weight: int, italic: bool, text: str
) -> tuple[str, ...] | None:
    """渲染线程：查该文本的缺字簇（未预热返回 None）。纯 dict。"""
    with _TABLE_LOCK:
        return _RESOLUTION_CACHE.get((_slot_key(family, weight, italic), text))


def clear_font_caches() -> None:
    """字体装卸后统一失效（capabilities + fallback 两层缓存）。"""
    clear_capabilities_cache()
    clear_font_fallback_cache()


__all__ = [
    "resolve_fallback_families",
    "slot_font",
    "clear_font_caches",
    "clear_font_fallback_cache",
    "cluster_covered",
    "grapheme_clusters",
    "lookup_fallback",
    "missing_clusters",
    "missing_clusters_lookup",
    "prewarm_fallback",
    "resolve_fallback_family",
    "slot_raw_font",
]
