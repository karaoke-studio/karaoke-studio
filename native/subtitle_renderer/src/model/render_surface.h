#pragma once

#include <cstdint>
#include <vector>

namespace krok::subtitle::native {

enum class PixelFormat {
    Rgba8888Straight,
    Bgra8888Premultiplied,
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
    struct FrameDiagnostics {
        bool countersEnabled = true;
        // Actual frame-wide path selection, independent of diagnostic counters.
        bool realizationPathReady = false;
        // 资源版本：本帧渲染时的 realization 代际（configure 重建时递增）。
        // 画面在某帧发生变化时，据此判断是否与资源换代相关（2026-10 抖动
        // 定位闭环：路径 + 代际 + 命中数一起记录，不再靠 FPS 猜）。
        std::uint64_t realizationGeneration = 0;
        std::uint64_t brushCreated = 0;
        std::uint64_t geometryCreatedStable = 0;
        std::uint64_t geometryCreatedDynamic = 0;
        std::uint64_t realizationHit = 0;
        std::uint64_t realizationMiss = 0;
        std::uint64_t strokeDraw = 0;
        std::uint64_t stroke2Draw = 0;
        std::uint64_t glowSourceAreaPx = 0;
        std::uint64_t layerPush = 0;
        double animationLayoutMs = 0.0;
        double geometryMs = 0.0;
        double strokeMs = 0.0;
        double glowMs = 0.0;
        double endDrawWaitMs = 0.0;
        double endDrawGlowSourceMs = 0.0;
        double endDrawRubyGlowSourceMs = 0.0;
        double endDrawInlineGlowSourceMs = 0.0;
        double endDrawFrameLayersMs = 0.0;
        double endDrawEmptyFrameMs = 0.0;
        std::uint64_t endDrawCount = 0;
        std::uint64_t endDrawGlowSourceCount = 0;
        std::uint64_t endDrawRubyGlowSourceCount = 0;
        std::uint64_t endDrawInlineGlowSourceCount = 0;
        std::uint64_t endDrawFrameLayersCount = 0;
        std::uint64_t endDrawEmptyFrameCount = 0;
        double gpuWaitMs = 0.0;
        double readbackCopyMs = 0.0;
    } frameDiagnostics;
};

}  // namespace krok::subtitle::native
