#pragma once

#include "include/core/SkRect.h"
#include <map>
#include <span>

namespace subtitle::skia { struct ResolvedLine; }
namespace krok::subtitle::native {
class SkiaImageFillCache;
struct RenderScene;
struct TextLine;
struct TextStyle;

struct RolePaintBounds {
    struct Domain {
        SkRect ink = SkRect::MakeEmpty();
        int paintIndex = -1;
    };
    std::map<int, Domain> roles;
    SkRect line = SkRect::MakeEmpty();

    const SkRect& forRole(int role) const;
};

RolePaintBounds resolveRoleBodyPaints(::subtitle::skia::ResolvedLine&, const TextLine&, const TextStyle&,
                                    std::span<const TextStyle>, SkiaImageFillCache&, float layoutScale);
void resolveRoleGlows(::subtitle::skia::ResolvedLine&, const TextLine&, const TextStyle&, std::span<const TextStyle>);
void splitLatinOutlineStyles(RenderScene&);
}
