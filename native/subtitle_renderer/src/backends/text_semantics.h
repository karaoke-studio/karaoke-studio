#pragma once

#include <string>
#include <utility>

namespace krok::subtitle::native {

bool isLatinText(const std::wstring &text);
bool isAsciiAlnumText(const std::wstring &text);
bool isWhitespaceText(const std::wstring &text);
bool verticalRotates(const std::wstring &text);

std::pair<float, float> verticalGlyphOffset(
    const std::wstring &text,
    float cellWidth,
    float cellHeight
);

}  // namespace krok::subtitle::native
