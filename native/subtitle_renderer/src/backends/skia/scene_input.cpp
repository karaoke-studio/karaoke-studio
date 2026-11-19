#include "scene_input.h"
#include "../render_backend.h"
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iterator>

namespace krok::subtitle::native {
namespace {
std::filesystem::path fontPath(const std::wstring& value) {
    // Legacy model strings are wide; the shared Skia core never sees this ABI.
    std::u32string scalars;
    for (std::size_t i = 0; i < value.size(); ++i) {
        char32_t c = value[i];
        if (c >= 0xd800 && c <= 0xdbff && i + 1 < value.size()) {
            const char32_t low = value[++i];
            if (low < 0xdc00 || low > 0xdfff) throw BackendError("Invalid font path Unicode");
            c = 0x10000 + ((c - 0xd800) << 10) + low - 0xdc00;
        }
        if (c > 0x10ffff || (c >= 0xd800 && c <= 0xdfff)) throw BackendError("Invalid font path Unicode");
        scalars.push_back(c);
    }
    return std::filesystem::path(scalars);
}
std::uint32_t argb(const RgbaColor& c) {
    return (std::uint32_t(c.alpha) << 24) | (std::uint32_t(c.red) << 16) | (std::uint32_t(c.green) << 8) | c.blue;
}
void supportedStyle(const TextStyle& s, bool canonical, const SkiaPaintCapabilities& caps) {
    const int outlineLayers = caps.outlineLayers;
    const auto gradient = [](const PaintStyle& paint) {
        return !paint.stops.empty()
            && (paint.mode == "gradient_horizontal" || paint.mode == "gradient_vertical" || paint.mode == "split_vertical");
    };
    const auto supportedBodyFill = [&](const PaintStyle& paint) {
        return paint.mode == "solid" || (caps.bodyGradients && gradient(paint)) || (caps.imageFills && paint.mode == "image");
    };
    const auto supportedOutline = [&](const PaintStyle& paint) {
        return paint.mode == "solid" || (caps.outlineGradients && gradient(paint)) || (caps.imageFills && paint.mode == "image");
    };
    const auto supportedDecoration = [&](bool after) {
        const auto kind = s.decorationKindForState(after);
        const auto& paint = after ? s.afterDecorPaint : s.beforeDecorPaint;
        if (kind == "none") return true;
        if (paint.mode != "solid") return false;
        if (kind == "shadow")
            return caps.staticShadow && std::isfinite(s.shadowOffsetXForState(after)) && std::isfinite(s.shadowOffsetYForState(after));
        // Ganesh's pinned blur image filter clamps larger sigmas.
        const float radius = after ? s.glowAfterRadius : s.glowBeforeRadius;
        return caps.staticGlow && kind == "glow" && std::isfinite(radius) && radius <= 532.f;
    };
    if ((canonical ? (s.layoutSemantics != "legacy" && s.layoutSemantics != "n3_1074") : s.layoutSemantics != "legacy")
        || (s.vertical && !canonical) || (s.rightToLeft && !canonical) || s.laneCount < 1 || s.laneCount > 32
        || (!canonical && (s.dualLineLayout || s.laneCount != 1))
        || (canonical && s.smartHorizontal != "none" && s.smartHorizontal != "center_position" && s.smartHorizontal != "equal_margins")
        || (s.alignment != "left" && s.alignment != "center" && s.alignment != "right")
        || (s.verticalPosition != "top" && s.verticalPosition != "center" && s.verticalPosition != "bottom")
        || (!canonical && (s.alignment != "center" || s.verticalPosition != "bottom" || s.smartHorizontal != "none"))
        || (s.allowBiting && !canonical)
        || (outlineLayers == 0 && (s.strokeWidth != 0 || s.latinStrokeWidth != 0))
        || (outlineLayers > 0 && (s.strokeWidth < 0 || !std::isfinite(s.strokeWidth)
            || s.strokeWidth != s.latinStrokeWidth
            || !supportedOutline(s.beforeStrokePaint) || !supportedOutline(s.afterStrokePaint)))
        || (outlineLayers < 2 && (s.stroke2Width != 0 || s.latinStroke2Width != 0))
        || (outlineLayers == 2 && (s.stroke2Width < 0 || !std::isfinite(s.stroke2Width)
            || s.stroke2Width != s.latinStroke2Width
            || !supportedOutline(s.beforeStroke2Paint) || !supportedOutline(s.afterStroke2Paint)))
        || !supportedDecoration(false) || !supportedDecoration(true)
        || !supportedBodyFill(s.beforeFillPaint) || !supportedBodyFill(s.afterFillPaint)
        // Canonical fonts come exclusively from the validated layout resource
        // table. D2D compatibility hints may be absent on inherited role slots.
        || (!canonical && (s.fontHint.resourcePath.empty() || s.fontHint.syntheticBold || s.fontHint.syntheticItalic
        || s.fontHint.variable || s.fontAxis || s.latinFontAxis || !s.fontHint.axes.empty() || s.italic || s.latinItalic))
        || (!canonical && (s.fontHint.scale != 1.f || s.latinFontStretchPct != 100
        || s.latinFontSize.value_or(s.fontSize) != s.fontSize
        || (s.latinFontFamily && !s.latinFontFamily->empty() && *s.latinFontFamily != s.fontFamily)
        || (s.latinFontHint.present && s.latinFontHint != s.fontHint)
        || s.latinFontWeight.value_or(s.fontWeight) != s.fontWeight))
        || s.volumeEnabled || s.litEnabled)
        throw BackendError(canonical ? "Skia 当前仅支持固定网格横排、实色填充与基础走字" :
            "Skia P04: unsupported style (requires single centered horizontal line, solid fills, static supplied font, no effects)");
}
}
void validateSkiaScene(const RenderScene& scene, bool canonical, SkiaPaintCapabilities caps) {
    if (scene.viewportScale != 1 || scene.viewportRotation != 0 || scene.viewportOffsetX != 0
        || scene.viewportOffsetY != 0 || scene.lineBand.enabled || (!canonical && !scene.charStyles.empty())
        || scene.exportCropHeight || !scene.exportBands.empty())
        throw BackendError("Skia P04: viewport, crop/bands, character styles and line backgrounds require later phases");
    if (caps.outlineLayers > 0 && !scene.charStyles.empty()) throw BackendError("Skia 实色描边暂不支持行内样式");
    for (const auto& style : scene.charStyles) supportedStyle(style, canonical, {.outlineLayers = caps.outlineLayers});
    for (std::size_t index = 0; index < scene.lines.size(); ++index) {
        const auto& line = scene.lines[index];
        const auto& style = index < scene.lineStyles.size() ? scene.lineStyles[index] : scene.style;
        supportedStyle(style, canonical, caps);
        if (line.lane < 0 || line.lane >= style.laneCount || (!canonical && line.lane != 0)
            || !line.rubies.empty() || (!canonical && !line.bursts.empty()) || (!canonical && !line.placementWindows.empty())
            || line.fadeInMs || line.fadeOutMs
            || (!canonical && (line.entryAnimation != "none" || line.exitAnimation != "none"))
            || (line.karaokeAnimation != "none" && line.karaokeAnimation != "inherit"
                && !(canonical && line.karaokeAnimation == "no_wipe"))
            || (!canonical && (line.scanlineEnabled || line.strokeFlashEnabled || line.wipeReverse)) || line.zoomPulseEnabled
            || line.staticOverlay || line.annotationBaselineY || line.displayWindows.size() != 1)
            throw BackendError("Skia P04: unsupported line animation/layout/annotation");
        if (line.displayWindows[0].fadeInMs > 0 || line.displayWindows[0].fadeOutMs > 0)
            throw BackendError("Skia P04: fading requires P07");
        for (const auto& ch : line.chars) {
            if ((!canonical && (ch.text.size() != 1 || ch.styleIndex >= 0 || ch.vectorGlyph)) || ch.bitmapGuide
                || !ch.wipePoints.empty() || !ch.fallbackFamily.empty())
                throw BackendError("Skia P04: shaping, guide symbols, fallback fonts and custom wipe curves require later phases");
            if (ch.styleIndex < -1 || ch.styleIndex >= int(scene.charStyles.size()))
                throw BackendError("Skia 字符样式引用无效");
        }
    }
}

::subtitle::skia::StaticShadow skiaStaticShadow(const TextStyle& style) {
    const auto state = [&](bool after) {
        const auto& paint = after ? style.afterDecorPaint : style.beforeDecorPaint;
        return ::subtitle::skia::ShadowState{style.decorationKindForState(after) == "shadow",
            style.shadowOffsetXForState(after), style.shadowOffsetYForState(after), argb(paint.color)};
    };
    return {state(false), state(true), style.strokeWidth, style.stroke2Width};
}

::subtitle::skia::StaticGlow skiaStaticGlow(const TextStyle& style) {
    const auto state = [&](bool after) {
        if (style.decorationKindForState(after) != "glow") return ::subtitle::skia::GlowState{};
        const auto& paint = after ? style.afterDecorPaint : style.beforeDecorPaint;
        const auto radius = after ? style.glowAfterRadius : style.glowBeforeRadius;
        return ::subtitle::skia::GlowState{int(std::lround(radius)), style.glowConcentrationForState(after), argb(paint.color)};
    };
    return {state(false), state(true), style.strokeWidth + style.stroke2Width};
}

::subtitle::skia::SubtitleInput skiaSceneInput(const RenderScene& scene) {
    validateSkiaScene(scene);
    ::subtitle::skia::SubtitleInput input{scene.width, scene.height, {}};
    for (std::size_t index = 0; index < scene.lines.size(); ++index) {
        const auto& line = scene.lines[index];
        const auto& style = index < scene.lineStyles.size() ? scene.lineStyles[index] : scene.style;
        ::subtitle::skia::SubtitleLine out;
        std::ifstream stream(fontPath(style.fontHint.resourcePath), std::ios::binary);
        out.fontBytes = {std::istreambuf_iterator<char>(stream), {}};
        if (out.fontBytes.empty()) throw BackendError("Skia P04: cannot read supplied font snapshot");
        out.fontSize = style.fontSize;
        out.letterSpacing = style.letterSpacing;
        out.spaceWidth = style.fontSize * style.spaceWidthPercent / 100;
        out.centerX = scene.width / 2.f + style.layoutOffsetX + style.laneOffsetX + style.centerOffsetX;
        out.bottomY = scene.height - style.bottomMargin + style.layoutOffsetY + style.centerOffsetY;
        out.beforeArgb = argb(style.beforeFillPaint.color);
        out.afterArgb = argb(style.afterFillPaint.color);
        out.visibleStartMs = line.displayWindows[0].startMs;
        out.visibleEndMs = line.displayWindows[0].endMs;
        for (const auto& ch : line.chars)
            out.cells.push_back({static_cast<std::uint32_t>(ch.text[0]), ch.startMs, ch.endMs});
        input.lines.push_back(std::move(out));
    }
    return input;
}
}
