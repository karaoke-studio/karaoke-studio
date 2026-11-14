#include "skia_subtitle_item.h"
#include "core/motion_probe.h"
#include <QtCore/QElapsedTimer>
#include <QtCore/QLoggingCategory>
#include <QtCore/QMetaObject>
#include <rhi/qrhi.h>
#include <exception>

#ifndef Q_OS_MACOS
#include "gpu/vulkan_borrowed.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkSurface.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include <QtGui/QVulkanInstance>
#include <algorithm>
#endif

namespace krok::subtitle::preview {
namespace {
Q_LOGGING_CATEGORY(lcPreview, "krok.subtitle.skia.preview")

// The one GPU API the shared Skia core drives on this platform. Platform branches belong in
// this adapter only, never in the core.
constexpr QRhi::Implementation requiredBackend() {
#ifdef Q_OS_MACOS
    return QRhi::Metal;
#else
    return QRhi::Vulkan;
#endif
}

#ifndef Q_OS_MACOS
// Translate Qt's scene graph device into the Qt-free borrowed-device description.
::subtitle::skia::VulkanBorrowedDevice borrowedDevice(QRhi& rhi) {
    const auto* handles = static_cast<const QRhiVulkanNativeHandles*>(rhi.nativeHandles());
    if (!handles || !handles->inst) throw std::runtime_error("Qt did not expose its Vulkan device");
    QVulkanInstance& instance = *handles->inst;
    ::subtitle::skia::VulkanBorrowedDevice device;
    device.instance = instance.vkInstance();
    device.physicalDevice = handles->physDev;
    device.device = handles->dev;
    device.queue = handles->gfxQueue;
    device.queueFamilyIndex = handles->gfxQueueFamilyIdx;
    device.getInstanceProcAddr = reinterpret_cast<PFN_vkGetInstanceProcAddr>(
        instance.getInstanceProcAddr("vkGetInstanceProcAddr"));
    // Skia may use at most what both the instance and the physical device allow.
    const QVersionNumber requested = instance.apiVersion();
    const uint32_t instanceVersion = requested.isNull()
        ? VK_API_VERSION_1_0
        : VK_MAKE_API_VERSION(0, requested.majorVersion(), requested.minorVersion(), 0);
    VkPhysicalDeviceProperties properties{};
    reinterpret_cast<PFN_vkGetPhysicalDeviceProperties>(
        instance.getInstanceProcAddr("vkGetPhysicalDeviceProperties"))(device.physicalDevice, &properties);
    device.apiVersion = std::min(instanceVersion, properties.apiVersion);
    return device;
}
#endif

class SkiaSubtitleRenderer final : public QQuickRhiItemRenderer {
public:
    ~SkiaSubtitleRenderer() override {
        // Destroyed on the render thread before Qt releases its device; drop Skia's use first.
#ifndef Q_OS_MACOS
        if (context_) context_->flushAndSubmit(GrSyncCpu::kYes);
        context_.reset();
#endif
    }
protected:
    void initialize(QRhiCommandBuffer*) override {
        // Runs again when the item's texture is recreated (resize, new window): re-report then.
        backend_ = QString::fromLatin1(rhi()->backendName());
        accepted_ = rhi()->backend() == requiredBackend();
        reported_ = false;
        if (!accepted_) {
            qCWarning(lcPreview) << "Rejecting scene graph backend" << backend_
                                 << "- Skia preview requires" << requiredName();
            return;
        }
        if (!clock_.isValid()) clock_.start();
#ifndef Q_OS_MACOS
        if (!context_ || deviceRhi_ != rhi()) {
            context_.reset();
            deviceRhi_ = rhi();
            try {
                device_ = borrowedDevice(*rhi());
                context_ = ::subtitle::skia::makeBorrowedVulkanContext(device_);
            } catch (const std::exception& error) {
                qCWarning(lcPreview) << "Skia could not share Qt's Vulkan device:" << error.what();
            }
        }
#endif
    }
    void synchronize(QQuickRhiItem* item) override {
        // synchronize() precedes initialize() within a frame, so a fresh result is published
        // on the following frame, which render() requests below.
        if (!backend_.isEmpty() && !reported_) {
            static_cast<SkiaSubtitleItem*>(item)->publishGraphicsBackend(backend_, accepted_);
            reported_ = true;
        }
    }
    void render(QRhiCommandBuffer* cb) override {
        if (!drawWithSkia()) {
            // Transparent texture; nothing else is drawn on an unaccepted or failed backend.
            cb->beginPass(renderTarget(), Qt::transparent, {1.0f, 0});
            cb->endPass();
        }
        // Keep requesting frames: the probe moves with time (and pending reports need a frame).
        update();
    }
private:
    bool drawWithSkia() {
#ifdef Q_OS_MACOS
        return false;  // Metal hand-off is the next P03.3 step.
#else
        if (!accepted_ || !context_) return false;
        QRhiTexture* texture = colorTexture();
        const QRhiTexture::NativeTexture native = texture->nativeTexture();
        const QSize size = texture->pixelSize();
        ::subtitle::skia::VulkanImageTarget target;
        target.image = reinterpret_cast<VkImage>(native.object);
        target.layout = static_cast<VkImageLayout>(native.layout);
        target.format = texture->format() == QRhiTexture::BGRA8 ? VK_FORMAT_B8G8R8A8_UNORM
                                                                : VK_FORMAT_R8G8B8A8_UNORM;
        // QRhi creates render-target textures with exactly these usages.
        target.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                       VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
        target.width = size.width();
        target.height = size.height();
        target.queueFamilyIndex = device_.queueFamilyIndex;
        try {
            auto surface = ::subtitle::skia::wrapVulkanImage(*context_, target);
            ::subtitle::skia::drawMotionProbe(*surface->getCanvas(), size.width(), size.height(),
                                              clock_.elapsed() / 1000.0);
            // Skia submits now, before Qt submits this frame's commands that sample the texture.
            ::subtitle::skia::submitForExternalUse(*context_, *surface,
                                                   VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                                                   device_.queueFamilyIndex);
            texture->setNativeLayout(VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
            return true;
        } catch (const std::exception& error) {
            qCWarning(lcPreview) << "Skia preview frame failed:" << error.what();
            context_.reset();
            return false;
        }
#endif
    }
    static const char* requiredName() { return requiredBackend() == QRhi::Metal ? "Metal" : "Vulkan"; }
    QString backend_;
    bool accepted_ = false;
    bool reported_ = false;
    QElapsedTimer clock_;
#ifndef Q_OS_MACOS
    QRhi* deviceRhi_ = nullptr;
    ::subtitle::skia::VulkanBorrowedDevice device_;
    sk_sp<GrDirectContext> context_;
#endif
};
}

SkiaSubtitleItem::SkiaSubtitleItem(QQuickItem* parent) : QQuickRhiItem(parent) {
    setAlphaBlending(true);
}

void SkiaSubtitleItem::publishGraphicsBackend(const QString& backend, bool accepted) {
    if (backend == backend_ && accepted == accepted_) return;
    backend_ = backend;
    accepted_ = accepted;
    // Never emit on the render thread: QML bindings and Python slots belong to the GUI thread.
    QMetaObject::invokeMethod(this, &SkiaSubtitleItem::graphicsBackendChanged, Qt::QueuedConnection);
}

QQuickRhiItemRenderer* SkiaSubtitleItem::createRenderer() {
    return new SkiaSubtitleRenderer;
}
}
