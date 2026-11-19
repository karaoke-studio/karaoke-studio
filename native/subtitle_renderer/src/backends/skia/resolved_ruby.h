#pragma once
#include "resolved_glyph.h"
#include "../../model/render_scene.h"

namespace krok::subtitle::native {
std::vector<::subtitle::skia::ResolvedRubyGroup> resolvedSkiaRubyGroups(
    const QJsonObject& line, const QJsonObject& ir, const ResolvedGlyphContext& glyphs,
    const RenderScene& scene, int lineIndex, int timingOffset, bool fadeComposition,
    std::size_t& totalCells, std::size_t& totalGroups);
}
