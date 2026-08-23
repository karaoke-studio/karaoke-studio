#include <QtCore/QByteArray>
#include <QtCore/QElapsedTimer>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtCore/QIODevice>
#include <QtCore/QTextStream>
#include <QtWidgets/QApplication>

#include "backends/qt/gpu_scene_projection.h"
#include "commands/qt_frame_commands.h"
#include "commands/qt_range_commands.h"
#include "diagnostics/gpu_diagnostics_json.h"
#include "diagnostics/shared_frame_metadata_json.h"
#include "protocol/json_protocol.h"
#include "protocol/json_value.h"
#include "protocol/render_config.h"
#include "runtime/checksum.h"
#include "runtime/gpu_backend_runtime.h"
#include "runtime/gpu_preview_worker_pool.h"
#include "runtime/render_runtime.h"

#include <algorithm>
#include <cstdint>
#include <optional>
#include <vector>

namespace {

using krok::subtitle::native::protocol::kRenderIrSchema;
using krok::subtitle::native::protocol::Command;
using krok::subtitle::native::protocol::commandFromName;
using krok::subtitle::native::protocol::intValue;
using krok::subtitle::native::protocol::parseRequestLine;
using krok::subtitle::native::protocol::RenderConfig;
using krok::subtitle::native::protocol::response;
using krok::subtitle::native::protocol::stringValue;
using krok::subtitle::native::protocol::writeJson;
using krok::subtitle::native::legacy_qt::gpuSceneFromConfig;
using krok::subtitle::native::diagnostics::appendGpuDiagnostics;
using krok::subtitle::native::diagnostics::appendGpuFrameDiagnostics;
using krok::subtitle::native::diagnostics::appendSharedFrameMetadata;
using krok::subtitle::native::commands::handleConfigure;
using krok::subtitle::native::commands::handleRenderFrame;
using krok::subtitle::native::commands::handleRenderFrameStats;
using krok::subtitle::native::commands::handleRenderRange;
using krok::subtitle::native::commands::handleRenderRangeStats;
using krok::subtitle::native::runtime::GpuPreviewWorkerPool;
using krok::subtitle::native::runtime::GpuPreviewPoolCacheEntry;
using krok::subtitle::native::runtime::RenderRuntime;
using krok::subtitle::native::runtime::SharedFrameRing;
using krok::subtitle::native::runtime::bytesChecksum;
using krok::subtitle::native::runtime::ensureGpuBackend;
using krok::subtitle::native::runtime::gpuPreviewPool;

bool generationCancelled(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return false;
    }
    return runtime->generationCancelled(generation);
}

void cancelGeneration(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return;
    }
    runtime->cancelGeneration(generation);
}

void joinRenderJobs(RenderRuntime *runtime) {
    if (runtime == nullptr) {
        return;
    }
    runtime->joinRenderJobs();
}

QString defaultSharedMemoryKey(int generation) {
    return krok::subtitle::native::runtime::defaultSharedMemoryKey(generation);
}

bool ensureSharedFrameRing(
    RenderRuntime *runtime,
    const QString &key,
    int ringSlotCount,
    int width,
    int height,
    QString *error
) {
    if (runtime == nullptr) {
        if (error != nullptr) {
            *error = QStringLiteral("render runtime is unavailable");
        }
        return false;
    }
    return runtime->ensureSharedFrameRing(
        key, ringSlotCount, width, height, error
    );
}

bool writeSharedRgbaSlot(
    RenderRuntime *runtime,
    const std::uint8_t *rgba,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut,
    int formatId = 1,
    const QString &pixelFormat = QStringLiteral("rgba8888")
);

bool writeSharedRgbaSlot(
    RenderRuntime *runtime,
    const std::uint8_t *rgba,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut,
    int formatId,
    const QString &pixelFormat
) {
    if (runtime == nullptr) {
        return false;
    }
    return runtime->writeSharedRgbaSlot(
        rgba,
        width,
        height,
        stride,
        generation,
        frameIndex,
        tMs,
        slotIndex,
        ringOut,
        formatId,
        pixelFormat
    );
}

bool writeSharedPackedRgbaSlot(
    RenderRuntime *runtime,
    const std::uint8_t *premultipliedBgra,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut
) {
    if (runtime == nullptr) {
        return false;
    }
    return runtime->writeSharedPackedRgbaSlot(
        premultipliedBgra,
        width,
        height,
        stride,
        generation,
        frameIndex,
        tMs,
        slotIndex,
        ringOut
    );
}

bool writeSharedBandSlot(
    RenderRuntime *runtime,
    const std::uint8_t *payloadData,
    int payloadBytes,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut
) {
    if (runtime == nullptr) {
        return false;
    }
    return runtime->writeSharedBandSlot(
        payloadData,
        payloadBytes,
        width,
        height,
        stride,
        generation,
        frameIndex,
        tMs,
        slotIndex,
        ringOut
    );
}


