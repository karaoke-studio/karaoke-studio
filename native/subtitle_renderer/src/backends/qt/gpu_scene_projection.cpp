#include "gpu_scene_projection.h"
#include "../../scene/paint_projection.h"
#include "../../scene/style_projection.h"
#include "../../scene/line_layout_projection.h"

#include "../text_semantics.h"
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
#include <QtCore/QJsonDocument>
#include <QtGui/QColor>

#include <algorithm>
#include <cmath>
#include <map>
#include <optional>
#include <utility>

namespace krok::subtitle::native::legacy_qt {

using scene::applyResolvedStyle;
using scene::applyLineLayout;
using scene::alignmentIndexForLane;
using scene::projectColor;
using scene::projectPaint;
using scene::latinStrokesDiffer;

using protocol::LineBandConfig;
using protocol::PaintFillSpec;
using protocol::RenderConfig;
using protocol::ResolvedLineLayout;
using protocol::ResolvedStyle;
using protocol::RubyAnnotation;
using protocol::TimingLine;
using protocol::parseBitmapGuide;
using protocol::parseVectorGlyph;
using protocol::resolvedStyleForCharacter;
using protocol::resolvedStyleForLine;
using protocol::resolvedStyleFromTitle;
using protocol::resolvedStyleKey;
using protocol::intValue;
using protocol::stringValue;

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
    applyResolvedStyle(scene.style, sourceStyle, scale);
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
    // 行背景带：px 全部换算成场景像素，染色值在这里解析成 RgbaColor。
    {
        const LineBandConfig &band = config.lineBand;
        LineBandStyle &target = scene.lineBand;
        target.enabled = band.enabled && !band.imagePath.isEmpty();
        target.imagePath = band.imagePath.toStdWString();
        target.fillMode = band.fillMode.toStdString();
        target.midMode = band.midMode.toStdString();
        // 端点以**源图像素**计：绘制端自己乘 (带高 / 图高)，这里再乘 dpr
        // 会二次缩放（dpr=2 时端点宽变 4 倍，谱号会被画两遍）。
        target.capLeft = static_cast<float>(band.capLeftPx);
        target.capRight = static_cast<float>(band.capRightPx);
        target.capLeftSrc = band.capLeftPx;
        target.capRightSrc = band.capRightPx;
        target.srcCapLeft = band.srcCapLeftPx;
        target.srcCapRight = band.srcCapRightPx;
        target.padTop = static_cast<float>(band.padTopPx * scale);
        target.padBottom = static_cast<float>(band.padBottomPx * scale);
        target.padLeft = static_cast<float>(band.padLeftPx * scale);
        target.padRight = static_cast<float>(band.padRightPx * scale);
        target.includeRuby = band.includeRuby;
        target.offsetY = static_cast<float>(band.offsetYPx * scale);
        target.opacity = static_cast<float>(
            std::clamp(band.opacityPct, 0, 100)
        ) / 100.0f;
        const QColor tint(band.tint);
        // 与 painter 的 QColor 解析同口径：非法/空值 = 不染色。
        target.hasTint = tint.isValid();
        if (target.hasTint) {
            target.tint = RgbaColor{
                static_cast<std::uint8_t>(tint.red()),
                static_cast<std::uint8_t>(tint.green()),
                static_cast<std::uint8_t>(tint.blue()),
                255,
            };
        }
        target.tintStrengthPct = std::clamp(band.tintStrengthPct, 0, 100);
        target.linesPath = band.linesPath.toStdWString();
        const QColor lineColor(band.lineColor);
        target.hasLineColor = !band.linesPath.isEmpty() && lineColor.isValid();
        if (target.hasLineColor) {
            target.lineColor = RgbaColor{
                static_cast<std::uint8_t>(lineColor.red()),
                static_cast<std::uint8_t>(lineColor.green()),
                static_cast<std::uint8_t>(lineColor.blue()),
                255,
            };
        }
        target.edgeKind = band.edgeKind == QStringLiteral("shadow")
                || band.edgeKind == QStringLiteral("glow")
                || band.edgeKind == QStringLiteral("stroke")
            ? band.edgeKind.toStdString()
            : "none";
        const QColor edgeColor(band.edgeColor);
        target.hasEdgeColor = edgeColor.isValid();
        if (target.hasEdgeColor) {
            target.edgeColor = RgbaColor{
                static_cast<std::uint8_t>(edgeColor.red()),
                static_cast<std::uint8_t>(edgeColor.green()),
                static_cast<std::uint8_t>(edgeColor.blue()),
                255,
            };
        }
        target.clefPath = band.clefPath.toStdWString();
        const QColor clefColor(band.clefColor);
        target.hasClefColor = !band.clefPath.isEmpty() && clefColor.isValid();
        if (target.hasClefColor) {
            target.clefColor = RgbaColor{
                static_cast<std::uint8_t>(clefColor.red()),
                static_cast<std::uint8_t>(clefColor.green()),
                static_cast<std::uint8_t>(clefColor.blue()),
                255,
            };
        }        target.edgeSize = static_cast<float>(
            std::max(band.edgeSizePx, 0) * scale
        );
        target.edgeOffsetX = static_cast<float>(band.edgeOffsetXPx * scale);
        target.edgeOffsetY = static_cast<float>(band.edgeOffsetYPx * scale);
        auto projectEffect = [&](const LineBandConfig::EffectConfig &source,
                                 LineBandStyle::Effect &target) {
            target.enabled = source.enabled && source.sizePx > 0;
            const QColor effectColor(source.color);
            target.hasColor = effectColor.isValid();
            if (target.hasColor) {
                target.color = RgbaColor{
                    static_cast<std::uint8_t>(effectColor.red()),
                    static_cast<std::uint8_t>(effectColor.green()),
                    static_cast<std::uint8_t>(effectColor.blue()),
                    255,
                };
            }
            target.size = static_cast<float>(
                std::max(source.sizePx, 0) * scale
            );
            // PS 标准：阴影偏移由 距离 + 角度 合成（135° = 右下）
            const double radians = source.angleDeg * 3.14159265358979323846 / 180.0;
            const double distance = source.distancePx > 0
                ? source.distancePx
                : (source.offsetXPx || source.offsetYPx
                    ? std::hypot(source.offsetXPx, source.offsetYPx) : 0.0);
            const double angle = source.distancePx > 0
                ? radians
                : (source.offsetXPx || source.offsetYPx
                    ? std::atan2(
                        static_cast<double>(source.offsetYPx),
                        static_cast<double>(source.offsetXPx)
                      )
                    : radians);
            target.offsetX = static_cast<float>(std::cos(angle) * distance * scale);
            target.offsetY = static_cast<float>(std::sin(angle) * distance * scale);
            target.spreadPx = static_cast<float>(
                source.spreadPct / 100.0 * std::max(source.sizePx, 0) * scale
            );
            target.range = static_cast<float>(source.rangePct) / 100.0f;
            target.position = source.position.toStdString();
        };
        projectEffect(band.shadow, target.shadow);
        projectEffect(band.stroke, target.stroke);
        projectEffect(band.glow, target.glow);        target.edgeOpacity = static_cast<float>(
            std::clamp(band.edgeOpacityPct, 0, 100)
        ) / 100.0f;
    }
    scene.lines.reserve(config.lines.size());
    scene.lineStyles.reserve(config.lines.size());
    QHash<QString, int> charStyleIndices;
    for (const TimingLine &sourceLine : config.lines) {
        if (sourceLine.chars.empty()) {
            continue;
        }
        TextStyle lineStyle = scene.style;
        applyResolvedStyle(
            lineStyle, resolvedStyleForLine(config, sourceLine), scale
        );
        if (sourceLine.layout.present) {
            applyLineLayout(
                lineStyle, sourceLine.layout, sourceLine.lane,
                sourceLine.centerOverride, sourceLine.pageLineCount, scale
            );
        } else {
            const int offsetIndex = alignmentIndexForLane(
                sourceLine.lane, static_cast<int>(config.lineAlignments.size()),
                config.dualLineLayout && config.lineHorizontalLayout == QStringLiteral("asymmetric")
                    ? sourceLine.pageLineCount : 0,
                config.lineYPosition
            );
            lineStyle.laneOffsetX = offsetIndex < static_cast<int>(config.lineOffsetsX.size())
                ? static_cast<float>(config.lineOffsetsX[offsetIndex] * scale) : 0.0f;
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
        if (sourceLine.annotationBaselineY.has_value()) {
            line.annotationBaselineY = static_cast<float>(*sourceLine.annotationBaselineY * scale);
        }
        const int sourceTimingOffset = config.timingOffsetMs + sourceLine.sourceOffsetMs;
        line.startMs = lineStartMs(sourceLine) + sourceTimingOffset;
        line.endMs = lineEndMs(sourceLine) + sourceTimingOffset;
        line.sourceIndex = sourceLine.sourceIndex;
        line.sourceLineIndex = sourceLine.sourceLineIndex;
        line.pageIndex = sourceLine.pageIndex;
        line.lane = sourceLine.lane;
        line.signalHead = sourceLine.signalHead;
        line.volumeHead = sourceLine.volumeHead;
        line.litHead = sourceLine.litHead;
        line.signalBandJoin = sourceLine.signalBandJoin;
        line.wipeReverse = sourceLine.wipeReverse;
        line.centerOverride = sourceLine.centerOverride;
        // 标题钉在最下层（compositeOrder = kTitleCompositeOrder），所以源之间不必
        // 再为它预留 1 号槽位：主字幕 0，副源依次 1、2……
        line.compositeOrder = sourceLine.sourceIndex;
        const auto verticalCharacterAnimation = [&](const QString &animation) {
            return config.vertical && (
                animation == QStringLiteral("char_fade")
                || animation == QStringLiteral("char_drip")
                || animation == QStringLiteral("spin_flip")
                || animation == QStringLiteral("utopia")
                || animation == QStringLiteral("tracking_in")
                || animation == QStringLiteral("wave_in")
                || animation == QStringLiteral("scatter_out")
                || animation == QStringLiteral("converge_out")
                || animation == QStringLiteral("glow_in")
                || animation == QStringLiteral("glow_out")
                || animation == QStringLiteral("stretch_in")
                || animation == QStringLiteral("stretch_out")
                || animation == QStringLiteral("assemble_in")
                || animation == QStringLiteral("dissolve_out")
                || animation == QStringLiteral("sparkle")
                || animation == QStringLiteral("ripple")
                || animation == QStringLiteral("note")
                || animation == QStringLiteral("heart")
                || animation == QStringLiteral("flame")
                || animation == QStringLiteral("petal")
                || animation == QStringLiteral("snow")
                || animation == QStringLiteral("snow_solid")
                || animation == QStringLiteral("feather")
                || animation == QStringLiteral("lightning")
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
        // 扫字线目前只有横排渲染路径（与 CPU Painter 同口径）；竖排行退回
        // 基础 Wipe 语义，不叠加高亮带。整字放大同为横排逐字变换，竖排一并关闭。
        line.scanlineEnabled = !config.vertical && sourceLine.scanlineEnabled;
        line.zoomPulseEnabled = !config.vertical && sourceLine.zoomPulseEnabled;
        // 唱字描边闪光 / 装饰粒子同为横排专属（与 Painter 同口径）。
        line.strokeFlashEnabled = !config.vertical && sourceLine.strokeFlashEnabled;
        if (!config.vertical) {
            line.bursts.reserve(sourceLine.fxBursts.size());
            for (const TimingLine::FxBurst &source : sourceLine.fxBursts) {
                krok::subtitle::native::ParticleBurst burst;
                burst.kind = source.kind.toStdString();
                burst.sprite = source.sprite.toStdString();
                burst.anchor = source.anchor.toStdString();
                burst.charIndex = source.charIndex;
                burst.startMs = source.startMs + sourceTimingOffset;
                burst.endMs = source.endMs + sourceTimingOffset;
                burst.count = source.count;
                burst.seed = source.seed;
                burst.sizePx = static_cast<float>(source.sizePx * scale);
                burst.travelPx = static_cast<float>(source.travelPx * scale);
                burst.emitX = static_cast<float>(source.emitX);
                burst.emitMs = source.emitMs;
                burst.timeScale = static_cast<float>(source.timeScale);
                burst.front = source.front;
                burst.reverse = source.reverse;
                burst.sweep = source.sweep;
                burst.color = projectColor(source.color, QStringLiteral("#FFFFFF"));
                // 2026-10 装饰规格：PaintFillSpec → PaintStyle（文本填充
                // 同一转换），描边宽随 scale 同比缩放。
                burst.charColors.reserve(source.charColors.size());
                for (const QString &hex : source.charColors) {
                    burst.charColors.push_back(
                        projectColor(hex, QStringLiteral("#FFFFFF"))
                    );
                }
                burst.hasPaint = source.hasPaint;
                if (source.hasPaint) {
                    burst.fill = projectPaint(source.fill, source.color);
                    burst.stroke = projectPaint(source.stroke, source.color);
                    burst.stroke2 = projectPaint(source.stroke2, source.color);
                    burst.strokeWidth = static_cast<float>(
                        source.strokeWidthPx * scale
                    );
                    burst.stroke2Width = static_cast<float>(
                        source.stroke2WidthPx * scale
                    );
                    // 取色层级「+装饰/全有」的装饰层：kind 随来源方案下发，
                    // 偏移/半径随 scale 同比缩放。
                    burst.hasDecor = source.hasDecor;
                    if (source.hasDecor) {
                        burst.decorKind = source.decorKind.toStdString();
                        burst.decor = projectPaint(source.decor, source.color);
                        burst.decorOffsetX = static_cast<float>(
                            source.decorOffsetX * scale
                        );
                        burst.decorOffsetY = static_cast<float>(
                            source.decorOffsetY * scale
                        );
                        burst.decorRadius = static_cast<float>(
                            source.decorRadius * scale
                        );
                        burst.decorConcentration = source.decorConcentration;
                    }
                }
                line.bursts.push_back(std::move(burst));
            }
        }
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
            // CPU 对齐（main_script_stroke_style）：横排时英数字符的描边走
            // 拉丁轨，按脚本派生变体样式；竖排 CPU 本就逐字用行样式，不派生。
            const protocol::TimingChar &sourceChar = sourceLine.chars[index];
            const bool latinGlyph = !config.vertical
                && sourceChar.vectorGlyph == nullptr
                && !sourceChar.bitmapGuide.has_value()
                && krok::subtitle::native::isLatinText(
                    sourceChar.text.toStdWString()
                );
            const bool latinVariant =
                latinGlyph
                && latinStrokesDiffer(
                    resolvedStyleForCharacter(config, sourceLine, sourceChar)
                );
            // Painter's vertical path currently uses the resolved line style
            // for every glyph; inline role styles are a horizontal-only
            // contract until the CPU oracle itself gains vertical runs.
            if (!config.vertical && !sourceChar.roleLabel.isEmpty()) {
                QString key = resolvedStyleKey(
                    sourceLine.singerId, sourceChar.roleLabel
                );
                if (latinVariant) {
                    key += QStringLiteral("|latin");
                }
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
                    applyResolvedStyle(
                        charStyle,
                        resolvedStyleForCharacter(config, sourceLine, sourceChar),
                        scale
                    );
                    applyLineLayout(
                        charStyle, sourceLine.layout, sourceLine.lane,
                        sourceLine.centerOverride, sourceLine.pageLineCount, scale
                    );
                    charStyle.layoutOffsetX += static_cast<float>(
                        sourceLine.layoutOffsetX * scale
                    );
                    charStyle.layoutOffsetY += static_cast<float>(
                        sourceLine.layoutOffsetY * scale
                    );
                    if (latinVariant) {
                        charStyle.strokeWidth = charStyle.latinStrokeWidth;
                        charStyle.stroke2Width = charStyle.latinStroke2Width;
                    }
                    styleIndex = static_cast<int>(scene.charStyles.size());
                    scene.charStyles.push_back(std::move(charStyle));
                    charStyleIndices.insert(key, styleIndex);
                }
            } else if (latinVariant) {
                // 无角色标注的英数字符同样需要脚本变体；键不含角色，仅
                // 拉丁轨 + 布局参数。
                QString key = QStringLiteral("latin");
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
                    applyResolvedStyle(
                        charStyle,
                        resolvedStyleForCharacter(config, sourceLine, sourceChar),
                        scale
                    );
                    applyLineLayout(
                        charStyle, sourceLine.layout, sourceLine.lane,
                        sourceLine.centerOverride, sourceLine.pageLineCount, scale
                    );
                    charStyle.layoutOffsetX += static_cast<float>(
                        sourceLine.layoutOffsetX * scale
                    );
                    charStyle.layoutOffsetY += static_cast<float>(
                        sourceLine.layoutOffsetY * scale
                    );
                    charStyle.strokeWidth = charStyle.latinStrokeWidth;
                    charStyle.stroke2Width = charStyle.latinStroke2Width;
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
            sceneChar.fallbackHint = sourceLine.chars[index].fallbackHint;
            sceneChar.fallbackFamily =
                sourceLine.chars[index].fallbackFamily.toStdWString();
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
            if (sourceRuby.lower && sourceRuby.lowerLineIndex != sourceLine.sourceLineIndex) {
                continue;
            }
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
            sceneRuby.lower = sourceRuby.lower;
            if (sourceRuby.lowerBaselineOffsetY.has_value()) {
                sceneRuby.lowerBaselineOffsetY = static_cast<float>(*sourceRuby.lowerBaselineOffsetY * scale);
            }
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
            if (!sourceRuby.lower && !mainWipeAlreadyAssigned && applyRubyMainWipeProjection(
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
            int rubyRoleIndex = *minimum;
            for (int targetIndex : targetIndices) {
                if (targetIndex < 0
                    || targetIndex >= static_cast<int>(sourceLine.chars.size())
                    || sourceLine.chars[static_cast<std::size_t>(targetIndex)].roleLabel.isEmpty()) {
                    continue;
                }
                sceneRuby.styleIndex = line.chars[
                    static_cast<std::size_t>(targetIndex)
                ].styleIndex;
                rubyRoleIndex = targetIndex;
                break;
            }
            if (sourceRuby.lower) {
                const QString key = QStringLiteral("lower:") + resolvedStyleKey(
                    sourceLine.singerId, sourceLine.chars[static_cast<std::size_t>(rubyRoleIndex)].roleLabel
                ) + QString::fromUtf8(
                    QJsonDocument(sourceRuby.lowerStyle).toJson(QJsonDocument::Compact));
                const auto existing = charStyleIndices.constFind(key);
                if (existing != charStyleIndices.constEnd()) {
                    sceneRuby.styleIndex = existing.value();
                } else {
                    TextStyle lowerStyle = scene.lineStyles.back();
                    applyResolvedStyle(lowerStyle, protocol::resolvedRubyStyle(
                        resolvedStyleForCharacter(config, sourceLine,
                            sourceLine.chars[static_cast<std::size_t>(rubyRoleIndex)]),
                        sourceRuby.lowerStyle), scale);
                    sceneRuby.styleIndex = static_cast<int>(scene.charStyles.size());
                    scene.charStyles.push_back(std::move(lowerStyle));
                    charStyleIndices.insert(key, sceneRuby.styleIndex);
                }
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
        applyResolvedStyle(
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
        // The title block is one page: each row carries its own alignment
        // (Python resolves the layout's line_alignments top-down), so the
        // edge margin must hold the title's own offsets whichever edge a row
        // ends up using.  The anchor's horizontal suffix only stays as the
        // default alignment.  Centered rows keep centerOffsetX at zero: N3's
        // SetOneLineX anchors Center rows to the full movie width without the
        // horizontal margin, and Middle centers vertically ignoring the
        // vertical margin -- mirroring Painter's title_row_screen_x and
        // title_block_y_top.
        titleStyle.horizontalMargin = offsetX + titleHalfEdge;
        if (anchor.endsWith(QStringLiteral("left"))) {
            titleStyle.alignment = "left";
        } else if (anchor.endsWith(QStringLiteral("right"))) {
            titleStyle.alignment = "right";
        } else {
            titleStyle.alignment = "center";
        }
        QStringList rowAlignments;
        for (const auto &value : title.value(
                 QStringLiteral("row_alignments")
             ).toArray()) {
            rowAlignments.append(value.toString());
        }
        if (anchor.startsWith(QStringLiteral("top"))) {
            titleStyle.verticalPosition = "top";
            titleStyle.bottomMargin = offsetY;
        } else if (anchor.startsWith(QStringLiteral("bottom"))) {
            titleStyle.verticalPosition = "bottom";
            titleStyle.bottomMargin = offsetY;
        } else {
            titleStyle.verticalPosition = "center";
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
            applyResolvedStyle(
                roleStyle,
                resolvedStyleFromTitle(sourceStyle, it.value().toObject()),
                scale
            );
            const int styleIndex = static_cast<int>(scene.charStyles.size());
            scene.charStyles.push_back(std::move(roleStyle));
            titleRoleStyleIndices.insert(it.key(), styleIndex);
        }

        // 标题图片导唱符：IR 下发 [行号, 符号] / [行号, 字符下标, 符号]
        // （render_ir._title_guide_to_ir），复用歌词字符的 bitmap/vector
        // 管线；标题永不走字，位图恒取「走字前」一侧。
        struct TitleGuideEntry {
            int count = 1;
            std::shared_ptr<const krok::subtitle::native::VectorGlyph> vectorGlyph;
            std::optional<krok::subtitle::native::BitmapGuide> bitmapGuide;
            QStringList roleLabels;
        };
        const auto parseTitleGuide = [&](const QJsonObject &guide) -> TitleGuideEntry {
            TitleGuideEntry entry;
            entry.count = std::max(
                guide.value(QStringLiteral("count")).toInt(1),
                1
            );
            const QJsonArray labels = guide.value(
                QStringLiteral("role_labels")
            ).toArray();
            for (int index = 0; index < entry.count; ++index) {
                entry.roleLabels.append(
                    index < labels.size() ? labels.at(index).toString() : QString{}
                );
            }
            if (guide.value(QStringLiteral("kind")).toString()
                == QStringLiteral("bitmap")) {
                entry.bitmapGuide = parseBitmapGuide(guide);
            } else {
                const auto glyph = parseVectorGlyph(
                    guide.value(QStringLiteral("vector_glyph"))
                );
                if (glyph.has_value()) {
                    entry.vectorGlyph = std::make_shared<
                        const krok::subtitle::native::VectorGlyph
                    >(std::move(*glyph));
                }
            }
            return entry;
        };
        QHash<int, TitleGuideEntry> rowGuides;
        for (const auto &entryValue : title.value(
                 QStringLiteral("guide_symbols")
             ).toArray()) {
            const QJsonArray entry = entryValue.toArray();
            if (entry.size() == 2 && entry.at(0).isDouble()) {
                rowGuides.insert(
                    entry.at(0).toInt(),
                    parseTitleGuide(entry.at(1).toObject())
                );
            }
        }
        std::map<std::pair<int, int>, TitleGuideEntry> inlineGuides;
        for (const auto &entryValue : title.value(
                 QStringLiteral("inline_guide_symbols")
             ).toArray()) {
            const QJsonArray entry = entryValue.toArray();
            if (entry.size() == 3
                && entry.at(0).isDouble()
                && entry.at(1).isDouble()) {
                inlineGuides.emplace(
                    std::make_pair(entry.at(0).toInt(), entry.at(1).toInt()),
                    parseTitleGuide(entry.at(2).toObject())
                );
            }
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
            const QJsonArray roleLabels = rowIndex < titleRoleRows.size()
                ? titleRoleRows.at(rowIndex).toArray()
                : QJsonArray{};
            const auto rowGuide = rowGuides.constFind(rowIndex);
            titleLine.chars.reserve(
                static_cast<std::size_t>(row.size())
                + (rowGuide != rowGuides.constEnd()
                    ? static_cast<std::size_t>(rowGuide->count)
                    : 0)
            );
            if (rowGuide != rowGuides.constEnd()) {
                // 行前导唱符：count 个虚拟字符插在正文之前；标题永不走字
                // （字符时间钉在遥远未来），位图恒取「走字前」一侧，动图按
                // IR 锚点钉死首帧——与 Painter 标题层的静态烘焙同一画面。
                for (int glyphIndex = 0; glyphIndex < rowGuide->count; ++glyphIndex) {
                    const QString guideRole = glyphIndex < rowGuide->roleLabels.size()
                        ? rowGuide->roleLabels.at(glyphIndex)
                        : QString{};
                    titleLine.chars.push_back(TextChar{
                        std::wstring(L"\uFFFC"),
                        1000000000,
                        1000000001,
                        titleRoleStyleIndices.value(guideRole, -1),
                        rowGuide->vectorGlyph,
                        rowGuide->bitmapGuide,
                    });
                }
            }
            // Combining marks (decomposed dakuten etc.) join the preceding
            // base character: the Python side sends role labels and inline
            // guide keys on the same unit basis (title_line_units), and each
            // unit must be one TextChar so both backends lay out identically.
            for (int charIndex = 0, unitIndex = 0; charIndex < row.size();
                 ++unitIndex) {
                QString cell(row.at(charIndex));
                ++charIndex;
                while (charIndex < row.size()
                       && row.at(charIndex).isMark()) {
                    cell.append(row.at(charIndex));
                    ++charIndex;
                }
                const QString roleLabel = unitIndex < roleLabels.size()
                    ? roleLabels.at(unitIndex).toString()
                    : QString{};
                const int styleIndex = titleRoleStyleIndices.value(roleLabel, -1);
                TextChar titleChar{
                    cell.toStdWString(),
                    1000000000,
                    1000000001,
                    styleIndex,
                    nullptr,
                    std::nullopt,
                };
                // 行内图片替换：与歌词 render line 同构——原字符槽位换成
                // \uFFFC 虚拟字符 + 导唱符，角色沿用被替换字符自己的标签。
                const auto inlineGuide = inlineGuides.find(
                    std::make_pair(rowIndex, unitIndex)
                );
                if (inlineGuide != inlineGuides.end()) {
                    titleChar.text = std::wstring(L"\uFFFC");
                    titleChar.vectorGlyph = inlineGuide->second.vectorGlyph;
                    titleChar.bitmapGuide = inlineGuide->second.bitmapGuide;
                }
                titleLine.chars.push_back(std::move(titleChar));
            }
            // 行级对齐：row_alignments 与 text 的原始行号一一对应（含空行），
            // 缺省回落锚点水平位；非法值同样回落，不让坏 IR 移动标题。
            TextStyle rowStyle = titleStyle;
            const QJsonArray rowOffsets = title.value(QStringLiteral("row_offsets_x")).toArray();
            rowStyle.laneOffsetX = rowIndex < rowOffsets.size()
                ? static_cast<float>(rowOffsets.at(rowIndex).toInt() * scale) : 0.0f;
            if (rowIndex < rowAlignments.size()) {
                const QString rowAlign = rowAlignments.at(rowIndex);
                if (rowAlign == QStringLiteral("left")
                    || rowAlign == QStringLiteral("right")
                    || rowAlign == QStringLiteral("center")) {
                    rowStyle.alignment = rowAlign.toStdString();
                }
            }
            scene.lineStyles.push_back(std::move(rowStyle));
            scene.lines.push_back(std::move(titleLine));
        }
    }
    // 装饰粒子 sprite 轮廓表（值语义投影；QHash 无序，按键排序保证相等性
    // 比较与缓存签名稳定）。
    if (!config.fxSprites.isEmpty()) {
        scene.fxSprites.reserve(static_cast<std::size_t>(config.fxSprites.size()));
        for (auto it = config.fxSprites.constBegin(); it != config.fxSprites.constEnd(); ++it) {
            scene.fxSprites.emplace_back(it.key().toStdString(), it.value());
        }
        std::sort(
            scene.fxSprites.begin(), scene.fxSprites.end(),
            [](const auto &a, const auto &b) { return a.first < b.first; }
        );
    }
    // 指示灯/音量柱 ``role`` 外观档（复用配色方案）的固定装饰源：方案叠加
    // 到全局 base 后投影成完整 TextStyle（镜像 Painter 的
    // appearance_role_source——``__global__`` 取主样式自身，其余名字查
    // customStyleSchemes；悬空/空名返回 nullopt，渲染端回退 auto 口径：
    // 段首行第一个角色）。
    const auto roleDecorTextStyle = [&](
        const QString &mode, const QString &roleName
    ) -> std::optional<TextStyle> {
        if (mode != QStringLiteral("role")) {
            return std::nullopt;
        }
        const QString name = roleName.trimmed();
        if (name.isEmpty()) {
            return std::nullopt;
        }
        if (name != QStringLiteral("__global__")) {
            const QJsonObject scheme =
                config.customStyleSchemes.value(name).toObject();
            if (scheme.isEmpty()) {
                return std::nullopt;
            }
            TextStyle decorStyle = scene.style;
            applyResolvedStyle(
                decorStyle,
                protocol::resolvedStyleForRole(config, config.baseStyle, name),
                scale
            );
            return decorStyle;
        }
        return scene.style;
    };
    scene.litDecorStyle = roleDecorTextStyle(
        config.baseStyle.litAppearanceMode, config.baseStyle.litRoleName
    );
    scene.volumeDecorStyle = roleDecorTextStyle(
        config.baseStyle.volumeAppearanceMode, config.baseStyle.volumeRoleName
    );
    return scene;
}

}  // namespace krok::subtitle::native::legacy_qt
