#include <QtCore/QByteArray>
#include <QtCore/QCoreApplication>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QElapsedTimer>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QHash>
#include <QtCore/QPointF>
#include <QtCore/QSet>
#include <QtCore/QSharedMemory>
#include <QtCore/QTextStream>
#include <QtGui/QBrush>
#include <QtGui/QColor>
#include <QtGui/QFont>
#include <QtGui/QFontMetricsF>
#include <QtGui/QImage>
#include <QtGui/QLinearGradient>
#include <QtGui/QPainter>
#include <QtGui/QPainterPath>
#include <QtGui/QPen>
#include <QtGui/QPixmap>
#include <QtGui/QTransform>
#include <QtWidgets/QApplication>
#include <QtWidgets/QGraphicsBlurEffect>
#include <QtWidgets/QGraphicsPixmapItem>
#include <QtWidgets/QGraphicsScene>

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cmath>
#include <iostream>
#include <limits>
#include <mutex>
#include <optional>
#include <cstring>
#include <thread>
#include <vector>

namespace {

constexpr int kProtocolSchema = 1;
constexpr double kPi = 3.14159265358979323846;
constexpr int kUtopiaIntroTimeMs = 700;
constexpr int kUtopiaIntroDelayMs = 200;
constexpr int kUtopiaIntroEnlargeMs = 400;
constexpr int kUtopiaIntroCondenseMs = 100;
constexpr double kUtopiaIntroOverRatio = 1.3;
constexpr double kUtopiaWipeOverRatio = 1.15;
constexpr double kUtopiaWipeOverTimeRatio = 0.25;
constexpr int kUtopiaWipeOverTimeLimitMs = 100;
constexpr int kUtopiaFadeOutTimeMs = 750;

struct TimingChar {
    QString text;
    int startMs = 0;
    std::optional<int> pauseReleaseMs;
    QString roleLabel;
};

struct TimingLine {
    std::vector<TimingChar> chars;
    int endMs = 0;
    QString singerLabel;
    int singerId = -1;
};

struct RubyAnnotation {
    QString kanji;
    QString reading;
    std::vector<int> readingPartMs;
    int posStartMs = 0;
    int posEndMs = 0;
};

struct PaintFillSpec {
    QString mode = QStringLiteral("solid");
    QString color = QStringLiteral("#FFFFFF");
    QString startColor = QStringLiteral("#FFFFFF");
    QString endColor = QStringLiteral("#FFFFFF");
    std::vector<std::pair<int, QString>> gradientStops;
    QString splitTopColor = QStringLiteral("#FFFFFF");
    QString splitBottomColor = QStringLiteral("#FFFFFF");
    int splitPositionPct = 50;
    QString imagePath;
    int imageScalePct = 100;
};

struct ResolvedStyle {
    QString fontFamily = QStringLiteral("UD Digi Kyokasho N-B");
    int fontSizePx = 100;
    int fontWeight = 400;
    int letterSpacingPx = 0;
    QString baseColor = QStringLiteral("#FFFFFF");
    QString fillColor = QStringLiteral("#FF5A6F");
    QString beforeStrokeColor = QStringLiteral("#222222");
    QString afterStrokeColor = QStringLiteral("#222222");
    QString beforeStroke2Color = QStringLiteral("#000000");
    QString afterStroke2Color = QStringLiteral("#000000");
    QString beforeShadowColor = QStringLiteral("#000000");
    QString afterShadowColor = QStringLiteral("#000000");
    PaintFillSpec baseFill;
    PaintFillSpec afterFill;
    PaintFillSpec beforeStrokeFill;
    PaintFillSpec afterStrokeFill;
    PaintFillSpec beforeStroke2Fill;
    PaintFillSpec afterStroke2Fill;
    PaintFillSpec beforeShadowFill;
    PaintFillSpec afterShadowFill;
    QString rubyColor = QStringLiteral("#FF5A6F");
    QString rubyBaseColor = QStringLiteral("#FFFFFF");
    QString rubyFillColor = QStringLiteral("#FF5A6F");
    QString rubyBeforeStrokeColor = QStringLiteral("#222222");
    QString rubyAfterStrokeColor = QStringLiteral("#222222");
    QString rubyBeforeStroke2Color = QStringLiteral("#000000");
    QString rubyAfterStroke2Color = QStringLiteral("#000000");
    QString rubyBeforeShadowColor = QStringLiteral("#000000");
    QString rubyAfterShadowColor = QStringLiteral("#000000");
    PaintFillSpec rubyBaseFill;
    PaintFillSpec rubyAfterFill;
    PaintFillSpec rubyBeforeStrokeFill;
    PaintFillSpec rubyAfterStrokeFill;
    PaintFillSpec rubyBeforeStroke2Fill;
    PaintFillSpec rubyAfterStroke2Fill;
    PaintFillSpec rubyBeforeShadowFill;
    PaintFillSpec rubyAfterShadowFill;
    int strokeWidthPx = 9;
    int stroke2WidthPx = 0;
    QString decorationKind = QStringLiteral("shadow");
    int glowRadiusPx = 10;
    int glowBeforeRadiusPx = 10;
    int glowAfterRadiusPx = 10;
    int shadowOffsetX = 0;
    int shadowOffsetY = 1;
    int rubyFontSizePx = 30;
    int rubyGapPx = 8;
    bool hasMainKaraokeColors = false;
    bool hasRubyKaraokeColors = false;
};

struct RenderConfig {
    int width = 1920;
    int height = 1080;
    int fps = 60;
    ResolvedStyle baseStyle;
    int lineYMarginPx = 80;
    int lineGapPx = 90;
    int lineLeadInMs = 1800;
    int lineTailMs = 1000;
    QString lineYPosition = QStringLiteral("bottom");
    QString lineHorizontalLayout = QStringLiteral("asymmetric");
    int upperLineLeftMarginPx = 50;
    int lowerLineRightMarginPx = 50;
    bool dualLineLayout = true;
    bool rightToLeft = false;
    QString entryAnim = QStringLiteral("none");
    int entryLeadMs = 300;
    QString exitAnim = QStringLiteral("none");
    int exitFadeMs = 300;
    QJsonObject singerStyleOverrides;
    QJsonObject customStyleSchemes;
    // Built during configure. render_frame must not insert here because LineLayout stores pointers into this QHash.
    QHash<QString, ResolvedStyle> resolvedStyles;
    std::vector<TimingLine> lines;
    std::vector<RubyAnnotation> rubies;
};

struct LineLayout {
    QString text;
    QFont font;
    QPainterPath path;
    std::vector<double> charLefts;
    std::vector<double> charWidths;
    std::vector<QFont> charFonts;
    const ResolvedStyle *lineStyle = nullptr;
    // Pointers into RenderConfig::resolvedStyles, which is frozen for render_frame.
    std::vector<const ResolvedStyle *> charStyles;
    double x = 0.0;
    double baselineY = 0.0;
    double width = 0.0;
    double height = 0.0;
    double ascent = 0.0;
    double descent = 0.0;
    double afterClipExtent = 0.0;
    bool hasInlineStyles = false;
};

struct LineDiagnostics {
    int lane = 0;
    double lineX = 0.0;
    double lineWidth = 0.0;
    double baselineY = 0.0;
    double afterClipLeft = 0.0;
    double afterClipRight = 0.0;
    double afterClipTop = 0.0;
    double afterClipHeight = 0.0;
};

struct RubyDiagnostics {
    QString kanji;
    QString reading;
    std::vector<int> indices;
    double x = 0.0;
    double baselineY = 0.0;
    double targetWidth = 0.0;
    double readingWidth = 0.0;
    double progress = 0.0;
    double afterClipLeft = 0.0;
    double afterClipRight = 0.0;
    double afterClipTop = 0.0;
    double afterClipHeight = 0.0;
};

struct RubyLayerImage {
    QImage image;
    QPointF offset;
};

struct RubyGroupInfo {
    std::vector<int> indices;
    RubyAnnotation ruby;
};

struct RubyUnitLayout {
    QString text;
    std::pair<int, int> interval;
    double x = 0.0;
    double width = 0.0;
};

struct LineCharTransition {
    QString phase;
    QString effect;
    double progress = 1.0;
    int startMs = 0;
    int endMs = 0;
};

struct AnimationState {
    double opacity = 1.0;
    double dx = 0.0;
    double dy = 0.0;
    double rotation = 0.0;
    double scaleX = 1.0;
    double scaleY = 1.0;
    double skewY = 0.0;
};

struct ImageFillCacheEntry {
    QString key;
    QImage image;
};

struct GlowBitmapCacheEntry {
    QString key;
    QImage image;
};

struct GlowBitmapCacheKeyParts {
    QString key;
    QString shapeKey;
    QString checksum;
    int radius = 1;
    int width = 0;
    int height = 0;
    int format = 0;
};

struct GlowBitmapCacheMissDiagnostic {
    QString scope;
    QString category;
    int radius = 1;
    int width = 0;
    int height = 0;
    int format = 0;
    QString checksum;
};

struct GlowLayerImage {
    QImage image;
    QPointF offset;
};

struct GlowBitmapCacheStats {
    int hits = 0;
    int misses = 0;
    int shapeMisses = 0;
    int contentVariantMisses = 0;
    int evictedKeyMisses = 0;
    QSet<QString> seenKeys;
    QSet<QString> seenShapes;
    QHash<QString, int> missesByScope;
    std::vector<GlowBitmapCacheMissDiagnostic> recentMisses;
};

struct RenderDiagnostics {
    int visibleLines = 0;
    bool hasFirstLine = false;
    double lineX = 0.0;
    double lineWidth = 0.0;
    double baselineY = 0.0;
    double afterClipLeft = 0.0;
    double afterClipRight = 0.0;
    double afterClipTop = 0.0;
    double afterClipHeight = 0.0;
    std::vector<LineDiagnostics> lines;
    std::vector<RubyDiagnostics> rubies;
};

struct RenderResult {
    QImage image;
    RenderDiagnostics diagnostics;
};

struct RangeFrameResult {
    int tMs = 0;
    double renderMs = 0.0;
    QString checksum;
    int visibleLines = 0;
    QImage image;
};

struct SharedFrameRing {
    QString key;
    int slotCount = 0;
    int width = 0;
    int height = 0;
    int stride = 0;
    int pixelBytes = 0;
    int headerBytes = 64;
    int slotBytes = 0;
    int totalBytes = 0;
    QString pixelFormat = QStringLiteral("rgba8888");
};

struct RenderRuntime {
    std::mutex cancelMutex;
    QSet<int> cancelledGenerations;
    std::atomic<bool> shutdownRequested{false};
    std::mutex jobsMutex;
    std::vector<std::thread> jobs;
    std::mutex sharedMemoryMutex;
    std::unique_ptr<QSharedMemory> sharedMemory;
    SharedFrameRing sharedRing;
};

QString stringValue(const QJsonObject &object, const QString &key, const QString &fallback = {}) {
    const auto value = object.value(key);
    return value.isString() ? value.toString() : fallback;
}

int intValue(const QJsonObject &object, const QString &key, int fallback = 0) {
    const auto value = object.value(key);
    return value.isDouble() ? value.toInt() : fallback;
}

bool supportedFillMode(const QString &mode) {
    return mode == QStringLiteral("solid")
        || mode == QStringLiteral("gradient_horizontal")
        || mode == QStringLiteral("gradient_vertical")
        || mode == QStringLiteral("split_vertical")
        || mode == QStringLiteral("image");
}

PaintFillSpec solidPaintFill(const QString &color) {
    PaintFillSpec fill;
    fill.color = color;
    fill.startColor = color;
    fill.endColor = color;
    fill.gradientStops = {{0, color}, {100, color}};
    fill.splitTopColor = color;
    fill.splitBottomColor = color;
    return fill;
}

std::vector<std::pair<int, QString>> parseGradientStops(
    const QJsonValue &value,
    const QString &startColor,
    const QString &endColor
) {
    std::vector<std::pair<int, QString>> stops;
    const QJsonArray items = value.toArray();
    for (const auto &item : items) {
        const QJsonArray pair = item.toArray();
        if (pair.size() < 2 || !pair.at(0).isDouble() || !pair.at(1).isString()) {
            continue;
        }
        stops.push_back({
            std::clamp(pair.at(0).toInt(), 0, 100),
            pair.at(1).toString(),
        });
    }
    if (stops.empty()) {
        stops = {{0, startColor}, {100, endColor}};
    }

    bool hasStart = false;
    bool hasEnd = false;
    for (const auto &stop : stops) {
        hasStart = hasStart || stop.first == 0;
        hasEnd = hasEnd || stop.first == 100;
    }
    if (!hasStart) {
        stops.push_back({0, startColor});
    }
    if (!hasEnd) {
        stops.push_back({100, endColor});
    }
    std::stable_sort(stops.begin(), stops.end(), [](const auto &left, const auto &right) {
        return left.first < right.first;
    });
    return stops;
}

PaintFillSpec paintFillSpec(const QJsonObject &object, const QString &fallback) {
    PaintFillSpec fill = solidPaintFill(fallback);
    if (object.isEmpty()) {
        return fill;
    }
    const QString mode = stringValue(object, QStringLiteral("mode"), fill.mode);
    fill.mode = supportedFillMode(mode) ? mode : QStringLiteral("solid");
    fill.color = stringValue(object, QStringLiteral("color"), fallback);
    fill.startColor = stringValue(object, QStringLiteral("start_color"), fill.color);
    fill.endColor = stringValue(object, QStringLiteral("end_color"), fill.color);
    fill.gradientStops = parseGradientStops(
        object.value(QStringLiteral("gradient_stops")),
        fill.startColor,
        fill.endColor
    );
    fill.splitTopColor = stringValue(object, QStringLiteral("split_top_color"), fill.startColor);
    fill.splitBottomColor = stringValue(object, QStringLiteral("split_bottom_color"), fill.endColor);
    fill.splitPositionPct = std::clamp(
        intValue(object, QStringLiteral("split_position_pct"), fill.splitPositionPct),
        0,
        100
    );
    fill.imagePath = stringValue(object, QStringLiteral("image_path"), fill.imagePath);
    fill.imageScalePct = std::max(
        1,
        intValue(object, QStringLiteral("image_scale_pct"), fill.imageScalePct)
    );
    return fill;
}

QString paintFillColor(const QJsonObject &object, const QString &fallback) {
    return paintFillSpec(object, fallback).color;
}

PaintFillSpec karaokeLayerFillFromColors(
    const QJsonObject &colors,
    const QString &stateKey,
    const QString &layerKey,
    const QString &fallback
) {
    const QJsonObject state = colors.value(stateKey).toObject();
    return paintFillSpec(state.value(layerKey).toObject(), fallback);
}

QString karaokeLayerColor(
    const QJsonObject &style,
    const QString &stateKey,
    const QString &layerKey,
    const QString &fallback
) {
    const QJsonObject colors = style.value(QStringLiteral("karaoke_colors")).toObject();
    const QJsonObject state = colors.value(stateKey).toObject();
    return paintFillColor(state.value(layerKey).toObject(), fallback);
}

QString karaokeLayerColorFromColors(
    const QJsonObject &colors,
    const QString &stateKey,
    const QString &layerKey,
    const QString &fallback
) {
    const QJsonObject state = colors.value(stateKey).toObject();
    return paintFillColor(state.value(layerKey).toObject(), fallback);
}

bool hasObject(const QJsonObject &object, const QString &key) {
    return object.value(key).isObject();
}

bool hasNonNull(const QJsonObject &object, const QString &key) {
    return object.contains(key) && !object.value(key).isNull() && !object.value(key).isUndefined();
}

void refreshLegacyMainFills(ResolvedStyle &cfg) {
    cfg.baseFill = solidPaintFill(cfg.baseColor);
    cfg.afterFill = solidPaintFill(cfg.fillColor);
    cfg.beforeStrokeFill = solidPaintFill(cfg.beforeStrokeColor);
    cfg.afterStrokeFill = solidPaintFill(cfg.afterStrokeColor);
    cfg.beforeStroke2Fill = solidPaintFill(cfg.beforeStroke2Color);
    cfg.afterStroke2Fill = solidPaintFill(cfg.afterStroke2Color);
    cfg.beforeShadowFill = solidPaintFill(cfg.beforeShadowColor);
    cfg.afterShadowFill = solidPaintFill(cfg.afterShadowColor);
}

void applyMainKaraokeColors(ResolvedStyle &cfg, const QJsonObject &colors) {
    cfg.baseColor = karaokeLayerColorFromColors(colors, QStringLiteral("before"), QStringLiteral("text"), cfg.baseColor);
    cfg.fillColor = karaokeLayerColorFromColors(colors, QStringLiteral("after"), QStringLiteral("text"), cfg.fillColor);
    cfg.beforeStrokeColor = karaokeLayerColorFromColors(colors, QStringLiteral("before"), QStringLiteral("stroke"), cfg.beforeStrokeColor);
    cfg.afterStrokeColor = karaokeLayerColorFromColors(colors, QStringLiteral("after"), QStringLiteral("stroke"), cfg.afterStrokeColor);
    cfg.beforeStroke2Color = karaokeLayerColorFromColors(colors, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.beforeStroke2Color);
    cfg.afterStroke2Color = karaokeLayerColorFromColors(colors, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.afterStroke2Color);
    cfg.beforeShadowColor = karaokeLayerColorFromColors(colors, QStringLiteral("before"), QStringLiteral("shadow"), cfg.beforeShadowColor);
    cfg.afterShadowColor = karaokeLayerColorFromColors(colors, QStringLiteral("after"), QStringLiteral("shadow"), cfg.afterShadowColor);
    cfg.baseFill = karaokeLayerFillFromColors(colors, QStringLiteral("before"), QStringLiteral("text"), cfg.baseColor);
    cfg.afterFill = karaokeLayerFillFromColors(colors, QStringLiteral("after"), QStringLiteral("text"), cfg.fillColor);
    cfg.beforeStrokeFill = karaokeLayerFillFromColors(colors, QStringLiteral("before"), QStringLiteral("stroke"), cfg.beforeStrokeColor);
    cfg.afterStrokeFill = karaokeLayerFillFromColors(colors, QStringLiteral("after"), QStringLiteral("stroke"), cfg.afterStrokeColor);
    cfg.beforeStroke2Fill = karaokeLayerFillFromColors(colors, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.beforeStroke2Color);
    cfg.afterStroke2Fill = karaokeLayerFillFromColors(colors, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.afterStroke2Color);
    cfg.beforeShadowFill = karaokeLayerFillFromColors(colors, QStringLiteral("before"), QStringLiteral("shadow"), cfg.beforeShadowColor);
    cfg.afterShadowFill = karaokeLayerFillFromColors(colors, QStringLiteral("after"), QStringLiteral("shadow"), cfg.afterShadowColor);
}

void copyMainColorsToRuby(ResolvedStyle &cfg) {
    cfg.rubyBaseColor = cfg.baseColor;
    cfg.rubyFillColor = cfg.fillColor;
    cfg.rubyBeforeStrokeColor = cfg.beforeStrokeColor;
    cfg.rubyAfterStrokeColor = cfg.afterStrokeColor;
    cfg.rubyBeforeStroke2Color = cfg.beforeStroke2Color;
    cfg.rubyAfterStroke2Color = cfg.afterStroke2Color;
    cfg.rubyBeforeShadowColor = cfg.beforeShadowColor;
    cfg.rubyAfterShadowColor = cfg.afterShadowColor;
    cfg.rubyBaseFill = cfg.baseFill;
    cfg.rubyAfterFill = cfg.afterFill;
    cfg.rubyBeforeStrokeFill = cfg.beforeStrokeFill;
    cfg.rubyAfterStrokeFill = cfg.afterStrokeFill;
    cfg.rubyBeforeStroke2Fill = cfg.beforeStroke2Fill;
    cfg.rubyAfterStroke2Fill = cfg.afterStroke2Fill;
    cfg.rubyBeforeShadowFill = cfg.beforeShadowFill;
    cfg.rubyAfterShadowFill = cfg.afterShadowFill;
}

void refreshLegacyRubyFills(ResolvedStyle &cfg) {
    cfg.rubyBaseColor = cfg.baseColor;
    cfg.rubyFillColor = cfg.rubyColor;
    cfg.rubyBeforeStrokeColor = cfg.beforeStrokeColor;
    cfg.rubyAfterStrokeColor = cfg.afterStrokeColor;
    cfg.rubyBeforeStroke2Color = QStringLiteral("#000000");
    cfg.rubyAfterStroke2Color = QStringLiteral("#000000");
    cfg.rubyBeforeShadowColor = cfg.beforeShadowColor;
    cfg.rubyAfterShadowColor = cfg.afterShadowColor;
    cfg.rubyBaseFill = solidPaintFill(cfg.rubyBaseColor);
    cfg.rubyAfterFill = solidPaintFill(cfg.rubyFillColor);
    cfg.rubyBeforeStrokeFill = solidPaintFill(cfg.rubyBeforeStrokeColor);
    cfg.rubyAfterStrokeFill = solidPaintFill(cfg.rubyAfterStrokeColor);
    cfg.rubyBeforeStroke2Fill = solidPaintFill(cfg.rubyBeforeStroke2Color);
    cfg.rubyAfterStroke2Fill = solidPaintFill(cfg.rubyAfterStroke2Color);
    cfg.rubyBeforeShadowFill = solidPaintFill(cfg.rubyBeforeShadowColor);
    cfg.rubyAfterShadowFill = solidPaintFill(cfg.rubyAfterShadowColor);
}

void applyRubyKaraokeColors(ResolvedStyle &cfg, const QJsonObject &colors) {
    cfg.rubyBaseColor = karaokeLayerColorFromColors(colors, QStringLiteral("before"), QStringLiteral("text"), cfg.baseColor);
    cfg.rubyFillColor = karaokeLayerColorFromColors(colors, QStringLiteral("after"), QStringLiteral("text"), cfg.rubyColor);
    cfg.rubyBeforeStrokeColor = karaokeLayerColorFromColors(colors, QStringLiteral("before"), QStringLiteral("stroke"), cfg.beforeStrokeColor);
    cfg.rubyAfterStrokeColor = karaokeLayerColorFromColors(colors, QStringLiteral("after"), QStringLiteral("stroke"), cfg.afterStrokeColor);
    cfg.rubyBeforeStroke2Color = karaokeLayerColorFromColors(colors, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.beforeStroke2Color);
    cfg.rubyAfterStroke2Color = karaokeLayerColorFromColors(colors, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.afterStroke2Color);
    cfg.rubyBeforeShadowColor = karaokeLayerColorFromColors(colors, QStringLiteral("before"), QStringLiteral("shadow"), cfg.beforeShadowColor);
    cfg.rubyAfterShadowColor = karaokeLayerColorFromColors(colors, QStringLiteral("after"), QStringLiteral("shadow"), cfg.afterShadowColor);
    cfg.rubyBaseFill = karaokeLayerFillFromColors(colors, QStringLiteral("before"), QStringLiteral("text"), cfg.rubyBaseColor);
    cfg.rubyAfterFill = karaokeLayerFillFromColors(colors, QStringLiteral("after"), QStringLiteral("text"), cfg.rubyFillColor);
    cfg.rubyBeforeStrokeFill = karaokeLayerFillFromColors(colors, QStringLiteral("before"), QStringLiteral("stroke"), cfg.rubyBeforeStrokeColor);
    cfg.rubyAfterStrokeFill = karaokeLayerFillFromColors(colors, QStringLiteral("after"), QStringLiteral("stroke"), cfg.rubyAfterStrokeColor);
    cfg.rubyBeforeStroke2Fill = karaokeLayerFillFromColors(colors, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.rubyBeforeStroke2Color);
    cfg.rubyAfterStroke2Fill = karaokeLayerFillFromColors(colors, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.rubyAfterStroke2Color);
    cfg.rubyBeforeShadowFill = karaokeLayerFillFromColors(colors, QStringLiteral("before"), QStringLiteral("shadow"), cfg.rubyBeforeShadowColor);
    cfg.rubyAfterShadowFill = karaokeLayerFillFromColors(colors, QStringLiteral("after"), QStringLiteral("shadow"), cfg.rubyAfterShadowColor);
}

void applyScalarStyleOverrides(ResolvedStyle &cfg, const QJsonObject &style) {
    if (hasNonNull(style, QStringLiteral("font_family"))) {
        cfg.fontFamily = stringValue(style, QStringLiteral("font_family"), cfg.fontFamily);
    }
    if (hasNonNull(style, QStringLiteral("font_size_px"))) {
        cfg.fontSizePx = std::max(1, intValue(style, QStringLiteral("font_size_px"), cfg.fontSizePx));
    }
    if (hasNonNull(style, QStringLiteral("font_weight"))) {
        cfg.fontWeight = std::clamp(intValue(style, QStringLiteral("font_weight"), cfg.fontWeight), 1, 999);
    }
    if (hasNonNull(style, QStringLiteral("letter_spacing_px"))) {
        cfg.letterSpacingPx = intValue(style, QStringLiteral("letter_spacing_px"), cfg.letterSpacingPx);
    }
    if (hasNonNull(style, QStringLiteral("base_color"))) {
        cfg.baseColor = stringValue(style, QStringLiteral("base_color"), cfg.baseColor);
    }
    if (hasNonNull(style, QStringLiteral("fill_color"))) {
        cfg.fillColor = stringValue(style, QStringLiteral("fill_color"), cfg.fillColor);
    }
    if (hasNonNull(style, QStringLiteral("ruby_color"))) {
        cfg.rubyColor = stringValue(style, QStringLiteral("ruby_color"), cfg.rubyColor);
    }
    if (hasNonNull(style, QStringLiteral("stroke_color"))) {
        const QString strokeColor = stringValue(style, QStringLiteral("stroke_color"), cfg.beforeStrokeColor);
        cfg.beforeStrokeColor = strokeColor;
        cfg.afterStrokeColor = strokeColor;
        cfg.rubyBeforeStrokeColor = strokeColor;
        cfg.rubyAfterStrokeColor = strokeColor;
    }
    if (hasNonNull(style, QStringLiteral("shadow_color"))) {
        const QString shadowColor = stringValue(style, QStringLiteral("shadow_color"), cfg.beforeShadowColor);
        cfg.beforeShadowColor = shadowColor;
        cfg.afterShadowColor = shadowColor;
        cfg.rubyBeforeShadowColor = shadowColor;
        cfg.rubyAfterShadowColor = shadowColor;
    }
    if (hasNonNull(style, QStringLiteral("stroke_width_px"))) {
        cfg.strokeWidthPx = std::max(0, intValue(style, QStringLiteral("stroke_width_px"), cfg.strokeWidthPx));
    }
    if (hasNonNull(style, QStringLiteral("stroke2_width_px"))) {
        cfg.stroke2WidthPx = std::max(0, intValue(style, QStringLiteral("stroke2_width_px"), cfg.stroke2WidthPx));
    }
    if (hasNonNull(style, QStringLiteral("decoration_kind"))) {
        cfg.decorationKind = stringValue(style, QStringLiteral("decoration_kind"), cfg.decorationKind);
    }
    if (hasNonNull(style, QStringLiteral("glow_radius_px"))) {
        cfg.glowRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_radius_px"), cfg.glowRadiusPx));
        if (!hasNonNull(style, QStringLiteral("glow_before_radius_px"))) {
            cfg.glowBeforeRadiusPx = cfg.glowRadiusPx;
        }
        if (!hasNonNull(style, QStringLiteral("glow_after_radius_px"))) {
            cfg.glowAfterRadiusPx = cfg.glowRadiusPx;
        }
    }
    if (hasNonNull(style, QStringLiteral("glow_before_radius_px"))) {
        cfg.glowBeforeRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_before_radius_px"), cfg.glowBeforeRadiusPx));
    }
    if (hasNonNull(style, QStringLiteral("glow_after_radius_px"))) {
        cfg.glowAfterRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_after_radius_px"), cfg.glowAfterRadiusPx));
    }
    if (hasNonNull(style, QStringLiteral("shadow_offset_x"))) {
        cfg.shadowOffsetX = intValue(style, QStringLiteral("shadow_offset_x"), cfg.shadowOffsetX);
    }
    if (hasNonNull(style, QStringLiteral("shadow_offset_y"))) {
        cfg.shadowOffsetY = intValue(style, QStringLiteral("shadow_offset_y"), cfg.shadowOffsetY);
    }
    if (hasNonNull(style, QStringLiteral("ruby_font_size_px"))) {
        cfg.rubyFontSizePx = std::max(1, intValue(style, QStringLiteral("ruby_font_size_px"), cfg.rubyFontSizePx));
    }
    if (hasNonNull(style, QStringLiteral("ruby_gap_px"))) {
        cfg.rubyGapPx = std::max(0, intValue(style, QStringLiteral("ruby_gap_px"), cfg.rubyGapPx));
    }
}

ResolvedStyle styleWithOverrides(const ResolvedStyle &base, const QJsonObject &scheme) {
    ResolvedStyle cfg = base;
    applyScalarStyleOverrides(cfg, scheme);

    if (hasObject(scheme, QStringLiteral("karaoke_colors"))) {
        cfg.hasMainKaraokeColors = true;
        applyMainKaraokeColors(cfg, scheme.value(QStringLiteral("karaoke_colors")).toObject());
    } else if (!cfg.hasMainKaraokeColors) {
        refreshLegacyMainFills(cfg);
    }

    if (hasObject(scheme, QStringLiteral("ruby_karaoke_colors"))) {
        cfg.hasRubyKaraokeColors = true;
        applyRubyKaraokeColors(cfg, scheme.value(QStringLiteral("ruby_karaoke_colors")).toObject());
    } else if (!cfg.hasRubyKaraokeColors) {
        if (cfg.hasMainKaraokeColors) {
            copyMainColorsToRuby(cfg);
        } else {
            refreshLegacyRubyFills(cfg);
        }
    }
    return cfg;
}

QJsonObject response(bool ok, const QString &event) {
    QJsonObject out;
    out.insert(QStringLiteral("ok"), ok);
    out.insert(QStringLiteral("event"), event);
    return out;
}

void writeJson(const QJsonObject &object) {
    static std::mutex mutex;
    std::lock_guard<std::mutex> lock(mutex);
    const QJsonDocument doc(object);
    std::cout << doc.toJson(QJsonDocument::Compact).constData() << std::endl;
}

bool generationCancelled(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return false;
    }
    if (runtime->shutdownRequested.load()) {
        return true;
    }
    std::lock_guard<std::mutex> lock(runtime->cancelMutex);
    return runtime->cancelledGenerations.contains(generation);
}

