#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>

namespace krok::subtitle::native {

struct BackendCaps {
    std::string backend;
    std::string adapterName;
    std::string featureLevel;
    std::uint32_t adapterVendorId = 0;
    std::uint32_t adapterDeviceId = 0;
    std::uint64_t dedicatedVideoMemory = 0;
    bool hardware = false;
    bool warp = false;
    bool supportsTransparentSurface = false;
    bool supportsStagingReadback = false;
    bool supportsGlyphs = false;
    bool supportsNativePreview = false;
};
class BackendError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

}  // namespace krok::subtitle::native
