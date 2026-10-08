#pragma once

#include <cstdint>
#include <memory>
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
    bool supportsNativePreview = false;
};

struct NativePreviewTarget {
    std::uintptr_t parentWindow = 0;
    // 子窗口在父窗口客户区的物理像素矩形（可能小于渲染纹理：场景映射
    // 矩形被视口裁剪）。srcX/srcY 是窗口左上角在纹理里的物理像素偏移，
    // present 时从该偏移 1:1 拷贝窗口大小的区域。
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;
    int srcX = 0;
    int srcY = 0;
};

// G6 拆分路径第一步：只渲染到纹理（不上屏），Python 侧到点后调
// gpu_present_rendered 上屏——「渲染可提前，播放必须到点」。
struct NativeRenderOnlyResult {
    double renderMs = 0.0;
};

struct NativePreviewResult {
    double renderMs = 0.0;
    double presentMs = 0.0;
    std::uintptr_t childWindow = 0;
    // 帧仓未命中（present 声称的 (generation, tMs) 不在仓里）：本次不上屏，
    // 调用方按丢帧处理。仓命中前绝不把「别的时刻的像素」端出去——G6 单
    // 纹理时代 present 无视 t_ms 直接拷最新渲染结果，是慢机预览回退的
    // 根因（2026-10）。
    bool dropped = false;
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

struct VectorPathCommand {
    char kind = 'M';
    std::vector<float> values;
    bool operator==(const VectorPathCommand &) const = default;
};

struct VectorGlyph {
    std::vector<VectorPathCommand> commands;
    float unitsPerEm = 1000.0f;
    float advanceWidth = 1000.0f;
    // Parsed content fingerprint used only for backend resource identity.  It
    // is deliberately excluded from render equality: commands and metrics are
    // the rendering semantics, while the fingerprint is their derived key.
    std::string resourceKey;
    bool operator==(const VectorGlyph &other) const {
        return commands == other.commands
            && unitsPerEm == other.unitsPerEm
            && advanceWidth == other.advanceWidth;
    }
};

struct BitmapGuide {
    std::wstring beforePath;
    std::wstring afterPath;
    float zoomPercent = 100.0f;
    bool fixSize = false;
    bool noDecor = false;
    bool forceWipeDecor = false;
    float marginLeft = 0.0f;
    float marginRight = 0.0f;
    float marginBottom = 0.0f;
    std::uint64_t beforeModifiedMs = 0;
    std::uint64_t beforeSize = 0;
    std::uint64_t afterModifiedMs = 0;
    std::uint64_t afterSize = 0;
    // 动图（GIF）循环锚点：行显示窗口起点（Python 侧单一事实源写入 IR，
    // 投影时补 sourceTimingOffset）。渲染帧时间减锚点后按累积延时表选帧。
    int animAnchorMs = 0;
    bool operator==(const BitmapGuide &) const = default;
};

struct WipePoint {
    int timeMs = 0;
    float position = 0.0f;
    bool operator==(const WipePoint &) const = default;
};

struct TextChar {
    std::wstring text;
    int startMs = 0;
    int endMs = 0;
    int styleIndex = -1;
    // Shared outline deduplicated per render IR (schema 2): every inline guide
    // glyph references the same immutable VectorGlyph, so per-glyph D2D geometry
    // can be cached and reused across all characters that reference it.
    std::shared_ptr<const VectorGlyph> vectorGlyph;
    std::optional<BitmapGuide> bitmapGuide;
    std::vector<WipePoint> wipePoints;
    bool operator==(const TextChar &other) const {
        const auto sameGlyph =
            (vectorGlyph == other.vectorGlyph)
            || (vectorGlyph && other.vectorGlyph && *vectorGlyph == *other.vectorGlyph);
        return text == other.text
            && startMs == other.startMs
            && endMs == other.endMs
            && styleIndex == other.styleIndex
            && sameGlyph
            && bitmapGuide == other.bitmapGuide
            && wipePoints == other.wipePoints;
    }
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
    int fadeInMs = -1;
    int fadeOutMs = -1;
    bool operator==(const DisplayWindow &) const = default;
};

/// 2026-09 装饰粒子（星光爆散 / 涟漪光环 / 唱字闪烁·音符）：Python 侧
/// plan_line_bursts 规划后随行 IR 下发；锚点坐标由各后端按自身布局解析，
/// 轨迹在渲染时按 seed 确定性求值（与 Python particles.py 镜像）。
struct ParticleBurst {
    std::string kind;  // sparkle / ripple / twinkle / twinkle_classic / note / assemble / dissolve
    std::string anchor;  // "line" | "char"
    int charIndex = -1;
    int startMs = 0;
    int endMs = 0;
    int count = 0;
    std::uint32_t seed = 0;
    float sizePx = 28.0f;
    float travelPx = 120.0f;
    bool front = true;
    bool reverse = false;
    // sparkle 整句扫过方向：+1 入场（左→右）、-1 退场（右→左）、0 不扫。
    int sweep = 0;
    RgbaColor color{255, 255, 255, 255};
    // 2026-10 颜色模式装饰规格（跟随字体/复用配色方案）：hasPaint=false
    // 时按 ``color`` 实心绘制（旧 IR / 单独颜色档）。宽度为物理 px（投影
    // 时随 scale 同比缩放，与 sizePx 同基准）；涟漪的非横向渐变由渲染端
    // 做径向映射（每环按扩散进度在渐变轴上采样实心色：内圈新环=起点
    // 色、外圈老环=终点色）。
    bool hasPaint = false;
    PaintStyle fill;
    PaintStyle stroke;
    PaintStyle stroke2;
    float strokeWidth = 0.0f;
    float stroke2Width = 0.0f;
    // 取色层级「+装饰/全有」的装饰层（2026-10）：kind 由来源角色方案的
    // decoration_kind 选定——"shadow" 用 decor 色画行空间常量偏移的整剪影
    // （偏移 decorOffsetX/Y，px，与 strokeWidth 同基准）；"glow" 用多级
    // 描边近似弥散晕（半径 decorRadius px + decorConcentration 档，与
    // kGlowHaloStrokes 配方同源）。fill 为装饰色（配色态 shadow 槽）。
    bool hasDecor = false;
    std::string decorKind;
    PaintStyle decor;
    float decorOffsetX = 0.0f;
    float decorOffsetY = 0.0f;
    float decorRadius = 0.0f;
    int decorConcentration = 0;
    // 行锚点星光的逐字实色表（#RRGGBB；空 = 不启用）。
    std::vector<RgbaColor> charColors;
    bool operator==(const ParticleBurst &) const = default;
};

struct PlacementWindow {
    int startMs = 0;
    int endMs = 0;
    float offsetX = 0.0f;
    float offsetY = 0.0f;
    bool operator==(const PlacementWindow &) const = default;
};

/// Composite slot of the title overlay.  Lower draws first, so the title sits
/// below every lyric source (which use their own 0-based source index).
inline constexpr int kTitleCompositeOrder = -1;

struct TextLine {
    std::vector<TextChar> chars;
    std::vector<TextRuby> rubies;
    int startMs = 0;
    int endMs = 0;
    int sourceIndex = 0;
    int sourceLineIndex = 0;
    int pageIndex = -1;
    int lane = 0;
    // Sayatoo signal lamps (every lit style) attach only to each section's
    // first page's first line; the painter stamps this flag in the render IR.
    bool signalHead = false;
    // 分模块宿主旗标（协议注释见 render_config.h）：柱组/形状灯各自门控。
    bool volumeHead = false;
    bool litHead = false;
    // 「真一组」渐变带正文侧拓宽闸门（协议注释见 render_config.h）。
    bool signalBandJoin = false;
    // Python 在源加载入口已把整行时间戳严格逆序的行镜像理顺为顺序，仅保留
    // 本标记让走字反向（横排 rtl 翻转 / 竖排自下而上），与 Painter 同口径。
    bool wipeReverse = false;
    int compositeOrder = 0;
    bool centerOverride = false;
    bool staticOverlay = false;
    int fadeInMs = 0;
    int fadeOutMs = 0;
    std::string entryAnimation = "none";
    int entryDurationMs = 0;
    std::string exitAnimation = "none";
    int exitDurationMs = 0;
    std::string karaokeAnimation = "none";
    // 扫字线叠加开关：Python 按该行烘焙后的 karaoke_anim 显式档位打标；
    // 缺省 false 兼容旧 IR。参数在 TextStyle（随行样式一起下发）。
    bool scanlineEnabled = false;
    // 整字放大（zoom_pulse）开关：本体 karaokeAnimation 仍是降维后的
    // "utopia"，靠这个行级标记切换缩放曲线并把缩放原点换成字符中心。
    bool zoomPulseEnabled = false;
    // 唱字描边闪光：与唱字档位正交；时间锚 = 字符唱字起点，240ms 白色脉冲。
    bool strokeFlashEnabled = false;
    // 装饰粒子（见 ParticleBurst）；空表兼容旧 IR。
    std::vector<ParticleBurst> bursts;
    std::vector<DisplayWindow> displayWindows;
    std::vector<PlacementWindow> placementWindows;
    bool operator==(const TextLine &) const = default;
};

struct TextStyle {
    std::string layoutSemantics = "legacy";
    std::string smartHorizontal = "none";
    std::wstring fontFamily;
    std::optional<std::wstring> latinFontFamily;
    float fontSize = 100.0f;
    std::optional<float> latinFontSize;
    int fontWeight = 400;
    // 可变字体标记（顺应引擎）：字重回到绝对字重，模拟加粗交还引擎，
    // 该标记仅指示渲染端是否走轴值实例（而非静态就近匹配）。
    bool fontAxis = false;
    std::optional<int> latinFontWeight;
    bool latinFontAxis = false;
    int latinFontStretchPct = 100;
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
    float layoutOffsetX = 0.0f;
    float layoutOffsetY = 0.0f;
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
    // Script-effective strokes for alnum characters (latin overrides with the
    // Japanese-track fallback already applied); the projection materializes
    // per-script style variants from them, mirroring the CPU painter's
    // main_script_stroke_style.
    float latinStrokeWidth = 0.0f;
    float latinStroke2Width = 0.0f;
    std::string decorationKind = "none";
    float glowBeforeRadius = 10.0f;
    float glowAfterRadius = 10.0f;
    int glowConcentrationLevel = 0;
    float shadowOffsetX = 5.0f;
    float shadowOffsetY = 5.0f;
    std::wstring rubyFontFamily;
    std::optional<std::wstring> rubyLatinFontFamily;
    float rubyFontSize = 45.0f;
    std::optional<float> rubyLatinFontSize;
    int rubyFontWeight = 400;
    bool rubyFontAxis = false;
    std::optional<int> rubyLatinFontWeight;
    bool rubyLatinFontAxis = false;
    int rubyLatinFontStretchPct = 100;
    float rubyGap = 0.0f;
    float rubyInterval = 0.0f;
    std::string rubyAlignment = "auto";
    std::string rubyMainProgressMode = "checkpoint_segments";
    bool rubyHorizontalGradientWithMain = true;
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
    // Script-effective ruby strokes for alnum readings (ruby-latin overrides
    // with the ruby Japanese-track fallback already applied); consumers pick
    // between the two pairs by the reading's script, mirroring the CPU
    // painter's ruby_script_stroke_style.
    float rubyLatinStrokeWidth = 0.0f;
    float rubyLatinStroke2Width = 0.0f;
    std::string rubyDecorationKind = "none";
    float rubyGlowBeforeRadius = 0.0f;
    float rubyGlowAfterRadius = 0.0f;
    int rubyGlowConcentrationLevel = 0;
    float rubyShadowOffsetX = 0.0f;
    float rubyShadowOffsetY = 1.0f;
    bool litEnabled = false;
    std::string litStyle = "circle";
    // auto 外观模式：矢量灯走主文字装饰管线（全程取走字后配色、含
    // 二重描边/发光/阴影，且不跟随行入退场动画），镜像 Painter 的
    // _draw_lit_decorated_group；大小/颜色已由 IR 物化成数值。
    std::string litAppearanceMode = "custom";
    int litNumber = 4;
    float litSize = 45.0f;
    float litOffsetX = 0.0f;
    float litOffsetY = -24.0f;
    float litTracking = 0.0f;
    // 图片模式素材：路径 + (mtime, size) 失效签名（与 PaintStyle.image*
    // 同口径，签名在 gpuSceneFromConfig 里用 QFileInfo 探测）。
    std::wstring litImagePath;
    std::uint64_t litImageModifiedMs = 0;
    std::uint64_t litImageSize = 0;
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
    bool volumeEnabled = false;
    // auto 外观模式：柱体走主文字装饰管线（填充/渐变、描边/二重描边、
    // 发光/阴影、整字放大），镜像 Painter 的 _draw_volume_decorated_group。
    std::string volumeAppearanceMode = "auto";
    int volumeDurationMs = 4000;
    int volumeWaitingTimeMs = 0;
    int volumeTimeOffsetMs = 0;
    float volumeStrokeWidth = 2.0f;
    float volumeOpacity = 1.0f;
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
    // Karaoke scan-line highlight (Sayatoo-style front sweep) parameters.
    // Whether the overlay is active is a per-line flag (TextLine), resolved by
    // the Python host from the line's baked karaoke_anim.
    // ``scanlineMode``: "color" fills the band with ``scanlineColor``;
    // "brighten" keeps each before/after colour's HSV hue and saturation,
    // raising only its value by ``scanlineBrightness``; "role" fills the whole
    // band with ``scanlineRolePaint`` (a role's after-state text fill). The
    // parser normalises a dangling role name back to "color", so render never
    // sees "role" without a resolved paint.
    float scanlineWidth = 16.0f;
    std::string scanlineMode = "color";
    RgbaColor scanlineColor{255, 255, 255, 255};
    PaintStyle scanlineRolePaint;
    float scanlineBrightness = 0.6f;
    float scanlineGlowRadius = 8.0f;
    // Whole-char zoom pulse easing order (0..5; 0 = linear).  Whether the
    // effect is active is a per-line flag (TextLine::zoomPulseEnabled).
    int zoomPulseCurveLevel = 1;
    bool operator==(const TextStyle &) const = default;
};

struct RenderScene {
    int width = 1920;
    int height = 1080;
    // Raster scale used by preview targets.  Layout keeps the output-resolution
    // integer semantics and scales the finished metrics by this factor; export
    // remains on the existing 1.0 path.
    float layoutReferenceScale = 1.0f;
    int exportCropTop = 0;
    int exportCropHeight = 0;
    std::vector<std::pair<int, int>> exportBands;
    int prewarmTimeMs = 0;
    bool realizationEnabled = true;
    bool deferRealizationPrewarmUntilFirstFrame = false;
    std::uint64_t realizationCapacity = 8192;
    float viewportScale = 1.0f;
    float viewportRotation = 0.0f;
    float viewportOffsetX = 0.0f;
    float viewportOffsetY = 0.0f;
    std::string viewportAlign = "center";
    TextStyle style;
    std::vector<TextStyle> lineStyles;
    std::vector<TextStyle> charStyles;
    // 指示灯/音量柱 ``role`` 外观档（复用配色方案）的固定装饰源：方案
    // 叠加到全局 base 后投影出的完整 TextStyle（镜像 Painter 的
    // appearance_role_source）。仅在对应 appearanceMode == "role" 且来源
    // 名可解析时存在；悬空时 nullopt，渲染端回退 auto 口径（段首行第一
    // 个角色）。
    std::optional<TextStyle> litDecorStyle;
    std::optional<TextStyle> volumeDecorStyle;
    std::vector<TextLine> lines;
    // 装饰粒子 sprite 轮廓表（scene IR ``fx_sprites``；Python 单一事实源，
    // 常量内容、值语义参与相等性比较）。
    std::vector<std::pair<std::string, VectorGlyph>> fxSprites;
    bool operator==(const RenderScene &) const = default;
};

struct BackendDiagnostics {
    std::uint64_t cacheHits = 0;
    std::uint64_t cacheMisses = 0;
    std::uint64_t estimatedCacheBytes = 0;
    std::uint64_t lineCount = 0;
    std::uint64_t charCount = 0;
    std::uint64_t geometryCount = 0;
    std::uint64_t glyphGeometryCacheHits = 0;
    std::uint64_t glyphGeometryCacheMisses = 0;
    std::uint64_t glyphGeometryCacheSize = 0;
    std::uint64_t glyphGeometryCacheEvictions = 0;
    std::uint64_t glyphGeometryCacheCapacity = 0;
    std::uint64_t glyphStrokeCacheHits = 0;
    std::uint64_t glyphStrokeCacheMisses = 0;
    double glyphGeometryBuildMs = 0.0;
    double glyphStrokeBuildMs = 0.0;
    std::uint64_t vectorGlyphCacheHits = 0;
    std::uint64_t vectorGlyphCacheMisses = 0;
    std::uint64_t vectorGlyphCacheSize = 0;
    std::uint64_t vectorGlyphCacheEvictions = 0;
    std::uint64_t vectorGlyphCacheCapacity = 0;
    double vectorGlyphBuildMs = 0.0;
    std::uint64_t imageCacheHits = 0;
    std::uint64_t imageCacheMisses = 0;
    std::uint64_t imageCacheSize = 0;
    std::uint64_t imageCacheEvictions = 0;
    std::uint64_t imageCacheCapacity = 0;
    double imageBuildMs = 0.0;
    std::uint64_t rubyCount = 0;
    std::uint64_t styleCount = 0;
    bool videoMemoryInfoAvailable = false;
    std::uint64_t localVideoMemoryUsageBytes = 0;
    std::uint64_t localVideoMemoryBudgetBytes = 0;
    std::uint64_t nonLocalVideoMemoryUsageBytes = 0;
    std::uint64_t nonLocalVideoMemoryBudgetBytes = 0;
    bool countersEnabled = true;
    std::uint64_t framesRendered = 0;
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
    double gpuWaitMs = 0.0;
    double readbackCopyMs = 0.0;
    bool resourceCacheEnabled = true;
    std::uint64_t brushCacheHits = 0;
    std::uint64_t brushCacheMisses = 0;
    std::uint64_t brushCacheEvictions = 0;
    std::uint64_t brushCacheInvalidations = 0;
    std::uint64_t brushCacheSize = 0;
    std::uint64_t brushCacheCapacity = 0;
    bool realizationEnabled = true;
    bool realizationSupported = false;
    bool realizationPrewarmComplete = true;
    std::uint64_t realizationCount = 0;
    std::uint64_t realizationCapacity = 0;
    std::uint64_t realizationPrewarmTasks = 0;
    std::uint64_t realizationPrewarmSkipped = 0;
    double realizationPrewarmMs = 0.0;
    std::uint64_t realizationPrewarmFillTasks = 0;
    std::uint64_t realizationPrewarmStrokeTasks = 0;
    double realizationPrewarmContextMs = 0.0;
    double realizationPrewarmWaitMs = 0.0;
    double realizationPrewarmFillCreateMs = 0.0;
    double realizationPrewarmStrokeCreateMs = 0.0;
    double realizationPrewarmPublishMs = 0.0;
    double realizationPrewarmCreateP50Ms = 0.0;
    double realizationPrewarmCreateP95Ms = 0.0;
    double realizationPrewarmCreateMaxMs = 0.0;
    bool glowDirtyRectEnabled = true;
};

class BackendError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

}  // namespace krok::subtitle::native
