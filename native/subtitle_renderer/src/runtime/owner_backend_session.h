#pragma once
#include "generation_cancellation.h"
#include "../backends/render_backend.h"
#include <condition_variable>
#include <deque>
#include <exception>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>

namespace krok::subtitle::native::runtime {
enum class JobOutcome { Completed, Cancelled, Failed };
struct JobCompletion {
    JobOutcome outcome;
    std::exception_ptr error;
};

// One backend is constructed, called, and destroyed on one owner thread.
// Capacity is one outstanding job; rejection is explicit backpressure, never an unbounded queue.
class OwnerBackendSession {
public:
    using Factory = std::function<std::unique_ptr<RenderBackend>()>;
    using Work = std::function<void(RenderBackend&)>;
    using Complete = std::function<void(JobCompletion)>;
    explicit OwnerBackendSession(Factory factory,
        std::shared_ptr<GenerationCancellation> cancellation = std::make_shared<GenerationCancellation>());
    ~OwnerBackendSession();
    OwnerBackendSession(const OwnerBackendSession&) = delete;
    OwnerBackendSession& operator=(const OwnerBackendSession&) = delete;
    bool submit(int generation, Work work, Complete complete);
    void close();
    int outstanding() const;
    std::exception_ptr lastFailure() const;
private:
    struct Job { int generation; Work work; Complete complete; };
    void run(RenderBackend& backend);
    void complete(Job& job, JobCompletion completion);
    std::shared_ptr<GenerationCancellation> cancellation_;
    mutable std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<Job> queue_;
    bool stopping_ = false;
    int outstanding_ = 0;
    std::exception_ptr failure_;
    std::mutex closeMutex_;
    std::thread thread_;
    std::thread::id owner_;
};
}
