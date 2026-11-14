#include "skia_subtitle_item.h"
#include "core/motion_probe.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkSurface.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include <QtCore/QElapsedTimer>
#include <QtCore/QLoggingCategory>
#include <QtCore/QMetaObject>
#include <rhi/qrhi.h>
#include <exception>
#include <stdexcept>

#ifdef Q_OS_MACOS
#include "gpu/metal_borrowed.h"
#else
#include "gpu/vulkan_borrowed.h"
#include <QtGui/QVulkanInstance>
#include <algorithm>
#endif

namespace krok::subtitle::preview {
namespace {
Q_LOGGING_CATEGORY(lcPreview, "krok.subtitle.skia.preview")

// Platform branches live only in this adapter: which API is accepted, how Qt's device is
// borrowed, and how one frame's texture is wrapped and handed back. Drawing is shared.
#ifdef Q_OS_MACOS
constexpr QRhi::Implementation kRequiredBackend = QRhi::Metal;
constexpr const char* kRequiredName = "Metal";

struct PlatformBridge {
    sk_sp<GrDirectContext> connect(QRhi& rhi) {
        const auto* handles = static_cast<const QRhiMetalNativeHandles*>(rhi.nativeHandles());
        if (!handles || !handles->dev || !handles->cmdQueue)
            throw std::runtime_error("Qt did not expose its Metal device");
        ::subtitle::skia::MetalBorrowedDevice device;
        device.device = handles->dev;
        device.queue = handles->cmdQueue;
        return ::subtitle::skia::makeBorrowedMetalContext(device);
    }
    sk_sp<SkSurface> wrap(GrDirectContext& context, QRhiTexture& texture) {
        ::subtitle::skia::MetalTextureTarget target;
        target.texture = reinterpret_cast<const void*>(texture.nativeTexture().object);
        target.width = texture.pixelSize().width();
        target.height = texture.pixelSize().height();
        target.bgra = texture.format() == QRhiTexture::BGRA8;
        return ::subtitle::skia::wrapMetalTexture(context, target);
    }
    void submit(GrDirectContext& context, SkSurface& surface, QRhiTexture&) {
        // Qt commits this frame's command buffer only in endFrame(), after this commit.
        ::subtitle::skia::submitMetal(context, surface);
    }
};
#else
constexpr QRhi::Implementation kRequiredBackend = QRhi::Vulkan;
constexpr const char* kRequiredName = "Vulkan";

struct PlatformBridge {
    ::subtitle::skia::VulkanBorrowedDevice device;

    sk_sp<GrDirectContext> connect(QRhi& rhi) {
        const auto* handles = static_cast<const QRhiVulkanNativeHandles*>(rhi.nativeHandles());
        if (!handles || !handles->inst) throw std::runtime_error("Qt did not expose its Vulkan device");
        QVulkanInstance& instance = *handles->inst;
        device = {};
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
        return ::subtitle::skia::makeBorrowedVulkanContext(device);
    }
    sk_sp<SkSurface> wrap(GrDirectContext& context, QRhiTexture& texture) {
        const QRhiTexture::NativeTexture native = texture.nativeTexture();
        ::subtitle::skia::VulkanImageTarget target;
        target.image = reinterpret_cast<VkImage>(native.object);
        target.layout = static_cast<VkImageLayout>(native.layout);
        target.format = texture.format() == QRhiTexture::BGRA8 ? VK_FORMAT_B8G8R8A8_UNORM
                                                               : VK_FORMAT_R8G8B8A8_UNORM;
        // QRhi creates render-target textures with exactly these usages.
        target.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                       VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
        target.width = texture.pixelSize().width();
        target.height = texture.pixelSize().height();
        target.queueFamilyIndex = device.queueFamilyIndex;
        return ::subtitle::skia::wrapVulkanImage(context, target);
    }
    void submit(GrDirectContext& context, SkSurface& surface, QRhiTexture& texture) {
        // Skia submits now, before Qt submits this frame's commands that sample the texture.
        ::subtitle::skia::submitForExternalUse(context, surface, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                                               device.queueFamilyIndex);
        texture.setNativeLayout(VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
    }
};
#endif

class SkiaSubtitleRenderer final : public QQuickRhiItemRenderer {
public:
    ~SkiaSubtitleRenderer() override {
        // Destroyed on the render thread before Qt releases its device; drop Skia's use first.
        if (context_) context_->flushAndSubmit(GrSyncCpu::kYes);
        context_.reset();
    }
protected:
    void initialize(QRhiCommandBuffer*) override {
        // Runs again when the item's texture is recreated (resize, new window): re-report then.
        backend_ = QString::fromLatin1(rhi()->backendName());
        accepted_ = rhi()->backend() == kRequiredBackend;
        reported_ = false;
        if (!accepted_) {
            qCWarning(lcPreview) << "Rejecting scene graph backend" << backend_
                                 << "- Skia preview requires" << kRequiredName;
            return;
        }
        if (!clock_.isValid()) clock_.start();
        if (!context_ || deviceRhi_ != rhi()) {
            context_.reset();
            deviceRhi_ = rhi();
            try {
                context_ = bridge_.connect(*rhi());
            } catch (const std::exception& error) {
                qCWarning(lcPreview) << "Skia could not share Qt's" << kRequiredName << "device:" << error.what();
            }
        }
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
        if (!accepted_ || !context_) return false;
        QRhiTexture& texture = *colorTexture();
        try {
            auto surface = bridge_.wrap(*context_, texture);
            ::subtitle::skia::drawMotionProbe(*surface->getCanvas(), texture.pixelSize().width(),
                                              texture.pixelSize().height(), clock_.elapsed() / 1000.0);
            bridge_.submit(*context_, *surface, texture);
            return true;
        } catch (const std::exception& error) {
            qCWarning(lcPreview) << "Skia preview frame failed:" << error.what();
            context_.reset();
            return false;
        }
    }
    QString backend_;
    bool accepted_ = false;
    bool reported_ = false;
    QElapsedTimer clock_;
    QRhi* deviceRhi_ = nullptr;
    PlatformBridge bridge_;
    sk_sp<GrDirectContext> context_;
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
