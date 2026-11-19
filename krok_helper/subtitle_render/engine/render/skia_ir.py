"""Canonical metrics -> existing semantic plan -> final Skia IR.

Font discovery and process IPC are injected configuration capabilities. No Qt
measurement or rendering is used here, and frame draws never call this module.
"""
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.display.section_edges import section_edge_context
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import positioned_cells, write_horizontal_pivots
from krok_helper.subtitle_render.engine.layout.line.skia_vertical import (
    validate_vertical_source, validate_vertical_style, vertical_cells, vertical_japanese_glyphs, vertical_solid_schema)
from krok_helper.subtitle_render.engine.layout.plan.skia_plan import prepare_skia_layout
from krok_helper.subtitle_render.engine.text.skia_batch import font_snapshot
from .render_ir import build_render_ir
from .skia_ruby_ir import prepared_ruby_layout
from .skia_vertical_ruby_ir import vertical_ruby_layout
from krok_helper.subtitle_render.engine.guide.skia_vector import validate_vector_guides
from .skia_lower_ir import lower_sources
from .skia_particle_ir import ENERGY_TRANSITIONS, FALLING_PARTICLES, PARTICLE_TRANSITIONS, write_particle_anchors
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


def _validate_scanline_band(track, style, lines):
    """Validate the horizontal solid-color band."""
    from krok_helper.subtitle_render.engine.guide.semantics import render_line_with_guide_symbols
    for source, line in zip(track.lines, lines, strict=True):
        if not line.get('scanline'):
            continue
        look = style_for_line(style, source)
        if (look.scanline_shape != 'band' or look.vertical or look.right_to_left
                or line.get('karaoke_anim') != 'none' or line.get('zoom_pulse') or line.get('stroke_flash')
                or track.rubies or look.lower_ruby_enabled
                or any(c.role_label is not None or c.vector_glyph is not None
                       for c in render_line_with_guide_symbols(source).chars)):
            raise ValueError('Skia 扫字线当前仅支持横排实色高亮带')


def _validate_stroke_flash(track, style, lines):
    for source, line in zip(track.lines, lines, strict=True):
        if not line.get('stroke_flash'):
            continue
        look = style_for_line(style, source)
        if (look.vertical or look.right_to_left or source.wipe_reverse or track.rubies or look.lower_ruby_enabled
                or line.get('entry_anim') != 'none' or line.get('exit_anim') != 'none'
                or line.get('karaoke_anim') != 'none' or line.get('scanline') or line.get('fx_bursts')
                or any(c.role_label is not None or c.vector_glyph is not None for c in source.chars)):
            raise ValueError('Skia 唱字描边闪光当前仅支持正向横排实色正文')


def _validate_noise_transition(track, style, lines):
    for source, line in zip(track.lines, lines, strict=True):
        entry, exit_ = line.get('entry_anim'), line.get('exit_anim')
        if 'noise' not in (entry, exit_):
            continue
        look = style_for_line(style, source)
        if (look.vertical or look.right_to_left or source.wipe_reverse or track.rubies or look.lower_ruby_enabled
                or entry not in {'noise', 'none'} or exit_ not in {'noise', 'none'}
                or line.get('karaoke_anim') != 'none' or line.get('scanline') or line.get('stroke_flash')
                or line.get('fx_bursts')
                or any(c.role_label is not None or c.vector_glyph is not None for c in source.chars)):
            raise ValueError('Skia 噪声出入场当前仅支持正向横排实色正文')


_HALO_TRANSFORMS = {'stretch_in', 'stretch_out', 'glow_in', 'glow_out'}
_GEOMETRY_TRANSFORMS = {'tracking_in', 'wave_in', 'scatter_out', 'converge_out'} | _HALO_TRANSFORMS | PARTICLE_TRANSITIONS.keys()
_TRANSFORMS = {'char_drip', 'spin_flip'} | _GEOMETRY_TRANSFORMS


