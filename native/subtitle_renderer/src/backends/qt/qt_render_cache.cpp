#include "qt_render_cache.h"

#include "../../runtime/checksum.h"

#include <QtCore/QByteArray>
#include <QtCore/QtGlobal>
#include <QtGui/QPainter>
#include <QtGui/QPixmap>
#include <QtWidgets/QGraphicsBlurEffect>
#include <QtWidgets/QGraphicsPixmapItem>
#include <QtWidgets/QGraphicsScene>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <mutex>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;
using protocol::ResolvedStyle;
using runtime::imageFullChecksum;

QImage blurImage(const QImage &source, int radius) {
    const int blurRadius = std::max(radius, 1);
    QImage result(source.size(), QImage::Format_ARGB32_Premultiplied);
    result.fill(Qt::transparent);

    auto *effect = new QGraphicsBlurEffect();
    effect->setBlurRadius(static_cast<qreal>(blurRadius));
    effect->setBlurHints(QGraphicsBlurEffect::QualityHint);

    QGraphicsPixmapItem item(QPixmap::fromImage(source));
    item.setGraphicsEffect(effect);

    QGraphicsScene scene;
    scene.setSceneRect(0.0, 0.0, static_cast<qreal>(source.width()), static_cast<qreal>(source.height()));
    scene.addItem(&item);

    QPainter painter(&result);
    painter.setRenderHint(QPainter::Antialiasing);
    scene.render(
        &painter,
        QRectF(0.0, 0.0, static_cast<qreal>(source.width()), static_cast<qreal>(source.height())),
        QRectF(0.0, 0.0, static_cast<qreal>(source.width()), static_cast<qreal>(source.height()))
    );
    painter.end();
    scene.removeItem(&item);
    return result;
}

bool environmentDisablesCache(const char *name) {
    const QByteArray value = qgetenv(name).trimmed().toLower();
    return value == QByteArray("0") || value == QByteArray("false") || value == QByteArray("off");
}

bool glowBitmapCacheEnabled() {
    static const bool enabled = !environmentDisablesCache("KROK_SUBTITLE_NATIVE_GLOW_CACHE")
        && !environmentDisablesCache("KROK_SUBTITLE_GLOW_CACHE");
    return enabled;
}

bool textLayerCacheEnabled() {
    static const bool enabled = !environmentDisablesCache("KROK_SUBTITLE_NATIVE_TEXT_LAYER_CACHE")
        && !environmentDisablesCache("KROK_SUBTITLE_TEXT_LAYER_CACHE");
    return enabled;
}

std::vector<GlowBitmapCacheEntry> &glowBitmapCache() {
    static std::vector<GlowBitmapCacheEntry> cache;
    return cache;
}

std::vector<TextLayerCacheEntry> &textLayerCache() {
    static std::vector<TextLayerCacheEntry> cache;
    return cache;
}

std::vector<LayoutCacheEntry> &layoutCache() {
    thread_local std::vector<LayoutCacheEntry> cache;
    return cache;
}

int &layoutCacheLocalGeneration() {
    thread_local int generation = -1;
    return generation;
}

std::mutex &glowBitmapCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

std::mutex &textLayerCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

std::mutex &layoutCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

std::atomic<int> &layoutCacheGeneration() {
    static std::atomic<int> generation{0};
    return generation;
}

GlowBitmapCacheStats &glowBitmapCacheStats() {
    static GlowBitmapCacheStats stats;
    return stats;
}

TextLayerCacheStats &textLayerCacheStats() {
    static TextLayerCacheStats stats;
    return stats;
}

LayoutCacheStats &layoutCacheStats() {
    static LayoutCacheStats stats;
    return stats;
}

int glowBitmapCacheSize() {
    std::lock_guard<std::mutex> lock(glowBitmapCacheMutex());
    return static_cast<int>(glowBitmapCache().size());
}

int textLayerCacheSize() {
    std::lock_guard<std::mutex> lock(textLayerCacheMutex());
    return static_cast<int>(textLayerCache().size());
}

