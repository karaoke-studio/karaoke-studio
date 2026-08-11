#include "render_job_runtime.h"

namespace krok::subtitle::native::runtime {

bool RenderJobRuntime::generationCancelled(int generation) {
    if (shutdownRequested_.load()) {
        return true;
    }
    std::lock_guard<std::mutex> lock(cancelMutex_);
    return cancelledGenerations_.contains(generation);
}

void RenderJobRuntime::cancelGeneration(int generation) {
    std::lock_guard<std::mutex> lock(cancelMutex_);
    cancelledGenerations_.insert(generation);
}

void RenderJobRuntime::clearGenerationCancel(int generation) {
    std::lock_guard<std::mutex> lock(cancelMutex_);
    cancelledGenerations_.remove(generation);
}

void RenderJobRuntime::remember(std::thread job) {
    std::lock_guard<std::mutex> lock(jobsMutex_);
    jobs_.push_back(std::move(job));
}

void RenderJobRuntime::joinAll() {
    std::vector<std::thread> jobs;
    {
        std::lock_guard<std::mutex> lock(jobsMutex_);
        jobs.swap(jobs_);
    }
    for (auto &job : jobs) {
        if (job.joinable()) {
            job.join();
        }
    }
}

void RenderJobRuntime::requestShutdown() noexcept {
    shutdownRequested_.store(true);
}

}  // namespace krok::subtitle::native::runtime