void cancelGeneration(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return;
    }
    std::lock_guard<std::mutex> lock(runtime->cancelMutex);
    runtime->cancelledGenerations.insert(generation);
}

void clearGenerationCancel(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return;
    }
    std::lock_guard<std::mutex> lock(runtime->cancelMutex);
    runtime->cancelledGenerations.remove(generation);
}

void rememberRenderJob(RenderRuntime *runtime, std::thread job) {
    if (runtime == nullptr) {
        if (job.joinable()) {
            job.detach();
        }
        return;
    }
    std::lock_guard<std::mutex> lock(runtime->jobsMutex);
    runtime->jobs.push_back(std::move(job));
}

void joinRenderJobs(RenderRuntime *runtime) {
    if (runtime == nullptr) {
        return;
    }
    std::vector<std::thread> jobs;
    {
        std::lock_guard<std::mutex> lock(runtime->jobsMutex);
        jobs.swap(runtime->jobs);
    }
    for (auto &job : jobs) {
        if (job.joinable()) {
            job.join();
        }
    }
}

QString defaultSharedMemoryKey(int generation) {
    return QStringLiteral("krok_subtitle_renderer_%1_%2")
        .arg(QCoreApplication::applicationPid())
        .arg(generation);
}

bool ensureSharedFrameRing(
    RenderRuntime *runtime,
    const QString &key,
    int ringSlotCount,
    int width,
    int height,
    QString *error
) {
    if (runtime == nullptr) {
        if (error != nullptr) {
            *error = QStringLiteral("render runtime is unavailable");
        }
        return false;
    }
    const int safeSlots = std::max(1, ringSlotCount);
    QImage probe(std::max(1, width), std::max(1, height), QImage::Format_RGBA8888);
    const int stride = probe.bytesPerLine();
    const int pixelBytes = stride * probe.height();
    constexpr int headerBytes = 64;
    const int slotBytes = headerBytes + pixelBytes;
    const int totalBytes = slotBytes * safeSlots;

    std::lock_guard<std::mutex> lock(runtime->sharedMemoryMutex);
    if (runtime->sharedMemory != nullptr && runtime->sharedMemory->isAttached()) {
        runtime->sharedMemory->detach();
    }
    runtime->sharedMemory = std::make_unique<QSharedMemory>(key);
    if (!runtime->sharedMemory->create(totalBytes)) {
        if (error != nullptr) {
            *error = runtime->sharedMemory->errorString();
        }
        runtime->sharedMemory.reset();
        return false;
    }
    runtime->sharedRing = SharedFrameRing{
        key,
        safeSlots,
        probe.width(),
        probe.height(),
        stride,
        pixelBytes,
        headerBytes,
        slotBytes,
        totalBytes,
        QStringLiteral("rgba8888"),
    };
    if (runtime->sharedMemory->lock()) {
        std::memset(runtime->sharedMemory->data(), 0, static_cast<std::size_t>(totalBytes));
        runtime->sharedMemory->unlock();
    }
    return true;
}

