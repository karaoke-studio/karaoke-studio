#pragma once

#include "d2d_backend.h"
#include "d2d_runtime_support.h"

#include <d2d1_2.h>

#include <atomic>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

namespace krok::subtitle::native {

struct Direct2DGpuBackend::Impl {
    struct CachedImage {
        std::wstring path;
        std::uint64_t modifiedMs = 0;
        std::uint64_t size = 0;
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
    };
    struct CachedChar {
        int startMs = 0;
        int endMs = 0;
        float left = 0.0f;
        float right = 0.0f;
        float layoutLeft = 0.0f;
        float layoutRight = 0.0f;
        float top = 0.0f;
        float bottom = 0.0f;
        int styleIndex = -1;
        float boxAscent = 0.0f;
        float pivotX = 0.0f;
        float pivotY = 0.0f;
        Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> protectedStrokeGeometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> strokeGeometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> stroke2Geometry;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> fillRealization;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> protectedStrokeRealization;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> strokeRealization;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> stroke2Realization;
        std::optional<BitmapGuide> bitmapGuide;
        D2D1_RECT_F bitmapRect{};
        std::vector<WipePoint> wipePoints;
    };

    struct CachedRuby {
        int startMs = 0;
        int endMs = 0;
        float baselineOffset = 0.0f;
        int styleIndex = -1;
        int transitionCharIndex = 0;
        int firstCharIndex = 0;
        int lastCharIndex = 0;
        float pivotX = 0.0f;
        float pivotY = 0.0f;
        D2D1_RECT_F bounds{};
        D2D1_RECT_F fillBounds{};
        D2D1_RECT_F horizontalFillBounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> protectedStrokeGeometries;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> strokeGeometries;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> stroke2Geometries;
    };

    struct CachedLine {
        int startMs = 0;
        int endMs = 0;
        int sourceIndex = 0;
        int sourceLineIndex = 0;
        int pageIndex = -1;
        int compositeOrder = 0;
        int lane = 0;
        bool signalHead = false;
        bool staticOverlay = false;
        int fadeInMs = 0;
        int fadeOutMs = 0;
        std::string entryAnimation = "none";
        int entryDurationMs = 0;
        std::string exitAnimation = "none";
        int exitDurationMs = 0;
        std::string karaokeAnimation = "none";
        std::vector<DisplayWindow> displayWindows;
        std::vector<PlacementWindow> placementWindows;
        TextStyle style;
        float ascent = 0.0f;
        float descent = 0.0f;
        float boxAscent = 0.0f;
        bool hasRubyAnchor = false;
        float verticalRubyAllowance = 0.0f;
        float maxVisualPad = 0.0f;
        float legacyLaneHeight = 1.0f;
        float legacyLaneDescent = 0.0f;
        float n3DrawHeight = 1.0f;
        float n3Descent = 0.0f;
        // N3 char boxes accumulated over the line's own glyphs, independent of
        // the line style.  Static overlays (the title) size their box from
        // these so an inline role style fully governs the block; lyrics keep
        // the line-style box to hold the shared lane grid steady.
        float n3CharAscent = 0.0f;
        float n3CharDescent = 0.0f;
        bool hasN3CharBox = false;
        bool hasInlineStyles = false;
        bool hasInlineLaneGeometryOverride = false;
        std::optional<float> guideAnchorLeft;
        std::optional<float> guideAnchorRight;
        bool centerOverride = false;
        D2D1_RECT_F bounds{};
        D2D1_RECT_F fillBounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
        std::vector<CachedRuby> rubies;
    };

    struct CachedBrush {
        PaintStyle paint;
        RgbaColor fallback;
        ID2D1Bitmap1 *imageIdentity = nullptr;
        D2D1_RECT_F rect{};
        float canvasDx = 0.0f;
        float canvasDy = 0.0f;
        Microsoft::WRL::ComPtr<ID2D1Brush> brush;
        std::uint64_t lastUse = 0;
    };

    enum class RealizationKind {
        Fill,
        ProtectedStroke,
        Stroke,
        Stroke2,
    };

    struct RealizationTask {
        std::size_t lineIndex = 0;
        int rubyIndex = -1;
        std::size_t charIndex = 0;
        RealizationKind kind = RealizationKind::Fill;
        Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
        float strokeWidth = 0.0f;
    };

    struct RealizationControl {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::uint64_t generation = 0;
    };

    struct RetiredRealizationWorker {
        std::shared_ptr<RealizationControl> control;
        std::thread thread;
    };

    struct GlowScratch {
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
        UINT32 width = 0;
        UINT32 height = 0;
    };

    RenderScene scene;
    std::vector<CachedLine> lines;
    std::vector<CachedImage> images;
    std::vector<CachedBrush> brushes;
    std::uint64_t brushUseSerial = 0;
    static constexpr std::size_t brushCapacity = 512;
    Microsoft::WRL::ComPtr<ID2D1DeviceContext1> realizationContext;
    std::uint64_t realizationCount = 0;
    std::uint64_t realizationGeneration = 0;
    static constexpr std::size_t defaultRealizationCapacity = 8192;
    static constexpr float realizationStrokeThreshold = 8.0f;
    std::shared_ptr<RealizationControl> realizationControl;
    std::thread realizationThread;
    std::vector<RetiredRealizationWorker> retiredRealizationWorkers;
    std::atomic<bool> realizationPrewarmComplete{true};
    std::atomic<bool> renderActive{false};
    std::atomic<bool> firstFrameCompleted{false};
    std::atomic<std::int64_t> lastRenderCompletedMs{0};
    mutable std::mutex realizationMutex;
    BackendDiagnostics diagnostics;
    bool configured = false;
    int frameSurfaceWidth = 0;
    int frameSurfaceHeight = 0;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> frameTargetTexture;
    Microsoft::WRL::ComPtr<ID2D1Bitmap1> frameTargetBitmap;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> frameStagingTexture;
    // Persistent glow scratch targets and GaussianBlur effects. Dirty-rect
    // mode grows each scratch slot only to its largest requested region;
    // entries rewind per line after the composite is flushed.
    std::vector<GlowScratch> glowScratchPool;
    std::vector<Microsoft::WRL::ComPtr<ID2D1Effect>> glowEffectPool;
    std::size_t glowScratchInUse = 0;
    std::size_t glowEffectInUse = 0;
#if KROK_GPU_DIAGNOSTICS
    bool countersEnabled = direct2d::environmentFlagEnabled("KROK_GPU_COUNTERS", true);
#else
    bool countersEnabled = false;
#endif
    bool resourceCacheEnabled = direct2d::environmentFlagEnabled(
        "KROK_GPU_RESOURCE_CACHE", true
    );
    bool realizationEnabled = direct2d::environmentFlagEnabled(
        "KROK_GPU_REALIZATION", true
    );
    bool realizationActive = false;
    bool glowDirtyRectEnabled = direct2d::environmentFlagEnabled(
        "KROK_GPU_GLOW_DIRTY_RECT", true
    );
    // N3 transforms one base glyph geometry and applies dynamic edge widths
    // with DrawGeometry.  Keep an environment rollback while this path is
    // measured against the previous transform(pre-expanded stroke)+FillGeometry
    // implementation.
    bool dynamicDirectStrokeEnabled = direct2d::environmentFlagEnabled(
        "KROK_GPU_DYNAMIC_DIRECT_STROKE", true
    );
};

}  // namespace krok::subtitle::native
