"""Deterministic generated OpenType inputs; no redistributed system fonts."""
from pathlib import Path


def make_font(path: Path, *, a_width=600, family="LinK P05 字体", weight=400,
              ps_name="LinK-P05-Regular", ascent=800, descent=200):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
    widths = {'.notdef': 600, 'space': 250, 'A': a_width, 'W': 800, 'i': 200, 'f': 300,
              'fi': 450, 'a': 500, 'aacute': 500, 'acutecomb': 0, 'cjk': 1000, 'hangul': 1000,
              'woman': 1000, 'laptop': 1000}
    glyphs = {}
    for name, advance in widths.items():
        pen = TTGlyphPen(None)
        if name == 'acutecomb':
            pen.moveTo((-150, 750)); pen.lineTo((-50, 850)); pen.lineTo((50, 850)); pen.lineTo((-50, 750)); pen.closePath()
        elif name != 'space':
            pen.moveTo((50, 0)); pen.lineTo((max(advance - 50, 100), 0))
            pen.lineTo((max(advance - 50, 100), 700)); pen.lineTo((50, 700)); pen.closePath()
        glyphs[name] = pen.glyph()
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(list(widths))
    fb.setupCharacterMap({32: 'space', 65: 'A', 87: 'W', 105: 'i', 102: 'f', 97: 'a', 225: 'aacute',
                         0x301: 'acutecomb', 0x5b57: 'cjk', 0xd55c: 'hangul', 0x1f469: 'woman', 0x1f4bb: 'laptop'})
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({name: (width, -150 if name == 'acutecomb' else 0 if name == 'space' else 50)
                               for name, width in widths.items()})
    fb.setupHorizontalHeader(ascent=ascent, descent=-descent)
    fb.setupNameTable({'familyName': family, 'styleName': 'Regular', 'uniqueFontIdentifier': family,
                       'fullName': family, 'psName': ps_name, 'version': 'Version 1.0'})
    fb.setupOS2(sTypoAscender=ascent, sTypoDescender=-descent, usWinAscent=ascent, usWinDescent=descent, usWeightClass=weight)
    fb.setupPost()
    fb.setupMaxp()
    addOpenTypeFeaturesFromString(fb.font, 'feature liga { sub f i by fi; } liga;')
    fb.font['head'].created = fb.font['head'].modified = 3400000000
    fb.font.recalcTimestamp = False
    fb.font.save(path)
    return path


def make_variable_font(root: Path):
    from fontTools.designspaceLib import AxisDescriptor, DesignSpaceDocument, SourceDescriptor
    from fontTools.varLib import build
    document = DesignSpaceDocument()
    axis = AxisDescriptor(); axis.name = 'Weight'; axis.tag = 'wght'
    axis.minimum = axis.default = 100; axis.maximum = 900
    document.addAxis(axis)
    for weight, width in ((100, 600), (900, 1000)):
        source = SourceDescriptor()
        source.path = str(make_font(root / f'master-{weight}.ttf', a_width=width, weight=weight))
        source.name = f'master-{weight}'; source.familyName = 'LinK P05 字体'; source.styleName = 'Regular'
        source.location = {'Weight': weight}
        if weight == 100:
            source.copyInfo = source.copyLib = source.copyFeatures = True
        document.addSource(source)
    document.write(root / 'fixture.designspace')
    font, _, _ = build(root / 'fixture.designspace')
    font['head'].created = font['head'].modified = 3400000000
    font.recalcTimestamp = False
    output = root / 'variable.ttf'; font.save(output)
    return output
