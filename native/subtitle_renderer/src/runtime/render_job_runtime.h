#pragma once

#include "generation_cancellation.h"
#include <memory>
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
    std::shared_ptr<GenerationCancellation> cancellation() const { return cancellation_; }

private:
    std::shared_ptr<GenerationCancellation> cancellation_ = std::make_shared<GenerationCancellation>();
    std::mutex jobsMutex_;
    std::vector<std::thread> jobs_;
};

}  // namespace krok::subtitle::native::runtime
