"""Canonical metrics -> existing semantic plan -> final Skia IR.

Font discovery and process IPC are injected configuration capabilities. No Qt
measurement or rendering is used here, and frame draws never call this module.
"""
from dataclasses import replace
from hashlib import sha256

from krok_helper.subtitle_render.domain.models import style_for_track
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.display.section_edges import section_edge_context
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import positioned_cells, write_horizontal_pivots
from krok_helper.subtitle_render.engine.layout.line.skia_vertical import (
    validate_vertical_source, validate_vertical_style, vertical_cells, vertical_japanese_glyphs, vertical_solid_schema)
from krok_helper.subtitle_render.engine.layout.plan.skia_plan import prepare_skia_layout
from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role_in_layout, uses_authored_decoration
from krok_helper.subtitle_render.engine.text.skia_batch import font_snapshot
from .render_ir import build_render_ir
from .skia_ruby_ir import prepared_ruby_layout
from .skia_ruby_paint_ir import ruby_decoration_kinds
from .skia_vertical_ruby_ir import vertical_ruby_layout
from krok_helper.subtitle_render.engine.guide.skia_vector import compose_vector_guides, validate_vector_guides
from krok_helper.subtitle_render.engine.guide.skia_bitmap import declare_bitmap_guides, validate_bitmap_guides
from krok_helper.subtitle_render.engine.guide.semantics import guide_symbol_is_bitmap
from .skia_lower_ir import lower_sources
from .skia_line_band_ir import write_line_band
from .skia_line_animation_ir import declare_line_animations, ordinary_line_animation, write_line_animation_schemas
from .skia_scanline_ir import scanline_features, write_scanline_schema
from .skia_outline_ir import declare_outline_layers
from .skia_title_ir import build_title_sources
from .skia_particle_ir import (
    ENERGY_TRANSITIONS, FALLING_PARTICLES, PARTICLE_TRANSITIONS,
    RUBY_PARTICLE_ENTRIES, RUBY_PARTICLE_EXITS, RUBY_PAIRED_PARTICLES,
    petal_entry_with_flame, petal_entry_with_scanline, petal_scanline_with_note_exit, write_particle_anchors)
from .skia_gradient_ir import write_body_gradient_bounds
from .skia_direction_ir import validate_reverse_source, project_reverse_wipe
from krok_helper.subtitle_render.engine.ruby.skia_overlap import line_endpoints
from krok_helper.subtitle_render.engine.ruby.timing import is_utopia_group_marker


# horizontal_rtl_cell_schema=1: multi-codepoint cells whose whole-cell RTL
# placement is GPU-validated under cell_glyph_schema=1. Mirrors native
# skia_ir_projection exactly. Kana, other scripts, ZWJ/variation selectors,
# supplementary scalars and default ignorables stay rejected.
_RTL_CELL_RANGES = ((0x20, 0x7e), (0xa0, 0xac), (0xae, 0xff), (0x300, 0x34e), (0x350, 0x35b), (0x363, 0x36f),
                    (0x4e00, 0x9fff), (0xac00, 0xd7a3))


# horizontal_rtl_kana_schema=1: kana and Japanese punctuation drawn from
# nominal glyphs. Combining U+3099/U+309A stay rejected: the existing GPU
# neither composes them nor applies fallback mark positioning.
_RTL_KANA_RANGES = ((0x3000, 0x3002), (0x300c, 0x300f), (0x301c, 0x301c), (0x3041, 0x3096), (0x309b, 0x309c),
                    (0x30a1, 0x30fc), (0xff01, 0xff01), (0xff1f, 0xff1f))


def _in(ranges, c):
    return any(low <= ord(c) <= high for low, high in ranges)


def _validated_rtl_cell(text):
    for i, c in enumerate(text):
        if c in '\u3099\u309a':
            # One authored mark following one kana base; repeated/isolated marks
            # and other mixed-script attachments need their own calibration.
            if i == 0 or not (0x3041 <= ord(text[i-1]) <= 0x3096 or 0x30a1 <= ord(text[i-1]) <= 0x30fa):
                return False
        elif not (_in(_RTL_CELL_RANGES, c) or _in(_RTL_KANA_RANGES, c)):
            return False
    return True


def _rtl_kana_cell(text):
    return len(text) > 1 and any(_in(_RTL_KANA_RANGES, c) or c in '\u3099\u309a' for c in text)


def _validate_style(style):
    if style.layout_semantics not in {'legacy', 'n3_1074'}:
        raise ValueError('Skia 当前规范布局不支持此语义')
    if style.vertical:
        validate_vertical_style(style)
    if type(style.right_to_left) is not bool:
        raise ValueError('Skia 右至左单元排列标记必须是布尔值')
    if style.dual_line_layout:
        if style.line_horizontal_layout == 'asymmetric' and style.smart_horizontal not in {'none','center_position','equal_margins'}:
            raise ValueError('Skia 智能水平布局模式无效')
        if len(style.line_alignments) > 32:
            raise ValueError('Skia 当前最多支持 32 行网格')


def _inline_role_labels(item):
    return {cell.role_label for cell in item.render_line.chars
            if cell.role_label in item.layout_style.custom_style_schemes}


def _uses_role_glow(item):
    look = item.layout_style
    labels = _inline_role_labels(item)
    return bool(labels) and (uses_authored_decoration(look, 'glow') or any(
        uses_authored_decoration(style_for_role_in_layout(look, label), 'glow') for label in labels
    ))


def _noise_animation(source):
    animations = {source.get('entry_anim'), source.get('exit_anim')}
    return 'noise' in animations and animations <= {'none', 'noise'}


def _composes_character_fade(source, item):
    look = item.layout_style
    return (source.get('entry_anim') == 'char_fade' and source.get('exit_anim') == 'none'
            and source.get('karaoke_anim') in {'none', 'utopia'} and not source.get('zoom_pulse')
            and not item.line.wipe_reverse
            and not (look.vertical or look.right_to_left or look.lower_ruby_enabled)
            and not (source.get('scanline') or source.get('stroke_flash') or source.get('fx_bursts')
                     or uses_authored_decoration(look, 'shadow'))
            and all(char.role_label is None and char.vector_glyph is None for char in item.render_line.chars))


def _vertical_singing_lines(ir, plan):
    tracks = set()
    for item in plan.lines:
        look = item.layout_style
        source = ir['track']['lines'][item.track_index]
        if (look.vertical and not (look.right_to_left or item.line.wipe_reverse)
                and source.get('karaoke_anim') == 'utopia'
                and {source.get('entry_anim'), source.get('exit_anim')} == {'none'}
                and not any(source.get(key) for key in ('zoom_pulse', 'stroke_flash', 'fx_bursts'))
                and not uses_authored_decoration(look, 'shadow')
                and all(char.role_label is None and char.vector_glyph is None for char in item.render_line.chars)):
            tracks.add(item.track_index)
    return tracks


