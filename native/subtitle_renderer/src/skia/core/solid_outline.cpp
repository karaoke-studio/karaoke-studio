#include "solid_outline.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkImage.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPathUtils.h"
#include "include/core/SkSurface.h"
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
void drawFixedOutline(SkCanvas& canvas, const SkPath& path, const SkPaint& paint, float width) {
    SkPaint stroke = paint;
    stroke.setStyle(SkPaint::kStroke_Style);
    stroke.setStrokeWidth(width);
    stroke.setStrokeJoin(SkPaint::kRound_Join);
    canvas.drawPath(path, stroke);
}

void drawSupersampledSolidPath(SkCanvas& canvas, const SkPath& path, const SkPaint& fill, const SkPaint& composite) {
    const auto devicePath = path.makeTransform(canvas.getTotalMatrix());
    auto bounds = devicePath.computeTightBounds().makeOutset(1.f, 1.f).roundOut();
    if (!bounds.intersect(canvas.getDeviceClipBounds())) return;

    constexpr int samples = 2;
    auto surface = canvas.makeSurface(canvas.imageInfo().makeWH(samples * bounds.width(), samples * bounds.height()));
    if (!surface) throw std::runtime_error("Skia 字幕笔画覆盖表面分配失败");

    auto& mask = *surface->getCanvas();
    mask.clear(SK_ColorTRANSPARENT);
    mask.scale(float(samples), float(samples));
    mask.translate(float(-bounds.left()), float(-bounds.top()));
    mask.drawPath(devicePath, fill);
    auto image = surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 字幕笔画覆盖表面读取失败");

    SkAutoCanvasRestore restore(&canvas, true);
    canvas.resetMatrix();
    canvas.drawImageRect(image, SkRect::Make(bounds), SkSamplingOptions(SkFilterMode::kLinear), &composite);
}
}
