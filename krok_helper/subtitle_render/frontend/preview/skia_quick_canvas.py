"""Opaque Qt Quick canvas for direct Skia preview and external video output."""
from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtQuick import QQuickItem
from PyQt6.QtQuickWidgets import QQuickWidget

from krok_helper.subtitle_render.domain.background import BackgroundSource, background_sequence_frame_path


class SkiaQuickCanvas(QQuickWidget):
    scene_prepared = pyqtSignal(object, bool, str)
    frame_submitted = pyqtSignal(object, object, int)

    def __init__(self, qml_directory: Path, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.engine().addImportPath(str(qml_directory))
        self.setSource(QUrl.fromLocalFile(str(Path(__file__).with_suffix('.qml'))))
        self._root = self.rootObject()
        if self._root is None:
            errors = '\n'.join(error.toString() for error in self.errors())
            raise RuntimeError(f'无法加载 Skia 直画预览\n{errors}')
        self._root.scenePrepared.connect(self.scene_prepared.emit)
        self._root.frameSubmitted.connect(self.frame_submitted.emit)
        self.renderer = self._root.findChild(QQuickItem, 'subtitleRenderer')
        self.video_output = self._root.findChild(QQuickItem, 'videoOutput')
        self._background = BackgroundSource()
        self._time_ms = 0

    def set_output_size(self, width: int, height: int) -> None:
        self._root.setProperty('outputWidth', max(1, width))
        self._root.setProperty('outputHeight', max(1, height))

    def set_background_source(self, source: BackgroundSource) -> None:
        self._background = source
        self._root.setProperty('backgroundKind', source.kind)
        color = QColor(source.color)
        self._root.setProperty('backgroundColor', color if color.isValid() else QColor(Qt.GlobalColor.black))
        self._root.setProperty('containImage', source.image_fit == 'contain')
        self._refresh_image()

    def _refresh_image(self) -> None:
        source = self._background
        if source.kind == 'image_sequence':
            path = background_sequence_frame_path(source, self._time_ms)
        else:
            path = Path(source.path) if source.kind == 'image' and source.path else None
        url = QUrl.fromLocalFile(str(path)) if path is not None else QUrl()
        if self._root.property('backgroundImage') != url:
            self._root.setProperty('backgroundImage', url)

    def configure_scene(self, ir: dict, revision: int) -> bool:
        return self.configure_scene_bytes(json.dumps(ir, ensure_ascii=False, allow_nan=False).encode('utf-8'), revision)

    def configure_scene_bytes(self, data: bytes, revision: int) -> bool:
        return bool(self._root.configureSubtitle(QByteArray(data), revision))

    def reserve_scene(self, revision: int) -> bool:
        return bool(self._root.reserveSubtitle(revision))

    def clear_scene(self, revision: int) -> bool:
        return bool(self._root.clearSubtitle(revision))

    def request_frame(self, revision: int, generation: int, time_ms: int) -> bool:
        accepted = self._root.requestSubtitleFrame(revision, generation, time_ms)
        if accepted:
            self._time_ms = time_ms
            if self._background.kind == 'image_sequence':
                self._refresh_image()
        return bool(accepted)
