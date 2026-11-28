#pragma once

#include "include/core/SkRect.h"
#include <map>
#include <optional>
#include <span>

class QJsonObject;
namespace subtitle::skia { struct ResolvedLine; }
namespace krok::subtitle::native {
class SkiaImageFillCache;
struct RenderScene;
struct TextLine;
struct TextStyle;

struct RolePaintBounds {
    struct Domain {
        SkRect ink = SkRect::MakeEmpty();
        SkRect rubyInk = SkRect::MakeEmpty();
        SkRect combinedInk = SkRect::MakeEmpty();
        int paintIndex = -1;
    };
    std::map<int, Domain> roles;
    SkRect line = SkRect::MakeEmpty();
    SkRect allRuby = SkRect::MakeEmpty();
    SkRect lineWithRuby = SkRect::MakeEmpty();
    float outlinePad = 0;

    const SkRect& forRole(int role) const;
    const SkRect& forRoleRuby(int role) const;
    const SkRect& forRoleCombined(int role) const;
    std::optional<int> leftmostRole() const;
};

RolePaintBounds collectRolePaintBounds(const ::subtitle::skia::ResolvedLine&, const TextLine&, const TextStyle&,
                                      std::span<const TextStyle>, float layoutScale, bool includeBitmaps = false);
void joinRubyPaintBounds(RolePaintBounds&, const QJsonObject& source, const TextLine&, double dpr);
void resolveRoleBodyPaints(::subtitle::skia::ResolvedLine&, const TextLine&, const TextStyle&,
                          std::span<const TextStyle>, RolePaintBounds&, SkiaImageFillCache&, float layoutScale,
                          bool outlineGradients, bool outlinePaints,
                          const SkRect& canvasBounds = SkRect::MakeEmpty());
void resolveRoleGlows(::subtitle::skia::ResolvedLine&, const TextLine&, const TextStyle&,
                      std::span<const TextStyle>, const RolePaintBounds* gradientBounds, bool bitmapGradients,
                      SkiaImageFillCache&, float layoutScale,
                      const SkRect& canvasBounds = SkRect::MakeEmpty());
// True when a glow of this style paints with an image brush.
bool usesImageGlow(const TextStyle&);
void resolveRoleShadows(::subtitle::skia::ResolvedLine&, const TextLine&, std::span<const TextStyle>,
                        const RolePaintBounds*, SkiaImageFillCache&, float layoutScale,
                        const SkRect& canvasBounds = SkRect::MakeEmpty());
void splitLatinOutlineStyles(RenderScene&, bool primary, bool secondary);
}