void writeSlotInt(char *base, int offset, int value) {
    std::int32_t stored = static_cast<std::int32_t>(value);
    std::memcpy(base + offset, &stored, sizeof(stored));
}

bool writeSharedFrameSlot(
    RenderRuntime *runtime,
    const RangeFrameResult &result,
    int generation,
    int frameIndex,
    int slotIndex,
    SharedFrameRing *ringOut
) {
    if (runtime == nullptr) {
        return false;
    }
    std::lock_guard<std::mutex> lock(runtime->sharedMemoryMutex);
    if (runtime->sharedMemory == nullptr || !runtime->sharedMemory->isAttached() || runtime->sharedRing.slotCount <= 0) {
        return false;
    }
    SharedFrameRing ring = runtime->sharedRing;
    const int safeSlot = ((slotIndex % ring.slotCount) + ring.slotCount) % ring.slotCount;
    QImage image = result.image.convertToFormat(QImage::Format_RGBA8888);
    if (image.width() != ring.width || image.height() != ring.height || image.bytesPerLine() != ring.stride) {
        image = image.scaled(ring.width, ring.height).convertToFormat(QImage::Format_RGBA8888);
    }
    if (!runtime->sharedMemory->lock()) {
        return false;
    }
    char *base = static_cast<char *>(runtime->sharedMemory->data());
    const int slotOffset = safeSlot * ring.slotBytes;
    char *slot = base + slotOffset;
    writeSlotInt(slot, 0, 1);  // writing
    writeSlotInt(slot, 4, generation);
    writeSlotInt(slot, 8, frameIndex);
    writeSlotInt(slot, 12, result.tMs);
    writeSlotInt(slot, 16, ring.width);
    writeSlotInt(slot, 20, ring.height);
    writeSlotInt(slot, 24, ring.stride);
    writeSlotInt(slot, 28, 1);  // rgba8888
    writeSlotInt(slot, 32, ring.headerBytes);
    writeSlotInt(slot, 36, ring.pixelBytes);
    const uchar *bits = image.constBits();
    std::memcpy(slot + ring.headerBytes, bits, static_cast<std::size_t>(ring.pixelBytes));
    writeSlotInt(slot, 0, 2);  // ready
    runtime->sharedMemory->unlock();
    if (ringOut != nullptr) {
        *ringOut = ring;
    }
    return true;
}

std::uint64_t imageChecksum(const QImage &image) {
    const uchar *data = image.constBits();
    const qsizetype size = image.sizeInBytes();
    std::uint64_t hash = 1469598103934665603ull;
    const qsizetype step = std::max<qsizetype>(1, size / 4096);
    for (qsizetype i = 0; i < size; i += step) {
        hash ^= static_cast<std::uint64_t>(data[i]);
        hash *= 1099511628211ull;
    }
    return hash;
}

std::uint64_t imageFullChecksum(const QImage &image) {
    const uchar *data = image.constBits();
    const qsizetype size = image.sizeInBytes();
    std::uint64_t hash = 1469598103934665603ull;
    for (qsizetype i = 0; i < size; ++i) {
        hash ^= static_cast<std::uint64_t>(data[i]);
        hash *= 1099511628211ull;
    }
    return hash;
}

std::vector<int> parseIntArray(const QJsonArray &items) {
    std::vector<int> out;
    out.reserve(static_cast<std::size_t>(items.size()));
    for (const auto &item : items) {
        if (item.isDouble()) {
            out.push_back(item.toInt());
        }
    }
    return out;
}

void buildResolvedStyleCache(RenderConfig &cfg);

std::optional<RenderConfig> parseConfig(const QJsonObject &ir, QString *error) {
    if (ir.value(QStringLiteral("schema")).toInt() != kProtocolSchema) {
        *error = QStringLiteral("unsupported Render IR schema");
        return std::nullopt;
    }

    RenderConfig cfg;
    const QJsonObject screen = ir.value(QStringLiteral("screen")).toObject();
    cfg.width = std::max(1, intValue(screen, QStringLiteral("width"), cfg.width));
    cfg.height = std::max(1, intValue(screen, QStringLiteral("height"), cfg.height));
    cfg.fps = std::max(1, intValue(screen, QStringLiteral("fps"), cfg.fps));

    const QJsonObject style = ir.value(QStringLiteral("style")).toObject();
    ResolvedStyle &base = cfg.baseStyle;
    base.fontFamily = stringValue(style, QStringLiteral("font_family"), base.fontFamily);
    base.fontSizePx = std::max(1, intValue(style, QStringLiteral("font_size_px"), base.fontSizePx));
    base.fontWeight = std::clamp(intValue(style, QStringLiteral("font_weight"), base.fontWeight), 1, 999);
    base.letterSpacingPx = intValue(style, QStringLiteral("letter_spacing_px"), base.letterSpacingPx);
    base.baseColor = stringValue(style, QStringLiteral("base_color"), base.baseColor);
    base.fillColor = stringValue(style, QStringLiteral("fill_color"), base.fillColor);
    base.rubyColor = stringValue(style, QStringLiteral("ruby_color"), base.rubyColor);
    const QString strokeColor = stringValue(style, QStringLiteral("stroke_color"), base.beforeStrokeColor);
    base.beforeStrokeColor = strokeColor;
    base.afterStrokeColor = strokeColor;
    base.rubyBeforeStrokeColor = strokeColor;
    base.rubyAfterStrokeColor = strokeColor;
    const QString shadowColor = stringValue(style, QStringLiteral("shadow_color"), base.beforeShadowColor);
    base.beforeShadowColor = shadowColor;
    base.afterShadowColor = shadowColor;
    base.rubyBeforeShadowColor = shadowColor;
    base.rubyAfterShadowColor = shadowColor;
    refreshLegacyMainFills(base);
    refreshLegacyRubyFills(base);
    base.strokeWidthPx = std::max(0, intValue(style, QStringLiteral("stroke_width_px"), base.strokeWidthPx));
    base.stroke2WidthPx = std::max(0, intValue(style, QStringLiteral("stroke2_width_px"), base.stroke2WidthPx));
    base.decorationKind = stringValue(style, QStringLiteral("decoration_kind"), base.decorationKind);
    base.glowRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_radius_px"), base.glowRadiusPx));
    base.glowBeforeRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_before_radius_px"), base.glowBeforeRadiusPx));
    base.glowAfterRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_after_radius_px"), base.glowAfterRadiusPx));
    base.shadowOffsetX = intValue(style, QStringLiteral("shadow_offset_x"), base.shadowOffsetX);
    base.shadowOffsetY = intValue(style, QStringLiteral("shadow_offset_y"), base.shadowOffsetY);
    base.rubyFontSizePx = std::max(1, intValue(style, QStringLiteral("ruby_font_size_px"), base.rubyFontSizePx));
    base.rubyGapPx = std::max(0, intValue(style, QStringLiteral("ruby_gap_px"), base.rubyGapPx));
    cfg.lineYMarginPx = std::max(0, intValue(style, QStringLiteral("line_y_margin_px"), cfg.lineYMarginPx));
    cfg.lineGapPx = std::max(0, intValue(style, QStringLiteral("line_gap_px"), cfg.lineGapPx));
    cfg.lineLeadInMs = std::max(0, intValue(style, QStringLiteral("line_lead_in_ms"), cfg.lineLeadInMs));
    cfg.lineTailMs = std::max(0, intValue(style, QStringLiteral("line_tail_ms"), cfg.lineTailMs));
    cfg.lineYPosition = stringValue(style, QStringLiteral("line_y_position"), cfg.lineYPosition);
    cfg.lineHorizontalLayout = stringValue(style, QStringLiteral("line_horizontal_layout"), cfg.lineHorizontalLayout);
    cfg.upperLineLeftMarginPx = std::max(0, intValue(style, QStringLiteral("upper_line_left_margin_px"), cfg.upperLineLeftMarginPx));
    cfg.lowerLineRightMarginPx = std::max(0, intValue(style, QStringLiteral("lower_line_right_margin_px"), cfg.lowerLineRightMarginPx));
    cfg.dualLineLayout = style.value(QStringLiteral("dual_line_layout")).isBool()
        ? style.value(QStringLiteral("dual_line_layout")).toBool()
        : cfg.dualLineLayout;
    cfg.rightToLeft = style.value(QStringLiteral("right_to_left")).isBool()
        ? style.value(QStringLiteral("right_to_left")).toBool()
        : cfg.rightToLeft;
    cfg.entryAnim = stringValue(style, QStringLiteral("entry_anim"), cfg.entryAnim);
    cfg.entryLeadMs = std::max(0, intValue(style, QStringLiteral("entry_lead_ms"), cfg.entryLeadMs));
    cfg.exitAnim = stringValue(style, QStringLiteral("exit_anim"), cfg.exitAnim);
    cfg.exitFadeMs = std::max(0, intValue(style, QStringLiteral("exit_fade_ms"), cfg.exitFadeMs));
    const bool hasMainKaraokeColors = style.value(QStringLiteral("karaoke_colors")).isObject();
    const bool hasRubyKaraokeColors = style.value(QStringLiteral("ruby_karaoke_colors")).isObject();
    base.hasMainKaraokeColors = hasMainKaraokeColors;
    base.hasRubyKaraokeColors = hasRubyKaraokeColors;
    cfg.singerStyleOverrides = style.value(QStringLiteral("singer_style_overrides")).toObject();
    cfg.customStyleSchemes = style.value(QStringLiteral("custom_style_schemes")).toObject();
    const QJsonObject mainKaraokeColors = style.value(QStringLiteral("karaoke_colors")).toObject();
    const QJsonObject rubyKaraokeColors = style.value(QStringLiteral("ruby_karaoke_colors")).toObject();

    applyMainKaraokeColors(base, mainKaraokeColors);

    if (hasRubyKaraokeColors) {
        applyRubyKaraokeColors(base, rubyKaraokeColors);
    } else if (hasMainKaraokeColors) {
        copyMainColorsToRuby(base);
    } else {
        refreshLegacyRubyFills(base);
    }

    const QJsonObject track = ir.value(QStringLiteral("track")).toObject();
    const QJsonArray lines = track.value(QStringLiteral("lines")).toArray();
    cfg.lines.reserve(static_cast<std::size_t>(lines.size()));
    for (const auto &lineValue : lines) {
        const QJsonObject lineObject = lineValue.toObject();
        TimingLine line;
        line.endMs = intValue(lineObject, QStringLiteral("end_ms"), 0);
        line.singerLabel = stringValue(lineObject, QStringLiteral("singer_label"));
        line.singerId = intValue(lineObject, QStringLiteral("singer_id"), -1);

        const QJsonArray chars = lineObject.value(QStringLiteral("chars")).toArray();
        line.chars.reserve(static_cast<std::size_t>(chars.size()));
        for (const auto &charValue : chars) {
            const QJsonObject charObject = charValue.toObject();
            TimingChar ch;
            ch.text = stringValue(charObject, QStringLiteral("text"));
            ch.startMs = intValue(charObject, QStringLiteral("start_ms"), 0);
            if (charObject.value(QStringLiteral("pause_release_ms")).isDouble()) {
                ch.pauseReleaseMs = charObject.value(QStringLiteral("pause_release_ms")).toInt();
            }
            ch.roleLabel = stringValue(charObject, QStringLiteral("role_label"));
            line.chars.push_back(ch);
        }
        cfg.lines.push_back(line);
    }

    const QJsonArray rubies = track.value(QStringLiteral("rubies")).toArray();
    cfg.rubies.reserve(static_cast<std::size_t>(rubies.size()));
    for (const auto &rubyValue : rubies) {
        const QJsonObject rubyObject = rubyValue.toObject();
        RubyAnnotation ruby;
        ruby.kanji = stringValue(rubyObject, QStringLiteral("kanji"));
        ruby.reading = stringValue(rubyObject, QStringLiteral("reading"));
        ruby.readingPartMs = parseIntArray(rubyObject.value(QStringLiteral("reading_part_ms")).toArray());
        ruby.posStartMs = intValue(rubyObject, QStringLiteral("pos_start_ms"), 0);
        ruby.posEndMs = intValue(rubyObject, QStringLiteral("pos_end_ms"), 0);
        cfg.rubies.push_back(ruby);
    }

    buildResolvedStyleCache(cfg);
    return cfg;
}

QString lineText(const TimingLine &line) {
    QString text;
    for (const auto &ch : line.chars) {
        text += ch.text;
    }
    return text;
}

bool lineHasRoleLabels(const TimingLine &line) {
    for (const auto &ch : line.chars) {
        if (!ch.roleLabel.isEmpty()) {
            return true;
        }
    }
    return false;
}

QString resolvedStyleKey(int singerId, const QString &roleLabel) {
    return QString::number(singerId) + QChar(0x1f) + roleLabel;
}

ResolvedStyle resolvedStyleForSinger(const RenderConfig &cfg, int singerId) {
    if (singerId < 0) {
        return cfg.baseStyle;
    }
    const QJsonValue value = cfg.singerStyleOverrides.value(QString::number(singerId));
    if (!value.isObject()) {
        return cfg.baseStyle;
    }
    return styleWithOverrides(cfg.baseStyle, value.toObject());
}

ResolvedStyle resolvedStyleForRole(const RenderConfig &cfg, const ResolvedStyle &lineStyle, const QString &roleLabel) {
    if (roleLabel.isEmpty()) {
        return lineStyle;
    }
    const QJsonValue value = cfg.customStyleSchemes.value(roleLabel);
    if (!value.isObject()) {
        return lineStyle;
    }
    return styleWithOverrides(lineStyle, value.toObject());
}

void cacheResolvedStyle(RenderConfig &cfg, int singerId, const QString &roleLabel) {
    const QString key = resolvedStyleKey(singerId, roleLabel);
    if (cfg.resolvedStyles.contains(key)) {
        return;
    }
    const ResolvedStyle lineStyle = resolvedStyleForSinger(cfg, singerId);
    const ResolvedStyle finalStyle = resolvedStyleForRole(cfg, lineStyle, roleLabel);
    cfg.resolvedStyles.insert(key, finalStyle);
}

void buildResolvedStyleCache(RenderConfig &cfg) {
    cfg.resolvedStyles.clear();
    cacheResolvedStyle(cfg, -1, QString());
    for (const TimingLine &line : cfg.lines) {
        cacheResolvedStyle(cfg, line.singerId, QString());
        for (const TimingChar &ch : line.chars) {
            if (!ch.roleLabel.isEmpty()) {
                cacheResolvedStyle(cfg, line.singerId, ch.roleLabel);
            }
        }
    }
}

const ResolvedStyle &resolvedStyleForLine(const RenderConfig &cfg, const TimingLine &line) {
    const auto it = cfg.resolvedStyles.constFind(resolvedStyleKey(line.singerId, QString()));
    if (it != cfg.resolvedStyles.constEnd()) {
        return it.value();
    }
    return cfg.baseStyle;
}

const ResolvedStyle &resolvedStyleForCharacter(const RenderConfig &cfg, const TimingLine &line, const TimingChar &ch) {
    const auto it = cfg.resolvedStyles.constFind(resolvedStyleKey(line.singerId, ch.roleLabel));
    if (it != cfg.resolvedStyles.constEnd()) {
        return it.value();
    }
    return resolvedStyleForLine(cfg, line);
}

int lineStartMs(const TimingLine &line) {
    if (line.chars.empty()) {
        return 0;
    }
    return line.chars.front().startMs;
}

bool lineVisible(const TimingLine &line, int tMs, const RenderConfig &cfg) {
    if (line.chars.empty()) {
        return false;
    }
    const int start = lineStartMs(line) - cfg.lineLeadInMs;
    const int end = std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs;
    return start <= tMs && tMs <= end;
}

