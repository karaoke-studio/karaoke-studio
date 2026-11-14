#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace krok::subtitle::native::effects {
int noiseLevel(const std::string &entry, const std::string &exit,
    int entryMs, int exitMs, int start, int end, int lineEnd, int t);
std::vector<std::uint32_t> noiseMaskPixels(int level, bool streaks);


}
