#include "d2d_font_fallback.h"

#include "../../diagnostics/native_trace.h"

#include <dwrite_3.h>
#include <winnls.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cwchar>
#include <unordered_map>
#include <utility>

namespace krok::subtitle::native::direct2d {

// 族名归一化（定义在本文件后段的 direct2d 作用域，诊断 trace 用）。
std::wstring familyNameOf(IDWriteFontFamily *family);

namespace {

// Unified weight resolution -- mirrors engine/text/font_weight.py on the
// Python side; the two implementations must stay in lockstep.
//
// Variable fonts (fvar wght axis): render the TRUE axis-value instance
// clamped to the axis range; never simulate.  Static fonts follow the
// engine (Qt/DirectWrite observed) matching: (a) exact face, (b) nearest
// face by |weight - request| with ties preferring the lighter face,
// (c) bold simulation only when request >= 600 AND the matched face < 600
// (semibold/bold faces are exempt; observed via Latin-glyph calibration).

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

// face 的**全部本地化** style 名（DWrite FaceNames 的每个条目）。
//
// 只看第一个条目会漏配：带多语言面名的字体会按语言排布（例如 ja-JP ``標準``
// 在前、en-US ``Regular`` 在后），而 Python 决策里的 face style 来自 Qt
// （可能是另一种语言的名字）。用于按决策选实例。
std::vector<std::wstring> faceStyleNamesOf(IDWriteFont *font) {
    std::vector<std::wstring> values;
    if (font == nullptr) {
        return values;
    }
    Microsoft::WRL::ComPtr<IDWriteLocalizedStrings> names;
    if (FAILED(font->GetFaceNames(names.ReleaseAndGetAddressOf()))
        || !names || names->GetCount() == 0) {
        return values;
    }
    const UINT32 count = names->GetCount();
    values.reserve(count);
    for (UINT32 index = 0; index < count; ++index) {
        UINT32 length = 0;
        if (FAILED(names->GetStringLength(index, &length))) {
            continue;
        }
        std::wstring name(static_cast<std::size_t>(length) + 1, L'\0');
        if (FAILED(names->GetString(index, name.data(), length + 1))) {
            continue;
        }
        name.resize(length);
        values.push_back(std::move(name));
    }
    return values;
}

// 空白归一化（``Semi Bold`` ↔ ``Semibold``；仅收拢空白，不改大小写）。
std::wstring collapseSpaces(const std::wstring &value) {
    std::wstring out;
    out.reserve(value.size());
    bool pendingSpace = false;
    for (const wchar_t ch : value) {
        if (ch == L' ' || ch == L'\t') {
            pendingSpace = !out.empty();
            continue;
        }
        if (pendingSpace) {
            out.push_back(L' ');
            pendingSpace = false;
        }
        out.push_back(ch);
    }
    return out;
}

// face 名等于决策点名的 style 吗（大小写不敏感；再退一步容忍空格差异）。
bool styleNameMatches(
    const std::vector<std::wstring> &names,
    const std::wstring &wanted
) {
    if (wanted.empty()) {
        return false;
    }
    const std::wstring wantedCollapsed = collapseSpaces(wanted);
    for (const std::wstring &name : names) {
        if (name.empty()) {
            continue;
        }
        if (_wcsicmp(name.c_str(), wanted.c_str()) == 0) {
            return true;
        }
        if (_wcsicmp(collapseSpaces(name).c_str(), wantedCollapsed.c_str()) == 0) {
            return true;
        }
    }
    return false;
}

// 诊断摘要：每条截断，最多 4 条。
std::wstring joinNamesForTrace(const std::vector<std::wstring> &names) {
    std::wstring out;
    std::size_t emitted = 0;
    for (const std::wstring &name : names) {
        if (emitted >= 4) {
            out += L",...";
            break;
        }
        if (emitted > 0) {
            out += L",";
        }
        out += name.size() > 24 ? name.substr(0, 24) + L"..." : name;
        ++emitted;
    }
    return out;
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

// Variable-font path: create the true axis-value instance from the requested
// axis set (DirectWrite clamps to each axis range, matching
// QFont.setVariableAxis).  Only the listed axes are overridden -- every other
// axis (opsz / wdth / ...) keeps the font's default, exactly like Qt.
// ``simulations`` carries the synthetic oblique the Python decision asked for
// (a family without an italic face *and* without an ital/slnt axis still needs
// the shear on top of the axis instance).  Returns null when the resource
// exposes none of the requested axes, so the caller falls through to the
// static rules.
Microsoft::WRL::ComPtr<IDWriteFontFace> axisInstanceFace(
    IDWriteFontFace *probeFace,
    const DWRITE_FONT_AXIS_VALUE *requested,
    UINT32 requestedCount,
    DWRITE_FONT_SIMULATIONS simulations = DWRITE_FONT_SIMULATIONS_NONE
) {
    if (probeFace == nullptr || requested == nullptr || requestedCount == 0) {
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
    // 只保留字体确实拥有的轴（DWrite 对未知轴返回失败，白名单化更稳）。
    DWRITE_FONT_AXIS_VALUE applied[32]{};
    UINT32 appliedCount = 0;
    for (UINT32 wanted = 0; wanted < requestedCount && appliedCount < axisCount; ++wanted) {
        for (UINT32 index = 0; index < axisCount; ++index) {
            if (defaults[index].axisTag != requested[wanted].axisTag) {
                continue;
            }
            applied[appliedCount++] = requested[wanted];
            break;
        }
    }
    if (appliedCount == 0) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontFace5> axisFace;
    if (SUCCEEDED(resource->CreateFontFace(
            simulations,
            applied,
            appliedCount,
            axisFace.ReleaseAndGetAddressOf()))) {
        return axisFace;
    }
    return {};
}

// wght-only convenience wrapper（无决策提示时的 axisHint 路径）。
Microsoft::WRL::ComPtr<IDWriteFontFace> axisWeightFace(
    IDWriteFontFace *probeFace,
    int weight,
    DWRITE_FONT_SIMULATIONS simulations = DWRITE_FONT_SIMULATIONS_NONE
) {
    DWRITE_FONT_AXIS_VALUE value{};
    value.axisTag = DWRITE_FONT_AXIS_TAG_WEIGHT;
    value.value = static_cast<float>(std::clamp(weight, 1, 1000));
    return axisInstanceFace(probeFace, &value, 1, simulations);
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
    bool axisHint,
    const ResolvedFaceHint &hint
) {
    ResolvedFontFaces result;
    Microsoft::WRL::ComPtr<IDWriteFontFace> probeFace;
    if (matchedFont == nullptr
        || FAILED(matchedFont->CreateFontFace(probeFace.ReleaseAndGetAddressOf()))
        || !probeFace) {
        return result;
    }

    // Python 决策带 face style 时按 style 选实例（同族同字重不同设计、或静态与
    // 可变共存时，按字重取第一个匹配项可能选到别的 face）；只在需要时读 face 名。
    const bool styleRequested = hint.present && !hint.style.empty();
    struct FaceEntry {
        int weight;
        bool italic;
        std::vector<std::wstring> styleNames;
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
                styleRequested ? faceStyleNamesOf(font.Get()) : std::vector<std::wstring>{},
                font
            });
        }
    }
    if (faces.empty()) {
        faces.push_back({
            static_cast<int>(matchedFont->GetWeight()),
            matchedFont->GetStyle() == DWRITE_FONT_STYLE_ITALIC,
            styleRequested ? faceStyleNamesOf(matchedFont) : std::vector<std::wstring>{},
            Microsoft::WRL::ComPtr<IDWriteFont>(matchedFont)
        });
    }
    // 持 face 本身而不是向量内指针：斜体池过滤会把 ``faces`` 整体替换，
    // 指向旧缓冲的裸指针会悬垂（实测直接导致 build face 失败、整帧降级）。
    Microsoft::WRL::ComPtr<IDWriteFont> styleMatchedFont;
    int styleMatchedWeight = 0;
    bool styleMatchedItalic = false;
    std::size_t facesWithNames = 0;
    if (styleRequested) {
        for (const FaceEntry &entry : faces) {
            if (!entry.styleNames.empty()) {
                ++facesWithNames;
            }
            if (styleNameMatches(entry.styleNames, hint.style)) {
                styleMatchedFont = entry.font;
                styleMatchedWeight = entry.weight;
                styleMatchedItalic = entry.italic;
                break;
            }
        }
    }
    // 决策点名了 face 却没有这个名字（或字体没提供面名）：降级原因在这里统一
    // 生成一次，供**轴实例分支与字重分支共用**——历史实现只在字重分支、且只在
    // 字重/斜体也不一致时才记；轴分支提前 return 会整段漏掉诊断。
    const auto traceUnmatchedStyle = [&](const char *stage) {
        if (!styleRequested || styleMatchedFont) {
            return;
        }
        std::wstring available;
        for (const FaceEntry &entry : faces) {
            if (available.size() > 128) {
                available += L",...";
                break;
            }
            if (!available.empty()) {
                available += L"|";
            }
            available += joinNamesForTrace(entry.styleNames);
        }
        krok::subtitle::native::diagnostics::nativeTrace(
            "font: %s style '%ls' unmatched in family '%ls' (faces=%zu withNames=%zu names=[%ls])\n",
            stage,
            hint.style.c_str(),
            family != nullptr ? familyNameOf(family).c_str() : L"<gdi-name>",
            faces.size(),
            facesWithNames,
            available.c_str()
        );
    };


