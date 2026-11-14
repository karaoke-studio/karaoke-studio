#include "frame_commands.h"
#include "../../diagnostics/shared_frame_metadata_json.h"
#include "../../protocol/json_protocol.h"
#include "../../runtime/checksum.h"
#include <QtCore/QJsonArray>
#include <QtCore/QSharedMemory>
#include <QtCore/QNativeIpcKey>
#include <QtGui/QImage>
#include <cmath>
#include <limits>

namespace krok::subtitle::native::commands {
void SkiaFrames::range(RenderBackend& backend, const QJsonObject& request, runtime::GenerationCancellation& cancellation) {
    const auto times = request.value("t_ms").toArray();
    const int generation = request.value("generation").toInt();
    const int slotCount = request.value("ring_slots").toInt();
    if (times.empty() || times.size() > 120 || slotCount < times.size() || slotCount > 120 || generation < 1)
        throw BackendError("Skia range requires 1..120 timestamps, unique generation and one retained slot per frame");
    auto key = request.value("shm_key").toString();
    if (key.isEmpty()) throw BackendError("Skia range requires an explicit shared-memory key");
#ifdef Q_OS_UNIX
    // macOS's legacy System V limits can reject even one full-HD frame. This
    // belongs to the transport adapter, never to subtitle geometry or drawing.
    key = QSharedMemory::platformSafeKey(key, QNativeIpcKey::Type::PosixRealtime).toString();
#endif
    const auto format = request.value("pixel_format").toString("rgba8888");
    const bool bgra = format == "bgra8888_premultiplied";
    if (!bgra && format != "rgba8888") throw BackendError("Unsupported Skia transfer pixel format");
    for (qsizetype i = 0; i < times.size(); ++i) {
        if (cancellation.cancelled(generation)) return;
        const double time = times[i].toDouble(std::numeric_limits<double>::quiet_NaN());
        if (!times[i].isDouble() || !std::isfinite(time) || std::floor(time) != time
            || time < -86400000 || time > 86400000)
            throw BackendError("Skia range requires integer timestamps within one day");
        const int tMs = times[i].toInt();
        auto result = backend.renderFrame(tMs);
        auto& pixels = result.surface;
        const auto ringBytes = (std::uint64_t(pixels.stride) * pixels.height + 64) * slotCount;
        if (ringBytes > 256u * 1024 * 1024)
            throw BackendError("Skia 共享内存批次超过 256 MiB，请减少批次帧数");
        if (bgra) {
            for (std::size_t offset = 0; offset < pixels.bytes.size(); offset += 4) {
                auto* p = pixels.bytes.data() + offset;
                const auto red = p[0];
                p[0] = std::uint8_t((int(p[2]) * p[3] + 127) / 255);
                p[1] = std::uint8_t((int(p[1]) * p[3] + 127) / 255);
                p[2] = std::uint8_t((int(red) * p[3] + 127) / 255);
            }
        }
        QString error;
        if (i == 0 && !ring_.ensure(key, slotCount, pixels.width, pixels.height, &error))
            throw BackendError(error.toStdString());
        cancellation.publishIfActive(generation, [&] {
            runtime::SharedFrameRing metadata;
            if (!ring_.writeRgba(pixels.bytes.data(), pixels.width, pixels.height, pixels.stride,
                                 generation, int(i), tMs, int(i), &metadata, bgra ? 2 : 1, format))
                throw BackendError("Skia shared-memory publication failed");
            auto out = protocol::response(true, "frame_ready");
            out.insert("generation", generation);
            out.insert("frame_index", int(i));
            out.insert("t_ms", tMs);
            out.insert("origin", "top_left");
            out.insert("alpha_type", bgra ? "premultiplied" : "straight");
            out.insert("color_space", "srgb");
            out.insert("render_ms", result.renderMs);
            out.insert("readback_ms", result.readbackMs);
            diagnostics::appendSharedFrameMetadata(out, metadata, int(i));
            protocol::writeJson(out);
        });
        protocol::emitProgress("skia_render_range", i + 1, times.size());
    }
}
QJsonObject SkiaFrames::frame(RenderBackend& backend, const QJsonObject& request) {
    const int tMs = request.value("t_ms").toInt();
    auto result = backend.renderFrame(tMs);
    const auto& surface = result.surface;
    auto out = protocol::response(true, request.value("cmd") == "render_frame_stats" ? "frame_stats" : "frame_ready");
    out.insert("t_ms", tMs);
    out.insert("width", surface.width);
    out.insert("height", surface.height);
    out.insert("render_ms", result.renderMs);
    out.insert("readback_ms", result.readbackMs);
    out.insert("checksum", QString::number(runtime::bytesChecksum(surface.bytes.data(), surface.bytes.size())));
    if (request.contains("output_path")) {
        const auto path = request.value("output_path").toString();
        QImage image(surface.bytes.data(), surface.width, surface.height, surface.stride, QImage::Format_RGBA8888);
        if (!image.save(path, "PNG")) throw BackendError("Skia PNG output failed");
        out.insert("output_path", path);
    }
    return out;
}
}