QJsonObject backendCapsJson(const krok::subtitle::native::BackendCaps &caps) {
    QJsonObject out;
    out.insert(QStringLiteral("backend"), QString::fromStdString(caps.backend));
    out.insert(QStringLiteral("adapter"), QString::fromStdString(caps.adapterName));
    out.insert(QStringLiteral("feature_level"), QString::fromStdString(caps.featureLevel));
    out.insert(QStringLiteral("vendor_id"), static_cast<qint64>(caps.adapterVendorId));
    out.insert(QStringLiteral("device_id"), static_cast<qint64>(caps.adapterDeviceId));
    out.insert(QStringLiteral("dedicated_video_memory"), static_cast<qint64>(caps.dedicatedVideoMemory));
    out.insert(QStringLiteral("hardware"), caps.hardware);
    out.insert(QStringLiteral("warp"), caps.warp);
    out.insert(QStringLiteral("transparent_surface"), caps.supportsTransparentSurface);
    out.insert(QStringLiteral("staging_readback"), caps.supportsStagingReadback);
    out.insert(QStringLiteral("glyphs"), caps.supportsGlyphs);
    out.insert(QStringLiteral("native_preview"), caps.supportsNativePreview);
    return out;
}

QJsonObject handleBackendInfo(const QJsonObject &request, RenderRuntime *runtime) {
    const bool forceWarp = request.value(QStringLiteral("force_warp")).toBool(false);
    QString error;
    auto *backend = ensureGpuBackend(runtime, forceWarp, &error);
    QJsonObject out = response(true, QStringLiteral("backend_info"));
    out.insert(QStringLiteral("available"), backend != nullptr);
    out.insert(QStringLiteral("requested_warp"), forceWarp);
    if (backend == nullptr) {
        out.insert(QStringLiteral("error"), error);
        return out;
    }
    const QJsonObject caps = backendCapsJson(backend->capabilities());
    for (auto it = caps.begin(); it != caps.end(); ++it) {
        out.insert(it.key(), it.value());
    }
    return out;
}

QJsonObject handleRenderProbe(const QJsonObject &request, RenderRuntime *runtime) {
    const int width = intValue(request, QStringLiteral("width"), 256);
    const int height = intValue(request, QStringLiteral("height"), 144);
    if (width <= 0 || height <= 0 || width > 8192 || height > 8192) {
        QJsonObject out = response(false, QStringLiteral("render_probe"));
        out.insert(QStringLiteral("error"), QStringLiteral("render probe dimensions must be within 1..8192"));
        return out;
    }
    const bool forceWarp = request.value(QStringLiteral("force_warp")).toBool(false);
    QString error;
    auto *backend = ensureGpuBackend(runtime, forceWarp, &error);
    if (backend == nullptr) {
        QJsonObject out = response(false, QStringLiteral("render_probe"));
        out.insert(QStringLiteral("error"), error);
        return out;
    }

    krok::subtitle::native::ProbeOptions options;
    options.width = width;
    options.height = height;
    options.red = static_cast<std::uint8_t>(std::clamp(intValue(request, QStringLiteral("red"), 51), 0, 255));
    options.green = static_cast<std::uint8_t>(std::clamp(intValue(request, QStringLiteral("green"), 102), 0, 255));
    options.blue = static_cast<std::uint8_t>(std::clamp(intValue(request, QStringLiteral("blue"), 204), 0, 255));
    options.alpha = static_cast<std::uint8_t>(std::clamp(intValue(request, QStringLiteral("alpha"), 128), 0, 255));
    options.drawGlyph = request.value(QStringLiteral("draw_glyph")).toBool(true);

    const int generation = intValue(request, QStringLiteral("generation"), 0);
    const int frameIndex = intValue(request, QStringLiteral("frame_index"), 0);
    const int slotIndex = 0;
    const QString shmKey = stringValue(
        request,
        QStringLiteral("shm_key"),
        defaultSharedMemoryKey(generation) + QStringLiteral("_gpu_probe")
    );
    QString shmError;
    if (!ensureSharedFrameRing(runtime, shmKey, 1, width, height, &shmError)) {
        QJsonObject out = response(false, QStringLiteral("render_probe"));
        out.insert(QStringLiteral("error"), QStringLiteral("failed to create shared memory: ") + shmError);
        return out;
    }

    QElapsedTimer totalTimer;
    totalTimer.start();
    try {
        const auto result = backend->renderProbe(options);
        SharedFrameRing ring;
        const bool wrote = writeSharedRgbaSlot(
            runtime,
            result.surface.bytes.data(),
            result.surface.width,
            result.surface.height,
            result.surface.stride,
            generation,
            frameIndex,
            0,
            slotIndex,
            &ring
        );
        if (!wrote) {
            QJsonObject out = response(false, QStringLiteral("render_probe"));
            out.insert(QStringLiteral("error"), QStringLiteral("failed to write GPU probe shared-memory slot"));
            return out;
        }
        QJsonObject out = response(true, QStringLiteral("probe_ready"));
        out.insert(QStringLiteral("generation"), generation);
        out.insert(QStringLiteral("frame_index"), frameIndex);
        out.insert(QStringLiteral("t_ms"), 0);
        out.insert(QStringLiteral("render_ms"), result.renderMs);
        out.insert(QStringLiteral("readback_ms"), result.readbackMs);
        out.insert(QStringLiteral("total_ms"), static_cast<double>(totalTimer.nsecsElapsed()) / 1000000.0);
        out.insert(
            QStringLiteral("checksum"),
            QString::number(bytesChecksum(result.surface.bytes.data(), result.surface.bytes.size()))
        );
        const QJsonObject caps = backendCapsJson(backend->capabilities());
        for (auto it = caps.begin(); it != caps.end(); ++it) {
            out.insert(it.key(), it.value());
        }
        appendSharedFrameMetadata(out, ring, slotIndex);
        return out;
    } catch (const std::exception &exception) {
        QJsonObject out = response(false, QStringLiteral("render_probe"));
        out.insert(QStringLiteral("error"), QString::fromUtf8(exception.what()));
        return out;
    }
}









