#pragma once
#include "../../model/render_scene.h"
#include "core/prepared_scene.h"
namespace krok::subtitle::native {
void validateSkiaScene(const RenderScene&, bool canonical = false);
::subtitle::skia::SubtitleInput skiaSceneInput(const RenderScene& scene);
}
