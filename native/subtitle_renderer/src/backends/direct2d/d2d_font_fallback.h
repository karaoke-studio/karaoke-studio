#pragma once

#include <dwrite.h>
#include <wrl/client.h>

#include <string>
#include <vector>

namespace krok::subtitle::native::direct2d {

// One (family, weight, italic) resolved through the unified weight rules.
// ``outline`` is the face actually drawn/measured per glyph: the variable
// axis-value instance for variable fonts, or the static face (plus bold
// simulation for the single-face synthetic case).  ``metrics`` is the
// default-instance / unsimulated face whose vertical metrics match the
// static OS/2 values QFontMetrics reports on the CPU side.
struct ResolvedFontFaces {
    Microsoft::WRL::ComPtr<IDWriteFontFace> outline;
    Microsoft::WRL::ComPtr<IDWriteFontFace> metrics;
};

// Resolve one family name through the unified weight rules (see
// d2d_font_fallback.cpp).  ``collection`` is the classic GDI-model system
// collection; ``typographicCollection`` (nullable) is the typographic-model
// collection that groups variable fonts under merged family names.  The name
// is tried typographic-first, then classic, then the Win32
// informational-name scan, so both spellings the Qt font picker offers
// resolve to the face the CPU renderer draws.
//
// ``axisHint`` true means the family is a real variable font: the matched
// face's wght axis is instantiated at the requested weight (clamped to the
// axis range).  ``axisHint`` false follows the engine-mirrored static rules
// (exact face / nearest with ties preferring lighter / bold simulation only
// when the request >= 600 and the matched face < 600).
ResolvedFontFaces resolveFontFaces(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    bool axisHint = false
);

// Outline-only view of resolveFontFaces for callers that do not need the
// metrics face (emoji / fallback chains).
Microsoft::WRL::ComPtr<IDWriteFontFace> createFontFace(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    bool axisHint = false
);

bool containsEmoji(const std::wstring &text);

std::vector<UINT16> glyphIndices(
    IDWriteFontFace *face,
    const std::wstring &text
);

bool validGlyphIndices(const std::vector<UINT16> &glyphs);

Microsoft::WRL::ComPtr<IDWriteFontFace> findFallbackFontFace(
    IDWriteFontCollection *collection,
    const std::wstring &text,
    std::vector<Microsoft::WRL::ComPtr<IDWriteFontFace>> &successfulFaces,
    std::vector<UINT16> &glyphs
);

}  // namespace krok::subtitle::native::direct2d
