#include "unit_hash.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::effects {
float fxUnitHash(std::uint32_t index, std::uint32_t salt) {
    std::uint32_t h = index * 0x85EBCA77u + salt * 0xC2B2AE3Du;
    h ^= h >> 15;
    h *= 0x2545F491u;
    h ^= h >> 13;
    return static_cast<float>(h & 0xFFFFFFu) / 16777216.0f;
}


}  // namespace krok::subtitle::native::effects
