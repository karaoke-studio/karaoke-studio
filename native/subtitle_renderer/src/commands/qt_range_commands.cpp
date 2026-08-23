#include "qt_range_commands.h"

#include "../backends/qt/qt_frame_renderer.h"
#include "../backends/qt/qt_render_cache.h"
#include "../backends/qt/qt_render_types.h"
#include "../protocol/json_protocol.h"
#include "../protocol/json_value.h"
#include "../runtime/checksum.h"
#include "../runtime/render_runtime.h"

#include <QtCore/QElapsedTimer>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtGui/QImage>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <mutex>
#include <thread>
#include <vector>

namespace krok::subtitle::native::commands {

using legacy_qt::RangeFrameResult;
using legacy_qt::RenderResult;
using legacy_qt::glowBitmapCacheSize;
using legacy_qt::glowBitmapCacheStats;
using legacy_qt::layoutCacheSize;
using legacy_qt::layoutCacheStats;
using legacy_qt::renderFrame;
using legacy_qt::textLayerCacheSize;
using legacy_qt::textLayerCacheStats;
using protocol::RenderConfig;
using protocol::intValue;
using protocol::parseIntArray;
using protocol::response;
using protocol::stringValue;
using protocol::writeJson;
using runtime::RenderRuntime;
using runtime::SharedFrameRing;
using runtime::imageChecksum;

namespace {

bool generationCancelled(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return false;
    }
    return runtime->generationCancelled(generation);
}

void clearGenerationCancel(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return;
    }
    runtime->clearGenerationCancel(generation);
}

void rememberRenderJob(RenderRuntime *runtime, std::thread job) {
    if (runtime == nullptr) {
        if (job.joinable()) {
            job.detach();
        }
        return;
    }
    runtime->rememberRenderJob(std::move(job));
}

