#pragma once
#include "include/core/SkRefCnt.h"
#include "include/core/SkSurface.h"
#include <memory>
#include <string>

class GrDirectContext;
namespace subtitle::skia {
struct DeviceInfo {
    std::string api;
    std::string device;
    std::string driver;
    std::string apiVersion;
    std::string type;
    bool hardware = false;
};

// Owned session, single calling thread. Surfaces must be released before the session.
// Platform subclasses release Ganesh before native device/queue/instance objects.
class GpuSession {
public:
    virtual ~GpuSession();
    GpuSession(const GpuSession&) = delete;
    GpuSession& operator=(const GpuSession&) = delete;
    const DeviceInfo& info() const { return info_; }
    sk_sp<SkSurface> makeSurface(int width, int height);
    void finish(SkSurface& surface);
protected:
    GpuSession();
    sk_sp<GrDirectContext> context_;
    DeviceInfo info_;
};
std::unique_ptr<GpuSession> makeGpuSession(bool requireHardware);
}
