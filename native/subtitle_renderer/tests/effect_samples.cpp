// Dump canonical scalar/pixel samples for comparison with the pre-extraction D2D evaluator
// and across platforms. scripts/compare_effect_samples.py parses this exact layout.
#ifdef KROK_LEGACY_FX_REFERENCE
#include "d2d_fx.h"
namespace fx = krok::subtitle::native::direct2d;
#else
#include "effects/geometry.h"
#include "effects/noise.h"
#include "effects/particles.h"
#include "effects/scanline_column.h"
#include "effects/scanline_image.h"
#endif
#include "effects/unit_hash.h"
#include <fstream>
#include <stdexcept>
#ifndef KROK_LEGACY_FX_REFERENCE
namespace fx = krok::subtitle::native::effects;
#endif
using namespace krok::subtitle::native;

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    std::ofstream out(argv[1], std::ios::binary);
    auto scalar = [&](auto v) { out.write(reinterpret_cast<const char*>(&v), sizeof(v)); };
    for (const char* effect : {"tracking_in", "wave_in", "scatter_out", "converge_out", "stretch_in",
         "stretch_out", "glow_in", "glow_out", "flame", "assemble_in", "dissolve_out", "petal"}) {
        for (int index = 0; index < 6; ++index) for (int t : {-20, 0, 100, 250, 400, 600, 1500}) {
            auto s = fx::geoCharState(effect, 45, index, 6, t, 0, 600, true, index * 40.f, 100, true);
            for (float v : {s.opacity, s.dx, s.dy, s.rotation, s.scaleX, s.scaleY, s.glowAlpha, s.glowRadiusEm}) scalar(v);
        }
    }
    for (const char* kind : {"ripple", "scanline", "sparkle", "twinkle", "twinkle_classic", "assemble",
         "dissolve", "note", "heart", "flame", "flame_alt", "fireball", "flash", "shockwave", "spark",
         "flame_glow", "lightning", "lightning_glow", "ember", "smoke", "ash", "petal", "feather", "snow", "snow_solid"}) {
        for (const char* sprite : {"star4", "petal", "feather", "snow", "snow_solid", "note"})
        for (int sweep : {-1, 0, 1, 2}) for (float speed : {0.5f, 1.f, 2.f})
        for (std::uint32_t seed : {0u, 0xfffffffau}) for (int t : {0, 80, 300, 700, 1200, 1800, 2000}) {
            ParticleBurst burst;
            burst.kind = kind; burst.sprite = sprite; burst.sweep = sweep; burst.timeScale = speed;
            burst.startMs = 0; burst.endMs = 2000; burst.emitMs = 1200; burst.seed = seed; burst.count = 12;
            const auto particles = fx::burstParticlesAt(burst, t, 20, 30, 80, 60);
            scalar(static_cast<std::uint32_t>(particles.size()));
            for (const auto& p : particles)
                for (float v : {p.x, p.y, p.rotationDeg, p.sizePx, p.alpha, p.stretchX, p.stretchY}) scalar(v);
        }
    }
    for (int level : {0, 1, 2, 16, 64, 128, 254, 255}) for (bool streaks : {false, true}) {
        auto pixels = fx::noiseMaskPixels(level, streaks);
        scalar(static_cast<std::uint32_t>(pixels.size()));
        out.write(reinterpret_cast<const char*>(pixels.data()), pixels.size() * sizeof(pixels[0]));
    }
    for (const char* shape : {"pillar", "flame"}) for (int t : {0, 100, 500}) {
        PaintStyle fill; fill.mode = "gradient"; fill.stops = {{0, {255, 40, 20, 180}}, {1, {20, 50, 255, 200}}};
        const auto pixels = fx::scanlineColumnPixels(shape, {}, {}, {}, &fill, t, 20, 40, 0, 20);
        out.write(reinterpret_cast<const char*>(pixels.data()), pixels.size());
    }
    std::vector<std::uint8_t> rgba(12 * 10 * 4);
    for (size_t i = 0; i < rgba.size(); ++i) rgba[i] = static_cast<std::uint8_t>(i % 127);
    for (int mode = 0; mode < 8; ++mode) {
        fx::ScanlineImageDecor decor;
        decor.tint = mode & 1; decor.glow = mode & 2; decor.shadow = mode & 4;
        decor.strokeWidth = 2; decor.stroke2Width = 1; decor.glowRadius = 4;
        auto image = fx::scanlineImageCompositePixels(rgba.data(), 48, 12, 10, decor);
        scalar(image.width); scalar(image.height); scalar(image.margin);
        out.write(reinterpret_cast<const char*>(image.pixels.data()), image.pixels.size());
    }
    return out ? 0 : 1;
}
