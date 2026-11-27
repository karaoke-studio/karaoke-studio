"""Platform font discovery exports bytes/instance decisions, never layout metrics."""
from contextlib import contextmanager
import hashlib
import os
import sys
import threading
from krok_helper.subtitle_render.engine.text.font_slots import is_emoji_text
from krok_helper.subtitle_render.engine.text.font_weight import resolve_font_instance, prepared_font_instance
from .backend import NativeRendererError
from .skia_font_aliases import (
    gui_font_instance,
    missing_family,
    prepared_alias,
    prepared_missing_fallback,
)


class SkiaFontNotPreparedError(NativeRendererError):
    """A worker needed a font decision the GUI thread has not prepared."""


class SkiaFontResourceMissingError(SkiaFontNotPreparedError):
    """A prepared font snapshot disappeared from disk and must be re-materialized on the GUI thread."""


def body_symbol_family(text):
    """Windows' legacy monochrome slot for single-symbol lyric cells."""
    if sys.platform == 'win32' and len(text) == 1 and is_emoji_text(text):
        return 'Segoe UI Symbol'
    return None


def _on_gui_thread():
    from PyQt6.QtCore import QThread
    from PyQt6.QtGui import QGuiApplication
    app = QGuiApplication.instance()
    return app is None or QThread.currentThread() is app.thread()


def font_instance(family, weight, italic):
    """Qt discovery only on the GUI thread; workers read prepared decisions.

    A registry change bumps the font generation, so a worker that would need
    Qt again fails explicitly; its owner re-prepares on the GUI thread. A
    family missing from Qt's family list first checks a verified localized
    alias (``skia_font_aliases``), and if genuinely missing falls back to a
    GUI-selected installed fallback family so Skia preview/export stays usable.
    """
    if _on_gui_thread():
        return gui_font_instance(family, weight, italic, resolve=resolve_font_instance)
    instance = prepared_font_instance(family, weight, italic)
    if missing_family(instance) and (alias := prepared_alias(family)):
        instance = prepared_font_instance(alias, weight, italic)
    if missing_family(instance) and (fallback := prepared_missing_fallback(family)):
        instance = prepared_font_instance(fallback, weight, italic)
    if instance is None:
        raise SkiaFontNotPreparedError(f'Skia 字体资源未在界面线程准备（字体库可能已变化）：{family}')
    if instance.resource_path and not os.path.isfile(instance.resource_path):
        raise SkiaFontResourceMissingError(
            f'Skia 字体快照文件已丢失，需要重新准备：{family}（{instance.resource_path}）'
        )
    return instance


def resolve_skia_font(style):
    instance = font_instance(style.font_family, style.font_weight, style.italic)
    return _export_instance(instance, style.font_family)


def _file_sha256(path, family=''):
    try:
        with open(path, 'rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    except FileNotFoundError as exc:
        label = f'{family}（{path}）' if family else str(path)
        raise SkiaFontResourceMissingError(
            f'Skia 字体快照文件已丢失，需要重新准备：{label}'
        ) from exc


def _export_instance(instance, family, digest=None):
    if not instance.resource_path:
        raise NativeRendererError(f'无法取得 Skia 字体资源：{family}')
    resource = {'path': instance.resource_path, 'face_index': 0,
                'sha256': digest or _file_sha256(instance.resource_path, family),
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
        self.missing_fallbacks: dict[str, str] = {}

    def _note_missing_fallback(self, requested_family: str, instance) -> None:
        if (
            requested_family
            and instance is not None
            and prepared_missing_fallback(requested_family)
            and instance.family
            and instance.family.casefold() != requested_family.casefold()
        ):
            self.missing_fallbacks[requested_family] = instance.family

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
                digests[path] = _file_sha256(path, family)
            # Axes/simulations/scale are recomputed per instance, never cached.
            return _export_instance(instance, family, digests[path])

    def __call__(self, style):
        instance = font_instance(style.font_family, style.font_weight, style.italic)
        self._note_missing_fallback(style.font_family, instance)
        return self._export(instance, style.font_family)

    def resolve_body_cell_fonts(self, style, text):
        chain = self.resolve_cell_fonts(style, text)
        if family := body_symbol_family(text):
            instance = font_instance(family, style.font_weight, style.italic)
            if instance.resource_path:
                symbol = self._export(instance, family)
                return [symbol, *(candidate for candidate in chain if candidate != symbol)]
        return chain

    def resolve_cell_fonts(self, style, text):
        from PyQt6.QtGui import QGuiApplication
        from krok_helper.subtitle_render.engine.text.font_fallback import (
            prewarm_fallback, lookup_fallback_instance, grapheme_clusters,
        )
        texts = list(dict.fromkeys([text, *grapheme_clusters(text)]))
        instance = font_instance(style.font_family, style.font_weight, style.italic)
        self._note_missing_fallback(style.font_family, instance)
        # A verified localized alias must use that same physical family for
        # fallback discovery and lookup. Asking Qt for the missing spelling
        # again can add unrelated (even unreadable) faces to an otherwise
        # valid explicit chain on CoreText.
        family = instance.family
        if QGuiApplication.instance() is not None and _on_gui_thread():
            prewarm_fallback(family, style.font_weight, style.italic, texts)
        primary = self._export(instance, style.font_family)
        chain = [primary]
        # Preserve the previous whole-cell candidate's priority. Additional
        # physical faces are only consumed if none covers the complete cell.
        for unit in texts:
            fallback = lookup_fallback_instance(family, style.font_weight, style.italic, unit)
            if fallback is None:
                continue
            resource, options = self._export(fallback, fallback.family)
            options = dict(options, size=style.font_size_px * fallback.fallback_scale, scale_x=1)
            if (resource, options) not in chain:
                chain.append((resource, options))
        return chain