    // 可变字体：轴值即请求字重（DirectWrite 会钳制到轴范围）。axisHint /
    // hint 由 Python 侧统一解析下发——这台 Win11 的 DWrite 对静态字体也报告
    // wght 标准轴，凭 GetFontAxisCount 判可变会把静态族全部劫持进恒定的
    // 轴实例；只有 Python 侧实测（轴两端指纹不同）确认的真可变字体才走这里。
    // 模拟倾斜要**叠加在轴实例上**（族内无斜体 face 时轴只管字重，倾斜来自
    // PyQt 同源的 OBLIQUE 模拟）——决策是「轴 + 合成倾斜」时漏掉倾斜会让
    // 斜体在 GPU 上不倾斜（CPU 侧 QFont 合成），逐像素对照实测差 10px 墨迹
    // 盒宽度。
    const DWRITE_FONT_SIMULATIONS hintSimulations =
        static_cast<DWRITE_FONT_SIMULATIONS>(
            (hint.present && hint.syntheticItalic ? DWRITE_FONT_SIMULATIONS_OBLIQUE : 0)
            | (hint.present && hint.syntheticBold ? DWRITE_FONT_SIMULATIONS_BOLD : 0)
        );
    // 决策可同时带 wght 与斜体轴（ital / slnt）：同一轴实例上一次设全，
    // 其余轴（opsz / wdth / ...）保持字体默认——与 Qt.setVariableAxis 同口径。
    const bool hintHasItalicAxis =
        hint.present && !hint.italicAxisTag.empty();
    const bool hintHasAxisSnapshot = hint.present && !hint.axes.empty();
    if (hint.present && (hint.variable || hintHasItalicAxis || hintHasAxisSnapshot)) {
        const auto tagToValue = [](const std::wstring &tag) -> DWRITE_FONT_AXIS_TAG {
            if (tag.size() != 4) {
                return static_cast<DWRITE_FONT_AXIS_TAG>(0);
            }
            return static_cast<DWRITE_FONT_AXIS_TAG>(DWRITE_MAKE_FONT_AXIS_TAG(
                static_cast<char>(tag[0]),
                static_cast<char>(tag[1]),
                static_cast<char>(tag[2]),
                static_cast<char>(tag[3])
            ));
        };
        DWRITE_FONT_AXIS_VALUE requested[34]{};
        UINT32 requestedCount = 0;
        for (const auto &entry : hint.axes) {
            if (requestedCount >= 34) {
                break;
            }
            const DWRITE_FONT_AXIS_TAG tag = tagToValue(entry.first);
            if (tag == static_cast<DWRITE_FONT_AXIS_TAG>(0)) {
                continue;
            }
            requested[requestedCount].axisTag = tag;
            requested[requestedCount].value = entry.second;
            ++requestedCount;
        }
        // 轴表存在时它就是全表（含 wght 与斜体轴）；否则按 axis / italic_axis 逐项补。
        if (!hintHasAxisSnapshot && hint.variable) {
            requested[requestedCount].axisTag = DWRITE_FONT_AXIS_TAG_WEIGHT;
            requested[requestedCount].value = static_cast<float>(
                std::lround(hint.axis)
            );
            ++requestedCount;
        }
        if (hintHasItalicAxis && !hintHasAxisSnapshot) {
            DWRITE_FONT_AXIS_VALUE &entry = requested[requestedCount];
            entry.axisTag = static_cast<DWRITE_FONT_AXIS_TAG>(
                tagToValue(hint.italicAxisTag)
            );
            entry.value = hint.italicAxisValue;
            ++requestedCount;
        }
        traceUnmatchedStyle("axis-instance");
        Microsoft::WRL::ComPtr<IDWriteFontFace> axisProbe = probeFace;
        if (styleMatchedFont) {
            Microsoft::WRL::ComPtr<IDWriteFontFace> styledFace;
            if (SUCCEEDED(styleMatchedFont->CreateFontFace(
                    styledFace.ReleaseAndGetAddressOf()
                ))) {
                axisProbe = styledFace;
            }
        }
        if (auto axisFace = axisInstanceFace(
                axisProbe.Get(), requested, requestedCount, hintSimulations
            )) {
            result.outline = axisFace;
            result.metrics = defaultAxisFace(axisProbe.Get());
            if (!result.metrics) {
                result.metrics = axisFace;
            }
            return result;
        }
    } else if (axisHint) {
        if (auto axisFace = axisWeightFace(probeFace.Get(), weight)) {
            result.outline = axisFace;
            result.metrics = defaultAxisFace(probeFace.Get());
            if (!result.metrics) {
                result.metrics = axisFace;
            }
            return result;
        }
    }

