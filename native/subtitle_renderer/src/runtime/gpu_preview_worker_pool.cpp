#include "gpu_preview_worker_pool.h"

#include "../backends/direct2d/d2d_backend.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

namespace krok::subtitle::native::runtime {

class GpuPreviewWorkerPool::Impl {
public:
    using Work = GpuPreviewWorkerPool::Work;
    using Publish = GpuPreviewWorkerPool::Publish;

    Impl(
        bool forceWarp,
        int workerCount,
        bool sharedResources,
        Publish publish
    )
        : forceWarp_(forceWarp),
          workerCount_(std::clamp(workerCount, 1, 8)),
          sharedResources_(sharedResources && workerCount_ > 1),
          publish_(std::move(publish)) {
        backends_.reserve(static_cast<std::size_t>(workerCount_));
        workers_.reserve(static_cast<std::size_t>(workerCount_));
        backends_.push_back(
            std::make_unique<krok::subtitle::native::Direct2DGpuBackend>(forceWarp_)
        );
        for (int index = 1; index < workerCount_; ++index) {
            if (sharedResources_) {
                backends_.push_back(
                    std::make_unique<krok::subtitle::native::Direct2DGpuBackend>(
                        forceWarp_, backends_.front()->sharedDeviceResources()
                    )
                );
                continue;
            }
            backends_.push_back(
                std::make_unique<krok::subtitle::native::Direct2DGpuBackend>(forceWarp_)
            );
        }
        for (int index = 0; index < workerCount_; ++index) {
            workers_.emplace_back([this, index]() { workerLoop(index); });
        }
    }

