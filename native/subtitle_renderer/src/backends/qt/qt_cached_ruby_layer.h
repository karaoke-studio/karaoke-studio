#pragma once

#include "qt_render_types.h"

#include <QtCore/QString>
#include <QtGui/QFont>
#include <QtGui/QFontMetricsF>

namespace krok::subtitle::native::legacy_qt {

RubyLayerImage cachedRubyTextLayer(
    const RubyDiagnostics &ruby,
    const QFont &rubyFont,
    const QFontMetricsF &rubyMetrics,
    const QString &phase,
    const protocol::PaintFillSpec &fill,
    const protocol::PaintFillSpec &stroke,
    const protocol::PaintFillSpec &stroke2,
    const protocol::PaintFillSpec &shadow,
    const protocol::ResolvedStyle &style,
    int strokeWidth, int stroke2Width,
    int shadowOffsetX, int shadowOffsetY, int glowRadiusValue
);

}  // namespace krok::subtitle::native::legacy_qt
