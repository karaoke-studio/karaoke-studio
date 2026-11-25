#pragma once
#include "include/core/SkRefCnt.h"
#include "include/core/SkImage.h"
#include "include/core/SkSurface.h"
#include <memory>
#include <stdexcept>
#include <string>

class GrDirectContext;
namespace subtitle::skia {
class GpuDeviceError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

// One asynchronous straight-RGBA readback. The session fills it from Ganesh's
// completion callback; `result` stays valid until the object is released.
struct AsyncStraightRead {
    bool done = false;
    std::unique_ptr<const SkImage::AsyncReadResult> result;
};

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
    void checkDevice() const;
    // Records the conversion of `source` to straight RGBA in `target` with the
    // expression Ganesh's synchronous readPixels uses, then submits an
    // asynchronous transfer of `target`. `read` must outlive its completion;
    // drain() completes every outstanding read.
    void readStraightAsync(SkSurface& source, SkSurface& target, AsyncStraightRead& read);
    // Polls completions until `read` has a result. A null result means the
    // transfer failed or is unsupported.
    void wait(AsyncStraightRead& read);
    void drain();
protected:
    GpuSession();
    sk_sp<GrDirectContext> context_;
    DeviceInfo info_;
};
std::unique_ptr<GpuSession> makeGpuSession(bool requireHardware);
}
