"""Configuration-time GPU wipe endpoints for overlapping annotation boxes."""
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


def reverse_main_endpoints(cells, measured, *, dpr=1):
    """Main endpoints of a reverse-singing line, cells kept in source order.

    The GPU reverses its cells into singing order and always applies this
    adjustment, so each cell is compared with its LEFT neighbour and the
    leftmost (sung last) keeps the basic endpoint. Coordinates are local.
    """
    import math
    if not math.isfinite(dpr) or dpr <= 0:
        raise ValueError('Skia 走字终点需要有效像素比例')
    lefts = [c['origin_x']-offset for c, offset in zip(cells, measured.offsets, strict=True)]
    rights = [left+width for left, width in zip(lefts, measured.widths, strict=True)]
    return (1.,)*min(len(lefts), 1) + tuple(
        _reverse_endpoint(lefts[i], rights[i], rights[i-1], dpr) for i in range(1, len(lefts)))


def prepare_overlap_wipes(local, measured, timing, *, dpr=1, reverse=False):
    """Mirror the GPU's final-point adjustment without changing any time.

    The +1 in its width is a physical pixel. Ruby groups are ordered spatially
    and upper/lower layers are independent; group-complete still releases the
    clip. The terminal compiler owns the optional endpoint protocol feature.

    ``reverse`` lines use :func:`reverse_main_endpoints`. They never carry
    annotations in the existing GPU; receiving one here is a contract error.
    """
    import math
    if not math.isfinite(dpr) or dpr <= 0:
        raise ValueError('Skia 走字终点需要有效像素比例')
    if reverse and local.rubies:
        raise ValueError('Skia 反向走字行不能携带注音组（现有 GPU 省略该行注音）')
    if reverse:
        if len(timing.main) != len(local.main.cells):
            raise ValueError('Skia 反向走字终点与正文单元数量不一致')
        return reverse_main_endpoints(local.main.cells, measured, dpr=dpr), ()
    main = [1.]*len(timing.main)
    lefts = [c['origin_x']-offset for c, offset in zip(local.main.cells, measured.offsets, strict=True)]
    for i in range(len(lefts)-1):
        endpoint = _endpoint(lefts[i], lefts[i]+measured.widths[i], lefts[i+1], dpr)
        main[i] = endpoint
    ends = [[1.]*len(g.cells) for g in local.rubies]
    previous = None
    for index, group in sorted(enumerate(local.rubies), key=lambda p: (p[1].target.lower, min(p[1].target.indices))):
        if previous is not None and local.rubies[previous[0]].target.lower != group.target.lower:
            previous = None
        for unit, cell in enumerate(group.cells):
            if previous is not None:
                gi, ui = previous
                before = local.rubies[gi].cells[ui]
                ends[gi][ui] = _endpoint(before['layout_left'], before['layout_right'], cell['layout_left'], dpr)
            previous = index, unit
    return tuple(main), tuple(tuple(row) for row in ends)
