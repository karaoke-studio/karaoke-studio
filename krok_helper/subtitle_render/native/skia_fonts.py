"""Platform font discovery exports bytes/instance decisions, never layout metrics."""
from contextlib import contextmanager
import hashlib
import threading
from krok_helper.subtitle_render.engine.text.font_weight import resolve_font_instance
from .backend import NativeRendererError


def resolve_skia_font(style):
    instance = resolve_font_instance(style.font_family, style.font_weight, italic=style.italic)
    return _export_instance(instance, style.font_family)


def _file_sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _export_instance(instance, family, digest=None):
    if not instance.resource_path:
        raise NativeRendererError(f'无法取得 Skia 字体资源：{family}')
    resource = {'path': instance.resource_path, 'face_index': 0,
                'sha256': digest or _file_sha256(instance.resource_path),
                'axes': dict(instance.axis_values)}
    return resource, {'synthetic_bold': instance.synthetic_bold,
                      'synthetic_italic': instance.synthetic_italic,
                      'scale_x': instance.fallback_scale}


class SkiaFontResolver:
    """Export prewarmed physical fallback faces, never discover on a worker.

    Qt is only a platform resource discovery adapter. HarfBuzz checks complete
    cell coverage, and the final scene revalidates the same explicit chain.

    Content digests live only inside ``snapshot()``: one canonical preparation
    reads each physical file once, whatever its instances or cells. File stat
    is never an identity, so a later preparation rereads replaced bytes even at
    the same path, size and mtime; a change inside one snapshot keeps the first
    digest and is rejected by native SHA-256 validation rather than mixed.
    """
    allow_mixed_fonts = True

    def __init__(self):
        self._local = threading.local()  # Per preparation thread: {path: sha256}.

    @contextmanager
    def snapshot(self):
        outer = getattr(self._local, 'digests', None) is None
        if outer:
            self._local.digests = {}
        try:
            yield self
        finally:
            if outer:
                self._local.digests = None

    def _export(self, instance, family):
        if not instance.resource_path:
            return _export_instance(instance, family)  # Keep the precise error.
        with self.snapshot():
            digests = self._local.digests
            path = str(instance.resource_path)
            if path not in digests:
                digests[path] = _file_sha256(path)
            # Axes/simulations/scale are recomputed per instance, never cached.
            return _export_instance(instance, family, digests[path])

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
