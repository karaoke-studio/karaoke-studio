#pragma once
#include "../../model/render_scene.h"
#include "core/prepared_scene.h"
namespace krok::subtitle::native {
struct SkiaPaintCapabilities {
    int outlineLayers = 0;
    bool bodyGradients = false;
    bool outlineGradients = false;
    bool imageFills = false;
    bool staticShadow = false;
    bool staticGlow = false;
};
void validateSkiaScene(const RenderScene&, bool canonical = false, SkiaPaintCapabilities = {});
SkMatrix skiaViewport(const RenderScene&);
::subtitle::skia::SubtitleInput skiaSceneInput(const RenderScene& scene);
::subtitle::skia::StaticShadow skiaStaticShadow(const TextStyle& style);
::subtitle::skia::StaticGlow skiaStaticGlow(const TextStyle& style);
}
