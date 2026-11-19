#pragma once
#include "../../model/render_scene.h"
#include "core/prepared_scene.h"
namespace krok::subtitle::native {
void validateSkiaScene(const RenderScene&, bool canonical = false, int outlineLayers = 0, bool bodyGradients = false,
                       bool outlineGradients = false, bool imageFills = false, bool staticShadow = false);
::subtitle::skia::SubtitleInput skiaSceneInput(const RenderScene& scene);
::subtitle::skia::StaticShadow skiaStaticShadow(const TextStyle& style);
}
