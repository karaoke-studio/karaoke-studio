#pragma once

#include "../render_backend.h"
#include "d2d_device.h"

namespace krok::subtitle::native {

class Direct2DGpuBackend final : public RenderBackend {
public:
    explicit Direct2DGpuBackend(bool forceWarp);

    BackendCaps capabilities() const override;
    ProbeResult renderProbe(const ProbeOptions &options) override;

private:
    D2DDevice device_;
};

}  // namespace krok::subtitle::native