int layoutCacheSize() {
    return static_cast<int>(layoutCache().size());
}

void clearGlowBitmapCache() {
    std::lock_guard<std::mutex> lock(glowBitmapCacheMutex());
    glowBitmapCache().clear();
    glowBitmapCacheStats() = GlowBitmapCacheStats{};
}

void clearTextLayerCache() {
    std::lock_guard<std::mutex> lock(textLayerCacheMutex());
    textLayerCache().clear();
    textLayerCacheStats() = TextLayerCacheStats{};
}

void clearLayoutCache() {
    std::lock_guard<std::mutex> lock(layoutCacheMutex());
    layoutCacheGeneration().fetch_add(1);
    layoutCache().clear();
    layoutCacheLocalGeneration() = layoutCacheGeneration().load();
    layoutCacheStats() = LayoutCacheStats{};
}

QString doubleCacheKey(double value) {
    return QString::number(static_cast<qint64>(std::llround(value * 1000.0)));
}

QString fontCacheKey(const QFont &font) {
    return QStringLiteral("%1|%2|%3|%4|%5")
        .arg(font.family())
        .arg(font.pixelSize())
        .arg(font.weight())
        .arg(font.italic() ? 1 : 0)
        .arg(font.letterSpacing());
}

QString fillCacheKey(const PaintFillSpec &fill) {
    QString key = QStringLiteral("%1|%2|%3|%4|%5|%6|%7|%8|%9")
        .arg(fill.mode)
        .arg(fill.color)
        .arg(fill.startColor)
        .arg(fill.endColor)
        .arg(fill.splitTopColor)
        .arg(fill.splitBottomColor)
        .arg(fill.splitPositionPct)
        .arg(fill.imagePath)
        .arg(fill.imageScalePct);
    for (const auto &stop : fill.gradientStops) {
        key += QStringLiteral("|%1:%2").arg(stop.first).arg(stop.second);
    }
    return key;
}

QString textStackStyleCacheKey(
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    return QStringLiteral("fill=%1|stroke=%2|stroke2=%3|shadow=%4|deco=%5|sw=%6|s2w=%7|sx=%8|sy=%9|glow=%10")
        .arg(fillCacheKey(fill))
        .arg(fillCacheKey(stroke))
        .arg(fillCacheKey(stroke2))
        .arg(fillCacheKey(shadow))
        .arg(style.decorationKind)
        .arg(strokeWidth)
        .arg(stroke2Width)
        .arg(shadowOffsetX)
        .arg(shadowOffsetY)
        .arg(glowRadiusValue);
}

std::optional<TextLayerImage> lookupTextLayerCache(const QString &key) {
    std::lock_guard<std::mutex> lock(textLayerCacheMutex());
    auto &cache = textLayerCache();
    auto &stats = textLayerCacheStats();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            TextLayerCacheEntry entry = *it;
            cache.erase(it);
            cache.push_back(entry);
            ++stats.hits;
            return entry.layer;
        }
    }
    ++stats.misses;
    return std::nullopt;
}

void storeTextLayerCache(const QString &key, const TextLayerImage &layer) {
    std::lock_guard<std::mutex> lock(textLayerCacheMutex());
    auto &cache = textLayerCache();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            cache.erase(it);
            break;
        }
    }
    constexpr std::size_t kTextLayerCacheMax = 256;
    if (cache.size() >= kTextLayerCacheMax) {
        cache.erase(cache.begin());
    }
    cache.push_back(TextLayerCacheEntry{key, layer});
}

std::optional<LineLayout> lookupLayoutCache(const QString &key) {
    const int generation = layoutCacheGeneration().load();
    if (layoutCacheLocalGeneration() != generation) {
        layoutCache().clear();
        layoutCacheLocalGeneration() = generation;
    }
    auto &cache = layoutCache();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            LayoutCacheEntry entry = *it;
            cache.erase(it);
            cache.push_back(entry);
            {
                std::lock_guard<std::mutex> lock(layoutCacheMutex());
                ++layoutCacheStats().hits;
            }
            return entry.layout;
        }
    }
    {
        std::lock_guard<std::mutex> lock(layoutCacheMutex());
        ++layoutCacheStats().misses;
    }
    return std::nullopt;
}

