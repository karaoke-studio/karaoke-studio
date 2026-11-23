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

void VolumeSignal::draw(SkCanvas& canvas, int time, const SignalTransition& transition) const {
    const auto state = krok::subtitle::native::volumeSignalState(clock, time);
    if (!state.visible || state.opacity <= 0 || opacity <= 0) return;
    SkAutoCanvasRestore flashLayer(&canvas, false);
    if (state.opacity < 1) canvas.saveLayerAlphaf(nullptr, state.opacity);
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
            if (!layer.visible(filled)) continue;
            auto source = layer.createSource(canvas);
            if (!source.surface) continue;
            layer.drawCell(*source.surface->getCanvas(), 0, filled, std::nullopt, nullptr, columnOpacity);
            layer.composite(canvas, source);
        }
    }
    SkPaint paint;
    paint.setAntiAlias(true);
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
        if (decorated) {
            if (SkColorGetA(color.shadow)) {
                const auto shadow = rect.makeOffset(color.shadowX, color.shadowY);
                if (strokeWidth + outerWidth > 0) draw(shadow, color.shadow, strokeWidth + outerWidth);
                draw(shadow, color.shadow, 0);
            }
            if (outerWidth > 0) draw(rect, color.outerStroke, strokeWidth + outerWidth);
        } else {
            draw(rect, color.fill, 0);
        }
        if (strokeWidth > 0) draw(rect, color.stroke, strokeWidth);
        if (decorated) draw(rect, color.fill, 0, fillGradients[filled]);
    };
    for (int index = state.activeIndex + 1; index < clock.count; ++index) column(index, false);
    for (int index = 0; index <= state.activeIndex; ++index) column(index, true);
}
}
