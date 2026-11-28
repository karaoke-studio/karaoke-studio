#pragma once

#include "subtitle_gradient.h"
#include "../../effects/effect_constants.h"
#include "../../model/particle_types.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include <algorithm>
#include <cmath>
#include <utility>
#include <vector>

namespace subtitle::skia {

// Authored paint and widths use the same 1000-unit space as the sprite path.
class ParticlePaint {
    struct Layer {
        static constexpr int lineSteps = 32;

        static SkColor color(const krok::subtitle::native::RgbaColor& value) {
            return SkColorSetARGB(value.alpha, value.red, value.green, value.blue);
        }

        static SkColor sample(const krok::subtitle::native::PaintStyle& source, float position) {
            if (source.stops.empty()) return color(source.color);
            if (source.mode == "split_vertical") {
                const auto next = std::upper_bound(source.stops.begin(), source.stops.end(), position,
                    [](float value, const auto& stop) { return value < stop.position; });
                return color((next == source.stops.begin() ? *next : *(next - 1)).color);
            }
            const auto right = std::lower_bound(source.stops.begin(), source.stops.end(), position,
                [](const auto& stop, float value) { return stop.position < value; });
            if (right == source.stops.begin()) return color(right->color);
            if (right == source.stops.end()) return color(source.stops.back().color);
            const auto& left = *(right - 1);
            const float t = (position - left.position) / (right->position - left.position);
            const auto channel = [t](int a, int b) { return int(std::lround(a + (b - a) * t)); };
            return SkColorSetARGB(channel(left.color.alpha, right->color.alpha),
                channel(left.color.red, right->color.red), channel(left.color.green, right->color.green),
                channel(left.color.blue, right->color.blue));
        }

        Layer(const krok::subtitle::native::PaintStyle& source, sk_sp<SkShader> image, bool sampleRadius = false,
              bool lineSampled = true)
            : tint(color(source.color)), shader(std::move(image)) {
            const bool radial = sampleRadius && (source.mode == "gradient_vertical"
                || source.mode == "gradient_radial" || source.mode == "split_vertical");
            if ((lineSampled && source.mode == "gradient_horizontal") || radial) {
                samples.reserve(lineSteps + 1);
                for (int i = 0; i <= lineSteps; ++i)
                    samples.push_back(sample(source, float(i) / lineSteps));
            } else if (!shader) {
                shader = lineGradient(source, SkRect::MakeLTRB(-500, -500, 500, 500), 0);
            }
        }

        void apply(SkPaint& paint, float opacity, int lineSample = 0) const {
            paint.setColor(shader ? SK_ColorWHITE : colorAt(lineSample));
            paint.setShader(shader);
            paint.setAlphaf(paint.getAlphaf() * opacity);
        }

        SkColor colorAt(int index) const { return samples.empty() ? tint : samples[index]; }

        SkColor tint;
        sk_sp<SkShader> shader;
        std::vector<SkColor> samples;
    };

public:
    struct Images {
        sk_sp<SkShader> fill, inner, outer, decoration;
    };

    ParticlePaint(const krok::subtitle::native::ParticleBurst& source, float lineLeft, float lineSpan, Images images)
        : fill_(source.fill, std::move(images.fill), source.kind == "ripple"),
          inner_(source.stroke, std::move(images.inner)),
          outer_(source.stroke2, std::move(images.outer)),
          // Like the GPU, the decoration brush spans the sprite's em box, not the line.
          decoration_(source.decor, std::move(images.decoration), false, false),
          innerWidth_(source.strokeWidth * 1000.f / source.sizePx),
          outerWidth_(source.stroke2Width * 1000.f / source.sizePx),
          shadow_(source.hasDecor && source.decorKind == "shadow"),
          shadowOffset_{source.decorOffsetX, source.decorOffsetY},
          glowRadius_(source.hasDecor ? source.decorRadius * 1000.f / source.sizePx : 0.f),
          glowPasses_(source.decorConcentration + 1),
          lineMapped_(source.fill.mode == "gradient_horizontal"
              || source.stroke.mode == "gradient_horizontal" || source.stroke2.mode == "gradient_horizontal"),
          radiusMapped_(source.kind == "ripple" && !fill_.samples.empty() && !lineMapped_),
          lineLeft_(lineLeft), lineSpan_(lineSpan) {}

    SkColor4f ringColor(float x, float sizeRatio) const {
        // The evaluator grows the ripple's diameter from .25 to 1.10 times the burst size.
        const int sample = radiusMapped_ ? quantizedPosition((sizeRatio - .25f) / .85f) : lineIndex(x);
        return SkColor4f::FromColor(fill_.colorAt(sample));
    }

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
        // The legacy renderer samples a uniform sprite color in 1/32 steps across the line.
        const int lineSample = lineIndex(pose.getTranslateX());
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
            outer_.apply(paint, opacity, lineSample);
            paint.setStrokeWidth(innerWidth_ + outerWidth_);
            canvas.drawPath(path, paint);
        }
        if (innerWidth_ >= 1.f) {
            inner_.apply(paint, opacity, lineSample);
            paint.setStrokeWidth(innerWidth_);
            canvas.drawPath(path, paint);
        }
        paint.setStyle(SkPaint::kFill_Style);
        fill_.apply(paint, opacity, lineSample);
        canvas.drawPath(path, paint);
    }

private:
    static int quantizedPosition(float fraction) {
        return int(std::lround(std::clamp(fraction, 0.f, 1.f) * Layer::lineSteps));
    }

    int lineIndex(float x) const {
        return lineMapped_ ? quantizedPosition((x - lineLeft_) / lineSpan_) : 0;
    }

    Layer fill_, inner_, outer_, decoration_;
    float innerWidth_, outerWidth_;
    bool shadow_;
    SkPoint shadowOffset_;
    float glowRadius_;
    int glowPasses_;
    bool lineMapped_, radiusMapped_;
    float lineLeft_, lineSpan_;
};
}
