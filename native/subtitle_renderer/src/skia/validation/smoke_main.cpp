#include "core/validation_scene.h"
#include "validation/artifacts.h"
#ifdef KROK_SKIA_GPU_SMOKE
#include "gpu/gpu_session.h"
#else
#include "raster/raster_surface.h"
#endif
#include <chrono>
#include <iostream>
#include <stdexcept>

namespace {
int smoke(const std::filesystem::path& font, const std::filesystem::path& prefix,
          bool requireHardware, int frames) {
    using namespace subtitle::skia;
    validation::Report report;
    const auto start = std::chrono::steady_clock::now();
    ValidationScene scene(font);
#ifdef KROK_SKIA_GPU_SMOKE
    auto session = makeGpuSession(requireHardware);
    const auto& device = session->info();
    report = {device.api, device.device, device.driver, device.apiVersion, device.type, device.hardware};
    auto surface = session->makeSurface(ValidationScene::width, ValidationScene::height);
#else
    if (requireHardware) throw std::runtime_error("Raster is CPU rendering; cannot require hardware GPU");
    report.api = "raster";
    report.device = "Skia CPU";
    report.deviceType = "cpu";
    auto surface = makeRasterSurface(ValidationScene::width, ValidationScene::height);
#endif
    std::vector<unsigned char> first;
    for (int i = 0; i < frames; ++i) {
        scene.draw(*surface->getCanvas());
#ifdef KROK_SKIA_GPU_SMOKE
        session->finish(*surface);
#endif
        auto rgba = validation::readPixels(*surface);
        validation::checkCapabilities(rgba);
        if (i && rgba != first) throw std::runtime_error("Repeated identical scene changed pixels");
        first = std::move(rgba);
    }
    report.frames = frames;
    report.elapsedMs = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
    validation::saveArtifacts(prefix, first, report, KROK_SKIA_FINGERPRINT);
    std::cout << report.api << ": " << report.device << " (" << report.deviceType << "), "
              << frames << " frames passed\n";
    return 0;
}
template<class Char> int entry(int argc, Char** argv) {
    try {
        if (argc < 3) throw std::runtime_error("Usage: smoke FONT OUTPUT_PREFIX [--require-hardware] [--frames N]");
        bool requireHardware = false;
        int frames = 3;
        for (int i = 3; i < argc; ++i) {
            const auto option = std::filesystem::path(argv[i]).string();
            if (option == "--require-hardware") requireHardware = true;
            else if (option == "--frames" && i + 1 < argc) frames = std::stoi(std::filesystem::path(argv[++i]).string());
            else throw std::runtime_error("Unknown/incomplete smoke option");
        }
        if (frames < 2) throw std::runtime_error("Smoke needs at least two frames for repeat verification");
        return smoke(argv[1], argv[2], requireHardware, frames);
    } catch (const std::exception& error) {
        std::cerr << "Skia smoke failed: " << error.what() << '\n';
        return 1;
    }
}
}
#ifdef _WIN32
int wmain(int argc, wchar_t** argv) { return entry(argc, argv); }
#else
int main(int argc, char** argv) { return entry(argc, argv); }
#endif
