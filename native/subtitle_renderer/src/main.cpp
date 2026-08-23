#include <QtCore/QByteArray>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QElapsedTimer>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QHash>
#include <QtCore/QPointF>
#include <QtCore/QSet>
#include <QtCore/QTextStream>
#include <QtGui/QBrush>
#include <QtGui/QColor>
#include <QtGui/QFont>
#include <QtGui/QFontDatabase>
#include <QtGui/QFontMetricsF>
#include <QtGui/QImage>
#include <QtGui/QLinearGradient>
#include <QtGui/QPainter>
#include <QtGui/QPainterPath>
#include <QtGui/QPen>
#include <QtGui/QPixmap>
#include <QtGui/QRegion>
#include <QtGui/QTransform>
#include <QtWidgets/QApplication>
#include <QtWidgets/QGraphicsBlurEffect>
#include <QtWidgets/QGraphicsPixmapItem>
#include <QtWidgets/QGraphicsScene>

#include "backends/direct2d/d2d_backend.h"
#include "backends/qt/qt_render_types.h"
#include "protocol/json_protocol.h"
#include "protocol/json_value.h"
#include "protocol/render_config.h"
#include "protocol/render_config_parser.h"
#include "runtime/gpu_preview_worker_pool.h"
#include "runtime/render_runtime.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cmath>
#include <deque>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <cstring>
#include <thread>
#include <vector>

namespace {

using krok::subtitle::native::protocol::kRenderIrSchema;
using krok::subtitle::native::protocol::Command;
using krok::subtitle::native::protocol::commandFromName;
using krok::subtitle::native::protocol::intValue;
using krok::subtitle::native::protocol::parseRequestLine;
using krok::subtitle::native::protocol::parseIntArray;
using krok::subtitle::native::protocol::PaintFillSpec;
using krok::subtitle::native::protocol::parseRenderConfig;
using krok::subtitle::native::protocol::RenderConfig;
using krok::subtitle::native::protocol::ResolvedLineLayout;
using krok::subtitle::native::protocol::ResolvedStyle;
using krok::subtitle::native::protocol::resolvedStyleForCharacter;
using krok::subtitle::native::protocol::resolvedStyleForLine;
using krok::subtitle::native::protocol::resolvedStyleFromTitle;
using krok::subtitle::native::protocol::resolvedStyleKey;
using krok::subtitle::native::protocol::response;
using krok::subtitle::native::protocol::stringValue;
using krok::subtitle::native::protocol::RubyAnnotation;
using krok::subtitle::native::protocol::TimingChar;
using krok::subtitle::native::protocol::TimingLine;
using krok::subtitle::native::protocol::writeJson;
using krok::subtitle::native::legacy_qt::LineLayout;
using krok::subtitle::native::legacy_qt::LineDiagnostics;
using krok::subtitle::native::legacy_qt::DisplayLineRef;
using krok::subtitle::native::legacy_qt::RubyDiagnostics;
using krok::subtitle::native::legacy_qt::RubyLayerImage;
using krok::subtitle::native::legacy_qt::TextLayerImage;
using krok::subtitle::native::legacy_qt::GlyphRunRef;
using krok::subtitle::native::legacy_qt::RubyGroupInfo;
using krok::subtitle::native::legacy_qt::RubyUnitLayout;
using krok::subtitle::native::legacy_qt::LineCharTransition;
using krok::subtitle::native::legacy_qt::AnimationState;
using krok::subtitle::native::legacy_qt::ImageFillCacheEntry;
using krok::subtitle::native::legacy_qt::GlowBitmapCacheEntry;
using krok::subtitle::native::legacy_qt::TextLayerCacheEntry;
using krok::subtitle::native::legacy_qt::LayoutCacheEntry;
using krok::subtitle::native::legacy_qt::GlowBitmapCacheKeyParts;
using krok::subtitle::native::legacy_qt::GlowBitmapCacheMissDiagnostic;
using krok::subtitle::native::legacy_qt::GlowLayerImage;
using krok::subtitle::native::legacy_qt::GlowBitmapCacheStats;
using krok::subtitle::native::legacy_qt::TextLayerCacheStats;
using krok::subtitle::native::legacy_qt::LayoutCacheStats;
using krok::subtitle::native::legacy_qt::RenderDiagnostics;
using krok::subtitle::native::legacy_qt::RenderResult;
using krok::subtitle::native::legacy_qt::RangeFrameResult;
using krok::subtitle::native::runtime::GpuPreviewWorkerPool;
using krok::subtitle::native::runtime::GpuPreviewPoolCacheEntry;
using krok::subtitle::native::runtime::RenderRuntime;
using krok::subtitle::native::runtime::SharedFrameRing;

constexpr double kPi = 3.14159265358979323846;
constexpr int kUtopiaIntroTimeMs = 700;
constexpr int kUtopiaIntroDelayMs = 200;
constexpr int kUtopiaIntroEnlargeMs = 400;
constexpr int kUtopiaIntroCondenseMs = 100;
constexpr double kUtopiaIntroOverRatio = 1.3;
constexpr double kUtopiaWipeOverRatio = 1.15;
constexpr double kUtopiaWipeOverTimeRatio = 0.25;
constexpr int kUtopiaWipeOverTimeLimitMs = 100;
constexpr int kUtopiaFadeOutTimeMs = 750;


QString fontCacheKey(const QFont &font);
std::optional<LineLayout> lookupLayoutCache(const QString &key);
void storeLayoutCache(const QString &key, const LineLayout &layout);


bool generationCancelled(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return false;
    }
    return runtime->jobs.generationCancelled(generation);
}

void cancelGeneration(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return;
    }
    runtime->jobs.cancelGeneration(generation);
}

void clearGenerationCancel(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return;
    }
    runtime->jobs.clearGenerationCancel(generation);
}

void rememberRenderJob(RenderRuntime *runtime, std::thread job) {
    if (runtime == nullptr) {
        if (job.joinable()) {
            job.detach();
        }
        return;
    }
    runtime->jobs.remember(std::move(job));
}

