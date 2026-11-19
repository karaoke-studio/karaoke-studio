#include "skia_particle_projection.h"
#include "paint_projection.h"
#include "../model/backend_caps.h"
#include <cmath>

namespace krok::subtitle::native::scene {
void projectSkiaSingParticles(const protocol::RenderConfig& cfg, const protocol::TimingLine& source,
                              const protocol::ResolvedStyle& style, int offset, RenderScene& scene, TextLine& line) {
    if (source.fxBursts.empty()) return;
    // sing_twinkle_schema=1 keeps the existing burst clocks and supports front
    // solid stars, entirely within an untransformed horizontal display window.
    if (cfg.vertical || cfg.rightToLeft || source.wipeReverse || source.scanlineEnabled
        || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit")
        || source.entryAnimation != "none" || source.exitAnimation != "none" || !cfg.rubies.empty()
        || style.strokeWidthPx > 0 || style.stroke2WidthPx > 0 || !source.displayStartMs || !source.displayEndMs
        || source.fxBursts.size() > 4096)
        throw BackendError("Skia 唱字星光仅支持正向横排基础正文");
    const auto sprite = cfg.fxSprites.constFind(QStringLiteral("star4"));
    if (sprite == cfg.fxSprites.constEnd() || sprite->commands.size() != 6)
        throw BackendError("Skia 唱字星光缺少粒子轮廓");
    if (scene.fxSprites.empty()) scene.fxSprites.emplace_back("star4", *sprite);
    line.bursts.reserve(source.fxBursts.size());
    for (const auto& fx : source.fxBursts) {
        if (fx.kind != "twinkle" || fx.anchor != "char" || fx.charIndex < 0 || fx.charIndex >= int(source.chars.size())
            || fx.hasPaint || fx.hasDecor || !fx.charColors.empty() || (!fx.sprite.isEmpty() && fx.sprite != "star4")
            || fx.timeScale != 1.0 || fx.emitMs != 0 || fx.emitX != 0 || fx.reverse || !fx.front
            || fx.count < 1 || fx.count > 256 || !std::isfinite(fx.sizePx) || fx.sizePx <= 0 || fx.sizePx > 4096
            || !std::isfinite(fx.travelPx) || fx.travelPx < 0 || fx.travelPx > 1e5
            || fx.startMs < *source.displayStartMs || fx.endMs > *source.displayEndMs)
            throw BackendError("Skia 唱字粒子当前仅支持实色原速唱字星光");
        ParticleBurst burst;
        burst.kind = fx.kind.toStdString();
        burst.anchor = fx.anchor.toStdString();
        burst.charIndex = fx.charIndex;
        burst.startMs = fx.startMs + offset;
        burst.endMs = fx.endMs + offset;
        burst.count = fx.count;
        burst.seed = fx.seed;
        burst.sizePx = float(fx.sizePx * cfg.dpr);
        burst.travelPx = float(fx.travelPx * cfg.dpr);
        burst.sweep = fx.sweep;
        burst.color = projectColor(fx.color, QStringLiteral("#FFFFFF"));
        line.bursts.push_back(std::move(burst));
    }
}
}
