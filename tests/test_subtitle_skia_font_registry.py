"""Application font registry lifecycle through the explicit Skia resources.

One reused ``SkiaFontResolver`` and the *same* track/style objects pass through
Qt application-font registration, removal, same-family replacement and
re-registration, plus localized family names and duplicate-family faces. The
oracle is the deterministic FontTools fixture bytes, never installed fonts.
"""
import copy
from dataclasses import replace
import json

import numpy as np
import pytest
from fontTools.ttLib import TTFont
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_protocol import application, binary, collect, selection, sample
from test_subtitle_skia_ruby_groups import configure, colors
from skia_text_fixture import make_font
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.text.font_capabilities import capabilities_generation
from krok_helper.subtitle_render.native.backend import NativeRendererError
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_font_preparation import iter_prepare_skia_fallbacks
from krok_helper.subtitle_render.native.skia_fonts import SkiaFontResolver
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess

MAIN, RUBY, ROLE = 'LinK Registry Main', 'LinK Registry Ruby', 'LinK Registry Role'
LOCALIZED = '凛K注册本地化'


def advance_of_a(path):
    with TTFont(path) as font:
        return font['hmtx']['A'][0]


def localize(path, name):
    """Add a Japanese (0x411) Windows family record next to the English one."""
    with TTFont(path) as font:
        for name_id in (1, 4, 16):
            if font['name'].getName(name_id, 3, 1, 0x409) is not None:
                font['name'].setName(name, name_id, 3, 1, 0x411)
        font.save(path)
    return path


class Registry:
    """Real Qt application-font registry with explicit cleanup; no cache clears."""

    def __init__(self, root):
        self.root, self.ids = root, {}

    def font(self, name, family, width, **kwargs):
        return make_font(self.root/f'{name}.ttf', family=family, a_width=width,
                         ps_name=kwargs.pop('ps_name', 'LinK-Registry-' + family.split()[-1]), **kwargs)

    def add(self, key, path, *, data=False):
        handle = (QFontDatabase.addApplicationFontFromData(path.read_bytes()) if data
                  else QFontDatabase.addApplicationFont(str(path)))
        assert handle >= 0
        self.ids[key] = handle
        return QFontDatabase.applicationFontFamilies(handle)

    def remove(self, key):
        assert QFontDatabase.removeApplicationFont(self.ids.pop(key))

    def close(self):
        for handle in self.ids.values():
            QFontDatabase.removeApplicationFont(handle)
        self.ids.clear()


@pytest.fixture
def registry(application, tmp_path):
    capabilities_generation()  # The product's first use connects the Qt hook.
    value = Registry(tmp_path)
    try: yield value
    finally: value.close()


def source():
    style = replace(sample()[1], font_family=MAIN, font_family_latin=MAIN, font_size_px=50,
        ruby_font_family=RUBY, ruby_font_family_latin=RUBY, ruby_font_size_px=24,
        ruby_latin_font_size_px=24, ruby_latin_font_stretch_pct=100, lower_ruby_enabled=False,
        stroke_width_px=0, stroke2_width_px=0, ruby_stroke_width_px=0, ruby_stroke2_enabled=False,
        karaoke_colors=colors('#FF0000', '#00FF00'), ruby_colors_follow_main=False,
        ruby_karaoke_colors=colors('#FF0000', '#00FF00'),
        custom_style_schemes={'lead': SubtitleStyleScheme(font_family=ROLE, font_family_latin=ROLE)})
    line = TimingLine(chars=[TimingChar(c, 200+i*400, role_label='lead' if i == 2 else None)
                             for i, c in enumerate('A字A')], end_ms=1400, track_line_index=0,
                      display_start_override_ms=0, display_end_override_ms=3000)
    ruby = RubyAnnotation('字', 'AW', pos_start_ms=600, pos_end_ms=1000, target_line_index=0,
                          target_char_start=1, target_char_end=2)
    return TimingTrack(lines=[line], rubies=[ruby]), style


