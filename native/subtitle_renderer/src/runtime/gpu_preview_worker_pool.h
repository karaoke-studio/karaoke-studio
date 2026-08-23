#pragma once

#include "../backends/render_backend.h"

#include <QtCore/QJsonObject>

#include <functional>
#include <memory>

namespace krok::subtitle::native::runtime {

class GpuPreviewWorkerPool {
public:
    using Work = std::function<QJsonObject(RenderBackend &, int)>;
    using Publish = std::function<void(const QJsonObject &)>;

    GpuPreviewWorkerPool(
        bool forceWarp,
        int workerCount,
        bool sharedResources,
        Publish publish
    );
    ~GpuPreviewWorkerPool();

    GpuPreviewWorkerPool(const GpuPreviewWorkerPool &) = delete;
    GpuPreviewWorkerPool &operator=(const GpuPreviewWorkerPool &) = delete;

    void pause();
    void resume(const RenderScene &scene, bool deferFollowers);
    void configure(
        const RenderScene &scene,
        bool waitForRealizations = false,
        bool deferFollowers = false
    );
    bool submit(Work work);

    int workerCount() const noexcept;
    int readyWorkerCount() const noexcept;
    bool sharedResources() const noexcept;
    int maxOutstanding() const noexcept;
    int outstanding() const noexcept;
    BackendCaps capabilities() const;
    BackendDiagnostics diagnostics() const;

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace krok::subtitle::native::runtime

