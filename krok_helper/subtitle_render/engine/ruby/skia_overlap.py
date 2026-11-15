"""Configuration-time GPU wipe endpoints for overlapping annotation boxes."""
from .skia_timing import _f32


def _endpoint(left, right, following, dpr):
    left, right, following = map(_f32, (left*dpr, right*dpr, following*dpr))
    if right < following:
        return 1.
    width = max(_f32(_f32(right-left)+1.), 1.)
    return max(0., min(_f32(_f32(following-left)/width), 1.))


def prepare_overlap_wipes(local, measured, timing, *, dpr=1):
    """Mirror the GPU's final-point adjustment without changing any time.

    The +1 in its width is a physical pixel. Ruby groups are ordered spatially
    and upper/lower layers are independent; group-complete still releases the
    clip. The terminal compiler owns the optional endpoint protocol feature.
    """
    import math
    if not math.isfinite(dpr) or dpr <= 0:
        raise ValueError('Skia 走字终点需要有效像素比例')
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
