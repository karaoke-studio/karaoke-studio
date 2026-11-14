#pragma once
#include <vulkan/vulkan.h>

namespace subtitle::skia {
class VulkanLoader {
public:
    VulkanLoader();
    ~VulkanLoader();
    VulkanLoader(const VulkanLoader&) = delete;
    VulkanLoader& operator=(const VulkanLoader&) = delete;
    PFN_vkGetInstanceProcAddr getInstanceProcAddr = nullptr;
private:
    void* library_ = nullptr;
};
}
