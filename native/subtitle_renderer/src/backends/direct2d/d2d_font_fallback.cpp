#include "d2d_font_fallback.h"

#include <dwrite_3.h>

#include <algorithm>
#include <cstdint>
#include <cwchar>
#include <unordered_map>
#include <utility>

namespace krok::subtitle::native::direct2d {

namespace {

// Unified weight resolution -- mirrors engine/text/font_weight.py on the
// Python side; the two implementations must stay in lockstep.
//
// Variable fonts (fvar wght axis): render the TRUE axis-value instance
// clamped to the axis range; never simulate.  Static fonts follow the
// engine (Qt/DirectWrite observed) matching: (a) exact face, (b) nearest
// face by |weight - request| with ties preferring the lighter face,
// (c) bold simulation only when request >= 600 AND the matched face < 600
// (coarse faces cannot be further bolded).

bool localizedStringsContain(
    IDWriteLocalizedStrings *strings,
    const std::wstring &needle
) {
    if (strings == nullptr) {
        return false;
    }
    const UINT32 count = strings->GetCount();
    for (UINT32 index = 0; index < count; ++index) {
        UINT32 length = 0;
        if (FAILED(strings->GetStringLength(index, &length))) {
            continue;
        }
        std::wstring value(static_cast<std::size_t>(length) + 1, L'\0');
        if (FAILED(strings->GetString(index, value.data(), length + 1))) {
            continue;
        }
        value.resize(length);
        if (_wcsicmp(value.c_str(), needle.c_str()) == 0) {
            return true;
        }
    }
    return false;
}

// DirectWrite groups faces under the typographic family (``モトヤ教科書 Pro``),
// while GDI - and therefore Qt, and therefore every family name the app stores -
// enumerates the legacy family that folds the weight into the name
// (``モトヤ教科書 Pro W2``).  ``FindFamilyName`` never matches those, so without
// this lookup the renderer silently substitutes a default font for any family
// the font picker offered under its legacy spelling.
//
// The legacy name identifies exactly one face, which is what Qt resolves it to,
// so the matching font is returned directly instead of re-selecting by weight.
Microsoft::WRL::ComPtr<IDWriteFont> findFontByGdiFamilyName(
    IDWriteFontCollection *collection,
    const std::wstring &familyName
) {
    // Walking every face is far too slow to repeat per line, and the system
    // collection is a cached object that only changes when it is rebuilt.
    static IDWriteFontCollection *cachedCollection = nullptr;
    static std::unordered_map<std::wstring, Microsoft::WRL::ComPtr<IDWriteFont>>
        cachedFonts;
    if (cachedCollection != collection) {
        cachedCollection = collection;
        cachedFonts.clear();
    }
    const auto cached = cachedFonts.find(familyName);
    if (cached != cachedFonts.end()) {
        return cached->second;
    }

    Microsoft::WRL::ComPtr<IDWriteFont> match;
    const UINT32 familyCount = collection->GetFontFamilyCount();
    for (UINT32 familyIndex = 0; familyIndex < familyCount && !match; ++familyIndex) {
        Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
        if (FAILED(collection->GetFontFamily(familyIndex, family.ReleaseAndGetAddressOf()))) {
            continue;
        }
        const UINT32 fontCount = family->GetFontCount();
        for (UINT32 fontIndex = 0; fontIndex < fontCount; ++fontIndex) {
            Microsoft::WRL::ComPtr<IDWriteFont> font;
            if (FAILED(family->GetFont(fontIndex, font.ReleaseAndGetAddressOf()))) {
                continue;
            }
            Microsoft::WRL::ComPtr<IDWriteLocalizedStrings> names;
            BOOL exists = FALSE;
            if (FAILED(font->GetInformationalStrings(
                    DWRITE_INFORMATIONAL_STRING_WIN32_FAMILY_NAMES,
                    names.ReleaseAndGetAddressOf(),
                    &exists))
                || !exists) {
                continue;
            }
            if (localizedStringsContain(names.Get(), familyName)) {
                match = font;
                break;
            }
        }
    }
    cachedFonts.emplace(familyName, match);
    return match;
}

// Look the family up by name in one collection and weight-match a probe font
// within it; the unified resolution re-enumerates exact faces itself.
Microsoft::WRL::ComPtr<IDWriteFont> tryFamilyFont(
    IDWriteFontCollection *collection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    Microsoft::WRL::ComPtr<IDWriteFontFamily> *familyOut
) {
    UINT32 familyIndex = 0;
    BOOL exists = FALSE;
    if (FAILED(collection->FindFamilyName(familyName.c_str(), &familyIndex, &exists))
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
    if (familyOut != nullptr) {
        *familyOut = family;
    }
    return font;
}

Microsoft::WRL::ComPtr<IDWriteFontFace> faceFromFont(
    IDWriteFont *font,
    DWRITE_FONT_SIMULATIONS simulations
) {
    if (font == nullptr) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFont3> font3;
    if (SUCCEEDED(font->QueryInterface(IID_PPV_ARGS(font3.ReleaseAndGetAddressOf())))
        && font3) {
        Microsoft::WRL::ComPtr<IDWriteFontFaceReference> reference;
        if (SUCCEEDED(font3->GetFontFaceReference(reference.ReleaseAndGetAddressOf()))
            && reference) {
            Microsoft::WRL::ComPtr<IDWriteFontFace3> face3;
            if (SUCCEEDED(reference->CreateFontFaceWithSimulations(
                    simulations,
                    face3.ReleaseAndGetAddressOf()))) {
                return face3;
            }
        }
    }
    Microsoft::WRL::ComPtr<IDWriteFontFace> face;
    if (SUCCEEDED(font->CreateFontFace(face.ReleaseAndGetAddressOf()))) {
        return face;
    }
    return {};
}

// Variable-font path: if the matched face's font resource exposes a wght
// axis, create the true axis-value instance (DirectWrite clamps the value to
// the axis range, matching QFont.setVariableAxis).  Returns null for static
// fonts so the caller falls through to the static rules.
Microsoft::WRL::ComPtr<IDWriteFontFace> axisWeightFace(
    IDWriteFontFace *probeFace,
    int weight
) {
    if (probeFace == nullptr) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontFace5> face5;
    if (FAILED(probeFace->QueryInterface(IID_PPV_ARGS(face5.ReleaseAndGetAddressOf())))
        || !face5) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontResource> resource;
    if (FAILED(face5->GetFontResource(resource.ReleaseAndGetAddressOf())) || !resource) {
        return {};
    }
    const UINT32 axisCount = resource->GetFontAxisCount();
    if (axisCount == 0 || axisCount > 32) {
        return {};
    }
    DWRITE_FONT_AXIS_VALUE defaults[32]{};
    if (FAILED(resource->GetDefaultFontAxisValues(defaults, axisCount))) {
        return {};
    }
    for (UINT32 index = 0; index < axisCount; ++index) {
        if (defaults[index].axisTag != DWRITE_FONT_AXIS_TAG_WEIGHT) {
            continue;
        }
        DWRITE_FONT_AXIS_VALUE value{};
        value.axisTag = DWRITE_FONT_AXIS_TAG_WEIGHT;
        value.value = static_cast<float>(std::clamp(weight, 1, 1000));
        // Unspecified axes keep their defaults; the metrics face below uses
        // the same resource with no axis overrides.
        Microsoft::WRL::ComPtr<IDWriteFontFace5> axisFace;
        if (SUCCEEDED(resource->CreateFontFace(
                DWRITE_FONT_SIMULATIONS_NONE,
                &value,
                1,
                axisFace.ReleaseAndGetAddressOf()))) {
            return axisFace;
        }
        return {};
    }
    return {};
}

Microsoft::WRL::ComPtr<IDWriteFontFace> defaultAxisFace(IDWriteFontFace *probeFace) {
    // Default-instance face: its GetMetrics report the static OS/2 table
    // values (no MVAR adjustment), which is what QFontMetrics uses on the
    // CPU side regardless of the selected instance.
    if (probeFace == nullptr) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontFace5> face5;
    if (FAILED(probeFace->QueryInterface(IID_PPV_ARGS(face5.ReleaseAndGetAddressOf())))
        || !face5) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontResource> resource;
    if (FAILED(face5->GetFontResource(resource.ReleaseAndGetAddressOf())) || !resource) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontFace5> face;
    if (SUCCEEDED(resource->CreateFontFace(
            DWRITE_FONT_SIMULATIONS_NONE,
            nullptr,
            0,
            face.ReleaseAndGetAddressOf()))) {
        return face;
    }
    return {};
}

ResolvedFontFaces resolveUnifiedFaces(
    IDWriteFont *matchedFont,
    IDWriteFontFamily *family,
    int weight,
    bool italic,
    bool axisHint
) {
    ResolvedFontFaces result;
    Microsoft::WRL::ComPtr<IDWriteFontFace> probeFace;
    if (FAILED(matchedFont->CreateFontFace(probeFace.ReleaseAndGetAddressOf()))) {
        return result;
    }

    // 可变字体：轴值即请求字重（DirectWrite 会钳制到轴范围）。axisHint
    // 由 Python 侧统一解析下发——这台 Win11 的 DWrite 对静态字体也报告
    // wght 标准轴，凭 GetFontAxisCount 判可变会把静态族全部劫持进恒定的
    // 轴实例；只有 Python 侧实测（轴两端指纹不同）确认的真可变字体才走这里。
    if (axisHint) {
        if (auto axisFace = axisWeightFace(probeFace.Get(), weight)) {
            result.outline = axisFace;
            result.metrics = defaultAxisFace(probeFace.Get());
            if (!result.metrics) {
                result.metrics = axisFace;
            }
            return result;
        }
    }

    struct FaceEntry {
        int weight;
        bool italic;
        Microsoft::WRL::ComPtr<IDWriteFont> font;
    };
    std::vector<FaceEntry> faces;
    if (family != nullptr) {
        const UINT32 count = family->GetFontCount();
        faces.reserve(count);
        for (UINT32 index = 0; index < count; ++index) {
            Microsoft::WRL::ComPtr<IDWriteFont> font;
            if (FAILED(family->GetFont(index, font.ReleaseAndGetAddressOf()))) {
                continue;
            }
            faces.push_back({
                static_cast<int>(font->GetWeight()),
                font->GetStyle() == DWRITE_FONT_STYLE_ITALIC,
                font
            });
        }
    }
    if (faces.empty()) {
        faces.push_back({
            static_cast<int>(matchedFont->GetWeight()),
            matchedFont->GetStyle() == DWRITE_FONT_STYLE_ITALIC,
            Microsoft::WRL::ComPtr<IDWriteFont>(matchedFont)
        });
    }

    // Italic requests select among italic faces (and upright requests among
    // upright faces); a family without a matching face falls back to the
    // other set, mirroring QFontDatabase-driven selection on the CPU side.
    std::vector<FaceEntry> matchingStyle;
    for (const FaceEntry &entry : faces) {
        if (entry.italic == italic) {
            matchingStyle.push_back(entry);
        }
    }
    if (matchingStyle.empty()) {
        matchingStyle = std::move(faces);
    } else {
        faces = std::move(matchingStyle);
    }

    // 引擎镜像（与 CPU 侧 weight_resolver._engine_face 同一规则，2026-10-08
    // QFontInfo/像素实测校准）：精确命中 → 该 face；缺档 → 就近匹配
    // （|face−W| 最小，平局取更轻 face）；合成粗体 = 请求≥600 且匹配
    // face<600（粗 face 不可再加粗）。
    const FaceEntry *chosen = nullptr;
    DWRITE_FONT_SIMULATIONS simulations = DWRITE_FONT_SIMULATIONS_NONE;
    const auto exact = std::find_if(
        faces.begin(), faces.end(),
        [&](const FaceEntry &entry) { return entry.weight == weight; }
    );
    if (exact != faces.end()) {
        chosen = &*exact;
    } else {
        chosen = &*std::min_element(
            faces.begin(), faces.end(),
            [&](const FaceEntry &lhs, const FaceEntry &rhs) {
                const int lhsDistance = std::abs(lhs.weight - weight);
                const int rhsDistance = std::abs(rhs.weight - weight);
                if (lhsDistance != rhsDistance) {
                    return lhsDistance < rhsDistance;
                }
                return lhs.weight < rhs.weight;
            }
        );
    }
    if (weight >= 600 && chosen->weight < 600) {
        simulations = DWRITE_FONT_SIMULATIONS_BOLD;
    }
    result.outline = faceFromFont(chosen->font.Get(), simulations);
    // Vertical metrics always come from the unsimulated base face: DWrite's
    // simulated faces keep the static ascent/descent, but stay explicit so a
    // future DWrite change cannot silently fork the two backends.
    result.metrics = simulations == DWRITE_FONT_SIMULATIONS_NONE
        ? result.outline
        : faceFromFont(chosen->font.Get(), DWRITE_FONT_SIMULATIONS_NONE);
    if (!result.metrics) {
        result.metrics = result.outline;
    }
    return result;
}

}  // namespace

ResolvedFontFaces resolveFontFaces(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    bool axisHint
) {
    if (familyName.empty()) {
        return {};
    }
    // Qt's font database offers both spellings: GDI-compatible per-weight
    // families (``Yu Gothic UI Semibold``) and the merged typographic
    // families DirectWrite groups variable fonts under (``Segoe UI
    // Variable``).  The classic system collection only knows the former, so
    // without the typographic collection a variable-font family silently
    // fell through to the caller's default-font substitution.
    if (typographicCollection != nullptr && typographicCollection != collection) {
        Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
        if (auto font = tryFamilyFont(
                typographicCollection, familyName, weight, italic, &family)) {
            return resolveUnifiedFaces(
                font.Get(), family.Get(), weight, italic, axisHint);
        }
    }
    {
        Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
        if (auto font = tryFamilyFont(
                collection, familyName, weight, italic, &family)) {
            return resolveUnifiedFaces(
                font.Get(), family.Get(), weight, italic, axisHint);
        }
    }
    if (auto font = findFontByGdiFamilyName(collection, familyName)) {
        return resolveUnifiedFaces(
            font.Get(), nullptr, weight, italic, axisHint);
    }
    return {};
}

Microsoft::WRL::ComPtr<IDWriteFontFace> createFontFace(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    bool axisHint
) {
    return resolveFontFaces(
        collection, typographicCollection, familyName, weight, italic, axisHint
    ).outline;
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
                collection, nullptr, L"Segoe UI Symbol", DWRITE_FONT_WEIGHT_NORMAL, false))) {
            return face;
        }
    }
    if (auto face = tryFace(createFontFace(
            collection, nullptr, L"Microsoft JhengHei", DWRITE_FONT_WEIGHT_BOLD, false))) {
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
