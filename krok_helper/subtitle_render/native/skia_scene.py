"""Prepare the canonical Skia scene shared by the sidecar and direct preview."""
import logging
import time

from .backend import NativeRendererError
from .protocol import apply_resolved_font_faces
from .skia_fonts import SkiaFontResolver
from .skia_text import SkiaTextMetricsProcess

_log = logging.getLogger(__name__)


_FONT_FALLBACK_ERROR_MARKERS = (
    "Skia 字体",
    "Skia 显式字体链",
    "Skia 混合字体",
    "Skia 连结字素",
    "Skia HarfBuzz",
    "Skia 无 GPOS",
    "Skia 无法读取指定字体",
    "Skia 无法实例化指定字体",
    "Skia shaping",
    "Skia 单元内字体分段",
    "矢量轮廓",
    "Qt font face",
)


def _has_missing_outline(node) -> bool:
    """Return True if any shaped cell in ``skia_layout`` lacks vector outlines."""
    if isinstance(node, dict):
        if node.get("outline_available") is False:
            return True
        return any(_has_missing_outline(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_missing_outline(item) for item in node)
    return False


def _is_recoverable_skia_font_error(exc: Exception) -> bool:
    """Return True when ``exc`` is a font/glyph/shaping rejection recoverable inside Skia."""
    from .skia_fonts import SkiaFontNotPreparedError
    if isinstance(exc, SkiaFontNotPreparedError):
        return False
    message = str(exc)
    return any(marker in message for marker in _FONT_FALLBACK_ERROR_MARKERS)


def prepare_skia_scene_ir(
    track,
    style,
    *,
    executable=None,
    missing_fallbacks=None,
    force_safe_fallback: bool = False,
    **options,
):
    from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir

    started_at = time.monotonic()

    def _compile(resolver: SkiaFontResolver):
        with SkiaTextMetricsProcess(executable) as metrics:
            built = build_skia_render_ir(
                track,
                style,
                measure=metrics.measure,
                resolve_font=resolver,
                **options,
            )
        if _has_missing_outline(built.get("skia_layout")):
            raise NativeRendererError("Skia 当前绘制仅支持有矢量轮廓的字体单元")
        apply_resolved_font_faces(built, resolve_face=resolver.resolve_slot_face)
        return built

    if force_safe_fallback:
        attempts = (
            ("in_skia_safe_font_fallback", SkiaFontResolver(allow_notdef_fallback=True, force_safe_fallback=True)),
        )
    else:
        attempts = (
            ("primary", SkiaFontResolver()),
            ("in_skia_notdef_fallback", SkiaFontResolver(allow_notdef_fallback=True)),
            ("in_skia_safe_font_fallback", SkiaFontResolver(allow_notdef_fallback=True, force_safe_fallback=True)),
        )
    last_error: Exception | None = None
    resolver = attempts[0][1]
    ir = None
    for stage_idx, (stage_name, candidate_resolver) in enumerate(attempts):
        resolver = candidate_resolver
        try:
            ir = _compile(resolver)
            if stage_idx > 0:
                _log.warning(
                    "Skia 字体在内部回退阶段 [%s] 恢复成功（未回退旧渲染器）: elapsed=%.0fms prev_err=%s",
                    stage_name,
                    (time.monotonic() - started_at) * 1000.0,
                    last_error,
                )
            break
        except NativeRendererError as exc:
            last_error = exc
            if stage_idx + 1 < len(attempts) and _is_recoverable_skia_font_error(exc):
                _log.warning(
                    "Skia 字体度量/塑形触发内部回退（%s -> %s）: err=%s",
                    stage_name,
                    attempts[stage_idx + 1][0],
                    exc,
                )
                continue
            raise
        except (ValueError, OSError) as exc:
            last_error = exc
            if stage_idx + 1 < len(attempts) and _is_recoverable_skia_font_error(exc):
                _log.warning(
                    "Skia 字体准备触发内部回退（%s -> %s）: err=%s",
                    stage_name,
                    attempts[stage_idx + 1][0],
                    exc,
                )
                continue
            _log.warning(
                "准备 Skia 场景 IR 失败: elapsed=%.0fms executable=%s err_type=%s err=%s",
                (time.monotonic() - started_at) * 1000.0,
                executable,
                type(exc).__name__,
                exc,
            )
            raise NativeRendererError(str(exc)) from exc
    if isinstance(missing_fallbacks, dict):
        missing_fallbacks.update(resolver.missing_fallbacks)
    return ir