void joinRenderJobs(RenderRuntime *runtime) {
    if (runtime == nullptr) {
        return;
    }
    runtime->jobs.joinAll();
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
    return runtime->sharedFrames.ensure(
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
    return writeSharedRgbaSlot(
        runtime,
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
    return runtime->sharedFrames.writeRgba(
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
    return runtime->sharedFrames.writePremultipliedBgra(
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
    return runtime->sharedFrames.writeBands(
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

std::uint64_t imageChecksum(const QImage &image) {
    const uchar *data = image.constBits();
    const qsizetype size = image.sizeInBytes();
    std::uint64_t hash = 1469598103934665603ull;
    const qsizetype step = std::max<qsizetype>(1, size / 4096);
    for (qsizetype i = 0; i < size; i += step) {
        hash ^= static_cast<std::uint64_t>(data[i]);
        hash *= 1099511628211ull;
    }
    return hash;
}

std::uint64_t imageFullChecksum(const QImage &image) {
    const uchar *data = image.constBits();
    const qsizetype size = image.sizeInBytes();
    std::uint64_t hash = 1469598103934665603ull;
    for (qsizetype i = 0; i < size; ++i) {
        hash ^= static_cast<std::uint64_t>(data[i]);
        hash *= 1099511628211ull;
    }
    return hash;
}

std::uint64_t bytesChecksum(const std::uint8_t *data, std::size_t size) {
    std::uint64_t hash = 1469598103934665603ull;
    for (std::size_t index = 0; index < size; ++index) {
        hash ^= static_cast<std::uint64_t>(data[index]);
        hash *= 1099511628211ull;
    }
    return hash;
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

krok::subtitle::native::RenderBackend *ensureGpuBackend(
    RenderRuntime *runtime,
    bool forceWarp,
    QString *error
) {
    if (runtime == nullptr) {
        if (error != nullptr) {
            *error = QStringLiteral("render runtime is unavailable");
        }
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(runtime->gpuBackendMutex);
    auto &backend = forceWarp ? runtime->warpGpuBackend : runtime->hardwareGpuBackend;
    if (backend == nullptr) {
        try {
            backend = std::make_unique<krok::subtitle::native::Direct2DGpuBackend>(forceWarp);
            const auto caps = backend->capabilities();
            std::cerr
                << "gpu_backend=direct2d adapter=\"" << caps.adapterName
                << "\" feature_level=" << caps.featureLevel
                << " warp=" << (caps.warp ? 1 : 0) << std::endl;
        } catch (const std::exception &exception) {
            if (error != nullptr) {
                *error = QString::fromUtf8(exception.what());
            }
            return nullptr;
        }
    }
    return backend.get();
}

GpuPreviewWorkerPool *gpuPreviewPool(RenderRuntime *runtime, bool forceWarp) {
    if (runtime == nullptr) {
        return nullptr;
    }
    return forceWarp
        ? runtime->warpGpuPreviewPool.get()
        : runtime->hardwareGpuPreviewPool.get();
}

void appendSharedRingMetadata(QJsonObject &out, const SharedFrameRing &ring, int slotIndex) {
    out.insert(QStringLiteral("payload"), QStringLiteral("shared_memory"));
    out.insert(QStringLiteral("shm_key"), ring.key);
    out.insert(QStringLiteral("slot_index"), slotIndex);
    out.insert(QStringLiteral("slot_count"), ring.slotCount);
    out.insert(QStringLiteral("slot_offset"), slotIndex * ring.slotBytes);
    out.insert(QStringLiteral("slot_bytes"), ring.slotBytes);
    out.insert(QStringLiteral("header_bytes"), ring.headerBytes);
    out.insert(QStringLiteral("payload_offset"), slotIndex * ring.slotBytes + ring.headerBytes);
    out.insert(QStringLiteral("payload_bytes"), ring.pixelBytes);
    out.insert(QStringLiteral("width"), ring.width);
    out.insert(QStringLiteral("height"), ring.height);
    out.insert(QStringLiteral("stride"), ring.stride);
    out.insert(QStringLiteral("pixel_format"), ring.pixelFormat);
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
        appendSharedRingMetadata(out, ring, slotIndex);
        return out;
    } catch (const std::exception &exception) {
        QJsonObject out = response(false, QStringLiteral("render_probe"));
        out.insert(QStringLiteral("error"), QString::fromUtf8(exception.what()));
        return out;
    }
}


QString lineText(const TimingLine &line) {
    QString text;
    for (const auto &ch : line.chars) {
        text += ch.text;
    }
    return text;
}

bool lineHasRoleLabels(const TimingLine &line) {
    for (const auto &ch : line.chars) {
        if (!ch.roleLabel.isEmpty()) {
            return true;
        }
    }
    return false;
}


int lineStartMs(const TimingLine &line) {
    if (line.chars.empty()) {
        return 0;
    }
    return line.chars.front().startMs;
}

bool lineVisible(const TimingLine &line, int tMs, const RenderConfig &cfg) {
    if (line.chars.empty()) {
        return false;
    }
    const int start = lineStartMs(line) - cfg.lineLeadInMs;
    const int end = std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs;
    return start <= tMs && tMs <= end;
}

int lineEndMs(const TimingLine &line) {
    if (line.endMs > 0) {
        return line.endMs;
    }
    if (!line.chars.empty()) {
        return line.chars.back().startMs + 1000;
    }
    return 0;
}

int effectiveLineProtectMs(const RenderConfig &cfg) {
    if (cfg.lineProtectMs > 0) {
        return cfg.lineProtectMs;
    }
    const int base = std::min(std::max(cfg.lineLeadInMs, 0), std::max(cfg.lineTailMs, 0)) / 2;
    return std::max(base, std::max(cfg.exitFadeMs, 0));
}

int pairSingEndMs(const std::vector<const TimingLine *> &lines, int index) {
    const int pairStart = (index / 2) * 2;
    int best = 0;
    for (int i = pairStart; i < std::min(pairStart + 2, static_cast<int>(lines.size())); ++i) {
        best = std::max(best, lineEndMs(*lines[static_cast<std::size_t>(i)]));
    }
    return best;
}

std::vector<int> computeSectionIds(const std::vector<const TimingLine *> &lines, int sectionGapMs) {
    std::vector<int> sectionIds;
    sectionIds.reserve(lines.size());
    int current = 0;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        if (i > 0 && sectionGapMs > 0) {
            const int gap = lineStartMs(*lines[i]) - lineEndMs(*lines[i - 1]);
            if (gap > sectionGapMs) {
                ++current;
            }
        }
        sectionIds.push_back(current);
    }
    return sectionIds;
}

QHash<int, int> computeSectionEnds(
    const std::vector<const TimingLine *> &lines,
    const std::vector<int> &sectionIds,
    int tailMs
) {
    QHash<int, int> ends;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        const int sid = sectionIds[i];
        const int end = lineEndMs(*lines[i]) + std::max(tailMs, 0);
        ends.insert(sid, std::max(ends.value(sid, end), end));
    }
    return ends;
}

bool isLastInLaneInSection(
    const std::vector<int> &lanes,
    const std::vector<int> &sectionIds,
    int index
) {
    const int lane = lanes[static_cast<std::size_t>(index)];
    const int sid = sectionIds[static_cast<std::size_t>(index)];
    for (int i = index + 1; i < static_cast<int>(lanes.size()); ++i) {
        if (sectionIds[static_cast<std::size_t>(i)] != sid) {
            break;
        }
        if (lanes[static_cast<std::size_t>(i)] == lane) {
            return false;
        }
    }
    return true;
}

void adjustSameLaneDisplayWindows(
    const std::vector<const TimingLine *> &lines,
    std::vector<int> *starts,
    std::vector<int> *displayEnds,
    const std::vector<int> &lanes,
    int protectMs,
    int laneGapMs
) {
    QHash<int, int> previousByLane;
    for (int index = 0; index < static_cast<int>(lines.size()); ++index) {
        const int lane = lanes[static_cast<std::size_t>(index)];
        if (!previousByLane.contains(lane)) {
            previousByLane.insert(lane, index);
            continue;
        }

        const int previous = previousByLane.value(lane);
        if ((*displayEnds)[static_cast<std::size_t>(previous)] + laneGapMs <= (*starts)[static_cast<std::size_t>(index)]) {
            previousByLane.insert(lane, index);
            continue;
        }

        const int previousFloor = lineEndMs(*lines[static_cast<std::size_t>(previous)]) + protectMs;
        const int currentProtectStart = std::max(lineStartMs(*lines[static_cast<std::size_t>(index)]) - protectMs, 0);

        (*displayEnds)[static_cast<std::size_t>(previous)] = std::max(
            previousFloor,
            (*starts)[static_cast<std::size_t>(index)] - laneGapMs
        );
        if ((*displayEnds)[static_cast<std::size_t>(previous)] + laneGapMs <= (*starts)[static_cast<std::size_t>(index)]) {
            previousByLane.insert(lane, index);
            continue;
        }

        const int targetStart = (*displayEnds)[static_cast<std::size_t>(previous)] + laneGapMs;
        const int latestStart = std::max(currentProtectStart, (*starts)[static_cast<std::size_t>(index)]);
        (*starts)[static_cast<std::size_t>(index)] = std::min(
            std::max((*starts)[static_cast<std::size_t>(index)], targetStart),
            latestStart
        );
        if ((*displayEnds)[static_cast<std::size_t>(previous)] + laneGapMs <= (*starts)[static_cast<std::size_t>(index)]) {
            previousByLane.insert(lane, index);
            continue;
        }

        if ((*displayEnds)[static_cast<std::size_t>(previous)] <= (*starts)[static_cast<std::size_t>(index)]) {
            previousByLane.insert(lane, index);
            continue;
        }

        if ((*displayEnds)[static_cast<std::size_t>(previous)] <= currentProtectStart) {
            (*starts)[static_cast<std::size_t>(index)] = (*displayEnds)[static_cast<std::size_t>(previous)];
        }
        previousByLane.insert(lane, index);
    }
}

std::vector<DisplayLineRef> computeDisplayLines(const RenderConfig &cfg) {
    std::vector<const TimingLine *> lines;
    lines.reserve(cfg.lines.size());
    for (const auto &line : cfg.lines) {
        if (!line.chars.empty()) {
            lines.push_back(&line);
        }
    }
    if (lines.empty()) {
        return {};
    }

    const int lead = std::max(cfg.lineLeadInMs, 0);
    const int tail = std::max(cfg.lineTailMs, 0);
    const int protect = effectiveLineProtectMs(cfg);
    const int laneGap = std::max(cfg.lineLaneGapMs, 0);
    const int snap = std::max(cfg.lineContinuitySnapMs, 0);
    const int pairSecondDelay = std::max(cfg.linePairSecondDelayMs, 0);
    const int maxHold = std::max(cfg.lineMaxHoldMs, 0);
    const int sectionGap = std::max(cfg.sectionGapMs, 0);

    const std::vector<int> sectionIds = computeSectionIds(lines, sectionGap);
    const QHash<int, int> sectionEnds = computeSectionEnds(lines, sectionIds, tail);

    std::vector<int> starts;
    std::vector<int> naturalEnds;
    std::vector<int> lanes;
    starts.reserve(lines.size());
    naturalEnds.reserve(lines.size());
    lanes.reserve(lines.size());
    QHash<int, int> prevLaneNaturalEnd;

    for (int index = 0; index < static_cast<int>(lines.size()); ++index) {
        const TimingLine &line = *lines[static_cast<std::size_t>(index)];
        const int lane = index % 2;
        lanes.push_back(lane);
        int preferredStart = std::max(lineStartMs(line) - lead, 0);
        if (
            index % 2 == 1
            && !starts.empty()
            && sectionIds[static_cast<std::size_t>(index)] == sectionIds[static_cast<std::size_t>(index - 1)]
        ) {
            preferredStart = std::min(preferredStart, starts[static_cast<std::size_t>(index - 1)] + pairSecondDelay);
        }

        int naturalEnd = pairSingEndMs(lines, index) + tail;
        if (maxHold > 0) {
            naturalEnd = std::min(naturalEnd, preferredStart + maxHold);
        }
        naturalEnds.push_back(naturalEnd);

        int displayStart = preferredStart;
        if (prevLaneNaturalEnd.contains(lane)) {
            const int availableStart = prevLaneNaturalEnd.value(lane) + laneGap;
            displayStart = std::abs(preferredStart - availableStart) <= snap
                ? availableStart
                : preferredStart;
        }
        starts.push_back(displayStart);
        prevLaneNaturalEnd.insert(lane, naturalEnd);
    }

    std::vector<int> displayEnds;
    displayEnds.reserve(lines.size());
    for (int index = 0; index < static_cast<int>(lines.size()); ++index) {
        const int floorEnd = lineEndMs(*lines[static_cast<std::size_t>(index)]) + protect;
        int displayEnd = std::max(naturalEnds[static_cast<std::size_t>(index)], floorEnd);
        if (maxHold > 0) {
            displayEnd = std::max(floorEnd, std::min(displayEnd, starts[static_cast<std::size_t>(index)] + maxHold));
        }
        displayEnds.push_back(displayEnd);
    }

    adjustSameLaneDisplayWindows(lines, &starts, &displayEnds, lanes, protect, laneGap);

    std::vector<DisplayLineRef> result;
    result.reserve(lines.size());
    for (int index = 0; index < static_cast<int>(lines.size()); ++index) {
        const int sid = sectionIds[static_cast<std::size_t>(index)];
        const int floorEnd = lineEndMs(*lines[static_cast<std::size_t>(index)]) + protect;
        int displayEnd = std::max(displayEnds[static_cast<std::size_t>(index)], floorEnd);
        if (maxHold > 0) {
            displayEnd = std::max(floorEnd, std::min(displayEnd, starts[static_cast<std::size_t>(index)] + maxHold));
        }
        if (cfg.syncEnding && isLastInLaneInSection(lanes, sectionIds, index)) {
            displayEnd = std::max(displayEnd, sectionEnds.value(sid, displayEnd));
        }
        if (cfg.sectionEndingMode == QStringLiteral("clear")) {
            displayEnd = std::max(floorEnd, std::min(displayEnd, sectionEnds.value(sid, displayEnd)));
        }
        if (displayEnd < starts[static_cast<std::size_t>(index)]) {
            displayEnd = starts[static_cast<std::size_t>(index)];
        }
        result.push_back(DisplayLineRef{
            lines[static_cast<std::size_t>(index)],
            lanes[static_cast<std::size_t>(index)],
            starts[static_cast<std::size_t>(index)],
            displayEnd,
        });
    }
    return result;
}

std::vector<DisplayLineRef> visibleDisplayLines(const RenderConfig &cfg, int tMs) {
    if (!cfg.dualLineLayout) {
        std::vector<DisplayLineRef> candidates;
        candidates.reserve(cfg.lines.size());
        for (const auto &line : cfg.lines) {
            if (!line.chars.empty() && lineVisible(line, tMs, cfg)) {
                candidates.push_back(DisplayLineRef{
                    &line,
                    0,
                    std::max(lineStartMs(line) - cfg.lineLeadInMs, 0),
                    std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs,
                });
            }
        }
        if (candidates.empty()) {
            return {};
        }
        std::stable_sort(candidates.begin(), candidates.end(), [](const DisplayLineRef &left, const DisplayLineRef &right) {
            return lineStartMs(*left.line) > lineStartMs(*right.line);
        });
        return {candidates.front()};
    }

    const auto all = computeDisplayLines(cfg);
    std::vector<DisplayLineRef> visible;
    visible.reserve(all.size());
    for (const auto &item : all) {
        if (item.line != nullptr && item.displayStartMs <= tMs && tMs < item.displayEndMs) {
            visible.push_back(item);
        }
    }
    return visible;
}

int charEndMs(const TimingLine &line, std::size_t index) {
    if (index >= line.chars.size()) {
        return 0;
    }
    const TimingChar &ch = line.chars[index];
    if (ch.resolvedEndMs.has_value()) {
        return std::max(ch.startMs, ch.resolvedEndMs.value());
    }
    if (ch.pauseReleaseMs.has_value()) {
        return std::max(ch.startMs, ch.pauseReleaseMs.value());
    }
    if (index + 1 < line.chars.size()) {
        return std::max(ch.startMs, line.chars[index + 1].startMs);
    }
    if (line.endMs > ch.startMs) {
        return line.endMs;
    }
    return ch.startMs + 1;
}

double progressRatio(int startMs, int endMs, int tMs) {
    if (endMs <= startMs) {
        return tMs >= startMs ? 1.0 : 0.0;
    }
    const double raw = static_cast<double>(tMs - startMs) / static_cast<double>(endMs - startMs);
    return std::clamp(raw, 0.0, 1.0);
}

int lineDisplayEndMs(const TimingLine &line, const RenderConfig &cfg) {
    if (line.chars.empty()) {
        return 0;
    }
    return std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs;
}

int nextValidCharIndex(const TimingLine &line, std::size_t startIndex) {
    for (std::size_t i = startIndex; i < line.chars.size(); ++i) {
        if (!line.chars[i].text.trimmed().isEmpty()) {
            return static_cast<int>(i);
        }
    }
    return -1;
}

int utopiaTailDelayMs(const RenderConfig &cfg) {
    return std::max(0, cfg.lineTailMs - kUtopiaFadeOutTimeMs);
}

int utopiaFollowingDoneTime(
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index,
    const RenderConfig &cfg
) {
    if (intervals.empty()) {
        return line.endMs;
    }
    index = std::clamp(index, 0, static_cast<int>(intervals.size()) - 1);
    const int currentEnd = intervals[static_cast<std::size_t>(index)].second;
    const int nextIndex = nextValidCharIndex(line, static_cast<std::size_t>(index + 1));
    if (nextIndex >= 0 && nextIndex < static_cast<int>(intervals.size())) {
        const int nextEnd = intervals[static_cast<std::size_t>(nextIndex)].second;
        if (currentEnd <= nextEnd) {
            return nextEnd;
        }
    }
    return currentEnd + utopiaTailDelayMs(cfg);
}

bool isUtopiaWiping(int tMs, int charStartMs, int charEndMs) {
    return charStartMs < tMs && tMs < charEndMs && charStartMs != charEndMs;
}

double utopiaWipeScale(int tMs, int charStartMs, int charEndMs) {
    if (!isUtopiaWiping(tMs, charStartMs, charEndMs)) {
        return 1.0;
    }
    const int overMs = std::min(
        static_cast<int>((charEndMs - charStartMs) * kUtopiaWipeOverTimeRatio),
        kUtopiaWipeOverTimeLimitMs
    );
    if (overMs <= 0) {
        return 1.0;
    }
    const int peakMs = charStartMs + overMs;
    double progress = 0.0;
    if (tMs <= peakMs) {
        progress = static_cast<double>(tMs - charStartMs) / overMs;
    } else {
        const int releaseMs = std::max(charEndMs - peakMs, 1);
        progress = static_cast<double>(charEndMs - tMs) / releaseMs;
    }
    return 1.0 + (kUtopiaWipeOverRatio - 1.0) * std::clamp(progress, 0.0, 1.0);
}

bool utopiaKaraokeEnabled(const RenderConfig &cfg) {
    if (cfg.karaokeAnim == QStringLiteral("utopia")) {
        return true;
    }
    if (cfg.karaokeAnim == QStringLiteral("none")) {
        return false;
    }
    return cfg.entryAnim == QStringLiteral("utopia")
        || cfg.exitAnim == QStringLiteral("utopia");
}

std::optional<LineCharTransition> lineCharTransitionContext(
    const RenderConfig &cfg,
    const TimingLine &line,
    int tMs,
    const std::vector<std::pair<int, int>> &intervals
) {
    if (line.chars.empty()) {
        return std::nullopt;
    }
    if (cfg.entryAnim != QStringLiteral("utopia")
        && cfg.exitAnim != QStringLiteral("utopia")
        && !utopiaKaraokeEnabled(cfg)) {
        return std::nullopt;
    }

    const int start = lineStartMs(line);
    const int end = lineDisplayEndMs(line, cfg);
    const bool inIntro = cfg.entryAnim == QStringLiteral("utopia") && tMs <= start + kUtopiaIntroTimeMs;
    const bool inExit = cfg.exitAnim == QStringLiteral("utopia")
        && !intervals.empty()
        && utopiaFollowingDoneTime(line, intervals, 0, cfg) <= tMs
        && tMs <= end;
    bool inWipe = false;
    if (utopiaKaraokeEnabled(cfg)) {
        for (const auto &interval : intervals) {
            if (isUtopiaWiping(tMs, interval.first, interval.second)) {
                inWipe = true;
                break;
            }
        }
    }
    if (!inIntro && !inExit && !inWipe) {
        return std::nullopt;
    }

    return LineCharTransition{
        QStringLiteral("utopia"),
        QStringLiteral("utopia"),
        1.0,
        start,
        end,
    };
}

QTransform characterTransform(
    double centerX,
    double centerY,
    const AnimationState &state,
    std::optional<QPointF> scaleOrigin = std::nullopt
) {
    QTransform transform;
    if (state.dx == 0.0
        && state.dy == 0.0
        && state.rotation == 0.0
        && state.scaleX == 1.0
        && state.scaleY == 1.0
        && state.skewY == 0.0) {
        return transform;
    }
    if (scaleOrigin.has_value()) {
        transform.translate(scaleOrigin->x() + state.dx, scaleOrigin->y() + state.dy);
        if (state.skewY != 0.0) {
            transform.shear(0.0, state.skewY);
        }
        if (state.scaleX != 1.0 || state.scaleY != 1.0) {
            transform.scale(state.scaleX, state.scaleY);
        }
        transform.translate(centerX - scaleOrigin->x(), centerY - scaleOrigin->y());
        if (state.rotation != 0.0) {
            transform.rotate(state.rotation);
        }
        transform.translate(-centerX, -centerY);
        return transform;
    }
    transform.translate(centerX + state.dx, centerY + state.dy);
    if (state.rotation != 0.0) {
        transform.rotate(state.rotation);
    }
    if (state.skewY != 0.0) {
        transform.shear(0.0, state.skewY);
    }
    if (state.scaleX != 1.0 || state.scaleY != 1.0) {
        transform.scale(state.scaleX, state.scaleY);
    }
    transform.translate(-centerX, -centerY);
    return transform;
}

AnimationState transitionCharState(
    const RenderConfig &cfg,
    const LineCharTransition &transition,
    const std::vector<std::pair<int, int>> &intervals,
    int index,
    int count,
    int tMs,
    int frameHeight,
    int followingDoneMs,
    std::optional<std::pair<int, int>> overrideInterval = std::nullopt
) {
    if (transition.effect == QStringLiteral("utopia") && transition.phase == QStringLiteral("utopia")) {
        if (cfg.entryAnim == QStringLiteral("utopia") && tMs <= transition.startMs + kUtopiaIntroTimeMs) {
            const int delay = count <= 1 ? 0 : kUtopiaIntroDelayMs / (count - 1) * index;
            const int elapsed = tMs - transition.startMs - delay;
            if (elapsed < 0) {
                return AnimationState{0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
            }
            const double opacity = std::min(static_cast<double>(elapsed) / kUtopiaIntroEnlargeMs, 1.0);
            double scale = 1.0;
            if (elapsed < kUtopiaIntroEnlargeMs) {
                scale = kUtopiaIntroOverRatio * static_cast<double>(elapsed) / kUtopiaIntroEnlargeMs;
            } else if (elapsed < kUtopiaIntroEnlargeMs + kUtopiaIntroCondenseMs) {
                const int remaining = kUtopiaIntroEnlargeMs + kUtopiaIntroCondenseMs - elapsed;
                scale = 1.0 + (kUtopiaIntroOverRatio - 1.0) * static_cast<double>(remaining) / kUtopiaIntroCondenseMs;
            }
            return AnimationState{opacity, 0.0, 0.0, 0.0, scale, scale, 0.0};
        }

        if (cfg.exitAnim == QStringLiteral("utopia") && tMs > followingDoneMs) {
            double local = static_cast<double>(tMs - followingDoneMs) / kUtopiaFadeOutTimeMs;
            local = std::clamp(local, 0.0, 1.0);
            const double opacity = std::max(0.0, 1.0 - local);
            const double shrink = 1.0 - local;
            const double amp = std::max(frameHeight, 1) / 15.0;
            const double xTravel = local <= 0.5
                ? std::sin(kPi * local) * amp
                : amp + std::sin((local - 0.5) * kPi) * amp;
            const double yTravel = std::sin(kPi * local / 2.0) * amp;
            const double xFlip = std::cos(kPi * local);
            return AnimationState{
                opacity,
                -xTravel,
                yTravel,
                -180.0 * local,
                shrink * xFlip,
                shrink,
                0.0,
            };
        }

        if (utopiaKaraokeEnabled(cfg)
            && (overrideInterval.has_value()
                || (index >= 0 && index < static_cast<int>(intervals.size())))) {
            const auto interval = overrideInterval.has_value()
                ? overrideInterval.value()
                : intervals[static_cast<std::size_t>(index)];
            if (isUtopiaWiping(tMs, interval.first, interval.second)) {
                const double scale = utopiaWipeScale(tMs, interval.first, interval.second);
                return AnimationState{1.0, 0.0, 0.0, 0.0, scale, scale, 0.0};
            }
        }
    }

    return AnimationState{};
}

QColor colorValue(const QString &value, const QColor &fallback) {
    const QColor color(value);
    return color.isValid() ? color : fallback;
}

QColor validColor(const QString &value, const QString &fallback) {
    const QColor color(value);
    if (color.isValid()) {
        return color;
    }
    const QColor fallbackColor(fallback);
    return fallbackColor.isValid() ? fallbackColor : QColor(QStringLiteral("#FFFFFF"));
}

std::vector<ImageFillCacheEntry> &imageFillCache() {
    static std::vector<ImageFillCacheEntry> cache;
    return cache;
}

std::mutex &imageFillCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

QString imageFillCacheKey(const QString &path) {
    return QFileInfo(path).absoluteFilePath();
}

QImage cachedFillImage(const QString &path) {
    if (path.isEmpty()) {
        return QImage();
    }

    const QString key = imageFillCacheKey(path);
    std::lock_guard<std::mutex> lock(imageFillCacheMutex());
    auto &cache = imageFillCache();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            ImageFillCacheEntry entry = *it;
            cache.erase(it);
            cache.push_back(entry);
            return entry.image;
        }
    }

    QImage image(path);
    if (image.isNull()) {
        return QImage();
    }

    constexpr std::size_t kImageFillCacheMax = 64;
    if (cache.size() >= kImageFillCacheMax) {
        cache.erase(cache.begin());
    }
    cache.push_back(ImageFillCacheEntry{key, image});
    return image;
}

QBrush brushForFill(const PaintFillSpec &fill, const QRectF &rect) {
    if (fill.mode == QStringLiteral("image") && !fill.imagePath.isEmpty()) {
        const QImage image = cachedFillImage(fill.imagePath);
        if (!image.isNull()) {
            QBrush brush(image);
            const double scale = std::max(fill.imageScalePct, 1) / 100.0;
            QTransform transform;
            transform.scale(1.0 / scale, 1.0 / scale);
            transform.translate(rect.left(), rect.top());
            brush.setTransform(transform);
            return brush;
        }
    }
    if (fill.mode == QStringLiteral("gradient_horizontal")
        || fill.mode == QStringLiteral("gradient_vertical")) {
        const bool horizontal = fill.mode == QStringLiteral("gradient_horizontal");
        const QPointF start = horizontal
            ? QPointF(rect.left(), rect.center().y())
            : QPointF(rect.center().x(), rect.top());
        const QPointF end = horizontal
            ? QPointF(rect.right(), rect.center().y())
            : QPointF(rect.center().x(), rect.bottom());
        QLinearGradient gradient(start, end);
        for (const auto &stop : fill.gradientStops) {
            gradient.setColorAt(
                std::clamp(stop.first / 100.0, 0.0, 1.0),
                validColor(stop.second, fill.color)
            );
        }
        return QBrush(gradient);
    }
    if (fill.mode == QStringLiteral("split_vertical")) {
        QLinearGradient gradient(
            QPointF(rect.left(), rect.top()),
            QPointF(rect.left(), rect.bottom())
        );
        const double position = std::clamp(fill.splitPositionPct / 100.0, 0.0, 1.0);
        const QColor top = validColor(fill.splitTopColor, fill.color);
        const QColor bottom = validColor(fill.splitBottomColor, fill.color);
        gradient.setColorAt(0.0, top);
        gradient.setColorAt(std::max(0.0, position - 0.001), top);
        gradient.setColorAt(std::min(1.0, position + 0.001), bottom);
        gradient.setColorAt(1.0, bottom);
        return QBrush(gradient);
    }
    return QBrush(validColor(fill.color, QStringLiteral("#FFFFFF")));
}

QFont buildLineFont(const ResolvedStyle &style) {
    QFont font(style.fontFamily);
    font.setPixelSize(style.fontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(style.fontWeight, 1, 999)));
    return font;
}

bool isEmojiText(const QString &text) {
    for (const uint scalar : text.toUcs4()) {
        if ((scalar >= 0x1F000U && scalar <= 0x1FAFFU)
            || (scalar >= 0x2600U && scalar <= 0x27BFU)) {
            return true;
        }
    }
    return false;
}

QFont buildEmojiFont(const ResolvedStyle &style) {
    static std::once_flag emojiFontRegistration;
    std::call_once(emojiFontRegistration, []() {
        const QString windowsRoot = qEnvironmentVariable("WINDIR", QStringLiteral("C:/Windows"));
        QFontDatabase::addApplicationFont(
            windowsRoot + QStringLiteral("/Fonts/seguisym.ttf")
        );
    });
    QFont font(QStringLiteral("Segoe UI Symbol"));
    font.setPixelSize(style.fontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(style.fontWeight, 1, 999)));
    font.setItalic(style.italic);
    return font;
}

QFont buildRubyFont(const ResolvedStyle &style) {
    QFont font(style.fontFamily);
    font.setPixelSize(style.rubyFontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(style.fontWeight, 1, 999)));
    return font;
}

double visualStrokeExtent(const ResolvedStyle &style) {
    return std::ceil((std::max(style.strokeWidthPx, 0) + std::max(style.stroke2WidthPx, 0)) / 2.0);
}

double visualStrokeExtentForWidths(int strokeWidth, int stroke2Width) {
    return std::ceil((std::max(strokeWidth, 0) + std::max(stroke2Width, 0)) / 2.0);
}

double strokePenWidth(const ResolvedStyle &style) {
    return std::max(style.strokeWidthPx, 0);
}

double stroke2PenWidth(const ResolvedStyle &style) {
    return std::max(style.strokeWidthPx, 0) + std::max(style.stroke2WidthPx, 0);
}

int glowRadius(const ResolvedStyle &style, bool after) {
    int value = after ? style.glowAfterRadiusPx : style.glowBeforeRadiusPx;
    if (value == 10 && style.glowRadiusPx != 10) {
        value = style.glowRadiusPx;
    }
    return std::max(value, 1);
}

double glowPenWidth(const ResolvedStyle &style, bool after) {
    const double baseWidth = style.stroke2WidthPx > 0 ? stroke2PenWidth(style) : strokePenWidth(style);
    return std::max(1.0, baseWidth + glowRadius(style, after));
}

double glowExtent(const ResolvedStyle &style, bool after) {
    const int radius = glowRadius(style, after);
    return std::ceil(glowPenWidth(style, after) / 2.0 + radius * 3.0);
}

int glowPenWidthForWidths(int strokeWidth, int stroke2Width, int glowRadiusValue) {
    const int baseWidth = stroke2Width > 0
        ? std::max(strokeWidth, 0) + std::max(stroke2Width, 0)
        : std::max(strokeWidth, 0);
    return std::max(1, baseWidth + std::max(glowRadiusValue, 1));
}

double glowExtentForWidths(int strokeWidth, int stroke2Width, int glowRadiusValue) {
    return std::ceil(
        glowPenWidthForWidths(strokeWidth, stroke2Width, glowRadiusValue) / 2.0
        + std::max(glowRadiusValue, 1) * 3.0
    );
}

double afterClipVerticalExtent(const ResolvedStyle &style) {
    const double strokeExtent = visualStrokeExtent(style);
    const double glowExtra = style.decorationKind == QStringLiteral("glow") ? glowExtent(style, true) : 0.0;
    const double shadowExtra = style.decorationKind == QStringLiteral("shadow") ? std::abs(style.shadowOffsetY) : 0.0;
    return std::max({strokeExtent, glowExtra, shadowExtra, 2.0}) + 4.0;
}

int scaledPx(int value, double scale) {
    if (value <= 0) {
        return 0;
    }
    return std::max(1, static_cast<int>(std::round(value * scale)));
}

int scaledSignedPx(int value, double scale) {
    if (value == 0) {
        return 0;
    }
    const int sign = value > 0 ? 1 : -1;
    return sign * std::max(1, static_cast<int>(std::round(std::abs(value) * scale)));
}

double rubyScale(const ResolvedStyle &style) {
    return static_cast<double>(std::max(style.rubyFontSizePx, 1)) / static_cast<double>(std::max(style.fontSizePx, 1));
}

double rubyVisualPadding(const ResolvedStyle &style) {
    const double scale = rubyScale(style);
    const int strokeWidth = scaledPx(style.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(style.stroke2WidthPx, scale);
    const double strokeExtent = std::ceil((std::max(strokeWidth, 0) + std::max(stroke2Width, 0)) / 2.0);
    double glowExtra = 0.0;
    if (style.decorationKind == QStringLiteral("glow")) {
        const int rubyGlowRadius = scaledPx(glowRadius(style, true), scale);
        const int baseWidth = stroke2Width > 0 ? strokeWidth + stroke2Width : strokeWidth;
        glowExtra = std::ceil((std::max(1, baseWidth + rubyGlowRadius)) / 2.0 + std::max(rubyGlowRadius, 1) * 3.0);
    }
    const double shadowX = style.decorationKind == QStringLiteral("shadow") ? std::abs(scaledSignedPx(style.shadowOffsetX, scale)) : 0.0;
    const double shadowY = style.decorationKind == QStringLiteral("shadow") ? std::abs(scaledSignedPx(style.shadowOffsetY, scale)) : 0.0;
    return std::max({strokeExtent, glowExtra, shadowX, shadowY, 2.0});
}

double baselineYForLine(const RenderConfig &cfg, const ResolvedStyle &style, const QFontMetricsF &metrics, int lane, int visibleLineCount) {
    const double pad = visualStrokeExtent(style);
    if (cfg.dualLineLayout) {
        const double mainHeight = metrics.ascent() + metrics.descent() + pad * 2.0;
        const double mainAscent = metrics.ascent() + pad;
        const double mainDescent = metrics.descent() + pad;
        double upperBaseline = 0.0;
        double lowerBaseline = 0.0;
        if (cfg.lineYPosition == QStringLiteral("top")) {
            upperBaseline = cfg.lineYMarginPx + mainAscent;
            lowerBaseline = upperBaseline + mainHeight + cfg.lineGapPx;
        } else if (cfg.lineYPosition == QStringLiteral("center")) {
            const double totalHeight = mainHeight * 2.0 + cfg.lineGapPx;
            const double upperMainTop = std::floor((cfg.height - totalHeight) / 2.0);
            upperBaseline = upperMainTop + mainAscent;
            lowerBaseline = upperBaseline + mainHeight + cfg.lineGapPx;
        } else {
            lowerBaseline = cfg.height - cfg.lineYMarginPx - mainDescent;
            upperBaseline = lowerBaseline - mainHeight - cfg.lineGapPx;
        }
        return (cfg.dualLineLayout && std::min(lane, 1) == 1) ? lowerBaseline : upperBaseline;
    }

    if (cfg.lineYPosition == QStringLiteral("top")) {
        return cfg.lineYMarginPx + pad + metrics.ascent();
    }
    if (cfg.lineYPosition == QStringLiteral("center")) {
        const double blockHeight = metrics.height() + pad * 2.0;
        return std::floor((cfg.height - blockHeight) / 2.0) + pad + metrics.ascent();
    }
    return cfg.height - cfg.lineYMarginPx - pad - metrics.descent();
}

double lineXForLine(const RenderConfig &cfg, double lineWidth, double pad, int lane) {
    if (cfg.lineHorizontalLayout == QStringLiteral("center")) {
        return (cfg.width - lineWidth) * 0.5;
    }
    if (cfg.dualLineLayout && std::min(lane, 1) == 0) {
        return cfg.upperLineLeftMarginPx + pad;
    }
    if (cfg.dualLineLayout && std::min(lane, 1) == 1) {
        return cfg.width - cfg.lowerLineRightMarginPx - lineWidth - pad;
    }
    return (cfg.width - lineWidth) * 0.5;
}

LineLayout layoutLine(const RenderConfig &cfg, const ResolvedStyle &lineStyle, const TimingLine &line, int lane, int visibleLineCount) {
    const QString text = lineText(line);
    const bool inlineStyles = lineHasRoleLabels(line);

    LineLayout layout;
    layout.text = text;
    layout.font = buildLineFont(lineStyle);
    layout.lineStyle = &lineStyle;
    layout.hasInlineStyles = inlineStyles;

    const QFontMetricsF metrics(layout.font);

    layout.charWidths.reserve(line.chars.size());
    layout.charFonts.reserve(line.chars.size());
    layout.charStyles.reserve(line.chars.size());
    double totalWidth = 0.0;
    double maxAscent = 0.0;
    double maxDescent = 0.0;
    double maxVisualPad = visualStrokeExtent(lineStyle);
    double maxAfterClipExtent = afterClipVerticalExtent(lineStyle);
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const auto &ch = line.chars[i];
        const ResolvedStyle &charStyle = inlineStyles ? resolvedStyleForCharacter(cfg, line, ch) : lineStyle;
        QFont charFont = isEmojiText(ch.text)
            ? buildEmojiFont(charStyle)
            : (inlineStyles ? buildLineFont(charStyle) : layout.font);
        const QFontMetricsF charMetrics(charFont);
        const double width = std::max(1.0, charMetrics.horizontalAdvance(ch.text));
        layout.charWidths.push_back(width);
        layout.charFonts.push_back(charFont);
        layout.charStyles.push_back(&charStyle);
        totalWidth += width;
        maxAscent = std::max(maxAscent, charMetrics.ascent());
        maxDescent = std::max(maxDescent, charMetrics.descent());
        maxVisualPad = std::max(maxVisualPad, visualStrokeExtent(charStyle));
        maxAfterClipExtent = std::max(maxAfterClipExtent, afterClipVerticalExtent(charStyle));
        if (i + 1 < line.chars.size()) {
            totalWidth += charStyle.letterSpacingPx;
        }
    }
    layout.ascent = inlineStyles ? maxAscent : metrics.ascent();
    layout.descent = inlineStyles ? maxDescent : metrics.descent();
    layout.height = layout.ascent + layout.descent;
    layout.afterClipExtent = maxAfterClipExtent;
    layout.width = std::max(1.0, totalWidth);
    const double visualPad = inlineStyles ? maxVisualPad : visualStrokeExtent(lineStyle);
    layout.x = lineXForLine(cfg, layout.width, visualPad, lane);

    layout.baselineY = baselineYForLine(cfg, lineStyle, metrics, lane, visibleLineCount);

    layout.charLefts.resize(line.chars.size());
    if (cfg.rightToLeft) {
        double cursor = layout.x + layout.width;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            cursor -= layout.charWidths[i];
            layout.charLefts[i] = cursor;
            cursor -= (i + 1 < layout.charStyles.size()) ? layout.charStyles[i]->letterSpacingPx : 0;
        }
    } else {
        double cursor = layout.x;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.charLefts[i] = cursor;
            cursor += layout.charWidths[i] + ((i + 1 < layout.charStyles.size()) ? layout.charStyles[i]->letterSpacingPx : 0);
        }
    }

    // C2 keeps one complete line path for both before/after layers. Karaoke
    // progress is expressed only by clipping the after layer, not by rebuilding
    // a prefix string path that can drift under kerning/shaping.
    if (inlineStyles) {
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        }
    } else {
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        }
    }
    return layout;
}

QString timingLineLayoutTextKey(const TimingLine &line) {
    QString key;
    for (const TimingChar &ch : line.chars) {
        key += QStringLiteral("|%1:%2").arg(ch.text.size()).arg(ch.text);
    }
    return key;
}

QString layoutLineCacheKey(
    const RenderConfig &cfg,
    const ResolvedStyle &lineStyle,
    const TimingLine &line,
    int lane,
    int visibleLineCount
) {
    const QFont font = buildLineFont(lineStyle);
    return QStringLiteral("layout|text=%1|font=%2|w=%3|h=%4|lane=%5|visible=%6|rtl=%7|y=%8|h_layout=%9|dual=%10|ym=%11|gap=%12|upper=%13|lower=%14|sw=%15|s2w=%16|deco=%17|sx=%18|sy=%19|glow=%20|spacing=%21")
        .arg(timingLineLayoutTextKey(line))
        .arg(fontCacheKey(font))
        .arg(cfg.width)
        .arg(cfg.height)
        .arg(lane)
        .arg(visibleLineCount)
        .arg(cfg.rightToLeft ? 1 : 0)
        .arg(cfg.lineYPosition)
        .arg(cfg.lineHorizontalLayout)
        .arg(cfg.dualLineLayout ? 1 : 0)
        .arg(cfg.lineYMarginPx)
        .arg(cfg.lineGapPx)
        .arg(cfg.upperLineLeftMarginPx)
        .arg(cfg.lowerLineRightMarginPx)
        .arg(lineStyle.strokeWidthPx)
        .arg(lineStyle.stroke2WidthPx)
        .arg(lineStyle.decorationKind)
        .arg(lineStyle.shadowOffsetX)
        .arg(lineStyle.shadowOffsetY)
        .arg(glowRadius(lineStyle, false))
        .arg(lineStyle.letterSpacingPx);
}

LineLayout cachedLayoutLine(
    const RenderConfig &cfg,
    const ResolvedStyle &lineStyle,
    const TimingLine &line,
    int lane,
    int visibleLineCount
) {
    if (lineHasRoleLabels(line)) {
        return layoutLine(cfg, lineStyle, line, lane, visibleLineCount);
    }
    const QString key = layoutLineCacheKey(cfg, lineStyle, line, lane, visibleLineCount);
    if (const auto cached = lookupLayoutCache(key)) {
        return *cached;
    }
    LineLayout layout = layoutLine(cfg, lineStyle, line, lane, visibleLineCount);
    layout.lineStyle = nullptr;
    layout.charStyles.clear();
    storeLayoutCache(key, layout);
    return layout;
}

// One horizontal band per character that has started, in layout order.
//
// A character whose wipe runs past the next character's start leaves two fronts
// alive at once (SUG writes this where a lead phrase's release timestamp crosses
// into the following harmony phrase).  Stopping at the first unfinished glyph --
// which is what this used to do -- can only ever show the first of them.  With
// sequential timing the bands are contiguous and their union is exactly the
// single rect this produced before.
std::vector<std::pair<double, double>> afterClipBandsFromCharacterTiming(
    const RenderConfig &cfg, const TimingLine &line, const LineLayout &layout, int tMs
) {
    std::vector<std::pair<double, double>> bands;
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const int start = line.chars[i].startMs;
        // Characters are ordered by start time, so nothing later has begun either.
        if (tMs < start) {
            break;
        }
        const double left = layout.charLefts[i];
        const double width = layout.charWidths[i];
        const double right = left + width;
        const double ratio = progressRatio(start, charEndMs(line, i), tMs);
        if (ratio >= 1.0) {
            bands.emplace_back(left, right);
            continue;
        }
        // Keep scanning: a later character may be running at the same time.
        if (cfg.rightToLeft) {
            bands.emplace_back(right - width * ratio, right);
        } else {
            bands.emplace_back(left, left + width * ratio);
        }
    }
    return bands;
}

