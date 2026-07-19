#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace krok::subtitle::native {

enum class PixelFormat {
    Rgba8888Straight,
};

struct BackendCaps {
    std::string backend;
    std::string adapterName;
    std::string featureLevel;
    std::uint32_t adapterVendorId = 0;
    std::uint32_t adapterDeviceId = 0;
    std::uint64_t dedicatedVideoMemory = 0;
    bool hardware = false;
    bool warp = false;
    bool supportsTransparentSurface = false;
    bool supportsStagingReadback = false;
    bool supportsGlyphs = false;
};

struct ProbeOptions {
    int width = 256;
    int height = 144;
    std::uint8_t red = 51;
    std::uint8_t green = 102;
    std::uint8_t blue = 204;
    std::uint8_t alpha = 128;
    bool drawGlyph = true;
};

struct RenderSurface {
    int width = 0;
    int height = 0;
    int stride = 0;
    PixelFormat pixelFormat = PixelFormat::Rgba8888Straight;
    std::vector<std::uint8_t> bytes;
};

struct ProbeResult {
    RenderSurface surface;
    double renderMs = 0.0;
    double readbackMs = 0.0;
};

class BackendError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class RenderBackend {
public:
    virtual ~RenderBackend() = default;
    virtual BackendCaps capabilities() const = 0;
    virtual ProbeResult renderProbe(const ProbeOptions &options) = 0;
};

}  // namespace krok::subtitle::native
