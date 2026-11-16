"""Explicit final ruby groups execute on the common GPU/Raster draw core."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np
import pytest

from test_subtitle_skia_ruby_pages import application, binary, project, mixed, annotated, pages, prepare
from test_subtitle_skia_protocol import collect, selection
from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine
from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
from krok_helper.subtitle_render.engine.render.skia_ruby_ir import prepared_ruby_layout
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def colors(before, after):
    return KaraokeColors(before=KaraokeColorState(text=PaintFill(color=before)),
                         after=KaraokeColorState(text=PaintFill(color=after)))


@pytest.fixture
def reading(pages):
    track, style = pages
    line = TimingLine(chars=[TimingChar('字', 300), TimingChar('字', 900)], end_ms=2100,
                      track_line_index=0, display_start_override_ms=0, display_end_override_ms=5000)
    ruby = replace(track.rubies[0], kanji='字字', reading='AA', pos_start_ms=300, pos_end_ms=2100,
                   target_char_end=2, reading_parts=['A', '', 'A'], reading_part_ms=[600, 1100])
    style = replace(style, dual_line_layout=False, line_horizontal_layout='center', smart_horizontal='none',
                    karaoke_colors=colors('#00000000', '#00000000'), ruby_colors_follow_main=False,
                    ruby_karaoke_colors=colors('#80FF0000', '#8000FF00'))
    return replace(track, lines=[line], rubies=[ruby]), style


def ir_for(binary, source, *, roll_shift_ms=0, dpr=1):
    track, style = source
    prepared = prepare(binary, track, style)
    # Isolate final-group execution from product source compilation. The
    # product upper-ruby tests separately exercise the retained annotations.
    ir = build_render_ir(replace(track, rubies=[]), style, width=641, height=720, fps=60, dpr=dpr,
                         roll_shift_ms=roll_shift_ms, layout_plan_builder=lambda *a, **k: prepared.plan,
                         serialize_source_baselines=False)
    ir['required_features'] = ['p05_resolved_horizontal', 'p05_segmented_wipe', 'p05_resolved_ruby']
    ir['skia_layout'] = prepared_ruby_layout(prepared, width=641, height=720)
    apply_resolved_font_faces(ir)
    return ir


def configure(process, ir):
    process._send({'cmd': 'configure', 'ir': ir})
    return process._expect_ok(process._read_until_event('configured'))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_ruby_group_pause_seek_and_translucent_alpha(binary, reading, target, tmp_path):
    ir = ir_for(binary, reading)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [100, 900, 1100, 1300, 1700, 2300, 1100, 5100])
    assert np.array_equal(frames[1], frames[2]) and np.array_equal(frames[2], frames[3])
    assert np.array_equal(frames[2], frames[6]) and not frames[7].any()
    assert all(f[..., 3].max() == 128 for f in frames[:-1])
    assert not np.array_equal(frames[0], frames[1]) and not np.array_equal(frames[3], frames[4])
    if target == 'gpu':
        (tmp_path/'ruby-group-layout.json').write_text(json.dumps({'lines': ir['skia_layout']['lines'],
            'runs': [{k: v for k, v in run.items() if k != 'font_ids'} for run in ir['skia_layout']['runs']]},
            sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('dpr', [1, 1.5])
def test_common_group_gpu_raster_and_independent_cpu_only_compilation(binary, reading, dpr):
    directory = Path(binary).parent
    cpu = directory.parent/(directory.name.rsplit('-', 1)[0]+'-raster')/Path(binary).name
    assert cpu.is_file(), 'Build the CPU-only sidecar'
    ir = ir_for(binary, reading, dpr=dpr)
    output = []
    for executable, target in [(binary, 'gpu'), (binary, 'raster'), (cpu, 'raster')]:
        with SkiaRendererProcess(executable, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [100, 500, 1100, 1700, 2300])
        output.append(np.stack(frames))
    assert np.array_equal(output[1], output[2])
    a, b = [x.astype(float) for x in output[:2]]
    a[..., :3] *= a[..., 3:4]/255; b[..., :3] *= b[..., 3:4]/255
    delta = np.abs(a-b)
    assert delta.mean() < .15 and (delta.max(axis=-1) > 16).mean() < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_zero_duration_group_and_time_offsets_preserve_initial_boundary(binary, reading, target):
    track, style = reading
    ir = ir_for(binary, reading)
    # Assert the exact zero boundary on final immutable unit intervals.
    for unit in ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']:
        unit['interval_ms'] = [300, 300]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [299, 300, 301])
    assert np.array_equal(frames[0], frames[1]) and not np.array_equal(frames[1], frames[2])
    shifted = ir_for(binary, (replace(track, meta=replace(track.meta, offset_ms=137)),
                              replace(style, timing_offset_ms=229)), roll_shift_ms=400)
    outputs = []
    for value, offset in [(ir_for(binary, reading), 0), (shifted, 766)]:
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, value)
            frames, _ = collect(process, [t+offset for t in [100, 500, 1100, 1700, 2300]])
        outputs.append(frames)
    assert all(np.array_equal(a, b) for a, b in zip(*outputs))


def test_negative_spacing_uses_one_group_front_not_independent_glyph_clips(binary, reading):
    track, style = reading
    ir = ir_for(binary, (track, replace(style, ruby_interval_px=-15)))
    independent = copy.deepcopy(ir)
    groups = independent['skia_layout']['lines'][0]['ruby_groups']
    groups[:] = [{'style_cell': groups[0]['style_cell'], 'units': [u]} for u in groups[0]['units']]
    output = []
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        for generation, value in enumerate([ir, independent], 1):
            configure(process, value)
            frames, _ = collect(process, [1100], generation=generation)
            output.append(frames[0])
    assert not np.array_equal(*output)


def test_roles_multiline_page_offsets_and_ruby_fonts_are_resolved_before_draw(binary, pages):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    track, style = pages
    role = SubtitleStyleScheme(ruby_font_family=style.font_family_latin,
        ruby_font_family_latin=style.font_family, ruby_font_size_px=35, ruby_latin_font_size_px=29,
        ruby_colors_follow_main=False, ruby_karaoke_colors=colors('#FF0000', '#00FF00'))
    style = replace(style, custom_style_schemes={'lead': role}, karaoke_colors=colors('#00000000', '#00000000'),
                    ruby_colors_follow_main=False, ruby_karaoke_colors=colors('#0000FF', '#0000FF'))
    line = replace(track.lines[0], chars=[replace(c, role_label='lead') for c in track.lines[0].chars])
    source = replace(track, lines=[line, *track.lines[1:]])
    ir = ir_for(binary, (source, style))
    assert len(ir['skia_layout']['lines']) == 3
    assert all(l['ruby_groups'] for l in ir['skia_layout']['lines'])
    outputs = []
    for target in ['gpu', 'raster']:
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [1900, 3000])
            shifted = copy.deepcopy(ir)
            for value in shifted['track']['lines']:
                value['layout_offset_windows'] = [{'start_ms': 2000, 'end_ms': 4000, 'offset_x': 0, 'offset_y': -30}]
            configure(process, shifted)
            moved, _ = collect(process, [1900, 3000], generation=2)
        assert np.array_equal(frames[0], moved[0])
        delta = np.abs(frames[1][30:].astype(np.int16)-moved[1][:-30].astype(np.int16))
        # Vulkan coverage quantization can differ by one alpha level when
        # translating fractional ruby outlines. Color and opaque interiors
        # must remain exact; Raster remains byte-for-byte identical.
        assert not delta[..., :3].any(), (target, delta.max(axis=(0, 1)).tolist())
        alpha = frames[1][30:, :, 3]
        if target == 'gpu':
            assert delta[..., 3].max() <= 1
            assert not delta[..., 3][(alpha == 0) | (alpha == 255)].any()
        else:
            assert not delta.any()
        outputs.append(frames[0])
    for output in outputs:
        assert (output[..., 1] > 200).any() and (output[..., 2] > 200).any()


@pytest.mark.parametrize('marker', ['', '^'])
def test_final_ruby_compiler_still_rejects_mismatched_marker_geometry(binary, reading, marker):
    track, style = reading
    prepared = prepare(binary, track, style)
    local = prepared.geometry.ruby.lines[0]
    group = local.rubies[0]
    target = replace(group.target, effective=replace(group.target.effective,
        reading=marker, reading_parts=[marker], reading_part_ms=[]))
    prepared.geometry.ruby = replace(prepared.geometry.ruby,
        lines=(replace(local, rubies=(replace(group, target=target),)),))
    # Altering only the target of already-measured geometry is not a valid
    # marker preparation; the unit clock cannot match the original glyphs.
    with pytest.raises(ValueError):
        prepared_ruby_layout(prepared, width=641, height=720)


@pytest.mark.parametrize('mutation', ['feature', 'schema', 'type', 'empty', 'style', 'style_bool', 'units',
    'unit_type', 'text', 'font', 'metrics', 'nan', 'coordinate', 'interval', 'fraction', 'reverse',
    'overlap', 'range', 'curve', 'stroke', 'gradient', 'groups'])
def test_bad_final_ruby_preserves_the_previous_scene(binary, reading, mutation):
    ir = ir_for(binary, reading)
    line = ir['skia_layout']['lines'][0]
    group = line['ruby_groups'][0]
    unit = group['units'][0]
    if mutation == 'feature': ir['required_features'].remove('p05_resolved_ruby')
    elif mutation == 'schema': ir['skia_layout']['ruby_schema'] = 2
    elif mutation == 'type': line['ruby_groups'] = {}
    elif mutation == 'empty': group['units'] = []
    elif mutation == 'style': group['style_cell'] = 999
    elif mutation == 'style_bool': group['style_cell'] = True
    elif mutation == 'units': group['units'] = [unit]*4097
    elif mutation == 'unit_type': group['units'][0] = None
    elif mutation == 'text': unit['text'] = 'missing'
    elif mutation == 'font': unit['run_index'] = -1
    elif mutation == 'metrics': unit['metrics']['glyphs'][0]['id'] += 1
    elif mutation == 'nan': unit['origin_x'] = float('nan')
    elif mutation == 'coordinate': unit['wipe_left'] += 10
    elif mutation == 'interval': unit['interval_ms'] = []
    elif mutation == 'fraction': unit['interval_ms'][0] += .5
    elif mutation == 'reverse': unit['interval_ms'] = [900, 300]
    elif mutation == 'overlap': group['units'][1]['interval_ms'][0] = 400
    elif mutation == 'range': unit['interval_ms'] = [-86400001, 900]
    elif mutation == 'curve': unit['wipe_points'] = []
    elif mutation == 'stroke': ir['style']['ruby_stroke_width_px'] = 5
    elif mutation == 'gradient': ir['style']['ruby_karaoke_colors']['before']['text']['mode'] = 'gradient_horizontal'
    elif mutation == 'groups': line['ruby_groups'] = [group]*4097
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir_for(binary, reading))
        before, _ = collect(process, [1100])
        with pytest.raises(NativeRendererError): configure(process, ir)
        after, _ = collect(process, [1100], generation=2)
    assert np.array_equal(before[0], after[0])


def test_old_sidecar_is_rejected_before_selecting_a_backend(monkeypatch, tmp_path):
    path = tmp_path/'old-renderer'; path.touch()
    profiles = ['p05_resolved_horizontal', 'p05_single_line_placement', 'p05_inline_roles', 'p05_fixed_row_grid',
                'p05_page_collisions', 'p05_smart_horizontal', 'p05_segmented_wipe']
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda _: {'backend': 'skia', 'schema': 3,
        'selection_version': 1, 'resolved_layout_schema': 2, 'resolved_wipe_schema': 1, 'profiles': profiles})
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    sent = []; monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, payload: sent.append(payload))
    with pytest.raises(NativeRendererError, match='注音组'):
        SkiaRendererProcess(path).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32', reason='Direct2D GPU reference is Windows-only')
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('mode', ['reading_units', 'checkpoint_segments'])
@pytest.mark.parametrize('visible_main', [False, True])
def test_final_ruby_group_matches_actual_d2d_gpu(binary, reading, semantics, mode, visible_main):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = reading
    style = replace(style, layout_semantics=semantics, ruby_main_progress_mode=mode,
                    ruby_karaoke_colors=colors('#FF0000', '#00FF00'))
    if visible_main:
        style = replace(style, karaoke_colors=colors('#FF0000', '#00FF00'))
    times = [299, 300, 500, 899, 900, 1100, 1400, 1700, 2100, 2300]
    with NativeRendererProcess() as d2d:
        _, reference = _render_g1_frames(d2d, style, tuple(times), force_warp=False,
                                        track=track, width=641, height=720)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style)))
        actual, _ = collect(process, times)
    def box(p, channel):
        p = p.astype(np.int16)
        y, x = np.where((p[..., 3] > 200) & (p[..., channel] > p[..., 1-channel]+50))
        return None if not len(x) else np.array([x.min(), y.min(), x.max(), y.max()])
    for time, raw, image in zip(times, reference, actual, strict=True):
        ref = np.frombuffer(raw, np.uint8).reshape(720, 641, 4)
        for channel in [0, 1]:
            a, b = box(ref, channel), box(image, channel)
            assert (a is None) == (b is None), (time, a, b)
            if a is not None: assert np.max(np.abs(a-b)) <= 1, (time, a, b)
