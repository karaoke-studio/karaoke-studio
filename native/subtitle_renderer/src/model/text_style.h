#pragma once

#include "glyph_resources.h"
#include "paint_types.h"
#include <optional>

namespace krok::subtitle::native {

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
    // Python 侧统一解析出的字体实例决策（见 FontInstanceHint）；缺省时
    // 渲染端按 fontAxis + 引擎镜像规则自行解析。
    FontInstanceHint fontHint;
    std::optional<int> latinFontWeight;
    bool latinFontAxis = false;
    FontInstanceHint latinFontHint;
    int latinFontStretchPct = 100;
    bool italic = false;
    // Per-slot italic (resolved by gpu_scene_projection from the stroke2-style
    // inheritance chain; legacy IR without the fields keeps them equal to
    // ``italic`` so old payloads render unchanged).
    bool latinItalic = false;
    bool rubyItalic = false;
    bool rubyLatinItalic = false;
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
    float laneOffsetX = 0.0f;  // Authored horizontal translation, after smart alignment.
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
    std::string decorationKindBefore;
    bool decorationBeforeSync = true;
    float glowBeforeRadius = 10.0f;
    float glowAfterRadius = 10.0f;
    int glowConcentrationLevel = 0;
    int glowBeforeConcentrationLevel = 0;
    float shadowOffsetX = 5.0f;
    float shadowOffsetY = 5.0f;
    float shadowBeforeOffsetX = 5.0f;
    float shadowBeforeOffsetY = 5.0f;
    std::wstring rubyFontFamily;
    std::optional<std::wstring> rubyLatinFontFamily;
    float rubyFontSize = 45.0f;
    std::optional<float> rubyLatinFontSize;
    int rubyFontWeight = 400;
    bool rubyFontAxis = false;
    FontInstanceHint rubyFontHint;
    std::optional<int> rubyLatinFontWeight;
    bool rubyLatinFontAxis = false;
    FontInstanceHint rubyLatinFontHint;
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
    std::string rubyDecorationKindBefore;
    float rubyGlowBeforeRadius = 0.0f;
    float rubyGlowAfterRadius = 0.0f;
    int rubyGlowConcentrationLevel = 0;
    int rubyGlowBeforeConcentrationLevel = 0;
    float rubyShadowOffsetX = 0.0f;
    float rubyShadowOffsetY = 1.0f;
    float rubyShadowBeforeOffsetX = 0.0f;
    float rubyShadowBeforeOffsetY = 1.0f;

    std::string decorationKindForState(bool after) const {
        if (!after && decorationBeforeSync) {
            after = true;
        }
        if (after) {
            if (decorationKind == "glow"
                && (glowConcentrationLevel < 0 || glowAfterRadius <= 0.0f)) {
                return "none";
            }
            return decorationKind;
        }
        const std::string kind = decorationKindBefore.empty()
            ? decorationKind
            : decorationKindBefore;
        if (kind == "glow"
            && (glowBeforeConcentrationLevel < 0
                || glowBeforeRadius <= 0.0f)) {
            return "none";
        }
        return kind;
    }

    int glowConcentrationForState(bool after) const {
        return (after || decorationBeforeSync)
            ? glowConcentrationLevel
            : glowBeforeConcentrationLevel;
    }

    float shadowOffsetXForState(bool after) const {
        return (after || decorationBeforeSync)
            ? shadowOffsetX
            : shadowBeforeOffsetX;
    }

    float shadowOffsetYForState(bool after) const {
        return (after || decorationBeforeSync)
            ? shadowOffsetY
            : shadowBeforeOffsetY;
    }

    std::string rubyDecorationKindForState(bool after) const {
        if (!after && decorationBeforeSync) {
            after = true;
        }
        std::string kind;
        if (after) {
            kind = rubyDecorationKind.empty()
                ? decorationKindForState(true)
                : rubyDecorationKind;
        } else if (!rubyDecorationKindBefore.empty()) {
            kind = rubyDecorationKindBefore;
        } else if (!rubyDecorationKind.empty()) {
            kind = rubyDecorationKind;
        } else {
            kind = decorationKindForState(false);
        }
        if (kind == "glow"
            && (rubyGlowConcentrationForState(after) < 0
                || (after ? rubyGlowAfterRadius : rubyGlowBeforeRadius) <= 0.0f)) {
            return "none";
        }
        return kind;
    }

