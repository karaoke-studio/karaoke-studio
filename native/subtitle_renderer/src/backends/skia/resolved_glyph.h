#pragma once
#include "core/resolved_scene.h"
#include "text/font_instance.h"
#include "../../model/glyph_resources.h"
#include <QtCore/QJsonObject>
#include <QtCore/QString>
#include <memory>
#include <string>
#include <vector>

namespace krok::subtitle::native {
// The existing GPU's M/L/C/Q/Z geometry at max(size, 1) / unitsPerEm (winding, y-down).
SkPath vectorGlyphPath(const VectorGlyph& glyph, double size);
struct ResolvedGlyphContext {
    using Fonts = std::vector<std::shared_ptr<const ::subtitle::skia::text::FontInstance>>;
    std::vector<Fonts> fonts;
    std::vector<std::string> languages;
    std::vector<bool> mixedFallback;
    double dpr = 1;
    ::subtitle::skia::ResolvedCell cell(const QJsonObject&, const QString& text, int startMs, int endMs,
                                     std::uint32_t before, std::uint32_t after, double wipePad = 0) const;
    // vector_guide_schema=1: an authored guide outline at logical font size.
    ::subtitle::skia::ResolvedCell vectorCell(const QJsonObject&, const VectorGlyph&, const QString& glyphId,
                                           double size, int startMs, int endMs,
                                           std::uint32_t before, std::uint32_t after, double wipePad) const;
};
}
