#pragma once
#include "include/core/SkRefCnt.h"
#include <vulkan/vulkan.h>
#include <cstdint>

class GrDirectContext;
class SkSurface;

namespace subtitle::skia {
// A Vulkan device owned by someone else (the Qt Quick scene graph). The owner keeps instance,
// device and queue alive for the context's whole life, and every call happens on the owner's
// submitting thread, so Skia never touches the queue concurrently with the owner.
struct VulkanBorrowedDevice {
    VkInstance instance = VK_NULL_HANDLE;
    VkPhysicalDevice physicalDevice = VK_NULL_HANDLE;
    VkDevice device = VK_NULL_HANDLE;
    VkQueue queue = VK_NULL_HANDLE;
    uint32_t queueFamilyIndex = 0;
    // Highest API version both the instance and the physical device allow.
    uint32_t apiVersion = VK_API_VERSION_1_0;
    PFN_vkGetInstanceProcAddr getInstanceProcAddr = nullptr;
};

// Ganesh context on a borrowed device. The owner does not publish which optional features and
// extensions it enabled, so none are declared: Skia then uses core Vulkan only, which every
// device the owner created must support. Destroy it before the owner destroys the device.
sk_sp<GrDirectContext> makeBorrowedVulkanContext(const VulkanBorrowedDevice& device);

// An image the owner allocated; Skia draws into it but never frees it.
struct VulkanImageTarget {
    VkImage image = VK_NULL_HANDLE;
    VkImageLayout layout = VK_IMAGE_LAYOUT_UNDEFINED;  // the owner's current layout
    VkFormat format = VK_FORMAT_R8G8B8A8_UNORM;
    VkImageUsageFlags usage = 0;
    int width = 0;
    int height = 0;
    uint32_t queueFamilyIndex = 0;
};
sk_sp<SkSurface> wrapVulkanImage(GrDirectContext& context, const VulkanImageTarget& target);

// Submits everything drawn into `surface` to the shared queue now, ending with the image in
// `finalLayout` for the owner's queue family. Work the owner submits later on the same queue
// is ordered after it.
void submitForExternalUse(GrDirectContext& context, SkSurface& surface, VkImageLayout finalLayout,
                          uint32_t queueFamilyIndex);
}
