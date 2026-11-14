#include "owner_backend_session.h"
#include "work_admission.h"
#include <future>
#include <utility>

namespace krok::subtitle::native::runtime {
OwnerBackendSession::OwnerBackendSession(Factory factory, std::shared_ptr<GenerationCancellation> cancellation)
    : cancellation_(std::move(cancellation)) {
    if (!factory || !cancellation_) throw BackendError("session factory and cancellation are required");
    std::promise<void> startup;
    auto initialized = startup.get_future();
    thread_ = std::thread([this, factory = std::move(factory), startup = std::move(startup)]() mutable {
        std::unique_ptr<RenderBackend> backend;
        try {
            backend = factory();
            if (!backend) throw BackendError("backend factory returned null");
            startup.set_value();
        } catch (...) {
            startup.set_exception(std::current_exception());
            return;
        }
        run(*backend);
        // Both backend and its device/session are destroyed here, before this owner thread exits.
    });
    owner_ = thread_.get_id();
    try { initialized.get(); }
    catch (...) { thread_.join(); throw; }
}
OwnerBackendSession::~OwnerBackendSession() { close(); }
bool OwnerBackendSession::submit(int generation, Work work, Complete completeCallback) {
    if (!work) throw BackendError("session work is required");
    std::lock_guard lock(mutex_);
    if (!canAcceptWork(stopping_, true, outstanding_, 1)) return false;
    queue_.push_back({generation, std::move(work), std::move(completeCallback)});
    ++outstanding_;
    ready_.notify_one();
    return true;
}
void OwnerBackendSession::close() {
    if (std::this_thread::get_id() == owner_) throw BackendError("不能从 owner 回调同步关闭所属 session");
    std::lock_guard closeLock(closeMutex_);
    {
        std::lock_guard lock(mutex_);
        stopping_ = true;
    }
    ready_.notify_one();
    if (thread_.joinable()) thread_.join();
}
int OwnerBackendSession::outstanding() const {
    std::lock_guard lock(mutex_);
    return outstanding_;
}
std::exception_ptr OwnerBackendSession::lastFailure() const {
    std::lock_guard lock(mutex_);
    return failure_;
}
void OwnerBackendSession::complete(Job& job, JobCompletion completion) {
    if (!job.complete) return;
    try { job.complete(completion); }
    catch (...) { std::lock_guard lock(mutex_); failure_ = std::current_exception(); }
}
void OwnerBackendSession::run(RenderBackend& backend) {
    for (;;) {
        Job job;
        bool stop;
        {
            std::unique_lock lock(mutex_);
            ready_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
            if (queue_.empty()) return;
            job = std::move(queue_.front()); queue_.pop_front();
            stop = stopping_;
        }
        JobCompletion result{JobOutcome::Completed, {}};
        if (stop || cancellation_->cancelled(job.generation)) result.outcome = JobOutcome::Cancelled;
        else {
            try { job.work(backend); }
            catch (...) {
                result = {JobOutcome::Failed, std::current_exception()};
                std::lock_guard lock(mutex_); failure_ = result.error;
            }
        }
        {
            std::lock_guard lock(mutex_);
            --outstanding_; // release credit before completion can submit the next job
            stop = stopping_;
        }
        if (stop || result.outcome == JobOutcome::Cancelled) complete(job, {JobOutcome::Cancelled, result.error});
        else if (!cancellation_->publishIfActive(job.generation, [&] { complete(job, result); }))
            complete(job, {JobOutcome::Cancelled, result.error});
    }
}
}
