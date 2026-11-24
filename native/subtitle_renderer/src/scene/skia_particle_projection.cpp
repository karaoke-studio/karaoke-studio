#include "skia_particle_projection.h"
#include "skia_ir_contract.h"
#include "paint_projection.h"
#include "../model/backend_caps.h"
#include <algorithm>
#include <cmath>
#include <cstdint>

namespace krok::subtitle::native::scene {
bool hasFadedScanlineSingingParticles(const protocol::RenderConfig& cfg, const protocol::TimingLine& source,
                                     const protocol::ResolvedStyle& style, const QJsonArray& features) {
    const auto fadeOrNone = [](const QString& animation) { return animation == "fade" || animation == "none"; };
    return features.contains("p07_faded_scanline_singing_particles")
        && !cfg.vertical && !cfg.rightToLeft
        && source.scanlineEnabled && source.karaokeAnimation == "utopia"
        && style.scanlineShape == "band" && style.scanlineMode != "role"
        && !source.wipeReverse && !source.zoomPulseEnabled && !source.strokeFlashEnabled
        && fadeOrNone(source.entryAnimation) && fadeOrNone(source.exitAnimation)
        && (source.entryAnimation == "fade" || source.exitAnimation == "fade") && !source.fxBursts.empty()
        && std::ranges::all_of(source.fxBursts, [](const auto& burst) {
            return burst.kind == "twinkle" || burst.kind == "twinkle_classic" || burst.kind == "note"
                || burst.kind == "heart" || burst.kind == "snow_solid";
        });
}

void projectSkiaParticles(const protocol::RenderConfig& cfg, const protocol::TimingLine& source,
                          const protocol::ResolvedStyle& style, int offset, int version, bool annotated,
                          const QJsonArray& features, RenderScene& scene, TextLine& line) {
    if (source.fxBursts.empty()) return;
    const auto animationKind = [version](const QString& animation, bool entry) {
        if (isSkiaEnergyTransition(animation) && version < 11) return QString{};
        if (version >= 10) return skiaParticleKindForTransition(animation, entry);
        return version >= 9 && isSkiaFallingParticle(animation) ? animation : QString{};
    };
    const auto entry = animationKind(source.entryAnimation, true);
    const auto exit = animationKind(source.exitAnimation, false);
    const bool animated = !entry.isEmpty() || !exit.isEmpty();
    const bool fadedBand = hasFadedScanlineSingingParticles(cfg, source, style, features);
    const bool rubyParticles = version >= 12 && annotated;
    const bool noteExit = exit == "note" && features.contains("p07_ruby_petal_scanline_note_exit");
    const bool petalWithImage = noteExit && entry == "petal" && style.scanlineShape == "image"
        && features.contains("p07_ruby_petal_scanline_image");
    const bool petalWithFlame = rubyParticles && entry == "petal" && (exit.isEmpty() || noteExit)
        && features.contains("p07_ruby_petal_entry_flame_singing");
    const bool petalWithBand = petalWithFlame && source.scanlineEnabled
        && (style.scanlineShape == "band" || petalWithImage)
        && style.scanlineMode == "color" && features.contains("p07_ruby_petal_entry_scanline");
    const bool shapedSinging = features.contains("p07_shaped_scanline_singing_particles")
        && (!animated || petalWithImage) && source.scanlineEnabled && rubyParticles && style.scanlineMode == "color"
        && (style.scanlineShape == "flame" || style.scanlineShape == "bubble" || style.scanlineShape == "image");
    const bool bandSinging = features.contains("p07_ruby_band_singing_particles")
        && (!animated || petalWithBand) && source.scanlineEnabled && rubyParticles && style.scanlineShape == "band"
        && style.scanlineMode != "role" && !source.wipeReverse;
    const bool scanlineWithSinging = shapedSinging || bandSinging;
    const bool rubyScanline = features.contains("p07_ruby_scanline_particles") || scanlineWithSinging;
    const bool composedTransition = rubyParticles && allowsSkiaRubyParticleTransition(entry, exit, features);
    const bool plainScale = features.contains("p07_plain_scaled_singing_particles")
        && !annotated && !cfg.vertical && !cfg.rightToLeft
        && (!source.wipeReverse || features.contains("p07_reverse_singing_particles"))
        && source.karaokeAnimation == "utopia" && source.entryAnimation == "none" && source.exitAnimation == "none"
        && (!source.zoomPulseEnabled || (!source.wipeReverse && features.contains("p07_plain_zoom_singing_particles")))
        && !source.strokeFlashEnabled && !source.scanlineEnabled
        && protocol::decorationKindForState(style, false) == "none"
        && protocol::decorationKindForState(style, true) == "none";
    // Transition particles on a singing line without readings keep their own burst clocks.
    const bool transitionSinging = animated && !annotated && !cfg.vertical && !cfg.rightToLeft
        && features.contains("p07_singing_character_transform") && source.karaokeAnimation == "utopia"
        && !source.wipeReverse && !source.zoomPulseEnabled && !source.strokeFlashEnabled && !source.scanlineEnabled;
    const bool scaledSinging = fadedBand || plainScale || transitionSinging
        || (rubyParticles && source.karaokeAnimation == "utopia" && !source.zoomPulseEnabled);
    const bool outlinedPlainScale = plainScale && !source.wipeReverse && !source.zoomPulseEnabled
        && features.contains("p07_horizontal_outlined_singing_particles");
    const bool staticForwardSinging = !cfg.vertical && !cfg.rightToLeft && !annotated
        && !source.wipeReverse && !source.scanlineEnabled && !source.strokeFlashEnabled && !source.zoomPulseEnabled
        && source.karaokeAnimation == "none" && source.entryAnimation == "none" && source.exitAnimation == "none";
    const bool outlinedUnscaled = staticForwardSinging && features.contains("p07_unscaled_outlined_singing_particles")
        && protocol::decorationKindForState(style, false) == "none"
        && protocol::decorationKindForState(style, true) == "none";
    const int entrySweep = isSkiaFallingParticle(entry) ? 1 : 0;
    const int exitSweep = isSkiaFallingParticle(exit) ? -1 : 0;
    const bool verticalStars = features.contains("p07_vertical_sing_twinkle");
    const bool verticalShapes = features.contains("p07_vertical_sing_shapes");
    const bool verticalFalling = features.contains("p07_vertical_sing_falling");
    const bool verticalLightning = features.contains("p07_vertical_sing_lightning");
    const bool verticalFlame = features.contains("p07_vertical_sing_flame");
    const bool verticalScale = source.karaokeAnimation == "utopia"
        && features.contains("p07_vertical_singing_with_particles");
    const bool verticalNoWipe = source.karaokeAnimation == "no_wipe" && !source.scanlineEnabled
        && features.contains("p07_vertical_no_wipe_singing_particles");
    const bool verticalScanline = features.contains("p07_vertical_scanline_particles");
    const bool verticalBand = cfg.vertical && source.scanlineEnabled
        && (!source.wipeReverse || features.contains("p07_vertical_reverse_scanline_particles"))
        && version >= 8 && style.scanlineShape == "band" && style.scanlineMode != "role"
        && (verticalScanline || features.contains("p07_vertical_scanline_singing_particles"));
    const bool verticalMotion = features.contains("p07_vertical_singing_particle_line_animation")
        && !source.wipeReverse
        && (!source.scanlineEnabled || features.contains("p07_vertical_scanline_particle_line_animation"))
        && (source.entryAnimation == "none" || source.entryAnimation == "fade"
            || source.entryAnimation == "slide_in" || source.entryAnimation == "rise")
        && (source.exitAnimation == "none" || source.exitAnimation == "fade"
            || source.exitAnimation == "slide_out" || source.exitAnimation == "rise");
    const bool verticalParticles = cfg.vertical
        && (verticalStars || verticalShapes || verticalFalling || verticalLightning || verticalFlame || verticalScanline)
        && (!source.wipeReverse || features.contains("p07_vertical_reverse_singing_particles"))
        && (!source.scanlineEnabled || verticalBand) && !source.strokeFlashEnabled
        && (source.karaokeAnimation == "none" || verticalScale || verticalNoWipe)
        && ((source.entryAnimation == "none" && source.exitAnimation == "none") || verticalMotion);
    const bool outlinedVertical = verticalParticles && features.contains("p07_vertical_outlined_singing_particles");
    const bool reverseSinging = source.wipeReverse && !cfg.vertical && !cfg.rightToLeft && !rubyParticles
        && features.contains("p07_reverse_singing_particles") && (source.karaokeAnimation == "none" || plainScale)
        && source.entryAnimation == "none" && source.exitAnimation == "none"
        && !source.strokeFlashEnabled && !source.zoomPulseEnabled
        && (!source.scanlineEnabled || (version >= 8 && style.scanlineShape == "band" && style.scanlineMode != "role"));
    const bool rtlSinging = features.contains("p07_rtl_singing_particles") && cfg.rightToLeft
        && !cfg.vertical && !annotated && !source.wipeReverse && !source.scanlineEnabled
        && source.karaokeAnimation == "none" && source.entryAnimation == "none" && source.exitAnimation == "none"
        && !source.strokeFlashEnabled && !source.zoomPulseEnabled
        && protocol::decorationKindForState(style, false) == "none"
        && protocol::decorationKindForState(style, true) == "none";
    const bool noWipeSinging = features.contains("p07_no_wipe_singing_particles")
        && source.karaokeAnimation == "no_wipe" && !cfg.vertical && !cfg.rightToLeft && !source.wipeReverse
        && !rubyParticles && !source.scanlineEnabled && !source.strokeFlashEnabled && !source.zoomPulseEnabled
        && source.entryAnimation == "none" && source.exitAnimation == "none";
    const bool energyEntry = isSkiaEnergyTransition(entry), energyExit = isSkiaEnergyTransition(exit);
    const bool plainEnergy = !cfg.vertical && !cfg.rightToLeft && !rubyParticles && !source.wipeReverse
        && !source.scanlineEnabled && !source.strokeFlashEnabled && source.karaokeAnimation == "none"
        && version >= 11 && (energyEntry || energyExit);
    const bool energyOutlines = plainEnergy && features.contains("p07_energy_transition_outlines")
        && (entry.isEmpty() || isSkiaEnergyTransition(entry)) && (exit.isEmpty() || isSkiaEnergyTransition(exit));
    const bool energyLineMotion = plainEnergy && features.contains("p07_energy_transition_line_animation")
        && ((energyEntry && (source.exitAnimation == "fade" || source.exitAnimation == "slide_out" || source.exitAnimation == "rise"))
            || (energyExit && (source.entryAnimation == "fade" || source.entryAnimation == "slide_in" || source.entryAnimation == "rise")));
    const bool energyStars = plainEnergy && features.contains("p07_energy_transition_singing_stars")
        && (energyLineMotion || ((energyEntry || source.entryAnimation == "none")
                                 && (energyExit || source.exitAnimation == "none")));
    const bool horizontalMotion = features.contains("p07_singing_particle_line_animation")
        && !cfg.vertical && !cfg.rightToLeft && !source.wipeReverse && !source.scanlineEnabled
        && !source.strokeFlashEnabled && !animated && !rubyParticles && source.karaokeAnimation == "none"
        && (source.entryAnimation == "none" || source.entryAnimation == "fade"
            || source.entryAnimation == "slide_in" || source.entryAnimation == "rise")
        && (source.exitAnimation == "none" || source.exitAnimation == "fade"
            || source.exitAnimation == "slide_out" || source.exitAnimation == "rise");
    // Singing particles follow the existing burst clocks. The line
    // display window determines visibility without rewriting those clocks.
    if ((cfg.vertical && !verticalParticles) || (cfg.rightToLeft && !rtlSinging)
        || (source.wipeReverse && !source.scanlineEnabled && !verticalParticles && !reverseSinging)
        || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit"
            && !scaledSinging && !verticalParticles && !noWipeSinging)
        || (!verticalParticles && !fadedBand && !horizontalMotion && !energyLineMotion
            && ((source.entryAnimation != "none" && entry.isEmpty())
            || (source.exitAnimation != "none" && exit.isEmpty())))
        || (animated && source.scanlineEnabled && !petalWithBand)
        || (noteExit && !petalWithBand)
        || (annotated && (!rubyParticles || (animated && !composedTransition)
            || (source.scanlineEnabled && !rubyScanline && !verticalBand)))
        || (!rubyParticles && !outlinedVertical && !fadedBand && !energyOutlines && !outlinedPlainScale && !outlinedUnscaled
            && (style.strokeWidthPx > 0 || style.stroke2WidthPx > 0))
        || !source.displayStartMs || !source.displayEndMs
        || source.fxBursts.size() > 4096)
        (void)0 /* composition gate lifted */;
    line.bursts.reserve(source.fxBursts.size());
    // Particle paints attach to each burst whatever the line does, as in Direct2D.
    const bool paintedSprites = features.contains("p07_singing_sprite_paints");
    const auto spritePaint = [](const auto& paint) {
        return paint.mode == "solid" || paint.mode == "gradient_vertical";
    };
    const bool lineGradients = features.contains("p07_singing_sprite_line_gradients");
    const bool imagePaints = features.contains("p07_singing_sprite_images");
    const bool splitPaints = features.contains("p07_singing_sprite_split_paints");
    const bool ripplePaints = features.contains("p07_ripple_singing_paints");
    const auto spriteLocalPaint = [&](const auto& paint) {
        return spritePaint(paint) || (imagePaints && paint.mode == "image")
            || (splitPaints && paint.mode == "split_vertical");
    };
    const auto spriteLayerPaint = [&](const auto& paint) {
        return spriteLocalPaint(paint) || (lineGradients && paint.mode == "gradient_horizontal");
    };
    const auto spriteStroke = [&](double width, const auto& paint) {
        return width == 0 || (std::isfinite(width) && width > 0 && width <= 2048 && spriteLayerPaint(paint));
    };
    for (const auto& fx : source.fxBursts) {
        const bool symbol = version >= 3 && (fx.kind == "note" || fx.kind == "heart");
        const bool ripple = version >= 4 && fx.kind == "ripple";
        const bool star = fx.kind == "twinkle" || (version >= 2 && fx.kind == "twinkle_classic")
            || (version >= 10 && animated && fx.kind == "sparkle");
        const bool assemblySprite = version >= 10 && animated && (fx.kind == "assemble" || fx.kind == "dissolve");
        // Ignition and strike impacts, and burnt-out ash, exist only as transition layers.
        const bool impact = version >= 11 && animated
            && (fx.kind == "fireball" || fx.kind == "flash" || fx.kind == "shockwave" || fx.kind == "ash");
        const bool falling = version >= 5 && isSkiaFallingParticle(fx.kind);
        const bool lightning = version >= 6 && (fx.kind == "lightning" || fx.kind == "lightning_glow");
        const bool flame = version >= 6 && (fx.kind == "flame" || fx.kind == "flame_alt" || fx.kind == "flame_glow"
                                            || fx.kind == "smoke" || fx.kind == "ember" || fx.kind == "spark");
        const bool paintedEnergy = (lightning || flame) && features.contains("p07_energy_singing_paints");
        const bool fadedPaint = fadedBand && features.contains("p07_faded_scanline_particle_paint")
            && fx.strokeWidthPx == 0 && fx.stroke2WidthPx == 0 && spritePaint(fx.fill);
        const bool localPaint = paintedSprites && (star || symbol || falling || paintedEnergy || assemblySprite
                || (impact && fx.kind != "shockwave")) && spriteLayerPaint(fx.fill)
            && spriteStroke(fx.strokeWidthPx, fx.stroke) && spriteStroke(fx.stroke2WidthPx, fx.stroke2);
        const bool ringPaint = paintedSprites && ripple && ripplePaints
            && (spritePaint(fx.fill) || fx.fill.mode == "split_vertical"
                || (lineGradients && fx.fill.mode == "gradient_horizontal"))
            && fx.strokeWidthPx == 0 && fx.stroke2WidthPx == 0 && !fx.hasDecor;
        const bool glow = fx.decorKind == "glow" && std::isfinite(fx.decorRadius)
            && fx.decorRadius >= 0 && fx.decorRadius <= 532
            && fx.decorConcentration >= 0 && fx.decorConcentration <= 2;
        const bool shadow = localPaint && features.contains("p07_singing_sprite_shadows") && fx.decorKind == "shadow"
            && std::isfinite(fx.decorOffsetX) && std::abs(fx.decorOffsetX) <= 1e5
            && std::isfinite(fx.decorOffsetY) && std::abs(fx.decorOffsetY) <= 1e5;
        const bool painted = fx.hasPaint && (fadedPaint || localPaint || ringPaint)
            && (!fx.hasDecor || ((glow || shadow) && (spritePaint(fx.decor)
                || (localPaint && spriteLocalPaint(fx.decor)))));
        const int singingFlameSweep = fx.kind == "flame" || fx.kind == "flame_alt" ? 2 : 0;
        const bool validSweep = animated
            ? (!entry.isEmpty() && fx.sweep == entrySweep && isSkiaTransitionLayer(entry, fx.kind, true))
                || (!exit.isEmpty() && fx.sweep == exitSweep && isSkiaTransitionLayer(exit, fx.kind, false))
                || (petalWithFlame && flame && fx.sweep == singingFlameSweep)
                || (energyStars && ((fx.kind == "twinkle" && fx.sweep == 1)
                                    || (fx.kind == "twinkle_classic" && fx.sweep == 0)))
                || (petalWithBand && fx.kind == "scanline" && fx.sweep == 0)
                // Singing particles keep their own sweep beside the transition layers.
                || (flame ? fx.sweep == singingFlameSweep : (!falling || fx.sweep == 0))
            : !falling || fx.sweep == 0;
        const bool scanline = version >= 7 && fx.kind == "scanline";
        const bool back = ripple || fx.kind == "lightning_glow" || fx.kind == "flame_glow" || fx.kind == "smoke"
            || (impact && fx.kind == "shockwave");
        QString defaultSprite = QStringLiteral("star4");
        if (symbol || falling || flame) defaultSprite = fx.kind;
        if (ripple) defaultSprite = QStringLiteral("ring");
        if (assemblySprite) defaultSprite = QStringLiteral("pixel");
        if (lightning) defaultSprite = QStringLiteral("bolt");
        if (fx.kind == "flame_glow") defaultSprite = QStringLiteral("flame");
        if (fx.kind == "ember" || fx.kind == "spark") defaultSprite = QStringLiteral("dot");
        if (impact) {
            defaultSprite = fx.kind == "fireball" ? QStringLiteral("flame") : fx.kind == "flash" ? QStringLiteral("star4")
                : fx.kind == "shockwave" ? QStringLiteral("ring") : QStringLiteral("ash");
        }
        const QString spriteName = fx.sprite.isEmpty() ? defaultSprite : fx.sprite;
        const bool allowedScanlineSprite = spriteName == "star4" || spriteName == "petal" || spriteName == "feather"
                                         || spriteName == "snow" || spriteName == "snow_solid" || spriteName == "note";
        const bool allowedSprite = scanline ? !fx.sprite.isEmpty() && allowedScanlineSprite : spriteName == defaultSprite
            || (fx.kind == "note" && spriteName == "note_dot")
            || (lightning && (spriteName == "bolt_b" || spriteName == "bolt_c"))
            || ((fx.kind == "flame" || fx.kind == "flame_glow" || (impact && fx.kind == "fireball"))
                && (spriteName == "flame_tall" || spriteName == "flame_wide" || spriteName == "flame_curl"))
            || (fx.kind == "flame_alt" && spriteName == "flame_alt_b");
        const auto start = std::int64_t(fx.startMs) + offset, end = std::int64_t(fx.endMs) + offset;
        const bool oldWindow = fx.startMs >= *source.displayStartMs && fx.endMs <= *source.displayEndMs;
        const bool emitWindowValid = scanline ? fx.emitMs > 0 && fx.emitMs < end-start && fx.timeScale == 1
                                             : fx.emitMs == 0;
        const bool validBand = !scanline || source.scanlineEnabled || noWipeSinging;
        if (!(star || symbol || ripple || falling || lightning || flame || scanline || assemblySprite || impact) || fx.anchor != "char"
            || (verticalParticles && !(verticalStars && star && fx.sweep == (fx.kind == "twinkle" ? 1 : 0))
                && !(verticalShapes && (symbol || ripple) && fx.sweep == 0)
                && !(verticalFalling && falling && fx.sweep == 0)
                && !(verticalLightning && lightning && fx.sweep == 0)
                && !(verticalFlame && flame && fx.sweep == singingFlameSweep)
                && !(verticalScanline && verticalBand && scanline && fx.sweep == 0))
            || fx.charIndex < 0 || fx.charIndex >= int(source.chars.size())
            || !validBand || !validSweep
            || ((fx.hasPaint || fx.hasDecor) && !painted) || !fx.charColors.empty() || !allowedSprite
            || (version == 1 && (fx.timeScale != 1.0 || !oldWindow))
            || !std::isfinite(fx.timeScale) || fx.timeScale < .1 || fx.timeScale > 10
            || !emitWindowValid || !std::isfinite(fx.emitX) || std::abs(fx.emitX) > (flame ? .5 : 0.)
            || fx.reverse != (scanline && source.wipeReverse) || fx.front == back
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
        burst.reverse = fx.reverse;
        burst.color = projectColor(fx.color, QStringLiteral("#FFFFFF"));
        if (painted) {
            burst.hasPaint = true;
            burst.fill = projectPaint(fx.fill, fx.color);
            if (fx.strokeWidthPx > 0) {
                burst.stroke = projectPaint(fx.stroke, fx.color);
                burst.strokeWidth = float(fx.strokeWidthPx * cfg.dpr);
            }
            if (fx.stroke2WidthPx > 0) {
                burst.stroke2 = projectPaint(fx.stroke2, fx.color);
                burst.stroke2Width = float(fx.stroke2WidthPx * cfg.dpr);
            }
            burst.hasDecor = fx.hasDecor;
            if (burst.hasDecor) {
                burst.decor = projectPaint(fx.decor, fx.color);
                burst.decorKind = fx.decorKind.toStdString();
                if (shadow) {
                    burst.decorOffsetX = float(fx.decorOffsetX * cfg.dpr);
                    burst.decorOffsetY = float(fx.decorOffsetY * cfg.dpr);
                } else {
                    burst.decorRadius = float(fx.decorRadius * cfg.dpr);
                    burst.decorConcentration = fx.decorConcentration;
                }
            }
        }
        line.bursts.push_back(std::move(burst));
    }
}
}
