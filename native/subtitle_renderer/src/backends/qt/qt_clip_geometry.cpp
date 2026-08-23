#include "qt_clip_geometry.h"

#include "qt_character_animation.h"
#include "qt_style_metrics.h"

#include <algorithm>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::ResolvedStyle;
using protocol::TimingLine;

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

}  // namespace krok::subtitle::native::legacy_qt
