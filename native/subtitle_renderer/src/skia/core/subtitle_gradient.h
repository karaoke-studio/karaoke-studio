#pragma once

#include "../../model/paint_types.h"
#include "include/core/SkRefCnt.h"
#include "include/core/SkShader.h"

struct SkRect;

namespace subtitle::skia {
struct ScanlinePaint;
sk_sp<SkShader> subtitleGradient(const krok::subtitle::native::PaintStyle&, const SkRect& bounds);

// The existing GPU maps every layer of a static line onto one domain chosen by
// that layer's own paint: horizontal paints span the glyph ink union (at least
// 1px wide); vertical and split paints span the ink union padded by the line's
// visual outline pad. Null for solid and image paints.
sk_sp<SkShader> lineGradient(const krok::subtitle::native::PaintStyle&, const SkRect& ink, float visualPad);
sk_sp<SkShader> scanlineGradient(const krok::subtitle::native::PaintStyle&, const SkRect& ink,
                                float visualPad, const ScanlinePaint&);
// ceil((stroke + stroke2) / scale / 2) * scale, at the layout reference scale.
float outlineVisualPad(float strokeWidth, float stroke2Width, float layoutScale);
// Translucent solid colors or any translucent gradient stop require body protection.
bool paintNeedsBodyProtection(const krok::subtitle::native::PaintStyle&);
}
