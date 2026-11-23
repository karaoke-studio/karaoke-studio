#include "solid_outline.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkImage.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPathUtils.h"
#include "include/core/SkSurface.h"
#include "include/effects/SkRuntimeEffect.h"
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
SkPath prepareOutlinePath(const SkPath& body, float width, bool includeBody) {
    SkPaint stroke;
    stroke.setStyle(includeBody ? SkPaint::kStrokeAndFill_Style : SkPaint::kStroke_Style);
    stroke.setStrokeWidth(width);
    stroke.setStrokeJoin(SkPaint::kRound_Join);
    bool isFill = false;
    auto path = skpathutils::FillPathWithPaint(body, stroke, &isFill);
    if (!isFill || !path.isFinite())
        throw std::runtime_error("Skia 无法准备正文描边轮廓");
    return path;
}
void drawFixedOutline(SkCanvas& canvas, const SkPath& path, const SkPaint& paint, float width) {
    SkPaint stroke = paint;
    stroke.setStyle(SkPaint::kStroke_Style);
    stroke.setStrokeWidth(width);
    stroke.setStrokeJoin(SkPaint::kRound_Join);
    canvas.drawPath(path, stroke);
}

void drawSupersampledSolidPath(SkCanvas& canvas, const SkPath& path, const SkPaint& fill, const SkPaint& composite) {
    const auto devicePath = path.makeTransform(canvas.getTotalMatrix());
    auto bounds = devicePath.computeTightBounds().makeOutset(1.f, 1.f).roundOut();
    if (!bounds.intersect(canvas.getDeviceClipBounds())) return;

    constexpr int samples = 2;
    auto surface = canvas.makeSurface(canvas.imageInfo().makeWH(samples * bounds.width(), samples * bounds.height()));
    if (!surface) throw std::runtime_error("Skia 字幕笔画覆盖表面分配失败");

    auto& mask = *surface->getCanvas();
    mask.clear(SK_ColorTRANSPARENT);
    mask.scale(float(samples), float(samples));
    mask.translate(float(-bounds.left()), float(-bounds.top()));
    mask.drawPath(devicePath, fill);
    auto image = surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 字幕笔画覆盖表面读取失败");

    SkAutoCanvasRestore restore(&canvas, true);
    canvas.resetMatrix();
    canvas.drawImageRect(image, SkRect::Make(bounds), SkSamplingOptions(SkFilterMode::kLinear), &composite);
}

void drawSamplePatternPath(SkCanvas& canvas, const SkPath& path, const SkPaint& fill, bool quantize) {
    const auto devicePath = path.makeTransform(canvas.getTotalMatrix());
    auto bounds = devicePath.computeTightBounds().roundOut();
    if (!bounds.intersect(canvas.getDeviceClipBounds())) return;
    // The fixed sample locations are spaced at 1/16 of a device pixel.
    // Bound the coverage mask when an authored sprite is enlarged.
    constexpr int scale = 16;
    constexpr int maxExtent = 200;
    if (bounds.width() > maxExtent || bounds.height() > maxExtent) {
        canvas.drawPath(path, fill);
        return;
    }
    auto surface = canvas.makeSurface(
        SkImageInfo::MakeA8(bounds.width() * scale, bounds.height() * scale));
    if (!surface) surface = canvas.makeSurface(
        SkImageInfo::MakeN32Premul(bounds.width() * scale, bounds.height() * scale));
    if (!surface) throw std::runtime_error("Skia 粒子采样覆盖表面分配失败");
    auto& mask = *surface->getCanvas();
    mask.clear(SK_ColorTRANSPARENT);
    mask.translate(.5f, .5f);
    mask.scale(scale, scale);
    mask.translate(float(-bounds.left()), float(-bounds.top()));
    mask.drawPath(devicePath, SkPaint{});
    auto image = surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 粒子采样覆盖表面读取失败");
    auto shader = image->makeShader(SkTileMode::kDecal, SkTileMode::kDecal, SkSamplingOptions{});
    static const auto effect = SkRuntimeEffect::MakeForShader(SkString(R"(
        uniform shader mask;
        uniform float2 origin;
        uniform float4 tint;
        uniform int quantize;
        half4 main(float2 position) {
            float2 p = (position - origin) * 16.0 + 0.5;
            float count = mask.eval(p + float2(1,1)).a + mask.eval(p + float2(-1,-3)).a
                + mask.eval(p + float2(-3,2)).a + mask.eval(p + float2(4,-1)).a
                + mask.eval(p + float2(-5,-2)).a + mask.eval(p + float2(2,5)).a
                + mask.eval(p + float2(5,3)).a + mask.eval(p + float2(3,-5)).a
                + mask.eval(p + float2(-2,6)).a + mask.eval(p + float2(0,-7)).a
                + mask.eval(p + float2(-4,-6)).a + mask.eval(p + float2(-6,4)).a
                + mask.eval(p + float2(-8,0)).a + mask.eval(p + float2(7,-4)).a
                + mask.eval(p + float2(6,7)).a + mask.eval(p + float2(-7,-8)).a;
            float amount = count / 16.0;
            if (quantize != 0) return half4(floor(tint * amount * 255.0 + 0.5) / 255.0);
            return half4(tint) * half(amount);
        }
    )"));
    if (!effect.effect) throw std::runtime_error(effect.errorText.c_str());
    SkRuntimeShaderBuilder builder(effect.effect);
    builder.child("mask") = std::move(shader);
    builder.uniform("origin") = SkV2{float(bounds.left()), float(bounds.top())};
    const auto color = fill.getColor4f();
    constexpr float opacitySteps = 4096.f;
    const float alpha = quantize ? std::floor(color.fA * opacitySteps) / opacitySteps : color.fA;
    builder.uniform("tint") = SkV4{color.fR * alpha, color.fG * alpha, color.fB * alpha, alpha};
    builder.uniform("quantize") = int(quantize);
    auto coverage = builder.makeShader();
    if (!coverage) throw std::runtime_error("Skia 粒子采样覆盖着色器初始化失败");
    SkPaint paint;
    paint.setShader(std::move(coverage));
    SkAutoCanvasRestore restore(&canvas, true);
    canvas.resetMatrix();
    canvas.drawRect(SkRect::Make(bounds), paint);
}
}