std::vector<std::pair<double, double>> mergeBands(
    std::vector<std::pair<double, double>> bands
) {
    std::sort(bands.begin(), bands.end());
    std::vector<std::pair<double, double>> merged;
    for (const auto &band : bands) {
        if (band.second <= band.first) {
            continue;
        }
        if (!merged.empty() && band.first <= merged.back().second) {
            merged.back().second = std::max(merged.back().second, band.second);
            continue;
        }
        merged.push_back(band);
    }
    return merged;
}

QRegion bandsToRegion(
    const std::vector<std::pair<double, double>> &bands, double top, double height
) {
    QRegion region;
    for (const auto &band : bands) {
        region += QRectF(band.first, top, band.second - band.first, height).toAlignedRect();
    }
    return region;
}

std::optional<QRectF> afterClipRectFromCharacterTiming(const RenderConfig &cfg, const ResolvedStyle &style, const TimingLine &line, const LineLayout &layout, int tMs) {
    if (line.chars.empty()) {
        return std::nullopt;
    }

    const auto bands = afterClipBandsFromCharacterTiming(cfg, line, layout, tMs);
    if (bands.empty()) {
        return std::nullopt;
    }
    // The line edge stays the outer boundary, exactly as before.
    double clipEdge = cfg.rightToLeft ? bands.front().first : bands.back().second;
    for (const auto &band : bands) {
        clipEdge = cfg.rightToLeft
            ? std::min(clipEdge, band.first)
            : std::max(clipEdge, band.second);
    }

    const double verticalExtent = layout.afterClipExtent > 0.0 ? layout.afterClipExtent : afterClipVerticalExtent(style);
    const double top = layout.baselineY - layout.ascent - verticalExtent;
    const double height = layout.height + verticalExtent * 2.0;
    if (cfg.rightToLeft) {
        const double left = std::clamp(clipEdge, layout.x, layout.x + layout.width);
        return QRectF(left, top, layout.x + layout.width - left, height);
    }
    const double right = std::clamp(clipEdge, layout.x, layout.x + layout.width);
    return QRectF(layout.x, top, right - layout.x, height);
}

std::vector<std::pair<int, int>> lineIntervals(const TimingLine &line) {
    std::vector<std::pair<int, int>> intervals;
    intervals.reserve(line.chars.size());
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        intervals.push_back({line.chars[i].startMs, charEndMs(line, i)});
    }
    return intervals;
}

std::vector<int> rubyTimeIndices(
    const RubyAnnotation &ruby,
    const std::vector<std::pair<int, int>> &intervals
) {
    std::vector<int> indices;
    for (std::size_t i = 0; i < intervals.size(); ++i) {
        if (intervals[i].first < ruby.posEndMs && intervals[i].second > ruby.posStartMs) {
            indices.push_back(static_cast<int>(i));
        }
    }
    return indices;
}

QString lineFullText(const TimingLine &line) {
    QString text;
    for (const auto &ch : line.chars) {
        text += ch.text;
    }
    return text;
}

std::vector<int> textSpanIndices(const std::pair<int, int> &span, const TimingLine &line) {
    std::vector<int> indices;
    int cursor = 0;
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const int unitStart = cursor;
        const int unitEnd = cursor + line.chars[i].text.size();
        cursor = unitEnd;
        if (unitStart < span.second && unitEnd > span.first) {
            indices.push_back(static_cast<int>(i));
        }
    }
    return indices;
}

std::optional<std::pair<int, int>> findRubyTextSpan(
    const QString &kanji,
    const TimingLine &line,
    const std::vector<int> &preferredIndices
) {
    if (kanji.isEmpty()) {
        return std::nullopt;
    }
    const QString text = lineFullText(line);
    std::vector<std::pair<int, int>> occurrences;
    int pos = text.indexOf(kanji);
    while (pos >= 0) {
        occurrences.push_back({pos, pos + kanji.size()});
        pos = text.indexOf(kanji, pos + 1);
    }
    if (occurrences.empty()) {
        return std::nullopt;
    }
    if (preferredIndices.empty()) {
        return occurrences.front();
    }

    std::pair<int, int> best = occurrences.front();
    std::pair<int, int> bestScore{-1, std::numeric_limits<int>::min()};
    for (const auto &span : occurrences) {
        const auto indices = textSpanIndices(span, line);
        int overlap = 0;
        int distance = std::numeric_limits<int>::max();
        for (int index : indices) {
            for (int preferred : preferredIndices) {
                if (index == preferred) {
                    ++overlap;
                }
                distance = std::min(distance, std::abs(index - preferred));
            }
        }
        if (distance == std::numeric_limits<int>::max()) {
            distance = 0;
        }
        const std::pair<int, int> score{overlap, -distance};
        if (score > bestScore) {
            bestScore = score;
            best = span;
        }
    }
    return best;
}

std::vector<int> rubyTargetIndices(
    const RubyAnnotation &ruby,
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals
) {
    // Rubies from every source and line are flattened into RenderConfig. Keep
    // ownership at this shared resolution boundary so CPU paint, diagnostics,
    // Utopia and GPU scene construction cannot accidentally apply different
    // filtering rules to overlapping or repeated lyrics.
    if (ruby.sourceIndex != line.sourceIndex) {
        return {};
    }
    if (ruby.targetLineIndex >= 0
        && line.trackLineIndex >= 0
        && ruby.targetLineIndex != line.trackLineIndex) {
        return {};
    }
    // Mirrors Painter's _ruby_explicit_target_indices: the loader already knows
    // the exact characters for .sug per-character ruby, and searching by text
    // instead collapses every repeat of one base onto its first occurrence.
    // Both guards below keep an annotation resolved against different line
    // contents from landing on an unrelated character.
    if (ruby.targetCharStart >= 0
        && ruby.targetCharEnd > ruby.targetCharStart
        && ruby.targetCharEnd <= static_cast<int>(line.chars.size())) {
        QString targetText;
        for (int index = ruby.targetCharStart; index < ruby.targetCharEnd; ++index) {
            targetText += line.chars[static_cast<std::size_t>(index)].text;
        }
        if (ruby.kanji.isEmpty() || targetText == ruby.kanji) {
            std::vector<int> explicitIndices;
            explicitIndices.reserve(
                static_cast<std::size_t>(ruby.targetCharEnd - ruby.targetCharStart)
            );
            for (int index = ruby.targetCharStart; index < ruby.targetCharEnd; ++index) {
                explicitIndices.push_back(index);
            }
            return explicitIndices;
        }
    }
    const auto timeIndices = rubyTimeIndices(ruby, intervals);
    if (!ruby.kanji.isEmpty()) {
        const auto span = findRubyTextSpan(ruby.kanji, line, timeIndices);
        if (!span.has_value()) {
            return {};
        }
        const auto indices = textSpanIndices(span.value(), line);
        const bool globalPosition = ruby.posStartMs == 0 && ruby.posEndMs == 0;
        if (!globalPosition) {
            const bool overlapsTimedTarget = std::any_of(
                indices.begin(), indices.end(), [&](int index) {
                    return std::find(
                        timeIndices.begin(), timeIndices.end(), index
                    ) != timeIndices.end();
                }
            );
            if (!overlapsTimedTarget) {
                return {};
            }
        }
        return indices;
    }
    return timeIndices;
}

RubyAnnotation effectiveRubyForTarget(
    const RubyAnnotation &ruby,
    const std::vector<int> &indices,
    const std::vector<std::pair<int, int>> &intervals
) {
    std::vector<int> validIndices;
    for (int index : indices) {
        if (index >= 0 && static_cast<std::size_t>(index) < intervals.size()) {
            validIndices.push_back(index);
        }
    }
    if (validIndices.empty()) {
        return ruby;
    }
    int start = intervals[validIndices.front()].first;
    int end = intervals[validIndices.front()].second;
    for (int index : validIndices) {
        start = std::min(start, intervals[index].first);
        end = std::max(end, intervals[index].second);
    }
    if (start == ruby.posStartMs && end == ruby.posEndMs) {
        return ruby;
    }
    RubyAnnotation out = ruby;
    out.posStartMs = start;
    out.posEndMs = end;
    const int duration = std::max(end - start, 0);
    for (int &relMs : out.readingPartMs) {
        relMs = std::max(0, std::min(duration, relMs));
    }
    return out;
}

std::optional<std::pair<double, double>> rubyTargetXRange(
    const RubyAnnotation &ruby,
    const TimingLine &line,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals
) {
    // Same precedence as rubyTargetIndices: an explicit loader target wins over
    // the text search so repeats of one base keep their own x.
    const auto resolved = rubyTargetIndices(ruby, line, intervals);
    if (!resolved.empty()
        && ruby.targetCharStart >= 0
        && ruby.targetCharEnd > ruby.targetCharStart
        && resolved.front() == ruby.targetCharStart) {
        double left = std::numeric_limits<double>::max();
        double right = std::numeric_limits<double>::lowest();
        for (int index : resolved) {
            if (index < 0 || index >= static_cast<int>(layout.charLefts.size())) {
                continue;
            }
            left = std::min(left, layout.charLefts[static_cast<std::size_t>(index)]);
            right = std::max(
                right,
                layout.charLefts[static_cast<std::size_t>(index)]
                    + layout.charWidths[static_cast<std::size_t>(index)]
            );
        }
        if (left <= right) {
            return std::make_pair(left, right);
        }
    }
    if (!ruby.kanji.isEmpty()) {
        const auto timeIndices = rubyTimeIndices(ruby, intervals);
        const auto span = findRubyTextSpan(ruby.kanji, line, timeIndices);
        if (!span.has_value()) {
            return std::nullopt;
        }
        int cursor = 0;
        std::optional<double> left;
        std::optional<double> right;
        for (std::size_t i = 0; i < line.chars.size() && i < layout.charLefts.size(); ++i) {
            const int textLen = line.chars[i].text.size();
            const int unitStart = cursor;
            const int unitEnd = cursor + textLen;
            cursor = unitEnd;
            if (textLen <= 0 || unitEnd <= span->first || unitStart >= span->second) {
                continue;
            }
            const int overlapStart = std::max(span->first, unitStart) - unitStart;
            const int overlapEnd = std::min(span->second, unitEnd) - unitStart;
            const double charLeft = layout.charLefts[i];
            const double width = layout.charWidths[i];
            const double segmentLeft = charLeft + std::round(width * overlapStart / textLen);
            const double segmentRight = charLeft + std::round(width * overlapEnd / textLen);
            left = left.has_value() ? std::min(left.value(), segmentLeft) : segmentLeft;
            right = right.has_value() ? std::max(right.value(), segmentRight) : segmentRight;
        }
        if (!left.has_value() || !right.has_value() || right.value() <= left.value()) {
            return std::nullopt;
        }
        return std::pair<double, double>{left.value(), right.value()};
    }

    const auto indices = rubyTimeIndices(ruby, intervals);
    if (indices.empty()) {
        return std::nullopt;
    }
    double left = 0.0;
    double right = 0.0;
    bool seen = false;
    for (int index : indices) {
        if (index < 0 || static_cast<std::size_t>(index) >= layout.charLefts.size()) {
            continue;
        }
        const double charLeft = layout.charLefts[index];
        const double charRight = charLeft + layout.charWidths[index];
        if (!seen) {
            left = charLeft;
            right = charRight;
            seen = true;
        } else {
            left = std::min(left, charLeft);
            right = std::max(right, charRight);
        }
    }
    if (!seen || right <= left) {
        return std::nullopt;
    }
    return std::pair<double, double>{left, right};
}

std::optional<RubyGroupInfo> rubyGroupForCharIndex(
    const RenderConfig &cfg,
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index
) {
    for (const RubyAnnotation &ruby : cfg.rubies) {
        auto indices = rubyTargetIndices(ruby, line, intervals);
        if (indices.size() <= 1) {
            continue;
        }
        if (std::find(indices.begin(), indices.end(), index) == indices.end()) {
            continue;
        }
        std::vector<int> valid;
        for (int candidate : indices) {
            if (candidate >= 0 && candidate < static_cast<int>(intervals.size())) {
                valid.push_back(candidate);
            }
        }
        if (valid.size() <= 1) {
            continue;
        }
        return RubyGroupInfo{valid, effectiveRubyForTarget(ruby, valid, intervals)};
    }
    return std::nullopt;
}

std::vector<QString> rubyReadingUnits(const QString &reading) {
    std::vector<QString> units;
    units.reserve(static_cast<std::size_t>(reading.size()));
    for (const QChar &ch : reading) {
        units.push_back(QString(ch));
    }
    return units;
}

std::vector<int> rubyReadingBoundaries(const RubyAnnotation &ruby, int unitCount) {
    if (unitCount <= 0) {
        return {ruby.posStartMs, ruby.posEndMs};
    }
    std::vector<int> boundaries{ruby.posStartMs};
    const int usableParts = std::max(unitCount - 1, 0);
    for (int i = 0; i < usableParts && static_cast<std::size_t>(i) < ruby.readingPartMs.size(); ++i) {
        int ts = ruby.posStartMs + ruby.readingPartMs[i];
        ts = std::max(boundaries.back(), std::min(ruby.posEndMs, ts));
        boundaries.push_back(ts);
    }
    if (static_cast<int>(boundaries.size()) < unitCount) {
        const int start = boundaries.back();
        const int remaining = unitCount - static_cast<int>(boundaries.size()) + 1;
        for (int step = 1; step < remaining; ++step) {
            boundaries.push_back(start + static_cast<int>(std::round((ruby.posEndMs - start) * step / static_cast<double>(remaining))));
        }
    }
    boundaries.push_back(std::max(boundaries.back(), ruby.posEndMs));
    return boundaries;
}

std::vector<std::pair<int, int>> rubyReadingIntervals(const RubyAnnotation &ruby) {
    const auto units = rubyReadingUnits(ruby.reading);
    const int unitCount = static_cast<int>(units.size());
    if (static_cast<int>(ruby.readingPartMs.size()) >= 2 * std::max(unitCount - 1, 0)) {
        std::vector<std::pair<int, int>> intervals;
        int currentStart = ruby.posStartMs;
        for (int i = 0; i < unitCount - 1; ++i) {
            int release = ruby.posStartMs + ruby.readingPartMs[i * 2];
            int nextStart = ruby.posStartMs + ruby.readingPartMs[i * 2 + 1];
            release = std::max(currentStart, std::min(release, ruby.posEndMs));
            nextStart = std::max(release, std::min(nextStart, ruby.posEndMs));
            intervals.push_back({currentStart, release});
            currentStart = nextStart;
        }
        intervals.push_back({currentStart, std::max(currentStart, ruby.posEndMs)});
        return intervals;
    }

    std::vector<std::pair<int, int>> intervals;
    const auto boundaries = rubyReadingBoundaries(ruby, unitCount);
    for (int i = 0; i < unitCount; ++i) {
        int start = boundaries[i];
        int end = boundaries[i + 1];
        if (end < start) {
            end = start;
        }
        intervals.push_back({start, end});
    }
    return intervals;
}

std::vector<QString> rubyUtopiaVisualUnits(const QString &text) {
    std::vector<QString> units;
    for (const QChar &ch : text) {
        if (!units.empty() && (ch == QChar(0x3099) || ch == QChar(0x309A))) {
            units.back().append(ch);
        } else {
            units.push_back(QString(ch));
        }
    }
    return units;
}

std::vector<std::pair<QString, std::pair<int, int>>> rubyProgressPartsAndIntervals(
    const RubyAnnotation &ruby
) {
    QString joined;
    for (const QString &part : ruby.readingParts) {
        joined += part;
    }
    if (!ruby.readingParts.empty()
        && ruby.readingParts.size() == ruby.readingPartMs.size() + 1
        && joined == ruby.reading) {
        std::vector<int> anchors{ruby.posStartMs};
        for (int relativeMs : ruby.readingPartMs) {
            const int timestamp = ruby.posStartMs + relativeMs;
            anchors.push_back(std::max(
                anchors.back(), std::min(ruby.posEndMs, timestamp)
            ));
        }
        anchors.push_back(std::max(anchors.back(), ruby.posEndMs));
        std::vector<std::pair<QString, std::pair<int, int>>> out;
        out.reserve(ruby.readingParts.size());
        for (std::size_t index = 0; index < ruby.readingParts.size(); ++index) {
            out.push_back({
                ruby.readingParts[index],
                {anchors[index], anchors[index + 1]},
            });
        }
        return out;
    }
    const auto units = rubyReadingUnits(ruby.reading);
    const auto intervals = rubyReadingIntervals(ruby);
    std::vector<std::pair<QString, std::pair<int, int>>> out;
    const std::size_t count = std::min(units.size(), intervals.size());
    out.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        out.push_back({units[index], intervals[index]});
    }
    return out;
}

std::vector<std::pair<QString, std::pair<int, int>>> rubyUtopiaReadingUnitsAndIntervals(
    const RubyAnnotation &ruby
) {
    const auto parts = rubyProgressPartsAndIntervals(ruby);
    std::vector<std::pair<QString, std::pair<int, int>>> out;
    for (const auto &part : parts) {
        const auto visualUnits = rubyUtopiaVisualUnits(part.first);
        if (visualUnits.empty()) {
            continue;
        }
        if (visualUnits.size() == 1) {
            out.push_back({visualUnits.front(), part.second});
            continue;
        }
        const int start = part.second.first;
        const int end = part.second.second;
        const int duration = std::max(end - start, 0);
        for (std::size_t j = 0; j < visualUnits.size(); ++j) {
            const int unitStart = start + duration * static_cast<int>(j)
                / static_cast<int>(visualUnits.size());
            const int unitEnd = start + duration * static_cast<int>(j + 1)
                / static_cast<int>(visualUnits.size());
            out.push_back({visualUnits[j], {unitStart, std::max(unitStart, unitEnd)}});
        }
    }
    return out;
}

bool isUtopiaGroupMarker(const RubyAnnotation &ruby) {
    const QString reading = ruby.reading.trimmed();
    if (!reading.isEmpty() && reading != QStringLiteral("^")) {
        return false;
    }
    return std::all_of(
        ruby.readingParts.begin(), ruby.readingParts.end(),
        [](const QString &part) {
            const QString trimmed = part.trimmed();
            return trimmed.isEmpty() || trimmed == QStringLiteral("^");
        }
    );
}

std::vector<std::pair<int, int>> rubyMainWipeIntervals(
    const RubyAnnotation &ruby, const std::string &mode
) {
    if (mode == "reading_units") {
        const auto units = rubyUtopiaReadingUnitsAndIntervals(ruby);
        if (!units.empty()) {
            std::vector<std::pair<int, int>> out;
            out.reserve(units.size());
            for (const auto &unit : units) {
                out.push_back(unit.second);
            }
            return out;
        }
    }

    const int start = ruby.posStartMs;
    const int end = std::max(start, ruby.posEndMs);
    std::vector<int> anchors{start};
    for (int relativeMs : ruby.readingPartMs) {
        anchors.push_back(std::max(
            anchors.back(), std::min(end, start + relativeMs)
        ));
    }
    anchors.push_back(std::max(anchors.back(), end));
    std::vector<std::pair<int, int>> out;
    out.reserve(anchors.size() - 1);
    for (std::size_t index = 0; index + 1 < anchors.size(); ++index) {
        out.push_back({anchors[index], anchors[index + 1]});
    }
    return out;
}

int rubyMainProgressTimeAtRatio(
    const RubyAnnotation &ruby,
    double target,
    const QString &mode,
    bool rightSide
) {
    const auto intervals = rubyMainWipeIntervals(ruby, mode.toStdString());
    if (intervals.empty()) {
        return ruby.posEndMs;
    }
    target = std::clamp(target, 0.0, 1.0);
    std::vector<std::pair<int, double>> points;
    points.reserve(intervals.size() * 2);
    const int count = static_cast<int>(intervals.size());
    for (int index = 0; index < count; ++index) {
        points.push_back({
            intervals[static_cast<std::size_t>(index)].first,
            static_cast<double>(index) / count,
        });
        points.push_back({
            intervals[static_cast<std::size_t>(index)].second,
            static_cast<double>(index + 1) / count,
        });
    }
    std::optional<int> exact;
    for (const auto &point : points) {
        if (std::abs(point.second - target) < 0.000001) {
            exact = exact.has_value()
                ? (rightSide ? std::max(*exact, point.first)
                             : std::min(*exact, point.first))
                : point.first;
        }
    }
    if (exact.has_value()) {
        return *exact;
    }
    for (std::size_t index = 1; index < points.size(); ++index) {
        const auto &previous = points[index - 1];
        const auto &following = points[index];
        if (previous.second <= target && target <= following.second) {
            if (following.second <= previous.second || following.first <= previous.first) {
                return rightSide ? following.first : previous.first;
            }
            const double local = (target - previous.second)
                / (following.second - previous.second);
            return previous.first + static_cast<int>(std::round(
                (following.first - previous.first) * local
            ));
        }
    }
    return target <= 0.0 ? points.front().first : points.back().first;
}

bool rubyMainUsesBaseTiming(
    const TimingLine &line,
    const std::vector<int> &indices
) {
    std::vector<int> valid;
    valid.reserve(indices.size());
    for (int index : indices) {
        if (index >= 0 && index < static_cast<int>(line.chars.size())) {
            valid.push_back(index);
        }
    }
    if (valid.size() <= 1) {
        return false;
    }
    for (std::size_t offset = 0; offset < valid.size(); ++offset) {
        const TimingChar &ch = line.chars[static_cast<std::size_t>(valid[offset])];
        if (offset > 0 && ch.explicitStart) {
            return true;
        }
        if (offset + 1 < valid.size() && ch.explicitEnd) {
            return true;
        }
    }
    return false;
}

std::pair<int, int> utopiaWipeWindowForIndex(
    const TimingLine &line,
    const LineLayout &layout,
    int index,
    const ResolvedStyle &style,
    const std::optional<RubyGroupInfo> &group,
    std::pair<int, int> fallback
) {
    if (!group.has_value()
        || isUtopiaGroupMarker(group->ruby)
        || rubyMainUsesBaseTiming(line, group->indices)
        || std::find(group->indices.begin(), group->indices.end(), index) == group->indices.end()) {
        return fallback;
    }
    double groupLeft = 0.0;
    double groupRight = 0.0;
    bool seen = false;
    for (int candidate : group->indices) {
        if (candidate < 0
            || static_cast<std::size_t>(candidate) >= layout.charLefts.size()
            || static_cast<std::size_t>(candidate) >= layout.charWidths.size()) {
            continue;
        }
        const double left = layout.charLefts[static_cast<std::size_t>(candidate)];
        const double right = left + layout.charWidths[static_cast<std::size_t>(candidate)];
        groupLeft = seen ? std::min(groupLeft, left) : left;
        groupRight = seen ? std::max(groupRight, right) : right;
        seen = true;
    }
    if (!seen || groupRight <= groupLeft
        || index < 0
        || static_cast<std::size_t>(index) >= layout.charLefts.size()
        || static_cast<std::size_t>(index) >= layout.charWidths.size()) {
        return fallback;
    }
    const double charLeft = layout.charLefts[static_cast<std::size_t>(index)];
    const double charRight = charLeft + layout.charWidths[static_cast<std::size_t>(index)];
    const double width = groupRight - groupLeft;
    const double startRatio = (charLeft - groupLeft) / width;
    const double endRatio = (charRight - groupLeft) / width;
    const int start = rubyMainProgressTimeAtRatio(
        group->ruby, startRatio, style.rubyMainProgressMode, true
    );
    const int end = rubyMainProgressTimeAtRatio(
        group->ruby, endRatio, style.rubyMainProgressMode, false
    );
    return {start, std::max(start, end)};
}

