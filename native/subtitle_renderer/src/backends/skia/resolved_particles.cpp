#include "resolved_particles.h"
#include "image_fill_cache.h"
#include "resolved_glyph.h"
#include "core/resolved_scene.h"
#include "../render_backend.h"
#include "../../model/render_scene.h"
#include "../../scene/skia_ir_contract.h"
#include "include/core/SkColor.h"
#include <QtCore/QJsonValue>
#include <algorithm>
#include <cmath>

namespace krok::subtitle::native {
SkiaParticleSprites resolveSkiaParticleSprites(const RenderScene& scene) {
    SkiaParticleSprites sprites;
    for (const auto& [name, shape] : scene.fxSprites)
        sprites.emplace(name, vectorGlyphPath(shape, 1000.0));
    return sprites;
}

void resolveSkiaParticleAnchors(const QJsonValue& box, const QJsonArray& features, const TextLine& line,
                                const TextStyle& style, bool rubyParticles, const SkiaParticleSprites& sprites,
                                SkiaImageFillCache& imageCache, double dpr, ::subtitle::skia::ResolvedLine& prepared) {
    if (line.bursts.empty()) {
        if (!box.isUndefined()) throw BackendError("Skia 粒子行盒高度缺少能力声明");
        return;
    }
    const auto lineHeight = box.toDouble();
    const bool validBox = style.vertical ? box.isUndefined()
        : box.isDouble() && std::isfinite(lineHeight) && lineHeight >= 0 && lineHeight <= 1e5;
    if (!validBox)
        throw BackendError("Skia 字幕粒子缺少有效的行盒高度");
    // Line-anchored particle paints span the physical line box (cells run right to left on RTL lines).
    float lineLeft = 0.f, lineRight = 1.f;
    if (!prepared.cells.empty()) {
        lineLeft = prepared.cells.front().pivot.layoutLeft;
        lineRight = prepared.cells.front().pivot.layoutRight;
        for (const auto& cell : prepared.cells) {
            lineLeft = std::min(lineLeft, cell.pivot.layoutLeft);
            lineRight = std::max(lineRight, cell.pivot.layoutRight);
        }
    }
    const float lineSpan = std::max(lineRight - lineLeft, 1.f);
    for (const auto& burst : line.bursts) {
        const auto sprite = sprites.find(burst.sprite);
        if (sprite == sprites.end() || sprite->second.isEmpty()
            || burst.charIndex < 0 || burst.charIndex >= int(prepared.cells.size()))
            throw BackendError("Skia 字幕粒子缺少有效的轮廓或锚点");
        const auto index = std::size_t(burst.charIndex);
        const auto& anchor = prepared.cells[line.wipeReverse ? prepared.cells.size() - 1 - index : index];
        // The vertical anchor uses the final glyph's ink center and vertical cell box.
        const float x = style.vertical ? (anchor.left + anchor.right) * .5f
                                       : (anchor.pivot.layoutLeft + anchor.pivot.layoutRight) * .5f;
        const float y = style.vertical ? (anchor.top + anchor.bottom) * .5f : anchor.pivot.centerY;
        const float height = style.vertical ? anchor.bottom - anchor.top : float(lineHeight * dpr);
        // Flame emitters travel along the wipe axis. Their plumes still rise on screen.
        auto localBurst = burst;
        const float verticalEmit = style.vertical ? localBurst.emitX * height * (line.wipeReverse ? -1.f : 1.f) : 0.f;
        if (style.vertical) localBurst.emitX = 0.f;
        const auto color = burst.color;
        std::optional<::subtitle::skia::ParticlePaint> paint;
        if (burst.hasPaint) {
            const auto spriteShader = [&](const PaintStyle& layer) {
                // Bitmap paint uses the unscaled sprite grid before particle transforms.
                return imageCache.fill(layer, 1.f).shaderAt(SkMatrix::I());
            };
            ::subtitle::skia::ParticlePaint::Images images{
                spriteShader(burst.fill), spriteShader(burst.stroke), spriteShader(burst.stroke2), spriteShader(burst.decor)};
            paint.emplace(burst, lineLeft, lineSpan, std::move(images));
        }
        prepared.particles.push_back({std::move(localBurst), sprite->second,
            SkColorSetARGB(color.alpha, color.red, color.green, color.blue),
            x, y + verticalEmit, std::max(anchor.right - anchor.left, 1.f), height,
            (burst.kind == "ripple" || burst.kind == "shockwave")
                && !(burst.hasPaint && burst.fill.mode == "image")
                ? ::subtitle::skia::concentricRing(sprite->second) : std::nullopt,
            std::move(paint)});
    }
}
}
