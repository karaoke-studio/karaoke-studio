#include "qt_fill_brush.h"

#include "qt_render_types.h"

#include <QtCore/QFileInfo>
#include <QtGui/QColor>
#include <QtGui/QImage>
#include <QtGui/QLinearGradient>
#include <QtGui/QPixmap>

#include <algorithm>
#include <mutex>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;

QColor colorValue(const QString &value, const QColor &fallback) {
    const QColor color(value);
    return color.isValid() ? color : fallback;
}

QColor validColor(const QString &value, const QString &fallback) {
    const QColor color(value);
    if (color.isValid()) {
        return color;
    }
    const QColor fallbackColor(fallback);
    return fallbackColor.isValid() ? fallbackColor : QColor(QStringLiteral("#FFFFFF"));
}

std::vector<ImageFillCacheEntry> &imageFillCache() {
    static std::vector<ImageFillCacheEntry> cache;
    return cache;
}

std::mutex &imageFillCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

QString imageFillCacheKey(const QString &path) {
    return QFileInfo(path).absoluteFilePath();
}

QImage cachedFillImage(const QString &path) {
    if (path.isEmpty()) {
        return QImage();
    }

    const QString key = imageFillCacheKey(path);
    std::lock_guard<std::mutex> lock(imageFillCacheMutex());
    auto &cache = imageFillCache();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            ImageFillCacheEntry entry = *it;
            cache.erase(it);
            cache.push_back(entry);
            return entry.image;
        }
    }

    QImage image(path);
    if (image.isNull()) {
        return QImage();
    }

    constexpr std::size_t kImageFillCacheMax = 64;
    if (cache.size() >= kImageFillCacheMax) {
        cache.erase(cache.begin());
    }
    cache.push_back(ImageFillCacheEntry{key, image});
    return image;
}

QBrush brushForFill(const PaintFillSpec &fill, const QRectF &rect) {
    if (fill.mode == QStringLiteral("image") && !fill.imagePath.isEmpty()) {
        const QImage image = cachedFillImage(fill.imagePath);
        if (!image.isNull()) {
            QBrush brush(image);
            const double scale = std::max(fill.imageScalePct, 1) / 100.0;
            QTransform transform;
            transform.scale(1.0 / scale, 1.0 / scale);
            transform.translate(rect.left(), rect.top());
            brush.setTransform(transform);
            return brush;
        }
    }
    if (fill.mode == QStringLiteral("gradient_horizontal")
        || fill.mode == QStringLiteral("gradient_vertical")) {
        const bool horizontal = fill.mode == QStringLiteral("gradient_horizontal");
        const QPointF start = horizontal
            ? QPointF(rect.left(), rect.center().y())
            : QPointF(rect.center().x(), rect.top());
        const QPointF end = horizontal
            ? QPointF(rect.right(), rect.center().y())
            : QPointF(rect.center().x(), rect.bottom());
        QLinearGradient gradient(start, end);
        for (const auto &stop : fill.gradientStops) {
            gradient.setColorAt(
                std::clamp(stop.first / 100.0, 0.0, 1.0),
                validColor(stop.second, fill.color)
            );
        }
        return QBrush(gradient);
    }
    if (fill.mode == QStringLiteral("split_vertical")) {
        QLinearGradient gradient(
            QPointF(rect.left(), rect.top()),
            QPointF(rect.left(), rect.bottom())
        );
        const double position = std::clamp(fill.splitPositionPct / 100.0, 0.0, 1.0);
        const QColor top = validColor(fill.splitTopColor, fill.color);
        const QColor bottom = validColor(fill.splitBottomColor, fill.color);
        gradient.setColorAt(0.0, top);
        gradient.setColorAt(std::max(0.0, position - 0.001), top);
        gradient.setColorAt(std::min(1.0, position + 0.001), bottom);
        gradient.setColorAt(1.0, bottom);
        return QBrush(gradient);
    }
    return QBrush(validColor(fill.color, QStringLiteral("#FFFFFF")));
}

}  // namespace krok::subtitle::native::legacy_qt
