#pragma once

#include "d2d_backend.h"
#include "d2d_runtime_support.h"

#include <d2d1_2.h>
#include <dwrite.h>

#include <atomic>
#include <cstdint>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <tuple>
#include <vector>

namespace krok::subtitle::native {


struct Direct2DGpuBackend::Impl {
    struct CachedImage {
        std::wstring path;
        std::uint64_t modifiedMs = 0;
        std::uint64_t size = 0;
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
        // 动图（GIF）合成帧序列 + 每帧时长；空 = 静态图，走 bitmap。
        // 选帧规则见 d2d_backend_render.cpp 的 guideBitmapAt，与 Python
        // metrics.AnimatedGuideImage.frame_at 是同一契约。
        std::vector<Microsoft::WRL::ComPtr<ID2D1Bitmap1>> frames;
        std::vector<int> frameDelaysMs;
        bool animationChecked = false;
        std::uint64_t lastUse = 0;
    };
    // 装饰粒子 sprite 轮廓几何（em 空间、中心原点；随场景重建，绘制期经
    // matrix 缩放平移）。key = sprite 名（star4 / ring / note）。
    std::map<std::string, Microsoft::WRL::ComPtr<ID2D1PathGeometry>> fxSpriteGeometries;
    // 2026-10 涟漪极坐标环烘焙纹理：非横向渐变映射为角向扫过（conic，
    // p=φ/2π，与 painter._fx_polar_ring_brush 镜像）。按 PaintStyle 签名
    // 缓存，随场景失效重建（fxSpriteGeometries 同一清理点）。
    // 2026-10 粒子装饰笔刷缓存：同一 (PaintStyle × 回退色 × 是否极坐标)
    // 组合全帧全 burst 复用一把笔刷——逐 burst 逐帧 CreateLinearGradientBrush
    // /位图笔刷曾是 GPU 粒子路径的性能大头（用户口径：用过的粒子+角色组合
    // 只烘焙一次）。极坐标分支连烘焙位图带包装笔刷一起缓存。LRU 尾部为
    // 最新，随场景失效重建。
    struct FxPaintBrushEntry {
        PaintStyle paint;
        RgbaColor fallback{255, 255, 255, 255};
        Microsoft::WRL::ComPtr<ID2D1Brush> brush;
    };
    std::vector<FxPaintBrushEntry> fxPaintBrushes;
    // 粒子描边样式（圆角连接/端点）。必须挂在 Impl 上：每个后端实例有
    // 自己的 D2D factory（worker 池/导出后端并存时多 factory 同进程），
    // 跨 factory 复用 stroke style 会在 EndDraw 报 D2DERR_WRONG_FACTORY
    // （2026-10 实测：函数级 static 缓存导致 GPU 预览直接回退）。
    Microsoft::WRL::ComPtr<ID2D1StrokeStyle> fxRoundStrokeStyle;
    // 指示灯「星型/音符」路径几何：按 (litStyle, size) 缓存，随场景失效重建。
    std::map<std::pair<std::string, float>, Microsoft::WRL::ComPtr<ID2D1PathGeometry>>
        lampShapeGeometries;
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
        // 矢量字形（导唱符）的 Clipper2 预展开描边轮廓：静态 realization
        // 按填充语义烘焙它，动画帧也直接填充它（替代对密集原路径的逐帧
        // DrawGeometry）。挂字形资源缓存（按宽度惰性建，毫秒级）。
        Microsoft::WRL::ComPtr<ID2D1Geometry> strokeOutline;
        Microsoft::WRL::ComPtr<ID2D1Geometry> stroke2Outline;
        // Realizations use the shared, unpositioned glyph geometry. Each
        // character keeps only the matrix that places that shared mesh.
        Microsoft::WRL::ComPtr<ID2D1Geometry> realizationGeometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> protectedRealizationGeometry;
        D2D1_MATRIX_3X2_F realizationTransform =
            D2D1::Matrix3x2F::Identity();
        D2D1_MATRIX_3X2_F fillRealizationTransform =
            D2D1::Matrix3x2F::Identity();
        D2D1_MATRIX_3X2_F protectedStrokeRealizationTransform =
            D2D1::Matrix3x2F::Identity();
        D2D1_MATRIX_3X2_F strokeRealizationTransform =
            D2D1::Matrix3x2F::Identity();
        D2D1_MATRIX_3X2_F stroke2RealizationTransform =
            D2D1::Matrix3x2F::Identity();
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
        // 读音是否全拉丁单元：预实现任务据此取 ruby-latin 还是日文轨描边。
        bool latin = false;
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
        bool volumeHead = false;
        bool litHead = false;
        // 「真一组」渐变带正文侧拓宽闸门（协议注释见 render_config.h）：
        // configure 据此拓宽第一角色的横向渐变跨度左缘。
        bool signalBandJoin = false;
        // Python 在源加载入口已把整行时间戳严格逆序的行镜像理顺为顺序；configure
        // 期会把 chars 反转为时间序并反序配对窗口，render 期按本标记翻转走字方向。
        bool wipeReverse = false;
        bool staticOverlay = false;
        int fadeInMs = 0;
        int fadeOutMs = 0;
        std::string entryAnimation = "none";
        int entryDurationMs = 0;
        std::string exitAnimation = "none";
        int exitDurationMs = 0;
        std::string karaokeAnimation = "none";
        // 扫字线叠加开关（来自 TextLine.scanlineEnabled，随行缓存）。
        bool scanlineEnabled = false;
        // 整字放大（zoom_pulse）开关（来自 TextLine.zoomPulseEnabled，随行缓存）：
        // 本体 karaokeAnimation 仍为 "utopia"，靠它切换缩放曲线与字符中心原点。
        bool zoomPulseEnabled = false;
        // 唱字描边闪光 / 装饰粒子（2026-09；渲染期确定性求值，轨迹与
        // Python particles.py 镜像）。粒子 sprite 几何缓存在 Impl 层
        // fxSpriteGeometries，随场景失效重建。
        bool strokeFlashEnabled = false;
        std::vector<ParticleBurst> bursts;
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
        // Style-font ascent/descent WITHOUT the visual pad, mirroring the
        // QFontMetrics the Painter feeds its signal anchors
        // (signal_lit_y).  Lane boxes pad the ascent; using them for the
        // lamp/bar Y would lift the indicator by the text stroke pad and,
        // under N3 semantics, swap in the N3 box entirely.
        float laneFontAscent = 0.0f;
        float laneFontDescent = 0.0f;
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
        bool centerOverride = false;
        D2D1_RECT_F bounds{};
        D2D1_RECT_F fillBounds{};
        // Visible main-glyph ink union per resolved role style.  Key -1 is
        // the line/default role; SVG and bitmap guide geometry participates.
        std::map<int, D2D1_RECT_F> horizontalFillBoundsByStyle;
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

