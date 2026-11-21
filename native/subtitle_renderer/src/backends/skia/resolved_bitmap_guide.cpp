#include "resolved_bitmap_guide.h"
#include "image_fill_cache.h"
#include "scene_input.h"
#include "../render_backend.h"
#include <QtCore/QJsonArray>
#include <QtGui/QImageReader>
#include <algorithm>
#include <cmath>

namespace krok::subtitle::native {
namespace {
double finite(const QJsonObject& o, const char* key) {
    const auto v = o.value(QLatin1String(key));
    if (!v.isDouble() || !std::isfinite(v.toDouble()) || std::abs(v.toDouble()) > 1e6)
        throw BackendError(std::string("Skia 图片导唱符几何无效：") + key);
    return v.toDouble();
}
bool near(double value, double expected) { return std::abs(value - expected) <= 1e-3; }
}

::subtitle::skia::ResolvedCell resolvedBitmapGuideCell(const QJsonObject& cell, const BitmapGuide& guide,
                                                     SkiaImageFillCache& images, const TextStyle& style, double dpr,
                                                     int start, int end, std::uint32_t before, std::uint32_t after,
                                                     double wipePad, bool animatedTitle) {
    if (style.beforeDecorPaint.mode != "solid" || style.afterDecorPaint.mode != "solid")
        throw BackendError("Skia 图片导唱符光晕仅支持实色装饰");
    const auto animated = [](const std::wstring& path) {
        return !path.empty() && QImageReader(QString::fromStdWString(path)).imageCount() > 1;
    };
    const bool beforeAnimated = animated(guide.beforePath);
    if (!animatedTitle && (beforeAnimated || animated(guide.afterPath)))
        throw BackendError("Skia 图片导唱符暂不支持动图");
    const auto animation = beforeAnimated
        ? images.animatedGuide(guide.beforePath, guide.beforeModifiedMs, guide.beforeSize) : nullptr;
    if (beforeAnimated && !animation) throw BackendError("Skia 标题动图无法解码或超过场景图片预算");
    const auto beforeImage = animation ? animation->front().image
        : images.staticImage(guide.beforePath, guide.beforeModifiedMs, guide.beforeSize);
    const auto afterImage = images.staticImage(guide.afterPath, guide.afterModifiedMs, guide.afterSize);
    if ((!guide.beforePath.empty() && !beforeImage) || (!guide.afterPath.empty() && !afterImage)
        || (!beforeImage && !afterImage))
        throw BackendError("Skia 图片导唱符无法解码图片");
    const auto metrics = cell.value("metrics").toObject();
    const auto inkValue = metrics.value("ink").toArray();
    if (!metrics.value("bitmap_guide").toBool() || !metrics.value("glyphs").isArray()
        || !metrics.value("glyphs").toArray().isEmpty() || inkValue.size() != 4
        || !metrics.value("font_identity").toString().startsWith(QStringLiteral("bitmap:")))
        throw BackendError("Skia 图片导唱符缺少版本化度量");
    // Size by the after image first, exactly like the existing GPU.
    const auto& sizing = afterImage ? afterImage : beforeImage;
    const int unit = std::max(int(std::lround(style.fontSize / dpr)), 1);
    const double imageW = std::max(sizing->width(), 1), imageH = std::max(sizing->height(), 1);
    double contentW = imageW, contentH = imageH;
    if (!guide.fixSize) {
        contentH = std::max(unit * std::max(int(guide.zoomPercent), 1) / 100, 1);
        contentW = std::max(contentH * imageW / imageH, 1.0);
    }
    const double ascent = finite(metrics, "ascent"), descent = finite(metrics, "descent");
    if (ascent < 0 || descent < 0 || ascent + descent <= 0) throw BackendError("Skia 图片导唱符字体纵向度量无效");
    const int edge = std::max(int(std::lround(style.strokeWidth / dpr)), 0);
    const double bottom = std::floor(unit * descent / (ascent + descent) + 1e-6) + edge / 2 - guide.marginBottom;
    const double advance = std::max(contentW + guide.marginLeft + guide.marginRight, 0.0);
    const double ink[4] = {guide.marginLeft, bottom - contentH, guide.marginLeft + contentW, bottom};
    if (!near(finite(metrics, "advance"), advance))
        throw BackendError("Skia 图片导唱符度量快照与图片尺寸不一致");
    for (int i = 0; i < 4; ++i)
        if (!inkValue[i].isDouble() || !near(inkValue[i].toDouble(), ink[i]))
            throw BackendError("Skia 图片导唱符度量快照与图片尺寸不一致");
    const double x = finite(cell, "origin_x"), y = finite(cell, "baseline_y");
    const double left = finite(cell, "wipe_left"), right = finite(cell, "wipe_right");
    if (!near(left, x + ink[0] - wipePad) || !near(right, x + ink[2] + wipePad))
        throw BackendError("Skia 图片导唱符走字边界与图片框不一致");
    ::subtitle::skia::ResolvedCell out{SkPath(), float(left * dpr), float(right * dpr), start, end, before, after};
    out.bitmap = ::subtitle::skia::BitmapGuide{
        beforeImage, afterImage,
        SkRect::MakeLTRB(float((x + ink[0]) * dpr), float((y + ink[1]) * dpr),
                         float((x + ink[2]) * dpr), float((y + ink[3]) * dpr)),
        skiaStaticGlow(style), !guide.noDecor,
        std::max(style.strokeWidth + style.stroke2Width, 2.f) + 4.f, animation};
    return out;
}
}