    // Italic requests select among italic faces (and upright requests among
    // upright faces); a family without a matching face falls back to the
    // other set, mirroring QFontDatabase-driven selection on the CPU side.
    // Python 决策说「族内无斜体 face，走合成倾斜」时按直立 face 选，倾斜由
    // DirectWrite 的 OBLIQUE 模拟补（与 QFont 合成斜体同源）。
    const bool wantItalic =
        hint.present && (hint.syntheticItalic || hintHasItalicAxis)
            ? false
            : italic;
    std::vector<FaceEntry> matchingStyle;
    for (const FaceEntry &entry : faces) {
        if (entry.italic == wantItalic) {
            matchingStyle.push_back(entry);
        }
    }
    if (matchingStyle.empty()) {
        // 族内无斜体（或无直立）face：回退到全部 face。不能 move 掏空
        // ``faces``——下方精确/就近匹配仍要遍历它（历史 bug：move 后对空
        // 容器解引用 end 迭代器，斜体+无斜体字族 GPU 场景必崩 0xC0000005，
        // 外层只能崩溃降级 CPU）。
    } else {
        faces = std::move(matchingStyle);
    }

    // 引擎镜像（与 CPU 侧 weight_resolver 同一规则，2026-10-08 QFontInfo/
    // 像素实测校准 + 2026-10-11 全档重校准）：精确命中 → 该 face；缺档 →
    // 就近匹配（|face−W| 最小，**平局取更接近 Normal(400) 的 face**——实测
    // Yu Gothic@350 → Regular 而非 Light、@600 → Medium+合成、Noto@200 →
    // Light、Yu Gothic UI@325 → Semilight、Meiryo@550 → Regular）；合成粗体
    // = 请求≥600 且匹配 face<600（600/700 face 豁免）。Python 决策在手时，
    // face 字重与两个合成标志直接取决策值。
    const FaceEntry *chosen = nullptr;
    const int targetWeight = hint.present && !hint.variable && hint.faceWeight > 0
        ? hint.faceWeight
        : weight;
    // 模拟标志（合成加粗/倾斜）由决策给出，先算好——style 命中与字重命中两条路
    // 都要按同一套标志建 face（历史实现里 style 早退会吞掉合成加粗）。
    DWRITE_FONT_SIMULATIONS simulations = DWRITE_FONT_SIMULATIONS_NONE;
    if (hint.present && !hint.variable) {
        if (hint.syntheticBold) {
            simulations = static_cast<DWRITE_FONT_SIMULATIONS>(
                simulations | DWRITE_FONT_SIMULATIONS_BOLD
            );
        }
        if (hint.syntheticItalic && !hintHasItalicAxis) {
            simulations = static_cast<DWRITE_FONT_SIMULATIONS>(
                simulations | DWRITE_FONT_SIMULATIONS_OBLIQUE
            );
        }
    } else if (weight >= 600 && styleMatchedWeight > 0 && styleMatchedWeight < 600) {
        simulations = DWRITE_FONT_SIMULATIONS_BOLD;
    }
    if (styleMatchedFont) {
        // 决策点名了 face（style 命中）：直接用该 face，不再按字重/斜体猜。
        result.outline = faceFromFont(styleMatchedFont.Get(), simulations);
        result.metrics = simulations == DWRITE_FONT_SIMULATIONS_NONE
            ? result.outline
            : faceFromFont(styleMatchedFont.Get(), DWRITE_FONT_SIMULATIONS_NONE);
        if (!result.metrics) {
            result.metrics = result.outline;
        }
        return result;
    } else {
        const auto exact = std::find_if(
            faces.begin(), faces.end(),
            [&](const FaceEntry &entry) { return entry.weight == targetWeight; }
        );
        if (exact != faces.end()) {
            chosen = &*exact;
        } else {
            chosen = &*std::min_element(
                faces.begin(), faces.end(),
                [&](const FaceEntry &lhs, const FaceEntry &rhs) {
                    const int lhsDistance = std::abs(lhs.weight - targetWeight);
                    const int rhsDistance = std::abs(rhs.weight - targetWeight);
                    if (lhsDistance != rhsDistance) {
                        return lhsDistance < rhsDistance;
                    }
                    const int lhsNormal = std::abs(lhs.weight - 400);
                    const int rhsNormal = std::abs(rhs.weight - 400);
                    if (lhsNormal != rhsNormal) {
                        return lhsNormal < rhsNormal;
                    }
                    return lhs.weight < rhs.weight;
                }
            );
        }
        traceUnmatchedStyle(
            chosen->weight != targetWeight || chosen->italic != wantItalic
                ? "weight-fallback-mismatch"
                : "weight-fallback-same-weight"
        );
    }
    if (!hint.present || hint.variable) {
        // 无决策/旧 IR：沿用引擎镜像规则（请求≥600 且命中 face<600 → 合成加粗）。
        simulations = simulations == DWRITE_FONT_SIMULATIONS_NONE
                && weight >= 600 && chosen->weight < 600
            ? DWRITE_FONT_SIMULATIONS_BOLD
            : simulations;
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

ResolvedFontFaces loadFontResource(IDWriteFactory *factory, const ResolvedFaceHint &hint) {
    ResolvedFontFaces result;
    if (factory == nullptr || hint.resourcePath.empty()) return result;
    Microsoft::WRL::ComPtr<IDWriteFontFile> file;
    if (FAILED(factory->CreateFontFileReference(hint.resourcePath.c_str(), nullptr,
                                               file.ReleaseAndGetAddressOf())) || !file) return result;
    BOOL supported = FALSE;
    DWRITE_FONT_FILE_TYPE fileType{};
    DWRITE_FONT_FACE_TYPE faceType{};
    UINT32 count = 0;
    if (FAILED(file->Analyze(&supported, &fileType, &faceType, &count))
        || !supported || count != 1) return result;
    IDWriteFontFile *files[] = {file.Get()};
    Microsoft::WRL::ComPtr<IDWriteFontFace> base;
    if (FAILED(factory->CreateFontFace(faceType, 1, files, 0,
            DWRITE_FONT_SIMULATIONS_NONE, base.ReleaseAndGetAddressOf())) || !base) return result;
    const auto simulations = static_cast<DWRITE_FONT_SIMULATIONS>(
        (hint.syntheticBold ? DWRITE_FONT_SIMULATIONS_BOLD : 0)
        | (hint.syntheticItalic ? DWRITE_FONT_SIMULATIONS_OBLIQUE : 0));
    std::vector<DWRITE_FONT_AXIS_VALUE> axes;
    for (const auto &entry : hint.axes) {
        if (entry.first.size() != 4 || !std::isfinite(entry.second)) return result;
        axes.push_back({static_cast<DWRITE_FONT_AXIS_TAG>(DWRITE_MAKE_FONT_AXIS_TAG(
            entry.first[0], entry.first[1], entry.first[2], entry.first[3])), entry.second});
    }
    if (!axes.empty()) {
        result.outline = axisInstanceFace(base.Get(), axes.data(),
                                          static_cast<UINT32>(axes.size()), simulations);
    } else if (simulations == DWRITE_FONT_SIMULATIONS_NONE) {
        result.outline = base;
    } else {
        factory->CreateFontFace(faceType, 1, files, 0, simulations,
                                result.outline.ReleaseAndGetAddressOf());
    }
    if (result.outline) result.metrics = base;
    return result;
}

ResolvedFontFaces resolveFontFaces(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    bool axisHint,
    const ResolvedFaceHint &hint
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
                font.Get(), family.Get(), weight, italic, axisHint, hint);
        }
    }
    {
        Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
        if (auto font = tryFamilyFont(
                collection, familyName, weight, italic, &family)) {
            return resolveUnifiedFaces(
                font.Get(), family.Get(), weight, italic, axisHint, hint);
        }
    }
    if (auto font = findFontByGdiFamilyName(collection, familyName)) {
        return resolveUnifiedFaces(
            font.Get(), nullptr, weight, italic, axisHint, hint);
    }
    return {};
}

