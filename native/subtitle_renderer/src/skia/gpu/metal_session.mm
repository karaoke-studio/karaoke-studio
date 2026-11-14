#include "gpu/gpu_session.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include "include/gpu/ganesh/mtl/GrMtlBackendContext.h"
#include "include/gpu/ganesh/mtl/GrMtlDirectContext.h"
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdexcept>

namespace subtitle::skia {
namespace {
class MetalSession final : public GpuSession {
public:
    ~MetalSession() override { context_.reset(); }
    void initialize(bool requireHardware) {
        @autoreleasepool {
            device_ = MTLCreateSystemDefaultDevice();
            if (!device_) throw std::runtime_error("Metal device unavailable");
            // Metal devices can be virtual in hosted environments. Keep that evidence explicit.
            NSString* name = device_.name;
            const bool virtualDevice = [name localizedCaseInsensitiveContainsString:@"virtual"] ||
                                       [name localizedCaseInsensitiveContainsString:@"software"];
            if (requireHardware && virtualDevice)
                throw std::runtime_error("Virtual/software Metal device rejected");
            queue_ = [device_ newCommandQueue];
            if (!queue_) throw std::runtime_error("Metal command queue unavailable");
            GrMtlBackendContext backend;
            backend.fDevice.retain((__bridge GrMTLHandle)device_);
            backend.fQueue.retain((__bridge GrMTLHandle)queue_);
            context_ = GrDirectContexts::MakeMetal(backend);
            if (!context_) throw std::runtime_error("Skia Ganesh Metal initialization failed");
            info_ = {"metal", name.UTF8String,
                     std::string("macOS ") + NSProcessInfo.processInfo.operatingSystemVersionString.UTF8String +
                     ";registryID=" + std::to_string(device_.registryID),
                     "Metal; see OS/SDK fingerprint", virtualDevice ? "virtual" : "hardware", !virtualDevice};
        }
    }
private:
    id<MTLDevice> device_;
    id<MTLCommandQueue> queue_;
};
}
std::unique_ptr<GpuSession> makeGpuSession(bool requireHardware) {
    auto session = std::make_unique<MetalSession>();
    session->initialize(requireHardware);
    return session;
}
}
