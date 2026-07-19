#pragma once

#include "../render_backend.h"
#include "d2d_device.h"

#include <memory>

namespace krok::subtitle::native {

class Direct2DGpuBackend final : public RenderBackend {
public:
    explicit Direct2DGpuBackend(bool forceWarp);
    ~Direct2DGpuBackend() override;

    BackendCaps capabilities() const override;
    ProbeResult renderProbe(const ProbeOptions &options) override;
    void configure(const RenderScene &scene) override;
    ProbeResult renderFrame(int tMs) override;

private:
    struct Impl;
    D2DDevice device_;
    std::unique_ptr<Impl> impl_;
};

}  // namespace krok::subtitle::native