Microsoft::WRL::ComPtr<IDWriteFontFace> createFontFace(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic,
    bool axisHint,
    const ResolvedFaceHint &hint
) {
    return resolveFontFaces(
        collection, typographicCollection, familyName, weight, italic,
        axisHint, hint
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

namespace {

// Combining marks (Mn/Me) in the ranges that matter for lyric text: they
// attach to the preceding base character instead of forming their own cluster.
bool combiningScalar(UINT32 value) {
    return (value >= 0x0300 && value <= 0x036F)
        || (value >= 0x0483 && value <= 0x0489)
        || (value >= 0x0591 && value <= 0x05BD)
        || (value >= 0x0610 && value <= 0x061A)
        || (value >= 0x064B && value <= 0x065F)
        || (value >= 0x0670 && value <= 0x0670)
        || (value >= 0x06D6 && value <= 0x06ED)
        || (value >= 0x0900 && value <= 0x0903)
        || (value >= 0x093C && value <= 0x094D)
        || (value >= 0x0E31 && value <= 0x0E3A)
        || (value >= 0x0E47 && value <= 0x0E4E)
        || (value >= 0x1AB0 && value <= 0x1AFF)
        || (value >= 0x1DC0 && value <= 0x1DFF)
        || (value >= 0x20D0 && value <= 0x20FF)
        || (value >= 0xFE20 && value <= 0xFE2F);
}

// Scalars that legitimately map to glyph 0: combining marks, controls,
// format characters (ZWJ/ZWNJ/BOM/bidi marks) and spaces.  A cluster only
// fails coverage when its *base* scalar has no glyph.
bool optionalBaseScalar(UINT32 value) {
    if (value <= 0x0020 || (value >= 0x007F && value <= 0x009F)) {
        return true;
    }
    if (value == 0x00AD || value == 0xFEFF) {
        return true;
    }
    if (value >= 0x200B && value <= 0x200F) {
        return true;
    }
    if (value >= 0x202A && value <= 0x202E) {
        return true;
    }
    if (value >= 0x2060 && value <= 0x2064) {
        return true;
    }
    return combiningScalar(value);
}

}  // namespace

bool textFullyCovered(
    IDWriteFontFace *face,
    const std::wstring &text,
    std::vector<UINT16> *glyphsOut
) {
    if (face == nullptr) {
        return false;
    }
    const std::vector<UINT32> scalars = unicodeScalars(text);
    if (glyphsOut != nullptr) {
        *glyphsOut = glyphIndices(face, text);
    }
    if (scalars.empty()) {
        return false;
    }
    std::vector<UINT16> glyphs;
    const std::vector<UINT16> *resolved = glyphsOut;
    if (resolved == nullptr) {
        glyphs = glyphIndices(face, text);
        resolved = &glyphs;
    }
    if (resolved->size() != scalars.size()) {
        return false;
    }
    // 逐簇判定：基础码点必须画得出字形；组合符/控制符/空白无字形属正常，
    // 整串全是这类码点时无需绘图（视为覆盖，不触发回退）。
    bool anyBase = false;
    for (std::size_t index = 0; index < scalars.size(); ++index) {
        const UINT32 value = scalars[index];
        if (combiningScalar(value)) {
            continue;
        }
        if (optionalBaseScalar(value)) {
            continue;
        }
        anyBase = true;
        if ((*resolved)[index] == 0) {
            return false;
        }
    }
    return anyBase;
}

// 缺字回退的候选族名不再写死在本文件：CPU(Qt) 实测选中的族名由 Python 侧
// 随 IR 下发（见 findFallbackFontFace），其余交给 DirectWrite 系统字体回退。

std::wstring familyNameOf(IDWriteFontFamily *family) {
    Microsoft::WRL::ComPtr<IDWriteLocalizedStrings> names;
    BOOL exists = FALSE;
    if (FAILED(family->GetFamilyNames(names.ReleaseAndGetAddressOf()))
        || !names || names->GetCount() == 0) {
        return {};
    }
    UINT32 length = 0;
    if (FAILED(names->GetStringLength(0, &length))) {
        return {};
    }
    std::wstring name(static_cast<std::size_t>(length) + 1, L'\0');
    if (FAILED(names->GetString(0, name.data(), length + 1))) {
        return {};
    }
    name.resize(length);
    return name;
}

namespace {

// Minimal IDWriteTextAnalysisSource for a single in-memory string: what
// IDWriteFontFallback::MapCharacters needs to pick a font for a range of it.
// The locale comes from GetUserDefaultLocaleName so the system fallback is
// language-aware (a zh-CN session prefers its own CJK face over a JP one).
class TextSource final : public IDWriteTextAnalysisSource {
public:
    explicit TextSource(std::wstring text) : text_(std::move(text)) {
        wchar_t locale[LOCALE_NAME_MAX_LENGTH]{};
        if (GetUserDefaultLocaleName(locale, LOCALE_NAME_MAX_LENGTH) > 0) {
            locale_ = locale;
        } else {
            locale_ = L"en-us";
        }
    }

    HRESULT STDMETHODCALLTYPE GetTextAtPosition(
        UINT32 position,
        const WCHAR **textString,
        UINT32 *textLength
    ) override {
        if (textString == nullptr || textLength == nullptr) {
            return E_POINTER;
        }
        if (position >= text_.size()) {
            *textString = nullptr;
            *textLength = 0;
        } else {
            *textString = text_.c_str() + position;
            *textLength = static_cast<UINT32>(text_.size() - position);
        }
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetTextBeforePosition(
        UINT32 position,
        const WCHAR **textString,
        UINT32 *textLength
    ) override {
        if (textString == nullptr || textLength == nullptr) {
            return E_POINTER;
        }
        if (position == 0 || position > text_.size()) {
            *textString = nullptr;
            *textLength = 0;
        } else {
            *textString = text_.c_str();
            *textLength = position;
        }
        return S_OK;
    }

    DWRITE_READING_DIRECTION STDMETHODCALLTYPE GetParagraphReadingDirection() override {
        return DWRITE_READING_DIRECTION_LEFT_TO_RIGHT;
    }

    HRESULT STDMETHODCALLTYPE GetLocaleName(
        UINT32 /*position*/,
        UINT32 *textLength,
        const WCHAR **localeName
    ) override {
        if (textLength == nullptr || localeName == nullptr) {
            return E_POINTER;
        }
        *textLength = static_cast<UINT32>(text_.size());
        *localeName = locale_.c_str();
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetNumberSubstitution(
        UINT32 /*position*/,
        UINT32 *textLength,
        IDWriteNumberSubstitution **numberSubstitution
    ) override {
        if (textLength == nullptr || numberSubstitution == nullptr) {
            return E_POINTER;
        }
        *textLength = static_cast<UINT32>(text_.size());
        *numberSubstitution = nullptr;
        return S_OK;
    }

    ULONG STDMETHODCALLTYPE AddRef() override {
        return 1;
    }

    ULONG STDMETHODCALLTYPE Release() override {
        return 1;
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **object) override {
        if (object == nullptr) {
            return E_POINTER;
        }
        if (iid == __uuidof(IDWriteTextAnalysisSource) || iid == __uuidof(IUnknown)) {
            *object = this;
            return S_OK;
        }
        *object = nullptr;
        return E_NOINTERFACE;
    }

private:
    std::wstring text_;
    std::wstring locale_;
};

class ScriptSink final : public IDWriteTextAnalysisSink {
public:
    explicit ScriptSink(std::size_t count) : scripts(count) {}
    std::vector<DWRITE_SCRIPT_ANALYSIS> scripts;
    HRESULT STDMETHODCALLTYPE SetScriptAnalysis(UINT32 start, UINT32 length,
        const DWRITE_SCRIPT_ANALYSIS *value) override {
        if (!value || start > scripts.size() || length > scripts.size() - start)
            return E_INVALIDARG;
        std::fill(scripts.begin() + start, scripts.begin() + start + length, *value);
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetLineBreakpoints(UINT32, UINT32,
        const DWRITE_LINE_BREAKPOINT *) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE SetBidiLevel(UINT32, UINT32, UINT8, UINT8) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE SetNumberSubstitution(UINT32, UINT32,
        IDWriteNumberSubstitution *) override { return S_OK; }
    ULONG STDMETHODCALLTYPE AddRef() override { return 1; }
    ULONG STDMETHODCALLTYPE Release() override { return 1; }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (iid == __uuidof(IUnknown) || iid == __uuidof(IDWriteTextAnalysisSink)) {
            *object = this;
            return S_OK;
        }
        return E_NOINTERFACE;
    }
};

// Family name of a DirectWrite-selected fallback font, preferring the Win32
// (GDI) family name so the caller's resolver finds it in either collection.
std::wstring familyNameOfFont(IDWriteFont *font) {
    Microsoft::WRL::ComPtr<IDWriteLocalizedStrings> win32Names;
    BOOL exists = FALSE;
    if (SUCCEEDED(font->GetInformationalStrings(
            DWRITE_INFORMATIONAL_STRING_WIN32_FAMILY_NAMES,
            win32Names.ReleaseAndGetAddressOf(),
            &exists))
        && exists && win32Names && win32Names->GetCount() > 0) {
        UINT32 length = 0;
        if (SUCCEEDED(win32Names->GetStringLength(0, &length))) {
            std::wstring name(static_cast<std::size_t>(length) + 1, L'\0');
            if (SUCCEEDED(win32Names->GetString(0, name.data(), length + 1))) {
                name.resize(length);
                return name;
            }
        }
    }
    Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
    if (SUCCEEDED(font->GetFontFamily(family.ReleaseAndGetAddressOf())) && family) {
        return familyNameOf(family.Get());
    }
    return {};
}

}  // namespace

HRESULT shapeGlyphRun(IDWriteFactory *factory, IDWriteFontFace *face,
    const std::wstring &text, float size, ShapedGlyphRun &result) {
    result = {};
    if (!factory || !face || text.empty()) return E_INVALIDARG;
    Microsoft::WRL::ComPtr<IDWriteTextAnalyzer> analyzer;
    HRESULT hr = factory->CreateTextAnalyzer(analyzer.ReleaseAndGetAddressOf());
    if (FAILED(hr)) return hr;
    TextSource source(text);
    ScriptSink sink(text.size());
    hr = analyzer->AnalyzeScript(&source, 0, static_cast<UINT32>(text.size()), &sink);
    if (FAILED(hr)) return hr;
    for (UINT32 start = 0; start < text.size();) {
        UINT32 end = start + 1;
        while (end < text.size() && sink.scripts[end].script == sink.scripts[start].script
            && sink.scripts[end].shapes == sink.scripts[start].shapes) ++end;
        const UINT32 length = end - start;
        UINT32 capacity = length * 3 + 16, count = 0;
        std::vector<UINT16> clusters(length), glyphs(capacity);
        std::vector<DWRITE_SHAPING_TEXT_PROPERTIES> textProps(length);
        std::vector<DWRITE_SHAPING_GLYPH_PROPERTIES> glyphProps(capacity);
        hr = analyzer->GetGlyphs(text.data() + start, length, face, FALSE, FALSE,
            &sink.scripts[start], nullptr, nullptr, nullptr, nullptr, 0, capacity,
            clusters.data(), textProps.data(), glyphs.data(), glyphProps.data(), &count);
        if (FAILED(hr)) return hr;
        std::vector<FLOAT> advances(count);
        std::vector<DWRITE_GLYPH_OFFSET> offsets(count);
        hr = analyzer->GetGlyphPlacements(text.data() + start, clusters.data(),
            textProps.data(), length, glyphs.data(), glyphProps.data(), count, face,
            size, FALSE, FALSE, &sink.scripts[start], nullptr, nullptr, nullptr, 0,
            advances.data(), offsets.data());
        if (FAILED(hr)) return hr;
        result.glyphs.insert(result.glyphs.end(), glyphs.begin(), glyphs.begin() + count);
        result.advances.insert(result.advances.end(), advances.begin(), advances.end());
        result.offsets.insert(result.offsets.end(), offsets.begin(), offsets.end());
        start = end;
    }
    return S_OK;
}

// 缺字回退：优先级固定为「Python 决策族名（Qt 实测选字）→ DirectWrite
// 系统字体回退（MapCharacters，带语言/字重/斜体）」。没有按脚本硬编码的
// 候选表，也不复用「上一个字符成功过的族」——同一请求恒得同一结果，缺字
// 观感不再随前一个字符漂移。
FallbackFontFace findFallbackFontFace(
    IDWriteFontFallback *systemFallback,
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &baseFamily,
    const std::wstring &text,
    int weight,
    bool italic,
    const std::wstring &preferredFamily,
    std::vector<UINT16> &glyphs
) {
    auto tryFamily = [&](const std::wstring &familyName) -> FallbackFontFace {
        if (familyName.empty()) {
            return {};
        }
        const auto faces = resolveFontFaces(
            collection, typographicCollection, familyName, weight, italic
        );
        if (!faces.outline) {
            return {};
        }
        std::vector<UINT16> candidate;
        if (!textFullyCovered(faces.outline.Get(), text, &candidate)) {
            return {};
        }
        glyphs = std::move(candidate);
        return {faces.outline, 1.0f};
    };

    if (const auto preferred = tryFamily(preferredFamily); preferred.face) {
        return preferred;
    }

    if (systemFallback != nullptr) {
        TextSource source(text);
        UINT32 mappedLength = 0;
        FLOAT scale = 1.0f;
        Microsoft::WRL::ComPtr<IDWriteFont> mapped;
        const HRESULT hr = systemFallback->MapCharacters(
            &source,
            0,
            static_cast<UINT32>(text.size()),
            collection,
            baseFamily.empty() ? nullptr : baseFamily.c_str(),
            static_cast<DWRITE_FONT_WEIGHT>(std::clamp(weight, 1, 999)),
            italic ? DWRITE_FONT_STYLE_ITALIC : DWRITE_FONT_STYLE_NORMAL,
            DWRITE_FONT_STRETCH_NORMAL,
            &mappedLength,
            mapped.ReleaseAndGetAddressOf(),
            &scale
        );
        if (SUCCEEDED(hr) && mapped && mappedLength >= text.size()
            && std::isfinite(scale) && scale > 0) {
            Microsoft::WRL::ComPtr<IDWriteFontFace> face;
            if (SUCCEEDED(mapped->CreateFontFace(face.ReleaseAndGetAddressOf())) && face
                && textFullyCovered(face.Get(), text, &glyphs)) {
                return {face, scale};
            }
        }
    }

    glyphs.clear();
    return {};
}

}  // namespace krok::subtitle::native::direct2d
