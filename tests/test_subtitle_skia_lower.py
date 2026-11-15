"""Lower-reading preparation: canonical clocks/fonts, with product gate closed."""
import ast
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_subtitle_skia_protocol import application, binary
from skia_text_fixture import make_font
from krok_helper.subtitle_render.domain.models import Style, SubtitleStyleScheme
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.ruby.skia_lower import prepare_lower_readings
from krok_helper.subtitle_render.engine.text.skia_metrics import measure_track
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


def appearance(**changes):
    return replace(Style(font_family='Main', font_family_latin='Latin', font_size_px=50,
        stroke_width_px=0, stroke2_width_px=0, decoration_kind='none',
        lower_ruby_enabled=True, _lower_romaji_options=(False, False, False, False),
        ruby_font_size_px=24, ruby_latin_font_size_px=32, ruby_latin_font_stretch_pct=125,
        ruby_stroke_width_px=0, ruby_stroke2_enabled=False), **changes)


def line(text, **changes):
    return TimingLine(chars=[TimingChar(t, 200+i*500) for i, t in enumerate(text)],
                      end_ms=200+len(text)*500, **changes)


def unmeasured(track):
    return SimpleNamespace(lines=[None]*len(track.lines), identity='main',
                           snapshot=SimpleNamespace(resolve_intervals=lambda *_: pytest.fail('unexpected clock query')))


def fail(*args, **kwargs):
    pytest.fail('unexpected external capability')


def test_disabled_preparation_does_not_discover_fonts_read_options_or_resolve_timing():
    track = TimingTrack(lines=[line('かな')])
    style = appearance(lower_ruby_enabled=False, _lower_romaji_options=None)
    result = prepare_lower_readings(track, style, [style], unmeasured(track), resolve_font=fail, measure=fail)
    assert result.lines == ((),)
    assert not result.measured.fonts and not result.measured.runs


@pytest.mark.parametrize('options', [None, (), (False,)*3, (1, False, False, False),
                                     (False, False, False, float('nan'))])
def test_worker_requires_frozen_boolean_options(options):
    track = TimingTrack(lines=[line('かな')])
    style = appearance(_lower_romaji_options=options)
    with pytest.raises(ValueError, match='已冻结'):
        prepare_lower_readings(track, style, [style], unmeasured(track), resolve_font=fail, measure=fail)


@pytest.mark.parametrize('mutation', ['vertical', 'right_to_left', 'wipe_reverse'])
def test_unsupported_direction_is_explicit(mutation):
    track = TimingTrack(lines=[line('かな', wipe_reverse=mutation == 'wipe_reverse')])
    style = appearance(**({mutation: True} if mutation != 'wipe_reverse' else {}))
    with pytest.raises(ValueError, match='正向横排'):
        prepare_lower_readings(track, style, [style], unmeasured(track), resolve_font=fail, measure=fail)


def test_preparation_requires_matching_source_style_and_metrics_counts():
    track = TimingTrack(lines=[line('かな')])
    with pytest.raises(ValueError):
        prepare_lower_readings(track, appearance(), [], unmeasured(track), resolve_font=fail, measure=fail)


