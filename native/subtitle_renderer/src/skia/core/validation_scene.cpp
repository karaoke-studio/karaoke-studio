#include "core/validation_scene.h"
#include "effects/particles.h"
#include "include/core/SkBitmap.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkData.h"
#include "include/core/SkFont.h"
#include "include/core/SkFontMgr.h"
#include "include/core/SkImage.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPathBuilder.h"
#include "include/core/SkSamplingOptions.h"
#include "include/core/SkTypeface.h"
#include "include/effects/SkImageFilters.h"
#include "include/effects/SkRuntimeEffect.h"
#include "include/encode/SkPngEncoder.h"
#include "include/ports/SkFontMgr_empty.h"
#include <fstream>
#include <algorithm>
#include <iterator>
#include <stdexcept>
#include <vector>

namespace subtitle::skia {
ValidationScene::ValidationScene(const std::filesystem::path& fontFile) {
    std::ifstream stream(fontFile, std::ios::binary);
    std::vector<char> bytes{std::istreambuf_iterator<char>(stream), {}};
    if (bytes.empty()) throw std::runtime_error("Cannot read smoke font");
    auto manager = SkFontMgr_New_Custom_Empty();
    typeface_ = manager->makeFromData(SkData::MakeWithCopy(bytes.data(), bytes.size()));
    if (!typeface_) throw std::runtime_error("FreeType cannot decode smoke font");
    auto result = SkRuntimeEffect::MakeForShader(SkString(
        "half4 main(float2 p) { return half4(p.x / 640, 0.5, 0.75, 1) * 0.6; }"));
    if (!result.effect) throw std::runtime_error(result.errorText.c_str());
    shader_ = result.effect->makeShader(nullptr, {});
    if (!shader_) throw std::runtime_error("RuntimeEffect shader creation failed");
    // Encode/decode a PNG in memory: exercises the pinned codecs without shipping test media.
    SkBitmap bitmap;
    bitmap.allocPixels(SkImageInfo::MakeN32Premul(16, 16));
    bitmap.eraseColor(SkColorSetARGB(128, 255, 64, 32));
    auto png = SkPngEncoder::Encode(bitmap.pixmap(), {});
    if (!png) throw std::runtime_error("PNG encoding failed");
    image_ = SkImages::DeferredFromEncodedData(std::move(png));
    if (!image_) throw std::runtime_error("PNG decoding failed");
}
ValidationScene::~ValidationScene() = default;

void ValidationScene::draw(SkCanvas& canvas) const {
    canvas.clear(SK_ColorTRANSPARENT);
    SkPaint paint;
    paint.setAntiAlias(true);
    paint.setShader(shader_);
    canvas.drawRect(SkRect::MakeXYWH(24, 24, 300, 70), paint);
    paint.setShader(nullptr);
    paint.setColor(SkColorSetARGB(180, 32, 180, 250));
    const auto path = SkPathBuilder().moveTo(24, 220).cubicTo(90, 90, 160, 320, 240, 200)
        .lineTo(240, 250).lineTo(24, 250).close().detach();
    canvas.drawPath(path, paint);
    paint.setColor(SkColorSetARGB(160, 255, 190, 30));
    paint.setImageFilter(SkImageFilters::Blur(6, 6, nullptr));
    canvas.drawCircle(390, 100, 38, paint);
    paint.setImageFilter(nullptr);
    paint.setColor(SK_ColorWHITE);
    SkFont font(typeface_, 44);
    font.setEdging(SkFont::Edging::kAntiAlias);
    font.setHinting(SkFontHinting::kNone);
    canvas.drawSimpleText("Lin-K Lyrics", 12, SkTextEncoding::kUTF8, 24, 320, font, paint);
    auto outline = font.getPath(font.unicharToGlyph('K'));
    if (!outline) throw std::runtime_error("FreeType glyph outline unavailable");
    canvas.save();
    canvas.translate(500, 90);
    canvas.drawPath(*outline, paint);
    canvas.restore();
    canvas.drawImageRect(image_, SkRect::MakeXYWH(470, 180, 80, 80), SkSamplingOptions(), nullptr);
    // Same deterministic evaluator as Direct2D; platform adapters supply only the canvas.
    krok::subtitle::native::ParticleBurst burst;
    burst.kind = "sparkle";
    burst.seed = 7;
    burst.startMs = 100;
    burst.endMs = 1600;
    burst.count = 6;
    burst.sizePx = 9;
    burst.travelPx = 30;
    for (const auto& particle : krok::subtitle::native::effects::burstParticlesAt(burst, 450, 590, 300, 30, 20)) {
        paint.setColor(SK_ColorWHITE);
        paint.setAlphaf(std::clamp(particle.alpha, 0.0f, 1.0f));
        canvas.drawCircle(particle.x, particle.y, particle.sizePx * 0.5f, paint);
    }
}
}
