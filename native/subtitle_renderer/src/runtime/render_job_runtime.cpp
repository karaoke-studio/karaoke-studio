#include "render_job_runtime.h"

namespace krok::subtitle::native::runtime {

bool RenderJobRuntime::generationCancelled(int generation) {
    return cancellation_->cancelled(generation);
}

void RenderJobRuntime::cancelGeneration(int generation) {
    cancellation_->cancel(generation);
}

void RenderJobRuntime::clearGenerationCancel(int generation) {
    cancellation_->clear(generation);
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
    cancellation_->shutdown();
}

}  // namespace krok::subtitle::native::runtime