def _character_transforms(track, ir, plan, measured) -> int:
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
        if (look.vertical or look.right_to_left or line.wipe_reverse or source.get('karaoke_anim') != 'none'
                or source.get('scanline') or track.rubies or look.lower_ruby_enabled
                or any(c.role_label is not None or c.vector_glyph is not None
                       for c in render_line_with_guide_symbols(line).chars)):
            raise ValueError('Skia 逐字滴落/翻转当前仅支持正向横排正文')
        entry = source.get('entry_anim') in _TRANSFORMS and source['entry_duration_ms'] > 0
        exit_ = source.get('exit_anim') in _TRANSFORMS and source['exit_duration_ms'] > 0
        if not (entry or exit_):
            continue
        if geometry and (look.stroke_width_px > 0 or (look.stroke2_enabled and look.stroke2_width_px > 0)):
            raise ValueError('Skia 逐字几何过渡当前仅支持无描边正文')
        cells, metrics = ir['skia_layout']['lines'][index]['cells'], measured.lines[item.track_index]
        write_horizontal_pivots(cells, metrics)
    return version


def build_skia_render_ir(track, style, *, measure, resolve_font, **kwargs):
    if kwargs.get('extra_tracks'):
        raise ValueError('Skia 当前规范布局尚不支持附加轨道')
    prepared = []

    def build_plan(source, source_style, **plan_kwargs):
        _validate_style(source_style)
        if source_style.right_to_left and (source.rubies or source_style.lower_ruby_enabled):
            raise ValueError('Skia 右至左单元排列暂不支持注音组合')
        if source_style.right_to_left:
            if any(len(char.text) > 1 and not _validated_rtl_cell(char.text) for line in source.lines for char in line.chars):
                raise ValueError('Skia 右至左排列的多码点单元包含尚未验证的字符')
        validate_reverse_source(source)
        if source_style.vertical:
            validate_vertical_source(source)
        validate_vector_guides(source, source_style)
        with layout_pass():
            section_edge_context(source, source_style)
            for line in source.lines:
                _validate_style(style_for_line(source_style, line))
            result = prepare_skia_layout(source, source_style, measure=measure, resolve_font=resolve_font, **plan_kwargs)
        prepared.append(result)
        return result.plan

    with font_snapshot(resolve_font):  # Every plan of this IR shares one content snapshot.
        ir = build_render_ir(track, style, layout_plan_builder=build_plan,
                             serialize_source_baselines=False, **kwargs)
        vertical_ruby = None
        if style.vertical and track.rubies:
            vertical_ruby = vertical_ruby_layout(prepared[0], width=kwargs['width'], height=kwargs['height'],
                                                dpr=kwargs.get('dpr', 1), resolve_font=resolve_font, measure=measure)
    plan, measured, geometry = prepared[0].plan, prepared[0].measured, prepared[0].geometry
    geometry.set_lanes(plan.lines)
    lines = []
    if vertical_ruby is None:
        for index, item in enumerate(plan.lines):
            if item.layout_style.vertical:
                lines.append({'cells': vertical_cells(geometry.locals[item.track_index], measured.lines[item.track_index], item,
                                                      width=kwargs['width'], height=kwargs['height']),
                              'text_flow': 'vertical'})
                continue
            position = geometry.position(item, kwargs['width'], kwargs['height'])
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
    if vertical_ruby is not None:
        ir['skia_layout'].update(vertical_ruby)
        ir['required_features'] += ['p05_resolved_ruby', 'p05_upper_ruby', 'p05_vertical_ruby']
    secondary_outline = any(item.layout_style.stroke2_enabled and item.layout_style.stroke2_width_px > 0
                            for item in plan.lines)
    if secondary_outline or any(item.layout_style.stroke_width_px > 0 for item in plan.lines):
        ir['required_features'].append('p06_primary_solid_outline')
    if secondary_outline:
        ir['required_features'].append('p06_secondary_solid_outline')
    if any(char.vector_glyph is not None for item in plan.lines for char in item.render_line.chars):
        ir['required_features'].append('p05_vector_guides')
        ir['skia_layout']['vector_guide_schema'] = 1
    if any(line.get('entry_anim') == 'fade' or line.get('exit_anim') == 'fade'
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_line_fade')
        ir['skia_layout']['line_fade_schema'] = 1
    if any(line.get('entry_anim') in {'slide_in', 'rise'} or line.get('exit_anim') in {'slide_out', 'rise'}
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_line_motion')
        ir['skia_layout']['line_motion_schema'] = 1
    if any(line.get('entry_anim') == 'char_fade' or line.get('exit_anim') == 'char_fade'
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_character_fade')
        ir['skia_layout']['character_fade_schema'] = 1
    if any({line.get('entry_anim'), line.get('exit_anim')} & _TRANSFORMS for line in ir['track']['lines']):
        ir['skia_layout']['character_transform_schema'] = _character_transforms(track, ir, plan, measured)
        ir['required_features'].append('p07_character_transform')
    if any(line.get('scanline') for line in ir['track']['lines']):
        _validate_scanline_band(track, style, ir['track']['lines'])
        ir['required_features'].append('p07_scanline_band')
        ir['skia_layout']['scanline_band_schema'] = (
            4 if any(line.get('scanline') and line.get('wipe_reverse') for line in ir['track']['lines']) else 3
        )
    gradients, outlined_gradients, images = write_body_gradient_bounds(track, ir, plan)
    if images:
        # Static image paints on any body or outline layer of a plain line.
        ir['required_features'].append('p06_image_fill')
    if gradients:
        ir['required_features'].append('p06_body_gradient')
    if outlined_gradients:
        # Gradient paint on, or under, a solid or gradient outline stack.
        ir['required_features'].append('p06_outline_gradient')
    if any(line.get('stroke_flash') for line in ir['track']['lines']):
        _validate_stroke_flash(track, style, ir['track']['lines'])
        ir['required_features'].append('p07_stroke_flash')
    if any('noise' in (line.get('entry_anim'), line.get('exit_anim')) for line in ir['track']['lines']):
        _validate_noise_transition(track, style, ir['track']['lines'])
        ir['required_features'].append('p07_noise_transition')
    if any(line.get('fx_bursts') for line in ir['track']['lines']):
        ir['skia_layout']['sing_twinkle_schema'] = write_particle_anchors(track, ir, plan, measured)
        ir['required_features'].append('p07_sing_twinkle')
    if style.vertical:
        ir['required_features'].append('p05_vertical_solid')
        ir['skia_layout']['vertical_solid_schema'] = vertical_solid_schema(track)
        if vertical_japanese_glyphs(track):
            ir['required_features'].append('p05_vertical_japanese_glyphs')
            ir['skia_layout']['vertical_japanese_glyph_schema'] = 1
    if style.right_to_left:
        ir['required_features'].append('p05_horizontal_rtl')
        ir['skia_layout']['horizontal_rtl_schema'] = 1
        for final in lines:
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
    if geometry.ruby is not None:
        lower_enabled = any(item.layout_style.lower_ruby_enabled for item in plan.lines)
        sources, indices = lower_sources(prepared[0])
        ir['skia_layout'] = prepared_ruby_layout(prepared[0], width=kwargs['width'], height=kwargs['height'],
            dpr=kwargs.get('dpr', 1), lower_source_indices=indices if lower_enabled else None)
        ir['track']['rubies'].extend(sources)
        ir['required_features'] += ['p05_resolved_ruby', 'p05_upper_ruby']
        markers = any(is_utopia_group_marker(ruby) for ruby in track.rubies)
        if markers:
            ir['required_features'].append('p05_ruby_markers')
        if 'ruby_unit_schema' in ir['skia_layout']:
            ir['required_features'].append('p05_ruby_units')
        if 'standalone_mark_schema' in ir['skia_layout']:
            ir['required_features'].append('p05_standalone_marks')
        if lower_enabled:
            ir['required_features'].append('p05_lower_ruby')
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
    # Attach the outline schemas to the final layout, including ruby's separate layout projection.
    if 'p06_primary_solid_outline' in ir['required_features']:
        ir['skia_layout']['primary_solid_outline_schema'] = 1
        if 'p05_resolved_ruby' in ir['required_features']:
            ir['required_features'].append('p06_outline_ruby')
    if secondary_outline:
        ir['skia_layout']['secondary_solid_outline_schema'] = 1
    project_reverse_wipe(ir)
    return ir
