#pragma once

#include "../../effects/wipe_clock.h"
#include <algorithm>
#include <cstddef>
#include <vector>

namespace subtitle::skia {
template <class Cell> int wipeStart(const Cell& cell) {
    return cell.wipePoints.empty() ? cell.startMs : cell.wipePoints.front().timeMs;
}

template <class Cell> int wipeEnd(const Cell& cell) {
    return cell.wipePoints.empty() ? cell.endMs : cell.wipePoints.back().timeMs;
}

template <class Cell> bool lineWipeComplete(const std::vector<Cell>& cells, int time) {
    return !cells.empty() && std::all_of(cells.begin(), cells.end(), [time](const Cell& cell) {
        return time >= wipeEnd(cell);
    });
}

template <class Cell> float wipeCoordinate(const Cell& cell, int time, bool rightToLeft, bool vertical = false) {
    const float position = cell.wipePoints.empty()
        ? std::clamp(float(time - cell.startMs) / float(std::max(cell.endMs - cell.startMs, 1)), 0.f, 1.f)
        : krok::subtitle::native::effects::wipePositionAt(cell.wipePoints, time);
    const float low = vertical ? cell.top : cell.left;
    const float high = vertical ? cell.bottom : cell.right;
    const float width = high - low;
    return rightToLeft ? high - width * position : low + width * position;
}

// The global clip follows the active cell and rests between strokes in a gap.
template <class Cell> float lineWipeFront(const std::vector<Cell>& cells, int time, float inkStart,
                                        float restingPad, bool rightToLeft, bool vertical = false) {
    float edge = inkStart;
    for (std::size_t index = 0; index < cells.size(); ++index) {
        const auto& cell = cells[index];
        if (time < wipeStart(cell)) {
            if (index > 0) {
                const float start = wipeCoordinate(cell, time, rightToLeft, vertical);
                edge = rightToLeft ? std::min(edge, start + restingPad) : std::max(edge, start - restingPad);
            }
            break;
        }
        edge = wipeCoordinate(cell, time, rightToLeft, vertical);
        if (time <= wipeEnd(cell)) break;
    }
    return edge;
}
}