QJsonObject handleConfigureGpu(
    const QJsonObject &request,
    const std::optional<RenderConfig> &config,
    RenderRuntime *runtime
) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("gpu_configure"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }
    const bool forceWarp = request.value(QStringLiteral("force_warp")).toBool(false);
    const bool realizationEnabled = request.value(
        QStringLiteral("realization_enabled")
    ).toBool(true);
    const bool sharedResources = request.value(
        QStringLiteral("shared_resources")
    ).toBool(false);
    const bool waitRealizations = request.value(
        QStringLiteral("wait_realizations")
    ).toBool(false);
    const bool deferFollowers = request.value(
        QStringLiteral("defer_followers")
    ).toBool(false);
    const bool deferRealizationsUntilFirstFrame = request.value(
        QStringLiteral("defer_realizations_until_first_frame")
    ).toBool(false);
    const int exportCropTop = std::max(
        intValue(request, QStringLiteral("export_crop_top"), 0), 0
    );
    const int exportCropHeight = std::max(
        intValue(request, QStringLiteral("export_crop_height"), 0), 0
    );
    std::vector<std::pair<int, int>> exportBands;
    const QJsonArray exportBandsJson = request.value(
        QStringLiteral("export_bands")
    ).toArray();
    exportBands.reserve(static_cast<std::size_t>(exportBandsJson.size()));
    for (const QJsonValue &value : exportBandsJson) {
        const QJsonObject item = value.toObject();
        const int top = std::max(
            intValue(item, QStringLiteral("top"), 0), 0
        );
        const int bandHeight = std::max(
            intValue(item, QStringLiteral("height"), 0), 0
        );
        if (bandHeight > 0) {
            exportBands.emplace_back(top, bandHeight);
        }
    }
    const int requestedWorkers = std::clamp(
        intValue(request, QStringLiteral("worker_count"), 1), 1, 8
    );
    const std::uint64_t realizationCapacity = static_cast<std::uint64_t>(
        std::clamp(
            intValue(request, QStringLiteral("realization_capacity"), 8192),
            8192,
            262144
        )
    );
    const int workerCount = forceWarp ? 1 : requestedWorkers;
    const bool targetResize = request.value(
        QStringLiteral("target_resize")
    ).toBool(false);
    if (!targetResize) {
        runtime->hardwareGpuPreviewPoolCache.clear();
        runtime->warpGpuPreviewPoolCache.clear();
    }
    if (workerCount > 1) {
        QElapsedTimer timer;
        timer.start();
        try {
            auto scene = gpuSceneFromConfig(*config);
            scene.prewarmTimeMs = std::max(
                intValue(request, QStringLiteral("prewarm_t_ms"), 0), 0
            );
            scene.realizationEnabled = realizationEnabled;
            scene.deferRealizationPrewarmUntilFirstFrame =
                deferRealizationsUntilFirstFrame;
            scene.exportCropTop = exportCropTop;
            scene.exportCropHeight = exportCropHeight;
            scene.exportBands = exportBands;
            scene.realizationCapacity = realizationCapacity;
            auto &pool = runtime->hardwareGpuPreviewPool;
            auto &poolKey = runtime->hardwareGpuPreviewPoolKey;
            auto &poolCache = runtime->hardwareGpuPreviewPoolCache;
            const QString targetKey = QStringLiteral("%1x%2@%3:w%4:s%5:r%6")
                .arg(scene.width)
                .arg(scene.height)
                .arg(static_cast<double>(scene.layoutReferenceScale), 0, 'f', 6)
                .arg(workerCount)
                .arg(sharedResources ? 1 : 0)
                .arg(realizationEnabled ? 1 : 0);
            bool targetCacheHit = false;
            if (targetResize && pool != nullptr && poolKey == targetKey) {
                targetCacheHit = true;
            } else if (targetResize && pool != nullptr && !poolKey.isEmpty()) {
                pool->pause();
                poolCache.erase(
                    std::remove_if(
                        poolCache.begin(), poolCache.end(),
                        [&](const GpuPreviewPoolCacheEntry &entry) {
                            return entry.key == poolKey;
                        }
                    ),
                    poolCache.end()
                );
                poolCache.push_front({poolKey, std::move(pool)});
                const auto cached = std::find_if(
                    poolCache.begin(), poolCache.end(),
                    [&](const GpuPreviewPoolCacheEntry &entry) {
                        return entry.key == targetKey;
                    }
                );
                if (cached != poolCache.end()) {
                    pool = std::move(cached->pool);
                    poolCache.erase(cached);
                    pool->resume(scene, deferFollowers);
                    targetCacheHit = true;
                }
                while (poolCache.size() > 2) {
                    poolCache.pop_back();
                }
            }
            if (pool == nullptr || pool->workerCount() != workerCount
                || pool->sharedResources() != sharedResources) {
                pool = std::make_unique<GpuPreviewWorkerPool>(
                    false, workerCount, sharedResources, writeJson
                );
            }
            if (!targetCacheHit) {
                pool->configure(scene, waitRealizations, deferFollowers);
            }
            poolKey = targetKey;
            runtime->hardwareGpuConfigured = true;
            QJsonObject out = response(true, QStringLiteral("gpu_configured"));
            out.insert(QStringLiteral("width"), scene.width);
            out.insert(QStringLiteral("height"), scene.height);
            out.insert(QStringLiteral("line_count"), static_cast<int>(scene.lines.size()));
            out.insert(QStringLiteral("worker_count"), pool->readyWorkerCount());
            out.insert(QStringLiteral("worker_count_requested"), requestedWorkers);
            out.insert(QStringLiteral("shared_resources"), pool->sharedResources());
            out.insert(QStringLiteral("target_cache_hit"), targetCacheHit);
            out.insert(
                QStringLiteral("configure_ms"),
                static_cast<double>(timer.nsecsElapsed()) / 1000000.0
            );
            const QJsonObject caps = backendCapsJson(pool->capabilities());
            for (auto it = caps.begin(); it != caps.end(); ++it) {
                out.insert(it.key(), it.value());
            }
            appendGpuDiagnostics(&out, pool->diagnostics());
            return out;
        } catch (const std::exception &exception) {
            runtime->hardwareGpuPreviewPool.reset();
            QJsonObject out = response(false, QStringLiteral("gpu_configure"));
            out.insert(QStringLiteral("error"), QString::fromUtf8(exception.what()));
            return out;
        }
    }
    if (forceWarp) {
        runtime->warpGpuPreviewPool.reset();
        runtime->warpGpuPreviewPoolKey.clear();
    } else {
        runtime->hardwareGpuPreviewPool.reset();
        runtime->hardwareGpuPreviewPoolKey.clear();
    }
    QString error;
    auto *backend = ensureGpuBackend(runtime, forceWarp, &error);
    if (backend == nullptr) {
        QJsonObject out = response(false, QStringLiteral("gpu_configure"));
        out.insert(QStringLiteral("error"), error);
        return out;
    }
    QElapsedTimer timer;
    timer.start();
    try {
        auto scene = gpuSceneFromConfig(*config);
        scene.prewarmTimeMs = std::max(
            intValue(request, QStringLiteral("prewarm_t_ms"), 0), 0
        );
        scene.realizationEnabled = realizationEnabled;
        scene.deferRealizationPrewarmUntilFirstFrame =
            deferRealizationsUntilFirstFrame;
        scene.exportCropTop = exportCropTop;
        scene.exportCropHeight = exportCropHeight;
        scene.exportBands = exportBands;
        scene.realizationCapacity = realizationCapacity;
        backend->configure(scene);
        if (forceWarp) {
            runtime->warpGpuConfigured = true;
        } else {
            runtime->hardwareGpuConfigured = true;
        }
        QJsonObject out = response(true, QStringLiteral("gpu_configured"));
        out.insert(QStringLiteral("width"), scene.width);
        out.insert(QStringLiteral("height"), scene.height);
        out.insert(QStringLiteral("line_count"), static_cast<int>(scene.lines.size()));
        out.insert(QStringLiteral("worker_count"), 1);
        out.insert(QStringLiteral("worker_count_requested"), requestedWorkers);
        out.insert(QStringLiteral("shared_resources"), false);
        out.insert(QStringLiteral("configure_ms"), static_cast<double>(timer.nsecsElapsed()) / 1000000.0);
        const QJsonObject caps = backendCapsJson(backend->capabilities());
        for (auto it = caps.begin(); it != caps.end(); ++it) {
            out.insert(it.key(), it.value());
        }
        appendGpuDiagnostics(&out, backend->diagnostics());
        return out;
    } catch (const std::exception &exception) {
        QJsonObject out = response(false, QStringLiteral("gpu_configure"));
        out.insert(QStringLiteral("error"), QString::fromUtf8(exception.what()));
        return out;
    }
}

