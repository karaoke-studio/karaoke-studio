#include "qt_display_plan.h"

#include <algorithm>
#include <limits>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::TimingLine;

QString lineText(const TimingLine &line) {
    QString text;
    for (const auto &ch : line.chars) {
        text += ch.text;
    }
    return text;
}

bool lineHasRoleLabels(const TimingLine &line) {
    for (const auto &ch : line.chars) {
        if (!ch.roleLabel.isEmpty()) {
            return true;
        }
    }
    return false;
}


int lineStartMs(const TimingLine &line) {
    if (line.chars.empty()) {
        return 0;
    }
    return line.chars.front().startMs;
}

bool lineVisible(const TimingLine &line, int tMs, const RenderConfig &cfg) {
    if (line.chars.empty()) {
        return false;
    }
    const int start = lineStartMs(line) - cfg.lineLeadInMs;
    const int end = std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs;
    return start <= tMs && tMs <= end;
}

int lineEndMs(const TimingLine &line) {
    if (line.endMs > 0) {
        return line.endMs;
    }
    if (!line.chars.empty()) {
        return line.chars.back().startMs + 1000;
    }
    return 0;
}

int effectiveLineProtectMs(const RenderConfig &cfg) {
    if (cfg.lineProtectMs > 0) {
        return cfg.lineProtectMs;
    }
    const int base = std::min(std::max(cfg.lineLeadInMs, 0), std::max(cfg.lineTailMs, 0)) / 2;
    return std::max(base, std::max(cfg.exitFadeMs, 0));
}

int pairSingEndMs(const std::vector<const TimingLine *> &lines, int index) {
    const int pairStart = (index / 2) * 2;
    int best = 0;
    for (int i = pairStart; i < std::min(pairStart + 2, static_cast<int>(lines.size())); ++i) {
        best = std::max(best, lineEndMs(*lines[static_cast<std::size_t>(i)]));
    }
    return best;
}

std::vector<int> computeSectionIds(const std::vector<const TimingLine *> &lines, int sectionGapMs) {
    std::vector<int> sectionIds;
    sectionIds.reserve(lines.size());
    int current = 0;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        if (i > 0 && sectionGapMs > 0) {
            const int gap = lineStartMs(*lines[i]) - lineEndMs(*lines[i - 1]);
            if (gap > sectionGapMs) {
                ++current;
            }
        }
        sectionIds.push_back(current);
    }
    return sectionIds;
}

QHash<int, int> computeSectionEnds(
    const std::vector<const TimingLine *> &lines,
    const std::vector<int> &sectionIds,
    int tailMs
) {
    QHash<int, int> ends;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        const int sid = sectionIds[i];
        const int end = lineEndMs(*lines[i]) + std::max(tailMs, 0);
        ends.insert(sid, std::max(ends.value(sid, end), end));
    }
    return ends;
}

bool isLastInLaneInSection(
    const std::vector<int> &lanes,
    const std::vector<int> &sectionIds,
    int index
) {
    const int lane = lanes[static_cast<std::size_t>(index)];
    const int sid = sectionIds[static_cast<std::size_t>(index)];
    for (int i = index + 1; i < static_cast<int>(lanes.size()); ++i) {
        if (sectionIds[static_cast<std::size_t>(i)] != sid) {
            break;
        }
        if (lanes[static_cast<std::size_t>(i)] == lane) {
            return false;
        }
    }
    return true;
}

