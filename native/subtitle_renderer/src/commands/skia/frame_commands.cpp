#include "frame_commands.h"
#include "../../backends/skia/skia_backend.h"
#include "../../diagnostics/shared_frame_metadata_json.h"
#include "../../protocol/json_protocol.h"
#include "../../runtime/checksum.h"
#include <QtCore/QJsonArray>
#include <QtCore/QSharedMemory>
#include <QtCore/QNativeIpcKey>
#include <QtGui/QImage>
#include <cmath>
#include <limits>
#include <vector>

namespace krok::subtitle::native::commands {
void SkiaFrames::range(RenderBackend& backend, const QJsonObject& request, runtime::GenerationCancellation& cancellation, const QString& key) {
    const auto times = request.value("t_ms").toArray();
    const int generation = request.value("generation").toInt();
    const int slotCount = request.value("ring_slots").toInt();
    if (times.empty() || times.size() > 120 || slotCount < times.size() || slotCount > 120 || generation < 1)
        throw BackendError("Skia range requires 1..120 timestamps, unique generation and one retained slot per frame");
    if (key.isEmpty()) throw BackendError("Skia range requires an explicit shared-memory key");
    const auto format = request.value("pixel_format").toString("rgba8888");
    const bool bgra = format == "bgra8888_premultiplied";
    if (!bgra && format != "rgba8888") throw BackendError("Unsupported Skia transfer pixel format");
    std::vector<int> timestamps;
    timestamps.reserve(std::size_t(times.size()));
    for (const auto& value : times) {
        const double time = value.toDouble(std::numeric_limits<double>::quiet_NaN());
        if (!value.isDouble() || !std::isfinite(time) || std::floor(time) != time
            || time < -86400000 || time > 86400000)
            throw BackendError("Skia range requires integer timestamps within one day");
        timestamps.push_back(value.toInt());
    }
    std::vector<std::uint8_t> premultiplied;
    static_cast<SkiaBackend&>(backend).renderSequence(timestamps,
        [&] { return cancellation.cancelled(generation); },
        [&](std::size_t index, const SkiaBackend::SequenceFrame& frame) {
            const int i = int(index);
            const int tMs = timestamps[index];
            const auto ringBytes = (std::uint64_t(frame.width) * 4 * frame.height + 64) * slotCount;
            if (ringBytes > 256u * 1024 * 1024)
                throw BackendError("Skia 共享内存批次超过 256 MiB，请减少批次帧数");
            const std::uint8_t* pixels = frame.rgba;
            int stride = int(frame.stride);
            if (bgra) {
                premultiplied.resize(std::size_t(frame.width) * 4 * frame.height);
                for (int y = 0; y < frame.height; ++y) {
                    const auto* source = frame.rgba + frame.stride * std::size_t(y);
                    auto* row = premultiplied.data() + std::size_t(frame.width) * 4 * y;
                    for (int x = 0; x < frame.width; ++x) {
                        const auto* in = source + x * 4;
                        auto* out = row + x * 4;
                        const int alpha = in[3];
                        out[0] = std::uint8_t((int(in[2]) * alpha + 127) / 255);
                        out[1] = std::uint8_t((int(in[1]) * alpha + 127) / 255);
                        out[2] = std::uint8_t((int(in[0]) * alpha + 127) / 255);
                        out[3] = std::uint8_t(alpha);
                    }
                }
                pixels = premultiplied.data();
                stride = frame.width * 4;
            }
            QString error;
            if (i == 0) posix_.create(key, int(ringBytes));
            if (i == 0 && !ring_.ensure(key, slotCount, frame.width, frame.height, &error))
                throw BackendError(error.toStdString());
            cancellation.publishIfActive(generation, [&] {
                runtime::SharedFrameRing metadata;
                if (!ring_.writeRgba(pixels, frame.width, frame.height, stride,
                                     generation, i, tMs, i, &metadata, bgra ? 2 : 1, format))
                    throw BackendError("Skia shared-memory publication failed");
                auto out = protocol::response(true, "frame_ready");
                out.insert("generation", generation);
                out.insert("frame_index", i);
                out.insert("t_ms", tMs);
                out.insert("origin", "top_left");
                out.insert("alpha_type", bgra ? "premultiplied" : "straight");
                out.insert("color_space", "srgb");
                out.insert("render_ms", frame.renderMs);
                out.insert("readback_ms", frame.readbackMs);
                diagnostics::appendSharedFrameMetadata(out, metadata, i);
                protocol::writeJson(out);
            });
            protocol::emitProgress("skia_render_range", i + 1, times.size());
        });
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
