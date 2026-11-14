#include "gpu/vulkan_loader.h"
#include <cstdlib>
#include <stdexcept>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace subtitle::skia {
VulkanLoader::VulkanLoader() {
    const char* overrideName = std::getenv("KROK_SKIA_VULKAN_LIBRARY");
#ifdef _WIN32
    // Default loader search is restricted to System32. Explicit override is for diagnostics.
    library_ = overrideName ? LoadLibraryExA(overrideName, nullptr, LOAD_LIBRARY_SEARCH_DEFAULT_DIRS)
                            : LoadLibraryExW(L"vulkan-1.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (library_) getInstanceProcAddr = reinterpret_cast<PFN_vkGetInstanceProcAddr>(
        GetProcAddress(static_cast<HMODULE>(library_), "vkGetInstanceProcAddr"));
#else
    library_ = dlopen(overrideName ? overrideName : "libvulkan.so.1", RTLD_NOW | RTLD_LOCAL);
    if (library_) getInstanceProcAddr = reinterpret_cast<PFN_vkGetInstanceProcAddr>(
        dlsym(library_, "vkGetInstanceProcAddr"));
#endif
    if (!getInstanceProcAddr) {
        if (library_) {
#ifdef _WIN32
            FreeLibrary(static_cast<HMODULE>(library_));
#else
            dlclose(library_);
#endif
            library_ = nullptr;
        }
        throw std::runtime_error("Vulkan loader unavailable; GPU initialization failed");
    }
}
VulkanLoader::~VulkanLoader() {
#ifdef _WIN32
    if (library_) FreeLibrary(static_cast<HMODULE>(library_));
#else
    if (library_) dlclose(library_);
#endif
}
}
