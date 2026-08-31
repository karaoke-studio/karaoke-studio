#include "d2d_backend.h"
#include "d2d_backend_internal.h"
#include "d2d_font_fallback.h"
#include "d2d_geometry_resources.h"
#include "d2d_paint_resources.h"
#include "d2d_runtime_support.h"
#include "../text_semantics.h"

#include <d2d1_2.h>
#include <d2d1helper.h>
#include <dwrite.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <mutex>
#include <thread>
#include <tuple>

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
using direct2d::steadyNowMs;
using direct2d::validGlyphIndices;
using direct2d::vectorGlyphGeometry;
using direct2d::widenedStrokeGeometry;
void Direct2DGpuBackend::configure(const RenderScene &scene) {
    if (scene.width <= 0 || scene.height <= 0 || scene.width > 8192 || scene.height > 8192) {
        throw BackendError("GPU scene dimensions must be within 1..8192");
    }
    if (impl_->configured && impl_->scene == scene) {
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
    }
    impl_->scene = scene;
    const float layoutScale = std::max(scene.layoutReferenceScale, 0.01f);
    const bool scaledPreviewLayout = std::abs(layoutScale - 1.0f) > 0.000001f;
    const auto referenceInt = [&](float scaledValue, int minimum) {
        const int value = scaledPreviewLayout
            ? static_cast<int>(std::lround(scaledValue / layoutScale))
            : static_cast<int>(scaledValue);
        return std::max(value, minimum);
    };
    const auto scaleReferenceGeometry = [&](Microsoft::WRL::ComPtr<ID2D1PathGeometry> &path,
                                            const char *operation) {
        if (!scaledPreviewLayout || !path) {
            return;
        }
        const D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Scale(
            layoutScale, layoutScale
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
    impl_->realizationActive = impl_->realizationEnabled
        && scene.realizationEnabled
        && impl_->realizationContext;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
    impl_->lines.clear();
    impl_->lines.reserve(scene.lines.size());
    impl_->images.clear();
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
        };
        for (const PaintStyle *paint : paints) {
            if (paint->mode != "image" || paint->imagePath.empty()) {
                continue;
            }
            const bool cached = std::any_of(
                impl_->images.begin(), impl_->images.end(),
                [&](const Impl::CachedImage &image) {
                    return image.path == paint->imagePath
                        && image.modifiedMs == paint->imageModifiedMs
                        && image.size == paint->imageSize;
                }
            );
            if (!cached) {
                impl_->images.push_back(Impl::CachedImage{
                    paint->imagePath,
                    paint->imageModifiedMs,
                    paint->imageSize,
                    loadWicBitmap(device_.d2dContext(), paint->imagePath),
                });
            }
        }
    };
    auto cacheBitmapImage = [&](const std::wstring &path,
                                std::uint64_t modifiedMs,
                                std::uint64_t size) {
        if (path.empty()) {
            return;
        }
        const bool cached = std::any_of(
            impl_->images.begin(), impl_->images.end(),
            [&](const Impl::CachedImage &image) {
                return image.path == path
                    && image.modifiedMs == modifiedMs
                    && image.size == size;
            }
        );
        if (!cached) {
            impl_->images.push_back(Impl::CachedImage{
                path,
                modifiedMs,
                size,
                loadWicBitmap(device_.d2dContext(), path),
            });
        }
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
    std::vector<Microsoft::WRL::ComPtr<IDWriteFontFace>> fallbackFaces;

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

    for (std::size_t lineIndex = 0; lineIndex < scene.lines.size(); ++lineIndex) {
        const TextLine &sourceLine = scene.lines[lineIndex];
        const TextStyle &style = lineIndex < scene.lineStyles.size()
            ? scene.lineStyles[lineIndex]
            : scene.style;
        auto resolveFace = [&](const std::wstring &family, int weight, bool italic) {
            const std::wstring resolvedFamily = family.empty() ? L"Segoe UI" : family;
            auto face = createFontFace(
                fontCollection.Get(), resolvedFamily, weight, italic
            );
            if (!face && resolvedFamily != L"Segoe UI") {
                face = createFontFace(
                    fontCollection.Get(), L"Segoe UI", weight, italic
                );
            }
            if (!face) {
                throw BackendError("DirectWrite could not resolve a usable font face");
            }
            return face;
        };
        const auto mainFace = resolveFace(style.fontFamily, style.fontWeight, style.italic);
        const auto latinFace = resolveFace(
            style.latinFontFamily.value_or(style.fontFamily),
            style.latinFontWeight.value_or(style.fontWeight),
            style.italic
        );
        const auto rubyFace = resolveFace(
            style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily,
            style.rubyFontWeight,
            style.italic
        );
        const auto rubyLatinFace = resolveFace(
            style.rubyLatinFontFamily.value_or(
                style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily
            ),
            style.rubyLatinFontWeight.value_or(style.rubyFontWeight),
            style.italic
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
        cached.guideAnchorLeft = sourceLine.guideAnchorLeft;
        cached.guideAnchorRight = sourceLine.guideAnchorRight;
        cached.centerOverride = sourceLine.centerOverride;
        cached.staticOverlay = sourceLine.staticOverlay;
        cached.fadeInMs = sourceLine.fadeInMs;
        cached.fadeOutMs = sourceLine.fadeOutMs;
        cached.entryAnimation = sourceLine.entryAnimation;
        cached.entryDurationMs = sourceLine.entryDurationMs;
        cached.exitAnimation = sourceLine.exitAnimation;
        cached.exitDurationMs = sourceLine.exitDurationMs;
        cached.karaokeAnimation = sourceLine.karaokeAnimation;
        cached.displayWindows = sourceLine.displayWindows;
        cached.placementWindows = sourceLine.placementWindows;
        DWRITE_FONT_METRICS laneMetrics{};
        mainFace->GetMetrics(&laneMetrics);
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
            rubyFace->GetMetrics(&rubyMetrics);
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
            const bool vectorGlyph = sourceChar.vectorGlyph.has_value();
            const bool bitmapGuide = sourceChar.bitmapGuide.has_value();
            const bool latin = !vectorGlyph && !bitmapGuide && isLatinText(sourceChar.text);
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
                    charStyle.italic
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
            requestedFace->GetMetrics(&fontMetrics);
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
            if (vectorGlyph) {
                path = vectorGlyphGeometry(
                    device_.d2dFactory(), *sourceChar.vectorGlyph,
                    static_cast<float>(unit), device_
                );
            } else if (!bitmapGuide) {
                if (containsEmoji(sourceChar.text)) {
                    outlineFace = createFontFace(
                        fontCollection.Get(), L"Segoe UI Symbol",
                        charStyle.fontWeight, charStyle.italic
                    );
                } else {
                    outlineFace = requestedFace;
                }
                glyphs = glyphIndices(outlineFace.Get(), sourceChar.text);
                if (!validGlyphIndices(glyphs)) {
                    outlineFace = findFallbackFontFace(
                        fontCollection.Get(), sourceChar.text, fallbackFaces, glyphs
                    );
                }
            }
            if (!vectorGlyph && !bitmapGuide && outlineFace && !glyphs.empty()) {
                checkHr(
                    device_.d2dFactory()->CreatePathGeometry(path.ReleaseAndGetAddressOf()),
                    "ID2D1Factory::CreatePathGeometry(character)",
                    device_
                );
                Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
                checkHr(path->Open(sink.ReleaseAndGetAddressOf()), "ID2D1PathGeometry::Open(character)", device_);
                sink->SetFillMode(D2D1_FILL_MODE_WINDING);
                sink->SetSegmentFlags(D2D1_PATH_SEGMENT_FORCE_ROUND_LINE_JOIN);
                const HRESULT outlineResult = outlineFace->GetGlyphRunOutline(
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
            }

            D2D1_RECT_F referenceCharBounds{};
            bool charHasBounds = path != nullptr;
            if (path) {
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

            scaleReferenceGeometry(
                path, "ID2D1Factory::CreateTransformedGeometry(scale preview character)"
            );
            D2D1_RECT_F charBounds{};
            if (path && charHasBounds) {
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
            if (charIndex + 1 < sourceLine.chars.size()) {
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
            mainFace->GetMetrics(&verticalMetrics);
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
                const bool vectorGlyph = sourceLine.chars[index].vectorGlyph.has_value();
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
            const auto selectedRubyFace = hasRubyStyle
                ? resolveFace(
                    rubyStyle.rubyFontFamily.empty()
                        ? rubyStyle.fontFamily
                        : rubyStyle.rubyFontFamily,
                    rubyStyle.rubyFontWeight,
                    rubyStyle.italic
                )
                : rubyFace;
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
                D2D1_RECT_F bounds{};
                float layoutWidth = 0.0f;
                float pathOffset = 0.0f;
            };
            std::vector<RubyGlyph> rubyGlyphs;
            rubyGlyphs.reserve(sourceRuby.units.size());
            float naturalWidth = 0.0f;
            float rubyBoxDescent = 0.0f;
            const int rubyEdgeSize = referenceInt(rubyStyle.rubyStrokeWidth, 0);
            const int rubyAnchorEdgeSize = rubyEdgeSize;

            for (const RubyUnit &sourceUnit : sourceRuby.units) {
                const bool latin = isLatinText(sourceUnit.text);
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
                drawingFace->GetMetrics(&fontMetrics);
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
                        fontCollection.Get(), sourceUnit.text, fallbackFaces, glyphs
                    );
                }
                Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
                if (outlineFace && !glyphs.empty()) {
                    checkHr(
                        device_.d2dFactory()->CreatePathGeometry(path.ReleaseAndGetAddressOf()),
                        "ID2D1Factory::CreatePathGeometry(ruby character)",
                        device_
                    );
                    Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
                    checkHr(
                        path->Open(sink.ReleaseAndGetAddressOf()),
                        "ID2D1PathGeometry::Open(ruby character)",
                        device_
                    );
                    sink->SetFillMode(D2D1_FILL_MODE_WINDING);
                    sink->SetSegmentFlags(D2D1_PATH_SEGMENT_FORCE_ROUND_LINE_JOIN);
                    const HRESULT outlineResult = outlineFace->GetGlyphRunOutline(
                        static_cast<float>(drawingUnit),
                        glyphs.data(),
                        nullptr,
                        nullptr,
                        static_cast<UINT32>(glyphs.size()),
                        FALSE,
                        FALSE,
                        sink.Get()
                    );
                    const HRESULT closeResult = sink->Close();
                    checkHr(outlineResult, "IDWriteFontFace::GetGlyphRunOutline(ruby)", device_);
                    checkHr(closeResult, "ID2D1GeometrySink::Close(ruby character)", device_);
                }

                D2D1_RECT_F referenceRubyBounds{};
                bool hasBounds = path != nullptr;
                if (path) {
                    checkHr(
                        path->GetBounds(nullptr, &referenceRubyBounds),
                        "ID2D1Geometry::GetBounds(ruby character)",
                        device_
                    );
                    hasBounds = std::isfinite(referenceRubyBounds.left)
                        && std::isfinite(referenceRubyBounds.right)
                        && referenceRubyBounds.right > referenceRubyBounds.left;
                }
                RubyGlyph glyph;
                glyph.source = &sourceUnit;
                if (hasBounds) {
                    std::vector<UINT16> measureGlyphs = glyphIndices(
                        measureFace.Get(), sourceUnit.text
                    );
                    Microsoft::WRL::ComPtr<IDWriteFontFace> metricFace = measureFace;
                    if (!validGlyphIndices(measureGlyphs)) {
                        metricFace = findFallbackFontFace(
                            fontCollection.Get(), sourceUnit.text, fallbackFaces, measureGlyphs
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
                scaleReferenceGeometry(
                    path, "ID2D1Factory::CreateTransformedGeometry(scale preview ruby)"
                );
                glyph.geometry = path;
                if (path && hasBounds) {
                    checkHr(
                        path->GetBounds(nullptr, &glyph.bounds),
                        "ID2D1Geometry::GetBounds(scaled preview ruby)",
                        device_
                    );
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
                    isAsciiAlnumText(sourceRuby.baseText)
                    || isAsciiAlnumText(sourceRuby.reading)
                ));
            float gap = rubyStyle.rubyInterval;
            if (!centered && rubyGlyphs.size() > 1) {
                const float slots = targetWidth <= naturalWidth
                    ? static_cast<float>(rubyGlyphs.size() - 1)
                    : static_cast<float>(rubyGlyphs.size() + 1);
                gap = std::max(
                    (targetWidth - naturalWidth) / std::max(slots, 1.0f),
                    rubyStyle.rubyInterval
                );
            }
            const float contentWidth = naturalWidth
                + gap * static_cast<float>(rubyGlyphs.size() - 1);
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
                    layoutCursor += gap;
                }
            }

            Impl::CachedRuby ruby;
            ruby.startMs = sourceRuby.startMs;
            ruby.endMs = sourceRuby.endMs;
            ruby.styleIndex = sourceRuby.styleIndex;
            ruby.transitionCharIndex = sourceRuby.firstCharIndex;
            ruby.firstCharIndex = sourceRuby.firstCharIndex;
            ruby.lastCharIndex = sourceRuby.lastCharIndex;
            ruby.baselineOffset = -cached.boxAscent - style.rubyGap - rubyBoxDescent;
            DWRITE_FONT_METRICS rubyFillMetrics{};
            const auto &rubyFillFace = rubyIsLatin
                ? selectedRubyLatinFace
                : selectedRubyFace;
            rubyFillFace->GetMetrics(&rubyFillMetrics);
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
            const int rubyDrawEdge = referenceInt(rubyStyle.rubyStrokeWidth, 0);
            const int rubyDrawEdge2 = referenceInt(rubyStyle.rubyStroke2Width, 0);
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
                    const D2D1_MATRIX_3X2_F position = D2D1::Matrix3x2F::Translation(
                        origin + glyph.pathOffset,
                        ruby.baselineOffset
                    );
                    Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> positioned;
                    checkHr(
                        device_.d2dFactory()->CreateTransformedGeometry(
                            glyph.geometry.Get(),
                            &position,
                            positioned.ReleaseAndGetAddressOf()
                        ),
                        "ID2D1Factory::CreateTransformedGeometry(position ruby character)",
                        device_
                    );
                    checkHr(
                        positioned->GetBounds(nullptr, &positionedBounds),
                        "ID2D1Geometry::GetBounds(positioned ruby character)",
                        device_
                    );
                    positionedHasBounds = positionedBounds.right > positionedBounds.left;
                    if (positionedHasBounds) {
                        extendBounds(ruby.bounds, rubyHasBounds, positionedBounds);
                    }
                    ruby.geometries.push_back(positioned);
                    ruby.strokeGeometries.push_back(widenedStrokeGeometry(
                        device_.d2dFactory(), positioned.Get(),
                        rubyStyle.rubyStrokeWidth, device_
                    ));
                    ruby.stroke2Geometries.push_back(widenedStrokeGeometry(
                        device_.d2dFactory(), positioned.Get(),
                        rubyStyle.rubyStroke2Width > 0.0f
                            ? std::max(rubyStyle.rubyStrokeWidth, 0.0f)
                                + rubyStyle.rubyStroke2Width
                            : 0.0f,
                        device_
                    ));
                    if (rubyStyle.rubyStrokeWidth > 0.0f
                        && (paintNeedsBodyProtection(rubyStyle.rubyBeforeFillPaint)
                            || paintNeedsBodyProtection(rubyStyle.rubyAfterFillPaint))) {
                        ruby.protectedStrokeGeometries.push_back(
                            outsideStrokeGeometry(
                                device_.d2dFactory(),
                                positioned.Get(),
                                rubyStyle.rubyStrokeWidth,
                                device_
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
                ruby.chars.back().pivotX = origin + glyph.layoutWidth * 0.5f;
                ruby.chars.back().pivotY = ruby.pivotY;
                ruby.chars.back().wipePoints = {
                    WipePoint{glyph.source->startMs, 0.0f},
                    WipePoint{glyph.source->endMs, 1.0f},
                };
            }
            if (style.vertical && rubyHasBounds && !ruby.geometries.empty()) {
                const float mainCellWidth = std::max(style.fontSize, 1.0f);
                DWRITE_FONT_METRICS mainVerticalMetrics{};
                mainFace->GetMetrics(&mainVerticalMetrics);
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
                selectedRubyFace->GetMetrics(&rubyVerticalMetrics);
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
            const bool rtl = style.rightToLeft;
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
        if (!cached.rubies.empty()) {
            D2D1_RECT_F sharedHorizontalBounds = cached.fillBounds;
            for (const Impl::CachedRuby &ruby : cached.rubies) {
                sharedHorizontalBounds.top = std::min(
                    sharedHorizontalBounds.top, ruby.fillBounds.top
                );
                sharedHorizontalBounds.bottom = std::max(
                    sharedHorizontalBounds.bottom, ruby.fillBounds.bottom
                );
            }
            sharedHorizontalBounds.right = std::max(
                sharedHorizontalBounds.right,
                sharedHorizontalBounds.left + 1.0f
            );
            sharedHorizontalBounds.bottom = std::max(
                sharedHorizontalBounds.bottom,
                sharedHorizontalBounds.top + 1.0f
            );
            for (Impl::CachedRuby &ruby : cached.rubies) {
                const TextStyle &rubyStyle = ruby.styleIndex >= 0
                    && ruby.styleIndex < static_cast<int>(scene.charStyles.size())
                    ? scene.charStyles[static_cast<std::size_t>(ruby.styleIndex)]
                    : style;
                ruby.horizontalFillBounds = rubyStyle.rubyHorizontalGradientWithMain
                    ? sharedHorizontalBounds
                    : ruby.fillBounds;
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
        std::vector<Impl::RealizationTask> tasks;
        const std::size_t realizationCapacity = static_cast<std::size_t>(
            std::max<std::uint64_t>(
                impl_->scene.realizationCapacity,
                Impl::defaultRealizationCapacity
            )
        );
        impl_->diagnostics.realizationCapacity = realizationCapacity;
        tasks.reserve(realizationCapacity);
        std::uint64_t capacitySkipped = 0;
        const auto appendTask = [&] (
            std::size_t lineIndex,
            int rubyIndex,
            std::size_t charIndex,
            Impl::RealizationKind kind,
            ID2D1Geometry *geometry,
            float strokeWidth
        ) {
            const bool isStroke = kind == Impl::RealizationKind::Stroke
                || kind == Impl::RealizationKind::Stroke2;
            if (geometry == nullptr
                || (isStroke && strokeWidth <= 0.0f)) {
                return;
            }
            if (tasks.size() >= realizationCapacity) {
                ++capacitySkipped;
                return;
            }
            Impl::RealizationTask task;
            task.lineIndex = lineIndex;
            task.rubyIndex = rubyIndex;
            task.charIndex = charIndex;
            task.kind = kind;
            task.geometry = geometry;
            task.strokeWidth = strokeWidth;
            tasks.push_back(std::move(task));
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
            if (mainWidth < Impl::realizationStrokeThreshold) {
                return;
            }
            appendTask(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::Fill, ch.geometry.Get(), 0.0f
            );
            appendTask(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::ProtectedStroke,
                ch.protectedStrokeGeometry.Get(), 0.0f
            );
            appendTask(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::Stroke, ch.geometry.Get(), mainWidth
            );
            appendTask(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::Stroke2, ch.geometry.Get(),
                stroke2Width > 0.0f ? mainWidth + stroke2Width : 0.0f
            );
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
                        rubyStyle.rubyStrokeWidth,
                        rubyStyle.rubyStroke2Width
                    );
                }
            }
        }
        impl_->diagnostics.realizationPrewarmSkipped = capacitySkipped;
        impl_->diagnostics.realizationPrewarmTasks = tasks.size();
        auto control = std::make_shared<Impl::RealizationControl>();
        control->generation = impl_->realizationGeneration;
        impl_->realizationControl = control;
        impl_->realizationPrewarmComplete.store(false, std::memory_order_release);
        const bool deferUntilFirstFrame =
            impl_->scene.deferRealizationPrewarmUntilFirstFrame;
        impl_->realizationThread = std::thread([
            this,
            control,
            deferUntilFirstFrame,
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
                    impl_->realizationPrewarmComplete.store(
                        true, std::memory_order_release
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
                if (shouldStop() || !isCurrent()
                    || task.lineIndex >= impl_->lines.size()) {
                    return false;
                }
                Impl::CachedLine &line = impl_->lines[task.lineIndex];
                Impl::CachedChar *targetChar = nullptr;
                if (task.rubyIndex < 0) {
                    if (task.charIndex < line.chars.size()) {
                        targetChar = &line.chars[task.charIndex];
                    }
                } else if (static_cast<std::size_t>(task.rubyIndex)
                           < line.rubies.size()) {
                    Impl::CachedRuby &ruby = line.rubies[
                        static_cast<std::size_t>(task.rubyIndex)
                    ];
                    if (task.charIndex < ruby.chars.size()) {
                        targetChar = &ruby.chars[task.charIndex];
                    }
                }
                if (targetChar == nullptr) {
                    return false;
                }
                switch (task.kind) {
                case Impl::RealizationKind::Fill:
                    targetChar->fillRealization = std::move(created);
                    break;
                case Impl::RealizationKind::ProtectedStroke:
                    targetChar->protectedStrokeRealization = std::move(created);
                    break;
                case Impl::RealizationKind::Stroke:
                    targetChar->strokeRealization = std::move(created);
                    break;
                case Impl::RealizationKind::Stroke2:
                    targetChar->stroke2Realization = std::move(created);
                    break;
                }
                ++impl_->realizationCount;
                return true;
            };
            const auto yieldSlice = [&]() {
                if (elapsedMs(sliceStart) >= 50.0) {
                    std::this_thread::yield();
                    sliceStart = Clock::now();
                }
            };
            for (const Impl::RealizationTask &task : tasks) {
                const auto waitStart = Clock::now();
                if (!waitForFrameGap()) {
                    break;
                }
                waitMs += elapsedMs(waitStart);
                Microsoft::WRL::ComPtr<ID2D1GeometryRealization> created;
                HRESULT result = E_FAIL;
                const bool stroked = task.kind == Impl::RealizationKind::Stroke
                    || task.kind == Impl::RealizationKind::Stroke2;
                const auto createStart = Clock::now();
                if (stroked) {
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
            }
            finish();
        });
    }
    impl_->diagnostics.lineCount = impl_->lines.size();
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
            + image.path.capacity() * sizeof(wchar_t);
        if (image.bitmap) {
            const D2D1_SIZE_U size = image.bitmap->GetPixelSize();
            impl_->diagnostics.estimatedCacheBytes += static_cast<std::uint64_t>(
                size.width
            ) * static_cast<std::uint64_t>(size.height) * 4;
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
