#include "qt_ruby_target.h"

#include <algorithm>
#include <limits>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::RubyAnnotation;
using protocol::TimingLine;

namespace {

std::vector<int> rubyTimeIndices(
    const RubyAnnotation &ruby,
    const std::vector<std::pair<int, int>> &intervals
) {
    std::vector<int> indices;
    for (std::size_t i = 0; i < intervals.size(); ++i) {
        if (intervals[i].first < ruby.posEndMs && intervals[i].second > ruby.posStartMs) {
            indices.push_back(static_cast<int>(i));
        }
    }
    return indices;
}

QString lineFullText(const TimingLine &line) {
    QString text;
    for (const auto &ch : line.chars) {
        text += ch.text;
    }
    return text;
}

std::vector<int> textSpanIndices(const std::pair<int, int> &span, const TimingLine &line) {
    std::vector<int> indices;
    int cursor = 0;
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const int unitStart = cursor;
        const int unitEnd = cursor + line.chars[i].text.size();
        cursor = unitEnd;
        if (unitStart < span.second && unitEnd > span.first) {
            indices.push_back(static_cast<int>(i));
        }
    }
    return indices;
}

std::optional<std::pair<int, int>> findRubyTextSpan(
    const QString &kanji,
    const TimingLine &line,
    const std::vector<int> &preferredIndices
) {
    if (kanji.isEmpty()) {
        return std::nullopt;
    }
    const QString text = lineFullText(line);
    std::vector<std::pair<int, int>> occurrences;
    int pos = text.indexOf(kanji);
    while (pos >= 0) {
        occurrences.push_back({pos, pos + kanji.size()});
        pos = text.indexOf(kanji, pos + 1);
    }
    if (occurrences.empty()) {
        return std::nullopt;
    }
    if (preferredIndices.empty()) {
        return occurrences.front();
    }

    std::pair<int, int> best = occurrences.front();
    std::pair<int, int> bestScore{-1, std::numeric_limits<int>::min()};
    for (const auto &span : occurrences) {
        const auto indices = textSpanIndices(span, line);
        int overlap = 0;
        int distance = std::numeric_limits<int>::max();
        for (int index : indices) {
            for (int preferred : preferredIndices) {
                if (index == preferred) {
                    ++overlap;
                }
                distance = std::min(distance, std::abs(index - preferred));
            }
        }
        if (distance == std::numeric_limits<int>::max()) {
            distance = 0;
        }
        const std::pair<int, int> score{overlap, -distance};
        if (score > bestScore) {
            bestScore = score;
            best = span;
        }
    }
    return best;
}

}  // namespace

std::vector<int> rubyTargetIndices(
    const RubyAnnotation &ruby,
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals
) {
    // Rubies from every source and line are flattened into RenderConfig. Keep
    // ownership at this shared resolution boundary so CPU paint, diagnostics,
    // Utopia and GPU scene construction cannot accidentally apply different
    // filtering rules to overlapping or repeated lyrics.
    if (ruby.sourceIndex != line.sourceIndex) {
        return {};
    }
    if (ruby.targetLineIndex >= 0
        && line.trackLineIndex >= 0
        && ruby.targetLineIndex != line.trackLineIndex) {
        return {};
    }
    // Mirrors Painter's _ruby_explicit_target_indices: the loader already knows
    // the exact characters for .sug per-character ruby, and searching by text
    // instead collapses every repeat of one base onto its first occurrence.
    // Both guards below keep an annotation resolved against different line
    // contents from landing on an unrelated character.
    if (ruby.targetCharStart >= 0
        && ruby.targetCharEnd > ruby.targetCharStart
        && ruby.targetCharEnd <= static_cast<int>(line.chars.size())) {
        QString targetText;
        for (int index = ruby.targetCharStart; index < ruby.targetCharEnd; ++index) {
            targetText += line.chars[static_cast<std::size_t>(index)].text;
        }
        if (ruby.kanji.isEmpty() || targetText == ruby.kanji) {
            std::vector<int> explicitIndices;
            explicitIndices.reserve(
                static_cast<std::size_t>(ruby.targetCharEnd - ruby.targetCharStart)
            );
            for (int index = ruby.targetCharStart; index < ruby.targetCharEnd; ++index) {
                explicitIndices.push_back(index);
            }
            return explicitIndices;
        }
    }
    const auto timeIndices = rubyTimeIndices(ruby, intervals);
    if (!ruby.kanji.isEmpty()) {
        const auto span = findRubyTextSpan(ruby.kanji, line, timeIndices);
        if (!span.has_value()) {
            return {};
        }
        const auto indices = textSpanIndices(span.value(), line);
        const bool globalPosition = ruby.posStartMs == 0 && ruby.posEndMs == 0;
        if (!globalPosition) {
            const bool overlapsTimedTarget = std::any_of(
                indices.begin(), indices.end(), [&](int index) {
                    return std::find(
                        timeIndices.begin(), timeIndices.end(), index
                    ) != timeIndices.end();
                }
            );
            if (!overlapsTimedTarget) {
                return {};
            }
        }
        return indices;
    }
    return timeIndices;
}

