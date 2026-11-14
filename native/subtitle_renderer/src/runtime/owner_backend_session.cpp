#include "owner_backend_session.h"
#include "work_admission.h"
#include <atomic>
#include <condition_variable>
#include <deque>
#include <future>
#include <utility>

namespace krok::subtitle::native::runtime {
namespace {
struct Job { int generation; OwnerBackendSession::Work work; OwnerBackendSession::Complete complete; };
}

// Everything the owner thread touches lives here, never in the session object.
struct OwnerBackendSession::State {
    explicit State(std::shared_ptr<GenerationCancellation> c) : cancellation(std::move(c)) {}
    std::shared_ptr<GenerationCancellation> cancellation;
    mutable std::mutex mutex;
    std::condition_variable ready;
    std::deque<Job> queue;
    bool stopping = false;
    int outstanding = 0;
    std::exception_ptr failure;
    // A completed std::thread id can be reused by a later control thread.
    std::atomic<bool> ownerRunning{true};

    void stop() {
        { std::lock_guard lock(mutex); stopping = true; }
        ready.notify_one();
    }
    void complete(Job& job, JobCompletion completion) {
        if (!job.complete) return;
        try { job.complete(completion); }
        catch (...) { std::lock_guard lock(mutex); failure = std::current_exception(); }
    }
    void run(RenderBackend& backend) {
        for (;;) {
            Job job;
            bool stop;
            {
                std::unique_lock lock(mutex);
                ready.wait(lock, [this] { return stopping || !queue.empty(); });
                if (queue.empty()) return;
                job = std::move(queue.front()); queue.pop_front();
                stop = stopping;
            }
            JobCompletion result{JobOutcome::Completed, {}};
            if (stop || cancellation->cancelled(job.generation)) result.outcome = JobOutcome::Cancelled;
            else {
                try { job.work(backend); }
                catch (...) {
                    result = {JobOutcome::Failed, std::current_exception()};
                    std::lock_guard lock(mutex); failure = result.error;
                }
            }
            {
                std::lock_guard lock(mutex);
                --outstanding; // release credit before completion can submit the next job
                stop = stopping;
            }
            if (stop || result.outcome == JobOutcome::Cancelled) complete(job, {JobOutcome::Cancelled, result.error});
            else if (!cancellation->publishIfActive(job.generation, [&] { complete(job, result); }))
                complete(job, {JobOutcome::Cancelled, result.error});
        }
    }
};

OwnerBackendSession::OwnerBackendSession(Factory factory, std::shared_ptr<GenerationCancellation> cancellation) {
    if (!factory || !cancellation) throw BackendError("session factory and cancellation are required");
    state_ = std::make_shared<State>(std::move(cancellation));
    std::promise<void> startup;
    auto initialized = startup.get_future();
    // The thread holds its own reference: it may outlive this object (see the destructor).
    thread_ = std::thread([state = state_, factory = std::move(factory), startup = std::move(startup)]() mutable {
        struct RunningGuard {
            std::atomic<bool>& running;
            ~RunningGuard() { running.store(false); }
        } runningGuard{state->ownerRunning};
        std::unique_ptr<RenderBackend> backend;
        try {
            backend = factory();
            if (!backend) throw BackendError("backend factory returned null");
            startup.set_value();
        } catch (...) {
            startup.set_exception(std::current_exception());
            return;
        }
        state->run(*backend);
        // Both backend and its device/session are destroyed here, before this owner thread exits.
    });
    owner_ = thread_.get_id();
    try { initialized.get(); }
    catch (...) { thread_.join(); throw; }
}

bool OwnerBackendSession::onOwnerThread() const {
    return state_->ownerRunning.load() && std::this_thread::get_id() == owner_;
}

OwnerBackendSession::~OwnerBackendSession() {
    if (onOwnerThread()) {
        // Destroyed from a completion callback: joining would deadlock and throwing from a
        // destructor terminates. Stop admission and detach; the owner thread finishes the
        // current callback, drains queued work as cancelled and destroys the backend itself.
        state_->stop();
        thread_.detach();
        return;
    }
    close();
}

bool OwnerBackendSession::submit(int generation, Work work, Complete completeCallback) {
    if (!work) throw BackendError("session work is required");
    std::lock_guard lock(state_->mutex);
    if (!canAcceptWork(state_->stopping, true, state_->outstanding, 1)) return false;
    state_->queue.push_back({generation, std::move(work), std::move(completeCallback)});
    ++state_->outstanding;
    state_->ready.notify_one();
    return true;
}

void OwnerBackendSession::close() {
    if (onOwnerThread()) throw BackendError("不能从 owner 回调同步关闭所属 session");
    std::lock_guard closeLock(closeMutex_);
    state_->stop();
    if (thread_.joinable()) thread_.join();
}

int OwnerBackendSession::outstanding() const {
    std::lock_guard lock(state_->mutex);
    return state_->outstanding;
}

std::exception_ptr OwnerBackendSession::lastFailure() const {
    std::lock_guard lock(state_->mutex);
    return state_->failure;
}
}
