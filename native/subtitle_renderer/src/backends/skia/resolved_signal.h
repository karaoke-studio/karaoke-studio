#pragma once

#include "core/shape_signal.h"

class QJsonObject;

namespace krok::subtitle::native {
class SkiaImageFillCache;
struct TextLine;

std::optional<::subtitle::skia::ShapeSignal> resolveShapeSignal(
    const QJsonObject&, const TextLine&, const TextStyle&, double dpr, SkiaImageFillCache&,
    const TextStyle* role);
}
