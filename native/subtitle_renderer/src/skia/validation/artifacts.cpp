#include "validation/artifacts.h"
#include "core/validation_scene.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkData.h"
#include "include/core/SkPixmap.h"
#include "include/encode/SkPngEncoder.h"
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace subtitle::skia::validation {
namespace {
SkImageInfo info() {
    return SkImageInfo::Make(ValidationScene::width, ValidationScene::height, kRGBA_8888_SkColorType,
                            kPremul_SkAlphaType, SkColorSpace::MakeSRGB());
}
std::string quote(const std::string& value) {
    std::ostringstream result;
    result << '"';
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') result << '\\' << c;
        else if (c < 32) result << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(c);
        else result << c;
    }
    result << '"';
    return result.str();
}
void write(const std::filesystem::path& path, const void* data, size_t size) {
    std::ofstream stream(path, std::ios::binary);
    stream.write(static_cast<const char*>(data), static_cast<std::streamsize>(size));
    if (!stream) throw std::runtime_error("Cannot write smoke artifact");
}
}
std::vector<unsigned char> readPixels(SkSurface& surface) {
    std::vector<unsigned char> rgba(info().computeMinByteSize());
    if (!surface.readPixels(info(), rgba.data(), info().minRowBytes(), 0, 0))
        throw std::runtime_error("Skia RGBA readback failed");
    return rgba;
}
void checkCapabilities(const std::vector<unsigned char>& rgba) {
    auto alpha = [&](int x, int y) { return rgba[(y * ValidationScene::width + x) * 4 + 3]; };
    if (alpha(0, 0) != 0) throw std::runtime_error("Transparent background failed");
    if (alpha(100, 50) < 150 || alpha(100, 50) > 155)
        throw std::runtime_error("RuntimeEffect/alpha failed");
    if (alpha(500, 200) < 126 || alpha(500, 200) > 130)
        throw std::runtime_error("PNG image/alpha failed");
    if (alpha(100, 235) < 175) throw std::runtime_error("Path drawing failed");
    if (!alpha(430, 100) || alpha(390, 100) < 155) throw std::runtime_error("Blur filter failed");
    int textPixels = 0;
    for (int y = 275; y < 322; ++y)
        for (int x = 24; x < 400; ++x) if (alpha(x, y)) ++textPixels;
    if (textPixels < 100) throw std::runtime_error("Font drawing failed");
    for (size_t i = 0; i < rgba.size(); i += 4)
        for (int c = 0; c < 3; ++c)
            if (rgba[i + c] > rgba[i + 3]) throw std::runtime_error("Invalid premultiplied RGBA");
}
void saveArtifacts(const std::filesystem::path& prefix, const std::vector<unsigned char>& rgba,
                   const Report& report, const std::string& fingerprint) {
    auto png = SkPngEncoder::Encode(SkPixmap(info(), rgba.data(), info().minRowBytes()), {});
    if (!png) throw std::runtime_error("Smoke output PNG encoding failed");
    write(prefix.native() + std::filesystem::path(".png").native(), png->data(), png->size());
    write(prefix.native() + std::filesystem::path(".rgba").native(), rgba.data(), rgba.size());
    std::ostringstream json;
    json << "{\n  \"status\": \"passed\",\n  \"api\": " << quote(report.api)
         << ",\n  \"device\": " << quote(report.device)
         << ",\n  \"driver\": " << quote(report.driver)
         << ",\n  \"api_version\": " << quote(report.apiVersion)
         << ",\n  \"device_type\": " << quote(report.deviceType)
         << ",\n  \"hardware\": " << (report.hardware ? "true" : "false")
         << ",\n  \"frames\": " << report.frames << ",\n  \"elapsed_ms\": " << report.elapsedMs
         << ",\n  \"width\": " << ValidationScene::width << ",\n  \"height\": " << ValidationScene::height
         << ",\n  \"format\": \"RGBA8888_premul_sRGB\",\n  \"fingerprint\": " << quote(fingerprint)
         << ",\n  \"checks\": [\"transparent\", \"path\", \"font\", \"png\", \"blur\", \"runtime_effect\", \"repeat_exact\"]\n}\n";
    auto text = json.str();
    write(prefix.native() + std::filesystem::path(".json").native(), text.data(), text.size());
}
}