int charEndMs(const TimingLine &line, std::size_t index) {
    if (index >= line.chars.size()) {
        return 0;
    }
    const TimingChar &ch = line.chars[index];
    if (ch.pauseReleaseMs.has_value()) {
        return std::max(ch.startMs, ch.pauseReleaseMs.value());
    }
    if (index + 1 < line.chars.size()) {
        return std::max(ch.startMs, line.chars[index + 1].startMs);
    }
    if (line.endMs > ch.startMs) {
        return line.endMs;
    }
    return ch.startMs + 1;
}

double progressRatio(int startMs, int endMs, int tMs) {
    if (endMs <= startMs) {
        return tMs >= startMs ? 1.0 : 0.0;
    }
    const double raw = static_cast<double>(tMs - startMs) / static_cast<double>(endMs - startMs);
    return std::clamp(raw, 0.0, 1.0);
}

int lineDisplayEndMs(const TimingLine &line, const RenderConfig &cfg) {
    if (line.chars.empty()) {
        return 0;
    }
    return std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs;
}

int nextValidCharIndex(const TimingLine &line, std::size_t startIndex) {
    for (std::size_t i = startIndex; i < line.chars.size(); ++i) {
        if (!line.chars[i].text.trimmed().isEmpty()) {
            return static_cast<int>(i);
        }
    }
    return -1;
}

int utopiaTailDelayMs(const RenderConfig &cfg) {
    return std::max(0, cfg.lineTailMs - kUtopiaFadeOutTimeMs);
}

int utopiaFollowingDoneTime(
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index,
    const RenderConfig &cfg
) {
    if (intervals.empty()) {
        return line.endMs;
    }
    index = std::clamp(index, 0, static_cast<int>(intervals.size()) - 1);
    const int currentEnd = intervals[static_cast<std::size_t>(index)].second;
    const int nextIndex = nextValidCharIndex(line, static_cast<std::size_t>(index + 1));
    if (nextIndex >= 0 && nextIndex < static_cast<int>(intervals.size())) {
        const int nextEnd = intervals[static_cast<std::size_t>(nextIndex)].second;
        if (currentEnd <= nextEnd) {
            return nextEnd;
        }
    }
    return currentEnd + utopiaTailDelayMs(cfg);
}

bool isUtopiaWiping(int tMs, int charStartMs, int charEndMs) {
    return charStartMs < tMs && tMs < charEndMs && charStartMs != charEndMs;
}

double utopiaWipeScale(int tMs, int charStartMs, int charEndMs) {
    if (!isUtopiaWiping(tMs, charStartMs, charEndMs)) {
        return 1.0;
    }
    const int overMs = std::min(
        static_cast<int>((charEndMs - charStartMs) * kUtopiaWipeOverTimeRatio),
        kUtopiaWipeOverTimeLimitMs
    );
    if (overMs <= 0) {
        return 1.0;
    }
    const int peakMs = charStartMs + overMs;
    double progress = 0.0;
    if (tMs <= peakMs) {
        progress = static_cast<double>(tMs - charStartMs) / overMs;
    } else {
        const int releaseMs = std::max(charEndMs - peakMs, 1);
        progress = static_cast<double>(charEndMs - tMs) / releaseMs;
    }
    return 1.0 + (kUtopiaWipeOverRatio - 1.0) * std::clamp(progress, 0.0, 1.0);
}

std::optional<LineCharTransition> lineCharTransitionContext(
    const RenderConfig &cfg,
    const TimingLine &line,
    int tMs,
    const std::vector<std::pair<int, int>> &intervals
) {
    if (line.chars.empty()) {
        return std::nullopt;
    }
    if (cfg.entryAnim != QStringLiteral("utopia") && cfg.exitAnim != QStringLiteral("utopia")) {
        return std::nullopt;
    }

    const int start = lineStartMs(line);
    const int end = lineDisplayEndMs(line, cfg);
    const bool inIntro = cfg.entryAnim == QStringLiteral("utopia") && tMs <= start + kUtopiaIntroTimeMs;
    const bool inExit = cfg.exitAnim == QStringLiteral("utopia")
        && !intervals.empty()
        && utopiaFollowingDoneTime(line, intervals, 0, cfg) <= tMs
        && tMs <= end;
    bool inWipe = false;
    for (const auto &interval : intervals) {
        if (isUtopiaWiping(tMs, interval.first, interval.second)) {
            inWipe = true;
            break;
        }
    }
    if (!inIntro && !inExit && !inWipe) {
        return std::nullopt;
    }

    return LineCharTransition{
        QStringLiteral("utopia"),
        QStringLiteral("utopia"),
        1.0,
        start,
        end,
    };
}

QTransform characterTransform(
    double centerX,
    double centerY,
    const AnimationState &state,
    std::optional<QPointF> scaleOrigin = std::nullopt
) {
    QTransform transform;
    if (state.dx == 0.0
        && state.dy == 0.0
        && state.rotation == 0.0
        && state.scaleX == 1.0
        && state.scaleY == 1.0
        && state.skewY == 0.0) {
        return transform;
    }
    if (scaleOrigin.has_value()) {
        transform.translate(scaleOrigin->x() + state.dx, scaleOrigin->y() + state.dy);
        if (state.skewY != 0.0) {
            transform.shear(0.0, state.skewY);
        }
        if (state.scaleX != 1.0 || state.scaleY != 1.0) {
            transform.scale(state.scaleX, state.scaleY);
        }
        transform.translate(centerX - scaleOrigin->x(), centerY - scaleOrigin->y());
        if (state.rotation != 0.0) {
            transform.rotate(state.rotation);
        }
        transform.translate(-centerX, -centerY);
        return transform;
    }
    transform.translate(centerX + state.dx, centerY + state.dy);
    if (state.rotation != 0.0) {
        transform.rotate(state.rotation);
    }
    if (state.skewY != 0.0) {
        transform.shear(0.0, state.skewY);
    }
    if (state.scaleX != 1.0 || state.scaleY != 1.0) {
        transform.scale(state.scaleX, state.scaleY);
    }
    transform.translate(-centerX, -centerY);
    return transform;
}

AnimationState transitionCharState(
    const RenderConfig &cfg,
    const LineCharTransition &transition,
    const std::vector<std::pair<int, int>> &intervals,
    int index,
    int count,
    int tMs,
    int frameHeight,
    int followingDoneMs,
    std::optional<std::pair<int, int>> overrideInterval = std::nullopt
) {
    if (transition.effect == QStringLiteral("utopia") && transition.phase == QStringLiteral("utopia")) {
        if (cfg.entryAnim == QStringLiteral("utopia") && tMs <= transition.startMs + kUtopiaIntroTimeMs) {
            const int delay = count <= 1 ? 0 : kUtopiaIntroDelayMs / (count - 1) * index;
            const int elapsed = tMs - transition.startMs - delay;
            if (elapsed < 0) {
                return AnimationState{0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
            }
            const double opacity = std::min(static_cast<double>(elapsed) / kUtopiaIntroEnlargeMs, 1.0);
            double scale = 1.0;
            if (elapsed < kUtopiaIntroEnlargeMs) {
                scale = kUtopiaIntroOverRatio * static_cast<double>(elapsed) / kUtopiaIntroEnlargeMs;
            } else if (elapsed < kUtopiaIntroEnlargeMs + kUtopiaIntroCondenseMs) {
                const int remaining = kUtopiaIntroEnlargeMs + kUtopiaIntroCondenseMs - elapsed;
                scale = 1.0 + (kUtopiaIntroOverRatio - 1.0) * static_cast<double>(remaining) / kUtopiaIntroCondenseMs;
            }
            return AnimationState{opacity, 0.0, 0.0, 0.0, scale, scale, 0.0};
        }

        if (cfg.exitAnim == QStringLiteral("utopia") && tMs > followingDoneMs) {
            double local = static_cast<double>(tMs - followingDoneMs) / kUtopiaFadeOutTimeMs;
            local = std::clamp(local, 0.0, 1.0);
            const double opacity = std::max(0.0, 1.0 - local);
            const double shrink = 1.0 - local;
            const double amp = std::max(frameHeight, 1) / 15.0;
            const double xTravel = local <= 0.5
                ? std::sin(kPi * local) * amp
                : amp + std::sin((local - 0.5) * kPi) * amp;
            const double yTravel = std::sin(kPi * local / 2.0) * amp;
            const double xFlip = std::cos(kPi * local);
            return AnimationState{
                opacity,
                -xTravel,
                yTravel,
                -180.0 * local,
                shrink * xFlip,
                shrink,
                0.0,
            };
        }

        if (overrideInterval.has_value() || (index >= 0 && index < static_cast<int>(intervals.size()))) {
            const auto interval = overrideInterval.has_value()
                ? overrideInterval.value()
                : intervals[static_cast<std::size_t>(index)];
            if (isUtopiaWiping(tMs, interval.first, interval.second)) {
                const double scale = utopiaWipeScale(tMs, interval.first, interval.second);
                return AnimationState{1.0, 0.0, 0.0, 0.0, scale, scale, 0.0};
            }
        }
    }

    return AnimationState{};
}

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

QFont buildLineFont(const ResolvedStyle &style) {
    QFont font(style.fontFamily);
    font.setPixelSize(style.fontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(style.fontWeight, 1, 999)));
    return font;
}

QFont buildRubyFont(const ResolvedStyle &style) {
    QFont font(style.fontFamily);
    font.setPixelSize(style.rubyFontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(style.fontWeight, 1, 999)));
    return font;
}

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
    return std::max(value, 1);
}

double glowPenWidth(const ResolvedStyle &style, bool after) {
    const double baseWidth = style.stroke2WidthPx > 0 ? stroke2PenWidth(style) : strokePenWidth(style);
    return std::max(1.0, baseWidth + glowRadius(style, after));
}

double glowExtent(const ResolvedStyle &style, bool after) {
    const int radius = glowRadius(style, after);
    return std::ceil(glowPenWidth(style, after) / 2.0 + radius * 3.0);
}

int glowPenWidthForWidths(int strokeWidth, int stroke2Width, int glowRadiusValue) {
    const int baseWidth = stroke2Width > 0
        ? std::max(strokeWidth, 0) + std::max(stroke2Width, 0)
        : std::max(strokeWidth, 0);
    return std::max(1, baseWidth + std::max(glowRadiusValue, 1));
}

double glowExtentForWidths(int strokeWidth, int stroke2Width, int glowRadiusValue) {
    return std::ceil(
        glowPenWidthForWidths(strokeWidth, stroke2Width, glowRadiusValue) / 2.0
        + std::max(glowRadiusValue, 1) * 3.0
    );
}

double afterClipVerticalExtent(const ResolvedStyle &style) {
    const double strokeExtent = visualStrokeExtent(style);
    const double glowExtra = style.decorationKind == QStringLiteral("glow") ? glowExtent(style, true) : 0.0;
    const double shadowExtra = style.decorationKind == QStringLiteral("shadow") ? std::abs(style.shadowOffsetY) : 0.0;
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
    if (style.decorationKind == QStringLiteral("glow")) {
        const int rubyGlowRadius = scaledPx(glowRadius(style, true), scale);
        const int baseWidth = stroke2Width > 0 ? strokeWidth + stroke2Width : strokeWidth;
        glowExtra = std::ceil((std::max(1, baseWidth + rubyGlowRadius)) / 2.0 + std::max(rubyGlowRadius, 1) * 3.0);
    }
    const double shadowX = style.decorationKind == QStringLiteral("shadow") ? std::abs(scaledSignedPx(style.shadowOffsetX, scale)) : 0.0;
    const double shadowY = style.decorationKind == QStringLiteral("shadow") ? std::abs(scaledSignedPx(style.shadowOffsetY, scale)) : 0.0;
    return std::max({strokeExtent, glowExtra, shadowX, shadowY, 2.0});
}

double baselineYForLine(const RenderConfig &cfg, const ResolvedStyle &style, const QFontMetricsF &metrics, int lane, int visibleLineCount) {
    const double pad = visualStrokeExtent(style);
    if (cfg.dualLineLayout) {
        const double mainHeight = metrics.ascent() + metrics.descent() + pad * 2.0;
        const double mainAscent = metrics.ascent() + pad;
        const double mainDescent = metrics.descent() + pad;
        double upperBaseline = 0.0;
        double lowerBaseline = 0.0;
        if (cfg.lineYPosition == QStringLiteral("top")) {
            upperBaseline = cfg.lineYMarginPx + mainAscent;
            lowerBaseline = upperBaseline + mainHeight + cfg.lineGapPx;
        } else if (cfg.lineYPosition == QStringLiteral("center")) {
            const double totalHeight = mainHeight * 2.0 + cfg.lineGapPx;
            const double upperMainTop = std::floor((cfg.height - totalHeight) / 2.0);
            upperBaseline = upperMainTop + mainAscent;
            lowerBaseline = upperBaseline + mainHeight + cfg.lineGapPx;
        } else {
            lowerBaseline = cfg.height - cfg.lineYMarginPx - mainDescent;
            upperBaseline = lowerBaseline - mainHeight - cfg.lineGapPx;
        }
        return (cfg.dualLineLayout && std::min(lane, 1) == 1) ? lowerBaseline : upperBaseline;
    }

    if (cfg.lineYPosition == QStringLiteral("top")) {
        return cfg.lineYMarginPx + pad + metrics.ascent();
    }
    if (cfg.lineYPosition == QStringLiteral("center")) {
        const double blockHeight = metrics.height() + pad * 2.0;
        return std::floor((cfg.height - blockHeight) / 2.0) + pad + metrics.ascent();
    }
    return cfg.height - cfg.lineYMarginPx - pad - metrics.descent();
}

double lineXForLine(const RenderConfig &cfg, double lineWidth, double pad, int lane) {
    if (cfg.lineHorizontalLayout == QStringLiteral("center")) {
        return (cfg.width - lineWidth) * 0.5;
    }
    if (cfg.dualLineLayout && std::min(lane, 1) == 0) {
        return cfg.upperLineLeftMarginPx + pad;
    }
    if (cfg.dualLineLayout && std::min(lane, 1) == 1) {
        return cfg.width - cfg.lowerLineRightMarginPx - lineWidth - pad;
    }
    return (cfg.width - lineWidth) * 0.5;
}

LineLayout layoutLine(const RenderConfig &cfg, const ResolvedStyle &lineStyle, const TimingLine &line, int lane, int visibleLineCount) {
    const QString text = lineText(line);
    const bool inlineStyles = lineHasRoleLabels(line);

    LineLayout layout;
    layout.text = text;
    layout.font = buildLineFont(lineStyle);
    layout.lineStyle = &lineStyle;
    layout.hasInlineStyles = inlineStyles;

    const QFontMetricsF metrics(layout.font);

    layout.charWidths.reserve(line.chars.size());
    layout.charFonts.reserve(line.chars.size());
    layout.charStyles.reserve(line.chars.size());
    double totalWidth = 0.0;
    double maxAscent = 0.0;
    double maxDescent = 0.0;
    double maxVisualPad = visualStrokeExtent(lineStyle);
    double maxAfterClipExtent = afterClipVerticalExtent(lineStyle);
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const auto &ch = line.chars[i];
        const ResolvedStyle &charStyle = inlineStyles ? resolvedStyleForCharacter(cfg, line, ch) : lineStyle;
        QFont charFont = inlineStyles ? buildLineFont(charStyle) : layout.font;
        const QFontMetricsF charMetrics(charFont);
        const double width = std::max(1.0, charMetrics.horizontalAdvance(ch.text));
        layout.charWidths.push_back(width);
        layout.charFonts.push_back(charFont);
        layout.charStyles.push_back(&charStyle);
        totalWidth += width;
        maxAscent = std::max(maxAscent, charMetrics.ascent());
        maxDescent = std::max(maxDescent, charMetrics.descent());
        maxVisualPad = std::max(maxVisualPad, visualStrokeExtent(charStyle));
        maxAfterClipExtent = std::max(maxAfterClipExtent, afterClipVerticalExtent(charStyle));
        if (i + 1 < line.chars.size()) {
            totalWidth += charStyle.letterSpacingPx;
        }
    }
    layout.ascent = inlineStyles ? maxAscent : metrics.ascent();
    layout.descent = inlineStyles ? maxDescent : metrics.descent();
    layout.height = layout.ascent + layout.descent;
    layout.afterClipExtent = maxAfterClipExtent;
    layout.width = std::max(1.0, totalWidth);
    const double visualPad = inlineStyles ? maxVisualPad : visualStrokeExtent(lineStyle);
    layout.x = lineXForLine(cfg, layout.width, visualPad, lane);

    layout.baselineY = baselineYForLine(cfg, lineStyle, metrics, lane, visibleLineCount);

    layout.charLefts.resize(line.chars.size());
    if (cfg.rightToLeft) {
        double cursor = layout.x + layout.width;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            cursor -= layout.charWidths[i];
            layout.charLefts[i] = cursor;
            cursor -= (i + 1 < layout.charStyles.size()) ? layout.charStyles[i]->letterSpacingPx : 0;
        }
    } else {
        double cursor = layout.x;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.charLefts[i] = cursor;
            cursor += layout.charWidths[i] + ((i + 1 < layout.charStyles.size()) ? layout.charStyles[i]->letterSpacingPx : 0);
        }
    }

    // C2 keeps one complete line path for both before/after layers. Karaoke
    // progress is expressed only by clipping the after layer, not by rebuilding
    // a prefix string path that can drift under kerning/shaping.
    if (inlineStyles) {
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        }
    } else {
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        }
    }
    return layout;
}