def _write_utopia_pivots(ir, plan, measured, transition_lines, particle_band_tracks, vertical_singing):
    singing, transition, singing_ruby = False, False, False
    role_singing = False
    with_line_animation = False
    for index, (final, item) in enumerate(zip(ir['skia_layout']['lines'], plan.lines, strict=True)):
        source = ir['track']['lines'][item.track_index]
        scales = source.get('karaoke_anim') == 'utopia'
        animations = {source.get('entry_anim'), source.get('exit_anim')}
        if not scales and 'utopia' not in animations:
            continue
        style = item.layout_style
        annotated = bool(final.get('ruby_groups'))
        if item.track_index in vertical_singing:
            singing = True
            singing_ruby |= annotated
            continue
        line_animation = scales and animations != {'none'} and ordinary_line_animation(source)
        with_character_transition = index in transition_lines
        with_noise = _noise_animation(source) and 'p07_noise_composition' in ir['required_features']
        scanline_motion = (line_animation and 'p07_scanline_line_animation' in ir['required_features']
                           or item.track_index in particle_band_tracks)
        ruby_zoom = 'p07_ruby_zoom_pulse' in ir['required_features']
        inline_roles = any(c.role_label is not None for c in item.render_line.chars)
        role_scale = inline_roles and scales and animations == {'none'} and not source.get('zoom_pulse')
        role_scale = role_scale and not (source.get('scanline') or source.get('stroke_flash') or source.get('fx_bursts'))
        rtl_singing = scales and annotated and 'p07_rtl_ruby_singing_scale' in ir['required_features']
        if (style.vertical or (style.right_to_left and not rtl_singing) or item.line.wipe_reverse
                or (annotated and ((source.get('zoom_pulse') and not ruby_zoom)
                                   or (animations != {'none'} and not (line_animation or with_character_transition or with_noise))))
                or (not (line_animation or with_character_transition or with_noise) and not animations <= {'none', 'utopia'})
                or source.get('karaoke_anim') not in {'none', 'utopia'}
                or (line_animation and source.get('zoom_pulse'))
                or (source.get('scanline') and ((animations != {'none'} and not scanline_motion) or (source.get('zoom_pulse')
                    and 'p07_composed_scanline' not in ir['required_features'])))
                or (source.get('stroke_flash') and 'p07_composed_stroke_flash' not in ir['required_features'])
                or (source.get('fx_bursts') and ir['skia_layout'].get('sing_twinkle_schema', 0) < 12)
                or (inline_roles and not role_scale)):
            raise ValueError('Skia Utopia 动画当前仅支持正向横排实色正文')
        write_horizontal_pivots(final['cells'], measured.lines[item.track_index])
        singing = singing or scales
        role_singing = role_singing or role_scale
        transition = transition or 'utopia' in animations
        singing_ruby = singing_ruby or (scales and annotated)
        with_line_animation = with_line_animation or line_animation
    if singing:
        ir['required_features'].append('p07_singing_scale')
        ir['skia_layout']['singing_scale_schema'] = 1
    if vertical_singing:
        ir['required_features'].append('p07_vertical_singing_scale')
        if 'p05_vertical_columns' in ir['required_features']:
            ir['required_features'].append('p07_vertical_columns_singing_scale')
    if role_singing:
        ir['required_features'].append('p07_role_singing_scale')
    if transition:
        ir['required_features'].append('p07_utopia_transition')
        ir['skia_layout']['utopia_transition_schema'] = 1
    if singing_ruby:
        ir['required_features'].append('p07_ruby_singing_scale')
    if with_line_animation:
        ir['required_features'].append('p07_utopia_line_animation')


def _validate_stroke_flash(track, style, lines):
    composed = False
    for source, line in zip(track.lines, lines, strict=True):
        if not line.get('stroke_flash'):
            continue
        look = style_for_line(style, source)
        if (look.vertical or look.right_to_left or source.wipe_reverse or look.lower_ruby_enabled
                or line.get('entry_anim') != 'none' or line.get('exit_anim') != 'none'
                or line.get('karaoke_anim') not in {'none', 'utopia', 'no_wipe'}
                or line.get('fx_bursts') or uses_authored_decoration(look, 'shadow')
                or any(c.role_label is not None or c.vector_glyph is not None for c in source.chars)):
            raise ValueError('Skia 唱字描边闪光当前仅支持正向横排实色正文')
        composed = (composed or bool(track.rubies) or uses_authored_decoration(look, 'glow')
                    or line.get('karaoke_anim') != 'none' or line.get('scanline'))
    return composed


def _validate_noise_transition(track, style, lines):
    composed = False
    for source, line in zip(track.lines, lines, strict=True):
        entry, exit_ = line.get('entry_anim'), line.get('exit_anim')
        if 'noise' not in (entry, exit_):
            continue
        look = style_for_line(style, source)
        if (look.vertical or look.right_to_left or source.wipe_reverse or look.lower_ruby_enabled
                or entry not in {'noise', 'none'} or exit_ not in {'noise', 'none'}
                or line.get('karaoke_anim') not in {'none', 'utopia'} or line.get('zoom_pulse')
                or line.get('scanline') or line.get('stroke_flash') or line.get('fx_bursts')
                or uses_authored_decoration(look, 'shadow')
                or any(c.role_label is not None or c.vector_glyph is not None for c in source.chars)):
            raise ValueError('Skia 噪声出入场当前仅支持正向横排实色正文')
        composed = (composed or bool(track.rubies) or line.get('karaoke_anim') == 'utopia'
                    or uses_authored_decoration(look, 'glow'))
    return composed


_HALO_TRANSFORMS = {'stretch_in', 'stretch_out', 'glow_in', 'glow_out'}
_GEOMETRY_TRANSFORMS = {'tracking_in', 'wave_in', 'scatter_out', 'converge_out'} | _HALO_TRANSFORMS | PARTICLE_TRANSITIONS.keys()
_TRANSFORMS = {'char_drip', 'spin_flip'} | _GEOMETRY_TRANSFORMS


