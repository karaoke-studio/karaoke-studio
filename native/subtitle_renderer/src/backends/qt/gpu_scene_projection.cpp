#include "gpu_scene_projection.h"

#include "qt_character_animation.h"
#include "qt_display_plan.h"
#include "qt_font_factory.h"
#include "qt_line_layout.h"
#include "qt_ruby_target.h"
#include "qt_ruby_timing.h"
#include "qt_ruby_wipe.h"
#include "qt_style_metrics.h"
#include "../../protocol/json_value.h"
#include "../../protocol/render_config_parser.h"

#include <QtCore/QFileInfo>
#include <QtCore/QHash>
#include <QtCore/QJsonArray>
#include <QtGui/QColor>

#include <algorithm>
#include <cmath>
#include <optional>

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;
using protocol::RenderConfig;
using protocol::ResolvedLineLayout;
using protocol::ResolvedStyle;
using protocol::RubyAnnotation;
using protocol::TimingLine;
using protocol::resolvedStyleForCharacter;
using protocol::resolvedStyleForLine;
using protocol::resolvedStyleFromTitle;
using protocol::resolvedStyleKey;
using protocol::intValue;
using protocol::stringValue;

namespace {

krok::subtitle::native::RgbaColor gpuColor(const QString &value, const QString &fallback) {
    QColor color(value);
    if (!color.isValid()) {
        color = QColor(fallback);
    }
    return krok::subtitle::native::RgbaColor{
        static_cast<std::uint8_t>(color.red()),
        static_cast<std::uint8_t>(color.green()),
        static_cast<std::uint8_t>(color.blue()),
        static_cast<std::uint8_t>(color.alpha()),
    };
}

krok::subtitle::native::PaintStyle gpuPaint(
    const PaintFillSpec &source,
    const QString &fallback
) {
    using krok::subtitle::native::PaintStop;
    using krok::subtitle::native::PaintStyle;
    PaintStyle paint;
    paint.mode = source.mode.toStdString();
    paint.color = gpuColor(source.color, fallback);
    paint.imagePath = source.imagePath.toStdWString();
    paint.imageScale = static_cast<float>(
        std::clamp(source.imageScalePct, 1, 1000) / 100.0
    );
    if (!source.imagePath.isEmpty()) {
        const QFileInfo info(source.imagePath);
        if (info.exists() && info.isFile()) {
            paint.imageModifiedMs = static_cast<std::uint64_t>(
                std::max<qint64>(info.lastModified().toMSecsSinceEpoch(), 0)
            );
            paint.imageSize = static_cast<std::uint64_t>(
                std::max<qint64>(info.size(), 0)
            );
        }
    }
    const auto &sourceStops = source.mode == QStringLiteral("split_vertical")
        ? source.splitStops
        : source.gradientStops;
    paint.stops.reserve(sourceStops.size());
    for (const auto &[position, color] : sourceStops) {
        paint.stops.push_back(PaintStop{
            static_cast<float>(std::clamp(position / 100.0, 0.0, 1.0)),
            gpuColor(color, source.color),
        });
    }
    if (paint.stops.empty()) {
        if (source.mode == QStringLiteral("split_vertical")) {
            paint.stops = {
                {0.0f, gpuColor(source.splitTopColor, source.color)},
                {
                    static_cast<float>(std::clamp(
                        source.splitPositionPct / 100.0, 0.0, 1.0
                    )),
                    gpuColor(source.splitBottomColor, source.color),
                },
                {1.0f, gpuColor(source.splitBottomColor, source.color)},
            };
        } else {
            paint.stops = {
                {0.0f, gpuColor(source.startColor, source.color)},
                {1.0f, gpuColor(source.endColor, source.color)},
            };
        }
    }
    return paint;
}

void applyGpuResolvedStyle(
    krok::subtitle::native::TextStyle &target,
    const ResolvedStyle &source,
    double scale
) {
    target.fontFamily = source.fontFamily.toStdWString();
    target.latinFontFamily = source.fontFamilyLatin.isEmpty()
        ? std::nullopt
        : std::optional<std::wstring>(source.fontFamilyLatin.toStdWString());
    target.fontSize = static_cast<float>(source.fontSizePx * scale);
    target.latinFontSize = source.latinFontSizePx.has_value()
        ? std::optional<float>(static_cast<float>(*source.latinFontSizePx * scale))
        : std::nullopt;
    target.fontWeight = source.fontWeight;
    target.latinFontWeight = source.latinFontWeight;
    target.italic = source.italic;
    target.allowBiting = source.allowBiting;
    target.affectsRubyAnchor = source.affectsRubyAnchor;
    target.spaceWidthPercent = source.spaceWidthPercent;
    target.letterSpacing = static_cast<float>(source.letterSpacingPx * scale);
    target.beforeFill = gpuColor(source.baseFill.color, source.baseColor);
    target.afterFill = gpuColor(source.afterFill.color, source.fillColor);
    target.beforeStroke = gpuColor(source.beforeStrokeFill.color, source.beforeStrokeColor);
    target.afterStroke = gpuColor(source.afterStrokeFill.color, source.afterStrokeColor);
    target.beforeStroke2 = gpuColor(source.beforeStroke2Fill.color, source.beforeStroke2Color);
    target.afterStroke2 = gpuColor(source.afterStroke2Fill.color, source.afterStroke2Color);
    target.beforeDecor = gpuColor(source.beforeShadowFill.color, source.beforeShadowColor);
    target.afterDecor = gpuColor(source.afterShadowFill.color, source.afterShadowColor);
    target.beforeFillPaint = gpuPaint(source.baseFill, source.baseColor);
    target.afterFillPaint = gpuPaint(source.afterFill, source.fillColor);
    target.beforeStrokePaint = gpuPaint(source.beforeStrokeFill, source.beforeStrokeColor);
    target.afterStrokePaint = gpuPaint(source.afterStrokeFill, source.afterStrokeColor);
    target.beforeStroke2Paint = gpuPaint(source.beforeStroke2Fill, source.beforeStroke2Color);
    target.afterStroke2Paint = gpuPaint(source.afterStroke2Fill, source.afterStroke2Color);
    target.beforeDecorPaint = gpuPaint(source.beforeShadowFill, source.beforeShadowColor);
    target.afterDecorPaint = gpuPaint(source.afterShadowFill, source.afterShadowColor);
    target.strokeWidth = static_cast<float>(source.strokeWidthPx * scale);
    target.stroke2Width = static_cast<float>(source.stroke2WidthPx * scale);
    target.decorationKind = source.decorationKind.toStdString();
    target.glowBeforeRadius = static_cast<float>(source.glowBeforeRadiusPx * scale);
    target.glowAfterRadius = static_cast<float>(source.glowAfterRadiusPx * scale);
    target.glowConcentrationLevel = source.glowConcentrationLevel;
    target.shadowOffsetX = static_cast<float>(source.shadowOffsetX * scale);
    target.shadowOffsetY = static_cast<float>(source.shadowOffsetY * scale);

    const bool rubyUsesMainFont = source.rubyFontFollowMain
        && source.rubyFontFamily.isEmpty()
        && source.rubyFontFamilyLatin.isEmpty()
        && !source.rubyFontWeight.has_value()
        && !source.rubyLatinFontSizePx.has_value()
        && !source.rubyLatinFontWeight.has_value()
        && source.rubyFontSizePx == 45;
    target.rubyFontFamily = (
        rubyUsesMainFont || source.rubyFontFamily.isEmpty()
            ? source.fontFamily
            : source.rubyFontFamily
    ).toStdWString();
    const QString rubyLatinFamily = source.rubyFontFamilyLatin.isEmpty()
        ? (source.fontFamilyLatin.isEmpty()
            ? QString::fromStdWString(target.rubyFontFamily)
            : source.fontFamilyLatin)
        : source.rubyFontFamilyLatin;
    target.rubyLatinFontFamily = rubyLatinFamily.isEmpty()
        ? std::nullopt
        : std::optional<std::wstring>(rubyLatinFamily.toStdWString());
    target.rubyFontSize = static_cast<float>(source.rubyFontSizePx * scale);
    target.rubyLatinFontSize = source.rubyLatinFontSizePx.has_value()
        ? std::optional<float>(static_cast<float>(*source.rubyLatinFontSizePx * scale))
        : std::nullopt;
    target.rubyFontWeight = rubyUsesMainFont
        ? source.fontWeight
        : source.rubyFontWeight.value_or(source.fontWeight);
    target.rubyLatinFontWeight = source.rubyLatinFontWeight;
    target.rubyGap = static_cast<float>(source.rubyGapPx * scale);
    target.rubyInterval = static_cast<float>(source.rubyIntervalPx * scale);
    target.rubyAlignment = source.rubyAlignment.toStdString();
    target.rubyMainProgressMode = source.rubyMainProgressMode.toStdString();
    target.rubyHorizontalGradientWithMain = source.rubyHorizontalGradientWithMain;
    target.rubyBeforeFill = gpuColor(source.rubyBaseFill.color, source.rubyBaseColor);
    target.rubyAfterFill = gpuColor(source.rubyAfterFill.color, source.rubyFillColor);
    target.rubyBeforeStroke = gpuColor(
        source.rubyBeforeStrokeFill.color, source.rubyBeforeStrokeColor
    );
    target.rubyAfterStroke = gpuColor(
        source.rubyAfterStrokeFill.color, source.rubyAfterStrokeColor
    );
    target.rubyBeforeStroke2 = gpuColor(
        source.rubyBeforeStroke2Fill.color, source.rubyBeforeStroke2Color
    );
    target.rubyAfterStroke2 = gpuColor(
        source.rubyAfterStroke2Fill.color, source.rubyAfterStroke2Color
    );
    target.rubyBeforeDecor = gpuColor(
        source.rubyBeforeShadowFill.color, source.rubyBeforeShadowColor
    );
    target.rubyAfterDecor = gpuColor(
        source.rubyAfterShadowFill.color, source.rubyAfterShadowColor
    );
    target.rubyBeforeFillPaint = gpuPaint(source.rubyBaseFill, source.rubyBaseColor);
    target.rubyAfterFillPaint = gpuPaint(source.rubyAfterFill, source.rubyFillColor);
    target.rubyBeforeStrokePaint = gpuPaint(
        source.rubyBeforeStrokeFill, source.rubyBeforeStrokeColor
    );
    target.rubyAfterStrokePaint = gpuPaint(
        source.rubyAfterStrokeFill, source.rubyAfterStrokeColor
    );
    target.rubyBeforeStroke2Paint = gpuPaint(
        source.rubyBeforeStroke2Fill, source.rubyBeforeStroke2Color
    );
    target.rubyAfterStroke2Paint = gpuPaint(
        source.rubyAfterStroke2Fill, source.rubyAfterStroke2Color
    );
    target.rubyBeforeDecorPaint = gpuPaint(
        source.rubyBeforeShadowFill, source.rubyBeforeShadowColor
    );
    target.rubyAfterDecorPaint = gpuPaint(
        source.rubyAfterShadowFill, source.rubyAfterShadowColor
    );
    // Ruby stroke/decoration/glow that a scheme left unset follow this scheme's
    // effective main text (same derivation the base parser used to bake from the
    // global main). Resolving here — against the role-resolved ``source`` — is
    // what keeps each role's ruby tied to its own main instead of the global.
    const double rubyScale = static_cast<double>(source.rubyFontSizePx)
        / static_cast<double>(std::max(source.fontSizePx, 1));
    auto scaledFromMain = [&](int mainPx) {
        return std::max(0, static_cast<int>(std::lround(mainPx * rubyScale)));
    };
    const int rubyStrokePx = source.rubyStrokeWidthPx.value_or(
        scaledFromMain(source.strokeWidthPx)
    );
    // Same split as the CPU painter's _ruby_stroke2_enabled/_ruby_stroke2_width_value:
    // the flag and the width inherit along separate chains, and the flag gates
    // the width exactly once at the end.  Letting the width answer first made a
    // saved ruby width draw stroke2 even though the main text had it switched
    // off; gating the inherited width a second time made an explicitly enabled
    // ruby with no width of its own collapse to 0.
    const bool rubyStroke2On = source.rubyStroke2Enabled.value_or(source.stroke2Enabled);
    const int rubyStroke2Px = rubyStroke2On
        ? source.rubyStroke2WidthPx.value_or(scaledFromMain(source.stroke2RawWidthPx))
        : 0;
    target.rubyStrokeWidth = static_cast<float>(rubyStrokePx * scale);
    target.rubyStroke2Width = static_cast<float>(rubyStroke2Px * scale);
    target.rubyDecorationKind = (
        source.rubyDecorationKind.isEmpty()
            ? source.decorationKind
            : source.rubyDecorationKind
    ).toStdString();
    target.rubyGlowBeforeRadius = static_cast<float>(
        source.rubyGlowBeforeRadiusPx.value_or(scaledFromMain(source.glowBeforeRadiusPx))
        * scale
    );
    target.rubyGlowAfterRadius = static_cast<float>(
        source.rubyGlowAfterRadiusPx.value_or(scaledFromMain(source.glowAfterRadiusPx))
        * scale
    );
    target.rubyGlowConcentrationLevel = source.rubyGlowConcentrationLevel.value_or(
        source.glowConcentrationLevel
    );
    target.rubyShadowOffsetX = static_cast<float>(
        source.rubyShadowOffsetX.value_or(source.shadowOffsetX) * scale
    );
    target.rubyShadowOffsetY = static_cast<float>(
        source.rubyShadowOffsetY.value_or(source.shadowOffsetY) * scale
    );
    target.litEnabled = source.litEnabled;
    target.litStyle = source.litStyle.toStdString();
    target.litNumber = source.litNumber;
    target.litSize = static_cast<float>(source.litSize * scale);
    target.litOffsetX = static_cast<float>(source.litOffsetX * scale);
    target.litOffsetY = static_cast<float>(source.litOffsetY * scale);
    target.litTracking = static_cast<float>(source.litTracking * scale);
    target.litFill = gpuColor(source.litFillColor, QStringLiteral("#0000FF"));
    target.litStroke = gpuColor(source.litStrokeColor, QStringLiteral("#FFFFFF"));
    target.litStrokeWidth = static_cast<float>(source.litStrokeWidth * scale);
    target.litStrokeSoften = static_cast<float>(source.litStrokeSoften * scale);
    target.litOpacity = static_cast<float>(source.litOpacityPct) / 100.0f;
    target.litEdgeBrightness = static_cast<float>(source.litEdgeBrightnessPct) / 100.0f;
    target.litShadow = source.litShadow;
    target.litTimeOffsetMs = source.litTimeOffsetMs;
    target.litWaitingTimeMs = source.litWaitingTimeMs;
    target.litTransitionMode = source.litTransitionMode.toStdString();
    target.litTransitionRatioPct = source.litTransitionRatioPct;
    target.litTransitionAngleDeg = static_cast<float>(source.litTransitionAngleDeg);
    target.litTransitionDistance = static_cast<float>(source.litTransitionDistance * scale);
    target.signalsDurationMs = source.signalsDurationMs;
    target.volumeSize = static_cast<float>(source.volumeSize * scale);
    target.volumeOffsetX = static_cast<float>(source.volumeOffsetX * scale);
    target.volumeOffsetY = static_cast<float>(source.volumeOffsetY * scale);
    target.volumeColumnWidth = static_cast<float>(source.volumeColumnWidth * scale);
    target.volumeColumnCount = source.volumeColumnCount;
    target.volumeColumnSpacing = static_cast<float>(source.volumeColumnSpacing * scale);
    target.volumeAlign = source.volumeAlign;
    target.volumeRatio = static_cast<float>(source.volumeRatio);
    target.volumeFill = gpuColor(source.volumeFillColor, QStringLiteral("#FFFFFF"));
    target.volumeStroke = gpuColor(source.volumeStrokeColor, QStringLiteral("#0000FF"));
    target.volumeOverlayFill = gpuColor(source.volumeOverlayFillColor, QStringLiteral("#0000FF"));
    target.volumeOverlayStroke = gpuColor(source.volumeOverlayStrokeColor, QStringLiteral("#FFFFFF"));
    target.volumeFlashTimes = source.volumeFlashTimes;
    target.volumeFlashDurationRatio = static_cast<float>(source.volumeFlashDurationRatio);
    target.volumeTransitionRatioPct = source.volumeTransitionRatioPct;
}

// N3 CalcHorizontalAlignment: Top/Middle count forward from the page's first
// line, Bottom counts backward from its last one.  The two agree on a full
// page and diverge on a short one, where Bottom takes the tail of the list --
// a 2-line page under [left, center, right] is "center + right".
int alignmentIndexForLane(
    int lane,
    int alignmentCount,
    int pageLineCount,
    const QString &verticalPosition
) {
    if (alignmentCount <= 0) {
        return 0;
    }
    int index = std::max(lane, 0);
    if (verticalPosition == QStringLiteral("bottom")
        && pageLineCount > 0
        && pageLineCount < alignmentCount) {
        index = std::max(alignmentCount - pageLineCount + index, 0);
    }
    return std::clamp(index, 0, alignmentCount - 1);
}

void applyGpuLineLayout(
    krok::subtitle::native::TextStyle &target,
    const ResolvedLineLayout &layout,
    int lane,
    bool centerOverride,
    int pageLineCount,
    double scale
) {
    if (!layout.present) {
        return;
    }
    target.bottomMargin = static_cast<float>(layout.lineYMarginPx * scale);
    target.lineGap = static_cast<float>(layout.lineGapPx * scale);
    target.dualLineLayout = layout.dualLineLayout;
    target.laneCount = layout.dualLineLayout
        ? std::max(static_cast<int>(layout.lineAlignments.size()), 1)
        : 1;
    target.verticalPosition = layout.lineYPosition.toStdString();
    target.smartHorizontal = layout.lineHorizontalLayout == QStringLiteral("asymmetric")
        ? layout.smartHorizontal.toStdString()
        : "none";
    target.letterSpacing = static_cast<float>(layout.letterSpacingPx * scale);
    target.spaceWidthPercent = layout.spaceWidthPercent;
    target.allowBiting = layout.allowBiting;
    target.rubyInterval = static_cast<float>(layout.rubyIntervalPx * scale);
    target.rubyAlignment = layout.rubyAlignment.toStdString();
    target.rubyGap = static_cast<float>(layout.rubyGapPx * scale);
    target.layoutOffsetX = 0.0f;
    target.layoutOffsetY = 0.0f;

    if (centerOverride || layout.lineHorizontalLayout == QStringLiteral("center")) {
        target.alignment = "center";
    } else if (layout.lineHorizontalLayout == QStringLiteral("per_row")) {
        const bool secondRow = lane == 1;
        target.alignment = (
            secondRow ? layout.row2Align : layout.row1Align
        ).toStdString();
        target.horizontalMargin = 0.0f;
        target.layoutOffsetX = static_cast<float>(
            (secondRow ? layout.row2OffsetX : layout.row1OffsetX) * scale
        );
    } else if (!layout.lineAlignments.empty()) {
        const int alignmentIndex = alignmentIndexForLane(
            lane,
            static_cast<int>(layout.lineAlignments.size()),
            pageLineCount,
            layout.lineYPosition
        );
        target.alignment = layout.lineAlignments[
            static_cast<std::size_t>(alignmentIndex)
        ].toStdString();
        target.horizontalMargin = static_cast<float>(
            layout.horizontalMarginPx * scale
        );
    }

    if (layout.lineHorizontalLayout == QStringLiteral("per_row")) {
        if (lane == 0) {
            target.layoutOffsetY = static_cast<float>(layout.row1OffsetY * scale);
        } else if (lane == 1) {
            target.layoutOffsetY = static_cast<float>(layout.row2OffsetY * scale);
        }
    }
}
}  // namespace