    struct GlyphGeometryResource {
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
        bool hasBounds = false;
        D2D1_RECT_F referenceBounds{};
        D2D1_RECT_F bounds{};
        std::map<float, Microsoft::WRL::ComPtr<ID2D1Geometry>> strokeGeometries;
        std::map<float, Microsoft::WRL::ComPtr<ID2D1Geometry>> stroke2Geometries;
        std::map<float, Microsoft::WRL::ComPtr<ID2D1Geometry>> protectedGeometries;
        // Clipper2 预展开描边轮廓（按宽度惰性缓存）：描边 realization 与
        // 动画帧共用同一份，创建毫秒级（见 d2d_stroke_outline.h）。
        std::map<float, Microsoft::WRL::ComPtr<ID2D1Geometry>> preexpandedStrokes;
        // 路径段数（GetSegmentCount 一次性记入）：任务成本估计与自适应
        // 预热调度的排序键。0 = 未知（按保守成本处理）。
        std::uint32_t segmentCount = 0;
        std::uint64_t lastUse = 0;
    };

    // (family, requested weight, italic, axis hint): the variable-font axis
    // flag must be part of the key, so a static lookup cannot poison the cache
    // for a later variable-axis instance (and vice versa) sharing the same
    // (family, weight, italic).
    using FontFaceKey = std::tuple<std::wstring, int, bool, bool>;
    using TextGlyphKey = std::tuple<
        std::uintptr_t, int, std::uint32_t, int, std::vector<UINT16>
    >;
    using VectorGlyphKey = std::tuple<std::string, int, std::uint32_t>;
    using RealizationCacheKey = std::tuple<
        std::uintptr_t,
        bool,
        std::uint32_t,
        bool,
        std::uint32_t,
        std::uint32_t,
        std::uint32_t,
        std::uint32_t,
        std::uint32_t,
        std::uint32_t
    >;

