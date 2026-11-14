#pragma once

#include "glyph_resources.h"
#include "particle_types.h"
#include <memory>
#include <optional>

namespace krok::subtitle::native {

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
    // Python-resolved missing-glyph fallback family for this cell (Qt's
    // measured choice, so the GPU draws the same font as the CPU renderer).
    // Empty = the requested face covers the cell or no decision was shipped.
    // Kept last so positional aggregate initialization stays valid.
    std::wstring fallbackFamily;
    FontInstanceHint fallbackHint;
    bool operator==(const TextChar &other) const {
        const auto sameGlyph =
            (vectorGlyph == other.vectorGlyph)
            || (vectorGlyph && other.vectorGlyph && *vectorGlyph == *other.vectorGlyph);
        return text == other.text
            && startMs == other.startMs
            && endMs == other.endMs
            && styleIndex == other.styleIndex
            && fallbackFamily == other.fallbackFamily
            && fallbackHint == other.fallbackHint
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
    bool lower = false;
    std::optional<float> lowerBaselineOffsetY;
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
    std::optional<float> annotationBaselineY;
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

// Python-resolved font instance for one style slot (single authority).
//
// engine/text/weight_resolver.py resolves (family, weight, italic) into an
// explicit decision -- target face weight plus whether the engine synthesises
// bold/oblique -- and native/protocol.py ships it as ``*_font_resolved``.
// The GPU executes the decision instead of matching on its own, so both
// backends draw the same face.  ``present`` false keeps the engine-mirrored
// rules (old IR / slots Python resolved trivially).

}  // namespace krok::subtitle::native