std::optional<QRectF> afterClipRectFromCharacterTiming(const RenderConfig &cfg, const ResolvedStyle &style, const TimingLine &line, const LineLayout &layout, int tMs) {
    if (line.chars.empty()) {
        return std::nullopt;
    }

    double clipEdge = cfg.rightToLeft ? layout.x + layout.width : layout.x;
    bool hasProgress = false;
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const int start = line.chars[i].startMs;
        const int end = charEndMs(line, i);
        const double left = layout.charLefts[i];
        const double right = left + layout.charWidths[i];

        if (tMs < start) {
            break;
        }

        hasProgress = true;
        const double ratio = progressRatio(start, end, tMs);
        if (ratio < 1.0) {
            clipEdge = cfg.rightToLeft
                ? right - layout.charWidths[i] * ratio
                : left + layout.charWidths[i] * ratio;
            break;
        }

        clipEdge = cfg.rightToLeft ? left : right;
    }
    if (!hasProgress) {
        return std::nullopt;
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

std::vector<std::pair<int, int>> lineIntervals(const TimingLine &line) {
    std::vector<std::pair<int, int>> intervals;
    intervals.reserve(line.chars.size());
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        intervals.push_back({line.chars[i].startMs, charEndMs(line, i)});
    }
    return intervals;
}

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

