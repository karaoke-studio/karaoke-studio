#pragma once
#include <variant>
namespace krok::subtitle::native {
enum class Direct2DAdapter { Hardware, Warp };
struct Direct2DOptions { Direct2DAdapter adapter = Direct2DAdapter::Hardware; };
enum class SkiaTarget { Gpu, Raster };
struct SkiaOptions {
    SkiaTarget target = SkiaTarget::Gpu;
    bool requireHardware = true; // applies to the GPU target only
};
using BackendSelection = std::variant<Direct2DOptions, SkiaOptions>;
}
