"""Prepare the canonical Skia scene shared by the sidecar and direct preview."""
import logging
import time

from .backend import NativeRendererError
from .protocol import apply_resolved_font_faces
from .skia_fonts import SkiaFontResolver
from .skia_text import SkiaTextMetricsProcess

_log = logging.getLogger(__name__)


def prepare_skia_scene_ir(track, style, *, executable=None, **options):
    from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir

    started_at = time.monotonic()
    try:
        with SkiaTextMetricsProcess(executable) as metrics:
            ir = build_skia_render_ir(track, style, measure=metrics.measure,
                                      resolve_font=SkiaFontResolver(), **options)
        apply_resolved_font_faces(ir)
    except NativeRendererError:
        raise
    except (ValueError, OSError) as exc:
        _log.warning(
            "准备 Skia 场景 IR 失败: elapsed=%.0fms executable=%s err_type=%s err=%s",
            (time.monotonic() - started_at) * 1000.0,
            executable,
            type(exc).__name__,
            exc,
        )
        raise NativeRendererError(str(exc)) from exc
    return ir
