"""Final clock ownership for reverse singing, independent of text direction.

Annotation contract: the shared selection (``active_rubies_for_line``), lower
derivation, Painter and the Qt->GPU projection all omit every upper/lower
reading on a ``wipe_reverse`` line. Source annotations stay as provenance; the
final reverse line therefore carries no ruby group.

Endpoint contract: the GPU always applies its right-to-left overlap endpoint
to reverse lines. Each source cell's ``wipe_end_position`` is prepared by
``skia_overlap.reverse_main_endpoints`` and stays with that cell's geometry;
only the clock (and its curve) moves to the mirrored owner.
"""


def validate_reverse_source(track):
    if any(type(line.wipe_reverse) is not bool for line in track.lines):
        raise ValueError('Skia 反向走字标记必须是布尔值')


def project_reverse_wipe(ir):
    reverse = overlap = False
    for source, final in zip(ir['track']['lines'], ir['skia_layout']['lines'], strict=True):
        if not source.get('wipe_reverse', False):
            continue
        reverse = True
        if final.get('ruby_groups'):
            raise ValueError('Skia 反向走字行不能携带注音组（现有 GPU 省略该行注音）')
        cells = final['cells']
        if any('clock_index' in cell for cell in cells):
            raise ValueError('Skia 反向走字时钟不能重复投影')
        if any('wipe_end_position' not in cell for cell in cells):
            raise ValueError('Skia 反向走字缺少 GPU 重叠终点')
        overlap = overlap or bool(cells)
        clocks = [cell.get('wipe_points') for cell in cells]
        for index, cell in enumerate(cells):
            owner = len(cells)-1-index
            cell['clock_index'] = owner
            cell.pop('wipe_points', None)
            if clocks[owner] is not None:
                cell['wipe_points'] = clocks[owner]
    if reverse:
        ir['required_features'].append('p05_reverse_wipe')
        ir['skia_layout']['reverse_wipe_schema'] = 1
    # Endpoints on reverse cells or reverse lines inside an annotation layout
    # are new to 14.36 sidecars, which must be rejected before selection.
    if overlap or (reverse and 'p05_resolved_ruby' in ir['required_features']):
        ir['required_features'].append('p05_reverse_overlap')
        ir['skia_layout']['reverse_overlap_schema'] = 1
    if overlap and ('p05_overlap_wipe' not in ir['required_features']
                    or ir['skia_layout'].get('overlap_schema') != 1):
        raise ValueError('Skia 反向走字终点缺少重叠走字能力声明')
