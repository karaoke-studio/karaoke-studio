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


def make_cff_font(path: Path):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(['.notdef', 'space', 'A'])
    fb.setupCharacterMap({32: 'space', 65: 'A'})
    strings = {}
    for name, width in (('.notdef', 600), ('space', 250), ('A', 600)):
        pen = T2CharStringPen(width, None)
        if name != 'space':
            pen.moveTo((50, 0)); pen.lineTo((550, 0)); pen.lineTo((550, 700)); pen.lineTo((50, 700)); pen.closePath()
        strings[name] = pen.getCharString()
    fb.setupCFF('LinK-P05-CFF', {'FullName': 'LinK P05 CFF', 'FamilyName': 'LinK P05 CFF', 'Weight': 'Regular'}, strings, {})
    fb.setupHorizontalMetrics({'.notdef': (600, 50), 'space': (250, 0), 'A': (600, 50)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({'familyName': 'LinK P05 CFF', 'styleName': 'Regular', 'psName': 'LinK-P05-CFF'})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost(); fb.setupMaxp()
    fb.font['head'].created = fb.font['head'].modified = 3400000000
    fb.font.recalcTimestamp = False
    fb.font.save(path)
    return path


def make_colr_font(path: Path):
    from fontTools.ttLib import TTFont
    from fontTools.colorLib.builder import buildCOLR, buildCPAL
    make_font(path)
    font = TTFont(path, recalcTimestamp=False)
    # Keep a nonempty monochrome base outline: a COLR renderer must use layers,
    # so simply finding a glyf outline cannot count as support for this glyph.
    font['COLR'] = buildCOLR({'woman': [('A', 0)]}, version=0)
    font['CPAL'] = buildCPAL([[(1.0, 0.0, 0.0, 1.0)]])
    font.save(path)
    return path


# Japanese kana fixture: fullwidth letters, small kana, precomposed voiced and
# semi-voiced forms, zero-advance combining U+3099/U+309A with a negative left
# bearing, spacing voiced marks and representative punctuation.
KANA_CODEPOINTS = {
    'kanaL': (0x3042, 0x304B, 0x304D, 0x306F, 0x3084, 0x3046, 0x30AB, 0x30AD, 0x30CF, 0x30A6),
    'kanaS': (0x3041, 0x3083, 0x3085, 0x3087, 0x3063, 0x3096, 0x30A1, 0x30E3, 0x30C3),
    'kanaV': (0x304C, 0x304E, 0x30AC),
    'kanaP': (0x3071, 0x30D1),
    'dakutenc': (0x3099,), 'handakutenc': (0x309A,), 'dakutens': (0x309B,), 'handakutens': (0x309C,),
    'comma': (0x3001,), 'stop': (0x3002,), 'open': (0x300C, 0x300E), 'close': (0x300D, 0x300F),
    'middot': (0x30FB,), 'prolong': (0x30FC,), 'wave': (0x301C,), 'excl': (0xFF01, 0xFF1F),
    'idsp': (0x3000,),
}


def make_kana_font(path: Path, *, gpos=True, family='LinK P05 Kana', ps_name='LinK-P05-Kana'):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
    boxes = {'.notdef': (600, [(50, 0, 550, 700)]), 'space': (250, []), 'A': (600, [(50, 0, 550, 700)]),
             'kanaL': (1000, [(100, 0, 900, 800)]), 'kanaS': (1000, [(250, 0, 750, 500)]),
             'kanaV': (1000, [(100, 0, 820, 800), (840, 760, 960, 900)]),
             'kanaP': (1000, [(100, 0, 820, 800), (860, 780, 960, 880)]),
             'dakutenc': (0, [(-200, 760, -60, 900)]), 'handakutenc': (0, [(-180, 780, -80, 880)]),
             'dakutens': (1000, [(600, 600, 900, 900)]), 'handakutens': (1000, [(650, 650, 850, 850)]),
             'comma': (1000, [(100, 0, 300, 200)]), 'stop': (1000, [(80, 0, 320, 240)]),
             'open': (1000, [(550, 300, 900, 900)]), 'close': (1000, [(100, -100, 450, 500)]),
             'middot': (1000, [(400, 300, 600, 500)]), 'prolong': (1000, [(100, 350, 900, 450)]),
             'wave': (1000, [(100, 300, 900, 500)]), 'excl': (1000, [(420, 0, 580, 800)]), 'idsp': (1000, [])}
    glyphs, metrics = {}, {}
    for name, (advance, rects) in boxes.items():
        pen = TTGlyphPen(None)
        for x0, y0, x1, y1 in rects:
            pen.moveTo((x0, y0)); pen.lineTo((x1, y0)); pen.lineTo((x1, y1)); pen.lineTo((x0, y1)); pen.closePath()
        glyphs[name] = pen.glyph()
        metrics[name] = (advance, min((r[0] for r in rects), default=0))
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(list(boxes))
    cmap = {32: 'space', 65: 'A'}
    for name, codepoints in KANA_CODEPOINTS.items():
        cmap.update({c: name for c in codepoints})
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=880, descent=-120)
    fb.setupNameTable({'familyName': family, 'styleName': 'Regular', 'uniqueFontIdentifier': family,
                       'fullName': family, 'psName': ps_name, 'version': 'Version 1.0'})
    fb.setupOS2(sTypoAscender=880, sTypoDescender=-120, usWinAscent=880, usWinDescent=120, usWeightClass=400)
    fb.setupPost()
    fb.setupMaxp()
    if gpos:
        # Uncomposable base+mark (e.g. U+3046 U+3099, no U+3094 glyph) is placed by GPOS.
        addOpenTypeFeaturesFromString(fb.font, '''
            markClass [dakutenc handakutenc] <anchor 0 0> @voice;
            feature mark { pos base [kanaL kanaS] <anchor 950 0> mark @voice; } mark;
        ''')
    fb.font['head'].created = fb.font['head'].modified = 3400000000
    fb.font.recalcTimestamp = False
    fb.font.save(path)
    return path
