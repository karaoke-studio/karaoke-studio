#pragma once
#include "../../backends/render_backend.h"
#include "../../runtime/shared_frame_ring.h"
#include "../../runtime/generation_cancellation.h"
#include <QtCore/QJsonObject>
#include "posix_ring_owner.h"
namespace krok::subtitle::native::commands {
class SkiaFrames {
public:
    QString reserveKey(const QString& key) { return posix_.reserve(key); }
    void completeKey(const QString& key) { posix_.complete(key); }
    bool releaseKey(const QString& key, bool force = false) { return posix_.release(key, force); }
    void range(RenderBackend& backend, const QJsonObject& request, runtime::GenerationCancellation& cancellation, const QString& key);
    QJsonObject frame(RenderBackend& backend, const QJsonObject& request);
private:
    PosixRingOwner posix_; // ring detaches before the remaining owned names are removed.
    runtime::SharedFrameRingBuffer ring_;
};
}
