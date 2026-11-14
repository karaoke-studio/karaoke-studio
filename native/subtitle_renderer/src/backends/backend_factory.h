#pragma once
#include "backend_selection.h"
#include "render_backend.h"
#include <memory>
namespace krok::subtitle::native {
// Each enabled backend implements its own typed overload; WARP belongs only to Direct2D.
std::unique_ptr<RenderBackend> makeBackend(const Direct2DOptions& options);
std::unique_ptr<RenderBackend> makeBackend(const SkiaOptions& options);
}
