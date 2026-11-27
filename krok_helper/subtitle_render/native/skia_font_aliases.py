"""Verified localized family aliases for explicit Skia font resources.

The shared capability layer may report ``missing_family`` for an unlisted
localized spelling. Skia verifies the platform match against its own ``name``
table, or lazily indexes the actual Qt families' name tables when the platform
matcher omits that spelling too. A substitute without that name is rejected.

Verification runs on the GUI thread; workers only read the decision recorded
for the current font generation. Shared CPU/D2D family resolution is unchanged.
"""
import logging
import sys
import threading

from krok_helper.subtitle_render.engine.text.font_capabilities import capabilities_generation
from krok_helper.subtitle_render.engine.text.font_resource import FontResourceError
from krok_helper.subtitle_render.engine.text.font_weight import resolve_font_instance
from krok_helper.subtitle_render.n3.font_catalog import (
    invalidate_n3_font_caches,
    opentype_family_names,
    resolve_localized_qt_alias,
)

_log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_ALIASES = {}  # {family: canonical family or None}, current generation only.
_MISSING_FALLBACKS = {}  # {missing family: resolved fallback family or None}, current generation only.
_LOGGED_FALLBACKS = set()  # {(missing family, fallback family)}, current generation only.
_GENERATION = None

# 缺失字体时的跨平台回退优先级：优先选择支持中日韩文字的常见系统无衬线/黑体，
# 避免直接回退到纯拉丁字体导致整段日文/中文缺字。
_PLATFORM_FALLBACK_FAMILIES = {
    'darwin': (
        'Hiragino Sans',
        'Hiragino Kaku Gothic ProN',
        'YuGothic',
        'PingFang SC',
        'Heiti SC',
        'Arial Unicode MS',
        'Helvetica Neue',
        'Arial',
    ),
    'win32': (
        'Yu Gothic UI',
        'Yu Gothic',
        'Meiryo',
        'Microsoft YaHei',
        'SimHei',
        'SimSun',
        'Segoe UI',
        'Arial',
    ),
}
_DEFAULT_FALLBACK_FAMILIES = (
    'Noto Sans CJK JP',
    'Noto Sans CJK SC',
    'WenQuanYi Micro Hei',
    'DejaVu Sans',
    'Liberation Sans',
    'Arial',
)


def family_names(table: bytes) -> set:
    """Decoded family-name records of an OpenType ``name`` table."""
    return opentype_family_names(table)


def _current():
    global _GENERATION
    generation = capabilities_generation()
    if generation != _GENERATION:
        _ALIASES.clear()
        _MISSING_FALLBACKS.clear()
        _LOGGED_FALLBACKS.clear()
        _GENERATION = generation


def verify_alias(family: str):
    """GUI thread: Qt's matched family when its face really carries ``family``."""
    with _LOCK:
        prev_gen = _GENERATION
        _current()
        generation_moved = prev_gen is not None and _GENERATION != prev_gen
    if generation_moved:
        invalidate_n3_font_caches()
    alias = resolve_localized_qt_alias(family)
    with _LOCK:
        _current()
        _ALIASES[family] = alias
    return alias


def prepared_alias(family: str):
    """Worker-safe lookup of a GUI-verified alias for the current generation."""
    with _LOCK:
        _current()
        return _ALIASES.get(family)


def prepared_missing_fallback(family: str):
    """Worker-safe lookup of a GUI-selected fallback for a missing family."""
    with _LOCK:
        _current()
        return _MISSING_FALLBACKS.get(family)


def missing_family(instance) -> bool:
    return instance is not None and instance.reason == 'missing_family' and not instance.resource_path


def _fallback_candidates(family: str) -> list[str]:
    """Ordered candidate families to try when ``family`` is genuinely missing."""
    from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo

    candidates: list[str] = []
    seen = {str(family or '').strip().casefold()}

    def add(name: str | None) -> None:
        cleaned = str(name or '').strip()
        if not cleaned:
            return
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            candidates.append(cleaned)

    preferred = _PLATFORM_FALLBACK_FAMILIES.get(sys.platform, _DEFAULT_FALLBACK_FAMILIES)
    for name in preferred:
        add(name)
    try:
        add(QFontInfo(QFont(family)).family())
        add(QFont().defaultFamily())
    except (RuntimeError, TypeError, ValueError):
        pass
    try:
        for installed in QFontDatabase.families():
            add(installed)
    except (RuntimeError, TypeError, ValueError):
        pass
    return candidates


def resolve_missing_fallback(family: str, weight: int, italic: bool, *, resolve=None):
    """GUI thread: pick an exportable installed fallback when ``family`` is absent."""
    resolve = resolve or resolve_font_instance
    with _LOCK:
        _current()
        cached_sentinel = object()
        cached = _MISSING_FALLBACKS.get(family, cached_sentinel)
    if cached is not cached_sentinel:
        if cached is None:
            return None
        try:
            instance = resolve(cached, weight, italic=italic)
            if not missing_family(instance) and instance.resource_path:
                return instance
        except (FontResourceError, OSError, ValueError, RuntimeError):
            pass

    for candidate in _fallback_candidates(family):
        try:
            instance = resolve(candidate, weight, italic=italic)
        except (FontResourceError, OSError, ValueError, RuntimeError):
            continue
        if not missing_family(instance) and instance.resource_path:
            chosen = instance.family or candidate
            should_log = False
            with _LOCK:
                _current()
                _MISSING_FALLBACKS[family] = chosen
                log_key = (family, chosen)
                if log_key not in _LOGGED_FALLBACKS:
                    _LOGGED_FALLBACKS.add(log_key)
                    should_log = True
            if should_log:
                _log.warning(
                    "Skia 未找到字体「%s」（字重 %d），已自动回退使用「%s」",
                    family,
                    weight,
                    chosen,
                )
            return instance

    with _LOCK:
        _current()
        _MISSING_FALLBACKS[family] = None
    _log.error(
        "Skia 未找到字体「%s」（字重 %d），且未能找到可用的系统回退字体",
        family,
        weight,
    )
    return None


def gui_font_instance(family: str, weight: int, italic: bool, *, resolve=None, allow_missing_fallback: bool = True):
    """GUI thread: the shared decision, a verified alias, or an explicit fallback.

    ``resolve`` is the caller's discovery seam (default: the shared resolver).
    """
    resolve = resolve or resolve_font_instance
    instance = resolve(family, weight, italic=italic)
    if missing_family(instance) and (alias := verify_alias(family)):
        instance = resolve(alias, weight, italic=italic)
    if allow_missing_fallback and missing_family(instance):
        fallback = resolve_missing_fallback(family, weight, italic, resolve=resolve)
        if fallback is not None:
            return fallback
    return instance
