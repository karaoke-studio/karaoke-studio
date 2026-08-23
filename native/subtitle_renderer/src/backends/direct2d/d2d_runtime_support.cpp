#include "d2d_runtime_support.h"

#include <d2d1helper.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <sstream>

namespace krok::subtitle::native::direct2d {

double elapsedMs(RuntimeClock::time_point start) {
    return std::chrono::duration<double, std::milli>(RuntimeClock::now() - start).count();
}

std::int64_t steadyNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        RuntimeClock::now().time_since_epoch()
    ).count();
}

bool environmentFlagEnabled(const char *name, bool defaultValue) {
    const char *value = std::getenv(name);
    if (value == nullptr || *value == '\0') {
        return defaultValue;
    }
    return std::strcmp(value, "0") != 0
        && std::strcmp(value, "false") != 0
        && std::strcmp(value, "False") != 0
        && std::strcmp(value, "FALSE") != 0;
}

std::uint64_t rectAreaPx(const D2D1_RECT_F &rect) {
    const double width = std::max(
        static_cast<double>(rect.right - rect.left), 0.0
    );
    const double height = std::max(
        static_cast<double>(rect.bottom - rect.top), 0.0
    );
    return static_cast<std::uint64_t>(std::ceil(width * height));
}

namespace {

std::string hresultText(
    const char *operation,
    HRESULT value,
    const std::string &deviceReason = {}
) {
    std::ostringstream stream;
    stream << operation << " failed (HRESULT=0x" << std::uppercase << std::hex
           << static_cast<unsigned long>(value) << ")";
    if (!deviceReason.empty()) {
        stream << "; " << deviceReason;
    }
    return stream.str();
}

}  // namespace

void checkHr(HRESULT value, const char *operation, const D2DDevice &device) {
    if (FAILED(value)) {
        throw BackendError(hresultText(operation, value, device.deviceRemovedReason()));
    }
}

std::uint8_t unpremultiply(std::uint8_t value, std::uint8_t alpha) {
    if (alpha == 0) {
        return 0;
    }
    return static_cast<std::uint8_t>(std::min(
        255u,
        (static_cast<unsigned>(value) * 255u + alpha / 2u) / alpha
    ));
}

D2D1_COLOR_F d2dColor(const RgbaColor &color) {
    return D2D1::ColorF(
        static_cast<float>(color.red) / 255.0f,
        static_cast<float>(color.green) / 255.0f,
        static_cast<float>(color.blue) / 255.0f,
        static_cast<float>(color.alpha) / 255.0f
    );
}

}  // namespace krok::subtitle::native::direct2d
