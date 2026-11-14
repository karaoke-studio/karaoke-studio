#include "runtime/owner_backend_session.h"
#include "runtime/work_admission.h"
#include <atomic>
#include <future>
#include <iostream>

using namespace krok::subtitle::native;
using namespace krok::subtitle::native::runtime;
void require(bool v, const char* text) { if (!v) throw std::runtime_error(text); }
struct State { std::thread::id created, worked, destroyed; };
class TestBackend final : public RenderBackend {
public:
    explicit TestBackend(std::shared_ptr<State> s) : state(std::move(s)) { state->created = std::this_thread::get_id(); }
    ~TestBackend() override { state->destroyed = std::this_thread::get_id(); }
    BackendCaps capabilities() const override { return {}; }
    BackendDiagnostics diagnostics() const override { return {}; }
    ProbeResult renderProbe(const ProbeOptions&) override { return {}; }
    void configure(const RenderScene&) override {}
    ProbeResult renderFrame(int, bool) override { return {}; }
    NativePreviewResult presentFrame(int, const NativePreviewTarget&, int) override { return {}; }
    NativeRenderOnlyResult renderFrameOnly(int, int) override { return {}; }
    NativePreviewResult presentRendered(const NativePreviewTarget&, int, int) override { return {}; }
    void closeNativePreview() override {}
    std::shared_ptr<State> state;
};
int main() {
    try {
        require(canAcceptWork(false, true, 0, 1), "admission empty");
        require(!canAcceptWork(false, true, 1, 1), "admission running counts");
        require(!canAcceptWork(true, true, 0, 1), "admission shutdown");
        auto state = std::make_shared<State>();
        auto cancellation = std::make_shared<GenerationCancellation>();
        OwnerBackendSession session([state] { return std::make_unique<TestBackend>(state); }, cancellation);
        require(state->created != std::this_thread::get_id(), "backend created on owner");
        std::promise<void> entered, release;
        auto releaseFuture = release.get_future();
        std::promise<JobCompletion> cancelled;
        auto cancelledFuture = cancelled.get_future();
        require(session.submit(17, [&](RenderBackend&) {
            state->worked = std::this_thread::get_id(); entered.set_value(); releaseFuture.wait();
        }, [&](JobCompletion r) { cancelled.set_value(r); }), "first job");
        entered.get_future().get();
        require(!session.submit(17, [](RenderBackend&) {}, {}), "bounded backpressure");
        cancellation->cancel(17);
        release.set_value();
        require(cancelledFuture.get().outcome == JobOutcome::Cancelled, "cancel suppresses completed publication");
        cancellation->clear(17);
        std::promise<JobCompletion> failed;
        auto failure = failed.get_future();
        require(session.submit(17, [](RenderBackend&) { throw std::runtime_error("expected"); },
            [&](JobCompletion r) { failed.set_value(r); }), "submit failure");
        auto result = failure.get();
        require(result.outcome == JobOutcome::Failed && result.error, "work error delivered");
        for (int i = 0; i < 1500; ++i) {
            std::promise<JobCompletion> done;
            auto future = done.get_future();
            require(session.submit(17, [](RenderBackend&) {}, [&](JobCompletion r) { done.set_value(r); }), "credit reusable");
            require(future.get().outcome == JobOutcome::Completed, "completed job");
        }
        require(session.outstanding() == 0, "credit drained");
        require(session.lastFailure() != nullptr, "error remains diagnosable");
        cancellation->shutdown();
        std::promise<JobCompletion> shutdown;
        auto shutdownFuture = shutdown.get_future();
        require(session.submit(18, [](RenderBackend&) { throw std::runtime_error("must not run"); },
            [&](JobCompletion r) { shutdown.set_value(r); }), "shutdown cancellation queued");
        require(shutdownFuture.get().outcome == JobOutcome::Cancelled, "global cancellation");
        std::thread closeOne([&] { session.close(); });
        std::thread closeTwo([&] { session.close(); });
        closeOne.join(); closeTwo.join();
        require(state->created == state->worked && state->created == state->destroyed, "single owner lifecycle");
        require(!session.submit(17, [](RenderBackend&) {}, {}), "closed rejects");
        bool startupFailed = false;
        try { OwnerBackendSession broken([]() -> std::unique_ptr<RenderBackend> { throw BackendError("init failed"); }); }
        catch (const BackendError&) { startupFailed = true; }
        require(startupFailed, "startup failure joined and propagated");
        std::cout << "single owner, cancellation, bounded admission, error recovery passed\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
