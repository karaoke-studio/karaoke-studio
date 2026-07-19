#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>
#include <optional>

namespace krok::subtitle::native {

enum class PixelFormat {
    Rgba8888Straight,
    Bgra8888Premultiplied,
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
    bool operator==(const RgbaColor &) const = default;
};

struct PaintStop {
    float position = 0.0f;
    RgbaColor color;
    bool operator==(const PaintStop &) const = default;
};

struct PaintStyle {
    std::string mode = "solid";
    RgbaColor color;
    std::vector<PaintStop> stops;
    std::wstring imagePath;
    float imageScale = 1.0f;
    std::uint64_t imageModifiedMs = 0;
    std::uint64_t imageSize = 0;
    bool operator==(const PaintStyle &) const = default;
};

struct TextChar {
    std::wstring text;
    int startMs = 0;
    int endMs = 0;
    int styleIndex = -1;
    bool operator==(const TextChar &) const = default;
};

struct RubyUnit {
    std::wstring text;
    int startMs = 0;
    int endMs = 0;
    bool operator==(const RubyUnit &) const = default;
};

struct TextRuby {
    std::wstring baseText;
    std::wstring reading;
    std::vector<RubyUnit> units;
    int firstCharIndex = 0;
    int lastCharIndex = 0;
    int startMs = 0;
    int endMs = 0;
    int styleIndex = -1;
    bool operator==(const TextRuby &) const = default;
};

struct TextLine {
    std::vector<TextChar> chars;
    std::vector<TextRuby> rubies;
    int startMs = 0;
    int endMs = 0;
    bool operator==(const TextLine &) const = default;
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
    bool affectsRubyAnchor = true;
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
    PaintStyle beforeFillPaint;
    PaintStyle afterFillPaint;
    PaintStyle beforeStrokePaint;
    PaintStyle afterStrokePaint;
    PaintStyle beforeStroke2Paint;
    PaintStyle afterStroke2Paint;
    PaintStyle beforeDecorPaint;
    PaintStyle afterDecorPaint;
    float strokeWidth = 0.0f;
    float stroke2Width = 0.0f;
    std::string decorationKind = "none";
    float glowBeforeRadius = 10.0f;
    float glowAfterRadius = 10.0f;
    int glowConcentrationLevel = 0;
    float shadowOffsetX = 0.0f;
    float shadowOffsetY = 1.0f;
    std::wstring rubyFontFamily;
    std::optional<std::wstring> rubyLatinFontFamily;
    float rubyFontSize = 45.0f;
    std::optional<float> rubyLatinFontSize;
    int rubyFontWeight = 400;
    std::optional<int> rubyLatinFontWeight;
    float rubyGap = 0.0f;
    float rubyInterval = 0.0f;
    std::string rubyAlignment = "auto";
    RgbaColor rubyBeforeFill;
    RgbaColor rubyAfterFill{255, 90, 111, 255};
    RgbaColor rubyBeforeStroke{34, 34, 34, 255};
    RgbaColor rubyAfterStroke{34, 34, 34, 255};
    RgbaColor rubyBeforeStroke2{0, 0, 0, 255};
    RgbaColor rubyAfterStroke2{0, 0, 0, 255};
    RgbaColor rubyBeforeDecor{0, 0, 0, 255};
    RgbaColor rubyAfterDecor{0, 0, 0, 255};
    PaintStyle rubyBeforeFillPaint;
    PaintStyle rubyAfterFillPaint;
    PaintStyle rubyBeforeStrokePaint;
    PaintStyle rubyAfterStrokePaint;
    PaintStyle rubyBeforeStroke2Paint;
    PaintStyle rubyAfterStroke2Paint;
    PaintStyle rubyBeforeDecorPaint;
    PaintStyle rubyAfterDecorPaint;
    float rubyStrokeWidth = 0.0f;
    float rubyStroke2Width = 0.0f;
    std::string rubyDecorationKind = "none";
    float rubyGlowBeforeRadius = 0.0f;
    float rubyGlowAfterRadius = 0.0f;
    int rubyGlowConcentrationLevel = 0;
    float rubyShadowOffsetX = 0.0f;
    float rubyShadowOffsetY = 1.0f;
    bool operator==(const TextStyle &) const = default;
};

struct RenderScene {
    int width = 1920;
    int height = 1080;
    TextStyle style;
    std::vector<TextStyle> lineStyles;
    std::vector<TextStyle> charStyles;
    std::vector<TextLine> lines;
    bool operator==(const RenderScene &) const = default;
};

struct BackendDiagnostics {
    std::uint64_t cacheHits = 0;
    std::uint64_t cacheMisses = 0;
    std::uint64_t estimatedCacheBytes = 0;
    std::uint64_t lineCount = 0;
    std::uint64_t charCount = 0;
    std::uint64_t geometryCount = 0;
    std::uint64_t rubyCount = 0;
    std::uint64_t styleCount = 0;
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
    virtual BackendDiagnostics diagnostics() const = 0;
    virtual void configure(const RenderScene &scene) = 0;
    virtual ProbeResult renderFrame(int tMs) = 0;
};

}  // namespace krok::subtitle::native
