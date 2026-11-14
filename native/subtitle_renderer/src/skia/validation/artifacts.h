#pragma once
#include "include/core/SkSurface.h"
#include <filesystem>
#include <string>
#include <vector>

namespace subtitle::skia::validation {
struct Report {
    std::string api, device, driver, apiVersion, deviceType;
    bool hardware = false;
    int frames = 0;
    double elapsedMs = 0;
};
std::vector<unsigned char> readPixels(SkSurface& surface);
void checkCapabilities(const std::vector<unsigned char>& rgba);
void saveArtifacts(const std::filesystem::path& prefix, const std::vector<unsigned char>& rgba,
                   const Report& report, const std::string& fingerprint);
}
