"""Shared GUI-thread font preparation for preview and export."""
from __future__ import annotations
import logging
from typing import Optional
from PyQt6.QtCore import QThread
from PyQt6.QtGui import QGuiApplication
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.native.protocol import collect_referenced_font_families
_log = logging.getLogger(__name__)

def prepare_fonts(
    track: Optional[TimingTrack],
    style: Optional[Style],
    extra_tracks: Optional[list[TimingTrack]],
) -> None:
    """GUI 线程预热字体能力缓存，让渲染线程 configure 只读缓存、不碰 Qt 字体库。

    ``apply_resolved_font_faces``（native.protocol）随 IR 下发 ``font_axis``
    标记，内部对每个 ``*font_family`` 槽位调 ``get_capabilities``——那会枚举
    QFontDatabase / QRawFont，必须留在 GUI 线程（EMBEDDING §8）。渲染线程
    持 Qt 字体互斥锁建引擎时一旦引擎创建发出 Qt 告警，消息处理器还要抢
    GIL / 写面包屑文件，等同一把锁的 GUI 线程（列表 setText→字体度量）就
    被拖成分钟级「未响应」（2026-10 用户必现，py-spy 双线程栈定位）。
    预热后 ``get_capabilities`` 走进程级缓存 dict 命中，渲染线程零 Qt 调用。
    只在本线程即应用线程时执行；非 GUI 线程调用（测试等）安全跳过。
    """
    app = QGuiApplication.instance()
    if app is None or QThread.currentThread() is not app.thread():
        return
    try:
        from krok_helper.subtitle_render.engine.text.font_capabilities import (
            get_capabilities,
        )

        families = collect_referenced_font_families(
            track, style, *(extra_tracks or ())
        )
        for family in families:
            get_capabilities(family)
        from krok_helper.subtitle_render.domain.models import style_to_dict, style_for_track
        from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
        if style is not None:
            apply_resolved_font_faces(style_to_dict(style))
            for item in ([track] if track is not None else []) + list(extra_tracks or ()):
                apply_resolved_font_faces(style_to_dict(style_for_track(style, item)))
            # Titles resolve their own scheme and inline roles; their effective
            # font/weight combinations need preparing on the GUI thread too.
            if track is not None:
                from krok_helper.subtitle_render.engine.render.render_ir import titles_to_ir
                apply_resolved_font_faces(titles_to_ir(track, style))
        _prewarm_font_fallback(track, style, extra_tracks)
    except Exception:  # noqa: BLE001 — 预热失败不阻断预览，渲染线程回退原路径
        _log.debug("字体能力缓存预热失败", exc_info=True)


def _prewarm_font_fallback(
    track: Optional[TimingTrack],
    style: Optional[Style],
    extra_tracks: Optional[list[TimingTrack]],
) -> None:
    """GUI 线程预热缺字回退表：逐槽位批量算好「Qt 实际会选哪个字体」。

    渲染线程的 IR 序列化只查这张纯 dict 表（``font_fallback.lookup_fallback``），
    与 ``get_capabilities`` 同一线程约定——QTextLayout / QRawFont 一律留在
    GUI 线程。槽位按 ``font_slot_for_text`` 分类（拉丁槽/主槽；emoji 走显式
    Symbol 字体，两侧同源不需要回退），同一槽位的全部字符合并成一次预热
    调用，逐字只付 ``glyphIndexesForString``。
    """
    if style is None:
        return
    from krok_helper.subtitle_render.engine.style.style_semantics import (
        style_for_role,
    )
    from krok_helper.subtitle_render.engine.text.font_fallback import prewarm_fallback
    from krok_helper.subtitle_render.engine.text.metrics import font_slot_for_text
    from krok_helper.subtitle_render.domain.models import style_for_track, style_to_dict
    from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces

    buckets: dict[tuple[str, int, bool], list[str]] = {}
    prepared_roles: set[tuple[int, str | None]] = set()
    tracks: list[TimingTrack] = []
    if track is not None:
        tracks.append(track)
    tracks.extend(extra_tracks or ())
    for item in tracks:
        track_style = style_for_track(style, item)
        for line in getattr(item, "lines", ()) or ():
            for char in getattr(line, "chars", ()) or ():
                text = str(getattr(char, "text", "") or "")
                if not text:
                    continue
                char_style = style_for_role(track_style, getattr(char, "role_label", None))
                role_key = (id(track_style), getattr(char, "role_label", None))
                if role_key not in prepared_roles:
                    apply_resolved_font_faces(style_to_dict(char_style))
                    prepared_roles.add(role_key)
                slot = font_slot_for_text(char_style, text)
                if slot is None:
                    continue
                buckets.setdefault(slot, []).append(text)
    for (family, weight, italic), texts in buckets.items():
        from krok_helper.subtitle_render.engine.text.font_weight import resolve_font_instance
        resolve_font_instance(family, weight, italic)
        prewarm_fallback(family, weight, italic, sorted(set(texts)))


