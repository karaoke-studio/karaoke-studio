#include "line_band.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkPaint.h"
#include "include/core/SkString.h"
#include "include/core/SkSurface.h"
#include "include/effects/SkImageFilters.h"
#include "include/effects/SkRuntimeEffect.h"
#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <stdexcept>
#include <utility>

namespace subtitle::skia {
namespace {
constexpr int maxBlurRadius = 127;
constexpr int smallBlurRadius = 15;

sk_sp<SkImageFilter> blurMask(int radius, int spread) {
    const int length = 2 * radius + 1;
    std::vector<float> weights(length, 1.f / length);
    auto layer = spread ? SkImageFilters::Dilate(spread, spread, nullptr) : nullptr;
    // The authored blur radius specifies three passes of a box kernel.
    for (int pass = 0; pass < 3; ++pass) {
        layer = SkImageFilters::MatrixConvolution({length, 1}, weights.data(), 1.f, 0, {radius, 0},
                                                   SkTileMode::kDecal, true, std::move(layer));
        layer = SkImageFilters::MatrixConvolution({1, length}, weights.data(), 1.f, 0, {0, radius},
                                                   SkTileMode::kDecal, true, std::move(layer));
    }
    return layer;
}

sk_sp<SkColorFilter> blurTint(const LineBandTexture::Blur& effect, float opacity) {
    auto tint = SkColor4f::FromColor(effect.color);
    tint.fA *= opacity;
    auto color = SkColorFilters::Blend(tint, nullptr, SkBlendMode::kSrcIn);
    if (effect.range != .5f) {
        const float gamma = .5f / std::clamp(effect.range, .01f, 1.f);
        std::array<std::uint8_t, 256> alpha{};
        for (std::size_t i = 0; i < alpha.size(); ++i)
            alpha[i] = std::uint8_t(std::lround(std::pow(float(i) / 255.f, gamma) * 255.f));
        color = color->makeComposed(SkColorFilters::TableARGB(alpha.data(), nullptr, nullptr, nullptr));
    }
    return color;
}

sk_sp<SkImage> wideBlurMask(SkCanvas& canvas, const SkImage& image, int radius, int spread,
                           const LineBandTexture::Blur& style) {
    static const auto effect = [] {
        auto result = SkRuntimeEffect::MakeForShader(SkString(R"(
            uniform shader mask;
            uniform shader original;
            uniform float2 axis;
            uniform float4 window;
            half4 main(float2 p) {
                p += window.y * axis;
                float alpha = (mask.eval(p).a + mask.eval(p + window.x * axis).a) * window.z;
                return half4(alpha + original.eval(p + 2.0 * window.x * axis).a * window.w);
            }
        )"));
        if (!result.effect) throw std::runtime_error(result.errorText.c_str());
        return result.effect;
    }();
    const int pad = 3 * radius + spread;
    const auto info = SkImageInfo::MakeN32Premul(image.width() + 2 * pad, image.height() + 2 * pad);
    const auto intermediate = info.makeColorType(kRGBA_F16_SkColorType);
    auto first = canvas.makeSurface(intermediate);
    auto partial = canvas.makeSurface(intermediate);
    auto next = canvas.makeSurface(intermediate);
    if (!first || !partial || !next)
        throw std::runtime_error("Skia 行背景带大半径滤镜表面分配失败");
    auto& source = *first->getCanvas();
    source.clear(SK_ColorTRANSPARENT);
    SkPaint initial;
    if (spread) initial.setImageFilter(SkImageFilters::Dilate(spread, spread, nullptr));
    source.drawImage(&image, pad, pad, SkSamplingOptions(), &initial);

    const auto extent = SkRect::MakeIWH(info.width(), info.height());
    const auto maskShader = [](SkSurface& surface) {
        auto image = surface.makeImageSnapshot();
        if (!image) throw std::runtime_error("Skia 行背景带大半径滤镜遮罩分配失败");
        return image->makeShader(SkTileMode::kDecal, SkTileMode::kDecal, SkSamplingOptions());
    };
    SkRuntimeShaderBuilder builder(effect);
    SkPaint kernel;
    kernel.setBlendMode(SkBlendMode::kSrc);
    const unsigned length = 2 * radius + 1;
    for (int pass = 0; pass < 6; ++pass) {
        builder.uniform("axis") = pass % 2 ? SkV2{0, 1} : SkV2{1, 0};
        auto original = maskShader(*first);
        builder.child("original") = original;
        builder.child("mask") = original;
        // Grow a trailing average by doubling its span and appending one
        // original texel for each set bit. Recenter at the last step.
        int span = 1;
        for (unsigned bit = std::bit_floor(length) / 2; bit; bit /= 2) {
            const bool last = bit == 1;
            const int extra = (length & bit) != 0;
            const int nextSpan = 2 * span + extra;
            builder.uniform("window") = SkV4{float(span), last ? -float(radius) : 0.f,
                                             float(span) / nextSpan, float(extra) / nextSpan};
            auto shader = builder.makeShader();
            if (!shader) throw std::runtime_error("Skia 行背景带大半径滤镜着色器不可用");
            kernel.setShader(std::move(shader));
            next->getCanvas()->drawRect(extent, kernel);
            if (last) {
                std::swap(first, next);
            } else {
                std::swap(partial, next);
                builder.child("mask") = maskShader(*partial);
            }
            span = nextSpan;
        }
    }
    static const auto transfer = [] {
        auto result = SkRuntimeEffect::MakeForShader(SkString(R"(
            uniform shader mask;
            uniform float gamma;
            uniform float4 tint;
            half4 main(float2 p) {
                float alpha = pow(float(mask.eval(p).a), gamma) * tint.a;
                return half4(tint.rgb * alpha, alpha);
            }
        )"));
        if (!result.effect) throw std::runtime_error(result.errorText.c_str());
        return result.effect;
    }();
    // The reference converts to a byte texture after the complete blur and
    // range curve. Apply the row's opacity when sampling that texture.
    auto colored = canvas.makeSurface(info);
    if (!colored) throw std::runtime_error("Skia 行背景带大半径滤镜遮罩分配失败");
    SkRuntimeShaderBuilder curve(transfer);
    curve.child("mask") = maskShader(*first);
    curve.uniform("gamma") = .5f / std::clamp(style.range, .01f, 1.f);
    const auto tint = SkColor4f::FromColor(style.color);
    curve.uniform("tint") = SkV4{tint.fR, tint.fG, tint.fB, tint.fA};
    auto shader = curve.makeShader();
    if (!shader) throw std::runtime_error("Skia 行背景带大半径滤镜着色器不可用");
    SkPaint ink;
    ink.setShader(std::move(shader));
    ink.setBlendMode(SkBlendMode::kSrc);
    colored->getCanvas()->drawRect(extent, ink);
    auto output = colored->makeImageSnapshot();
    if (!output) throw std::runtime_error("Skia 行背景带大半径滤镜图像分配失败");
    return output;
}

void drawBlurTexture(SkCanvas& canvas, const SkImage& image, float x, float y, float opacity) {
    static const auto effect = [] {
        auto result = SkRuntimeEffect::MakeForShader(SkString(R"(
            uniform shader texture;
            uniform float opacity;
            half4 main(float2 p) {
                return texture.eval(p) * opacity;
            }
        )"));
        if (!result.effect) throw std::runtime_error(result.errorText.c_str());
        return result.effect;
    }();
    const auto offset = SkMatrix::Translate(x, y);
    SkRuntimeShaderBuilder builder(effect);
    builder.child("texture") = image.makeShader(SkTileMode::kClamp, SkTileMode::kClamp,
                                                SkSamplingOptions(SkFilterMode::kLinear), &offset);
    builder.uniform("opacity") = opacity;
    auto shader = builder.makeShader();
    if (!shader) throw std::runtime_error("Skia 行背景带大半径滤镜着色器不可用");
    SkPaint ink;
    ink.setShader(std::move(shader));
    canvas.drawRect(SkRect::MakeXYWH(x, y, image.width(), image.height()), ink);
}

sk_sp<SkImageFilter> strokeMask(const LineBandTexture::Stroke& effect, float scale) {
    using Position = LineBandTexture::Stroke::Position;
    const int size = std::max(1, int(std::lround(effect.size * scale)));
    const bool inside = effect.position == Position::Inside;
    const bool center = effect.position == Position::Center;
    const int radius = center ? std::max(1, size / 2) : size;
    auto expanded = inside ? nullptr : SkImageFilters::Dilate(radius, radius, nullptr);
    if (inside || center) {
        auto eroded = SkImageFilters::Erode(radius, radius, nullptr);
        expanded = SkImageFilters::Arithmetic(0, 1, -1, 0, true, std::move(eroded), std::move(expanded));
    }
    return SkImageFilters::ColorFilter(SkColorFilters::Blend(effect.color, SkBlendMode::kSrcIn),
                                       std::move(expanded));
}
}

void LineBandTexture::validateEffectBounds(const SkRect& bounds) const {
    if ((!stroke && !shadow && !glow) || !image || bounds.isEmpty()) return;
    const float scale = bounds.height() / image->height();
    const float width = std::ceil(bounds.width()), height = std::ceil(bounds.height());
    if (width > 8192 || height > 8192 || width * height > 16 * 1024 * 1024
        || (stroke && stroke->size * scale > 512))
        throw std::runtime_error("Skia 行背景带描边尺寸超限");
    for (const auto* effect : {&shadow, &glow}) {
        if (!*effect) continue;
        const float radius = (*effect)->size * scale, spread = (*effect)->spread * scale;
        if (radius >= maxBlurRadius + .5f || spread > 512)
            throw std::runtime_error("Skia 行背景带阴影或发光尺寸超限");
        if (radius >= smallBlurRadius + .5f) {
            const float border = 2 * (3 * std::lround(radius) + std::lround(spread));
            // Three RGBA F16 scratch surfaces, one RGBA8 mask and the source.
            if (width + border > 8192 || height + border > 8192
                || (width + border) * (height + border) > 8 * 1024 * 1024)
                throw std::runtime_error("Skia 行背景带大半径滤镜表面尺寸超限");
        }
    }
}

void LineBandTexture::drawDecorated(SkCanvas& canvas, const SkRect& bounds, float alpha, int timeMs) const {
    validateEffectBounds(bounds);
    const float scale = bounds.height() / image->height();
    auto visible = bounds;
    for (const auto* effect : {&shadow, &glow})
        if (*effect) {
            const float pad = (3 * (*effect)->size + (*effect)->spread) * scale + 2;
            visible.join(bounds.makeOffset((*effect)->dx, (*effect)->dy).makeOutset(pad, pad));
        }
    if (stroke) {
        const float pad = stroke->size * scale + 2;
        visible.join(bounds.makeOutset(pad, pad));
    }
    if (!SkRect::Intersects(visible, canvas.getLocalClipBounds())) return;
    const int width = int(std::ceil(bounds.width())), height = int(std::ceil(bounds.height()));
    auto surface = canvas.makeSurface(SkImageInfo::MakeN32Premul(width, height));
    if (!surface) throw std::runtime_error("Skia 行背景带描边临时表面分配失败");
    auto& source = *surface->getCanvas();
    source.clear(SK_ColorTRANSPARENT);
    drawContent(source, SkRect::MakeWH(bounds.width(), bounds.height()), 1.f, timeMs);
    auto image = surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 行背景带描边临时图像分配失败");
    const SkSamplingOptions linear(SkFilterMode::kLinear);
    SkPaint paint;
    const auto draw = [&](const sk_sp<SkImageFilter>& effect, float opacity, float dx = 0.f, float dy = 0.f) {
        paint.setAlphaf(opacity);
        paint.setImageFilter(effect);
        canvas.drawImage(image, bounds.left() + dx, bounds.top() + dy, linear, &paint);
    };
    // Apply opacity after the mask transfer. Applying it to the input before
    // the glow range curve changes the shape and strength of the glow.
    const auto drawBlur = [&](const Blur& effect) {
        const int radius = std::max(1, int(std::lround(effect.size * scale)));
        const int spread = std::max(0, int(std::lround(effect.spread * scale)));
        if (radius <= smallBlurRadius) {
            draw(SkImageFilters::ColorFilter(blurTint(effect, alpha), blurMask(radius, spread)),
                 1.f, effect.dx, effect.dy);
            return;
        }
        auto mask = wideBlurMask(canvas, *image, radius, spread, effect);
        const int pad = 3 * radius + spread;
        drawBlurTexture(canvas, *mask, bounds.left() + effect.dx - pad, bounds.top() + effect.dy - pad, alpha);
    };
    if (shadow) drawBlur(*shadow);
    if (glow) drawBlur(*glow);
    if (stroke && stroke->position == Stroke::Position::Outside) draw(strokeMask(*stroke, scale), alpha);
    draw(nullptr, alpha);
    if (stroke && stroke->position != Stroke::Position::Outside) draw(strokeMask(*stroke, scale), alpha);
}
}