QString defaultSharedMemoryKey(int generation) {
    return runtime::defaultSharedMemoryKey(generation);
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

bool writeSharedFrameSlot(
    RenderRuntime *runtime,
    const RangeFrameResult &result,
    int generation,
    int frameIndex,
    int slotIndex,
    SharedFrameRing *ringOut
) {
    if (runtime == nullptr) {
        return false;
    }
    QImage image = result.image.convertToFormat(QImage::Format_RGBA8888);
    return runtime->writeSharedRgbaSlot(
        image.constBits(),
        image.width(),
        image.height(),
        image.bytesPerLine(),
        generation,
        frameIndex,
        result.tMs,
        slotIndex,
        ringOut
    );
}

std::vector<int> rangeTimestampsFromRequest(
    const QJsonObject &request,
    const RenderConfig &config
) {
    std::vector<int> timestamps = parseIntArray(request.value(QStringLiteral("t_ms")).toArray());
    if (timestamps.empty()) {
        const int startFrame = std::max(0, intValue(request, QStringLiteral("start_frame"), 0));
        const int count = std::max(0, intValue(request, QStringLiteral("count"), 0));
        timestamps.reserve(static_cast<std::size_t>(count));
        for (int index = 0; index < count; ++index) {
            const double frameMs = 1000.0 / static_cast<double>(std::max(config.fps, 1));
            timestamps.push_back(static_cast<int>(std::round((startFrame + index) * frameMs)));
        }
    }
    return timestamps;
}

int rangeWorkerCountFromRequest(
    const QJsonObject &request,
    const RenderConfig &config,
    int frameCount
) {
    const unsigned int hardwareThreads = std::max(1u, std::thread::hardware_concurrency());
    const int requestedThreads = intValue(request, QStringLiteral("threads"), static_cast<int>(hardwareThreads));
    return std::max(1, std::min(requestedThreads, std::max(frameCount, 1)));
}

void launchRenderRangeJob(
    RenderRuntime *runtime,
    RenderConfig config,
    std::vector<int> timestamps,
    int generation,
    int workerCount
) {
    auto job = std::thread([runtime, config = std::move(config), timestamps = std::move(timestamps), generation, workerCount]() {
        std::vector<RangeFrameResult> results(timestamps.size());
        std::vector<bool> ready(timestamps.size(), false);
        std::mutex resultMutex;
        std::condition_variable resultReady;
        std::atomic<int> nextIndex{0};
        std::atomic<int> activeWorkers{workerCount};
        std::atomic<int> completedFrames{0};
        QElapsedTimer totalTimer;
        totalTimer.start();

        auto worker = [&]() {
            while (true) {
                if (generationCancelled(runtime, generation)) {
                    break;
                }
                const int index = nextIndex.fetch_add(1);
                if (index >= static_cast<int>(timestamps.size())) {
                    break;
                }
                QElapsedTimer frameTimer;
                frameTimer.start();
                RenderResult rendered = renderFrame(config, timestamps[static_cast<std::size_t>(index)]);
                const double renderMs = static_cast<double>(frameTimer.nsecsElapsed()) / 1000000.0;
                if (generationCancelled(runtime, generation)) {
                    break;
                }
                {
                    std::lock_guard<std::mutex> lock(resultMutex);
                    results[static_cast<std::size_t>(index)] = RangeFrameResult{
                        timestamps[static_cast<std::size_t>(index)],
                        renderMs,
                        QString::number(imageChecksum(rendered.image)),
                        rendered.diagnostics.visibleLines,
                        std::move(rendered.image),
                    };
                    ready[static_cast<std::size_t>(index)] = true;
                }
                ++completedFrames;
                resultReady.notify_all();
            }
            --activeWorkers;
            resultReady.notify_all();
        };

        std::vector<std::thread> workers;
        workers.reserve(static_cast<std::size_t>(workerCount));
        for (int index = 0; index < workerCount; ++index) {
            workers.emplace_back(worker);
        }

        int nextEmit = 0;
        while (nextEmit < static_cast<int>(timestamps.size())) {
            RangeFrameResult result;
            {
                std::unique_lock<std::mutex> lock(resultMutex);
                resultReady.wait(lock, [&]() {
                    return ready[static_cast<std::size_t>(nextEmit)]
                        || activeWorkers.load() == 0
                        || generationCancelled(runtime, generation);
                });
                if (!ready[static_cast<std::size_t>(nextEmit)]) {
                    break;
                }
                result = results[static_cast<std::size_t>(nextEmit)];
            }
            const int slotIndex = nextEmit % std::max(
                1, runtime->sharedFrameSlotCount()
            );
            SharedFrameRing ring;
            const bool wroteSlot = writeSharedFrameSlot(runtime, result, generation, nextEmit, slotIndex, &ring);
            QJsonObject frame = response(true, QStringLiteral("frame_ready"));
            frame.insert(QStringLiteral("generation"), generation);
            frame.insert(QStringLiteral("frame_index"), nextEmit);
            frame.insert(QStringLiteral("t_ms"), result.tMs);
            frame.insert(QStringLiteral("render_ms"), result.renderMs);
            frame.insert(QStringLiteral("checksum"), result.checksum);
            frame.insert(QStringLiteral("visible_lines"), result.visibleLines);
            frame.insert(QStringLiteral("payload"), wroteSlot ? QStringLiteral("shared_memory") : QStringLiteral("metadata"));
            if (wroteSlot) {
                frame.insert(QStringLiteral("shm_key"), ring.key);
                frame.insert(QStringLiteral("slot_index"), slotIndex);
                frame.insert(QStringLiteral("slot_count"), ring.slotCount);
                frame.insert(QStringLiteral("slot_offset"), slotIndex * ring.slotBytes);
                frame.insert(QStringLiteral("slot_bytes"), ring.slotBytes);
                frame.insert(QStringLiteral("header_bytes"), ring.headerBytes);
                frame.insert(QStringLiteral("payload_offset"), slotIndex * ring.slotBytes + ring.headerBytes);
                frame.insert(QStringLiteral("payload_bytes"), ring.pixelBytes);
                frame.insert(QStringLiteral("width"), ring.width);
                frame.insert(QStringLiteral("height"), ring.height);
                frame.insert(QStringLiteral("stride"), ring.stride);
                frame.insert(QStringLiteral("pixel_format"), ring.pixelFormat);
            }
            writeJson(frame);
            ++nextEmit;
        }

        for (auto &thread : workers) {
            if (thread.joinable()) {
                thread.join();
            }
        }

        const bool cancelled = generationCancelled(runtime, generation);
        QJsonObject done = response(true, QStringLiteral("range_done"));
        done.insert(QStringLiteral("generation"), generation);
        done.insert(QStringLiteral("frames"), static_cast<int>(timestamps.size()));
        done.insert(QStringLiteral("frames_done"), completedFrames.load());
        done.insert(QStringLiteral("frames_emitted"), nextEmit);
        done.insert(QStringLiteral("threads"), workerCount);
        done.insert(QStringLiteral("cancelled"), cancelled);
        done.insert(QStringLiteral("elapsed_ms"), static_cast<double>(totalTimer.nsecsElapsed()) / 1000000.0);
        writeJson(done);
    });
    rememberRenderJob(runtime, std::move(job));
}

}  // namespace

