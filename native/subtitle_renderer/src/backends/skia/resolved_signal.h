#pragma once

#include "core/shape_signal.h"
#include "core/volume_signal.h"

class QJsonObject;

namespace krok::subtitle::native {
class SkiaImageFillCache;
struct TextLine;
struct SignalPaintContext {
    SkiaImageFillCache& images;
    double dpr;
    float layoutScale;
};

std::optional<::subtitle::skia::ShapeSignal> resolveShapeSignal(
    const QJsonObject&, const TextLine&, const TextStyle&, const SignalPaintContext&, const TextStyle* role);
std::optional<::subtitle::skia::VolumeSignal> resolveVolumeSignal(
    const QJsonObject&, const TextLine&, const TextStyle&, const SignalPaintContext&, const TextStyle* role,
    const SkRect& firstRoleInk);
}
