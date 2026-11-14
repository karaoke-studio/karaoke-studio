#pragma once
#include "generation_cancellation.h"
#include "../backends/render_backend.h"
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
// Queue state is shared with the owner thread, so the session object itself may be destroyed
// from a completion callback: the owner thread then drains and destroys the backend on its own.
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
    // Stops admission and joins the owner thread. Calling it from the owner thread is an
    // error (a thread cannot join itself); destroying the session there is allowed instead.
    void close();
    int outstanding() const;
    std::exception_ptr lastFailure() const;
private:
    struct State;
    bool onOwnerThread() const;
    std::shared_ptr<State> state_;
    std::mutex closeMutex_;
    std::thread thread_;
    std::thread::id owner_;
};
}