QJsonObject handleRenderRangeStats(
    const QJsonObject &request,
    const std::optional<RenderConfig> &config
) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_range_stats"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    std::vector<int> timestamps = rangeTimestampsFromRequest(request, *config);
    if (timestamps.empty()) {
        QJsonObject out = response(false, QStringLiteral("render_range_stats"));
        out.insert(QStringLiteral("error"), QStringLiteral("t_ms array or positive count is required"));
        return out;
    }

    const int workerCount = rangeWorkerCountFromRequest(request, *config, static_cast<int>(timestamps.size()));
    std::vector<RangeFrameResult> results(timestamps.size());
    std::atomic<int> nextIndex{0};
    QElapsedTimer totalTimer;
    totalTimer.start();

    auto worker = [&]() {
        while (true) {
            const int index = nextIndex.fetch_add(1);
            if (index >= static_cast<int>(timestamps.size())) {
                return;
            }
            QElapsedTimer frameTimer;
            frameTimer.start();
            RenderResult rendered = renderFrame(*config, timestamps[static_cast<std::size_t>(index)]);
            const double renderMs = static_cast<double>(frameTimer.nsecsElapsed()) / 1000000.0;
            results[static_cast<std::size_t>(index)] = RangeFrameResult{
                timestamps[static_cast<std::size_t>(index)],
                renderMs,
                QString::number(imageChecksum(rendered.image)),
                rendered.diagnostics.visibleLines,
            };
        }
    };

    std::vector<std::thread> workers;
    workers.reserve(static_cast<std::size_t>(workerCount));
    for (int index = 0; index < workerCount; ++index) {
        workers.emplace_back(worker);
    }
    for (auto &thread : workers) {
        thread.join();
    }

    const double elapsedMs = static_cast<double>(totalTimer.nsecsElapsed()) / 1000000.0;
    QJsonObject out = response(true, QStringLiteral("range_stats"));
    out.insert(QStringLiteral("frames"), static_cast<int>(timestamps.size()));
    out.insert(QStringLiteral("threads"), workerCount);
    out.insert(QStringLiteral("elapsed_ms"), elapsedMs);
    out.insert(QStringLiteral("fps"), elapsedMs > 0.0 ? (static_cast<double>(timestamps.size()) * 1000.0 / elapsedMs) : 0.0);
    out.insert(QStringLiteral("glow_cache_hits"), glowBitmapCacheStats().hits);
    out.insert(QStringLiteral("glow_cache_misses"), glowBitmapCacheStats().misses);
    out.insert(QStringLiteral("glow_cache_shape_misses"), glowBitmapCacheStats().shapeMisses);
    out.insert(QStringLiteral("glow_cache_content_variant_misses"), glowBitmapCacheStats().contentVariantMisses);
    out.insert(QStringLiteral("glow_cache_evicted_key_misses"), glowBitmapCacheStats().evictedKeyMisses);
    out.insert(QStringLiteral("glow_cache_size"), glowBitmapCacheSize());
    out.insert(QStringLiteral("text_layer_cache_hits"), textLayerCacheStats().hits);
    out.insert(QStringLiteral("text_layer_cache_misses"), textLayerCacheStats().misses);
    out.insert(QStringLiteral("text_layer_cache_size"), textLayerCacheSize());
    out.insert(QStringLiteral("layout_cache_hits"), layoutCacheStats().hits);
    out.insert(QStringLiteral("layout_cache_misses"), layoutCacheStats().misses);
    out.insert(QStringLiteral("layout_cache_size"), layoutCacheSize());
    QJsonObject missesByScope;
    const auto scopeKeys = glowBitmapCacheStats().missesByScope.keys();
    for (const QString &scope : scopeKeys) {
        missesByScope.insert(scope, glowBitmapCacheStats().missesByScope.value(scope));
    }
    out.insert(QStringLiteral("glow_cache_misses_by_scope"), missesByScope);

    QJsonArray frames;
    for (const RangeFrameResult &result : results) {
        QJsonObject item;
        item.insert(QStringLiteral("t_ms"), result.tMs);
        item.insert(QStringLiteral("render_ms"), result.renderMs);
        item.insert(QStringLiteral("checksum"), result.checksum);
        item.insert(QStringLiteral("visible_lines"), result.visibleLines);
        frames.append(item);
    }
    out.insert(QStringLiteral("frame_stats"), frames);
    return out;
}

