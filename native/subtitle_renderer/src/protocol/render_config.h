#pragma once

#include "../model/render_types.h"

#include <QtCore/QHash>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtCore/QString>

#include <algorithm>
#include <cmath>
#include <memory>
#include <optional>
#include <utility>
#include <vector>

namespace krok::subtitle::native::protocol {

struct TimingChar {
    QString text;
    int startMs = 0;
    std::optional<int> resolvedEndMs;
    std::optional<int> pauseReleaseMs;
    bool explicitStart = false;
    bool explicitEnd = false;
    QString roleLabel;
    // Python-resolved missing-glyph fallback family for this cell (Qt's
    // measured choice); empty = requested face covers the cell.
    QString fallbackFamily;
    FontInstanceHint fallbackHint;
    // Shared pointer into RenderConfig::vectorGlyphs (schema 2 dedup table).
    std::shared_ptr<const krok::subtitle::native::VectorGlyph> vectorGlyph;
    std::optional<krok::subtitle::native::BitmapGuide> bitmapGuide;
};

struct ResolvedLineLayout {
    bool present = false;
    QString lineYPosition = QStringLiteral("bottom");
    int lineYMarginPx = 80;
    int lineGapPx = 90;
    QString smartHorizontal = QStringLiteral("equal_margins");
    int horizontalMarginPx = 50;
    std::vector<QString> lineAlignments{QStringLiteral("left"), QStringLiteral("right")};
    std::vector<int> lineOffsetsX;
    bool dualLineLayout = true;
    QString lineHorizontalLayout = QStringLiteral("asymmetric");
    QString row1Align = QStringLiteral("left");
    int row1OffsetX = 50;
    int row1OffsetY = 0;
    QString row2Align = QStringLiteral("right");
    int row2OffsetX = -50;
    int row2OffsetY = 0;
    int letterSpacingPx = 0;
    int spaceWidthPercent = 20;
    bool allowBiting = false;
    int rubyIntervalPx = 0;
    QString rubyAlignment = QStringLiteral("auto");
    int rubyGapPx = 0;
};

struct PaintFillSpec {
    QString mode = QStringLiteral("solid");
    QString color = QStringLiteral("#FFFFFF");
    QString startColor = QStringLiteral("#FFFFFF");
    QString endColor = QStringLiteral("#FFFFFF");
    std::vector<std::pair<double, QString>> gradientStops;
    QString splitTopColor = QStringLiteral("#FFFFFF");
    QString splitBottomColor = QStringLiteral("#FFFFFF");
    int splitPositionPct = 50;
    std::vector<std::pair<double, QString>> splitStops;
    QString imagePath;
    int imageScalePct = 100;
};

struct TimingLine {
    std::vector<TimingChar> chars;
    int endMs = 0;
    QString singerLabel;
    int singerId = -1;
    int sourceIndex = 0;
    int sourceLineIndex = 0;
    // Loader-stamped identity from TimingLine.track_line_index; -1 = unknown.
    int trackLineIndex = -1;
    int pageIndex = -1;
    int pageLineCount = 0;
    // Sayatoo signal lamps (every lit style) attach only to each section's
    // first page's first line; Python stamps the flag so both backends share
    // one decision. Absent field parses as true to keep legacy per-line IRs.
    bool signalHead = false;
    // Per-module host flags (Python 特效行开关 / volume_head_override &
    // lit_head_override): the bar group and the shape lamps can be forced
    // on/off per line, so each side gates on its own flag. Absent field
    // parses as the signalHead value to keep legacy per-line IRs identical.
    bool volumeHead = false;
    bool litHead = false;
    // 「真一组」渐变带正文侧拓宽闸门：音量柱 auto/role 且装饰源与正文
    // 第一角色同源（auto 或 role 悬空回退）+ 段首行 + 非 RTL。configure
    // 据此把第一角色（及 ruby 共享盒）的横向渐变跨度左缘拓宽到柱组左
    // 缘——柱体与正文共用同一条渐变带（镜像 Painter 的
    // LineLayout.signal_band_left）。缺省 false 兼容旧 IR。
    bool signalBandJoin = false;
    // Python 在源加载入口已把整行时间戳严格逆序的行镜像理顺为顺序，仅保留
    // 本标记让走字反向（横排 rtl 翻转 / 竖排自下而上），与 Painter 同口径。
    bool wipeReverse = false;
    int sourceOffsetMs = 0;
    int lane = 0;
    double layoutOffsetX = 0.0;
    double layoutOffsetY = 0.0;
    std::vector<krok::subtitle::native::PlacementWindow> placementWindows;
    std::optional<int> displayStartMs;
    std::optional<int> displayEndMs;
    bool centerOverride = false;
    QString entryAnimation = QStringLiteral("none");
    int entryDurationMs = 0;
    QString exitAnimation = QStringLiteral("none");
    int exitDurationMs = 0;
    QString karaokeAnimation = QStringLiteral("none");
    // 扫字线叠加开关：Python 按该行烘焙后的 karaoke_anim 显式档位打标；
    // 缺省 false 兼容旧 IR。参数在 ResolvedStyle（全局样式）。
    bool scanlineEnabled = false;
    // 整字放大（zoom_pulse）开关：本体 karaokeAnimation 仍是降维后的
    // "utopia"，靠这个行级标记切换缩放曲线（唱字期间缓出放大、唱字结束
    // 后 300ms 缓入缩回）并把缩放原点换成字符中心。缺省 false 兼容旧 IR。
    bool zoomPulseEnabled = false;
    // 唱字描边闪光开关（与唱字档位正交）；缺省 false 兼容旧 IR。
    bool strokeFlashEnabled = false;
    // 装饰粒子（Python plan_line_bursts 规划；空表兼容旧 IR）。
    struct FxBurst {
        QString kind;
        // 可选 sprite 覆盖（火形/雷形预设）；空 = 按 kind 取默认。
        QString sprite;
        QString anchor;
        int charIndex = -1;
        int startMs = 0;
        int endMs = 0;
        int count = 0;
        quint32 seed = 0;
        double sizePx = 28.0;
        double travelPx = 120.0;
        // 唱字火焰按簇横向扫过的发射点偏移（归一化 × 字框宽；旧 IR 缺省 0）。
        double emitX = 0.0;
        // 扫字线特效（kind="scanline"）的锋面发射窗口（ms）：粒子出生时刻
        // 换算成锋面横向进度；窗口尾部剩余部分是粒子寿命。0 = 整个窗口
        // 都是发射窗（旧 IR / 其余 kind 缺省，行为不变）。
        int emitMs = 0;
        bool front = true;
        bool reverse = false;
        int sweep = 0;
        QString color = QStringLiteral("#FFFFFF");
        // 2026-10 颜色模式（跟随字体/复用配色方案）：完整装饰规格——
        // 填充/描边/二重描边的 PaintFill + 已按 粒子尺寸/来源字号 缩放的
        // 描边宽（px，与 sizePx 同基准）。旧 IR 缺省 hasPaint=false，按
        // ``color`` 实心绘制（旧 sidecar 读到未知键自动忽略，兼容互通）。
        bool hasPaint = false;
        PaintFillSpec fill;
        PaintFillSpec stroke;
        PaintFillSpec stroke2;
        double strokeWidthPx = 0.0;
        double stroke2WidthPx = 0.0;
        // 取色层级「+装饰/全有」的装饰层（2026-10）：kind 由来源角色方案的
        // decoration_kind 选定——shadow=行空间常量偏移剪影（offset_*_px）；
        // glow=多级描边弥散晕（radius_px + concentration_level）。偏移/半径
        // 均为粒子尺寸基准的物理 px（规划期已按 size/字号同比缩放）。
        bool hasDecor = false;
        QString decorKind;
        PaintFillSpec decor;
        double decorOffsetX = 0.0;
        double decorOffsetY = 0.0;
        double decorRadius = 0.0;
        int decorConcentration = 0;
        // 行锚点星光的逐字颜色表（跟随模式 + 联动入退场）：动画仍是整行
        // 一条 burst，绘制端按每颗粒子落点所在字符取色（Python 预解析，
        // 旧 IR / 其余模式缺省为空）。
        std::vector<QString> charColors;
    };
    std::vector<FxBurst> fxBursts;
    ResolvedLineLayout layout;
};

struct RubyAnnotation {
    QString kanji;
    QString reading;
    std::vector<int> readingPartMs;
    std::vector<QString> readingParts;
    int posStartMs = 0;
    int posEndMs = 0;
    int sourceIndex = 0;
    int sourceOffsetMs = 0;
    // Loader-resolved owning line plus the line-local, half-open target range;
    // -1 = unresolved, in which case the historical text search runs (see
    // rubyTargetIndices). Source ownership is always enforced, while line
    // ownership is enforced whenever both sides carry a resolved identity.
    int targetLineIndex = -1;
    int targetCharStart = -1;
    int targetCharEnd = -1;
};

struct ResolvedStyle {
    QString fontFamily = QStringLiteral("UD Digi Kyokasho N-B");
    QString fontFamilyLatin;
    int fontSizePx = 100;
    std::optional<int> latinFontSizePx;
    int fontWeight = 400;
    // 可变字体标记（顺应引擎）：字重回到绝对字重，模拟加粗交还引擎，
    // 该标记仅指示渲染端是否走轴值实例（而非静态就近匹配）。
    bool fontAxis = false;
    // Python 侧统一解析出的字体实例（``*_font_resolved``）：face 字重 +
    // 模拟加粗/倾斜 + 轴值。present=false（旧 IR / 平凡决策）时渲染端按
    // fontAxis + 引擎镜像规则自行解析。
    FontInstanceHint fontHint;
    std::optional<int> latinFontWeight;
    bool latinFontAxis = false;
    FontInstanceHint latinFontHint;
    int latinFontStretchPct = 100;
    bool italic = false;
    // Per-slot italic overrides (Style.latin_italic / ruby_italic /
    // ruby_latin_italic).  ``nullopt`` = follow the same chain as the stroke2
    // switch: latin/ruby follow the main text, ruby-latin follows the
    // resolved ruby value.  gpu_scene_projection resolves them into the
    // per-slot bools on TextStyle.
    std::optional<bool> latinItalic;
    std::optional<bool> rubyItalic;
    std::optional<bool> rubyLatinItalic;
    bool allowBiting = false;
    bool affectsRubyAnchor = true;
    int spaceWidthPercent = 20;
    int letterSpacingPx = 0;
    QString baseColor = QStringLiteral("#FFFFFF");
    QString fillColor = QStringLiteral("#FF5A6F");
    // Legacy fill gradient（Style 级 fill_gradient_* 字段）：仅在没有显式
    // karaoke_colors 矩阵时经 refreshLegacyMainFills 物化成 afterFill 渐变，
    // 与 Painter 的 legacy_after_text_fill 同口径。
    bool fillGradientEnabled = false;
    QString fillGradientStartColor = QStringLiteral("#FF5A6F");
    QString fillGradientEndColor = QStringLiteral("#0055FF");
    int fillGradientAngleDeg = 0;
    QString beforeStrokeColor = QStringLiteral("#222222");
    QString afterStrokeColor = QStringLiteral("#222222");
    QString beforeStroke2Color = QStringLiteral("#000000");
    QString afterStroke2Color = QStringLiteral("#000000");
    QString beforeShadowColor = QStringLiteral("#000000");
    QString afterShadowColor = QStringLiteral("#000000");
    PaintFillSpec baseFill;
    PaintFillSpec afterFill;
    PaintFillSpec beforeStrokeFill;
    PaintFillSpec afterStrokeFill;
    PaintFillSpec beforeStroke2Fill;
    PaintFillSpec afterStroke2Fill;
    PaintFillSpec beforeShadowFill;
    PaintFillSpec afterShadowFill;
    QString rubyColor = QStringLiteral("#FF5A6F");
    QString rubyBaseColor = QStringLiteral("#FFFFFF");
    QString rubyFillColor = QStringLiteral("#FF5A6F");
    QString rubyBeforeStrokeColor = QStringLiteral("#222222");
    QString rubyAfterStrokeColor = QStringLiteral("#222222");
    QString rubyBeforeStroke2Color = QStringLiteral("#000000");
    QString rubyAfterStroke2Color = QStringLiteral("#000000");
    QString rubyBeforeShadowColor = QStringLiteral("#000000");
    QString rubyAfterShadowColor = QStringLiteral("#000000");
    PaintFillSpec rubyBaseFill;
    PaintFillSpec rubyAfterFill;
    PaintFillSpec rubyBeforeStrokeFill;
    PaintFillSpec rubyAfterStrokeFill;
    PaintFillSpec rubyBeforeStroke2Fill;
    PaintFillSpec rubyAfterStroke2Fill;
    PaintFillSpec rubyBeforeShadowFill;
    PaintFillSpec rubyAfterShadowFill;
    int strokeWidthPx = 9;
    int stroke2WidthPx = 0;
    // Tracked separately from the width because ``stroke2WidthPx`` is zeroed as
    // soon as the flag turns off: an unset ruby stroke2 has to inherit the main
    // text's *flag*, which a zeroed width can no longer distinguish from a
    // deliberate 0 px. Defaults to true so an absent key keeps the old
    // "no key means do not zero" behaviour (and matches Style.stroke2_enabled).
    bool stroke2Enabled = true;
    // The width before the flag zeroes it. Ruby inherits the flag and the width
    // along separate chains (N3 resolves UseEdge2 and EdgeSize2 independently),
    // so switching the main text's stroke2 off must not erase the width an
    // explicitly enabled ruby still inherits.
    int stroke2RawWidthPx = 0;
    // Latin-track stroke overrides for alnum characters; unset (or a
    // non-positive width) follows the Japanese-track values above — mirroring
    // the CPU painter's main_script_stroke_style fallback chain.
    std::optional<int> latinStrokeWidthPx;
    std::optional<bool> latinStroke2Enabled;
    std::optional<int> latinStroke2WidthPx;
    QString decorationKind = QStringLiteral("shadow");
    QString decorationKindBefore;
    bool decorationBeforeSync = true;
    int glowRadiusPx = 10;
    int glowBeforeRadiusPx = 10;
    int glowAfterRadiusPx = 10;
    int glowConcentrationLevel = 0;
    std::optional<int> glowBeforeConcentrationLevel;
    int shadowOffsetX = 0;
    int shadowOffsetY = 1;
    std::optional<int> shadowBeforeOffsetX;
    std::optional<int> shadowBeforeOffsetY;
    std::optional<int> rubyShadowOffsetX;
    std::optional<int> rubyShadowOffsetY;
    std::optional<int> rubyShadowBeforeOffsetX;
    std::optional<int> rubyShadowBeforeOffsetY;
    int rubyFontSizePx = 30;
    QString rubyFontFamily;
    QString rubyFontFamilyLatin;
    std::optional<int> rubyFontWeight;
    bool rubyFontAxis = false;
    FontInstanceHint rubyFontHint;
    std::optional<int> rubyLatinFontSizePx;
    std::optional<int> rubyLatinFontWeight;
    bool rubyLatinFontAxis = false;
    FontInstanceHint rubyLatinFontHint;
    std::optional<int> rubyLatinFontStretchPct;
    bool rubyFontFollowMain = true;
    int rubyGapPx = 8;
    int rubyIntervalPx = 0;
    QString rubyAlignment = QStringLiteral("auto");
    QString rubyMainProgressMode = QStringLiteral("checkpoint_segments");
    bool rubyHorizontalGradientWithMain = true;
    // These ruby fallbacks are "unset" until a scheme sets them explicitly.
    // Empty/nullopt means "follow the effective (role) main text", resolved in
    // applyGpuResolvedStyle against the role-resolved style — mirroring the CPU
    // painter's lazy _ruby_* helpers and the existing rubyShadowOffset pattern.
    // Baking them from the *global* main during base parsing made a role that
    // overrode its main decoration/glow keep the global default for ruby.
    std::optional<int> rubyStrokeWidthPx;
    std::optional<bool> rubyStroke2Enabled;
    std::optional<int> rubyStroke2WidthPx;
    // Ruby-latin stroke overrides for alnum readings; unset (or a
    // non-positive width) follows the ruby Japanese-track values resolved
    // by the projection — mirroring the CPU painter's
    // ruby_script_stroke_style fallback chain.
    std::optional<int> rubyLatinStrokeWidthPx;
    std::optional<bool> rubyLatinStroke2Enabled;
    std::optional<int> rubyLatinStroke2WidthPx;
    QString rubyDecorationKind;
    QString rubyDecorationKindBefore;
    std::optional<int> rubyGlowBeforeRadiusPx;
    std::optional<int> rubyGlowAfterRadiusPx;
    std::optional<int> rubyGlowConcentrationLevel;
    std::optional<int> rubyGlowBeforeConcentrationLevel;
    bool litEnabled = false;
    QString litStyle = QStringLiteral("volume");
    // 指示灯 auto/role 外观模式：矢量灯走主文字装饰管线（镜像 Painter 的
    // _draw_lit_decorated_group）；大小/颜色已在 IR 侧物化成数值。``role``
    // 档装饰源改为 ``litRoleName`` 指定方案（场景投影额外产出一个 decor
    // TextStyle，见 RenderScene::litDecorStyle；名字悬空回退 auto 口径）。
    QString litAppearanceMode = QStringLiteral("custom");
    QString litRoleName;
    int litNumber = 4;
    int litSize = 32;
    int litOffsetX = 0;
    int litOffsetY = -24;
    int litTracking = 0;
    QString litImagePath;
    QString litFillColor = QStringLiteral("#0000FF");
    QString litStrokeColor = QStringLiteral("#FFFFFF");
    int litStrokeWidth = 2;
    int litStrokeSoften = 0;
    int litOpacityPct = 100;
    int litEdgeBrightnessPct = 60;
    bool litShadow = true;
    int litTimeOffsetMs = 0;
    int litWaitingTimeMs = 0;
    QString litTransitionMode = QStringLiteral("fade");
    int litTransitionRatioPct = 67;
    int litTransitionAngleDeg = 0;
    int litTransitionDistance = 0;
    int signalsDurationMs = 4000;
    bool volumeEnabled = false;
    // 音量柱外观模式：``auto``/``role`` 同走主文字装饰管线（镜像 Painter
    // 的 _draw_volume_decorated_group）；``role`` 档装饰源由
    // ``volumeRoleName`` 指定（悬空回退 auto 口径），大小/颜色已在 IR 侧
    // 物化成数值。
    QString volumeAppearanceMode = QStringLiteral("custom");
    QString volumeRoleName;
    int volumeDurationMs = 4000;
    int volumeWaitingTimeMs = 0;
    int volumeTimeOffsetMs = 0;
    int volumeStrokeWidth = 2;
    int volumeOpacityPct = 100;
    int volumeSize = 48;
    int volumeOffsetX = 0;
    int volumeOffsetY = 0;
    int volumeColumnWidth = 12;
    int volumeColumnCount = 4;
    int volumeColumnSpacing = 0;
    int volumeAlign = 1;
    double volumeRatio = 3.0;
    QString volumeFillColor = QStringLiteral("#FFFFFF");
    QString volumeStrokeColor = QStringLiteral("#0000FF");
    QString volumeOverlayFillColor = QStringLiteral("#0000FF");
    QString volumeOverlayStrokeColor = QStringLiteral("#FFFFFF");
    int volumeFlashTimes = 3;
    double volumeFlashDurationRatio = 1.0;
    int volumeTransitionRatioPct = 67;
    // 扫字线参数（模式/粗细/颜色/发光/亮度）来自全局 Style；仅显式选择扫字线
    // 档位的行会置位 TimingLine::scanlineEnabled。
    int scanlineWidthPx = 16;
    QString scanlineMode = QStringLiteral("color");
    QString scanlineColor = QStringLiteral("#FFFFFF");
    int scanlineBrightnessPct = 60;
    int scanlineGlowPx = 8;
    // 扫字线形态（"band" / "flame" / "bubble"）：烈焰棒/水泡替换普通
    // 高亮带，由渲染端生成程序化柱状图样；缺省 band 兼容旧 IR。
    QString scanlineShape = QStringLiteral("band");
    // ``role`` 模式：``scanlineRoleName`` 指向来源的「走字后-主文字」填充
    // （customStyleSchemes 解析）。整条带（含描边）都用这一份填充重绘。
    // 名字解析失败时 scanlineMode 归一回 ``color``。
    QString scanlineRoleName;
    PaintFillSpec scanlineRolePaint;
    // 整字放大缓动档位（0~5，0=线性）来自全局 Style；仅显式选择 zoom_pulse
    // 档位的行会置位 TimingLine::zoomPulseEnabled。
    int zoomPulseCurveLevel = 1;
    bool hasMainKaraokeColors = false;
    bool hasRubyKaraokeColors = false;
};

inline QString decorationKindForState(const ResolvedStyle &style, bool after) {
    if (after) {
        return style.decorationKind;
    }
    if (style.decorationBeforeSync) {
        return style.decorationKind;
    }
    return style.decorationKindBefore.isEmpty()
        ? style.decorationKind
        : style.decorationKindBefore;
}

inline int rubyGlowConcentrationForState(const ResolvedStyle &style, bool after);

inline int glowConcentrationForState(const ResolvedStyle &style, bool after) {
    if (after || style.decorationBeforeSync) {
        return style.glowConcentrationLevel;
    }
    return style.glowBeforeConcentrationLevel.value_or(
        style.glowConcentrationLevel
    );
}

inline int shadowOffsetXForState(const ResolvedStyle &style, bool after) {
    if (after || style.decorationBeforeSync) {
        return style.shadowOffsetX;
    }
    return style.shadowBeforeOffsetX.value_or(style.shadowOffsetX);
}

inline int shadowOffsetYForState(const ResolvedStyle &style, bool after) {
    if (after || style.decorationBeforeSync) {
        return style.shadowOffsetY;
    }
    return style.shadowBeforeOffsetY.value_or(style.shadowOffsetY);
}

inline int glowRadiusForState(const ResolvedStyle &style, bool after) {
    if (glowConcentrationForState(style, after) < 0) {
        return 0;
    }
    const int value = after ? style.glowAfterRadiusPx : style.glowBeforeRadiusPx;
    return std::max(value, 0);
}

inline int rubyGlowRadiusForState(const ResolvedStyle &style, bool after) {
    if (rubyGlowConcentrationForState(style, after) < 0) {
        return 0;
    }
    const std::optional<int> &value = after
        ? style.rubyGlowAfterRadiusPx
        : style.rubyGlowBeforeRadiusPx;
    if (value.has_value()) {
        return std::max(*value, 0);
    }
    return std::max(
        static_cast<int>(std::lround(
            glowRadiusForState(style, after)
            * (static_cast<double>(style.rubyFontSizePx)
               / static_cast<double>(std::max(style.fontSizePx, 1)))
        )),
        0
    );
}

inline QString rubyDecorationKindForState(const ResolvedStyle &style, bool after) {
    if (!after && style.decorationBeforeSync) {
        after = true;
    }
    QString kind;
    if (after) {
        kind = style.rubyDecorationKind;
    } else if (!style.rubyDecorationKindBefore.isEmpty()) {
        kind = style.rubyDecorationKindBefore;
    } else if (!style.rubyDecorationKind.isEmpty()) {
        kind = style.rubyDecorationKind;
    }
    if (kind.isEmpty()) {
        kind = decorationKindForState(style, after);
    }
    if (kind == QStringLiteral("glow")
        && (rubyGlowConcentrationForState(style, after) < 0
            || rubyGlowRadiusForState(style, after) <= 0)) {
        return QStringLiteral("none");
    }
    return kind;
}

inline int rubyGlowConcentrationForState(const ResolvedStyle &style, bool after) {
    if (!after && style.decorationBeforeSync) {
        after = true;
    }
    if (after) {
        return style.rubyGlowConcentrationLevel.value_or(
            style.glowConcentrationLevel
        );
    }
    if (style.rubyGlowBeforeConcentrationLevel.has_value()) {
        return *style.rubyGlowBeforeConcentrationLevel;
    }
    return style.rubyGlowConcentrationLevel.value_or(
        glowConcentrationForState(style, false)
    );
}

inline int rubyShadowOffsetXForState(const ResolvedStyle &style, bool after) {
    if (!after && style.decorationBeforeSync) {
        after = true;
    }
    if (after) {
        return style.rubyShadowOffsetX.value_or(style.shadowOffsetX);
    }
    if (style.rubyShadowBeforeOffsetX.has_value()) {
        return *style.rubyShadowBeforeOffsetX;
    }
    return style.rubyShadowOffsetX.value_or(
        shadowOffsetXForState(style, false)
    );
}

inline int rubyShadowOffsetYForState(const ResolvedStyle &style, bool after) {
    if (!after && style.decorationBeforeSync) {
        after = true;
    }
    if (after) {
        return style.rubyShadowOffsetY.value_or(style.shadowOffsetY);
    }
    if (style.rubyShadowBeforeOffsetY.has_value()) {
        return *style.rubyShadowBeforeOffsetY;
    }
    return style.rubyShadowOffsetY.value_or(
        shadowOffsetYForState(style, false)
    );
}


// 行背景带（每行一条装饰底带）：纯装饰层，不参与布局与碰撞，几何 = 行文本
// 盒 + 四边独立留白，图像按 fillMode 铺进这条带子。镜像 Python 的
// ``Style.line_band_*`` 与 CPU painter 的 elements/band.py。
// 上下留白可以大于行距，所以渲染端必须在画字之前整帧先铺一遍带子。
struct LineBandConfig {
    bool enabled = false;
    // 绝对路径：IR 侧已把 ``preset:<id>`` 解析成素材文件路径。
    QString imagePath;
    QString fillMode = QStringLiteral("three_slice");
    QString midMode = QStringLiteral("repeat");
    // 端点像素宽（源图像素；实际宽度按带高/图高等比缩放）。
    int capLeftPx = 0;
    int capRightPx = 0;
    // 中段切片的源图边界（预设素材的设计端点）；显示端点由 capLeftPx 控制。
    int srcCapLeftPx = 0;
    int srcCapRightPx = 0;
    // 行文本盒到带子四条边的距离（逻辑像素，投影期乘 dpr）。
    int padTopPx = 20;
    int padBottomPx = 20;
    int padLeftPx = 40;
    int padRightPx = 40;
    // 上边距的量取基准：true = 注音盒顶，false = 主文字盒顶。
    bool includeRuby = false;
    int offsetYPx = 0;
    int opacityPct = 100;
    // 空 = 不染色；染色色值 "#RRGGBB"（镜像 CPU 的 SourceIn 叠色语义）。
    QString tint;
    int tintStrengthPct = 100;
    // 预设的独立谱线层（五线谱）：路径 + 谱线颜色。
    QString linesPath;
    QString lineColor;
    // 独立谱号层（含升降号）：路径 + 谱号颜色。
    QString clefPath;
    QString clefColor;
    // 图片非透明部分的投影/发光："none" / "shadow" / "glow"。
    QString edgeKind = QStringLiteral("none");
    QString edgeColor = QStringLiteral("#000000");
    int edgeSizePx = 12;
    int edgeOffsetXPx = 8;
    int edgeOffsetYPx = 8;
    int edgeOpacityPct = 70;
    // 图层效果（PS 图层样式口径）：三种效果各自独立开关 + 配置，可同时开启。
    struct EffectConfig {
        bool enabled = false;
        QString color;
        // 0 = 关闭；阴影/发光是模糊半径，描边是向外宽度。
        int sizePx = 0;
        // PS/AE 标准：扩展（膨胀百分比）、距离/角度（阴影偏移）、
        // 描边位置（outside/center/inside）、发光范围（衰减曲线）。
        int spreadPct = 0;
        int distancePx = 0;
        int angleDeg = 135;
        int rangePct = 50;
        QString position = QStringLiteral("outside");
        int offsetXPx = 0;
        int offsetYPx = 0;
    };
    EffectConfig shadow;
    EffectConfig stroke;
    EffectConfig glow;
};

struct RenderConfig {
    int width = 1920;
    int height = 1080;
    int fps = 60;
    // 预览缩放：布局仍在 width/height 逻辑坐标系计算，光栅化画布按 dpr 缩放
    // （对应 Python 侧 preview_render_target_size + setDevicePixelRatio 的语义）。
    double dpr = 1.0;

