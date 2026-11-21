#include "skia_subtitle_item.h"
#include "core/motion_probe.h"
#include "core/prepared_scene.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkSurface.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include <QtCore/QElapsedTimer>
#include <QtCore/QCoreApplication>
#include <QtCore/QLoggingCategory>
#include <QtCore/QMetaObject>
#include <QtCore/QPointer>
#include <QtCore/QThread>
#include <QtConcurrent/QtConcurrentRun>
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
Q_LOGGING_CATEGORY(lcPreview, "krok.subtitle.skia.preview", QtWarningMsg)
Q_LOGGING_CATEGORY(lcPreviewResources, "krok.subtitle.skia.resources", QtWarningMsg)

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
    SkiaSubtitleRenderer() { trace("created"); }
    ~SkiaSubtitleRenderer() override {
        requireOwner();
        // Destroyed on the render thread before Qt releases its device; drop Skia's use first.
        if (context_) context_->flushAndSubmit(GrSyncCpu::kYes);
        context_.reset();
        trace("released");
    }
protected:
    void initialize(QRhiCommandBuffer*) override {
        requireOwner();
        const auto backend = QString::fromLatin1(rhi()->backendName());
        const auto driver = rhi()->driverInfo();
        const auto deviceName = driver.deviceName.toLower();
        const bool hardware = (driver.deviceType == QRhiDriverInfo::DiscreteDevice
            || driver.deviceType == QRhiDriverInfo::IntegratedDevice || driver.deviceType == QRhiDriverInfo::ExternalDevice)
            && !deviceName.contains("virtual") && !deviceName.contains("software");
        if (backend != backend_ || hardware != hardware_) reported_ = false;
        backend_ = backend;
        hardware_ = hardware;
        trace("initialized");
        qCDebug(lcPreview) << "texture-size" << colorTexture()->pixelSize();
        if (rhi()->backend() != kRequiredBackend) {
            context_.reset();
            qCWarning(lcPreview) << "Rejecting scene graph backend" << backend_
                                 << "- Skia preview requires" << kRequiredName;
            return;
        }
        if (!clock_.isValid()) clock_.start();
        if (deviceRhi_ != rhi()) {
            context_.reset();
            deviceRhi_ = rhi();
            reported_ = false;
            nextResourceSampleMs_ = 0;
            try {
                context_ = bridge_.connect(*rhi());
            } catch (const std::exception& error) {
                qCWarning(lcPreview) << "Skia could not share Qt's" << kRequiredName << "device:" << error.what();
            }
        }
    }
    void synchronize(QQuickRhiItem* item) override {
        requireOwner();
        const auto preview = static_cast<SkiaSubtitleItem*>(item);
        item_ = preview;
        motionProbe_ = preview->usesMotionProbe();
        frame_ = preview->frameRequest();
        // synchronize() precedes initialize() within a frame, so a fresh result is published
        // on the following frame, which render() requests below.
        if (!backend_.isEmpty() && !reported_) {
            preview->publishGraphicsBackend(backend_, bool(context_), hardware_);
            reported_ = true;
        }
    }
    void render(QRhiCommandBuffer* cb) override {
        requireOwner();
        const auto id = colorTexture()->globalResourceId();
        const auto native = colorTexture()->nativeTexture().object;
        const auto size = colorTexture()->pixelSize();
        // The item texture persists between requests. A follow-up synchronization
        // just to deliver the receipt needs no second draw or submit.
        if (!motionProbe_ && frame_.scene && frame_ == lastSubmitted_ && id == submittedTextureId_
            && native == submittedTextureObject_ && size == submittedTextureSize_) return;
        if (!drawWithSkia()) {
            lastSubmitted_ = {};
            // Transparent texture; nothing else is drawn on an unaccepted or failed backend.
            cb->beginPass(renderTarget(), Qt::transparent, {1.0f, 0});
            cb->endPass();
        } else if (!motionProbe_) {
            lastSubmitted_ = frame_;
            submittedTextureId_ = id;
            submittedTextureObject_ = native;
            submittedTextureSize_ = size;
            // The application outlives the scene graph. Resolve the item's
            // guarded pointer only on its owning GUI thread.
            QMetaObject::invokeMethod(QCoreApplication::instance(),
                [item = item_, revision = frame_.revision, generation = frame_.generation, time = frame_.timeMs] {
                    if (item) item->publishFrameSubmission(revision, generation, time);
                }, Qt::QueuedConnection);
        }
        if (motionProbe_ || !reported_) update();
    }
