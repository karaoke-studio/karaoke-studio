#include "qt_clip_geometry.h"

#include "qt_character_animation.h"
#include "qt_ruby_target.h"
#include "qt_ruby_timing.h"
#include "qt_style_metrics.h"

#include <algorithm>
#include <cmath>
#include <optional>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::ResolvedStyle;
using protocol::RubyAnnotation;
using protocol::TimingLine;

namespace {

std::vector<std::pair<double, double>> afterClipBandsFromCharacterTiming(
    const RenderConfig &cfg, const TimingLine &line, const LineLayout &layout, int tMs
) {
    std::vector<std::pair<double, double>> bands;
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const int start = line.chars[i].startMs;
        // Characters are ordered by start time, so nothing later has begun either.
        if (tMs < start) {
            break;
        }
        const double left = layout.charLefts[i];
        const double width = layout.charWidths[i];
        const double right = left + width;
        const double ratio = progressRatio(start, charEndMs(line, i), tMs);
        if (ratio >= 1.0) {
            bands.emplace_back(left, right);
            continue;
        }
        // Keep scanning: a later character may be running at the same time.
        if (cfg.rightToLeft) {
            bands.emplace_back(right - width * ratio, right);
        } else {
            bands.emplace_back(left, left + width * ratio);
        }
    }
    return bands;
}

std::vector<std::pair<double, double>> mergeBands(
    std::vector<std::pair<double, double>> bands
) {
    std::sort(bands.begin(), bands.end());
    std::vector<std::pair<double, double>> merged;
    for (const auto &band : bands) {
        if (band.second <= band.first) {
            continue;
        }
        if (!merged.empty() && band.first <= merged.back().second) {
            merged.back().second = std::max(merged.back().second, band.second);
            continue;
        }
        merged.push_back(band);
    }
    return merged;
}

QRegion bandsToRegion(
    const std::vector<std::pair<double, double>> &bands, double top, double height
) {
    QRegion region;
    for (const auto &band : bands) {
        region += QRectF(band.first, top, band.second - band.first, height).toAlignedRect();
    }
    return region;
}

std::optional<QRectF> afterClipRectFromCharacterTiming(const RenderConfig &cfg, const ResolvedStyle &style, const TimingLine &line, const LineLayout &layout, int tMs) {
    if (line.chars.empty()) {
        return std::nullopt;
    }

    const auto bands = afterClipBandsFromCharacterTiming(cfg, line, layout, tMs);
    if (bands.empty()) {
        return std::nullopt;
    }
    // The line edge stays the outer boundary, exactly as before.
    double clipEdge = cfg.rightToLeft ? bands.front().first : bands.back().second;
    for (const auto &band : bands) {
        clipEdge = cfg.rightToLeft
            ? std::min(clipEdge, band.first)
            : std::max(clipEdge, band.second);
    }

    const double verticalExtent = layout.afterClipExtent > 0.0 ? layout.afterClipExtent : afterClipVerticalExtent(style);
    const double top = layout.baselineY - layout.ascent - verticalExtent;
    const double height = layout.height + verticalExtent * 2.0;
    if (cfg.rightToLeft) {
        const double left = std::clamp(clipEdge, layout.x, layout.x + layout.width);
        return QRectF(left, top, layout.x + layout.width - left, height);
    }
    const double right = std::clamp(clipEdge, layout.x, layout.x + layout.width);
    return QRectF(layout.x, top, right - layout.x, height);
}

struct NativeFillSegment {
    double left = 0.0;
    double right = 0.0;
    double ratio = 0.0;
};

std::optional<RubyAnnotation> rubyForCharIndex(
    const RenderConfig &cfg,
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index
) {
    for (const RubyAnnotation &ruby : cfg.rubies) {
        const auto indices = rubyTargetIndices(ruby, line, intervals);
        if (std::find(indices.begin(), indices.end(), index) != indices.end()) {
            return ruby;
        }
    }
    return std::nullopt;
}

std::vector<NativeFillSegment> fillSegmentsForLine(
    const RenderConfig &cfg,
    const TimingLine &line,
    const LineLayout &layout,
    int tMs
) {
    std::vector<NativeFillSegment> segments;
    const auto intervals = lineIntervals(line);
    int index = 0;
    while (index < static_cast<int>(line.chars.size())) {
        const auto ruby = rubyForCharIndex(cfg, line, intervals, index);
        if (!ruby.has_value()) {
            if (static_cast<std::size_t>(index) >= layout.charLefts.size()) {
                break;
            }
            const double left = layout.charLefts[index];
            const double right = left + layout.charWidths[index];
            const double ratio = index < static_cast<int>(intervals.size())
                ? progressRatio(intervals[index].first, intervals[index].second, tMs)
                : 0.0;
            segments.push_back({left, right, ratio});
            ++index;
            continue;
        }

        auto indices = rubyTargetIndices(ruby.value(), line, intervals);
        std::vector<int> validIndices;
        for (int candidate : indices) {
            if (
                candidate >= 0
                && static_cast<std::size_t>(candidate) < layout.charLefts.size()
                && static_cast<std::size_t>(candidate) < intervals.size()
            ) {
                validIndices.push_back(candidate);
            }
        }
        if (validIndices.empty()) {
            const double left = layout.charLefts[index];
            const double right = left + layout.charWidths[index];
            const double ratio = index < static_cast<int>(intervals.size())
                ? progressRatio(intervals[index].first, intervals[index].second, tMs)
                : 0.0;
            segments.push_back({left, right, ratio});
            ++index;
            continue;
        }

        double left = layout.charLefts[validIndices.front()];
        double right = layout.charLefts[validIndices.front()] + layout.charWidths[validIndices.front()];
        for (int candidate : validIndices) {
            left = std::min(left, layout.charLefts[candidate]);
            right = std::max(right, layout.charLefts[candidate] + layout.charWidths[candidate]);
        }
        const RubyAnnotation effectiveRuby = effectiveRubyForTarget(ruby.value(), validIndices, intervals);
        segments.push_back({left, right, rubyProgressRatio(effectiveRuby, tMs)});
        index = *std::max_element(validIndices.begin(), validIndices.end()) + 1;
    }
    return segments;
}