QJsonObject handleResizeGpuTarget(
    const QJsonObject &request,
    std::optional<RenderConfig> *config,
    RenderRuntime *runtime
) {
    if (config == nullptr || !config->has_value()) {
        QJsonObject out = response(false, QStringLiteral("gpu_configure"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }
    const int width = std::clamp(
        intValue(request, QStringLiteral("width"), config->value().width),
        1,
        8192
    );
    const int height = std::clamp(
        intValue(request, QStringLiteral("height"), config->value().height),
        1,
        8192
    );
    const double dpr = std::clamp(
        request.value(QStringLiteral("dpr")).toDouble(config->value().dpr),
        0.01,
        4.0
    );
    if (static_cast<double>(width) * dpr > 8192.0
        || static_cast<double>(height) * dpr > 8192.0) {
        QJsonObject out = response(false, QStringLiteral("gpu_configure"));
        out.insert(
            QStringLiteral("error"),
            QStringLiteral("GPU target dimensions must be within 1..8192")
        );
        return out;
    }
    config->value().width = width;
    config->value().height = height;
    config->value().dpr = dpr;
    QJsonObject resizeRequest = request;
    resizeRequest.insert(QStringLiteral("target_resize"), true);
    return handleConfigureGpu(resizeRequest, *config, runtime);
}

QJsonObject handleGpuDiagnostics(
    const QJsonObject &request,
    RenderRuntime *runtime
) {
    const bool forceWarp = request.value(QStringLiteral("force_warp")).toBool(false);
    const bool configured = forceWarp
        ? runtime->warpGpuConfigured
        : runtime->hardwareGpuConfigured;
    if (!configured) {
        QJsonObject out = response(false, QStringLiteral("gpu_diagnostics"));
        out.insert(QStringLiteral("error"), QStringLiteral("GPU backend is not configured"));
        return out;
    }
    QJsonObject out = response(true, QStringLiteral("gpu_diagnostics"));
    if (auto *pool = gpuPreviewPool(runtime, forceWarp)) {
        appendGpuDiagnostics(&out, pool->diagnostics());
        out.insert(QStringLiteral("worker_count"), pool->workerCount());
        out.insert(QStringLiteral("worker_count_ready"), pool->readyWorkerCount());
        out.insert(QStringLiteral("in_flight"), pool->outstanding());
        out.insert(QStringLiteral("max_in_flight"), pool->maxOutstanding());
    } else {
        QString error;
        auto *backend = ensureGpuBackend(runtime, forceWarp, &error);
        if (backend == nullptr) {
            out.insert(QStringLiteral("ok"), false);
            out.insert(QStringLiteral("error"), error);
            return out;
        }
        appendGpuDiagnostics(&out, backend->diagnostics());
        out.insert(QStringLiteral("worker_count"), 1);
        out.insert(QStringLiteral("in_flight"), 0);
        out.insert(QStringLiteral("max_in_flight"), 1);
    }
    return out;
}

QJsonObject renderGpuFrameWithBackend(
    const QJsonObject &request,
    const RenderConfig &config,
    RenderRuntime *runtime,
    krok::subtitle::native::RenderBackend *backend
) {
    if (backend == nullptr) {
        QJsonObject out = response(false, QStringLiteral("gpu_render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("GPU backend is unavailable"));
        return out;
    }
    const int generation = intValue(request, QStringLiteral("generation"), 0);
    const int frameIndex = intValue(request, QStringLiteral("frame_index"), 0);
    const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
    // G7 export pipelining: with slot_count > 1 the consumer may still be
    // expanding frame N while this call renders frame N+1 into another slot.
    const int slotCount = std::clamp(
        intValue(request, QStringLiteral("slot_count"), 1), 1, 4
    );
    const int slotIndex = ((frameIndex % slotCount) + slotCount) % slotCount;
    const bool packedRgba = request.value(
        QStringLiteral("packed_rgba")
    ).toBool(false);
    const int packedHeight = packedRgba
        ? std::clamp(
            intValue(
                request,
                QStringLiteral("packed_height"),
                config.physicalHeight()
            ),
            1,
            config.physicalHeight()
        )
        : config.physicalHeight();
    const QString shmKey = stringValue(
        request,
        QStringLiteral("shm_key"),
        defaultSharedMemoryKey(generation) + QStringLiteral("_gpu_frame")
    );
    QString shmError;
    if (!ensureSharedFrameRing(
            runtime,
            shmKey,
            slotCount,
            config.physicalWidth(),
            packedHeight,
            &shmError
        )) {
        QJsonObject out = response(false, QStringLiteral("gpu_render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("failed to create shared memory: ") + shmError);
        return out;
    }
    try {
        const bool readbackBands = !packedRgba && request.value(
            QStringLiteral("readback_bands")
        ).toBool(false);
        const auto result = backend->renderFrame(tMs, readbackBands);
        SharedFrameRing ring;
        QElapsedTimer sharedMemoryTimer;
        sharedMemoryTimer.start();
        const bool wrote = packedRgba
            ? writeSharedPackedRgbaSlot(
                runtime,
                result.surface.bytes.data(),
                result.surface.width,
                result.surface.height,
                result.surface.stride,
                generation,
                frameIndex,
                tMs,
                slotIndex,
                &ring
            )
            : (readbackBands ? writeSharedBandSlot(
                runtime,
                result.surface.bytes.data(),
                static_cast<int>(result.surface.bytes.size()),
                result.surface.width,
                result.surface.height,
                result.surface.stride,
                generation,
                frameIndex,
                tMs,
                slotIndex,
                &ring
            ) : writeSharedRgbaSlot(
                runtime,
                result.surface.bytes.data(),
                result.surface.width,
                result.surface.height,
                result.surface.stride,
                generation,
                frameIndex,
                tMs,
                slotIndex,
                &ring,
                result.surface.pixelFormat
                    == krok::subtitle::native::PixelFormat::Bgra8888Premultiplied ? 2 : 1,
                result.surface.pixelFormat
                    == krok::subtitle::native::PixelFormat::Bgra8888Premultiplied
                    ? QStringLiteral("bgra8888_premultiplied")
                    : QStringLiteral("rgba8888")
            ));
        const double sharedMemoryCopyMs =
            static_cast<double>(sharedMemoryTimer.nsecsElapsed()) / 1000000.0;
        if (!wrote) {
            QJsonObject out = response(false, QStringLiteral("gpu_render_frame"));
            out.insert(QStringLiteral("error"), QStringLiteral("failed to write GPU frame shared-memory slot"));
            return out;
        }
        QJsonObject out = response(true, QStringLiteral("gpu_frame_ready"));
        out.insert(QStringLiteral("generation"), generation);
        out.insert(QStringLiteral("frame_index"), frameIndex);
        out.insert(
            QStringLiteral("request_serial"),
            intValue(request, QStringLiteral("request_serial"), frameIndex)
        );
        out.insert(
            QStringLiteral("worker_index"),
            intValue(request, QStringLiteral("worker_index"), 0)
        );
        out.insert(QStringLiteral("t_ms"), tMs);
        out.insert(QStringLiteral("render_ms"), result.renderMs);
        out.insert(QStringLiteral("readback_ms"), result.readbackMs);
        out.insert(
            QStringLiteral("shm_copy_ms"),
            packedRgba ? 0.0 : sharedMemoryCopyMs
        );
        out.insert(
            QStringLiteral("native_pack_ms"),
            packedRgba ? sharedMemoryCopyMs : 0.0
        );
        appendGpuFrameDiagnostics(&out, result.frameDiagnostics);
        if (request.value(QStringLiteral("include_checksum")).toBool(true)) {
            out.insert(
                QStringLiteral("checksum"),
                QString::number(bytesChecksum(result.surface.bytes.data(), result.surface.bytes.size()))
            );
        }
        appendSharedFrameMetadata(out, ring, slotIndex);
        if (packedRgba) {
            out.insert(
                QStringLiteral("readback_ratio"),
                config.physicalHeight() > 0
                    ? static_cast<double>(packedHeight)
                        / static_cast<double>(config.physicalHeight())
                    : 0.0
            );
        }
        if (readbackBands) {
            QJsonArray bands;
            int packedHeight = 0;
            for (const auto &band : result.surface.bands) {
                QJsonObject item;
                item.insert(QStringLiteral("top"), band.top);
                item.insert(QStringLiteral("height"), band.height);
                item.insert(QStringLiteral("packed_top"), band.packedTop);
                bands.append(item);
                packedHeight = std::max(
                    packedHeight, band.packedTop + band.height
                );
            }
            out.insert(QStringLiteral("bands"), bands);
            out.insert(QStringLiteral("packed_height"), packedHeight);
            out.insert(
                QStringLiteral("readback_ratio"),
                config.physicalHeight() > 0
                    ? static_cast<double>(packedHeight)
                        / static_cast<double>(config.physicalHeight())
                    : 0.0
            );
        }
        return out;
    } catch (const std::exception &exception) {
        QJsonObject out = response(false, QStringLiteral("gpu_render_frame"));
        out.insert(QStringLiteral("error"), QString::fromUtf8(exception.what()));
        return out;
    }
}

std::optional<QJsonObject> handleRenderGpuFrame(
    const QJsonObject &request,
    const std::optional<RenderConfig> &config,
    RenderRuntime *runtime
) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("gpu_render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }
    const bool forceWarp = request.value(QStringLiteral("force_warp")).toBool(false);
    const bool configured = forceWarp
        ? runtime->warpGpuConfigured
        : runtime->hardwareGpuConfigured;
    if (!configured) {
        QJsonObject out = response(false, QStringLiteral("gpu_render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("GPU backend is not configured"));
        return out;
    }

    auto *pool = gpuPreviewPool(runtime, forceWarp);
    if (pool == nullptr) {
        QString error;
        auto *backend = ensureGpuBackend(runtime, forceWarp, &error);
        if (backend == nullptr) {
            QJsonObject out = response(false, QStringLiteral("gpu_render_frame"));
            out.insert(QStringLiteral("error"), error);
            return out;
        }
        return renderGpuFrameWithBackend(request, *config, runtime, backend);
    }

    const int generation = intValue(request, QStringLiteral("generation"), 0);
    const int frameIndex = intValue(request, QStringLiteral("frame_index"), 0);
    const int requestSerial = intValue(
        request, QStringLiteral("request_serial"), frameIndex
    );
    const RenderConfig snapshot = *config;
    const QJsonObject requestSnapshot = request;
    const bool accepted = pool->submit(
        [runtime, snapshot, requestSnapshot, generation, requestSerial, forceWarp](
            krok::subtitle::native::RenderBackend &backend,
            int workerIndex
        ) {
            if (generationCancelled(runtime, generation)) {
                QJsonObject dropped = response(true, QStringLiteral("gpu_frame_dropped"));
                dropped.insert(QStringLiteral("generation"), generation);
                dropped.insert(QStringLiteral("request_serial"), requestSerial);
                dropped.insert(QStringLiteral("reason"), QStringLiteral("generation_cancelled"));
                return dropped;
            }
            QJsonObject workerRequest = requestSnapshot;
            workerRequest.insert(QStringLiteral("worker_index"), workerIndex);
            QJsonObject out = renderGpuFrameWithBackend(
                workerRequest, snapshot, runtime, &backend
            );
            if (auto *currentPool = gpuPreviewPool(runtime, forceWarp)) {
                out.insert(
                    QStringLiteral("worker_count_ready"),
                    currentPool->readyWorkerCount()
                );
            }
            if (generationCancelled(runtime, generation)) {
                out = response(true, QStringLiteral("gpu_frame_dropped"));
                out.insert(QStringLiteral("generation"), generation);
                out.insert(QStringLiteral("request_serial"), requestSerial);
                out.insert(QStringLiteral("reason"), QStringLiteral("generation_cancelled"));
            }
            return out;
        }
    );
    if (!accepted) {
        QJsonObject out = response(false, QStringLiteral("gpu_queue_full"));
        out.insert(QStringLiteral("error"), QStringLiteral("GPU preview in-flight limit reached"));
        out.insert(QStringLiteral("generation"), generation);
        out.insert(QStringLiteral("request_serial"), requestSerial);
        out.insert(QStringLiteral("in_flight"), pool->outstanding());
        out.insert(QStringLiteral("worker_count"), pool->workerCount());
        return out;
    }
    return std::nullopt;
}

QJsonObject handlePresentGpuFrame(
    const QJsonObject &request,
    const std::optional<RenderConfig> &config,
    RenderRuntime *runtime
) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("gpu_present_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }
    const bool forceWarp = request.value(QStringLiteral("force_warp")).toBool(false);
    const bool configured = forceWarp
        ? runtime->warpGpuConfigured
        : runtime->hardwareGpuConfigured;
    if (!configured) {
        QJsonObject out = response(false, QStringLiteral("gpu_present_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("GPU backend is not configured"));
        return out;
    }
    QString error;
    auto *backend = ensureGpuBackend(runtime, forceWarp, &error);
    if (backend == nullptr) {
        QJsonObject out = response(false, QStringLiteral("gpu_present_frame"));
        out.insert(QStringLiteral("error"), error);
        return out;
    }
    bool parentOk = false;
    const qulonglong parentWindow = stringValue(
        request, QStringLiteral("parent_hwnd")
    ).toULongLong(&parentOk, 10);
    if (!parentOk || parentWindow == 0) {
        QJsonObject out = response(false, QStringLiteral("gpu_present_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("parent_hwnd must be a non-zero decimal string"));
        return out;
    }
    krok::subtitle::native::NativePreviewTarget target;
    target.parentWindow = static_cast<std::uintptr_t>(parentWindow);
    target.x = intValue(request, QStringLiteral("x"), 0);
    target.y = intValue(request, QStringLiteral("y"), 0);
    target.width = intValue(request, QStringLiteral("width"), 0);
    target.height = intValue(request, QStringLiteral("height"), 0);
    if (target.width != config->physicalWidth()
        || target.height != config->physicalHeight()) {
        QJsonObject out = response(false, QStringLiteral("gpu_present_frame"));
        out.insert(
            QStringLiteral("error"),
            QStringLiteral("native preview dimensions must match the configured physical render target")
        );
        return out;
    }
    try {
        const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
        const auto result = backend->presentFrame(tMs, target);
        QJsonObject out = response(true, QStringLiteral("gpu_frame_presented"));
        out.insert(QStringLiteral("generation"), intValue(request, QStringLiteral("generation"), 0));
        out.insert(QStringLiteral("frame_index"), intValue(request, QStringLiteral("frame_index"), 0));
        out.insert(QStringLiteral("t_ms"), tMs);
        out.insert(QStringLiteral("render_ms"), result.renderMs);
        out.insert(QStringLiteral("present_ms"), result.presentMs);
        out.insert(QStringLiteral("readback_ms"), 0.0);
        out.insert(QStringLiteral("child_hwnd"), QString::number(result.childWindow));
        out.insert(QStringLiteral("transport"), QStringLiteral("direct_composition"));
        return out;
    } catch (const std::exception &exception) {
        QJsonObject out = response(false, QStringLiteral("gpu_present_frame"));
        out.insert(QStringLiteral("error"), QString::fromUtf8(exception.what()));
        return out;
    }
}

QJsonObject handleCloseGpuPreview(
    const QJsonObject &request,
    RenderRuntime *runtime
) {
    const bool forceWarp = request.value(QStringLiteral("force_warp")).toBool(false);
    QString error;
    auto *backend = ensureGpuBackend(runtime, forceWarp, &error);
    if (backend == nullptr) {
        QJsonObject out = response(false, QStringLiteral("gpu_preview_close"));
        out.insert(QStringLiteral("error"), error);
        return out;
    }
    backend->closeNativePreview();
    return response(true, QStringLiteral("gpu_preview_closed"));
}

QJsonObject handleCancelGeneration(const QJsonObject &request, RenderRuntime *runtime) {
    const int generation = intValue(request, QStringLiteral("generation"), 0);
    cancelGeneration(runtime, generation);
    QJsonObject out = response(true, QStringLiteral("generation_cancelled"));
    out.insert(QStringLiteral("generation"), generation);
    return out;
}

}  // namespace

int main(int argc, char **argv) {
#if !defined(Q_OS_WIN)
    qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty() ? QByteArray("offscreen") : qgetenv("QT_QPA_PLATFORM"));
#endif
    QApplication app(argc, argv);

    QJsonObject ready = response(true, QStringLiteral("ready"));
    ready.insert(QStringLiteral("schema"), kRenderIrSchema);
    ready.insert(QStringLiteral("gpu_protocol"), 1);
    ready.insert(QStringLiteral("native_preview_protocol"), 1);
    ready.insert(QStringLiteral("qt"), QString::fromLatin1(qVersion()));
    writeJson(ready);

    std::optional<RenderConfig> config;
    RenderRuntime runtime;
    QTextStream input(stdin, QIODevice::ReadOnly);
    while (!input.atEnd()) {
        const QString line = input.readLine().trimmed();
        if (line.isEmpty()) {
            continue;
        }

        QJsonObject parseError;
        const auto request = parseRequestLine(line, &parseError);
        if (!request.has_value()) {
            writeJson(parseError);
            continue;
        }

        const QString commandName = stringValue(*request, QStringLiteral("cmd"));
        switch (commandFromName(commandName)) {
        case Command::BackendInfo:
            writeJson(handleBackendInfo(*request, &runtime));
            break;
        case Command::RenderProbe:
            writeJson(handleRenderProbe(*request, &runtime));
            break;
        case Command::GpuConfigure:
            writeJson(handleConfigureGpu(*request, config, &runtime));
            break;
        case Command::GpuResizeTarget:
            writeJson(handleResizeGpuTarget(*request, &config, &runtime));
            break;
        case Command::GpuRenderFrame:
            if (auto out = handleRenderGpuFrame(*request, config, &runtime)) {
                writeJson(*out);
            }
            break;
        case Command::GpuPresentFrame:
            writeJson(handlePresentGpuFrame(*request, config, &runtime));
            break;
        case Command::GpuPreviewClose:
            writeJson(handleCloseGpuPreview(*request, &runtime));
            break;
        case Command::GpuDiagnostics:
            writeJson(handleGpuDiagnostics(*request, &runtime));
            break;
        case Command::Configure:
            writeJson(handleConfigure(*request, &config));
            break;
        case Command::RenderFrame:
            writeJson(handleRenderFrame(*request, config));
            break;
        case Command::RenderFrameStats:
            writeJson(handleRenderFrameStats(*request, config));
            break;
        case Command::RenderRangeStats:
            writeJson(handleRenderRangeStats(*request, config));
            break;
        case Command::RenderRange:
            writeJson(handleRenderRange(*request, config, &runtime));
            break;
        case Command::CancelGeneration:
            writeJson(handleCancelGeneration(*request, &runtime));
            break;
        case Command::Shutdown:
            runtime.requestShutdown();
            joinRenderJobs(&runtime);
            writeJson(response(true, QStringLiteral("shutdown")));
            return 0;
        case Command::Unknown: {
            QJsonObject out = response(false, QStringLiteral("unknown_command"));
            out.insert(
                QStringLiteral("error"),
                QStringLiteral("unknown command: ") + commandName
            );
            writeJson(out);
            break;
        }
        }
    }

    runtime.requestShutdown();
    joinRenderJobs(&runtime);
    return 0;
}