    ~Impl() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
            cancelFollowerConfigure_ = true;
            queue_.clear();
        }
        ready_.notify_all();
        followerReady_.notify_all();
        if (followerConfigureThread_.joinable()) {
            followerConfigureThread_.join();
        }
        for (auto &worker : workers_) {
            if (worker.joinable()) {
                worker.join();
            }
        }
    }

    void pause() {
        {
            std::unique_lock<std::mutex> lock(mutex_);
            accepting_ = false;
            cancelFollowerConfigure_ = true;
            followerReady_.notify_all();
            outstanding_ -= static_cast<int>(queue_.size());
            queue_.clear();
            if (outstanding_ == 0) {
                drained_.notify_all();
            }
            drained_.wait(lock, [this]() { return outstanding_ == 0; });
        }
        if (followerConfigureThread_.joinable()) {
            followerConfigureThread_.join();
        }
        for (auto &backend : backends_) {
            backend->cancelRealizationPrewarm();
        }
    }

    void resume(
        const krok::subtitle::native::RenderScene &scene,
        bool deferFollowers
    ) {
        bool restartFollowers = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            cancelFollowerConfigure_ = false;
            accepting_ = true;
            restartFollowers = deferFollowers
                && readyWorkerCount_ < workerCount_
                && backends_.size() > 1;
            if (restartFollowers) {
                firstFrameDelivered_ = false;
            }
        }
        if (restartFollowers) {
            startDeferredFollowers(scene);
        }
        ready_.notify_all();
    }

    void configure(
        const krok::subtitle::native::RenderScene &scene,
        bool waitForRealizations = false,
        bool deferFollowers = false
    ) {
        pause();
        {
            std::lock_guard<std::mutex> lock(mutex_);
            cancelFollowerConfigure_ = false;
            readyWorkerCount_ = 0;
            firstFrameDelivered_ = false;
        }
        if (sharedResources_ && scene.realizationEnabled) {
            backends_.front()->configure(scene);
            backends_.front()->waitForRealizationPrewarm();
            backends_.front()->renderFrame(scene.prewarmTimeMs, true);
            krok::subtitle::native::RenderScene followerScene = scene;
            followerScene.realizationEnabled = false;
            for (std::size_t index = 1; index < backends_.size(); ++index) {
                backends_[index]->configure(followerScene);
                backends_[index]->adoptSharedGlyphResources(*backends_.front());
                backends_[index]->renderFrame(scene.prewarmTimeMs, true);
            }
        } else if (waitForRealizations || !deferFollowers) {
            for (auto &backend : backends_) {
                backend->configure(scene);
            }
            if (scene.realizationEnabled) {
                for (auto &backend : backends_) {
                    backend->waitForRealizationPrewarm();
                }
            }
            for (auto &backend : backends_) {
                backend->renderFrame(scene.prewarmTimeMs, true);
            }
        } else {
            // Interactive preview becomes usable as soon as worker zero has
            // its scene.  Followers are expensive independent Direct2D
            // configurations, so bring them online after a short foreground
            // grace period instead of extending the configure response.
            backends_.front()->configure(scene);
            {
                std::lock_guard<std::mutex> lock(mutex_);
                readyWorkerCount_ = 1;
            }
            if (backends_.size() > 1) {
                startDeferredFollowers(scene);
            }
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (waitForRealizations || !deferFollowers
                || (sharedResources_ && scene.realizationEnabled)) {
                readyWorkerCount_ = workerCount_;
            }
            accepting_ = true;
        }
        ready_.notify_all();
    }

    bool submit(Work work) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping_ || !accepting_ || outstanding_ >= workerCount_) {
            return false;
        }
        queue_.push_back(std::move(work));
        ++outstanding_;
        maxOutstanding_ = std::max(maxOutstanding_, outstanding_);
        if (readyWorkerCount_ < workerCount_) {
            ready_.notify_all();
        } else {
            ready_.notify_one();
        }
        return true;
    }

    int workerCount() const noexcept { return workerCount_; }
    int readyWorkerCount() const noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        return readyWorkerCount_;
    }
    bool sharedResources() const noexcept { return sharedResources_; }
    int maxOutstanding() const noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        return maxOutstanding_;
    }
    int outstanding() const noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        return outstanding_;
    }
    krok::subtitle::native::BackendCaps capabilities() const {
        return backends_.front()->capabilities();
    }
    krok::subtitle::native::BackendDiagnostics diagnostics() const {
        std::unique_lock<std::mutex> lock(mutex_);
        drained_.wait(lock, [this]() { return outstanding_ == 0; });
        lock.unlock();
        int readyWorkerCount = 1;
        {
            std::lock_guard<std::mutex> readyLock(mutex_);
            readyWorkerCount = std::max(readyWorkerCount_, 1);
        }
        auto aggregate = backends_.front()->diagnostics();
        for (std::size_t index = 1;
             index < static_cast<std::size_t>(readyWorkerCount); ++index) {
            const auto current = backends_[index]->diagnostics();
            aggregate.estimatedCacheBytes += current.estimatedCacheBytes;
            aggregate.realizationPrewarmComplete =
                aggregate.realizationPrewarmComplete
                && current.realizationPrewarmComplete;
            aggregate.realizationCount += current.realizationCount;
            aggregate.realizationCapacity += current.realizationCapacity;
            aggregate.realizationPrewarmTasks += current.realizationPrewarmTasks;
            aggregate.realizationPrewarmSkipped += current.realizationPrewarmSkipped;
            aggregate.realizationPrewarmMs = std::max(
                aggregate.realizationPrewarmMs,
                current.realizationPrewarmMs
            );
            aggregate.realizationPrewarmFillTasks +=
                current.realizationPrewarmFillTasks;
            aggregate.realizationPrewarmStrokeTasks +=
                current.realizationPrewarmStrokeTasks;
            aggregate.realizationPrewarmContextMs +=
                current.realizationPrewarmContextMs;
            aggregate.realizationPrewarmWaitMs += current.realizationPrewarmWaitMs;
            aggregate.realizationPrewarmFillCreateMs +=
                current.realizationPrewarmFillCreateMs;
            aggregate.realizationPrewarmStrokeCreateMs +=
                current.realizationPrewarmStrokeCreateMs;
            aggregate.realizationPrewarmPublishMs +=
                current.realizationPrewarmPublishMs;
            aggregate.realizationPrewarmCreateP50Ms = std::max(
                aggregate.realizationPrewarmCreateP50Ms,
                current.realizationPrewarmCreateP50Ms
            );
            aggregate.realizationPrewarmCreateP95Ms = std::max(
                aggregate.realizationPrewarmCreateP95Ms,
                current.realizationPrewarmCreateP95Ms
            );
            aggregate.realizationPrewarmCreateMaxMs = std::max(
                aggregate.realizationPrewarmCreateMaxMs,
                current.realizationPrewarmCreateMaxMs
            );
        }
        return aggregate;
    }

