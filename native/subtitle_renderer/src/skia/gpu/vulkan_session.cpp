#include "gpu/gpu_session.h"
#include "gpu/vulkan_loader.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include "include/gpu/ganesh/vk/GrVkDirectContext.h"
#include "include/gpu/vk/VulkanBackendContext.h"
#include <algorithm>
#include <stdexcept>
#include <vector>

namespace subtitle::skia {
namespace {
void check(VkResult result, const char* operation) {
    if (result != VK_SUCCESS) throw std::runtime_error(std::string(operation) +
        " failed: VkResult=" + std::to_string(result));
}
std::string version(uint32_t value) {
    return std::to_string(VK_VERSION_MAJOR(value)) + "." + std::to_string(VK_VERSION_MINOR(value)) +
        "." + std::to_string(VK_VERSION_PATCH(value));
}
class VulkanSession final : public GpuSession {
public:
    ~VulkanSession() override {
        context_.reset();
        if (device_) {
            auto idle = reinterpret_cast<PFN_vkDeviceWaitIdle>(getDeviceProcAddr_(device_, "vkDeviceWaitIdle"));
            if (idle) idle(device_);
            auto destroy = reinterpret_cast<PFN_vkDestroyDevice>(getDeviceProcAddr_(device_, "vkDestroyDevice"));
            if (destroy) destroy(device_, nullptr);
        }
        if (instance_) {
            auto destroy = reinterpret_cast<PFN_vkDestroyInstance>(loader_.getInstanceProcAddr(instance_, "vkDestroyInstance"));
            if (destroy) destroy(instance_, nullptr);
        }
    }
    void initialize(bool requireHardware);
private:
    template<class Function> Function instanceProc(const char* name) const {
        auto fn = reinterpret_cast<Function>(loader_.getInstanceProcAddr(instance_, name));
        if (!fn) throw std::runtime_error(std::string("Missing Vulkan function: ") + name);
        return fn;
    }
    template<class Function> Function deviceProc(const char* name) const {
        auto fn = reinterpret_cast<Function>(getDeviceProcAddr_(device_, name));
        if (!fn) throw std::runtime_error(std::string("Missing Vulkan device function: ") + name);
        return fn;
    }
    VulkanLoader loader_;
    VkInstance instance_ = VK_NULL_HANDLE;
    VkPhysicalDevice physical_ = VK_NULL_HANDLE;
    VkDevice device_ = VK_NULL_HANDLE;
    PFN_vkGetDeviceProcAddr getDeviceProcAddr_ = nullptr;
    std::string unresolved_;
};

void VulkanSession::initialize(bool requireHardware) {
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.pApplicationName = "Lin-K Skia renderer";
    app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo instanceInfo{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    instanceInfo.pApplicationInfo = &app;
    check(instanceProc<PFN_vkCreateInstance>("vkCreateInstance")(&instanceInfo, nullptr, &instance_),
          "vkCreateInstance");
    auto enumerate = instanceProc<PFN_vkEnumeratePhysicalDevices>("vkEnumeratePhysicalDevices");
    uint32_t count = 0;
    check(enumerate(instance_, &count, nullptr), "vkEnumeratePhysicalDevices");
    std::vector<VkPhysicalDevice> devices(count);
    check(enumerate(instance_, &count, devices.data()), "vkEnumeratePhysicalDevices");
    devices.resize(count);
    int bestScore = -1;
    uint32_t queueIndex = 0;
    VkPhysicalDeviceProperties selected{};
    for (auto candidate : devices) {
        VkPhysicalDeviceProperties properties{};
        instanceProc<PFN_vkGetPhysicalDeviceProperties>("vkGetPhysicalDeviceProperties")(candidate, &properties);
        const bool hardware = properties.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU ||
                              properties.deviceType == VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU;
        if ((requireHardware && !hardware) || properties.apiVersion < VK_API_VERSION_1_1) continue;
        uint32_t queues = 0;
        auto getQueues = instanceProc<PFN_vkGetPhysicalDeviceQueueFamilyProperties>(
            "vkGetPhysicalDeviceQueueFamilyProperties");
        getQueues(candidate, &queues, nullptr);
        std::vector<VkQueueFamilyProperties> families(queues);
        getQueues(candidate, &queues, families.data());
        for (uint32_t index = 0; index < queues; ++index) {
            if (!families[index].queueCount || !(families[index].queueFlags & VK_QUEUE_GRAPHICS_BIT)) continue;
            int score = properties.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU ? 3 : hardware ? 2 : 1;
            if (score > bestScore) {
                bestScore = score;
                physical_ = candidate;
                selected = properties;
                queueIndex = index;
            }
        }
    }
    if (!physical_) throw std::runtime_error(requireHardware ?
        "No hardware Vulkan 1.1 graphics device (software/virtual devices rejected)" :
        "No Vulkan 1.1 graphics device");
    info_ = {"vulkan", selected.deviceName, "vendor=" + std::to_string(selected.vendorID) +
        ";device=" + std::to_string(selected.deviceID) + ";driverVersion=" +
        std::to_string(selected.driverVersion), version(selected.apiVersion),
        selected.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU ? "discrete" :
        selected.deviceType == VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU ? "integrated" :
        selected.deviceType == VK_PHYSICAL_DEVICE_TYPE_CPU ? "cpu" : "virtual_or_other", bestScore >= 2};
    VkPhysicalDeviceFeatures features{};
    instanceProc<PFN_vkGetPhysicalDeviceFeatures>("vkGetPhysicalDeviceFeatures")(physical_, &features);
    // Bounds-checked buffer access costs throughput on some drivers and Ganesh never relies on it.
    features.robustBufferAccess = VK_FALSE;
    float priority = 1;
    VkDeviceQueueCreateInfo queueInfo{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    queueInfo.queueFamilyIndex = queueIndex;
    queueInfo.queueCount = 1;
    queueInfo.pQueuePriorities = &priority;
    VkDeviceCreateInfo deviceInfo{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    deviceInfo.queueCreateInfoCount = 1;
    deviceInfo.pQueueCreateInfos = &queueInfo;
    deviceInfo.pEnabledFeatures = &features;
    // Resolve this before creating the device so partial initialization can always clean up.
    getDeviceProcAddr_ = instanceProc<PFN_vkGetDeviceProcAddr>("vkGetDeviceProcAddr");
    check(instanceProc<PFN_vkCreateDevice>("vkCreateDevice")(physical_, &deviceInfo, nullptr, &device_),
          "vkCreateDevice");
    VkQueue queue = VK_NULL_HANDLE;
    deviceProc<PFN_vkGetDeviceQueue>("vkGetDeviceQueue")(device_, queueIndex, 0, &queue);
    skgpu::VulkanBackendContext backend;
    backend.fInstance = instance_;
    backend.fPhysicalDevice = physical_;
    backend.fDevice = device_;
    backend.fQueue = queue;
    backend.fGraphicsQueueIndex = queueIndex;
    backend.fMaxAPIVersion = app.apiVersion;
    backend.fDeviceFeatures = &features;
    backend.fGetProc = [this](const char* name, VkInstance instance, VkDevice device) {
        auto proc = device ? getDeviceProcAddr_(device, name) : loader_.getInstanceProcAddr(instance, name);
        if (!proc) unresolved_ += std::string(name) + ";";
        return proc;
    };
    context_ = GrDirectContexts::MakeVulkan(backend);
    if (!context_) throw std::runtime_error("Skia Ganesh Vulkan initialization failed; unresolved=" + unresolved_);
}
}
std::unique_ptr<GpuSession> makeGpuSession(bool requireHardware) {
    auto session = std::make_unique<VulkanSession>();
    session->initialize(requireHardware);
    return session;
}
}
