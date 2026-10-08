#include "d2d_backend.h"
#include "d2d_backend_internal.h"
#include "../signal_state.h"
#include "d2d_font_fallback.h"
#include "d2d_geometry_resources.h"
#include "d2d_paint_resources.h"
#include "d2d_runtime_support.h"
#include "d2d_stroke_outline.h"
// 本 TU 原本 Qt-free，局部变量名 slots 与 Qt 的关键字宏冲突——在本
// TU 内禁用 signals/slots/emit 宏后再引入协议头（仅心跳上报用）。
#define QT_NO_KEYWORDS
#include "../../protocol/json_protocol.h"
#undef QT_NO_KEYWORDS
#include "../../diagnostics/long_call_watchdog.h"
#include "../text_semantics.h"

#include <d2d1_2.h>
#include <d2d1helper.h>
#include <dwrite.h>
#include <dwrite_3.h>
#include <optional>

#include <algorithm>
#include <bit>
#include <chrono>
#include <cmath>
#include <map>
#include <mutex>
#include <set>
#include <thread>
#include <tuple>
#include <utility>

namespace krok::subtitle::native {

using Clock = direct2d::RuntimeClock;
using direct2d::checkHr;
using direct2d::containsEmoji;
using direct2d::createFontFace;
using direct2d::elapsedMs;
using direct2d::findFallbackFontFace;
using direct2d::glyphIndices;
using direct2d::loadWicBitmap;
using direct2d::outsideStrokeGeometry;
using direct2d::paintNeedsBodyProtection;
using direct2d::resolveFontFaces;
using direct2d::steadyNowMs;
using direct2d::validGlyphIndices;
using direct2d::vectorGlyphGeometry;
using direct2d::widenedStrokeGeometry;

namespace {

void eraseRgb(RgbaColor &color) {
    color.red = 0;
    color.green = 0;
    color.blue = 0;
}

void erasePaintRgb(PaintStyle &paint) {
    eraseRgb(paint.color);
    for (PaintStop &stop : paint.stops) {
        eraseRgb(stop.color);
    }
}

TextStyle geometryStyle(TextStyle style) {
    RgbaColor *colors[] = {
        &style.beforeFill, &style.afterFill,
        &style.beforeStroke, &style.afterStroke,
        &style.beforeStroke2, &style.afterStroke2,
        &style.beforeDecor, &style.afterDecor,
        &style.rubyBeforeFill, &style.rubyAfterFill,
        &style.rubyBeforeStroke, &style.rubyAfterStroke,
        &style.rubyBeforeStroke2, &style.rubyAfterStroke2,
        &style.rubyBeforeDecor, &style.rubyAfterDecor,
        &style.litFill, &style.litStroke,
        &style.volumeFill, &style.volumeStroke,
        &style.volumeOverlayFill, &style.volumeOverlayStroke,
    };
    for (RgbaColor *color : colors) {
        eraseRgb(*color);
    }
    PaintStyle *paints[] = {
        &style.beforeFillPaint, &style.afterFillPaint,
        &style.beforeStrokePaint, &style.afterStrokePaint,
        &style.beforeStroke2Paint, &style.afterStroke2Paint,
        &style.beforeDecorPaint, &style.afterDecorPaint,
        &style.rubyBeforeFillPaint, &style.rubyAfterFillPaint,
        &style.rubyBeforeStrokePaint, &style.rubyAfterStrokePaint,
        &style.rubyBeforeStroke2Paint, &style.rubyAfterStroke2Paint,
        &style.rubyBeforeDecorPaint, &style.rubyAfterDecorPaint,
        &style.scanlineRolePaint,
    };
    for (PaintStyle *paint : paints) {
        erasePaintRgb(*paint);
    }
    return style;
}

bool geometryStylesEqual(const std::vector<TextStyle> &left,
                         const std::vector<TextStyle> &right) {
    if (left.size() != right.size()) {
        return false;
    }
    for (std::size_t index = 0; index < left.size(); ++index) {
        if (geometryStyle(left[index]) != geometryStyle(right[index])) {
            return false;
        }
    }
    return true;
}

bool sceneDiffersOnlyInRgb(const RenderScene &left, const RenderScene &right) {
    return left.width == right.width
        && left.height == right.height
        && left.layoutReferenceScale == right.layoutReferenceScale
        && left.exportCropTop == right.exportCropTop
        && left.exportCropHeight == right.exportCropHeight
        && left.exportBands == right.exportBands
        && left.prewarmTimeMs == right.prewarmTimeMs
        && left.realizationEnabled == right.realizationEnabled
        && left.deferRealizationPrewarmUntilFirstFrame
            == right.deferRealizationPrewarmUntilFirstFrame
        && left.realizationCapacity == right.realizationCapacity
        && left.viewportScale == right.viewportScale
        && left.viewportRotation == right.viewportRotation
        && left.viewportOffsetX == right.viewportOffsetX
        && left.viewportOffsetY == right.viewportOffsetY
        && left.viewportAlign == right.viewportAlign
        && geometryStyle(left.style) == geometryStyle(right.style)
        && geometryStylesEqual(left.lineStyles, right.lineStyles)
        && geometryStylesEqual(left.charStyles, right.charStyles)
        && left.lines == right.lines;
}

}  // namespace

void Direct2DGpuBackend::configure(const RenderScene &scene) {
    if (scene.width <= 0 || scene.height <= 0 || scene.width > 8192 || scene.height > 8192) {
        throw BackendError("GPU scene dimensions must be within 1..8192");
    }
    if (impl_->configured && impl_->scene == scene) {
        ++impl_->diagnostics.cacheHits;
        return;
    }
    if (impl_->configured && sceneDiffersOnlyInRgb(impl_->scene, scene)) {
        // Chroma-only edits do not alter glyph outlines, protection topology,
        // layout, or realization meshes.  Swap the paints in place and keep
        // every positioned geometry/realization alive; alpha/mode/image/stop
        // topology remains part of the comparison and falls back to rebuild.
        impl_->scene = scene;
        // 帧仓登记的像素是旧色画的，随画笔一起作废（纹理同尺寸可复用）。
        clearFrameStoreRegistrations();
        // 仅改色也会换掉 glow 画笔颜色：稳态模糊缓存条目按旧色烘焙，作废。
        impl_->glowBlurCache.clear();
        for (std::size_t index = 0; index < impl_->lines.size(); ++index) {
            impl_->lines[index].style = index < scene.lineStyles.size()
                ? scene.lineStyles[index]
                : scene.style;
        }
        if (!impl_->brushes.empty()) {
            impl_->brushes.clear();
            impl_->brushUseSerial = 0;
            if (impl_->countersEnabled) {
                ++impl_->diagnostics.brushCacheInvalidations;
            }
        }
        impl_->diagnostics.realizationPrewarmTasks = 0;
        impl_->diagnostics.realizationPrewarmSkipped = 0;
        impl_->diagnostics.realizationPrewarmMs = 0.0;
        ++impl_->diagnostics.cacheHits;
        return;
    }
    ++impl_->diagnostics.cacheMisses;
    if (impl_->realizationControl) {
        impl_->realizationControl->stop.store(true, std::memory_order_release);
    }
    if (impl_->realizationThread.joinable()) {
        if (impl_->realizationControl
            && impl_->realizationControl->done.load(std::memory_order_acquire)) {
            impl_->realizationThread.join();
        } else {
            impl_->retiredRealizationWorkers.push_back({
                impl_->realizationControl,
                std::move(impl_->realizationThread),
            });
        }
    }
    for (auto worker = impl_->retiredRealizationWorkers.begin();
         worker != impl_->retiredRealizationWorkers.end();) {
        if (!worker->control->done.load(std::memory_order_acquire)) {
            ++worker;
            continue;
        }
        if (worker->thread.joinable()) {
            worker->thread.join();
        }
        worker = impl_->retiredRealizationWorkers.erase(worker);
    }
    {
        std::lock_guard<std::mutex> realizationLock(impl_->realizationMutex);
        ++impl_->realizationGeneration;
        impl_->realizationCount = 0;
    }
    impl_->realizationControl.reset();
    impl_->realizationPrewarmComplete.store(true, std::memory_order_release);
    impl_->firstFrameCompleted.store(false, std::memory_order_release);
    if (!impl_->brushes.empty()) {
        impl_->brushes.clear();
        impl_->brushUseSerial = 0;
        if (impl_->countersEnabled) {
            ++impl_->diagnostics.brushCacheInvalidations;
        }
    }
    if (impl_->frameSurfaceWidth != scene.width || impl_->frameSurfaceHeight != scene.height) {
        impl_->frameTargetBitmap.Reset();
        impl_->frameTargetTexture.Reset();
        impl_->frameStagingTexture.Reset();
        impl_->glowScratchPool.clear();
        impl_->glowEffectPool.clear();
        impl_->glowScratchInUse = 0;
        impl_->glowEffectInUse = 0;
        impl_->frameSurfaceWidth = scene.width;
        impl_->frameSurfaceHeight = scene.height;
        // 尺寸变化：仓里的纹理全部失配，连登记带纹理整仓释放。
        releaseFrameStoreTextures();
    }
    impl_->scene = scene;
    // 全量重建（含同尺寸）：行集/几何全部换血，仓里任何登记帧都已过期。
    clearFrameStoreRegistrations();
    // 稳态 glow 模糊缓存随场景整体失效：行向量即将清空重建，缓存条目
    // 持有的行指针全部悬空。
    impl_->glowBlurCache.clear();
    impl_->glowBlurCacheSerial = 0;
    impl_->glowBlurCacheMode = direct2d::environmentSize(
        "KROK_SUBTITLE_GPU_GLOW_CACHE_MODE", 1, 1, 4
    );
    impl_->diagnostics.glyphGeometryCacheHits = 0;
    impl_->diagnostics.glyphGeometryCacheMisses = 0;
    impl_->diagnostics.glyphGeometryCacheSize = 0;
    impl_->diagnostics.glyphGeometryCacheEvictions = 0;
    impl_->diagnostics.glyphGeometryCacheCapacity =
        impl_->resourceCacheEnabled ? impl_->glyphGeometryCapacity : 0;
    impl_->diagnostics.glyphStrokeCacheHits = 0;
    impl_->diagnostics.glyphStrokeCacheMisses = 0;
    impl_->diagnostics.glyphGeometryBuildMs = 0.0;
    impl_->diagnostics.glyphStrokeBuildMs = 0.0;
    impl_->diagnostics.vectorGlyphCacheHits = 0;
    impl_->diagnostics.vectorGlyphCacheMisses = 0;
    impl_->diagnostics.vectorGlyphCacheSize = 0;
    impl_->diagnostics.vectorGlyphCacheEvictions = 0;
    impl_->diagnostics.vectorGlyphCacheCapacity =
        impl_->resourceCacheEnabled ? impl_->vectorGlyphCapacity : 0;
    impl_->diagnostics.vectorGlyphBuildMs = 0.0;
    impl_->diagnostics.imageCacheHits = 0;
    impl_->diagnostics.imageCacheMisses = 0;
    impl_->diagnostics.imageCacheSize = 0;
    impl_->diagnostics.imageCacheEvictions = 0;
    impl_->diagnostics.imageCacheCapacity =
        impl_->resourceCacheEnabled ? impl_->imageCapacity : 0;
    impl_->diagnostics.imageBuildMs = 0.0;
    const float layoutScale = std::max(scene.layoutReferenceScale, 0.01f);
    const std::uint32_t layoutScaleKey = std::bit_cast<std::uint32_t>(layoutScale);
    const bool scaledPreviewLayout = std::abs(layoutScale - 1.0f) > 0.000001f;
    const auto referenceInt = [&](float scaledValue, int minimum) {
        const int value = scaledPreviewLayout
            ? static_cast<int>(std::lround(scaledValue / layoutScale))
            : static_cast<int>(scaledValue);
        return std::max(value, minimum);
    };
    const auto transformPath = [&](Microsoft::WRL::ComPtr<ID2D1PathGeometry> &path,
                                   float scaleX, float scaleY,
                                   const char *operation) {
        if (!path || (std::abs(scaleX - 1.0f) < 0.000001f
                      && std::abs(scaleY - 1.0f) < 0.000001f)) {
            return;
        }
        const D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Scale(
            scaleX, scaleY
        );
        Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
        checkHr(
            device_.d2dFactory()->CreateTransformedGeometry(
                path.Get(), &matrix, transformed.ReleaseAndGetAddressOf()
            ),
            operation,
            device_
        );
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> scaledPath;
        checkHr(
            device_.d2dFactory()->CreatePathGeometry(scaledPath.ReleaseAndGetAddressOf()),
            "ID2D1Factory::CreatePathGeometry(scale preview outline)",
            device_
        );
        Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
        checkHr(
            scaledPath->Open(sink.ReleaseAndGetAddressOf()),
            "ID2D1PathGeometry::Open(scale preview outline)",
            device_
        );
        sink->SetFillMode(D2D1_FILL_MODE_WINDING);
        sink->SetSegmentFlags(D2D1_PATH_SEGMENT_FORCE_ROUND_LINE_JOIN);
        const HRESULT simplifyResult = transformed->Simplify(
            D2D1_GEOMETRY_SIMPLIFICATION_OPTION_CUBICS_AND_LINES,
            nullptr,
            sink.Get()
        );
        const HRESULT closeResult = sink->Close();
        checkHr(simplifyResult, operation, device_);
        checkHr(closeResult, "ID2D1GeometrySink::Close(scale preview outline)", device_);
        path = scaledPath;
    };
    const auto scaleReferenceGeometry = [&](Microsoft::WRL::ComPtr<ID2D1PathGeometry> &path,
                                            const char *operation) {
        if (scaledPreviewLayout) {
            transformPath(path, layoutScale, layoutScale, operation);
        }
    };
    impl_->realizationActive = impl_->realizationEnabled
        && scene.realizationEnabled
        && impl_->realizationContext;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
    impl_->lines.clear();
    impl_->lines.reserve(scene.lines.size());
    // 装饰粒子 sprite 轮廓几何：em 空间一次构建，绘制期按 sizePx/1000 缩放。
    impl_->fxSpriteGeometries.clear();
    impl_->fxPaintBrushes.clear();
    impl_->lampShapeGeometries.clear();
    for (const auto &entry : scene.fxSprites) {
        impl_->fxSpriteGeometries[entry.first] = vectorGlyphGeometry(
            device_.d2dFactory(), entry.second, 1000.0f, device_
        );
    }
    if (!impl_->resourceCacheEnabled) {
        impl_->images.clear();
        impl_->imageUseSerial = 0;
    }
    using ImageKey = std::tuple<std::wstring, std::uint64_t, std::uint64_t>;
    std::set<ImageKey> activeImages;
    auto cacheImage = [&] (
        const std::wstring &path,
        std::uint64_t modifiedMs,
        std::uint64_t size,
        bool animatedGuide
    ) -> Impl::CachedImage * {
        if (path.empty()) {
            return nullptr;
        }
        const ImageKey key{path, modifiedMs, size};
        activeImages.insert(key);
        auto found = std::find_if(
            impl_->images.begin(), impl_->images.end(),
            [&](const Impl::CachedImage &image) {
                return image.path == path
                    && image.modifiedMs == modifiedMs
                    && image.size == size;
            }
        );
        if (found != impl_->images.end()) {
            ++impl_->diagnostics.imageCacheHits;
            found->lastUse = ++impl_->imageUseSerial;
            // A file first encountered as an image fill only needs frame zero.
            // Upgrade that same persistent entry once if it is later used as a
            // bitmap guide, so GIF animation is not accidentally frozen.
            if (animatedGuide && !found->animationChecked) {
                const auto buildStart = Clock::now();
                direct2d::AnimatedBitmapFrames animated =
                    direct2d::loadWicAnimatedBitmaps(
                        device_.d2dContext(), path, 60
                    );
                if (!animated.bitmaps.empty()) {
                    found->bitmap = animated.bitmaps.front();
                    found->frames = std::move(animated.bitmaps);
                    found->frameDelaysMs = std::move(animated.delaysMs);
                }
                found->animationChecked = true;
                impl_->diagnostics.imageBuildMs += elapsedMs(buildStart);
            }
            return &*found;
        }

        ++impl_->diagnostics.imageCacheMisses;
        const auto buildStart = Clock::now();
        Impl::CachedImage image;
        image.path = path;
        image.modifiedMs = modifiedMs;
        image.size = size;
        image.animationChecked = animatedGuide;
        if (animatedGuide) {
            direct2d::AnimatedBitmapFrames animated =
                direct2d::loadWicAnimatedBitmaps(
                    device_.d2dContext(), path, 60
                );
            if (!animated.bitmaps.empty()) {
                image.bitmap = animated.bitmaps.front();
                image.frames = std::move(animated.bitmaps);
                image.frameDelaysMs = std::move(animated.delaysMs);
            }
        }
        if (!image.bitmap) {
            image.bitmap = loadWicBitmap(device_.d2dContext(), path);
        }
        impl_->diagnostics.imageBuildMs += elapsedMs(buildStart);
        if (!image.bitmap) {
            return nullptr;
        }
        image.lastUse = ++impl_->imageUseSerial;
        impl_->images.push_back(std::move(image));
        return &impl_->images.back();
    };
    auto cacheStyleImages = [&](const TextStyle &style) {
        const PaintStyle *paints[] = {
            &style.beforeFillPaint, &style.afterFillPaint,
            &style.beforeStrokePaint, &style.afterStrokePaint,
            &style.beforeStroke2Paint, &style.afterStroke2Paint,
            &style.beforeDecorPaint, &style.afterDecorPaint,
            &style.rubyBeforeFillPaint, &style.rubyAfterFillPaint,
            &style.rubyBeforeStrokePaint, &style.rubyAfterStrokePaint,
            &style.rubyBeforeStroke2Paint, &style.rubyAfterStroke2Paint,
            &style.rubyBeforeDecorPaint, &style.rubyAfterDecorPaint,
            &style.scanlineRolePaint,
        };
        for (const PaintStyle *paint : paints) {
            if (paint->mode != "image" || paint->imagePath.empty()) {
                continue;
            }
            cacheImage(
                paint->imagePath,
                paint->imageModifiedMs,
                paint->imageSize,
                false
            );
        }
        // 形状灯「图片」模式素材共用同一缓存池（按 path+mtime+size 键控）。
        if (style.litStyle == "image" && !style.litImagePath.empty()) {
            cacheImage(
                style.litImagePath,
                style.litImageModifiedMs,
                style.litImageSize,
                false
            );
        }
    };
    auto cacheBitmapImage = [&](const std::wstring &path,
                                std::uint64_t modifiedMs,
                                std::uint64_t size) {
        cacheImage(path, modifiedMs, size, true);
    };
    cacheStyleImages(scene.style);
    for (const TextStyle &style : scene.lineStyles) {
        cacheStyleImages(style);
    }
    for (const TextStyle &style : scene.charStyles) {
        cacheStyleImages(style);
    }
    for (const TextLine &line : scene.lines) {
        for (const TextChar &ch : line.chars) {
            if (!ch.bitmapGuide.has_value()) {
                continue;
            }
            cacheBitmapImage(
                ch.bitmapGuide->beforePath,
                ch.bitmapGuide->beforeModifiedMs,
                ch.bitmapGuide->beforeSize
            );
            cacheBitmapImage(
                ch.bitmapGuide->afterPath,
                ch.bitmapGuide->afterModifiedMs,
                ch.bitmapGuide->afterSize
            );
        }
    }

    Microsoft::WRL::ComPtr<IDWriteFontCollection> fontCollection;
    checkHr(
        device_.dwriteFactory()->GetSystemFontCollection(
            fontCollection.ReleaseAndGetAddressOf(),
            FALSE
        ),
        "IDWriteFactory::GetSystemFontCollection",
        device_
    );
    // Typographic-model collection: variable fonts group under merged family
    // names ("Segoe UI Variable") here while the classic GDI-model collection
    // above only lists per-weight subfamilies.  Qt's font picker offers the
    // merged spelling, so without this collection those names resolved to the
    // default-font substitution.  Null on pre-RS4 DirectWrite keeps legacy
    // behavior; both collections contain the same fonts, just grouped
    // differently.
    Microsoft::WRL::ComPtr<IDWriteFontCollection> typographicFontCollection;
    {
        Microsoft::WRL::ComPtr<IDWriteFactory7> factory7;
        Microsoft::WRL::ComPtr<IDWriteFontCollection3> typedCollection;
        if (SUCCEEDED(device_.dwriteFactory()->QueryInterface(
                IID_PPV_ARGS(factory7.ReleaseAndGetAddressOf())))
            && SUCCEEDED(factory7->GetSystemFontCollection(
                FALSE,
                DWRITE_FONT_FAMILY_MODEL_TYPOGRAPHIC,
                typedCollection.ReleaseAndGetAddressOf()))) {
            typographicFontCollection = typedCollection;
        }
    }
    auto resolveFaces = [&](
        const std::wstring &family, int weight, bool italic,
        int faceWeight = -1, bool simBold = false, bool axisHint = false
    ) {
        const std::wstring resolvedFamily = family.empty() ? L"Segoe UI" : family;
        const Impl::FontFaceKey key{
            resolvedFamily, weight, italic,
            faceWeight > 0 ? faceWeight : -1,
            faceWeight > 0 && simBold
        };
        const auto found = impl_->fontFaces.find(key);
        if (found != impl_->fontFaces.end()) {
            const auto metricFound = impl_->metricFaces.find(key);
            return std::make_pair(
                found->second,
                metricFound != impl_->metricFaces.end() && metricFound->second
                    ? metricFound->second
                    : found->second
            );
        }
        auto faces = resolveFontFaces(
            fontCollection.Get(),
            typographicFontCollection.Get(),
            resolvedFamily,
            weight,
            italic,
            faceWeight,
            simBold,
            axisHint
        );
        if (!faces.outline && resolvedFamily != L"Segoe UI") {
            faces = resolveFontFaces(
                fontCollection.Get(),
                typographicFontCollection.Get(),
                L"Segoe UI",
                weight,
                italic
            );
        }
        if (!faces.outline) {
            throw BackendError("DirectWrite could not resolve a usable font face");
        }
        impl_->fontFaces.emplace(key, faces.outline);
        const auto metricsFace = faces.metrics ? faces.metrics : faces.outline;
        impl_->metricFaces.emplace(key, metricsFace);
        return std::make_pair(faces.outline, metricsFace);
    };
    // Outline face (axis-value instance / simulated) for glyph runs and
    // per-glyph metrics; vertical box math must use the metrics face below.
    auto resolveFace = [&](
        const std::wstring &family, int weight, bool italic,
        int faceWeight = -1, bool simBold = false, bool axisHint = false
    ) {
        return resolveFaces(family, weight, italic, faceWeight, simBold, axisHint).first;
    };
    auto resolveMetricsFace = [&](
        const std::wstring &family, int weight, bool italic,
        int faceWeight = -1, bool simBold = false, bool axisHint = false
    ) {
        return resolveFaces(family, weight, italic, faceWeight, simBold, axisHint).second;
    };

    auto extendBounds = [](D2D1_RECT_F &target, bool &hasBounds, const D2D1_RECT_F &value) {
        if (!hasBounds) {
            target = value;
            hasBounds = true;
            return;
        }
        target.left = std::min(target.left, value.left);
        target.top = std::min(target.top, value.top);
        target.right = std::max(target.right, value.right);
        target.bottom = std::max(target.bottom, value.bottom);
    };
    auto imageForBitmapGuide = [&](const std::wstring &path,
                                   std::uint64_t modifiedMs,
                                   std::uint64_t size) -> ID2D1Bitmap1 * {
        const auto found = std::find_if(
            impl_->images.begin(), impl_->images.end(),
            [&](const Impl::CachedImage &image) {
                return image.path == path
                    && image.modifiedMs == modifiedMs
                    && image.size == size;
            }
        );
        return found == impl_->images.end() ? nullptr : found->bitmap.Get();
    };

    // Vector guide glyphs carry a content fingerprint computed while parsing
    // the IR.  It is stable across configure generations even when table order
    // changes, so expensive SVG paths and widened/protected stroke geometries
    // survive ordinary scene rebuilds just like text glyph resources.
    using GlyphGeometryResource = Impl::GlyphGeometryResource;
    std::map<Impl::VectorGlyphKey, GlyphGeometryResource> configureVectorResources;
    auto &vectorGlyphRealizations = impl_->resourceCacheEnabled
        ? impl_->vectorGlyphResources
        : configureVectorResources;
    auto vectorRealizationFor = [&](
        const std::shared_ptr<const krok::subtitle::native::VectorGlyph> &glyph,
        int unit
    ) -> GlyphGeometryResource & {
        const Impl::VectorGlyphKey key{
            glyph->resourceKey,
            unit,
            layoutScaleKey,
        };
        const auto found = vectorGlyphRealizations.find(key);
        if (found != vectorGlyphRealizations.end()) {
            ++impl_->diagnostics.vectorGlyphCacheHits;
            found->second.lastUse = ++impl_->glyphGeometryUseSerial;
            return found->second;
        }
        ++impl_->diagnostics.vectorGlyphCacheMisses;
        const auto buildStart = Clock::now();
        GlyphGeometryResource realization;
        realization.lastUse = ++impl_->glyphGeometryUseSerial;
        auto path = vectorGlyphGeometry(
            device_.d2dFactory(), *glyph, static_cast<float>(unit), device_
        );
        bool hasBounds = false;
        if (path) {
            checkHr(
                path->GetBounds(nullptr, &realization.referenceBounds),
                "ID2D1Geometry::GetBounds(vector glyph)",
                device_
            );
            hasBounds = std::isfinite(realization.referenceBounds.left)
                && std::isfinite(realization.referenceBounds.top)
                && std::isfinite(realization.referenceBounds.right)
                && std::isfinite(realization.referenceBounds.bottom)
                && realization.referenceBounds.right > realization.referenceBounds.left;
        }
        scaleReferenceGeometry(
            path, "ID2D1Factory::CreateTransformedGeometry(scale preview character)"
        );
        if (path && hasBounds) {
            checkHr(
                path->GetBounds(nullptr, &realization.bounds),
                "ID2D1Geometry::GetBounds(scaled preview character)",
                device_
            );
        }
        realization.path = path;
        realization.hasBounds = hasBounds;
        if (path) {
            UINT32 segments = 0;
            if (SUCCEEDED(path->GetSegmentCount(&segments))) {
                realization.segmentCount = segments;
            }
        }
        impl_->diagnostics.vectorGlyphBuildMs += elapsedMs(buildStart);
        return vectorGlyphRealizations
            .emplace(key, std::move(realization))
            .first->second;
    };
    // Cache the resolved DirectWrite glyph identity, not Unicode text: fallback
    // fonts, variation selectors and multi-scalar glyph runs may map the same
    // source spelling to a different outline. Font faces are kept alive by the
    // backend-lifetime face caches, making the raw-pointer portion stable across
    // configure calls. Layout scale is part of the key because the stored path
    // has already been transformed into output coordinates.
    std::map<Impl::TextGlyphKey, GlyphGeometryResource> configureGlyphResources;
    auto &textGlyphRealizations = impl_->resourceCacheEnabled
        ? impl_->textGlyphResources
        : configureGlyphResources;
    auto textRealizationFor = [&] (
        const Microsoft::WRL::ComPtr<IDWriteFontFace> &face,
        const std::vector<UINT16> &glyphs,
        int unit,
        int stretchPct,
        int emboldenDelta
    ) -> GlyphGeometryResource & {
        const Impl::TextGlyphKey key{
            reinterpret_cast<std::uintptr_t>(face.Get()),
            unit,
            layoutScaleKey,
            stretchPct,
            emboldenDelta,
            glyphs,
        };
        const auto found = textGlyphRealizations.find(key);
        if (found != textGlyphRealizations.end()) {
            ++impl_->diagnostics.glyphGeometryCacheHits;
            found->second.lastUse = ++impl_->glyphGeometryUseSerial;
            return found->second;
        }
        ++impl_->diagnostics.glyphGeometryCacheMisses;
        const auto buildStart = Clock::now();
        GlyphGeometryResource resource;
        resource.lastUse = ++impl_->glyphGeometryUseSerial;
        checkHr(
            device_.d2dFactory()->CreatePathGeometry(
                resource.path.ReleaseAndGetAddressOf()
            ),
            "ID2D1Factory::CreatePathGeometry(character)",
            device_
        );
        Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
        checkHr(
            resource.path->Open(sink.ReleaseAndGetAddressOf()),
            "ID2D1PathGeometry::Open(character)",
            device_
        );
        sink->SetFillMode(D2D1_FILL_MODE_WINDING);
        sink->SetSegmentFlags(D2D1_PATH_SEGMENT_FORCE_ROUND_LINE_JOIN);
        const HRESULT outlineResult = face->GetGlyphRunOutline(
            static_cast<float>(unit),
            glyphs.data(),
            nullptr,
            nullptr,
            static_cast<UINT32>(glyphs.size()),
            FALSE,
            FALSE,
            sink.Get()
        );
        const HRESULT closeResult = sink->Close();
        checkHr(outlineResult, "IDWriteFontFace::GetGlyphRunOutline", device_);
        checkHr(closeResult, "ID2D1GeometrySink::Close(character)", device_);
        transformPath(
            resource.path,
            static_cast<float>(stretchPct) / 100.0f,
            1.0f,
            "ID2D1Factory::CreateTransformedGeometry(stretch Latin character)"
        );
        if (emboldenDelta > 0 && unit > 0) {
            // 粗上加粗：与 CPU 侧 font_weight.embolden_glyph_path 同一公式
            // （字号 x 重量差 / 3000，圆帽圆角）对轮廓做 Widen+Union 膨胀，
            // 下游描边/走字/墨迹盒全部消费膨胀后的几何。
            // 连续模拟放大：宽度 = 字号 × Δ / 13800（Δ=300 → 2.17% em，
            // 与 v4.2.x 楷体 faux bold 实测一致）。两后端同一公式。
            const float emboldenWidth =
                emboldenDelta > 0
                    ? static_cast<float>(unit) * static_cast<float>(emboldenDelta) / 13800.0f
                    : 0.0f;
            D2D1_STROKE_STYLE_PROPERTIES properties = D2D1::StrokeStyleProperties();
            properties.startCap = D2D1_CAP_STYLE_ROUND;
            properties.endCap = D2D1_CAP_STYLE_ROUND;
            properties.dashCap = D2D1_CAP_STYLE_ROUND;
            properties.lineJoin = D2D1_LINE_JOIN_ROUND;
            Microsoft::WRL::ComPtr<ID2D1StrokeStyle> emboldenStyle;
            checkHr(
                device_.d2dFactory()->CreateStrokeStyle(
                    properties, nullptr, 0, emboldenStyle.ReleaseAndGetAddressOf()
                ),
                "Create embolden stroke style",
                device_
            );
            Microsoft::WRL::ComPtr<ID2D1PathGeometry> widened;
            checkHr(
                device_.d2dFactory()->CreatePathGeometry(
                    widened.ReleaseAndGetAddressOf()
                ),
                "Create embolden widened geometry",
                device_
            );
            Microsoft::WRL::ComPtr<ID2D1GeometrySink> widenedSink;
            checkHr(
                widened->Open(widenedSink.ReleaseAndGetAddressOf()),
                "Open embolden widened geometry",
                device_
            );
            widenedSink->SetFillMode(D2D1_FILL_MODE_WINDING);
            checkHr(
                resource.path->Widen(
                    emboldenWidth,
                    emboldenStyle.Get(),
                    nullptr,
                    0.5f,
                    widenedSink.Get()
                ),
                "Widen embolden body",
                device_
            );
            checkHr(
                widenedSink->Close(),
                "Close embolden widened geometry",
                device_
            );
            Microsoft::WRL::ComPtr<ID2D1PathGeometry> united;
            checkHr(
                device_.d2dFactory()->CreatePathGeometry(
                    united.ReleaseAndGetAddressOf()
                ),
                "Create embolden united geometry",
                device_
            );
            Microsoft::WRL::ComPtr<ID2D1GeometrySink> unitedSink;
            checkHr(
                united->Open(unitedSink.ReleaseAndGetAddressOf()),
                "Open embolden united geometry",
                device_
            );
            unitedSink->SetFillMode(D2D1_FILL_MODE_WINDING);
            checkHr(
                resource.path->CombineWithGeometry(
                    widened.Get(),
                    D2D1_COMBINE_MODE_UNION,
                    nullptr,
                    0.5f,
                    unitedSink.Get()
                ),
                "Union embolden body",
                device_
            );
            checkHr(
                unitedSink->Close(),
                "Close embolden united geometry",
                device_
            );
            resource.path = united;
        }
        checkHr(
            resource.path->GetBounds(nullptr, &resource.referenceBounds),
            "ID2D1Geometry::GetBounds(character)",
            device_
        );
        resource.hasBounds = std::isfinite(resource.referenceBounds.left)
            && std::isfinite(resource.referenceBounds.top)
            && std::isfinite(resource.referenceBounds.right)
            && std::isfinite(resource.referenceBounds.bottom)
            && resource.referenceBounds.right > resource.referenceBounds.left;
        scaleReferenceGeometry(
            resource.path,
            "ID2D1Factory::CreateTransformedGeometry(scale preview character)"
        );
        if (resource.hasBounds) {
            checkHr(
                resource.path->GetBounds(nullptr, &resource.bounds),
                "ID2D1Geometry::GetBounds(scaled preview character)",
                device_
            );
        }
        impl_->diagnostics.glyphGeometryBuildMs += elapsedMs(buildStart);
        return textGlyphRealizations
            .emplace(key, std::move(resource))
            .first->second;
    };
    auto translatedGeometry = [&](
        ID2D1Geometry *source,
        float offsetX,
        float offsetY,
        const char *operation
    ) -> Microsoft::WRL::ComPtr<ID2D1Geometry> {
        if (source == nullptr) {
            return {};
        }
        const D2D1_MATRIX_3X2_F matrix =
            D2D1::Matrix3x2F::Translation(offsetX, offsetY);
        Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
        checkHr(
            device_.d2dFactory()->CreateTransformedGeometry(
                source, &matrix, transformed.ReleaseAndGetAddressOf()
            ),
            operation,
            device_
        );
        Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
        transformed.As(&geometry);
        return geometry;
    };
    auto cachedWidenedStroke = [&](
        std::map<float, Microsoft::WRL::ComPtr<ID2D1Geometry>> &cache,
        ID2D1Geometry *body,
        float width,
        float offsetX,
        float offsetY,
        const char *operation
    ) -> Microsoft::WRL::ComPtr<ID2D1Geometry> {
        if (body == nullptr || width <= 0.0f) {
            return {};
        }
        auto entry = cache.find(width);
        if (entry == cache.end()) {
            ++impl_->diagnostics.glyphStrokeCacheMisses;
            const auto buildStart = Clock::now();
            entry = cache
                .emplace(
                    width,
                    widenedStrokeGeometry(device_.d2dFactory(), body, width, device_)
                )
                .first;
            impl_->diagnostics.glyphStrokeBuildMs += elapsedMs(buildStart);
        } else {
            ++impl_->diagnostics.glyphStrokeCacheHits;
        }
        if (!entry->second) {
            return {};
        }
        return translatedGeometry(entry->second.Get(), offsetX, offsetY, operation);
    };

    for (std::size_t lineIndex = 0; lineIndex < scene.lines.size();
         ++lineIndex) {
        // 看门狗心跳：场景构建是 configure 响应路径上的主线程长任务，
        // 逐行上报进度让 GUI 续租等待（见 protocol::emitProgress）。
        krok::subtitle::native::protocol::emitProgress(
            QStringLiteral("scene"),
            lineIndex,
            scene.lines.size()
        );
        const TextLine &sourceLine = scene.lines[lineIndex];
        const TextStyle &style = lineIndex < scene.lineStyles.size()
            ? scene.lineStyles[lineIndex]
            : scene.style;
        const auto mainFace = resolveFace(
            style.fontFamily, style.fontWeight, style.italic,
            style.fontFaceWeight, style.fontSimBold, style.fontAxis
        );
        const auto mainMetricsFace = resolveMetricsFace(
            style.fontFamily, style.fontWeight, style.italic,
            style.fontFaceWeight, style.fontSimBold, style.fontAxis
        );
        const auto latinFace = resolveFace(
            style.latinFontFamily.value_or(style.fontFamily),
            style.latinFontWeight.value_or(style.fontWeight),
            style.italic,
            style.latinFontFaceWeight, style.latinFontSimBold, style.latinFontAxis
        );
        const auto rubyFace = resolveFace(
            style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily,
            style.rubyFontWeight,
            style.italic,
            style.rubyFontFaceWeight, style.rubyFontSimBold, style.rubyFontAxis
        );
        const auto rubyLatinFace = resolveFace(
            style.rubyLatinFontFamily.value_or(
                style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily
            ),
            style.rubyLatinFontWeight.value_or(style.rubyFontWeight),
            style.italic,
            style.rubyLatinFontFaceWeight, style.rubyLatinFontSimBold,
            style.rubyLatinFontAxis
        );
        Impl::CachedLine cached;
        cached.style = style;
        cached.startMs = sourceLine.startMs;
        cached.endMs = sourceLine.endMs;
        cached.sourceIndex = sourceLine.sourceIndex;
        cached.sourceLineIndex = sourceLine.sourceLineIndex;
        cached.pageIndex = sourceLine.pageIndex;
        cached.compositeOrder = sourceLine.compositeOrder;
        cached.signalHead = sourceLine.signalHead;
        cached.volumeHead = sourceLine.volumeHead;
        cached.litHead = sourceLine.litHead;
        cached.signalBandJoin = sourceLine.signalBandJoin;
        cached.wipeReverse = sourceLine.wipeReverse;
        cached.centerOverride = sourceLine.centerOverride;
        cached.staticOverlay = sourceLine.staticOverlay;
        cached.fadeInMs = sourceLine.fadeInMs;
        cached.fadeOutMs = sourceLine.fadeOutMs;
        cached.entryAnimation = sourceLine.entryAnimation;
        cached.entryDurationMs = sourceLine.entryDurationMs;
        cached.exitAnimation = sourceLine.exitAnimation;
        cached.exitDurationMs = sourceLine.exitDurationMs;
        cached.karaokeAnimation = sourceLine.karaokeAnimation;
        cached.scanlineEnabled = sourceLine.scanlineEnabled;
        cached.zoomPulseEnabled = sourceLine.zoomPulseEnabled;
        cached.strokeFlashEnabled = sourceLine.strokeFlashEnabled;
        cached.bursts = sourceLine.bursts;
        cached.displayWindows = sourceLine.displayWindows;
        cached.placementWindows = sourceLine.placementWindows;
        DWRITE_FONT_METRICS laneMetrics{};
        mainMetricsFace->GetMetrics(&laneMetrics);
        const int laneFontSize = referenceInt(style.fontSize, 1);
        const float laneMetricUnits = static_cast<float>(std::max<UINT16>(
            laneMetrics.designUnitsPerEm, 1
        ));
        const float laneAscent = static_cast<float>(laneFontSize) * layoutScale
            * static_cast<float>(laneMetrics.ascent) / laneMetricUnits;
        const float laneDescent = static_cast<float>(laneFontSize) * layoutScale
            * static_cast<float>(laneMetrics.descent) / laneMetricUnits;
        const float laneVisualPad = std::ceil((
            std::max(style.strokeWidth / layoutScale, 0.0f)
            + std::max(style.stroke2Width / layoutScale, 0.0f)
        ) * 0.5f) * layoutScale;
        // Shared horizontal lanes use the line style's main font box. Inline
        // role/guide geometry may overflow visually, but must not change the
        // baseline step for only that line.
        cached.legacyLaneHeight = laneAscent + laneDescent + laneVisualPad * 2.0f;
        cached.legacyLaneDescent = laneDescent + laneVisualPad;
        cached.laneFontAscent = laneAscent;
        cached.laneFontDescent = laneDescent;
        if (style.layoutSemantics == "n3_1074") {
            const int fontSize = referenceInt(style.fontSize, 1);
            const int edgeSize = referenceInt(style.strokeWidth, 0);
            const int metricTotal = std::max(
                static_cast<int>(laneMetrics.ascent) + static_cast<int>(laneMetrics.descent), 1
            );
            cached.n3DrawHeight = static_cast<float>(fontSize + edgeSize) * layoutScale;
            cached.n3Descent = static_cast<float>(
                fontSize * static_cast<int>(laneMetrics.descent) / metricTotal
                + edgeSize / 2
            ) * layoutScale;
        }
        if (style.vertical && !sourceLine.rubies.empty()) {
            DWRITE_FONT_METRICS rubyMetrics{};
            resolveMetricsFace(
                style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily,
                style.rubyFontWeight,
                style.italic
            )->GetMetrics(&rubyMetrics);
            const float rubyUnits = static_cast<float>(std::max<UINT16>(
                rubyMetrics.designUnitsPerEm, 1
            ));
            // QFontMetrics::height() uses the face's full ascent + descent,
            // rounded to device pixels.  This differs materially from the em
            // size for fonts such as Meiryo (28 px -> 42 px).
            cached.verticalRubyAllowance = std::max(
                std::round(
                    style.rubyFontSize * static_cast<float>(
                        rubyMetrics.ascent + rubyMetrics.descent
                    ) / rubyUnits
                ) + style.rubyGap,
                0.0f
            );
        }
        cached.lane = style.dualLineLayout
            ? sourceLine.lane % std::max(style.laneCount, 1)
            : 0;
        cached.chars.reserve(sourceLine.chars.size());
        bool lineHasBounds = false;
        float cursor = 0.0f;
        float firstSlotDescent = 0.0f;
        float firstSlotEdge = 0.0f;
        float firstSlotEdge2 = 0.0f;
        float maxDrawHeight = layoutScale;
        bool hasFirstSlot = false;

        for (std::size_t charIndex = 0; charIndex < sourceLine.chars.size(); ++charIndex) {
            const TextChar &sourceChar = sourceLine.chars[charIndex];
            const bool hasCharStyle = sourceChar.styleIndex >= 0
                && sourceChar.styleIndex < static_cast<int>(scene.charStyles.size());
            const TextStyle &charStyle = hasCharStyle
                ? scene.charStyles[static_cast<std::size_t>(sourceChar.styleIndex)]
                : style;
            cached.hasInlineStyles = cached.hasInlineStyles || hasCharStyle;
            cached.hasInlineLaneGeometryOverride =
                cached.hasInlineLaneGeometryOverride
                || (hasCharStyle && (
                    charStyle.fontFamily != style.fontFamily
                    || charStyle.latinFontFamily != style.latinFontFamily
                    || charStyle.fontSize != style.fontSize
                    || charStyle.latinFontSize != style.latinFontSize
                    || charStyle.fontWeight != style.fontWeight
                    || charStyle.latinFontWeight != style.latinFontWeight
                    || charStyle.italic != style.italic
                    || charStyle.strokeWidth != style.strokeWidth
                    || charStyle.stroke2Width != style.stroke2Width
                ));
            const bool vectorGlyph = sourceChar.vectorGlyph != nullptr;
            const bool bitmapGuide = sourceChar.bitmapGuide.has_value();
            const bool latin = !vectorGlyph && !bitmapGuide && isLatinText(sourceChar.text);
            const int stretchPct = latin
                ? std::clamp(charStyle.latinFontStretchPct, 50, 200)
                : 100;
            Microsoft::WRL::ComPtr<IDWriteFontFace> requestedFace = latin
                ? latinFace
                : mainFace;
            if (hasCharStyle) {
                requestedFace = resolveFace(
                    latin
                        ? charStyle.latinFontFamily.value_or(charStyle.fontFamily)
                        : charStyle.fontFamily,
                    latin
                        ? charStyle.latinFontWeight.value_or(charStyle.fontWeight)
                        : charStyle.fontWeight,
                    charStyle.italic,
                    latin
                        ? charStyle.latinFontFaceWeight
                        : charStyle.fontFaceWeight,
                    latin
                        ? charStyle.latinFontSimBold
                        : charStyle.fontSimBold,
                    latin
                        ? charStyle.latinFontAxis
                        : charStyle.fontAxis
                );
            }
            const float fontSize = latin
                ? charStyle.latinFontSize.value_or(charStyle.fontSize)
                : charStyle.fontSize;
            const int unit = referenceInt(fontSize, 1);
            const int edgeSize = referenceInt(charStyle.strokeWidth, 0);
            const int edge2Size = referenceInt(charStyle.stroke2Width, 0);
            cached.maxVisualPad = std::max(
                cached.maxVisualPad,
                std::ceil((
                    std::max(charStyle.strokeWidth / layoutScale, 0.0f)
                    + std::max(charStyle.stroke2Width / layoutScale, 0.0f)
                ) * 0.5f) * layoutScale
            );

            DWRITE_FONT_METRICS fontMetrics{};
            // Vertical box metrics follow the metrics face (static/default
            // instance), matching QFontMetrics on the CPU side; charStyle
            // equals the line style when no inline style is attached.
            resolveMetricsFace(
                latin
                    ? charStyle.latinFontFamily.value_or(charStyle.fontFamily)
                    : charStyle.fontFamily,
                latin
                    ? charStyle.latinFontWeight.value_or(charStyle.fontWeight)
                    : charStyle.fontWeight,
                charStyle.italic,
                latin
                    ? charStyle.latinFontFaceWeight
                    : charStyle.fontFaceWeight,
                latin
                    ? charStyle.latinFontSimBold
                    : charStyle.fontSimBold,
                latin
                    ? charStyle.latinFontAxis
                    : charStyle.fontAxis
            )->GetMetrics(&fontMetrics);
            if (!hasFirstSlot) {
                const int metricTotal = std::max(
                    static_cast<int>(fontMetrics.ascent)
                        + static_cast<int>(fontMetrics.descent),
                    1
                );
                firstSlotDescent = static_cast<float>(
                    unit * static_cast<int>(fontMetrics.descent) / metricTotal
                ) * layoutScale;
                firstSlotEdge = static_cast<float>(edgeSize) * layoutScale;
                firstSlotEdge2 = static_cast<float>(edge2Size) * layoutScale;
                hasFirstSlot = true;
            }
            maxDrawHeight = std::max(
                maxDrawHeight, static_cast<float>(unit + edgeSize) * layoutScale
            );
            // The product's lane boxes remain Painter-compatible. N3's exact
            // glyph bearings/outline are used inside those boxes, while the
            // face's em scale keeps mixed-font baselines close to QFontMetrics.
            const float verticalUnits = static_cast<float>(std::max<UINT16>(
                fontMetrics.designUnitsPerEm,
                1
            ));
            const float charAscent = static_cast<float>(unit) * layoutScale
                * static_cast<float>(fontMetrics.ascent) / verticalUnits;
            const float charDescent = static_cast<float>(unit) * layoutScale
                * static_cast<float>(fontMetrics.descent) / verticalUnits;
            cached.ascent = std::max(cached.ascent, charAscent);
            cached.descent = std::max(cached.descent, charDescent);
            const float boxMetricTotal = static_cast<float>(std::max(
                static_cast<int>(fontMetrics.ascent) + static_cast<int>(fontMetrics.descent),
                1
            ));
            const float charBoxAscent =
                static_cast<float>(unit) * layoutScale
                    * static_cast<float>(fontMetrics.ascent) / boxMetricTotal
                + static_cast<float>(edgeSize) * layoutScale * 0.5f;
            const float charBoxDescent =
                static_cast<float>(unit) * layoutScale
                    * static_cast<float>(fontMetrics.descent) / boxMetricTotal
                + static_cast<float>(edgeSize) * layoutScale * 0.5f;
            cached.n3CharAscent = std::max(cached.n3CharAscent, charBoxAscent);
            cached.n3CharDescent = std::max(cached.n3CharDescent, charBoxDescent);
            cached.hasN3CharBox = true;
            if (!isWhitespaceText(sourceChar.text) && charStyle.affectsRubyAnchor) {
                cached.boxAscent = std::max(cached.boxAscent, charBoxAscent);
                cached.hasRubyAnchor = true;
            }

            std::vector<UINT16> glyphs;
            Microsoft::WRL::ComPtr<IDWriteFontFace> outlineFace;
            Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
            GlyphGeometryResource *glyphResource = nullptr;
            if (vectorGlyph) {
                glyphResource = &vectorRealizationFor(sourceChar.vectorGlyph, unit);
                path = glyphResource->path;
            } else if (!bitmapGuide) {
                if (containsEmoji(sourceChar.text)) {
                    outlineFace = resolveFace(
                        L"Segoe UI Symbol", charStyle.fontWeight, charStyle.italic
                    );
                } else {
                    outlineFace = requestedFace;
                }
                glyphs = glyphIndices(outlineFace.Get(), sourceChar.text);
                if (!validGlyphIndices(glyphs)) {
                    outlineFace = findFallbackFontFace(
                        fontCollection.Get(), sourceChar.text, impl_->fallbackFaces, glyphs
                    );
                }
                if (outlineFace && !glyphs.empty()) {
                    const int emboldenDelta = hasCharStyle
                        ? (latin ? charStyle.latinFontEmbolden : charStyle.fontEmbolden)
                        : (latin ? style.latinFontEmbolden : style.fontEmbolden);
                    glyphResource = &textRealizationFor(
                        outlineFace, glyphs, unit, stretchPct, emboldenDelta
                    );
                    path = glyphResource->path;
                }
            }

            D2D1_RECT_F referenceCharBounds{};
            bool charHasBounds = path != nullptr;
            if (path && glyphResource != nullptr) {
                charHasBounds = glyphResource->hasBounds;
                referenceCharBounds = glyphResource->referenceBounds;
            } else if (path) {
                checkHr(path->GetBounds(nullptr, &referenceCharBounds), "ID2D1Geometry::GetBounds(character)", device_);
                charHasBounds = std::isfinite(referenceCharBounds.left)
                    && std::isfinite(referenceCharBounds.top)
                    && std::isfinite(referenceCharBounds.right)
                    && std::isfinite(referenceCharBounds.bottom)
                    && referenceCharBounds.right > referenceCharBounds.left;
            }

            float layoutWidth = 0.0f;
            float pathOffset = 0.0f;
            D2D1_RECT_F bitmapRect{};
            bool bitmapHasBounds = false;
            if (bitmapGuide) {
                const BitmapGuide &guide = *sourceChar.bitmapGuide;
                // Size the cell by the after-state image first: the SHINTA
                // @Emoji avatar pattern pairs a transparent spacer (before)
                // with the real picture (after); sizing by the spacer would
                // collapse the avatar into a pixel-wide sliver.
                ID2D1Bitmap1 *bitmap = nullptr;
                if (!guide.afterPath.empty()) {
                    bitmap = imageForBitmapGuide(
                        guide.afterPath, guide.afterModifiedMs, guide.afterSize
                    );
                }
                if (bitmap == nullptr) {
                    bitmap = imageForBitmapGuide(
                        guide.beforePath, guide.beforeModifiedMs, guide.beforeSize
                    );
                }
                float contentWidth = 1.0f * layoutScale;
                float contentHeight = std::max(static_cast<float>(unit), 1.0f) * layoutScale;
                if (bitmap != nullptr) {
                    const D2D1_SIZE_U pixelSize = bitmap->GetPixelSize();
                    const float imageWidth = std::max(static_cast<float>(pixelSize.width), 1.0f);
                    const float imageHeight = std::max(static_cast<float>(pixelSize.height), 1.0f);
                    if (guide.fixSize) {
                        contentWidth = imageWidth * layoutScale;
                        contentHeight = imageHeight * layoutScale;
                    } else {
                        contentHeight = std::max(
                            static_cast<float>(
                                std::max(
                                    static_cast<int>(unit * guide.zoomPercent) / 100,
                                    1
                                )
                            ),
                            1.0f
                        ) * layoutScale;
                        contentWidth = std::max(
                            contentHeight * imageWidth / imageHeight,
                            1.0f * layoutScale
                        );
                    }
                }
                const float marginLeft = guide.marginLeft * layoutScale;
                const float marginRight = guide.marginRight * layoutScale;
                const float marginBottom = guide.marginBottom * layoutScale;
                const int metricTotal = std::max(
                    static_cast<int>(fontMetrics.ascent)
                        + static_cast<int>(fontMetrics.descent),
                    1
                );
                const float anchorDescent = (
                    static_cast<float>(
                        unit * static_cast<int>(fontMetrics.descent) / metricTotal
                            + edgeSize / 2
                    ) * layoutScale
                );
                const float bitmapBottom = anchorDescent - marginBottom;
                // Negative margins may deliberately collapse the cell to zero
                // width (N3 colour separation pulls following text over the
                // avatar); the advance only floors at zero so ranges stay
                // ordered, while bitmapRect keeps the overflowing image box.
                layoutWidth = std::max(
                    contentWidth + marginLeft + marginRight,
                    0.0f
                );
                bitmapRect = D2D1::RectF(
                    cursor + marginLeft,
                    bitmapBottom - contentHeight,
                    cursor + marginLeft + contentWidth,
                    bitmapBottom
                );
                bitmapHasBounds = bitmapRect.right > bitmapRect.left
                    && bitmapRect.bottom > bitmapRect.top;
                if (bitmapHasBounds) {
                    extendBounds(cached.bounds, lineHasBounds, bitmapRect);
                }
            } else if (vectorGlyph) {
                layoutWidth = (static_cast<float>(unit)
                    * std::max(sourceChar.vectorGlyph->advanceWidth, 0.0f)
                    / std::max(sourceChar.vectorGlyph->unitsPerEm, 1.0f));
                layoutWidth = std::max(layoutWidth, 1.0f) * layoutScale;
            } else if (charHasBounds) {
                std::vector<DWRITE_GLYPH_METRICS> metrics(glyphs.size());
                // N3 deliberately asks the originally requested face for
                // ordinary fallback metrics. Emoji glyph IDs belong to the
                // Symbol face, however, so querying them on the requested face
                // produces unrelated widths (or E_INVALIDARG).
                IDWriteFontFace *metricFace = containsEmoji(sourceChar.text)
                    ? outlineFace.Get()
                    : requestedFace.Get();
                checkHr(
                    metricFace->GetDesignGlyphMetrics(
                        glyphs.data(),
                        static_cast<UINT32>(glyphs.size()),
                        metrics.data(),
                        FALSE
                    ),
                    "IDWriteFontFace::GetDesignGlyphMetrics(character)",
                    device_
                );
                const int inkWidth = std::max(static_cast<int>(
                    referenceCharBounds.right - referenceCharBounds.left
                ), 0);
                int leftBearing = metrics.front().leftSideBearing;
                int rightBearing = metrics.front().rightSideBearing;
                if (!charStyle.allowBiting) {
                    leftBearing = std::max(leftBearing, 0);
                    rightBearing = std::max(rightBearing, 0);
                }
                const int advance = std::max(static_cast<int>(metrics.front().advanceWidth), 1);
                const int bodyWidth = inkWidth * (leftBearing + advance + rightBearing) / advance;
                layoutWidth = static_cast<float>(
                    std::max(bodyWidth, 0) + edgeSize
                ) * layoutScale;
                const int geometryLeft = inkWidth * leftBearing / advance;
                pathOffset = (-referenceCharBounds.left
                    + static_cast<float>(geometryLeft)
                    + static_cast<float>(edgeSize / 2)) * layoutScale;
            } else if (sourceChar.text == L" ") {
                layoutWidth = static_cast<float>(
                    unit * std::clamp(charStyle.spaceWidthPercent, 10, 100) / 100
                ) * layoutScale;
            } else {
                layoutWidth = static_cast<float>(
                    unit * std::clamp(charStyle.spaceWidthPercent, 10, 100) * 25 / 100 / 10
                    + edgeSize
                ) * layoutScale;
            }

            if (glyphResource == nullptr) {
                scaleReferenceGeometry(
                    path, "ID2D1Factory::CreateTransformedGeometry(scale preview character)"
                );
            }
            D2D1_RECT_F charBounds{};
            if (glyphResource != nullptr) {
                charBounds = glyphResource->bounds;
            } else if (path && charHasBounds) {
                checkHr(
                    path->GetBounds(nullptr, &charBounds),
                    "ID2D1Geometry::GetBounds(scaled preview character)",
                    device_
                );
            }

            D2D1_RECT_F positionedCharBounds{};
            bool positionedHasBounds = false;
            if (bitmapHasBounds) {
                positionedCharBounds = bitmapRect;
                positionedHasBounds = true;
            } else if (path && charHasBounds && glyphResource != nullptr) {
                // Cached glyph: bounds of a pure translation are the
                // translated bounds, so extend them arithmetically and wrap the
                // shared outline in a lazy translation geometry per character.
                D2D1_RECT_F bounds = glyphResource->bounds;
                const float positionDx = cursor + pathOffset;
                bounds.left += positionDx;
                bounds.right += positionDx;
                extendBounds(positionedCharBounds, positionedHasBounds, bounds);
                extendBounds(cached.bounds, lineHasBounds, bounds);
                Microsoft::WRL::ComPtr<ID2D1Geometry> positioned = translatedGeometry(
                    path.Get(),
                    positionDx,
                    0.0f,
                    "ID2D1Factory::CreateTransformedGeometry(position character)"
                );
                cached.geometries.push_back(positioned);
            } else if (path && charHasBounds) {
                const D2D1_MATRIX_3X2_F position = D2D1::Matrix3x2F::Translation(
                    cursor + pathOffset,
                    0.0f
                );
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> positioned;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        path.Get(),
                        &position,
                        positioned.ReleaseAndGetAddressOf()
                    ),
                    "ID2D1Factory::CreateTransformedGeometry(position character)",
                    device_
                );
                D2D1_RECT_F bounds{};
                checkHr(positioned->GetBounds(nullptr, &bounds), "ID2D1Geometry::GetBounds(positioned character)", device_);
                extendBounds(positionedCharBounds, positionedHasBounds, bounds);
                extendBounds(cached.bounds, lineHasBounds, bounds);
                cached.geometries.push_back(positioned);
            }
            const float wipePad = static_cast<float>(edgeSize / 2) * layoutScale;
            cached.chars.push_back(Impl::CachedChar{
                sourceChar.startMs,
                sourceChar.endMs,
                positionedHasBounds ? positionedCharBounds.left - wipePad : cursor,
                positionedHasBounds ? positionedCharBounds.right + wipePad : cursor + layoutWidth,
                cursor,
                cursor + layoutWidth,
                positionedHasBounds ? positionedCharBounds.top : -charAscent,
                positionedHasBounds ? positionedCharBounds.bottom : charDescent,
            });
            cached.chars.back().styleIndex = sourceChar.styleIndex;
            cached.chars.back().bitmapGuide = sourceChar.bitmapGuide;
            cached.chars.back().bitmapRect = bitmapRect;
            cached.chars.back().wipePoints = sourceChar.wipePoints;
            if (cached.chars.back().wipePoints.empty()) {
                cached.chars.back().wipePoints = {
                    WipePoint{sourceChar.startMs, 0.0f},
                    WipePoint{sourceChar.endMs, 1.0f},
                };
            }
            cached.chars.back().boxAscent = charBoxAscent;
            cached.chars.back().pivotX = cursor + layoutWidth * 0.5f;
            cached.chars.back().pivotY = (charDescent - charAscent) * 0.5f;
            if (positionedHasBounds && path) {
                cached.chars.back().geometry = cached.geometries.back();
                if (glyphResource != nullptr) {
                    const float strokeDx = cursor + pathOffset;
                    cached.chars.back().realizationGeometry = path;
                    cached.chars.back().realizationTransform =
                        D2D1::Matrix3x2F::Translation(strokeDx, 0.0f);
                    const float stroke2Width =
                        charStyle.stroke2Width > 0.0f
                            ? std::max(charStyle.strokeWidth, 0.0f)
                                + charStyle.stroke2Width
                            : 0.0f;
                    if (vectorGlyph) {
                        // 矢量字形（导唱符）：描边一律走 Clipper2 预展开
                        // 轮廓（同输入毫秒级，对密集位图描摹 SVG 免疫——
                        // D2D Widen / CreateStrokedGeometryRealization 对
                        // 「密集路径×宽描边」是平方级，实测 3847 段 ≈ 49s）。
                        // 轮廓按 (字形资源, 宽度) 惰性缓存：静态 realization
                        // 按填充语义烘焙它，动画帧直接填充它。与原生描边
                        // 的差异为亚像素级（0.17-0.30 源px/侧，8 符号实测）。
                        const auto outlineFor = [&](
                            float width
                        ) -> Microsoft::WRL::ComPtr<ID2D1Geometry> {
                            if (width <= 0.0f) {
                                return {};
                            }
                            auto &cache = glyphResource->preexpandedStrokes;
                            const auto entry = cache.find(width);
                            if (entry != cache.end()) {
                                return Microsoft::WRL::ComPtr<ID2D1Geometry>{
                                    entry->second
                                };
                            }
                            auto outline = direct2d::strokeOutlineGeometry(
                                device_, path.Get(), width
                            );
                            auto stored = cache
                                .emplace(width, std::move(outline))
                                .first;
                            return Microsoft::WRL::ComPtr<ID2D1Geometry>{
                                stored->second
                            };
                        };
                        cached.chars.back().strokeOutline = outlineFor(
                            std::max(charStyle.strokeWidth, 0.0f)
                        );
                        cached.chars.back().stroke2Outline = outlineFor(
                            stroke2Width
                        );
                        // 动画帧描边：填充预展开轮廓（亚毫秒/帧），替代对
                        // 密集原路径的逐帧 DrawGeometry（~4ms/帧/符号）；
                        // direct-stroke 关闭时也走它而非 D2D Widen（平方级）。
                        if (cached.chars.back().strokeOutline) {
                            cached.chars.back().strokeGeometry =
                                translatedGeometry(
                                    cached.chars.back().strokeOutline.Get(),
                                    strokeDx,
                                    0.0f,
                                    "ID2D1Factory::CreateTransformedGeometry(vector preexpanded stroke)"
                                );
                        }
                        if (cached.chars.back().stroke2Outline) {
                            cached.chars.back().stroke2Geometry =
                                translatedGeometry(
                                    cached.chars.back().stroke2Outline.Get(),
                                    strokeDx,
                                    0.0f,
                                    "ID2D1Factory::CreateTransformedGeometry(vector preexpanded stroke2)"
                                );
                        }
                    } else if (!impl_->dynamicDirectStrokeEnabled) {
                        cached.chars.back().strokeGeometry = cachedWidenedStroke(
                            glyphResource->strokeGeometries,
                            path.Get(),
                            charStyle.strokeWidth,
                            strokeDx,
                            0.0f,
                            "ID2D1Factory::CreateTransformedGeometry(position vector stroke)"
                        );
                        cached.chars.back().stroke2Geometry = cachedWidenedStroke(
                            glyphResource->stroke2Geometries,
                            path.Get(),
                            stroke2Width,
                            strokeDx,
                            0.0f,
                            "ID2D1Factory::CreateTransformedGeometry(position vector stroke2)"
                        );
                    }
                    if (charStyle.strokeWidth > 0.0f
                        && (paintNeedsBodyProtection(charStyle.beforeFillPaint)
                            || paintNeedsBodyProtection(charStyle.afterFillPaint))) {
                        Microsoft::WRL::ComPtr<ID2D1Geometry> protectedStroke;
                        auto &cache = glyphResource->protectedGeometries;
                        const float width = charStyle.strokeWidth;
                        if (width > 0.0f) {
                            auto entry = cache.find(width);
                            if (entry == cache.end()) {
                                ++impl_->diagnostics.glyphStrokeCacheMisses;
                                const auto buildStart = Clock::now();
                                entry = cache
                                    .emplace(
                                        width,
                                        outsideStrokeGeometry(
                                            device_.d2dFactory(),
                                            path.Get(),
                                            width,
                                            device_
                                        )
                                    )
                                    .first;
                                impl_->diagnostics.glyphStrokeBuildMs += elapsedMs(buildStart);
                            } else {
                                ++impl_->diagnostics.glyphStrokeCacheHits;
                            }
                            protectedStroke = translatedGeometry(
                                entry->second.Get(),
                                strokeDx,
                                0.0f,
                                "ID2D1Factory::CreateTransformedGeometry(position vector protected stroke)"
                            );
                            cached.chars.back().protectedRealizationGeometry =
                                entry->second;
                        }
                        cached.chars.back().protectedStrokeGeometry = protectedStroke;
                    }
                } else {
                    if (!impl_->dynamicDirectStrokeEnabled) {
                        cached.chars.back().strokeGeometry = widenedStrokeGeometry(
                            device_.d2dFactory(),
                            cached.chars.back().geometry.Get(),
                            charStyle.strokeWidth,
                            device_
                        );
                        cached.chars.back().stroke2Geometry = widenedStrokeGeometry(
                            device_.d2dFactory(),
                            cached.chars.back().geometry.Get(),
                            charStyle.stroke2Width > 0.0f
                                ? std::max(charStyle.strokeWidth, 0.0f)
                                    + charStyle.stroke2Width
                                : 0.0f,
                            device_
                        );
                    }
                    if (charStyle.strokeWidth > 0.0f
                        && (paintNeedsBodyProtection(charStyle.beforeFillPaint)
                            || paintNeedsBodyProtection(charStyle.afterFillPaint))) {
                        cached.chars.back().protectedStrokeGeometry = outsideStrokeGeometry(
                            device_.d2dFactory(),
                            cached.chars.back().geometry.Get(),
                            charStyle.strokeWidth,
                            device_
                        );
                    }
                }
            }
            if (layoutWidth <= 0.0f) {
                // A cell collapsed to zero width (negative-margin bitmap
                // guides) is fully invisible in layout; it must not consume
                // letter spacing either, or the following text drifts by one
                // spacing relative to the Python layout.
            } else if (charIndex + 1 < sourceLine.chars.size()) {
                // N3's AlignOneLine never lets a sufficiently negative
                // LyricsInterval move the next character back past this one.
                cursor += std::max(layoutWidth + charStyle.letterSpacing, 0.0f);
            } else {
                cursor += layoutWidth;
            }
        }

