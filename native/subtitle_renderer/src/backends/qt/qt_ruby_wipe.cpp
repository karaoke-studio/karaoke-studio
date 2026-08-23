#include "qt_ruby_wipe.h"

#include "qt_ruby_timing.h"

#include <algorithm>
#include <cmath>
#include <optional>

namespace krok::subtitle::native::legacy_qt {

using protocol::ResolvedStyle;
using protocol::RubyAnnotation;
using protocol::TimingChar;
using protocol::TimingLine;

namespace {

bool isUtopiaGroupMarker(const RubyAnnotation &ruby) {
    const QString reading = ruby.reading.trimmed();
    if (!reading.isEmpty() && reading != QStringLiteral("^")) {
        return false;
    }
    return std::all_of(
        ruby.readingParts.begin(), ruby.readingParts.end(),
        [](const QString &part) {
            const QString trimmed = part.trimmed();
            return trimmed.isEmpty() || trimmed == QStringLiteral("^");
        }
    );
}

std::vector<std::pair<int, int>> rubyMainWipeIntervals(
    const RubyAnnotation &ruby, const std::string &mode
) {
    if (mode == "reading_units") {
        const auto units = rubyUtopiaReadingUnitsAndIntervals(ruby);
        if (!units.empty()) {
            std::vector<std::pair<int, int>> out;
            out.reserve(units.size());
            for (const auto &unit : units) {
                out.push_back(unit.second);
            }
            return out;
        }
    }

    const int start = ruby.posStartMs;
    const int end = std::max(start, ruby.posEndMs);
    std::vector<int> anchors{start};
    for (int relativeMs : ruby.readingPartMs) {
        anchors.push_back(std::max(
            anchors.back(), std::min(end, start + relativeMs)
        ));
    }
    anchors.push_back(std::max(anchors.back(), end));
    std::vector<std::pair<int, int>> out;
    out.reserve(anchors.size() - 1);
    for (std::size_t index = 0; index + 1 < anchors.size(); ++index) {
        out.push_back({anchors[index], anchors[index + 1]});
    }
    return out;
}

int rubyMainProgressTimeAtRatio(
    const RubyAnnotation &ruby,
    double target,
    const QString &mode,
    bool rightSide
) {
    const auto intervals = rubyMainWipeIntervals(ruby, mode.toStdString());
    if (intervals.empty()) {
        return ruby.posEndMs;
    }
    target = std::clamp(target, 0.0, 1.0);
    std::vector<std::pair<int, double>> points;
    points.reserve(intervals.size() * 2);
    const int count = static_cast<int>(intervals.size());
    for (int index = 0; index < count; ++index) {
        points.push_back({
            intervals[static_cast<std::size_t>(index)].first,
            static_cast<double>(index) / count,
        });
        points.push_back({
            intervals[static_cast<std::size_t>(index)].second,
            static_cast<double>(index + 1) / count,
        });
    }
    std::optional<int> exact;
    for (const auto &point : points) {
        if (std::abs(point.second - target) < 0.000001) {
            exact = exact.has_value()
                ? (rightSide ? std::max(*exact, point.first)
                             : std::min(*exact, point.first))
                : point.first;
        }
    }
    if (exact.has_value()) {
        return *exact;
    }
    for (std::size_t index = 1; index < points.size(); ++index) {
        const auto &previous = points[index - 1];
        const auto &following = points[index];
        if (previous.second <= target && target <= following.second) {
            if (following.second <= previous.second || following.first <= previous.first) {
                return rightSide ? following.first : previous.first;
            }
            const double local = (target - previous.second)
                / (following.second - previous.second);
            return previous.first + static_cast<int>(std::round(
                (following.first - previous.first) * local
            ));
        }
    }
    return target <= 0.0 ? points.front().first : points.back().first;
}

bool rubyMainUsesBaseTiming(
    const TimingLine &line,
    const std::vector<int> &indices
) {
    std::vector<int> valid;
    valid.reserve(indices.size());
    for (int index : indices) {
        if (index >= 0 && index < static_cast<int>(line.chars.size())) {
            valid.push_back(index);
        }
    }
    if (valid.size() <= 1) {
        return false;
    }
    for (std::size_t offset = 0; offset < valid.size(); ++offset) {
        const TimingChar &ch = line.chars[static_cast<std::size_t>(valid[offset])];
        if (offset > 0 && ch.explicitStart) {
            return true;
        }
        if (offset + 1 < valid.size() && ch.explicitEnd) {
            return true;
        }
    }
    return false;
}

std::pair<int, int> utopiaWipeWindowForIndexImpl(
    const TimingLine &line,
    const LineLayout &layout,
    int index,
    const ResolvedStyle &style,
    const std::optional<RubyGroupInfo> &group,
    std::pair<int, int> fallback
) {
    if (!group.has_value()
        || isUtopiaGroupMarker(group->ruby)
        || rubyMainUsesBaseTiming(line, group->indices)
        || std::find(group->indices.begin(), group->indices.end(), index) == group->indices.end()) {
        return fallback;
    }
    double groupLeft = 0.0;
    double groupRight = 0.0;
    bool seen = false;
    for (int candidate : group->indices) {
        if (candidate < 0
            || static_cast<std::size_t>(candidate) >= layout.charLefts.size()
            || static_cast<std::size_t>(candidate) >= layout.charWidths.size()) {
            continue;
        }
        const double left = layout.charLefts[static_cast<std::size_t>(candidate)];
        const double right = left + layout.charWidths[static_cast<std::size_t>(candidate)];
        groupLeft = seen ? std::min(groupLeft, left) : left;
        groupRight = seen ? std::max(groupRight, right) : right;
        seen = true;
    }
    if (!seen || groupRight <= groupLeft
        || index < 0
        || static_cast<std::size_t>(index) >= layout.charLefts.size()
        || static_cast<std::size_t>(index) >= layout.charWidths.size()) {
        return fallback;
    }
    const double charLeft = layout.charLefts[static_cast<std::size_t>(index)];
    const double charRight = charLeft + layout.charWidths[static_cast<std::size_t>(index)];
    const double width = groupRight - groupLeft;
    const double startRatio = (charLeft - groupLeft) / width;
    const double endRatio = (charRight - groupLeft) / width;
    const int start = rubyMainProgressTimeAtRatio(
        group->ruby, startRatio, style.rubyMainProgressMode, true
    );
    const int end = rubyMainProgressTimeAtRatio(
        group->ruby, endRatio, style.rubyMainProgressMode, false
    );
    return {start, std::max(start, end)};
}

void applyRubyMainWipePoints(
    krok::subtitle::native::TextLine &line,
    int firstCharIndex,
    int lastCharIndex,
    const std::vector<std::pair<int, int>> &unitIntervals,
    int timingOffsetMs
) {
    using krok::subtitle::native::WipePoint;
    if (unitIntervals.empty()
        || firstCharIndex < 0
        || lastCharIndex < firstCharIndex
        || lastCharIndex >= static_cast<int>(line.chars.size())) {
        return;
    }
    const int baseCount = lastCharIndex - firstCharIndex + 1;
    const int unitCount = static_cast<int>(unitIntervals.size());
    std::vector<WipePoint> progressPoints;
    progressPoints.reserve(static_cast<std::size_t>(unitCount * 2));
    for (int unitIndex = 0; unitIndex < unitCount; ++unitIndex) {
        progressPoints.push_back(WipePoint{
            unitIntervals[static_cast<std::size_t>(unitIndex)].first + timingOffsetMs,
            static_cast<float>(unitIndex) / static_cast<float>(unitCount),
        });
        progressPoints.push_back(WipePoint{
            unitIntervals[static_cast<std::size_t>(unitIndex)].second + timingOffsetMs,
            static_cast<float>(unitIndex + 1) / static_cast<float>(unitCount),
        });
    }
    const auto timeAtProgress = [&](float target, bool rightSide) {
        std::optional<int> exact;
        for (const WipePoint &point : progressPoints) {
            if (std::abs(point.position - target) < 0.0001f) {
                exact = exact.has_value()
                    ? (rightSide ? std::max(*exact, point.timeMs)
                                 : std::min(*exact, point.timeMs))
                    : point.timeMs;
            }
        }
        if (exact.has_value()) {
            return *exact;
        }
        for (std::size_t index = 1; index < progressPoints.size(); ++index) {
            const WipePoint &previous = progressPoints[index - 1];
            const WipePoint &following = progressPoints[index];
            if (previous.position < target && target < following.position) {
                const float local = (target - previous.position)
                    / (following.position - previous.position);
                return previous.timeMs + static_cast<int>(std::round(
                    static_cast<float>(following.timeMs - previous.timeMs) * local
                ));
            }
        }
        return target <= 0.0f
            ? progressPoints.front().timeMs
            : progressPoints.back().timeMs;
    };
    for (int baseIndex = 0; baseIndex < baseCount; ++baseIndex) {
        auto &target = line.chars[static_cast<std::size_t>(firstCharIndex + baseIndex)];
        const float progressStart = static_cast<float>(baseIndex)
            / static_cast<float>(baseCount);
        const float progressEnd = static_cast<float>(baseIndex + 1)
            / static_cast<float>(baseCount);
        target.wipePoints.clear();
        target.wipePoints.push_back(WipePoint{
            timeAtProgress(progressStart, true), 0.0f,
        });
        for (const WipePoint &point : progressPoints) {
            if (point.position > progressStart + 0.0001f
                && point.position < progressEnd - 0.0001f) {
                target.wipePoints.push_back(WipePoint{
                    point.timeMs,
                    (point.position - progressStart)
                        / (progressEnd - progressStart),
                });
            }
        }
        target.wipePoints.push_back(WipePoint{
            timeAtProgress(progressEnd, false), 1.0f,
        });
        std::stable_sort(
            target.wipePoints.begin(), target.wipePoints.end(),
            [](const WipePoint &left, const WipePoint &right) {
                return left.timeMs < right.timeMs;
            }
        );
    }
}

}  // namespace

std::pair<int, int> utopiaWipeWindowForIndex(
    const TimingLine &line,
    const LineLayout &layout,
    int index,
    const ResolvedStyle &style,
    const std::optional<RubyGroupInfo> &group,
    std::pair<int, int> fallback
) {
    return utopiaWipeWindowForIndexImpl(
        line, layout, index, style, group, fallback
    );
}

bool applyRubyMainWipeProjection(
    krok::subtitle::native::TextLine &line,
    const TimingLine &sourceLine,
    const RubyAnnotation &ruby,
    const std::vector<int> &targetIndices,
    const std::string &progressMode,
    int timingOffsetMs
) {
    if (isUtopiaGroupMarker(ruby)) {
        return false;
    }
    if (!rubyMainUsesBaseTiming(sourceLine, targetIndices)) {
        const auto [minimum, maximum] = std::minmax_element(
            targetIndices.begin(), targetIndices.end()
        );
        if (minimum != targetIndices.end()) {
            applyRubyMainWipePoints(
                line,
                *minimum,
                *maximum,
                rubyMainWipeIntervals(ruby, progressMode),
                timingOffsetMs
            );
        }
    }
    return true;
}

}  // namespace krok::subtitle::native::legacy_qt