private:
    void requireOwner() const {
        if (QThread::currentThread() != owner_)
            qFatal("Skia preview GPU resource accessed outside its owner thread");
    }
    void trace(const char* event) const {
        qCDebug(lcPreview).nospace() << "lifecycle " << event
            << " renderer=" << quintptr(this) << " thread=" << quintptr(owner_)
            << " gui=" << (owner_ == QCoreApplication::instance()->thread());
    }
    void traceResources() {
        if (!lcPreviewResources().isDebugEnabled()) return;
        const auto timeMs = clock_.elapsed();
        if (timeMs < nextResourceSampleMs_) return;
        nextResourceSampleMs_ = timeMs + 10000;
        int count = 0;
        std::size_t bytes = 0;
        context_->getResourceCacheUsage(&count, &bytes);
        qCDebug(lcPreviewResources) << "GPU cache" << backend_ << "elapsed-ms" << timeMs
            << "resources" << count << "used-bytes" << quint64(bytes)
            << "purgeable-bytes" << quint64(context_->getResourceCachePurgeableBytes())
            << "budget-bytes" << quint64(context_->getResourceCacheLimit());
    }
    bool drawWithSkia() {
        if (!context_ || (!motionProbe_ && !frame_.scene)) return false;
        QRhiTexture& texture = *colorTexture();
        try {
            auto surface = bridge_.wrap(*context_, texture);
            auto& canvas = *surface->getCanvas();
            if (motionProbe_) {
                ::subtitle::skia::drawMotionProbe(canvas, texture.pixelSize().width(),
                    texture.pixelSize().height(), clock_.elapsed() / 1000.0);
            } else {
                canvas.scale(float(texture.pixelSize().width()) / frame_.scene->width(),
                             float(texture.pixelSize().height()) / frame_.scene->height());
                frame_.scene->draw(canvas, frame_.timeMs);
            }
            bridge_.submit(*context_, *surface, texture);
            traceResources();
            if (!motionProbe_) qCDebug(lcPreview) << "scene-time" << frame_.timeMs
                << "generation" << frame_.generation << "revision" << frame_.revision;
            trace("rendered");
            return true;
        } catch (const std::exception& error) {
            qCWarning(lcPreview) << "Skia preview frame failed:" << error.what();
            context_.reset();
            reported_ = false;
            return false;
        }
    }
    QString backend_;
    QThread* const owner_ = QThread::currentThread();
    bool hardware_ = false;
    bool reported_ = false;
    bool motionProbe_ = false;
    QPointer<SkiaSubtitleItem> item_;
    PreviewFrame frame_;
    PreviewFrame lastSubmitted_;
    quint64 submittedTextureId_ = 0;
    quint64 submittedTextureObject_ = 0;
    QSize submittedTextureSize_;
    QElapsedTimer clock_;
    qint64 nextResourceSampleMs_ = 0;
    QRhi* deviceRhi_ = nullptr;
    PlatformBridge bridge_;
    sk_sp<GrDirectContext> context_;
};
}

SkiaSubtitleItem::SkiaSubtitleItem(QQuickItem* parent) : QQuickRhiItem(parent) {
    setAlphaBlending(true);
    connect(&preparation_, &QFutureWatcher<PreviewSceneResult>::finished, this, &SkiaSubtitleItem::finishPreparation);
}

void SkiaSubtitleItem::setMotionProbeEnabled(bool enabled) {
    if (enabled == motionProbeEnabled_) return;
    motionProbeEnabled_ = enabled;
    update();
    emit motionProbeEnabledChanged();
}

bool SkiaSubtitleItem::reserveScene(quint64 revision) {
    if (revision <= requestedRevision_) return false;
    requestedRevision_ = revision;
    pendingConfig_.reset();
    awaitingConfig_ = true;
    update();
    return true;
}

bool SkiaSubtitleItem::configureScene(const QByteArray& ir, quint64 revision) {
    if (ir.isEmpty()) return false;
    if (revision > requestedRevision_) reserveScene(revision);
    if (revision != requestedRevision_ || !awaitingConfig_) return false;
    awaitingConfig_ = false;
    pendingConfig_ = PendingConfig{ir, revision};
    if (!preparingRevision_) startPreparation();
    update();
    return true;
}

bool SkiaSubtitleItem::clearScene(quint64 revision) {
    if (!reserveScene(revision)) return false;
    awaitingConfig_ = false;
    scene_.reset();
    sceneRevision_ = 0;
    frame_ = {};
    update();
    return true;
}

void SkiaSubtitleItem::startPreparation() {
    if (!pendingConfig_) return;
    auto config = std::move(*pendingConfig_);
    pendingConfig_.reset();
    preparingRevision_ = config.revision;
    preparation_.setFuture(QtConcurrent::run(preparePreviewScene, std::move(config.ir)));
}

void SkiaSubtitleItem::finishPreparation() {
    const auto revision = preparingRevision_;
    const auto result = preparation_.result();
    preparingRevision_ = 0;
    if (revision == requestedRevision_ && result.scene) {
        scene_ = result.scene;
        sceneRevision_ = revision;
    }
    startPreparation();
    if (revision == requestedRevision_) emit scenePrepared(revision, bool(result.scene), result.error);
}

bool SkiaSubtitleItem::requestFrame(quint64 revision, quint64 generation, int timeMs) {
    if (!scene_ || revision != sceneRevision_) return false;
    if (frame_.revision == revision) {
        if (generation < frame_.generation) return false;
        if (generation == frame_.generation && timeMs == frame_.timeMs) return true;
    }
    frame_ = {scene_, revision, generation, timeMs};
    update();
    return true;
}

void SkiaSubtitleItem::publishGraphicsBackend(const QString& backend, bool accepted, bool hardware) {
    if (backend == backend_ && accepted == accepted_ && hardware == hardware_) return;
    backend_ = backend;
    accepted_ = accepted;
    hardware_ = hardware;
    // Never emit on the render thread: QML bindings and Python slots belong to the GUI thread.
    QMetaObject::invokeMethod(this, &SkiaSubtitleItem::graphicsBackendChanged, Qt::QueuedConnection);
}

void SkiaSubtitleItem::publishFrameSubmission(quint64 revision, quint64 generation, int timeMs) {
    const auto receipt = std::tuple{revision, generation, timeMs};
    // A newer playback tick can be queued before this GPU submission reaches
    // the GUI. Only scene replacement or a seek invalidates that submission.
    if (frame_.revision == revision && frame_.generation == generation && lastReceipt_ != receipt) {
        lastReceipt_ = receipt;
        emit frameSubmitted(revision, generation, timeMs);
    }
}

QQuickRhiItemRenderer* SkiaSubtitleItem::createRenderer() {
    return new SkiaSubtitleRenderer;
}
}
