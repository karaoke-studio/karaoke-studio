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
    struct Band {
        int top = 0;
        int height = 0;
        int packedTop = 0;
        bool operator==(const Band &) const = default;
    };
    std::vector<Band> bands;
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

struct DisplayWindow {
    int startMs = 0;
    int endMs = 0;
    bool operator==(const DisplayWindow &) const = default;
};

struct TextLine {
    std::vector<TextChar> chars;
    std::vector<TextRuby> rubies;
    int startMs = 0;
    int endMs = 0;
    int sourceIndex = 0;
    int sourceLineIndex = 0;
    int lane = 0;
    int compositeOrder = 0;
    bool staticOverlay = false;
    int fadeInMs = 0;
    int fadeOutMs = 0;
    std::string entryAnimation = "none";
    int entryDurationMs = 0;
    std::string exitAnimation = "none";
    int exitDurationMs = 0;
    std::vector<DisplayWindow> displayWindows;
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
    bool vertical = false;
    bool rightToLeft = false;
    float centerOffsetX = 0.0f;
    float centerOffsetY = 0.0f;
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
    bool litEnabled = false;
    std::string litStyle = "volume";
    int litNumber = 4;
    float litSize = 32.0f;
    float litOffsetX = 0.0f;
    float litOffsetY = -24.0f;
    float litTracking = 0.0f;
    RgbaColor litFill{0, 0, 255, 255};
    RgbaColor litStroke{255, 255, 255, 255};
    float litStrokeWidth = 2.0f;
    float litStrokeSoften = 0.0f;
    float litOpacity = 1.0f;
    float litEdgeBrightness = 0.6f;
    bool litShadow = true;
    int litTimeOffsetMs = 0;
    int litWaitingTimeMs = 0;
    std::string litTransitionMode = "fade";
    int litTransitionRatioPct = 67;
    float litTransitionAngleDeg = 0.0f;
    float litTransitionDistance = 0.0f;
    int signalsDurationMs = 4000;
    float volumeSize = 48.0f;
    float volumeOffsetX = 0.0f;
    float volumeOffsetY = 0.0f;
    float volumeColumnWidth = 12.0f;
    int volumeColumnCount = 4;
    float volumeColumnSpacing = 0.0f;
    int volumeAlign = 1;
    float volumeRatio = 3.0f;
    RgbaColor volumeFill{255, 255, 255, 255};
    RgbaColor volumeStroke{0, 0, 255, 255};
    RgbaColor volumeOverlayFill{0, 0, 255, 255};
    RgbaColor volumeOverlayStroke{255, 255, 255, 255};
    int volumeFlashTimes = 3;
    float volumeFlashDurationRatio = 1.0f;
    int volumeTransitionRatioPct = 67;
    bool operator==(const TextStyle &) const = default;
};

struct RenderScene {
    int width = 1920;
    int height = 1080;
    float viewportScale = 1.0f;
    float viewportRotation = 0.0f;
    float viewportOffsetX = 0.0f;
    float viewportOffsetY = 0.0f;
    std::string viewportAlign = "center";
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
    bool videoMemoryInfoAvailable = false;
    std::uint64_t localVideoMemoryUsageBytes = 0;
    std::uint64_t localVideoMemoryBudgetBytes = 0;
    std::uint64_t nonLocalVideoMemoryUsageBytes = 0;
    std::uint64_t nonLocalVideoMemoryBudgetBytes = 0;
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
    virtual ProbeResult renderFrame(int tMs, bool compactBands = false) = 0;
};

}  // namespace krok::subtitle::native
