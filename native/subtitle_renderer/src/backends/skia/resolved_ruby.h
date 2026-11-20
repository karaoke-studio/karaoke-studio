#pragma once
#include "resolved_glyph.h"
#include "../../model/render_scene.h"

namespace krok::subtitle::native {
enum class RubyTransition { None, Fade, Spin, Translation };

std::vector<::subtitle::skia::ResolvedRubyGroup> resolvedSkiaRubyGroups(
    const QJsonObject& line, const QJsonObject& ir, const ResolvedGlyphContext& glyphs,
    const RenderScene& scene, int lineIndex, int timingOffset, RubyTransition,
    std::size_t& totalCells, std::size_t& totalGroups);
}
