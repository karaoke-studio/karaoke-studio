#include "shared_frame_ring.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QSharedMemory>
#include <QtGui/QImage>

#include <algorithm>
#include <cstring>

namespace krok::subtitle::native::runtime {
namespace {

void writeSlotInt(char *base, int offset, int value) {
    const std::int32_t stored = static_cast<std::int32_t>(value);
    std::memcpy(base + offset, &stored, sizeof(stored));
}

int normalizedSlot(int slotIndex, int slotCount) {
    return ((slotIndex % slotCount) + slotCount) % slotCount;
}

}  // namespace

SharedFrameRingBuffer::SharedFrameRingBuffer() = default;
SharedFrameRingBuffer::~SharedFrameRingBuffer() = default;

int SharedFrameRingBuffer::slotCount() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return ring_.slotCount;
}

QString defaultSharedMemoryKey(int generation) {
    return QStringLiteral("krok_subtitle_renderer_%1_%2")
        .arg(QCoreApplication::applicationPid())
        .arg(generation);
}

bool SharedFrameRingBuffer::ensure(
    const QString &key,
    int ringSlotCount,
    int width,
    int height,
    QString *error
) {
    const int safeSlots = std::max(1, ringSlotCount);
    QImage probe(std::max(1, width), std::max(1, height), QImage::Format_RGBA8888);
    const int stride = probe.bytesPerLine();
    const int pixelBytes = stride * probe.height();
    constexpr int headerBytes = 64;
    const int slotBytes = headerBytes + pixelBytes;
    const int totalBytes = slotBytes * safeSlots;

    std::lock_guard<std::mutex> lock(mutex_);
    if (sharedMemory_ != nullptr && sharedMemory_->isAttached()
        && ring_.key == key
        && ring_.slotCount == safeSlots
        && ring_.width == probe.width()
        && ring_.height == probe.height()) {
        return true;
    }
    if (sharedMemory_ != nullptr && sharedMemory_->isAttached()) {
        sharedMemory_->detach();
    }
    sharedMemory_ = std::make_unique<QSharedMemory>(key);
    if (!sharedMemory_->create(totalBytes)) {
        if (error != nullptr) {
            *error = sharedMemory_->errorString();
        }
        sharedMemory_.reset();
        return false;
    }
    ring_ = SharedFrameRing{
        key,
        safeSlots,
        probe.width(),
        probe.height(),
        stride,
        pixelBytes,
        headerBytes,
        slotBytes,
        totalBytes,
        QStringLiteral("rgba8888"),
    };
    if (sharedMemory_->lock()) {
        std::memset(sharedMemory_->data(), 0, static_cast<std::size_t>(totalBytes));
        sharedMemory_->unlock();
    }
    return true;
}

