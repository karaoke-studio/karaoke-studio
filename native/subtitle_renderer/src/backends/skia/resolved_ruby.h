#pragma once
#include "resolved_glyph.h"
#include "../../model/render_scene.h"

namespace krok::subtitle::native {
std::vector<std::vector<::subtitle::skia::ResolvedCell>> resolvedSkiaRubyGroups(
    const QJsonObject& line, const QJsonObject& ir, const ResolvedGlyphContext& glyphs,
    const RenderScene& scene, int lineIndex, int timingOffset, std::size_t& totalCells, std::size_t& totalGroups);
}
