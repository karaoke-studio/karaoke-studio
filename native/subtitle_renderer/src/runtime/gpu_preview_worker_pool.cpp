#include "gpu_preview_worker_pool.h"

#include "../backends/direct2d/d2d_backend.h"
#include "../diagnostics/native_trace.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

namespace {

using krok::subtitle::native::diagnostics::nativeTrace;

}  // namespace


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
        // follower 装配线程可能正启动其 backend 的预热线程——先取消预热
        // 再 join，收尾顺序与 pause() 一致。
        for (auto &backend : backends_) {
            backend->cancelRealizationPrewarm();
        }
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
            // 有界排空等待：并发使用共享 D3D 立即上下文的 UB 可能把某个
            // 渲染任务永久卡死（2026-10 拖大复现：worker 卡在 renderFrame
            // 内，pause 无限等 outstanding → resize 主循环全哑 → 宿主只能
            // 靠进程级超时重启）。超时后放弃等待并标记不健康：调用方
            // （configureGpuPreviewPool）会废弃本池重建，卡死的线程随
            // abandon() detach、资源随进程退出回收。
            if (!drained_.wait_for(
                    lock,
                    std::chrono::seconds(2),
                    [this]() { return outstanding_ == 0; }
                )) {
                pauseTimedOut_ = true;
                nativeTrace(
                    "pool pause DRAIN TIMEOUT outstanding=%d",
                    outstanding_
                );
                // 不健康直接返回：下方 follower join 与 cancelRealization-
                // Prewarm 在此状态下会永久等待——cancelRealizationPrewarm
                // join 各 backend 的 realization 预热线程，而预热线程等
                // 着被卡死 worker 持有的 realizationMutex（worker 又卡在
                // GPU 内），三层等待链把主线程（命令循环）一并拖死、
                // sidecar 全哑（2026-10 拖大楔死的主线程侧根因）。调用方
                // 对不健康池走 abandon+重建，本池剩余线程随进程退出回收。
                return;
            }
        }
        // 先取消各 backend 的 realization 预热、再 join follower 装配线程：
        // follower 装配会启动各自 backend 的预热线程，先停预热让装配线程
        // 内的 configure 尽早返回（顺序与析构一致；b 方案下无互等死锁，
        // 此顺序仅为收尾更快）。
        for (auto &backend : backends_) {
            backend->cancelRealizationPrewarm();
        }
        if (followerConfigureThread_.joinable()) {
            followerConfigureThread_.join();
        }
    }

    bool healthy() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return !pauseTimedOut_;
    }

    void abandon() noexcept {
        // 不健康池的终局处置：卡死的 worker 线程无法安全 join（会拖死
        // 调用方），也无法安全析构（std::thread joinable → terminate；
        // backends_ 释放后卡死线程醒来 → UAF）。detach 全部线程并整体
        // 泄漏 Impl：对象永不析构，卡死线程引用的内存始终有效，泄漏
        // 存活到进程退出（宿主随后会重启本进程）。
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
            cancelFollowerConfigure_ = true;
            outstanding_ = 0;
            queue_.clear();
            ready_.notify_all();
            drained_.notify_all();
            followerReady_.notify_all();
        }
        if (followerConfigureThread_.joinable()) {
            followerConfigureThread_.detach();
        }
        for (auto &worker : workers_) {
            if (worker.joinable()) {
                worker.detach();
            }
        }
        workers_.clear();
        abandoned_ = true;
        nativeTrace("pool ABANDONED (leaked until process exit)");
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
            realizationPublicationReady_ = false;
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
            realizationPublicationReady_ = false;
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
            nativeTrace(
                "submit rejected stopping=%d accepting=%d outstanding=%d workers=%d",
                stopping_ ? 1 : 0,
                accepting_ ? 1 : 0,
                outstanding_,
                workerCount_
            );
            return false;
        }
        // Each frame snapshots this gate when drawing. Publication checks the
        // snapshot again so older raw frames cannot follow the transition.
        refreshRealizationPoolReadyLocked();
        queue_.push_back(std::move(work));
        ++outstanding_;
        maxOutstanding_ = std::max(maxOutstanding_, outstanding_);
        nativeTrace("submit accepted outstanding=%d queued=%zu", outstanding_, queue_.size());
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
    // Called with mutex_ held. Wait for follower assembly and all active
    // prewarmers; failed followers are excluded after assembly has finished.
    void refreshRealizationPoolReadyLocked() {
        // Pending followers must join before the gate can open; otherwise a
        // newly configured raw worker could close an already opened gate.
        bool allReady = readyWorkerCount_ > 0 && !followersPending_;
        for (int index = 0; index < readyWorkerCount_; ++index) {
            if (!backends_[static_cast<std::size_t>(index)]
                     ->realizationPrewarmComplete()) {
                allReady = false;
                break;
            }
        }
        for (int index = 0; index < readyWorkerCount_; ++index) {
            backends_[static_cast<std::size_t>(index)]
                ->setRealizationPoolReady(allReady);
        }
        if (allReady && backends_.front()->realizationPathReady()) {
            realizationPublicationReady_ = true;
        }
        nativeTrace(
            "pool gate refresh ready=%d allReady=%d",
            readyWorkerCount_, allReady ? 1 : 0
        );
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

    bool submitStalled() const {
        std::lock_guard<std::mutex> lock(mutex_);
        if (lastCompletionMs_ == 0 || outstanding_ <= 0) {
            return false;
        }
        const auto nowMs = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()
        ).count();
        return nowMs - lastCompletionMs_ > 4000;
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
        {
            std::lock_guard<std::mutex> lock(mutex_);
            followersPending_ = true;
            refreshRealizationPoolReadyLocked();
        }
        followerConfigureThread_ = std::thread([this, scene]() {
            struct AssemblyGuard {
                Impl *pool;
                ~AssemblyGuard() {
                    std::lock_guard<std::mutex> lock(pool->mutex_);
                    pool->followersPending_ = false;
                }
            } assemblyGuard{this};
            nativeTrace("follower wait first frame");
            {
                std::unique_lock<std::mutex> lock(mutex_);
                followerReady_.wait(lock, [this]() {
                    return stopping_ || cancelFollowerConfigure_
                        || firstFrameDelivered_;
                });
                if (stopping_ || cancelFollowerConfigure_) {
                    nativeTrace("follower wait exit cancelled=%d", cancelFollowerConfigure_ ? 1 : 0);
                    return;
                }
                followerReady_.wait_for(
                    lock,
                    std::chrono::milliseconds(250),
                    [this]() { return stopping_ || cancelFollowerConfigure_; }
                );
                if (stopping_ || cancelFollowerConfigure_) {
                    nativeTrace("follower grace exit cancelled=%d", cancelFollowerConfigure_ ? 1 : 0);
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
                nativeTrace("follower configure begin backend=%zu", index);
                try {
                    // b 方案（2026-10）：follower 装配完立即上岗，不等待预热。
                    // 跨 worker 的像素一致性由「全池同步门」保证——任一在岗
                    // worker 预热未完成则全池（含主 worker）统一走原路径直描，
                    // follower 装配结束且全员完成后一起切 realization 网格。清掉 defer
                    // 标志让 follower 的预热随 configure 立即启动（fresh
                    // backend 从未渲染、EMA 为 0，自适应调度不会因压力让路
                    // 卡死）；其预热的推进不影响上岗与吞吐。
                    krok::subtitle::native::RenderScene followerScene = scene;
                    followerScene.deferRealizationPrewarmUntilFirstFrame = false;
                    backends_[index]->configure(followerScene);
                } catch (...) {
                    nativeTrace("follower configure FAILED backend=%zu", index);
                    return;
                }
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (stopping_ || cancelFollowerConfigure_) {
                        nativeTrace("follower configure aborted backend=%zu", index);
                        return;
                    }
                    // Fresh backends default to an open local gate. Close
                    // it before making this follower eligible for work.
                    backends_[index]->setRealizationPoolReady(false);
                    readyWorkerCount_ = static_cast<int>(index + 1);
                }
                nativeTrace("follower ready backend=%zu readyWorkers=%d", index, index + 1);
                ready_.notify_all();
            }
        });
    }

    void workerLoop(int workerIndex) {
        while (true) {
            Work work;
            std::size_t queuedCount = 0;
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
                queuedCount = queue_.size();
            }
            nativeTrace("worker %d task begin queued=%zu", workerIndex, queuedCount);
            const auto taskStarted = std::chrono::steady_clock::now();
            QJsonObject result = work(
                *backends_[static_cast<std::size_t>(workerIndex)], workerIndex
            );
            const auto taskElapsedMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - taskStarted
            ).count();
            {
                std::lock_guard<std::mutex> lock(mutex_);
                refreshRealizationPoolReadyLocked();
                if (result.value(QStringLiteral("event")) == QStringLiteral("gpu_frame_ready")) {
                    result.insert(QStringLiteral("worker_count_ready"), readyWorkerCount_);
                    result.insert(QStringLiteral("realization_ready"), realizationPublicationReady_);
                    if (realizationPublicationReady_
                        && !result.value(QStringLiteral("realization_path_ready")).toBool()) {
                        result.insert(QStringLiteral("event"), QStringLiteral("gpu_frame_dropped"));
                        result.insert(QStringLiteral("dropped"), true);
                        result.insert(QStringLiteral("reason"), QStringLiteral("realization_ready"));
                    }
                }
                --outstanding_;
                lastCompletionMs_ = std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now().time_since_epoch()
                ).count();
                firstFrameDelivered_ = true;
                followerReady_.notify_all();
                if (outstanding_ == 0) {
                    drained_.notify_all();
                }
                // Release credit before publishing, but serialize the gate
                // check and publication against other workers/configuration.
                publish_(result);
            }
            nativeTrace("worker %d task end %lldms", workerIndex, taskElapsedMs);
            nativeTrace(
                "worker %d published %s serial=%d",
                workerIndex,
                result.value(QStringLiteral("event")).toString().toUtf8().constData(),
                result.value(QStringLiteral("request_serial")).toInt()
            );
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
    bool followersPending_ = false;
    bool realizationPublicationReady_ = false;
    bool pauseTimedOut_ = false;
    bool abandoned_ = false;
    int readyWorkerCount_ = 0;
    int outstanding_ = 0;
    int maxOutstanding_ = 0;
    // 最近一次任务完成时刻（steady 毫秒）。submit 被拒时若距它超过阈值，
    // 说明 in-flight 槽被永久卡死的 worker 占据——是池死亡信号而非流控。
    long long lastCompletionMs_ = 0;
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

bool GpuPreviewWorkerPool::healthy() const noexcept {
    return impl_ != nullptr && impl_->healthy();
}

void GpuPreviewWorkerPool::abandon() noexcept {
    // 整体泄漏 Impl：卡死线程仍引用其内存，析构即 UAF/terminate。
    if (impl_ == nullptr) {
        return;
    }
    impl_->abandon();
    impl_.release();
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

bool GpuPreviewWorkerPool::submitStalled() const {
    return impl_ != nullptr && impl_->submitStalled();
}

BackendCaps GpuPreviewWorkerPool::capabilities() const {
    return impl_->capabilities();
}

BackendDiagnostics GpuPreviewWorkerPool::diagnostics() const {
    return impl_->diagnostics();
}

}  // namespace krok::subtitle::native::runtime
