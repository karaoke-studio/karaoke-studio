#pragma once

#include "include/core/SkPath.h"
#include <map>
#include <string>

class QJsonArray;
class QJsonValue;

namespace subtitle::skia { struct ResolvedLine; }

namespace krok::subtitle::native {
class SkiaImageFillCache;
struct RenderScene;
struct TextLine;
struct TextStyle;

using SkiaParticleSprites = std::map<std::string, SkPath>;

SkiaParticleSprites resolveSkiaParticleSprites(const RenderScene&);
void resolveSkiaParticleAnchors(const QJsonValue& box, const QJsonArray& features, const TextLine&,
                                const TextStyle&, bool rubyParticles, const SkiaParticleSprites&,
                                SkiaImageFillCache&, double dpr, ::subtitle::skia::ResolvedLine&);
}