// Per-segment bands, same reasoning as afterClipBandsFromCharacterTiming:
// stopping at the first unfinished segment can only show one wipe front.
std::vector<std::pair<double, double>> fillClipBands(
    const std::vector<NativeFillSegment> &segments,
    bool rtl
) {
    std::vector<std::pair<double, double>> bands;
    for (const auto &segment : segments) {
        if (segment.ratio <= 0.0) {
            break;  // segments are time-ordered, so nothing later has begun
        }
        const double width = segment.right - segment.left;
        if (segment.ratio >= 1.0) {
            bands.emplace_back(segment.left, segment.right);
            continue;
        }
        if (rtl) {
            bands.emplace_back(segment.right - std::round(width * segment.ratio), segment.right);
        } else {
            bands.emplace_back(segment.left, segment.left + std::round(width * segment.ratio));
        }
    }
    return bands;
}

std::optional<std::pair<double, double>> fillClipBand(
    const std::vector<NativeFillSegment> &segments,
    bool rtl
) {
    if (segments.empty()) {
        return std::nullopt;
    }
    if (rtl) {
        double left = segments.front().right;
        double right = segments.front().right;
        for (const auto &segment : segments) {
            right = std::max(right, segment.right);
            if (segment.ratio <= 0.0) {
                break;
            }
            if (segment.ratio >= 1.0) {
                left = segment.left;
                continue;
            }
            left = segment.right - std::round((segment.right - segment.left) * segment.ratio);
            break;
        }
        if (right <= left) {
            return std::nullopt;
        }
        return std::pair<double, double>{left, right};
    }

    const double left = segments.front().left;
    double right = left;
    for (const auto &segment : segments) {
        if (segment.ratio <= 0.0) {
            break;
        }
        if (segment.ratio >= 1.0) {
            right = segment.right;
            continue;
        }
        right = segment.left + std::round((segment.right - segment.left) * segment.ratio);
        break;
    }
    if (right <= left) {
        return std::nullopt;
    }
    return std::pair<double, double>{left, right};
}

}  // namespace

// The clip actually applied to the after-colour layer.
//
// Sequential timing yields one contiguous band and this is the same rectangle
// afterClipRect returns; concurrent wipes yield two, and only a region can hold
// both.  afterClipRect stays as the bounding rect for diagnostics.
QRegion afterClipRegion(const RenderConfig &cfg, const ResolvedStyle &style, const TimingLine &line, const LineLayout &layout, int tMs) {
    const double verticalExtent = layout.afterClipExtent > 0.0
        ? layout.afterClipExtent
        : afterClipVerticalExtent(style);
    const double top = layout.baselineY - layout.ascent - verticalExtent;
    const double height = layout.height + verticalExtent * 2.0;
    std::vector<std::pair<double, double>> bands = cfg.rubies.empty()
        ? afterClipBandsFromCharacterTiming(cfg, line, layout, tMs)
        : fillClipBands(fillSegmentsForLine(cfg, line, layout, tMs), cfg.rightToLeft);
    const double lineLeft = layout.x;
    const double lineRight = layout.x + layout.width;
    for (auto &band : bands) {
        band.first = std::clamp(band.first, lineLeft, lineRight);
        band.second = std::clamp(band.second, lineLeft, lineRight);
    }
    return bandsToRegion(mergeBands(std::move(bands)), top, height);
}

std::optional<QRectF> afterClipRect(const RenderConfig &cfg, const ResolvedStyle &style, const TimingLine &line, const LineLayout &layout, int tMs) {
    if (cfg.rubies.empty()) {
        return afterClipRectFromCharacterTiming(cfg, style, line, layout, tMs);
    }
    const auto band = fillClipBand(fillSegmentsForLine(cfg, line, layout, tMs), cfg.rightToLeft);
    if (!band.has_value()) {
        return std::nullopt;
    }
    const double verticalExtent = layout.afterClipExtent > 0.0 ? layout.afterClipExtent : afterClipVerticalExtent(style);
    const double top = layout.baselineY - layout.ascent - verticalExtent;
    const double height = layout.height + verticalExtent * 2.0;
    return QRectF(band->first, top, band->second - band->first, height);
}

}  // namespace krok::subtitle::native::legacy_qt
