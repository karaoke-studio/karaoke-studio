#pragma once

namespace krok::subtitle::native {
// Final normalized progress at an authored/resolved integer timestamp.
struct WipePoint {
    int timeMs = 0;
    float position = 0.0f;
    bool operator==(const WipePoint&) const = default;
};
}