void applyRubyMainWipePoints(
    krok::subtitle::native::TextLine &line,
    int firstCharIndex,
    int lastCharIndex,
    const std::vector<std::pair<int, int>> &unitIntervals,
    int timingOffsetMs
) {
    using krok::subtitle::native::WipePoint;
    if (unitIntervals.empty()
        || firstCharIndex < 0
        || lastCharIndex < firstCharIndex
        || lastCharIndex >= static_cast<int>(line.chars.size())) {
        return;
    }
    const int baseCount = lastCharIndex - firstCharIndex + 1;
    const int unitCount = static_cast<int>(unitIntervals.size());
    std::vector<WipePoint> progressPoints;
    progressPoints.reserve(static_cast<std::size_t>(unitCount * 2));
    for (int unitIndex = 0; unitIndex < unitCount; ++unitIndex) {
        progressPoints.push_back(WipePoint{
            unitIntervals[static_cast<std::size_t>(unitIndex)].first + timingOffsetMs,
            static_cast<float>(unitIndex) / static_cast<float>(unitCount),
        });
        progressPoints.push_back(WipePoint{
            unitIntervals[static_cast<std::size_t>(unitIndex)].second + timingOffsetMs,
            static_cast<float>(unitIndex + 1) / static_cast<float>(unitCount),
        });
    }
    const auto timeAtProgress = [&](float target, bool rightSide) {
        std::optional<int> exact;
        for (const WipePoint &point : progressPoints) {
            if (std::abs(point.position - target) < 0.0001f) {
                exact = exact.has_value()
                    ? (rightSide ? std::max(*exact, point.timeMs)
                                 : std::min(*exact, point.timeMs))
                    : point.timeMs;
            }
        }
        if (exact.has_value()) {
            return *exact;
        }
        for (std::size_t index = 1; index < progressPoints.size(); ++index) {
            const WipePoint &previous = progressPoints[index - 1];
            const WipePoint &following = progressPoints[index];
            if (previous.position < target && target < following.position) {
                const float local = (target - previous.position)
                    / (following.position - previous.position);
                return previous.timeMs + static_cast<int>(std::round(
                    static_cast<float>(following.timeMs - previous.timeMs) * local
                ));
            }
        }
        return target <= 0.0f
            ? progressPoints.front().timeMs
            : progressPoints.back().timeMs;
    };
    for (int baseIndex = 0; baseIndex < baseCount; ++baseIndex) {
        auto &target = line.chars[static_cast<std::size_t>(firstCharIndex + baseIndex)];
        const float progressStart = static_cast<float>(baseIndex)
            / static_cast<float>(baseCount);
        const float progressEnd = static_cast<float>(baseIndex + 1)
            / static_cast<float>(baseCount);
        target.wipePoints.clear();
        target.wipePoints.push_back(WipePoint{
            timeAtProgress(progressStart, true), 0.0f,
        });
        for (const WipePoint &point : progressPoints) {
            if (point.position > progressStart + 0.0001f
                && point.position < progressEnd - 0.0001f) {
                target.wipePoints.push_back(WipePoint{
                    point.timeMs,
                    (point.position - progressStart)
                        / (progressEnd - progressStart),
                });
            }
        }
        target.wipePoints.push_back(WipePoint{
            timeAtProgress(progressEnd, false), 1.0f,
        });
        std::stable_sort(
            target.wipePoints.begin(), target.wipePoints.end(),
            [](const WipePoint &left, const WipePoint &right) {
                return left.timeMs < right.timeMs;
            }
        );
    }
}

double rubyProgressRatio(const RubyAnnotation &ruby, int tMs) {
    if (ruby.reading.isEmpty() || ruby.readingPartMs.empty()) {
        return progressRatio(ruby.posStartMs, ruby.posEndMs, tMs);
    }
    const auto intervals = rubyReadingIntervals(ruby);
    const int total = std::max(static_cast<int>(intervals.size()), 1);
    for (int i = 0; i < static_cast<int>(intervals.size()); ++i) {
        const int start = intervals[i].first;
        const int end = intervals[i].second;
        if (tMs < start) {
            return static_cast<double>(i) / total;
        }
        if (tMs < end) {
            return (i + progressRatio(start, end, tMs)) / total;
        }
    }
    return 1.0;
}

double rubyLayoutWidth(const QString &reading, const QFontMetricsF &metrics, double targetWidth) {
    const double natural = metrics.horizontalAdvance(reading);
    if (targetWidth <= natural) {
        return natural;
    }
    return targetWidth;
}

QPainterPath rubyTextPath(
    const QString &reading,
    const QFont &font,
    const QFontMetricsF &metrics,
    double x,
    double baselineY,
    double targetWidth
) {
    const auto units = rubyReadingUnits(reading);
    std::vector<double> widths;
    widths.reserve(units.size());
    double natural = 0.0;
    for (const QString &unit : units) {
        const double width = metrics.horizontalAdvance(unit);
        widths.push_back(width);
        natural += width;
    }

    QPainterPath path;
    if (units.empty()) {
        return path;
    }
    if (units.size() <= 1 || targetWidth <= natural * 1.15) {
        double cursor = x + std::max((targetWidth - natural) / 2.0, 0.0);
        for (std::size_t i = 0; i < units.size(); ++i) {
            path.addText(QPointF(cursor, baselineY), font, units[i]);
            cursor += widths[i];
        }
        return path;
    }

    const double slotWidth = targetWidth / static_cast<double>(units.size());
    for (std::size_t i = 0; i < units.size(); ++i) {
        const double unitX = x + slotWidth * static_cast<double>(i) + (slotWidth - widths[i]) / 2.0;
        path.addText(QPointF(unitX, baselineY), font, units[i]);
    }
    return path;
}

std::vector<RubyUnitLayout> rubyUnitLayouts(
    const std::vector<std::pair<QString, std::pair<int, int>>> &unitsAndIntervals,
    const QFontMetricsF &metrics,
    double x,
    double targetWidth
) {
    std::vector<RubyUnitLayout> out;
    if (unitsAndIntervals.empty()) {
        return out;
    }
    std::vector<double> widths;
    widths.reserve(unitsAndIntervals.size());
    double natural = 0.0;
    for (const auto &item : unitsAndIntervals) {
        const double width = metrics.horizontalAdvance(item.first);
        widths.push_back(width);
        natural += width;
    }
    if (unitsAndIntervals.size() <= 1 || targetWidth <= natural * 1.15) {
        double cursor = x + std::max((targetWidth - natural) / 2.0, 0.0);
        for (std::size_t i = 0; i < unitsAndIntervals.size(); ++i) {
            out.push_back(RubyUnitLayout{unitsAndIntervals[i].first, unitsAndIntervals[i].second, cursor, widths[i]});
            cursor += widths[i];
        }
        return out;
    }

    const double slotWidth = targetWidth / static_cast<double>(unitsAndIntervals.size());
    for (std::size_t i = 0; i < unitsAndIntervals.size(); ++i) {
        const double unitX = x + slotWidth * static_cast<double>(i) + (slotWidth - widths[i]) / 2.0;
        out.push_back(RubyUnitLayout{unitsAndIntervals[i].first, unitsAndIntervals[i].second, unitX, widths[i]});
    }
    return out;
}

std::vector<RubyDiagnostics> rubyDiagnosticsForLine(
    const RenderConfig &cfg,
    const ResolvedStyle &style,
    const TimingLine &line,
    const LineLayout &layout,
    int tMs
) {
    std::vector<RubyDiagnostics> diagnostics;
    if (cfg.rubies.empty()) {
        return diagnostics;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const auto intervals = lineIntervals(line);
    const double rubyBaselineY = layout.baselineY - layout.ascent - style.rubyGapPx;
    const double pad = rubyVisualPadding(style);

    for (const RubyAnnotation &ruby : cfg.rubies) {
        const auto indices = rubyTargetIndices(ruby, line, intervals);
        if (indices.empty()) {
            continue;
        }
        const auto targetRange = rubyTargetXRange(ruby, line, layout, intervals);
        if (!targetRange.has_value()) {
            continue;
        }
        const RubyAnnotation paintRuby = effectiveRubyForTarget(ruby, indices, intervals);
        const double x = targetRange->first;
        const double targetWidth = std::max(targetRange->second - targetRange->first, 1.0);
        const double readingWidth = rubyLayoutWidth(paintRuby.reading, rubyMetrics, targetWidth);
        const double ratio = rubyProgressRatio(paintRuby, tMs);
        const double ratioC = std::min(ratio, 1.0);
        const QRectF rect(x, rubyBaselineY - rubyMetrics.ascent(), readingWidth, rubyMetrics.height());
        const double clipLeft = cfg.rightToLeft
            ? rect.left() + rect.width() * (1.0 - ratioC) - pad
            : rect.left() - pad;
        const double clipWidth = rect.width() * ratioC + pad;

        RubyDiagnostics item;
        item.kanji = paintRuby.kanji;
        item.reading = paintRuby.reading;
        item.indices = indices;
        item.x = x;
        item.baselineY = rubyBaselineY;
        item.targetWidth = targetWidth;
        item.readingWidth = readingWidth;
        item.progress = ratio;
        item.afterClipLeft = clipLeft;
        item.afterClipRight = clipLeft + clipWidth;
        item.afterClipTop = rect.top() - pad;
        item.afterClipHeight = rect.height() + pad * 2.0;
        diagnostics.push_back(item);
    }
    return diagnostics;
}

struct NativeFillSegment {
    double left = 0.0;
    double right = 0.0;
    double ratio = 0.0;
};

std::optional<RubyAnnotation> rubyForCharIndex(
    const RenderConfig &cfg,
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index
) {
    for (const RubyAnnotation &ruby : cfg.rubies) {
        const auto indices = rubyTargetIndices(ruby, line, intervals);
        if (std::find(indices.begin(), indices.end(), index) != indices.end()) {
            return ruby;
        }
    }
    return std::nullopt;
}

std::vector<NativeFillSegment> fillSegmentsForLine(
    const RenderConfig &cfg,
    const TimingLine &line,
    const LineLayout &layout,
    int tMs
) {
    std::vector<NativeFillSegment> segments;
    const auto intervals = lineIntervals(line);
    int index = 0;
    while (index < static_cast<int>(line.chars.size())) {
        const auto ruby = rubyForCharIndex(cfg, line, intervals, index);
        if (!ruby.has_value()) {
            if (static_cast<std::size_t>(index) >= layout.charLefts.size()) {
                break;
            }
            const double left = layout.charLefts[index];
            const double right = left + layout.charWidths[index];
            const double ratio = index < static_cast<int>(intervals.size())
                ? progressRatio(intervals[index].first, intervals[index].second, tMs)
                : 0.0;
            segments.push_back({left, right, ratio});
            ++index;
            continue;
        }

        auto indices = rubyTargetIndices(ruby.value(), line, intervals);
        std::vector<int> validIndices;
        for (int candidate : indices) {
            if (
                candidate >= 0
                && static_cast<std::size_t>(candidate) < layout.charLefts.size()
                && static_cast<std::size_t>(candidate) < intervals.size()
            ) {
                validIndices.push_back(candidate);
            }
        }
        if (validIndices.empty()) {
            const double left = layout.charLefts[index];
            const double right = left + layout.charWidths[index];
            const double ratio = index < static_cast<int>(intervals.size())
                ? progressRatio(intervals[index].first, intervals[index].second, tMs)
                : 0.0;
            segments.push_back({left, right, ratio});
            ++index;
            continue;
        }

        double left = layout.charLefts[validIndices.front()];
        double right = layout.charLefts[validIndices.front()] + layout.charWidths[validIndices.front()];
        for (int candidate : validIndices) {
            left = std::min(left, layout.charLefts[candidate]);
            right = std::max(right, layout.charLefts[candidate] + layout.charWidths[candidate]);
        }
        const RubyAnnotation effectiveRuby = effectiveRubyForTarget(ruby.value(), validIndices, intervals);
        segments.push_back({left, right, rubyProgressRatio(effectiveRuby, tMs)});
        index = *std::max_element(validIndices.begin(), validIndices.end()) + 1;
    }
    return segments;
}

// Per-segment bands, same reasoning as afterClipBandsFromCharacterTiming:
// stopping at the first unfinished segment can only show one wipe front.
std::vector<std::pair<double, double>> fillClipBands(
    const std::vector<NativeFillSegment> &segments,
    bool rtl
) {
    std::vector<std::pair<double, double>> bands;
    for (const auto &segment : segments) {
        if (segment.ratio <= 0.0) {
            break;  // segments are time-ordered, so nothing later has begun
        }
        const double width = segment.right - segment.left;
        if (segment.ratio >= 1.0) {
            bands.emplace_back(segment.left, segment.right);
            continue;
        }
        if (rtl) {
            bands.emplace_back(segment.right - std::round(width * segment.ratio), segment.right);
        } else {
            bands.emplace_back(segment.left, segment.left + std::round(width * segment.ratio));
        }
    }
    return bands;
}

std::optional<std::pair<double, double>> fillClipBand(
    const std::vector<NativeFillSegment> &segments,
    bool rtl
) {
    if (segments.empty()) {
        return std::nullopt;
    }
    if (rtl) {
        double left = segments.front().right;
        double right = segments.front().right;
        for (const auto &segment : segments) {
            right = std::max(right, segment.right);
            if (segment.ratio <= 0.0) {
                break;
            }
            if (segment.ratio >= 1.0) {
                left = segment.left;
                continue;
            }
            left = segment.right - std::round((segment.right - segment.left) * segment.ratio);
            break;
        }
        if (right <= left) {
            return std::nullopt;
        }
        return std::pair<double, double>{left, right};
    }

    const double left = segments.front().left;
    double right = left;
    for (const auto &segment : segments) {
        if (segment.ratio <= 0.0) {
            break;
        }
        if (segment.ratio >= 1.0) {
            right = segment.right;
            continue;
        }
        right = segment.left + std::round((segment.right - segment.left) * segment.ratio);
        break;
    }
    if (right <= left) {
        return std::nullopt;
    }
    return std::pair<double, double>{left, right};
}

// The clip actually applied to the after-colour layer.
//
// Sequential timing yields one contiguous band and this is the same rectangle
// afterClipRect returns; concurrent wipes yield two, and only a region can hold
// both.  afterClipRect stays as the bounding rect for diagnostics.
QRegion afterClipRegion(const RenderConfig &cfg, const ResolvedStyle &style, const TimingLine &line, const LineLayout &layout, int tMs) {
    const double verticalExtent = layout.afterClipExtent > 0.0
        ? layout.afterClipExtent
        : afterClipVerticalExtent(style);
    const double top = layout.baselineY - layout.ascent - verticalExtent;
    const double height = layout.height + verticalExtent * 2.0;
    std::vector<std::pair<double, double>> bands = cfg.rubies.empty()
        ? afterClipBandsFromCharacterTiming(cfg, line, layout, tMs)
        : fillClipBands(fillSegmentsForLine(cfg, line, layout, tMs), cfg.rightToLeft);
    const double lineLeft = layout.x;
    const double lineRight = layout.x + layout.width;
    for (auto &band : bands) {
        band.first = std::clamp(band.first, lineLeft, lineRight);
        band.second = std::clamp(band.second, lineLeft, lineRight);
    }
    return bandsToRegion(mergeBands(std::move(bands)), top, height);
}

std::optional<QRectF> afterClipRect(const RenderConfig &cfg, const ResolvedStyle &style, const TimingLine &line, const LineLayout &layout, int tMs) {
    if (cfg.rubies.empty()) {
        return afterClipRectFromCharacterTiming(cfg, style, line, layout, tMs);
    }
    const auto band = fillClipBand(fillSegmentsForLine(cfg, line, layout, tMs), cfg.rightToLeft);
    if (!band.has_value()) {
        return std::nullopt;
    }
    const double verticalExtent = layout.afterClipExtent > 0.0 ? layout.afterClipExtent : afterClipVerticalExtent(style);
    const double top = layout.baselineY - layout.ascent - verticalExtent;
    const double height = layout.height + verticalExtent * 2.0;
    return QRectF(band->first, top, band->second - band->first, height);
}

void paintKaraokePathWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    int strokeWidth,
    int stroke2Width
) {
    if (stroke2Width > 0) {
        painter.strokePath(path, QPen(brushForFill(stroke2, rect), strokeWidth + stroke2Width, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    if (strokeWidth > 0) {
        painter.strokePath(path, QPen(brushForFill(stroke, rect), strokeWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    painter.fillPath(path, brushForFill(fill, rect));
}

QImage blurImage(const QImage &source, int radius) {
    const int blurRadius = std::max(radius, 1);
    QImage result(source.size(), QImage::Format_ARGB32_Premultiplied);
    result.fill(Qt::transparent);

    auto *effect = new QGraphicsBlurEffect();
    effect->setBlurRadius(static_cast<qreal>(blurRadius));
    effect->setBlurHints(QGraphicsBlurEffect::QualityHint);

    QGraphicsPixmapItem item(QPixmap::fromImage(source));
    item.setGraphicsEffect(effect);

    QGraphicsScene scene;
    scene.setSceneRect(0.0, 0.0, static_cast<qreal>(source.width()), static_cast<qreal>(source.height()));
    scene.addItem(&item);

    QPainter painter(&result);
    painter.setRenderHint(QPainter::Antialiasing);
    scene.render(
        &painter,
        QRectF(0.0, 0.0, static_cast<qreal>(source.width()), static_cast<qreal>(source.height())),
        QRectF(0.0, 0.0, static_cast<qreal>(source.width()), static_cast<qreal>(source.height()))
    );
    painter.end();
    scene.removeItem(&item);
    return result;
}

bool environmentDisablesCache(const char *name) {
    const QByteArray value = qgetenv(name).trimmed().toLower();
    return value == QByteArray("0") || value == QByteArray("false") || value == QByteArray("off");
}

bool glowBitmapCacheEnabled() {
    static const bool enabled = !environmentDisablesCache("KROK_SUBTITLE_NATIVE_GLOW_CACHE")
        && !environmentDisablesCache("KROK_SUBTITLE_GLOW_CACHE");
    return enabled;
}

bool textLayerCacheEnabled() {
    static const bool enabled = !environmentDisablesCache("KROK_SUBTITLE_NATIVE_TEXT_LAYER_CACHE")
        && !environmentDisablesCache("KROK_SUBTITLE_TEXT_LAYER_CACHE");
    return enabled;
}

std::vector<GlowBitmapCacheEntry> &glowBitmapCache() {
    static std::vector<GlowBitmapCacheEntry> cache;
    return cache;
}

std::vector<TextLayerCacheEntry> &textLayerCache() {
    static std::vector<TextLayerCacheEntry> cache;
    return cache;
}

std::vector<LayoutCacheEntry> &layoutCache() {
    thread_local std::vector<LayoutCacheEntry> cache;
    return cache;
}

int &layoutCacheLocalGeneration() {
    thread_local int generation = -1;
    return generation;
}

std::mutex &glowBitmapCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

std::mutex &textLayerCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

std::mutex &layoutCacheMutex() {
    static std::mutex mutex;
    return mutex;
}

std::atomic<int> &layoutCacheGeneration() {
    static std::atomic<int> generation{0};
    return generation;
}

GlowBitmapCacheStats &glowBitmapCacheStats() {
    static GlowBitmapCacheStats stats;
    return stats;
}

TextLayerCacheStats &textLayerCacheStats() {
    static TextLayerCacheStats stats;
    return stats;
}

LayoutCacheStats &layoutCacheStats() {
    static LayoutCacheStats stats;
    return stats;
}

void clearGlowBitmapCache() {
    std::lock_guard<std::mutex> lock(glowBitmapCacheMutex());
    glowBitmapCache().clear();
    glowBitmapCacheStats() = GlowBitmapCacheStats{};
}

void clearTextLayerCache() {
    std::lock_guard<std::mutex> lock(textLayerCacheMutex());
    textLayerCache().clear();
    textLayerCacheStats() = TextLayerCacheStats{};
}

void clearLayoutCache() {
    std::lock_guard<std::mutex> lock(layoutCacheMutex());
    layoutCacheGeneration().fetch_add(1);
    layoutCache().clear();
    layoutCacheLocalGeneration() = layoutCacheGeneration().load();
    layoutCacheStats() = LayoutCacheStats{};
}

QString doubleCacheKey(double value) {
    return QString::number(static_cast<qint64>(std::llround(value * 1000.0)));
}

QString fontCacheKey(const QFont &font) {
    return QStringLiteral("%1|%2|%3|%4|%5")
        .arg(font.family())
        .arg(font.pixelSize())
        .arg(font.weight())
        .arg(font.italic() ? 1 : 0)
        .arg(font.letterSpacing());
}

QString fillCacheKey(const PaintFillSpec &fill) {
    QString key = QStringLiteral("%1|%2|%3|%4|%5|%6|%7|%8|%9")
        .arg(fill.mode)
        .arg(fill.color)
        .arg(fill.startColor)
        .arg(fill.endColor)
        .arg(fill.splitTopColor)
        .arg(fill.splitBottomColor)
        .arg(fill.splitPositionPct)
        .arg(fill.imagePath)
        .arg(fill.imageScalePct);
    for (const auto &stop : fill.gradientStops) {
        key += QStringLiteral("|%1:%2").arg(stop.first).arg(stop.second);
    }
    return key;
}

QString textStackStyleCacheKey(
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    return QStringLiteral("fill=%1|stroke=%2|stroke2=%3|shadow=%4|deco=%5|sw=%6|s2w=%7|sx=%8|sy=%9|glow=%10")
        .arg(fillCacheKey(fill))
        .arg(fillCacheKey(stroke))
        .arg(fillCacheKey(stroke2))
        .arg(fillCacheKey(shadow))
        .arg(style.decorationKind)
        .arg(strokeWidth)
        .arg(stroke2Width)
        .arg(shadowOffsetX)
        .arg(shadowOffsetY)
        .arg(glowRadiusValue);
}

std::optional<TextLayerImage> lookupTextLayerCache(const QString &key) {
    std::lock_guard<std::mutex> lock(textLayerCacheMutex());
    auto &cache = textLayerCache();
    auto &stats = textLayerCacheStats();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            TextLayerCacheEntry entry = *it;
            cache.erase(it);
            cache.push_back(entry);
            ++stats.hits;
            return entry.layer;
        }
    }
    ++stats.misses;
    return std::nullopt;
}

void storeTextLayerCache(const QString &key, const TextLayerImage &layer) {
    std::lock_guard<std::mutex> lock(textLayerCacheMutex());
    auto &cache = textLayerCache();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            cache.erase(it);
            break;
        }
    }
    constexpr std::size_t kTextLayerCacheMax = 256;
    if (cache.size() >= kTextLayerCacheMax) {
        cache.erase(cache.begin());
    }
    cache.push_back(TextLayerCacheEntry{key, layer});
}

std::optional<LineLayout> lookupLayoutCache(const QString &key) {
    const int generation = layoutCacheGeneration().load();
    if (layoutCacheLocalGeneration() != generation) {
        layoutCache().clear();
        layoutCacheLocalGeneration() = generation;
    }
    auto &cache = layoutCache();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            LayoutCacheEntry entry = *it;
            cache.erase(it);
            cache.push_back(entry);
            {
                std::lock_guard<std::mutex> lock(layoutCacheMutex());
                ++layoutCacheStats().hits;
            }
            return entry.layout;
        }
    }
    {
        std::lock_guard<std::mutex> lock(layoutCacheMutex());
        ++layoutCacheStats().misses;
    }
    return std::nullopt;
}

void storeLayoutCache(const QString &key, const LineLayout &layout) {
    const int generation = layoutCacheGeneration().load();
    if (layoutCacheLocalGeneration() != generation) {
        layoutCache().clear();
        layoutCacheLocalGeneration() = generation;
    }
    auto &cache = layoutCache();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key) {
            cache.erase(it);
            break;
        }
    }
    constexpr std::size_t kLayoutCacheMax = 512;
    if (cache.size() >= kLayoutCacheMax) {
        cache.erase(cache.begin());
    }
    cache.push_back(LayoutCacheEntry{key, layout});
}

GlowBitmapCacheKeyParts glowBitmapCacheKey(const QImage &source, int radius) {
    GlowBitmapCacheKeyParts parts;
    parts.radius = std::max(radius, 1);
    parts.width = source.width();
    parts.height = source.height();
    parts.format = static_cast<int>(source.format());
    parts.checksum = QString::number(imageFullChecksum(source), 16);
    parts.shapeKey = QStringLiteral("%1:%2:%3:%4")
        .arg(parts.radius)
        .arg(parts.width)
        .arg(parts.height)
        .arg(parts.format);
    parts.key = QStringLiteral("%1:%2").arg(parts.shapeKey, parts.checksum);
    return parts;
}

void recordGlowBitmapCacheMiss(GlowBitmapCacheStats *stats, const GlowBitmapCacheKeyParts &parts, const QString &scope) {
    GlowBitmapCacheMissDiagnostic diagnostic;
    diagnostic.scope = scope.isEmpty() ? QStringLiteral("unknown") : scope;
    diagnostic.radius = parts.radius;
    diagnostic.width = parts.width;
    diagnostic.height = parts.height;
    diagnostic.format = parts.format;
    diagnostic.checksum = parts.checksum.left(16);
    if (stats->seenKeys.contains(parts.key)) {
        ++stats->evictedKeyMisses;
        diagnostic.category = QStringLiteral("evicted_key");
    } else if (stats->seenShapes.contains(parts.shapeKey)) {
        ++stats->contentVariantMisses;
        diagnostic.category = QStringLiteral("content_variant");
    } else {
        ++stats->shapeMisses;
        diagnostic.category = QStringLiteral("new_shape");
    }
    stats->seenKeys.insert(parts.key);
    stats->seenShapes.insert(parts.shapeKey);
    stats->missesByScope[diagnostic.scope] = stats->missesByScope.value(diagnostic.scope, 0) + 1;
    constexpr std::size_t kRecentMissLimit = 64;
    if (stats->recentMisses.size() >= kRecentMissLimit) {
        stats->recentMisses.erase(stats->recentMisses.begin());
    }
    stats->recentMisses.push_back(diagnostic);
}

