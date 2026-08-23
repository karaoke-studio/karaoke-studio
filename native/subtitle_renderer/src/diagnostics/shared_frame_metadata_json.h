#pragma once

class QJsonObject;

namespace krok::subtitle::native::runtime {

struct SharedFrameRing;

}  // namespace krok::subtitle::native::runtime

namespace krok::subtitle::native::diagnostics {

void appendSharedFrameMetadata(
    QJsonObject &out,
    const runtime::SharedFrameRing &ring,
    int slotIndex
);

}  // namespace krok::subtitle::native::diagnostics
