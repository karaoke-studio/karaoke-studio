#include "resolved_particles.h"
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
                                double dpr, ::subtitle::skia::ResolvedLine& prepared) {
    if (line.bursts.empty()) {
        if (!box.isUndefined()) throw BackendError("Skia 粒子行盒高度缺少能力声明");
        return;
    }
    const auto lineHeight = box.toDouble();
    const bool validBox = style.vertical ? box.isUndefined()
        : box.isDouble() && std::isfinite(lineHeight) && lineHeight >= 0 && lineHeight <= 1e5;
    if (!validBox || (!prepared.rubyGroups.empty() && !rubyParticles)
        || (style.vertical && !scene::hasSkiaVerticalParticleProfile(features)) || style.rightToLeft)
        throw BackendError("Skia 字幕粒子缺少有效的行盒高度");
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
        prepared.particles.push_back({std::move(localBurst), sprite->second,
            SkColorSetARGB(color.alpha, color.red, color.green, color.blue),
            x, y + verticalEmit, std::max(anchor.right - anchor.left, 1.f), height,
            burst.kind == "ripple" || burst.kind == "shockwave"
                ? ::subtitle::skia::concentricRing(sprite->second) : std::nullopt,
            burst.hasPaint ? std::optional<::subtitle::skia::ParticlePaint>(burst) : std::nullopt});
    }
}
}
