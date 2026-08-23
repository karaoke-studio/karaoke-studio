#include "shared_frame_metadata_json.h"

#include "../runtime/shared_frame_ring.h"

#include <QtCore/QJsonObject>

namespace krok::subtitle::native::diagnostics {

void appendSharedFrameMetadata(
    QJsonObject &out,
    const runtime::SharedFrameRing &ring,
    int slotIndex
) {
    out.insert(QStringLiteral("payload"), QStringLiteral("shared_memory"));
    out.insert(QStringLiteral("shm_key"), ring.key);
    out.insert(QStringLiteral("slot_index"), slotIndex);
    out.insert(QStringLiteral("slot_count"), ring.slotCount);
    out.insert(QStringLiteral("slot_offset"), slotIndex * ring.slotBytes);
    out.insert(QStringLiteral("slot_bytes"), ring.slotBytes);
    out.insert(QStringLiteral("header_bytes"), ring.headerBytes);
    out.insert(QStringLiteral("payload_offset"), slotIndex * ring.slotBytes + ring.headerBytes);
    out.insert(QStringLiteral("payload_bytes"), ring.pixelBytes);
    out.insert(QStringLiteral("width"), ring.width);
    out.insert(QStringLiteral("height"), ring.height);
    out.insert(QStringLiteral("stride"), ring.stride);
    out.insert(QStringLiteral("pixel_format"), ring.pixelFormat);
}

}  // namespace krok::subtitle::native::diagnostics