private:
    void startDeferredFollowers(
        const krok::subtitle::native::RenderScene &scene
    ) {
        followerConfigureThread_ = std::thread([this, scene]() {
            {
                std::unique_lock<std::mutex> lock(mutex_);
                followerReady_.wait(lock, [this]() {
                    return stopping_ || cancelFollowerConfigure_
                        || firstFrameDelivered_;
                });
                if (stopping_ || cancelFollowerConfigure_) {
                    return;
                }
                followerReady_.wait_for(
                    lock,
                    std::chrono::milliseconds(250),
                    [this]() { return stopping_ || cancelFollowerConfigure_; }
                );
                if (stopping_ || cancelFollowerConfigure_) {
                    return;
                }
            }
            for (std::size_t index = 1; index < backends_.size(); ++index) {
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (static_cast<int>(index) < readyWorkerCount_) {
                        continue;
                    }
                }
                try {
                    backends_[index]->configure(scene);
                } catch (...) {
                    return;
                }
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (stopping_ || cancelFollowerConfigure_) {
                        return;
                    }
                    readyWorkerCount_ = static_cast<int>(index + 1);
                }
                ready_.notify_all();
            }
        });
    }

    void workerLoop(int workerIndex) {
        while (true) {
            Work work;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                ready_.wait(lock, [this, workerIndex]() {
                    return stopping_
                        || (workerIndex < readyWorkerCount_ && !queue_.empty());
                });
                if (stopping_ && queue_.empty()) {
                    return;
                }
                work = std::move(queue_.front());
                queue_.pop_front();
            }
            QJsonObject result = work(
                *backends_[static_cast<std::size_t>(workerIndex)], workerIndex
            );
            {
                std::lock_guard<std::mutex> lock(mutex_);
                --outstanding_;
                firstFrameDelivered_ = true;
                followerReady_.notify_all();
                if (outstanding_ == 0) {
                    drained_.notify_all();
                }
            }
            // Publish only after releasing one in-flight credit.  An export
            // consumer may immediately submit the next frame for the freed
            // ring slot as soon as it receives this response.
            publish_(result);
        }
    }

    bool forceWarp_ = false;
    int workerCount_ = 1;
    bool sharedResources_ = false;
    mutable std::mutex mutex_;
    std::condition_variable ready_;
    mutable std::condition_variable drained_;
    std::condition_variable followerReady_;
    std::deque<Work> queue_;
    std::vector<std::unique_ptr<krok::subtitle::native::Direct2DGpuBackend>> backends_;
    std::vector<std::thread> workers_;
    bool stopping_ = false;
    bool accepting_ = false;
    bool cancelFollowerConfigure_ = false;
    bool firstFrameDelivered_ = false;
    int readyWorkerCount_ = 0;
    int outstanding_ = 0;
    int maxOutstanding_ = 0;
    std::thread followerConfigureThread_;
    Publish publish_;
};

GpuPreviewWorkerPool::GpuPreviewWorkerPool(
    bool forceWarp,
    int workerCount,
    bool sharedResources,
    Publish publish
)
    : impl_(std::make_unique<Impl>(
          forceWarp,
          workerCount,
          sharedResources,
          std::move(publish)
      )) {}

GpuPreviewWorkerPool::~GpuPreviewWorkerPool() = default;

void GpuPreviewWorkerPool::pause() {
    impl_->pause();
}

void GpuPreviewWorkerPool::resume(
    const RenderScene &scene,
    bool deferFollowers
) {
    impl_->resume(scene, deferFollowers);
}

void GpuPreviewWorkerPool::configure(
    const RenderScene &scene,
    bool waitForRealizations,
    bool deferFollowers
) {
    impl_->configure(scene, waitForRealizations, deferFollowers);
}

bool GpuPreviewWorkerPool::submit(Work work) {
    return impl_->submit(std::move(work));
}

int GpuPreviewWorkerPool::workerCount() const noexcept {
    return impl_->workerCount();
}

int GpuPreviewWorkerPool::readyWorkerCount() const noexcept {
    return impl_->readyWorkerCount();
}

bool GpuPreviewWorkerPool::sharedResources() const noexcept {
    return impl_->sharedResources();
}

int GpuPreviewWorkerPool::maxOutstanding() const noexcept {
    return impl_->maxOutstanding();
}

int GpuPreviewWorkerPool::outstanding() const noexcept {
    return impl_->outstanding();
}

BackendCaps GpuPreviewWorkerPool::capabilities() const {
    return impl_->capabilities();
}

BackendDiagnostics GpuPreviewWorkerPool::diagnostics() const {
    return impl_->diagnostics();
}

}  // namespace krok::subtitle::native::runtime
