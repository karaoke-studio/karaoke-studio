"""Configuration-time GPU wipe endpoints for every canonical horizontal line.

The existing GPU gives every main and annotation character basic wipe points
and always applies ``AdjustWipeEnd`` (``d2d_backend_configure.cpp``): main
cells chain in singing order, annotation units chain per layer across groups.
These functions prepare that single final endpoint once; no time changes.
"""
import math

from .skia_timing import _f32


def _endpoint(left, right, following, dpr):
    left, right, following = map(_f32, (left*dpr, right*dpr, following*dpr))
    if right < following:
        return 1.
    width = max(_f32(_f32(right-left)+1.), 1.)
    return max(0., min(_f32(_f32(following-left)/width), 1.))


def _reverse_endpoint(left, right, following_right, dpr):
    """The GPU's right-to-left rule, ``(right-following.right)/(width+1)``.

    It is the forward rule on x-negated boxes; float32 negation is exact, so
    every intermediate matches the GPU's float arithmetic.
    """
    return _endpoint(-right, -left, -following_right, dpr)


def _valid_dpr(dpr):
    if not math.isfinite(dpr) or dpr <= 0:
        raise ValueError('Skia 走字终点需要有效像素比例')


def _main_endpoints(cells, measured, dpr, reverse, right_to_left):
    lefts = [c['origin_x']-offset for c, offset in zip(cells, measured.offsets, strict=True)]
    rights = [left+width for left, width in zip(lefts, measured.widths, strict=True)]
    main = [1.]*len(lefts)
    for i in range(len(lefts)):
        following = i-1 if reverse else i+1
        if not 0 <= following < len(lefts):
            continue
        main[i] = (_reverse_endpoint(lefts[i], rights[i], rights[following], dpr)
                   if right_to_left != reverse else _endpoint(lefts[i], rights[i], lefts[following], dpr))
    return tuple(main)


def _ruby_endpoints(rubies, dpr):
    ends = [[1.]*len(g.cells) for g in rubies]
    previous = None
    for index, group in sorted(enumerate(rubies), key=lambda p: (p[1].target.lower, min(p[1].target.indices))):
        if previous is not None and rubies[previous[0]].target.lower != group.target.lower:
            previous = None
        for unit, cell in enumerate(group.cells):
            if previous is not None:
                gi, ui = previous
                before = rubies[gi].cells[ui]
                ends[gi][ui] = _endpoint(before['layout_left'], before['layout_right'], cell['layout_left'], dpr)
            previous = index, unit
    return tuple(tuple(row) for row in ends)


def line_endpoints(main_cells, measured, rubies=(), *, dpr=1, reverse=False, right_to_left=False):
    """Final ``(main, per-group annotation)`` endpoints of one prepared line.

    The +1 in the width is a physical pixel. Upper/lower layers are separate
    chains; group completion still releases the clip in the drawing core.
    Reverse lines never carry annotations in the existing GPU.
    """
    _valid_dpr(dpr)
    if reverse and rubies:
        raise ValueError('Skia 反向走字行不能携带注音组（现有 GPU 省略该行注音）')
    if right_to_left and rubies:
        raise ValueError('Skia 右至左单元排列暂不支持注音组合')
    return _main_endpoints(main_cells, measured, dpr, reverse, right_to_left), _ruby_endpoints(rubies, dpr)


def reverse_main_endpoints(cells, measured, *, dpr=1):
    """Main endpoints of a reverse-singing line, cells kept in source order."""
    return line_endpoints(cells, measured, dpr=dpr, reverse=True)[0]


def prepare_overlap_wipes(local, measured, timing, *, dpr=1, reverse=False):
    """Endpoints for a prepared annotation line; ``timing`` fixes the cell count."""
    if len(timing.main) != len(local.main.cells):
        raise ValueError('Skia 走字终点与正文单元数量不一致')
    return line_endpoints(local.main.cells, measured, local.rubies, dpr=dpr, reverse=reverse)