void adjustSameLaneDisplayWindows(
    const std::vector<const TimingLine *> &lines,
    std::vector<int> *starts,
    std::vector<int> *displayEnds,
    const std::vector<int> &lanes,
    int protectMs,
    int laneGapMs
) {
    QHash<int, int> previousByLane;
    for (int index = 0; index < static_cast<int>(lines.size()); ++index) {
        const int lane = lanes[static_cast<std::size_t>(index)];
        if (!previousByLane.contains(lane)) {
            previousByLane.insert(lane, index);
            continue;
        }

        const int previous = previousByLane.value(lane);
        if ((*displayEnds)[static_cast<std::size_t>(previous)] + laneGapMs <= (*starts)[static_cast<std::size_t>(index)]) {
            previousByLane.insert(lane, index);
            continue;
        }

        const int previousFloor = lineEndMs(*lines[static_cast<std::size_t>(previous)]) + protectMs;
        const int currentProtectStart = std::max(lineStartMs(*lines[static_cast<std::size_t>(index)]) - protectMs, 0);

        (*displayEnds)[static_cast<std::size_t>(previous)] = std::max(
            previousFloor,
            (*starts)[static_cast<std::size_t>(index)] - laneGapMs
        );
        if ((*displayEnds)[static_cast<std::size_t>(previous)] + laneGapMs <= (*starts)[static_cast<std::size_t>(index)]) {
            previousByLane.insert(lane, index);
            continue;
        }

        const int targetStart = (*displayEnds)[static_cast<std::size_t>(previous)] + laneGapMs;
        const int latestStart = std::max(currentProtectStart, (*starts)[static_cast<std::size_t>(index)]);
        (*starts)[static_cast<std::size_t>(index)] = std::min(
            std::max((*starts)[static_cast<std::size_t>(index)], targetStart),
            latestStart
        );
        if ((*displayEnds)[static_cast<std::size_t>(previous)] + laneGapMs <= (*starts)[static_cast<std::size_t>(index)]) {
            previousByLane.insert(lane, index);
            continue;
        }

        if ((*displayEnds)[static_cast<std::size_t>(previous)] <= (*starts)[static_cast<std::size_t>(index)]) {
            previousByLane.insert(lane, index);
            continue;
        }

        if ((*displayEnds)[static_cast<std::size_t>(previous)] <= currentProtectStart) {
            (*starts)[static_cast<std::size_t>(index)] = (*displayEnds)[static_cast<std::size_t>(previous)];
        }
        previousByLane.insert(lane, index);
    }
}

