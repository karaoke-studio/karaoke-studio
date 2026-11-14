#include "gpu/vulkan_borrowed.h"
#include "gpu/context_options.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkSurface.h"
#include "include/gpu/GpuTypes.h"
#include "include/gpu/MutableTextureState.h"
#include "include/gpu/ganesh/GrBackendSurface.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include "include/gpu/ganesh/SkSurfaceGanesh.h"
#include "include/gpu/ganesh/vk/GrVkBackendSurface.h"
#include "include/gpu/ganesh/vk/GrVkDirectContext.h"
#include "include/gpu/ganesh/vk/GrVkTypes.h"
#include "include/gpu/vk/VulkanBackendContext.h"
#include "include/gpu/vk/VulkanMutableTextureState.h"
#include <stdexcept>

namespace subtitle::skia {
sk_sp<GrDirectContext> makeBorrowedVulkanContext(const VulkanBorrowedDevice& borrowed) {
    if (!borrowed.instance || !borrowed.physicalDevice || !borrowed.device || !borrowed.queue ||
        !borrowed.getInstanceProcAddr)
        throw std::runtime_error("Borrowed Vulkan device is incomplete");
    const auto getInstanceProc = borrowed.getInstanceProcAddr;
    const auto getDeviceProc = reinterpret_cast<PFN_vkGetDeviceProcAddr>(
        getInstanceProc(borrowed.instance, "vkGetDeviceProcAddr"));
    if (!getDeviceProc) throw std::runtime_error("vkGetDeviceProcAddr unavailable on borrowed instance");
    skgpu::VulkanBackendContext backend;
    backend.fInstance = borrowed.instance;
    backend.fPhysicalDevice = borrowed.physicalDevice;
    backend.fDevice = borrowed.device;
    backend.fQueue = borrowed.queue;
    backend.fGraphicsQueueIndex = borrowed.queueFamilyIndex;
    backend.fMaxAPIVersion = borrowed.apiVersion;
    // fDeviceFeatures/fVkExtensions stay null: "no optional features or extensions enabled".
    backend.fGetProc = [getInstanceProc, getDeviceProc](const char* name, VkInstance instance, VkDevice device) {
        return device ? getDeviceProc(device, name) : getInstanceProc(instance, name);
    };
    auto context = GrDirectContexts::MakeVulkan(backend, subtitleContextOptions());
    if (!context) throw std::runtime_error("Skia Ganesh could not use the borrowed Vulkan device");
    return context;
}

sk_sp<SkSurface> wrapVulkanImage(GrDirectContext& context, const VulkanImageTarget& target) {
    GrVkImageInfo info;
    info.fImage = target.image;
    info.fImageTiling = VK_IMAGE_TILING_OPTIMAL;
    info.fImageLayout = target.layout;
    info.fFormat = target.format;
    info.fImageUsageFlags = target.usage;
    info.fSampleCount = 1;
    info.fLevelCount = 1;
    info.fCurrentQueueFamily = target.queueFamilyIndex;
    const auto renderTarget = GrBackendRenderTargets::MakeVk(target.width, target.height, info);
    const SkColorType colorType =
        target.format == VK_FORMAT_B8G8R8A8_UNORM ? kBGRA_8888_SkColorType : kRGBA_8888_SkColorType;
    auto surface = SkSurfaces::WrapBackendRenderTarget(&context, renderTarget, kTopLeft_GrSurfaceOrigin,
                                                       colorType, SkColorSpace::MakeSRGB(), nullptr);
    if (!surface) throw std::runtime_error("Skia could not wrap the borrowed Vulkan image");
    return surface;
}

void submitForExternalUse(GrDirectContext& context, SkSurface& surface, VkImageLayout finalLayout,
                          uint32_t queueFamilyIndex) {
    const auto state = skgpu::MutableTextureStates::MakeVulkan(finalLayout, queueFamilyIndex);
    context.flush(&surface, GrFlushInfo(), &state);
    if (!context.submit(GrSyncCpu::kNo) || context.abandoned())
        throw std::runtime_error("Skia could not submit to the borrowed Vulkan queue");
}
}
