#include "command_router.h"
#include "../../backends/backend_factory.h"
#include "../../scene/skia_ir_projection.h"
#include "../../protocol/json_protocol.h"
#include <future>

namespace krok::subtitle::native::commands {
using protocol::response;
using protocol::writeJson;
QJsonObject SkiaCommandRouter::info(RenderBackend& backend) const {
    const auto caps = backend.capabilities();
    return {{"backend", "skia"}, {"api", QString::fromStdString(caps.api)},
        {"execution_target", QString::fromStdString(caps.executionTarget)}, {"hardware", caps.hardware},
        {"adapter_name", QString::fromStdString(caps.adapterName)}, {"requested_target", requestedTarget_},
        {"fallback_reason", fallbackReason_}, {"profile", "p04_ascii_solid_wipe"},
        {"pixel_format", "rgba8888"}, {"alpha_type", "straight"}, {"origin", "top_left"}, {"color_space", "srgb"}};
}
QJsonObject SkiaCommandRouter::select(const QJsonObject& request) {
    if (session_) throw BackendError("Skia backend selection is immutable for this process");
    requestedTarget_ = request.value("target").toString();
    const auto fallback = request.value("fallback").toString();
    if (request.value("selection_version").toInt() != 1 || request.value("backend") != "skia"
        || (requestedTarget_ != "gpu" && requestedTarget_ != "raster")
        || (fallback != "none" && fallback != "raster") || !request.value("require_hardware").isBool())
        throw BackendError("Invalid explicit Skia selection/version/fallback policy");
    SkiaOptions options{requestedTarget_ == "gpu" ? SkiaTarget::Gpu : SkiaTarget::Raster};
    options.requireHardware = request.value("require_hardware").toBool();
    try {
        // The owner retains the transport until its thread exits. close() drops the
        // control-thread reference first, so QSharedMemory is destroyed by its owner.
        session_ = std::make_unique<runtime::OwnerBackendSession>([options, frames = frames_] { return makeBackend(options); }, cancellation_);
    } catch (const std::exception& e) {
        if (requestedTarget_ != "gpu" || fallback != "raster") throw;
        fallbackReason_ = QString::fromUtf8(e.what());
        session_ = std::make_unique<runtime::OwnerBackendSession>([frames = frames_] { return makeBackend(SkiaOptions{SkiaTarget::Raster}); }, cancellation_);
    }
    return response(true, "backend_selected");
}
void SkiaCommandRouter::synchronous(const QJsonObject& request) {
    if (!session_) throw BackendError("Explicit select_backend handshake is required first");
    std::promise<runtime::JobCompletion> done;
    auto finished = done.get_future();
    QJsonObject out;
    const bool accepted = session_->submit(0, [&, frames = frames_](RenderBackend& backend) {
        const auto cmd = request.value("cmd").toString();
        if (cmd == "configure") {
            auto scene = scene::skiaSceneFromIr(request.value("ir").toObject());
            backend.configure(scene);
            configured_ = true;
            out = response(true, "configured");
            out.insert("width", scene.width); out.insert("height", scene.height);
            out.insert("schema", 3);
        } else if (cmd == "backend_info" || cmd == "gpu_diagnostics" || cmd == "diagnostics")
            out = response(true, cmd == "backend_info" ? "backend_info" : "gpu_diagnostics");
        else if (cmd == "render_frame" || cmd == "render_frame_stats") out = frames->frame(backend, request);
        else throw BackendError("Unsupported Skia command");
        out.insert("backend_info", info(backend));
    }, [&](runtime::JobCompletion result) { done.set_value(result); });
    if (!accepted) throw BackendError("Skia owner busy; one outstanding operation is allowed");
    const auto result = finished.get();
    if (result.error) std::rethrow_exception(result.error);
    if (result.outcome != runtime::JobOutcome::Completed) throw BackendError("Skia command cancelled");
    writeJson(out);
}
bool SkiaCommandRouter::dispatch(const QJsonObject& request) {
    const auto cmd = request.value("cmd").toString();
    try {
        if (cmd == "shutdown") { close(); writeJson(response(true, "shutdown")); return false; }
        if (cmd == "cancel_generation") {
            const int generation = request.value("generation").toInt();
            if (generation < 1) throw BackendError("Invalid cancellation generation");
            cancellation_->cancel(generation);
            auto out = response(true, "generation_cancelled"); out.insert("generation", generation); writeJson(out);
        } else if (cmd == "select_backend") {
            select(request);
            std::promise<runtime::JobCompletion> done;
            auto finished = done.get_future();
            auto out = response(true, "backend_selected");
            session_->submit(0, [&](RenderBackend& backend) { out.insert("backend_info", info(backend)); },
                             [&](runtime::JobCompletion result) { done.set_value(result); });
            auto result = finished.get();
            if (result.error) std::rethrow_exception(result.error);
            writeJson(out);
        } else if (cmd == "render_range") {
            if (!session_ || !configured_) throw BackendError("Skia must be configured before rendering");
            const int generation = request.value("generation").toInt();
            if (generation < 1 || cancellation_->cancelled(generation)) throw BackendError("Invalid/cancelled generation");
            const bool accepted = session_->submit(generation, [frames = frames_, cancellation = cancellation_, request, generation](RenderBackend& backend) {
                auto started = response(true, "range_started"); started.insert("generation", generation); writeJson(started);
                frames->range(backend, request, *cancellation);
            }, [generation](runtime::JobCompletion result) {
                auto out = response(result.outcome != runtime::JobOutcome::Failed, "range_done");
                out.insert("generation", generation);
                out.insert("cancelled", result.outcome == runtime::JobOutcome::Cancelled);
                if (result.error) {
                    try { std::rethrow_exception(result.error); }
                    catch (const std::exception& e) { out.insert("error", QString::fromUtf8(e.what())); }
                }
                writeJson(out);
            });
            if (!accepted) throw BackendError("Skia owner busy; one outstanding operation is allowed");
        } else synchronous(request);
    } catch (const std::exception& e) {
        auto out = response(false, "error");
        out.insert("error", QString::fromUtf8(e.what()));
        out.insert("cmd", cmd);
        writeJson(out);
    }
    return true;
}
void SkiaCommandRouter::close() {
    cancellation_->shutdown();
    frames_.reset();
    if (session_) session_->close();
    session_.reset();
}
}
