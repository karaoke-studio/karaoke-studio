#pragma once

#include "include/core/SkShader.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

namespace subtitle::skia {
enum class ScanlineColorMode { Color, FollowBefore, FollowAfter, Brighten };

// Outer outline, primary outline, and body, each indexed by wipe phase.
using ScanlineGradients = std::array<std::array<sk_sp<SkShader>, 2>, 3>;

struct ScanlinePaint {
    ScanlineColorMode mode = ScanlineColorMode::Color;
    std::uint32_t argb = 0xffffffff;
    float brightness = 0;

    bool splitAtFront() const { return mode == ScanlineColorMode::Brighten; }
    bool visible() const { return !splitAtFront() || brightness > 0; }

    std::uint32_t shade(std::uint32_t before, std::uint32_t after, bool sung) const {
        if (mode == ScanlineColorMode::Color) return argb;
        const bool afterColor = mode == ScanlineColorMode::FollowAfter || (splitAtFront() && sung);
        const std::uint32_t source = afterColor ? after : before;
        const auto red = (source >> 16) & 0xff, green = (source >> 8) & 0xff, blue = source & 0xff;
        const float maximum = float(std::max({red, green, blue}));
        const float raised = maximum + (255.f - maximum) * brightness;
        const auto channel = [&](std::uint32_t component) {
            return std::uint32_t(std::lround(maximum > 0 ? std::min(float(component) * (raised / maximum), 255.f)
                                                       : raised));
        };
        return (source & 0xff000000U) | (channel(red) << 16) | (channel(green) << 8) | channel(blue);
    }
};
}
