#include "noise.h"
#include "unit_hash.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::effects {
int noiseLevel(const std::string &entry, const std::string &exit,
    int entryMs, int exitMs, int start, int end, int lineEnd, int t) {
    if (exit == "noise" && exitMs > 0) {
        const int exitStart = std::max(lineEnd, end - std::clamp(exitMs, 120, 3000));
        if (t >= exitStart) {
            const int duration = std::max(end - exitStart, 1);
            return std::clamp(((end - t) * 64 + duration / 2) / duration, 0, 64);
        }
    }
    if (entry == "noise" && entryMs > 0) {
        const int duration = std::clamp(entryMs, 120, 3000);
        return std::clamp(((t - start) * 64 + duration / 2) / duration, 0, 64);
    }
    return 64;
}

std::vector<std::uint32_t> noiseMaskPixels(int level, bool streaks) {
    const auto hash = [](std::uint32_t index, std::uint32_t salt) {
        return static_cast<std::uint32_t>(fxUnitHash(index, salt) * 16777216.0f);
    };
    std::vector<std::uint32_t> pixels(128 * 128);
    for (int y = 0; y < 128; ++y) {
        for (int x = 0; x < 128; ++x) {
            const auto index = hash(x / 32, 91) % 4 == 0
                ? (y / 2) * 128 + x / 24 : y * 128 + x;
            const bool visible = streaks ? hash(x, 73) % 8 < 3
                : static_cast<int>(hash(index, 37) % 64) < level;
            pixels[y * 128 + x] = visible ? 0xFFFFFFFFu : 0u;
        }
    }
    return pixels;
}


}  // namespace krok::subtitle::native::effects