std::vector<int> rubyTargetIndices(
    const RubyAnnotation &ruby,
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals
) {
    const auto timeIndices = rubyTimeIndices(ruby, intervals);
    if (!ruby.kanji.isEmpty()) {
        const auto span = findRubyTextSpan(ruby.kanji, line, timeIndices);
        if (!span.has_value()) {
            return {};
        }
        return textSpanIndices(span.value(), line);
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

std::vector<QString> rubyReadingUnits(const QString &reading) {
    std::vector<QString> units;
    units.reserve(static_cast<std::size_t>(reading.size()));
    for (const QChar &ch : reading) {
        units.push_back(QString(ch));
    }
    return units;
}

std::vector<int> rubyReadingBoundaries(const RubyAnnotation &ruby, int unitCount) {
    if (unitCount <= 0) {
        return {ruby.posStartMs, ruby.posEndMs};
    }
    std::vector<int> boundaries{ruby.posStartMs};
    const int usableParts = std::max(unitCount - 1, 0);
    for (int i = 0; i < usableParts && static_cast<std::size_t>(i) < ruby.readingPartMs.size(); ++i) {
        int ts = ruby.posStartMs + ruby.readingPartMs[i];
        ts = std::max(boundaries.back(), std::min(ruby.posEndMs, ts));
        boundaries.push_back(ts);
    }
    if (static_cast<int>(boundaries.size()) < unitCount) {
        const int start = boundaries.back();
        const int remaining = unitCount - static_cast<int>(boundaries.size()) + 1;
        for (int step = 1; step < remaining; ++step) {
            boundaries.push_back(start + static_cast<int>(std::round((ruby.posEndMs - start) * step / static_cast<double>(remaining))));
        }
    }
    boundaries.push_back(std::max(boundaries.back(), ruby.posEndMs));
    return boundaries;
}

std::vector<std::pair<int, int>> rubyReadingIntervals(const RubyAnnotation &ruby) {
    const auto units = rubyReadingUnits(ruby.reading);
    const int unitCount = static_cast<int>(units.size());
    if (static_cast<int>(ruby.readingPartMs.size()) >= 2 * std::max(unitCount - 1, 0)) {
        std::vector<std::pair<int, int>> intervals;
        int currentStart = ruby.posStartMs;
        for (int i = 0; i < unitCount - 1; ++i) {
            int release = ruby.posStartMs + ruby.readingPartMs[i * 2];
            int nextStart = ruby.posStartMs + ruby.readingPartMs[i * 2 + 1];
            release = std::max(currentStart, std::min(release, ruby.posEndMs));
            nextStart = std::max(release, std::min(nextStart, ruby.posEndMs));
            intervals.push_back({currentStart, release});
            currentStart = nextStart;
        }
        intervals.push_back({currentStart, std::max(currentStart, ruby.posEndMs)});
        return intervals;
    }

    std::vector<std::pair<int, int>> intervals;
    const auto boundaries = rubyReadingBoundaries(ruby, unitCount);
    for (int i = 0; i < unitCount; ++i) {
        int start = boundaries[i];
        int end = boundaries[i + 1];
        if (end < start) {
            end = start;
        }
        intervals.push_back({start, end});
    }
    return intervals;
}

std::vector<QString> rubyUtopiaVisualUnits(const QString &text) {
    std::vector<QString> units;
    for (const QChar &ch : text) {
        if (!units.empty() && (ch == QChar(0x3099) || ch == QChar(0x309A))) {
            units.back().append(ch);
        } else {
            units.push_back(QString(ch));
        }
    }
    return units;
}

std::vector<std::pair<QString, std::pair<int, int>>> rubyUtopiaReadingUnitsAndIntervals(
    const RubyAnnotation &ruby
) {
    const auto moraUnits = rubyReadingUnits(ruby.reading);
    const auto moraIntervals = rubyReadingIntervals(ruby);
    std::vector<std::pair<QString, std::pair<int, int>>> out;
    const std::size_t count = std::min(moraUnits.size(), moraIntervals.size());
    for (std::size_t i = 0; i < count; ++i) {
        const auto visualUnits = rubyUtopiaVisualUnits(moraUnits[i]);
        if (visualUnits.size() <= 1) {
            out.push_back({moraUnits[i], moraIntervals[i]});
            continue;
        }
        const int start = moraIntervals[i].first;
        const int end = moraIntervals[i].second;
        const int duration = std::max(end - start, 0);
        for (std::size_t j = 0; j < visualUnits.size(); ++j) {
            const int unitStart = start + static_cast<int>(std::round(duration * static_cast<double>(j) / visualUnits.size()));
            const int unitEnd = start + static_cast<int>(std::round(duration * static_cast<double>(j + 1) / visualUnits.size()));
            out.push_back({visualUnits[j], {unitStart, std::max(unitStart, unitEnd)}});
        }
    }
    return out;
}

double rubyProgressRatio(const RubyAnnotation &ruby, int tMs) {
    if (ruby.reading.isEmpty() || ruby.readingPartMs.empty()) {
        return progressRatio(ruby.posStartMs, ruby.posEndMs, tMs);
    }
    const auto intervals = rubyReadingIntervals(ruby);
    const int total = std::max(static_cast<int>(intervals.size()), 1);
    for (int i = 0; i < static_cast<int>(intervals.size()); ++i) {
        const int start = intervals[i].first;
        const int end = intervals[i].second;
        if (tMs < start) {
            return static_cast<double>(i) / total;
        }
        if (tMs < end) {
            return (i + progressRatio(start, end, tMs)) / total;
        }
    }
    return 1.0;
}

double rubyLayoutWidth(const QString &reading, const QFontMetricsF &metrics, double targetWidth) {
    const double natural = metrics.horizontalAdvance(reading);
    if (targetWidth <= natural) {
        return natural;
    }
    return targetWidth;
}

QPainterPath rubyTextPath(
    const QString &reading,
    const QFont &font,
    const QFontMetricsF &metrics,
    double x,
    double baselineY,
    double targetWidth
) {
    const auto units = rubyReadingUnits(reading);
    std::vector<double> widths;
    widths.reserve(units.size());
    double natural = 0.0;
    for (const QString &unit : units) {
        const double width = metrics.horizontalAdvance(unit);
        widths.push_back(width);
        natural += width;
    }

    QPainterPath path;
    if (units.empty()) {
        return path;
    }
    if (units.size() <= 1 || targetWidth <= natural * 1.15) {
        double cursor = x + std::max((targetWidth - natural) / 2.0, 0.0);
        for (std::size_t i = 0; i < units.size(); ++i) {
            path.addText(QPointF(cursor, baselineY), font, units[i]);
            cursor += widths[i];
        }
        return path;
    }

    const double slotWidth = targetWidth / static_cast<double>(units.size());
    for (std::size_t i = 0; i < units.size(); ++i) {
        const double unitX = x + slotWidth * static_cast<double>(i) + (slotWidth - widths[i]) / 2.0;
        path.addText(QPointF(unitX, baselineY), font, units[i]);
    }
    return path;
}

std::vector<RubyUnitLayout> rubyUnitLayouts(
    const std::vector<std::pair<QString, std::pair<int, int>>> &unitsAndIntervals,
    const QFontMetricsF &metrics,
    double x,
    double targetWidth
) {
    std::vector<RubyUnitLayout> out;
    if (unitsAndIntervals.empty()) {
        return out;
    }
    std::vector<double> widths;
    widths.reserve(unitsAndIntervals.size());
    double natural = 0.0;
    for (const auto &item : unitsAndIntervals) {
        const double width = metrics.horizontalAdvance(item.first);
        widths.push_back(width);
        natural += width;
    }
    if (unitsAndIntervals.size() <= 1 || targetWidth <= natural * 1.15) {
        double cursor = x + std::max((targetWidth - natural) / 2.0, 0.0);
        for (std::size_t i = 0; i < unitsAndIntervals.size(); ++i) {
            out.push_back(RubyUnitLayout{unitsAndIntervals[i].first, unitsAndIntervals[i].second, cursor, widths[i]});
            cursor += widths[i];
        }
        return out;
    }

    const double slotWidth = targetWidth / static_cast<double>(unitsAndIntervals.size());
    for (std::size_t i = 0; i < unitsAndIntervals.size(); ++i) {
        const double unitX = x + slotWidth * static_cast<double>(i) + (slotWidth - widths[i]) / 2.0;
        out.push_back(RubyUnitLayout{unitsAndIntervals[i].first, unitsAndIntervals[i].second, unitX, widths[i]});
    }
    return out;
}

std::vector<RubyDiagnostics> rubyDiagnosticsForLine(
    const RenderConfig &cfg,
    const ResolvedStyle &style,
    const TimingLine &line,
    const LineLayout &layout,
    int tMs
) {
    std::vector<RubyDiagnostics> diagnostics;
    if (cfg.rubies.empty()) {
        return diagnostics;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const auto intervals = lineIntervals(line);
    const double rubyBaselineY = layout.baselineY - layout.ascent - style.rubyGapPx;
    const double pad = rubyVisualPadding(style);

    for (const RubyAnnotation &ruby : cfg.rubies) {
        const auto indices = rubyTargetIndices(ruby, line, intervals);
        if (indices.empty()) {
            continue;
        }
        const auto targetRange = rubyTargetXRange(ruby, line, layout, intervals);
        if (!targetRange.has_value()) {
            continue;
        }
        const RubyAnnotation paintRuby = effectiveRubyForTarget(ruby, indices, intervals);
        const double x = targetRange->first;
        const double targetWidth = std::max(targetRange->second - targetRange->first, 1.0);
        const double readingWidth = rubyLayoutWidth(paintRuby.reading, rubyMetrics, targetWidth);
        const double ratio = rubyProgressRatio(paintRuby, tMs);
        const double ratioC = std::min(ratio, 1.0);
        const QRectF rect(x, rubyBaselineY - rubyMetrics.ascent(), readingWidth, rubyMetrics.height());
        const double clipLeft = cfg.rightToLeft
            ? rect.left() + rect.width() * (1.0 - ratioC) - pad
            : rect.left() - pad;
        const double clipWidth = rect.width() * ratioC + pad;

        RubyDiagnostics item;
        item.kanji = paintRuby.kanji;
        item.reading = paintRuby.reading;
        item.indices = indices;
        item.x = x;
        item.baselineY = rubyBaselineY;
        item.targetWidth = targetWidth;
        item.readingWidth = readingWidth;
        item.progress = ratio;
        item.afterClipLeft = clipLeft;
        item.afterClipRight = clipLeft + clipWidth;
        item.afterClipTop = rect.top() - pad;
        item.afterClipHeight = rect.height() + pad * 2.0;
        diagnostics.push_back(item);
    }
    return diagnostics;
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

void paintKaraokePathWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    int strokeWidth,
    int stroke2Width
) {
    if (stroke2Width > 0) {
        painter.strokePath(path, QPen(brushForFill(stroke2, rect), strokeWidth + stroke2Width, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    if (strokeWidth > 0) {
        painter.strokePath(path, QPen(brushForFill(stroke, rect), strokeWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    painter.fillPath(path, brushForFill(fill, rect));
}

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

std::vector<GlowBitmapCacheEntry> &glowBitmapCache() {
    static std::vector<GlowBitmapCacheEntry> cache;
    return cache;
}

std::mutex &glowBitmapCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

GlowBitmapCacheStats &glowBitmapCacheStats() {
    static GlowBitmapCacheStats stats;
    return stats;
}

void clearGlowBitmapCache() {
    std::lock_guard<std::mutex> lock(glowBitmapCacheMutex());
    glowBitmapCache().clear();
    glowBitmapCacheStats() = GlowBitmapCacheStats{};
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

QImage cachedBlurImage(const QImage &source, int radius, const QString &scope = QStringLiteral("unknown")) {
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

GlowLayerImage buildGlowLayerWithWidths(
    const QPainterPath &path,
    const PaintFillSpec &fill,
    const QRectF &rect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QString &scope = QStringLiteral("unknown")
) {
    const int glowRadius = std::max(radius, 1);
    const int baseWidth = stroke2Width > 0 ? strokeWidth + stroke2Width : std::max(strokeWidth, 0);
    const int glowWidth = std::max(1, baseWidth + glowRadius);
    const QRectF bounds = path.boundingRect();
    if (bounds.isEmpty()) {
        return GlowLayerImage{};
    }
    const double pad = std::ceil(glowWidth / 2.0 + glowRadius * 3.0) + 2.0;
    const QRectF layerRect = bounds.adjusted(-pad, -pad, pad, pad);
    const int imageWidth = std::max(1, static_cast<int>(std::ceil(layerRect.width())));
    const int imageHeight = std::max(1, static_cast<int>(std::ceil(layerRect.height())));

    QImage source(imageWidth, imageHeight, QImage::Format_ARGB32_Premultiplied);
    source.fill(Qt::transparent);

    QPainterPath localPath(path);
    localPath.translate(-layerRect.left(), -layerRect.top());
    const QRectF localRect = rect.translated(-layerRect.left(), -layerRect.top());

    QPainter layerPainter(&source);
    layerPainter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing);
    layerPainter.strokePath(localPath, QPen(brushForFill(fill, localRect), glowWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    layerPainter.end();

    return GlowLayerImage{
        cachedBlurImage(source, glowRadius, scope),
        QPointF(layerRect.left(), layerRect.top()),
    };
}

void paintGlowPathWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const PaintFillSpec &fill,
    const QRectF &rect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QString &scope = QStringLiteral("text")
) {
    const GlowLayerImage layer = buildGlowLayerWithWidths(path, fill, rect, radius, strokeWidth, stroke2Width, scope);
    if (!layer.image.isNull()) {
        painter.drawImage(layer.offset, layer.image);
    }
}

void blitTransformedGlowLayerWithWidths(
    QPainter &painter,
    const QPainterPath &uprightPath,
    const PaintFillSpec &fill,
    const QRectF &uprightRect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QTransform &transform,
    const QString &scope = QStringLiteral("transformed_text")
) {
    const GlowLayerImage layer = buildGlowLayerWithWidths(
        uprightPath,
        fill,
        uprightRect,
        radius,
        strokeWidth,
        stroke2Width,
        scope
    );
    if (layer.image.isNull()) {
        return;
    }
    painter.save();
    painter.setRenderHint(QPainter::SmoothPixmapTransform, true);
    painter.setTransform(transform, true);
    painter.drawImage(layer.offset, layer.image);
    painter.restore();
}

void paintTextLayerStackWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue,
    bool drawGlow = true,
    const QString &glowScope = QStringLiteral("text")
) {
    if (style.decorationKind == QStringLiteral("glow") && drawGlow) {
        paintGlowPathWithWidths(
            painter,
            path,
            shadow,
            rect,
            glowRadiusValue,
            strokeWidth,
            stroke2Width,
            glowScope
        );
    } else if (shadowOffsetX != 0 || shadowOffsetY != 0) {
        QPainterPath shadowPath(path);
        shadowPath.translate(shadowOffsetX, shadowOffsetY);
        painter.fillPath(shadowPath, brushForFill(shadow, rect.translated(shadowOffsetX, shadowOffsetY)));
    }

    paintKaraokePathWithWidths(
        painter,
        path,
        rect,
        fill,
        stroke,
        stroke2,
        strokeWidth,
        stroke2Width
    );
}

RubyLayerImage buildRubyTextLayer(
    const RubyDiagnostics &ruby,
    const QFont &rubyFont,
    const QFontMetricsF &rubyMetrics,
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
    const double strokeExtent = visualStrokeExtentForWidths(strokeWidth, stroke2Width);
    const double glowExtra = style.decorationKind == QStringLiteral("glow")
        ? glowExtentForWidths(strokeWidth, stroke2Width, glowRadiusValue)
        : 0.0;
    const int extent = static_cast<int>(std::max({
        strokeExtent,
        glowExtra,
        static_cast<double>(std::abs(shadowOffsetX)),
        static_cast<double>(std::abs(shadowOffsetY)),
        2.0,
    })) + 4;
    const int padLeft = std::max(0, -shadowOffsetX) + extent;
    const int padRight = std::max(0, shadowOffsetX) + extent;
    const int padTop = std::max(0, -shadowOffsetY) + extent;
    const int padBottom = std::max(0, shadowOffsetY) + extent;

    const int rubyWidth = std::max(1, static_cast<int>(std::ceil(ruby.readingWidth)));
    const int rubyHeight = std::max(1, static_cast<int>(std::ceil(rubyMetrics.height())));
    const int imageWidth = std::max(1, padLeft + rubyWidth + padRight);
    const int imageHeight = std::max(1, padTop + rubyHeight + padBottom);

    QImage image(imageWidth, imageHeight, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);

    const double localBaseline = padTop + rubyMetrics.ascent();
    const QPainterPath localPath = rubyTextPath(
        ruby.reading,
        rubyFont,
        rubyMetrics,
        padLeft,
        localBaseline,
        ruby.targetWidth
    );
    const QRectF localRect(
        padLeft,
        localBaseline - rubyMetrics.ascent(),
        ruby.readingWidth,
        rubyMetrics.height()
    );

    QPainter layerPainter(&image);
    layerPainter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing);
    paintTextLayerStackWithWidths(
        layerPainter,
        localPath,
        localRect,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        glowRadiusValue
    );
    layerPainter.end();

    return RubyLayerImage{
        image,
        QPointF(-padLeft, -(padTop + rubyMetrics.ascent())),
    };
}

void paintRubyDiagnostics(
    QPainter &painter,
    const ResolvedStyle &style,
    const std::vector<RubyDiagnostics> &rubies,
    const PaintFillSpec &base,
    const PaintFillSpec &fill,
    const PaintFillSpec &beforeStroke,
    const PaintFillSpec &afterStroke,
    const PaintFillSpec &beforeStroke2,
    const PaintFillSpec &afterStroke2,
    const PaintFillSpec &beforeShadow,
    const PaintFillSpec &afterShadow
) {
    if (rubies.empty()) {
        return;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const double scale = rubyScale(style);
    const int strokeWidth = scaledPx(style.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(style.stroke2WidthPx, scale);
    const int shadowOffsetX = scaledSignedPx(style.shadowOffsetX, scale);
    const int shadowOffsetY = scaledSignedPx(style.shadowOffsetY, scale);
    for (const RubyDiagnostics &ruby : rubies) {
        const RubyLayerImage beforeLayer = buildRubyTextLayer(
            ruby,
            rubyFont,
            rubyMetrics,
            base,
            beforeStroke,
            beforeStroke2,
            beforeShadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            scaledPx(glowRadius(style, false), scale)
        );
        painter.drawImage(QPointF(ruby.x, ruby.baselineY) + beforeLayer.offset, beforeLayer.image);
        if (ruby.progress <= 0.0) {
            continue;
        }
        const RubyLayerImage afterLayer = buildRubyTextLayer(
            ruby,
            rubyFont,
            rubyMetrics,
            fill,
            afterStroke,
            afterStroke2,
            afterShadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            scaledPx(glowRadius(style, true), scale)
        );
        painter.save();
        painter.setClipRect(
            QRectF(
                ruby.afterClipLeft,
                ruby.afterClipTop,
                ruby.afterClipRight - ruby.afterClipLeft,
                ruby.afterClipHeight
            ),
            Qt::IntersectClip
        );
        painter.drawImage(QPointF(ruby.x, ruby.baselineY) + afterLayer.offset, afterLayer.image);
        painter.restore();
    }
}

void paintInlineTextLayerStack(
    QPainter &painter,
    const TimingLine &line,
    const LineLayout &layout,
    bool after
) {
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        if (i >= layout.charLefts.size() || i >= layout.charWidths.size() || i >= layout.charFonts.size() || i >= layout.charStyles.size()) {
            continue;
        }
        const ResolvedStyle &style = *layout.charStyles[i];
        const QFontMetricsF metrics(layout.charFonts[i]);
        QPainterPath path;
        path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        const QRectF rect(
            layout.charLefts[i],
            layout.baselineY - metrics.ascent(),
            layout.charWidths[i],
            metrics.height()
        );
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            after ? style.afterFill : style.baseFill,
            after ? style.afterStrokeFill : style.beforeStrokeFill,
            after ? style.afterStroke2Fill : style.beforeStroke2Fill,
            after ? style.afterShadowFill : style.beforeShadowFill,
            style,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glowRadius(style, after)
        );
    }
}

double characterFillRatio(
    const std::vector<std::pair<int, int>> &intervals,
    std::size_t index,
    int tMs
) {
    if (index >= intervals.size()) {
        return 0.0;
    }
    const auto interval = intervals[index];
    if (tMs < interval.first) {
        return 0.0;
    }
    if (tMs >= interval.second) {
        return 1.0;
    }
    return progressRatio(interval.first, interval.second, tMs);
}

void paintTransformedTextStackWithFills(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &baseFill,
    const PaintFillSpec &afterFill,
    const PaintFillSpec &beforeStrokeFill,
    const PaintFillSpec &afterStrokeFill,
    const PaintFillSpec &beforeStroke2Fill,
    const PaintFillSpec &afterStroke2Fill,
    const PaintFillSpec &beforeShadowFill,
    const PaintFillSpec &afterShadowFill,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    int charX,
    int charWidth,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int beforeGlowRadius,
    int afterGlowRadius,
    bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("transformed_text")
) {
    const bool useCachedGlow = style.decorationKind == QStringLiteral("glow")
        && uprightPath != nullptr
        && uprightRect != nullptr
        && uprightTransform != nullptr
        && glowBitmapCacheEnabled();
    auto blitGlow = [&](const PaintFillSpec &shadowFill, int radius) {
        if (!useCachedGlow) {
            return;
        }
        blitTransformedGlowLayerWithWidths(
            painter,
            *uprightPath,
            shadowFill,
            *uprightRect,
            radius,
            strokeWidth,
            stroke2Width,
            *uprightTransform,
            glowScope
        );
    };

    const double clampedRatio = forceAfter ? 1.0 : std::clamp(ratio, 0.0, 1.0);
    if (clampedRatio <= 0.0) {
        blitGlow(beforeShadowFill, beforeGlowRadius);
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            baseFill,
            beforeStrokeFill,
            beforeStroke2Fill,
            beforeShadowFill,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            beforeGlowRadius,
            !useCachedGlow,
            glowScope + QStringLiteral(":before")
        );
        return;
    }
    if (clampedRatio >= 1.0) {
        blitGlow(afterShadowFill, afterGlowRadius);
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            afterFill,
            afterStrokeFill,
            afterStroke2Fill,
            afterShadowFill,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            afterGlowRadius,
            !useCachedGlow,
            glowScope + QStringLiteral(":after")
        );
        return;
    }

    blitGlow(beforeShadowFill, beforeGlowRadius);
    paintTextLayerStackWithWidths(
        painter,
        path,
        rect,
        baseFill,
        beforeStrokeFill,
        beforeStroke2Fill,
        beforeShadowFill,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        beforeGlowRadius,
        !useCachedGlow,
        glowScope + QStringLiteral(":before")
    );

    const double strokePad = visualStrokeExtentForWidths(strokeWidth, stroke2Width);
    const double clipX = rtl
        ? charX + charWidth * (1.0 - clampedRatio)
        : charX;
    const double clipWidth = std::max(charWidth * clampedRatio + strokePad, 1.0);
    painter.save();
    painter.setClipRect(
        QRectF(
            clipX - strokePad,
            rect.top() - strokePad,
            clipWidth,
            rect.height() + strokePad * 2.0
        ),
        Qt::IntersectClip
    );
    blitGlow(afterShadowFill, afterGlowRadius);
    paintTextLayerStackWithWidths(
        painter,
        path,
        rect,
        afterFill,
        afterStrokeFill,
        afterStroke2Fill,
        afterShadowFill,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        afterGlowRadius,
        !useCachedGlow,
        glowScope + QStringLiteral(":after")
    );
    painter.restore();
}

void paintTransformedTextStack(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    int charX,
    int charWidth,
    bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("main_transformed")
) {
    paintTransformedTextStackWithFills(
        painter,
        path,
        rect,
        style.baseFill,
        style.afterFill,
        style.beforeStrokeFill,
        style.afterStrokeFill,
        style.beforeStroke2Fill,
        style.afterStroke2Fill,
        style.beforeShadowFill,
        style.afterShadowFill,
        style,
        ratio,
        rtl,
        charX,
        charWidth,
        style.strokeWidthPx,
        style.stroke2WidthPx,
        style.shadowOffsetX,
        style.shadowOffsetY,
        glowRadius(style, false),
        glowRadius(style, true),
        forceAfter,
        uprightPath,
        uprightRect,
        uprightTransform,
        glowScope
    );
}

void paintRubyTransformedStack(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("ruby_transformed")
) {
    const double scale = rubyScale(style);
    const int strokeWidth = scaledPx(style.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(style.stroke2WidthPx, scale);
    const int shadowOffsetX = scaledSignedPx(style.shadowOffsetX, scale);
    const int shadowOffsetY = scaledSignedPx(style.shadowOffsetY, scale);
    paintTransformedTextStackWithFills(
        painter,
        path,
        rect,
        style.rubyBaseFill,
        style.rubyAfterFill,
        style.rubyBeforeStrokeFill,
        style.rubyAfterStrokeFill,
        style.rubyBeforeStroke2Fill,
        style.rubyAfterStroke2Fill,
        style.rubyBeforeShadowFill,
        style.rubyAfterShadowFill,
        style,
        ratio,
        rtl,
        static_cast<int>(std::round(rect.left())),
        std::max(1, static_cast<int>(std::round(rect.width()))),
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        scaledPx(glowRadius(style, false), scale),
        scaledPx(glowRadius(style, true), scale),
        forceAfter,
        uprightPath,
        uprightRect,
        uprightTransform,
        glowScope
    );
}

void paintRubyUtopiaText(
    QPainter &painter,
    const RenderConfig &cfg,
    const ResolvedStyle &style,
    const TimingLine &line,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals,
    const LineCharTransition &transition,
    int tMs
) {
    if (cfg.rubies.empty()) {
        return;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const double rubyBaselineY = layout.baselineY - layout.ascent - style.rubyGapPx;
    const int count = std::max(static_cast<int>(line.chars.size()), 1);

    for (const RubyAnnotation &ruby : cfg.rubies) {
        const auto indices = rubyTargetIndices(ruby, line, intervals);
        if (indices.empty()) {
            continue;
        }
        const auto targetRange = rubyTargetXRange(ruby, line, layout, intervals);
        if (!targetRange.has_value()) {
            continue;
        }
        const RubyAnnotation paintRuby = effectiveRubyForTarget(ruby, indices, intervals);
        const int firstIndex = *std::min_element(indices.begin(), indices.end());
        const int lastIndex = *std::max_element(indices.begin(), indices.end());
        const int followingDoneMs = utopiaFollowingDoneTime(line, intervals, lastIndex, cfg);
        const AnimationState state = transitionCharState(
            cfg,
            transition,
            intervals,
            firstIndex,
            count,
            tMs,
            cfg.height,
            followingDoneMs
        );
        if (state.opacity <= 0.0) {
            continue;
        }

        const double x = targetRange->first;
        const double targetWidth = std::max(targetRange->second - targetRange->first, 1.0);
        const double readingWidth = rubyLayoutWidth(paintRuby.reading, rubyMetrics, targetWidth);
        const bool groupExiting = indices.size() > 1 && tMs > followingDoneMs;
        painter.save();
        painter.setOpacity(painter.opacity() * state.opacity);

        if (groupExiting) {
            QString reading = paintRuby.reading;
            if (cfg.rightToLeft) {
                const auto visual = rubyUtopiaVisualUnits(reading);
                reading.clear();
                for (auto it = visual.rbegin(); it != visual.rend(); ++it) {
                    reading += *it;
                }
            }
            QPainterPath uprightPath = rubyTextPath(reading, rubyFont, rubyMetrics, x, rubyBaselineY, targetWidth);
            const QRectF sourceRect(
                x,
                rubyBaselineY - rubyMetrics.ascent(),
                readingWidth,
                rubyMetrics.height()
            );
            const double centerX = x + readingWidth / 2.0;
            const double centerY = rubyBaselineY - rubyMetrics.ascent() + rubyMetrics.height() / 2.0;
            const QTransform transform = characterTransform(
                centerX,
                centerY,
                state,
                QPointF(x, rubyBaselineY)
            );
            QPainterPath path = transform.map(uprightPath);
            const QRectF rect = path.boundingRect();
            if (!rect.isEmpty()) {
                paintRubyTransformedStack(
                    painter,
                    path,
                    rect,
                    style,
                    rubyProgressRatio(paintRuby, tMs),
                    cfg.rightToLeft,
                    true,
                    &uprightPath,
                    &sourceRect,
                    &transform,
                    QStringLiteral("ruby_utopia_group")
                );
            }
            painter.restore();
            continue;
        }

        auto unitsAndIntervals = rubyUtopiaReadingUnitsAndIntervals(paintRuby);
        if (cfg.rightToLeft) {
            std::reverse(unitsAndIntervals.begin(), unitsAndIntervals.end());
        }
        const auto layouts = rubyUnitLayouts(unitsAndIntervals, rubyMetrics, x, targetWidth);
        for (const RubyUnitLayout &unit : layouts) {
            const AnimationState unitState = transitionCharState(
                cfg,
                transition,
                intervals,
                firstIndex,
                count,
                tMs,
                cfg.height,
                followingDoneMs,
                unit.interval
            );
            if (unitState.opacity <= 0.0) {
                continue;
            }
            QPainterPath uprightPath;
            uprightPath.addText(QPointF(unit.x, rubyBaselineY), rubyFont, unit.text);
            const QRectF sourceRect(
                unit.x,
                rubyBaselineY - rubyMetrics.ascent(),
                unit.width,
                rubyMetrics.height()
            );
            const double centerX = unit.x + unit.width / 2.0;
            const double centerY = rubyBaselineY - rubyMetrics.ascent() + rubyMetrics.height() / 2.0;
            const QTransform transform = characterTransform(
                centerX,
                centerY,
                unitState,
                QPointF(unit.x, rubyBaselineY)
            );
            QPainterPath path = transform.map(uprightPath);
            const QRectF rect = path.boundingRect();
            if (rect.isEmpty()) {
                continue;
            }
            painter.save();
            painter.setOpacity(painter.opacity() * unitState.opacity);
            paintRubyTransformedStack(
                painter,
                path,
                rect,
                style,
                progressRatio(unit.interval.first, unit.interval.second, tMs),
                cfg.rightToLeft,
                false,
                &uprightPath,
                &sourceRect,
                &transform,
                QStringLiteral("ruby_utopia_reading")
            );
            painter.restore();
        }
        painter.restore();
    }
}

void paintUtopiaMainText(
    QPainter &painter,
    const RenderConfig &cfg,
    const TimingLine &line,
    const ResolvedStyle &style,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals,
    const LineCharTransition &transition,
    int tMs
) {
    const QFontMetricsF metrics(layout.font);
    const int count = std::max(static_cast<int>(line.chars.size()), 1);
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        if (i >= layout.charLefts.size() || i >= layout.charWidths.size()) {
            continue;
        }

        std::vector<int> indices{static_cast<int>(i)};
        std::optional<RubyAnnotation> groupRuby;
        const auto group = rubyGroupForCharIndex(cfg, line, intervals, static_cast<int>(i));
        bool groupExiting = false;
        if (group.has_value()) {
            const int groupDoneMs = utopiaFollowingDoneTime(line, intervals, group->indices.back(), cfg);
            groupExiting = tMs > groupDoneMs;
            if (groupExiting && static_cast<int>(i) != group->indices.front()) {
                continue;
            }
            if (groupExiting) {
                indices = group->indices;
                groupRuby = group->ruby;
            }
        }

        const int firstIndex = indices.front();
        const int lastIndex = indices.back();
        const int followingDoneMs = utopiaFollowingDoneTime(line, intervals, lastIndex, cfg);
        const AnimationState state = transitionCharState(
            cfg,
            transition,
            intervals,
            firstIndex,
            count,
            tMs,
            cfg.height,
            followingDoneMs
        );
        if (state.opacity <= 0.0) {
            continue;
        }

        QPainterPath path;
        double left = layout.charLefts[static_cast<std::size_t>(firstIndex)];
        double right = left + layout.charWidths[static_cast<std::size_t>(firstIndex)];
        for (int index : indices) {
            if (index < 0 || static_cast<std::size_t>(index) >= line.chars.size()) {
                continue;
            }
            const std::size_t pos = static_cast<std::size_t>(index);
            if (pos >= layout.charLefts.size() || pos >= layout.charWidths.size()) {
                continue;
            }
            path.addText(QPointF(layout.charLefts[pos], layout.baselineY), layout.font, line.chars[pos].text);
            left = std::min(left, layout.charLefts[pos]);
            right = std::max(right, layout.charLefts[pos] + layout.charWidths[pos]);
        }
        const double width = std::max(right - left, 1.0);
        const QRectF sourceRect(left, layout.baselineY - metrics.ascent(), width, metrics.height());
        const double centerX = left + width / 2.0;
        const double centerY = layout.baselineY - metrics.ascent() + metrics.height() / 2.0;
        const QTransform transform = characterTransform(
            centerX,
            centerY,
            state,
            QPointF(left, layout.baselineY)
        );
        const QPainterPath paintPath = transform.map(path);
        const QRectF paintRect = paintPath.boundingRect();
        if (paintRect.isEmpty()) {
            continue;
        }
        const int paintLeft = static_cast<int>(std::round(paintRect.left()));
        const int paintWidth = std::max(1, static_cast<int>(std::round(paintRect.width())));
        const bool inUtopiaExit = cfg.exitAnim == QStringLiteral("utopia") && tMs > followingDoneMs;
        const double ratio = groupRuby.has_value()
            ? rubyProgressRatio(groupRuby.value(), tMs)
            : characterFillRatio(intervals, i, tMs);

        painter.save();
        painter.setOpacity(painter.opacity() * state.opacity);
        paintTransformedTextStack(
            painter,
            paintPath,
            paintRect,
            style,
            ratio,
            cfg.rightToLeft,
            paintLeft,
            paintWidth,
            inUtopiaExit,
            &path,
            &sourceRect,
            &transform,
            groupRuby.has_value() ? QStringLiteral("main_utopia_ruby_group") : QStringLiteral("main_utopia_char")
        );
        painter.restore();
    }
}

void paintLine(QPainter &painter, const RenderConfig &cfg, const TimingLine &line, int tMs, int lane, int visibleLineCount, RenderDiagnostics *diagnostics) {
    const QString text = lineText(line);
    if (text.isEmpty()) {
        return;
    }

    const ResolvedStyle &lineStyle = resolvedStyleForLine(cfg, line);

    const LineLayout layout = layoutLine(cfg, lineStyle, line, lane, visibleLineCount);

    const QRectF lineRect(layout.x, layout.baselineY - layout.ascent, layout.width, layout.height);
    const auto intervals = lineIntervals(line);
    const auto transition = lineCharTransitionContext(cfg, line, tMs, intervals);
    const auto rubyDiagnostics = rubyDiagnosticsForLine(cfg, lineStyle, line, layout, tMs);
    const bool useUtopiaMainText = transition.has_value()
        && transition->effect == QStringLiteral("utopia")
        && !layout.hasInlineStyles;

    if (useUtopiaMainText) {
        paintRubyUtopiaText(
            painter,
            cfg,
            lineStyle,
            line,
            layout,
            intervals,
            transition.value(),
            tMs
        );
    } else {
        paintRubyDiagnostics(
            painter,
            lineStyle,
            rubyDiagnostics,
            lineStyle.rubyBaseFill,
            lineStyle.rubyAfterFill,
            lineStyle.rubyBeforeStrokeFill,
            lineStyle.rubyAfterStrokeFill,
            lineStyle.rubyBeforeStroke2Fill,
            lineStyle.rubyAfterStroke2Fill,
            lineStyle.rubyBeforeShadowFill,
            lineStyle.rubyAfterShadowFill
        );
    }

    if (useUtopiaMainText) {
        paintUtopiaMainText(
            painter,
            cfg,
            line,
            lineStyle,
            layout,
            intervals,
            transition.value(),
            tMs
        );
    } else if (layout.hasInlineStyles) {
        paintInlineTextLayerStack(painter, line, layout, false);
    } else {
        paintTextLayerStackWithWidths(
            painter,
            layout.path,
            lineRect,
            lineStyle.baseFill,
            lineStyle.beforeStrokeFill,
            lineStyle.beforeStroke2Fill,
            lineStyle.beforeShadowFill,
            lineStyle,
            lineStyle.strokeWidthPx,
            lineStyle.stroke2WidthPx,
            lineStyle.shadowOffsetX,
            lineStyle.shadowOffsetY,
            glowRadius(lineStyle, false)
        );
    }

    const auto clip = afterClipRect(cfg, lineStyle, line, layout, tMs);
    if (!useUtopiaMainText && clip.has_value() && clip->width() > 0.0) {
        painter.save();
        painter.setClipRect(*clip, Qt::IntersectClip);
        if (layout.hasInlineStyles) {
            paintInlineTextLayerStack(painter, line, layout, true);
        } else {
            paintTextLayerStackWithWidths(
                painter,
                layout.path,
                lineRect,
                lineStyle.afterFill,
                lineStyle.afterStrokeFill,
                lineStyle.afterStroke2Fill,
                lineStyle.afterShadowFill,
                lineStyle,
                lineStyle.strokeWidthPx,
                lineStyle.stroke2WidthPx,
                lineStyle.shadowOffsetX,
                lineStyle.shadowOffsetY,
                glowRadius(lineStyle, true)
            );
        }
        painter.restore();
    }

    if (diagnostics != nullptr) {
        LineDiagnostics lineDiagnostics;
        lineDiagnostics.lane = lane;
        lineDiagnostics.lineX = layout.x;
        lineDiagnostics.lineWidth = layout.width;
        lineDiagnostics.baselineY = layout.baselineY;
        if (clip.has_value()) {
            lineDiagnostics.afterClipLeft = clip->left();
            lineDiagnostics.afterClipRight = clip->right();
            lineDiagnostics.afterClipTop = clip->top();
            lineDiagnostics.afterClipHeight = clip->height();
        } else {
            lineDiagnostics.afterClipLeft = layout.x;
            lineDiagnostics.afterClipRight = layout.x;
            const double verticalExtent = layout.afterClipExtent > 0.0 ? layout.afterClipExtent : afterClipVerticalExtent(lineStyle);
            lineDiagnostics.afterClipTop = layout.baselineY - layout.ascent - verticalExtent;
            lineDiagnostics.afterClipHeight = layout.height + verticalExtent * 2.0;
        }
        diagnostics->lines.push_back(lineDiagnostics);
        if (!diagnostics->hasFirstLine) {
            diagnostics->hasFirstLine = true;
            diagnostics->lineX = lineDiagnostics.lineX;
            diagnostics->lineWidth = lineDiagnostics.lineWidth;
            diagnostics->baselineY = lineDiagnostics.baselineY;
            diagnostics->afterClipLeft = lineDiagnostics.afterClipLeft;
            diagnostics->afterClipRight = lineDiagnostics.afterClipRight;
            diagnostics->afterClipTop = lineDiagnostics.afterClipTop;
            diagnostics->afterClipHeight = lineDiagnostics.afterClipHeight;
        }
        diagnostics->rubies.insert(
            diagnostics->rubies.end(),
            rubyDiagnostics.begin(),
            rubyDiagnostics.end()
        );
    }
}

RenderResult renderFrame(const RenderConfig &cfg, int tMs) {
    RenderResult result{
        QImage(cfg.width, cfg.height, QImage::Format_ARGB32_Premultiplied),
        RenderDiagnostics{},
    };
    result.image.fill(Qt::transparent);

    QPainter painter(&result.image);
    painter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing | QPainter::SmoothPixmapTransform);

    std::vector<const TimingLine *> visibleLines;
    visibleLines.reserve(cfg.lines.size());
    for (const auto &line : cfg.lines) {
        if (lineVisible(line, tMs, cfg)) {
            visibleLines.push_back(&line);
        }
    }
    result.diagnostics.visibleLines = static_cast<int>(visibleLines.size());
    if (cfg.dualLineLayout && visibleLines.size() > 2) {
        visibleLines.resize(2);
    }

    int lane = 0;
    for (const TimingLine *line : visibleLines) {
        paintLine(painter, cfg, *line, tMs, lane, result.diagnostics.visibleLines, &result.diagnostics);
        lane = std::min(lane + 1, 2);
    }

    painter.end();
    return result;
}

QJsonObject handleConfigure(const QJsonObject &request, std::optional<RenderConfig> *config) {
    QString error;
    auto parsed = parseConfig(request.value(QStringLiteral("ir")).toObject(), &error);
    if (!parsed.has_value()) {
        QJsonObject out = response(false, QStringLiteral("configure"));
        out.insert(QStringLiteral("error"), error);
        return out;
    }
    *config = parsed;
    clearGlowBitmapCache();
    QJsonObject out = response(true, QStringLiteral("configured"));
    out.insert(QStringLiteral("width"), parsed->width);
    out.insert(QStringLiteral("height"), parsed->height);
    out.insert(QStringLiteral("fps"), parsed->fps);
    out.insert(QStringLiteral("line_count"), static_cast<int>(parsed->lines.size()));
    out.insert(QStringLiteral("ruby_count"), static_cast<int>(parsed->rubies.size()));
    return out;
}

void appendFrameDiagnostics(
    QJsonObject *out,
    int tMs,
    const QImage &image,
    const RenderDiagnostics &diagnostics,
    double renderMs
) {
    out->insert(QStringLiteral("t_ms"), tMs);
    out->insert(QStringLiteral("width"), image.width());
    out->insert(QStringLiteral("height"), image.height());
    out->insert(QStringLiteral("checksum"), QString::number(imageChecksum(image)));
    out->insert(QStringLiteral("render_ms"), renderMs);
    out->insert(QStringLiteral("visible_lines"), diagnostics.visibleLines);
    out->insert(QStringLiteral("glow_cache_hits"), glowBitmapCacheStats().hits);
    out->insert(QStringLiteral("glow_cache_misses"), glowBitmapCacheStats().misses);
    out->insert(QStringLiteral("glow_cache_shape_misses"), glowBitmapCacheStats().shapeMisses);
    out->insert(QStringLiteral("glow_cache_content_variant_misses"), glowBitmapCacheStats().contentVariantMisses);
    out->insert(QStringLiteral("glow_cache_evicted_key_misses"), glowBitmapCacheStats().evictedKeyMisses);
    out->insert(QStringLiteral("glow_cache_size"), static_cast<int>(glowBitmapCache().size()));
    QJsonObject missesByScope;
    const auto scopeKeys = glowBitmapCacheStats().missesByScope.keys();
    for (const QString &scope : scopeKeys) {
        missesByScope.insert(scope, glowBitmapCacheStats().missesByScope.value(scope));
    }
    out->insert(QStringLiteral("glow_cache_misses_by_scope"), missesByScope);
    QJsonArray recentGlowMisses;
    const auto &misses = glowBitmapCacheStats().recentMisses;
    const std::size_t start = misses.size() > 8 ? misses.size() - 8 : 0;
    for (std::size_t index = start; index < misses.size(); ++index) {
        const GlowBitmapCacheMissDiagnostic &miss = misses[index];
        QJsonObject item;
        item.insert(QStringLiteral("scope"), miss.scope);
        item.insert(QStringLiteral("category"), miss.category);
        item.insert(QStringLiteral("radius"), miss.radius);
        item.insert(QStringLiteral("width"), miss.width);
        item.insert(QStringLiteral("height"), miss.height);
        item.insert(QStringLiteral("format"), miss.format);
        item.insert(QStringLiteral("checksum"), miss.checksum);
        recentGlowMisses.append(item);
    }
    out->insert(QStringLiteral("glow_cache_recent_misses"), recentGlowMisses);
    QJsonArray lineDiagnostics;
    for (const LineDiagnostics &line : diagnostics.lines) {
        QJsonObject item;
        item.insert(QStringLiteral("lane"), line.lane);
        item.insert(QStringLiteral("line_x"), line.lineX);
        item.insert(QStringLiteral("line_width"), line.lineWidth);
        item.insert(QStringLiteral("baseline_y"), line.baselineY);
        item.insert(QStringLiteral("after_clip_left"), line.afterClipLeft);
        item.insert(QStringLiteral("after_clip_right"), line.afterClipRight);
        item.insert(QStringLiteral("after_clip_top"), line.afterClipTop);
        item.insert(QStringLiteral("after_clip_height"), line.afterClipHeight);
        lineDiagnostics.append(item);
    }
    out->insert(QStringLiteral("line_diagnostics"), lineDiagnostics);
    QJsonArray rubyDiagnostics;
    for (const RubyDiagnostics &ruby : diagnostics.rubies) {
        QJsonObject item;
        item.insert(QStringLiteral("kanji"), ruby.kanji);
        item.insert(QStringLiteral("reading"), ruby.reading);
        QJsonArray indices;
        for (int index : ruby.indices) {
            indices.append(index);
        }
        item.insert(QStringLiteral("indices"), indices);
        item.insert(QStringLiteral("x"), ruby.x);
        item.insert(QStringLiteral("baseline_y"), ruby.baselineY);
        item.insert(QStringLiteral("target_width"), ruby.targetWidth);
        item.insert(QStringLiteral("reading_width"), ruby.readingWidth);
        item.insert(QStringLiteral("progress"), ruby.progress);
        item.insert(QStringLiteral("after_clip_left"), ruby.afterClipLeft);
        item.insert(QStringLiteral("after_clip_right"), ruby.afterClipRight);
        item.insert(QStringLiteral("after_clip_top"), ruby.afterClipTop);
        item.insert(QStringLiteral("after_clip_height"), ruby.afterClipHeight);
        rubyDiagnostics.append(item);
    }
    out->insert(QStringLiteral("ruby_diagnostics"), rubyDiagnostics);
    if (diagnostics.hasFirstLine) {
        out->insert(QStringLiteral("line_x"), diagnostics.lineX);
        out->insert(QStringLiteral("line_width"), diagnostics.lineWidth);
        out->insert(QStringLiteral("baseline_y"), diagnostics.baselineY);
        out->insert(QStringLiteral("after_clip_left"), diagnostics.afterClipLeft);
        out->insert(QStringLiteral("after_clip_right"), diagnostics.afterClipRight);
        out->insert(QStringLiteral("after_clip_top"), diagnostics.afterClipTop);
        out->insert(QStringLiteral("after_clip_height"), diagnostics.afterClipHeight);
    }
}

QJsonObject handleRenderFrame(const QJsonObject &request, const std::optional<RenderConfig> &config) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
    const QString outputPath = stringValue(request, QStringLiteral("output_path"));
    if (outputPath.isEmpty()) {
        QJsonObject out = response(false, QStringLiteral("render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("output_path is required for native smoke render"));
        return out;
    }

    QElapsedTimer timer;
    timer.start();
    RenderResult rendered = renderFrame(*config, tMs);
    const double renderMs = static_cast<double>(timer.nsecsElapsed()) / 1000000.0;
    QImage &image = rendered.image;
    const bool saved = image.save(outputPath);
    QJsonObject out = response(saved, QStringLiteral("frame_ready"));
    out.insert(QStringLiteral("output_path"), outputPath);
    appendFrameDiagnostics(&out, tMs, image, rendered.diagnostics, renderMs);
    if (!saved) {
        out.insert(QStringLiteral("error"), QStringLiteral("failed to save output image"));
    }
    return out;
}

QJsonObject handleRenderFrameStats(const QJsonObject &request, const std::optional<RenderConfig> &config) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_frame_stats"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
    QElapsedTimer timer;
    timer.start();
    RenderResult rendered = renderFrame(*config, tMs);
    const double renderMs = static_cast<double>(timer.nsecsElapsed()) / 1000000.0;
    QJsonObject out = response(true, QStringLiteral("frame_stats"));
    appendFrameDiagnostics(&out, tMs, rendered.image, rendered.diagnostics, renderMs);
    return out;
}

std::vector<int> rangeTimestampsFromRequest(const QJsonObject &request, const RenderConfig &config) {
    std::vector<int> timestamps = parseIntArray(request.value(QStringLiteral("t_ms")).toArray());
    if (timestamps.empty()) {
        const int startFrame = std::max(0, intValue(request, QStringLiteral("start_frame"), 0));
        const int count = std::max(0, intValue(request, QStringLiteral("count"), 0));
        timestamps.reserve(static_cast<std::size_t>(count));
        for (int index = 0; index < count; ++index) {
            const double frameMs = 1000.0 / static_cast<double>(std::max(config.fps, 1));
            timestamps.push_back(static_cast<int>(std::round((startFrame + index) * frameMs)));
        }
    }
    return timestamps;
}

int rangeWorkerCountFromRequest(const QJsonObject &request, const RenderConfig &config, int frameCount) {
    const unsigned int hardwareThreads = std::max(1u, std::thread::hardware_concurrency());
    const int requestedThreads = intValue(request, QStringLiteral("threads"), static_cast<int>(hardwareThreads));
    return std::max(1, std::min(requestedThreads, std::max(frameCount, 1)));
}

QJsonObject handleRenderRangeStats(const QJsonObject &request, const std::optional<RenderConfig> &config) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_range_stats"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    std::vector<int> timestamps = rangeTimestampsFromRequest(request, *config);
    if (timestamps.empty()) {
        QJsonObject out = response(false, QStringLiteral("render_range_stats"));
        out.insert(QStringLiteral("error"), QStringLiteral("t_ms array or positive count is required"));
        return out;
    }

    const int workerCount = rangeWorkerCountFromRequest(request, *config, static_cast<int>(timestamps.size()));
    std::vector<RangeFrameResult> results(timestamps.size());
    std::atomic<int> nextIndex{0};
    QElapsedTimer totalTimer;
    totalTimer.start();

    auto worker = [&]() {
        while (true) {
            const int index = nextIndex.fetch_add(1);
            if (index >= static_cast<int>(timestamps.size())) {
                return;
            }
            QElapsedTimer frameTimer;
            frameTimer.start();
            RenderResult rendered = renderFrame(*config, timestamps[static_cast<std::size_t>(index)]);
            const double renderMs = static_cast<double>(frameTimer.nsecsElapsed()) / 1000000.0;
            results[static_cast<std::size_t>(index)] = RangeFrameResult{
                timestamps[static_cast<std::size_t>(index)],
                renderMs,
                QString::number(imageChecksum(rendered.image)),
                rendered.diagnostics.visibleLines,
            };
        }
    };

    std::vector<std::thread> workers;
    workers.reserve(static_cast<std::size_t>(workerCount));
    for (int index = 0; index < workerCount; ++index) {
        workers.emplace_back(worker);
    }
    for (auto &thread : workers) {
        thread.join();
    }

    const double elapsedMs = static_cast<double>(totalTimer.nsecsElapsed()) / 1000000.0;
    QJsonObject out = response(true, QStringLiteral("range_stats"));
    out.insert(QStringLiteral("frames"), static_cast<int>(timestamps.size()));
    out.insert(QStringLiteral("threads"), workerCount);
    out.insert(QStringLiteral("elapsed_ms"), elapsedMs);
    out.insert(QStringLiteral("fps"), elapsedMs > 0.0 ? (static_cast<double>(timestamps.size()) * 1000.0 / elapsedMs) : 0.0);
    out.insert(QStringLiteral("glow_cache_hits"), glowBitmapCacheStats().hits);
    out.insert(QStringLiteral("glow_cache_misses"), glowBitmapCacheStats().misses);
    out.insert(QStringLiteral("glow_cache_shape_misses"), glowBitmapCacheStats().shapeMisses);
    out.insert(QStringLiteral("glow_cache_content_variant_misses"), glowBitmapCacheStats().contentVariantMisses);
    out.insert(QStringLiteral("glow_cache_evicted_key_misses"), glowBitmapCacheStats().evictedKeyMisses);
    out.insert(QStringLiteral("glow_cache_size"), static_cast<int>(glowBitmapCache().size()));
    QJsonObject missesByScope;
    const auto scopeKeys = glowBitmapCacheStats().missesByScope.keys();
    for (const QString &scope : scopeKeys) {
        missesByScope.insert(scope, glowBitmapCacheStats().missesByScope.value(scope));
    }
    out.insert(QStringLiteral("glow_cache_misses_by_scope"), missesByScope);

    QJsonArray frames;
    for (const RangeFrameResult &result : results) {
        QJsonObject item;
        item.insert(QStringLiteral("t_ms"), result.tMs);
        item.insert(QStringLiteral("render_ms"), result.renderMs);
        item.insert(QStringLiteral("checksum"), result.checksum);
        item.insert(QStringLiteral("visible_lines"), result.visibleLines);
        frames.append(item);
    }
    out.insert(QStringLiteral("frame_stats"), frames);
    return out;
}

void launchRenderRangeJob(
    RenderRuntime *runtime,
    RenderConfig config,
    std::vector<int> timestamps,
    int generation,
    int workerCount
) {
    auto job = std::thread([runtime, config = std::move(config), timestamps = std::move(timestamps), generation, workerCount]() {
        std::vector<RangeFrameResult> results(timestamps.size());
        std::vector<bool> ready(timestamps.size(), false);
        std::mutex resultMutex;
        std::condition_variable resultReady;
        std::atomic<int> nextIndex{0};
        std::atomic<int> activeWorkers{workerCount};
        std::atomic<int> completedFrames{0};
        QElapsedTimer totalTimer;
        totalTimer.start();

        auto worker = [&]() {
            while (true) {
                if (generationCancelled(runtime, generation)) {
                    break;
                }
                const int index = nextIndex.fetch_add(1);
                if (index >= static_cast<int>(timestamps.size())) {
                    break;
                }
                QElapsedTimer frameTimer;
                frameTimer.start();
                RenderResult rendered = renderFrame(config, timestamps[static_cast<std::size_t>(index)]);
                const double renderMs = static_cast<double>(frameTimer.nsecsElapsed()) / 1000000.0;
                if (generationCancelled(runtime, generation)) {
                    break;
                }
                {
                    std::lock_guard<std::mutex> lock(resultMutex);
                    results[static_cast<std::size_t>(index)] = RangeFrameResult{
                        timestamps[static_cast<std::size_t>(index)],
                        renderMs,
                        QString::number(imageChecksum(rendered.image)),
                        rendered.diagnostics.visibleLines,
                        std::move(rendered.image),
                    };
                    ready[static_cast<std::size_t>(index)] = true;
                }
                ++completedFrames;
                resultReady.notify_all();
            }
            --activeWorkers;
            resultReady.notify_all();
        };

        std::vector<std::thread> workers;
        workers.reserve(static_cast<std::size_t>(workerCount));
        for (int index = 0; index < workerCount; ++index) {
            workers.emplace_back(worker);
        }

        int nextEmit = 0;
        while (nextEmit < static_cast<int>(timestamps.size())) {
            RangeFrameResult result;
            {
                std::unique_lock<std::mutex> lock(resultMutex);
                resultReady.wait(lock, [&]() {
                    return ready[static_cast<std::size_t>(nextEmit)]
                        || activeWorkers.load() == 0
                        || generationCancelled(runtime, generation);
                });
                if (!ready[static_cast<std::size_t>(nextEmit)]) {
                    break;
                }
                result = results[static_cast<std::size_t>(nextEmit)];
            }
            const int slotIndex = nextEmit % std::max(1, runtime->sharedRing.slotCount);
            SharedFrameRing ring;
            const bool wroteSlot = writeSharedFrameSlot(runtime, result, generation, nextEmit, slotIndex, &ring);
            QJsonObject frame = response(true, QStringLiteral("frame_ready"));
            frame.insert(QStringLiteral("generation"), generation);
            frame.insert(QStringLiteral("frame_index"), nextEmit);
            frame.insert(QStringLiteral("t_ms"), result.tMs);
            frame.insert(QStringLiteral("render_ms"), result.renderMs);
            frame.insert(QStringLiteral("checksum"), result.checksum);
            frame.insert(QStringLiteral("visible_lines"), result.visibleLines);
            frame.insert(QStringLiteral("payload"), wroteSlot ? QStringLiteral("shared_memory") : QStringLiteral("metadata"));
            if (wroteSlot) {
                frame.insert(QStringLiteral("shm_key"), ring.key);
                frame.insert(QStringLiteral("slot_index"), slotIndex);
                frame.insert(QStringLiteral("slot_count"), ring.slotCount);
                frame.insert(QStringLiteral("slot_offset"), slotIndex * ring.slotBytes);
                frame.insert(QStringLiteral("slot_bytes"), ring.slotBytes);
                frame.insert(QStringLiteral("header_bytes"), ring.headerBytes);
                frame.insert(QStringLiteral("payload_offset"), slotIndex * ring.slotBytes + ring.headerBytes);
                frame.insert(QStringLiteral("payload_bytes"), ring.pixelBytes);
                frame.insert(QStringLiteral("width"), ring.width);
                frame.insert(QStringLiteral("height"), ring.height);
                frame.insert(QStringLiteral("stride"), ring.stride);
                frame.insert(QStringLiteral("pixel_format"), ring.pixelFormat);
            }
            writeJson(frame);
            ++nextEmit;
        }

        for (auto &thread : workers) {
            if (thread.joinable()) {
                thread.join();
            }
        }

        const bool cancelled = generationCancelled(runtime, generation);
        QJsonObject done = response(true, QStringLiteral("range_done"));
        done.insert(QStringLiteral("generation"), generation);
        done.insert(QStringLiteral("frames"), static_cast<int>(timestamps.size()));
        done.insert(QStringLiteral("frames_done"), completedFrames.load());
        done.insert(QStringLiteral("frames_emitted"), nextEmit);
        done.insert(QStringLiteral("threads"), workerCount);
        done.insert(QStringLiteral("cancelled"), cancelled);
        done.insert(QStringLiteral("elapsed_ms"), static_cast<double>(totalTimer.nsecsElapsed()) / 1000000.0);
        writeJson(done);
    });
    rememberRenderJob(runtime, std::move(job));
}

QJsonObject handleRenderRange(const QJsonObject &request, const std::optional<RenderConfig> &config, RenderRuntime *runtime) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_range"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    std::vector<int> timestamps = rangeTimestampsFromRequest(request, *config);
    if (timestamps.empty()) {
        QJsonObject out = response(false, QStringLiteral("render_range"));
        out.insert(QStringLiteral("error"), QStringLiteral("t_ms array or positive count is required"));
        return out;
    }

    const int generation = intValue(request, QStringLiteral("generation"), 0);
    clearGenerationCancel(runtime, generation);
    const int workerCount = rangeWorkerCountFromRequest(request, *config, static_cast<int>(timestamps.size()));
    const QString shmKey = stringValue(
        request,
        QStringLiteral("shm_key"),
        defaultSharedMemoryKey(generation)
    );
    const int ringSlots = std::max(1, intValue(request, QStringLiteral("ring_slots"), 3));
    QString shmError;
    if (!ensureSharedFrameRing(runtime, shmKey, ringSlots, config->width, config->height, &shmError)) {
        QJsonObject out = response(false, QStringLiteral("render_range"));
        out.insert(QStringLiteral("generation"), generation);
        out.insert(QStringLiteral("error"), QStringLiteral("failed to create shared memory: ") + shmError);
        return out;
    }
    QJsonObject out = response(true, QStringLiteral("range_started"));
    out.insert(QStringLiteral("generation"), generation);
    out.insert(QStringLiteral("frames"), static_cast<int>(timestamps.size()));
    out.insert(QStringLiteral("threads"), workerCount);
    out.insert(QStringLiteral("shm_key"), shmKey);
    out.insert(QStringLiteral("ring_slots"), ringSlots);
    out.insert(QStringLiteral("width"), config->width);
    out.insert(QStringLiteral("height"), config->height);
    launchRenderRangeJob(runtime, *config, std::move(timestamps), generation, workerCount);
    return out;
}

QJsonObject handleCancelGeneration(const QJsonObject &request, RenderRuntime *runtime) {
    const int generation = intValue(request, QStringLiteral("generation"), 0);
    cancelGeneration(runtime, generation);
    QJsonObject out = response(true, QStringLiteral("generation_cancelled"));
    out.insert(QStringLiteral("generation"), generation);
    return out;
}

QJsonObject parseErrorResponse(const QString &message) {
    QJsonObject out = response(false, QStringLiteral("parse_error"));
    out.insert(QStringLiteral("error"), message);
    return out;
}

}  // namespace

int main(int argc, char **argv) {
#if !defined(Q_OS_WIN)
    qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty() ? QByteArray("offscreen") : qgetenv("QT_QPA_PLATFORM"));
#endif
    QApplication app(argc, argv);

    QJsonObject ready = response(true, QStringLiteral("ready"));
    ready.insert(QStringLiteral("schema"), kProtocolSchema);
    ready.insert(QStringLiteral("qt"), QString::fromLatin1(qVersion()));
    writeJson(ready);

    std::optional<RenderConfig> config;
    RenderRuntime runtime;
    QTextStream input(stdin, QIODevice::ReadOnly);
    while (!input.atEnd()) {
        const QString line = input.readLine().trimmed();
        if (line.isEmpty()) {
            continue;
        }

        QJsonParseError parseError;
        const QJsonDocument doc = QJsonDocument::fromJson(line.toUtf8(), &parseError);
        if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
            writeJson(parseErrorResponse(parseError.errorString()));
            continue;
        }

        const QJsonObject request = doc.object();
        const QString command = stringValue(request, QStringLiteral("cmd"));
        if (command == QStringLiteral("configure")) {
            writeJson(handleConfigure(request, &config));
        } else if (command == QStringLiteral("render_frame")) {
            writeJson(handleRenderFrame(request, config));
        } else if (command == QStringLiteral("render_frame_stats")) {
            writeJson(handleRenderFrameStats(request, config));
        } else if (command == QStringLiteral("render_range_stats")) {
            writeJson(handleRenderRangeStats(request, config));
        } else if (command == QStringLiteral("render_range")) {
            writeJson(handleRenderRange(request, config, &runtime));
        } else if (command == QStringLiteral("cancel_generation")) {
            writeJson(handleCancelGeneration(request, &runtime));
        } else if (command == QStringLiteral("shutdown")) {
            runtime.shutdownRequested.store(true);
            joinRenderJobs(&runtime);
            writeJson(response(true, QStringLiteral("shutdown")));
            return 0;
        } else {
            QJsonObject out = response(false, QStringLiteral("unknown_command"));
            out.insert(QStringLiteral("error"), QStringLiteral("unknown command: ") + command);
            writeJson(out);
        }
    }

    runtime.shutdownRequested.store(true);
    joinRenderJobs(&runtime);
    return 0;
}
