"""Multi-unit vertical reading geometry and its authored clocks."""
from dataclasses import replace
import copy
import json

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_vertical import GPU, application, binary, project, vertical_ruby_source
from test_subtitle_skia_rtl_flow import collect, configure, ir_for, selection
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def with_pause(track):
    return replace(track, rubies=[replace(track.rubies[0], reading_parts=['字', '', '漢'],
                                           reading_part_ms=[200, 500])])


def test_vertical_ruby_units_split_one_span_and_keep_the_pause(binary, project, tmp_path):
    track, style = vertical_ruby_source(project, span=(1, 3), reading='字한')
    ruby = replace(track.rubies[0], reading_parts=['字', '', '한'], reading_part_ms=[200, 500])
    ir = ir_for(binary, (replace(track, rubies=[ruby]), style), dpr=1.5)
    layout = ir['skia_layout']
    assert layout['vertical_ruby_schema'] == 2
    line = layout['lines'][0]
    units = line['ruby_groups'][0]['units']
    assert [u['text'] for u in units] == ['字', '한']
    assert [u['interval_ms'] for u in units] == [[600, 800], [1100, 1400]]
    assert units[1]['baseline_y'] - units[0]['baseline_y'] == pytest.approx(50)
    for unit in units:
        _, top, _, bottom = unit['metrics']['ink']
        assert unit['wipe_top'] == pytest.approx(unit['baseline_y'] + top)
        assert unit['wipe_bottom'] == pytest.approx(unit['baseline_y'] + bottom)
    (tmp_path / 'vertical-multi-ruby-layout.json').write_text(json.dumps({
        'schema': layout['vertical_ruby_schema'],
        'line': line,
        'intervals': ir['track']['lines'][0]['resolved_intervals'],
    }, sort_keys=True), encoding='utf-8')


def test_previous_vertical_ruby_schema_rejects_multi_unit_groups(binary, project):
    ir = ir_for(binary, vertical_ruby_source(project, span=(0, 1), reading='字한'))
    bad = copy.deepcopy(ir)
    bad['skia_layout']['vertical_ruby_schema'] = 1
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [399])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [399], generation=2)
    assert np.array_equal(before[0], after[0])


def ink_bounds(mask):
    y, x = np.nonzero(mask)
    return np.array([x.min(), y.min(), x.max(), y.max()]) if x.size else np.array([])


@GPU
@pytest.mark.parametrize('span, reading, style_name, dpr, pause, no_wipe', [
    ((0, 1), '字漢', 'legacy', 1, False, False),
    ((1, 3), '字漢字', 'n3_1074', 1.5, False, False),
    ((1, 3), '字漢', 'n3_1074', 1.5, True, False),
    ((1, 3), '字漢', 'legacy', 1, True, True),
])
def test_vertical_ruby_multiple_units_match_actual_gpu(binary, project, span, reading, style_name, dpr, pause, no_wipe):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    if 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    track, style = vertical_ruby_source(project, span=span, reading=reading, family='Meiryo')
    track = with_pause(track) if pause else track
    style = replace(style, layout_semantics=style_name, karaoke_anim='no_wipe' if no_wipe else 'none')
    times = (199, 350, 399, 400, 450, 599, 600, 601, 799, 800, 900, 1099, 1100, 1200, 1399, 1400, 2200)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        for t, expected, actual in zip(times, reference, frames, strict=True):
            for after in (False, True):
                def mask(pixels):
                    ruby = pixels[..., 2] > 127
                    return ruby & (pixels[..., 0] > 127) if after else ruby
                a, b = ink_bounds(mask(expected)), ink_bounds(mask(actual))
                if after and a.size != b.size:
                    # A subpixel front may color a single antialiasing row
                    # on one target before it crosses the mask threshold on both.
                    emerging = a if a.size else b
                    assert emerging[3] == emerging[1], (target, t, a, b)
                    continue
                assert a.size == b.size, (target, t, after, a, b)
                if a.size:
                    assert abs(a-b).max() <= 1, (target, t, after, a, b)
