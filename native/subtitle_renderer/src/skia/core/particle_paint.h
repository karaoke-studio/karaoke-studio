#pragma once

#include "subtitle_gradient.h"
#include "../../effects/effect_constants.h"
#include "../../model/particle_types.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"

namespace subtitle::skia {

// Authored paint and widths use the same 1000-unit space as the sprite path.
class ParticlePaint {
    struct Layer {
        explicit Layer(const krok::subtitle::native::PaintStyle& source)
            : tint(SkColorSetARGB(source.color.alpha, source.color.red, source.color.green, source.color.blue)),
              shader(lineGradient(source, SkRect::MakeLTRB(-500, -500, 500, 500), 0)) {}

        void apply(SkPaint& paint, float opacity) const {
            paint.setColor(shader ? SK_ColorWHITE : tint);
            paint.setShader(shader);
            paint.setAlphaf(paint.getAlphaf() * opacity);
        }

        SkColor tint;
        sk_sp<SkShader> shader;
    };

public:
    explicit ParticlePaint(const krok::subtitle::native::ParticleBurst& source)
        : fill_(source.fill), inner_(source.stroke), outer_(source.stroke2), decoration_(source.decor),
          innerWidth_(source.strokeWidth * 1000.f / source.sizePx),
          outerWidth_(source.stroke2Width * 1000.f / source.sizePx),
          shadow_(source.hasDecor && source.decorKind == "shadow"),
          shadowOffset_{source.decorOffsetX, source.decorOffsetY},
          glowRadius_(source.hasDecor ? source.decorRadius * 1000.f / source.sizePx : 0.f),
          glowPasses_(source.decorConcentration + 1) {}

    void draw(SkCanvas& canvas, const SkPath& path, const SkMatrix& pose, float opacity) const {
        SkAutoCanvasRestore restore(&canvas, true);
        SkPaint paint;
        paint.setAntiAlias(true);
        paint.setStrokeJoin(SkPaint::kRound_Join);
        paint.setStrokeCap(SkPaint::kRound_Cap);
        if (shadow_) {
            SkAutoCanvasRestore restorePose(&canvas, true);
            // The offset is in line coordinates, independent of sprite rotation and scale.
            canvas.translate(shadowOffset_.x(), shadowOffset_.y());
            canvas.concat(pose);
            decoration_.apply(paint, opacity);
            const float width = innerWidth_ + outerWidth_;
            if (width >= 1.f) {
                paint.setStyle(SkPaint::kStroke_Style);
                paint.setStrokeWidth(width);
                canvas.drawPath(path, paint);
            }
            paint.setStyle(SkPaint::kFill_Style);
            canvas.drawPath(path, paint);
        }
        canvas.concat(pose);
        paint.setStyle(SkPaint::kStroke_Style);
        if (glowRadius_ > 0.f) {
            decoration_.apply(paint, opacity);
            const float alpha = paint.getAlphaf();
            using krok::subtitle::native::effects::kGlowHaloStrokes;
            for (int pass = glowPasses_; pass > 0; --pass) {
                const float radius = glowRadius_ * float(pass) / float(glowPasses_);
                for (const auto& halo : kGlowHaloStrokes) {
                    const float width = radius * halo.widthEm;
                    if (width < .5f) continue;
                    paint.setStrokeWidth(width);
                    paint.setAlphaf(alpha * halo.strength);
                    canvas.drawPath(path, paint);
                }
            }
        }
        if (outerWidth_ >= 1.f) {
            outer_.apply(paint, opacity);
            paint.setStrokeWidth(innerWidth_ + outerWidth_);
            canvas.drawPath(path, paint);
        }
        if (innerWidth_ >= 1.f) {
            inner_.apply(paint, opacity);
            paint.setStrokeWidth(innerWidth_);
            canvas.drawPath(path, paint);
        }
        paint.setStyle(SkPaint::kFill_Style);
        fill_.apply(paint, opacity);
        canvas.drawPath(path, paint);
    }

private:
    Layer fill_, inner_, outer_, decoration_;
    float innerWidth_, outerWidth_;
    bool shadow_;
    SkPoint shadowOffset_;
    float glowRadius_;
    int glowPasses_;
};
}
