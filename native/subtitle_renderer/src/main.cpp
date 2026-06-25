#include <QtCore/QByteArray>
#include <QtCore/QFile>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QPointF>
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
#include <QtWidgets/QApplication>
#include <QtWidgets/QGraphicsBlurEffect>
#include <QtWidgets/QGraphicsPixmapItem>
#include <QtWidgets/QGraphicsScene>

#include <algorithm>
#include <cstdint>
#include <cmath>
#include <iostream>
#include <limits>
#include <optional>
#include <vector>

namespace {

constexpr int kProtocolSchema = 1;

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
};

struct RenderConfig {
    int width = 1920;
    int height = 1080;
    int fps = 60;
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
    std::vector<TimingLine> lines;
    std::vector<RubyAnnotation> rubies;
};

struct LineLayout {
    QString text;
    QFont font;
    QPainterPath path;
    std::vector<double> charLefts;
    std::vector<double> charWidths;
    double x = 0.0;
    double baselineY = 0.0;
    double width = 0.0;
    double height = 0.0;
    double ascent = 0.0;
    double descent = 0.0;
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
        || mode == QStringLiteral("split_vertical");
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

QJsonObject response(bool ok, const QString &event) {
    QJsonObject out;
    out.insert(QStringLiteral("ok"), ok);
    out.insert(QStringLiteral("event"), event);
    return out;
}

void writeJson(const QJsonObject &object) {
    const QJsonDocument doc(object);
    std::cout << doc.toJson(QJsonDocument::Compact).constData() << std::endl;
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
    cfg.fontFamily = stringValue(style, QStringLiteral("font_family"), cfg.fontFamily);
    cfg.fontSizePx = std::max(1, intValue(style, QStringLiteral("font_size_px"), cfg.fontSizePx));
    cfg.fontWeight = std::clamp(intValue(style, QStringLiteral("font_weight"), cfg.fontWeight), 1, 999);
    cfg.letterSpacingPx = intValue(style, QStringLiteral("letter_spacing_px"), cfg.letterSpacingPx);
    cfg.baseColor = stringValue(style, QStringLiteral("base_color"), cfg.baseColor);
    cfg.fillColor = stringValue(style, QStringLiteral("fill_color"), cfg.fillColor);
    cfg.rubyColor = stringValue(style, QStringLiteral("ruby_color"), cfg.rubyColor);
    const QString strokeColor = stringValue(style, QStringLiteral("stroke_color"), cfg.beforeStrokeColor);
    cfg.beforeStrokeColor = strokeColor;
    cfg.afterStrokeColor = strokeColor;
    cfg.rubyBeforeStrokeColor = strokeColor;
    cfg.rubyAfterStrokeColor = strokeColor;
    const QString shadowColor = stringValue(style, QStringLiteral("shadow_color"), cfg.beforeShadowColor);
    cfg.beforeShadowColor = shadowColor;
    cfg.afterShadowColor = shadowColor;
    cfg.rubyBeforeShadowColor = shadowColor;
    cfg.rubyAfterShadowColor = shadowColor;
    cfg.baseFill = solidPaintFill(cfg.baseColor);
    cfg.afterFill = solidPaintFill(cfg.fillColor);
    cfg.beforeStrokeFill = solidPaintFill(cfg.beforeStrokeColor);
    cfg.afterStrokeFill = solidPaintFill(cfg.afterStrokeColor);
    cfg.beforeStroke2Fill = solidPaintFill(cfg.beforeStroke2Color);
    cfg.afterStroke2Fill = solidPaintFill(cfg.afterStroke2Color);
    cfg.beforeShadowFill = solidPaintFill(cfg.beforeShadowColor);
    cfg.afterShadowFill = solidPaintFill(cfg.afterShadowColor);
    cfg.rubyBaseFill = solidPaintFill(cfg.rubyBaseColor);
    cfg.rubyAfterFill = solidPaintFill(cfg.rubyFillColor);
    cfg.rubyBeforeStrokeFill = solidPaintFill(cfg.rubyBeforeStrokeColor);
    cfg.rubyAfterStrokeFill = solidPaintFill(cfg.rubyAfterStrokeColor);
    cfg.rubyBeforeStroke2Fill = solidPaintFill(cfg.rubyBeforeStroke2Color);
    cfg.rubyAfterStroke2Fill = solidPaintFill(cfg.rubyAfterStroke2Color);
    cfg.rubyBeforeShadowFill = solidPaintFill(cfg.rubyBeforeShadowColor);
    cfg.rubyAfterShadowFill = solidPaintFill(cfg.rubyAfterShadowColor);
    cfg.strokeWidthPx = std::max(0, intValue(style, QStringLiteral("stroke_width_px"), cfg.strokeWidthPx));
    cfg.stroke2WidthPx = std::max(0, intValue(style, QStringLiteral("stroke2_width_px"), cfg.stroke2WidthPx));
    cfg.decorationKind = stringValue(style, QStringLiteral("decoration_kind"), cfg.decorationKind);
    cfg.glowRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_radius_px"), cfg.glowRadiusPx));
    cfg.glowBeforeRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_before_radius_px"), cfg.glowBeforeRadiusPx));
    cfg.glowAfterRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_after_radius_px"), cfg.glowAfterRadiusPx));
    cfg.shadowOffsetX = intValue(style, QStringLiteral("shadow_offset_x"), cfg.shadowOffsetX);
    cfg.shadowOffsetY = intValue(style, QStringLiteral("shadow_offset_y"), cfg.shadowOffsetY);
    cfg.rubyFontSizePx = std::max(1, intValue(style, QStringLiteral("ruby_font_size_px"), cfg.rubyFontSizePx));
    cfg.rubyGapPx = std::max(0, intValue(style, QStringLiteral("ruby_gap_px"), cfg.rubyGapPx));
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
    const bool hasMainKaraokeColors = style.value(QStringLiteral("karaoke_colors")).isObject();
    const bool hasRubyKaraokeColors = style.value(QStringLiteral("ruby_karaoke_colors")).isObject();
    const QJsonObject mainKaraokeColors = style.value(QStringLiteral("karaoke_colors")).toObject();
    const QJsonObject rubyKaraokeColors = style.value(QStringLiteral("ruby_karaoke_colors")).toObject();

    cfg.baseColor = karaokeLayerColorFromColors(mainKaraokeColors, QStringLiteral("before"), QStringLiteral("text"), cfg.baseColor);
    cfg.fillColor = karaokeLayerColorFromColors(mainKaraokeColors, QStringLiteral("after"), QStringLiteral("text"), cfg.fillColor);
    cfg.beforeStrokeColor = karaokeLayerColorFromColors(mainKaraokeColors, QStringLiteral("before"), QStringLiteral("stroke"), cfg.beforeStrokeColor);
    cfg.afterStrokeColor = karaokeLayerColorFromColors(mainKaraokeColors, QStringLiteral("after"), QStringLiteral("stroke"), cfg.afterStrokeColor);
    cfg.beforeStroke2Color = karaokeLayerColorFromColors(mainKaraokeColors, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.beforeStroke2Color);
    cfg.afterStroke2Color = karaokeLayerColorFromColors(mainKaraokeColors, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.afterStroke2Color);
    cfg.beforeShadowColor = karaokeLayerColorFromColors(mainKaraokeColors, QStringLiteral("before"), QStringLiteral("shadow"), cfg.beforeShadowColor);
    cfg.afterShadowColor = karaokeLayerColorFromColors(mainKaraokeColors, QStringLiteral("after"), QStringLiteral("shadow"), cfg.afterShadowColor);
    cfg.baseFill = karaokeLayerFillFromColors(mainKaraokeColors, QStringLiteral("before"), QStringLiteral("text"), cfg.baseColor);
    cfg.afterFill = karaokeLayerFillFromColors(mainKaraokeColors, QStringLiteral("after"), QStringLiteral("text"), cfg.fillColor);
    cfg.beforeStrokeFill = karaokeLayerFillFromColors(mainKaraokeColors, QStringLiteral("before"), QStringLiteral("stroke"), cfg.beforeStrokeColor);
    cfg.afterStrokeFill = karaokeLayerFillFromColors(mainKaraokeColors, QStringLiteral("after"), QStringLiteral("stroke"), cfg.afterStrokeColor);
    cfg.beforeStroke2Fill = karaokeLayerFillFromColors(mainKaraokeColors, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.beforeStroke2Color);
    cfg.afterStroke2Fill = karaokeLayerFillFromColors(mainKaraokeColors, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.afterStroke2Color);
    cfg.beforeShadowFill = karaokeLayerFillFromColors(mainKaraokeColors, QStringLiteral("before"), QStringLiteral("shadow"), cfg.beforeShadowColor);
    cfg.afterShadowFill = karaokeLayerFillFromColors(mainKaraokeColors, QStringLiteral("after"), QStringLiteral("shadow"), cfg.afterShadowColor);

    if (hasRubyKaraokeColors) {
        cfg.rubyBaseColor = karaokeLayerColorFromColors(rubyKaraokeColors, QStringLiteral("before"), QStringLiteral("text"), cfg.baseColor);
        cfg.rubyFillColor = karaokeLayerColorFromColors(rubyKaraokeColors, QStringLiteral("after"), QStringLiteral("text"), cfg.rubyColor);
        cfg.rubyBeforeStrokeColor = karaokeLayerColorFromColors(rubyKaraokeColors, QStringLiteral("before"), QStringLiteral("stroke"), cfg.beforeStrokeColor);
        cfg.rubyAfterStrokeColor = karaokeLayerColorFromColors(rubyKaraokeColors, QStringLiteral("after"), QStringLiteral("stroke"), cfg.afterStrokeColor);
        cfg.rubyBeforeStroke2Color = karaokeLayerColorFromColors(rubyKaraokeColors, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.beforeStroke2Color);
        cfg.rubyAfterStroke2Color = karaokeLayerColorFromColors(rubyKaraokeColors, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.afterStroke2Color);
        cfg.rubyBeforeShadowColor = karaokeLayerColorFromColors(rubyKaraokeColors, QStringLiteral("before"), QStringLiteral("shadow"), cfg.beforeShadowColor);
        cfg.rubyAfterShadowColor = karaokeLayerColorFromColors(rubyKaraokeColors, QStringLiteral("after"), QStringLiteral("shadow"), cfg.afterShadowColor);
        cfg.rubyBaseFill = karaokeLayerFillFromColors(rubyKaraokeColors, QStringLiteral("before"), QStringLiteral("text"), cfg.rubyBaseColor);
        cfg.rubyAfterFill = karaokeLayerFillFromColors(rubyKaraokeColors, QStringLiteral("after"), QStringLiteral("text"), cfg.rubyFillColor);
        cfg.rubyBeforeStrokeFill = karaokeLayerFillFromColors(rubyKaraokeColors, QStringLiteral("before"), QStringLiteral("stroke"), cfg.rubyBeforeStrokeColor);
        cfg.rubyAfterStrokeFill = karaokeLayerFillFromColors(rubyKaraokeColors, QStringLiteral("after"), QStringLiteral("stroke"), cfg.rubyAfterStrokeColor);
        cfg.rubyBeforeStroke2Fill = karaokeLayerFillFromColors(rubyKaraokeColors, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.rubyBeforeStroke2Color);
        cfg.rubyAfterStroke2Fill = karaokeLayerFillFromColors(rubyKaraokeColors, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.rubyAfterStroke2Color);
        cfg.rubyBeforeShadowFill = karaokeLayerFillFromColors(rubyKaraokeColors, QStringLiteral("before"), QStringLiteral("shadow"), cfg.rubyBeforeShadowColor);
        cfg.rubyAfterShadowFill = karaokeLayerFillFromColors(rubyKaraokeColors, QStringLiteral("after"), QStringLiteral("shadow"), cfg.rubyAfterShadowColor);
    } else if (hasMainKaraokeColors) {
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
    } else {
        cfg.rubyBaseColor = cfg.baseColor;
        cfg.rubyFillColor = cfg.rubyColor;
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

    return cfg;
}

QString lineText(const TimingLine &line) {
    QString text;
    for (const auto &ch : line.chars) {
        text += ch.text;
    }
    return text;
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

QBrush brushForFill(const PaintFillSpec &fill, const QRectF &rect) {
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

QFont buildLineFont(const RenderConfig &cfg) {
    QFont font(cfg.fontFamily);
    font.setPixelSize(cfg.fontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(cfg.fontWeight, 1, 999)));
    if (cfg.letterSpacingPx != 0) {
        font.setLetterSpacing(QFont::AbsoluteSpacing, cfg.letterSpacingPx);
    }
    return font;
}

QFont buildRubyFont(const RenderConfig &cfg) {
    QFont font(cfg.fontFamily);
    font.setPixelSize(cfg.rubyFontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(cfg.fontWeight, 1, 999)));
    return font;
}

double visualStrokeExtent(const RenderConfig &cfg) {
    return std::ceil((std::max(cfg.strokeWidthPx, 0) + std::max(cfg.stroke2WidthPx, 0)) / 2.0);
}

double strokePenWidth(const RenderConfig &cfg) {
    return std::max(cfg.strokeWidthPx, 0);
}

double stroke2PenWidth(const RenderConfig &cfg) {
    return std::max(cfg.strokeWidthPx, 0) + std::max(cfg.stroke2WidthPx, 0);
}

int glowRadius(const RenderConfig &cfg, bool after) {
    int value = after ? cfg.glowAfterRadiusPx : cfg.glowBeforeRadiusPx;
    if (value == 10 && cfg.glowRadiusPx != 10) {
        value = cfg.glowRadiusPx;
    }
    return std::max(value, 1);
}

double glowPenWidth(const RenderConfig &cfg, bool after) {
    const double baseWidth = cfg.stroke2WidthPx > 0 ? stroke2PenWidth(cfg) : strokePenWidth(cfg);
    return std::max(1.0, baseWidth + glowRadius(cfg, after));
}

double glowExtent(const RenderConfig &cfg, bool after) {
    const int radius = glowRadius(cfg, after);
    return std::ceil(glowPenWidth(cfg, after) / 2.0 + radius * 3.0);
}

double afterClipVerticalExtent(const RenderConfig &cfg) {
    const double strokeExtent = visualStrokeExtent(cfg);
    const double glowExtra = cfg.decorationKind == QStringLiteral("glow") ? glowExtent(cfg, true) : 0.0;
    const double shadowExtra = cfg.decorationKind == QStringLiteral("shadow") ? std::abs(cfg.shadowOffsetY) : 0.0;
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

double rubyScale(const RenderConfig &cfg) {
    return static_cast<double>(std::max(cfg.rubyFontSizePx, 1)) / static_cast<double>(std::max(cfg.fontSizePx, 1));
}

double rubyVisualPadding(const RenderConfig &cfg) {
    const double scale = rubyScale(cfg);
    const int strokeWidth = scaledPx(cfg.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(cfg.stroke2WidthPx, scale);
    const double strokeExtent = std::ceil((std::max(strokeWidth, 0) + std::max(stroke2Width, 0)) / 2.0);
    double glowExtra = 0.0;
    if (cfg.decorationKind == QStringLiteral("glow")) {
        const int rubyGlowRadius = scaledPx(glowRadius(cfg, true), scale);
        const int baseWidth = stroke2Width > 0 ? strokeWidth + stroke2Width : strokeWidth;
        glowExtra = std::ceil((std::max(1, baseWidth + rubyGlowRadius)) / 2.0 + std::max(rubyGlowRadius, 1) * 3.0);
    }
    const double shadowX = cfg.decorationKind == QStringLiteral("shadow") ? std::abs(scaledSignedPx(cfg.shadowOffsetX, scale)) : 0.0;
    const double shadowY = cfg.decorationKind == QStringLiteral("shadow") ? std::abs(scaledSignedPx(cfg.shadowOffsetY, scale)) : 0.0;
    return std::max({strokeExtent, glowExtra, shadowX, shadowY, 2.0});
}

double baselineYForLine(const RenderConfig &cfg, const QFontMetricsF &metrics, int lane, int visibleLineCount) {
    const double pad = visualStrokeExtent(cfg);
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

LineLayout layoutLine(const RenderConfig &cfg, const TimingLine &line, int lane, int visibleLineCount) {
    const QString text = lineText(line);

    LineLayout layout;
    layout.text = text;
    layout.font = buildLineFont(cfg);

    const QFontMetricsF metrics(layout.font);
    layout.ascent = metrics.ascent();
    layout.descent = metrics.descent();
    layout.height = metrics.height();

    layout.charWidths.reserve(line.chars.size());
    double totalWidth = 0.0;
    for (const auto &ch : line.chars) {
        const double width = std::max(1.0, metrics.horizontalAdvance(ch.text));
        layout.charWidths.push_back(width);
        totalWidth += width;
    }
    totalWidth += std::max(0, static_cast<int>(line.chars.size()) - 1) * cfg.letterSpacingPx;
    layout.width = std::max(1.0, totalWidth);
    layout.x = lineXForLine(cfg, layout.width, visualStrokeExtent(cfg), lane);

    layout.baselineY = baselineYForLine(cfg, metrics, lane, visibleLineCount);

    layout.charLefts.resize(line.chars.size());
    if (cfg.rightToLeft) {
        double cursor = layout.x + layout.width;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            cursor -= layout.charWidths[i];
            layout.charLefts[i] = cursor;
            cursor -= cfg.letterSpacingPx;
        }
    } else {
        double cursor = layout.x;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.charLefts[i] = cursor;
            cursor += layout.charWidths[i] + cfg.letterSpacingPx;
        }
    }

    // C2 keeps one complete line path for both before/after layers. Karaoke
    // progress is expressed only by clipping the after layer, not by rebuilding
    // a prefix string path that can drift under kerning/shaping.
    layout.path.addText(QPointF(layout.x, layout.baselineY), layout.font, text);
    return layout;
}

std::optional<QRectF> afterClipRectFromCharacterTiming(const RenderConfig &cfg, const TimingLine &line, const LineLayout &layout, int tMs) {
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

    const double verticalExtent = afterClipVerticalExtent(cfg);
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

std::vector<RubyDiagnostics> rubyDiagnosticsForLine(
    const RenderConfig &cfg,
    const TimingLine &line,
    const LineLayout &layout,
    int tMs
) {
    std::vector<RubyDiagnostics> diagnostics;
    if (cfg.rubies.empty()) {
        return diagnostics;
    }
    const QFont rubyFont = buildRubyFont(cfg);
    const QFontMetricsF rubyMetrics(rubyFont);
    const auto intervals = lineIntervals(line);
    const double rubyBaselineY = layout.baselineY - layout.ascent - cfg.rubyGapPx;
    const double pad = rubyVisualPadding(cfg);

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

std::optional<QRectF> afterClipRect(const RenderConfig &cfg, const TimingLine &line, const LineLayout &layout, int tMs) {
    if (cfg.rubies.empty()) {
        return afterClipRectFromCharacterTiming(cfg, line, layout, tMs);
    }
    const auto band = fillClipBand(fillSegmentsForLine(cfg, line, layout, tMs), cfg.rightToLeft);
    if (!band.has_value()) {
        return std::nullopt;
    }
    const double verticalExtent = afterClipVerticalExtent(cfg);
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

void paintGlowPathWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const PaintFillSpec &fill,
    const QRectF &rect,
    int radius,
    int strokeWidth,
    int stroke2Width
) {
    const int glowRadius = std::max(radius, 1);
    const int baseWidth = stroke2Width > 0 ? strokeWidth + stroke2Width : std::max(strokeWidth, 0);
    const int glowWidth = std::max(1, baseWidth + glowRadius);
    const QRectF bounds = path.boundingRect();
    if (bounds.isEmpty()) {
        return;
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

    painter.drawImage(QPointF(layerRect.left(), layerRect.top()), blurImage(source, glowRadius));
}

void paintTextLayerStackWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const RenderConfig &cfg,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    if (cfg.decorationKind == QStringLiteral("glow")) {
        paintGlowPathWithWidths(
            painter,
            path,
            shadow,
            rect,
            glowRadiusValue,
            strokeWidth,
            stroke2Width
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

void paintRubyDiagnostics(
    QPainter &painter,
    const RenderConfig &cfg,
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
    const QFont rubyFont = buildRubyFont(cfg);
    const QFontMetricsF rubyMetrics(rubyFont);
    const double scale = rubyScale(cfg);
    const int strokeWidth = scaledPx(cfg.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(cfg.stroke2WidthPx, scale);
    for (const RubyDiagnostics &ruby : rubies) {
        const QPainterPath path = rubyTextPath(
            ruby.reading,
            rubyFont,
            rubyMetrics,
            ruby.x,
            ruby.baselineY,
            ruby.targetWidth
        );
        const QRectF rect(
            ruby.x,
            ruby.baselineY - rubyMetrics.ascent(),
            ruby.readingWidth,
            rubyMetrics.height()
        );
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            base,
            beforeStroke,
            beforeStroke2,
            beforeShadow,
            cfg,
            strokeWidth,
            stroke2Width,
            scaledSignedPx(cfg.shadowOffsetX, scale),
            scaledSignedPx(cfg.shadowOffsetY, scale),
            scaledPx(glowRadius(cfg, false), scale)
        );
        if (ruby.progress <= 0.0) {
            continue;
        }
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
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            fill,
            afterStroke,
            afterStroke2,
            afterShadow,
            cfg,
            strokeWidth,
            stroke2Width,
            scaledSignedPx(cfg.shadowOffsetX, scale),
            scaledSignedPx(cfg.shadowOffsetY, scale),
            scaledPx(glowRadius(cfg, true), scale)
        );
        painter.restore();
    }
}

void paintLine(QPainter &painter, const RenderConfig &cfg, const TimingLine &line, int tMs, int lane, int visibleLineCount, RenderDiagnostics *diagnostics) {
    const QString text = lineText(line);
    if (text.isEmpty()) {
        return;
    }

    const LineLayout layout = layoutLine(cfg, line, lane, visibleLineCount);

    const QRectF lineRect(layout.x, layout.baselineY - layout.ascent, layout.width, layout.height);
    const auto rubyDiagnostics = rubyDiagnosticsForLine(cfg, line, layout, tMs);

    paintRubyDiagnostics(
        painter,
        cfg,
        rubyDiagnostics,
        cfg.rubyBaseFill,
        cfg.rubyAfterFill,
        cfg.rubyBeforeStrokeFill,
        cfg.rubyAfterStrokeFill,
        cfg.rubyBeforeStroke2Fill,
        cfg.rubyAfterStroke2Fill,
        cfg.rubyBeforeShadowFill,
        cfg.rubyAfterShadowFill
    );
    paintTextLayerStackWithWidths(
        painter,
        layout.path,
        lineRect,
        cfg.baseFill,
        cfg.beforeStrokeFill,
        cfg.beforeStroke2Fill,
        cfg.beforeShadowFill,
        cfg,
        cfg.strokeWidthPx,
        cfg.stroke2WidthPx,
        cfg.shadowOffsetX,
        cfg.shadowOffsetY,
        glowRadius(cfg, false)
    );

    const auto clip = afterClipRect(cfg, line, layout, tMs);
    if (clip.has_value() && clip->width() > 0.0) {
        painter.save();
        painter.setClipRect(*clip, Qt::IntersectClip);
        paintTextLayerStackWithWidths(
            painter,
            layout.path,
            lineRect,
            cfg.afterFill,
            cfg.afterStrokeFill,
            cfg.afterStroke2Fill,
            cfg.afterShadowFill,
            cfg,
            cfg.strokeWidthPx,
            cfg.stroke2WidthPx,
            cfg.shadowOffsetX,
            cfg.shadowOffsetY,
            glowRadius(cfg, true)
        );
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
            const double verticalExtent = afterClipVerticalExtent(cfg);
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
    QJsonObject out = response(true, QStringLiteral("configured"));
    out.insert(QStringLiteral("width"), parsed->width);
    out.insert(QStringLiteral("height"), parsed->height);
    out.insert(QStringLiteral("fps"), parsed->fps);
    out.insert(QStringLiteral("line_count"), static_cast<int>(parsed->lines.size()));
    out.insert(QStringLiteral("ruby_count"), static_cast<int>(parsed->rubies.size()));
    return out;
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

    RenderResult rendered = renderFrame(*config, tMs);
    QImage &image = rendered.image;
    const bool saved = image.save(outputPath);
    QJsonObject out = response(saved, QStringLiteral("frame_ready"));
    out.insert(QStringLiteral("t_ms"), tMs);
    out.insert(QStringLiteral("width"), image.width());
    out.insert(QStringLiteral("height"), image.height());
    out.insert(QStringLiteral("output_path"), outputPath);
    out.insert(QStringLiteral("checksum"), QString::number(imageChecksum(image)));
    out.insert(QStringLiteral("visible_lines"), rendered.diagnostics.visibleLines);
    QJsonArray lineDiagnostics;
    for (const LineDiagnostics &line : rendered.diagnostics.lines) {
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
    out.insert(QStringLiteral("line_diagnostics"), lineDiagnostics);
    QJsonArray rubyDiagnostics;
    for (const RubyDiagnostics &ruby : rendered.diagnostics.rubies) {
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
    out.insert(QStringLiteral("ruby_diagnostics"), rubyDiagnostics);
    if (rendered.diagnostics.hasFirstLine) {
        out.insert(QStringLiteral("line_x"), rendered.diagnostics.lineX);
        out.insert(QStringLiteral("line_width"), rendered.diagnostics.lineWidth);
        out.insert(QStringLiteral("baseline_y"), rendered.diagnostics.baselineY);
        out.insert(QStringLiteral("after_clip_left"), rendered.diagnostics.afterClipLeft);
        out.insert(QStringLiteral("after_clip_right"), rendered.diagnostics.afterClipRight);
        out.insert(QStringLiteral("after_clip_top"), rendered.diagnostics.afterClipTop);
        out.insert(QStringLiteral("after_clip_height"), rendered.diagnostics.afterClipHeight);
    }
    if (!saved) {
        out.insert(QStringLiteral("error"), QStringLiteral("failed to save output image"));
    }
    return out;
}

QJsonObject parseErrorResponse(const QString &message) {
    QJsonObject out = response(false, QStringLiteral("parse_error"));
    out.insert(QStringLiteral("error"), message);
    return out;
}

}  // namespace

int main(int argc, char **argv) {
    qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty() ? QByteArray("offscreen") : qgetenv("QT_QPA_PLATFORM"));
    QApplication app(argc, argv);

    QJsonObject ready = response(true, QStringLiteral("ready"));
    ready.insert(QStringLiteral("schema"), kProtocolSchema);
    ready.insert(QStringLiteral("qt"), QString::fromLatin1(qVersion()));
    writeJson(ready);

    std::optional<RenderConfig> config;
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
        } else if (command == QStringLiteral("shutdown")) {
            writeJson(response(true, QStringLiteral("shutdown")));
            return 0;
        } else {
            QJsonObject out = response(false, QStringLiteral("unknown_command"));
            out.insert(QStringLiteral("error"), QStringLiteral("unknown command: ") + command);
            writeJson(out);
        }
    }

    return 0;
}