    int rubyGlowConcentrationForState(bool after) const {
        return (after || decorationBeforeSync)
            ? rubyGlowConcentrationLevel
            : rubyGlowBeforeConcentrationLevel;
    }

    float rubyShadowOffsetXForState(bool after) const {
        return (after || decorationBeforeSync)
            ? rubyShadowOffsetX
            : rubyShadowBeforeOffsetX;
    }

    float rubyShadowOffsetYForState(bool after) const {
        return (after || decorationBeforeSync)
            ? rubyShadowOffsetY
            : rubyShadowBeforeOffsetY;
    }

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
    // Scan-line shape: "band" draws the highlight band; "flame" / "bubble"
    // replace it with a procedural fire / bubble column at the wipe front;
    // "image" overlays a user image asset (see scanlineImage* below).
    std::string scanlineShape = "band";
    // 自定义图片形态（scanlineShape == "image"）：素材绝对路径（Python 侧
    // 已做「绝对路径 → 工程目录回退」解析，解析不到发空串=素材缺失，
    // 渲染端回退普通高亮带）。(mtime, size) 失效签名同 litImage* 口径，
    // 在 gpuSceneFromConfig 里用 QFileInfo 探测。
    std::wstring scanlineImagePath;
    std::uint64_t scanlineImageModifiedMs = 0;
    std::uint64_t scanlineImageSize = 0;
    // 是否同时绘制普通文字高亮带；素材缺失时渲染端恒回退绘制。
    bool scanlineImageBandEnabled = true;
    // 图片锚点（走字锋面 + 主文字垂直中心）偏移，场景像素；正右/上，负左/下。
    float scanlineImageOffsetX = 0.0f;
    float scanlineImageOffsetY = 0.0f;
    // 取色层级（排列组合）：实色上色 / 描边 / 装饰，三个开关任意组合。
    bool scanlineImageTint = false;
    bool scanlineImageStroke = false;
    bool scanlineImageDecor = false;
    // 显示尺寸倍率（1.0 = 自然像素 × 画布高/1080）与上色强度（0~100，
    // 部分强度按预乘混合保留原色，与 Python _tinted 同公式）。
    float scanlineImageScale = 1.0f;
    int scanlineImageTintStrength = 50;
    // ``role`` 模式的装饰几何来自来源方案（Python 侧已物化成数值；-1 =
    // 未下发，沿用行样式）。与 Painter scanline_image._decor_plan 同口径：
    // 方案未显式指定的槽位按 0 处理（不继承全局），镜像 CPU 的取值。
    float scanlineImageRoleStrokeWidth = -1.0f;
    float scanlineImageRoleStroke2Width = -1.0f;
    float scanlineImageRoleGlowRadius = -1.0f;
    float scanlineImageRoleShadowOffsetX = -1.0f;
    float scanlineImageRoleShadowOffsetY = -1.0f;
    float scanlineImageRoleFontSize = -1.0f;
    std::string scanlineImageRoleDecorationKind;
    // Whole-char zoom pulse easing order (0..5; 0 = linear).  Whether the
    // effect is active is a per-line flag (TextLine::zoomPulseEnabled).
    int zoomPulseCurveLevel = 1;
    // Whole-char zoom pulse peak magnification in percent (100..200;
    // 125 = historical 1.25x).  Dimensionless; mirrors Python
    // Style.zoom_pulse_peak_pct via gpu_scene_projection.
    int zoomPulsePeakPct = 125;
    bool operator==(const TextStyle &) const = default;
};

// 行背景带（行装饰层）的渲染规格：几何已按 layoutReferenceScale 缩放成
// 场景像素；空 imagePath 或 enabled=false 时渲染端整段跳过。镜像
// Python 的 ``Style.line_band_*`` 与 CPU painter 的 elements/band.py。

}  // namespace krok::subtitle::native
