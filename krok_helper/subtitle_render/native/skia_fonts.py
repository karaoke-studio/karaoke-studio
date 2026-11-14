"""Platform font discovery exports bytes/instance decisions, never layout metrics."""
import hashlib
from pathlib import Path
from krok_helper.subtitle_render.engine.text.font_weight import resolve_font_instance
from .backend import NativeRendererError


def resolve_skia_font(style):
    instance = resolve_font_instance(style.font_family, style.font_weight, italic=style.italic)
    if not instance.resource_path:
        raise NativeRendererError(f'无法取得 Skia 字体资源：{style.font_family}')
    resource = {'path': instance.resource_path, 'face_index': 0,
                'sha256': hashlib.sha256(Path(instance.resource_path).read_bytes()).hexdigest(),
                'axes': dict(instance.axis_values)}
    return resource, {'synthetic_bold': instance.synthetic_bold,
                      'synthetic_italic': instance.synthetic_italic,
                      'scale_x': instance.fallback_scale}
