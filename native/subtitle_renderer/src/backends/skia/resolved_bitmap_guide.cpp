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
                                                     double wipePad, bool allowAnimation, bool allowShadow,
                                                     bool allowGradientGlow) {
    const auto supportsGlowPaint = [](const PaintStyle& paint) {
        return paint.mode == "solid" || (!paint.stops.empty()
            && (paint.mode == "gradient_horizontal" || paint.mode == "gradient_vertical" || paint.mode == "split_vertical"));
    };
    if (!guide.noDecor && ((style.decorationKindForState(false) == "glow" && !supportsGlowPaint(style.beforeDecorPaint))
        || (style.decorationKindForState(true) == "glow" && !supportsGlowPaint(style.afterDecorPaint))))
        throw BackendError("Skia 图片导唱符光晕画笔无效");
    const auto shadow = guide.noDecor ? ::subtitle::skia::StaticShadow{} : skiaStaticShadow(style);
    const auto animated = [](const std::wstring& path) {
        return !path.empty() && QImageReader(QString::fromStdWString(path)).imageCount() > 1;
    };
    std::array<std::shared_ptr<const ::subtitle::skia::BitmapGuideAnimation>, 2> animations;
    const auto imageFor = [&](const std::wstring& path, auto modified, auto size, bool afterPhase) {
        if (!animated(path)) return images.staticImage(path, modified, size);
        auto& animation = animations[afterPhase];
        animation = images.animatedGuide(path, modified, size);
        if (!animation) throw BackendError("Skia 图片导唱符动图无法解码或超过场景图片预算");
        return animation->front().image;
    };
    const auto beforeImage = imageFor(guide.beforePath, guide.beforeModifiedMs, guide.beforeSize, false);
    const auto afterImage = imageFor(guide.afterPath, guide.afterModifiedMs, guide.afterSize, true);
    const auto metrics = cell.value("metrics").toObject();
    const auto inkValue = metrics.value("ink").toArray();
    if (!metrics.value("bitmap_guide").toBool() || !metrics.value("glyphs").isArray()
        || !metrics.value("glyphs").toArray().isEmpty() || inkValue.size() != 4
        || !metrics.value("font_identity").toString().startsWith(QStringLiteral("bitmap:")))
        throw BackendError("Skia 图片导唱符缺少版本化度量");
    // Size by the after image first, exactly like the existing GPU. A missing
    // or undecodable file keeps the GPU's 1 x font-size box and draws nothing.
    const auto& sizing = afterImage ? afterImage : beforeImage;
    const int unit = std::max(int(std::lround(style.fontSize / dpr)), 1);
    double contentW = 1.0, contentH = unit;
    if (sizing) {
        const double imageW = std::max(sizing->width(), 1), imageH = std::max(sizing->height(), 1);
        contentW = imageW, contentH = imageH;
        if (!guide.fixSize) {
            contentH = std::max(unit * std::max(int(guide.zoomPercent), 1) / 100, 1);
            contentW = std::max(contentH * imageW / imageH, 1.0);
        }
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
        std::max(style.strokeWidth + style.stroke2Width, 2.f) + 4.f, std::move(animations), guide.animAnchorMs,
        std::abs(double(guide.marginBottom)) >= contentH, {shadow.before, shadow.after}, {},
        !guide.afterPath.empty()};
    return out;
}
}
