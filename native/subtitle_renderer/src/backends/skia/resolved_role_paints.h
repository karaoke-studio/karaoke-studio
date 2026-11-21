#pragma once

#include <span>

namespace subtitle::skia { struct ResolvedLine; }
namespace krok::subtitle::native {
class SkiaImageFillCache;
struct TextLine;
struct TextStyle;

void resolveRoleBodyPaints(::subtitle::skia::ResolvedLine&, const TextLine&, const TextStyle&,
                           std::span<const TextStyle>, SkiaImageFillCache&, float layoutScale);
}