krok::subtitle::native::RenderScene gpuSceneFromConfig(const RenderConfig &config) {
    using krok::subtitle::native::RenderScene;
    using krok::subtitle::native::TextChar;
    using krok::subtitle::native::TextLine;
    using krok::subtitle::native::TextStyle;

    const double scale = std::max(config.dpr, 0.01);
    const ResolvedStyle &sourceStyle = config.baseStyle;
    RenderScene scene;
    scene.width = config.physicalWidth();
    scene.height = config.physicalHeight();
    scene.layoutReferenceScale = static_cast<float>(scale);
    scene.viewportScale = static_cast<float>(config.viewportScalePct) / 100.0f;
    scene.viewportRotation = static_cast<float>(config.viewportRotationDeg);
    scene.viewportOffsetX = static_cast<float>(config.viewportOffsetX * scale);
    scene.viewportOffsetY = static_cast<float>(config.viewportOffsetY * scale);
    scene.viewportAlign = config.viewportAlign.toStdString();
    applyGpuResolvedStyle(scene.style, sourceStyle, scale);
    scene.style.layoutSemantics = config.layoutSemantics.toStdString();
    scene.style.smartHorizontal = config.lineHorizontalLayout == QStringLiteral("asymmetric")
        ? config.smartHorizontal.toStdString()
        : "none";
    scene.style.horizontalMargin = static_cast<float>(config.horizontalMarginPx * scale);
    scene.style.bottomMargin = static_cast<float>(config.lineYMarginPx * scale);
    scene.style.lineGap = static_cast<float>(config.lineGapPx * scale);
    scene.style.dualLineLayout = config.dualLineLayout;
    scene.style.laneCount = config.dualLineLayout
        ? std::max(static_cast<int>(config.lineAlignments.size()), 1)
        : 1;
    scene.style.verticalPosition = config.lineYPosition.toStdString();
    scene.style.vertical = config.vertical;
    scene.style.rightToLeft = config.rightToLeft;
    scene.style.leadInMs = config.lineLeadInMs;
    scene.style.tailMs = config.lineTailMs;
    if (config.lineHorizontalLayout == QStringLiteral("center")) {
        scene.style.alignment = "center";
    } else if (!config.lineAlignments.empty()) {
        scene.style.alignment = config.lineAlignments.front().toStdString();
    } else {
        scene.style.alignment = "left";
    }
    scene.lines.reserve(config.lines.size());
    scene.lineStyles.reserve(config.lines.size());
    QHash<QString, int> charStyleIndices;
    for (const TimingLine &sourceLine : config.lines) {
        if (sourceLine.chars.empty()) {
            continue;
        }
        TextStyle lineStyle = scene.style;
        applyGpuResolvedStyle(
            lineStyle, resolvedStyleForLine(config, sourceLine), scale
        );
        if (sourceLine.layout.present) {
            applyGpuLineLayout(
                lineStyle, sourceLine.layout, sourceLine.lane,
                sourceLine.centerOverride, sourceLine.pageLineCount, scale
            );
        } else {
            lineStyle.horizontalMargin = static_cast<float>(
                config.horizontalMarginPx * scale
            );
            if (sourceLine.centerOverride
                || config.lineHorizontalLayout == QStringLiteral("center")) {
                lineStyle.alignment = "center";
            } else if (!config.lineAlignments.empty()) {
                const int alignmentIndex = alignmentIndexForLane(
                    sourceLine.lane,
                    static_cast<int>(config.lineAlignments.size()),
                    sourceLine.pageLineCount,
                    config.lineYPosition
                );
                lineStyle.alignment = config.lineAlignments[
                    static_cast<std::size_t>(alignmentIndex)
                ].toStdString();
            }
        }
        lineStyle.layoutOffsetX += static_cast<float>(
            sourceLine.layoutOffsetX * scale
        );
        lineStyle.layoutOffsetY += static_cast<float>(
            sourceLine.layoutOffsetY * scale
        );
        scene.lineStyles.push_back(std::move(lineStyle));
        TextLine line;
        const int sourceTimingOffset = config.timingOffsetMs + sourceLine.sourceOffsetMs;
        line.startMs = lineStartMs(sourceLine) + sourceTimingOffset;
        line.endMs = lineEndMs(sourceLine) + sourceTimingOffset;
        line.sourceIndex = sourceLine.sourceIndex;
        line.sourceLineIndex = sourceLine.sourceLineIndex;
        line.pageIndex = sourceLine.pageIndex;
        line.lane = sourceLine.lane;
        line.signalHead = sourceLine.signalHead;
        line.wipeReverse = sourceLine.wipeReverse;
        line.centerOverride = sourceLine.centerOverride;
        // 标题钉在最下层（compositeOrder = kTitleCompositeOrder），所以源之间不必
        // 再为它预留 1 号槽位：主字幕 0，副源依次 1、2……
        line.compositeOrder = sourceLine.sourceIndex;
        if (sourceLine.guideAnchorLeft.has_value()
            && sourceLine.guideAnchorRight.has_value()) {
            line.guideAnchorLeft = static_cast<float>(
                *sourceLine.guideAnchorLeft * scale
            );
            line.guideAnchorRight = static_cast<float>(
                *sourceLine.guideAnchorRight * scale
            );
        }
        const auto verticalCharacterAnimation = [&](const QString &animation) {
            return config.vertical && (
                animation == QStringLiteral("char_fade")
                || animation == QStringLiteral("char_drip")
                || animation == QStringLiteral("spin_flip")
                || animation == QStringLiteral("utopia")
            );
        };
        line.entryAnimation = verticalCharacterAnimation(sourceLine.entryAnimation)
            ? "none"
            : sourceLine.entryAnimation.toStdString();
        line.entryDurationMs = verticalCharacterAnimation(sourceLine.entryAnimation)
            ? 0
            : sourceLine.entryDurationMs;
        line.exitAnimation = verticalCharacterAnimation(sourceLine.exitAnimation)
            ? "none"
            : sourceLine.exitAnimation.toStdString();
        line.exitDurationMs = verticalCharacterAnimation(sourceLine.exitAnimation)
            ? 0
            : sourceLine.exitDurationMs;
        line.karaokeAnimation = config.vertical
            && sourceLine.karaokeAnimation != QStringLiteral("no_wipe")
            ? "none"
            : sourceLine.karaokeAnimation.toStdString();
        if (sourceLine.displayStartMs.has_value()
            && sourceLine.displayEndMs.has_value()) {
            line.displayWindows.push_back(krok::subtitle::native::DisplayWindow{
                *sourceLine.displayStartMs + sourceTimingOffset,
                *sourceLine.displayEndMs + sourceTimingOffset,
            });
        }
        line.placementWindows.reserve(sourceLine.placementWindows.size());
        for (const auto &window : sourceLine.placementWindows) {
            line.placementWindows.push_back(
                krok::subtitle::native::PlacementWindow{
                    window.startMs + sourceTimingOffset,
                    window.endMs + sourceTimingOffset,
                    window.offsetX * static_cast<float>(scale),
                    window.offsetY * static_cast<float>(scale),
                }
            );
        }
        line.chars.reserve(sourceLine.chars.size());
        for (std::size_t index = 0; index < sourceLine.chars.size(); ++index) {
            int styleIndex = -1;
            // Painter's vertical path currently uses the resolved line style
            // for every glyph; inline role styles are a horizontal-only
            // contract until the CPU oracle itself gains vertical runs.
            if (!config.vertical && !sourceLine.chars[index].roleLabel.isEmpty()) {
                QString key = resolvedStyleKey(
                    sourceLine.singerId, sourceLine.chars[index].roleLabel
                );
                if (sourceLine.layout.present) {
                    key += QStringLiteral("|layout:%1:%2:%3:%4:%5:%6")
                        .arg(sourceLine.layout.letterSpacingPx)
                        .arg(sourceLine.layout.spaceWidthPercent)
                        .arg(sourceLine.layout.allowBiting ? 1 : 0)
                        .arg(sourceLine.layout.rubyIntervalPx)
                        .arg(sourceLine.layout.rubyAlignment)
                        .arg(sourceLine.layout.rubyGapPx);
                }
                const auto existing = charStyleIndices.constFind(key);
                if (existing != charStyleIndices.constEnd()) {
                    styleIndex = existing.value();
                } else {
                    TextStyle charStyle = scene.lineStyles.back();
                    applyGpuResolvedStyle(
                        charStyle,
                        resolvedStyleForCharacter(config, sourceLine, sourceLine.chars[index]),
                        scale
                    );
                    applyGpuLineLayout(
                        charStyle, sourceLine.layout, sourceLine.lane,
                        sourceLine.centerOverride, sourceLine.pageLineCount, scale
                    );
                    charStyle.layoutOffsetX += static_cast<float>(
                        sourceLine.layoutOffsetX * scale
                    );
                    charStyle.layoutOffsetY += static_cast<float>(
                        sourceLine.layoutOffsetY * scale
                    );
                    styleIndex = static_cast<int>(scene.charStyles.size());
                    scene.charStyles.push_back(std::move(charStyle));
                    charStyleIndices.insert(key, styleIndex);
                }
            }
            krok::subtitle::native::TextChar sceneChar{
                sourceLine.chars[index].text.toStdWString(),
                sourceLine.chars[index].startMs + sourceTimingOffset,
                charEndMs(sourceLine, index) + sourceTimingOffset,
                styleIndex,
                sourceLine.chars[index].vectorGlyph,
                sourceLine.chars[index].bitmapGuide,
            };
            if (sceneChar.bitmapGuide.has_value()) {
                // 动图锚点与 displayWindows 同口径：IR 侧是 track 时间，
                // 渲染 tMs 含 timingOffset / sourceOffset，这里补齐偏移，
                // 与 Python painter 的有效时间换算保持一致。
                sceneChar.bitmapGuide->animAnchorMs += sourceTimingOffset;
            }
            line.chars.push_back(std::move(sceneChar));
            line.chars.back().wipePoints = {
                krok::subtitle::native::WipePoint{line.chars.back().startMs, 0.0f},
                krok::subtitle::native::WipePoint{line.chars.back().endMs, 1.0f},
            };
        }
        if (sourceLine.wipeReverse) {
            // Reverse-wipe compatibility is deliberately limited to main text.
            // Ruby has an independent per-reading clock and is omitted until
            // that clock has a verified reverse-normalization contract.
            scene.lines.push_back(std::move(line));
            continue;
        }
        const auto intervals = lineIntervals(sourceLine);
        const int sourceLineStart = lineStartMs(sourceLine);
        const int sourceLineEnd = lineEndMs(sourceLine);
        std::vector<bool> rubyMainWipeAssigned(line.chars.size(), false);
        for (const RubyAnnotation &sourceRuby : config.rubies) {
            const bool globalPosition = sourceRuby.posStartMs == 0 && sourceRuby.posEndMs == 0;
            if (!globalPosition && (
                sourceRuby.posEndMs <= sourceLineStart || sourceRuby.posStartMs >= sourceLineEnd
            )) {
                continue;
            }
            const auto targetIndices = rubyTargetIndices(sourceRuby, sourceLine, intervals);
            if (targetIndices.empty()) {
                continue;
            }
            const auto [minimum, maximum] = std::minmax_element(
                targetIndices.begin(), targetIndices.end()
            );
            if (*minimum < 0 || *maximum >= static_cast<int>(sourceLine.chars.size())) {
                continue;
            }
            const RubyAnnotation ruby = effectiveRubyForTarget(
                sourceRuby, targetIndices, intervals
            );
            krok::subtitle::native::TextRuby sceneRuby;
            sceneRuby.baseText = ruby.kanji.toStdWString();
            sceneRuby.reading = ruby.reading.toStdWString();
            sceneRuby.firstCharIndex = *minimum;
            sceneRuby.lastCharIndex = *maximum;
            sceneRuby.startMs = ruby.posStartMs + sourceTimingOffset;
            sceneRuby.endMs = ruby.posEndMs + sourceTimingOffset;
            const bool mainWipeAlreadyAssigned = std::any_of(
                targetIndices.begin(), targetIndices.end(),
                [&](int targetIndex) {
                    return targetIndex >= 0
                        && targetIndex < static_cast<int>(rubyMainWipeAssigned.size())
                        && rubyMainWipeAssigned[static_cast<std::size_t>(targetIndex)];
                }
            );
            if (!mainWipeAlreadyAssigned && applyRubyMainWipeProjection(
                line,
                sourceLine,
                ruby,
                targetIndices,
                scene.lineStyles.back().rubyMainProgressMode,
                sourceTimingOffset
            )) {
                for (int targetIndex : targetIndices) {
                    if (targetIndex >= 0
                        && targetIndex < static_cast<int>(rubyMainWipeAssigned.size())) {
                        rubyMainWipeAssigned[static_cast<std::size_t>(targetIndex)] = true;
                    }
                }
            }
            for (int targetIndex : targetIndices) {
                if (targetIndex < 0
                    || targetIndex >= static_cast<int>(sourceLine.chars.size())
                    || sourceLine.chars[static_cast<std::size_t>(targetIndex)].roleLabel.isEmpty()) {
                    continue;
                }
                sceneRuby.styleIndex = line.chars[
                    static_cast<std::size_t>(targetIndex)
                ].styleIndex;
                break;
            }
            for (const auto &unit : rubyUtopiaReadingUnitsAndIntervals(ruby)) {
                sceneRuby.units.push_back(krok::subtitle::native::RubyUnit{
                    unit.first.normalized(QString::NormalizationForm_C).toStdWString(),
                    unit.second.first + sourceTimingOffset,
                    unit.second.second + sourceTimingOffset,
                });
            }
            if (!sceneRuby.units.empty()) {
                line.rubies.push_back(std::move(sceneRuby));
            }
        }
        scene.lines.push_back(std::move(line));
    }

    // Multi-title: every entry projects into the shared TextLine pipeline with
    // its own windows/styles.  Entry z-order = list order: compositeOrder
    // decreases from kTitleCompositeOrder so title[0] stays lowest and all
    // titles remain below the lyrics (line sort is ascending by compositeOrder).
    int titleIndex = 0;
    for (const auto &titleValue : config.titles) {
        const QJsonObject title = titleValue.toObject();
        ++titleIndex;
        if (title.isEmpty()
            || !title.value(QStringLiteral("enabled")).toBool(false)) {
            continue;
        }
        const QString text = stringValue(title, QStringLiteral("text"));
        const QStringList rows = text.split(u'\n', Qt::KeepEmptyParts);
        std::vector<krok::subtitle::native::DisplayWindow> windows;
        const int defaultFadeInMs = std::max(
            0, intValue(title, QStringLiteral("fade_in_ms"), 0)
        );
        const int defaultFadeOutMs = std::max(
            0, intValue(title, QStringLiteral("fade_out_ms"), 0)
        );
        for (const auto &windowValue : title.value(
                 QStringLiteral("windows")
             ).toArray()) {
            const QJsonArray window = windowValue.toArray();
            if (window.size() < 2) {
                continue;
            }
            // Title windows are already resolved on the project/media
            // timeline. Lyrics timing and primary-track offsets must not move
            // the opening or ending title.
            const int start = window.at(0).toInt();
            const int end = window.at(1).toInt();
            const int fadeInMs = window.size() > 2
                ? std::max(0, window.at(2).toInt())
                : defaultFadeInMs;
            const int fadeOutMs = window.size() > 3
                ? std::max(0, window.at(3).toInt())
                : defaultFadeOutMs;
            if (end > start) {
                windows.push_back({start, end, fadeInMs, fadeOutMs});
            }
        }
        if (windows.empty() || !std::any_of(
                rows.begin(), rows.end(), [](const QString &row) {
                    return !row.trimmed().isEmpty();
                }
            )) {
            continue;
        }
        TextStyle titleStyle;
        applyGpuResolvedStyle(
            titleStyle,
            resolvedStyleFromTitle(sourceStyle, title),
            scale
        );
        // The title always uses N3 char-box geometry, independent of the
        // project's layout semantics: box height = font size + edge with the
        // baseline split by the face's A:D ratio.  Qt/DWrite ascent carries
        // the em's internal leading, which would leave the top margin
        // visibly larger than the side margins for the same number.  Mirrors
        // Painter's _layout_title_overlay.
        titleStyle.layoutSemantics = "n3_1074";
        titleStyle.lineGap = static_cast<float>(std::max(
            0, intValue(title, QStringLiteral("line_gap_px"), 0)
        ) * scale);
        titleStyle.dualLineLayout = rows.size() > 1;
        titleStyle.laneCount = std::max(static_cast<int>(rows.size()), 1);
        titleStyle.leadInMs = 0;
        titleStyle.tailMs = 0;
        const QString anchor = stringValue(
            title, QStringLiteral("anchor"), QStringLiteral("top_left")
        );
        const float offsetX = static_cast<float>(
            intValue(title, QStringLiteral("offset_x"), 0) * scale
        );
        const float offsetY = static_cast<float>(
            intValue(title, QStringLiteral("offset_y"), 0) * scale
        );
        // N3 counts half the edge inside the char box on every side, so an
        // edge-anchored title keeps its stroke inside the margin.  The
        // vertical half is already part of the N3 box height; the horizontal
        // one has to be folded into the margin here, exactly as Painter does
        // in _title_block_origin.
        const float titleHalfEdge = std::max(titleStyle.strokeWidth, 0.0f) * 0.5f;
        if (anchor.endsWith(QStringLiteral("left"))) {
            titleStyle.alignment = "left";
            titleStyle.horizontalMargin = offsetX + titleHalfEdge;
        } else if (anchor.endsWith(QStringLiteral("right"))) {
            titleStyle.alignment = "right";
            titleStyle.horizontalMargin = offsetX + titleHalfEdge;
        } else {
            titleStyle.alignment = "center";
            titleStyle.centerOffsetX = offsetX;
        }
        if (anchor.startsWith(QStringLiteral("top"))) {
            titleStyle.verticalPosition = "top";
            titleStyle.bottomMargin = offsetY;
        } else if (anchor.startsWith(QStringLiteral("bottom"))) {
            titleStyle.verticalPosition = "bottom";
            titleStyle.bottomMargin = offsetY;
        } else {
            titleStyle.verticalPosition = "center";
            titleStyle.centerOffsetY = offsetY;
        }

        const QJsonObject titleRoleStyles = title.value(
            QStringLiteral("role_styles")
        ).toObject();
        const QJsonArray titleRoleRows = title.value(
            QStringLiteral("resolved_role_labels")
        ).toArray();
        QHash<QString, int> titleRoleStyleIndices;
        for (auto it = titleRoleStyles.begin(); it != titleRoleStyles.end(); ++it) {
            if (!it.value().isObject()) {
                continue;
            }
            TextStyle roleStyle = titleStyle;
            applyGpuResolvedStyle(
                roleStyle,
                resolvedStyleFromTitle(sourceStyle, it.value().toObject()),
                scale
            );
            const int styleIndex = static_cast<int>(scene.charStyles.size());
            scene.charStyles.push_back(std::move(roleStyle));
            titleRoleStyleIndices.insert(it.key(), styleIndex);
        }

        for (int rowIndex = 0; rowIndex < rows.size(); ++rowIndex) {
            const QString &row = rows.at(rowIndex);
            if (row.isEmpty()) {
                continue;
            }
            TextLine titleLine;
            titleLine.startMs = windows.front().startMs;
            titleLine.endMs = windows.back().endMs;
            // 每个标题占一个独立的负 sourceIndex：D2D 活动行按
            // (sourceIndex, sourceLineIndex) 去重，全部用 -1 会让时间重叠的
            // 第二个标题被当成同一行丢掉（无法同时显示）。
            titleLine.sourceIndex = -titleIndex;
            titleLine.sourceLineIndex = rowIndex;
            // lane 只表示标题块内的行号（纵向 dy = firstBaseline + step*lane），
            // 不能做跨标题唯一化，否则第二个标题会被推出屏幕。
            titleLine.lane = rowIndex;
            titleLine.compositeOrder =
                krok::subtitle::native::kTitleCompositeOrder - (titleIndex - 1);
            titleLine.staticOverlay = true;
            titleLine.fadeInMs = defaultFadeInMs;
            titleLine.fadeOutMs = defaultFadeOutMs;
            titleLine.displayWindows = windows;
            titleLine.chars.reserve(static_cast<std::size_t>(row.size()));
            const QJsonArray roleLabels = rowIndex < titleRoleRows.size()
                ? titleRoleRows.at(rowIndex).toArray()
                : QJsonArray{};
            for (int charIndex = 0; charIndex < row.size(); ++charIndex) {
                const QString roleLabel = charIndex < roleLabels.size()
                    ? roleLabels.at(charIndex).toString()
                    : QString{};
                const int styleIndex = titleRoleStyleIndices.value(roleLabel, -1);
                titleLine.chars.push_back(TextChar{
                    QString(row.at(charIndex)).toStdWString(),
                    1000000000,
                    1000000001,
                    styleIndex,
                    nullptr,
                    std::nullopt,
                });
            }
            scene.lineStyles.push_back(titleStyle);
            scene.lines.push_back(std::move(titleLine));
        }
    }
    return scene;
}

}  // namespace krok::subtitle::native::legacy_qt