RubyAnnotation effectiveRubyForTarget(
    const RubyAnnotation &ruby,
    const std::vector<int> &indices,
    const std::vector<std::pair<int, int>> &intervals
) {
    std::vector<int> validIndices;
    for (int index : indices) {
        if (index >= 0 && static_cast<std::size_t>(index) < intervals.size()) {
            validIndices.push_back(index);
        }
    }
    if (validIndices.empty()) {
        return ruby;
    }
    int start = intervals[validIndices.front()].first;
    int end = intervals[validIndices.front()].second;
    for (int index : validIndices) {
        start = std::min(start, intervals[index].first);
        end = std::max(end, intervals[index].second);
    }
    if (start == ruby.posStartMs && end == ruby.posEndMs) {
        return ruby;
    }
    RubyAnnotation out = ruby;
    out.posStartMs = start;
    out.posEndMs = end;
    const int duration = std::max(end - start, 0);
    for (int &relMs : out.readingPartMs) {
        relMs = std::max(0, std::min(duration, relMs));
    }
    return out;
}

std::optional<std::pair<double, double>> rubyTargetXRange(
    const RubyAnnotation &ruby,
    const TimingLine &line,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals
) {
    // Same precedence as rubyTargetIndices: an explicit loader target wins over
    // the text search so repeats of one base keep their own x.
    const auto resolved = rubyTargetIndices(ruby, line, intervals);
    if (!resolved.empty()
        && ruby.targetCharStart >= 0
        && ruby.targetCharEnd > ruby.targetCharStart
        && resolved.front() == ruby.targetCharStart) {
        double left = std::numeric_limits<double>::max();
        double right = std::numeric_limits<double>::lowest();
        for (int index : resolved) {
            if (index < 0 || index >= static_cast<int>(layout.charLefts.size())) {
                continue;
            }
            left = std::min(left, layout.charLefts[static_cast<std::size_t>(index)]);
            right = std::max(
                right,
                layout.charLefts[static_cast<std::size_t>(index)]
                    + layout.charWidths[static_cast<std::size_t>(index)]
            );
        }
        if (left <= right) {
            return std::make_pair(left, right);
        }
    }
    if (!ruby.kanji.isEmpty()) {
        const auto timeIndices = rubyTimeIndices(ruby, intervals);
        const auto span = findRubyTextSpan(ruby.kanji, line, timeIndices);
        if (!span.has_value()) {
            return std::nullopt;
        }
        int cursor = 0;
        std::optional<double> left;
        std::optional<double> right;
        for (std::size_t i = 0; i < line.chars.size() && i < layout.charLefts.size(); ++i) {
            const int textLen = line.chars[i].text.size();
            const int unitStart = cursor;
            const int unitEnd = cursor + textLen;
            cursor = unitEnd;
            if (textLen <= 0 || unitEnd <= span->first || unitStart >= span->second) {
                continue;
            }
            const int overlapStart = std::max(span->first, unitStart) - unitStart;
            const int overlapEnd = std::min(span->second, unitEnd) - unitStart;
            const double charLeft = layout.charLefts[i];
            const double width = layout.charWidths[i];
            const double segmentLeft = charLeft + std::round(width * overlapStart / textLen);
            const double segmentRight = charLeft + std::round(width * overlapEnd / textLen);
            left = left.has_value() ? std::min(left.value(), segmentLeft) : segmentLeft;
            right = right.has_value() ? std::max(right.value(), segmentRight) : segmentRight;
        }
        if (!left.has_value() || !right.has_value() || right.value() <= left.value()) {
            return std::nullopt;
        }
        return std::pair<double, double>{left.value(), right.value()};
    }

    const auto indices = rubyTimeIndices(ruby, intervals);
    if (indices.empty()) {
        return std::nullopt;
    }
    double left = 0.0;
    double right = 0.0;
    bool seen = false;
    for (int index : indices) {
        if (index < 0 || static_cast<std::size_t>(index) >= layout.charLefts.size()) {
            continue;
        }
        const double charLeft = layout.charLefts[index];
        const double charRight = charLeft + layout.charWidths[index];
        if (!seen) {
            left = charLeft;
            right = charRight;
            seen = true;
        } else {
            left = std::min(left, charLeft);
            right = std::max(right, charRight);
        }
    }
    if (!seen || right <= left) {
        return std::nullopt;
    }
    return std::pair<double, double>{left, right};
}

std::optional<RubyGroupInfo> rubyGroupForCharIndex(
    const RenderConfig &cfg,
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index
) {
    for (const RubyAnnotation &ruby : cfg.rubies) {
        auto indices = rubyTargetIndices(ruby, line, intervals);
        if (indices.size() <= 1) {
            continue;
        }
        if (std::find(indices.begin(), indices.end(), index) == indices.end()) {
            continue;
        }
        std::vector<int> valid;
        for (int candidate : indices) {
            if (candidate >= 0 && candidate < static_cast<int>(intervals.size())) {
                valid.push_back(candidate);
            }
        }
        if (valid.size() <= 1) {
            continue;
        }
        return RubyGroupInfo{valid, effectiveRubyForTarget(ruby, valid, intervals)};
    }
    return std::nullopt;
}

}  // namespace krok::subtitle::native::legacy_qt