def _character_transforms(track, ir, plan, measured, spin_lines, composed_geometry_lines, particle_band_tracks) -> int:
    """Write canonical cell and glyph pivots and return the required version."""
    from krok_helper.subtitle_render.engine.guide.semantics import render_line_with_guide_symbols
    version = 3
    for index, item in enumerate(plan.lines):
        source = ir['track']['lines'][item.track_index]
        animations = {source.get('entry_anim'), source.get('exit_anim')}
        if not animations & _TRANSFORMS:
            continue
        geometry = bool(animations & _GEOMETRY_TRANSFORMS)
        if animations & PARTICLE_TRANSITIONS.keys():
            version = max(version, 7 if animations & (PARTICLE_TRANSITIONS.keys() - FALLING_PARTICLES) else 6)
            if animations & ENERGY_TRANSITIONS:
                version = 8
        elif geometry:
            version = max(version, 5 if animations & _HALO_TRANSFORMS else 4)
        line, look = track.lines[item.track_index], item.layout_style
        if (look.vertical or look.right_to_left or line.wipe_reverse
                or (index not in spin_lines and index not in composed_geometry_lines
                    and (source.get('karaoke_anim') != 'none' or track.rubies))
                or (source.get('scanline') and item.track_index not in particle_band_tracks) or look.lower_ruby_enabled
                or any(c.role_label is not None or c.vector_glyph is not None
                       for c in render_line_with_guide_symbols(line).chars)):
            raise ValueError('Skia 逐字滴落/翻转当前仅支持正向横排正文')
        entry = source.get('entry_anim') in _TRANSFORMS and source['entry_duration_ms'] > 0
        exit_ = source.get('exit_anim') in _TRANSFORMS and source['exit_duration_ms'] > 0
        if not (entry or exit_):
            continue
        if geometry and index not in composed_geometry_lines and (
                look.stroke_width_px > 0 or (look.stroke2_enabled and look.stroke2_width_px > 0)):
            raise ValueError('Skia 逐字几何过渡当前仅支持无描边正文')
        cells, metrics = ir['skia_layout']['lines'][index]['cells'], measured.lines[item.track_index]
        write_horizontal_pivots(cells, metrics)
    return version


def build_skia_render_ir(track, style, *, measure, resolve_font, extra_tracks=None, **kwargs):
    sources = tuple(extra_tracks or ())
    if len(sources) > 15:
        raise ValueError('Skia 规范布局最多支持 15 条附加轨道')
    with font_snapshot(resolve_font):
        ir = _build_source_ir(track, style, measure=measure, resolve_font=resolve_font, **kwargs)
        titles = ir['titles']
        if not sources and not titles:
            return ir
        extras = [_build_source_ir(source, replace(style_for_track(style, source), title_overlays=[]),
                                   measure=measure, resolve_font=resolve_font, **kwargs)
                  for source in sources]
        title_sources = build_title_sources(titles, _build_source_ir, measure=measure, resolve_font=resolve_font, **kwargs)
    ir['titles'] = []
    if not extras and not title_sources:
        return ir
    if extras:
        ir['skia_extra_sources'] = extras
        ir['required_features'].append('p08_multiple_sources')
        if any('p05_vertical_solid' in source['required_features'] for source in (ir, *extras)):
            ir['required_features'].append('p08_multiple_vertical_sources')
    if title_sources:
        ir['skia_titles'] = title_sources
        ir['required_features'].append('p08_solid_title')
    identities = (source['skia_layout']['metrics_identity']
                  for source in (ir, *extras, *(title['source'] for title in title_sources)))
    ir['skia_layout']['metrics_identity'] = sha256(''.join(identities).encode('ascii')).hexdigest()
    return ir