        if (style.rightToLeft && !style.vertical && !cached.chars.empty()) {
            auto translateGeometry = [&](ID2D1Geometry *source, float offsetX,
                                         Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                                         const char *operation) {
                if (source == nullptr) {
                    target.Reset();
                    return;
                }
                const D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Translation(
                    offsetX, 0.0f
                );
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        source, &matrix, transformed.ReleaseAndGetAddressOf()
                    ),
                    operation,
                    device_
                );
                target = transformed;
            };
            cached.bounds = {};
            cached.geometries.clear();
            lineHasBounds = false;
            for (Impl::CachedChar &ch : cached.chars) {
                const float oldLayoutLeft = ch.layoutLeft;
                const float oldLayoutRight = ch.layoutRight;
                const float newLayoutLeft = cursor - oldLayoutRight;
                const float offsetX = newLayoutLeft - oldLayoutLeft;
                ch.left += offsetX;
                ch.right += offsetX;
                ch.layoutLeft = newLayoutLeft;
                ch.layoutRight = cursor - oldLayoutLeft;
                ch.pivotX += offsetX;
                ch.realizationTransform = ch.realizationTransform
                    * D2D1::Matrix3x2F::Translation(offsetX, 0.0f);
                if (ch.bitmapGuide.has_value()) {
                    ch.bitmapRect.left += offsetX;
                    ch.bitmapRect.right += offsetX;
                }
                translateGeometry(
                    ch.geometry.Get(), offsetX, ch.geometry,
                    "ID2D1Factory::CreateTransformedGeometry(RTL character)"
                );
                translateGeometry(
                    ch.protectedStrokeGeometry.Get(), offsetX,
                    ch.protectedStrokeGeometry,
                    "ID2D1Factory::CreateTransformedGeometry(RTL protected stroke)"
                );
                translateGeometry(
                    ch.strokeGeometry.Get(), offsetX, ch.strokeGeometry,
                    "ID2D1Factory::CreateTransformedGeometry(RTL stroke)"
                );
                translateGeometry(
                    ch.stroke2Geometry.Get(), offsetX, ch.stroke2Geometry,
                    "ID2D1Factory::CreateTransformedGeometry(RTL stroke2)"
                );
                if (ch.geometry) {
                    D2D1_RECT_F bounds{};
                    checkHr(
                        ch.geometry->GetBounds(nullptr, &bounds),
                        "ID2D1Geometry::GetBounds(RTL character)",
                        device_
                    );
                    extendBounds(cached.bounds, lineHasBounds, bounds);
                    cached.geometries.push_back(ch.geometry);
                } else if (ch.bitmapGuide.has_value()) {
                    extendBounds(cached.bounds, lineHasBounds, ch.bitmapRect);
                }
            }
        }

        if (style.vertical && !cached.chars.empty()) {
            DWRITE_FONT_METRICS verticalMetrics{};
            mainMetricsFace->GetMetrics(&verticalMetrics);
            const float designUnits = static_cast<float>(std::max<UINT16>(
                verticalMetrics.designUnitsPerEm, 1
            ));
            const float cellWidth = std::max(style.fontSize, 1.0f);
            const float cellHeight = std::max(
                style.fontSize
                    * static_cast<float>(verticalMetrics.ascent + verticalMetrics.descent)
                    / designUnits,
                1.0f
            );
            const float verticalAscent = style.fontSize
                * static_cast<float>(verticalMetrics.ascent) / designUnits;
            cached.geometries.clear();
            cached.bounds = {};
            lineHasBounds = false;
            auto transformVertical = [&](ID2D1Geometry *source,
                                         const D2D1_MATRIX_3X2_F &matrix,
                                         Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                                         const char *operation) {
                if (source == nullptr) {
                    target.Reset();
                    return;
                }
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        source, &matrix, transformed.ReleaseAndGetAddressOf()
                    ),
                    operation,
                    device_
                );
                target = transformed;
            };
            for (std::size_t index = 0; index < cached.chars.size(); ++index) {
                Impl::CachedChar &ch = cached.chars[index];
                const float cellTop = static_cast<float>(index) * cellHeight;
                // Painter advances the vertical wipe through every fixed cell,
                // including spaces and other glyphs with no outline geometry.
                ch.top = cellTop;
                ch.bottom = cellTop + cellHeight;
                if (ch.bitmapGuide.has_value()) {
                    const float bitmapWidth = std::max(
                        ch.bitmapRect.right - ch.bitmapRect.left, 1.0f
                    );
                    const float bitmapHeight = std::max(
                        ch.bitmapRect.bottom - ch.bitmapRect.top, 1.0f
                    );
                    ch.bitmapRect = D2D1::RectF(
                        -bitmapWidth * 0.5f,
                        cellTop + (cellHeight - bitmapHeight) * 0.5f,
                        bitmapWidth * 0.5f,
                        cellTop + (cellHeight + bitmapHeight) * 0.5f
                    );
                    ch.left = ch.bitmapRect.left;
                    ch.right = ch.bitmapRect.right;
                    ch.top = ch.bitmapRect.top;
                    ch.bottom = ch.bitmapRect.bottom;
                }
                const auto [offsetX, offsetY] = verticalGlyphOffset(
                    sourceLine.chars[index].text, cellWidth, cellHeight
                );
                const bool vectorGlyph = sourceLine.chars[index].vectorGlyph != nullptr;
                D2D1_MATRIX_3X2_F matrix{};
                if (vectorGlyph && ch.geometry) {
                    D2D1_RECT_F vectorBounds{};
                    checkHr(
                        ch.geometry->GetBounds(nullptr, &vectorBounds),
                        "ID2D1Geometry::GetBounds(vertical vector glyph)",
                        device_
                    );
                    matrix = D2D1::Matrix3x2F::Translation(
                        -(vectorBounds.left + vectorBounds.right) * 0.5f,
                        cellTop + cellHeight * 0.5f
                            - (vectorBounds.top + vectorBounds.bottom) * 0.5f
                    );
                } else {
                    matrix = D2D1::Matrix3x2F::Translation(
                        -ch.pivotX + offsetX,
                        cellTop + verticalAscent + offsetY
                    );
                }
                if (!vectorGlyph && verticalRotates(sourceLine.chars[index].text)) {
                    matrix = matrix * D2D1::Matrix3x2F::Rotation(
                        90.0f, D2D1::Point2F(0.0f, cellTop + cellHeight * 0.5f)
                    );
                }
                ch.realizationTransform = ch.realizationTransform * matrix;
                transformVertical(
                    ch.geometry.Get(), matrix, ch.geometry,
                    "ID2D1Factory::CreateTransformedGeometry(vertical character)"
                );
                transformVertical(
                    ch.protectedStrokeGeometry.Get(), matrix,
                    ch.protectedStrokeGeometry,
                    "ID2D1Factory::CreateTransformedGeometry(vertical protected stroke)"
                );
                transformVertical(
                    ch.strokeGeometry.Get(), matrix, ch.strokeGeometry,
                    "ID2D1Factory::CreateTransformedGeometry(vertical stroke)"
                );
                transformVertical(
                    ch.stroke2Geometry.Get(), matrix, ch.stroke2Geometry,
                    "ID2D1Factory::CreateTransformedGeometry(vertical stroke2)"
                );
                if (ch.geometry) {
                    D2D1_RECT_F bounds{};
                    checkHr(
                        ch.geometry->GetBounds(nullptr, &bounds),
                        "ID2D1Geometry::GetBounds(vertical character)",
                        device_
                    );
                    const TextStyle &charStyle = ch.styleIndex >= 0
                        && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                        ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                        : style;
                    const float wipePad = static_cast<float>(
                        std::max(static_cast<int>(charStyle.strokeWidth), 0) / 2
                    );
                    ch.left = bounds.left - wipePad;
                    ch.right = bounds.right + wipePad;
                    extendBounds(cached.bounds, lineHasBounds, bounds);
                    cached.geometries.push_back(ch.geometry);
                } else if (ch.bitmapGuide.has_value()) {
                    extendBounds(cached.bounds, lineHasBounds, ch.bitmapRect);
                }
                ch.layoutLeft = -cellWidth * 0.5f;
                ch.layoutRight = cellWidth * 0.5f;
                ch.pivotX = 0.0f;
                ch.pivotY = cellTop + cellHeight * 0.5f;
            }
            cached.fillBounds = D2D1::RectF(
                -cellWidth * 0.5f,
                0.0f,
                cellWidth * 0.5f,
                cellHeight * static_cast<float>(cached.chars.size())
            );
        } else if (hasFirstSlot) {
            // Glyph-ink vertical anchor, mirroring the Painter's ink-based
            // n3_main_fill_rect: the metric em box systematically displaced
            // MilleFeuille / vertical-gradient bands on faces whose ink is
            // taller than the em, and the wrapping band texture then painted
            // glyph tops in the bottom band colour.  Union the placed glyph
            // bounds (bitmap guides through their content box) and pad
            // symmetrically by the maximal stroke extent so widened outlines
            // do not wrap either; lines without visible ink keep the metric
            // box.
            bool inkHasBounds = false;
            D2D1_RECT_F inkBounds{};
            for (const Impl::CachedChar &ch : cached.chars) {
                if (ch.bitmapGuide.has_value()) {
                    if (ch.bitmapRect.right > ch.bitmapRect.left
                        && ch.bitmapRect.bottom > ch.bitmapRect.top) {
                        extendBounds(inkBounds, inkHasBounds, ch.bitmapRect);
                    }
                } else if (ch.geometry) {
                    extendBounds(
                        inkBounds,
                        inkHasBounds,
                        D2D1::RectF(ch.left, ch.top, ch.right, ch.bottom)
                    );
                }
            }
            if (inkHasBounds) {
                cached.fillBounds = D2D1::RectF(
                    0.0f,
                    inkBounds.top - cached.maxVisualPad,
                    std::max(cursor, 1.0f),
                    inkBounds.bottom + cached.maxVisualPad
                );
            } else {
                const float drawBottom = firstSlotDescent + std::floor(
                    firstSlotEdge / layoutScale / 2.0f
                ) * layoutScale;
                const float inset = std::floor(
                    (firstSlotEdge + firstSlotEdge2) / layoutScale / 2.0f
                ) * layoutScale;
                cached.fillBounds = D2D1::RectF(
                    0.0f,
                    drawBottom - maxDrawHeight + inset,
                    std::max(cursor, 1.0f),
                    std::max(drawBottom - inset, drawBottom - maxDrawHeight + inset + layoutScale)
                );
            }
        }

        if (!cached.hasRubyAnchor) {
            for (const TextRuby &ruby : sourceLine.rubies) {
                const int first = std::max(ruby.firstCharIndex, 0);
                const int last = std::min(
                    ruby.lastCharIndex,
                    static_cast<int>(cached.chars.size()) - 1
                );
                for (int index = first; index <= last; ++index) {
                    cached.boxAscent = std::max(
                        cached.boxAscent,
                        cached.chars[static_cast<std::size_t>(index)].boxAscent
                    );
                }
            }
        }

        for (const TextRuby &sourceRuby : sourceLine.rubies) {
            if (sourceRuby.units.empty()
                || sourceRuby.firstCharIndex < 0
                || sourceRuby.lastCharIndex < sourceRuby.firstCharIndex
                || sourceRuby.lastCharIndex >= static_cast<int>(cached.chars.size())) {
                continue;
            }
            const bool hasRubyStyle = sourceRuby.styleIndex >= 0
                && sourceRuby.styleIndex < static_cast<int>(scene.charStyles.size());
            const TextStyle &rubyStyle = hasRubyStyle
                ? scene.charStyles[static_cast<std::size_t>(sourceRuby.styleIndex)]
                : style;
            const bool rubyIsLatin = std::all_of(
                sourceRuby.units.begin(),
                sourceRuby.units.end(),
                [](const RubyUnit &unit) { return isLatinText(unit.text); }
            );
            // CPU 对齐（engine/ruby/style.py ruby_script_stroke_style）：英数
            // 读音的描边（布局预留、几何、绘制、逐对净空）走 ruby-latin 轨；
            // 投影已把未设/非正值解析为与日文轨同值，按读音脚本二选一即可。
            const float rubyStrokeWidthFx = rubyIsLatin
                ? rubyStyle.rubyLatinStrokeWidth
                : rubyStyle.rubyStrokeWidth;
            const float rubyStroke2WidthFx = rubyIsLatin
                ? rubyStyle.rubyLatinStroke2Width
                : rubyStyle.rubyStroke2Width;
            const auto selectedRubyFace = hasRubyStyle
                ? resolveFace(
                    rubyStyle.rubyFontFamily.empty()
                        ? rubyStyle.fontFamily
                        : rubyStyle.rubyFontFamily,
                    rubyStyle.rubyFontWeight,
                    rubyStyle.italic,
                    rubyStyle.rubyFontFaceWeight, rubyStyle.rubyFontSimBold,
                    rubyStyle.rubyFontAxis
                )
                : rubyFace;
            const auto selectedRubyMetricsFace = hasRubyStyle
                ? resolveMetricsFace(
                    rubyStyle.rubyFontFamily.empty()
                        ? rubyStyle.fontFamily
                        : rubyStyle.rubyFontFamily,
                    rubyStyle.rubyFontWeight,
                    rubyStyle.italic,
                    rubyStyle.rubyFontFaceWeight, rubyStyle.rubyFontSimBold,
                    rubyStyle.rubyFontAxis
                )
                : resolveMetricsFace(
                    style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily,
                    style.rubyFontWeight,
                    style.italic,
                    style.rubyFontFaceWeight, style.rubyFontSimBold,
                    style.rubyFontAxis
                );
            const auto selectedRubyLatinFace = hasRubyStyle
                ? resolveFace(
                    rubyStyle.rubyLatinFontFamily.value_or(
                        rubyStyle.rubyFontFamily.empty()
                            ? rubyStyle.fontFamily
                            : rubyStyle.rubyFontFamily
                    ),
                    rubyStyle.rubyLatinFontWeight.value_or(rubyStyle.rubyFontWeight),
                    rubyStyle.italic
                )
                : rubyLatinFace;
            struct RubyGlyph {
                const RubyUnit *source = nullptr;
                Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
                GlyphGeometryResource *resource = nullptr;
                D2D1_RECT_F bounds{};
                float layoutWidth = 0.0f;
                float pathOffset = 0.0f;
                // 墨迹盒在单元盒内的左右边（输出像素），供相邻单元的
                // 描边净空下限使用；空墨迹单元退化为 pathOffset 处。
                float inkLeft = 0.0f;
                float inkRight = 0.0f;
            };
            std::vector<RubyGlyph> rubyGlyphs;
            rubyGlyphs.reserve(sourceRuby.units.size());
            float naturalWidth = 0.0f;
            float rubyBoxDescent = 0.0f;
            const int rubyEdgeSize = referenceInt(rubyStrokeWidthFx, 0);
            const int rubyAnchorEdgeSize = rubyEdgeSize;

            for (const RubyUnit &sourceUnit : sourceRuby.units) {
                const bool latin = isLatinText(sourceUnit.text);
                const int stretchPct = latin
                    ? std::clamp(rubyStyle.rubyLatinFontStretchPct, 50, 200)
                    : 100;
                const auto &measureFace = latin
                    ? selectedRubyLatinFace
                    : selectedRubyFace;
                const auto &drawingFace = measureFace;
                const float measureFontSize = latin
                    ? rubyStyle.rubyLatinFontSize.value_or(rubyStyle.rubyFontSize)
                    : rubyStyle.rubyFontSize;
                const float drawingFontSize = measureFontSize;
                const int measureUnit = referenceInt(measureFontSize, 1);
                const int drawingUnit = referenceInt(drawingFontSize, 1);
                DWRITE_FONT_METRICS fontMetrics{};
                // Box ratios follow the metrics face (static default
                // instance) to match the CPU renderer's QFontMetrics.
                resolveMetricsFace(
                    latin
                        ? rubyStyle.rubyLatinFontFamily.value_or(
                              rubyStyle.rubyFontFamily.empty()
                                  ? rubyStyle.fontFamily
                                  : rubyStyle.rubyFontFamily)
                        : (rubyStyle.rubyFontFamily.empty()
                              ? rubyStyle.fontFamily
                              : rubyStyle.rubyFontFamily),
                    latin
                        ? rubyStyle.rubyLatinFontWeight.value_or(rubyStyle.rubyFontWeight)
                        : rubyStyle.rubyFontWeight,
                    rubyStyle.italic,
                    latin
                        ? rubyStyle.rubyLatinFontFaceWeight
                        : rubyStyle.rubyFontFaceWeight,
                    latin
                        ? rubyStyle.rubyLatinFontSimBold
                        : rubyStyle.rubyFontSimBold,
                    latin
                        ? rubyStyle.rubyLatinFontAxis
                        : rubyStyle.rubyFontAxis
                )->GetMetrics(&fontMetrics);
                const float boxMetricTotal = static_cast<float>(std::max(
                    static_cast<int>(fontMetrics.ascent) + static_cast<int>(fontMetrics.descent),
                    1
                ));
                rubyBoxDescent = std::max(
                    rubyBoxDescent,
                    (static_cast<float>(drawingUnit)
                        * static_cast<float>(fontMetrics.descent) / boxMetricTotal
                        + static_cast<float>(rubyAnchorEdgeSize) * 0.5f)
                        * layoutScale
                );

                std::vector<UINT16> glyphs = glyphIndices(drawingFace.Get(), sourceUnit.text);
                Microsoft::WRL::ComPtr<IDWriteFontFace> outlineFace = drawingFace;
                if (!validGlyphIndices(glyphs)) {
                    outlineFace = findFallbackFontFace(
                        fontCollection.Get(), sourceUnit.text, impl_->fallbackFaces, glyphs
                    );
                }
                Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
                GlyphGeometryResource *glyphResource = nullptr;
                if (outlineFace && !glyphs.empty()) {
                    const int emboldenDelta = latin
                        ? rubyStyle.rubyLatinFontEmbolden
                        : rubyStyle.rubyFontEmbolden;
                    glyphResource = &textRealizationFor(
                        outlineFace, glyphs, drawingUnit, stretchPct, emboldenDelta
                    );
                    path = glyphResource->path;
                }

                D2D1_RECT_F referenceRubyBounds{};
                bool hasBounds = path != nullptr;
                if (glyphResource != nullptr) {
                    referenceRubyBounds = glyphResource->referenceBounds;
                    hasBounds = glyphResource->hasBounds;
                }
                RubyGlyph glyph;
                glyph.source = &sourceUnit;
                glyph.resource = glyphResource;
                if (hasBounds) {
                    std::vector<UINT16> measureGlyphs = glyphIndices(
                        measureFace.Get(), sourceUnit.text
                    );
                    Microsoft::WRL::ComPtr<IDWriteFontFace> metricFace = measureFace;
                    if (!validGlyphIndices(measureGlyphs)) {
                        metricFace = findFallbackFontFace(
                            fontCollection.Get(), sourceUnit.text,
                            impl_->fallbackFaces, measureGlyphs
                        );
                    }
                    std::vector<DWRITE_GLYPH_METRICS> metrics(measureGlyphs.size());
                    checkHr(
                        metricFace->GetDesignGlyphMetrics(
                            measureGlyphs.data(),
                            static_cast<UINT32>(measureGlyphs.size()),
                            metrics.data(),
                            FALSE
                        ),
                        "IDWriteFontFace::GetDesignGlyphMetrics(ruby character)",
                        device_
                    );
                    const int drawingInkWidth = std::max(
                        static_cast<int>(
                            referenceRubyBounds.right - referenceRubyBounds.left
                        ), 0
                    );
                    const int inkWidth = drawingUnit > 0
                        ? drawingInkWidth * measureUnit / drawingUnit
                        : drawingInkWidth;
                    int leftBearing = metrics.front().leftSideBearing;
                    int rightBearing = metrics.front().rightSideBearing;
                    if (!rubyStyle.allowBiting) {
                        leftBearing = std::max(leftBearing, 0);
                        rightBearing = std::max(rightBearing, 0);
                    }
                    const int advance = std::max(static_cast<int>(metrics.front().advanceWidth), 1);
                    const int bodyWidth = inkWidth * (leftBearing + advance + rightBearing) / advance;
                    glyph.layoutWidth = static_cast<float>(
                        std::max(bodyWidth, 0) + rubyEdgeSize
                    ) * layoutScale;
                    const int geometryLeft = inkWidth * leftBearing / advance;
                    glyph.pathOffset = (-referenceRubyBounds.left
                        + static_cast<float>(geometryLeft)
                        + static_cast<float>(rubyEdgeSize / 2)) * layoutScale;
                    glyph.inkLeft = glyph.pathOffset
                        + referenceRubyBounds.left * layoutScale;
                    glyph.inkRight = glyph.pathOffset
                        + referenceRubyBounds.right * layoutScale;
                } else if (sourceUnit.text == L" ") {
                    glyph.layoutWidth = static_cast<float>(
                        measureUnit * std::clamp(rubyStyle.spaceWidthPercent, 10, 100) / 100
                            + rubyEdgeSize
                    ) * layoutScale;
                } else {
                    glyph.layoutWidth = static_cast<float>(
                        measureUnit * std::clamp(rubyStyle.spaceWidthPercent, 10, 100) * 25 / 100 / 10
                            + rubyEdgeSize
                    ) * layoutScale;
                }
                glyph.geometry = path;
                if (path && hasBounds) {
                    glyph.bounds = glyphResource->bounds;
                }
                naturalWidth += glyph.layoutWidth;
                rubyGlyphs.push_back(std::move(glyph));
            }
            if (rubyGlyphs.empty()) {
                continue;
            }

            const float targetLeft = std::min(
                cached.chars[static_cast<std::size_t>(sourceRuby.firstCharIndex)].layoutLeft,
                cached.chars[static_cast<std::size_t>(sourceRuby.lastCharIndex)].layoutLeft
            );
            const float targetRight = std::max(
                cached.chars[static_cast<std::size_t>(sourceRuby.firstCharIndex)].layoutRight,
                cached.chars[static_cast<std::size_t>(sourceRuby.lastCharIndex)].layoutRight
            );
            const float targetWidth = std::max(
                targetRight - targetLeft, layoutScale
            );
            const bool centered = rubyStyle.rubyAlignment == "center"
                || (rubyStyle.rubyAlignment != "equal_space" && (
                    rubyAutoCenterLayout(sourceRuby.baseText)
                    || rubyAutoCenterLayout(sourceRuby.reading)
                ));
            float baseGap = rubyStyle.rubyInterval;
            if (!centered && rubyGlyphs.size() > 1) {
                const float slots = targetWidth <= naturalWidth
                    ? static_cast<float>(rubyGlyphs.size() - 1)
                    : static_cast<float>(rubyGlyphs.size() + 1);
                baseGap = std::max(
                    (targetWidth - naturalWidth) / std::max(slots, 1.0f),
                    rubyStyle.rubyInterval
                );
            }
            std::vector<float> pairGaps(
                rubyGlyphs.empty() ? 0 : rubyGlyphs.size() - 1, baseGap
            );
            // Pair clearance floor (mirrors engine/ruby/layout.py
            // ruby_pair_gap_floors): with a non-negative RubyInterval each
            // adjacent pair keeps enough room for both stroke layers so fat
            // outlines cannot cross on bearing-less glyphs (narrow Latin
            // letters).  A negative interval means deliberate manual
            // squeezing and disables the floor entirely.
            if (rubyStyle.rubyInterval >= 0.0f && rubyGlyphs.size() > 1) {
                const float outlineNeed =
                    std::max(rubyStrokeWidthFx, 0.0f)
                    + std::max(rubyStroke2WidthFx, 0.0f);
                for (std::size_t pairIndex = 0;
                     pairIndex + 1 < rubyGlyphs.size(); ++pairIndex) {
                    // pairIndex addresses the logical pair (k, k+1); in RTL
                    // the visually-left glyph is the higher logical index.
                    const std::size_t leftIndex = style.rightToLeft
                        ? pairIndex + 1
                        : pairIndex;
                    const std::size_t rightIndex = style.rightToLeft
                        ? pairIndex
                        : pairIndex + 1;
                    const RubyGlyph &leftGlyph = rubyGlyphs[leftIndex];
                    const RubyGlyph &rightGlyph = rubyGlyphs[rightIndex];
                    const float existing =
                        std::max(
                            leftGlyph.layoutWidth - leftGlyph.inkRight, 0.0f
                        )
                        + std::max(rightGlyph.inkLeft, 0.0f);
                    pairGaps[pairIndex] = std::max(
                        pairGaps[pairIndex], outlineNeed - existing
                    );
                }
            }
            float gapsTotal = 0.0f;
            for (float pairGap : pairGaps) {
                gapsTotal += pairGap;
            }
            const float contentWidth = naturalWidth + gapsTotal;
            float rubyCursor = targetLeft + (targetWidth - contentWidth) * 0.5f;
            if (centered || rubyGlyphs.size() == 1) {
                rubyCursor = targetLeft
                    + static_cast<float>(static_cast<int>(
                        (targetWidth - contentWidth) / layoutScale
                    ) / 2) * layoutScale;
            }
            std::vector<float> rubyOrigins(rubyGlyphs.size(), rubyCursor);
            float layoutCursor = rubyCursor;
            for (std::size_t visualIndex = 0;
                 visualIndex < rubyGlyphs.size(); ++visualIndex) {
                const std::size_t logicalIndex = style.rightToLeft
                    ? rubyGlyphs.size() - visualIndex - 1
                    : visualIndex;
                rubyOrigins[logicalIndex] = (centered || rubyGlyphs.size() == 1)
                    ? layoutCursor
                    : static_cast<float>(static_cast<int>(
                        layoutCursor / layoutScale
                    )) * layoutScale;
                layoutCursor += rubyGlyphs[logicalIndex].layoutWidth;
                if (visualIndex + 1 < rubyGlyphs.size()) {
                    // Gap between this visual unit and the next one maps to
                    // the logical pair (min(logicalIndex, neighbour), +1).
                    const std::size_t pairIndex = style.rightToLeft
                        ? logicalIndex - 1
                        : logicalIndex;
                    layoutCursor += pairGaps[pairIndex];
                }
            }

            Impl::CachedRuby ruby;
            ruby.startMs = sourceRuby.startMs;
            ruby.endMs = sourceRuby.endMs;
            ruby.styleIndex = sourceRuby.styleIndex;
            ruby.latin = rubyIsLatin;
            ruby.transitionCharIndex = sourceRuby.firstCharIndex;
            ruby.firstCharIndex = sourceRuby.firstCharIndex;
            ruby.lastCharIndex = sourceRuby.lastCharIndex;
            ruby.baselineOffset = -cached.boxAscent - style.rubyGap - rubyBoxDescent;
            DWRITE_FONT_METRICS rubyFillMetrics{};
            resolveMetricsFace(
                rubyIsLatin
                    ? rubyStyle.rubyLatinFontFamily.value_or(
                          rubyStyle.rubyFontFamily.empty()
                              ? rubyStyle.fontFamily
                              : rubyStyle.rubyFontFamily)
                    : (rubyStyle.rubyFontFamily.empty()
                          ? rubyStyle.fontFamily
                          : rubyStyle.rubyFontFamily),
                rubyIsLatin
                    ? rubyStyle.rubyLatinFontWeight.value_or(rubyStyle.rubyFontWeight)
                    : rubyStyle.rubyFontWeight,
                rubyStyle.italic,
                rubyIsLatin
                    ? rubyStyle.rubyLatinFontFaceWeight
                    : rubyStyle.rubyFontFaceWeight,
                rubyIsLatin
                    ? rubyStyle.rubyLatinFontSimBold
                    : rubyStyle.rubyFontSimBold,
                rubyIsLatin
                    ? rubyStyle.rubyLatinFontAxis
                    : rubyStyle.rubyFontAxis
            )->GetMetrics(&rubyFillMetrics);
            const int rubyMetricTotal = std::max(
                static_cast<int>(rubyFillMetrics.ascent)
                    + static_cast<int>(rubyFillMetrics.descent),
                1
            );
            const int rubyFillSize = referenceInt(
                rubyIsLatin
                    ? rubyStyle.rubyLatinFontSize.value_or(rubyStyle.rubyFontSize)
                    : rubyStyle.rubyFontSize,
                1
            );
            const int rubyFillDescent = rubyFillSize
                * static_cast<int>(rubyFillMetrics.descent) / rubyMetricTotal;
            ruby.pivotX = rubyCursor + contentWidth * 0.5f;
            ruby.pivotY = ruby.baselineOffset
                + static_cast<float>(rubyFillDescent) * layoutScale
                - static_cast<float>(rubyFillSize) * layoutScale * 0.5f;
            const int rubyDrawEdge = referenceInt(rubyStrokeWidthFx, 0);
            const int rubyDrawEdge2 = referenceInt(rubyStroke2WidthFx, 0);
            const float rubyDrawBottom = ruby.baselineOffset
                + static_cast<float>(rubyFillDescent + rubyDrawEdge / 2) * layoutScale;
            const float rubyInset = static_cast<float>(
                (rubyDrawEdge + rubyDrawEdge2) / 2
            ) * layoutScale;
            ruby.fillBounds = D2D1::RectF(
                targetLeft,
                rubyDrawBottom - static_cast<float>(rubyFillSize + rubyDrawEdge) * layoutScale
                    + rubyInset,
                targetRight,
                std::max(
                    rubyDrawBottom - rubyInset,
                    rubyDrawBottom - static_cast<float>(rubyFillSize + rubyDrawEdge)
                        + rubyInset + 1.0f
                )
            );
            bool rubyHasBounds = false;
            for (std::size_t unitIndex = 0; unitIndex < rubyGlyphs.size(); ++unitIndex) {
                RubyGlyph &glyph = rubyGlyphs[unitIndex];
                const float origin = rubyOrigins[unitIndex];
                D2D1_RECT_F positionedBounds{};
                bool positionedHasBounds = false;
                if (glyph.geometry) {
                    const float positionDx = origin + glyph.pathOffset;
                    const float positionDy = ruby.baselineOffset;
                    Microsoft::WRL::ComPtr<ID2D1Geometry> positioned = translatedGeometry(
                        glyph.geometry.Get(), positionDx, positionDy,
                        "ID2D1Factory::CreateTransformedGeometry(position ruby character)"
                    );
                    positionedBounds = glyph.bounds;
                    positionedBounds.left += positionDx;
                    positionedBounds.right += positionDx;
                    positionedBounds.top += positionDy;
                    positionedBounds.bottom += positionDy;
                    positionedHasBounds = positionedBounds.right > positionedBounds.left;
                    if (positionedHasBounds) {
                        extendBounds(ruby.bounds, rubyHasBounds, positionedBounds);
                    }
                    ruby.geometries.push_back(positioned);
                    ruby.strokeGeometries.push_back(
                        !impl_->dynamicDirectStrokeEnabled
                            ? cachedWidenedStroke(
                                glyph.resource->strokeGeometries,
                                glyph.geometry.Get(), rubyStrokeWidthFx,
                                positionDx, positionDy,
                                "ID2D1Factory::CreateTransformedGeometry(position ruby stroke)"
                            )
                            : nullptr
                    );
                    const float stroke2Width = rubyStroke2WidthFx > 0.0f
                        ? std::max(rubyStrokeWidthFx, 0.0f)
                            + rubyStroke2WidthFx
                        : 0.0f;
                    ruby.stroke2Geometries.push_back(
                        !impl_->dynamicDirectStrokeEnabled
                            ? cachedWidenedStroke(
                                glyph.resource->stroke2Geometries,
                                glyph.geometry.Get(), stroke2Width,
                                positionDx, positionDy,
                                "ID2D1Factory::CreateTransformedGeometry(position ruby stroke2)"
                            )
                            : nullptr
                    );
                    if (rubyStrokeWidthFx > 0.0f
                        && (paintNeedsBodyProtection(rubyStyle.rubyBeforeFillPaint)
                            || paintNeedsBodyProtection(rubyStyle.rubyAfterFillPaint))) {
                        auto &cache = glyph.resource->protectedGeometries;
                        const float width = rubyStrokeWidthFx;
                        auto entry = cache.find(width);
                        if (entry == cache.end()) {
                            ++impl_->diagnostics.glyphStrokeCacheMisses;
                            const auto buildStart = Clock::now();
                            entry = cache.emplace(
                                width,
                                outsideStrokeGeometry(
                                    device_.d2dFactory(), glyph.geometry.Get(),
                                    width, device_
                                )
                            ).first;
                            impl_->diagnostics.glyphStrokeBuildMs += elapsedMs(buildStart);
                        } else {
                            ++impl_->diagnostics.glyphStrokeCacheHits;
                        }
                        ruby.protectedStrokeGeometries.push_back(
                            translatedGeometry(
                                entry->second.Get(), positionDx, positionDy,
                                "ID2D1Factory::CreateTransformedGeometry(position ruby protected stroke)"
                            )
                        );
                    } else {
                        ruby.protectedStrokeGeometries.push_back({});
                    }
                }
                const float wipePad = static_cast<float>(rubyEdgeSize / 2);
                ruby.chars.push_back(Impl::CachedChar{
                    glyph.source->startMs,
                    glyph.source->endMs,
                    positionedHasBounds ? positionedBounds.left - wipePad : origin,
                    positionedHasBounds
                        ? positionedBounds.right + wipePad
                        : origin + glyph.layoutWidth,
                    origin,
                    origin + glyph.layoutWidth,
                    positionedHasBounds ? positionedBounds.top : ruby.bounds.top,
                    positionedHasBounds ? positionedBounds.bottom : ruby.bounds.bottom,
                });
                if (glyph.geometry && glyph.resource != nullptr) {
                    ruby.chars.back().realizationGeometry = glyph.geometry;
                    ruby.chars.back().realizationTransform =
                        D2D1::Matrix3x2F::Translation(
                            origin + glyph.pathOffset,
                            ruby.baselineOffset
                        );
                    const auto protectedEntry =
                        glyph.resource->protectedGeometries.find(
                            rubyStrokeWidthFx
                        );
                    if (protectedEntry
                        != glyph.resource->protectedGeometries.end()) {
                        ruby.chars.back().protectedRealizationGeometry =
                            protectedEntry->second;
                    }
                }
                ruby.chars.back().pivotX = origin + glyph.layoutWidth * 0.5f;
                ruby.chars.back().pivotY = ruby.pivotY;
                ruby.chars.back().wipePoints = {
                    WipePoint{glyph.source->startMs, 0.0f},
                    WipePoint{glyph.source->endMs, 1.0f},
                };
            }
            if (!style.vertical && rubyHasBounds) {
                // Same glyph-ink vertical anchor as the main text (see the
                // horizontal fillBounds pass above): ruby bands follow the
                // reading's placed ink instead of the metric em box.
                const float rubyInkPad = static_cast<float>(
                    (rubyDrawEdge + rubyDrawEdge2 + 1) / 2
                ) * layoutScale;
                ruby.fillBounds.top = ruby.bounds.top - rubyInkPad;
                ruby.fillBounds.bottom = ruby.bounds.bottom + rubyInkPad;
            }
            if (style.vertical && rubyHasBounds && !ruby.geometries.empty()) {
                const float mainCellWidth = std::max(style.fontSize, 1.0f);
                DWRITE_FONT_METRICS mainVerticalMetrics{};
                mainMetricsFace->GetMetrics(&mainVerticalMetrics);
                const float mainUnits = static_cast<float>(std::max<UINT16>(
                    mainVerticalMetrics.designUnitsPerEm, 1
                ));
                const float mainCellHeight = std::max(
                    style.fontSize * static_cast<float>(
                        mainVerticalMetrics.ascent + mainVerticalMetrics.descent
                    ) / mainUnits,
                    1.0f
                );
                DWRITE_FONT_METRICS rubyVerticalMetrics{};
                selectedRubyMetricsFace->GetMetrics(&rubyVerticalMetrics);
                const float rubyUnits = static_cast<float>(std::max<UINT16>(
                    rubyVerticalMetrics.designUnitsPerEm, 1
                ));
                const float rubyCellWidth = std::max(rubyStyle.rubyFontSize, 1.0f);
                const float rubyAscent = rubyStyle.rubyFontSize
                    * static_cast<float>(rubyVerticalMetrics.ascent) / rubyUnits;
                const float rubyX = mainCellWidth * 0.5f + style.rubyGap
                    + rubyCellWidth * 0.5f;
                const float baseTop = static_cast<float>(sourceRuby.firstCharIndex)
                    * mainCellHeight;
                const float spanHeight = static_cast<float>(
                    sourceRuby.lastCharIndex - sourceRuby.firstCharIndex + 1
                ) * mainCellHeight;
                ruby.bounds = {};
                rubyHasBounds = false;
                auto transformRubyVertical = [&](ID2D1Geometry *source,
                                                  const D2D1_MATRIX_3X2_F &matrix,
                                                  Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                                                  const char *operation) {
                    if (source == nullptr) {
                        target.Reset();
                        return;
                    }
                    Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                    checkHr(
                        device_.d2dFactory()->CreateTransformedGeometry(
                            source, &matrix, transformed.ReleaseAndGetAddressOf()
                        ),
                        operation,
                        device_
                    );
                    target = transformed;
                };
                const std::size_t count = sourceRuby.units.size();
                std::size_t geometryIndex = 0;
                for (std::size_t unitIndex = 0; unitIndex < count; ++unitIndex) {
                    const float slotTop = baseTop + spanHeight
                        * static_cast<float>(unitIndex) / static_cast<float>(count);
                    const float slotHeight = spanHeight / static_cast<float>(count);
                    const auto [offsetX, offsetY] = verticalGlyphOffset(
                        sourceRuby.units[unitIndex].text,
                        rubyCellWidth,
                        slotHeight
                    );
                    D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Translation(
                        -ruby.chars[unitIndex].pivotX + rubyX + offsetX,
                        slotTop + rubyAscent - ruby.baselineOffset + offsetY
                    );
                    if (verticalRotates(sourceRuby.units[unitIndex].text)) {
                        matrix = matrix * D2D1::Matrix3x2F::Rotation(
                            90.0f,
                            D2D1::Point2F(rubyX, slotTop + slotHeight * 0.5f)
                        );
                    }
                    ruby.chars[unitIndex].realizationTransform =
                        ruby.chars[unitIndex].realizationTransform * matrix;
                    ruby.chars[unitIndex].left = rubyX - rubyCellWidth * 0.5f;
                    ruby.chars[unitIndex].right = rubyX + rubyCellWidth * 0.5f;
                    ruby.chars[unitIndex].top = slotTop;
                    ruby.chars[unitIndex].bottom = slotTop + slotHeight;
                    if (!rubyGlyphs[unitIndex].geometry) {
                        continue;
                    }
                    transformRubyVertical(
                        ruby.geometries[geometryIndex].Get(), matrix,
                        ruby.geometries[geometryIndex],
                        "ID2D1Factory::CreateTransformedGeometry(vertical ruby)"
                    );
                    if (geometryIndex < ruby.protectedStrokeGeometries.size()) {
                        transformRubyVertical(
                            ruby.protectedStrokeGeometries[geometryIndex].Get(), matrix,
                            ruby.protectedStrokeGeometries[geometryIndex],
                            "ID2D1Factory::CreateTransformedGeometry(vertical ruby protected)"
                        );
                    }
                    if (geometryIndex < ruby.strokeGeometries.size()) {
                        transformRubyVertical(
                            ruby.strokeGeometries[geometryIndex].Get(), matrix,
                            ruby.strokeGeometries[geometryIndex],
                            "ID2D1Factory::CreateTransformedGeometry(vertical ruby stroke)"
                        );
                    }
                    if (geometryIndex < ruby.stroke2Geometries.size()) {
                        transformRubyVertical(
                            ruby.stroke2Geometries[geometryIndex].Get(), matrix,
                            ruby.stroke2Geometries[geometryIndex],
                            "ID2D1Factory::CreateTransformedGeometry(vertical ruby stroke2)"
                        );
                    }
                    D2D1_RECT_F bounds{};
                    checkHr(
                        ruby.geometries[geometryIndex]->GetBounds(nullptr, &bounds),
                        "ID2D1Geometry::GetBounds(vertical ruby)",
                        device_
                    );
                    extendBounds(ruby.bounds, rubyHasBounds, bounds);
                    ruby.chars[unitIndex].left = bounds.left;
                    ruby.chars[unitIndex].right = bounds.right;
                    ruby.chars[unitIndex].top = bounds.top;
                    ruby.chars[unitIndex].bottom = bounds.bottom;
                    ++geometryIndex;
                }
                ruby.fillBounds = D2D1::RectF(
                    rubyX - rubyCellWidth * 0.5f,
                    baseTop,
                    rubyX + rubyCellWidth * 0.5f,
                    baseTop + spanHeight
                );
                ruby.pivotX = rubyX;
                ruby.pivotY = baseTop + spanHeight * 0.5f;
            }
            if (rubyHasBounds && !ruby.geometries.empty()) {
                cached.rubies.push_back(std::move(ruby));
            }
        }
        // Ruby annotations are stored in source-file order.  RL exports do not
        // guarantee that @RubyN entries follow their target characters' visual
        // order (for example, 出 may be listed before 逃 in 逃げ出したいと).
        // The interference pass below compares neighbouring ruby boxes and
        // shifts all text from the current target onward, so feeding it source
        // order can mistake a right-to-left jump for an overlap and move the
        // whole line underneath the wrong annotation.  Painter sorts the same
        // pass by target index; keep Direct2D on that shared layout semantic.
        std::stable_sort(
            cached.rubies.begin(), cached.rubies.end(),
            [](const Impl::CachedRuby &left, const Impl::CachedRuby &right) {
                return left.firstCharIndex < right.firstCharIndex;
            }
        );
        if (!style.vertical && !style.rightToLeft && cached.rubies.size() > 1) {
            auto translateGeometryX = [&](Microsoft::WRL::ComPtr<ID2D1Geometry> &geometry,
                                          float offsetX,
                                          const char *operation) {
                if (!geometry || offsetX == 0.0f) {
                    return;
                }
                const D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Translation(
                    offsetX, 0.0f
                );
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        geometry.Get(), &matrix, transformed.ReleaseAndGetAddressOf()
                    ),
                    operation,
                    device_
                );
                geometry = transformed;
            };
            auto translateCharX = [&](Impl::CachedChar &ch, float offsetX) {
                ch.left += offsetX;
                ch.right += offsetX;
                ch.layoutLeft += offsetX;
                ch.layoutRight += offsetX;
                ch.pivotX += offsetX;
                translateGeometryX(
                    ch.geometry, offsetX,
                    "ID2D1Factory::CreateTransformedGeometry(ruby interference character)"
                );
                translateGeometryX(
                    ch.protectedStrokeGeometry, offsetX,
                    "ID2D1Factory::CreateTransformedGeometry(ruby interference protected stroke)"
                );
                translateGeometryX(
                    ch.strokeGeometry, offsetX,
                    "ID2D1Factory::CreateTransformedGeometry(ruby interference stroke)"
                );
                translateGeometryX(
                    ch.stroke2Geometry, offsetX,
                    "ID2D1Factory::CreateTransformedGeometry(ruby interference stroke2)"
                );
            };
            auto translateRubyX = [&](Impl::CachedRuby &ruby, float offsetX) {
                ruby.bounds.left += offsetX;
                ruby.bounds.right += offsetX;
                ruby.fillBounds.left += offsetX;
                ruby.fillBounds.right += offsetX;
                ruby.pivotX += offsetX;
                for (Impl::CachedChar &ch : ruby.chars) {
                    ch.left += offsetX;
                    ch.right += offsetX;
                    ch.layoutLeft += offsetX;
                    ch.layoutRight += offsetX;
                    ch.pivotX += offsetX;
                }
                for (auto &geometry : ruby.geometries) {
                    translateGeometryX(
                        geometry, offsetX,
                        "ID2D1Factory::CreateTransformedGeometry(ruby interference ruby)"
                    );
                }
                for (auto &geometry : ruby.protectedStrokeGeometries) {
                    translateGeometryX(
                        geometry, offsetX,
                        "ID2D1Factory::CreateTransformedGeometry(ruby interference ruby protected stroke)"
                    );
                }
                for (auto &geometry : ruby.strokeGeometries) {
                    translateGeometryX(
                        geometry, offsetX,
                        "ID2D1Factory::CreateTransformedGeometry(ruby interference ruby stroke)"
                    );
                }
                for (auto &geometry : ruby.stroke2Geometries) {
                    translateGeometryX(
                        geometry, offsetX,
                        "ID2D1Factory::CreateTransformedGeometry(ruby interference ruby stroke2)"
                    );
                }
            };

            for (std::size_t rubyIndex = 1; rubyIndex < cached.rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &previous = cached.rubies[rubyIndex - 1];
                Impl::CachedRuby &current = cached.rubies[rubyIndex];
                if (previous.chars.empty() || current.chars.empty()) {
                    continue;
                }
                const float deficit = previous.chars.back().layoutRight
                    + style.rubyInterval - current.chars.front().layoutLeft;
                if (deficit <= 0.0f) {
                    continue;
                }
                const float push = std::ceil(deficit);
                const std::size_t firstChar = static_cast<std::size_t>(std::clamp(
                    current.firstCharIndex,
                    0,
                    static_cast<int>(cached.chars.size())
                ));
                for (std::size_t charIndex = firstChar;
                     charIndex < cached.chars.size(); ++charIndex) {
                    translateCharX(cached.chars[charIndex], push);
                }
                for (std::size_t followingIndex = rubyIndex;
                     followingIndex < cached.rubies.size(); ++followingIndex) {
                    translateRubyX(cached.rubies[followingIndex], push);
                }
                cursor += push;
            }

            cached.geometries.clear();
            cached.bounds = {};
            lineHasBounds = false;
            for (const Impl::CachedChar &ch : cached.chars) {
                if (ch.geometry) {
                    D2D1_RECT_F bounds{};
                    checkHr(
                        ch.geometry->GetBounds(nullptr, &bounds),
                        "ID2D1Geometry::GetBounds(ruby interference character)",
                        device_
                    );
                    extendBounds(cached.bounds, lineHasBounds, bounds);
                    cached.geometries.push_back(ch.geometry);
                } else if (ch.bitmapGuide.has_value()) {
                    extendBounds(cached.bounds, lineHasBounds, ch.bitmapRect);
                }
            }
            cached.fillBounds.right = std::max(cursor, 1.0f);
        }
        if (cached.wipeReverse) {
            // Python 在源加载入口已把整行时间戳严格逆序的行镜像理顺为顺序，
            // 仅保留 wipeReverse 标记。这里把缓存字符反转为演唱时间序，并把
            // 时间窗口反序配对（位置 i 用窗口 n-1-i）：反转后数组序 = 时间序，
            // 几何自右向左递降（竖排自下而上），与 RTL 文本同构，render 期
            // 的方向 XOR 直接复用既有扫描/分侧逻辑。几何、位图导唱符与逐字
            // 样式随字符走，仅时间字段跨字符交换；ruby 的字符索引同步重映射。
            if (cached.chars.size() > 1) {
                std::vector<std::pair<int, int>> windows;
                std::vector<std::vector<WipePoint>> pointTracks;
                windows.reserve(cached.chars.size());
                pointTracks.reserve(cached.chars.size());
                for (Impl::CachedChar &ch : cached.chars) {
                    windows.emplace_back(ch.startMs, ch.endMs);
                    pointTracks.push_back(std::move(ch.wipePoints));
                }
                std::reverse(windows.begin(), windows.end());
                std::reverse(pointTracks.begin(), pointTracks.end());
                for (std::size_t index = 0; index < cached.chars.size(); ++index) {
                    Impl::CachedChar &ch = cached.chars[index];
                    ch.startMs = windows[index].first;
                    ch.endMs = windows[index].second;
                    ch.wipePoints = std::move(pointTracks[index]);
                }
                std::reverse(cached.chars.begin(), cached.chars.end());
                std::reverse(cached.geometries.begin(), cached.geometries.end());
            }
            const int charCount = static_cast<int>(cached.chars.size());
            for (Impl::CachedRuby &ruby : cached.rubies) {
                const int oldFirst = ruby.firstCharIndex;
                const int oldLast = ruby.lastCharIndex;
                ruby.firstCharIndex = charCount - 1 - oldLast;
                ruby.lastCharIndex = charCount - 1 - oldFirst;
                ruby.transitionCharIndex = charCount - 1 - ruby.transitionCharIndex;
            }
        }
        const auto adjustWipeEnd = [](Impl::CachedChar &current,
                                      const Impl::CachedChar &following,
                                      bool rtl) {
            if (current.wipePoints.empty()) {
                return;
            }
            const float width = std::max(
                current.layoutRight - current.layoutLeft + 1.0f, 1.0f
            );
            if (!rtl && current.layoutRight >= following.layoutLeft) {
                current.wipePoints.back().position = std::clamp(
                    (following.layoutLeft - current.layoutLeft) / width,
                    0.0f, 1.0f
                );
            } else if (rtl && current.layoutLeft <= following.layoutRight) {
                current.wipePoints.back().position = std::clamp(
                    (current.layoutRight - following.layoutRight) / width,
                    0.0f, 1.0f
                );
            }
        };
        if (!style.vertical) {
            const bool rtl = style.rightToLeft != cached.wipeReverse;
            for (std::size_t index = 0; index + 1 < cached.chars.size(); ++index) {
                adjustWipeEnd(cached.chars[index], cached.chars[index + 1], rtl);
            }
            Impl::CachedChar *previousRubyChar = nullptr;
            for (Impl::CachedRuby &ruby : cached.rubies) {
                if (previousRubyChar != nullptr && !ruby.chars.empty()) {
                    adjustWipeEnd(*previousRubyChar, ruby.chars.front(), rtl);
                }
                for (std::size_t index = 0; index + 1 < ruby.chars.size(); ++index) {
                    adjustWipeEnd(ruby.chars[index], ruby.chars[index + 1], rtl);
                }
                if (!ruby.chars.empty()) {
                    previousRubyChar = &ruby.chars.back();
                }
            }
        }
        cached.horizontalFillBoundsByStyle.clear();
        for (const Impl::CachedChar &ch : cached.chars) {
            D2D1_RECT_F inkBounds{};
            bool hasInk = false;
            if (ch.geometry) {
                checkHr(
                    ch.geometry->GetBounds(nullptr, &inkBounds),
                    "ID2D1Geometry::GetBounds(role horizontal fill)",
                    device_
                );
                hasInk = inkBounds.right > inkBounds.left;
            } else if (ch.bitmapGuide.has_value()
                       && ch.bitmapRect.right > ch.bitmapRect.left) {
                inkBounds = ch.bitmapRect;
                hasInk = true;
            }
            if (!hasInk) {
                continue;
            }
            const auto found = cached.horizontalFillBoundsByStyle.find(
                ch.styleIndex
            );
            if (found == cached.horizontalFillBoundsByStyle.end()) {
                cached.horizontalFillBoundsByStyle.emplace(
                    ch.styleIndex,
                    D2D1::RectF(
                        inkBounds.left,
                        cached.fillBounds.top,
                        std::max(inkBounds.right, inkBounds.left + 1.0f),
                        cached.fillBounds.bottom
                    )
                );
            } else {
                found->second.left = std::min(
                    found->second.left, inkBounds.left
                );
                found->second.right = std::max(
                    found->second.right, inkBounds.right
                );
            }
        }
        // 「真一组」渐变带正文侧拓宽：柱体装饰源与正文第一角色同源时
        // （signalBandJoin，Python 侧同款闸门），把最左角色样式（= 行内
        // 第一角色）的横向渐变跨度左缘拓宽到柱组左缘——主文字与 ruby
        // 共享盒（下方 ruby 块读本表）随之共用柱体同一条渐变带，镜像
        // Painter 的 apply_signal_band_left。band 左缘 = volumeOffsetX −
        // groupWidth + strokeExtent（行本地，与渲染端 volumeBarRectAt(0)
        // 同式）。
        if (cached.signalBandJoin) {
            const VolumeSignalGeometry bandGeometry = volumeSignalGeometry(style);
            const float bandLeft = style.volumeOffsetX
                - bandGeometry.groupWidth + bandGeometry.strokeExtent;
            std::optional<int> firstStyleIndex;
            float firstLeft = 0.0f;
            for (const auto &[styleIndex, bounds]
                 : cached.horizontalFillBoundsByStyle) {
                if (!firstStyleIndex.has_value()
                    || bounds.left < firstLeft) {
                    firstStyleIndex = styleIndex;
                    firstLeft = bounds.left;
                }
            }
            if (firstStyleIndex.has_value()) {
                D2D1_RECT_F &bounds =
                    cached.horizontalFillBoundsByStyle[*firstStyleIndex];
                bounds.left = std::min(bounds.left, bandLeft);
            }
        }
        if (!cached.rubies.empty()) {
            D2D1_RECT_F sharedVerticalBounds = cached.fillBounds;
            for (const Impl::CachedRuby &ruby : cached.rubies) {
                sharedVerticalBounds.top = std::min(
                    sharedVerticalBounds.top, ruby.fillBounds.top
                );
                sharedVerticalBounds.bottom = std::max(
                    sharedVerticalBounds.bottom, ruby.fillBounds.bottom
                );
            }
            sharedVerticalBounds.bottom = std::max(
                sharedVerticalBounds.bottom,
                sharedVerticalBounds.top + 1.0f
            );
            for (Impl::CachedRuby &ruby : cached.rubies) {
                const TextStyle &rubyStyle = ruby.styleIndex >= 0
                    && ruby.styleIndex < static_cast<int>(scene.charStyles.size())
                    ? scene.charStyles[static_cast<std::size_t>(ruby.styleIndex)]
                    : style;
                D2D1_RECT_F localHorizontalBounds = ruby.fillBounds;
                if (ruby.bounds.right > ruby.bounds.left) {
                    localHorizontalBounds.left = ruby.bounds.left;
                    localHorizontalBounds.right = ruby.bounds.right;
                }
                D2D1_RECT_F sharedHorizontalBounds = sharedVerticalBounds;
                const auto roleBounds = cached.horizontalFillBoundsByStyle.find(
                    ruby.styleIndex
                );
                if (roleBounds != cached.horizontalFillBoundsByStyle.end()) {
                    sharedHorizontalBounds.left = roleBounds->second.left;
                    sharedHorizontalBounds.right = roleBounds->second.right;
                } else if (cached.bounds.right > cached.bounds.left) {
                    sharedHorizontalBounds.left = cached.bounds.left;
                    sharedHorizontalBounds.right = cached.bounds.right;
                }
                ruby.horizontalFillBounds = rubyStyle.rubyHorizontalGradientWithMain
                    ? sharedHorizontalBounds
                    : localHorizontalBounds;
            }
        }
        if (!lineHasBounds) {
            cached.bounds = D2D1::RectF(0.0f, 0.0f, 0.0f, 0.0f);
        }
        impl_->lines.push_back(std::move(cached));
    }
    // Ruby drawing keeps geometry arrays for historical phase ordering. Mirror
    // their final post-layout/post-interference geometry into CachedChar so the
    // realization pack is indexed exactly like the main-character pack.
    for (Impl::CachedLine &line : impl_->lines) {
        for (Impl::CachedRuby &ruby : line.rubies) {
            for (std::size_t index = 0; index < ruby.chars.size(); ++index) {
                Impl::CachedChar &ch = ruby.chars[index];
                if (index < ruby.geometries.size()) {
                    ch.geometry = ruby.geometries[index];
                }
                if (index < ruby.protectedStrokeGeometries.size()) {
                    ch.protectedStrokeGeometry =
                        ruby.protectedStrokeGeometries[index];
                }
                if (index < ruby.strokeGeometries.size()) {
                    ch.strokeGeometry = ruby.strokeGeometries[index];
                }
                if (index < ruby.stroke2Geometries.size()) {
                    ch.stroke2Geometry = ruby.stroke2Geometries[index];
                }
            }
        }
    }
    impl_->diagnostics.realizationPrewarmSkipped = 0;
    impl_->diagnostics.realizationPrewarmTasks = 0;
    impl_->diagnostics.realizationPrewarmMs = 0.0;
    impl_->diagnostics.realizationPrewarmFillTasks = 0;
    impl_->diagnostics.realizationPrewarmStrokeTasks = 0;
    impl_->diagnostics.realizationPrewarmContextMs = 0.0;
    impl_->diagnostics.realizationPrewarmWaitMs = 0.0;
    impl_->diagnostics.realizationPrewarmFillCreateMs = 0.0;
    impl_->diagnostics.realizationPrewarmStrokeCreateMs = 0.0;
    impl_->diagnostics.realizationPrewarmPublishMs = 0.0;
    impl_->diagnostics.realizationPrewarmCreateP50Ms = 0.0;
    impl_->diagnostics.realizationPrewarmCreateP95Ms = 0.0;
    impl_->diagnostics.realizationPrewarmCreateMaxMs = 0.0;
    impl_->lastRenderCompletedMs.store(steadyNowMs(), std::memory_order_release);
    if (impl_->realizationActive) {
        std::vector<std::size_t> lineOrder(impl_->lines.size());
        for (std::size_t index = 0; index < lineOrder.size(); ++index) {
            lineOrder[index] = index;
        }
        const int prewarmTimeMs = impl_->scene.prewarmTimeMs;
        const auto distanceFromPrewarm = [&](const Impl::CachedLine &line) {
            int distance = std::min(
                std::abs(prewarmTimeMs - line.startMs),
                std::abs(prewarmTimeMs - line.endMs)
            );
            if (prewarmTimeMs >= line.startMs && prewarmTimeMs <= line.endMs) {
                distance = 0;
            }
            for (const DisplayWindow &window : line.displayWindows) {
                if (prewarmTimeMs >= window.startMs
                    && prewarmTimeMs <= window.endMs) {
                    return 0;
                }
                distance = std::min(
                    distance,
                    std::min(
                        std::abs(prewarmTimeMs - window.startMs),
                        std::abs(prewarmTimeMs - window.endMs)
                    )
                );
            }
            return distance;
        };
        std::stable_sort(
            lineOrder.begin(), lineOrder.end(),
            [&](std::size_t left, std::size_t right) {
                return distanceFromPrewarm(impl_->lines[left])
                    < distanceFromPrewarm(impl_->lines[right]);
            }
        );
        struct RealizationCandidate {
            Impl::RealizationTarget target;
            Microsoft::WRL::ComPtr<ID2D1Geometry> sharedGeometry;
            Microsoft::WRL::ComPtr<ID2D1Geometry> positionedGeometry;
            D2D1_MATRIX_3X2_F instanceTransform =
                D2D1::Matrix3x2F::Identity();
            float strokeWidth = 0.0f;
            bool stroked = false;
            // 矢量字形描边任务按填充语义烘焙预展开轮廓（创建毫秒级）。
            bool fillOutline = false;
            // 预估创建成本（ms）：自适应预热调度的排序键（粗估即可，
            // 只需保序：段数越多、描边越宽越贵；预展开任务恒廉价）。
            float estCostMs = 1.0f;
        };
        std::vector<RealizationCandidate> candidates;
        const std::size_t realizationCapacity = static_cast<std::size_t>(
            std::max<std::uint64_t>(
                impl_->scene.realizationCapacity,
                Impl::defaultRealizationCapacity
            )
        );
        impl_->diagnostics.realizationCapacity = realizationCapacity;
        const auto appendCandidate = [&] (
            std::size_t lineIndex,
            int rubyIndex,
            std::size_t charIndex,
            Impl::RealizationKind kind,
            ID2D1Geometry *sharedGeometry,
            ID2D1Geometry *positionedGeometry,
            const D2D1_MATRIX_3X2_F &instanceTransform,
            float strokeWidth,
            bool fillOutline = false,
            float estCostMs = 1.0f
        ) {
            const bool isStroke = kind == Impl::RealizationKind::Stroke
                || kind == Impl::RealizationKind::Stroke2;
            if (sharedGeometry == nullptr || positionedGeometry == nullptr
                || (isStroke && strokeWidth <= 0.0f)) {
                return;
            }
            candidates.push_back({
                {lineIndex, rubyIndex, charIndex, kind},
                sharedGeometry,
                positionedGeometry,
                instanceTransform,
                strokeWidth,
                isStroke,
            });
            if (fillOutline) {
                candidates.back().fillOutline = true;
            }
            candidates.back().estCostMs = estCostMs;
        };
        const auto appendCharTasks = [&] (
            std::size_t lineIndex,
            int rubyIndex,
            std::size_t charIndex,
            const Impl::CachedChar &ch,
            float strokeWidth,
            float stroke2Width
        ) {
            const float mainWidth = std::max(strokeWidth, 0.0f);
            // 8px 门已拆（2026-10 用户拍板，二次确认）：细描边同样参与预
            // 热——自适应调度需要它在重载下有余量可押。字节一致性不靠阈
            // 值保证（任何阈值下「预热窗口内」都存在 realization/直绘的
            // ±4 alpha 边缘差），靠金标准比较点两侧同烘焙状态（测试等
            // prewarm 完成或用容差）。描边任务保留 0.5px 发丝卫生阀防
            // 洪泛；何时/何序烘焙由预热线程按实测帧耗自适应。
            const float strokeTaskWidth = mainWidth >= 0.5f ? mainWidth : 0.0f;
            // 矢量字形（有预展开轮廓）的描边任务：几何换成轮廓、按填充
            // 语义烘焙（毫秒级）；文本字形保持原生描边 realization。
            const bool vectorChar = ch.strokeOutline != nullptr;
            // 成本粗估（保序即可）：文本描边 ≈ 段数 × (1+宽/8)；矢量/
            // 填充恒按 1ms。
            const auto textStrokeCost = [](float width) -> float {
                return 1.0f + (1.0f + width / 8.0f) * 2.0f;
            };
            appendCandidate(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::Fill,
                ch.realizationGeometry.Get(), ch.geometry.Get(),
                ch.realizationTransform, 0.0f
            );
            appendCandidate(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::ProtectedStroke,
                ch.protectedRealizationGeometry.Get(),
                ch.protectedStrokeGeometry.Get(),
                ch.realizationTransform, 0.0f
            );
            if (strokeTaskWidth > 0.0f) {
                appendCandidate(
                    lineIndex, rubyIndex, charIndex,
                    Impl::RealizationKind::Stroke,
                    vectorChar ? ch.strokeOutline.Get()
                               : ch.realizationGeometry.Get(),
                    vectorChar ? ch.strokeGeometry.Get()
                               : ch.geometry.Get(),
                    ch.realizationTransform, strokeTaskWidth,
                    vectorChar,
                    vectorChar ? 1.0f : textStrokeCost(strokeTaskWidth)
                );
            }
            const float combinedStroke2 =
                stroke2Width > 0.0f && strokeTaskWidth > 0.0f
                    ? strokeTaskWidth + std::max(stroke2Width, 0.0f)
                    : 0.0f;
            if (combinedStroke2 > 0.0f) {
                const bool vectorStroke2 = ch.stroke2Outline != nullptr;
                appendCandidate(
                    lineIndex, rubyIndex, charIndex,
                    Impl::RealizationKind::Stroke2,
                    vectorStroke2 ? ch.stroke2Outline.Get()
                                  : ch.realizationGeometry.Get(),
                    vectorStroke2 ? ch.stroke2Geometry.Get()
                                  : ch.geometry.Get(),
                    ch.realizationTransform, combinedStroke2,
                    vectorStroke2,
                    vectorStroke2 ? 1.0f : textStrokeCost(combinedStroke2)
                );
            }
        };
        for (std::size_t lineIndex : lineOrder) {
            const Impl::CachedLine &line = impl_->lines[lineIndex];
            for (std::size_t charIndex = 0;
                 charIndex < line.chars.size(); ++charIndex) {
                const Impl::CachedChar &ch = line.chars[charIndex];
                const TextStyle &charStyle = ch.styleIndex >= 0
                    && ch.styleIndex < static_cast<int>(impl_->scene.charStyles.size())
                    ? impl_->scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                    : line.style;
                appendCharTasks(
                    lineIndex, -1, charIndex, ch,
                    charStyle.strokeWidth, charStyle.stroke2Width
                );
            }
            for (std::size_t rubyIndex = 0;
                 rubyIndex < line.rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line.rubies[rubyIndex];
                const TextStyle &rubyStyle = ruby.styleIndex >= 0
                    && ruby.styleIndex < static_cast<int>(impl_->scene.charStyles.size())
                    ? impl_->scene.charStyles[static_cast<std::size_t>(ruby.styleIndex)]
                    : line.style;
                for (std::size_t charIndex = 0;
                     charIndex < ruby.chars.size(); ++charIndex) {
                    appendCharTasks(
                        lineIndex, static_cast<int>(rubyIndex), charIndex,
                        ruby.chars[charIndex],
                        ruby.latin ? rubyStyle.rubyLatinStrokeWidth
                                   : rubyStyle.rubyStrokeWidth,
                        ruby.latin ? rubyStyle.rubyLatinStroke2Width
                                   : rubyStyle.rubyStroke2Width
                    );
                }
            }
        }
        std::vector<Impl::RealizationTask> tasks;
        tasks.reserve(std::min(candidates.size(), realizationCapacity));
        std::uint64_t capacitySkipped = 0;
        const auto realizationKey = [] (
            ID2D1Geometry *keyGeometry,
            bool stroked,
            float strokeWidth,
            bool sharedResource,
            const D2D1_MATRIX_3X2_F &transform
        ) {
            return Impl::RealizationCacheKey{
                reinterpret_cast<std::uintptr_t>(keyGeometry),
                stroked,
                std::bit_cast<std::uint32_t>(stroked ? strokeWidth : 0.0f),
                sharedResource,
                std::bit_cast<std::uint32_t>(transform._11),
                std::bit_cast<std::uint32_t>(transform._12),
                std::bit_cast<std::uint32_t>(transform._21),
                std::bit_cast<std::uint32_t>(transform._22),
                std::bit_cast<std::uint32_t>(transform._31),
                std::bit_cast<std::uint32_t>(transform._32),
            };
        };
        if (candidates.size() <= realizationCapacity) {
            // The original positioned realization is faster at draw time: no
            // per-instance world transform is needed. Keep that path whenever
            // every requested resource fits in the configured budget.
            for (const RealizationCandidate &candidate : candidates) {
                Impl::RealizationTask task;
                task.targets.push_back(candidate.target);
                task.geometry = candidate.positionedGeometry;
                task.keyGeometry = candidate.sharedGeometry;
                task.cacheKey = realizationKey(
                    candidate.sharedGeometry.Get(), candidate.stroked,
                    candidate.strokeWidth, false, candidate.instanceTransform
                );
                task.strokeWidth = candidate.strokeWidth;
                task.fillOutline = candidate.fillOutline;
                task.estCostMs = candidate.estCostMs;
                tasks.push_back(std::move(task));
            }
        } else {
            // Only capacity-bound scenes pay the instance-transform cost. A
            // shared glyph realization replaces many positioned resources and
            // prevents the tail of a long song from falling back to geometry.
            using RealizationKey =
                std::tuple<std::uintptr_t, bool, std::uint32_t>;
            std::map<RealizationKey, std::size_t> taskByResource;
            for (const RealizationCandidate &candidate : candidates) {
                const RealizationKey key{
                    reinterpret_cast<std::uintptr_t>(
                        candidate.sharedGeometry.Get()
                    ),
                    candidate.stroked,
                    std::bit_cast<std::uint32_t>(
                        candidate.stroked ? candidate.strokeWidth : 0.0f
                    ),
                };
                const auto existing = taskByResource.find(key);
                Impl::RealizationTarget target = candidate.target;
                target.transform = candidate.instanceTransform;
                if (existing != taskByResource.end()) {
                    tasks[existing->second].targets.push_back(target);
                    continue;
                }
                if (tasks.size() >= realizationCapacity) {
                    ++capacitySkipped;
                    continue;
                }
                Impl::RealizationTask task;
                task.targets.push_back(target);
                task.geometry = candidate.sharedGeometry;
                task.keyGeometry = candidate.sharedGeometry;
                task.cacheKey = realizationKey(
                    candidate.sharedGeometry.Get(), candidate.stroked,
                    candidate.strokeWidth, true,
                    D2D1::Matrix3x2F::Identity()
                );
                task.strokeWidth = candidate.strokeWidth;
                task.fillOutline = candidate.fillOutline;
                task.estCostMs = candidate.estCostMs;
                tasks.push_back(std::move(task));
                taskByResource.emplace(key, tasks.size() - 1);
            }
        }
        const auto assignRealization = [this] (
            const Impl::RealizationTask &task,
            ID2D1GeometryRealization *realization
        ) {
            bool published = false;
            for (const Impl::RealizationTarget &target : task.targets) {
                if (target.lineIndex >= impl_->lines.size()) {
                    continue;
                }
                Impl::CachedLine &line = impl_->lines[target.lineIndex];
                Impl::CachedChar *targetChar = nullptr;
                if (target.rubyIndex < 0) {
                    if (target.charIndex < line.chars.size()) {
                        targetChar = &line.chars[target.charIndex];
                    }
                } else if (static_cast<std::size_t>(target.rubyIndex)
                           < line.rubies.size()) {
                    Impl::CachedRuby &ruby = line.rubies[
                        static_cast<std::size_t>(target.rubyIndex)
                    ];
                    if (target.charIndex < ruby.chars.size()) {
                        targetChar = &ruby.chars[target.charIndex];
                    }
                }
                if (targetChar == nullptr) {
                    continue;
                }
                switch (target.kind) {
                case Impl::RealizationKind::Fill:
                    targetChar->fillRealization = realization;
                    targetChar->fillRealizationTransform = target.transform;
                    break;
                case Impl::RealizationKind::ProtectedStroke:
                    targetChar->protectedStrokeRealization = realization;
                    targetChar->protectedStrokeRealizationTransform = target.transform;
                    break;
                case Impl::RealizationKind::Stroke:
                    targetChar->strokeRealization = realization;
                    targetChar->strokeRealizationTransform = target.transform;
                    break;
                case Impl::RealizationKind::Stroke2:
                    targetChar->stroke2Realization = realization;
                    targetChar->stroke2RealizationTransform = target.transform;
                    break;
                }
                published = true;
            }
            return published;
        };
        std::vector<Impl::RealizationTask> pendingTasks;
        pendingTasks.reserve(tasks.size());
        std::set<Impl::RealizationCacheKey> reusedResources;
        if (impl_->resourceCacheEnabled) {
            std::lock_guard<std::mutex> lock(impl_->realizationMutex);
            for (Impl::RealizationTask &task : tasks) {
                const auto found = impl_->realizationResources.find(task.cacheKey);
                if (found == impl_->realizationResources.end()) {
                    pendingTasks.push_back(std::move(task));
                    continue;
                }
                found->second.lastUse = ++impl_->realizationResourceUseSerial;
                if (assignRealization(task, found->second.realization.Get())) {
                    reusedResources.insert(task.cacheKey);
                }
            }
            impl_->realizationCount = reusedResources.size();
        } else {
            pendingTasks = std::move(tasks);
        }
        tasks = std::move(pendingTasks);
        impl_->diagnostics.realizationPrewarmSkipped = capacitySkipped;
        impl_->diagnostics.realizationPrewarmTasks = tasks.size();
        if (tasks.empty()) {
            // Every geometry resource survived from the previous generation.
            // The current CachedChar slots are already rebound above.
            impl_->realizationPrewarmComplete.store(
                true, std::memory_order_release
            );
        } else {
            auto control = std::make_shared<Impl::RealizationControl>();
            control->generation = impl_->realizationGeneration;
            impl_->realizationControl = control;
            impl_->realizationPrewarmComplete.store(
                false, std::memory_order_release
            );
            const bool deferUntilFirstFrame =
                impl_->scene.deferRealizationPrewarmUntilFirstFrame;
            impl_->realizationThread = std::thread([
            this,
            control,
            deferUntilFirstFrame,
            realizationCapacity,
            assignRealization,
            tasks = std::move(tasks)
            ]() mutable {
            // Keep individual background realization chunks short enough for
            // seek/style churn while staying inside the wide-stroke A/B gate.
            // Match N3's export precision. Export uses these cached realizations
            // directly, so a coarse tolerance becomes visible as faceted curves.
            constexpr float flatteningTolerance = 0.25f;
            const auto prewarmStart = Clock::now();
            auto sliceStart = prewarmStart;
            std::uint64_t failed = 0;
            std::uint64_t fillTasks = 0;
            std::uint64_t strokeTasks = 0;
            double contextMs = 0.0;
            double waitMs = 0.0;
            double fillCreateMs = 0.0;
            double strokeCreateMs = 0.0;
            double publishMs = 0.0;
            std::vector<double> createDurations;
            createDurations.reserve(tasks.size());
            const auto isCurrent = [&]() {
                return control->generation == impl_->realizationGeneration;
            };
            const auto finish = [&]() {
                std::lock_guard<std::mutex> lock(impl_->realizationMutex);
                if (isCurrent()) {
                    impl_->diagnostics.realizationPrewarmSkipped += failed;
                    impl_->diagnostics.realizationPrewarmMs = elapsedMs(prewarmStart);
                    impl_->diagnostics.realizationPrewarmFillTasks = fillTasks;
                    impl_->diagnostics.realizationPrewarmStrokeTasks = strokeTasks;
                    impl_->diagnostics.realizationPrewarmContextMs = contextMs;
                    impl_->diagnostics.realizationPrewarmWaitMs = waitMs;
                    impl_->diagnostics.realizationPrewarmFillCreateMs = fillCreateMs;
                    impl_->diagnostics.realizationPrewarmStrokeCreateMs = strokeCreateMs;
                    impl_->diagnostics.realizationPrewarmPublishMs = publishMs;
                    if (!createDurations.empty()) {
                        std::sort(createDurations.begin(), createDurations.end());
                        const auto percentile = [&](double value) {
                            const std::size_t index = static_cast<std::size_t>(
                                std::ceil(value * static_cast<double>(
                                    createDurations.size() - 1
                                ))
                            );
                            return createDurations[index];
                        };
                        impl_->diagnostics.realizationPrewarmCreateP50Ms =
                            percentile(0.50);
                        impl_->diagnostics.realizationPrewarmCreateP95Ms =
                            percentile(0.95);
                        impl_->diagnostics.realizationPrewarmCreateMaxMs =
                            createDurations.back();
                    }
                    if (impl_->resourceCacheEnabled) {
                        while (impl_->realizationResources.size()
                               > realizationCapacity) {
                            const auto victim = std::min_element(
                                impl_->realizationResources.begin(),
                                impl_->realizationResources.end(),
                                [](const auto &left, const auto &right) {
                                    return left.second.lastUse
                                        < right.second.lastUse;
                                }
                            );
                            if (victim == impl_->realizationResources.end()) {
                                break;
                            }
                            impl_->realizationResources.erase(victim);
                        }
                    }
                    // 被取消（pause/析构）的预热不得标记完成：渲染侧的整
                    // 体门控（realizationReady）按 prewarmComplete 放行
                    // realization 网格路径，半热状态若被置 true 会把「部分
                    // 字符网格 + 部分字符原路径」锁死成常态（2026-10 多
                    // worker 抖动的放大器之一）。取消即保持未完成，渲染继
                    // 续原路径直到下一次 configure 重建预热。
                    impl_->realizationPrewarmComplete.store(
                        !control->stop.load(std::memory_order_acquire),
                        std::memory_order_release
                    );
                }
                control->done.store(true, std::memory_order_release);
            };
            Microsoft::WRL::ComPtr<ID2D1DeviceContext> workerBaseContext;
            Microsoft::WRL::ComPtr<ID2D1DeviceContext1> workerContext;
            const auto contextStart = Clock::now();
            HRESULT contextResult = device_.d2dDevice()->CreateDeviceContext(
                D2D1_DEVICE_CONTEXT_OPTIONS_ENABLE_MULTITHREADED_OPTIMIZATIONS,
                workerBaseContext.ReleaseAndGetAddressOf()
            );
            if (SUCCEEDED(contextResult)) {
                contextResult = workerBaseContext.As(&workerContext);
            }
            contextMs = elapsedMs(contextStart);
            if (FAILED(contextResult) || !workerContext) {
                ++failed;
                finish();
                return;
            }
            const auto shouldStop = [&]() {
                return control->stop.load(std::memory_order_acquire);
            };
            const auto waitForFrameGap = [&]() {
                while (!shouldStop()) {
                    if (deferUntilFirstFrame
                        && !impl_->firstFrameCompleted.load(
                            std::memory_order_acquire
                        )) {
                        std::this_thread::sleep_for(std::chrono::milliseconds(1));
                        continue;
                    }
                    const bool active = impl_->renderActive.load(
                        std::memory_order_acquire
                    );
                    const std::int64_t idleMs = steadyNowMs()
                        - impl_->lastRenderCompletedMs.load(
                            std::memory_order_acquire
                        );
                    // Continuous 60fps playback never has a 100ms idle
                    // window.  Waiting for that long left the real project
                    // permanently on DrawGeometry.  Start at most one task
                    // in each inter-frame gap after a short foreground grace
                    // period; publishing still waits on realizationMutex, so
                    // a completed resource cannot race the active frame.
                    if (!active && idleMs >= 2) {
                        return true;
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                }
                return false;
            };
            const auto publish = [&] (
                const Impl::RealizationTask &task,
                Microsoft::WRL::ComPtr<ID2D1GeometryRealization> created
            ) {
                std::lock_guard<std::mutex> lock(impl_->realizationMutex);
                if (shouldStop() || !isCurrent()) {
                    return false;
                }
                if (impl_->resourceCacheEnabled) {
                    impl_->realizationResources[task.cacheKey] = {
                        task.keyGeometry,
                        created,
                        ++impl_->realizationResourceUseSerial,
                    };
                }
                const bool published = assignRealization(task, created.Get());
                if (published) {
                    ++impl_->realizationCount;
                }
                return published;
            };
            const auto yieldSlice = [&]() {
                if (elapsedMs(sliceStart) >= 50.0) {
                    std::this_thread::yield();
                    sliceStart = Clock::now();
                }
            };
            // 自适应预热调度（2026-10 用户拍板）：8px 硬门限已废除——任务
            // 全量入表，何时/何序创建按实测帧成本动态决定：
            // - 富余（EMA 远低于帧预算）：全速烘烤，贵任务优先（最坏情形
            //   最早暴露并完成）；稳定尺寸下最终全表完成，帧全部命中缓存。
            // - 压力（EMA ≥ 帧预算，掉出 60fps）：可见行的任务豁免（命中
            //   缓存直接消减 EMA），不可见的贵任务让路等待——空闲或压力
            //   解除后自动继续；已完成任务即时入账，播放/暂停不重做。
            // - 廉价任务（填充 / 预展开轮廓，≈1ms）任何状态都可跑。
            constexpr float frameBudgetMs = 1000.0f / 60.0f;
            const auto lineVisible = [&](std::size_t lineIndex) {
                if (lineIndex >= impl_->lines.size()) {
                    return false;
                }
                const Impl::CachedLine &line = impl_->lines[lineIndex];
                const std::int64_t now = impl_->lastRenderedTimeMs.load(
                    std::memory_order_acquire
                );
                if (now >= line.startMs && now <= line.endMs) {
                    return true;
                }
                for (const DisplayWindow &window : line.displayWindows) {
                    if (now >= window.startMs && now <= window.endMs) {
                        return true;
                    }
                }
                return false;
            };
            const auto taskVisible = [&](const Impl::RealizationTask &task) {
                for (const Impl::RealizationTarget &target : task.targets) {
                    if (lineVisible(target.lineIndex)) {
                        return true;
                    }
                }
                return false;
            };
            std::vector<std::size_t> pending(tasks.size());
            for (std::size_t index = 0; index < pending.size(); ++index) {
                pending[index] = index;
            }
            std::size_t completed = 0;
            while (!pending.empty()) {
                if (!waitForFrameGap()) {
                    pending.clear();
                    break;
                }
                const float ema = impl_->frameRenderMsEma.load(
                    std::memory_order_acquire
                );
                const bool stress = ema > frameBudgetMs;
                // 选下一个合格任务：可见（压力态加权）+ 成本降序。
                std::size_t pickSlot = pending.size();
                float bestScore = -1.0f;
                for (std::size_t slot = 0; slot < pending.size(); ++slot) {
                    const Impl::RealizationTask &task = tasks[pending[slot]];
                    const bool visible = stress && taskVisible(task);
                    const bool cheap = task.fillOutline
                        || task.strokeWidth <= 0.0f
                        || task.estCostMs <= 2.0f;
                    if (!cheap && stress && !visible) {
                        continue;
                    }
                    const float score =
                        (visible ? 1.0e6f : 0.0f) + task.estCostMs;
                    if (score > bestScore) {
                        bestScore = score;
                        pickSlot = slot;
                    }
                }
                if (pickSlot == pending.size()) {
                    // 压力态下暂无合格任务：小睡后再评估（EMA 与可见性
                    // 随播放变化），停止请求由 waitForFrameGap 处理。
                    std::this_thread::sleep_for(
                        std::chrono::milliseconds(50)
                    );
                    continue;
                }
                const Impl::RealizationTask &task = tasks[pending[pickSlot]];
                // 看门狗心跳：逐任务上报。单个任务可能合法耗时数十秒
                // （密集矢量路径 × 宽描边的 CPU 细分），任务间的心跳保
                // 证「忙碌但在推进」期间 GUI 的等待租期持续续期。
                krok::subtitle::native::protocol::emitProgress(
                    QStringLiteral("realize"),
                    completed,
                    tasks.size()
                );
                const auto waitStart = Clock::now();
                if (!waitForFrameGap()) {
                    pending.clear();
                    break;
                }
                waitMs += elapsedMs(waitStart);
                Microsoft::WRL::ComPtr<ID2D1GeometryRealization> created;
                HRESULT result = E_FAIL;
                const bool stroked = task.strokeWidth > 0.0f;
                const auto createStart = Clock::now();
                // 描边 realization 创建是单体不可中断调用（密集路径 ×
                // 宽描边实测最坏 49s）：预热线程被它阻塞期间逐任务心跳
                // 发不出。主线程此时通常空闲（空闲层心跳在喂），但用户
                // 若恰好再触发 configure，等待期就只剩这里——按预算登记
                // 代喂兜底（双保险）。矢量字形的描边任务走 fillOutline
                //（预展开轮廓 + 填充语义，毫秒级），不经过昂贵分支。
                if (task.fillOutline) {
                    result = workerContext->CreateFilledGeometryRealization(
                        task.geometry.Get(),
                        flatteningTolerance,
                        created.ReleaseAndGetAddressOf()
                    );
                } else if (stroked) {
                    krok::subtitle::native::diagnostics::LongCallScope
                        realizeCall("d2d-realize", 240.0);
                    result = workerContext->CreateStrokedGeometryRealization(
                        task.geometry.Get(),
                        flatteningTolerance,
                        task.strokeWidth,
                        nullptr,
                        created.ReleaseAndGetAddressOf()
                    );
                } else {
                    result = workerContext->CreateFilledGeometryRealization(
                        task.geometry.Get(),
                        flatteningTolerance,
                        created.ReleaseAndGetAddressOf()
                    );
                }
                const double createMs = elapsedMs(createStart);
                createDurations.push_back(createMs);
                if (stroked) {
                    ++strokeTasks;
                    strokeCreateMs += createMs;
                } else {
                    ++fillTasks;
                    fillCreateMs += createMs;
                }
                if (SUCCEEDED(result)) {
                    const auto publishStart = Clock::now();
                    publish(task, std::move(created));
                    publishMs += elapsedMs(publishStart);
                } else {
                    ++failed;
                }
                yieldSlice();
                pending[pickSlot] = pending.back();
                pending.pop_back();
                ++completed;
            }
            finish();
            });
        }
    }
    if (impl_->resourceCacheEnabled) {
        while (textGlyphRealizations.size() > impl_->glyphGeometryCapacity) {
            const auto victim = std::min_element(
                textGlyphRealizations.begin(), textGlyphRealizations.end(),
                [](const auto &left, const auto &right) {
                    return left.second.lastUse < right.second.lastUse;
                }
            );
            if (victim == textGlyphRealizations.end()) {
                break;
            }
            textGlyphRealizations.erase(victim);
            ++impl_->diagnostics.glyphGeometryCacheEvictions;
        }
        while (vectorGlyphRealizations.size() > impl_->vectorGlyphCapacity) {
            const auto victim = std::min_element(
                vectorGlyphRealizations.begin(), vectorGlyphRealizations.end(),
                [](const auto &left, const auto &right) {
                    return left.second.lastUse < right.second.lastUse;
                }
            );
            if (victim == vectorGlyphRealizations.end()) {
                break;
            }
            vectorGlyphRealizations.erase(victim);
            ++impl_->diagnostics.vectorGlyphCacheEvictions;
        }
    }
    if (impl_->resourceCacheEnabled) {
        while (impl_->images.size() > impl_->imageCapacity) {
            const auto victim = std::min_element(
                impl_->images.begin(), impl_->images.end(),
                [&](const Impl::CachedImage &left, const Impl::CachedImage &right) {
                    const ImageKey leftKey{left.path, left.modifiedMs, left.size};
                    const ImageKey rightKey{right.path, right.modifiedMs, right.size};
                    const bool leftActive = activeImages.contains(leftKey);
                    const bool rightActive = activeImages.contains(rightKey);
                    if (leftActive != rightActive) {
                        return !leftActive;
                    }
                    return left.lastUse < right.lastUse;
                }
            );
            if (victim == impl_->images.end()) {
                break;
            }
            const ImageKey victimKey{
                victim->path, victim->modifiedMs, victim->size
            };
            if (activeImages.contains(victimKey)) {
                // The active scene may legitimately reference more resources
                // than the retention cap; correctness wins over the soft cap.
                break;
            }
            impl_->images.erase(victim);
            ++impl_->diagnostics.imageCacheEvictions;
        }
    }
    impl_->diagnostics.imageCacheSize = impl_->images.size();
    impl_->diagnostics.lineCount = impl_->lines.size();
    impl_->diagnostics.glyphGeometryCacheSize = textGlyphRealizations.size();
    impl_->diagnostics.vectorGlyphCacheSize = vectorGlyphRealizations.size();
    impl_->diagnostics.charCount = 0;
    impl_->diagnostics.geometryCount = 0;
    impl_->diagnostics.rubyCount = 0;
    impl_->diagnostics.styleCount = 1
        + scene.lineStyles.size()
        + scene.charStyles.size();
    impl_->diagnostics.estimatedCacheBytes = sizeof(Impl)
        + scene.lineStyles.capacity() * sizeof(TextStyle)
        + scene.charStyles.capacity() * sizeof(TextStyle);
    for (const Impl::CachedImage &image : impl_->images) {
        impl_->diagnostics.estimatedCacheBytes += sizeof(Impl::CachedImage)
            + image.path.capacity() * sizeof(wchar_t)
            + image.frameDelaysMs.capacity() * sizeof(int)
            + image.frames.capacity()
                * sizeof(Microsoft::WRL::ComPtr<ID2D1Bitmap1>);
        const auto addBitmapBytes = [&](ID2D1Bitmap1 *bitmap) {
            if (bitmap == nullptr) {
                return;
            }
            const D2D1_SIZE_U size = bitmap->GetPixelSize();
            impl_->diagnostics.estimatedCacheBytes += static_cast<std::uint64_t>(
                size.width
            ) * static_cast<std::uint64_t>(size.height) * 4;
        };
        if (image.frames.empty()) {
            addBitmapBytes(image.bitmap.Get());
        } else {
            for (const auto &frame : image.frames) {
                addBitmapBytes(frame.Get());
            }
        }
    }
    for (const Impl::CachedLine &line : impl_->lines) {
        impl_->diagnostics.charCount += line.chars.size();
        impl_->diagnostics.geometryCount += line.geometries.size();
        impl_->diagnostics.geometryCount += static_cast<std::uint64_t>(std::count_if(
            line.chars.begin(), line.chars.end(), [](const Impl::CachedChar &ch) {
                return ch.protectedStrokeGeometry != nullptr;
            }
        ));
        impl_->diagnostics.estimatedCacheBytes += sizeof(Impl::CachedLine)
            + line.chars.capacity() * sizeof(Impl::CachedChar)
            + line.geometries.capacity() * sizeof(Microsoft::WRL::ComPtr<ID2D1Geometry>);
        impl_->diagnostics.rubyCount += line.rubies.size();
        for (const Impl::CachedRuby &ruby : line.rubies) {
            impl_->diagnostics.charCount += ruby.chars.size();
            impl_->diagnostics.geometryCount += ruby.geometries.size();
            impl_->diagnostics.geometryCount += static_cast<std::uint64_t>(std::count_if(
                ruby.protectedStrokeGeometries.begin(),
                ruby.protectedStrokeGeometries.end(),
                [](const auto &geometry) { return geometry != nullptr; }
            ));
            impl_->diagnostics.estimatedCacheBytes += sizeof(Impl::CachedRuby)
                + ruby.chars.capacity() * sizeof(Impl::CachedChar)
                + ruby.geometries.capacity() * sizeof(Microsoft::WRL::ComPtr<ID2D1Geometry>)
                + ruby.protectedStrokeGeometries.capacity()
                    * sizeof(Microsoft::WRL::ComPtr<ID2D1Geometry>);
        }
    }
    // Direct2D does not expose path allocation bytes. Keep a conservative
    // diagnostic estimate so cache growth/churn remains observable.
    impl_->diagnostics.estimatedCacheBytes += impl_->diagnostics.geometryCount * 256;
    if (impl_->countersEnabled) {
        impl_->diagnostics.geometryCreatedStable += impl_->diagnostics.geometryCount;
    }
    impl_->configured = true;
}

}  // namespace krok::subtitle::native