bool SharedFrameRingBuffer::writeRgba(
    const std::uint8_t *rgba,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut,
    int formatId,
    const QString &pixelFormat
) {
    if (rgba == nullptr || width <= 0 || height <= 0 || stride < width * 4) {
        return false;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    if (sharedMemory_ == nullptr || !sharedMemory_->isAttached() || ring_.slotCount <= 0) {
        return false;
    }
    SharedFrameRing ring = ring_;
    if (width != ring.width || height != ring.height) {
        return false;
    }
    const int safeSlot = normalizedSlot(slotIndex, ring.slotCount);
    if (!sharedMemory_->lock()) {
        return false;
    }
    char *base = static_cast<char *>(sharedMemory_->data());
    const int slotOffset = safeSlot * ring.slotBytes;
    char *slot = base + slotOffset;
    writeSlotInt(slot, 0, 1);
    writeSlotInt(slot, 4, generation);
    writeSlotInt(slot, 8, frameIndex);
    writeSlotInt(slot, 12, tMs);
    writeSlotInt(slot, 16, ring.width);
    writeSlotInt(slot, 20, ring.height);
    writeSlotInt(slot, 24, ring.stride);
    writeSlotInt(slot, 28, formatId);
    writeSlotInt(slot, 32, ring.headerBytes);
    writeSlotInt(slot, 36, ring.pixelBytes);
    char *payload = slot + ring.headerBytes;
    if (stride == ring.stride) {
        std::memcpy(payload, rgba, static_cast<std::size_t>(ring.pixelBytes));
    } else {
        for (int y = 0; y < height; ++y) {
            std::memcpy(
                payload + static_cast<std::size_t>(ring.stride) * y,
                rgba + static_cast<std::size_t>(stride) * y,
                static_cast<std::size_t>(width * 4)
            );
        }
    }
    writeSlotInt(slot, 0, 2);
    sharedMemory_->unlock();
    if (ringOut != nullptr) {
        ring.pixelFormat = pixelFormat;
        *ringOut = ring;
    }
    return true;
}

bool SharedFrameRingBuffer::writePremultipliedBgra(
    const std::uint8_t *premultipliedBgra,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut
) {
    if (premultipliedBgra == nullptr || width <= 0 || height <= 0
        || stride < width * 4) {
        return false;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    if (sharedMemory_ == nullptr || !sharedMemory_->isAttached()
        || ring_.slotCount <= 0) {
        return false;
    }
    SharedFrameRing ring = ring_;
    if (width != ring.width || height != ring.height
        || ring.stride != width * 4) {
        return false;
    }
    const int safeSlot = normalizedSlot(slotIndex, ring.slotCount);
    if (!sharedMemory_->lock()) {
        return false;
    }
    char *base = static_cast<char *>(sharedMemory_->data());
    const int slotOffset = safeSlot * ring.slotBytes;
    char *slot = base + slotOffset;
    writeSlotInt(slot, 0, 1);
    writeSlotInt(slot, 4, generation);
    writeSlotInt(slot, 8, frameIndex);
    writeSlotInt(slot, 12, tMs);
    writeSlotInt(slot, 16, width);
    writeSlotInt(slot, 20, height);
    writeSlotInt(slot, 24, ring.stride);
    writeSlotInt(slot, 28, 1);
    writeSlotInt(slot, 32, ring.headerBytes);
    writeSlotInt(slot, 36, ring.pixelBytes);
    QImage premultiplied(
        const_cast<std::uint8_t *>(premultipliedBgra),
        width,
        height,
        stride,
        QImage::Format_ARGB32_Premultiplied
    );
    const QImage straight = premultiplied.convertToFormat(QImage::Format_RGBA8888);
    if (straight.isNull()) {
        sharedMemory_->unlock();
        return false;
    }
    auto *payload = reinterpret_cast<std::uint8_t *>(slot + ring.headerBytes);
    const int rowBytes = width * 4;
    for (int y = 0; y < height; ++y) {
        auto *destination = payload + static_cast<std::size_t>(y) * ring.stride;
        std::memcpy(destination, straight.constScanLine(y), rowBytes);
    }
    writeSlotInt(slot, 0, 2);
    sharedMemory_->unlock();
    if (ringOut != nullptr) {
        ring.pixelFormat = QStringLiteral("rgba8888");
        *ringOut = ring;
    }
    return true;
}

bool SharedFrameRingBuffer::writeBands(
    const std::uint8_t *payloadData,
    int payloadBytes,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut
) {
    if (payloadBytes < 0 || (payloadBytes > 0 && payloadData == nullptr)) {
        return false;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    if (sharedMemory_ == nullptr || !sharedMemory_->isAttached()
        || ring_.slotCount <= 0) {
        return false;
    }
    SharedFrameRing ring = ring_;
    if (width != ring.width || height != ring.height || stride != ring.stride
        || payloadBytes > ring.pixelBytes) {
        return false;
    }
    const int safeSlot = normalizedSlot(slotIndex, ring.slotCount);
    if (!sharedMemory_->lock()) {
        return false;
    }
    char *base = static_cast<char *>(sharedMemory_->data());
    const int slotOffset = safeSlot * ring.slotBytes;
    char *slot = base + slotOffset;
    writeSlotInt(slot, 0, 1);
    writeSlotInt(slot, 4, generation);
    writeSlotInt(slot, 8, frameIndex);
    writeSlotInt(slot, 12, tMs);
    writeSlotInt(slot, 16, width);
    writeSlotInt(slot, 20, height);
    writeSlotInt(slot, 24, stride);
    writeSlotInt(slot, 28, 3);
    writeSlotInt(slot, 32, ring.headerBytes);
    writeSlotInt(slot, 36, payloadBytes);
    if (payloadBytes > 0) {
        std::memcpy(
            slot + ring.headerBytes,
            payloadData,
            static_cast<std::size_t>(payloadBytes)
        );
    }
    writeSlotInt(slot, 0, 2);
    sharedMemory_->unlock();
    if (ringOut != nullptr) {
        ring.pixelBytes = payloadBytes;
        ring.pixelFormat = QStringLiteral("bgra8888_premultiplied_bands");
        *ringOut = ring;
    }
    return true;
}

}  // namespace krok::subtitle::native::runtime