void storeLayoutCache(const QString &key, const LineLayout &layout) {
    const int generation = layoutCacheGeneration().load();
    if (layoutCacheLocalGeneration() != generation) {
        layoutCache().clear();
        layoutCacheLocalGeneration() = generation;
    }
    auto &cache = layoutCache();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            cache.erase(it);
            break;
        }
    }
    constexpr std::size_t kLayoutCacheMax = 512;
    if (cache.size() >= kLayoutCacheMax) {
        cache.erase(cache.begin());
    }
    cache.push_back(LayoutCacheEntry{key, layout});
}

GlowBitmapCacheKeyParts glowBitmapCacheKey(const QImage &source, int radius) {
    GlowBitmapCacheKeyParts parts;
    parts.radius = std::max(radius, 1);
    parts.width = source.width();
    parts.height = source.height();
    parts.format = static_cast<int>(source.format());
    parts.checksum = QString::number(imageFullChecksum(source), 16);
    parts.shapeKey = QStringLiteral("%1:%2:%3:%4")
        .arg(parts.radius)
        .arg(parts.width)
        .arg(parts.height)
        .arg(parts.format);
    parts.key = QStringLiteral("%1:%2").arg(parts.shapeKey, parts.checksum);
    return parts;
}

void recordGlowBitmapCacheMiss(GlowBitmapCacheStats *stats, const GlowBitmapCacheKeyParts &parts, const QString &scope) {
    GlowBitmapCacheMissDiagnostic diagnostic;
    diagnostic.scope = scope.isEmpty() ? QStringLiteral("unknown") : scope;
    diagnostic.radius = parts.radius;
    diagnostic.width = parts.width;
    diagnostic.height = parts.height;
    diagnostic.format = parts.format;
    diagnostic.checksum = parts.checksum.left(16);
    if (stats->seenKeys.contains(parts.key)) {
        ++stats->evictedKeyMisses;
        diagnostic.category = QStringLiteral("evicted_key");
    } else if (stats->seenShapes.contains(parts.shapeKey)) {
        ++stats->contentVariantMisses;
        diagnostic.category = QStringLiteral("content_variant");
    } else {
        ++stats->shapeMisses;
        diagnostic.category = QStringLiteral("new_shape");
    }
    stats->seenKeys.insert(parts.key);
    stats->seenShapes.insert(parts.shapeKey);
    stats->missesByScope[diagnostic.scope] = stats->missesByScope.value(diagnostic.scope, 0) + 1;
    constexpr std::size_t kRecentMissLimit = 64;
    if (stats->recentMisses.size() >= kRecentMissLimit) {
        stats->recentMisses.erase(stats->recentMisses.begin());
    }
    stats->recentMisses.push_back(diagnostic);
}

QImage cachedBlurImage(const QImage &source, int radius, const QString &scope) {
    if (!glowBitmapCacheEnabled()) {
        return blurImage(source, radius);
    }

    const GlowBitmapCacheKeyParts key = glowBitmapCacheKey(source, radius);
    std::lock_guard<std::mutex> lock(glowBitmapCacheMutex());
    auto &cache = glowBitmapCache();
    auto &stats = glowBitmapCacheStats();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key.key) {
            GlowBitmapCacheEntry entry = *it;
            cache.erase(it);
            cache.push_back(entry);
            ++stats.hits;
            return entry.image;
        }
    }

    ++stats.misses;
    recordGlowBitmapCacheMiss(&stats, key, scope);
    QImage blurred = blurImage(source, radius);
    constexpr std::size_t kGlowBitmapCacheMax = 128;
    if (cache.size() >= kGlowBitmapCacheMax) {
        cache.erase(cache.begin());
    }
    cache.push_back(GlowBitmapCacheEntry{key.key, blurred});
    return blurred;
}

}  // namespace krok::subtitle::native::legacy_qt