    int physicalWidth() const { return std::max(1, static_cast<int>(std::lround(width * dpr))); }
    int physicalHeight() const { return std::max(1, static_cast<int>(std::lround(height * dpr))); }
    ResolvedStyle baseStyle;
    // 行背景带为全局样式字段（不按角色/歌手分层）。
    LineBandConfig lineBand;
    QString layoutSemantics = QStringLiteral("legacy");
    int lineYMarginPx = 80;
    int lineGapPx = 90;
    int lineLeadInMs = 1800;
    int lineTailMs = 1000;
    int lineProtectMs = 0;
    int lineLaneGapMs = 300;
    int lineContinuitySnapMs = 800;
    int linePairSecondDelayMs = 3000;
    int lineMaxHoldMs = 12000;
    int sectionGapMs = 4000;
    QString lineYPosition = QStringLiteral("bottom");
    QString lineHorizontalLayout = QStringLiteral("asymmetric");
    QString sectionEndingMode = QStringLiteral("hold");
    bool syncEnding = false;
    int upperLineLeftMarginPx = 50;
    int lowerLineRightMarginPx = 50;
    int horizontalMarginPx = 50;
    QString smartHorizontal = QStringLiteral("equal_margins");
    std::vector<QString> lineAlignments{QStringLiteral("left"), QStringLiteral("right")};
    std::vector<int> lineOffsetsX;
    bool dualLineLayout = true;
    bool rightToLeft = false;
    bool vertical = false;
    int viewportScalePct = 100;
    int viewportRotationDeg = 0;
    int viewportOffsetX = 0;
    int viewportOffsetY = 0;
    QString viewportAlign = QStringLiteral("center");
    QString entryAnim = QStringLiteral("none");
    int entryLeadMs = 300;
    QString exitAnim = QStringLiteral("none");
    int exitFadeMs = 300;
    QString karaokeAnim = QStringLiteral("inherit");
    int timingOffsetMs = 0;
    int primaryTrackOffsetMs = 0;
    QJsonObject singerStyleOverrides;
    QJsonObject customStyleSchemes;
    // Built during configure. render_frame must not insert here because LineLayout stores pointers into this QHash.
    QHash<QString, ResolvedStyle> resolvedStyles;
    // Render IR schema 2: deduplicated vector guide glyph outlines. Characters
    // reference entries through ``vector_glyph_id`` and share the same immutable
    // object, so a thousand inline guide glyphs pay for one outline only.
    QHash<QString, std::shared_ptr<const krok::subtitle::native::VectorGlyph>> vectorGlyphs;
    // 发送端对 vector_glyphs 表的内容摘要；表按哈希门省发时 sidecar 据此
    // 保留上一份表（跨 configure 的行数据引用依赖同构符号对象）。
    QString vectorGlyphsHash;
    // 装饰粒子 sprite 轮廓表（fx_sprites；值语义，投影到 RenderScene::fxSprites）。
    QHash<QString, krok::subtitle::native::VectorGlyph> fxSprites;
    std::vector<TimingLine> lines;
    std::vector<RubyAnnotation> rubies;
    // Render IR schema: ``titles`` is an array of per-entry title payloads
    // (multi-title). Python and sidecar ship together, so the legacy single
    // ``title`` object key is not read anymore.
    QJsonArray titles;
};

}  // namespace krok::subtitle::native::protocol