def _build_source_ir(track, style, *, measure, resolve_font, adjust_position=None, **kwargs):
    prepared = []
    animated_title_guides = False

    def build_plan(source, source_style, **plan_kwargs):
        _validate_style(source_style)
        if source_style.right_to_left and source_style.lower_ruby_enabled:
            raise ValueError('Skia 右至左单元排列暂不支持下方注音组合')
        if source_style.right_to_left:
            if any(len(char.text) > 1 and not _validated_rtl_cell(char.text) for line in source.lines for char in line.chars):
                raise ValueError('Skia 右至左排列的多码点单元包含尚未验证的字符')
        validate_reverse_source(source)
        if source_style.vertical:
            validate_vertical_source(source)
        validate_vector_guides(source, source_style)
        nonlocal animated_title_guides
        animated_title_guides = validate_bitmap_guides(source, source_style, static_title=adjust_position is not None)
        with layout_pass():
            section_edge_context(source, source_style)
            for line in source.lines:
                _validate_style(style_for_line(source_style, line))
            result = prepare_skia_layout(source, source_style, measure=measure, resolve_font=resolve_font,
                                         dpr=kwargs.get('dpr', 1), **plan_kwargs)
        prepared.append(result)
        return result.plan

    ir = build_render_ir(track, style, layout_plan_builder=build_plan,
                         serialize_source_baselines=False, **kwargs)
    vertical_singing = _vertical_singing_lines(ir, prepared[0].plan) if style.vertical else set()
    vertical_ruby = None
    if prepared[0].vertical_readings is not None:
        vertical_ruby = vertical_ruby_layout(prepared[0], width=kwargs['width'], height=kwargs['height'],
                                            dpr=kwargs.get('dpr', 1), singing_tracks=vertical_singing)
    plan, measured, geometry = prepared[0].plan, prepared[0].measured, prepared[0].geometry
    geometry.set_lanes(plan.lines)
    spin_lines = set()
    translation_lines = set()
    affine_lines = set()
    stretch_lines = set()
    stretch_entry_lines = set()
    glow_lines = set()
    paired_geometry_lines = set()
    assembly_lines = set()
    assembly_pair = False
    particle_fade_lines = set()
    particle_affine_lines = set()
    energy_pair_lines = set()
    same_energy_pair = False
    particle_entry_lines = set()
    particle_entry_glow = False
    repeated_particle_pair = False
    mixed_particle_pair = False
    petal_note_pair = False
    petal_flame = False
    particle_band_tracks = set()
    if geometry.ruby is not None:
        for index, item in enumerate(plan.lines):
            source = ir['track']['lines'][item.track_index]
            entry, leave = source.get('entry_anim'), source.get('exit_anim')
            look = item.layout_style
            with_flame = petal_entry_with_flame(source, look)
            with_band = petal_entry_with_scanline(source, look)
            if (source.get('karaoke_anim') not in {'none', 'utopia'} or source.get('zoom_pulse')
                    or (source.get('scanline') and not with_band) or source.get('stroke_flash')
                    or uses_authored_decoration(look, 'shadow')):
                continue
            if source.get('fx_bursts'):
                if len(plan.lines) == 1 and (look.sing_fx == 'none' or with_flame):
                    both_phases = source['entry_duration_ms'] > 0 and source['exit_duration_ms'] > 0
                    if both_phases and (entry, leave) == ('assemble_in', 'dissolve_out'):
                        assembly_lines.add(index)
                        assembly_pair = True
                    if (entry in ENERGY_TRANSITIONS and leave in ENERGY_TRANSITIONS
                            and both_phases):
                        energy_pair_lines.add(index)
                        same_energy_pair |= entry == leave
                        if 'flame' in (entry, leave):
                            particle_affine_lines.add(index)
                    effect = (entry if leave == 'none' and source['entry_duration_ms'] > 0
                              else leave if entry == 'none' and source['exit_duration_ms'] > 0 else None)
                    if effect in {'assemble_in', 'dissolve_out'}:
                        assembly_lines.add(index)
                    paired = both_phases and entry in RUBY_PAIRED_PARTICLES and leave in RUBY_PAIRED_PARTICLES
                    note_exit = petal_scanline_with_note_exit(source, look)
                    if paired or note_exit or (leave == 'none' and effect in RUBY_PARTICLE_ENTRIES):
                        readings = geometry.ruby.lines[index].rubies
                        ruby_decorations = {kind for reading in readings
                                            for kind in ruby_decoration_kinds(reading.target.style)}
                        body_glow = uses_authored_decoration(look, 'glow')
                        if ('shadow' in ruby_decorations or ('glow' in ruby_decorations and not body_glow)
                                or (body_glow and (paired or (with_flame and not note_exit)
                                                   or entry not in {'heart', 'petal', 'feather', 'snow', 'snow_solid'}))):
                            raise ValueError('Skia 带上注音的粒子入场暂不支持阴影或光晕')
                        particle_entry_glow |= body_glow
                        particle_entry_lines.add(index)
                        repeated_particle_pair |= paired and entry == leave
                        mixed_particle_pair |= paired and entry != leave
                        petal_note_pair |= note_exit
                        petal_flame |= with_flame
                        if with_band:
                            particle_band_tracks.add(item.track_index)
                    if effect in ENERGY_TRANSITIONS or (entry == 'none' and effect in RUBY_PARTICLE_EXITS):
                        particle_fade_lines.add(index)
                        if effect == 'flame':
                            particle_affine_lines.add(index)
                continue
            if (source.get('entry_anim') == 'none' and source.get('exit_anim') == 'spin_flip'
                    and source.get('exit_duration_ms', 0) > 0):
                spin_lines.add(index)
            if (source.get('entry_anim') in {'tracking_in', 'wave_in'} and source.get('exit_anim') == 'none'
                    and source.get('entry_duration_ms', 0) > 0):
                translation_lines.add(index)
            if (source.get('entry_anim') == 'none' and source.get('exit_anim') in {'scatter_out', 'converge_out'}
                    and source.get('exit_duration_ms', 0) > 0):
                affine_lines.add(index)
            if (source.get('entry_anim') == 'none' and source.get('exit_anim') == 'stretch_out'
                    and source.get('exit_duration_ms', 0) > 0):
                stretch_lines.add(index)
            if (source.get('entry_anim') == 'stretch_in' and source.get('exit_anim') == 'none'
                    and source.get('entry_duration_ms', 0) > 0):
                stretch_entry_lines.add(index)
            if ((source.get('entry_anim') == 'glow_in' and source.get('exit_anim') == 'none'
                    and source.get('entry_duration_ms', 0) > 0)
                    or (source.get('entry_anim') == 'none' and source.get('exit_anim') == 'glow_out'
                        and source.get('exit_duration_ms', 0) > 0)):
                glow_lines.add(index)
            if (source.get('entry_anim') == 'wave_in' and source.get('exit_anim') == 'scatter_out'
                    and source.get('entry_duration_ms', 0) > 0 and source.get('exit_duration_ms', 0) > 0):
                paired_geometry_lines.add(index)
    composed_particle_lines = assembly_lines | particle_fade_lines | energy_pair_lines | particle_entry_lines
    affine_geometry_lines = (affine_lines | stretch_lines | stretch_entry_lines | glow_lines
                             | paired_geometry_lines | particle_affine_lines)
    composed_geometry_lines = translation_lines | affine_geometry_lines | composed_particle_lines
    lines = []
    if vertical_ruby is None:
        for index, item in enumerate(plan.lines):
            if item.layout_style.vertical:
                line = {'cells': vertical_cells(geometry.locals[item.track_index], measured.lines[item.track_index], item,
                                               width=kwargs['width'], height=kwargs['height'],
                                               singing_scale=item.track_index in vertical_singing),
                        'text_flow': 'vertical'}
                if item.layout_style.dual_line_layout:
                    line['vertical_lane'] = item.lane
                lines.append(line)
                continue
            position = geometry.position(item, kwargs['width'], kwargs['height'])
            if adjust_position is not None:
                position = adjust_position(geometry.locals[index], item, measured, kwargs['width'], kwargs['height'])
            cells = positioned_cells(geometry.locals[index], position)
            ends, _ = line_endpoints(geometry.locals[index].cells, measured.lines[item.track_index],
                                     dpr=kwargs.get('dpr', 1), reverse=item.line.wipe_reverse,
                                     right_to_left=item.layout_style.right_to_left)
            for cell, end in zip(cells, ends, strict=True):
                cell['wipe_end_position'] = end
            lines.append({'cells': cells})
    else:
        lines = vertical_ruby['lines']
    # Every canonical line carries the GPU's final wipe endpoint, so the
    # overlap contract is part of the base canonical feature set.
    ir['required_features'] = ['p05_resolved_horizontal', 'p05_single_line_placement', 'p05_inline_roles',
                               'p05_fixed_row_grid', 'p05_page_collisions', 'p05_smart_horizontal', 'p05_segmented_wipe',
                               'p05_overlap_wipe']
    ir['skia_layout'] = {'version': 2, 'wipe_schema': 1, 'overlap_schema': 1, 'metrics_identity': measured.identity,
                         'fonts': measured.fonts, 'runs': measured.runs, 'lines': lines}
    if style.vertical and style.dual_line_layout:
        ir['required_features'].append('p05_vertical_columns')
    if vertical_ruby is not None:
        ir['skia_layout'].update(vertical_ruby)
        ir['required_features'] += ['p05_resolved_ruby', 'p05_upper_ruby', 'p05_vertical_ruby']
        if style.dual_line_layout:
            ir['required_features'].append('p05_vertical_columns_ruby')
        if any(len(line['ruby_groups']) > 1 for line in lines):
            ir['required_features'].append('p05_vertical_ruby_groups')
        if vertical_ruby.get('vertical_ruby_shift_schema'):
            ir['required_features'].append('p05_vertical_shifted_ruby')
        if vertical_ruby.get('vertical_ruby_rotation_schema'):
            ir['required_features'].append('p05_vertical_rotated_ruby')
        if vertical_ruby.get('ruby_solid_outline_schema'):
            ir['required_features'] += ['p06_ruby_solid_outline', 'p06_vertical_ruby_outline']
        ir['required_features'].extend(f'p06_ruby_{kind}' for kind in ('shadow', 'glow')
                                       if vertical_ruby.get(f'ruby_{kind}_schema'))
    secondary_outline = declare_outline_layers(ir, plan, title=adjust_position is not None)
    if any(char.vector_glyph is not None and not guide_symbol_is_bitmap(char.vector_glyph)
           for item in plan.lines for char in item.render_line.chars):
        ir['required_features'].append('p05_vector_guides')
        ir['skia_layout']['vector_guide_schema'] = 1
    declare_line_animations(ir)
    fade_lines = set()
    if any(line.get('entry_anim') == 'char_fade' or line.get('exit_anim') == 'char_fade'
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_character_fade')
        fade_lines = {
            index for index, item in enumerate(plan.lines)
            if _composes_character_fade(ir['track']['lines'][item.track_index], item)
            and (geometry.ruby is not None or uses_authored_decoration(item.layout_style, 'glow'))
        }
        if fade_lines:
            ir['required_features'].append('p07_character_entry_fade_composition')
    if any(line.get('scanline') for line in ir['track']['lines']):
        ir['required_features'] += scanline_features(
            track, style, ir['track']['lines'], particle_band_tracks, vertical_singing)
        if vertical_ruby is not None:
            ir['required_features'].append('p07_ruby_scanline')
    decorations = [kind for kind in ('shadow', 'glow') if any(
        uses_authored_decoration(item.layout_style, kind) for item in plan.lines)]
    ir['required_features'].extend(f'p06_static_{kind}' for kind in decorations)
    if decorations and not (style.vertical or style.right_to_left or track.rubies):
        decorated_reverse = [item for item in plan.lines if item.line.wipe_reverse
                             and any(uses_authored_decoration(item.layout_style, kind) for kind in decorations)]
        for item in decorated_reverse:
            source = ir['track']['lines'][item.track_index]
            if (source.get('karaoke_anim') not in {'none', 'no_wipe'} or not ordinary_line_animation(source)
                    or source.get('scanline') or source.get('stroke_flash') or source.get('fx_bursts')
                    or _inline_role_labels(item)):
                raise ValueError('Skia 横排反向实色装饰仅支持普通走字或整字变色正文')
        if decorated_reverse:
            ir['required_features'].append('p06_reverse_decoration')
            if any(ir['track']['lines'][item.track_index].get('karaoke_anim') == 'no_wipe' for item in decorated_reverse):
                ir['required_features'].append('p07_reverse_decoration_no_wipe')
            if any({ir['track']['lines'][item.track_index].get(phase) for phase in ('entry_anim', 'exit_anim')} != {'none'}
                   for item in decorated_reverse):
                ir['required_features'].append('p07_reverse_decoration_line_animation')
    if any(_uses_role_glow(item) for item in plan.lines):
        ir['required_features'].append('p06_role_static_glow')
    ruby_decorations = vertical_ruby is not None and any(
        vertical_ruby.get(f'ruby_{kind}_schema') for kind in ('shadow', 'glow'))
    if style.vertical and (decorations or ruby_decorations):
        reverse_vertical = any(line.wipe_reverse for line in track.lines)
        columns = style.dual_line_layout
        vertical_paints = set(decorations) | {kind for kind in ('shadow', 'glow')
            if f'p06_ruby_{kind}' in ir['required_features']}
        vertical_no_wipe = bool(vertical_paints) and any(
            line.get('karaoke_anim') == 'no_wipe' for line in ir['track']['lines'])
        modes = {'none', 'no_wipe'} if vertical_no_wipe else {'none'}
        if any((index not in vertical_singing and line.get('karaoke_anim') not in modes)
               or not ordinary_line_animation(line)
               or line.get('stroke_flash') or line.get('fx_bursts')
               for index, line in enumerate(ir['track']['lines'])):
            raise ValueError('Skia 竖排静态装饰超出已支持的走字组合')
        if 'p07_vertical_scanline_band' in ir['required_features']:
            ir['required_features'].append('p07_vertical_scanline_decoration')
        ir['required_features'].extend(f'p06_vertical_{kind}' for kind in decorations)
        if vertical_no_wipe:
            kind = next(iter(vertical_paints)) if len(vertical_paints) == 1 else 'mixed_decoration'
            ir['required_features'].append(f'p07_vertical_{kind}_no_wipe')
            if {'p07_line_fade', 'p07_line_motion'} & set(ir['required_features']):
                ir['required_features'].append('p07_vertical_decoration_no_wipe_animation')
        if columns:
            ir['required_features'].append('p06_vertical_columns_decoration')
            if vertical_ruby is not None:
                ir['required_features'].append('p06_vertical_columns_ruby_decoration')
            if vertical_no_wipe:
                ir['required_features'].append('p07_vertical_columns_decoration_no_wipe')
            if reverse_vertical and any(not line.wipe_reverse for line in track.lines):
                ir['required_features'].append('p06_vertical_mixed_column_decoration')
        if reverse_vertical:
            ir['required_features'].extend(f'p06_vertical_reverse_{kind}' for kind in decorations)
            if vertical_no_wipe:
                ir['required_features'].append('p07_vertical_reverse_decoration_no_wipe')
        if 'p07_line_fade' in ir['required_features']:
            ir['required_features'].append('p07_vertical_decoration_fade')
            if columns:
                ir['required_features'].append('p07_vertical_columns_decoration_fade')
        if 'p07_line_motion' in ir['required_features']:
            ir['required_features'].append('p07_vertical_decoration_line_motion')
        if vertical_ruby is not None:
            ir['required_features'].append('p06_vertical_ruby_decoration')
    if any(line.get('stroke_flash') for line in ir['track']['lines']):
        if _validate_stroke_flash(track, style, ir['track']['lines']):
            ir['required_features'].append('p07_composed_stroke_flash')
        ir['required_features'].append('p07_stroke_flash')
    if any('noise' in (line.get('entry_anim'), line.get('exit_anim')) for line in ir['track']['lines']):
        if _validate_noise_transition(track, style, ir['track']['lines']):
            ir['required_features'].append('p07_noise_composition')
        ir['required_features'].append('p07_noise_transition')
    if style.vertical:
        ir['required_features'].append('p05_vertical_solid')
        ir['skia_layout']['vertical_solid_schema'] = vertical_solid_schema(track)
        if vertical_japanese_glyphs(track):
            ir['required_features'].append('p05_vertical_japanese_glyphs')
            ir['skia_layout']['vertical_japanese_glyph_schema'] = 1
    utopia_exit_lines = set()
    utopia_exit_ruby_glow = False
    utopia_exit_body_glow = False
    if geometry.ruby is not None:
        lower_enabled = any(item.layout_style.lower_ruby_enabled for item in plan.lines)
        if not lower_enabled:
            for index, (item, reading) in enumerate(zip(plan.lines, geometry.ruby.lines, strict=True)):
                source = ir['track']['lines'][item.track_index]
                look = item.layout_style
                if (reading.rubies and source.get('entry_anim') == 'none' and source.get('exit_anim') == 'utopia'
                        and source.get('karaoke_anim') == 'utopia' and not source.get('zoom_pulse')
                        and not (source.get('scanline') or source.get('stroke_flash') or source.get('fx_bursts'))
                        and not (look.right_to_left or item.line.wipe_reverse or _inline_role_labels(item))
                        and not uses_authored_decoration(look, 'shadow')
                        and all(kind != 'shadow' for group in reading.rubies
                                for kind in ruby_decoration_kinds(group.target.style))
                        and all(cell.vector_glyph is None for cell in item.render_line.chars)):
                    utopia_exit_lines.add(index)
                    utopia_exit_body_glow |= uses_authored_decoration(look, 'glow')
                    utopia_exit_ruby_glow |= any('glow' in ruby_decoration_kinds(group.target.style)
                                                for group in reading.rubies)
        rtl_shadow = False
        rtl_motion = False
        rtl_motion_without_scale = False
        if style.right_to_left:
            for item, reading in zip(plan.lines, geometry.ruby.lines, strict=True):
                look = item.layout_style
                source = ir['track']['lines'][item.track_index]
                shadow = uses_authored_decoration(look, 'shadow') or any(
                    'shadow' in ruby_decoration_kinds(group.target.style) for group in reading.rubies)
                moving = {source.get('entry_anim'), source.get('exit_anim')} != {'none'}
                karaoke = source.get('karaoke_anim')
                if (look.stroke_width_px <= 0 or item.line.wipe_reverse
                        or karaoke not in {'none', 'no_wipe', 'utopia'} or source.get('zoom_pulse')
                        or (moving and not ordinary_line_animation(source))
                        or source.get('scanline') or source.get('stroke_flash') or source.get('fx_bursts')
                        or _inline_role_labels(item)
                        or (shadow and karaoke == 'utopia')):
                    raise ValueError('Skia 右至左上注音需要有描边的普通走字、整字变色或 Utopia 唱字，阴影限前两种')
                rtl_shadow |= shadow
                rtl_motion |= moving and karaoke == 'utopia'
                rtl_motion_without_scale |= moving and karaoke != 'utopia'
        markers = any(is_utopia_group_marker(ruby) for ruby in track.rubies)
        sources, indices = lower_sources(prepared[0])
        zoom_lines = {
            index for index, (line, geometry_line) in enumerate(zip(ir['track']['lines'], geometry.ruby.lines, strict=True))
            if line.get('zoom_pulse') and geometry_line.rubies
            and not lower_enabled and not markers
            and {line.get('entry_anim'), line.get('exit_anim')} == {'none'}
        }
        singing_lines = {
            index for index, line in enumerate(ir['track']['lines'])
            if line.get('karaoke_anim') == 'utopia' and not line.get('zoom_pulse')
            and (ordinary_line_animation(line) or index in fade_lines or index in spin_lines
                 or index in utopia_exit_lines
                 or index in composed_geometry_lines or _noise_animation(line))
        }
        ir['skia_layout'] = prepared_ruby_layout(prepared[0], width=kwargs['width'], height=kwargs['height'],
            dpr=kwargs.get('dpr', 1), lower_source_indices=indices if lower_enabled else None,
            singing_lines=singing_lines | zoom_lines, zoom_lines=zoom_lines,
            fade_lines=fade_lines | composed_geometry_lines, spin_lines=spin_lines,
            affine_lines=affine_geometry_lines | utopia_exit_lines, utopia_exit_lines=utopia_exit_lines)
        if 'p05_vector_guides' in ir['required_features']:
            ir['skia_layout']['vector_guide_schema'] = 1
        ir['track']['rubies'].extend(sources)
        ir['required_features'] += ['p05_resolved_ruby', 'p05_upper_ruby']
        if utopia_exit_lines:
            ir['required_features'].append('p07_ruby_utopia_exit')
        if utopia_exit_ruby_glow:
            ir['required_features'].append('p07_ruby_utopia_exit_glow')
        if utopia_exit_body_glow:
            ir['required_features'].append('p07_ruby_utopia_exit_body_glow')
        if style.right_to_left:
            ir['required_features'].append('p05_horizontal_rtl_ruby')
            if rtl_shadow:
                ir['required_features'].append('p06_rtl_ruby_shadow')
            if rtl_motion:
                ir['required_features'].append('p07_rtl_ruby_line_animation')
            if rtl_motion_without_scale:
                ir['required_features'].append('p07_rtl_ruby_unscaled_line_animation')
            if singing_lines:
                ir['required_features'].append('p07_rtl_ruby_singing_scale')
        if zoom_lines:
            ir['required_features'].append('p07_ruby_zoom_pulse')
        if 'p07_scanline_band' in ir['required_features']:
            ir['required_features'].append('p07_ruby_scanline')
        if markers:
            ir['required_features'].append('p05_ruby_markers')
        if 'ruby_unit_schema' in ir['skia_layout']:
            ir['required_features'].append('p05_ruby_units')
        if 'standalone_mark_schema' in ir['skia_layout']:
            ir['required_features'].append('p05_standalone_marks')
        if lower_enabled:
            ir['required_features'].append('p05_lower_ruby')
        if 'ruby_solid_outline_schema' in ir['skia_layout']:
            ir['required_features'].append('p06_ruby_solid_outline')
        if 'ruby_shadow_schema' in ir['skia_layout']:
            ir['required_features'].append('p06_ruby_shadow')
        if 'ruby_glow_schema' in ir['skia_layout']:
            ir['required_features'].append('p06_ruby_glow')
            if any(_inline_role_labels(item) for item in plan.lines):
                ir['required_features'].append('p06_ruby_role_glow')
        if 'lower_ruby_glow_schema' in ir['skia_layout']:
            ir['required_features'].append('p06_lower_ruby_glow')
        if 'lower_ruby_shadow_schema' in ir['skia_layout']:
            ir['required_features'].append('p06_lower_ruby_shadow')
        if spin_lines:
            ir['required_features'].append('p07_ruby_character_exit_spin')
        if translation_lines:
            ir['required_features'].append('p07_ruby_character_translation')
        if affine_lines:
            ir['required_features'].append('p07_ruby_character_affine_exit')
        if stretch_lines:
            ir['required_features'].append('p07_ruby_character_stretch_exit')
        if stretch_entry_lines:
            ir['required_features'].append('p07_ruby_character_stretch_entry')
        if glow_lines:
            ir['required_features'].append('p07_ruby_character_glow_transition')
        if paired_geometry_lines:
            ir['required_features'].append('p07_ruby_character_wave_scatter')
        if assembly_lines:
            ir['required_features'].append('p07_ruby_particle_assembly')
        if assembly_pair:
            ir['required_features'].append('p07_ruby_particle_assembly_pair')
        if particle_fade_lines:
            ir['required_features'].append('p07_ruby_particle_fade')
        if energy_pair_lines:
            ir['required_features'].append('p07_ruby_energy_pair')
        if same_energy_pair:
            ir['required_features'].append('p07_ruby_same_energy_pair')
        if particle_entry_lines:
            ir['required_features'].append('p07_ruby_particle_entry')
        if particle_entry_glow:
            ir['required_features'].append('p07_ruby_particle_entry_glow')
        if repeated_particle_pair:
            ir['required_features'].append('p07_ruby_particle_entry_exit')
        if mixed_particle_pair:
            ir['required_features'].append('p07_ruby_mixed_particle_entry_exit')
        if petal_note_pair:
            ir['required_features'].append('p07_ruby_petal_scanline_note_exit')
        if petal_flame:
            ir['required_features'].append('p07_ruby_petal_entry_flame_singing')
    if style.right_to_left:
        ir['required_features'].append('p05_horizontal_rtl')
        ir['skia_layout']['horizontal_rtl_schema'] = 1
        for final in ir['skia_layout']['lines']:
            final['text_flow'] = 'rtl'
        if any(len(char.text) > 1 for item in plan.lines for char in item.line.chars):
            ir['required_features'].append('p05_horizontal_rtl_cells')
            ir['skia_layout']['horizontal_rtl_cell_schema'] = 1
        if any(_rtl_kana_cell(char.text) for item in plan.lines for char in item.line.chars):
            ir['required_features'].append('p05_horizontal_rtl_kana_cells')
            ir['skia_layout']['horizontal_rtl_kana_schema'] = 1
        if any(any(c in '\u3099\u309a' for c in char.text) and len(char.text) > 1
               for item in plan.lines for char in item.line.chars):
            ir['required_features'].append('p05_horizontal_rtl_kana_marks')
            ir['skia_layout']['kana_mark_schema'] = 1
    if any({line.get('entry_anim'), line.get('exit_anim')} & _TRANSFORMS for line in ir['track']['lines']):
        ir['skia_layout']['character_transform_schema'] = _character_transforms(
            track, ir, plan, measured, spin_lines, composed_geometry_lines, particle_band_tracks)
        ir['required_features'].append('p07_character_transform')
    write_body_gradient_bounds(ir, plan)
    write_scanline_schema(ir, track, style)
    if any(line.get('fx_bursts') for line in ir['track']['lines']):
        ir['skia_layout']['sing_twinkle_schema'] = write_particle_anchors(track, ir, plan, measured, composed_particle_lines)
        ir['required_features'].append('p07_sing_twinkle')
    if any(len(run['font_ids']) > 1 or 'font_options' in run for run in ir['skia_layout']['runs']):
        ir['required_features'].append('p05_font_fallback')
        ir['skia_layout']['font_chain_schema'] = 1
    if any(run.get('mixed_fallback') for run in ir['skia_layout']['runs']):
        ir['required_features'].append('p05_mixed_fonts')
        ir['skia_layout']['mixed_font_schema'] = 1
    if any(line.get("karaoke_anim") == "no_wipe" for line in ir["track"]["lines"]):
        if any(source.get('karaoke_anim') == 'no_wipe' and any(
                    cell['interval_ms'][0] == cell['interval_ms'][1]
                    for group in final.get('ruby_groups', []) for cell in group['units'])
               for source, final in zip(ir['track']['lines'], ir['skia_layout']['lines'], strict=True)):
            ir['required_features'].append('p07_zero_ruby_no_wipe')
            ir['skia_layout']['zero_ruby_no_wipe_schema'] = 1
        ir["required_features"].append("p07_no_wipe")
        ir["skia_layout"]["no_wipe_schema"] = 1
        if 'p05_horizontal_rtl_ruby' in ir['required_features']:
            ir['required_features'].append('p07_rtl_ruby_no_wipe')
        lower_decoration = any(source.get('karaoke_anim') == 'no_wipe' and any(
            'lower_glow' in group or 'lower_shadow' in group for group in final.get('ruby_groups', ()))
            for source, final in zip(ir['track']['lines'], ir['skia_layout']['lines'], strict=True))
        if lower_decoration:
            ir['required_features'].append('p07_lower_ruby_no_wipe_decoration')
        if ('p05_lower_ruby' not in ir['required_features'] or lower_decoration) and any(
                source.get('karaoke_anim') == 'no_wipe' and (source.get('stroke_flash')
                or uses_authored_decoration(item.layout_style, 'glow') or 'p06_ruby_glow' in ir['required_features'])
                for source, item in zip(ir['track']['lines'], plan.lines, strict=True)):
            ir['required_features'].append('p07_no_wipe_decoration')
    # Attach the outline schemas to the final layout, including ruby's separate layout projection.
    if 'p06_primary_solid_outline' in ir['required_features']:
        ir['skia_layout']['primary_solid_outline_schema'] = 1
        if style.vertical:
            if style.dual_line_layout:
                ir['required_features'].append('p06_vertical_columns_outline')
            if any(line.wipe_reverse for line in track.lines):
                ir['required_features'].append('p06_vertical_reverse_outline')
            if vertical_ruby is not None:
                if 'p06_vertical_ruby_outline' not in ir['required_features']:
                    ir['required_features'].append('p06_vertical_ruby_outline')
            ir['required_features'].append('p06_vertical_outline')
        if style.dual_line_layout:
            ir['required_features'].append('p06_multi_lane_outline')
        if 'p05_resolved_ruby' in ir['required_features']:
            ir['required_features'].append('p06_outline_ruby')
    if secondary_outline:
        ir['skia_layout']['secondary_solid_outline_schema'] = 1
    if ('p05_vertical_ruby_groups' in ir['required_features'] and (
            'p06_vertical_ruby_outline' in ir['required_features'] or 'p06_vertical_ruby_decoration' in ir['required_features'])):
        ir['required_features'].append('p06_vertical_ruby_group_paint')
    animates_line = write_line_animation_schemas(ir)
    if geometry.ruby is not None and animates_line:
        ir['required_features'].append('p07_ruby_line_animation')
        lower_motion_modes = {
            source.get('karaoke_anim')
            for source, final in zip(ir['track']['lines'], ir['skia_layout']['lines'], strict=True)
            if ordinary_line_animation(source) and {source.get('entry_anim'), source.get('exit_anim')} != {'none'}
            and any('lower_glow' in group or 'lower_shadow' in group for group in final['ruby_groups'])
        }
        if 'utopia' in lower_motion_modes:
            ir['required_features'].append('p07_lower_ruby_decoration_line_animation')
        if lower_motion_modes & {'none', 'no_wipe'}:
            ir['required_features'].append('p07_lower_ruby_unscaled_line_animation')
    if animates_line and not (style.vertical or style.right_to_left or style.lower_ruby_enabled):
        motion_decorations = set()
        for index, item in enumerate(plan.lines):
            source = ir['track']['lines'][item.track_index]
            if (source.get('karaoke_anim') in {'none', 'no_wipe'}
                    and {source.get('entry_anim'), source.get('exit_anim')} != {'none'} and ordinary_line_animation(source)
                    and not (source.get('zoom_pulse') or source.get('scanline') or source.get('stroke_flash')
                             or source.get('fx_bursts') or item.line.wipe_reverse or _inline_role_labels(item))
                    and all(char.vector_glyph is None for char in item.render_line.chars)):
                rubies = geometry.ruby.lines[index].rubies if geometry.ruby is not None else ()
                kinds = {kind for group in rubies for kind in ruby_decoration_kinds(group.target.style)}
                kinds.update(kind for kind in ('shadow', 'glow') if uses_authored_decoration(item.layout_style, kind))
                if 'shadow' in kinds:
                    motion_decorations.add('shadow')
                elif 'glow' in kinds and 'p06_gradient_glow' not in ir['required_features']:
                    motion_decorations.add('glow')
        ir['required_features'].extend(f'p07_horizontal_{kind}_line_animation' for kind in sorted(motion_decorations))
    if 'p07_character_fade' in ir['required_features']:
        ir['skia_layout']['character_fade_schema'] = 1
    _write_utopia_pivots(ir, plan, measured, fade_lines | spin_lines | composed_geometry_lines | utopia_exit_lines,
                        particle_band_tracks, vertical_singing)
    role_glow = any(feature in ir['required_features'] for feature in ('p06_role_static_glow', 'p06_ruby_role_glow'))
    if 'p07_line_fade' in ir['required_features'] and (role_glow or any(
            feature in ir['required_features'] for feature in ('p06_role_solid_outlines', 'p06_role_body_paints'))):
        ir['required_features'].append('p07_role_line_fade')
    if 'p07_singing_scale' in ir['required_features'] and (
            role_glow or 'p06_static_glow' in ir['required_features'] or 'p06_ruby_glow' in ir['required_features']):
        ir['required_features'].append('p07_singing_glow')
        if role_glow and 'p07_role_singing_scale' in ir['required_features']:
            ir['required_features'].append('p07_role_singing_glow')
    if any(line.get('zoom_pulse') and (
            line.get('stroke_flash') or 'p07_singing_glow' in ir['required_features'])
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_zoom_pulse_decoration')
    project_reverse_wipe(ir)
    if any(item.layout_style.lit_enabled and item.layout_style.lit_style != 'volume' for item in plan.lines):
        ir['required_features'].append('p08_custom_shape_signal')
        if any(item.layout_style.lit_enabled and item.layout_style.lit_style == 'image' for item in plan.lines):
            ir['required_features'].append('p08_custom_image_signal')
        if any(item.layout_style.lit_enabled and item.layout_style.lit_appearance_mode != 'custom' for item in plan.lines):
            ir['required_features'].append('p08_decorated_shape_signal')
        if any(item.layout_style.lit_enabled and item.layout_style.lit_style in {'star', 'note8', 'note16', 'notepair'}
               for item in plan.lines):
            ir['required_features'].append('p08_vector_shape_signal')
        for item, source, final in zip(plan.lines, ir['track']['lines'], ir['skia_layout']['lines'], strict=True):
            if item.layout_style.lit_enabled and item.layout_style.lit_style != 'volume' and source.get('lit_head'):
                position = geometry.position(item, kwargs['width'], kwargs['height'])
                ascent = float(measured.lines[item.track_index].baseline_metrics['ascent'])
                final['shape_signal_anchor'] = [position.offset_x, position.baseline_y - ascent]
    if any(item.layout_style.volume_enabled or (item.layout_style.lit_enabled and item.layout_style.lit_style == 'volume')
           for item in plan.lines):
        ir['required_features'].append('p08_volume_signal')
        for item, final in zip(plan.lines, ir['skia_layout']['lines'], strict=True):
            if geometry.volume_widths[item.track_index]:
                position = geometry.position(item, kwargs['width'], kwargs['height'])
                metrics = measured.lines[item.track_index].baseline_metrics
                center = position.baseline_y + (float(metrics['descent']) - float(metrics['ascent'])) * .5
                final['volume_signal_anchor'] = [position.offset_x, center]
    if (style.viewport_scale_pct != 100 or style.viewport_rotation_deg
            or style.viewport_offset_x or style.viewport_offset_y):
        ir['required_features'].append('p08_viewport')
        if style.vertical:
            ir['required_features'].append('p08_vertical_viewport')
            features = set(ir['required_features'])
            if 'p07_vertical_scanline_band' in features and not features.intersection({
                    'p07_vertical_reverse_scanline', 'p07_vertical_scanline_line_animation',
                    'p07_vertical_scanline_singing', 'p07_vertical_scanline_palette'}):
                ir['required_features'].append('p08_vertical_scanline_viewport')
    write_line_band(ir, style, prepared[0], width=kwargs['width'], height=kwargs['height'])
    compose_vector_guides(track, ir['required_features'])
    declare_bitmap_guides(plan.lines, ir, static_title=adjust_position is not None)
    if animated_title_guides:
        ir['required_features'].append('p08_animated_title_guides')
    return ir