QImage cachedBlurImage(const QImage &source, int radius, const QString &scope = QStringLiteral("unknown")) {
    if (!glowBitmapCacheEnabled()) {
        return blurImage(source, radius);
    }

    const GlowBitmapCacheKeyParts key = glowBitmapCacheKey(source, radius);
    std::lock_guard<std::mutex> lock(glowBitmapCacheMutex());
    auto &cache = glowBitmapCache();
    auto &stats = glowBitmapCacheStats();
    for (auto it = cache.begin(); it != cache.end(); ++it) {
        if (it->key == key.key) {
            GlowBitmapCacheEntry entry = *it;
            cache.erase(it);
            cache.push_back(entry);
            ++stats.hits;
            return entry.image;
        }
    }

    ++stats.misses;
    recordGlowBitmapCacheMiss(&stats, key, scope);
    QImage blurred = blurImage(source, radius);
    constexpr std::size_t kGlowBitmapCacheMax = 128;
    if (cache.size() >= kGlowBitmapCacheMax) {
        cache.erase(cache.begin());
    }
    cache.push_back(GlowBitmapCacheEntry{key.key, blurred});
    return blurred;
}

GlowLayerImage buildGlowLayerWithWidths(
    const QPainterPath &path,
    const PaintFillSpec &fill,
    const QRectF &rect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QString &scope = QStringLiteral("unknown")
) {
    const int glowRadius = std::max(radius, 1);
    const int baseWidth = stroke2Width > 0 ? strokeWidth + stroke2Width : std::max(strokeWidth, 0);
    const int glowWidth = std::max(1, baseWidth + glowRadius);
    const QRectF bounds = path.boundingRect();
    if (bounds.isEmpty()) {
        return GlowLayerImage{};
    }
    const double pad = std::ceil(glowWidth / 2.0 + glowRadius * 3.0) + 2.0;
    const QRectF layerRect = bounds.adjusted(-pad, -pad, pad, pad);
    const int imageWidth = std::max(1, static_cast<int>(std::ceil(layerRect.width())));
    const int imageHeight = std::max(1, static_cast<int>(std::ceil(layerRect.height())));

    QImage source(imageWidth, imageHeight, QImage::Format_ARGB32_Premultiplied);
    source.fill(Qt::transparent);

    QPainterPath localPath(path);
    localPath.translate(-layerRect.left(), -layerRect.top());
    const QRectF localRect = rect.translated(-layerRect.left(), -layerRect.top());

    QPainter layerPainter(&source);
    layerPainter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing);
    layerPainter.strokePath(localPath, QPen(brushForFill(fill, localRect), glowWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    layerPainter.end();

    return GlowLayerImage{
        cachedBlurImage(source, glowRadius, scope),
        QPointF(layerRect.left(), layerRect.top()),
    };
}

void paintGlowPathWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const PaintFillSpec &fill,
    const QRectF &rect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QString &scope = QStringLiteral("text")
) {
    const GlowLayerImage layer = buildGlowLayerWithWidths(path, fill, rect, radius, strokeWidth, stroke2Width, scope);
    if (!layer.image.isNull()) {
        painter.drawImage(layer.offset, layer.image);
    }
}

void blitTransformedGlowLayerWithWidths(
    QPainter &painter,
    const QPainterPath &uprightPath,
    const PaintFillSpec &fill,
    const QRectF &uprightRect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QTransform &transform,
    const QString &scope = QStringLiteral("transformed_text")
) {
    const GlowLayerImage layer = buildGlowLayerWithWidths(
        uprightPath,
        fill,
        uprightRect,
        radius,
        strokeWidth,
        stroke2Width,
        scope
    );
    if (layer.image.isNull()) {
        return;
    }
    painter.save();
    painter.setRenderHint(QPainter::SmoothPixmapTransform, true);
    painter.setTransform(transform, true);
    painter.drawImage(layer.offset, layer.image);
    painter.restore();
}

void paintTextLayerStackWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue,
    bool drawGlow = true,
    const QString &glowScope = QStringLiteral("text")
) {
    if (style.decorationKind == QStringLiteral("glow") && drawGlow) {
        paintGlowPathWithWidths(
            painter,
            path,
            shadow,
            rect,
            glowRadiusValue,
            strokeWidth,
            stroke2Width,
            glowScope
        );
    } else if (style.decorationKind == QStringLiteral("shadow")
               && (shadowOffsetX != 0 || shadowOffsetY != 0)) {
        QPainterPath shadowPath(path);
        shadowPath.translate(shadowOffsetX, shadowOffsetY);
        painter.fillPath(shadowPath, brushForFill(shadow, rect.translated(shadowOffsetX, shadowOffsetY)));
    }

    paintKaraokePathWithWidths(
        painter,
        path,
        rect,
        fill,
        stroke,
        stroke2,
        strokeWidth,
        stroke2Width
    );
}

TextLayerImage buildTextLayerStackWithWidths(
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    const QRectF bounds = path.boundingRect().united(rect);
    if (bounds.isEmpty()) {
        return TextLayerImage{};
    }
    const double strokeExtent = visualStrokeExtentForWidths(strokeWidth, stroke2Width);
    const double glowExtra = style.decorationKind == QStringLiteral("glow")
        ? glowExtentForWidths(strokeWidth, stroke2Width, glowRadiusValue)
        : 0.0;
    const int extent = static_cast<int>(std::max({
        strokeExtent,
        glowExtra,
        static_cast<double>(std::abs(shadowOffsetX)),
        static_cast<double>(std::abs(shadowOffsetY)),
        2.0,
    })) + 4;
    const int padLeft = std::max(0, -shadowOffsetX) + extent;
    const int padRight = std::max(0, shadowOffsetX) + extent;
    const int padTop = std::max(0, -shadowOffsetY) + extent;
    const int padBottom = std::max(0, shadowOffsetY) + extent;

    const QRectF layerRect(
        std::floor(bounds.left() - padLeft),
        std::floor(bounds.top() - padTop),
        std::ceil(bounds.width() + padLeft + padRight),
        std::ceil(bounds.height() + padTop + padBottom)
    );
    const int imageWidth = std::max(1, static_cast<int>(std::ceil(layerRect.width())));
    const int imageHeight = std::max(1, static_cast<int>(std::ceil(layerRect.height())));

    QImage image(imageWidth, imageHeight, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);

    QPainterPath localPath(path);
    localPath.translate(-layerRect.left(), -layerRect.top());
    const QRectF localRect = rect.translated(-layerRect.left(), -layerRect.top());

    QPainter layerPainter(&image);
    layerPainter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing);
    paintTextLayerStackWithWidths(
        layerPainter,
        localPath,
        localRect,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        glowRadiusValue
    );
    layerPainter.end();

    return TextLayerImage{
        image,
        QPointF(layerRect.left(), layerRect.top()),
    };
}

QString mainTextLayerCacheKey(
    const LineLayout &layout,
    const QRectF &rect,
    const QString &phase,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    return QStringLiteral("main|%1|text=%2|font=%3|x=%4|y=%5|w=%6|h=%7|ascent=%8|descent=%9|style=%10")
        .arg(phase)
        .arg(layout.text)
        .arg(fontCacheKey(layout.font))
        .arg(doubleCacheKey(rect.left()))
        .arg(doubleCacheKey(rect.top()))
        .arg(doubleCacheKey(rect.width()))
        .arg(doubleCacheKey(rect.height()))
        .arg(doubleCacheKey(layout.ascent))
        .arg(doubleCacheKey(layout.descent))
        .arg(textStackStyleCacheKey(
            fill,
            stroke,
            stroke2,
            shadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            glowRadiusValue
        ));
}

void paintCachedTextLayerStackWithWidths(
    QPainter &painter,
    const QString &cacheKey,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
);

const ResolvedStyle &layoutCharStyle(
    const LineLayout &layout,
    const ResolvedStyle &lineStyle,
    std::size_t index
) {
    if (layout.hasInlineStyles && index < layout.charStyles.size() && layout.charStyles[index] != nullptr) {
        return *layout.charStyles[index];
    }
    return lineStyle;
}

QFont layoutCharFont(const LineLayout &layout, std::size_t index) {
    if (index < layout.charFonts.size()) {
        return layout.charFonts[index];
    }
    return layout.font;
}

QString glyphRunVisualSignature(
    const LineLayout &layout,
    const ResolvedStyle &lineStyle,
    std::size_t index
) {
    const ResolvedStyle &style = layoutCharStyle(layout, lineStyle, index);
    return QStringLiteral("font=%1|before=%2|after=%3")
        .arg(fontCacheKey(layoutCharFont(layout, index)))
        .arg(textStackStyleCacheKey(
            style.baseFill,
            style.beforeStrokeFill,
            style.beforeStroke2Fill,
            style.beforeShadowFill,
            style,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glowRadius(style, false)
        ))
        .arg(textStackStyleCacheKey(
            style.afterFill,
            style.afterStrokeFill,
            style.afterStroke2Fill,
            style.afterShadowFill,
            style,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glowRadius(style, true)
        ));
}

std::vector<GlyphRunRef> glyphRunsForLayout(
    const TimingLine &line,
    const LineLayout &layout,
    const ResolvedStyle &lineStyle
) {
    std::vector<GlyphRunRef> runs;
    if (line.chars.empty()) {
        return runs;
    }
    if (!layout.hasInlineStyles) {
        runs.push_back(GlyphRunRef{0, line.chars.size()});
        return runs;
    }
    std::size_t start = 0;
    QString current = glyphRunVisualSignature(layout, lineStyle, 0);
    for (std::size_t index = 1; index < line.chars.size(); ++index) {
        const QString signature = glyphRunVisualSignature(layout, lineStyle, index);
        if (signature == current) {
            continue;
        }
        runs.push_back(GlyphRunRef{start, index});
        start = index;
        current = signature;
    }
    runs.push_back(GlyphRunRef{start, line.chars.size()});
    return runs;
}

QPainterPath glyphRunPath(
    const TimingLine &line,
    const LineLayout &layout,
    const GlyphRunRef &run
) {
    QPainterPath path;
    for (std::size_t index = run.start; index < run.end; ++index) {
        if (index >= line.chars.size() || index >= layout.charLefts.size()) {
            continue;
        }
        path.addText(
            QPointF(layout.charLefts[index], layout.baselineY),
            layoutCharFont(layout, index),
            line.chars[index].text
        );
    }
    return path;
}

QRectF glyphRunRect(
    const TimingLine &line,
    const LineLayout &layout,
    const GlyphRunRef &run
) {
    double left = std::numeric_limits<double>::infinity();
    double right = -std::numeric_limits<double>::infinity();
    double ascent = 0.0;
    double descent = 0.0;
    for (std::size_t index = run.start; index < run.end; ++index) {
        if (index >= line.chars.size() || index >= layout.charLefts.size() || index >= layout.charWidths.size()) {
            continue;
        }
        const QFont font = layoutCharFont(layout, index);
        const QFontMetricsF metrics(font);
        left = std::min(left, layout.charLefts[index]);
        right = std::max(right, layout.charLefts[index] + layout.charWidths[index]);
        ascent = std::max(ascent, metrics.ascent());
        descent = std::max(descent, metrics.descent());
    }
    if (!std::isfinite(left) || !std::isfinite(right)) {
        return QRectF();
    }
    return QRectF(
        left,
        layout.baselineY - ascent,
        std::max(right - left, 1.0),
        std::max(ascent + descent, 1.0)
    );
}

QString glyphRunTextLayerCacheKey(
    const TimingLine &line,
    const LineLayout &layout,
    const GlyphRunRef &run,
    const QRectF &rect,
    const QString &phase,
    const ResolvedStyle &style,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    QString glyphKey;
    for (std::size_t index = run.start; index < run.end; ++index) {
        if (index >= line.chars.size() || index >= layout.charLefts.size() || index >= layout.charWidths.size()) {
            continue;
        }
        glyphKey += QStringLiteral("|%1:%2@%3:%4:%5")
            .arg(index)
            .arg(line.chars[index].text)
            .arg(doubleCacheKey(layout.charLefts[index]))
            .arg(doubleCacheKey(layout.charWidths[index]))
            .arg(fontCacheKey(layoutCharFont(layout, index)));
    }
    return QStringLiteral("glyph_run|%1|glyphs=%2|x=%3|y=%4|w=%5|h=%6|style=%7")
        .arg(phase)
        .arg(glyphKey)
        .arg(doubleCacheKey(rect.left()))
        .arg(doubleCacheKey(rect.top()))
        .arg(doubleCacheKey(rect.width()))
        .arg(doubleCacheKey(rect.height()))
        .arg(textStackStyleCacheKey(
            fill,
            stroke,
            stroke2,
            shadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            glowRadiusValue
        ));
}

void paintGlyphRunTextLayer(
    QPainter &painter,
    const TimingLine &line,
    const LineLayout &layout,
    const GlyphRunRef &run,
    const ResolvedStyle &style,
    bool after
) {
    const QPainterPath path = glyphRunPath(line, layout, run);
    const QRectF rect = glyphRunRect(line, layout, run);
    if (path.isEmpty() || rect.isEmpty()) {
        return;
    }
    const PaintFillSpec &fill = after ? style.afterFill : style.baseFill;
    const PaintFillSpec &stroke = after ? style.afterStrokeFill : style.beforeStrokeFill;
    const PaintFillSpec &stroke2 = after ? style.afterStroke2Fill : style.beforeStroke2Fill;
    const PaintFillSpec &shadow = after ? style.afterShadowFill : style.beforeShadowFill;
    const int glow = glowRadius(style, after);
    paintCachedTextLayerStackWithWidths(
        painter,
        glyphRunTextLayerCacheKey(
            line,
            layout,
            run,
            rect,
            after ? QStringLiteral("after") : QStringLiteral("before"),
            style,
            fill,
            stroke,
            stroke2,
            shadow,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glow
        ),
        path,
        rect,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        style.strokeWidthPx,
        style.stroke2WidthPx,
        style.shadowOffsetX,
        style.shadowOffsetY,
        glow
    );
}

void paintGlyphRunTextLayers(
    QPainter &painter,
    const TimingLine &line,
    const LineLayout &layout,
    const ResolvedStyle &lineStyle,
    bool after
) {
    const auto runs = glyphRunsForLayout(line, layout, lineStyle);
    for (const GlyphRunRef &run : runs) {
        if (run.start >= run.end) {
            continue;
        }
        const ResolvedStyle &style = layoutCharStyle(layout, lineStyle, run.start);
        paintGlyphRunTextLayer(painter, line, layout, run, style, after);
    }
}

void paintCachedTextLayerStackWithWidths(
    QPainter &painter,
    const QString &cacheKey,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    if (!textLayerCacheEnabled()) {
        const TextLayerImage layer = buildTextLayerStackWithWidths(
            path,
            rect,
            fill,
            stroke,
            stroke2,
            shadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            glowRadiusValue
        );
        painter.drawImage(layer.offset, layer.image);
        return;
    }

    if (const auto cached = lookupTextLayerCache(cacheKey)) {
        painter.drawImage(cached->offset, cached->image);
        return;
    }

    const TextLayerImage layer = buildTextLayerStackWithWidths(
        path,
        rect,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        glowRadiusValue
    );
    storeTextLayerCache(cacheKey, layer);
    painter.drawImage(layer.offset, layer.image);
}

RubyLayerImage buildRubyTextLayer(
    const RubyDiagnostics &ruby,
    const QFont &rubyFont,
    const QFontMetricsF &rubyMetrics,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    const double strokeExtent = visualStrokeExtentForWidths(strokeWidth, stroke2Width);
    const double glowExtra = style.decorationKind == QStringLiteral("glow")
        ? glowExtentForWidths(strokeWidth, stroke2Width, glowRadiusValue)
        : 0.0;
    const int extent = static_cast<int>(std::max({
        strokeExtent,
        glowExtra,
        static_cast<double>(std::abs(shadowOffsetX)),
        static_cast<double>(std::abs(shadowOffsetY)),
        2.0,
    })) + 4;
    const int padLeft = std::max(0, -shadowOffsetX) + extent;
    const int padRight = std::max(0, shadowOffsetX) + extent;
    const int padTop = std::max(0, -shadowOffsetY) + extent;
    const int padBottom = std::max(0, shadowOffsetY) + extent;

    const int rubyWidth = std::max(1, static_cast<int>(std::ceil(ruby.readingWidth)));
    const int rubyHeight = std::max(1, static_cast<int>(std::ceil(rubyMetrics.height())));
    const int imageWidth = std::max(1, padLeft + rubyWidth + padRight);
    const int imageHeight = std::max(1, padTop + rubyHeight + padBottom);

    QImage image(imageWidth, imageHeight, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);

    const double localBaseline = padTop + rubyMetrics.ascent();
    const QPainterPath localPath = rubyTextPath(
        ruby.reading,
        rubyFont,
        rubyMetrics,
        padLeft,
        localBaseline,
        ruby.targetWidth
    );
    const QRectF localRect(
        padLeft,
        localBaseline - rubyMetrics.ascent(),
        ruby.readingWidth,
        rubyMetrics.height()
    );

    QPainter layerPainter(&image);
    layerPainter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing);
    paintTextLayerStackWithWidths(
        layerPainter,
        localPath,
        localRect,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        glowRadiusValue
    );
    layerPainter.end();

    return RubyLayerImage{
        image,
        QPointF(-padLeft, -(padTop + rubyMetrics.ascent())),
    };
}

QString rubyTextLayerCacheKey(
    const RubyDiagnostics &ruby,
    const QFont &rubyFont,
    const QFontMetricsF &rubyMetrics,
    const QString &phase,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    return QStringLiteral("ruby|%1|reading=%2|target=%3|reading_w=%4|font=%5|height=%6|ascent=%7|style=%8")
        .arg(phase)
        .arg(ruby.reading)
        .arg(doubleCacheKey(ruby.targetWidth))
        .arg(doubleCacheKey(ruby.readingWidth))
        .arg(fontCacheKey(rubyFont))
        .arg(doubleCacheKey(rubyMetrics.height()))
        .arg(doubleCacheKey(rubyMetrics.ascent()))
        .arg(textStackStyleCacheKey(
            fill,
            stroke,
            stroke2,
            shadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            glowRadiusValue
        ));
}

RubyLayerImage cachedRubyTextLayer(
    const RubyDiagnostics &ruby,
    const QFont &rubyFont,
    const QFontMetricsF &rubyMetrics,
    const QString &phase,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    const QString cacheKey = rubyTextLayerCacheKey(
        ruby,
        rubyFont,
        rubyMetrics,
        phase,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        glowRadiusValue
    );
    if (textLayerCacheEnabled()) {
        if (const auto cached = lookupTextLayerCache(cacheKey)) {
            return RubyLayerImage{cached->image, cached->offset};
        }
    }

    const RubyLayerImage layer = buildRubyTextLayer(
        ruby,
        rubyFont,
        rubyMetrics,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        glowRadiusValue
    );
    if (textLayerCacheEnabled()) {
        storeTextLayerCache(cacheKey, TextLayerImage{layer.image, layer.offset});
    }
    return layer;
}

void paintRubyDiagnostics(
    QPainter &painter,
    const ResolvedStyle &style,
    const std::vector<RubyDiagnostics> &rubies,
    const PaintFillSpec &base,
    const PaintFillSpec &fill,
    const PaintFillSpec &beforeStroke,
    const PaintFillSpec &afterStroke,
    const PaintFillSpec &beforeStroke2,
    const PaintFillSpec &afterStroke2,
    const PaintFillSpec &beforeShadow,
    const PaintFillSpec &afterShadow
) {
    if (rubies.empty()) {
        return;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const double scale = rubyScale(style);
    const int strokeWidth = scaledPx(style.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(style.stroke2WidthPx, scale);
    const int shadowOffsetX = scaledSignedPx(style.shadowOffsetX, scale);
    const int shadowOffsetY = scaledSignedPx(style.shadowOffsetY, scale);
    for (const RubyDiagnostics &ruby : rubies) {
        const RubyLayerImage beforeLayer = cachedRubyTextLayer(
            ruby,
            rubyFont,
            rubyMetrics,
            QStringLiteral("before"),
            base,
            beforeStroke,
            beforeStroke2,
            beforeShadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            scaledPx(glowRadius(style, false), scale)
        );
        painter.drawImage(QPointF(ruby.x, ruby.baselineY) + beforeLayer.offset, beforeLayer.image);
        if (ruby.progress <= 0.0) {
            continue;
        }
        const RubyLayerImage afterLayer = cachedRubyTextLayer(
            ruby,
            rubyFont,
            rubyMetrics,
            QStringLiteral("after"),
            fill,
            afterStroke,
            afterStroke2,
            afterShadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            scaledPx(glowRadius(style, true), scale)
        );
        painter.save();
        painter.setClipRect(
            QRectF(
                ruby.afterClipLeft,
                ruby.afterClipTop,
                ruby.afterClipRight - ruby.afterClipLeft,
                ruby.afterClipHeight
            ),
            Qt::IntersectClip
        );
        painter.drawImage(QPointF(ruby.x, ruby.baselineY) + afterLayer.offset, afterLayer.image);
        painter.restore();
    }
}

void paintInlineTextLayerStack(
    QPainter &painter,
    const TimingLine &line,
    const LineLayout &layout,
    bool after
) {
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        if (i >= layout.charLefts.size() || i >= layout.charWidths.size() || i >= layout.charFonts.size() || i >= layout.charStyles.size()) {
            continue;
        }
        const ResolvedStyle &style = *layout.charStyles[i];
        const QFontMetricsF metrics(layout.charFonts[i]);
        QPainterPath path;
        path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        const QRectF rect(
            layout.charLefts[i],
            layout.baselineY - metrics.ascent(),
            layout.charWidths[i],
            metrics.height()
        );
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            after ? style.afterFill : style.baseFill,
            after ? style.afterStrokeFill : style.beforeStrokeFill,
            after ? style.afterStroke2Fill : style.beforeStroke2Fill,
            after ? style.afterShadowFill : style.beforeShadowFill,
            style,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glowRadius(style, after)
        );
    }
}

double characterFillRatio(
    const std::vector<std::pair<int, int>> &intervals,
    std::size_t index,
    int tMs
) {
    if (index >= intervals.size()) {
        return 0.0;
    }
    const auto interval = intervals[index];
    if (tMs < interval.first) {
        return 0.0;
    }
    if (tMs >= interval.second) {
        return 1.0;
    }
    return progressRatio(interval.first, interval.second, tMs);
}

void paintTransformedTextStackWithFills(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &baseFill,
    const PaintFillSpec &afterFill,
    const PaintFillSpec &beforeStrokeFill,
    const PaintFillSpec &afterStrokeFill,
    const PaintFillSpec &beforeStroke2Fill,
    const PaintFillSpec &afterStroke2Fill,
    const PaintFillSpec &beforeShadowFill,
    const PaintFillSpec &afterShadowFill,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    int charX,
    int charWidth,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int beforeGlowRadius,
    int afterGlowRadius,
    bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("transformed_text")
) {
    const bool useCachedGlow = style.decorationKind == QStringLiteral("glow")
        && uprightPath != nullptr
        && uprightRect != nullptr
        && uprightTransform != nullptr
        && glowBitmapCacheEnabled();
    auto blitGlow = [&](const PaintFillSpec &shadowFill, int radius) {
        if (!useCachedGlow) {
            return;
        }
        blitTransformedGlowLayerWithWidths(
            painter,
            *uprightPath,
            shadowFill,
            *uprightRect,
            radius,
            strokeWidth,
            stroke2Width,
            *uprightTransform,
            glowScope
        );
    };

    const double clampedRatio = forceAfter ? 1.0 : std::clamp(ratio, 0.0, 1.0);
    if (clampedRatio <= 0.0) {
        blitGlow(beforeShadowFill, beforeGlowRadius);
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            baseFill,
            beforeStrokeFill,
            beforeStroke2Fill,
            beforeShadowFill,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            beforeGlowRadius,
            !useCachedGlow,
            glowScope + QStringLiteral(":before")
        );
        return;
    }
    if (clampedRatio >= 1.0) {
        blitGlow(afterShadowFill, afterGlowRadius);
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            afterFill,
            afterStrokeFill,
            afterStroke2Fill,
            afterShadowFill,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            afterGlowRadius,
            !useCachedGlow,
            glowScope + QStringLiteral(":after")
        );
        return;
    }

    blitGlow(beforeShadowFill, beforeGlowRadius);
    paintTextLayerStackWithWidths(
        painter,
        path,
        rect,
        baseFill,
        beforeStrokeFill,
        beforeStroke2Fill,
        beforeShadowFill,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        beforeGlowRadius,
        !useCachedGlow,
        glowScope + QStringLiteral(":before")
    );

    const double strokePad = visualStrokeExtentForWidths(strokeWidth, stroke2Width);
    const double clipX = rtl
        ? charX + charWidth * (1.0 - clampedRatio)
        : charX;
    const double clipWidth = std::max(charWidth * clampedRatio + strokePad, 1.0);
    painter.save();
    painter.setClipRect(
        QRectF(
            clipX - strokePad,
            rect.top() - strokePad,
            clipWidth,
            rect.height() + strokePad * 2.0
        ),
        Qt::IntersectClip
    );
    blitGlow(afterShadowFill, afterGlowRadius);
    paintTextLayerStackWithWidths(
        painter,
        path,
        rect,
        afterFill,
        afterStrokeFill,
        afterStroke2Fill,
        afterShadowFill,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        afterGlowRadius,
        !useCachedGlow,
        glowScope + QStringLiteral(":after")
    );
    painter.restore();
}

