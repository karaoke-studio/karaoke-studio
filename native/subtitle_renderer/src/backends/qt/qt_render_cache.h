#pragma once

#include "qt_render_types.h"

#include <QtCore/QString>
#include <QtGui/QFont>
#include <QtGui/QImage>

#include <optional>

namespace krok::subtitle::native::legacy_qt {

bool glowBitmapCacheEnabled();
bool textLayerCacheEnabled();
GlowBitmapCacheStats &glowBitmapCacheStats();
TextLayerCacheStats &textLayerCacheStats();
LayoutCacheStats &layoutCacheStats();
int glowBitmapCacheSize();
int textLayerCacheSize();
int layoutCacheSize();

void clearGlowBitmapCache();
void clearTextLayerCache();
void clearLayoutCache();

QString doubleCacheKey(double value);
QString fontCacheKey(const QFont &font);
QString textStackStyleCacheKey(
    const protocol::PaintFillSpec &fill,
    const protocol::PaintFillSpec &stroke,
    const protocol::PaintFillSpec &stroke2,
    const protocol::PaintFillSpec &shadow,
    const protocol::ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
);

std::optional<TextLayerImage> lookupTextLayerCache(const QString &key);
void storeTextLayerCache(const QString &key, const TextLayerImage &layer);
std::optional<LineLayout> lookupLayoutCache(const QString &key);
void storeLayoutCache(const QString &key, const LineLayout &layout);
QImage cachedBlurImage(
    const QImage &source,
    int radius,
    const QString &scope = QStringLiteral("unknown")
);

}  // namespace krok::subtitle::native::legacy_qt