    enum class RealizationKind {
        Fill,
        ProtectedStroke,
        Stroke,
        Stroke2,
    };

    struct RealizationTarget {
        std::size_t lineIndex = 0;
        int rubyIndex = -1;
        std::size_t charIndex = 0;
        RealizationKind kind = RealizationKind::Fill;
        D2D1_MATRIX_3X2_F transform = D2D1::Matrix3x2F::Identity();
    };

    struct RealizationTask {
        std::vector<RealizationTarget> targets;
        Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> keyGeometry;
        RealizationCacheKey cacheKey{};
        float strokeWidth = 0.0f;
        // 矢量字形的描边任务按填充语义烘焙预展开轮廓（创建走
        // CreateFilledGeometryRealization，毫秒级）。strokeWidth 仍进
        // 缓存键做宽度去重。
        bool fillOutline = false;
        // 预估创建成本（ms）：自适应预热调度用它排序与限流。
        float estCostMs = 0.0f;
    };

    struct CachedRealization {
        // Holding the key geometry prevents COM pointer reuse from aliasing a
        // stale cache key after the outline LRU releases its own reference.
        Microsoft::WRL::ComPtr<ID2D1Geometry> keyGeometry;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> realization;
        std::uint64_t lastUse = 0;
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

    // 稳态 glow 层的「模糊结果」缓存（2026-10 预览帧率优化）。稳态行
    // （无 Wiping 字、无入退场淡变、无逐字动画）每帧的 glow 源重绘与
    // 高斯模糊效果执行和上一帧逐位相同：utopia/缩放变换在合成阶段施加，
    // 源与模糊结果跟变换无关。命中时跳过源绘制与 blur 效果图，直接
    // DrawBitmap 展平结果。烘焙帧同样画展平位图（2026-10 统一）：缓存
    // 冷热不改变任何一帧的画面——多 worker 预览各持一份缓存，各自独立
    // 的 miss→hit 翻转不得引入跨 worker 可见的像素差。条目随 configure
    // 整体失效（lines 向量清空重建，行指针即行身份）；LRU 上限防止显存
    // 膨胀。
    struct GlowBlurCacheEntry {
        const CachedLine *line = nullptr;
        int styleIndex = -1;
        bool after = false;
        std::uint64_t contentSignature = 0;
        float radius = 0.0f;
        int passes = 0;
        D2D1_RECT_F sourceRect{};
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> blurred;
        std::uint64_t lastUsed = 0;
    };
    std::vector<GlowBlurCacheEntry> glowBlurCache;
    std::uint64_t glowBlurCacheSerial = 0;
    std::uint64_t glowBlurCacheHits = 0;
    std::uint64_t glowBlurCacheMisses = 0;
    static constexpr std::size_t glowBlurCacheCapacity = 8;
    // 默认开启（2026-10 用户目视验收通过）。历史口径：烘焙帧逐 pass
    // DrawImage、命中帧 DrawBitmap，两条路径在 clearRect 裁剪边界处差
    // 光晕尾部 1-3 单位（逐帧 ~120px@1280 / ~44Kpx@2560，肉眼不可辨）；
    // 2026-10 起烘焙帧与命中帧统一为 DrawBitmap，该差异不再出现在帧间。
    // KROK_SUBTITLE_GPU_GLOW_CACHE=0 可关闭回退（全程逐 pass）。
    bool glowBlurCacheEnabled = direct2d::environmentFlagEnabled(
        "KROK_SUBTITLE_GPU_GLOW_CACHE",
        true
    );
    // 诊断模式：2 = 只烘焙不命中（隔离烘焙副作用）；3 = 只命中不烘焙
    // （仅供已填充的缓存，诊断用）；4 = 现烘现用不跨帧存储（隔离跨帧
    // 过期变量）。默认 1 = 完整缓存（烘焙帧与命中帧同一像素路径）。
    int glowBlurCacheMode = 1;