void paintTransformedTextStack(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    int charX,
    int charWidth,
    bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("main_transformed")
) {
    paintTransformedTextStackWithFills(
        painter,
        path,
        rect,
        style.baseFill,
        style.afterFill,
        style.beforeStrokeFill,
        style.afterStrokeFill,
        style.beforeStroke2Fill,
        style.afterStroke2Fill,
        style.beforeShadowFill,
        style.afterShadowFill,
        style,
        ratio,
        rtl,
        charX,
        charWidth,
        style.strokeWidthPx,
        style.stroke2WidthPx,
        style.shadowOffsetX,
        style.shadowOffsetY,
        glowRadius(style, false),
        glowRadius(style, true),
        forceAfter,
        uprightPath,
        uprightRect,
        uprightTransform,
        glowScope
    );
}

void paintRubyTransformedStack(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("ruby_transformed")
) {
    const double scale = rubyScale(style);
    const int strokeWidth = scaledPx(style.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(style.stroke2WidthPx, scale);
    const int shadowOffsetX = scaledSignedPx(style.shadowOffsetX, scale);
    const int shadowOffsetY = scaledSignedPx(style.shadowOffsetY, scale);
    paintTransformedTextStackWithFills(
        painter,
        path,
        rect,
        style.rubyBaseFill,
        style.rubyAfterFill,
        style.rubyBeforeStrokeFill,
        style.rubyAfterStrokeFill,
        style.rubyBeforeStroke2Fill,
        style.rubyAfterStroke2Fill,
        style.rubyBeforeShadowFill,
        style.rubyAfterShadowFill,
        style,
        ratio,
        rtl,
        static_cast<int>(std::round(rect.left())),
        std::max(1, static_cast<int>(std::round(rect.width()))),
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        scaledPx(glowRadius(style, false), scale),
        scaledPx(glowRadius(style, true), scale),
        forceAfter,
        uprightPath,
        uprightRect,
        uprightTransform,
        glowScope
    );
}

void paintRubyUtopiaText(
    QPainter &painter,
    const RenderConfig &cfg,
    const ResolvedStyle &style,
    const TimingLine &line,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals,
    const LineCharTransition &transition,
    int tMs
) {
    if (cfg.rubies.empty()) {
        return;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const double rubyBaselineY = layout.baselineY - layout.ascent - style.rubyGapPx;
    const int count = std::max(static_cast<int>(line.chars.size()), 1);

    for (const RubyAnnotation &ruby : cfg.rubies) {
        const auto indices = rubyTargetIndices(ruby, line, intervals);
        if (indices.empty()) {
            continue;
        }
        const auto targetRange = rubyTargetXRange(ruby, line, layout, intervals);
        if (!targetRange.has_value()) {
            continue;
        }
        const RubyAnnotation paintRuby = effectiveRubyForTarget(ruby, indices, intervals);
        const int firstIndex = *std::min_element(indices.begin(), indices.end());
        const int lastIndex = *std::max_element(indices.begin(), indices.end());
        const int followingDoneMs = utopiaFollowingDoneTime(line, intervals, lastIndex, cfg);
        const AnimationState state = transitionCharState(
            cfg,
            transition,
            intervals,
            firstIndex,
            count,
            tMs,
            cfg.height,
            followingDoneMs
        );
        if (state.opacity <= 0.0) {
            continue;
        }

        const double x = targetRange->first;
        const double targetWidth = std::max(targetRange->second - targetRange->first, 1.0);
        const double readingWidth = rubyLayoutWidth(paintRuby.reading, rubyMetrics, targetWidth);
        const bool groupExiting = indices.size() > 1 && tMs > followingDoneMs;
        painter.save();
        painter.setOpacity(painter.opacity() * state.opacity);

        if (groupExiting) {
            QString reading = paintRuby.reading;
            if (cfg.rightToLeft) {
                const auto visual = rubyUtopiaVisualUnits(reading);
                reading.clear();
                for (auto it = visual.rbegin(); it != visual.rend(); ++it) {
                    reading += *it;
                }
            }
            QPainterPath uprightPath = rubyTextPath(reading, rubyFont, rubyMetrics, x, rubyBaselineY, targetWidth);
            const QRectF sourceRect(
                x,
                rubyBaselineY - rubyMetrics.ascent(),
                readingWidth,
                rubyMetrics.height()
            );
            const double centerX = x + readingWidth / 2.0;
            const double centerY = rubyBaselineY - rubyMetrics.ascent() + rubyMetrics.height() / 2.0;
            const QTransform transform = characterTransform(
                centerX,
                centerY,
                state,
                QPointF(x, rubyBaselineY)
            );
            QPainterPath path = transform.map(uprightPath);
            const QRectF rect = path.boundingRect();
            if (!rect.isEmpty()) {
                paintRubyTransformedStack(
                    painter,
                    path,
                    rect,
                    style,
                    rubyProgressRatio(paintRuby, tMs),
                    cfg.rightToLeft,
                    true,
                    &uprightPath,
                    &sourceRect,
                    &transform,
                    QStringLiteral("ruby_utopia_group")
                );
            }
            painter.restore();
            continue;
        }

        auto unitsAndIntervals = rubyUtopiaReadingUnitsAndIntervals(paintRuby);
        if (cfg.rightToLeft) {
            std::reverse(unitsAndIntervals.begin(), unitsAndIntervals.end());
        }
        const auto layouts = rubyUnitLayouts(unitsAndIntervals, rubyMetrics, x, targetWidth);
        for (const RubyUnitLayout &unit : layouts) {
            const AnimationState unitState = transitionCharState(
                cfg,
                transition,
                intervals,
                firstIndex,
                count,
                tMs,
                cfg.height,
                followingDoneMs,
                unit.interval
            );
            if (unitState.opacity <= 0.0) {
                continue;
            }
            QPainterPath uprightPath;
            uprightPath.addText(QPointF(unit.x, rubyBaselineY), rubyFont, unit.text);
            const QRectF sourceRect(
                unit.x,
                rubyBaselineY - rubyMetrics.ascent(),
                unit.width,
                rubyMetrics.height()
            );
            const double centerX = unit.x + unit.width / 2.0;
            const double centerY = rubyBaselineY - rubyMetrics.ascent() + rubyMetrics.height() / 2.0;
            const QTransform transform = characterTransform(
                centerX,
                centerY,
                unitState,
                QPointF(unit.x, rubyBaselineY)
            );
            QPainterPath path = transform.map(uprightPath);
            const QRectF rect = path.boundingRect();
            if (rect.isEmpty()) {
                continue;
            }
            painter.save();
            painter.setOpacity(painter.opacity() * unitState.opacity);
            paintRubyTransformedStack(
                painter,
                path,
                rect,
                style,
                progressRatio(unit.interval.first, unit.interval.second, tMs),
                cfg.rightToLeft,
                false,
                &uprightPath,
                &sourceRect,
                &transform,
                QStringLiteral("ruby_utopia_reading")
            );
            painter.restore();
        }
        painter.restore();
    }
}

void paintUtopiaMainText(
    QPainter &painter,
    const RenderConfig &cfg,
    const TimingLine &line,
    const ResolvedStyle &style,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals,
    const LineCharTransition &transition,
    int tMs
) {
    const QFontMetricsF metrics(layout.font);
    const int count = std::max(static_cast<int>(line.chars.size()), 1);
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        if (i >= layout.charLefts.size() || i >= layout.charWidths.size()) {
            continue;
        }

        std::vector<int> indices{static_cast<int>(i)};
        std::optional<RubyAnnotation> groupRuby;
        const auto group = rubyGroupForCharIndex(cfg, line, intervals, static_cast<int>(i));
        bool groupExiting = false;
        if (group.has_value()) {
            const int groupDoneMs = utopiaFollowingDoneTime(line, intervals, group->indices.back(), cfg);
            groupExiting = tMs > groupDoneMs;
            if (groupExiting && static_cast<int>(i) != group->indices.front()) {
                continue;
            }
            if (groupExiting) {
                indices = group->indices;
                groupRuby = group->ruby;
            }
        }

        const int firstIndex = indices.front();
        const int lastIndex = indices.back();
        const int followingDoneMs = utopiaFollowingDoneTime(line, intervals, lastIndex, cfg);
        const auto wipeWindow = utopiaWipeWindowForIndex(
            line,
            layout,
            firstIndex,
            style,
            group,
            intervals[static_cast<std::size_t>(firstIndex)]
        );
        const AnimationState state = transitionCharState(
            cfg,
            transition,
            intervals,
            firstIndex,
            count,
            tMs,
            cfg.height,
            followingDoneMs,
            wipeWindow
        );
        if (state.opacity <= 0.0) {
            continue;
        }

        QPainterPath path;
        double left = layout.charLefts[static_cast<std::size_t>(firstIndex)];
        double right = left + layout.charWidths[static_cast<std::size_t>(firstIndex)];
        for (int index : indices) {
            if (index < 0 || static_cast<std::size_t>(index) >= line.chars.size()) {
                continue;
            }
            const std::size_t pos = static_cast<std::size_t>(index);
            if (pos >= layout.charLefts.size() || pos >= layout.charWidths.size()) {
                continue;
            }
            path.addText(QPointF(layout.charLefts[pos], layout.baselineY), layout.font, line.chars[pos].text);
            left = std::min(left, layout.charLefts[pos]);
            right = std::max(right, layout.charLefts[pos] + layout.charWidths[pos]);
        }
        const double width = std::max(right - left, 1.0);
        const QRectF sourceRect(left, layout.baselineY - metrics.ascent(), width, metrics.height());
        const double centerX = left + width / 2.0;
        const double centerY = layout.baselineY - metrics.ascent() + metrics.height() / 2.0;
        const QTransform transform = characterTransform(
            centerX,
            centerY,
            state,
            QPointF(left, layout.baselineY)
        );
        const QPainterPath paintPath = transform.map(path);
        const QRectF paintRect = paintPath.boundingRect();
        if (paintRect.isEmpty()) {
            continue;
        }
        const int paintLeft = static_cast<int>(std::round(paintRect.left()));
        const int paintWidth = std::max(1, static_cast<int>(std::round(paintRect.width())));
        const bool inUtopiaExit = cfg.exitAnim == QStringLiteral("utopia") && tMs > followingDoneMs;
        const double ratio = groupRuby.has_value()
            ? rubyProgressRatio(groupRuby.value(), tMs)
            : characterFillRatio(intervals, i, tMs);

        painter.save();
        painter.setOpacity(painter.opacity() * state.opacity);
        paintTransformedTextStack(
            painter,
            paintPath,
            paintRect,
            style,
            ratio,
            cfg.rightToLeft,
            paintLeft,
            paintWidth,
            inUtopiaExit,
            &path,
            &sourceRect,
            &transform,
            groupRuby.has_value() ? QStringLiteral("main_utopia_ruby_group") : QStringLiteral("main_utopia_char")
        );
        painter.restore();
    }
}

void paintLine(QPainter &painter, const RenderConfig &cfg, const TimingLine &line, int tMs, int lane, int visibleLineCount, RenderDiagnostics *diagnostics) {
    const QString text = lineText(line);
    if (text.isEmpty()) {
        return;
    }

    const ResolvedStyle &lineStyle = resolvedStyleForLine(cfg, line);

    const LineLayout layout = cachedLayoutLine(cfg, lineStyle, line, lane, visibleLineCount);

    const QRectF lineRect(layout.x, layout.baselineY - layout.ascent, layout.width, layout.height);
    const auto intervals = lineIntervals(line);
    const auto transition = lineCharTransitionContext(cfg, line, tMs, intervals);
    const auto rubyDiagnostics = rubyDiagnosticsForLine(cfg, lineStyle, line, layout, tMs);
    const bool useUtopiaMainText = transition.has_value()
        && transition->effect == QStringLiteral("utopia")
        && !layout.hasInlineStyles;

    if (useUtopiaMainText) {
        paintRubyUtopiaText(
            painter,
            cfg,
            lineStyle,
            line,
            layout,
            intervals,
            transition.value(),
            tMs
        );
    } else {
        paintRubyDiagnostics(
            painter,
            lineStyle,
            rubyDiagnostics,
            lineStyle.rubyBaseFill,
            lineStyle.rubyAfterFill,
            lineStyle.rubyBeforeStrokeFill,
            lineStyle.rubyAfterStrokeFill,
            lineStyle.rubyBeforeStroke2Fill,
            lineStyle.rubyAfterStroke2Fill,
            lineStyle.rubyBeforeShadowFill,
            lineStyle.rubyAfterShadowFill
        );
    }

    if (useUtopiaMainText) {
        paintUtopiaMainText(
            painter,
            cfg,
            line,
            lineStyle,
            layout,
            intervals,
            transition.value(),
            tMs
        );
    } else {
        paintGlyphRunTextLayers(painter, line, layout, lineStyle, false);
    }

    const auto clip = afterClipRect(cfg, lineStyle, line, layout, tMs);
    if (!useUtopiaMainText && clip.has_value() && clip->width() > 0.0) {
        // One band -> the original rectangle, byte for byte.  Two bands only
        // happen when the source really has two wipes running at once.
        const QRegion region = afterClipRegion(cfg, lineStyle, line, layout, tMs);
        painter.save();
        if (region.rectCount() > 1) {
            painter.setClipRegion(region, Qt::IntersectClip);
        } else {
            painter.setClipRect(*clip, Qt::IntersectClip);
        }
        paintGlyphRunTextLayers(painter, line, layout, lineStyle, true);
        painter.restore();
    }

    if (diagnostics != nullptr) {
        LineDiagnostics lineDiagnostics;
        lineDiagnostics.lane = lane;
        lineDiagnostics.lineX = layout.x;
        lineDiagnostics.lineWidth = layout.width;
        lineDiagnostics.baselineY = layout.baselineY;
        if (clip.has_value()) {
            lineDiagnostics.afterClipLeft = clip->left();
            lineDiagnostics.afterClipRight = clip->right();
            lineDiagnostics.afterClipTop = clip->top();
            lineDiagnostics.afterClipHeight = clip->height();
        } else {
            lineDiagnostics.afterClipLeft = layout.x;
            lineDiagnostics.afterClipRight = layout.x;
            const double verticalExtent = layout.afterClipExtent > 0.0 ? layout.afterClipExtent : afterClipVerticalExtent(lineStyle);
            lineDiagnostics.afterClipTop = layout.baselineY - layout.ascent - verticalExtent;
            lineDiagnostics.afterClipHeight = layout.height + verticalExtent * 2.0;
        }
        diagnostics->lines.push_back(lineDiagnostics);
        if (!diagnostics->hasFirstLine) {
            diagnostics->hasFirstLine = true;
            diagnostics->lineX = lineDiagnostics.lineX;
            diagnostics->lineWidth = lineDiagnostics.lineWidth;
            diagnostics->baselineY = lineDiagnostics.baselineY;
            diagnostics->afterClipLeft = lineDiagnostics.afterClipLeft;
            diagnostics->afterClipRight = lineDiagnostics.afterClipRight;
            diagnostics->afterClipTop = lineDiagnostics.afterClipTop;
            diagnostics->afterClipHeight = lineDiagnostics.afterClipHeight;
        }
        diagnostics->rubies.insert(
            diagnostics->rubies.end(),
            rubyDiagnostics.begin(),
            rubyDiagnostics.end()
        );
    }
}

RenderResult renderFrame(const RenderConfig &cfg, int tMs) {
    RenderResult result{
        QImage(cfg.physicalWidth(), cfg.physicalHeight(), QImage::Format_ARGB32_Premultiplied),
        RenderDiagnostics{},
    };
    result.image.fill(Qt::transparent);

    QPainter painter(&result.image);
    painter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing | QPainter::SmoothPixmapTransform);
    if (cfg.dpr != 1.0) {
        painter.scale(cfg.dpr, cfg.dpr);
    }

    const std::vector<DisplayLineRef> visibleLines = visibleDisplayLines(cfg, tMs);
    result.diagnostics.visibleLines = static_cast<int>(visibleLines.size());

    for (const DisplayLineRef &displayLine : visibleLines) {
        if (displayLine.line == nullptr) {
            continue;
        }
        paintLine(
            painter,
            cfg,
            *displayLine.line,
            tMs,
            displayLine.lane,
            result.diagnostics.visibleLines,
            &result.diagnostics
        );
    }

    painter.end();
    return result;
}

QJsonObject handleConfigure(const QJsonObject &request, std::optional<RenderConfig> *config) {
    QString error;
    auto parsed = parseRenderConfig(
        request.value(QStringLiteral("ir")).toObject(), &error
    );
    if (!parsed.has_value()) {
        QJsonObject out = response(false, QStringLiteral("configure"));
        out.insert(QStringLiteral("error"), error);
        return out;
    }
    *config = parsed;
    clearGlowBitmapCache();
    clearTextLayerCache();
    clearLayoutCache();
    QJsonObject out = response(true, QStringLiteral("configured"));
    out.insert(QStringLiteral("width"), parsed->width);
    out.insert(QStringLiteral("height"), parsed->height);
    out.insert(QStringLiteral("fps"), parsed->fps);
    out.insert(QStringLiteral("dpr"), parsed->dpr);
    out.insert(QStringLiteral("physical_width"), parsed->physicalWidth());
    out.insert(QStringLiteral("physical_height"), parsed->physicalHeight());
    out.insert(QStringLiteral("line_count"), static_cast<int>(parsed->lines.size()));
    out.insert(QStringLiteral("ruby_count"), static_cast<int>(parsed->rubies.size()));
    return out;
}

void appendFrameDiagnostics(
    QJsonObject *out,
    int tMs,
    const QImage &image,
    const RenderDiagnostics &diagnostics,
    double renderMs
) {
    out->insert(QStringLiteral("t_ms"), tMs);
    out->insert(QStringLiteral("width"), image.width());
    out->insert(QStringLiteral("height"), image.height());
    out->insert(QStringLiteral("checksum"), QString::number(imageChecksum(image)));
    out->insert(QStringLiteral("render_ms"), renderMs);
    out->insert(QStringLiteral("visible_lines"), diagnostics.visibleLines);
    out->insert(QStringLiteral("glow_cache_hits"), glowBitmapCacheStats().hits);
    out->insert(QStringLiteral("glow_cache_misses"), glowBitmapCacheStats().misses);
    out->insert(QStringLiteral("glow_cache_shape_misses"), glowBitmapCacheStats().shapeMisses);
    out->insert(QStringLiteral("glow_cache_content_variant_misses"), glowBitmapCacheStats().contentVariantMisses);
    out->insert(QStringLiteral("glow_cache_evicted_key_misses"), glowBitmapCacheStats().evictedKeyMisses);
    out->insert(QStringLiteral("glow_cache_size"), static_cast<int>(glowBitmapCache().size()));
    out->insert(QStringLiteral("text_layer_cache_hits"), textLayerCacheStats().hits);
    out->insert(QStringLiteral("text_layer_cache_misses"), textLayerCacheStats().misses);
    out->insert(QStringLiteral("text_layer_cache_size"), static_cast<int>(textLayerCache().size()));
    out->insert(QStringLiteral("layout_cache_hits"), layoutCacheStats().hits);
    out->insert(QStringLiteral("layout_cache_misses"), layoutCacheStats().misses);
    out->insert(QStringLiteral("layout_cache_size"), static_cast<int>(layoutCache().size()));
    QJsonObject missesByScope;
    const auto scopeKeys = glowBitmapCacheStats().missesByScope.keys();
    for (const QString &scope : scopeKeys) {
        missesByScope.insert(scope, glowBitmapCacheStats().missesByScope.value(scope));
    }
    out->insert(QStringLiteral("glow_cache_misses_by_scope"), missesByScope);
    QJsonArray recentGlowMisses;
    const auto &misses = glowBitmapCacheStats().recentMisses;
    const std::size_t start = misses.size() > 8 ? misses.size() - 8 : 0;
    for (std::size_t index = start; index < misses.size(); ++index) {
        const GlowBitmapCacheMissDiagnostic &miss = misses[index];
        QJsonObject item;
        item.insert(QStringLiteral("scope"), miss.scope);
        item.insert(QStringLiteral("category"), miss.category);
        item.insert(QStringLiteral("radius"), miss.radius);
        item.insert(QStringLiteral("width"), miss.width);
        item.insert(QStringLiteral("height"), miss.height);
        item.insert(QStringLiteral("format"), miss.format);
        item.insert(QStringLiteral("checksum"), miss.checksum);
        recentGlowMisses.append(item);
    }
    out->insert(QStringLiteral("glow_cache_recent_misses"), recentGlowMisses);
    QJsonArray lineDiagnostics;
    for (const LineDiagnostics &line : diagnostics.lines) {
        QJsonObject item;
        item.insert(QStringLiteral("lane"), line.lane);
        item.insert(QStringLiteral("line_x"), line.lineX);
        item.insert(QStringLiteral("line_width"), line.lineWidth);
        item.insert(QStringLiteral("baseline_y"), line.baselineY);
        item.insert(QStringLiteral("after_clip_left"), line.afterClipLeft);
        item.insert(QStringLiteral("after_clip_right"), line.afterClipRight);
        item.insert(QStringLiteral("after_clip_top"), line.afterClipTop);
        item.insert(QStringLiteral("after_clip_height"), line.afterClipHeight);
        lineDiagnostics.append(item);
    }
    out->insert(QStringLiteral("line_diagnostics"), lineDiagnostics);
    QJsonArray rubyDiagnostics;
    for (const RubyDiagnostics &ruby : diagnostics.rubies) {
        QJsonObject item;
        item.insert(QStringLiteral("kanji"), ruby.kanji);
        item.insert(QStringLiteral("reading"), ruby.reading);
        QJsonArray indices;
        for (int index : ruby.indices) {
            indices.append(index);
        }
        item.insert(QStringLiteral("indices"), indices);
        item.insert(QStringLiteral("x"), ruby.x);
        item.insert(QStringLiteral("baseline_y"), ruby.baselineY);
        item.insert(QStringLiteral("target_width"), ruby.targetWidth);
        item.insert(QStringLiteral("reading_width"), ruby.readingWidth);
        item.insert(QStringLiteral("progress"), ruby.progress);
        item.insert(QStringLiteral("after_clip_left"), ruby.afterClipLeft);
        item.insert(QStringLiteral("after_clip_right"), ruby.afterClipRight);
        item.insert(QStringLiteral("after_clip_top"), ruby.afterClipTop);
        item.insert(QStringLiteral("after_clip_height"), ruby.afterClipHeight);
        rubyDiagnostics.append(item);
    }
    out->insert(QStringLiteral("ruby_diagnostics"), rubyDiagnostics);
    if (diagnostics.hasFirstLine) {
        out->insert(QStringLiteral("line_x"), diagnostics.lineX);
        out->insert(QStringLiteral("line_width"), diagnostics.lineWidth);
        out->insert(QStringLiteral("baseline_y"), diagnostics.baselineY);
        out->insert(QStringLiteral("after_clip_left"), diagnostics.afterClipLeft);
        out->insert(QStringLiteral("after_clip_right"), diagnostics.afterClipRight);
        out->insert(QStringLiteral("after_clip_top"), diagnostics.afterClipTop);
        out->insert(QStringLiteral("after_clip_height"), diagnostics.afterClipHeight);
    }
}

QJsonObject handleRenderFrame(const QJsonObject &request, const std::optional<RenderConfig> &config) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
    const QString outputPath = stringValue(request, QStringLiteral("output_path"));
    if (outputPath.isEmpty()) {
        QJsonObject out = response(false, QStringLiteral("render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("output_path is required for native smoke render"));
        return out;
    }

    QElapsedTimer timer;
    timer.start();
    RenderResult rendered = renderFrame(*config, tMs);
    const double renderMs = static_cast<double>(timer.nsecsElapsed()) / 1000000.0;
    QImage &image = rendered.image;
    const bool saved = image.save(outputPath);
    QJsonObject out = response(saved, QStringLiteral("frame_ready"));
    out.insert(QStringLiteral("output_path"), outputPath);
    appendFrameDiagnostics(&out, tMs, image, rendered.diagnostics, renderMs);
    if (!saved) {
        out.insert(QStringLiteral("error"), QStringLiteral("failed to save output image"));
    }
    return out;
}

QJsonObject handleRenderFrameStats(const QJsonObject &request, const std::optional<RenderConfig> &config) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_frame_stats"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
    QElapsedTimer timer;
    timer.start();
    RenderResult rendered = renderFrame(*config, tMs);
    const double renderMs = static_cast<double>(timer.nsecsElapsed()) / 1000000.0;
    QJsonObject out = response(true, QStringLiteral("frame_stats"));
    appendFrameDiagnostics(&out, tMs, rendered.image, rendered.diagnostics, renderMs);
    return out;
}

