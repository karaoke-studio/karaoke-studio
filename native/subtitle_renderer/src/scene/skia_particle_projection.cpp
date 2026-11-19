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
    if (cfg.vertical || cfg.rightToLeft || source.wipeReverse
        || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit")
        || source.entryAnimation != "none" || source.exitAnimation != "none" || !cfg.rubies.empty()
        || style.strokeWidthPx > 0 || style.stroke2WidthPx > 0 || !source.displayStartMs || !source.displayEndMs
        || source.fxBursts.size() > 4096)
        throw BackendError("Skia 唱字粒子仅支持正向横排基础正文");
    line.bursts.reserve(source.fxBursts.size());
    for (const auto& fx : source.fxBursts) {
        const bool symbol = version >= 3 && (fx.kind == "note" || fx.kind == "heart");
        const bool ripple = version >= 4 && fx.kind == "ripple";
        const bool star = fx.kind == "twinkle" || (version >= 2 && fx.kind == "twinkle_classic");
        const bool falling = version >= 5 && (fx.kind == "petal" || fx.kind == "feather"
                                               || fx.kind == "snow" || fx.kind == "snow_solid");
        const bool lightning = version >= 6 && (fx.kind == "lightning" || fx.kind == "lightning_glow");
        const bool flame = version >= 6 && (fx.kind == "flame" || fx.kind == "flame_alt" || fx.kind == "flame_glow"
                                            || fx.kind == "smoke" || fx.kind == "ember" || fx.kind == "spark");
        const bool scanline = version >= 7 && fx.kind == "scanline";
        const bool back = ripple || fx.kind == "lightning_glow" || fx.kind == "flame_glow" || fx.kind == "smoke";
        QString defaultSprite = QStringLiteral("star4");
        if (symbol || falling || flame) defaultSprite = fx.kind;
        if (ripple) defaultSprite = QStringLiteral("ring");
        if (lightning) defaultSprite = QStringLiteral("bolt");
        if (fx.kind == "flame_glow") defaultSprite = QStringLiteral("flame");
        if (fx.kind == "ember" || fx.kind == "spark") defaultSprite = QStringLiteral("dot");
        const QString spriteName = fx.sprite.isEmpty() ? defaultSprite : fx.sprite;
        const bool allowedScanlineSprite = spriteName == "star4" || spriteName == "petal" || spriteName == "feather"
                                         || spriteName == "snow" || spriteName == "snow_solid" || spriteName == "note";
        const bool allowedSprite = scanline ? !fx.sprite.isEmpty() && allowedScanlineSprite : spriteName == defaultSprite
            || (fx.kind == "note" && spriteName == "note_dot")
            || (lightning && (spriteName == "bolt_b" || spriteName == "bolt_c"))
            || ((fx.kind == "flame" || fx.kind == "flame_glow")
                && (spriteName == "flame_tall" || spriteName == "flame_wide" || spriteName == "flame_curl"))
            || (fx.kind == "flame_alt" && spriteName == "flame_alt_b");
        const auto start = std::int64_t(fx.startMs) + offset, end = std::int64_t(fx.endMs) + offset;
        const bool oldWindow = fx.startMs >= *source.displayStartMs && fx.endMs <= *source.displayEndMs;
        const bool emitWindowValid = scanline ? fx.emitMs > 0 && fx.emitMs < end-start && fx.timeScale == 1
                                             : fx.emitMs == 0;
        if (!(star || symbol || ripple || falling || lightning || flame || scanline) || fx.anchor != "char"
            || fx.charIndex < 0 || fx.charIndex >= int(source.chars.size())
            || source.scanlineEnabled != scanline
            || fx.hasPaint || fx.hasDecor || !fx.charColors.empty() || !allowedSprite
            || (version == 1 && (fx.timeScale != 1.0 || !oldWindow))
            || !std::isfinite(fx.timeScale) || fx.timeScale < .1 || fx.timeScale > 10
            || !emitWindowValid || !std::isfinite(fx.emitX) || std::abs(fx.emitX) > (flame ? .5 : 0.)
            || fx.reverse || fx.front == back
            || fx.count < 1 || fx.count > 256 || !std::isfinite(fx.sizePx) || fx.sizePx <= 0 || fx.sizePx > 4096
            || !std::isfinite(fx.travelPx) || fx.travelPx < 0 || fx.travelPx > 1e5
            || end <= start || std::abs(start) > 172800000 || std::abs(end) > 172800000)
            throw BackendError("Skia 唱字粒子的类型、参数或时钟超出支持范围");
        const std::string spriteId = spriteName.toStdString();
        const auto loaded = std::any_of(scene.fxSprites.begin(), scene.fxSprites.end(),
            [&](const auto& entry) { return entry.first == spriteId; });
        if (!loaded) {
            const auto sprite = cfg.fxSprites.constFind(spriteName);
            // The hollow snowflake has a branched contour and several holes.
            const std::size_t commandLimit = spriteName == "snow" ? 192 : 64;
            if (sprite == cfg.fxSprites.constEnd() || sprite->commands.empty() || sprite->commands.size() > commandLimit
                || (star && sprite->commands.size() != 6))
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
        burst.emitX = float(fx.emitX);
        burst.emitMs = fx.emitMs;
        burst.sweep = fx.sweep;
        burst.front = fx.front;
        burst.color = projectColor(fx.color, QStringLiteral("#FFFFFF"));
        line.bursts.push_back(std::move(burst));
    }
}
}
