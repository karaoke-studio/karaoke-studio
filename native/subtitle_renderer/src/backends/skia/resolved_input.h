#pragma once
#include "core/resolved_scene.h"
#include <QtCore/QJsonObject>
namespace krok::subtitle::native {
class SkiaImageFillCache;
::subtitle::skia::ResolvedSubtitleInput resolvedSkiaInput(const QJsonObject& ir);
::subtitle::skia::ResolvedSubtitleInput resolvedSkiaSource(
    const QJsonObject& ir, SkiaImageFillCache&, const ::subtitle::skia::LineBandTexture* sharedBand = nullptr);
}
