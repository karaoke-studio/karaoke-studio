#include "volume_signal.h"
#include "character_halo.h"
#include "signal_transition.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"

namespace subtitle::skia {
void VolumeSignal::drawTransitionHalo(SkCanvas& canvas, int time, const SignalTransition& transition) const {
    const auto state = krok::subtitle::native::volumeSignalState(clock, time);
    if (!state.visible || state.opacity <= 0) return;
    for (int index = 0; index < clock.count; ++index) {
        const auto& rect = bars[index].rect;
        const auto& box = rect.rect();
        const auto pose = transition.at(time, index, clock.count, box.centerX(), box.centerY());
        if (pose.glowAlpha <= 0) continue;
        const auto contour = SkPath::RRect(rect);
        drawCharacterHalo(canvas, pose.transformed ? contour.makeTransform(pose.matrix) : contour, pose,
                          transition.geometry.fontPx, state.opacity);
    }
}

void VolumeSignal::draw(SkCanvas& canvas, int time, const SignalTransition& transition, SkPoint paintShift) const {
    const auto state = krok::subtitle::native::volumeSignalState(clock, time);
    if (!state.visible || state.opacity <= 0 || opacity <= 0) return;
    SkAutoCanvasRestore flashLayer(&canvas, false);
    if (state.opacity < 1) canvas.saveLayerAlphaf(nullptr, state.opacity);
    const auto brushOrigin = SkMatrix::Translate(paintShift.x(), paintShift.y());
    const auto shaderAt = [&](const ImageFill& texture, const sk_sp<SkShader>& gradient = nullptr) {
        return texture ? texture.shaderAt(brushOrigin) : gradient;
    };
    const std::array glowBrushes{shaderAt(palette[0].glowImage), shaderAt(palette[1].glowImage)};
    const auto poseAt = [&](int index) {
        const auto& box = bars[index].rect.rect();
        auto pose = transition.at(time, index, clock.count, box.centerX(), box.centerY());
        if (decorated && transition.singingScale.zoomPulse) {
            const auto start = std::int64_t(state.fillDurationMs * index / clock.count);
            const auto end = std::int64_t(state.fillDurationMs * (index + 1) / clock.count);
            transition.applyZoomPulse(pose, state.fillElapsedMs, start, end, box.centerX(), box.centerY());
        }
        return pose;
    };
    for (int index = 0; index < clock.count; ++index) {
        const auto pose = poseAt(index);
        const float columnOpacity = opacity * pose.opacity;
        if (columnOpacity <= 0) continue;
        SkAutoCanvasRestore restore(&canvas, pose.transformed);
        if (pose.transformed) canvas.concat(pose.matrix);
        const bool filled = index <= state.activeIndex;
        for (const auto& layer : bars[index].glow) {
            if (!glowBrushes[filled] && !layer.visible(filled)) continue;
            auto source = layer.createSource(canvas);
            if (!source.surface) continue;
            auto& target = *source.surface->getCanvas();
            if (glowBrushes[filled]) layer.drawCellWithShader(target, 0, filled, glowBrushes[filled], columnOpacity);
            else layer.drawCell(target, 0, filled, std::nullopt, nullptr, columnOpacity);
            layer.composite(canvas, source);
        }
    }
    SkPaint paint;
    paint.setAntiAlias(true);
    struct Brushes {
        sk_sp<SkShader> fill, stroke, outer, shadow;
    };
    const auto brushesFor = [&](const Palette& look) {
        return Brushes{shaderAt(look.fillImage, look.fillGradient), shaderAt(look.strokeImage, look.strokeGradient),
                       shaderAt(look.outerImage, look.outerGradient), shaderAt(look.shadowImage, look.shadowGradient)};
    };
    const std::array brushes{brushesFor(palette[0]), brushesFor(palette[1])};
    const auto column = [&](int index, bool filled) {
        const auto pose = poseAt(index);
        const float columnOpacity = opacity * pose.opacity;
        if (columnOpacity <= 0) return;
        SkAutoCanvasRestore restore(&canvas, pose.transformed);
        if (pose.transformed) canvas.concat(pose.matrix);
        const auto draw = [&](const SkRRect& rect, SkColor color, float width, sk_sp<SkShader> shader = nullptr) {
            if (!shader && !SkColorGetA(color)) return;
            paint.setColor(shader ? SK_ColorWHITE : color);
            paint.setShader(std::move(shader));
            paint.setAlphaf(paint.getAlphaf() * columnOpacity);
            paint.setStyle(width > 0 ? SkPaint::kStroke_Style : SkPaint::kFill_Style);
            paint.setStrokeWidth(width);
            canvas.drawRRect(rect, paint);
        };
        const auto& rect = bars[index].rect;
        const auto& color = palette[filled];
        const auto& brush = brushes[filled];
        if (decorated) {
            if (SkColorGetA(color.shadow)) {
                const auto shadow = rect.makeOffset(color.shadowX, color.shadowY);
                if (strokeWidth + outerWidth > 0) draw(shadow, color.shadow, strokeWidth + outerWidth, brush.shadow);
                draw(shadow, color.shadow, 0, brush.shadow);
            }
            if (outerWidth > 0) draw(rect, color.outerStroke, strokeWidth + outerWidth, brush.outer);
        } else {
            draw(rect, color.fill, 0);
        }
        if (strokeWidth > 0) draw(rect, color.stroke, strokeWidth, brush.stroke);
        if (decorated) draw(rect, color.fill, 0, brush.fill);
    };
    for (int index = state.activeIndex + 1; index < clock.count; ++index) column(index, false);
    for (int index = 0; index <= state.activeIndex; ++index) column(index, true);
}
}
