#include "render_config_parser.h"

#include "json_protocol.h"
#include "json_value.h"

#include <algorithm>
#include <utility>
#include <vector>

namespace krok::subtitle::native::protocol {

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

std::vector<std::pair<double, QString>> parseGradientStops(
    const QJsonValue &value,
    const QString &startColor,
    const QString &endColor
) {
    std::vector<std::pair<double, QString>> stops;
    const QJsonArray items = value.toArray();
    for (const auto &item : items) {
        const QJsonArray pair = item.toArray();
        if (pair.size() < 2 || !pair.at(0).isDouble() || !pair.at(1).isString()) {
            continue;
        }
        stops.push_back({
            std::clamp(pair.at(0).toDouble(), 0.0, 100.0),
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
    const QJsonValue splitStopsValue = object.value(QStringLiteral("split_stops"));
    if (splitStopsValue.toArray().isEmpty()) {
        fill.splitStops = {
            {0.0, fill.splitTopColor},
            {static_cast<double>(fill.splitPositionPct), fill.splitBottomColor},
            {100.0, fill.splitBottomColor},
        };
    } else {
        fill.splitStops = parseGradientStops(
            splitStopsValue,
            fill.splitTopColor,
            fill.splitBottomColor
        );
    }
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

void applySignalStyleOverrides(ResolvedStyle &cfg, const QJsonObject &style) {
    if (hasNonNull(style, QStringLiteral("lit_enabled"))) {
        cfg.litEnabled = style.value(QStringLiteral("lit_enabled")).toBool(cfg.litEnabled);
    }
    if (hasNonNull(style, QStringLiteral("lit_style"))) {
        cfg.litStyle = stringValue(style, QStringLiteral("lit_style"), cfg.litStyle);
    }
    cfg.litNumber = std::clamp(intValue(style, QStringLiteral("lit_number"), cfg.litNumber), 1, 8);
    cfg.litSize = std::max(1, intValue(style, QStringLiteral("lit_size"), cfg.litSize));
    cfg.litOffsetX = intValue(style, QStringLiteral("lit_offset_x"), cfg.litOffsetX);
    cfg.litOffsetY = intValue(style, QStringLiteral("lit_offset_y"), cfg.litOffsetY);
    cfg.litTracking = std::max(0, intValue(style, QStringLiteral("lit_tracking"), cfg.litTracking));
    cfg.litFillColor = stringValue(style, QStringLiteral("lit_fill_color"), cfg.litFillColor);
    cfg.litStrokeColor = stringValue(style, QStringLiteral("lit_stroke_color"), cfg.litStrokeColor);
    cfg.litStrokeWidth = std::max(0, intValue(style, QStringLiteral("lit_stroke_width"), cfg.litStrokeWidth));
    cfg.litStrokeSoften = std::max(0, intValue(style, QStringLiteral("lit_stroke_soften"), cfg.litStrokeSoften));
    cfg.litOpacityPct = std::clamp(intValue(style, QStringLiteral("lit_opacity_pct"), cfg.litOpacityPct), 0, 100);
    cfg.litEdgeBrightnessPct = std::clamp(intValue(style, QStringLiteral("lit_edge_brightness_pct"), cfg.litEdgeBrightnessPct), 0, 100);
    if (hasNonNull(style, QStringLiteral("lit_shadow"))) {
        cfg.litShadow = style.value(QStringLiteral("lit_shadow")).toBool(cfg.litShadow);
    }
    cfg.litTimeOffsetMs = intValue(style, QStringLiteral("lit_time_offset_ms"), cfg.litTimeOffsetMs);
    cfg.litWaitingTimeMs = std::max(0, intValue(style, QStringLiteral("lit_waiting_time_ms"), cfg.litWaitingTimeMs));
    cfg.litTransitionMode = stringValue(style, QStringLiteral("lit_transition_mode"), cfg.litTransitionMode);
    cfg.litTransitionRatioPct = std::clamp(intValue(style, QStringLiteral("lit_transition_ratio_pct"), cfg.litTransitionRatioPct), 0, 100);
    cfg.litTransitionAngleDeg = intValue(style, QStringLiteral("lit_transition_angle_deg"), cfg.litTransitionAngleDeg);
    cfg.litTransitionDistance = std::max(0, intValue(style, QStringLiteral("lit_transition_distance"), cfg.litTransitionDistance));
    cfg.signalsDurationMs = std::max(0, intValue(style, QStringLiteral("signals_duration_ms"), cfg.signalsDurationMs));
    cfg.volumeSize = std::max(1, intValue(style, QStringLiteral("volume_size"), cfg.volumeSize));
    cfg.volumeOffsetX = intValue(style, QStringLiteral("volume_offset_x"), cfg.volumeOffsetX);
    cfg.volumeOffsetY = intValue(style, QStringLiteral("volume_offset_y"), cfg.volumeOffsetY);
    cfg.volumeColumnWidth = std::max(1, intValue(style, QStringLiteral("volume_column_width"), cfg.volumeColumnWidth));
    cfg.volumeColumnCount = std::clamp(intValue(style, QStringLiteral("volume_column_count"), cfg.volumeColumnCount), 1, 16);
    cfg.volumeColumnSpacing = std::max(0, intValue(style, QStringLiteral("volume_column_spacing"), cfg.volumeColumnSpacing));
    cfg.volumeAlign = intValue(style, QStringLiteral("volume_align"), cfg.volumeAlign);
    cfg.volumeRatio = std::max(style.value(QStringLiteral("volume_ratio")).toDouble(cfg.volumeRatio), 0.01);
    cfg.volumeFillColor = stringValue(style, QStringLiteral("volume_fill_color"), cfg.volumeFillColor);
    cfg.volumeStrokeColor = stringValue(style, QStringLiteral("volume_stroke_color"), cfg.volumeStrokeColor);
    cfg.volumeOverlayFillColor = stringValue(style, QStringLiteral("volume_overlay_fill_color"), cfg.volumeOverlayFillColor);
    cfg.volumeOverlayStrokeColor = stringValue(style, QStringLiteral("volume_overlay_stroke_color"), cfg.volumeOverlayStrokeColor);
    cfg.volumeFlashTimes = std::max(0, intValue(style, QStringLiteral("volume_flash_times"), cfg.volumeFlashTimes));
    cfg.volumeFlashDurationRatio = std::max(style.value(QStringLiteral("volume_flash_duration_ratio")).toDouble(cfg.volumeFlashDurationRatio), 0.0);
    cfg.volumeTransitionRatioPct = std::clamp(intValue(style, QStringLiteral("volume_transition_ratio_pct"), cfg.volumeTransitionRatioPct), 0, 100);
}

std::optional<krok::subtitle::native::VectorGlyph> parseVectorGlyph(
    const QJsonValue &value
) {
    if (!value.isObject()) {
        return std::nullopt;
    }
    const QJsonObject object = value.toObject();
    const QJsonArray sourceCommands = object.value(
        QStringLiteral("path_commands")
    ).toArray();
    if (sourceCommands.isEmpty()) {
        return std::nullopt;
    }
    krok::subtitle::native::VectorGlyph glyph;
    glyph.unitsPerEm = static_cast<float>(std::max(
        object.value(QStringLiteral("units_per_em")).toDouble(1000.0),
        1.0
    ));
    glyph.advanceWidth = static_cast<float>(std::max(
        object.value(QStringLiteral("advance_width")).toDouble(glyph.unitsPerEm),
        0.0
    ));
    for (const QJsonValue &commandValue : sourceCommands) {
        const QJsonArray commandArray = commandValue.toArray();
        if (commandArray.isEmpty() || !commandArray.first().isString()) {
            return std::nullopt;
        }
        const QString kindText = commandArray.first().toString().toUpper();
        if (kindText.size() != 1) {
            return std::nullopt;
        }
        const char kind = kindText.at(0).toLatin1();
        int expectedValues = -1;
        if (kind == 'M' || kind == 'L') {
            expectedValues = 2;
        } else if (kind == 'C') {
            expectedValues = 6;
        } else if (kind == 'Q') {
            expectedValues = 4;
        } else if (kind == 'Z') {
            expectedValues = 0;
        }
        if (expectedValues < 0 || commandArray.size() != expectedValues + 1) {
            return std::nullopt;
        }
        krok::subtitle::native::VectorPathCommand command;
        command.kind = kind;
        command.values.reserve(static_cast<std::size_t>(expectedValues));
        for (int index = 0; index < expectedValues; ++index) {
            const QJsonValue coordinate = commandArray.at(index + 1);
            if (!coordinate.isDouble() || !std::isfinite(coordinate.toDouble())) {
                return std::nullopt;
            }
            command.values.push_back(static_cast<float>(coordinate.toDouble()));
        }
        glyph.commands.push_back(std::move(command));
    }
    return glyph.commands.empty()
        ? std::nullopt
        : std::optional<krok::subtitle::native::VectorGlyph>(std::move(glyph));
}

std::optional<krok::subtitle::native::BitmapGuide> parseBitmapGuide(
    const QJsonValue &value
) {
    if (!value.isObject()) {
        return std::nullopt;
    }
    const QJsonObject object = value.toObject();
    const QString beforePath = stringValue(object, QStringLiteral("before_path"));
    if (beforePath.isEmpty()) {
        return std::nullopt;
    }
    krok::subtitle::native::BitmapGuide guide;
    guide.beforePath = beforePath.toStdWString();
    guide.afterPath = stringValue(object, QStringLiteral("after_path")).toStdWString();
    guide.zoomPercent = static_cast<float>(std::clamp(
        object.value(QStringLiteral("zoom_percent")).toDouble(100.0),
        1.0,
        500.0
    ));
    guide.fixSize = object.value(QStringLiteral("fix_size")).toBool(false);
    guide.noDecor = object.value(QStringLiteral("no_decor")).toBool(false);
    guide.forceWipeDecor = object.value(QStringLiteral("force_wipe_decor")).toBool(false);
    guide.marginLeft = static_cast<float>(
        object.value(QStringLiteral("margin_left_px")).toDouble(0.0)
    );
    guide.marginRight = static_cast<float>(
        object.value(QStringLiteral("margin_right_px")).toDouble(0.0)
    );
    guide.marginBottom = static_cast<float>(
        object.value(QStringLiteral("margin_bottom_px")).toDouble(0.0)
    );
    guide.beforeModifiedMs = static_cast<std::uint64_t>(
        std::max(object.value(QStringLiteral("before_modified_ms")).toDouble(0.0), 0.0)
    );
    guide.beforeSize = static_cast<std::uint64_t>(
        std::max(object.value(QStringLiteral("before_size")).toDouble(0.0), 0.0)
    );
    guide.afterModifiedMs = static_cast<std::uint64_t>(
        std::max(object.value(QStringLiteral("after_modified_ms")).toDouble(0.0), 0.0)
    );
    guide.afterSize = static_cast<std::uint64_t>(
        std::max(object.value(QStringLiteral("after_size")).toDouble(0.0), 0.0)
    );
    guide.animAnchorMs = static_cast<int>(std::clamp(
        object.value(QStringLiteral("anim_anchor_ms")).toDouble(0.0),
        -2147483648.0,
        2147483647.0
    ));
    return guide;
}

void applyScalarStyleOverrides(ResolvedStyle &cfg, const QJsonObject &style) {
    applySignalStyleOverrides(cfg, style);
    if (hasNonNull(style, QStringLiteral("font_family"))) {
        cfg.fontFamily = stringValue(style, QStringLiteral("font_family"), cfg.fontFamily);
    }
    if (hasNonNull(style, QStringLiteral("font_family_latin"))) {
        cfg.fontFamilyLatin = stringValue(
            style, QStringLiteral("font_family_latin"), cfg.fontFamilyLatin
        );
    }
    if (hasNonNull(style, QStringLiteral("font_size_px"))) {
        cfg.fontSizePx = std::max(1, intValue(style, QStringLiteral("font_size_px"), cfg.fontSizePx));
    }
    if (hasNonNull(style, QStringLiteral("font_weight"))) {
        cfg.fontWeight = std::clamp(intValue(style, QStringLiteral("font_weight"), cfg.fontWeight), 1, 999);
    }
    if (hasNonNull(style, QStringLiteral("latin_font_size_px"))) {
        cfg.latinFontSizePx = std::max(
            1, intValue(style, QStringLiteral("latin_font_size_px"), cfg.fontSizePx)
        );
    }
    if (hasNonNull(style, QStringLiteral("latin_font_weight"))) {
        cfg.latinFontWeight = std::clamp(
            intValue(style, QStringLiteral("latin_font_weight"), cfg.fontWeight), 1, 999
        );
    }
    if (hasNonNull(style, QStringLiteral("italic"))) {
        cfg.italic = style.value(QStringLiteral("italic")).toBool(cfg.italic);
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
    if (hasNonNull(style, QStringLiteral("stroke2_enabled"))
        && !style.value(QStringLiteral("stroke2_enabled")).toBool()) {
        cfg.stroke2Enabled = false;
        cfg.stroke2WidthPx = 0;
        if (hasNonNull(style, QStringLiteral("stroke2_width_px"))) {
            cfg.stroke2RawWidthPx = std::max(0, intValue(style, QStringLiteral("stroke2_width_px"), cfg.stroke2RawWidthPx));
        }
    } else {
        if (hasNonNull(style, QStringLiteral("stroke2_enabled"))) {
            cfg.stroke2Enabled = true;
        }
        if (hasNonNull(style, QStringLiteral("stroke2_width_px"))) {
            cfg.stroke2WidthPx = std::max(0, intValue(style, QStringLiteral("stroke2_width_px"), cfg.stroke2WidthPx));
            cfg.stroke2RawWidthPx = cfg.stroke2WidthPx;
        }
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
    if (hasNonNull(style, QStringLiteral("allow_biting"))) {
        cfg.allowBiting = style.value(QStringLiteral("allow_biting")).toBool(cfg.allowBiting);
    }
    if (hasNonNull(style, QStringLiteral("affects_ruby_anchor"))) {
        cfg.affectsRubyAnchor = style.value(
            QStringLiteral("affects_ruby_anchor")
        ).toBool(cfg.affectsRubyAnchor);
    }
    if (hasNonNull(style, QStringLiteral("space_width_percent"))) {
        cfg.spaceWidthPercent = std::clamp(
            intValue(style, QStringLiteral("space_width_percent"), cfg.spaceWidthPercent),
            10,
            100
        );
    }
    if (hasNonNull(style, QStringLiteral("glow_concentration_level"))) {
        cfg.glowConcentrationLevel = std::clamp(
            intValue(style, QStringLiteral("glow_concentration_level"), cfg.glowConcentrationLevel),
            0,
            2
        );
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
    if (hasNonNull(style, QStringLiteral("ruby_font_family"))) {
        cfg.rubyFontFamily = stringValue(
            style, QStringLiteral("ruby_font_family"), cfg.rubyFontFamily
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_font_family_latin"))) {
        cfg.rubyFontFamilyLatin = stringValue(
            style, QStringLiteral("ruby_font_family_latin"), cfg.rubyFontFamilyLatin
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_font_weight"))) {
        cfg.rubyFontWeight = std::clamp(
            intValue(style, QStringLiteral("ruby_font_weight"), cfg.fontWeight), 1, 999
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_latin_font_size_px"))) {
        cfg.rubyLatinFontSizePx = std::max(
            1, intValue(style, QStringLiteral("ruby_latin_font_size_px"), cfg.rubyFontSizePx)
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_latin_font_weight"))) {
        cfg.rubyLatinFontWeight = std::clamp(
            intValue(style, QStringLiteral("ruby_latin_font_weight"), cfg.fontWeight), 1, 999
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_font_follow_main"))) {
        cfg.rubyFontFollowMain = style.value(
            QStringLiteral("ruby_font_follow_main")
        ).toBool(cfg.rubyFontFollowMain);
    }
    if (hasNonNull(style, QStringLiteral("ruby_gap_px"))) {
        cfg.rubyGapPx = intValue(style, QStringLiteral("ruby_gap_px"), cfg.rubyGapPx);
    }
    if (hasNonNull(style, QStringLiteral("ruby_main_progress_mode"))) {
        cfg.rubyMainProgressMode = stringValue(
            style, QStringLiteral("ruby_main_progress_mode"), cfg.rubyMainProgressMode
        );
        if (cfg.rubyMainProgressMode != QStringLiteral("reading_units")) {
            cfg.rubyMainProgressMode = QStringLiteral("checkpoint_segments");
        }
    }
    if (hasNonNull(style, QStringLiteral("ruby_horizontal_gradient_with_main"))) {
        cfg.rubyHorizontalGradientWithMain = style.value(
            QStringLiteral("ruby_horizontal_gradient_with_main")
        ).toBool(cfg.rubyHorizontalGradientWithMain);
    }
    if (hasNonNull(style, QStringLiteral("ruby_stroke_width_px"))) {
        cfg.rubyStrokeWidthPx = std::max(
            0, intValue(style, QStringLiteral("ruby_stroke_width_px"), 0)
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_stroke2_enabled"))) {
        cfg.rubyStroke2Enabled = style.value(QStringLiteral("ruby_stroke2_enabled")).toBool();
    }
    if (hasNonNull(style, QStringLiteral("ruby_stroke2_width_px"))) {
        cfg.rubyStroke2WidthPx = std::max(
            0, intValue(style, QStringLiteral("ruby_stroke2_width_px"), 0)
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_decoration_kind"))) {
        cfg.rubyDecorationKind = stringValue(
            style, QStringLiteral("ruby_decoration_kind"), cfg.rubyDecorationKind
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_glow_radius_px"))) {
        const int radius = std::max(
            0, intValue(style, QStringLiteral("ruby_glow_radius_px"), 0)
        );
        cfg.rubyGlowBeforeRadiusPx = radius;
        cfg.rubyGlowAfterRadiusPx = radius;
    }
    if (hasNonNull(style, QStringLiteral("ruby_glow_before_radius_px"))) {
        cfg.rubyGlowBeforeRadiusPx = std::max(
            0,
            intValue(
                style,
                QStringLiteral("ruby_glow_before_radius_px"),
                0
            )
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_glow_after_radius_px"))) {
        cfg.rubyGlowAfterRadiusPx = std::max(
            0,
            intValue(
                style,
                QStringLiteral("ruby_glow_after_radius_px"),
                0
            )
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_glow_concentration_level"))) {
        cfg.rubyGlowConcentrationLevel = std::clamp(
            intValue(
                style,
                QStringLiteral("ruby_glow_concentration_level"),
                0
            ),
            0,
            2
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_shadow_offset_x"))) {
        cfg.rubyShadowOffsetX = intValue(
            style, QStringLiteral("ruby_shadow_offset_x"), cfg.shadowOffsetX
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_shadow_offset_y"))) {
        cfg.rubyShadowOffsetY = intValue(
            style, QStringLiteral("ruby_shadow_offset_y"), cfg.shadowOffsetY
        );
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
    } else if (hasObject(scheme, QStringLiteral("karaoke_colors"))) {
        // Painter treats a role's main colors as that role's implicit ruby
        // colors. A global explicit ruby palette must not leak into it.
        cfg.hasRubyKaraokeColors = true;
        copyMainColorsToRuby(cfg);
    } else if (!cfg.hasRubyKaraokeColors) {
        if (cfg.hasMainKaraokeColors) {
            copyMainColorsToRuby(cfg);
        } else {
            refreshLegacyRubyFills(cfg);
        }
    }
    return cfg;
}

ResolvedStyle resolvedStyleFromTitle(
    const ResolvedStyle &base,
    const QJsonObject &title
) {
    ResolvedStyle cfg = base;
    cfg.fontFamily = stringValue(title, QStringLiteral("font_family"), cfg.fontFamily);
    cfg.fontFamilyLatin = stringValue(
        title, QStringLiteral("font_family_latin"), cfg.fontFamilyLatin
    );
    cfg.fontSizePx = std::max(
        1, intValue(title, QStringLiteral("font_size_px"), cfg.fontSizePx)
    );
    cfg.latinFontSizePx = hasNonNull(title, QStringLiteral("latin_font_size_px"))
        ? std::max(
            1,
            intValue(title, QStringLiteral("latin_font_size_px"), cfg.fontSizePx)
        )
        : cfg.fontSizePx;
    cfg.fontWeight = std::clamp(
        intValue(title, QStringLiteral("font_weight"), cfg.fontWeight), 1, 999
    );
    cfg.latinFontWeight = hasNonNull(title, QStringLiteral("latin_font_weight"))
        ? std::clamp(
            intValue(title, QStringLiteral("latin_font_weight"), cfg.fontWeight),
            1,
            999
        )
        : cfg.fontWeight;
    cfg.italic = title.value(QStringLiteral("italic")).toBool(cfg.italic);
    cfg.letterSpacingPx = intValue(
        title, QStringLiteral("letter_spacing_px"), cfg.letterSpacingPx
    );

    cfg.baseFill = paintFillSpec(
        title.value(QStringLiteral("fill")).toObject(), cfg.baseColor
    );
    cfg.baseColor = cfg.baseFill.color;
    cfg.afterFill = cfg.baseFill;
    cfg.fillColor = cfg.baseColor;
    cfg.beforeStrokeFill = paintFillSpec(
        title.value(QStringLiteral("stroke")).toObject(), cfg.beforeStrokeColor
    );
    cfg.beforeStrokeColor = cfg.beforeStrokeFill.color;
    cfg.afterStrokeFill = cfg.beforeStrokeFill;
    cfg.afterStrokeColor = cfg.beforeStrokeColor;
    cfg.beforeStroke2Fill = paintFillSpec(
        title.value(QStringLiteral("stroke2")).toObject(), cfg.beforeStroke2Color
    );
    cfg.beforeStroke2Color = cfg.beforeStroke2Fill.color;
    cfg.afterStroke2Fill = cfg.beforeStroke2Fill;
    cfg.afterStroke2Color = cfg.beforeStroke2Color;
    cfg.beforeShadowFill = paintFillSpec(
        title.value(QStringLiteral("shadow")).toObject(), cfg.beforeShadowColor
    );
    cfg.beforeShadowColor = cfg.beforeShadowFill.color;
    cfg.afterShadowFill = cfg.beforeShadowFill;
    cfg.afterShadowColor = cfg.beforeShadowColor;
    cfg.strokeWidthPx = std::max(
        0, intValue(title, QStringLiteral("stroke_width_px"), cfg.strokeWidthPx)
    );
    cfg.stroke2WidthPx = std::max(
        0, intValue(title, QStringLiteral("stroke2_width_px"), cfg.stroke2WidthPx)
    );
    cfg.decorationKind = stringValue(
        title, QStringLiteral("decoration_kind"), cfg.decorationKind
    );
    const int glowRadius = std::max(
        0, intValue(title, QStringLiteral("glow_radius_px"), cfg.glowRadiusPx)
    );
    cfg.glowRadiusPx = glowRadius;
    cfg.glowBeforeRadiusPx = glowRadius;
    cfg.glowAfterRadiusPx = glowRadius;
    cfg.glowConcentrationLevel = std::clamp(
        intValue(
            title,
            QStringLiteral("glow_concentration_level"),
            cfg.glowConcentrationLevel
        ),
        0,
        2
    );
    cfg.shadowOffsetX = intValue(
        title, QStringLiteral("shadow_offset_x"), cfg.shadowOffsetX
    );
    cfg.shadowOffsetY = intValue(
        title, QStringLiteral("shadow_offset_y"), cfg.shadowOffsetY
    );
    cfg.hasMainKaraokeColors = true;
    return cfg;
}

void buildResolvedStyleCache(RenderConfig &cfg);

std::optional<RenderConfig> parseRenderConfig(const QJsonObject &ir, QString *error) {
    if (ir.value(QStringLiteral("schema")).toInt() != kRenderIrSchema) {
        *error = QStringLiteral("unsupported Render IR schema");
        return std::nullopt;
    }

    RenderConfig cfg;
    // Schema 2：根级矢量导唱符轮廓表。相同 SVG 符号只出现一次，字符通过
    // ``vector_glyph_id`` 引用；共享同一不可变对象使 D2D geometry 可按符号缓存。
    const QJsonObject vectorGlyphTable = ir.value(QStringLiteral("vector_glyphs")).toObject();
    for (auto it = vectorGlyphTable.constBegin(); it != vectorGlyphTable.constEnd(); ++it) {
        if (auto glyph = parseVectorGlyph(it.value())) {
            cfg.vectorGlyphs.insert(
                it.key(),
                std::make_shared<const krok::subtitle::native::VectorGlyph>(std::move(*glyph))
            );
        }
    }
    const auto resolveVectorGlyph = [&cfg](const QJsonObject &charObject) {
        const QString glyphId = stringValue(
            charObject, QStringLiteral("vector_glyph_id")
        );
        if (!glyphId.isEmpty()) {
            return cfg.vectorGlyphs.value(glyphId, nullptr);
        }
        // 旧内嵌格式（无符号表）：按值解析后包装成共享指针。
        if (auto glyph = parseVectorGlyph(
                charObject.value(QStringLiteral("vector_glyph"))
            )) {
            return std::shared_ptr<const krok::subtitle::native::VectorGlyph>(
                new krok::subtitle::native::VectorGlyph(std::move(*glyph))
            );
        }
        return std::shared_ptr<const krok::subtitle::native::VectorGlyph>();
    };
    const QJsonObject screen = ir.value(QStringLiteral("screen")).toObject();
    cfg.width = std::max(1, intValue(screen, QStringLiteral("width"), cfg.width));
    cfg.height = std::max(1, intValue(screen, QStringLiteral("height"), cfg.height));
    cfg.fps = std::max(1, intValue(screen, QStringLiteral("fps"), cfg.fps));
    cfg.dpr = std::clamp(screen.value(QStringLiteral("dpr")).toDouble(1.0), 0.01, 4.0);

    const QJsonObject style = ir.value(QStringLiteral("style")).toObject();
    ResolvedStyle &base = cfg.baseStyle;
    applySignalStyleOverrides(base, style);
    base.fontFamily = stringValue(style, QStringLiteral("font_family"), base.fontFamily);
    base.fontFamilyLatin = stringValue(style, QStringLiteral("font_family_latin"), base.fontFamilyLatin);
    base.fontSizePx = std::max(1, intValue(style, QStringLiteral("font_size_px"), base.fontSizePx));
    if (style.value(QStringLiteral("latin_font_size_px")).isDouble()) {
        base.latinFontSizePx = std::max(1, intValue(style, QStringLiteral("latin_font_size_px"), base.fontSizePx));
    }
    base.fontWeight = std::clamp(intValue(style, QStringLiteral("font_weight"), base.fontWeight), 1, 999);
    if (style.value(QStringLiteral("latin_font_weight")).isDouble()) {
        base.latinFontWeight = std::clamp(intValue(style, QStringLiteral("latin_font_weight"), base.fontWeight), 1, 999);
    }
    base.italic = style.value(QStringLiteral("italic")).toBool(base.italic);
    base.allowBiting = style.value(QStringLiteral("allow_biting")).toBool(base.allowBiting);
    base.affectsRubyAnchor = style.value(
        QStringLiteral("affects_ruby_anchor")
    ).toBool(base.affectsRubyAnchor);
    base.spaceWidthPercent = std::clamp(
        intValue(style, QStringLiteral("space_width_percent"), base.spaceWidthPercent),
        10,
        100
    );
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
    base.stroke2RawWidthPx = base.stroke2WidthPx;
    if (style.value(QStringLiteral("stroke2_enabled")).isBool()) {
        base.stroke2Enabled = style.value(QStringLiteral("stroke2_enabled")).toBool();
        if (!base.stroke2Enabled) {
            base.stroke2WidthPx = 0;
        }
    }
    base.decorationKind = stringValue(style, QStringLiteral("decoration_kind"), base.decorationKind);
    base.glowRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_radius_px"), base.glowRadiusPx));
    base.glowBeforeRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_before_radius_px"), base.glowBeforeRadiusPx));
    base.glowAfterRadiusPx = std::max(1, intValue(style, QStringLiteral("glow_after_radius_px"), base.glowAfterRadiusPx));
    base.glowConcentrationLevel = std::clamp(
        intValue(style, QStringLiteral("glow_concentration_level"), base.glowConcentrationLevel),
        0,
        2
    );
    base.shadowOffsetX = intValue(style, QStringLiteral("shadow_offset_x"), base.shadowOffsetX);
    base.shadowOffsetY = intValue(style, QStringLiteral("shadow_offset_y"), base.shadowOffsetY);
    if (style.value(QStringLiteral("ruby_shadow_offset_x")).isDouble()) {
        base.rubyShadowOffsetX = style.value(QStringLiteral("ruby_shadow_offset_x")).toInt();
    }
    if (style.value(QStringLiteral("ruby_shadow_offset_y")).isDouble()) {
        base.rubyShadowOffsetY = style.value(QStringLiteral("ruby_shadow_offset_y")).toInt();
    }
    base.rubyFontSizePx = std::max(1, intValue(style, QStringLiteral("ruby_font_size_px"), base.rubyFontSizePx));
    base.rubyFontFamily = stringValue(style, QStringLiteral("ruby_font_family"), base.rubyFontFamily);
    base.rubyFontFamilyLatin = stringValue(
        style, QStringLiteral("ruby_font_family_latin"), base.rubyFontFamilyLatin
    );
    if (style.value(QStringLiteral("ruby_font_weight")).isDouble()) {
        base.rubyFontWeight = std::clamp(
            intValue(style, QStringLiteral("ruby_font_weight"), base.fontWeight), 1, 999
        );
    }
    if (style.value(QStringLiteral("ruby_latin_font_size_px")).isDouble()) {
        base.rubyLatinFontSizePx = std::max(
            1, intValue(style, QStringLiteral("ruby_latin_font_size_px"), base.rubyFontSizePx)
        );
    }
    if (style.value(QStringLiteral("ruby_latin_font_weight")).isDouble()) {
        base.rubyLatinFontWeight = std::clamp(
            intValue(style, QStringLiteral("ruby_latin_font_weight"), base.fontWeight), 1, 999
        );
    }
    base.rubyFontFollowMain = style.value(QStringLiteral("ruby_font_follow_main")).isBool()
        ? style.value(QStringLiteral("ruby_font_follow_main")).toBool()
        : base.rubyFontFollowMain;
    base.rubyGapPx = intValue(style, QStringLiteral("ruby_gap_px"), base.rubyGapPx);
    base.rubyIntervalPx = intValue(style, QStringLiteral("ruby_interval_px"), base.rubyIntervalPx);
    base.rubyAlignment = stringValue(
        style, QStringLiteral("ruby_alignment"), base.rubyAlignment
    );
    if (base.rubyAlignment != QStringLiteral("center")
        && base.rubyAlignment != QStringLiteral("equal_space")) {
        base.rubyAlignment = QStringLiteral("auto");
    }
    base.rubyMainProgressMode = stringValue(
        style, QStringLiteral("ruby_main_progress_mode"), base.rubyMainProgressMode
    );
    if (base.rubyMainProgressMode != QStringLiteral("reading_units")) {
        base.rubyMainProgressMode = QStringLiteral("checkpoint_segments");
    }
    base.rubyHorizontalGradientWithMain = style.value(
        QStringLiteral("ruby_horizontal_gradient_with_main")
    ).toBool(base.rubyHorizontalGradientWithMain);
    // Ruby stroke/decoration/glow stay unset unless the *global* style sets them
    // explicitly. The fallback to the effective main is applied per role in
    // applyGpuResolvedStyle, so a role that overrides its own main no longer
    // inherits the global main's baked ruby decoration/glow/stroke.
    if (style.value(QStringLiteral("ruby_stroke_width_px")).isDouble()) {
        base.rubyStrokeWidthPx = std::max(
            0, intValue(style, QStringLiteral("ruby_stroke_width_px"), 0)
        );
    }
    // The flag and the width are independent: ``ruby_stroke2_enabled: null``
    // means "follow the main text", and a saved width must not switch stroke2
    // back on by itself.  N3 projects that omit ``UseEdge2`` but keep
    // ``EdgeSize2`` hit exactly that combination.
    if (style.value(QStringLiteral("ruby_stroke2_enabled")).isBool()) {
        base.rubyStroke2Enabled = style.value(QStringLiteral("ruby_stroke2_enabled")).toBool();
    }
    if (style.value(QStringLiteral("ruby_stroke2_width_px")).isDouble()) {
        base.rubyStroke2WidthPx = std::max(
            0, intValue(style, QStringLiteral("ruby_stroke2_width_px"), 0)
        );
    }
    if (hasNonNull(style, QStringLiteral("ruby_decoration_kind"))) {
        base.rubyDecorationKind = stringValue(
            style, QStringLiteral("ruby_decoration_kind"), QString()
        );
    }
    const int rubyGlowCommon = style.value(QStringLiteral("ruby_glow_radius_px")).isDouble()
        ? std::max(0, intValue(style, QStringLiteral("ruby_glow_radius_px"), 0))
        : -1;
    if (style.value(QStringLiteral("ruby_glow_before_radius_px")).isDouble()) {
        base.rubyGlowBeforeRadiusPx = std::max(
            0, intValue(style, QStringLiteral("ruby_glow_before_radius_px"), 0)
        );
    } else if (rubyGlowCommon >= 0) {
        base.rubyGlowBeforeRadiusPx = rubyGlowCommon;
    }
    if (style.value(QStringLiteral("ruby_glow_after_radius_px")).isDouble()) {
        base.rubyGlowAfterRadiusPx = std::max(
            0, intValue(style, QStringLiteral("ruby_glow_after_radius_px"), 0)
        );
    } else if (rubyGlowCommon >= 0) {
        base.rubyGlowAfterRadiusPx = rubyGlowCommon;
    }
    if (style.value(QStringLiteral("ruby_glow_concentration_level")).isDouble()) {
        base.rubyGlowConcentrationLevel = std::clamp(
            intValue(style, QStringLiteral("ruby_glow_concentration_level"), 0), 0, 2
        );
    }
    cfg.lineYMarginPx = intValue(style, QStringLiteral("line_y_margin_px"), cfg.lineYMarginPx);
    cfg.layoutSemantics = stringValue(
        style, QStringLiteral("layout_semantics"), cfg.layoutSemantics
    );
    if (cfg.layoutSemantics != QStringLiteral("n3_1074")) {
        cfg.layoutSemantics = QStringLiteral("legacy");
    }
    cfg.lineGapPx = std::max(0, intValue(style, QStringLiteral("line_gap_px"), cfg.lineGapPx));
    cfg.lineLeadInMs = std::max(0, intValue(style, QStringLiteral("line_lead_in_ms"), cfg.lineLeadInMs));
    cfg.lineTailMs = std::max(0, intValue(style, QStringLiteral("line_tail_ms"), cfg.lineTailMs));
    cfg.lineProtectMs = std::max(0, intValue(style, QStringLiteral("line_protect_ms"), cfg.lineProtectMs));
    cfg.lineLaneGapMs = std::max(0, intValue(style, QStringLiteral("line_lane_gap_ms"), cfg.lineLaneGapMs));
    cfg.lineContinuitySnapMs = std::max(0, intValue(style, QStringLiteral("line_continuity_snap_ms"), cfg.lineContinuitySnapMs));
    cfg.linePairSecondDelayMs = std::max(0, intValue(style, QStringLiteral("line_pair_second_delay_ms"), cfg.linePairSecondDelayMs));
    cfg.lineMaxHoldMs = std::max(0, intValue(style, QStringLiteral("line_max_hold_ms"), cfg.lineMaxHoldMs));
    cfg.sectionGapMs = std::max(0, intValue(style, QStringLiteral("section_gap_ms"), cfg.sectionGapMs));
    cfg.lineYPosition = stringValue(style, QStringLiteral("line_y_position"), cfg.lineYPosition);
    cfg.lineHorizontalLayout = stringValue(style, QStringLiteral("line_horizontal_layout"), cfg.lineHorizontalLayout);
    cfg.sectionEndingMode = stringValue(style, QStringLiteral("section_ending_mode"), cfg.sectionEndingMode);
    cfg.syncEnding = style.value(QStringLiteral("sync_ending")).isBool()
        ? style.value(QStringLiteral("sync_ending")).toBool()
        : cfg.syncEnding;
    cfg.upperLineLeftMarginPx = intValue(style, QStringLiteral("upper_line_left_margin_px"), cfg.upperLineLeftMarginPx);
    cfg.lowerLineRightMarginPx = intValue(style, QStringLiteral("lower_line_right_margin_px"), cfg.lowerLineRightMarginPx);
    cfg.horizontalMarginPx = intValue(style, QStringLiteral("horizontal_margin_px"), cfg.horizontalMarginPx);
    cfg.smartHorizontal = stringValue(style, QStringLiteral("smart_horizontal"), cfg.smartHorizontal);
    const QJsonArray lineAlignments = style.value(QStringLiteral("line_alignments")).toArray();
    if (!lineAlignments.isEmpty()) {
        cfg.lineAlignments.clear();
        cfg.lineAlignments.reserve(static_cast<std::size_t>(lineAlignments.size()));
        for (const auto &alignment : lineAlignments) {
            if (alignment.isString()) {
                cfg.lineAlignments.push_back(alignment.toString());
            }
        }
    }
    cfg.dualLineLayout = style.value(QStringLiteral("dual_line_layout")).isBool()
        ? style.value(QStringLiteral("dual_line_layout")).toBool()
        : cfg.dualLineLayout;
    cfg.rightToLeft = style.value(QStringLiteral("right_to_left")).isBool()
        ? style.value(QStringLiteral("right_to_left")).toBool()
        : cfg.rightToLeft;
    cfg.vertical = style.value(QStringLiteral("vertical")).isBool()
        ? style.value(QStringLiteral("vertical")).toBool()
        : cfg.vertical;
    cfg.viewportScalePct = std::max(
        intValue(style, QStringLiteral("viewport_scale_pct"), cfg.viewportScalePct), 1
    );
    cfg.viewportRotationDeg = intValue(
        style, QStringLiteral("viewport_rotation_deg"), cfg.viewportRotationDeg
    );
    cfg.viewportOffsetX = intValue(
        style, QStringLiteral("viewport_offset_x"), cfg.viewportOffsetX
    );
    cfg.viewportOffsetY = intValue(
        style, QStringLiteral("viewport_offset_y"), cfg.viewportOffsetY
    );
    cfg.viewportAlign = stringValue(
        style, QStringLiteral("viewport_align"), cfg.viewportAlign
    );
    cfg.entryAnim = stringValue(style, QStringLiteral("entry_anim"), cfg.entryAnim);
    cfg.entryLeadMs = std::max(0, intValue(style, QStringLiteral("entry_lead_ms"), cfg.entryLeadMs));
    cfg.exitAnim = stringValue(style, QStringLiteral("exit_anim"), cfg.exitAnim);
    cfg.exitFadeMs = std::max(0, intValue(style, QStringLiteral("exit_fade_ms"), cfg.exitFadeMs));
    cfg.karaokeAnim = stringValue(
        style, QStringLiteral("karaoke_anim"), cfg.karaokeAnim
    );
    cfg.timingOffsetMs = intValue(style, QStringLiteral("timing_offset_ms"), cfg.timingOffsetMs);
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

    std::vector<QJsonObject> sourceTracks;
    sourceTracks.push_back(ir.value(QStringLiteral("track")).toObject());
    const QJsonArray extraTracks = ir.value(QStringLiteral("extra_tracks")).toArray();
    sourceTracks.reserve(1 + static_cast<std::size_t>(extraTracks.size()));
    for (const auto &trackValue : extraTracks) {
        if (trackValue.isObject()) {
            sourceTracks.push_back(trackValue.toObject());
        }
    }
    for (std::size_t sourceIndex = 0; sourceIndex < sourceTracks.size(); ++sourceIndex) {
        const QJsonObject &track = sourceTracks[sourceIndex];
        const int sourceOffsetMs = intValue(
            track.value(QStringLiteral("meta")).toObject(),
            QStringLiteral("offset_ms"),
            0
        );
        if (sourceIndex == 0) {
            cfg.primaryTrackOffsetMs = sourceOffsetMs;
        }
        const QJsonArray lines = track.value(QStringLiteral("lines")).toArray();
        for (int sourceLineIndex = 0; sourceLineIndex < lines.size(); ++sourceLineIndex) {
            const QJsonObject lineObject = lines.at(sourceLineIndex).toObject();
            TimingLine line;
            line.endMs = intValue(lineObject, QStringLiteral("end_ms"), 0);
            line.singerLabel = stringValue(lineObject, QStringLiteral("singer_label"));
            line.singerId = intValue(lineObject, QStringLiteral("singer_id"), -1);
            line.sourceIndex = static_cast<int>(sourceIndex);
            line.sourceLineIndex = sourceLineIndex;
            line.trackLineIndex = intValue(
                lineObject, QStringLiteral("track_line_index"), -1
            );
            line.pageIndex = intValue(lineObject, QStringLiteral("page_index"), -1);
            line.pageLineCount = std::max(
                0, intValue(lineObject, QStringLiteral("page_line_count"), 0)
            );
            // Default true: an IR from an older Python host has no stamp, and
            // the pre-change behavior (bars on every line) must survive that
            // pairing. New IRs always stamp the flag explicitly.
            line.signalHead = lineObject.value(
                QStringLiteral("signal_head")
            ).toBool(true);
            // Python 在源加载入口把整行时间戳严格逆序的行镜像理顺为顺序，
            // 并只在此处打标记；sidecar 据此对齐 Painter 的反向走字。
            line.wipeReverse = lineObject.value(
                QStringLiteral("wipe_reverse")
            ).toBool(false);
            line.sourceOffsetMs = sourceOffsetMs;
            line.lane = std::max(0, intValue(lineObject, QStringLiteral("lane"), 0));
            line.layoutOffsetX = lineObject.value(
                QStringLiteral("layout_offset_x")
            ).toDouble(0.0);
            line.layoutOffsetY = lineObject.value(
                QStringLiteral("layout_offset_y")
            ).toDouble(0.0);
            const QJsonArray placementWindows = lineObject.value(
                QStringLiteral("layout_offset_windows")
            ).toArray();
            line.placementWindows.reserve(
                static_cast<std::size_t>(placementWindows.size())
            );
            for (const QJsonValue &placementValue : placementWindows) {
                const QJsonObject placement = placementValue.toObject();
                const int startMs = intValue(
                    placement, QStringLiteral("start_ms"), 0
                );
                const int endMs = intValue(
                    placement, QStringLiteral("end_ms"), 0
                );
                if (endMs <= startMs) {
                    continue;
                }
                line.placementWindows.push_back(
                    krok::subtitle::native::PlacementWindow{
                        startMs,
                        endMs,
                        static_cast<float>(placement.value(
                            QStringLiteral("offset_x")
                        ).toDouble(0.0)),
                        static_cast<float>(placement.value(
                            QStringLiteral("offset_y")
                        ).toDouble(0.0)),
                    }
                );
            }
            if (lineObject.value(QStringLiteral("display_start_ms")).isDouble()) {
                line.displayStartMs = lineObject.value(
                    QStringLiteral("display_start_ms")
                ).toInt();
            }
            if (lineObject.value(QStringLiteral("display_end_ms")).isDouble()) {
                line.displayEndMs = lineObject.value(
                    QStringLiteral("display_end_ms")
                ).toInt();
            }
            line.centerOverride = lineObject.value(
                QStringLiteral("center_override")
            ).toBool(false);
            line.entryAnimation = stringValue(
                lineObject, QStringLiteral("entry_anim"), QStringLiteral("none")
            );
            line.entryDurationMs = std::max(
                0, intValue(lineObject, QStringLiteral("entry_duration_ms"), 0)
            );
            line.exitAnimation = stringValue(
                lineObject, QStringLiteral("exit_anim"), QStringLiteral("none")
            );
            line.exitDurationMs = std::max(
                0, intValue(lineObject, QStringLiteral("exit_duration_ms"), 0)
            );
            QString karaokeFallback = cfg.karaokeAnim;
            if (karaokeFallback == QStringLiteral("inherit")) {
                karaokeFallback = (
                    line.entryAnimation == QStringLiteral("utopia")
                    || line.exitAnimation == QStringLiteral("utopia")
                ) ? QStringLiteral("utopia") : QStringLiteral("none");
            }
            line.karaokeAnimation = stringValue(
                lineObject, QStringLiteral("karaoke_anim"), karaokeFallback
            );
            const QJsonObject layoutObject = lineObject.value(
                QStringLiteral("layout")
            ).toObject();
            if (!layoutObject.isEmpty()) {
                line.layout.present = true;
                line.layout.lineYPosition = stringValue(
                    layoutObject, QStringLiteral("line_y_position"),
                    line.layout.lineYPosition
                );
                line.layout.lineYMarginPx = intValue(
                    layoutObject, QStringLiteral("line_y_margin_px"),
                    line.layout.lineYMarginPx
                );
                line.layout.lineGapPx = intValue(
                    layoutObject, QStringLiteral("line_gap_px"),
                    line.layout.lineGapPx
                );
                line.layout.smartHorizontal = stringValue(
                    layoutObject, QStringLiteral("smart_horizontal"),
                    line.layout.smartHorizontal
                );
                line.layout.horizontalMarginPx = intValue(
                    layoutObject, QStringLiteral("horizontal_margin_px"),
                    line.layout.horizontalMarginPx
                );
                const QJsonArray layoutAlignments = layoutObject.value(
                    QStringLiteral("line_alignments")
                ).toArray();
                if (!layoutAlignments.isEmpty()) {
                    line.layout.lineAlignments.clear();
                    for (const QJsonValue &alignment : layoutAlignments) {
                        if (alignment.isString()) {
                            line.layout.lineAlignments.push_back(alignment.toString());
                        }
                    }
                }
                line.layout.dualLineLayout = layoutObject.value(
                    QStringLiteral("dual_line_layout")
                ).toBool(line.layout.dualLineLayout);
                line.layout.lineHorizontalLayout = stringValue(
                    layoutObject, QStringLiteral("line_horizontal_layout"),
                    line.layout.lineHorizontalLayout
                );
                line.layout.row1Align = stringValue(
                    layoutObject, QStringLiteral("row1_align"),
                    line.layout.row1Align
                );
                line.layout.row1OffsetX = intValue(
                    layoutObject, QStringLiteral("row1_offset_x"),
                    line.layout.row1OffsetX
                );
                line.layout.row1OffsetY = intValue(
                    layoutObject, QStringLiteral("row1_offset_y"),
                    line.layout.row1OffsetY
                );
                line.layout.row2Align = stringValue(
                    layoutObject, QStringLiteral("row2_align"),
                    line.layout.row2Align
                );
                line.layout.row2OffsetX = intValue(
                    layoutObject, QStringLiteral("row2_offset_x"),
                    line.layout.row2OffsetX
                );
                line.layout.row2OffsetY = intValue(
                    layoutObject, QStringLiteral("row2_offset_y"),
                    line.layout.row2OffsetY
                );
                line.layout.letterSpacingPx = intValue(
                    layoutObject, QStringLiteral("letter_spacing_px"),
                    line.layout.letterSpacingPx
                );
                line.layout.spaceWidthPercent = std::clamp(
                    intValue(
                        layoutObject, QStringLiteral("space_width_percent"),
                        line.layout.spaceWidthPercent
                    ),
                    10,
                    100
                );
                line.layout.allowBiting = layoutObject.value(
                    QStringLiteral("allow_biting")
                ).toBool(line.layout.allowBiting);
                line.layout.rubyIntervalPx = intValue(
                    layoutObject, QStringLiteral("ruby_interval_px"),
                    line.layout.rubyIntervalPx
                );
                line.layout.rubyAlignment = stringValue(
                    layoutObject, QStringLiteral("ruby_alignment"),
                    line.layout.rubyAlignment
                );
                line.layout.rubyGapPx = intValue(
                    layoutObject, QStringLiteral("ruby_gap_px"),
                    line.layout.rubyGapPx
                );
            }

            const QJsonArray chars = lineObject.value(QStringLiteral("chars")).toArray();
            line.chars.reserve(static_cast<std::size_t>(chars.size()));
            for (const auto &charValue : chars) {
                const QJsonObject charObject = charValue.toObject();
                TimingChar ch;
                ch.text = stringValue(charObject, QStringLiteral("text"));
                ch.startMs = intValue(charObject, QStringLiteral("start_ms"), 0);
                ch.explicitStart = charObject.value(
                    QStringLiteral("explicit_start")
                ).toBool(false);
                ch.explicitEnd = charObject.value(
                    QStringLiteral("explicit_end")
                ).toBool(false);
                if (charObject.value(QStringLiteral("pause_release_ms")).isDouble()) {
                    ch.pauseReleaseMs = charObject.value(QStringLiteral("pause_release_ms")).toInt();
                }
                ch.roleLabel = stringValue(charObject, QStringLiteral("role_label"));
                ch.vectorGlyph = resolveVectorGlyph(charObject);
                ch.bitmapGuide = parseBitmapGuide(
                    charObject.value(QStringLiteral("bitmap_guide"))
                );
                line.chars.push_back(ch);
            }
            const QJsonArray resolvedIntervals = lineObject.value(
                QStringLiteral("resolved_intervals")
            ).toArray();
            if (resolvedIntervals.size() == static_cast<int>(line.chars.size())) {
                for (int index = 0; index < resolvedIntervals.size(); ++index) {
                    const QJsonArray interval = resolvedIntervals.at(index).toArray();
                    if (interval.size() < 2
                        || !interval.at(0).isDouble()
                        || !interval.at(1).isDouble()) {
                        continue;
                    }
                    TimingChar &ch = line.chars[static_cast<std::size_t>(index)];
                    ch.startMs = interval.at(0).toInt(ch.startMs);
                    ch.resolvedEndMs = std::max(
                        ch.startMs, interval.at(1).toInt(ch.startMs)
                    );
                }
            }
            const QJsonArray guideAnchorBounds = lineObject.value(
                QStringLiteral("guide_anchor_bounds")
            ).toArray();
            if (guideAnchorBounds.size() == 2
                && guideAnchorBounds.at(0).isDouble()
                && guideAnchorBounds.at(1).isDouble()) {
                const double left = guideAnchorBounds.at(0).toDouble();
                const double right = guideAnchorBounds.at(1).toDouble();
                if (std::isfinite(left) && std::isfinite(right) && right > left) {
                    line.guideAnchorLeft = left;
                    line.guideAnchorRight = right;
                }
            }
            cfg.lines.push_back(std::move(line));
        }

        const QJsonArray rubies = track.value(QStringLiteral("rubies")).toArray();
        for (const auto &rubyValue : rubies) {
            const QJsonObject rubyObject = rubyValue.toObject();
            RubyAnnotation ruby;
            ruby.kanji = stringValue(rubyObject, QStringLiteral("kanji"));
            ruby.reading = stringValue(rubyObject, QStringLiteral("reading"));
            ruby.readingPartMs = parseIntArray(
                rubyObject.value(QStringLiteral("reading_part_ms")).toArray()
            );
            const QJsonArray readingParts = rubyObject.value(
                QStringLiteral("reading_parts")
            ).toArray();
            ruby.readingParts.reserve(static_cast<std::size_t>(readingParts.size()));
            for (const auto &part : readingParts) {
                if (part.isString()) {
                    ruby.readingParts.push_back(part.toString());
                }
            }
            ruby.posStartMs = intValue(rubyObject, QStringLiteral("pos_start_ms"), 0);
            ruby.posEndMs = intValue(rubyObject, QStringLiteral("pos_end_ms"), 0);
            ruby.targetLineIndex = intValue(
                rubyObject, QStringLiteral("target_line_index"), -1
            );
            ruby.targetCharStart = intValue(
                rubyObject, QStringLiteral("target_char_start"), -1
            );
            ruby.targetCharEnd = intValue(
                rubyObject, QStringLiteral("target_char_end"), -1
            );
            ruby.sourceIndex = static_cast<int>(sourceIndex);
            ruby.sourceOffsetMs = sourceOffsetMs;
            cfg.rubies.push_back(std::move(ruby));
        }
    }

    cfg.title = ir.value(QStringLiteral("title")).toObject();

    buildResolvedStyleCache(cfg);
    return cfg;
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

}  // namespace krok::subtitle::native::protocol

