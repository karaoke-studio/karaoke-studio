"""Skia part-local marks, never permission to join authored timing units."""
import unicodedata


def starts_with_mark(unit):
    return bool(unit) and unicodedata.category(unit[0]).startswith('M')


def supported_standalone_mark(unit):
    if len(unit) != 1 or unicodedata.category(unit) != 'Mn':
        return False
    code = ord(unit)
    # Exclude CGJ and IPA double-base ties; neither is an independent accent.
    return ((0x300 <= code <= 0x36f and code != 0x34f and not 0x35c <= code <= 0x362)
            or code in (0x3099, 0x309a))


def validate_mark_metrics(unit, metrics):
    if not supported_standalone_mark(unit):
        raise ValueError('Skia 注音 part 的独立组合标记尚未完成 GPU 字形兼容')
    glyphs = metrics.get('glyphs', [])
    if (len(glyphs) != 1 or glyphs[0]['x'] != 0 or glyphs[0]['y'] != 0
            or metrics.get('selected_font', -1) < 0):
        raise ValueError('Skia 独立组合标记必须是单字体原位字形，不能插入占位圆圈')
