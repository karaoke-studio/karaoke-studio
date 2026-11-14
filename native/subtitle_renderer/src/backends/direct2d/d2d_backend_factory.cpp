#include "../backend_factory.h"
#include "d2d_backend.h"
namespace krok::subtitle::native {
std::unique_ptr<RenderBackend> makeBackend(const Direct2DOptions& options) {
    if (options.adapter != Direct2DAdapter::Hardware && options.adapter != Direct2DAdapter::Warp)
        throw BackendError("无效的 Direct2D 设备选择");
    return std::make_unique<Direct2DGpuBackend>(options.adapter == Direct2DAdapter::Warp);
}
}
