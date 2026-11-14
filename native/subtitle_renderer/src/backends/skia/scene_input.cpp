#include "scene_input.h"
#include "../render_backend.h"
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
void supportedStyle(const TextStyle& s) {
    if (s.vertical || s.rightToLeft || s.alignment != "center" || s.verticalPosition != "bottom"
        || s.laneCount != 1 || s.smartHorizontal != "none" || s.allowBiting
        || s.strokeWidth != 0 || s.stroke2Width != 0 || s.latinStrokeWidth != 0 || s.latinStroke2Width != 0
        || s.decorationKindForState(false) != "none" || s.decorationKindForState(true) != "none"
        || s.beforeFillPaint.mode != "solid" || s.afterFillPaint.mode != "solid"
        || s.fontHint.resourcePath.empty() || s.fontHint.syntheticBold || s.fontHint.syntheticItalic
        || s.fontHint.variable || !s.fontHint.axes.empty() || s.italic || s.latinItalic
        || s.fontHint.scale != 1.f || s.latinFontStretchPct != 100
        || s.latinFontSize.value_or(s.fontSize) != s.fontSize
        || (s.latinFontFamily && !s.latinFontFamily->empty() && *s.latinFontFamily != s.fontFamily)
        || (!s.latinFontHint.resourcePath.empty() && s.latinFontHint.resourcePath != s.fontHint.resourcePath)
        || s.volumeEnabled || s.litEnabled)
        throw BackendError("Skia P04: unsupported style (requires single centered horizontal line, solid fills, static supplied font, no effects)");
}
}
::subtitle::skia::SubtitleInput skiaSceneInput(const RenderScene& scene) {
    if (scene.viewportScale != 1 || scene.viewportRotation != 0 || scene.viewportOffsetX != 0
        || scene.viewportOffsetY != 0 || scene.lineBand.enabled || !scene.charStyles.empty()
        || scene.exportCropHeight || !scene.exportBands.empty())
        throw BackendError("Skia P04: viewport, crop/bands, character styles and line backgrounds require later phases");
    ::subtitle::skia::SubtitleInput input{scene.width, scene.height, {}};
    for (std::size_t index = 0; index < scene.lines.size(); ++index) {
        const auto& line = scene.lines[index];
        const auto& style = index < scene.lineStyles.size() ? scene.lineStyles[index] : scene.style;
        supportedStyle(style);
        if (line.lane != 0 || !line.rubies.empty() || !line.bursts.empty() || !line.placementWindows.empty()
            || line.fadeInMs || line.fadeOutMs || line.entryAnimation != "none" || line.exitAnimation != "none"
            || (line.karaokeAnimation != "none" && line.karaokeAnimation != "inherit")
            || line.scanlineEnabled || line.zoomPulseEnabled || line.strokeFlashEnabled || line.wipeReverse
            || line.staticOverlay || line.annotationBaselineY || line.displayWindows.size() != 1)
            throw BackendError("Skia P04: unsupported line animation/layout/annotation");
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
        if (line.displayWindows[0].fadeInMs > 0 || line.displayWindows[0].fadeOutMs > 0)
            throw BackendError("Skia P04: fading requires P07");
        for (const auto& ch : line.chars) {
            if (ch.text.size() != 1 || ch.styleIndex >= 0 || ch.vectorGlyph || ch.bitmapGuide
                || !ch.wipePoints.empty() || !ch.fallbackFamily.empty())
                throw BackendError("Skia P04: shaping, guide symbols, fallback fonts and custom wipe curves require later phases");
            out.cells.push_back({static_cast<std::uint32_t>(ch.text[0]), ch.startMs, ch.endMs});
        }
        input.lines.push_back(std::move(out));
    }
    return input;
}
}
