#pragma once
#include "core/resolved_scene.h"
#include "../../model/glyph_resources.h"
#include "../../model/text_style.h"
#include <QtCore/QJsonObject>

namespace krok::subtitle::native {
class SkiaImageFillCache;
// bitmap_guide_schema=1: decode the guide images, re-derive the canonical box
// from the decoded size and the cell's main-font metrics, and verify the
// final IR geometry before handing physical pixels to the drawing core.
::subtitle::skia::ResolvedCell resolvedBitmapGuideCell(const QJsonObject& cell, const BitmapGuide&,
                                                     SkiaImageFillCache&, const TextStyle&, double dpr,
                                                     int startMs, int endMs, std::uint32_t before,
                                                     std::uint32_t after, double wipePad, bool allowAnimation,
                                                     bool allowShadow);
}
