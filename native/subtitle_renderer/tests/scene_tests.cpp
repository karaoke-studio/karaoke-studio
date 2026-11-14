#include "effects/geometry.h"
#include "effects/noise.h"
#include "effects/particles.h"
#include "effects/scanline_column.h"
#include "effects/scanline_image.h"
#include "effects/unit_hash.h"
#include "model/render_scene.h"
#include <cmath>
#include <iostream>
#include <stdexcept>

using namespace krok::subtitle::native;
namespace fx = krok::subtitle::native::effects;
void require(bool condition, const char* reason) {
    if (!condition) throw std::runtime_error(reason);
}
int main() {
    try {
        require(fx::fxUnitHash(0, 0) == 0, "hash zero");
        require(fx::fxUnitHash(0xffffffffu, 5) >= 0 && fx::fxUnitHash(0xffffffffu, 5) < 1, "unsigned hash");
        for (const auto* kind : {"ripple", "scanline", "sparkle", "twinkle", "twinkle_classic",
             "assemble", "dissolve", "note", "heart", "flame", "flame_alt", "fireball", "flash",
             "shockwave", "spark", "flame_glow", "lightning", "lightning_glow", "ember", "smoke",
             "ash", "petal", "feather", "snow", "snow_solid"}) {
            for (const auto* sprite : {"star4", "petal", "feather", "snow", "snow_solid", "note"}) {
                ParticleBurst burst;
                burst.kind = kind;
                burst.sprite = sprite;
                burst.startMs = 100;
                burst.endMs = 2100;
                burst.emitMs = 1200;
                burst.count = 12;
                burst.seed = 0xfffffffau;
                require(fx::burstParticlesAt(burst, 99, 20, 30, 80, 60).empty(), "before window");
                require(fx::burstParticlesAt(burst, 2101, 20, 30, 80, 60).empty(), "after window");
                for (int t : {100, 300, 800, 1500, 2100, 800, 300}) {
                    auto first = fx::burstParticlesAt(burst, t, 20, 30, 80, 60);
                    const auto again = fx::burstParticlesAt(burst, t, 20, 30, 80, 60);
                    require(first.size() == again.size(), "deterministic particle count");
                    for (size_t i = 0; i < first.size(); ++i) {
                        const auto& a = first[i]; const auto& b = again[i];
                        require(a.x == b.x && a.y == b.y && a.alpha == b.alpha && a.sizePx == b.sizePx,
                                "deterministic particle state");
                        require(std::isfinite(a.x) && std::isfinite(a.y) && std::isfinite(a.rotationDeg)
                                && std::isfinite(a.sizePx) && std::isfinite(a.alpha), "finite particle state");
                    }
                }
            }
        }
        for (const auto* effect : {"tracking_in", "wave_in", "scatter_out", "converge_out", "glow_in", "glow_out"}) {
            const auto state = fx::geoCharState(effect, 40, 1, 5, 400, 100, 600, false, 60, 100, true);
            require(std::isfinite(state.opacity) && state.opacity >= 0 && state.opacity <= 1, "geometry opacity");
        }
        require(fx::fallEntryLeadMs("petal") == 340 && fx::fallEntryLeadMs("snow") == 300, "fall timing");
        const auto column = fx::scanlineColumnPixels("pillar", {}, {}, {}, nullptr, 300, 12, 30, 0, 12);
        require(column.size() == 12 * 30 * 4, "column byte size");
        std::vector<std::uint8_t> source(8 * 8 * 4, 100);
        fx::ScanlineImageDecor decor;
        decor.tint = true;
        auto image = fx::scanlineImageCompositePixels(source.data(), 32, 8, 8, decor);
        require(image.width >= 8 && image.height >= 8 && !image.pixels.empty(), "image decoration");
        std::cout << "scene values and deterministic effects passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
