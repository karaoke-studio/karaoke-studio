#include "qt_style_metrics.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::legacy_qt {

using protocol::ResolvedStyle;

double visualStrokeExtent(const ResolvedStyle &style) {
    return std::ceil((std::max(style.strokeWidthPx, 0) + std::max(style.stroke2WidthPx, 0)) / 2.0);
}

double visualStrokeExtentForWidths(int strokeWidth, int stroke2Width) {
    return std::ceil((std::max(strokeWidth, 0) + std::max(stroke2Width, 0)) / 2.0);
}

double strokePenWidth(const ResolvedStyle &style) {
    return std::max(style.strokeWidthPx, 0);
}

double stroke2PenWidth(const ResolvedStyle &style) {
    return std::max(style.strokeWidthPx, 0) + std::max(style.stroke2WidthPx, 0);
}

int glowRadius(const ResolvedStyle &style, bool after) {
    int value = after ? style.glowAfterRadiusPx : style.glowBeforeRadiusPx;
    if (value == 10 && style.glowRadiusPx != 10) {
        value = style.glowRadiusPx;
    }
    return std::max(value, 0);
}

double glowPenWidth(const ResolvedStyle &style, bool after) {
    const int radius = glowRadius(style, after);
    if (radius <= 0) {
        return 0.0;
    }
    const double baseWidth = style.stroke2WidthPx > 0 ? stroke2PenWidth(style) : strokePenWidth(style);
    return std::max(1.0, baseWidth + radius);
}

double glowExtent(const ResolvedStyle &style, bool after) {
    const int radius = glowRadius(style, after);
    if (radius <= 0) {
        return 0.0;
    }
    return std::ceil(glowPenWidth(style, after) / 2.0 + radius * 3.0);
}

int glowPenWidthForWidths(int strokeWidth, int stroke2Width, int glowRadiusValue) {
    if (glowRadiusValue <= 0) {
        return 0;
    }
    const int baseWidth = stroke2Width > 0
        ? std::max(strokeWidth, 0) + std::max(stroke2Width, 0)
        : std::max(strokeWidth, 0);
    return std::max(1, baseWidth + glowRadiusValue);
}

double glowExtentForWidths(int strokeWidth, int stroke2Width, int glowRadiusValue) {
    if (glowRadiusValue <= 0) {
        return 0.0;
    }
    return std::ceil(
        glowPenWidthForWidths(strokeWidth, stroke2Width, glowRadiusValue) / 2.0
        + glowRadiusValue * 3.0
    );
}

double afterClipVerticalExtent(const ResolvedStyle &style) {
    const double strokeExtent = visualStrokeExtent(style);
    const double glowExtra = decorationKindForState(style, true) == QStringLiteral("glow")
        ? glowExtent(style, true)
        : 0.0;
    const double shadowExtra = decorationKindForState(style, true) == QStringLiteral("shadow")
        ? std::abs(shadowOffsetYForState(style, true))
        : 0.0;
    return std::max({strokeExtent, glowExtra, shadowExtra, 2.0}) + 4.0;
}

int scaledPx(int value, double scale) {
    if (value <= 0) {
        return 0;
    }
    return std::max(1, static_cast<int>(std::round(value * scale)));
}

int scaledSignedPx(int value, double scale) {
    if (value == 0) {
        return 0;
    }
    const int sign = value > 0 ? 1 : -1;
    return sign * std::max(1, static_cast<int>(std::round(std::abs(value) * scale)));
}

double rubyScale(const ResolvedStyle &style) {
    return static_cast<double>(std::max(style.rubyFontSizePx, 1)) / static_cast<double>(std::max(style.fontSizePx, 1));
}

double rubyVisualPadding(const ResolvedStyle &style) {
    const double scale = rubyScale(style);
    const int strokeWidth = scaledPx(style.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(style.stroke2WidthPx, scale);
    const double strokeExtent = std::ceil((std::max(strokeWidth, 0) + std::max(stroke2Width, 0)) / 2.0);
    double glowExtra = 0.0;
    if (decorationKindForState(style, true) == QStringLiteral("glow")) {
        const int rubyGlowRadius = scaledPx(glowRadius(style, true), scale);
        const int baseWidth = stroke2Width > 0 ? strokeWidth + stroke2Width : strokeWidth;
        if (rubyGlowRadius > 0) {
            glowExtra = std::ceil((std::max(1, baseWidth + rubyGlowRadius)) / 2.0 + rubyGlowRadius * 3.0);
        }
    }
    const bool shadow = decorationKindForState(style, true) == QStringLiteral("shadow");
    const double shadowX = shadow ? std::abs(scaledSignedPx(shadowOffsetXForState(style, true), scale)) : 0.0;
    const double shadowY = shadow ? std::abs(scaledSignedPx(shadowOffsetYForState(style, true), scale)) : 0.0;
    return std::max({strokeExtent, glowExtra, shadowX, shadowY, 2.0});
}

}  // namespace krok::subtitle::native::legacy_qt