    RenderScene scene;
    std::vector<CachedLine> lines;
    std::vector<CachedImage> images;
    std::uint64_t imageUseSerial = 0;
    static constexpr std::size_t defaultImageCapacity = 64;
    std::size_t imageCapacity = direct2d::environmentSize(
        "KROK_GPU_IMAGE_CACHE_CAPACITY",
        defaultImageCapacity,
        1,
        1024
    );
    std::vector<CachedBrush> brushes;
    std::uint64_t brushUseSerial = 0;
    static constexpr std::size_t brushCapacity = 512;
    std::map<FontFaceKey, Microsoft::WRL::ComPtr<IDWriteFontFace>> fontFaces;
    // Vertical-metrics faces (default instance / unsimulated) parallel to
    // ``fontFaces``; see resolveFontFaces in d2d_font_fallback.cpp.
    std::map<FontFaceKey, Microsoft::WRL::ComPtr<IDWriteFontFace>> metricFaces;
    std::vector<Microsoft::WRL::ComPtr<IDWriteFontFace>> fallbackFaces;
    std::map<TextGlyphKey, GlyphGeometryResource> textGlyphResources;
    std::map<VectorGlyphKey, GlyphGeometryResource> vectorGlyphResources;
    std::uint64_t glyphGeometryUseSerial = 0;
    static constexpr std::size_t defaultGlyphGeometryCapacity = 1024;
    std::size_t glyphGeometryCapacity = direct2d::environmentSize(
        "KROK_GPU_GLYPH_GEOMETRY_CAPACITY",
        defaultGlyphGeometryCapacity,
        1,
        16384
    );
    static constexpr std::size_t defaultVectorGlyphCapacity = 256;
    std::size_t vectorGlyphCapacity = direct2d::environmentSize(
        "KROK_GPU_VECTOR_GLYPH_CAPACITY",
        defaultVectorGlyphCapacity,
        1,
        4096
    );
    Microsoft::WRL::ComPtr<ID2D1DeviceContext1> realizationContext;
    std::uint64_t realizationCount = 0;
    std::uint64_t realizationGeneration = 0;
    std::map<RealizationCacheKey, CachedRealization> realizationResources;
    std::uint64_t realizationResourceUseSerial = 0;
    static constexpr std::size_t defaultRealizationCapacity = 8192;
    static constexpr float realizationStrokeThreshold = 8.0f;
    std::shared_ptr<RealizationControl> realizationControl;
    std::thread realizationThread;
    std::vector<RetiredRealizationWorker> retiredRealizationWorkers;
    std::atomic<bool> realizationPrewarmComplete{true};
    // realization「全池同步门」（2026-10 G5 多 worker 抖动修复 b 方案）：
    // 池在每次提交渲染任务前按「所有在岗 worker 的预热是否完成」刷新，渲染
    // 侧据此决定整帧走网格还是原路径。默认 true = 非池路径（G6 直画、无池
    // 渲染）保持旧行为（仅按本 backend 预热完成放行）；池路径由 submit 前的
    // 刷新覆盖，未刷新前不会有任务进队。
    std::atomic<bool> realizationPoolReady{true};
    std::atomic<bool> renderActive{false};
    std::atomic<bool> firstFrameCompleted{false};
    std::atomic<std::int64_t> lastRenderCompletedMs{0};
    // 自适应预热调度的反馈信号（render 线程写、预热线程读）：
    // frameRenderMsEma = 最近帧渲染耗时指数均值（ms，α=0.2）；
    // lastRenderedTimeMs = 最近渲染的项目时间（判可见行，压力状态下
    // 可见行的任务优先烘烤）。见 runRealizationPrewarm 的调度注释。
    std::atomic<float> frameRenderMsEma{0.0f};
    std::atomic<std::int64_t> lastRenderedTimeMs{0};
    mutable std::mutex realizationMutex;
    BackendDiagnostics diagnostics;
    bool configured = false;
    int frameSurfaceWidth = 0;
    int frameSurfaceHeight = 0;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> frameTargetTexture;
    Microsoft::WRL::ComPtr<ID2D1Bitmap1> frameTargetBitmap;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> frameStagingTexture;
    // G6 直画帧仓（2026-10 用户拍板与 G5 帧缓存同口径 24+1+1 = 25 槽，
    // 60fps 语义 ~417ms 窗口）：
    // direct 渲染完成后按 (generation, tMs) 登记进仓，present 按同一身份
    // 取槽上屏——present 的像素永远属于它宣称的时刻，「到点才播放」第一
    // 次在运输层成立。此前单纹理下渲染 N+k 会覆盖 N 的像素，而调度器
    // 仍按 N 的到点记账出队，慢机上表现为字幕回退/超前。
    // 失效三类：configure 改场景或改色（登记作废；尺寸变化连纹理释放）、
    // 渲染/present 遇到更新代际（旧代整仓释放纹理）、present 成功后
    // 同代更早的帧按时间丢弃（登记清空、纹理留作复用）。槽懒分配：稳态
    // 只占实际在飞深度，25 是上界不是常态。env KROK_SUBTITLE_GPU_FRAME_STORE。
    struct FrameStoreSlot {
        bool realizationPathReady = false;
        Microsoft::WRL::ComPtr<ID3D11Texture2D> texture;
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
        int generation = -1;
        std::int64_t tMs = -1;  // -1 = 未登记（空闲 / 已消费 / 待渲染）
        std::uint64_t lastUse = 0;
    };
    std::vector<FrameStoreSlot> frameStore;
    std::size_t frameStoreCapacity = direct2d::environmentSize(
        "KROK_SUBTITLE_GPU_FRAME_STORE", 25, 1, 64
    );
    std::uint64_t frameStoreUseSerial = 0;
    // Persistent glow scratch targets and GaussianBlur effects. Dirty-rect
    // mode grows each scratch slot only to its largest requested region;
    // entries rewind per line after the composite is flushed.
    std::vector<GlowScratch> glowScratchPool;
    std::vector<Microsoft::WRL::ComPtr<ID2D1Effect>> glowEffectPool;
    std::size_t glowScratchInUse = 0;
    std::size_t glowEffectInUse = 0;
    // Bitmap-guide decor (N3 shadow/glow) renders through its own ColorMatrix
    // + GaussianBlur effects. They cannot share the text-glow pools: those
    // entries may still be pending composite when a bitmap guide is drawn,
    // and re-binding their inputs would corrupt the pending text glow.
    std::vector<Microsoft::WRL::ComPtr<ID2D1Effect>> decorTintEffectPool;
    std::vector<Microsoft::WRL::ComPtr<ID2D1Effect>> decorBlurEffectPool;
    std::size_t decorTintEffectInUse = 0;
    std::size_t decorBlurEffectInUse = 0;
    std::vector<Microsoft::WRL::ComPtr<ID2D1Effect>> decorCompositeEffectPool;
    std::size_t decorCompositeEffectInUse = 0;
    // Gradient decor paints are rasterized on the CPU (avatar-native size,
    // sampled through the line fill bounds) so the glow pipeline can consume
    // them as a plain bitmap input of the Composite effect. Keyed by the full
    // geometric context; small LRU because gradient avatars are rare.
    struct CachedDecorGradient {
        PaintStyle paint;
        D2D1_RECT_F fillBounds;
        D2D1_RECT_F bitmapRect;
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
        std::uint64_t lastUse = 0;
    };
    std::vector<CachedDecorGradient> decorGradientCache;
    std::uint64_t decorGradientUseSerial = 0;
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