std::vector<int> rangeTimestampsFromRequest(const QJsonObject &request, const RenderConfig &config) {
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

int rangeWorkerCountFromRequest(const QJsonObject &request, const RenderConfig &config, int frameCount) {
    const unsigned int hardwareThreads = std::max(1u, std::thread::hardware_concurrency());
    const int requestedThreads = intValue(request, QStringLiteral("threads"), static_cast<int>(hardwareThreads));
    return std::max(1, std::min(requestedThreads, std::max(frameCount, 1)));
}

QJsonObject handleRenderRangeStats(const QJsonObject &request, const std::optional<RenderConfig> &config) {
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
    out.insert(QStringLiteral("glow_cache_size"), static_cast<int>(glowBitmapCache().size()));
    out.insert(QStringLiteral("text_layer_cache_hits"), textLayerCacheStats().hits);
    out.insert(QStringLiteral("text_layer_cache_misses"), textLayerCacheStats().misses);
    out.insert(QStringLiteral("text_layer_cache_size"), static_cast<int>(textLayerCache().size()));
    out.insert(QStringLiteral("layout_cache_hits"), layoutCacheStats().hits);
    out.insert(QStringLiteral("layout_cache_misses"), layoutCacheStats().misses);
    out.insert(QStringLiteral("layout_cache_size"), static_cast<int>(layoutCache().size()));
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
                1, runtime->sharedFrames.slotCount()
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

QJsonObject handleRenderRange(const QJsonObject &request, const std::optional<RenderConfig> &config, RenderRuntime *runtime) {
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

krok::subtitle::native::RgbaColor gpuColor(const QString &value, const QString &fallback) {
    QColor color(value);
    if (!color.isValid()) {
        color = QColor(fallback);
    }
    return krok::subtitle::native::RgbaColor{
        static_cast<std::uint8_t>(color.red()),
        static_cast<std::uint8_t>(color.green()),
        static_cast<std::uint8_t>(color.blue()),
        static_cast<std::uint8_t>(color.alpha()),
    };
}

krok::subtitle::native::PaintStyle gpuPaint(
    const PaintFillSpec &source,
    const QString &fallback
) {
    using krok::subtitle::native::PaintStop;
    using krok::subtitle::native::PaintStyle;
    PaintStyle paint;
    paint.mode = source.mode.toStdString();
    paint.color = gpuColor(source.color, fallback);
    paint.imagePath = source.imagePath.toStdWString();
    paint.imageScale = static_cast<float>(
        std::clamp(source.imageScalePct, 1, 1000) / 100.0
    );
    if (!source.imagePath.isEmpty()) {
        const QFileInfo info(source.imagePath);
        if (info.exists() && info.isFile()) {
            paint.imageModifiedMs = static_cast<std::uint64_t>(
                std::max<qint64>(info.lastModified().toMSecsSinceEpoch(), 0)
            );
            paint.imageSize = static_cast<std::uint64_t>(
                std::max<qint64>(info.size(), 0)
            );
        }
    }
    const auto &sourceStops = source.mode == QStringLiteral("split_vertical")
        ? source.splitStops
        : source.gradientStops;
    paint.stops.reserve(sourceStops.size());
    for (const auto &[position, color] : sourceStops) {
        paint.stops.push_back(PaintStop{
            static_cast<float>(std::clamp(position / 100.0, 0.0, 1.0)),
            gpuColor(color, source.color),
        });
    }
    if (paint.stops.empty()) {
        if (source.mode == QStringLiteral("split_vertical")) {
            paint.stops = {
                {0.0f, gpuColor(source.splitTopColor, source.color)},
                {
                    static_cast<float>(std::clamp(
                        source.splitPositionPct / 100.0, 0.0, 1.0
                    )),
                    gpuColor(source.splitBottomColor, source.color),
                },
                {1.0f, gpuColor(source.splitBottomColor, source.color)},
            };
        } else {
            paint.stops = {
                {0.0f, gpuColor(source.startColor, source.color)},
                {1.0f, gpuColor(source.endColor, source.color)},
            };
        }
    }
    return paint;
}

void applyGpuResolvedStyle(
    krok::subtitle::native::TextStyle &target,
    const ResolvedStyle &source,
    double scale
) {
    target.fontFamily = source.fontFamily.toStdWString();
    target.latinFontFamily = source.fontFamilyLatin.isEmpty()
        ? std::nullopt
        : std::optional<std::wstring>(source.fontFamilyLatin.toStdWString());
    target.fontSize = static_cast<float>(source.fontSizePx * scale);
    target.latinFontSize = source.latinFontSizePx.has_value()
        ? std::optional<float>(static_cast<float>(*source.latinFontSizePx * scale))
        : std::nullopt;
    target.fontWeight = source.fontWeight;
    target.latinFontWeight = source.latinFontWeight;
    target.italic = source.italic;
    target.allowBiting = source.allowBiting;
    target.affectsRubyAnchor = source.affectsRubyAnchor;
    target.spaceWidthPercent = source.spaceWidthPercent;
    target.letterSpacing = static_cast<float>(source.letterSpacingPx * scale);
    target.beforeFill = gpuColor(source.baseFill.color, source.baseColor);
    target.afterFill = gpuColor(source.afterFill.color, source.fillColor);
    target.beforeStroke = gpuColor(source.beforeStrokeFill.color, source.beforeStrokeColor);
    target.afterStroke = gpuColor(source.afterStrokeFill.color, source.afterStrokeColor);
    target.beforeStroke2 = gpuColor(source.beforeStroke2Fill.color, source.beforeStroke2Color);
    target.afterStroke2 = gpuColor(source.afterStroke2Fill.color, source.afterStroke2Color);
    target.beforeDecor = gpuColor(source.beforeShadowFill.color, source.beforeShadowColor);
    target.afterDecor = gpuColor(source.afterShadowFill.color, source.afterShadowColor);
    target.beforeFillPaint = gpuPaint(source.baseFill, source.baseColor);
    target.afterFillPaint = gpuPaint(source.afterFill, source.fillColor);
    target.beforeStrokePaint = gpuPaint(source.beforeStrokeFill, source.beforeStrokeColor);
    target.afterStrokePaint = gpuPaint(source.afterStrokeFill, source.afterStrokeColor);
    target.beforeStroke2Paint = gpuPaint(source.beforeStroke2Fill, source.beforeStroke2Color);
    target.afterStroke2Paint = gpuPaint(source.afterStroke2Fill, source.afterStroke2Color);
    target.beforeDecorPaint = gpuPaint(source.beforeShadowFill, source.beforeShadowColor);
    target.afterDecorPaint = gpuPaint(source.afterShadowFill, source.afterShadowColor);
    target.strokeWidth = static_cast<float>(source.strokeWidthPx * scale);
    target.stroke2Width = static_cast<float>(source.stroke2WidthPx * scale);
    target.decorationKind = source.decorationKind.toStdString();
    target.glowBeforeRadius = static_cast<float>(source.glowBeforeRadiusPx * scale);
    target.glowAfterRadius = static_cast<float>(source.glowAfterRadiusPx * scale);
    target.glowConcentrationLevel = source.glowConcentrationLevel;
    target.shadowOffsetX = static_cast<float>(source.shadowOffsetX * scale);
    target.shadowOffsetY = static_cast<float>(source.shadowOffsetY * scale);

    const bool rubyUsesMainFont = source.rubyFontFollowMain
        && source.rubyFontFamily.isEmpty()
        && source.rubyFontFamilyLatin.isEmpty()
        && !source.rubyFontWeight.has_value()
        && !source.rubyLatinFontSizePx.has_value()
        && !source.rubyLatinFontWeight.has_value()
        && source.rubyFontSizePx == 45;
    target.rubyFontFamily = (
        rubyUsesMainFont || source.rubyFontFamily.isEmpty()
            ? source.fontFamily
            : source.rubyFontFamily
    ).toStdWString();
    const QString rubyLatinFamily = source.rubyFontFamilyLatin.isEmpty()
        ? (source.fontFamilyLatin.isEmpty()
            ? QString::fromStdWString(target.rubyFontFamily)
            : source.fontFamilyLatin)
        : source.rubyFontFamilyLatin;
    target.rubyLatinFontFamily = rubyLatinFamily.isEmpty()
        ? std::nullopt
        : std::optional<std::wstring>(rubyLatinFamily.toStdWString());
    target.rubyFontSize = static_cast<float>(source.rubyFontSizePx * scale);
    target.rubyLatinFontSize = source.rubyLatinFontSizePx.has_value()
        ? std::optional<float>(static_cast<float>(*source.rubyLatinFontSizePx * scale))
        : std::nullopt;
    target.rubyFontWeight = rubyUsesMainFont
        ? source.fontWeight
        : source.rubyFontWeight.value_or(source.fontWeight);
    target.rubyLatinFontWeight = source.rubyLatinFontWeight;
    target.rubyGap = static_cast<float>(source.rubyGapPx * scale);
    target.rubyInterval = static_cast<float>(source.rubyIntervalPx * scale);
    target.rubyAlignment = source.rubyAlignment.toStdString();
    target.rubyMainProgressMode = source.rubyMainProgressMode.toStdString();
    target.rubyHorizontalGradientWithMain = source.rubyHorizontalGradientWithMain;
    target.rubyBeforeFill = gpuColor(source.rubyBaseFill.color, source.rubyBaseColor);
    target.rubyAfterFill = gpuColor(source.rubyAfterFill.color, source.rubyFillColor);
    target.rubyBeforeStroke = gpuColor(
        source.rubyBeforeStrokeFill.color, source.rubyBeforeStrokeColor
    );
    target.rubyAfterStroke = gpuColor(
        source.rubyAfterStrokeFill.color, source.rubyAfterStrokeColor
    );
    target.rubyBeforeStroke2 = gpuColor(
        source.rubyBeforeStroke2Fill.color, source.rubyBeforeStroke2Color
    );
    target.rubyAfterStroke2 = gpuColor(
        source.rubyAfterStroke2Fill.color, source.rubyAfterStroke2Color
    );
    target.rubyBeforeDecor = gpuColor(
        source.rubyBeforeShadowFill.color, source.rubyBeforeShadowColor
    );
    target.rubyAfterDecor = gpuColor(
        source.rubyAfterShadowFill.color, source.rubyAfterShadowColor
    );
    target.rubyBeforeFillPaint = gpuPaint(source.rubyBaseFill, source.rubyBaseColor);
    target.rubyAfterFillPaint = gpuPaint(source.rubyAfterFill, source.rubyFillColor);
    target.rubyBeforeStrokePaint = gpuPaint(
        source.rubyBeforeStrokeFill, source.rubyBeforeStrokeColor
    );
    target.rubyAfterStrokePaint = gpuPaint(
        source.rubyAfterStrokeFill, source.rubyAfterStrokeColor
    );
    target.rubyBeforeStroke2Paint = gpuPaint(
        source.rubyBeforeStroke2Fill, source.rubyBeforeStroke2Color
    );
    target.rubyAfterStroke2Paint = gpuPaint(
        source.rubyAfterStroke2Fill, source.rubyAfterStroke2Color
    );
    target.rubyBeforeDecorPaint = gpuPaint(
        source.rubyBeforeShadowFill, source.rubyBeforeShadowColor
    );
    target.rubyAfterDecorPaint = gpuPaint(
        source.rubyAfterShadowFill, source.rubyAfterShadowColor
    );
    // Ruby stroke/decoration/glow that a scheme left unset follow this scheme's
    // effective main text (same derivation the base parser used to bake from the
    // global main). Resolving here — against the role-resolved ``source`` — is
    // what keeps each role's ruby tied to its own main instead of the global.
    const double rubyScale = static_cast<double>(source.rubyFontSizePx)
        / static_cast<double>(std::max(source.fontSizePx, 1));
    auto scaledFromMain = [&](int mainPx) {
        return std::max(0, static_cast<int>(std::lround(mainPx * rubyScale)));
    };
    const int rubyStrokePx = source.rubyStrokeWidthPx.value_or(
        scaledFromMain(source.strokeWidthPx)
    );
    // Same split as the CPU painter's _ruby_stroke2_enabled/_ruby_stroke2_width_value:
    // the flag and the width inherit along separate chains, and the flag gates
    // the width exactly once at the end.  Letting the width answer first made a
    // saved ruby width draw stroke2 even though the main text had it switched
    // off; gating the inherited width a second time made an explicitly enabled
    // ruby with no width of its own collapse to 0.
    const bool rubyStroke2On = source.rubyStroke2Enabled.value_or(source.stroke2Enabled);
    const int rubyStroke2Px = rubyStroke2On
        ? source.rubyStroke2WidthPx.value_or(scaledFromMain(source.stroke2RawWidthPx))
        : 0;
    target.rubyStrokeWidth = static_cast<float>(rubyStrokePx * scale);
    target.rubyStroke2Width = static_cast<float>(rubyStroke2Px * scale);
    target.rubyDecorationKind = (
        source.rubyDecorationKind.isEmpty()
            ? source.decorationKind
            : source.rubyDecorationKind
    ).toStdString();
    target.rubyGlowBeforeRadius = static_cast<float>(
        source.rubyGlowBeforeRadiusPx.value_or(scaledFromMain(source.glowBeforeRadiusPx))
        * scale
    );
    target.rubyGlowAfterRadius = static_cast<float>(
        source.rubyGlowAfterRadiusPx.value_or(scaledFromMain(source.glowAfterRadiusPx))
        * scale
    );
    target.rubyGlowConcentrationLevel = source.rubyGlowConcentrationLevel.value_or(
        source.glowConcentrationLevel
    );
    target.rubyShadowOffsetX = static_cast<float>(
        source.rubyShadowOffsetX.value_or(source.shadowOffsetX) * scale
    );
    target.rubyShadowOffsetY = static_cast<float>(
        source.rubyShadowOffsetY.value_or(source.shadowOffsetY) * scale
    );
    target.litEnabled = source.litEnabled;
    target.litStyle = source.litStyle.toStdString();
    target.litNumber = source.litNumber;
    target.litSize = static_cast<float>(source.litSize * scale);
    target.litOffsetX = static_cast<float>(source.litOffsetX * scale);
    target.litOffsetY = static_cast<float>(source.litOffsetY * scale);
    target.litTracking = static_cast<float>(source.litTracking * scale);
    target.litFill = gpuColor(source.litFillColor, QStringLiteral("#0000FF"));
    target.litStroke = gpuColor(source.litStrokeColor, QStringLiteral("#FFFFFF"));
    target.litStrokeWidth = static_cast<float>(source.litStrokeWidth * scale);
    target.litStrokeSoften = static_cast<float>(source.litStrokeSoften * scale);
    target.litOpacity = static_cast<float>(source.litOpacityPct) / 100.0f;
    target.litEdgeBrightness = static_cast<float>(source.litEdgeBrightnessPct) / 100.0f;
    target.litShadow = source.litShadow;
    target.litTimeOffsetMs = source.litTimeOffsetMs;
    target.litWaitingTimeMs = source.litWaitingTimeMs;
    target.litTransitionMode = source.litTransitionMode.toStdString();
    target.litTransitionRatioPct = source.litTransitionRatioPct;
    target.litTransitionAngleDeg = static_cast<float>(source.litTransitionAngleDeg);
    target.litTransitionDistance = static_cast<float>(source.litTransitionDistance * scale);
    target.signalsDurationMs = source.signalsDurationMs;
    target.volumeSize = static_cast<float>(source.volumeSize * scale);
    target.volumeOffsetX = static_cast<float>(source.volumeOffsetX * scale);
    target.volumeOffsetY = static_cast<float>(source.volumeOffsetY * scale);
    target.volumeColumnWidth = static_cast<float>(source.volumeColumnWidth * scale);
    target.volumeColumnCount = source.volumeColumnCount;
    target.volumeColumnSpacing = static_cast<float>(source.volumeColumnSpacing * scale);
    target.volumeAlign = source.volumeAlign;
    target.volumeRatio = static_cast<float>(source.volumeRatio);
    target.volumeFill = gpuColor(source.volumeFillColor, QStringLiteral("#FFFFFF"));
    target.volumeStroke = gpuColor(source.volumeStrokeColor, QStringLiteral("#0000FF"));
    target.volumeOverlayFill = gpuColor(source.volumeOverlayFillColor, QStringLiteral("#0000FF"));
    target.volumeOverlayStroke = gpuColor(source.volumeOverlayStrokeColor, QStringLiteral("#FFFFFF"));
    target.volumeFlashTimes = source.volumeFlashTimes;
    target.volumeFlashDurationRatio = static_cast<float>(source.volumeFlashDurationRatio);
    target.volumeTransitionRatioPct = source.volumeTransitionRatioPct;
}

// N3 CalcHorizontalAlignment: Top/Middle count forward from the page's first
// line, Bottom counts backward from its last one.  The two agree on a full
// page and diverge on a short one, where Bottom takes the tail of the list --
// a 2-line page under [left, center, right] is "center + right".
int alignmentIndexForLane(
    int lane,
    int alignmentCount,
    int pageLineCount,
    const QString &verticalPosition
) {
    if (alignmentCount <= 0) {
        return 0;
    }
    int index = std::max(lane, 0);
    if (verticalPosition == QStringLiteral("bottom")
        && pageLineCount > 0
        && pageLineCount < alignmentCount) {
        index = std::max(alignmentCount - pageLineCount + index, 0);
    }
    return std::clamp(index, 0, alignmentCount - 1);
}

void applyGpuLineLayout(
    krok::subtitle::native::TextStyle &target,
    const ResolvedLineLayout &layout,
    int lane,
    bool centerOverride,
    int pageLineCount,
    double scale
) {
    if (!layout.present) {
        return;
    }
    target.bottomMargin = static_cast<float>(layout.lineYMarginPx * scale);
    target.lineGap = static_cast<float>(layout.lineGapPx * scale);
    target.dualLineLayout = layout.dualLineLayout;
    target.laneCount = layout.dualLineLayout
        ? std::max(static_cast<int>(layout.lineAlignments.size()), 1)
        : 1;
    target.verticalPosition = layout.lineYPosition.toStdString();
    target.smartHorizontal = layout.lineHorizontalLayout == QStringLiteral("asymmetric")
        ? layout.smartHorizontal.toStdString()
        : "none";
    target.letterSpacing = static_cast<float>(layout.letterSpacingPx * scale);
    target.allowBiting = layout.allowBiting;
    target.rubyInterval = static_cast<float>(layout.rubyIntervalPx * scale);
    target.rubyAlignment = layout.rubyAlignment.toStdString();
    target.rubyGap = static_cast<float>(layout.rubyGapPx * scale);
    target.layoutOffsetX = 0.0f;
    target.layoutOffsetY = 0.0f;

    if (centerOverride || layout.lineHorizontalLayout == QStringLiteral("center")) {
        target.alignment = "center";
    } else if (layout.lineHorizontalLayout == QStringLiteral("per_row")) {
        const bool secondRow = lane == 1;
        target.alignment = (
            secondRow ? layout.row2Align : layout.row1Align
        ).toStdString();
        target.horizontalMargin = 0.0f;
        target.layoutOffsetX = static_cast<float>(
            (secondRow ? layout.row2OffsetX : layout.row1OffsetX) * scale
        );
    } else if (!layout.lineAlignments.empty()) {
        const int alignmentIndex = alignmentIndexForLane(
            lane,
            static_cast<int>(layout.lineAlignments.size()),
            pageLineCount,
            layout.lineYPosition
        );
        target.alignment = layout.lineAlignments[
            static_cast<std::size_t>(alignmentIndex)
        ].toStdString();
        target.horizontalMargin = static_cast<float>(
            layout.horizontalMarginPx * scale
        );
    }

    if (layout.lineHorizontalLayout == QStringLiteral("per_row")) {
        if (lane == 0) {
            target.layoutOffsetY = static_cast<float>(layout.row1OffsetY * scale);
        } else if (lane == 1) {
            target.layoutOffsetY = static_cast<float>(layout.row2OffsetY * scale);
        }
    }
}

krok::subtitle::native::RenderScene gpuSceneFromConfig(const RenderConfig &config) {
    using krok::subtitle::native::RenderScene;
    using krok::subtitle::native::TextChar;
    using krok::subtitle::native::TextLine;
    using krok::subtitle::native::TextStyle;

    const double scale = std::max(config.dpr, 0.01);
    const ResolvedStyle &sourceStyle = config.baseStyle;
    RenderScene scene;
    scene.width = config.physicalWidth();
    scene.height = config.physicalHeight();
    scene.layoutReferenceScale = static_cast<float>(scale);
    scene.viewportScale = static_cast<float>(config.viewportScalePct) / 100.0f;
    scene.viewportRotation = static_cast<float>(config.viewportRotationDeg);
    scene.viewportOffsetX = static_cast<float>(config.viewportOffsetX * scale);
    scene.viewportOffsetY = static_cast<float>(config.viewportOffsetY * scale);
    scene.viewportAlign = config.viewportAlign.toStdString();
    applyGpuResolvedStyle(scene.style, sourceStyle, scale);
    scene.style.layoutSemantics = config.layoutSemantics.toStdString();
    scene.style.smartHorizontal = config.lineHorizontalLayout == QStringLiteral("asymmetric")
        ? config.smartHorizontal.toStdString()
        : "none";
    scene.style.horizontalMargin = static_cast<float>(config.horizontalMarginPx * scale);
    scene.style.bottomMargin = static_cast<float>(config.lineYMarginPx * scale);
    scene.style.lineGap = static_cast<float>(config.lineGapPx * scale);
    scene.style.dualLineLayout = config.dualLineLayout;
    scene.style.laneCount = config.dualLineLayout
        ? std::max(static_cast<int>(config.lineAlignments.size()), 1)
        : 1;
    scene.style.verticalPosition = config.lineYPosition.toStdString();
    scene.style.vertical = config.vertical;
    scene.style.rightToLeft = config.rightToLeft;
    scene.style.leadInMs = config.lineLeadInMs;
    scene.style.tailMs = config.lineTailMs;
    if (config.lineHorizontalLayout == QStringLiteral("center")) {
        scene.style.alignment = "center";
    } else if (!config.lineAlignments.empty()) {
        scene.style.alignment = config.lineAlignments.front().toStdString();
    } else {
        scene.style.alignment = "left";
    }
    scene.lines.reserve(config.lines.size());
    scene.lineStyles.reserve(config.lines.size());
    QHash<QString, int> charStyleIndices;
    for (const TimingLine &sourceLine : config.lines) {
        if (sourceLine.chars.empty()) {
            continue;
        }
        TextStyle lineStyle = scene.style;
        applyGpuResolvedStyle(
            lineStyle, resolvedStyleForLine(config, sourceLine), scale
        );
        if (sourceLine.layout.present) {
            applyGpuLineLayout(
                lineStyle, sourceLine.layout, sourceLine.lane,
                sourceLine.centerOverride, sourceLine.pageLineCount, scale
            );
        } else {
            lineStyle.horizontalMargin = static_cast<float>(
                config.horizontalMarginPx * scale
            );
            if (sourceLine.centerOverride
                || config.lineHorizontalLayout == QStringLiteral("center")) {
                lineStyle.alignment = "center";
            } else if (!config.lineAlignments.empty()) {
                const int alignmentIndex = alignmentIndexForLane(
                    sourceLine.lane,
                    static_cast<int>(config.lineAlignments.size()),
                    sourceLine.pageLineCount,
                    config.lineYPosition
                );
                lineStyle.alignment = config.lineAlignments[
                    static_cast<std::size_t>(alignmentIndex)
                ].toStdString();
            }
        }
        lineStyle.layoutOffsetX += static_cast<float>(
            sourceLine.layoutOffsetX * scale
        );
        lineStyle.layoutOffsetY += static_cast<float>(
            sourceLine.layoutOffsetY * scale
        );
        scene.lineStyles.push_back(std::move(lineStyle));
        TextLine line;
        const int sourceTimingOffset = config.timingOffsetMs + sourceLine.sourceOffsetMs;
        line.startMs = lineStartMs(sourceLine) + sourceTimingOffset;
        line.endMs = lineEndMs(sourceLine) + sourceTimingOffset;
        line.sourceIndex = sourceLine.sourceIndex;
        line.sourceLineIndex = sourceLine.sourceLineIndex;
        line.pageIndex = sourceLine.pageIndex;
        line.lane = sourceLine.lane;
        line.signalHead = sourceLine.signalHead;
        line.centerOverride = sourceLine.centerOverride;
        // 标题钉在最下层（compositeOrder = kTitleCompositeOrder），所以源之间不必
        // 再为它预留 1 号槽位：主字幕 0，副源依次 1、2……
        line.compositeOrder = sourceLine.sourceIndex;
        if (sourceLine.guideAnchorLeft.has_value()
            && sourceLine.guideAnchorRight.has_value()) {
            line.guideAnchorLeft = static_cast<float>(
                *sourceLine.guideAnchorLeft * scale
            );
            line.guideAnchorRight = static_cast<float>(
                *sourceLine.guideAnchorRight * scale
            );
        }
        const auto verticalCharacterAnimation = [&](const QString &animation) {
            return config.vertical && (
                animation == QStringLiteral("char_fade")
                || animation == QStringLiteral("char_drip")
                || animation == QStringLiteral("spin_flip")
                || animation == QStringLiteral("utopia")
            );
        };
        line.entryAnimation = verticalCharacterAnimation(sourceLine.entryAnimation)
            ? "none"
            : sourceLine.entryAnimation.toStdString();
        line.entryDurationMs = verticalCharacterAnimation(sourceLine.entryAnimation)
            ? 0
            : sourceLine.entryDurationMs;
        line.exitAnimation = verticalCharacterAnimation(sourceLine.exitAnimation)
            ? "none"
            : sourceLine.exitAnimation.toStdString();
        line.exitDurationMs = verticalCharacterAnimation(sourceLine.exitAnimation)
            ? 0
            : sourceLine.exitDurationMs;
        line.karaokeAnimation = config.vertical
            ? "none"
            : sourceLine.karaokeAnimation.toStdString();
        if (sourceLine.displayStartMs.has_value()
            && sourceLine.displayEndMs.has_value()) {
            line.displayWindows.push_back(krok::subtitle::native::DisplayWindow{
                *sourceLine.displayStartMs + sourceTimingOffset,
                *sourceLine.displayEndMs + sourceTimingOffset,
            });
        }
        line.placementWindows.reserve(sourceLine.placementWindows.size());
        for (const auto &window : sourceLine.placementWindows) {
            line.placementWindows.push_back(
                krok::subtitle::native::PlacementWindow{
                    window.startMs + sourceTimingOffset,
                    window.endMs + sourceTimingOffset,
                    window.offsetX * static_cast<float>(scale),
                    window.offsetY * static_cast<float>(scale),
                }
            );
        }
        line.chars.reserve(sourceLine.chars.size());
        for (std::size_t index = 0; index < sourceLine.chars.size(); ++index) {
            int styleIndex = -1;
            // Painter's vertical path currently uses the resolved line style
            // for every glyph; inline role styles are a horizontal-only
            // contract until the CPU oracle itself gains vertical runs.
            if (!config.vertical && !sourceLine.chars[index].roleLabel.isEmpty()) {
                QString key = resolvedStyleKey(
                    sourceLine.singerId, sourceLine.chars[index].roleLabel
                );
                if (sourceLine.layout.present) {
                    key += QStringLiteral("|layout:%1:%2:%3:%4:%5")
                        .arg(sourceLine.layout.letterSpacingPx)
                        .arg(sourceLine.layout.allowBiting ? 1 : 0)
                        .arg(sourceLine.layout.rubyIntervalPx)
                        .arg(sourceLine.layout.rubyAlignment)
                        .arg(sourceLine.layout.rubyGapPx);
                }
                const auto existing = charStyleIndices.constFind(key);
                if (existing != charStyleIndices.constEnd()) {
                    styleIndex = existing.value();
                } else {
                    TextStyle charStyle = scene.lineStyles.back();
                    applyGpuResolvedStyle(
                        charStyle,
                        resolvedStyleForCharacter(config, sourceLine, sourceLine.chars[index]),
                        scale
                    );
                    applyGpuLineLayout(
                        charStyle, sourceLine.layout, sourceLine.lane,
                        sourceLine.centerOverride, sourceLine.pageLineCount, scale
                    );
                    charStyle.layoutOffsetX += static_cast<float>(
                        sourceLine.layoutOffsetX * scale
                    );
                    charStyle.layoutOffsetY += static_cast<float>(
                        sourceLine.layoutOffsetY * scale
                    );
                    styleIndex = static_cast<int>(scene.charStyles.size());
                    scene.charStyles.push_back(std::move(charStyle));
                    charStyleIndices.insert(key, styleIndex);
                }
            }
            line.chars.push_back(TextChar{
                sourceLine.chars[index].text.toStdWString(),
                sourceLine.chars[index].startMs + sourceTimingOffset,
                charEndMs(sourceLine, index) + sourceTimingOffset,
                styleIndex,
                sourceLine.chars[index].vectorGlyph,
                sourceLine.chars[index].bitmapGuide,
            });
            line.chars.back().wipePoints = {
                krok::subtitle::native::WipePoint{line.chars.back().startMs, 0.0f},
                krok::subtitle::native::WipePoint{line.chars.back().endMs, 1.0f},
            };
        }
        const auto intervals = lineIntervals(sourceLine);
        const int sourceLineStart = lineStartMs(sourceLine);
        const int sourceLineEnd = lineEndMs(sourceLine);
        std::vector<bool> rubyMainWipeAssigned(line.chars.size(), false);
        for (const RubyAnnotation &sourceRuby : config.rubies) {
            const bool globalPosition = sourceRuby.posStartMs == 0 && sourceRuby.posEndMs == 0;
            if (!globalPosition && (
                sourceRuby.posEndMs <= sourceLineStart || sourceRuby.posStartMs >= sourceLineEnd
            )) {
                continue;
            }
            const auto targetIndices = rubyTargetIndices(sourceRuby, sourceLine, intervals);
            if (targetIndices.empty()) {
                continue;
            }
            const auto [minimum, maximum] = std::minmax_element(
                targetIndices.begin(), targetIndices.end()
            );
            if (*minimum < 0 || *maximum >= static_cast<int>(sourceLine.chars.size())) {
                continue;
            }
            const RubyAnnotation ruby = effectiveRubyForTarget(
                sourceRuby, targetIndices, intervals
            );
            krok::subtitle::native::TextRuby sceneRuby;
            sceneRuby.baseText = ruby.kanji.toStdWString();
            sceneRuby.reading = ruby.reading.toStdWString();
            sceneRuby.firstCharIndex = *minimum;
            sceneRuby.lastCharIndex = *maximum;
            sceneRuby.startMs = ruby.posStartMs + sourceTimingOffset;
            sceneRuby.endMs = ruby.posEndMs + sourceTimingOffset;
            const bool mainWipeAlreadyAssigned = std::any_of(
                targetIndices.begin(), targetIndices.end(),
                [&](int targetIndex) {
                    return targetIndex >= 0
                        && targetIndex < static_cast<int>(rubyMainWipeAssigned.size())
                        && rubyMainWipeAssigned[static_cast<std::size_t>(targetIndex)];
                }
            );
            if (!mainWipeAlreadyAssigned && !isUtopiaGroupMarker(ruby)) {
                const bool preserveBaseTiming =
                    rubyMainUsesBaseTiming(sourceLine, targetIndices);
                if (!preserveBaseTiming) {
                    applyRubyMainWipePoints(
                        line, *minimum, *maximum,
                        rubyMainWipeIntervals(
                            ruby, scene.lineStyles.back().rubyMainProgressMode
                        ),
                        sourceTimingOffset
                    );
                }
                for (int targetIndex : targetIndices) {
                    if (targetIndex >= 0
                        && targetIndex < static_cast<int>(rubyMainWipeAssigned.size())) {
                        rubyMainWipeAssigned[static_cast<std::size_t>(targetIndex)] = true;
                    }
                }
            }
            for (int targetIndex : targetIndices) {
                if (targetIndex < 0
                    || targetIndex >= static_cast<int>(sourceLine.chars.size())
                    || sourceLine.chars[static_cast<std::size_t>(targetIndex)].roleLabel.isEmpty()) {
                    continue;
                }
                sceneRuby.styleIndex = line.chars[
                    static_cast<std::size_t>(targetIndex)
                ].styleIndex;
                break;
            }
            for (const auto &unit : rubyUtopiaReadingUnitsAndIntervals(ruby)) {
                sceneRuby.units.push_back(krok::subtitle::native::RubyUnit{
                    unit.first.normalized(QString::NormalizationForm_C).toStdWString(),
                    unit.second.first + sourceTimingOffset,
                    unit.second.second + sourceTimingOffset,
                });
            }
            if (!sceneRuby.units.empty()) {
                line.rubies.push_back(std::move(sceneRuby));
            }
        }
        scene.lines.push_back(std::move(line));
    }

    if (!config.title.isEmpty()
        && config.title.value(QStringLiteral("enabled")).toBool(false)) {
        const QString text = stringValue(config.title, QStringLiteral("text"));
        const QStringList rows = text.split(u'\n', Qt::KeepEmptyParts);
        std::vector<krok::subtitle::native::DisplayWindow> windows;
        const int defaultFadeInMs = std::max(
            0, intValue(config.title, QStringLiteral("fade_in_ms"), 0)
        );
        const int defaultFadeOutMs = std::max(
            0, intValue(config.title, QStringLiteral("fade_out_ms"), 0)
        );
        for (const auto &windowValue : config.title.value(
                 QStringLiteral("windows")
             ).toArray()) {
            const QJsonArray window = windowValue.toArray();
            if (window.size() < 2) {
                continue;
            }
            // Title windows are already resolved on the project/media
            // timeline. Lyrics timing and primary-track offsets must not move
            // the opening or ending title.
            const int start = window.at(0).toInt();
            const int end = window.at(1).toInt();
            const int fadeInMs = window.size() > 2
                ? std::max(0, window.at(2).toInt())
                : defaultFadeInMs;
            const int fadeOutMs = window.size() > 3
                ? std::max(0, window.at(3).toInt())
                : defaultFadeOutMs;
            if (end > start) {
                windows.push_back({start, end, fadeInMs, fadeOutMs});
            }
        }
        if (!windows.empty() && std::any_of(
                rows.begin(), rows.end(), [](const QString &row) {
                    return !row.trimmed().isEmpty();
                }
            )) {
            TextStyle titleStyle;
            applyGpuResolvedStyle(
                titleStyle,
                resolvedStyleFromTitle(sourceStyle, config.title),
                scale
            );
            // The title always uses N3 char-box geometry, independent of the
            // project's layout semantics: box height = font size + edge with the
            // baseline split by the face's A:D ratio.  Qt/DWrite ascent carries
            // the em's internal leading, which would leave the top margin
            // visibly larger than the side margins for the same number.  Mirrors
            // Painter's _layout_title_overlay.
            titleStyle.layoutSemantics = "n3_1074";
            titleStyle.lineGap = static_cast<float>(std::max(
                0, intValue(config.title, QStringLiteral("line_gap_px"), 0)
            ) * scale);
            titleStyle.dualLineLayout = rows.size() > 1;
            titleStyle.laneCount = std::max(static_cast<int>(rows.size()), 1);
            titleStyle.leadInMs = 0;
            titleStyle.tailMs = 0;
            const QString anchor = stringValue(
                config.title, QStringLiteral("anchor"), QStringLiteral("top_left")
            );
            const float offsetX = static_cast<float>(
                intValue(config.title, QStringLiteral("offset_x"), 0) * scale
            );
            const float offsetY = static_cast<float>(
                intValue(config.title, QStringLiteral("offset_y"), 0) * scale
            );
            // N3 counts half the edge inside the char box on every side, so an
            // edge-anchored title keeps its stroke inside the margin.  The
            // vertical half is already part of the N3 box height; the horizontal
            // one has to be folded into the margin here, exactly as Painter does
            // in _title_block_origin.
            const float titleHalfEdge = std::max(titleStyle.strokeWidth, 0.0f) * 0.5f;
            if (anchor.endsWith(QStringLiteral("left"))) {
                titleStyle.alignment = "left";
                titleStyle.horizontalMargin = offsetX + titleHalfEdge;
            } else if (anchor.endsWith(QStringLiteral("right"))) {
                titleStyle.alignment = "right";
                titleStyle.horizontalMargin = offsetX + titleHalfEdge;
            } else {
                titleStyle.alignment = "center";
                titleStyle.centerOffsetX = offsetX;
            }
            if (anchor.startsWith(QStringLiteral("top"))) {
                titleStyle.verticalPosition = "top";
                titleStyle.bottomMargin = offsetY;
            } else if (anchor.startsWith(QStringLiteral("bottom"))) {
                titleStyle.verticalPosition = "bottom";
                titleStyle.bottomMargin = offsetY;
            } else {
                titleStyle.verticalPosition = "center";
                titleStyle.centerOffsetY = offsetY;
            }

            const QJsonObject titleRoleStyles = config.title.value(
                QStringLiteral("role_styles")
            ).toObject();
            const QJsonArray titleRoleRows = config.title.value(
                QStringLiteral("resolved_role_labels")
            ).toArray();
            QHash<QString, int> titleRoleStyleIndices;
            for (auto it = titleRoleStyles.begin(); it != titleRoleStyles.end(); ++it) {
                if (!it.value().isObject()) {
                    continue;
                }
                TextStyle roleStyle = titleStyle;
                applyGpuResolvedStyle(
                    roleStyle,
                    resolvedStyleFromTitle(sourceStyle, it.value().toObject()),
                    scale
                );
                const int styleIndex = static_cast<int>(scene.charStyles.size());
                scene.charStyles.push_back(std::move(roleStyle));
                titleRoleStyleIndices.insert(it.key(), styleIndex);
            }

            for (int rowIndex = 0; rowIndex < rows.size(); ++rowIndex) {
                const QString &row = rows.at(rowIndex);
                if (row.isEmpty()) {
                    continue;
                }
                TextLine titleLine;
                titleLine.startMs = windows.front().startMs;
                titleLine.endMs = windows.back().endMs;
                titleLine.sourceIndex = -1;
                titleLine.sourceLineIndex = rowIndex;
                titleLine.lane = rowIndex;
                titleLine.compositeOrder =
                    krok::subtitle::native::kTitleCompositeOrder;
                titleLine.staticOverlay = true;
                titleLine.fadeInMs = defaultFadeInMs;
                titleLine.fadeOutMs = defaultFadeOutMs;
                titleLine.displayWindows = windows;
                titleLine.chars.reserve(static_cast<std::size_t>(row.size()));
                const QJsonArray roleLabels = rowIndex < titleRoleRows.size()
                    ? titleRoleRows.at(rowIndex).toArray()
                    : QJsonArray{};
                for (int charIndex = 0; charIndex < row.size(); ++charIndex) {
                    const QString roleLabel = charIndex < roleLabels.size()
                        ? roleLabels.at(charIndex).toString()
                        : QString{};
                    const int styleIndex = titleRoleStyleIndices.value(roleLabel, -1);
                    titleLine.chars.push_back(TextChar{
                        QString(row.at(charIndex)).toStdWString(),
                        1000000000,
                        1000000001,
                        styleIndex,
                        std::nullopt,
                        std::nullopt,
                    });
                }
                scene.lineStyles.push_back(titleStyle);
                scene.lines.push_back(std::move(titleLine));
            }
        }
    }
    return scene;
}

void appendGpuDiagnostics(
    QJsonObject *out,
    const krok::subtitle::native::BackendDiagnostics &diagnostics
) {
    out->insert(QStringLiteral("cache_hits"), static_cast<qint64>(diagnostics.cacheHits));
    out->insert(QStringLiteral("cache_misses"), static_cast<qint64>(diagnostics.cacheMisses));
    out->insert(
        QStringLiteral("estimated_cache_bytes"),
        static_cast<qint64>(diagnostics.estimatedCacheBytes)
    );
    out->insert(QStringLiteral("cached_lines"), static_cast<qint64>(diagnostics.lineCount));
    out->insert(QStringLiteral("cached_chars"), static_cast<qint64>(diagnostics.charCount));
    out->insert(
        QStringLiteral("cached_geometries"),
        static_cast<qint64>(diagnostics.geometryCount)
    );
    out->insert(QStringLiteral("cached_rubies"), static_cast<qint64>(diagnostics.rubyCount));
    out->insert(QStringLiteral("cached_styles"), static_cast<qint64>(diagnostics.styleCount));
    out->insert(
        QStringLiteral("video_memory_info_available"),
        diagnostics.videoMemoryInfoAvailable
    );
    out->insert(
        QStringLiteral("local_video_memory_usage_bytes"),
        static_cast<qint64>(diagnostics.localVideoMemoryUsageBytes)
    );
    out->insert(
        QStringLiteral("local_video_memory_budget_bytes"),
        static_cast<qint64>(diagnostics.localVideoMemoryBudgetBytes)
    );
    out->insert(
        QStringLiteral("non_local_video_memory_usage_bytes"),
        static_cast<qint64>(diagnostics.nonLocalVideoMemoryUsageBytes)
    );
    out->insert(
        QStringLiteral("non_local_video_memory_budget_bytes"),
        static_cast<qint64>(diagnostics.nonLocalVideoMemoryBudgetBytes)
    );
    out->insert(QStringLiteral("counters_enabled"), diagnostics.countersEnabled);
    out->insert(
        QStringLiteral("frames_rendered"),
        static_cast<qint64>(diagnostics.framesRendered)
    );
    out->insert(
        QStringLiteral("brush_created"),
        static_cast<qint64>(diagnostics.brushCreated)
    );
    out->insert(
        QStringLiteral("geometry_created_stable"),
        static_cast<qint64>(diagnostics.geometryCreatedStable)
    );
    out->insert(
        QStringLiteral("geometry_created_dynamic"),
        static_cast<qint64>(diagnostics.geometryCreatedDynamic)
    );
    out->insert(
        QStringLiteral("realization_hit"),
        static_cast<qint64>(diagnostics.realizationHit)
    );
    out->insert(
        QStringLiteral("realization_miss"),
        static_cast<qint64>(diagnostics.realizationMiss)
    );
    out->insert(
        QStringLiteral("stroke_draw"),
        static_cast<qint64>(diagnostics.strokeDraw)
    );
    out->insert(
        QStringLiteral("stroke2_draw"),
        static_cast<qint64>(diagnostics.stroke2Draw)
    );
    out->insert(
        QStringLiteral("glow_source_area_px"),
        static_cast<qint64>(diagnostics.glowSourceAreaPx)
    );
    out->insert(
        QStringLiteral("layer_push"),
        static_cast<qint64>(diagnostics.layerPush)
    );
    out->insert(QStringLiteral("animation_layout_ms"), diagnostics.animationLayoutMs);
    out->insert(QStringLiteral("geometry_ms"), diagnostics.geometryMs);
    out->insert(QStringLiteral("stroke_ms"), diagnostics.strokeMs);
    out->insert(QStringLiteral("glow_ms"), diagnostics.glowMs);
    out->insert(QStringLiteral("gpu_wait_ms"), diagnostics.gpuWaitMs);
    out->insert(QStringLiteral("readback_copy_ms"), diagnostics.readbackCopyMs);
    out->insert(
        QStringLiteral("resource_cache_enabled"),
        diagnostics.resourceCacheEnabled
    );
    out->insert(
        QStringLiteral("brush_cache_hits"),
        static_cast<qint64>(diagnostics.brushCacheHits)
    );
    out->insert(
        QStringLiteral("brush_cache_misses"),
        static_cast<qint64>(diagnostics.brushCacheMisses)
    );
    out->insert(
        QStringLiteral("brush_cache_evictions"),
        static_cast<qint64>(diagnostics.brushCacheEvictions)
    );
    out->insert(
        QStringLiteral("brush_cache_invalidations"),
        static_cast<qint64>(diagnostics.brushCacheInvalidations)
    );
    out->insert(
        QStringLiteral("brush_cache_size"),
        static_cast<qint64>(diagnostics.brushCacheSize)
    );
    out->insert(
        QStringLiteral("brush_cache_capacity"),
        static_cast<qint64>(diagnostics.brushCacheCapacity)
    );
    out->insert(
        QStringLiteral("realization_enabled"),
        diagnostics.realizationEnabled
    );
    out->insert(
        QStringLiteral("realization_supported"),
        diagnostics.realizationSupported
    );
    out->insert(
        QStringLiteral("realization_prewarm_complete"),
        diagnostics.realizationPrewarmComplete
    );
    out->insert(
        QStringLiteral("realization_count"),
        static_cast<qint64>(diagnostics.realizationCount)
    );
    out->insert(
        QStringLiteral("realization_capacity"),
        static_cast<qint64>(diagnostics.realizationCapacity)
    );
    out->insert(
        QStringLiteral("realization_prewarm_tasks"),
        static_cast<qint64>(diagnostics.realizationPrewarmTasks)
    );
    out->insert(
        QStringLiteral("realization_prewarm_skipped"),
        static_cast<qint64>(diagnostics.realizationPrewarmSkipped)
    );
    out->insert(
        QStringLiteral("realization_prewarm_ms"),
        diagnostics.realizationPrewarmMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_fill_tasks"),
        static_cast<qint64>(diagnostics.realizationPrewarmFillTasks)
    );
    out->insert(
        QStringLiteral("realization_prewarm_stroke_tasks"),
        static_cast<qint64>(diagnostics.realizationPrewarmStrokeTasks)
    );
    out->insert(
        QStringLiteral("realization_prewarm_context_ms"),
        diagnostics.realizationPrewarmContextMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_wait_ms"),
        diagnostics.realizationPrewarmWaitMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_fill_create_ms"),
        diagnostics.realizationPrewarmFillCreateMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_stroke_create_ms"),
        diagnostics.realizationPrewarmStrokeCreateMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_publish_ms"),
        diagnostics.realizationPrewarmPublishMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_create_p50_ms"),
        diagnostics.realizationPrewarmCreateP50Ms
    );
    out->insert(
        QStringLiteral("realization_prewarm_create_p95_ms"),
        diagnostics.realizationPrewarmCreateP95Ms
    );
    out->insert(
        QStringLiteral("realization_prewarm_create_max_ms"),
        diagnostics.realizationPrewarmCreateMaxMs
    );
    out->insert(
        QStringLiteral("glow_dirty_rect_enabled"),
        diagnostics.glowDirtyRectEnabled
    );
}

void appendGpuFrameDiagnostics(
    QJsonObject *out,
    const krok::subtitle::native::ProbeResult::FrameDiagnostics &diagnostics
) {
    out->insert(QStringLiteral("counters_enabled"), diagnostics.countersEnabled);
    out->insert(QStringLiteral("brush_created"), static_cast<qint64>(diagnostics.brushCreated));
    out->insert(QStringLiteral("geometry_created_stable"), static_cast<qint64>(diagnostics.geometryCreatedStable));
    out->insert(QStringLiteral("geometry_created_dynamic"), static_cast<qint64>(diagnostics.geometryCreatedDynamic));
    out->insert(QStringLiteral("realization_hit"), static_cast<qint64>(diagnostics.realizationHit));
    out->insert(QStringLiteral("realization_miss"), static_cast<qint64>(diagnostics.realizationMiss));
    out->insert(QStringLiteral("stroke_draw"), static_cast<qint64>(diagnostics.strokeDraw));
    out->insert(QStringLiteral("stroke2_draw"), static_cast<qint64>(diagnostics.stroke2Draw));
    out->insert(QStringLiteral("glow_source_area_px"), static_cast<qint64>(diagnostics.glowSourceAreaPx));
    out->insert(QStringLiteral("layer_push"), static_cast<qint64>(diagnostics.layerPush));
    out->insert(QStringLiteral("animation_layout_ms"), diagnostics.animationLayoutMs);
    out->insert(QStringLiteral("geometry_ms"), diagnostics.geometryMs);
    out->insert(QStringLiteral("stroke_ms"), diagnostics.strokeMs);
    out->insert(QStringLiteral("glow_ms"), diagnostics.glowMs);
    out->insert(QStringLiteral("end_draw_wait_ms"), diagnostics.endDrawWaitMs);
    out->insert(QStringLiteral("end_draw_glow_source_ms"), diagnostics.endDrawGlowSourceMs);
    out->insert(QStringLiteral("end_draw_ruby_glow_source_ms"), diagnostics.endDrawRubyGlowSourceMs);
    out->insert(QStringLiteral("end_draw_inline_glow_source_ms"), diagnostics.endDrawInlineGlowSourceMs);
    out->insert(QStringLiteral("end_draw_frame_layers_ms"), diagnostics.endDrawFrameLayersMs);
    out->insert(QStringLiteral("end_draw_empty_frame_ms"), diagnostics.endDrawEmptyFrameMs);
    out->insert(QStringLiteral("end_draw_count"), static_cast<qint64>(diagnostics.endDrawCount));
    out->insert(QStringLiteral("end_draw_glow_source_count"), static_cast<qint64>(diagnostics.endDrawGlowSourceCount));
    out->insert(QStringLiteral("end_draw_ruby_glow_source_count"), static_cast<qint64>(diagnostics.endDrawRubyGlowSourceCount));
    out->insert(QStringLiteral("end_draw_inline_glow_source_count"), static_cast<qint64>(diagnostics.endDrawInlineGlowSourceCount));
    out->insert(QStringLiteral("end_draw_frame_layers_count"), static_cast<qint64>(diagnostics.endDrawFrameLayersCount));
    out->insert(QStringLiteral("end_draw_empty_frame_count"), static_cast<qint64>(diagnostics.endDrawEmptyFrameCount));
    out->insert(QStringLiteral("gpu_wait_ms"), diagnostics.gpuWaitMs);
    out->insert(QStringLiteral("readback_copy_ms"), diagnostics.readbackCopyMs);
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
        appendSharedRingMetadata(out, ring, slotIndex);
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
            runtime.jobs.requestShutdown();
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

    runtime.jobs.requestShutdown();
    joinRenderJobs(&runtime);
    return 0;
}
