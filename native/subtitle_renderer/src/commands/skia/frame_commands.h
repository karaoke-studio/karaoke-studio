#pragma once
#include "../../backends/render_backend.h"
#include "../../runtime/shared_frame_ring.h"
#include "../../runtime/generation_cancellation.h"
#include <QtCore/QJsonObject>
namespace krok::subtitle::native::commands {
class SkiaFrames {
public:
    void range(RenderBackend& backend, const QJsonObject& request, runtime::GenerationCancellation& cancellation);
    QJsonObject frame(RenderBackend& backend, const QJsonObject& request);
private:
    runtime::SharedFrameRingBuffer ring_;
};
}
