#pragma once

#include "qt_render_types.h"

#include <QtCore/QRectF>
#include <QtCore/QString>
#include <QtGui/QPainter>
#include <QtGui/QPainterPath>
#include <QtGui/QTransform>

namespace krok::subtitle::native::legacy_qt {

void paintTransformedTextStack(
    QPainter &painter, const QPainterPath &path, const QRectF &rect,
    const protocol::ResolvedStyle &style,
    double ratio, bool rtl, int charX, int charWidth, bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("main_transformed")
);
void paintRubyTransformedStack(
    QPainter &painter, const QPainterPath &path, const QRectF &rect,
    const protocol::ResolvedStyle &style,
    double ratio, bool rtl, bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("ruby_transformed")
);

}  // namespace krok::subtitle::native::legacy_qt
