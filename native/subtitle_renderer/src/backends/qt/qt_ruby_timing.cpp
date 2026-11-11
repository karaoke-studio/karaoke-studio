#include "qt_ruby_timing.h"

#include "qt_character_animation.h"

#include <QTextBoundaryFinder>

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::legacy_qt {

using protocol::RubyAnnotation;

namespace {

std::vector<int> rubyReadingBoundaries(const RubyAnnotation &ruby, int unitCount) {
    if (unitCount <= 0) {
        return {ruby.posStartMs, ruby.posEndMs};
    }
    std::vector<int> boundaries{ruby.posStartMs};
    const int usableParts = std::max(unitCount - 1, 0);
    for (int i = 0; i < usableParts && static_cast<std::size_t>(i) < ruby.readingPartMs.size(); ++i) {
        int ts = ruby.posStartMs + ruby.readingPartMs[i];
        ts = std::max(boundaries.back(), std::min(ruby.posEndMs, ts));
        boundaries.push_back(ts);
    }
    if (static_cast<int>(boundaries.size()) < unitCount) {
        const int start = boundaries.back();
        const int remaining = unitCount - static_cast<int>(boundaries.size()) + 1;
        for (int step = 1; step < remaining; ++step) {
            boundaries.push_back(start + static_cast<int>(std::round((ruby.posEndMs - start) * step / static_cast<double>(remaining))));
        }
    }
    boundaries.push_back(std::max(boundaries.back(), ruby.posEndMs));
    return boundaries;
}

std::vector<std::pair<int, int>> rubyReadingIntervals(const RubyAnnotation &ruby) {
    const auto units = rubyReadingUnits(ruby.reading);
    const int unitCount = static_cast<int>(units.size());
    if (static_cast<int>(ruby.readingPartMs.size()) >= 2 * std::max(unitCount - 1, 0)) {
        std::vector<std::pair<int, int>> intervals;
        int currentStart = ruby.posStartMs;
        for (int i = 0; i < unitCount - 1; ++i) {
            int release = ruby.posStartMs + ruby.readingPartMs[i * 2];
            int nextStart = ruby.posStartMs + ruby.readingPartMs[i * 2 + 1];
            release = std::max(currentStart, std::min(release, ruby.posEndMs));
            nextStart = std::max(release, std::min(nextStart, ruby.posEndMs));
            intervals.push_back({currentStart, release});
            currentStart = nextStart;
        }
        intervals.push_back({currentStart, std::max(currentStart, ruby.posEndMs)});
        return intervals;
    }

    std::vector<std::pair<int, int>> intervals;
    const auto boundaries = rubyReadingBoundaries(ruby, unitCount);
    for (int i = 0; i < unitCount; ++i) {
        int start = boundaries[i];
        int end = boundaries[i + 1];
        if (end < start) {
            end = start;
        }
        intervals.push_back({start, end});
    }
    return intervals;
}

std::vector<RubyTimedUnit> rubyProgressPartsAndIntervals(
    const RubyAnnotation &ruby
) {
    QString joined;
    for (const QString &part : ruby.readingParts) {
        joined += part;
    }
    if (!ruby.readingParts.empty()
        && ruby.readingParts.size() == ruby.readingPartMs.size() + 1
        && joined == ruby.reading) {
        std::vector<int> anchors{ruby.posStartMs};
        for (int relativeMs : ruby.readingPartMs) {
            const int timestamp = ruby.posStartMs + relativeMs;
            anchors.push_back(std::max(
                anchors.back(), std::min(ruby.posEndMs, timestamp)
            ));
        }
        anchors.push_back(std::max(anchors.back(), ruby.posEndMs));
        std::vector<RubyTimedUnit> out;
        out.reserve(ruby.readingParts.size());
        for (std::size_t index = 0; index < ruby.readingParts.size(); ++index) {
            out.push_back({
                ruby.readingParts[index],
                {anchors[index], anchors[index + 1]},
            });
        }
        return out;
    }
    const auto units = rubyReadingUnits(ruby.reading);
    const auto intervals = rubyReadingIntervals(ruby);
    std::vector<RubyTimedUnit> out;
    const std::size_t count = std::min(units.size(), intervals.size());
    out.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        out.push_back({units[index], intervals[index]});
    }
    return out;
}

}  // namespace

std::vector<QString> rubyReadingUnits(const QString &reading) {
    return rubyUtopiaVisualUnits(reading);
}

std::vector<QString> rubyUtopiaVisualUnits(const QString &text) {
    std::vector<QString> units;
    QTextBoundaryFinder finder(QTextBoundaryFinder::Grapheme, text);
    int start = 0;
    bool pendingDouble = false;
    for (int end = finder.toNextBoundary(); end >= 0;
         end = finder.toNextBoundary()) {
        const QString cluster = text.mid(start, end - start);
        // SUG extends Unicode graphemes with IPA double-diacritic ties.
        // The mark belongs to both bases, so retain the following cluster.
        if (!units.empty() && pendingDouble && !cluster.isEmpty()
            && cluster.front().isLetter()) {
            units.back() += cluster;
        } else {
            units.push_back(cluster);
        }
        pendingDouble = false;
        for (const QChar ch : cluster) {
            const auto code = ch.unicode();
            if ((code >= 0x035C && code <= 0x0362)
                || code == 0x1DCD || code == 0x1DFC) {
                pendingDouble = true;
            } else if (!ch.isMark() && code != 0x200D) {
                pendingDouble = false;
            }
        }
        start = end;
    }
    return units;
}

std::vector<RubyTimedUnit> rubyUtopiaReadingUnitsAndIntervals(
    const RubyAnnotation &ruby
) {
    const auto parts = rubyProgressPartsAndIntervals(ruby);
    std::vector<RubyTimedUnit> out;
    for (const auto &part : parts) {
        const auto visualUnits = rubyUtopiaVisualUnits(part.first);
        if (visualUnits.empty()) {
            continue;
        }
        if (visualUnits.size() == 1) {
            out.push_back({visualUnits.front(), part.second});
            continue;
        }
        const int start = part.second.first;
        const int end = part.second.second;
        const int duration = std::max(end - start, 0);
        for (std::size_t j = 0; j < visualUnits.size(); ++j) {
            const int unitStart = start + duration * static_cast<int>(j)
                / static_cast<int>(visualUnits.size());
            const int unitEnd = start + duration * static_cast<int>(j + 1)
                / static_cast<int>(visualUnits.size());
            out.push_back({visualUnits[j], {unitStart, std::max(unitStart, unitEnd)}});
        }
    }
    return out;
}

double rubyProgressRatio(const RubyAnnotation &ruby, int tMs) {
    if (ruby.reading.isEmpty() || ruby.readingPartMs.empty()) {
        return progressRatio(ruby.posStartMs, ruby.posEndMs, tMs);
    }
    const auto intervals = rubyReadingIntervals(ruby);
    const int total = std::max(static_cast<int>(intervals.size()), 1);
    for (int i = 0; i < static_cast<int>(intervals.size()); ++i) {
        const int start = intervals[i].first;
        const int end = intervals[i].second;
        if (tMs < start) {
            return static_cast<double>(i) / total;
        }
        if (tMs < end) {
            return (i + progressRatio(start, end, tMs)) / total;
        }
    }
    return 1.0;
}

}  // namespace krok::subtitle::native::legacy_qt
