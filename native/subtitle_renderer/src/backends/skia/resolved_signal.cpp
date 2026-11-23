#include "resolved_signal.h"
#include "image_fill_cache.h"
#include "../../skia/core/subtitle_gradient.h"
#include "../render_backend.h"
#include "../../model/text_line.h"
#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <algorithm>
#include <cmath>
namespace krok::subtitle::native {
namespace {
std::uint32_t argb(const RgbaColor& c) {
    return (std::uint32_t(c.alpha) << 24) | (std::uint32_t(c.red) << 16) | (std::uint32_t(c.green) << 8) | c.blue;
}
}

std::optional<::subtitle::skia::ShapeSignal> resolveShapeSignal(
    const QJsonObject& source, const TextLine& line, const TextStyle& style, double dpr, SkiaImageFillCache& images,
    const TextStyle* decorStyle
) {
    const auto anchor = source.value("shape_signal_anchor");
    if (!style.litEnabled || style.litStyle == "volume" || !line.litHead) {
        if (!anchor.isUndefined()) throw BackendError("Skia 指示灯锚点缺少宿主行");
        return std::nullopt;
    }
    const auto xy = anchor.toArray();
    if (xy.size() != 2 || !std::ranges::all_of(xy, [](const auto& value) {
            return value.isDouble() && std::isfinite(value.toDouble()) && std::abs(value.toDouble()) <= 1e6;
        }))
        throw BackendError("Skia 指示灯缺少有效的最终锚点");
    if (style.litTransitionMode != "none" && style.litTransitionMode != "fade"
        && style.litTransitionMode != "slide")
        throw BackendError("Skia 指示灯的熄灭过渡模式尚未支持");
    if (std::max({style.litSize, style.litTracking, std::abs(style.litOffsetX),
            std::abs(style.litOffsetY), style.litStrokeWidth, style.litStrokeSoften,
            style.litTransitionDistance}) > 8192.f)
        throw BackendError("Skia 指示灯的几何参数超限");
    const auto geometry = shapeSignalGeometry(style);
    using Signal = ::subtitle::skia::ShapeSignal;
    using Shape = Signal::Shape;
    auto shape = Shape::Circle;
    if (style.litStyle == "square") shape = Shape::Square;
    if (style.litStyle == "rounded") shape = Shape::Rounded;
    if (style.litStyle == "image") shape = Shape::Image;
    Signal signal{
        shapeSignalClock(line.startMs, style),
        SkPoint::Make(float(xy[0].toDouble() * dpr) + style.litOffsetX,
                      float(xy[1].toDouble() * dpr) + style.litOffsetY - geometry.size),
        shape,
        geometry.size, geometry.size * 1.5f + geometry.tracking,
        style.litStrokeWidth, style.litStrokeSoften, style.litOpacity,
        std::min(style.litEdgeBrightness * .55f, 1.f), argb(style.litFill), argb(style.litStroke), style.litShadow,
        shape == Shape::Image ? images.staticImage(style.litImagePath, style.litImageModifiedMs, style.litImageSize) : nullptr,
    };
    signal.path = ::subtitle::skia::signalPath(style.litStyle, signal.size);
    if (style.litAppearanceMode == "custom") return signal;
    const auto& palette = decorStyle ? *decorStyle : style;
    const auto solid = [](const auto& paint) { return paint.mode == "solid"; };
    Signal::Decoration decoration;
    if (!signal.image) {
        decoration.unitFillGradient = ::subtitle::skia::lineGradient(palette.afterFillPaint, SkRect::MakeWH(1, 1), 0);
        if (!solid(palette.afterFillPaint) && !decoration.unitFillGradient)
            throw BackendError("Skia 指示灯当前支持实色或渐变填充");
    }
    if (signal.path && decoration.unitFillGradient) {
        const auto baseline = source.value("cells").toArray().at(0).toObject().value("baseline_y");
        if (!baseline.isDouble() || !std::isfinite(baseline.toDouble()) || std::abs(baseline.toDouble()) > 1e6)
            throw BackendError("Skia 矢量指示灯渐变缺少有效的基线");
        decoration.glyphPaintOrigin = SkPoint::Make(float(xy[0].toDouble() * dpr), float(baseline.toDouble() * dpr));
    }
    const float ratio = palette.fontSize > 0 ? signal.size / palette.fontSize : 1.f;
    const float maxStroke = std::floor(signal.size * .5f);
    signal.fill = argb(palette.afterFillPaint.color);
    signal.stroke = argb(palette.afterStrokePaint.color);
    signal.strokeWidth = std::clamp(palette.strokeWidth * ratio, 0.f, maxStroke);
    decoration.outerStroke = argb(palette.afterStroke2Paint.color);
    decoration.outerWidth = std::clamp(palette.stroke2Width * ratio, 0.f, maxStroke);
    if (!signal.image && ((signal.strokeWidth > 0 && !solid(palette.afterStrokePaint))
        || (decoration.outerWidth > 0 && !solid(palette.afterStroke2Paint))))
        throw BackendError("Skia 指示灯描边当前仅支持实色");
    const auto kind = palette.decorationKindForState(true);
    if (!signal.image && kind == "shadow") {
        decoration.shadowX = palette.shadowOffsetXForState(true) * ratio;
        decoration.shadowY = palette.shadowOffsetYForState(true) * ratio;
        if (decoration.shadowX || decoration.shadowY) {
            if (!solid(palette.afterDecorPaint)) throw BackendError("Skia 指示灯阴影当前仅支持实色");
            decoration.shadow = argb(palette.afterDecorPaint.color);
        }
    }
    if (kind == "glow") {
        using namespace ::subtitle::skia;
        const auto radius = std::lround(std::max(0.f, palette.glowAfterRadius * ratio));
        if (radius > 256) throw BackendError("Skia 指示灯的光晕半径超限");
        if (radius > 0 && !solid(palette.afterDecorPaint)) throw BackendError("Skia 指示灯光晕当前仅支持实色");
        const auto glow = GlowState{int(radius), palette.glowConcentrationForState(true), argb(palette.afterDecorPaint.color)};
        const auto join = style.litStyle == "star" ? SkPaint::kMiter_Join : SkPaint::kRound_Join;
        decoration.glow = PreparedGlow::prepare({{}, glow, signal.strokeWidth + decoration.outerWidth, join});
        for (auto& layer : decoration.glow) layer.appendCell(signal.outline());
    }
    signal.decoration = std::move(decoration);
    return signal;
}

std::optional<::subtitle::skia::VolumeSignal> resolveVolumeSignal(
    const QJsonObject& source, const TextLine& line, const TextStyle& style, double dpr, const TextStyle* decorStyle
) {
    const auto anchor = source.value("volume_signal_anchor");
    const bool enabled = style.volumeEnabled || (style.litEnabled && style.litStyle == "volume");
    const auto clock = volumeSignalClock(line.startMs, style);
    if (!enabled || !line.volumeHead || clock.activeDurationMs <= 0) {
        if (!anchor.isUndefined()) throw BackendError("Skia 音量柱锚点缺少宿主行");
        return std::nullopt;
    }
    const auto xy = anchor.toArray();
    if (xy.size() != 2 || !std::ranges::all_of(xy, [](const auto& value) {
            return value.isDouble() && std::isfinite(value.toDouble()) && std::abs(value.toDouble()) <= 1e6;
        }))
        throw BackendError("Skia 音量柱缺少有效的最终锚点");
    const auto geometry = volumeSignalGeometry(style);
    if (!std::isfinite(geometry.groupWidth) || geometry.groupWidth > 8192
        || !std::isfinite(geometry.size) || geometry.size > 8192
        || !std::isfinite(clock.flashRatio))
        throw BackendError("Skia 音量柱参数超限");
    using namespace ::subtitle::skia;
    VolumeSignal signal{clock};
    signal.singingStartMs = line.startMs;
    signal.opacity = style.volumeEnabled ? style.volumeOpacity : style.litOpacity;
    signal.strokeWidth = geometry.strokeExtent;
    signal.palette = {{{argb(style.volumeFill), argb(style.volumeStroke)},
                       {argb(style.volumeOverlayFill), argb(style.volumeOverlayStroke)}}};
    const auto& mode = style.volumeAppearanceMode;
    signal.decorated = style.volumeEnabled && (mode == "auto" || mode == "role");
    const auto& palette = decorStyle ? *decorStyle : style;
    StaticGlow glow;
    if (signal.decorated) {
        const float ratio = palette.fontSize > 0 ? geometry.size / palette.fontSize : 1.f;
        const float maximum = std::floor(geometry.columnWidth * .5f);
        signal.strokeWidth = std::clamp(palette.strokeWidth * ratio, 0.f, maximum);
        signal.outerWidth = std::clamp(palette.stroke2Width * ratio, 0.f, maximum);
        for (const bool after : {false, true}) {
            const auto& fill = after ? palette.afterFillPaint : palette.beforeFillPaint;
            const auto& stroke = after ? palette.afterStrokePaint : palette.beforeStrokePaint;
            const auto& outer = after ? palette.afterStroke2Paint : palette.beforeStroke2Paint;
            const auto& decor = after ? palette.afterDecorPaint : palette.beforeDecorPaint;
            if (fill.mode != "solid" && fill.mode != "gradient_vertical")
                throw BackendError("Skia 音量柱当前支持实色或纵向渐变填充");
            if ((signal.strokeWidth > 0 && stroke.mode != "solid")
                || (signal.outerWidth > 0 && outer.mode != "solid"))
                throw BackendError("Skia 音量柱描边当前仅支持实色");
            auto& paint = signal.palette[after];
            paint = {argb(fill.color), argb(stroke.color), argb(outer.color)};
            const auto kind = palette.decorationKindForState(after);
            if (kind == "shadow") {
                paint.shadowX = palette.shadowOffsetXForState(after) * ratio;
                paint.shadowY = palette.shadowOffsetYForState(after) * ratio;
                if (paint.shadowX || paint.shadowY) {
                    if (decor.mode != "solid") throw BackendError("Skia 音量柱阴影当前仅支持实色");
                    paint.shadow = argb(decor.color);
                }
            }
            if (kind == "glow" && palette.glowConcentrationForState(after) >= 0) {
                const auto radius = std::lround(std::max(0.f, (after ? palette.glowAfterRadius : palette.glowBeforeRadius) * ratio));
                if (radius > 256) throw BackendError("Skia 音量柱的光晕半径超限");
                if (radius > 0 && decor.mode != "solid") throw BackendError("Skia 音量柱光晕当前仅支持实色");
                (after ? glow.after : glow.before) = {int(radius), palette.glowConcentrationForState(after), argb(decor.color)};
            }
        }
        glow.outlineWidth = signal.strokeWidth + signal.outerWidth;
    }
    signal.textOriginX = float(xy[0].toDouble() * dpr);
    signal.groupRight = signal.textOriginX + style.volumeOffsetX;
    signal.groupLeft = signal.groupRight - geometry.groupWidth;
    const float x = signal.groupLeft + geometry.strokeExtent;
    const float y = float(xy[1].toDouble() * dpr) + style.volumeOffsetY - geometry.size * .5f + geometry.alignBaseShift;
    for (int index = 0; index < geometry.count; ++index) {
        const float height = std::max(geometry.frontHeight + index * geometry.heightDelta, 1.f);
        const float radius = std::max(std::min(height, geometry.columnWidth) * .22f, 1.f);
        const auto box = SkRect::MakeXYWH(x + index * geometry.pitch, y + index * geometry.alignDeltaShift,
                                         geometry.columnWidth, height);
        VolumeSignal::Bar bar{SkRRect::MakeRectXY(box, radius, radius), PreparedGlow::prepare(glow)};
        for (auto& layer : bar.glow) layer.appendCell(SkPath::RRect(bar.rect));
        signal.bars.push_back(std::move(bar));
    }
    if (signal.decorated) {
        SkRect bounds = SkRect::MakeEmpty();
        for (const auto& bar : signal.bars) bounds.join(bar.rect.rect());
        if (palette.beforeFillPaint.mode == "gradient_vertical")
            signal.fillGradients[0] = subtitleGradient(palette.beforeFillPaint, bounds);
        if (palette.afterFillPaint.mode == "gradient_vertical")
            signal.fillGradients[1] = subtitleGradient(palette.afterFillPaint, bounds);
    }
    return signal;
}
}
