#pragma once

#include "subtitle_gradient.h"
#include "../../effects/effect_constants.h"
#include "../../model/particle_types.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"

namespace subtitle::skia {

// Authored fills and glow widths use the same 1000-unit sprite space as the outline.
class ParticlePaint {
public:
    explicit ParticlePaint(const krok::subtitle::native::ParticleBurst& source)
        : fill_(color(source.fill.color)), glow_(color(source.decor.color)),
          gradient_(lineGradient(source.fill, SkRect::MakeLTRB(-500, -500, 500, 500), 0)),
          glowGradient_(lineGradient(source.decor, SkRect::MakeLTRB(-500, -500, 500, 500), 0)),
          glowRadius_(source.hasDecor ? source.decorRadius * 1000.f / source.sizePx : 0.f),
          glowPasses_(source.decorConcentration + 1) {}

    void draw(SkCanvas& canvas, const SkPath& path, const SkMatrix& pose, float opacity) const {
        SkAutoCanvasRestore restore(&canvas, true);
        canvas.concat(pose);
        SkPaint paint;
        paint.setAntiAlias(true);
        if (glowRadius_ > 0.f) {
            paint.setStrokeJoin(SkPaint::kRound_Join);
            paint.setStrokeCap(SkPaint::kRound_Cap);
            paint.setStyle(SkPaint::kStroke_Style);
            paint.setColor(glowGradient_ ? SK_ColorWHITE : glow_);
            paint.setShader(glowGradient_);
            const float alpha = paint.getAlphaf() * opacity;
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
        paint.setStyle(SkPaint::kFill_Style);
        paint.setColor(gradient_ ? SK_ColorWHITE : fill_);
        paint.setShader(gradient_);
        paint.setAlphaf(paint.getAlphaf() * opacity);
        canvas.drawPath(path, paint);
    }

private:
    static SkColor color(const krok::subtitle::native::RgbaColor& value) {
        return SkColorSetARGB(value.alpha, value.red, value.green, value.blue);
    }

    SkColor fill_, glow_;
    sk_sp<SkShader> gradient_, glowGradient_;
    float glowRadius_;
    int glowPasses_;
};
}
