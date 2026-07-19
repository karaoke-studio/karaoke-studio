#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>
#include <optional>

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

struct RgbaColor {
    std::uint8_t red = 255;
    std::uint8_t green = 255;
    std::uint8_t blue = 255;
    std::uint8_t alpha = 255;
};

struct TextChar {
    std::wstring text;
    int startMs = 0;
    int endMs = 0;
};

struct TextLine {
    std::vector<TextChar> chars;
    int startMs = 0;
    int endMs = 0;
};

struct TextStyle {
    std::wstring fontFamily;
    std::optional<std::wstring> latinFontFamily;
    float fontSize = 100.0f;
    std::optional<float> latinFontSize;
    int fontWeight = 400;
    std::optional<int> latinFontWeight;
    bool italic = false;
    bool allowBiting = false;
    int spaceWidthPercent = 20;
    float letterSpacing = 0.0f;
    float horizontalMargin = 50.0f;
    float bottomMargin = 80.0f;
    float lineGap = 90.0f;
    bool dualLineLayout = true;
    int laneCount = 2;
    std::string alignment = "center";
    std::string verticalPosition = "bottom";
    int leadInMs = 1800;
    int tailMs = 1000;
    RgbaColor beforeFill;
    RgbaColor afterFill{255, 90, 111, 255};
    RgbaColor beforeStroke{34, 34, 34, 255};
    RgbaColor afterStroke{34, 34, 34, 255};
    RgbaColor beforeStroke2{0, 0, 0, 255};
    RgbaColor afterStroke2{0, 0, 0, 255};
    RgbaColor beforeDecor{0, 0, 0, 255};
    RgbaColor afterDecor{0, 0, 0, 255};
    float strokeWidth = 0.0f;
    float stroke2Width = 0.0f;
    std::string decorationKind = "none";
    float glowBeforeRadius = 10.0f;
    float glowAfterRadius = 10.0f;
    int glowConcentrationLevel = 0;
};

struct RenderScene {
    int width = 1920;
    int height = 1080;
    TextStyle style;
    std::vector<TextLine> lines;
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
    virtual void configure(const RenderScene &scene) = 0;
    virtual ProbeResult renderFrame(int tMs) = 0;
};

}  // namespace krok::subtitle::native
