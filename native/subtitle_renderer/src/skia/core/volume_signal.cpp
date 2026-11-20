#include "volume_signal.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"

namespace subtitle::skia {
void VolumeSignal::draw(SkCanvas& canvas, int time) const {
    const auto state = krok::subtitle::native::volumeSignalState(clock, time);
    if (!state.visible || state.opacity <= 0 || opacity <= 0) return;
    SkAutoCanvasRestore flashLayer(&canvas, false);
    if (state.opacity < 1) canvas.saveLayerAlphaf(nullptr, state.opacity);
    for (int index = 0; index < clock.count; ++index) {
        const bool filled = index <= state.activeIndex;
        for (const auto& layer : bars[index].glow) {
            if (!layer.visible(filled)) continue;
            auto source = layer.createSource(canvas);
            if (!source.surface) continue;
            layer.drawCell(*source.surface->getCanvas(), 0, filled, std::nullopt, nullptr, opacity);
            layer.composite(canvas, source);
        }
    }
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto draw = [&](const SkRRect& rect, SkColor color, float width) {
        if (!SkColorGetA(color)) return;
        paint.setColor(color);
        paint.setAlphaf(paint.getAlphaf() * opacity);
        paint.setStyle(width > 0 ? SkPaint::kStroke_Style : SkPaint::kFill_Style);
        paint.setStrokeWidth(width);
        canvas.drawRRect(rect, paint);
    };
    const auto column = [&](int index, bool filled) {
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
        if (decorated) draw(rect, color.fill, 0);
    };
    for (int index = state.activeIndex + 1; index < clock.count; ++index) column(index, false);
    for (int index = 0; index <= state.activeIndex; ++index) column(index, true);
}
}
