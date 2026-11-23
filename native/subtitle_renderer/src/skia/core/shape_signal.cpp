#include "shape_signal.h"
#include "character_halo.h"
#include "signal_transition.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include "include/core/SkSamplingOptions.h"
#include <algorithm>

namespace subtitle::skia {
SkPath ShapeSignal::outline() const {
    if (path) return path->body;
    const auto bounds = SkRect::MakeWH(size, size);
    if (shape == Shape::Square) return SkPath::Rect(bounds);
    if (shape == Shape::Rounded) {
        const float radius = std::max(size * .22f, 1.f);
        return SkPath::RRect(bounds, radius, radius);
    }
    return SkPath::Oval(bounds);
}

void ShapeSignal::draw(SkCanvas& canvas, int time, const SignalTransition& transition, float entryOpacity) const {
    const auto state = krok::subtitle::native::shapeSignalState(clock, time);
    const float signalOpacity = opacity * entryOpacity;
    if (!state.visible || state.activeIndex < 0 || signalOpacity <= 0) return;
    SkAutoCanvasRestore group(&canvas, false);
    if (decoration && signalOpacity < 1) canvas.saveLayerAlphaf(nullptr, signalOpacity);
    const float brushOpacity = decoration ? 1.f : signalOpacity;
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto drawShape = [&](const SkRect& box, SkColor color, float width, sk_sp<SkShader> shader = nullptr) {
        paint.setColor(shader ? SK_ColorWHITE : color);
        paint.setShader(shader);
        paint.setAlphaf(paint.getAlphaf() * brushOpacity);
        paint.setStyle(width > 0 ? SkPaint::kStroke_Style : SkPaint::kFill_Style);
        paint.setStrokeWidth(width);
        if (path) {
            SkAutoCanvasRestore restore(&canvas, true);
            canvas.translate(box.left(), box.top());
            canvas.drawPath(path->body, paint);
            return;
        }
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
    const auto boxAt = [&](int index) {
        const bool active = index == state.activeIndex;
        return SkRect::MakeXYWH(origin.x() + index * pitch + (active ? state.dx : 0.f),
                               origin.y() + (active ? state.dy : 0.f), size, size);
    };
    sk_sp<SkShader> fillGradient;
    if (decoration && decoration->unitFillGradient) {
        // Include every slot so extinguishing a lamp cannot change its neighbours' colors.
        SkRect bounds = boxAt(0);
        for (int index = 1; index < clock.count; ++index) bounds.join(boxAt(index));
        // The existing GPU applies the group brush in the translated coordinates of a vector lamp.
        if (path) bounds.offset(-decoration->glyphPaintOrigin.x(), -decoration->glyphPaintOrigin.y());
        fillGradient = decoration->unitFillGradient->makeWithLocalMatrix(
            SkMatrix::RectToRect(SkRect::MakeWH(1, 1), bounds));
    }
    const float shadowOffset = std::max(size * .08f, 1.f);
    for (int index = 0; index <= state.activeIndex; ++index) {
        const bool active = index == state.activeIndex;
        const auto box = boxAt(index);
        const float x = box.left(), y = box.top();
        auto pose = transition.at(time, index, clock.count, box.centerX(), box.centerY(), true);
        const float countdownOpacity = active ? state.activeOpacity : 1.f;
        if (pose.glowAlpha > 0 && countdownOpacity > 0) {
            auto contour = outline().makeOffset(x, y);
            const bool strokeInPose = pose.transformed && !pose.glowEcho;
            SkAutoCanvasRestore halo(&canvas, strokeInPose);
            if (strokeInPose) canvas.concat(pose.matrix);
            else if (pose.transformed) contour = contour.makeTransform(pose.matrix);
            drawCharacterHalo(canvas, contour, pose, transition.geometry.fontPx, brushOpacity * countdownOpacity);
        }
        const float alpha = pose.opacity * countdownOpacity;
        if (alpha <= 0) continue;
        if (decoration && transition.singingScale.zoomPulse) {
            const auto duration = std::int64_t(clock.activeDurationMs);
            const auto elapsed = std::max(time - (clock.endMs - duration), std::int64_t{0});
            const auto start = duration * (clock.count - index - 1) / clock.count;
            const auto end = duration * (clock.count - index) / clock.count;
            transition.applyZoomPulse(pose, elapsed, start, end, box.centerX(), box.centerY());
        }
        SkAutoCanvasRestore restore(&canvas, pose.transformed);
        if (pose.transformed) canvas.concat(pose.matrix);
        if (alpha < 1) {
            const float pad = (strokeWidth + strokeSoften) * .5f + shadowOffset + 2.f;
            const auto bounds = box.makeOutset(pad, pad);
            canvas.saveLayerAlphaf(decoration ? nullptr : &bounds, alpha);
        }
        if (decoration && !decoration->glow.empty()) {
            SkAutoCanvasRestore local(&canvas, true);
            canvas.translate(x, y);
            for (const auto& layer : decoration->glow) {
                if (!layer.visible(true)) continue;
                auto source = layer.createSource(canvas);
                if (!source.surface) continue;
                layer.drawCell(*source.surface->getCanvas(), 0, true, std::nullopt);
                layer.composite(canvas, source);
            }
        }
        if (image) {
            const float fit = size / std::max(image->width(), image->height());
            const float width = image->width() * fit, height = image->height() * fit;
            const auto dst = SkRect::MakeXYWH(box.centerX() - width * .5f, box.centerY() - height * .5f, width, height);
            paint.setAlphaf(brushOpacity);
            canvas.drawImageRect(image, dst, SkSamplingOptions(SkFilterMode::kLinear), &paint);
            continue;
        }
        if (decoration) {
            if (SkColorGetA(decoration->shadow)) {
                const auto shadowBox = box.makeOffset(decoration->shadowX, decoration->shadowY);
                const float width = strokeWidth + decoration->outerWidth;
                if (width > 0) drawShape(shadowBox, decoration->shadow, width);
                drawShape(shadowBox, decoration->shadow, 0);
            }
            if (decoration->outerWidth > 0 && SkColorGetA(decoration->outerStroke))
                drawShape(box, decoration->outerStroke, strokeWidth + decoration->outerWidth);
        } else if (shape != Shape::Image) {
            if (shadow) drawShape(box.makeOffset(shadowOffset, shadowOffset), SkColorSetA(SK_ColorBLACK, 89), 0);
            if (strokeWidth > 0 && strokeSoften > 0) {
                drawShape(box, fill, 0);
                drawShape(box, SkColorSetA(stroke, 71), strokeWidth + strokeSoften);
            }
        }
        if (!decoration)
            drawShape(box, fill, 0);
        if (strokeWidth > 0 && SkColorGetA(stroke) > 0)
            drawShape(box, stroke, strokeWidth);
        if (decoration)
            drawShape(box, fill, 0, fillGradient);
        if (highlight > 0 && (decoration || shape != Shape::Image)) {
            paint.setShader(nullptr);
            paint.setColor(SK_ColorWHITE);
            paint.setAlphaf(brushOpacity * highlight);
            paint.setStyle(SkPaint::kFill_Style);
            const auto center = path ? path->highlightCenter : SkPoint::Make(size * .34f, size * .34f);
            const float radius = path ? path->highlightRadius : size * .16f;
            canvas.drawCircle(x + center.x(), y + center.y(), radius, paint);
        }
    }
}
}
