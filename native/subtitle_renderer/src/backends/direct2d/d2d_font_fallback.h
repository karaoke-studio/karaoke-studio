#pragma once

#include <dwrite.h>
#include <wrl/client.h>

#include <string>
#include <vector>

namespace krok::subtitle::native::direct2d {

Microsoft::WRL::ComPtr<IDWriteFontFace> createFontFace(
    IDWriteFontCollection *collection,
    const std::wstring &familyName,
    int weight,
    bool italic
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
