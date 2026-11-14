#include "style_projection.h"
#include "paint_projection.h"
#include <algorithm>
#include <cmath>
#include <QtCore/QFileInfo>

namespace krok::subtitle::native::scene {
using protocol::ResolvedStyle;
using protocol::ResolvedLineLayout;
using protocol::PaintFillSpec;

void applyResolvedStyle(
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
    target.fontAxis = source.fontAxis;
    target.fontHint = source.fontHint;
    target.latinFontWeight = source.latinFontWeight;
    target.latinFontAxis = source.latinFontAxis;
    target.latinFontHint = source.latinFontHint;
    target.latinFontStretchPct = source.latinFontStretchPct;
    target.italic = source.italic;
    // Per-slot italic: the same inheritance chain as the stroke2 switch
    // (latin/ruby follow the main text; ruby-latin follows the resolved ruby
    // value).  Resolved here once so every backend consumes plain bools.
    target.latinItalic = source.latinItalic.value_or(source.italic);
    target.rubyItalic = source.rubyItalic.value_or(source.italic);
    target.rubyLatinItalic = source.rubyLatinItalic.value_or(target.rubyItalic);
    target.allowBiting = source.allowBiting;
    target.affectsRubyAnchor = source.affectsRubyAnchor;
    target.spaceWidthPercent = source.spaceWidthPercent;
    target.letterSpacing = static_cast<float>(source.letterSpacingPx * scale);
    target.beforeFill = projectColor(source.baseFill.color, source.baseColor);
    target.afterFill = projectColor(source.afterFill.color, source.fillColor);
    target.beforeStroke = projectColor(source.beforeStrokeFill.color, source.beforeStrokeColor);
    target.afterStroke = projectColor(source.afterStrokeFill.color, source.afterStrokeColor);
    target.beforeStroke2 = projectColor(source.beforeStroke2Fill.color, source.beforeStroke2Color);
    target.afterStroke2 = projectColor(source.afterStroke2Fill.color, source.afterStroke2Color);
    target.beforeDecor = projectColor(source.beforeShadowFill.color, source.beforeShadowColor);
    target.afterDecor = projectColor(source.afterShadowFill.color, source.afterShadowColor);
    target.beforeFillPaint = projectPaint(source.baseFill, source.baseColor);
    target.afterFillPaint = projectPaint(source.afterFill, source.fillColor);
    target.beforeStrokePaint = projectPaint(source.beforeStrokeFill, source.beforeStrokeColor);
    target.afterStrokePaint = projectPaint(source.afterStrokeFill, source.afterStrokeColor);
    target.beforeStroke2Paint = projectPaint(source.beforeStroke2Fill, source.beforeStroke2Color);
    target.afterStroke2Paint = projectPaint(source.afterStroke2Fill, source.afterStroke2Color);
    target.beforeDecorPaint = projectPaint(source.beforeShadowFill, source.beforeShadowColor);
    target.afterDecorPaint = projectPaint(source.afterShadowFill, source.afterShadowColor);
    target.strokeWidth = static_cast<float>(source.strokeWidthPx * scale);
    target.stroke2Width = static_cast<float>(source.stroke2WidthPx * scale);
    // Latin-track strokes for alnum characters (CPU: main_script_stroke_style):
    // an unset or non-positive latin width follows the Japanese-track value;
    // the latin stroke2 flag follows the main flag when unset and gates the
    // width exactly once, inheriting the ungated raw width so an explicitly
    // enabled latin stroke2 survives a switched-off main track.
    {
        const int latinStrokePx =
            (source.latinStrokeWidthPx.has_value()
                && *source.latinStrokeWidthPx > 0)
                ? *source.latinStrokeWidthPx
                : source.strokeWidthPx;
        const bool latinStroke2On = source.latinStroke2Enabled.value_or(
            source.stroke2Enabled
        );
        const int latinStroke2RawPx =
            (source.latinStroke2WidthPx.has_value()
                && *source.latinStroke2WidthPx > 0)
                ? *source.latinStroke2WidthPx
                : source.stroke2RawWidthPx;
        const int latinStroke2Px = latinStroke2On ? latinStroke2RawPx : 0;
        target.latinStrokeWidth = static_cast<float>(latinStrokePx * scale);
        target.latinStroke2Width = static_cast<float>(latinStroke2Px * scale);
    }
    target.decorationKind = source.decorationKind.toStdString();
    target.decorationKindBefore = source.decorationKindBefore.isEmpty()
        ? target.decorationKind
        : source.decorationKindBefore.toStdString();
    target.decorationBeforeSync = source.decorationBeforeSync;
    target.glowBeforeRadius = static_cast<float>(source.glowBeforeRadiusPx * scale);
    target.glowAfterRadius = static_cast<float>(source.glowAfterRadiusPx * scale);
    target.glowConcentrationLevel = source.glowConcentrationLevel;
    target.glowBeforeConcentrationLevel =
        source.glowBeforeConcentrationLevel.value_or(
            source.glowConcentrationLevel
        );
    const int beforeShadowX = source.shadowBeforeOffsetX.value_or(
        source.shadowOffsetX
    );
    const int beforeShadowY = source.shadowBeforeOffsetY.value_or(
        source.shadowOffsetY
    );
    target.shadowOffsetX = static_cast<float>(source.shadowOffsetX * scale);
    target.shadowOffsetY = static_cast<float>(source.shadowOffsetY * scale);
    target.shadowBeforeOffsetX = static_cast<float>(beforeShadowX * scale);
    target.shadowBeforeOffsetY = static_cast<float>(beforeShadowY * scale);
    // 扫字线参数：随布局缩放；颜色走纯色画刷（projectColor 的 paint/fallback 同源）。
    target.scanlineWidth = static_cast<float>(
        std::max(source.scanlineWidthPx, 1) * scale
    );
    if (source.scanlineMode == QStringLiteral("brighten")) {
        target.scanlineMode = "brighten";
    } else if (source.scanlineMode == QStringLiteral("follow_before")) {
        target.scanlineMode = "follow_before";
    } else if (source.scanlineMode == QStringLiteral("follow_after")) {
        target.scanlineMode = "follow_after";
    } else if (source.scanlineMode == QStringLiteral("role")) {
        target.scanlineMode = "role";
        target.scanlineRolePaint = projectPaint(
            source.scanlineRolePaint, source.scanlineColor
        );
    } else if (source.scanlineMode == QStringLiteral("default")) {
        target.scanlineMode = "default";
    } else {
        target.scanlineMode = "color";
    }
    target.scanlineColor = projectColor(source.scanlineColor, source.scanlineColor);
    target.scanlineBrightness = std::clamp(
        static_cast<float>(source.scanlineBrightnessPct) / 100.0f, 0.0f, 1.0f
    );
    target.scanlineGlowRadius = static_cast<float>(
        std::max(source.scanlineGlowPx, 0) * scale
    );
    target.scanlineShape = (
        source.scanlineShape == QStringLiteral("flame")
        || source.scanlineShape == QStringLiteral("bubble")
        || source.scanlineShape == QStringLiteral("image")
    )
        ? source.scanlineShape.toStdString()
        : "band";
    // 自定义图片形态：路径为空 = 素材缺失（渲染端回退普通高亮带）；
    // (mtime, size) 失效签名与 PaintStyle.image* / litImage* 同口径，
    // configure 据此缓存解码位图。偏移随 dpr 缩放成场景像素。
    target.scanlineImagePath = source.scanlineImagePath.toStdWString();
    if (!source.scanlineImagePath.isEmpty()) {
        const QFileInfo info(source.scanlineImagePath);
        if (info.exists() && info.isFile()) {
            target.scanlineImageModifiedMs = static_cast<std::uint64_t>(
                std::max<qint64>(info.lastModified().toMSecsSinceEpoch(), 0)
            );
            target.scanlineImageSize = static_cast<std::uint64_t>(
                std::max<qint64>(info.size(), 0)
            );
        }
    }
    target.scanlineImageBandEnabled = source.scanlineImageBandEnabled;
    target.scanlineImageOffsetX = static_cast<float>(
        source.scanlineImageOffsetXPx * scale
    );
    target.scanlineImageOffsetY = static_cast<float>(
        source.scanlineImageOffsetYPx * scale
    );
    {
        const QStringList tokens = source.scanlineImageColorLayers.split(
            ',', Qt::SkipEmptyParts
        );
        target.scanlineImageTint = tokens.contains(
            QStringLiteral("solid"), Qt::CaseSensitive
        );
        target.scanlineImageStroke = tokens.contains(
            QStringLiteral("stroke"), Qt::CaseSensitive
        );
        target.scanlineImageDecor = tokens.contains(
            QStringLiteral("decor"), Qt::CaseSensitive
        );
    }
    target.scanlineImageScale = static_cast<float>(
        std::clamp(source.scanlineImageScalePct, 10, 400)
    ) / 100.0f;
    target.scanlineImageTintStrength = std::clamp(
        source.scanlineImageTintStrengthPct, 0, 100
    );
    // role 模式装饰几何（Python 物化值，输出像素 → 场景像素；< 0 未下发）。
    if (source.scanlineImageRoleFontSizePx >= 0.0) {
        target.scanlineImageRoleStrokeWidth = static_cast<float>(
            std::max(source.scanlineImageRoleStrokeWidthPx, 0.0) * scale
        );
        target.scanlineImageRoleStroke2Width = static_cast<float>(
            std::max(source.scanlineImageRoleStroke2WidthPx, 0.0) * scale
        );
        target.scanlineImageRoleFontSize = static_cast<float>(
            std::max(source.scanlineImageRoleFontSizePx, 1.0) * scale
        );
        target.scanlineImageRoleGlowRadius = static_cast<float>(
            std::max(source.scanlineImageRoleGlowRadiusPx, 0.0) * scale
        );
        target.scanlineImageRoleShadowOffsetX = static_cast<float>(
            source.scanlineImageRoleShadowOffsetXPx * scale
        );
        target.scanlineImageRoleShadowOffsetY = static_cast<float>(
            source.scanlineImageRoleShadowOffsetYPx * scale
        );
        target.scanlineImageRoleDecorationKind = (
            source.scanlineImageRoleDecorationKind == QStringLiteral("shadow")
            || source.scanlineImageRoleDecorationKind
                == QStringLiteral("glow")
        )
            ? source.scanlineImageRoleDecorationKind.toStdString()
            : "none";
    }
    // 整字放大缓动档位与峰值倍数：无量纲，不随布局缩放。
    target.zoomPulseCurveLevel = std::clamp(source.zoomPulseCurveLevel, 0, 5);
    target.zoomPulsePeakPct = std::clamp(source.zoomPulsePeakPct, 100, 200);

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
    target.rubyFontAxis = rubyUsesMainFont
        ? source.fontAxis
        : source.rubyFontAxis;
    target.rubyFontHint = rubyUsesMainFont
        ? source.fontHint
        : source.rubyFontHint;
    target.rubyLatinFontWeight = source.rubyLatinFontWeight;
    target.rubyLatinFontAxis = source.rubyLatinFontAxis;
    target.rubyLatinFontHint = source.rubyLatinFontHint;
    target.rubyLatinFontStretchPct = source.rubyLatinFontStretchPct.value_or(
        source.latinFontStretchPct
    );
    target.rubyGap = static_cast<float>(source.rubyGapPx * scale);
    target.rubyInterval = static_cast<float>(source.rubyIntervalPx * scale);
    target.rubyAlignment = source.rubyAlignment.toStdString();
    target.rubyMainProgressMode = source.rubyMainProgressMode.toStdString();
    target.rubyHorizontalGradientWithMain = source.rubyHorizontalGradientWithMain;
    target.rubyBeforeFill = projectColor(source.rubyBaseFill.color, source.rubyBaseColor);
    target.rubyAfterFill = projectColor(source.rubyAfterFill.color, source.rubyFillColor);
    target.rubyBeforeStroke = projectColor(
        source.rubyBeforeStrokeFill.color, source.rubyBeforeStrokeColor
    );
    target.rubyAfterStroke = projectColor(
        source.rubyAfterStrokeFill.color, source.rubyAfterStrokeColor
    );
    target.rubyBeforeStroke2 = projectColor(
        source.rubyBeforeStroke2Fill.color, source.rubyBeforeStroke2Color
    );
    target.rubyAfterStroke2 = projectColor(
        source.rubyAfterStroke2Fill.color, source.rubyAfterStroke2Color
    );
    target.rubyBeforeDecor = projectColor(
        source.rubyBeforeShadowFill.color, source.rubyBeforeShadowColor
    );
    target.rubyAfterDecor = projectColor(
        source.rubyAfterShadowFill.color, source.rubyAfterShadowColor
    );
    target.rubyBeforeFillPaint = projectPaint(source.rubyBaseFill, source.rubyBaseColor);
    target.rubyAfterFillPaint = projectPaint(source.rubyAfterFill, source.rubyFillColor);
    target.rubyBeforeStrokePaint = projectPaint(
        source.rubyBeforeStrokeFill, source.rubyBeforeStrokeColor
    );
    target.rubyAfterStrokePaint = projectPaint(
        source.rubyAfterStrokeFill, source.rubyAfterStrokeColor
    );
    target.rubyBeforeStroke2Paint = projectPaint(
        source.rubyBeforeStroke2Fill, source.rubyBeforeStroke2Color
    );
    target.rubyAfterStroke2Paint = projectPaint(
        source.rubyAfterStroke2Fill, source.rubyAfterStroke2Color
    );
    target.rubyBeforeDecorPaint = projectPaint(
        source.rubyBeforeShadowFill, source.rubyBeforeShadowColor
    );
    target.rubyAfterDecorPaint = projectPaint(
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
    const int rubyStroke2RawPx = source.rubyStroke2WidthPx.value_or(
        scaledFromMain(source.stroke2RawWidthPx)
    );
    const int rubyStroke2Px = rubyStroke2On ? rubyStroke2RawPx : 0;
    target.rubyStrokeWidth = static_cast<float>(rubyStrokePx * scale);
    target.rubyStroke2Width = static_cast<float>(rubyStroke2Px * scale);
    // Ruby-latin strokes for alnum readings (CPU: ruby_script_stroke_style):
    // an unset or non-positive latin width follows the ruby Japanese-track
    // value; the latin stroke2 flag follows the ruby flag when unset, and the
    // ungated ruby width keeps an explicitly enabled latin stroke2 from
    // collapsing when the ruby track itself is switched off.
    const int rubyLatinStrokePx =
        (source.rubyLatinStrokeWidthPx.has_value()
            && *source.rubyLatinStrokeWidthPx > 0)
            ? *source.rubyLatinStrokeWidthPx
            : rubyStrokePx;
    const bool rubyLatinStroke2On = source.rubyLatinStroke2Enabled.value_or(
        rubyStroke2On
    );
    const int rubyLatinStroke2RawPx =
        (source.rubyLatinStroke2WidthPx.has_value()
            && *source.rubyLatinStroke2WidthPx > 0)
            ? *source.rubyLatinStroke2WidthPx
            : rubyStroke2RawPx;
    const int rubyLatinStroke2Px = rubyLatinStroke2On ? rubyLatinStroke2RawPx : 0;
    target.rubyLatinStrokeWidth = static_cast<float>(rubyLatinStrokePx * scale);
    target.rubyLatinStroke2Width = static_cast<float>(rubyLatinStroke2Px * scale);
    target.rubyDecorationKind = (
        source.rubyDecorationKind.isEmpty()
            ? source.decorationKind
            : source.rubyDecorationKind
    ).toStdString();
    target.rubyDecorationKindBefore = source.rubyDecorationKindBefore.isEmpty()
        ? (source.rubyDecorationKind.isEmpty()
            ? target.decorationKindBefore
            : source.rubyDecorationKind.toStdString())
        : source.rubyDecorationKindBefore.toStdString();
    target.rubyGlowBeforeRadius = static_cast<float>(
        source.rubyGlowBeforeRadiusPx.value_or(
            source.rubyGlowAfterRadiusPx.value_or(
                scaledFromMain(source.glowBeforeRadiusPx)
            )
        ) * scale
    );
    target.rubyGlowAfterRadius = static_cast<float>(
        source.rubyGlowAfterRadiusPx.value_or(scaledFromMain(source.glowAfterRadiusPx))
        * scale
    );
    target.rubyGlowConcentrationLevel = source.rubyGlowConcentrationLevel.value_or(
        source.glowConcentrationLevel
    );
    target.rubyGlowBeforeConcentrationLevel =
        source.rubyGlowBeforeConcentrationLevel.value_or(
            source.rubyGlowConcentrationLevel.value_or(
                source.glowBeforeConcentrationLevel.value_or(
                    source.glowConcentrationLevel
                )
            )
        );
    target.rubyShadowOffsetX = static_cast<float>(
        source.rubyShadowOffsetX.value_or(source.shadowOffsetX) * scale
    );
    target.rubyShadowOffsetY = static_cast<float>(
        source.rubyShadowOffsetY.value_or(source.shadowOffsetY) * scale
    );
    target.rubyShadowBeforeOffsetX = static_cast<float>(
        source.rubyShadowBeforeOffsetX.value_or(
            source.rubyShadowOffsetX.value_or(beforeShadowX)
        ) * scale
    );
    target.rubyShadowBeforeOffsetY = static_cast<float>(
        source.rubyShadowBeforeOffsetY.value_or(
            source.rubyShadowOffsetY.value_or(beforeShadowY)
        ) * scale
    );
    target.litEnabled = source.litEnabled;
    target.litStyle = source.litStyle.toStdString();
    target.litAppearanceMode = source.litAppearanceMode.toStdString();
    target.litImagePath = source.litImagePath.toStdWString();
    if (!source.litImagePath.isEmpty()) {
        // 与 PaintStyle.image* 同口径：QFileInfo 探测 (mtime, size) 作失效
        // 签名，configure 按它缓存解码位图。
        const QFileInfo info(source.litImagePath);
        if (info.exists() && info.isFile()) {
            target.litImageModifiedMs = static_cast<std::uint64_t>(
                std::max<qint64>(info.lastModified().toMSecsSinceEpoch(), 0)
            );
            target.litImageSize = static_cast<std::uint64_t>(
                std::max<qint64>(info.size(), 0)
            );
        }
    }
    target.litNumber = source.litNumber;
    target.litSize = static_cast<float>(source.litSize * scale);
    target.litOffsetX = static_cast<float>(source.litOffsetX * scale);
    target.litOffsetY = static_cast<float>(source.litOffsetY * scale);
    target.litTracking = static_cast<float>(source.litTracking * scale);
    target.litFill = projectColor(source.litFillColor, QStringLiteral("#0000FF"));
    target.litStroke = projectColor(source.litStrokeColor, QStringLiteral("#FFFFFF"));
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
    target.volumeEnabled = source.volumeEnabled;
    target.volumeAppearanceMode = source.volumeAppearanceMode.toStdString();
    target.volumeDurationMs = source.volumeDurationMs;
    target.volumeWaitingTimeMs = source.volumeWaitingTimeMs;
    target.volumeTimeOffsetMs = source.volumeTimeOffsetMs;
    target.volumeStrokeWidth = static_cast<float>(source.volumeStrokeWidth * scale);
    target.volumeOpacity = static_cast<float>(source.volumeOpacityPct) / 100.0f;
    target.volumeSize = static_cast<float>(source.volumeSize * scale);
    target.volumeOffsetX = static_cast<float>(source.volumeOffsetX * scale);
    target.volumeOffsetY = static_cast<float>(source.volumeOffsetY * scale);
    target.volumeColumnWidth = static_cast<float>(source.volumeColumnWidth * scale);
    target.volumeColumnCount = source.volumeColumnCount;
    target.volumeColumnSpacing = static_cast<float>(source.volumeColumnSpacing * scale);
    target.volumeAlign = source.volumeAlign;
    target.volumeRatio = static_cast<float>(source.volumeRatio);
    target.volumeFill = projectColor(source.volumeFillColor, QStringLiteral("#FFFFFF"));
    target.volumeStroke = projectColor(source.volumeStrokeColor, QStringLiteral("#0000FF"));
    target.volumeOverlayFill = projectColor(source.volumeOverlayFillColor, QStringLiteral("#0000FF"));
    target.volumeOverlayStroke = projectColor(source.volumeOverlayStrokeColor, QStringLiteral("#FFFFFF"));
    target.volumeFlashTimes = source.volumeFlashTimes;
    target.volumeFlashDurationRatio = static_cast<float>(source.volumeFlashDurationRatio);
    target.volumeTransitionRatioPct = source.volumeTransitionRatioPct;
}

bool latinStrokesDiffer(const protocol::ResolvedStyle &style) {
    const int japaneseStroke2 = style.stroke2Enabled ? style.stroke2RawWidthPx : 0;
    const int latinStroke =
        (style.latinStrokeWidthPx.has_value() && *style.latinStrokeWidthPx > 0)
            ? *style.latinStrokeWidthPx
            : style.strokeWidthPx;
    const bool latinStroke2On = style.latinStroke2Enabled.value_or(
        style.stroke2Enabled
    );
    const int latinStroke2Raw =
        (style.latinStroke2WidthPx.has_value() && *style.latinStroke2WidthPx > 0)
            ? *style.latinStroke2WidthPx
            : style.stroke2RawWidthPx;
    const int latinStroke2 = latinStroke2On ? latinStroke2Raw : 0;
    return latinStroke != style.strokeWidthPx || latinStroke2 != japaneseStroke2;
}

}  // namespace krok::subtitle::native::scene
