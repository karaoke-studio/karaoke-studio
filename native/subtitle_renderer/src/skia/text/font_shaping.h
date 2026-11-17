#pragma once
#include "shaped_cell.h"

namespace subtitle::skia::text {
struct ShapedRange {
    std::vector<ShapedGlyph> glyphs;
    float advance = 0;
};
// Authored-cell glyph policy, cell_glyph_schema=1 (the existing GPU's rule).
// OpenType shaping applies only to a cell holding a supplementary-plane scalar,
// or two or more scalars including a mark (Mn/Mc/Me) or ZWJ. Every other cell
// draws each scalar's nominal cmap glyph at its design advance: no GSUB/GPOS,
// so no ligature, kerning or localized form. Language is irrelevant there.
bool cellUsesOpenTypeShaping(const std::string& utf8);
// Preserve full-cell HB context while shaping a font range in logical pixels.
// With opentype=false the range is nominal glyphs placed at design advances.
ShapedRange shapeFontRange(const std::string& utf8, const FontInstance& font,
    std::uint32_t begin, std::uint32_t end, const std::string& language, bool rtl = false, bool opentype = true);
// Nominal cmap glyph per scalar in [begin, end); 0 where the face maps none.
// Cell design advance/bearings always come from these, even for shaped cells.
std::vector<SkGlyphID> nominalGlyphs(const std::string& utf8, const FontInstance& font,
    std::uint32_t begin, std::uint32_t end);
}
