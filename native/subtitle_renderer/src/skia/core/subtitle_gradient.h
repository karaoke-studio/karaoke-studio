#pragma once

#include "../../model/paint_types.h"
#include "include/core/SkRefCnt.h"
#include "include/core/SkShader.h"

struct SkRect;

namespace subtitle::skia {
sk_sp<SkShader> subtitleGradient(const krok::subtitle::native::PaintStyle&, const SkRect& bounds);
}
