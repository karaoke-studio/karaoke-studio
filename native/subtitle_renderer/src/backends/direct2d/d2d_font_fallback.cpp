#include "d2d_font_fallback.h"

#include <algorithm>
#include <cstdint>
#include <utility>

namespace krok::subtitle::native::direct2d {

Microsoft::WRL::ComPtr<IDWriteFontFace> createFontFace(
    IDWriteFontCollection *collection,
    const std::wstring &familyName,
    int weight,
    bool italic
) {
    UINT32 familyIndex = 0;
    BOOL exists = FALSE;
    if (familyName.empty()
        || FAILED(collection->FindFamilyName(familyName.c_str(), &familyIndex, &exists))
        || !exists) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
    if (FAILED(collection->GetFontFamily(familyIndex, family.ReleaseAndGetAddressOf()))) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFont> font;
    if (FAILED(family->GetFirstMatchingFont(
            static_cast<DWRITE_FONT_WEIGHT>(std::clamp(weight, 1, 999)),
            DWRITE_FONT_STRETCH_NORMAL,
            italic ? DWRITE_FONT_STYLE_ITALIC : DWRITE_FONT_STYLE_NORMAL,
            font.ReleaseAndGetAddressOf()))) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontFace> face;
    if (FAILED(font->CreateFontFace(face.ReleaseAndGetAddressOf()))) {
        return {};
    }
    return face;
}

namespace {

std::vector<UINT32> unicodeScalars(const std::wstring &text) {
    std::vector<UINT32> values;
    values.reserve(text.size());
    for (std::size_t index = 0; index < text.size(); ++index) {
        const UINT32 first = static_cast<std::uint16_t>(text[index]);
        if (first >= 0xD800 && first <= 0xDBFF && index + 1 < text.size()) {
            const UINT32 second = static_cast<std::uint16_t>(text[index + 1]);
            if (second >= 0xDC00 && second <= 0xDFFF) {
                values.push_back(
                    0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
                );
                ++index;
                continue;
            }
        }
        if (first >= 0xFE00 && first <= 0xFE0F) {
            continue;
        }
        values.push_back(first);
    }
    return values;
}

}  // namespace

bool containsEmoji(const std::wstring &text) {
    const auto scalars = unicodeScalars(text);
    return std::any_of(scalars.begin(), scalars.end(), [](UINT32 value) {
        return (value >= 0x1F000 && value <= 0x1FAFF)
            || (value >= 0x2600 && value <= 0x27BF);
    });
}

std::vector<UINT16> glyphIndices(IDWriteFontFace *face, const std::wstring &text) {
    const std::vector<UINT32> scalars = unicodeScalars(text);
    std::vector<UINT16> glyphs(scalars.size());
    if (!scalars.empty()
        && FAILED(face->GetGlyphIndices(
            scalars.data(),
            static_cast<UINT32>(scalars.size()),
            glyphs.data()))) {
        glyphs.clear();
    }
    return glyphs;
}

bool validGlyphIndices(const std::vector<UINT16> &glyphs) {
    return !glyphs.empty() && glyphs.front() != 0;
}

Microsoft::WRL::ComPtr<IDWriteFontFace> findFallbackFontFace(
    IDWriteFontCollection *collection,
    const std::wstring &text,
    std::vector<Microsoft::WRL::ComPtr<IDWriteFontFace>> &successfulFaces,
    std::vector<UINT16> &glyphs
) {
    for (const auto &face : successfulFaces) {
        glyphs = glyphIndices(face.Get(), text);
        if (validGlyphIndices(glyphs)) {
            return face;
        }
    }

    auto tryFace = [&](Microsoft::WRL::ComPtr<IDWriteFontFace> face) {
        if (!face) {
            return Microsoft::WRL::ComPtr<IDWriteFontFace>{};
        }
        std::vector<UINT16> candidate = glyphIndices(face.Get(), text);
        if (!validGlyphIndices(candidate)) {
            return Microsoft::WRL::ComPtr<IDWriteFontFace>{};
        }
        glyphs = std::move(candidate);
        successfulFaces.push_back(face);
        return face;
    };

    if (containsEmoji(text)) {
        if (auto face = tryFace(createFontFace(
                collection, L"Segoe UI Symbol", DWRITE_FONT_WEIGHT_NORMAL, false))) {
            return face;
        }
    }
    if (auto face = tryFace(createFontFace(
            collection, L"Microsoft JhengHei", DWRITE_FONT_WEIGHT_BOLD, false))) {
        return face;
    }
    const UINT32 familyCount = collection->GetFontFamilyCount();
    for (UINT32 index = 0; index < familyCount; ++index) {
        Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
        if (FAILED(collection->GetFontFamily(index, family.ReleaseAndGetAddressOf()))) {
            continue;
        }
        Microsoft::WRL::ComPtr<IDWriteFont> font;
        if (FAILED(family->GetFirstMatchingFont(
                DWRITE_FONT_WEIGHT_BOLD,
                DWRITE_FONT_STRETCH_NORMAL,
                DWRITE_FONT_STYLE_NORMAL,
                font.ReleaseAndGetAddressOf()))) {
            continue;
        }
        Microsoft::WRL::ComPtr<IDWriteFontFace> candidate;
        if (FAILED(font->CreateFontFace(candidate.ReleaseAndGetAddressOf()))) {
            continue;
        }
        if (auto face = tryFace(std::move(candidate))) {
            return face;
        }
    }
    glyphs.clear();
    return {};
}

}  // namespace krok::subtitle::native::direct2d
