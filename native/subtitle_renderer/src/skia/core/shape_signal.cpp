#include "shape_signal.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include "include/core/SkSamplingOptions.h"
#include <algorithm>

namespace subtitle::skia {
void ShapeSignal::draw(SkCanvas& canvas, int time) const {
    const auto state = krok::subtitle::native::shapeSignalState(clock, time);
    if (!state.visible || opacity <= 0) return;
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto drawShape = [&](const SkRect& box, SkColor color, float width) {
        paint.setColor(color);
        paint.setAlphaf(paint.getAlphaf() * opacity);
        paint.setStyle(width > 0 ? SkPaint::kStroke_Style : SkPaint::kFill_Style);
        paint.setStrokeWidth(width);
        switch (shape) {
        case Shape::Image: // Missing image falls back to a solid circle.
        case Shape::Circle: canvas.drawOval(box, paint); break;
        case Shape::Square: canvas.drawRect(box, paint); break;
        case Shape::Rounded:
            const float radius = std::max(size * .22f, 1.f);
            canvas.drawRoundRect(box, radius, radius, paint);
            break;
        }
    };
    const float shadowOffset = std::max(size * .08f, 1.f);
    for (int index = 0; index <= state.activeIndex; ++index) {
        const bool active = index == state.activeIndex;
        if (active && state.activeOpacity <= 0) continue;
        const float x = origin.x() + index * pitch + (active ? state.dx : 0.f);
        const float y = origin.y() + (active ? state.dy : 0.f);
        const auto box = SkRect::MakeXYWH(x, y, size, size);
        const bool fading = active && state.activeOpacity < 1;
        SkAutoCanvasRestore restore(&canvas, false);
        if (fading) {
            const float pad = (strokeWidth + strokeSoften) * .5f + shadowOffset + 2.f;
            const auto bounds = box.makeOutset(pad, pad);
            canvas.saveLayerAlphaf(&bounds, state.activeOpacity);
        }
        if (shape == Shape::Image) {
            if (image) {
                const float fit = size / std::max(image->width(), image->height());
                const float width = image->width() * fit, height = image->height() * fit;
                const auto dst = SkRect::MakeXYWH(box.centerX() - width * .5f, box.centerY() - height * .5f, width, height);
                paint.setAlphaf(opacity);
                canvas.drawImageRect(image, dst, SkSamplingOptions(SkFilterMode::kLinear), &paint);
            } else {
                drawShape(box, fill, 0);
                if (strokeWidth > 0 && SkColorGetA(stroke) > 0) drawShape(box, stroke, strokeWidth);
            }
            continue;
        }
        if (shadow) drawShape(box.makeOffset(shadowOffset, shadowOffset), SkColorSetA(SK_ColorBLACK, 89), 0);
        if (strokeWidth > 0 && strokeSoften > 0) {
            drawShape(box, fill, 0);
            drawShape(box, SkColorSetA(stroke, 71), strokeWidth + strokeSoften);
        }
        drawShape(box, fill, 0);
        if (strokeWidth > 0 && SkColorGetA(stroke) > 0)
            drawShape(box, stroke, strokeWidth);
        if (highlight > 0) {
            paint.setColor(SK_ColorWHITE);
            paint.setAlphaf(opacity * highlight);
            paint.setStyle(SkPaint::kFill_Style);
            canvas.drawCircle(x + size * .34f, y + size * .34f, size * .16f, paint);
        }
    }
}
}
