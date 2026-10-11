#pragma once

#include <dwrite.h>
#include <dwrite_2.h>
#include <wrl/client.h>

#include <string>
#include <vector>

// FontInstanceHint (Python-resolved font instance for one style slot) lives
// with the scene model because the IR parser and the projection carry it.
#include "../../model/render_types.h"

namespace krok::subtitle::native::direct2d {

// Scene-model alias: one resolved font instance = one hint record.
using ResolvedFaceHint = FontInstanceHint;

// One (family, weight, italic) resolved through the unified weight rules.
// ``outline`` is the face actually drawn/measured per glyph: the variable
// axis-value instance for variable fonts, or the static face (plus bold /
// oblique simulation for the synthetic cases).  ``metrics`` is the
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
//
// ``hint`` (Python decision) overrides both: the named face weight, the
// synthetic bold/oblique flags and the axis instance come from the decision.
ResolvedFontFaces resolveFontFaces(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    bool axisHint = false,
    const ResolvedFaceHint &hint = {}
);

// Outline-only view of resolveFontFaces for callers that do not need the
// metrics face (emoji / fallback chains).
Microsoft::WRL::ComPtr<IDWriteFontFace> createFontFace(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    bool axisHint = false,
    const ResolvedFaceHint &hint = {}
);

bool containsEmoji(const std::wstring &text);

std::vector<UINT16> glyphIndices(
    IDWriteFontFace *face,
    const std::wstring &text
);

// Coverage check over the whole text, cluster-aware: variation selectors and
// control/format characters are ignored, combining marks attach to the
// preceding base character, and every cluster must draw at least one non-zero
// glyph.  Replaces the historical "first glyph only" test, which let a cell
// whose later characters were missing render as tofu instead of falling back.
bool textFullyCovered(
    IDWriteFontFace *face,
    const std::wstring &text,
    std::vector<UINT16> *glyphsOut = nullptr
);

// Fallback for characters the requested family cannot cover.
//
// Resolution order: the Python-supplied ``preferredFamily`` (Qt's measured
// choice, so both backends agree) first, then the DirectWrite **system font
// fallback** (``IDWriteFontFallback::MapCharacters`` with the user locale and
// the requested weight/style) -- never a hardcoded per-script family list.
// The lookup is a pure function of (base family, weight, italic, text): an
// earlier character's success never changes a later character's priority.
// Returns the covering family name (empty when not found); the caller
// resolves the face through its cached resolver so the pointer stays stable
// for the glyph-geometry cache.
std::wstring findFallbackFontFace(
    IDWriteFontFallback *systemFallback,
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &baseFamily,
    const std::wstring &text,
    int weight,
    bool italic,
    const std::wstring &preferredFamily,
    std::vector<UINT16> &glyphs
);

}  // namespace krok::subtitle::native::direct2d
