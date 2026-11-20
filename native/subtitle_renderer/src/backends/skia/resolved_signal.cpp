#include "resolved_signal.h"
#include "image_fill_cache.h"
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
    const TextStyle* role
) {
    const auto anchor = source.value("shape_signal_anchor");
    if (!style.litEnabled || !line.litHead) {
        if (!anchor.isUndefined()) throw BackendError("Skia 指示灯锚点缺少宿主行");
        return std::nullopt;
    }
    const auto xy = anchor.toArray();
    if (xy.size() != 2 || !std::ranges::all_of(xy, [](const auto& value) {
            return value.isDouble() && std::isfinite(value.toDouble()) && std::abs(value.toDouble()) <= 1e6;
        }))
        throw BackendError("Skia 指示灯缺少有效的最终锚点");
    if (line.entryAnimation != "none" || line.exitAnimation != "none"
        || (style.litTransitionMode != "none" && style.litTransitionMode != "fade"
            && style.litTransitionMode != "slide"))
        throw BackendError("Skia 自定义指示灯的入退场组合或过渡模式尚未支持");
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
    if (style.litAppearanceMode == "custom") return signal;
    if (line.zoomPulseEnabled || std::ranges::any_of(line.chars, [](const auto& cell) {
            return cell.styleIndex >= 0 || bool(cell.vectorGlyph);
        }))
        throw BackendError("Skia 自动指示灯暂不支持角色字符、导唱符或整字放大");
    const auto& palette = style.litAppearanceMode == "role" && role ? *role : style;
    const auto solid = [](const auto& paint) { return paint.mode == "solid"; };
    if (!solid(palette.afterFillPaint) || !solid(palette.afterStrokePaint)
        || !solid(palette.afterStroke2Paint) || !solid(palette.afterDecorPaint))
        throw BackendError("Skia 自动指示灯当前仅支持实色装饰");
    const float ratio = palette.fontSize > 0 ? signal.size / palette.fontSize : 1.f;
    const float maxStroke = std::floor(signal.size * .5f);
    signal.fill = argb(palette.afterFillPaint.color);
    signal.stroke = argb(palette.afterStrokePaint.color);
    signal.strokeWidth = std::clamp(palette.strokeWidth * ratio, 0.f, maxStroke);
    Signal::Decoration decoration;
    decoration.outerStroke = argb(palette.afterStroke2Paint.color);
    decoration.outerWidth = std::clamp(palette.stroke2Width * ratio, 0.f, maxStroke);
    const auto kind = palette.decorationKindForState(true);
    if (!signal.image && kind == "shadow") {
        decoration.shadowX = palette.shadowOffsetXForState(true) * ratio;
        decoration.shadowY = palette.shadowOffsetYForState(true) * ratio;
        if (decoration.shadowX || decoration.shadowY) decoration.shadow = argb(palette.afterDecorPaint.color);
    }
    if (kind == "glow") {
        using namespace ::subtitle::skia;
        const auto radius = std::lround(std::max(0.f, palette.glowAfterRadius * ratio));
        if (radius > 256) throw BackendError("Skia 指示灯的光晕半径超限");
        const auto glow = GlowState{int(radius), palette.glowConcentrationForState(true), argb(palette.afterDecorPaint.color)};
        decoration.glow = PreparedGlow::prepare({{}, glow, signal.strokeWidth + decoration.outerWidth});
        for (auto& layer : decoration.glow) layer.appendCell(signal.outline());
    }
    signal.decoration = std::move(decoration);
    return signal;
}
}
