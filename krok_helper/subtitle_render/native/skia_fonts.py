"""Platform font discovery exports bytes/instance decisions, never layout metrics."""
import hashlib
from pathlib import Path
from krok_helper.subtitle_render.engine.text.font_weight import resolve_font_instance
from .backend import NativeRendererError


def resolve_skia_font(style):
    instance = resolve_font_instance(style.font_family, style.font_weight, italic=style.italic)
    return _export_instance(instance, style.font_family)


def _export_instance(instance, family):
    if not instance.resource_path:
        raise NativeRendererError(f'无法取得 Skia 字体资源：{family}')
    resource = {'path': instance.resource_path, 'face_index': 0,
                'sha256': hashlib.sha256(Path(instance.resource_path).read_bytes()).hexdigest(),
                'axes': dict(instance.axis_values)}
    return resource, {'synthetic_bold': instance.synthetic_bold,
                      'synthetic_italic': instance.synthetic_italic,
                      'scale_x': instance.fallback_scale}


class SkiaFontResolver:
    """Export prewarmed physical fallback faces, never discover on a worker.

    Qt is only a platform resource discovery adapter. HarfBuzz checks complete
    cell coverage, and the final scene revalidates the same explicit chain.
    """
    allow_mixed_fonts = True

    def __init__(self):
        self._exports = {}

    def _export(self, instance, family):
        if not instance.resource_path:
            return _export_instance(instance, family)  # Keep the precise error.
        path = Path(instance.resource_path)
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns, instance.axis_values,
               instance.synthetic_bold, instance.synthetic_italic, instance.fallback_scale)
        if key not in self._exports:
            self._exports[key] = _export_instance(instance, family)
        return self._exports[key]

    def __call__(self, style):
        instance = resolve_font_instance(style.font_family, style.font_weight, italic=style.italic)
        return self._export(instance, style.font_family)

    def resolve_cell_fonts(self, style, text):
        from PyQt6.QtCore import QThread
        from PyQt6.QtGui import QGuiApplication
        from krok_helper.subtitle_render.engine.text.font_fallback import (
            prewarm_fallback, lookup_fallback_instance, grapheme_clusters,
        )
        texts = list(dict.fromkeys([text, *grapheme_clusters(text)]))
        app = QGuiApplication.instance()
        if app is not None and QThread.currentThread() is app.thread():
            prewarm_fallback(style.font_family, style.font_weight, style.italic, texts)
        primary = self(style)
        chain = [primary]
        # Preserve the previous whole-cell candidate's priority. Additional
        # physical faces are only consumed if none covers the complete cell.
        for unit in texts:
            fallback = lookup_fallback_instance(style.font_family, style.font_weight, style.italic, unit)
            if fallback is None:
                continue
            resource, options = self._export(fallback, fallback.family)
            options = dict(options, size=style.font_size_px * fallback.fallback_scale, scale_x=1)
            if (resource, options) not in chain:
                chain.append((resource, options))
        return chain