def test_pure_lower_modules_do_not_import_font_or_settings_adapters():
    root = Path(__file__).resolve().parents[1]/'krok_helper/subtitle_render/engine/ruby'
    for name in ('lower_semantics.py', 'skia_lower.py'):
        tree = ast.parse((root/name).read_text(encoding='utf-8'))
        imports = [n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert not any('PyQt' in i or '.frontend' in i or i.endswith(('.style', '.lower'))
                       for i in imports), name


@pytest.fixture
def prepared_source(tmp_path):
    from fontTools.ttLib import TTFont
    paths = {}
    for family, width in [('Main', 600), ('Latin', 800)]:
        path = make_font(tmp_path/f'{family}.ttf', family=family, a_width=width, ps_name=f'LinK-{family}')
        font = TTFont(path, recalcTimestamp=False)
        for cmap in font['cmap'].tables:
            if cmap.isUnicode():
                cmap.cmap.update({ord(c): 'cjk' for c in '国あいうえおアイウエオかないきゃユーぐ'})
                cmap.cmap.update({ord(c): 'A' for c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-'})
        font.save(path); font.close()
        paths[family] = path
    track = TimingTrack(lines=[line('字かな'), line('字かな'), line('きゃユー')], rubies=[
        RubyAnnotation('字', 'なぐ', reading_parts=['な', '', 'ぐ'], reading_part_ms=[150, 250],
                       pos_start_ms=200, pos_end_ms=700, target_line_index=0,
                       target_char_start=0, target_char_end=1)])
    return track, appearance(), lambda style: ({'path': str(paths[style.font_family])}, {})


def prepare(binary, source, *, on_measure=None):
    track, style, resolve = source
    styles = [style_for_line(style, l) for l in track.lines]
    with SkiaTextMetricsProcess(binary) as process:
        main = measure_track(track, style, styles, resolve_font=resolve, measure=process.measure)
        def measure(fonts, runs):
            if on_measure is not None: on_measure(fonts, runs)
            return process.measure(fonts, runs)
        lower = prepare_lower_readings(track, style, styles, main, resolve_font=resolve, measure=measure)
    return main, lower


def test_lower_canonical_batch_keeps_pauses_ownership_and_main_clock(binary, prepared_source, monkeypatch, tmp_path):
    from krok_helper.subtitle_render.engine.ruby import lower as adapter, style as qt_style, skia_timing
    monkeypatch.setattr(adapter, 'read_sug_romaji_options', fail)
    monkeypatch.setattr(qt_style, 'build_ruby_font_for_text', fail)
    monkeypatch.setattr(skia_timing, 'main_wipe_points', fail)
    track, style, _ = prepared_source
    original = deepcopy((track, style))
    batches = []
    main, result = prepare(binary, prepared_source, on_measure=lambda f, r: batches.append((f, r)))
    assert [[g.effective.reading for g in groups] for groups in result.lines] == [
        ['nagu', 'ka', 'na'], ['ka', 'na'], ['kya', 'yu-']]
    assert [g.indices for g in result.lines[0]] == [(0,), (1,), (2,)]
    assert [g.indices for g in result.lines[2]] == [(0, 1), (2, 3)]
    first = result.lines[0][0]
    assert first.clock.units == tuple('nagu')
    assert first.clock.intervals == ((200, 275), (275, 350), (450, 575), (575, 700))
    assert result.lines[2][1].effective.reading_parts == ['yu', '-']
    assert result.lines[2][1].clock.intervals == ((1200, 1450), (1450, 1700), (1700, 2200))
    assert result.main_identity == main.identity
    assert (track, style) == original
    assert all(main.snapshot.resolve_intervals(l, style) ==
               [(c.start_ms, l.chars[i+1].start_ms if i+1 < len(l.chars) else l.end_ms)
                for i, c in enumerate(l.chars)] for l in track.lines)
    assert len(batches) == 1 and len(batches[0][0]) == 2 and len(batches[0][1]) == 2
    latin = result.measured.runs[first.metrics.run_indices[0]]
    assert latin['size'] == 32 and latin['scale_x'] == 1.25
    # Stable explicit-font sample excludes local paths and their resource keys.
    sample = {'lines': [[{'indices': g.indices, 'reading': g.effective.reading,
                          'units': g.clock.units, 'intervals': g.clock.intervals,
                          'cells': g.metrics.cells, 'layouts': g.metrics.unit_layouts,
                          'baseline': g.metrics.baseline_metrics} for g in groups] for groups in result.lines],
              'runs': [{k: v for k, v in r.items() if k != 'font_ids'} for r in result.measured.runs]}
    (tmp_path/'lower-ruby-preparation.json').write_text(json.dumps(sample, sort_keys=True), encoding='utf-8')


def test_lower_independent_appearance_and_options_invalidate_preparation(binary, prepared_source):
    track, style, resolve = prepared_source
    _, original = prepare(binary, prepared_source)
    changed = replace(style, lower_ruby_interval_px=-3, lower_ruby_gap_px=17)
    _, spaced = prepare(binary, (track, changed, resolve))
    assert [g.clock for l in spaced.lines for g in l] == [g.clock for l in original.lines for g in l]
    assert original.identity != spaced.identity
    assert all(g.style.ruby_interval_px == -3 and g.style.ruby_gap_px == 17 for l in spaced.lines for g in l)
    independent = replace(style, lower_ruby_follow_upper=False,
                          lower_ruby_style={'ruby_latin_font_size_px': 40})
    _, sized = prepare(binary, (track, independent, resolve))
    g = sized.lines[0][0]
    assert sized.measured.runs[g.metrics.run_indices[0]]['size'] == 40
    assert sized.identity != original.identity
    _, uppercase = prepare(binary, (track, replace(style, _lower_romaji_options=(False, False, False, True)), resolve))
    assert uppercase.lines[0][0].effective.reading == 'NAGU'
    assert uppercase.identity != original.identity


def test_lower_roles_inherit_target_style_without_changing_main_baseline(binary, prepared_source):
    track, style, resolve = prepared_source
    first = replace(track.lines[0], chars=[replace(track.lines[0].chars[0], role_label='lead'), *track.lines[0].chars[1:]])
    track = replace(track, lines=[first, *track.lines[1:]])
    style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(ruby_latin_font_size_px=48)})
    main, lower = prepare(binary, (track, style, resolve))
    a, b = lower.lines[0][0], lower.lines[0][1]
    assert lower.measured.runs[a.metrics.run_indices[0]]['size'] == 48
    assert lower.measured.runs[b.metrics.run_indices[0]]['size'] == 32
    assert main.lines[0].baseline_metrics == main.lines[1].baseline_metrics


def test_lower_odd_duration_preserves_gpu_floor_within_generated_parts(binary, prepared_source):
    track, style, resolve = prepared_source
    track = replace(track, lines=[TimingLine(chars=[TimingChar('字', 0)], end_ms=5)], rubies=[
        RubyAnnotation('字', 'な', pos_start_ms=0, pos_end_ms=5, target_line_index=0,
                       target_char_start=0, target_char_end=1)])
    _, result = prepare(binary, (track, style, resolve))
    assert result.lines[0][0].clock.units == ('n', 'a')
    assert result.lines[0][0].effective.reading_parts == ['na']
    # The 5ms generated part uses native duration*j/count, not fallback round.
    assert result.lines[0][0].clock.intervals == ((0, 2), (2, 5))
