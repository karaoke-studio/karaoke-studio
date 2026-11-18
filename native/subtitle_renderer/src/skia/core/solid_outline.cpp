#include "solid_outline.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPathUtils.h"
#include <stdexcept>

namespace subtitle::skia {
SkPath prepareOutlinePath(const SkPath& body, float width) {
    SkPaint stroke;
    stroke.setStyle(SkPaint::kStroke_Style);
    stroke.setStrokeWidth(width);
    stroke.setStrokeJoin(SkPaint::kRound_Join);
    bool isFill = false;
    auto path = skpathutils::FillPathWithPaint(body, stroke, &isFill);
    if (!isFill || !path.isFinite())
        throw std::runtime_error("Skia 无法准备正文描边轮廓");
    return path;
}
}
