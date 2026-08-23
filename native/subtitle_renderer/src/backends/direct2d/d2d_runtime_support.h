#pragma once

#include "../render_backend.h"
#include "d2d_device.h"

#include <d2d1_2.h>

#include <chrono>
#include <cstdint>

namespace krok::subtitle::native::direct2d {

using RuntimeClock = std::chrono::steady_clock;

double elapsedMs(RuntimeClock::time_point start);
std::int64_t steadyNowMs();
bool environmentFlagEnabled(const char *name, bool defaultValue);
std::uint64_t rectAreaPx(const D2D1_RECT_F &rect);
void checkHr(HRESULT value, const char *operation, const D2DDevice &device);
std::uint8_t unpremultiply(std::uint8_t value, std::uint8_t alpha);
D2D1_COLOR_F d2dColor(const RgbaColor &color);

}  // namespace krok::subtitle::native::direct2d
