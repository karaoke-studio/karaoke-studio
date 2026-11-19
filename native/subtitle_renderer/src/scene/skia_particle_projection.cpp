#include "skia_particle_projection.h"
#include "paint_projection.h"
#include "../model/backend_caps.h"
#include <algorithm>
#include <cmath>
#include <cstdint>

namespace krok::subtitle::native::scene {
void projectSkiaSingParticles(const protocol::RenderConfig& cfg, const protocol::TimingLine& source,
                              const protocol::ResolvedStyle& style, int offset, int version, RenderScene& scene, TextLine& line) {
    if (source.fxBursts.empty()) return;
    // Singing particles follow the existing burst clocks. The line
    // display window determines visibility without rewriting those clocks.
    if (cfg.vertical || cfg.rightToLeft || source.wipeReverse || source.scanlineEnabled
        || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit")
        || source.entryAnimation != "none" || source.exitAnimation != "none" || !cfg.rubies.empty()
        || style.strokeWidthPx > 0 || style.stroke2WidthPx > 0 || !source.displayStartMs || !source.displayEndMs
        || source.fxBursts.size() > 4096)
        throw BackendError("Skia 唱字星光仅支持正向横排基础正文");
    line.bursts.reserve(source.fxBursts.size());
    for (const auto& fx : source.fxBursts) {
        const bool decorative = version >= 3 && (fx.kind == "note" || fx.kind == "heart");
        const bool kindSupported = decorative || fx.kind == "twinkle" || (version >= 2 && fx.kind == "twinkle_classic");
        const bool dottedNote = decorative && fx.kind == "note" && fx.sprite == "note_dot";
        const QString spriteName = dottedNote ? fx.sprite : decorative ? fx.kind : QStringLiteral("star4");
        const auto start = std::int64_t(fx.startMs) + offset, end = std::int64_t(fx.endMs) + offset;
        const bool oldWindow = fx.startMs >= *source.displayStartMs && fx.endMs <= *source.displayEndMs;
        if (!kindSupported || fx.anchor != "char" || fx.charIndex < 0 || fx.charIndex >= int(source.chars.size())
            || fx.hasPaint || fx.hasDecor || !fx.charColors.empty() || (!fx.sprite.isEmpty() && fx.sprite != spriteName)
            || (version == 1 && (fx.timeScale != 1.0 || !oldWindow))
            || !std::isfinite(fx.timeScale) || fx.timeScale < .1 || fx.timeScale > 10
            || fx.emitMs != 0 || fx.emitX != 0 || fx.reverse || !fx.front
            || fx.count < 1 || fx.count > 256 || !std::isfinite(fx.sizePx) || fx.sizePx <= 0 || fx.sizePx > 4096
            || !std::isfinite(fx.travelPx) || fx.travelPx < 0 || fx.travelPx > 1e5
            || end <= start || std::abs(start) > 172800000 || std::abs(end) > 172800000)
            throw BackendError("Skia 唱字粒子的类型、参数或时钟超出支持范围");
        const std::string spriteId = spriteName.toStdString();
        const auto loaded = std::any_of(scene.fxSprites.begin(), scene.fxSprites.end(),
            [&](const auto& entry) { return entry.first == spriteId; });
        if (!loaded) {
            const auto sprite = cfg.fxSprites.constFind(spriteName);
            if (sprite == cfg.fxSprites.constEnd() || sprite->commands.empty() || sprite->commands.size() > 64
                || (!decorative && sprite->commands.size() != 6))
                throw BackendError("Skia 唱字粒子缺少有效的轮廓");
            scene.fxSprites.emplace_back(spriteId, *sprite);
        }

        ParticleBurst burst;
        burst.kind = fx.kind.toStdString();
        burst.sprite = spriteId;
        burst.anchor = fx.anchor.toStdString();
        burst.charIndex = fx.charIndex;
        burst.startMs = int(start);
        burst.endMs = int(end);
        burst.timeScale = float(fx.timeScale);
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
