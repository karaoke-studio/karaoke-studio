"""Prepare the canonical Skia scene shared by the sidecar and direct preview."""
from .backend import NativeRendererError
from .protocol import apply_resolved_font_faces
from .skia_fonts import SkiaFontResolver
from .skia_text import SkiaTextMetricsProcess


def prepare_skia_scene_ir(track, style, *, executable=None, **options):
    from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir

    try:
        with SkiaTextMetricsProcess(executable) as metrics:
            ir = build_skia_render_ir(track, style, measure=metrics.measure,
                                      resolve_font=SkiaFontResolver(), **options)
        apply_resolved_font_faces(ir)
    except (ValueError, OSError) as exc:
        raise NativeRendererError(str(exc)) from exc
    return ir
