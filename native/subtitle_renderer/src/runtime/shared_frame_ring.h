#pragma once

#include <QtCore/QString>

#include <cstdint>
#include <memory>
#include <mutex>

class QSharedMemory;

namespace krok::subtitle::native::runtime {

struct SharedFrameRing {
    QString key;
    int slotCount = 0;
    int width = 0;
    int height = 0;
    int stride = 0;
    int pixelBytes = 0;
    int headerBytes = 64;
    int slotBytes = 0;
    int totalBytes = 0;
    QString pixelFormat = QStringLiteral("rgba8888");
};

class SharedFrameRingBuffer {
public:
    SharedFrameRingBuffer();
    ~SharedFrameRingBuffer();

    SharedFrameRingBuffer(const SharedFrameRingBuffer &) = delete;
    SharedFrameRingBuffer &operator=(const SharedFrameRingBuffer &) = delete;

    int slotCount() const;
    bool ensure(
        const QString &key,
        int ringSlotCount,
        int width,
        int height,
        QString *error
    );
    bool writeRgba(
        const std::uint8_t *rgba,
        int width,
        int height,
        int stride,
        int generation,
        int frameIndex,
        int tMs,
        int slotIndex,
        SharedFrameRing *ringOut,
        int formatId = 1,
        const QString &pixelFormat = QStringLiteral("rgba8888")
    );
    bool writePremultipliedBgra(
        const std::uint8_t *premultipliedBgra,
        int width,
        int height,
        int stride,
        int generation,
        int frameIndex,
        int tMs,
        int slotIndex,
        SharedFrameRing *ringOut
    );
    bool writeBands(
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
    );

private:
    mutable std::mutex mutex_;
    std::unique_ptr<QSharedMemory> sharedMemory_;
    SharedFrameRing ring_;
};

QString defaultSharedMemoryKey(int generation);

}  // namespace krok::subtitle::native::runtime