def prepare(binary, track, style, resolver, *, diagnostic_dir=None, **kwargs):
    list(iter_prepare_skia_fallbacks(track, style))  # GUI-thread prewarm, as in the product.
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts, runs):
            try:
                return process.measure(fonts, runs)
            except NativeRendererError:
                if diagnostic_dir is not None:
                    import shutil
                    destination = diagnostic_dir/'failed-fonts'
                    destination.mkdir(exist_ok=True)
                    (destination/'request.json').write_text(json.dumps({'fonts': fonts, 'runs': runs}), encoding='utf-8')
                    for index, resource in enumerate(fonts.values()):
                        shutil.copyfile(resource['path'], destination/f'{index}.otf')
                raise
        ir = build_skia_render_ir(track, style, resolve_font=resolver, measure=measure,
                                  width=640, height=360, fps=60, **kwargs)
    apply_resolved_font_faces(ir)
    return ir


def used_fonts(ir):
    """Map slot -> exported digest of the cells that actually use it."""
    layout = ir['skia_layout']
    line = layout['lines'][0]
    def digest(cell):
        run = layout['runs'][cell['run_index']]
        return layout['fonts'][run['font_ids'][cell['metrics']['selected_font']]]['sha256']
    return {'main': digest(line['cells'][0]), 'cjk': digest(line['cells'][1]), 'role': digest(line['cells'][2]),
            'ruby': {digest(u) for g in line['ruby_groups'] for u in g['units']}}


def drawing_geometry(value):
    """Run indices can differ when unused platform fallback chains differ.

    Keep every actual glyph/metric/clock/coordinate field. Used resource
    identities are compared separately by ``used_fonts``.
    """
    if isinstance(value, dict):
        return {key: drawing_geometry(item) for key, item in value.items() if key != 'run_index'}
    if isinstance(value, list):
        return [drawing_geometry(item) for item in value]
    return value


def frames(binary, ir, target):
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        return collect(process, [150, 700, 2000])[0]


@pytest.mark.parametrize('data', [False, True], ids=['file', 'data'])
def test_reused_resolver_follows_register_replace_remove_and_reregister(binary, registry, data, tmp_path):
    main_a, main_b = registry.font('main-a', MAIN, 600), registry.font('main-b', MAIN, 900)
    ruby_a, ruby_b = registry.font('ruby-a', RUBY, 500), registry.font('ruby-b', RUBY, 700)
    role = registry.font('role', ROLE, 760)
    track, style = source()
    resolver = SkiaFontResolver()
    for key, path in (('main', main_a), ('ruby', ruby_a), ('role', role)):
        registry.add(key, path, data=data)
    first = prepare(binary, track, style, resolver)
    fonts = used_fonts(first)
    # Exported snapshots are standalone faces of the registered bytes.
    paths = {f['sha256']: f['path'] for f in first['skia_layout']['fonts'].values()}
    assert advance_of_a(paths[fonts['main']]) == 600 and advance_of_a(paths[fonts['role']]) == 760
    assert {advance_of_a(paths[d]) for d in fonts['ruby']} == {500}
    assert fonts['main'] == fonts['cjk'] and fonts['main'] != fonts['role']

    # Same family, different file: main changes; ruby/role identities do not.
    generation = capabilities_generation()
    registry.remove('main'); registry.add('main', main_b, data=data)
    assert capabilities_generation() > generation  # No explicit cache clear.
    replaced = prepare(binary, track, style, resolver)
    after = used_fonts(replaced)
    paths = {f['sha256']: f['path'] for f in replaced['skia_layout']['fonts'].values()}
    assert advance_of_a(paths[after['main']]) == 900
    assert after['role'] == fonts['role'] and after['ruby'] == fonts['ruby']
    assert replaced['skia_layout']['metrics_identity'] != first['skia_layout']['metrics_identity']
    cells = lambda ir: ir['skia_layout']['lines'][0]['cells']
    assert cells(replaced)[0]['metrics']['advance'] == pytest.approx(cells(first)[0]['metrics']['advance']*1.5)
    assert cells(replaced)[2]['origin_x'] != cells(first)[2]['origin_x']

    # Annotation replacement only changes annotation resources and geometry.
    registry.remove('ruby'); registry.add('ruby', ruby_b, data=data)
    reruby = prepare(binary, track, style, resolver)
    assert used_fonts(reruby)['ruby'] != after['ruby'] and used_fonts(reruby)['main'] == after['main']
    units = lambda ir: [u for g in ir['skia_layout']['lines'][0]['ruby_groups'] for u in g['units']]
    assert [u['metrics']['advance'] for u in units(reruby)] != [u['metrics']['advance'] for u in units(replaced)]

    # Restoring every original registration reproduces the first IR exactly:
    # content addressing, not stale per-object caches, owns the identity.
    for key, path in (('main', main_a), ('ruby', ruby_a)):
        registry.remove(key); registry.add(key, path, data=data)
    restored = prepare(binary, track, style, resolver)
    assert restored == first


