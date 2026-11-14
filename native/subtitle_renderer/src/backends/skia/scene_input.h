#pragma once
#include "../../model/render_scene.h"
#include "core/prepared_scene.h"
namespace krok::subtitle::native {
::subtitle::skia::SubtitleInput skiaSceneInput(const RenderScene& scene);
}