QJsonObject handleRenderRange(
    const QJsonObject &request,
    const std::optional<RenderConfig> &config,
    RenderRuntime *runtime
) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_range"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    std::vector<int> timestamps = rangeTimestampsFromRequest(request, *config);
    if (timestamps.empty()) {
        QJsonObject out = response(false, QStringLiteral("render_range"));
        out.insert(QStringLiteral("error"), QStringLiteral("t_ms array or positive count is required"));
        return out;
    }

    const int generation = intValue(request, QStringLiteral("generation"), 0);
    clearGenerationCancel(runtime, generation);
    const int workerCount = rangeWorkerCountFromRequest(request, *config, static_cast<int>(timestamps.size()));
    const QString shmKey = stringValue(
        request,
        QStringLiteral("shm_key"),
        defaultSharedMemoryKey(generation)
    );
    const int ringSlots = std::max(1, intValue(request, QStringLiteral("ring_slots"), 3));
    QString shmError;
    if (!ensureSharedFrameRing(runtime, shmKey, ringSlots, config->physicalWidth(), config->physicalHeight(), &shmError)) {
        QJsonObject out = response(false, QStringLiteral("render_range"));
        out.insert(QStringLiteral("generation"), generation);
        out.insert(QStringLiteral("error"), QStringLiteral("failed to create shared memory: ") + shmError);
        return out;
    }
    QJsonObject out = response(true, QStringLiteral("range_started"));
    out.insert(QStringLiteral("generation"), generation);
    out.insert(QStringLiteral("frames"), static_cast<int>(timestamps.size()));
    out.insert(QStringLiteral("threads"), workerCount);
    out.insert(QStringLiteral("shm_key"), shmKey);
    out.insert(QStringLiteral("ring_slots"), ringSlots);
    out.insert(QStringLiteral("width"), config->width);
    out.insert(QStringLiteral("height"), config->height);
    launchRenderRangeJob(runtime, *config, std::move(timestamps), generation, workerCount);
    return out;
}

}  // namespace krok::subtitle::native::commands