@pytest.mark.parametrize('slot', ['main', 'ruby', 'role'])
def test_removed_family_falls_back_to_installed_font_and_recovers(binary, registry, slot):
    for key, path in (('main', registry.font('main', MAIN, 600)), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    track, style = source()
    resolver = SkiaFontResolver()
    valid = prepare(binary, track, style, resolver)
    registry.remove(slot)
    family = {'main': MAIN, 'ruby': RUBY, 'role': ROLE}[slot]
    fallback_resolver = SkiaFontResolver()
    fallback_ir = prepare(binary, track, style, fallback_resolver)
    assert family in fallback_resolver.missing_fallbacks
    assert fallback_resolver.missing_fallbacks[family] != family
    # 缺失字体回退后仍可由 Skia 正常渲染出非空帧，不致 Skia 崩溃。
    rendered = frames(binary, fallback_ir, 'raster')
    assert rendered[1][..., 3].max() > 0
    registry.add(slot, registry.root/f'{slot}.ttf')
    assert prepare(binary, track, style, resolver) == valid


def test_localized_and_english_family_names_select_the_same_bytes(binary, registry):
    path = localize(registry.font('localized', MAIN, 640), LOCALIZED)
    # Windows lists both names; fontconfig lists only the English family, but
    # Qt's matcher still resolves the Japanese record (a verified alias).
    assert MAIN in registry.add('main', path)
    for key, extra in (('ruby', registry.font('ruby', RUBY, 500)), ('role', registry.font('role', ROLE, 760))):
        registry.add(key, extra)
    track, style = source()
    resolver = SkiaFontResolver()
    # Resolve the localized spelling cold: no earlier English preparation may
    # be required to seed its resource identity.
    localized = prepare(binary, track, replace(style, font_family=LOCALIZED, font_family_latin=LOCALIZED), resolver,
                        diagnostic_dir=registry.root)
    english = prepare(binary, track, style, resolver)
    assert used_fonts(localized) == used_fonts(english)
    assert drawing_geometry(localized['skia_layout']['lines']) == drawing_geometry(english['skia_layout']['lines'])
    # Workers read the GUI-verified alias; they never query Qt themselves.
    worker = _prepare_on_worker(binary, track, replace(style, font_family=LOCALIZED, font_family_latin=LOCALIZED),
                                resolver)
    assert 'ir' in worker and used_fonts(worker['ir']) == used_fonts(english)
    # Removing the font invalidates the verified alias and falls back instead of crashing.
    registry.remove('main')
    removed_resolver = SkiaFontResolver()
    fallback_ir = prepare(binary, track, replace(style, font_family=LOCALIZED, font_family_latin=LOCALIZED),
                          removed_resolver)
    assert LOCALIZED in removed_resolver.missing_fallbacks
    assert used_fonts(fallback_ir)['main'] != used_fonts(english)['main']


def test_duplicate_family_faces_select_physical_weights(binary, registry):
    regular = registry.font('dup-regular', MAIN, 610, weight=400)
    bold = registry.font('dup-bold', MAIN, 910, weight=700, ps_name='LinK-Registry-Main-Bold')
    with TTFont(bold) as font:
        # CoreText reads the Mac records too. Keep every existing platform's
        # style/full/unique names consistent with the physical bold face.
        for record in list(font['name'].names):
            values = {2: 'Bold', 3: MAIN + ' Bold', 4: MAIN + ' Bold'}
            if record.nameID in values:
                font['name'].setName(values[record.nameID], record.nameID,
                                     record.platformID, record.platEncID, record.langID)
        font['OS/2'].fsSelection = (font['OS/2'].fsSelection & ~0x40) | 0x20
        font['head'].macStyle |= 1
        font.save(bold)
    for key, path in (('regular', regular), ('bold', bold), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    track, style = source()
    resolver = SkiaFontResolver()
    results = {}
    for weight in (400, 700):
        ir = prepare(binary, track, replace(style, font_weight=weight), resolver)
        sha = used_fonts(ir)['main']
        paths = {f['sha256']: f for f in ir['skia_layout']['fonts'].values()}
        run = ir['skia_layout']['runs'][ir['skia_layout']['lines'][0]['cells'][0]['run_index']]
        results[weight] = (advance_of_a(paths[sha]['path']), run.get('synthetic_bold', False), sha)
    assert results[400][:2] == (610, False) and results[700][:2] == (910, False)
    # Removing the bold face is a different physical decision, not stale bytes.
    registry.remove('bold')
    ir = prepare(binary, track, replace(style, font_weight=700), resolver)
    sha = used_fonts(ir)['main']
    assert sha == results[400][2]


def test_gpu_and_raster_draw_identical_geometry_after_replacement(binary, registry):
    for key, path in (('main', registry.font('main-a', MAIN, 600)), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    track, style = source()
    resolver = SkiaFontResolver()
    before = prepare(binary, track, style, resolver, dpr=1.5)
    registry.remove('main'); registry.add('main', registry.font('main-b', MAIN, 900))
    after = prepare(binary, track, style, resolver, dpr=1.5)
    outputs = {(name, target): frames(binary, ir, target)
               for name, ir in (('before', before), ('after', after)) for target in ('gpu', 'raster')}
    for name in ('before', 'after'):
        for a, b in zip(outputs[name, 'gpu'], outputs[name, 'raster'], strict=True):
            def premult(p):
                p = p.astype(float); p[..., :3] *= p[..., 3:] / 255
                return p
            delta = abs(premult(a)-premult(b))
            assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003
    width = lambda p: np.flatnonzero(p[..., 3].any(axis=0))
    assert np.ptp(width(outputs['after', 'gpu'][2])) > np.ptp(width(outputs['before', 'gpu'][2]))


def _prepare_on_worker(binary, track, style, resolver):
    import threading
    out = {}
    def body():
        try:
            with SkiaTextMetricsProcess(binary) as process:
                out['ir'] = build_skia_render_ir(track, style, resolve_font=resolver, measure=process.measure,
                                                 width=640, height=360, fps=60)
        except Exception as exc:  # noqa: BLE001 - reported to the test thread
            out['error'] = exc
    worker = threading.Thread(target=body, name='skia-registry-worker')
    worker.start(); worker.join(60)
    assert not worker.is_alive()
    return out


def test_worker_never_discovers_fonts_after_a_registry_change(binary, registry, monkeypatch):
    """Workers read GUI-prepared resources; a newer generation must be re-prepared."""
    import threading
    from krok_helper.subtitle_render.engine.text import font_capabilities, font_resource, font_weight
    for key, path in (('main', registry.font('main-a', MAIN, 600)), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    track, style = source()
    resolver = SkiaFontResolver()
    discovery = []
    def watch(module, name):
        real = getattr(module, name)
        def watched(first, *args, **kwargs):
            discovery.append((name, threading.current_thread() is threading.main_thread()))
            return real(first, *args, **kwargs)
        monkeypatch.setattr(module, name, watched)
    # Every Qt font discovery/snapshot entry point used by the preparation.
    watch(font_weight, 'get_capabilities'); watch(font_capabilities, 'get_capabilities')
    watch(font_resource, 'snapshot_raw_font')
    list(iter_prepare_skia_fallbacks(track, style))
    prepared = _prepare_on_worker(binary, track, style, resolver)
    assert 'ir' in prepared and all(gui for _, gui in discovery)
    registry.remove('main'); registry.add('main', registry.font('main-b', MAIN, 900))
    discovery.clear()
    stale = _prepare_on_worker(binary, track, style, resolver)
    assert discovery == []  # No Qt font discovery off the GUI thread.
    assert isinstance(stale.get('error'), NativeRendererError) and '界面线程' in str(stale['error'])
    list(iter_prepare_skia_fallbacks(track, style))  # GUI re-preparation, as the owners do.
    fresh = _prepare_on_worker(binary, track, style, resolver)
    assert 'ir' in fresh and all(gui for _, gui in discovery)
    assert used_fonts(fresh['ir'])['main'] != used_fonts(prepared['ir'])['main']


def test_skia_preview_reprepares_identical_state_after_registry_change(binary, registry, monkeypatch):
    import time
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.engine.text.font_preparation import prepare_fonts
    from krok_helper.subtitle_render.frontend.preview.skia_async import SkiaAsyncSubtitleRenderer
    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    for key, path in (('main', registry.font('main-a', MAIN, 600)), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    track, style = source()
    prepare_fonts(track, style, [])
    renderer = SkiaAsyncSubtitleRenderer(640, 360, gpu_enabled=False)
    images, errors = [], []
    renderer.frame_ready.connect(lambda image, t: images.append(image))
    renderer.renderError.connect(errors.append)
    def wait(count):
        deadline = time.monotonic() + 30
        while len(images) < count and not errors and time.monotonic() < deadline:
            QApplication.processEvents(); time.sleep(0.01)
        assert not errors, errors
        assert len(images) >= count
    try:
        renderer.set_state(track, style)
        renderer.request(2000)
        wait(1)
        registry.remove('main'); registry.add('main', registry.font('main-b', MAIN, 900))
        wait(2)  # Same state objects, same time: the registry change alone redraws.
    finally:
        renderer.stop()
    width = lambda image: np.ptp(np.flatnonzero(np.frombuffer(image.constBits().asstring(image.sizeInBytes()),
        np.uint8).reshape(image.height(), image.bytesPerLine()//4, 4)[:, :image.width(), 3].any(axis=0)))
    assert width(images[-1]) > width(images[0])


@pytest.mark.parametrize('registry_changed', [True, False], ids=['superseded', 'genuine'])
def test_preview_suppresses_only_superseded_not_prepared_errors(binary, registry, monkeypatch, registry_changed):
    import time
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.engine.text.font_capabilities import clear_capabilities_cache
    from krok_helper.subtitle_render.engine.text.font_preparation import prepare_fonts
    from krok_helper.subtitle_render.frontend.preview import skia_async
    from krok_helper.subtitle_render.native.skia_fonts import SkiaFontNotPreparedError
    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    for key, path in (('main', registry.font('main-a', MAIN, 600)), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    track, style = source()
    prepare_fonts(track, style, [])
    real = skia_async.SkiaRendererProcess.configure
    calls = []
    def configure(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            if registry_changed:
                clear_capabilities_cache()  # Generation moves without a GUI preparation.
            raise SkiaFontNotPreparedError('Skia 字体资源未在界面线程准备（测试）')
        return real(self, *args, **kwargs)
    monkeypatch.setattr(skia_async.SkiaRendererProcess, 'configure', configure)
    renderer = skia_async.SkiaAsyncSubtitleRenderer(640, 360, gpu_enabled=False)
    images, errors = [], []
    renderer.frame_ready.connect(lambda image, t: images.append(image))
    renderer.renderError.connect(errors.append)
    def pump(until, seconds=20):
        deadline = time.monotonic() + seconds
        while not until() and time.monotonic() < deadline:
            QApplication.processEvents(); time.sleep(0.01)
    try:
        renderer.set_state(track, style)
        renderer.request(700)
        pump(lambda: calls and (errors or images), 3)
        pump(lambda: False, 0.3)
        if registry_changed:
            assert not errors and not images
            renderer._reprepare_fonts()  # What the fontDatabaseChanged hook schedules.
            pump(lambda: images)
            assert images and not errors and len(calls) == 2
        else:
            assert errors and '界面线程' in errors[0] and not images
    finally:
        renderer.stop()


def test_unverified_platform_substitute_is_not_an_alias_and_uses_missing_fallback(binary, registry, monkeypatch):
    """verify_alias stays strict; missing fonts use explicit fallback (and fail only if no fallback exists)."""
    from krok_helper.subtitle_render.native import skia_font_aliases
    for key, path in (('main', registry.font('main', MAIN, 600)), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    absent = 'LinK Registry Absent 9'
    assert skia_font_aliases.verify_alias(absent) is None
    track, style = source()
    resolver = SkiaFontResolver()
    fallback_ir = prepare(binary, track, replace(style, font_family=absent, font_family_latin=absent), resolver)
    assert absent in resolver.missing_fallbacks
    assert frames(binary, fallback_ir, 'raster')[1][..., 3].max() > 0
    # When no system fallback is available at all, fail explicitly with the requested family name.
    monkeypatch.setattr(skia_font_aliases, 'resolve_missing_fallback', lambda *a, **k: None)
    monkeypatch.setattr(skia_font_aliases, 'prepared_missing_fallback', lambda *a, **k: None)
    with pytest.raises(NativeRendererError, match=absent):
        prepare(binary, track, replace(style, font_family=absent, font_family_latin=absent), SkiaFontResolver())


def test_family_name_records_cover_languages_and_platforms(tmp_path):
    from krok_helper.subtitle_render.native.skia_font_aliases import family_names
    path = localize(make_font(tmp_path/'names.ttf', family=MAIN), LOCALIZED)
    with TTFont(path) as font:
        font['name'].setName('LinK Mac Family', 16, 1, 0, 0)
        font.save(path)
    with TTFont(path) as font:
        table = font['name'].compile(font)
    assert {MAIN, LOCALIZED, 'LinK Mac Family'} <= family_names(table)
    assert family_names(b'') == set() and family_names(table[:5]) == set()


def test_unlisted_locale_record_resolves_to_its_own_bytes_or_fails_explicitly(binary, registry):
    """Windows Qt only lists user/en names and substitutes Tahoma for a Korean
    record; fontconfig matches it. Either way, never an unrelated face."""
    korean = '링크등록별칭'
    path = registry.font('korean', MAIN, 640)
    with TTFont(path) as font:
        font['name'].setName(korean, 1, 3, 1, 0x412)
        font.save(path)
    registry.add('main', path)
    for key, extra in (('ruby', registry.font('ruby', RUBY, 500)), ('role', registry.font('role', ROLE, 760))):
        registry.add(key, extra)
    track, style = source()
    resolver = SkiaFontResolver()
    english = prepare(binary, track, style, resolver)
    try:
        aliased = prepare(binary, track, replace(style, font_family=korean, font_family_latin=korean), resolver,
                          diagnostic_dir=registry.root)
    except NativeRendererError as exc:
        assert korean in str(exc)
    else:
        assert used_fonts(aliased) == used_fonts(english)
        assert drawing_geometry(aliased['skia_layout']['lines']) == drawing_geometry(english['skia_layout']['lines'])


def test_registry_change_drops_an_inflight_frame_before_gui_repreparation(binary, registry, monkeypatch):
    import threading
    import time
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.engine.text.font_preparation import prepare_fonts
    from krok_helper.subtitle_render.frontend.preview import skia_async
    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    for key, path in (('main', registry.font('main-a', MAIN, 600)), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    track, style = source()
    prepare_fonts(track, style, [])
    reached, release, idle = threading.Event(), threading.Event(), threading.Event()
    real = skia_async.SkiaRendererProcess.read_event
    def read_event(self):
        event = real(self)
        if event.get('event') == 'range_done' and event.get('generation') == 1:
            reached.set()
            assert release.wait(10)
        return event
    monkeypatch.setattr(skia_async.SkiaRendererProcess, 'read_event', read_event)
    renderer = skia_async.SkiaAsyncSubtitleRenderer(640, 360, gpu_enabled=False)
    # A direct connection records emission before the GUI processes its timer.
    images, errors = [], []
    renderer.frame_ready.connect(lambda image, t: images.append(image), Qt.ConnectionType.DirectConnection)
    renderer.renderError.connect(errors.append)
    wait_for = renderer._condition.wait_for
    def waiting(predicate):
        if reached.is_set():
            idle.set()
        return wait_for(predicate)
    monkeypatch.setattr(renderer._condition, 'wait_for', waiting)
    try:
        renderer.set_state(track, style)
        renderer.request(2000)
        assert reached.wait(15)
        registry.remove('main')
        registry.add('main', registry.font('main-b', MAIN, 900))
        assert renderer._font_change_pending
        release.set()
        assert idle.wait(10)
        assert not images  # Old bytes must not be published during the GUI preparation gap.
        deadline = time.monotonic() + 20
        while not images and not errors and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(.01)
        assert images and not errors
    finally:
        release.set()
        renderer.stop()


def test_preview_falls_back_on_removed_font_then_recovers_after_reregistration(binary, registry, monkeypatch):
    import time
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.engine.text.font_preparation import prepare_fonts
    from krok_helper.subtitle_render.frontend.preview.skia_async import SkiaAsyncSubtitleRenderer
    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    main = registry.font('main', MAIN, 600)
    for key, path in (('main', main), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    track, style = source()
    prepare_fonts(track, style, [])
    renderer = SkiaAsyncSubtitleRenderer(640, 360, gpu_enabled=False)
    images, errors = [], []
    renderer.frame_ready.connect(lambda image, t: images.append(image))
    renderer.renderError.connect(errors.append)
    def pump(predicate):
        deadline = time.monotonic() + 20
        while not predicate() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(.01)
        assert predicate()
    try:
        renderer.set_state(track, style)
        renderer.request(700)
        pump(lambda: bool(images or errors))
        assert images and not errors
        first_bytes = images[0].constBits().asstring(images[0].sizeInBytes())
        registry.remove('main')
        pump(lambda: len(images) >= 2 or bool(errors))
        assert not errors and len(images) >= 2
        fallback_bytes = images[-1].constBits().asstring(images[-1].sizeInBytes())
        assert fallback_bytes != first_bytes
        count_before = len(images)
        registry.add('main', main)
        pump(lambda: len(images) > count_before)
        assert not errors
        restored_bytes = images[-1].constBits().asstring(images[-1].sizeInBytes())
        assert restored_bytes == first_bytes
    finally:
        renderer.stop()


def test_verified_alias_uses_its_physical_family_for_fallback_preparation_and_lookup(registry, monkeypatch):
    from krok_helper.subtitle_render.native import skia_fonts, skia_font_aliases
    from krok_helper.subtitle_render.engine.text import font_fallback
    for key, path in (('main', registry.font('main', MAIN, 600)), ('ruby', registry.font('ruby', RUBY, 500)),
                      ('role', registry.font('role', ROLE, 760))):
        registry.add(key, path)
    physical = skia_font_aliases.gui_font_instance(MAIN, 400, False)
    alias = 'LinK Verified Alias Boundary'
    resolve = skia_fonts.font_instance
    gui_resolve = skia_font_aliases.gui_font_instance
    monkeypatch.setattr(skia_fonts, 'font_instance', lambda family, weight, italic:
                        physical if family == alias else resolve(family, weight, italic))
    monkeypatch.setattr(skia_font_aliases, 'gui_font_instance', lambda family, weight, italic:
                        physical if family == alias else gui_resolve(family, weight, italic))
    prewarm, lookup = font_fallback.prewarm_fallback, font_fallback.lookup_fallback_instance
    prepared, consumed = [], []
    def warm(family, *args):
        prepared.append(family)
        return prewarm(family, *args)
    def find(family, *args):
        consumed.append(family)
        return lookup(family, *args)
    monkeypatch.setattr(font_fallback, 'prewarm_fallback', warm)
    monkeypatch.setattr(font_fallback, 'lookup_fallback_instance', find)
    track, style = source()
    style = replace(style, font_family=alias, font_family_latin=alias)
    list(iter_prepare_skia_fallbacks(track, style))
    assert physical.family in prepared and alias not in prepared
    prepared.clear()
    SkiaFontResolver().resolve_cell_fonts(style, 'A字')
    assert prepared == [physical.family] and set(consumed) == {physical.family}


def test_product_export_with_missing_font_falls_back_within_skia(binary, monkeypatch, tmp_path):
    """导出工程包含缺失字体时，Skia 应回退到系统字体继续导出并记日志，而非中断或退回旧渲染器。"""
    from krok_helper.subtitle_render.domain.background import BackgroundSource
    from krok_helper.subtitle_render.engine import renderer
    from krok_helper.subtitle_render.engine.export.render_job import RenderJob
    from krok_helper.subtitle_render.engine.text.font_preparation import prepare_fonts
    from test_subtitle_skia_protocol import export_ffmpeg

    ffmpeg = export_ffmpeg(tmp_path)
    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    monkeypatch.setattr(renderer, 'paint_frame', lambda *a, **k: pytest.fail('subtitle QPainter export was called'))
    monkeypatch.setattr(renderer, 'paint_frame_to_painter', lambda *a, **k: pytest.fail('subtitle QPainter export was called'))

    missing = 'LinK Nonexistent Missing Export Font 42'
    track, style = sample()
    style = replace(style, font_family=missing, font_family_latin=missing)
    prepare_fonts(track, style, [], use_skia_backend=True)
    job = RenderJob(
        track, style, tmp_path / 'unused', tmp_path / 'missing-font.mp4',
        width=640, height=360, background_source=BackgroundSource(kind='solid', color='#183048'),
        fps=30, duration_ms=1000, include_audio=False, gpu_export_enabled=False, preset='ultrafast',
        use_skia_backend=True,
    )
    logs = []
    output = renderer.render_subtitle_video(job, ffmpeg_dir=ffmpeg.parent, logger=logs.append)
    assert output.is_file() and output.stat().st_size > 1000
    assert any('Skia 字幕后端' in line for line in logs)
    assert any('Skia 字体回退' in line and missing in line for line in logs)
    assert not any('Skia 字幕导出失败' in line for line in logs)
