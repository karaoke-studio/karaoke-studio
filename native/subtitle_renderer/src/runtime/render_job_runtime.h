#pragma once

#include <QtCore/QSet>

#include <atomic>
#include <mutex>
#include <thread>
#include <vector>

namespace krok::subtitle::native::runtime {

class RenderJobRuntime {
public:
    RenderJobRuntime() = default;
    ~RenderJobRuntime() = default;

    RenderJobRuntime(const RenderJobRuntime &) = delete;
    RenderJobRuntime &operator=(const RenderJobRuntime &) = delete;

    bool generationCancelled(int generation);
    void cancelGeneration(int generation);
    void clearGenerationCancel(int generation);
    void remember(std::thread job);
    void joinAll();
    void requestShutdown() noexcept;

private:
    std::mutex cancelMutex_;
    QSet<int> cancelledGenerations_;
    std::atomic<bool> shutdownRequested_{false};
    std::mutex jobsMutex_;
    std::vector<std::thread> jobs_;
};

}  // namespace krok::subtitle::native::runtime