std::vector<DisplayLineRef> computeDisplayLines(const RenderConfig &cfg) {
    std::vector<const TimingLine *> lines;
    lines.reserve(cfg.lines.size());
    for (const auto &line : cfg.lines) {
        if (!line.chars.empty()) {
            lines.push_back(&line);
        }
    }
    if (lines.empty()) {
        return {};
    }

    const int lead = std::max(cfg.lineLeadInMs, 0);
    const int tail = std::max(cfg.lineTailMs, 0);
    const int protect = effectiveLineProtectMs(cfg);
    const int laneGap = std::max(cfg.lineLaneGapMs, 0);
    const int snap = std::max(cfg.lineContinuitySnapMs, 0);
    const int pairSecondDelay = std::max(cfg.linePairSecondDelayMs, 0);
    const int maxHold = std::max(cfg.lineMaxHoldMs, 0);
    const int sectionGap = std::max(cfg.sectionGapMs, 0);

    const std::vector<int> sectionIds = computeSectionIds(lines, sectionGap);
    const QHash<int, int> sectionEnds = computeSectionEnds(lines, sectionIds, tail);

    std::vector<int> starts;
    std::vector<int> naturalEnds;
    std::vector<int> lanes;
    starts.reserve(lines.size());
    naturalEnds.reserve(lines.size());
    lanes.reserve(lines.size());
    QHash<int, int> prevLaneNaturalEnd;

    for (int index = 0; index < static_cast<int>(lines.size()); ++index) {
        const TimingLine &line = *lines[static_cast<std::size_t>(index)];
        const int lane = index % 2;
        lanes.push_back(lane);
        int preferredStart = std::max(lineStartMs(line) - lead, 0);
        if (
            index % 2 == 1
            && !starts.empty()
            && sectionIds[static_cast<std::size_t>(index)] == sectionIds[static_cast<std::size_t>(index - 1)]
        ) {
            preferredStart = std::min(preferredStart, starts[static_cast<std::size_t>(index - 1)] + pairSecondDelay);
        }

        int naturalEnd = pairSingEndMs(lines, index) + tail;
        if (maxHold > 0) {
            naturalEnd = std::min(naturalEnd, preferredStart + maxHold);
        }
        naturalEnds.push_back(naturalEnd);

        int displayStart = preferredStart;
        if (prevLaneNaturalEnd.contains(lane)) {
            const int availableStart = prevLaneNaturalEnd.value(lane) + laneGap;
            displayStart = std::abs(preferredStart - availableStart) <= snap
                ? availableStart
                : preferredStart;
        }
        starts.push_back(displayStart);
        prevLaneNaturalEnd.insert(lane, naturalEnd);
    }

    std::vector<int> displayEnds;
    displayEnds.reserve(lines.size());
    for (int index = 0; index < static_cast<int>(lines.size()); ++index) {
        const int floorEnd = lineEndMs(*lines[static_cast<std::size_t>(index)]) + protect;
        int displayEnd = std::max(naturalEnds[static_cast<std::size_t>(index)], floorEnd);
        if (maxHold > 0) {
            displayEnd = std::max(floorEnd, std::min(displayEnd, starts[static_cast<std::size_t>(index)] + maxHold));
        }
        displayEnds.push_back(displayEnd);
    }

    adjustSameLaneDisplayWindows(lines, &starts, &displayEnds, lanes, protect, laneGap);

    std::vector<DisplayLineRef> result;
    result.reserve(lines.size());
    for (int index = 0; index < static_cast<int>(lines.size()); ++index) {
        const int sid = sectionIds[static_cast<std::size_t>(index)];
        const int floorEnd = lineEndMs(*lines[static_cast<std::size_t>(index)]) + protect;
        int displayEnd = std::max(displayEnds[static_cast<std::size_t>(index)], floorEnd);
        if (maxHold > 0) {
            displayEnd = std::max(floorEnd, std::min(displayEnd, starts[static_cast<std::size_t>(index)] + maxHold));
        }
        if (cfg.syncEnding && isLastInLaneInSection(lanes, sectionIds, index)) {
            displayEnd = std::max(displayEnd, sectionEnds.value(sid, displayEnd));
        }
        if (cfg.sectionEndingMode == QStringLiteral("clear")) {
            displayEnd = std::max(floorEnd, std::min(displayEnd, sectionEnds.value(sid, displayEnd)));
        }
        if (displayEnd < starts[static_cast<std::size_t>(index)]) {
            displayEnd = starts[static_cast<std::size_t>(index)];
        }
        result.push_back(DisplayLineRef{
            lines[static_cast<std::size_t>(index)],
            lanes[static_cast<std::size_t>(index)],
            starts[static_cast<std::size_t>(index)],
            displayEnd,
        });
    }
    return result;
}

std::vector<DisplayLineRef> visibleDisplayLines(const RenderConfig &cfg, int tMs) {
    if (!cfg.dualLineLayout) {
        std::vector<DisplayLineRef> candidates;
        candidates.reserve(cfg.lines.size());
        for (const auto &line : cfg.lines) {
            if (!line.chars.empty() && lineVisible(line, tMs, cfg)) {
                candidates.push_back(DisplayLineRef{
                    &line,
                    0,
                    std::max(lineStartMs(line) - cfg.lineLeadInMs, 0),
                    std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs,
                });
            }
        }
        if (candidates.empty()) {
            return {};
        }
        std::stable_sort(candidates.begin(), candidates.end(), [](const DisplayLineRef &left, const DisplayLineRef &right) {
            return lineStartMs(*left.line) > lineStartMs(*right.line);
        });
        return {candidates.front()};
    }

    const auto all = computeDisplayLines(cfg);
    std::vector<DisplayLineRef> visible;
    visible.reserve(all.size());
    for (const auto &item : all) {
        if (item.line != nullptr && item.displayStartMs <= tMs && tMs < item.displayEndMs) {
            visible.push_back(item);
        }
    }
    return visible;
}

}  // namespace krok::subtitle::native::legacy_qt

