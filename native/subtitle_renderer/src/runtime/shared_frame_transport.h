#pragma once

#include <QtCore/QString>

#include <cstdint>

namespace krok::subtitle::native::runtime {

class RenderRuntime;
struct SharedFrameRing;

bool ensureSharedFrameRing(
    RenderRuntime *runtime,
    const QString &key,
    int ringSlotCount,
    int width,
    int height,
    QString *error
);

bool writeSharedRgbaSlot(
    RenderRuntime *runtime,
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

bool writeSharedPackedRgbaSlot(
    RenderRuntime *runtime,
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

bool writeSharedBandSlot(
    RenderRuntime *runtime,
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

}  // namespace krok::subtitle::native::runtime
