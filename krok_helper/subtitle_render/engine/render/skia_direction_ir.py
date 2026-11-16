"""Final clock ownership for reverse singing, independent of text direction."""


def validate_reverse_source(track, styles):
    if any(type(line.wipe_reverse) is not bool for line in track.lines):
        raise ValueError('Skia 反向走字标记必须是布尔值')
    if any(line.wipe_reverse for line in track.lines) and (
            track.rubies or any(style.lower_ruby_enabled for style in styles)):
        raise ValueError('Skia 反向走字暂不支持注音组合')


def project_reverse_wipe(ir):
    reverse = False
    for source, final in zip(ir['track']['lines'], ir['skia_layout']['lines'], strict=True):
        if not source.get('wipe_reverse', False):
            continue
        reverse = True
        cells = final['cells']
        if any('clock_index' in cell for cell in cells):
            raise ValueError('Skia 反向走字时钟不能重复投影')
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
