#include "text_semantics.h"

#include <algorithm>
#include <cwctype>

namespace krok::subtitle::native {

bool isLatinText(const std::wstring &text) {
    if (text.empty()) {
        return false;
    }
    return std::all_of(text.begin(), text.end(), [](wchar_t value) {
        return value >= 0x20 && value <= 0x7e;
    });
}

bool isAsciiAlnumText(const std::wstring &text) {
    bool seen = false;
    for (wchar_t value : text) {
        if (value == L' ' || value == L'\t' || value == L'\r' || value == L'\n') {
            continue;
        }
        seen = true;
        if (!((value >= L'0' && value <= L'9')
            || (value >= L'A' && value <= L'Z')
            || (value >= L'a' && value <= L'z'))) {
            return false;
        }
    }
    return seen;
}

bool isWhitespaceText(const std::wstring &text) {
    return !text.empty() && std::all_of(text.begin(), text.end(), [](wchar_t value) {
        return std::iswspace(static_cast<wint_t>(value)) != 0;
    });
}

bool verticalRotates(const std::wstring &text) {
    static const std::wstring rotated =
        L"\u2190\u2192\u2010\u2011\u2012\u2013\u2014\u2015\u301c\uff5e"
        L"\u3008\u3009\u300a\u300b\u300c\u300d\u300e\u300f\u3010\u3011"
        L"\u3014\u3015\uff08\uff09\uff3b\uff3d\uff5b\uff5d\u30fc\uff70"
        L"<>()[]{}";
    return text.size() == 1 && rotated.find(text.front()) != std::wstring::npos;
}

std::pair<float, float> verticalGlyphOffset(
    const std::wstring &text,
    float cellWidth,
    float cellHeight
) {
    static const std::wstring corner = L"\u3001\u3002\uff0c\uff0e";
    static const std::wstring smallKana =
        L"\u3041\u3043\u3045\u3047\u3049\u3063\u3083\u3085\u3087\u308e"
        L"\u30a1\u30a3\u30a5\u30a7\u30a9\u30c3\u30e3\u30e5\u30e7\u30ee"
        L"\u30f5\u30f6";
    if (text.size() == 1 && corner.find(text.front()) != std::wstring::npos) {
        return {cellWidth * 0.28f, -cellHeight * 0.28f};
    }
    if (text.size() == 1 && smallKana.find(text.front()) != std::wstring::npos) {
        return {cellWidth * 0.10f, -cellHeight * 0.10f};
    }
    return {0.0f, 0.0f};
}

}  // namespace krok::subtitle::native
