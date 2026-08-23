#pragma once

#include "../../protocol/render_config.h"

namespace krok::subtitle::native::legacy_qt {

double visualStrokeExtent(const protocol::ResolvedStyle &style);
double visualStrokeExtentForWidths(int strokeWidth, int stroke2Width);
int glowRadius(const protocol::ResolvedStyle &style, bool after);
double glowExtentForWidths(
    int strokeWidth,
    int stroke2Width,
    int glowRadiusValue
);
double afterClipVerticalExtent(const protocol::ResolvedStyle &style);
int scaledPx(int value, double scale);
int scaledSignedPx(int value, double scale);
double rubyScale(const protocol::ResolvedStyle &style);
double rubyVisualPadding(const protocol::ResolvedStyle &style);

}  // namespace krok::subtitle::native::legacy_qt
