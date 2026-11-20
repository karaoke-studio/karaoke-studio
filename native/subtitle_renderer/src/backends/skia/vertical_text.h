#pragma once

#include <string_view>

namespace krok::subtitle::native {
inline constexpr std::u32string_view verticalSmallKana =
    U"\u3041\u3043\u3045\u3047\u3049\u3063\u3083\u3085\u3087\u308e"
    U"\u30a1\u30a3\u30a5\u30a7\u30a9\u30c3\u30e3\u30e5\u30e7\u30ee\u30f5\u30f6";

inline bool verticalUprightScalar(char32_t code) {
    return (code >= 0x4e00 && code <= 0x9fff) || (code >= 0xac00 && code <= 0xd7a3)
        || (((code >= 0x3041 && code <= 0x3094) || (code >= 0x30a1 && code <= 0x30fa))
            && verticalSmallKana.find(code) == std::u32string_view::npos);
}

inline bool verticalShiftedScalar(char32_t code) {
    return code == 0x3001 || code == 0x3002
        || verticalSmallKana.find(code) != std::u32string_view::npos;
}

inline bool verticalRotatedScalar(char32_t code) {
    constexpr std::u32string_view rotated = U"\u300c\u300d\u300e\u300f\u30fc\u301c";
    return rotated.find(code) != std::u32string_view::npos;
}
}
