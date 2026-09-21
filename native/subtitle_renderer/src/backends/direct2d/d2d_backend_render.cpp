#include "d2d_backend.h"
#include "d2d_backend_internal.h"
#include "d2d_geometry_resources.h"
#include "d2d_opacity_layer.h"
#include "d2d_paint_resources.h"
#include "d2d_runtime_support.h"
#include "../signal_state.h"

#include <d2d1_2.h>
#include <d2d1effects.h>
#include <d2d1helper.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <limits>
#include <mutex>
#include <tuple>

namespace krok::subtitle::native {

using Clock = direct2d::RuntimeClock;
using direct2d::checkHr;
using direct2d::createPaintBrush;
using direct2d::elapsedMs;
using direct2d::OpacityLayerScope;
using direct2d::paintNeedsBodyProtection;
using direct2d::rectAreaPx;
using direct2d::rubyPaintBounds;
using direct2d::steadyNowMs;
using direct2d::updatePaintBrush;

namespace {

RgbaColor brightenHsvValue(const RgbaColor &source, float amount) {
    amount = std::clamp(amount, 0.0f, 1.0f);
    const float maximum = static_cast<float>(std::max({
        source.red, source.green, source.blue
    }));
    const float raised = maximum + (255.0f - maximum) * amount;
    if (maximum <= 0.0f) {
        const auto value = static_cast<std::uint8_t>(std::lround(raised));
        return RgbaColor{value, value, value, source.alpha};
    }
    const float scale = raised / maximum;
    const auto channel = [&](std::uint8_t value) {
        return static_cast<std::uint8_t>(std::lround(std::min(
            static_cast<float>(value) * scale, 255.0f
        )));
    };
    return RgbaColor{
        channel(source.red), channel(source.green), channel(source.blue), source.alpha
    };
}

PaintStyle brightenPaintHsvValue(const PaintStyle &source, float amount) {
    PaintStyle result = source;
    result.color = brightenHsvValue(result.color, amount);
    for (PaintStop &stop : result.stops) {
        stop.color = brightenHsvValue(stop.color, amount);
    }
    return result;
}

PaintStyle solidPaint(const RgbaColor &color) {
    PaintStyle result;
    result.mode = "solid";
    result.color = color;
    return result;
}

}  // namespace

ProbeResult Direct2DGpuBackend::renderFrame(int tMs, bool compactBands) {
    return renderFrameInternal(tMs, compactBands, true);
}

ProbeResult Direct2DGpuBackend::renderFrameInternal(
    int tMs,
    bool compactBands,
    bool readback
) {
    if (!impl_->configured) {
        throw BackendError("GPU backend is not configured");
    }
    impl_->renderActive.store(true, std::memory_order_release);
    struct RenderActivityGuard {
        Impl *impl = nullptr;
        ~RenderActivityGuard() {
            impl->lastRenderCompletedMs.store(
                steadyNowMs(), std::memory_order_release
            );
            impl->firstFrameCompleted.store(true, std::memory_order_release);
            impl->renderActive.store(false, std::memory_order_release);
        }
    } renderActivityGuard{impl_.get()};
    // The prewarmer builds realizations on a second DeviceContext and only
    // publishes completed COM resources between frames. Holding this lock for
    // the frame keeps CachedChar realization slots race-free without blocking
    // the expensive creation work itself.
    std::lock_guard<std::mutex> realizationLock(impl_->realizationMutex);
    const RenderScene &scene = impl_->scene;
    const TextStyle &baseStyle = scene.style;
    ProbeResult::FrameDiagnostics frameDiagnostics;
    frameDiagnostics.countersEnabled = impl_->countersEnabled;
    const auto count = [&](std::uint64_t &counter, std::uint64_t amount = 1) {
        if (impl_->countersEnabled) {
            counter += amount;
        }
    };
    const auto finalizeDiagnostics = [&](ProbeResult &result) {
        result.frameDiagnostics = frameDiagnostics;
        if (!impl_->countersEnabled) {
            return;
        }
        BackendDiagnostics &total = impl_->diagnostics;
        ++total.framesRendered;
        total.brushCreated += frameDiagnostics.brushCreated;
        total.geometryCreatedStable += frameDiagnostics.geometryCreatedStable;
        total.geometryCreatedDynamic += frameDiagnostics.geometryCreatedDynamic;
        total.realizationHit += frameDiagnostics.realizationHit;
        total.realizationMiss += frameDiagnostics.realizationMiss;
        total.strokeDraw += frameDiagnostics.strokeDraw;
        total.stroke2Draw += frameDiagnostics.stroke2Draw;
        total.glowSourceAreaPx += frameDiagnostics.glowSourceAreaPx;
        total.layerPush += frameDiagnostics.layerPush;
        total.animationLayoutMs += frameDiagnostics.animationLayoutMs;
        total.geometryMs += frameDiagnostics.geometryMs;
        total.strokeMs += frameDiagnostics.strokeMs;
        total.glowMs += frameDiagnostics.glowMs;
        total.gpuWaitMs += frameDiagnostics.gpuWaitMs;
        total.readbackCopyMs += frameDiagnostics.readbackCopyMs;
    };

    D3D11_TEXTURE2D_DESC targetDesc{};
    targetDesc.Width = static_cast<UINT>(scene.width);
    targetDesc.Height = static_cast<UINT>(scene.height);
    targetDesc.MipLevels = 1;
    targetDesc.ArraySize = 1;
    targetDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    targetDesc.SampleDesc.Count = 1;
    targetDesc.Usage = D3D11_USAGE_DEFAULT;
    targetDesc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;

    const D2D1_BITMAP_PROPERTIES1 bitmapProperties = D2D1::BitmapProperties1(
        D2D1_BITMAP_OPTIONS_TARGET,
        D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED),
        96.0f,
        96.0f
    );
    if (!impl_->frameTargetTexture || !impl_->frameTargetBitmap) {
        checkHr(
            device_.d3dDevice()->CreateTexture2D(
                &targetDesc,
                nullptr,
                impl_->frameTargetTexture.ReleaseAndGetAddressOf()
            ),
            "ID3D11Device::CreateTexture2D(frame target)",
            device_
        );
        Microsoft::WRL::ComPtr<IDXGISurface> targetSurface;
        checkHr(
            impl_->frameTargetTexture.As(&targetSurface),
            "Query frame target IDXGISurface",
            device_
        );
        checkHr(
            device_.d2dContext()->CreateBitmapFromDxgiSurface(
                targetSurface.Get(),
                &bitmapProperties,
                impl_->frameTargetBitmap.ReleaseAndGetAddressOf()
            ),
            "ID2D1DeviceContext::CreateBitmapFromDxgiSurface(frame)",
            device_
        );
    }
    if (readback && !impl_->frameStagingTexture) {
        D3D11_TEXTURE2D_DESC stagingDesc = targetDesc;
        stagingDesc.Usage = D3D11_USAGE_STAGING;
        stagingDesc.BindFlags = 0;
        stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        checkHr(
            device_.d3dDevice()->CreateTexture2D(
                &stagingDesc,
                nullptr,
                impl_->frameStagingTexture.ReleaseAndGetAddressOf()
            ),
            "ID3D11Device::CreateTexture2D(frame staging)",
            device_
        );
    }
    ID3D11Texture2D *targetTexture = impl_->frameTargetTexture.Get();
    ID2D1Bitmap1 *targetBitmap = impl_->frameTargetBitmap.Get();

    const auto renderStart = Clock::now();
    ID2D1DeviceContext *context = device_.d2dContext();
    auto endDrawMeasured = [&](
        const char *operation,
        double &phaseMs,
        std::uint64_t &phaseCount
    ) {
        const auto started = Clock::now();
        const HRESULT result = context->EndDraw();
        const double durationMs = elapsedMs(started);
        frameDiagnostics.endDrawWaitMs += durationMs;
        phaseMs += durationMs;
        ++frameDiagnostics.endDrawCount;
        ++phaseCount;
        checkHr(result, operation, device_);
    };
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    context->SetTarget(nullptr);

    // Glow scratch bitmaps and GaussianBlur effects live on impl_ so steady
    // state playback allocates nothing; entries rewind per line once the
    // line's composite has been flushed to the frame target.
    impl_->glowScratchInUse = 0;
    impl_->glowEffectInUse = 0;
    impl_->decorTintEffectInUse = 0;
    impl_->decorBlurEffectInUse = 0;
    impl_->decorCompositeEffectInUse = 0;
    auto acquireGlowScratch = [&](float requestedWidth,
                                  float requestedHeight) -> ID2D1Bitmap1 * {
        const UINT32 width = impl_->glowDirtyRectEnabled
            ? static_cast<UINT32>(std::max(
                std::ceil(static_cast<double>(requestedWidth)), 1.0
            ))
            : static_cast<UINT32>(scene.width);
        const UINT32 height = impl_->glowDirtyRectEnabled
            ? static_cast<UINT32>(std::max(
                std::ceil(static_cast<double>(requestedHeight)), 1.0
            ))
            : static_cast<UINT32>(scene.height);
        const std::size_t index = impl_->glowScratchInUse++;
        if (index >= impl_->glowScratchPool.size()) {
            impl_->glowScratchPool.emplace_back();
        }
        Impl::GlowScratch &scratch = impl_->glowScratchPool[index];
        if (scratch.bitmap
            && scratch.width >= width
            && scratch.height >= height) {
            return scratch.bitmap.Get();
        }
        scratch.width = std::max(scratch.width, width);
        scratch.height = std::max(scratch.height, height);
        scratch.bitmap.Reset();
        checkHr(
            context->CreateBitmap(
                D2D1::SizeU(
                    scratch.width,
                    scratch.height
                ),
                nullptr,
                0,
                &bitmapProperties,
                scratch.bitmap.ReleaseAndGetAddressOf()
            ),
            "ID2D1DeviceContext::CreateBitmap(glow scratch)",
            device_
        );
        return scratch.bitmap.Get();
    };
    auto acquireGlowEffect = [&]() -> ID2D1Effect * {
        if (impl_->glowEffectInUse < impl_->glowEffectPool.size()) {
            return impl_->glowEffectPool[impl_->glowEffectInUse++].Get();
        }
        Microsoft::WRL::ComPtr<ID2D1Effect> effect;
        checkHr(
            context->CreateEffect(
                CLSID_D2D1GaussianBlur, effect.ReleaseAndGetAddressOf()
            ),
            "ID2D1DeviceContext::CreateEffect(GaussianBlur)",
            device_
        );
        impl_->glowEffectPool.push_back(effect);
        ++impl_->glowEffectInUse;
        return impl_->glowEffectPool.back().Get();
    };

    const bool hasViewportTransform = scene.viewportScale != 1.0f
        || scene.viewportRotation != 0.0f
        || scene.viewportOffsetX != 0.0f
        || scene.viewportOffsetY != 0.0f;
    float viewportFractionX = 0.5f;
    float viewportFractionY = 0.5f;
    if (scene.viewportAlign.find("left") != std::string::npos) {
        viewportFractionX = 0.0f;
    } else if (scene.viewportAlign.find("right") != std::string::npos) {
        viewportFractionX = 1.0f;
    }
    if (scene.viewportAlign.find("top") != std::string::npos) {
        viewportFractionY = 0.0f;
    } else if (scene.viewportAlign.find("bottom") != std::string::npos) {
        viewportFractionY = 1.0f;
    }
    const D2D1_POINT_2F viewportPivot = D2D1::Point2F(
        static_cast<float>(scene.width) * viewportFractionX,
        static_cast<float>(scene.height) * viewportFractionY
    );
    const D2D1_MATRIX_3X2_F viewportTransform = hasViewportTransform
        ? D2D1::Matrix3x2F::Translation(-viewportPivot.x, -viewportPivot.y)
            * D2D1::Matrix3x2F::Scale(scene.viewportScale, scene.viewportScale)
            * D2D1::Matrix3x2F::Rotation(scene.viewportRotation)
            * D2D1::Matrix3x2F::Translation(
                viewportPivot.x + scene.viewportOffsetX,
                viewportPivot.y + scene.viewportOffsetY
            )
        : D2D1::Matrix3x2F::Identity();
    auto overlayOpacityAt = [&](const Impl::CachedLine &line) {
        if (!line.staticOverlay) {
            return 1.0f;
        }
        float best = 0.0f;
        for (const DisplayWindow &window : line.displayWindows) {
            if (window.endMs <= window.startMs
                || tMs < window.startMs
                || tMs > window.endMs) {
                continue;
            }
            float opacity = 1.0f;
            const int fadeInMs = window.fadeInMs >= 0
                ? window.fadeInMs
                : line.fadeInMs;
            const int fadeOutMs = window.fadeOutMs >= 0
                ? window.fadeOutMs
                : line.fadeOutMs;
            if (fadeInMs > 0 && tMs < window.startMs + fadeInMs) {
                opacity = std::min(
                    opacity,
                    static_cast<float>(tMs - window.startMs)
                        / static_cast<float>(fadeInMs)
                );
            }
            if (fadeOutMs > 0 && tMs > window.endMs - fadeOutMs) {
                opacity = std::min(
                    opacity,
                    static_cast<float>(window.endMs - tMs)
                        / static_cast<float>(fadeOutMs)
                );
            }
            best = std::max(best, std::clamp(opacity, 0.0f, 1.0f));
        }
        return best;
    };
    struct LineAnimationState {
        float opacity = 1.0f;
        float dx = 0.0f;
        float dy = 0.0f;
    };
    auto lineAnimationAt = [&](const Impl::CachedLine &line) {
        LineAnimationState state;
        if (line.staticOverlay || line.displayWindows.empty()) {
            return state;
        }
        const DisplayWindow &window = line.displayWindows.front();
        const auto progress = [](int elapsedMs, int durationMs) {
            if (durationMs <= 0) {
                return 1.0f;
            }
            return std::clamp(
                static_cast<float>(elapsedMs) / static_cast<float>(durationMs),
                0.0f,
                1.0f
            );
        };
        if (line.entryAnimation != "none" && line.entryDurationMs > 0) {
            const float linear = progress(tMs - window.startMs, line.entryDurationMs);
            const float eased = 1.0f - (1.0f - linear) * (1.0f - linear);
            if (line.entryAnimation == "fade") {
                state.opacity *= eased;
            } else if (line.entryAnimation == "slide_in") {
                state.opacity *= eased;
                const float direction = line.lane == 0 ? -1.0f : 1.0f;
                state.dx += direction * (1.0f - eased)
                    * std::max(line.style.fontSize * 0.9f, 36.0f);
            } else if (line.entryAnimation == "rise") {
                state.opacity *= eased;
                state.dy += (1.0f - eased)
                    * std::max(line.style.fontSize * 0.35f, 18.0f);
            }
        }
        if (line.exitAnimation != "none" && line.exitDurationMs > 0) {
            const float linear = progress(window.endMs - tMs, line.exitDurationMs);
            const float eased = linear * linear;
            if (line.exitAnimation == "fade") {
                state.opacity *= eased;
            } else if (line.exitAnimation == "slide_out") {
                state.opacity *= eased;
                const float direction = line.lane == 0 ? -1.0f : 1.0f;
                state.dx += direction * (1.0f - eased)
                    * std::max(line.style.fontSize * 0.9f, 36.0f);
            } else if (line.exitAnimation == "rise") {
                state.opacity *= eased;
                state.dy -= (1.0f - eased)
                    * std::max(line.style.fontSize * 0.35f, 18.0f);
            }
        }
        state.opacity = std::clamp(state.opacity, 0.0f, 1.0f);
        return state;
    };
    std::vector<const Impl::CachedLine *> activeLines;
    for (const Impl::CachedLine &candidate : impl_->lines) {
        const bool resolvedWindowVisible = !candidate.displayWindows.empty()
            && std::any_of(
                candidate.displayWindows.begin(), candidate.displayWindows.end(),
                [&](const DisplayWindow &window) {
                    return window.endMs > window.startMs
                        && tMs >= window.startMs
                        && tMs < window.endMs;
                }
            );
        const bool visible = candidate.staticOverlay
            ? overlayOpacityAt(candidate) > 0.0f
            : (!candidate.displayWindows.empty() ? resolvedWindowVisible : (
                tMs >= candidate.startMs - std::max(baseStyle.leadInMs, 0)
                && tMs < candidate.endMs + std::max(baseStyle.tailMs, 0)
            ));
        if (visible) {
            const bool sourceLineAlreadyActive = std::any_of(
                activeLines.begin(), activeLines.end(),
                [&](const Impl::CachedLine *line) {
                    return line->sourceIndex == candidate.sourceIndex
                        && line->sourceLineIndex == candidate.sourceLineIndex;
                }
            );
            if (!sourceLineAlreadyActive) {
                activeLines.push_back(&candidate);
            }
        }
    }
    std::stable_sort(
        activeLines.begin(), activeLines.end(),
        [](const Impl::CachedLine *left, const Impl::CachedLine *right) {
            return left->compositeOrder < right->compositeOrder;
        }
    );
    frameDiagnostics.animationLayoutMs += elapsedMs(renderStart);
    bool renderedAnyLine = false;
    std::vector<std::pair<int, int>> readbackIntervals;
    D2D1_MATRIX_3X2_F realizationBaseTransform =
        D2D1::Matrix3x2F::Identity();
    bool sharedInstanceTransformActive = false;
    const auto restoreRealizationBaseTransform = [&]() {
        if (sharedInstanceTransformActive) {
            context->SetTransform(realizationBaseTransform);
            sharedInstanceTransformActive = false;
        }
    };
    const auto pushAxisAlignedClip = [&] (
        const D2D1_RECT_F &rect,
        D2D1_ANTIALIAS_MODE antialiasMode
    ) {
        restoreRealizationBaseTransform();
        count(frameDiagnostics.layerPush);
        context->PushAxisAlignedClip(rect, antialiasMode);
    };
    const auto drawCountedStroke = [&] (
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        float width,
        bool secondStroke
    ) {
        restoreRealizationBaseTransform();
        const auto start = Clock::now();
        context->DrawGeometry(geometry, brush, width);
        frameDiagnostics.strokeMs += elapsedMs(start);
        count(
            secondStroke
                ? frameDiagnostics.stroke2Draw
                : frameDiagnostics.strokeDraw
        );
    };
    const auto fillCountedStroke = [&] (
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        bool secondStroke
    ) {
        restoreRealizationBaseTransform();
        const auto start = Clock::now();
        context->FillGeometry(geometry, brush);
        frameDiagnostics.strokeMs += elapsedMs(start);
        count(
            secondStroke
                ? frameDiagnostics.stroke2Draw
                : frameDiagnostics.strokeDraw
        );
    };
    const auto drawSharedRealization = [&] (
        ID2D1GeometryRealization *realization,
        ID2D1Brush *brush,
        const D2D1_MATRIX_3X2_F &instanceTransform
    ) {
        const bool identity = instanceTransform._11 == 1.0f
            && instanceTransform._12 == 0.0f
            && instanceTransform._21 == 0.0f
            && instanceTransform._22 == 1.0f
            && instanceTransform._31 == 0.0f
            && instanceTransform._32 == 0.0f;
        if (identity) {
            restoreRealizationBaseTransform();
            impl_->realizationContext->DrawGeometryRealization(
                realization, brush
            );
            return;
        }
        context->SetTransform(instanceTransform * realizationBaseTransform);
        sharedInstanceTransformActive = true;
        impl_->realizationContext->DrawGeometryRealization(realization, brush);
    };
    const auto fillWithRealization = [&] (
        ID2D1GeometryRealization *realization,
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        const D2D1_MATRIX_3X2_F &instanceTransform,
        bool eligible
    ) {
        if (eligible && impl_->realizationActive && realization != nullptr) {
            drawSharedRealization(realization, brush, instanceTransform);
            count(frameDiagnostics.realizationHit);
            return;
        }
        if (impl_->realizationActive && eligible) {
            count(frameDiagnostics.realizationMiss);
        }
        restoreRealizationBaseTransform();
        context->FillGeometry(geometry, brush);
    };
    const auto strokeWithRealization = [&] (
        ID2D1GeometryRealization *realization,
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        float width,
        bool secondStroke,
        const D2D1_MATRIX_3X2_F &instanceTransform,
        bool eligible
    ) {
        const auto start = Clock::now();
        if (eligible && impl_->realizationActive && realization != nullptr) {
            drawSharedRealization(realization, brush, instanceTransform);
            count(frameDiagnostics.realizationHit);
        } else {
            if (impl_->realizationActive && eligible) {
                count(frameDiagnostics.realizationMiss);
            }
            restoreRealizationBaseTransform();
            context->DrawGeometry(geometry, brush, width);
        }
        frameDiagnostics.strokeMs += elapsedMs(start);
        count(
            secondStroke
                ? frameDiagnostics.stroke2Draw
                : frameDiagnostics.strokeDraw
        );
    };
    const auto fillStrokeWithRealization = [&] (
        ID2D1GeometryRealization *realization,
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        bool secondStroke,
        const D2D1_MATRIX_3X2_F &instanceTransform,
        bool eligible
    ) {
        const auto start = Clock::now();
        fillWithRealization(
            realization, geometry, brush, instanceTransform, eligible
        );
        frameDiagnostics.strokeMs += elapsedMs(start);
        count(
            secondStroke
                ? frameDiagnostics.stroke2Draw
                : frameDiagnostics.strokeDraw
        );
    };
    for (const Impl::CachedLine *line : activeLines) {
      if (line != nullptr && (
        !line->geometries.empty()
        || std::any_of(
            line->chars.begin(), line->chars.end(),
            [](const Impl::CachedChar &ch) { return ch.bitmapGuide.has_value(); }
        )
      )) {
        const LineAnimationState animation = lineAnimationAt(*line);
        float placementOffsetX = 0.0f;
        float placementOffsetY = 0.0f;
        for (const PlacementWindow &window : line->placementWindows) {
            if (tMs >= window.startMs && tMs < window.endMs) {
                placementOffsetX = window.offsetX;
                placementOffsetY = window.offsetY;
                break;
            }
        }
        if (animation.opacity <= 0.0f) {
            continue;
        }
        // Fade the composed line, not every brush inside it (see
        // OpacityLayerScope).  If the layer cannot be created, fall back to the
        // legacy per-brush opacity rather than dropping the line.
        OpacityLayerScope lineOpacityLayer;
        float lineAnimationOpacity = animation.opacity;
        if (lineAnimationOpacity < 1.0f
            && lineOpacityLayer.prepare(context, lineAnimationOpacity)) {
            lineAnimationOpacity = 1.0f;
        }
        const float globalOpacity = overlayOpacityAt(*line) * lineAnimationOpacity;
        const TextStyle &style = line->style;
        // Painter restores the viewport transform before drawing the title
        // overlay, so static title lines stay in screen coordinates.
        const D2D1_MATRIX_3X2_F lineViewportTransform = line->staticOverlay
            ? D2D1::Matrix3x2F::Identity()
            : viewportTransform;
        auto withViewport = [&](const D2D1_MATRIX_3X2_F &local) {
            return local * lineViewportTransform;
        };
        const bool hasCharacterTransition = line->entryAnimation == "char_fade"
            || line->exitAnimation == "char_fade"
            || line->entryAnimation == "char_drip"
            || line->exitAnimation == "char_drip"
            || line->entryAnimation == "spin_flip"
            || line->exitAnimation == "spin_flip"
            || line->entryAnimation == "utopia"
            || line->exitAnimation == "utopia"
            || line->karaokeAnimation == "utopia";
        const bool hasUtopiaTransition = line->entryAnimation == "utopia"
            || line->exitAnimation == "utopia"
            || line->karaokeAnimation == "utopia";
        std::string activeCharacterTransition;
        int activeCharacterDirection = 0;
        if (!line->displayWindows.empty()) {
            const DisplayWindow &window = line->displayWindows.front();
            if ((line->exitAnimation == "char_fade"
                    || line->exitAnimation == "char_drip"
                    || line->exitAnimation == "spin_flip")
                && line->exitDurationMs > 0
                && tMs >= std::max(line->endMs, window.endMs - 600)) {
                activeCharacterTransition = line->exitAnimation;
                activeCharacterDirection = 1;
            } else if ((line->entryAnimation == "char_fade"
                    || line->entryAnimation == "char_drip"
                    || line->entryAnimation == "spin_flip")
                && line->entryDurationMs > 0
                && tMs <= window.startMs + 600) {
                activeCharacterTransition = line->entryAnimation;
                activeCharacterDirection = -1;
            }
        }
        // Painter owns one per-character transition context at a time.  Utopia
        // remains the steady-state path, but an active char fade/spin on the
        // opposite side must temporarily take precedence.
        const bool useUtopiaTransition = hasUtopiaTransition
            && activeCharacterTransition.empty();
        auto charFadeOpacityAt = [&](std::size_t charIndex) {
            if (!hasCharacterTransition || line->displayWindows.empty()) {
                return 1.0f;
            }
            const int count = std::max(static_cast<int>(line->chars.size()), 1);
            const int index = std::clamp(
                static_cast<int>(charIndex), 0, count - 1
            );
            const int delayStep = count <= 1 ? 0 : 350 / (count - 1);
            const DisplayWindow &window = line->displayWindows.front();
            if ((line->exitAnimation == "char_fade"
                    || line->exitAnimation == "char_drip"
                    || line->exitAnimation == "spin_flip")
                && line->exitDurationMs > 0) {
                const int exitStart = std::max(line->endMs, window.endMs - 600);
                if (tMs >= exitStart) {
                    const int endMs = window.endMs
                        - delayStep * (count - index - 1);
                    return std::clamp(
                        static_cast<float>(endMs - tMs) / 250.0f,
                        0.0f,
                        1.0f
                    );
                }
            }
            if ((line->entryAnimation == "char_fade"
                    || line->entryAnimation == "char_drip"
                    || line->entryAnimation == "spin_flip")
                && line->entryDurationMs > 0
                && tMs <= window.startMs + 600) {
                const int startMs = window.startMs + delayStep * index;
                return std::clamp(
                    static_cast<float>(tMs - startMs) / 250.0f,
                    0.0f,
                    1.0f
                );
            }
            return 1.0f;
        };
        const int spinDirection = activeCharacterTransition == "spin_flip"
            ? activeCharacterDirection
            : 0;
        const int dripDirection = activeCharacterTransition == "char_drip"
            ? -activeCharacterDirection
            : 0;
        auto spinMatrix = [&](float opacity, float centerX, float centerY) {
            const float clamped = std::clamp(opacity, 0.0f, 1.0f);
            if (spinDirection == 0 || clamped >= 1.0f) {
                return D2D1::Matrix3x2F::Identity();
            }
            constexpr float pi = 3.14159265358979323846f;
            const float angle = std::min(
                (pi * 0.5f) * (1.0f - clamped),
                pi * 89.0f / 180.0f
            );
            const float skew = static_cast<float>(spinDirection) * std::tan(angle);
            // QTransform: translate(center), shear(0, skew), scale(opacity),
            // translate(-center). Direct2D uses the same row-vector matrix
            // layout, so write the resulting coefficients explicitly.
            return D2D1::Matrix3x2F(
                clamped,
                clamped * skew,
                0.0f,
                clamped,
                centerX * (1.0f - clamped),
                centerY * (1.0f - clamped) - clamped * skew * centerX
            );
        };
        auto dripMatrix = [&](float progress, float pivotX) {
            const float clamped = std::clamp(progress, 0.0f, 1.0f);
            if (dripDirection == 0 || clamped >= 1.0f) {
                return D2D1::Matrix3x2F::Identity();
            }
            constexpr float pi = 3.14159265358979323846f;
            const float angle = std::min(
                (pi * 0.5f) * (1.0f - clamped),
                pi * 89.0f / 180.0f
            );
            const float skew = static_cast<float>(dripDirection) * std::tan(angle);
            // N3 pivots CharDrip at (drawWidth, 0) for intro and
            // (drawWidth, -height) for outro.  A vertical shear depends only
            // on pivot X, so both reduce to the glyph's right edge here.
            return D2D1::Matrix3x2F(
                1.0f, skew, 0.0f, 1.0f, 0.0f, -skew * pivotX
            );
        };
        struct CharacterAnimationState {
            float opacity = 1.0f;
            D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Identity();
            bool transformed = false;
            bool utopiaExit = false;
        };
        auto utopiaFollowingDoneAt = [&](std::size_t charIndex) {
            const int count = static_cast<int>(line->chars.size());
            int index = std::clamp(static_cast<int>(charIndex), 0, count - 1);
            for (const Impl::CachedRuby &ruby : line->rubies) {
                if (ruby.lastCharIndex > ruby.firstCharIndex
                    && index >= ruby.firstCharIndex
                    && index <= ruby.lastCharIndex) {
                    index = std::clamp(ruby.lastCharIndex, 0, count - 1);
                    break;
                }
            }
            const int currentEnd = line->chars[static_cast<std::size_t>(index)].endMs;
            for (int next = index + 1; next < count; ++next) {
                const Impl::CachedChar &candidate = line->chars[
                    static_cast<std::size_t>(next)
                ];
                if (candidate.geometry != nullptr || candidate.bitmapGuide.has_value()) {
                    return currentEnd <= candidate.endMs
                        ? candidate.endMs
                        : currentEnd;
                }
            }
            return currentEnd + std::max(line->style.tailMs - 750, 0);
        };
        auto utopiaMatrix = [&](float dxValue, float dyValue, float rotation,
                                float scaleX, float scaleY,
                                float left, float baseline,
                                float centerX, float centerY) {
            // QTransform mutators pre-multiply in row-vector space. Reverse
            // the call order from _character_transform's scale-origin branch.
            return D2D1::Matrix3x2F::Translation(-centerX, -centerY)
                * D2D1::Matrix3x2F::Rotation(rotation)
                * D2D1::Matrix3x2F::Translation(
                    centerX - left, centerY - baseline
                )
                * D2D1::Matrix3x2F::Scale(scaleX, scaleY)
                * D2D1::Matrix3x2F::Translation(
                    left + dxValue, baseline + dyValue
                );
        };
        const auto wipeStartMs = [](const Impl::CachedChar &ch) {
            return ch.wipePoints.empty() ? ch.startMs : ch.wipePoints.front().timeMs;
        };
        const auto wipeEndMs = [](const Impl::CachedChar &ch) {
            return ch.wipePoints.empty() ? ch.endMs : ch.wipePoints.back().timeMs;
        };
        // 整字放大（zoom_pulse）：与 Python transitions.zoom_pulse_wipe_scale
        // 同曲线——唱字期间缓出放大到 1.25，唱字结束后 300ms 缓入缩回。
        // 缓动阶数 level（0~5，0=线性）来自 TextStyle.zoomPulseCurveLevel；
        // 峰值两侧导数为 0（停留感），幂用循环连乘近似避免 pow。
        const auto zoomPulseActive = [](int tMs, int startMs, int endMs) {
            constexpr int kShrinkMs = 300;
            return startMs != endMs
                && startMs < tMs
                && tMs < endMs + kShrinkMs;
        };
        const auto zoomPulsePowi = [](double base, int exponent) {
            double result = 1.0;
            for (int i = 0; i < exponent; ++i) {
                result *= base;
            }
            return result;
        };
        const auto zoomPulseScale = [&](
            int tMs, int startMs, int endMs, int curveLevel
        ) {
            constexpr double kPeak = 1.25;
            constexpr int kShrinkMs = 300;
            if (startMs == endMs || tMs <= startMs || tMs >= endMs + kShrinkMs) {
                return 1.0f;
            }
            const int level = std::clamp(curveLevel, 0, 5);
            if (tMs < endMs) {
                const double one = 1.0
                    - static_cast<double>(tMs - startMs)
                        / static_cast<double>(endMs - startMs);
                const double eased = level <= 0
                    ? 1.0 - one
                    : 1.0 - zoomPulsePowi(one, level);
                return static_cast<float>(1.0 + (kPeak - 1.0) * eased);
            }
            const double q = static_cast<double>(tMs - endMs) / kShrinkMs;
            const double eased = level <= 0 ? 1.0 - q : 1.0 - zoomPulsePowi(q, level);
            return static_cast<float>(1.0 + (kPeak - 1.0) * eased);
        };
        auto characterAnimationAt = [&](std::size_t charIndex) {
            CharacterAnimationState state;
            if (charIndex >= line->chars.size()) {
                state.opacity = 0.0f;
                return state;
            }
            const Impl::CachedChar &ch = line->chars[charIndex];
            if (!useUtopiaTransition) {
                const float progress = charFadeOpacityAt(charIndex);
                state.opacity = dripDirection != 0
                    ? (progress > 0.0f ? 1.0f : 0.0f)
                    : progress;
                if (dripDirection != 0) {
                    state.matrix = dripMatrix(
                        progress, ch.layoutRight
                    );
                    state.transformed = progress > 0.0f && progress < 1.0f;
                } else {
                    state.matrix = spinMatrix(progress, ch.pivotX, ch.pivotY);
                    state.transformed = spinDirection != 0 && progress < 1.0f;
                }
                return state;
            }
            if (line->displayWindows.empty()) {
                return state;
            }
            constexpr float pi = 3.14159265358979323846f;
            const DisplayWindow &window = line->displayWindows.front();
            float dxValue = 0.0f;
            float dyValue = 0.0f;
            float rotation = 0.0f;
            float scaleX = 1.0f;
            float scaleY = 1.0f;
            if (line->entryAnimation == "utopia"
                && tMs <= window.startMs + 700) {
                const int count = std::max(static_cast<int>(line->chars.size()), 1);
                const int delayStep = count <= 1 ? 0 : 200 / (count - 1);
                const int elapsed = tMs - window.startMs
                    - delayStep * static_cast<int>(charIndex);
                if (elapsed < 0) {
                    state.opacity = 0.0f;
                    scaleX = scaleY = 0.0f;
                } else {
                    state.opacity = std::min(
                        static_cast<float>(elapsed) / 400.0f, 1.0f
                    );
                    if (elapsed < 400) {
                        scaleX = scaleY = 1.3f
                            * static_cast<float>(elapsed) / 400.0f;
                    } else if (elapsed < 500) {
                        const float remaining = static_cast<float>(500 - elapsed);
                        scaleX = scaleY = 1.0f + 0.3f * remaining / 100.0f;
                    }
                }
            } else if (line->exitAnimation == "utopia"
                && tMs > utopiaFollowingDoneAt(charIndex)) {
                const float local = std::clamp(
                    static_cast<float>(tMs - utopiaFollowingDoneAt(charIndex))
                        / 750.0f,
                    0.0f,
                    1.0f
                );
                state.opacity = 1.0f - local;
                state.utopiaExit = true;
                const float shrink = 1.0f - local;
                const float amplitude = static_cast<float>(scene.height) / 15.0f;
                const float xTravel = local <= 0.5f
                    ? std::sin(pi * local) * amplitude
                    : amplitude + std::sin((local - 0.5f) * pi) * amplitude;
                const float yTravel = std::sin(pi * local * 0.5f) * amplitude;
                dxValue = -xTravel;
                dyValue = yTravel;
                rotation = -180.0f * local;
                scaleX = shrink * std::cos(pi * local);
                scaleY = shrink;
            } else if (line->karaokeAnimation == "utopia"
                && line->zoomPulseEnabled
                && zoomPulseActive(tMs, wipeStartMs(ch), wipeEndMs(ch))) {
                // 整字放大：窗口延伸到唱字结束后 300ms 的缓入缩回段；
                // intro/exit 相位在链上更早命中，退场照常接管。
                scaleX = scaleY = zoomPulseScale(
                    tMs, wipeStartMs(ch), wipeEndMs(ch),
                    line->style.zoomPulseCurveLevel
                );
            } else if (line->karaokeAnimation == "utopia"
                && tMs > wipeStartMs(ch) && tMs < wipeEndMs(ch)
                && wipeStartMs(ch) != wipeEndMs(ch)) {
                const int overMs = std::min(
                    static_cast<int>((wipeEndMs(ch) - wipeStartMs(ch)) * 0.25f), 100
                );
                if (overMs > 0) {
                    const int peakMs = wipeStartMs(ch) + overMs;
                    const float progress = tMs <= peakMs
                        ? static_cast<float>(tMs - wipeStartMs(ch))
                            / static_cast<float>(overMs)
                        : static_cast<float>(wipeEndMs(ch) - tMs)
                            / static_cast<float>(std::max(wipeEndMs(ch) - peakMs, 1));
                    scaleX = scaleY = 1.0f
                        + 0.15f * std::clamp(progress, 0.0f, 1.0f);
                }
            }
            if (state.opacity <= 0.0f) {
                return state;
            }
            state.matrix = line->zoomPulseEnabled
                ? utopiaMatrix(
                    dxValue, dyValue, rotation, scaleX, scaleY,
                    ch.pivotX, ch.pivotY, ch.pivotX, ch.pivotY
                )
                : utopiaMatrix(
                    dxValue, dyValue, rotation, scaleX, scaleY,
                    ch.layoutLeft, 0.0f, ch.pivotX, ch.pivotY
                );
            state.transformed = dxValue != 0.0f || dyValue != 0.0f
                || rotation != 0.0f || scaleX != 1.0f || scaleY != 1.0f;
            return state;
        };
        auto characterOpacityAt = [&](std::size_t charIndex) {
            return characterAnimationAt(charIndex).opacity;
        };
        auto rubyUnitAnimationAt = [&](const Impl::CachedRuby &ruby,
                                       std::size_t unitIndex) {
            CharacterAnimationState state;
            if (!useUtopiaTransition) {
                const std::size_t transitionIndex = static_cast<std::size_t>(
                    std::max(ruby.transitionCharIndex, 0)
                );
                const float progress = charFadeOpacityAt(transitionIndex);
                state.opacity = dripDirection != 0
                    ? (progress > 0.0f ? 1.0f : 0.0f)
                    : progress;
                if (dripDirection != 0) {
                    const float pivotX = unitIndex < ruby.chars.size()
                        ? ruby.chars[unitIndex].layoutRight
                        : ruby.bounds.right;
                    state.matrix = dripMatrix(progress, pivotX);
                    state.transformed = progress > 0.0f && progress < 1.0f;
                } else {
                    // N3 spins a ruby run as one visual unit. Keep that
                    // established pivot while sharing the same animation
                    // classification as the main glyphs.
                    state.matrix = spinMatrix(progress, ruby.pivotX, ruby.pivotY);
                    state.transformed = spinDirection != 0 && progress < 1.0f;
                }
                return state;
            }
            if (unitIndex >= ruby.chars.size() || line->displayWindows.empty()) {
                state.opacity = characterOpacityAt(static_cast<std::size_t>(
                    std::max(ruby.transitionCharIndex, 0)
                ));
                return state;
            }
            constexpr float pi = 3.14159265358979323846f;
            const Impl::CachedChar &unit = ruby.chars[unitIndex];
            const DisplayWindow &window = line->displayWindows.front();
            float dxValue = 0.0f;
            float dyValue = 0.0f;
            float rotation = 0.0f;
            float scaleX = 1.0f;
            float scaleY = 1.0f;
            if (line->entryAnimation == "utopia"
                && tMs <= window.startMs + 700) {
                const int count = std::max(static_cast<int>(line->chars.size()), 1);
                const int delayStep = count <= 1 ? 0 : 200 / (count - 1);
                const int staggerIndex = std::clamp(
                    ruby.firstCharIndex, 0, count - 1
                );
                const int elapsed = tMs - window.startMs
                    - delayStep * staggerIndex;
                if (elapsed < 0) {
                    state.opacity = 0.0f;
                    scaleX = scaleY = 0.0f;
                } else {
                    state.opacity = std::min(
                        static_cast<float>(elapsed) / 400.0f, 1.0f
                    );
                    if (elapsed < 400) {
                        scaleX = scaleY = 1.3f
                            * static_cast<float>(elapsed) / 400.0f;
                    } else if (elapsed < 500) {
                        scaleX = scaleY = 1.0f
                            + 0.3f * static_cast<float>(500 - elapsed) / 100.0f;
                    }
                }
            } else if (line->exitAnimation == "utopia"
                && tMs > utopiaFollowingDoneAt(static_cast<std::size_t>(std::max(
                    ruby.lastCharIndex, 0
                )))) {
                const int doneMs = utopiaFollowingDoneAt(
                    static_cast<std::size_t>(std::max(ruby.lastCharIndex, 0))
                );
                const float local = std::clamp(
                    static_cast<float>(tMs - doneMs) / 750.0f, 0.0f, 1.0f
                );
                state.opacity = 1.0f - local;
                state.utopiaExit = true;
                const float shrink = 1.0f - local;
                const float amplitude = static_cast<float>(scene.height) / 15.0f;
                const float xTravel = local <= 0.5f
                    ? std::sin(pi * local) * amplitude
                    : amplitude + std::sin((local - 0.5f) * pi) * amplitude;
                dxValue = -xTravel;
                dyValue = std::sin(pi * local * 0.5f) * amplitude;
                rotation = -180.0f * local;
                scaleX = shrink * std::cos(pi * local);
                scaleY = shrink;
            } else if (line->karaokeAnimation == "utopia"
                && line->zoomPulseEnabled
                && zoomPulseActive(tMs, unit.startMs, unit.endMs)) {
                scaleX = scaleY = zoomPulseScale(
                    tMs, unit.startMs, unit.endMs,
                    line->style.zoomPulseCurveLevel
                );
            } else if (line->karaokeAnimation == "utopia"
                && tMs > unit.startMs && tMs < unit.endMs
                && unit.startMs != unit.endMs) {
                const int overMs = std::min(
                    static_cast<int>((unit.endMs - unit.startMs) * 0.25f), 100
                );
                if (overMs > 0) {
                    const int peakMs = unit.startMs + overMs;
                    const float progress = tMs <= peakMs
                        ? static_cast<float>(tMs - unit.startMs)
                            / static_cast<float>(overMs)
                        : static_cast<float>(unit.endMs - tMs)
                            / static_cast<float>(std::max(unit.endMs - peakMs, 1));
                    scaleX = scaleY = 1.0f
                        + 0.15f * std::clamp(progress, 0.0f, 1.0f);
                }
            }
            if (state.opacity <= 0.0f) {
                return state;
            }
            state.matrix = line->zoomPulseEnabled
                ? utopiaMatrix(
                    dxValue, dyValue, rotation, scaleX, scaleY,
                    unit.pivotX, unit.pivotY, unit.pivotX, unit.pivotY
                )
                : utopiaMatrix(
                    dxValue, dyValue, rotation, scaleX, scaleY,
                    unit.layoutLeft, ruby.baselineOffset,
                    unit.pivotX, unit.pivotY
                );
            state.transformed = dxValue != 0.0f || dyValue != 0.0f
                || rotation != 0.0f || scaleX != 1.0f || scaleY != 1.0f;
            return state;
        };
        auto rubyFadeOpacityAt = [&](const Impl::CachedRuby &ruby) {
            return characterOpacityAt(static_cast<std::size_t>(std::max(
                ruby.transitionCharIndex, 0
            )));
        };
        auto rubyUnitOpacityAt = [&](const Impl::CachedRuby &ruby,
                                     std::size_t unitIndex) {
            return rubyUnitAnimationAt(ruby, unitIndex).opacity;
        };
        if (hasCharacterTransition) {
            float maxOpacity = 0.0f;
            for (std::size_t index = 0; index < line->chars.size(); ++index) {
                maxOpacity = std::max(maxOpacity, characterOpacityAt(index));
            }
            if (maxOpacity <= 0.0f) {
                continue;
            }
        }
        const auto geometryStart = Clock::now();
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> frameCharGeometries(
            line->chars.size()
        );
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> frameProtectedGeometries(
            line->chars.size()
        );
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> frameStrokeGeometries(
            line->chars.size()
        );
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> frameStroke2Geometries(
            line->chars.size()
        );
        for (std::size_t index = 0; index < line->chars.size(); ++index) {
            const Impl::CachedChar &ch = line->chars[index];
            const CharacterAnimationState charAnimation = characterAnimationAt(index);
            const float opacity = charAnimation.opacity;
            if (!ch.geometry || opacity <= 0.0f) {
                continue;
            }
            if (!charAnimation.transformed) {
                frameCharGeometries[index] = ch.geometry;
                frameProtectedGeometries[index] = ch.protectedStrokeGeometry;
                frameStrokeGeometries[index] = ch.strokeGeometry;
                frameStroke2Geometries[index] = ch.stroke2Geometry;
                continue;
            }
            const D2D1_MATRIX_3X2_F matrix = charAnimation.matrix;
            Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
            checkHr(
                device_.d2dFactory()->CreateTransformedGeometry(
                    ch.geometry.Get(), &matrix,
                    transformed.ReleaseAndGetAddressOf()
                ),
                "ID2D1Factory::CreateTransformedGeometry(spin character)",
                device_
            );
            count(frameDiagnostics.geometryCreatedDynamic);
            frameCharGeometries[index] = transformed;
            if (ch.protectedStrokeGeometry) {
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry>
                    transformedProtected;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        ch.protectedStrokeGeometry.Get(), &matrix,
                        transformedProtected.ReleaseAndGetAddressOf()
                    ),
                    "ID2D1Factory::CreateTransformedGeometry(spin protected stroke)",
                    device_
                );
                count(frameDiagnostics.geometryCreatedDynamic);
                frameProtectedGeometries[index] = transformedProtected;
            }
            auto transformStroke = [&](ID2D1Geometry *source,
                                       Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                                       const char *operation) {
                if (source == nullptr) {
                    return;
                }
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformedStroke;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        source, &matrix,
                        transformedStroke.ReleaseAndGetAddressOf()
                    ),
                    operation,
                    device_
                );
                count(frameDiagnostics.geometryCreatedDynamic);
                target = transformedStroke;
            };
            if (!impl_->dynamicDirectStrokeEnabled) {
                transformStroke(
                    ch.strokeGeometry.Get(), frameStrokeGeometries[index],
                    "ID2D1Factory::CreateTransformedGeometry(dynamic stroke)"
                );
                transformStroke(
                    ch.stroke2Geometry.Get(), frameStroke2Geometries[index],
                    "ID2D1Factory::CreateTransformedGeometry(dynamic stroke2)"
                );
            }
        }
        std::vector<std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>>>
            frameRubyGeometries(line->rubies.size());
        std::vector<std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>>>
            frameRubyProtectedGeometries(line->rubies.size());
        std::vector<std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>>>
            frameRubyStrokeGeometries(line->rubies.size());
        std::vector<std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>>>
            frameRubyStroke2Geometries(line->rubies.size());
        for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
            float maxRubyOpacity = 0.0f;
            for (std::size_t index = 0; index < ruby.geometries.size(); ++index) {
                maxRubyOpacity = std::max(
                    maxRubyOpacity,
                    rubyUnitAnimationAt(ruby, index).opacity
                );
            }
            if (maxRubyOpacity <= 0.0f) {
                continue;
            }
            frameRubyGeometries[rubyIndex].resize(ruby.geometries.size());
            frameRubyProtectedGeometries[rubyIndex].resize(
                ruby.protectedStrokeGeometries.size()
            );
            frameRubyStrokeGeometries[rubyIndex].resize(ruby.strokeGeometries.size());
            frameRubyStroke2Geometries[rubyIndex].resize(ruby.stroke2Geometries.size());
            for (std::size_t index = 0; index < ruby.geometries.size(); ++index) {
                const CharacterAnimationState rubyAnimation =
                    rubyUnitAnimationAt(ruby, index);
                if (rubyAnimation.opacity <= 0.0f) {
                    continue;
                }
                const D2D1_MATRIX_3X2_F matrix = rubyAnimation.matrix;
                if (!rubyAnimation.transformed) {
                    frameRubyGeometries[rubyIndex][index] = ruby.geometries[index];
                    if (index < ruby.protectedStrokeGeometries.size()) {
                        frameRubyProtectedGeometries[rubyIndex][index]
                            = ruby.protectedStrokeGeometries[index];
                    }
                    if (index < ruby.strokeGeometries.size()) {
                        frameRubyStrokeGeometries[rubyIndex][index]
                            = ruby.strokeGeometries[index];
                    }
                    if (index < ruby.stroke2Geometries.size()) {
                        frameRubyStroke2Geometries[rubyIndex][index]
                            = ruby.stroke2Geometries[index];
                    }
                } else {
                    Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                    checkHr(
                        device_.d2dFactory()->CreateTransformedGeometry(
                            ruby.geometries[index].Get(), &matrix,
                            transformed.ReleaseAndGetAddressOf()
                        ),
                        "ID2D1Factory::CreateTransformedGeometry(spin ruby)",
                        device_
                    );
                    count(frameDiagnostics.geometryCreatedDynamic);
                    frameRubyGeometries[rubyIndex][index] = transformed;
                    if (index < ruby.protectedStrokeGeometries.size()
                        && ruby.protectedStrokeGeometries[index]) {
                        Microsoft::WRL::ComPtr<ID2D1TransformedGeometry>
                            transformedProtected;
                        checkHr(
                            device_.d2dFactory()->CreateTransformedGeometry(
                                ruby.protectedStrokeGeometries[index].Get(), &matrix,
                                transformedProtected.ReleaseAndGetAddressOf()
                            ),
                            "ID2D1Factory::CreateTransformedGeometry(spin ruby protected stroke)",
                            device_
                        );
                        count(frameDiagnostics.geometryCreatedDynamic);
                        frameRubyProtectedGeometries[rubyIndex][index]
                            = transformedProtected;
                    }
                    auto transformRubyStroke = [&] (
                        ID2D1Geometry *source,
                        Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                        const char *operation
                    ) {
                        if (source == nullptr) {
                            return;
                        }
                        Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformedStroke;
                        checkHr(
                            device_.d2dFactory()->CreateTransformedGeometry(
                                source, &matrix,
                                transformedStroke.ReleaseAndGetAddressOf()
                            ),
                            operation,
                            device_
                        );
                        count(frameDiagnostics.geometryCreatedDynamic);
                        target = transformedStroke;
                    };
                    if (!impl_->dynamicDirectStrokeEnabled
                        && index < ruby.strokeGeometries.size()) {
                        transformRubyStroke(
                            ruby.strokeGeometries[index].Get(),
                            frameRubyStrokeGeometries[rubyIndex][index],
                            "ID2D1Factory::CreateTransformedGeometry(spin ruby stroke)"
                        );
                    }
                    if (!impl_->dynamicDirectStrokeEnabled
                        && index < ruby.stroke2Geometries.size()) {
                        transformRubyStroke(
                            ruby.stroke2Geometries[index].Get(),
                            frameRubyStroke2Geometries[rubyIndex][index],
                            "ID2D1Factory::CreateTransformedGeometry(spin ruby stroke2)"
                        );
                    }
                }
            }
        }
        frameDiagnostics.geometryMs += elapsedMs(geometryStart);
        auto charGeometryAt = [&](std::size_t index) -> ID2D1Geometry * {
            return index < frameCharGeometries.size()
                ? frameCharGeometries[index].Get()
                : nullptr;
        };
        auto protectedGeometryAt = [&](std::size_t index) -> ID2D1Geometry * {
            return index < frameProtectedGeometries.size()
                ? frameProtectedGeometries[index].Get()
                : nullptr;
        };
        auto strokeGeometryAt = [&](std::size_t index) -> ID2D1Geometry * {
            return index < frameStrokeGeometries.size()
                ? frameStrokeGeometries[index].Get()
                : nullptr;
        };
        auto stroke2GeometryAt = [&](std::size_t index) -> ID2D1Geometry * {
            return index < frameStroke2Geometries.size()
                ? frameStroke2Geometries[index].Get()
                : nullptr;
        };
        auto rubyGeometryAt = [&](
            std::size_t rubyIndex, std::size_t geometryIndex
        ) -> ID2D1Geometry * {
            return rubyIndex < frameRubyGeometries.size()
                && geometryIndex < frameRubyGeometries[rubyIndex].size()
                ? frameRubyGeometries[rubyIndex][geometryIndex].Get()
                : nullptr;
        };
        auto rubyProtectedGeometryAt = [&](
            std::size_t rubyIndex, std::size_t geometryIndex
        ) -> ID2D1Geometry * {
            return rubyIndex < frameRubyProtectedGeometries.size()
                && geometryIndex < frameRubyProtectedGeometries[rubyIndex].size()
                ? frameRubyProtectedGeometries[rubyIndex][geometryIndex].Get()
                : nullptr;
        };
        auto rubyStrokeGeometryAt = [&] (
            std::size_t rubyIndex, std::size_t geometryIndex
        ) -> ID2D1Geometry * {
            return rubyIndex < frameRubyStrokeGeometries.size()
                && geometryIndex < frameRubyStrokeGeometries[rubyIndex].size()
                ? frameRubyStrokeGeometries[rubyIndex][geometryIndex].Get()
                : nullptr;
        };
        auto rubyStroke2GeometryAt = [&] (
            std::size_t rubyIndex, std::size_t geometryIndex
        ) -> ID2D1Geometry * {
            return rubyIndex < frameRubyStroke2Geometries.size()
                && geometryIndex < frameRubyStroke2Geometries[rubyIndex].size()
                ? frameRubyStroke2Geometries[rubyIndex][geometryIndex].Get()
                : nullptr;
        };
        // A character/ruby unit counts as "transformed" only on frames where
        // its animation matrix is non-identity.
        auto charTransformedAt = [&](std::size_t index) {
            return characterAnimationAt(index).transformed;
        };
        // N3's Utopia override substitutes the transformed geometry while
        // DrawOneLineDecorBlur is building the shared work bitmap, then blurs
        // that combined line once. Keep the dedicated blur-then-transform
        // layers for the other character animations, but never split Utopia
        // between two glow representations.
        auto charUsesGroupedGlowAt = [&](std::size_t index) {
            return useUtopiaTransition || !charTransformedAt(index);
        };
        auto rubyUnitTransformed = [&](const Impl::CachedRuby &ruby,
                                       std::size_t unitIndex) {
            return rubyUnitAnimationAt(ruby, unitIndex).transformed;
        };
        auto rubyUnitUsesGroupedGlowAt = [&](const Impl::CachedRuby &ruby,
                                             std::size_t unitIndex) {
            return useUtopiaTransition
                || !rubyUnitTransformed(ruby, unitIndex);
        };
        const auto expandedRect = [](const D2D1_RECT_F &rect, float amount) {
            return D2D1::RectF(
                rect.left - amount, rect.top - amount,
                rect.right + amount, rect.bottom + amount
            );
        };
        const auto unionRect = [](const D2D1_RECT_F &a, const D2D1_RECT_F &b) {
            return D2D1::RectF(
                std::min(a.left, b.left), std::min(a.top, b.top),
                std::max(a.right, b.right), std::max(a.bottom, b.bottom)
            );
        };
        int displayEndMs = line->endMs + std::max(style.tailMs, 0);
        for (const DisplayWindow &window : line->displayWindows) {
            if (tMs >= window.startMs && tMs < window.endMs) {
                displayEndMs = window.endMs;
                break;
            }
        }
        const VolumeSignalState signalState = volumeSignalState(
            line->startMs, style, tMs, displayEndMs, line->signalHead
        );
        const VolumeSignalGeometry signalGeometry = volumeSignalGeometry(style);
        const ShapeSignalState shapeState = shapeSignalState(
            line->startMs, style, tMs, displayEndMs, line->signalHead
        );
        const ShapeSignalGeometry shapeGeometry = shapeSignalGeometry(style);
        const bool independentVolume = style.volumeEnabled;
        const bool legacyVolume = style.litEnabled && style.litStyle == "volume";
        // auto 外观模式：柱体走主文字装饰管线（渐变/描边/二重描边/发光/
        // 阴影/整字放大），与 Painter 的 _draw_volume_decorated_group 同口径。
        const bool volumeAutoDecorated = independentVolume
            && style.volumeAppearanceMode == "auto";
        const float volumeDecorScale = style.fontSize > 0.0f
            ? signalGeometry.size / style.fontSize
            : 1.0f;
        const float volumeDecorStrokeWidth = std::max(style.volumeStrokeWidth, 0.0f);
        const float volumeDecorStroke2Width = std::min(
            std::max(style.stroke2Width * volumeDecorScale, 0.0f),
            std::floor(signalGeometry.columnWidth * 0.5f)
        );
        // 柱体逐字入退场动画：镜像 Painter 的
        // volume_bar_transition_states —— 同一行、同一显示窗口，柱
        // index 走字符交错公式（count = 柱数）。utopia 退场文字按
        // 「后一字唱完」逐字离场，柱体不演唱，done 时刻均匀铺在演唱
        // 窗口 [startMs, endMs] 上保持同节奏。柱以中心为轴（文字的
        // utopia/drip 用字框角轴），两端都用中心轴即保持一致。
        struct BarAnimationState {
            float opacity = 1.0f;
            D2D1::Matrix3x2F matrix = D2D1::Matrix3x2F::Identity();
        };
        const int barCount = signalGeometry.count;
        const bool hasBarCharFadeExit
            = (independentVolume || legacyVolume)
            && (line->exitAnimation == "char_fade"
                || line->exitAnimation == "char_drip"
                || line->exitAnimation == "spin_flip")
            && line->exitDurationMs > 0;
        const bool hasBarCharFadeEntry
            = (independentVolume || legacyVolume)
            && (line->entryAnimation == "char_fade"
                || line->entryAnimation == "char_drip"
                || line->entryAnimation == "spin_flip")
            && line->entryDurationMs > 0;
        const bool hasBarUtopia
            = (independentVolume || legacyVolume)
            && (line->entryAnimation == "utopia"
                || line->exitAnimation == "utopia");
        const int barWindowStartMs = line->displayWindows.empty()
            ? line->startMs
            : line->displayWindows.front().startMs;
        auto barCharFadeProgress = [&](int index) {
            const int count = std::max(barCount, 1);
            const int delayStep = count <= 1 ? 0 : 350 / (count - 1);
            if (line->displayWindows.empty()) {
                return 1.0f;
            }
            const DisplayWindow &window = line->displayWindows.front();
            if (hasBarCharFadeExit) {
                const int exitStart = std::max(
                    line->endMs, window.endMs - 600
                );
                if (tMs >= exitStart) {
                    const int endMs = window.endMs
                        - delayStep * (count - index - 1);
                    return std::clamp(
                        static_cast<float>(endMs - tMs) / 250.0f,
                        0.0f,
                        1.0f
                    );
                }
            }
            if (hasBarCharFadeEntry && tMs <= window.startMs + 600) {
                const int startMs = window.startMs + delayStep * index;
                return std::clamp(
                    static_cast<float>(tMs - startMs) / 250.0f,
                    0.0f,
                    1.0f
                );
            }
            return 1.0f;
        };
        auto barCenteredMatrix = [&](
            float dx, float dy, float rotation,
            float scaleX, float scaleY, float skewY,
            float cx, float cy
        ) {
            // Painter character_transform 无 scale-origin 分支的逐项
            // 镜像。QTransform 的 translate/rotate/... 是前乘（坐标
            // 系语义），因此最终矩阵是
            // T(−c)·Scale·Shear·Rotate·T(c+dx)（行向量布局，D2D 的
            // operator* 组合顺序与矩阵积一致）。
            D2D1::Matrix3x2F matrix = D2D1::Matrix3x2F::Translation(
                -cx, -cy
            );
            if (scaleX != 1.0f || scaleY != 1.0f) {
                matrix = matrix * D2D1::Matrix3x2F::Scale(scaleX, scaleY);
            }
            if (skewY != 0.0f) {
                matrix = matrix * D2D1::Matrix3x2F(
                    1.0f, skewY, 0.0f, 1.0f, 0.0f, 0.0f
                );
            }
            if (rotation != 0.0f) {
                matrix = matrix * D2D1::Matrix3x2F::Rotation(rotation);
            }
            matrix = matrix * D2D1::Matrix3x2F::Translation(
                cx + dx, cy + dy
            );
            return matrix;
        };
        auto barAnimationAt = [&](int index, float cx, float cy) {
            BarAnimationState state;
            if (!(independentVolume || legacyVolume)) {
                return state;
            }
            const int count = std::max(barCount, 1);
            if (activeCharacterTransition == "char_fade"
                || activeCharacterTransition == "char_drip"
                || activeCharacterTransition == "spin_flip") {
                const float progress = barCharFadeProgress(index);
                if (progress <= 0.0f) {
                    state.opacity = 0.0f;
                    return state;
                }
                constexpr float pi = 3.14159265358979323846f;
                const float clamped = std::clamp(progress, 0.0f, 1.0f);
                const float angle = std::min(
                    (pi * 0.5f) * (1.0f - clamped),
                    pi * 89.0f / 180.0f
                );
                const float skew = std::tan(angle);
                if (activeCharacterTransition == "spin_flip") {
                    state.opacity = progress;
                    const float direction = static_cast<float>(
                        activeCharacterDirection
                    );
                    state.matrix = barCenteredMatrix(
                        0.0f, 0.0f, 0.0f,
                        clamped, clamped, direction * skew,
                        cx, cy
                    );
                } else if (activeCharacterTransition == "char_drip") {
                    state.opacity = 1.0f;
                    const float direction = -static_cast<float>(
                        activeCharacterDirection
                    );
                    state.matrix = barCenteredMatrix(
                        0.0f, 0.0f, 0.0f,
                        1.0f, 1.0f, direction * skew,
                        cx, cy
                    );
                } else {
                    state.opacity = progress;
                }
                return state;
            }
            if (hasBarUtopia) {
                constexpr float pi = 3.14159265358979323846f;
                if (line->entryAnimation == "utopia"
                    && tMs <= barWindowStartMs + 700) {
                    const int delayStep = count <= 1
                        ? 0
                        : 200 / (count - 1);
                    const int elapsed = tMs - barWindowStartMs
                        - delayStep * index;
                    if (elapsed < 0) {
                        state.opacity = 0.0f;
                        return state;
                    }
                    state.opacity = std::min(
                        static_cast<float>(elapsed) / 400.0f, 1.0f
                    );
                    float scale = 1.0f;
                    if (elapsed < 400) {
                        scale = 1.3f * static_cast<float>(elapsed) / 400.0f;
                    } else if (elapsed < 500) {
                        const float remaining = static_cast<float>(500 - elapsed);
                        scale = 1.0f + 0.3f * remaining / 100.0f;
                    }
                    state.matrix = barCenteredMatrix(
                        0.0f, 0.0f, 0.0f, scale, scale, 0.0f, cx, cy
                    );
                    return state;
                }
                if (line->exitAnimation == "utopia") {
                    const int span = std::max(line->endMs - line->startMs, 0);
                    const int doneMs = line->startMs + static_cast<int>(
                        static_cast<float>(span * index)
                            / static_cast<float>(std::max(count - 1, 1))
                    );
                    if (tMs > doneMs) {
                        const float local = std::clamp(
                            static_cast<float>(tMs - doneMs) / 750.0f,
                            0.0f,
                            1.0f
                        );
                        state.opacity = 1.0f - local;
                        if (state.opacity <= 0.0f) {
                            return state;
                        }
                        const float shrink = 1.0f - local;
                        const float amplitude = static_cast<float>(scene.height) / 15.0f;
                        const float xTravel = local <= 0.5f
                            ? std::sin(pi * local) * amplitude
                            : amplitude + std::sin((local - 0.5f) * pi) * amplitude;
                        const float yTravel = std::sin(pi * local * 0.5f) * amplitude;
                        state.matrix = barCenteredMatrix(
                            -xTravel, yTravel, -180.0f * local,
                            shrink * std::cos(pi * local), shrink, 0.0f,
                            cx, cy
                        );
                        return state;
                    }
                }
            }
            return state;
        };
        // auto 档柱体发光层：源位图在最终批次前烘好（含每柱动画透明度），
        // 模糊后于柱体绘制点套柱动画矩阵合成（blur-then-transform，与
        // 文字变换柱的发光同语义，镜像 Painter 每柱 paint_glow_path）。
        struct VolumeBarGlowLayer {
            ID2D1Bitmap1 *source = nullptr;
            ID2D1Effect *blur = nullptr;
            std::vector<int> sigmas;
            D2D1_RECT_F layerRect{};
            int barIndex = 0;
        };
        std::vector<VolumeBarGlowLayer> volumeBarGlowLayers;
        const int signalActiveDuration = std::max(
            (independentVolume ? style.volumeDurationMs : style.signalsDurationMs)
                - std::max(
                    independentVolume ? style.volumeWaitingTimeMs : style.litWaitingTimeMs,
                    0
                ),
            0
        );
        const int signalEndMs = line->startMs + (
            independentVolume ? style.volumeTimeOffsetMs : style.litTimeOffsetMs
        );
        // Every lit style (volume bars and shape lamps) attaches only to each
        // section's first page's first line (signalHead).
        const bool signalLayoutActive = (independentVolume || legacyVolume)
            && !style.vertical
            && signalActiveDuration > 0
            && line->signalHead
            && tMs >= signalEndMs - signalActiveDuration
            && tMs < displayEndMs;
        float lyricLeft = line->bounds.left;
        float lyricRight = line->bounds.right;
        const bool n3Layout = style.layoutSemantics == "n3_1074";
        if (n3Layout && !style.vertical) {
            lyricLeft = line->fillBounds.left;
            lyricRight = line->fillBounds.right;
            // N3 anchors the complete line box, including a reading that
            // overhangs its base-text target.  Painter folds the same ruby
            // layout boxes into _line_total_width even for centered rows.
            for (const Impl::CachedRuby &ruby : line->rubies) {
                for (const Impl::CachedChar &unit : ruby.chars) {
                    lyricLeft = std::min(lyricLeft, unit.layoutLeft);
                    lyricRight = std::max(lyricRight, unit.layoutRight);
                }
            }
        }
        // Guide-symbol lines must keep anchoring this complete layout box:
        // fillBounds spans [0, cursor] over every layout cell (vector guide
        // glyphs included) and the branches above add ruby overhang.  A
        // source-text-only anchor box would push right-aligned lines one
        // guide advance past the right margin.
        // The Painter no longer pads the horizontal line box with the stroke
        // extent under legacy semantics either -- both now anchor N3's logical
        // DrawLineLeft/Right, so mixed-role lines keep the glyph box as well.
        float unionLeft = lyricLeft;
        float unionRight = lyricRight;
        if (signalLayoutActive) {
            // Painter aligns the offset-free union of the text and signal
            // module throughout the guide window, including flash-off frames.
            // The volume offset moves only the bars afterwards.
            unionLeft = std::min(unionLeft, -signalGeometry.groupWidth);
            unionRight = std::max(unionRight, 0.0f);
        }
        // Shape lamps ride above the text start, so they reserve no horizontal
        // room: the Painter keeps their span outside the anchored union and
        // lets them overhang freely at their offset.
        auto alignedDx = [&](float left, float right) {
            const float inkWidth = right - left;
            float value = (static_cast<float>(scene.width) - inkWidth) * 0.5f
                - left + style.centerOffsetX;
            if (style.alignment == "left") {
                value = style.horizontalMargin - left;
            } else if (style.alignment == "right") {
                value = static_cast<float>(scene.width) - style.horizontalMargin - right;
            }
            if (!style.vertical) {
                value += style.layoutOffsetX;
            }
            return value + animation.dx;
        };
        // 正文与音量柱共用同一 union 盒对齐（Sayatoo 对齐完整 LineDrawingData）。
        // 行内混排行（角色标签）此前按歌词盒单独锚定，柱体仍按 union 放置，
        // 居中对齐下柱体会右侵 groupWidth/2 压住正文——与 Painter 侧同修。
        float dx = alignedDx(unionLeft, unionRight) + placementOffsetX;
        float signalDx = alignedDx(unionLeft, unionRight) + placementOffsetX;
        // 形状灯悬浮在文字实际起点上：跟随文字变换（音量柱 union 生效时
        // 文字已被右移），而不是按无音量柱的歌词盒单独对齐——这与 Painter
        // 双模块时 text_x + lit_offset_x 的锚定语义一致。
        float shapeDx = dx;
        // N3 applies SmartHorizon after ordinary lane alignment.  Page ids
        // come from the same assign_lanes result used by the Painter oracle,
        // so invisible siblings still contribute to page-wide width maxima.
        // SmartHorizon is not part of the N3-only layout semantics: the layout
        // tab offers it for every project, so legacy styles take it as well.
        if (!style.vertical
            && style.dualLineLayout
            && style.smartHorizontal != "none"
            && style.alignment != "center"
            && line->pageIndex >= 0) {
            const auto layoutWidth = [](const Impl::CachedLine &candidate) {
                float left = candidate.fillBounds.left;
                float right = candidate.fillBounds.right;
                for (const Impl::CachedRuby &ruby : candidate.rubies) {
                    for (const Impl::CachedChar &unit : ruby.chars) {
                        left = std::min(left, unit.layoutLeft);
                        right = std::max(right, unit.layoutRight);
                    }
                }
                return std::max(right - left, 1.0f);
            };
            const auto firstCharFontSize = [&](const Impl::CachedLine &candidate) {
                if (!candidate.chars.empty()) {
                    const int styleIndex = candidate.chars.front().styleIndex;
                    if (styleIndex >= 0
                        && styleIndex < static_cast<int>(scene.charStyles.size())) {
                        return std::max(
                            scene.charStyles[static_cast<std::size_t>(styleIndex)].fontSize,
                            1.0f
                        );
                    }
                }
                return std::max(candidate.style.fontSize, 1.0f);
            };
            // The Painter feeds the signal-union width into its smart pass for
            // the lamp line itself, so mirror that here: smart thresholds and
            // page maxima stay backend-consistent while the bars widen a line.
            const float ownWidth = signalLayoutActive
                ? std::max(unionRight - unionLeft, 1.0f)
                : layoutWidth(*line);
            const float ownFontSize = firstCharFontSize(*line);
            float smartDx = 0.0f;
            if (style.smartHorizontal == "center_position") {
                const float threshold = std::floor(
                    static_cast<float>(scene.width) * 0.5f
                    + ownFontSize * 0.5f
                    - ownWidth
                );
                if (threshold > style.horizontalMargin) {
                    if (style.alignment == "right") {
                        const float currentLeft = static_cast<float>(scene.width)
                            - style.horizontalMargin - ownWidth;
                        smartDx = std::floor(
                            static_cast<float>(scene.width) * 0.5f
                            - ownFontSize * 0.5f
                        ) - currentLeft;
                    } else {
                        smartDx = threshold - style.horizontalMargin;
                    }
                }
            } else if (style.smartHorizontal == "equal_margins") {
                float maxLeft = 0.0f;
                float maxCenter = 0.0f;
                float maxRight = 0.0f;
                float pageHeadFontSize = style.fontSize;
                bool foundPageHead = false;
                for (const Impl::CachedLine &candidate : impl_->lines) {
                    if (candidate.sourceIndex != line->sourceIndex
                        || candidate.pageIndex != line->pageIndex) {
                        continue;
                    }
                    if (!foundPageHead) {
                        pageHeadFontSize = firstCharFontSize(candidate);
                        foundPageHead = true;
                    }
                    const float width = layoutWidth(candidate);
                    if (candidate.style.alignment == "right") {
                        maxRight = std::max(maxRight, width);
                    } else if (candidate.style.alignment == "center") {
                        maxCenter = std::max(maxCenter, width);
                    } else {
                        maxLeft = std::max(maxLeft, width);
                    }
                }
                if (maxLeft > 0.0f && maxRight > 0.0f) {
                    const float slack = static_cast<float>(scene.width)
                        - style.horizontalMargin * 2.0f
                        - maxLeft - maxCenter - maxRight
                        + pageHeadFontSize;
                    if (slack > 0.0f) {
                        const float halfSlack = std::floor(slack * 0.5f);
                        smartDx = style.alignment == "right"
                            ? -halfSlack
                            : halfSlack;
                    }
                }
            }
            dx += smartDx;
            signalDx += smartDx;
            shapeDx += smartDx;
        }
        // The title is a standalone block with no lane grid to hold steady, so
        // its box comes from the glyphs it actually draws.  Sizing it from the
        // line style would let the base title scheme's font size move a title
        // that is entirely rendered with some other role scheme.  Mirrors
        // Painter's _layout_title_overlay.
        const bool ownCharBox = line->staticOverlay && line->hasN3CharBox;
        const float visualPad = n3Layout
            ? 0.0f
            : (line->hasInlineStyles
            ? line->maxVisualPad
            : std::ceil(
                (std::max(style.strokeWidth, 0.0f)
                    + std::max(style.stroke2Width, 0.0f)) * 0.5f
            ));
        // Lyric lanes are a page-level grid: Painter derives them from the style
        // alone (_fixed_line_geometry), never from what a line happens to
        // contain.  legacyLaneHeight/Descent carry exactly that style-level box,
        // so use it for every legacy lyric line, not only the ones carrying an
        // inline role scheme.  Deriving the grid from the line's own glyph
        // metrics let a single half-width space -- Latin text, therefore
        // measured with the Latin face -- move the whole upper row by the two
        // faces' ascent gap.  The title keeps its glyph-derived box per above.
        const float mainHeight = n3Layout
            ? (ownCharBox
                ? line->n3CharAscent + line->n3CharDescent
                : line->n3DrawHeight)
            : line->staticOverlay
                ? (line->ascent > 0.0f ? line->ascent : -line->bounds.top)
                    + (line->descent > 0.0f ? line->descent : line->bounds.bottom)
                    + visualPad * 2.0f
                : line->legacyLaneHeight;
        const float descent = n3Layout
            ? (ownCharBox ? line->n3CharDescent : line->n3Descent)
            : line->staticOverlay
                ? (line->descent > 0.0f ? line->descent : line->bounds.bottom)
                    + visualPad
                : line->legacyLaneDescent;
        const float ascent = mainHeight - descent;
        const int lanes = style.dualLineLayout ? std::max(style.laneCount, 1) : 1;
        // Ruby allowance is a style-level lane quantity (mirrors Painter's
        // ruby_vertical_extra on the style): every legacy line reserves it,
        // ruby-bearing or not, so baselines don't jump between pages.  N3
        // ignores ruby in the line grid; the title overlay has no ruby and
        // keeps its glyph-derived box.
        const float rubyExtra = n3Layout || line->staticOverlay
            ? 0.0f
            : std::max(
                style.rubyGap + style.rubyFontSize
                    + std::max(style.rubyStrokeWidth, 0.0f),
                0.0f
            );
        const float step = mainHeight + style.lineGap;
        float firstBaseline = static_cast<float>(scene.height) - style.bottomMargin
            - descent - step * static_cast<float>(lanes - 1);
        if (style.verticalPosition == "top") {
            firstBaseline = style.bottomMargin + rubyExtra + ascent;
        } else if (style.verticalPosition == "center") {
            const float totalHeight = mainHeight * static_cast<float>(lanes)
                + style.lineGap * static_cast<float>(lanes - 1);
            firstBaseline = (static_cast<float>(scene.height) - totalHeight) * 0.5f
                + ascent;
            // Painter's shared baseline never consults inline role/guide
            // geometry for lyric lines, so the style-level box plus ruby
            // reserve applies with or without inline styles.  The title keeps
            // centering on the glyphs it actually draws.
            if (lanes == 1 && !n3Layout && line->staticOverlay) {
                firstBaseline = (static_cast<float>(scene.height)
                    - (line->bounds.bottom - line->bounds.top)) * 0.5f
                    - line->bounds.top;
            } else if (lanes == 1 && !n3Layout) {
                const float blockHeight = mainHeight + rubyExtra;
                firstBaseline = (static_cast<float>(scene.height) - blockHeight) * 0.5f
                    + rubyExtra + ascent;
            }
        }
        if (style.verticalPosition == "center") {
            firstBaseline += style.centerOffsetY;
        }
        float dy = firstBaseline + step * static_cast<float>(line->lane)
            + animation.dy;
        if (style.vertical) {
            const float cellWidth = std::max(
                line->fillBounds.right - line->fillBounds.left, 1.0f
            );
            const float blockHeight = std::max(
                line->fillBounds.bottom - line->fillBounds.top, 1.0f
            );
            const float verticalRubyAllowance = line->verticalRubyAllowance;
            dx = static_cast<float>(scene.width) - style.bottomMargin
                - verticalRubyAllowance - cellWidth * 0.5f
                - static_cast<float>(line->lane)
                    * (cellWidth + verticalRubyAllowance + style.lineGap)
                + animation.dx;
            if (style.verticalPosition == "top") {
                dy = style.bottomMargin;
            } else if (style.verticalPosition == "center") {
                dy = std::max(
                    (static_cast<float>(scene.height) - blockHeight) * 0.5f,
                    0.0f
                );
            } else {
                dy = static_cast<float>(scene.height) - style.bottomMargin
                    - blockHeight;
            }
            dy += animation.dy;
        }
        if (!style.vertical) {
            dy += style.layoutOffsetY + placementOffsetY;
        }
        auto visualVerticalPadding = [](const TextStyle &item, bool ruby) {
            const float stroke = ruby
                ? std::max(item.rubyStrokeWidth, 0.0f)
                    + std::max(item.rubyStroke2Width, 0.0f)
                : std::max(item.strokeWidth, 0.0f)
                    + std::max(item.stroke2Width, 0.0f);
            const std::string &decoration = ruby
                ? item.rubyDecorationKind
                : item.decorationKind;
            const float glow = ruby
                ? std::max(item.rubyGlowBeforeRadius, item.rubyGlowAfterRadius)
                : std::max(item.glowBeforeRadius, item.glowAfterRadius);
            const float shadowY = ruby ? item.rubyShadowOffsetY : item.shadowOffsetY;
            float top = stroke * 0.5f + 3.0f;
            float bottom = top;
            if (decoration == "glow") {
                top += std::max(glow, 0.0f) * 3.0f;
                bottom += std::max(glow, 0.0f) * 3.0f;
            } else if (decoration == "shadow") {
                top += std::max(-shadowY, 0.0f);
                bottom += std::max(shadowY, 0.0f);
            }
            return std::pair<float, float>{top, bottom};
        };
        auto visualTransformPadding = [](const TextStyle &item, bool ruby) {
            const float stroke = ruby
                ? std::max(item.rubyStrokeWidth, 0.0f)
                    + std::max(item.rubyStroke2Width, 0.0f)
                : std::max(item.strokeWidth, 0.0f)
                    + std::max(item.stroke2Width, 0.0f);
            const std::string &decoration = ruby
                ? item.rubyDecorationKind
                : item.decorationKind;
            float padding = stroke * 0.5f + 3.0f;
            if (decoration == "glow") {
                const float glow = ruby
                    ? std::max(item.rubyGlowBeforeRadius, item.rubyGlowAfterRadius)
                    : std::max(item.glowBeforeRadius, item.glowAfterRadius);
                padding += std::max(glow, 0.0f) * 3.0f;
            } else if (decoration == "shadow") {
                const float shadowX = ruby
                    ? item.rubyShadowOffsetX
                    : item.shadowOffsetX;
                const float shadowY = ruby
                    ? item.rubyShadowOffsetY
                    : item.shadowOffsetY;
                padding += std::max(std::abs(shadowX), std::abs(shadowY));
            }
            return padding;
        };
        float contentTop = line->bounds.top;
        float contentBottom = line->bounds.bottom;
        auto [topPad, bottomPad] = visualVerticalPadding(style, false);
        for (const Impl::CachedChar &ch : line->chars) {
            if (ch.styleIndex < 0
                || ch.styleIndex >= static_cast<int>(scene.charStyles.size())) {
                continue;
            }
            const auto padding = visualVerticalPadding(
                scene.charStyles[static_cast<std::size_t>(ch.styleIndex)], false
            );
            topPad = std::max(topPad, padding.first);
            bottomPad = std::max(bottomPad, padding.second);
        }
        // Compact readback bands must follow the complete transformed visual,
        // not the stable line box. N3 avoids this class of clipping by always
        // reading its full-frame target. Preserve our band optimization by
        // transforming a conservatively padded glyph rectangle instead. The
        // horizontal padding is important for CharDrip/SpinFlip: their shear
        // maps glow and shadow pixels from X into a much larger Y extent.
        auto extendAnimatedVisualBounds = [&] (
            ID2D1Geometry *baseGeometry,
            const CharacterAnimationState &animationState,
            float padding
        ) {
            if (baseGeometry == nullptr || !animationState.transformed) {
                return;
            }
            D2D1_RECT_F bounds{};
            checkHr(
                baseGeometry->GetBounds(nullptr, &bounds),
                "ID2D1Geometry::GetBounds(animated readback band)",
                device_
            );
            bounds.left -= padding;
            bounds.top -= padding;
            bounds.right += padding;
            bounds.bottom += padding;
            const D2D1_MATRIX_3X2_F &matrix = animationState.matrix;
            const auto transformedY = [&](float x, float y) {
                return x * matrix._12 + y * matrix._22 + matrix._32;
            };
            const float visualTop = std::min({
                transformedY(bounds.left, bounds.top),
                transformedY(bounds.right, bounds.top),
                transformedY(bounds.left, bounds.bottom),
                transformedY(bounds.right, bounds.bottom),
            });
            const float visualBottom = std::max({
                transformedY(bounds.left, bounds.top),
                transformedY(bounds.right, bounds.top),
                transformedY(bounds.left, bounds.bottom),
                transformedY(bounds.right, bounds.bottom),
            });
            contentTop = std::min(contentTop, visualTop);
            contentBottom = std::max(contentBottom, visualBottom);
        };
        for (std::size_t index = 0; index < line->chars.size(); ++index) {
            const Impl::CachedChar &ch = line->chars[index];
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            extendAnimatedVisualBounds(
                ch.geometry.Get(), characterAnimationAt(index),
                visualTransformPadding(charStyle, false)
            );
        }
        for (const Impl::CachedRuby &ruby : line->rubies) {
            const TextStyle &rubyStyle = ruby.styleIndex >= 0
                && ruby.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ruby.styleIndex)]
                : style;
            const float padding = visualTransformPadding(rubyStyle, true);
            for (std::size_t index = 0; index < ruby.chars.size(); ++index) {
                extendAnimatedVisualBounds(
                    ruby.chars[index].geometry.Get(),
                    rubyUnitAnimationAt(ruby, index), padding
                );
            }
        }
        for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
            contentTop = std::min(contentTop, ruby.bounds.top);
            contentBottom = std::max(contentBottom, ruby.bounds.bottom);
            const TextStyle &rubyStyle = ruby.styleIndex >= 0
                && ruby.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ruby.styleIndex)]
                : style;
            const auto padding = visualVerticalPadding(rubyStyle, true);
            topPad = std::max(topPad, padding.first);
            bottomPad = std::max(bottomPad, padding.second);
        }
        // Signal Y anchors follow the style font metrics without the lane's
        // visual pad (and never the N3 box), mirroring the Painter's
        // signal_lit_y which feeds on QFontMetrics of the line style's font.
        const float signalTextMetric =
            (line->laneFontAscent - line->laneFontDescent) * 0.5f;
        const float signalGroupY = style.volumeOffsetY
            - signalGeometry.strokeExtent
            - signalGeometry.size * 0.5f
            - signalTextMetric;
        const float volumeGroupX
            = style.volumeOffsetX - signalGeometry.groupWidth;
        // 柱体几何（供绘制、发光源与合成三处共用）。
        auto volumeBarRectAt = [&](int index) {
            const float left = volumeGroupX + signalGeometry.strokeExtent
                + static_cast<float>(index) * signalGeometry.pitch;
            const float top = signalGroupY + signalGeometry.strokeExtent
                + signalGeometry.alignBaseShift
                + static_cast<float>(index) * signalGeometry.alignDeltaShift;
            const float height = std::max(
                signalGeometry.frontHeight
                    + static_cast<float>(index) * signalGeometry.heightDelta,
                1.0f
            );
            return D2D1::RectF(
                left, top, left + signalGeometry.columnWidth, top + height
            );
        };
        auto volumeBarRoundedRectAt = [&](int index) {
            const D2D1_RECT_F rect = volumeBarRectAt(index);
            const float radius = std::max(
                std::min(
                    rect.right - rect.left, rect.bottom - rect.top
                ) * 0.22f,
                1.0f
            );
            return D2D1::RoundedRect(rect, radius, radius);
        };


        if (signalState.visible) {
            for (int index = 0; index < signalGeometry.count; ++index) {
                const float top = signalGroupY + signalGeometry.strokeExtent
                    + signalGeometry.alignBaseShift
                    + static_cast<float>(index) * signalGeometry.alignDeltaShift;
                const float height = std::max(
                    signalGeometry.frontHeight
                        + static_cast<float>(index) * signalGeometry.heightDelta,
                    1.0f
                );
                contentTop = std::min(
                    contentTop, top - signalGeometry.strokeExtent - 2.0f
                );
                contentBottom = std::max(
                    contentBottom,
                    top + height + signalGeometry.strokeExtent + 2.0f
                );
            }
        }
        const float shapeGroupY = style.litOffsetY - line->laneFontAscent
            - shapeGeometry.size;
        if (shapeState.visible && shapeState.activeIndex >= 0) {
            for (int index = 0; index <= shapeState.activeIndex; ++index) {
                const bool active = index == shapeState.activeIndex;
                const float offsetY = active ? shapeState.dy : 0.0f;
                const float top = shapeGroupY + offsetY;
                const float shadowOffset = style.litShadow
                    ? std::max(shapeGeometry.size * 0.08f, 1.0f)
                    : 0.0f;
                contentTop = std::min(
                    contentTop,
                    top - shapeGeometry.strokeExtent - 2.0f
                );
                contentBottom = std::max(
                    contentBottom,
                    top + shapeGeometry.size + shadowOffset
                        + shapeGeometry.strokeExtent + 2.0f
                );
            }
        }
        int intervalTop = std::clamp(
            static_cast<int>(std::floor(dy + contentTop - topPad)),
            0,
            scene.height
        );
        int intervalBottom = std::clamp(
            static_cast<int>(std::ceil(dy + contentBottom + bottomPad)),
            0,
            scene.height
        );
        if (hasViewportTransform && !line->staticOverlay
            && intervalBottom > intervalTop) {
            auto transformedY = [&](float x, float y) {
                return x * lineViewportTransform._12
                    + y * lineViewportTransform._22
                    + lineViewportTransform._32;
            };
            const float transformedTop = std::min({
                transformedY(0.0f, static_cast<float>(intervalTop)),
                transformedY(static_cast<float>(scene.width), static_cast<float>(intervalTop)),
                transformedY(0.0f, static_cast<float>(intervalBottom)),
                transformedY(static_cast<float>(scene.width), static_cast<float>(intervalBottom)),
            });
            const float transformedBottom = std::max({
                transformedY(0.0f, static_cast<float>(intervalTop)),
                transformedY(static_cast<float>(scene.width), static_cast<float>(intervalTop)),
                transformedY(0.0f, static_cast<float>(intervalBottom)),
                transformedY(static_cast<float>(scene.width), static_cast<float>(intervalBottom)),
            });
            intervalTop = std::clamp(
                static_cast<int>(std::floor(transformedTop)) - 2,
                0,
                scene.height
            );
            intervalBottom = std::clamp(
                static_cast<int>(std::ceil(transformedBottom)) + 2,
                0,
                scene.height
            );
        }
        if (intervalBottom > intervalTop) {
            readbackIntervals.emplace_back(intervalTop, intervalBottom);
        }
        auto imageForPaint = [&](const PaintStyle &paint) -> ID2D1Bitmap1 * {
            const auto found = std::find_if(
                impl_->images.begin(), impl_->images.end(),
                [&](const Impl::CachedImage &image) {
                    return image.path == paint.imagePath
                        && image.modifiedMs == paint.imageModifiedMs
                        && image.size == paint.imageSize;
                }
            );
            return found == impl_->images.end() ? nullptr : found->bitmap.Get();
        };
        auto bitmapImageForGuide = [&](const BitmapGuide &guide, bool after) -> ID2D1Bitmap1 * {
            const std::wstring &path = after && !guide.afterPath.empty()
                ? guide.afterPath
                : guide.beforePath;
            const std::uint64_t modifiedMs = after && !guide.afterPath.empty()
                ? guide.afterModifiedMs
                : guide.beforeModifiedMs;
            const std::uint64_t size = after && !guide.afterPath.empty()
                ? guide.afterSize
                : guide.beforeSize;
            const auto found = std::find_if(
                impl_->images.begin(), impl_->images.end(),
                [&](const Impl::CachedImage &image) {
                    return image.path == path
                        && image.modifiedMs == modifiedMs
                        && image.size == size;
                }
            );
            if (found == impl_->images.end()) {
                return nullptr;
            }
            // 动图（GIF）按渲染时间选帧：与 Python 侧
            // metrics.AnimatedGuideImage.frame_at 同一契约（循环取模 +
            // 累积延时表线性查找），锚点来自 IR 的 anim_anchor_ms。
            if (found->frames.size() > 1 && found->frames.size() == found->frameDelaysMs.size()) {
                int totalMs = 0;
                for (int delay : found->frameDelaysMs) {
                    totalMs += delay;
                }
                if (totalMs > 0) {
                    const long long elapsed = std::max(
                        static_cast<long long>(tMs) - static_cast<long long>(guide.animAnchorMs),
                        0LL
                    );
                    const long long position = elapsed % totalMs;
                    long long cumulative = 0;
                    for (std::size_t index = 0; index < found->frameDelaysMs.size(); ++index) {
                        cumulative += found->frameDelaysMs[index];
                        if (position < cumulative) {
                            return found->frames[index].Get();
                        }
                    }
                }
                return found->frames.back().Get();
            }
            return found->bitmap.Get();
        };
        const auto roleMainFillBounds = [&](int styleIndex) {
            const auto found = line->horizontalFillBoundsByStyle.find(styleIndex);
            if (found != line->horizontalFillBoundsByStyle.end()) {
                return found->second;
            }
            D2D1_RECT_F bounds = line->fillBounds;
            if (line->bounds.right > line->bounds.left) {
                bounds.left = line->bounds.left;
                bounds.right = line->bounds.right;
            }
            return bounds;
        };
        auto paintBrushAt = [&](const PaintStyle &paint, const D2D1_RECT_F &rect,
                                 const RgbaColor &fallback,
                                 float offsetX, float offsetY) {
            D2D1_RECT_F effectiveRect = rect;
            ID2D1Bitmap1 *image = imageForPaint(paint);
            const float canvasDx = dx + offsetX;
            const float canvasDy = dy + offsetY;
            const bool gradientPositionDependent = paint.mode == "gradient_horizontal"
                || paint.mode == "gradient_vertical"
                || paint.mode == "split_vertical";
            const auto samePosition = [&](const Impl::CachedBrush &entry) {
                if (gradientPositionDependent) {
                    return entry.rect.left == effectiveRect.left
                        && entry.rect.top == effectiveRect.top
                        && entry.rect.right == effectiveRect.right
                        && entry.rect.bottom == effectiveRect.bottom;
                }
                if (paint.mode == "image") {
                    return entry.canvasDx == canvasDx
                        && entry.canvasDy == canvasDy;
                }
                return true;
            };
            Microsoft::WRL::ComPtr<ID2D1Brush> brush;
            if (impl_->resourceCacheEnabled) {
                const auto found = std::find_if(
                    impl_->brushes.begin(), impl_->brushes.end(),
                    [&](const Impl::CachedBrush &entry) {
                        return entry.paint == paint
                            && entry.fallback == fallback
                            && entry.imageIdentity == image
                            && samePosition(entry);
                    }
                );
                if (found != impl_->brushes.end()) {
                    found->lastUse = ++impl_->brushUseSerial;
                    brush = found->brush;
                    if (impl_->countersEnabled) {
                        ++impl_->diagnostics.brushCacheHits;
                    }
                } else {
                    if (impl_->countersEnabled) {
                        ++impl_->diagnostics.brushCacheMisses;
                    }
                    brush = createPaintBrush(
                        context, paint, effectiveRect, fallback, device_, image,
                        canvasDx, canvasDy,
                        impl_->countersEnabled
                            ? &frameDiagnostics.brushCreated
                            : nullptr
                    );
                    if (impl_->brushes.size() >= Impl::brushCapacity) {
                        const auto oldest = std::min_element(
                            impl_->brushes.begin(), impl_->brushes.end(),
                            [](const Impl::CachedBrush &left,
                               const Impl::CachedBrush &right) {
                                return left.lastUse < right.lastUse;
                            }
                        );
                        impl_->brushes.erase(oldest);
                        if (impl_->countersEnabled) {
                            ++impl_->diagnostics.brushCacheEvictions;
                        }
                    }
                    impl_->brushes.push_back(Impl::CachedBrush{
                        paint,
                        fallback,
                        image,
                        effectiveRect,
                        canvasDx,
                        canvasDy,
                        brush,
                        ++impl_->brushUseSerial,
                    });
                }
            } else {
                brush = createPaintBrush(
                    context, paint, effectiveRect, fallback, device_, image,
                    canvasDx, canvasDy,
                    impl_->countersEnabled
                        ? &frameDiagnostics.brushCreated
                        : nullptr
                );
            }
            if (brush) {
                updatePaintBrush(
                    brush.Get(), paint, effectiveRect, canvasDx, canvasDy
                );
                brush->SetOpacity(globalOpacity);
            }
            return brush;
        };
        auto paintBrush = [&](const PaintStyle &paint, const D2D1_RECT_F &rect,
                              const RgbaColor &fallback) {
            return paintBrushAt(paint, rect, fallback, 0.0f, 0.0f);
        };
        const auto mainPaintBounds = [&](const PaintStyle &paint, int styleIndex) {
            return paint.mode == "gradient_horizontal"
                ? roleMainFillBounds(styleIndex)
                : line->fillBounds;
        };
        Microsoft::WRL::ComPtr<ID2D1Brush> beforeFill = paintBrush(
            style.beforeFillPaint, mainPaintBounds(style.beforeFillPaint, -1), style.beforeFill
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> afterFill = paintBrush(
            style.afterFillPaint, mainPaintBounds(style.afterFillPaint, -1), style.afterFill
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> beforeStroke = paintBrush(
            style.beforeStrokePaint, mainPaintBounds(style.beforeStrokePaint, -1), style.beforeStroke
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> afterStroke = paintBrush(
            style.afterStrokePaint, mainPaintBounds(style.afterStrokePaint, -1), style.afterStroke
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> beforeStroke2 = paintBrush(
            style.beforeStroke2Paint, mainPaintBounds(style.beforeStroke2Paint, -1), style.beforeStroke2
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> afterStroke2 = paintBrush(
            style.afterStroke2Paint, mainPaintBounds(style.afterStroke2Paint, -1), style.afterStroke2
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> beforeDecor = paintBrush(
            style.beforeDecorPaint, mainPaintBounds(style.beforeDecorPaint, -1), style.beforeDecor
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> afterDecor = paintBrush(
            style.afterDecorPaint, mainPaintBounds(style.afterDecorPaint, -1), style.afterDecor
        );

        const bool reverseVertical = style.vertical && line->wipeReverse;
        const bool rtl = !style.vertical
            && (style.rightToLeft != line->wipeReverse);
        const bool noWipe = line->karaokeAnimation == "no_wipe";
        const auto wipePositionAt = [&](const Impl::CachedChar &ch) {
            if (ch.wipePoints.empty()) {
                if (noWipe) {
                    return tMs >= ch.endMs ? 1.0f : 0.0f;
                }
                const int duration = std::max(ch.endMs - ch.startMs, 1);
                return std::clamp(
                    static_cast<float>(tMs - ch.startMs) / static_cast<float>(duration),
                    0.0f, 1.0f
                );
            }
            if (noWipe) {
                return tMs >= ch.wipePoints.back().timeMs
                    ? ch.wipePoints.back().position
                    : ch.wipePoints.front().position;
            }
            if (tMs <= ch.wipePoints.front().timeMs) {
                return ch.wipePoints.front().position;
            }
            if (tMs >= ch.wipePoints.back().timeMs) {
                return ch.wipePoints.back().position;
            }
            for (std::size_t index = 1; index < ch.wipePoints.size(); ++index) {
                const WipePoint &previous = ch.wipePoints[index - 1];
                const WipePoint &following = ch.wipePoints[index];
                if (tMs >= following.timeMs) {
                    continue;
                }
                const int duration = following.timeMs - previous.timeMs;
                if (duration <= 0) {
                    return following.position;
                }
                const float local = std::clamp(
                    static_cast<float>(tMs - previous.timeMs)
                        / static_cast<float>(duration),
                    0.0f, 1.0f
                );
                return previous.position
                    + (following.position - previous.position) * local;
            }
            return ch.wipePoints.back().position;
        };
        const auto wipeCoordinateAt = [&](const Impl::CachedChar &ch) {
            const float position = wipePositionAt(ch);
            return style.vertical
                ? (reverseVertical
                    ? ch.bottom - (ch.bottom - ch.top) * position
                    : ch.top + (ch.bottom - ch.top) * position)
                : (rtl
                    ? ch.right - (ch.right - ch.left) * position
                     : ch.left + (ch.right - ch.left) * position);
        };
        const auto unclampedWipePositionAt = [&](const Impl::CachedChar &ch) {
            if (noWipe) {
                if (ch.wipePoints.empty()) {
                    return tMs >= ch.endMs ? 1.0f : 0.0f;
                }
                return tMs >= ch.wipePoints.back().timeMs
                    ? ch.wipePoints.back().position
                    : ch.wipePoints.front().position;
            }
            if (ch.wipePoints.size() < 2) {
                const int duration = std::max(ch.endMs - ch.startMs, 1);
                return static_cast<float>(tMs - ch.startMs)
                    / static_cast<float>(duration);
            }
            if (tMs < ch.wipePoints.front().timeMs) {
                return ch.wipePoints.front().position;
            }
            std::size_t begin = 0;
            for (std::size_t index = ch.wipePoints.size() - 1; index > 0; --index) {
                if (ch.wipePoints[index - 1].timeMs <= tMs) {
                    begin = index - 1;
                    break;
                }
            }
            const WipePoint &previous = ch.wipePoints[begin];
            const WipePoint &following = ch.wipePoints[begin + 1];
            const int duration = following.timeMs - previous.timeMs;
            if (duration == 0) {
                return following.position;
            }
            return previous.position
                + (following.position - previous.position)
                    * static_cast<float>(tMs - previous.timeMs)
                    / static_cast<float>(duration);
        };
        const auto unclampedWipeCoordinateAt = [&](const Impl::CachedChar &ch) {
            const float position = unclampedWipePositionAt(ch);
            return style.vertical
                ? (reverseVertical
                    ? ch.bottom - (ch.bottom - ch.top) * position
                    : ch.top + (ch.bottom - ch.top) * position)
                : (rtl
                    ? ch.right - (ch.right - ch.left) * position
                    : ch.left + (ch.right - ch.left) * position);
        };
        enum class N3WipePhase { Before, After, Wiping };
        const auto wipePhaseAt = [&](const std::vector<Impl::CachedChar> &chars,
                                     std::size_t index) {
            const Impl::CachedChar &ch = chars[index];
            const int start = wipeStartMs(ch);
            const int end = wipeEndMs(ch);
            bool wiping = start < tMs && tMs < end && start != end;
            if (!wiping && index + 1 < chars.size()) {
                const int followingEnd = wipeEndMs(chars[index + 1]);
                wiping = start < tMs && tMs < followingEnd
                    && start != followingEnd;
            }
            if (wiping) {
                return N3WipePhase::Wiping;
            }
            return tMs <= start ? N3WipePhase::Before : N3WipePhase::After;
        };
        const auto delegatedWipeCoordinateAt = [&](
            const std::vector<Impl::CachedChar> &chars, std::size_t index
        ) {
            const Impl::CachedChar &ch = chars[index];
            if (tMs > wipeEndMs(ch) && index + 1 < chars.size()
                && chars[index + 1].geometry != nullptr) {
                return wipeCoordinateAt(chars[index + 1]);
            }
            return tMs > wipeEndMs(ch)
                ? unclampedWipeCoordinateAt(ch)
                : wipeCoordinateAt(ch);
        };
        const auto charWipeComplete = [&](std::size_t charIndex) {
            return charIndex < line->chars.size()
                && wipePhaseAt(line->chars, charIndex) == N3WipePhase::After;
        };
        float wipeEdge = style.vertical
            ? (reverseVertical ? line->fillBounds.bottom : line->fillBounds.top)
            : (rtl ? line->bounds.right : line->bounds.left);
        for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
            const Impl::CachedChar &ch = line->chars[charIndex];
            if (tMs < wipeStartMs(ch)) {
                if (charIndex > 0) {
                    // A finished character rests inside the timing gap while
                    // the line is still clipped. Resting the front at that
                    // character's own endpoint (ink + primary edge / 2) cuts
                    // its outer stroke2 ring, but resting it on the following
                    // character's start front is too far: the legacy after
                    // stack draws EVERY character through one clip rect, and
                    // the next character's stroke2 ring reaches s2/2 past its
                    // wipe-left, so its left sliver would paint in after
                    // colours. Rest between the two painted extents instead:
                    // pull back by the decoration overhang, and never cover
                    // less than the pre-fix resting edge.
                    const TextStyle &charStyle = ch.styleIndex >= 0
                        && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                        ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                        : style;
                    const float decor = std::max(charStyle.stroke2Width, 0.0f) * 0.5f
                        + 0.5f;
                    const float resting = wipeCoordinateAt(ch)
                        + ((style.vertical ? reverseVertical : rtl) ? decor : -decor);
                    wipeEdge = (style.vertical ? !reverseVertical : !rtl)
                        ? std::max(wipeEdge, resting)
                        : std::min(wipeEdge, resting);
                }
                break;
            }
            wipeEdge = wipeCoordinateAt(ch);
            // At the exact hand-off frame N3 still uses this character's
            // AdjustWipeEnd endpoint. The following scanline takes over on
            // the next sample.
            if (tMs <= wipeEndMs(ch)) {
                break;
            }
        }
        // Painter releases the wipe once every timing segment is complete.
        // Keeping the final clip would leave before-colour pixels in outer
        // antialiasing, stroke2, shadow and glow extents.
        const bool mainWipeComplete = !line->chars.empty()
            && std::all_of(
                line->chars.begin(), line->chars.end(),
                [&](const Impl::CachedChar &ch) { return tMs >= wipeEndMs(ch); }
            );
        using UtopiaWipe = std::pair<D2D1_RECT_F, float>;
        std::vector<UtopiaWipe> utopiaCharWipeCache(line->chars.size());
        std::vector<bool> utopiaCharWipeReady(line->chars.size(), false);
        auto utopiaCharWipe = [&](std::size_t charIndex) {
            D2D1_RECT_F bounds{};
            if (charIndex >= line->chars.size()) {
                return UtopiaWipe{bounds, 0.0f};
            }
            if (utopiaCharWipeReady[charIndex]) {
                return utopiaCharWipeCache[charIndex];
            }
            std::size_t wipeIndex = charIndex;
            if (charIndex < line->chars.size()
                && tMs > wipeEndMs(line->chars[charIndex])
                && charIndex + 1 < line->chars.size()
                && line->chars[charIndex + 1].geometry != nullptr) {
                wipeIndex = charIndex + 1;
            }
            ID2D1Geometry *geometry = charGeometryAt(charIndex);
            if (geometry == nullptr) {
                utopiaCharWipeReady[charIndex] = true;
                utopiaCharWipeCache[charIndex] = UtopiaWipe{bounds, 0.0f};
                return utopiaCharWipeCache[charIndex];
            }
            checkHr(
                geometry->GetBounds(nullptr, &bounds),
                "ID2D1Geometry::GetBounds(utopia wipe)",
                device_
            );
            // The wipe edge delegates to the following character while that
            // character is still wiping, but the clip rect must keep covering
            // this character's own glyph; only the edge travels across the
            // delegated extent.
            D2D1_RECT_F wipeBounds = bounds;
            if (wipeIndex != charIndex) {
                ID2D1Geometry *wipeGeometry = charGeometryAt(wipeIndex);
                if (wipeGeometry == nullptr) {
                    wipeIndex = charIndex;
                } else {
                    checkHr(
                        wipeGeometry->GetBounds(nullptr, &wipeBounds),
                        "ID2D1Geometry::GetBounds(utopia wipe)",
                        device_
                    );
                }
            }
            const Impl::CachedChar &ch = line->chars[wipeIndex];
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            const float edgeHalf = static_cast<float>(
                std::max(static_cast<int>(charStyle.strokeWidth), 0) / 2
            );
            const float left = std::floor(wipeBounds.left) - edgeHalf;
            const float right = std::ceil(wipeBounds.right) + edgeHalf;
            float ratio = 0.0f;
            const CharacterAnimationState animationState = characterAnimationAt(wipeIndex);
            if (animationState.utopiaExit) {
                ratio = 1.0f;
            } else if (tMs > wipeStartMs(ch)) {
                ratio = tMs > wipeEndMs(ch)
                    ? unclampedWipePositionAt(ch)
                    : wipePositionAt(ch);
            }
            utopiaCharWipeReady[charIndex] = true;
            utopiaCharWipeCache[charIndex] = UtopiaWipe{
                bounds,
                style.vertical
                    ? (reverseVertical
                        ? std::ceil(wipeBounds.bottom) + edgeHalf
                            - std::max(
                                std::ceil(wipeBounds.bottom)
                                    - std::floor(wipeBounds.top) + edgeHalf * 2.0f,
                                1.0f
                            ) * ratio
                        : std::floor(wipeBounds.top) - edgeHalf
                            + std::max(
                                std::ceil(wipeBounds.bottom)
                                    - std::floor(wipeBounds.top) + edgeHalf * 2.0f,
                                1.0f
                            ) * ratio)
                    : (rtl
                        ? right - std::max(right - left, 1.0f) * ratio
                        : left + std::max(right - left, 1.0f) * ratio)
            };
            return utopiaCharWipeCache[charIndex];
        };
        std::vector<std::vector<UtopiaWipe>> utopiaRubyWipeCache;
        std::vector<std::vector<bool>> utopiaRubyWipeReady;
        utopiaRubyWipeCache.reserve(line->rubies.size());
        utopiaRubyWipeReady.reserve(line->rubies.size());
        for (const Impl::CachedRuby &ruby : line->rubies) {
            utopiaRubyWipeCache.emplace_back(ruby.chars.size());
            utopiaRubyWipeReady.emplace_back(ruby.chars.size(), false);
        }
        auto utopiaRubyUnitWipe = [&](const Impl::CachedRuby &ruby,
                                      std::size_t rubyIndex,
                                      std::size_t unitIndex,
                                      const TextStyle &rubyStyle) {
            D2D1_RECT_F bounds{};
            if (rubyIndex >= utopiaRubyWipeCache.size()
                || unitIndex >= utopiaRubyWipeCache[rubyIndex].size()) {
                return UtopiaWipe{bounds, 0.0f};
            }
            if (utopiaRubyWipeReady[rubyIndex][unitIndex]) {
                return utopiaRubyWipeCache[rubyIndex][unitIndex];
            }
            ID2D1Geometry *geometry = rubyGeometryAt(rubyIndex, unitIndex);
            if (geometry == nullptr || unitIndex >= ruby.chars.size()) {
                utopiaRubyWipeReady[rubyIndex][unitIndex] = true;
                utopiaRubyWipeCache[rubyIndex][unitIndex] = UtopiaWipe{bounds, 0.0f};
                return utopiaRubyWipeCache[rubyIndex][unitIndex];
            }
            checkHr(
                geometry->GetBounds(nullptr, &bounds),
                "ID2D1Geometry::GetBounds(utopia ruby wipe)",
                device_
            );
            const Impl::CachedChar &unit = ruby.chars[unitIndex];
            const float edgeHalf = static_cast<float>(
                std::max(static_cast<int>(rubyStyle.rubyStrokeWidth), 0) / 2
            );
            const float left = std::floor(bounds.left) - edgeHalf;
            const float right = std::ceil(bounds.right) + edgeHalf;
            float ratio = 0.0f;
            const CharacterAnimationState animationState = rubyUnitAnimationAt(
                ruby, unitIndex
            );
            if (animationState.utopiaExit || tMs >= wipeEndMs(unit)) {
                ratio = 1.0f;
            } else if (tMs > wipeStartMs(unit)) {
                ratio = wipePositionAt(unit);
            }
            utopiaRubyWipeReady[rubyIndex][unitIndex] = true;
            utopiaRubyWipeCache[rubyIndex][unitIndex] = UtopiaWipe{
                bounds,
                style.vertical
                    ? (reverseVertical
                        ? std::ceil(bounds.bottom) + edgeHalf
                            - std::max(
                                std::ceil(bounds.bottom) - std::floor(bounds.top)
                                    + edgeHalf * 2.0f,
                                1.0f
                            ) * ratio
                        : std::floor(bounds.top) - edgeHalf
                            + std::max(
                                std::ceil(bounds.bottom) - std::floor(bounds.top)
                                    + edgeHalf * 2.0f,
                                1.0f
                            ) * ratio)
                    : (rtl
                        ? right - std::max(right - left, 1.0f) * ratio
                        : left + std::max(right - left, 1.0f) * ratio)
            };
            return utopiaRubyWipeCache[rubyIndex][unitIndex];
        };

        const float geometryPad = std::max(style.strokeWidth + style.stroke2Width, 2.0f) + 4.0f;
        // N3 splits before/after colours with a full-frame vertical clip
        // (0..MovieInfo.Height). A line-local vertical clip creates a hard
        // horizontal seam when Utopia/CharDrip moves a glyph outside the
        // stable row. Keep a deliberately oversized local range so the target
        // surface, not the row box, is the only vertical boundary.
        const float fullWipeClipTop = -static_cast<float>(scene.height) * 2.0f;
        const float fullWipeClipBottom = static_cast<float>(scene.height) * 2.0f;
        const auto directionalWipeClip = [&](const D2D1_RECT_F &bounds,
                                             float edge, float pad, bool after) {
            if (style.vertical) {
                if (reverseVertical) {
                    return after
                        ? D2D1::RectF(
                            bounds.left - pad, edge,
                            bounds.right + pad, bounds.bottom + pad
                        )
                        : D2D1::RectF(
                            bounds.left - pad, bounds.top - pad,
                            bounds.right + pad, edge
                        );
                }
                return after
                    ? D2D1::RectF(
                        bounds.left - pad, bounds.top - pad,
                        bounds.right + pad, edge
                    )
                    : D2D1::RectF(
                        bounds.left - pad, edge,
                        bounds.right + pad, bounds.bottom + pad
                    );
            }
            if (rtl) {
                return after
                    ? D2D1::RectF(
                        edge, fullWipeClipTop,
                        bounds.right + pad, fullWipeClipBottom
                    )
                    : D2D1::RectF(
                        bounds.left - pad, fullWipeClipTop,
                        edge, fullWipeClipBottom
                    );
            }
            return after
                ? D2D1::RectF(
                    bounds.left - pad, fullWipeClipTop,
                    edge, fullWipeClipBottom
                )
                : D2D1::RectF(
                    edge, fullWipeClipTop,
                    bounds.right + pad, fullWipeClipBottom
                );
        };
        const D2D1_RECT_F afterClip = style.vertical
            ? (reverseVertical
                ? D2D1::RectF(
                    line->bounds.left - geometryPad,
                    wipeEdge,
                    line->bounds.right + geometryPad,
                    line->fillBounds.bottom + geometryPad
                )
                : D2D1::RectF(
                    line->bounds.left - geometryPad,
                    line->fillBounds.top - geometryPad,
                    line->bounds.right + geometryPad,
                    wipeEdge
                ))
            : (rtl
                ? D2D1::RectF(
                    wipeEdge,
                    fullWipeClipTop,
                    line->bounds.right + geometryPad,
                    fullWipeClipBottom
                )
                : D2D1::RectF(
                    line->bounds.left - geometryPad,
                    fullWipeClipTop,
                    wipeEdge,
                    fullWipeClipBottom
                ));
        const bool hasAfterWipe = style.vertical
            ? (reverseVertical
                ? wipeEdge < line->fillBounds.bottom
                : wipeEdge > line->fillBounds.top)
            : (rtl ? wipeEdge < line->bounds.right : wipeEdge > line->bounds.left);
        auto bitmapGuideNoWipe = [&](const Impl::CachedChar &ch) {
            return ch.bitmapGuide.has_value() && ch.bitmapGuide->afterPath.empty();
        };
        // N3 文字装饰（shadow / glow）套用到位图导唱符：ColorMatrix 把图片
        // Alpha 染成飾り色（走字前后两态，仅有走字后图片时切到后态），
        // shadow 按偏移平移绘制、glow 级联 GaussianBlur（BlurLevel+1 层，
        // 与文字发光同一 sigma 公式）。效果直接画到帧目标上，继承调用方已
        // 设置的 wipe 分侧裁切；NoDecor 跳过（@Emoji NoDecor 语义）。
        auto acquireDecorEffect = [&](
            const IID &effectId,
            std::vector<Microsoft::WRL::ComPtr<ID2D1Effect>> &pool,
            std::size_t &inUse,
            const char *operation
        ) -> ID2D1Effect * {
            if (inUse < pool.size()) {
                return pool[inUse++].Get();
            }
            Microsoft::WRL::ComPtr<ID2D1Effect> effect;
            checkHr(
                context->CreateEffect(
                    effectId, effect.ReleaseAndGetAddressOf()
                ),
                operation,
                device_
            );
            pool.push_back(effect);
            ++inUse;
            return pool.back().Get();
        };
        // 渐变飾り（gradient_horizontal / gradient_vertical / split_vertical）
        // 按画刷语义在 CPU 上采样成位图：native 像素 (x,y) 对应位图导唱符矩形
        // 内的设备坐标，t 沿整行 fillBounds 量取，与 createPaintBrush 的
        // 线性渐变映射同口径（GAMMA_2_2 = 分量直插，split 为硬边界）。
        auto decorGradientBitmap = [&](
            const PaintStyle &paint,
            const D2D1_RECT_F &fillBounds,
            const D2D1_RECT_F &bitmapRect,
            float pixelW,
            float pixelH
        ) -> ID2D1Bitmap1 * {
            const bool gradient = paint.mode == "gradient_horizontal"
                || paint.mode == "gradient_vertical"
                || paint.mode == "split_vertical";
            if (!gradient || paint.stops.empty()) {
                return nullptr;
            }
            const std::uint32_t width = std::max<std::uint32_t>(
                static_cast<std::uint32_t>(std::lround(pixelW)), 1
            );
            const std::uint32_t height = std::max<std::uint32_t>(
                static_cast<std::uint32_t>(std::lround(pixelH)), 1
            );
            const auto sameRect = [](const D2D1_RECT_F &left,
                                     const D2D1_RECT_F &right) {
                return std::abs(left.left - right.left) < 0.5f
                    && std::abs(left.top - right.top) < 0.5f
                    && std::abs(left.right - right.right) < 0.5f
                    && std::abs(left.bottom - right.bottom) < 0.5f;
            };
            for (Impl::CachedDecorGradient &entry : impl_->decorGradientCache) {
                if (entry.paint == paint
                    && entry.width == width
                    && entry.height == height
                    && sameRect(entry.fillBounds, fillBounds)
                    && sameRect(entry.bitmapRect, bitmapRect)) {
                    entry.lastUse = ++impl_->decorGradientUseSerial;
                    return entry.bitmap.Get();
                }
            }
            std::vector<PaintStop> ordered = paint.stops;
            std::stable_sort(
                ordered.begin(), ordered.end(),
                [](const PaintStop &left, const PaintStop &right) {
                    return left.position < right.position;
                }
            );
            if (ordered.size() == 1) {
                ordered.push_back(PaintStop{1.0f, ordered.front().color});
            }
            const bool split = paint.mode == "split_vertical";
            const auto sampleAt = [&](float t) -> RgbaColor {
                if (split) {
                    if (t <= ordered.front().position) {
                        return ordered.front().color;
                    }
                    for (std::size_t index = 1; index < ordered.size(); ++index) {
                        if (t <= ordered[index].position) {
                            return ordered[index - 1].color;
                        }
                    }
                    return ordered.back().color;
                }
                t = std::clamp(t, 0.0f, 1.0f);
                if (t <= ordered.front().position) {
                    return ordered.front().color;
                }
                for (std::size_t index = 1; index < ordered.size(); ++index) {
                    if (t <= ordered[index].position) {
                        const PaintStop &lo = ordered[index - 1];
                        const PaintStop &hi = ordered[index];
                        const float span = std::max(
                            hi.position - lo.position, 1e-6f
                        );
                        const float factor = std::clamp(
                            (t - lo.position) / span, 0.0f, 1.0f
                        );
                        const auto mix = [&](std::uint8_t a, std::uint8_t b) {
                            return static_cast<std::uint8_t>(
                                std::lround(a + (b - a) * factor)
                            );
                        };
                        return RgbaColor{
                            mix(lo.color.red, hi.color.red),
                            mix(lo.color.green, hi.color.green),
                            mix(lo.color.blue, hi.color.blue),
                            mix(lo.color.alpha, hi.color.alpha),
                        };
                    }
                }
                return ordered.back().color;
            };
            const float scaleX = (bitmapRect.right - bitmapRect.left) / pixelW;
            const float scaleY = (bitmapRect.bottom - bitmapRect.top) / pixelH;
            const float fillW = std::max(
                fillBounds.right - fillBounds.left, 1.0f
            );
            const float fillH = std::max(
                fillBounds.bottom - fillBounds.top, 1.0f
            );
            const bool horizontal = paint.mode == "gradient_horizontal";
            std::vector<std::uint8_t> pixels(
                static_cast<std::size_t>(width) * height * 4, 0
            );
            for (std::uint32_t y = 0; y < height; ++y) {
                for (std::uint32_t x = 0; x < width; ++x) {
                    const float deviceX = bitmapRect.left + (x + 0.5f) * scaleX;
                    const float deviceY = bitmapRect.top + (y + 0.5f) * scaleY;
                    const float t = horizontal
                        ? (deviceX - fillBounds.left) / fillW
                        : (deviceY - fillBounds.top) / fillH;
                    const RgbaColor color = sampleAt(t);
                    const float alpha = color.alpha / 255.0f;
                    std::uint8_t *pixel = &pixels[
                        (static_cast<std::size_t>(y) * width + x) * 4
                    ];
                    pixel[0] = static_cast<std::uint8_t>(
                        std::lround(color.blue * alpha)
                    );
                    pixel[1] = static_cast<std::uint8_t>(
                        std::lround(color.green * alpha)
                    );
                    pixel[2] = static_cast<std::uint8_t>(
                        std::lround(color.red * alpha)
                    );
                    pixel[3] = color.alpha;
                }
            }
            const D2D1_BITMAP_PROPERTIES1 properties = D2D1::BitmapProperties1(
                D2D1_BITMAP_OPTIONS_NONE,
                D2D1::PixelFormat(
                    DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED
                )
            );
            Impl::CachedDecorGradient entry;
            entry.paint = paint;
            entry.fillBounds = fillBounds;
            entry.bitmapRect = bitmapRect;
            entry.width = width;
            entry.height = height;
            checkHr(
                context->CreateBitmap(
                    D2D1::SizeU(width, height),
                    pixels.data(),
                    width * 4,
                    properties,
                    entry.bitmap.ReleaseAndGetAddressOf()
                ),
                "ID2D1DeviceContext::CreateBitmap(bitmap guide decor gradient)",
                device_
            );
            entry.lastUse = ++impl_->decorGradientUseSerial;
            constexpr std::size_t kDecorGradientCap = 8;
            if (impl_->decorGradientCache.size() >= kDecorGradientCap) {
                const auto oldest = std::min_element(
                    impl_->decorGradientCache.begin(),
                    impl_->decorGradientCache.end(),
                    [](const Impl::CachedDecorGradient &left,
                       const Impl::CachedDecorGradient &right) {
                        return left.lastUse < right.lastUse;
                    }
                );
                impl_->decorGradientCache.erase(oldest);
            }
            impl_->decorGradientCache.push_back(std::move(entry));
            return impl_->decorGradientCache.back().bitmap.Get();
        };
        auto drawBitmapGuideDecor = [&](
            const Impl::CachedChar &ch,
            ID2D1Bitmap1 *bitmap,
            bool after,
            float opacity
        ) {
            const BitmapGuide &guide = *ch.bitmapGuide;
            if (guide.noDecor || opacity <= 0.0f) {
                return;
            }
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            const bool isGlow = charStyle.decorationKind == "glow";
            const bool isShadow = charStyle.decorationKind == "shadow"
                && (charStyle.shadowOffsetX != 0.0f
                    || charStyle.shadowOffsetY != 0.0f);
            if (!isGlow && !isShadow) {
                return;
            }
            const bool wiped = after && !guide.afterPath.empty();
            const PaintStyle &paint = wiped
                ? charStyle.afterDecorPaint
                : charStyle.beforeDecorPaint;
            const RgbaColor &decor = wiped
                ? charStyle.afterDecor
                : charStyle.beforeDecor;
            const D2D1_SIZE_U pixelSize = bitmap->GetPixelSize();
            const float pixelW = std::max(
                static_cast<float>(pixelSize.width), 1.0f
            );
            const float pixelH = std::max(
                static_cast<float>(pixelSize.height), 1.0f
            );
            const float rectW = ch.bitmapRect.right - ch.bitmapRect.left;
            const float rectH = ch.bitmapRect.bottom - ch.bitmapRect.top;
            if (rectW <= 0.0f || rectH <= 0.0f) {
                return;
            }
            // shadow 装饰用 FillOpacityMask + 完整画刷：纯色 / 渐变 /
            // 千层 / 贴图统一支持，剪影平移到偏移位置（src→dest 隐含缩放）。
            if (isShadow) {
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                    paint, mainPaintBounds(paint, ch.styleIndex), decor
                );
                if (!brush) {
                    return;
                }
                brush->SetOpacity(opacity);
                const D2D1_RECT_F shadowDest = D2D1::RectF(
                    ch.bitmapRect.left + charStyle.shadowOffsetX,
                    ch.bitmapRect.top + charStyle.shadowOffsetY,
                    ch.bitmapRect.right + charStyle.shadowOffsetX,
                    ch.bitmapRect.bottom + charStyle.shadowOffsetY
                );
                const D2D1_RECT_F sourceRect = D2D1::RectF(
                    0.0f, 0.0f, pixelW, pixelH
                );
                context->SetAntialiasMode(D2D1_ANTIALIAS_MODE_ALIASED);
                context->FillOpacityMask(
                    bitmap,
                    brush.Get(),
                    D2D1_OPACITY_MASK_CONTENT_GRAPHICS,
                    &shadowDest,
                    &sourceRect
                );
                context->SetAntialiasMode(D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
                return;
            }
            const float requestedRadius = wiped
                ? charStyle.glowAfterRadius
                : charStyle.glowBeforeRadius;
            const int radius = std::max(
                0, static_cast<int>(std::lround(requestedRadius))
            );
            if (radius <= 0) {
                return;
            }
            // glow 的模糊输入需要独立图像：渐变飾り用 CPU 栅格化的渐变位图
            // 经 Composite(SOURCE_IN) 与位图 Alpha 相乘；纯色 / 贴图回落到
            // ColorMatrix 主色（贴图模式与 shadow 的画刷路径存在差异）。
            Microsoft::WRL::ComPtr<ID2D1Image> tintOutput;
            ID2D1Bitmap1 *gradientRaster = decorGradientBitmap(
                paint, mainPaintBounds(paint, ch.styleIndex), ch.bitmapRect, pixelW, pixelH
            );
            if (gradientRaster != nullptr) {
                ID2D1Effect *composite = acquireDecorEffect(
                    CLSID_D2D1Composite,
                    impl_->decorCompositeEffectPool,
                    impl_->decorCompositeEffectInUse,
                    "ID2D1DeviceContext::CreateEffect(bitmap guide decor composite)"
                );
                checkHr(
                    composite->SetValue(
                        D2D1_COMPOSITE_PROP_MODE,
                        D2D1_COMPOSITE_MODE_SOURCE_IN
                    ),
                    "ID2D1Effect::SetValue(bitmap guide decor composite)",
                    device_
                );
                // SOURCE_IN 保留 source：Input 1 = source（渐变栅格）、
                // Input 0 = destination（位图 Alpha）。接反会把位图自身的
                // 颜色当剪影（探针：绿色头像会把光晕染成绿色）。
                composite->SetInput(0, bitmap);
                composite->SetInput(1, gradientRaster);
                composite->GetOutput(tintOutput.ReleaseAndGetAddressOf());
            } else {
                ID2D1Effect *tint = acquireDecorEffect(
                    CLSID_D2D1ColorMatrix,
                    impl_->decorTintEffectPool,
                    impl_->decorTintEffectInUse,
                    "ID2D1DeviceContext::CreateEffect(bitmap guide decor tint)"
                );
                // 行 = 输入 R/G/B/A/1，列 = 输出 RGBA：RGB 取飾り色常量、
                // A 保留位图 Alpha（× 本次绘制透明度）。PREMULTIPLIED 模式下
                // 效果对矩阵输出做预乘回转换——直通输出 decor×a 才是正确的
                // 剪影染色；STRAIGHT 模式不回转换，非黑颜色会被当作预乘值
                // 过亮失真（黑色对预乘不变，唯一"看起来对"的颜色）。
                const auto matrix = D2D1::Matrix5x4F(
                    0.0f, 0.0f, 0.0f, 0.0f,
                    0.0f, 0.0f, 0.0f, 0.0f,
                    0.0f, 0.0f, 0.0f, 0.0f,
                    0.0f, 0.0f, 0.0f, opacity,
                    decor.red / 255.0f, decor.green / 255.0f,
                    decor.blue / 255.0f, 0.0f
                );
                checkHr(
                    tint->SetValue(
                        D2D1_COLORMATRIX_PROP_COLOR_MATRIX, matrix
                    ),
                    "ID2D1Effect::SetValue(bitmap guide decor matrix)",
                    device_
                );
                checkHr(
                    tint->SetValue(
                        D2D1_COLORMATRIX_PROP_ALPHA_MODE,
                        D2D1_COLORMATRIX_ALPHA_MODE_PREMULTIPLIED
                    ),
                    "ID2D1Effect::SetValue(bitmap guide decor alpha mode)",
                    device_
                );
                tint->SetInput(0, bitmap);
                tint->GetOutput(tintOutput.ReleaseAndGetAddressOf());
            }
            ID2D1Effect *blur = acquireDecorEffect(
                CLSID_D2D1GaussianBlur,
                impl_->decorBlurEffectPool,
                impl_->decorBlurEffectInUse,
                "ID2D1DeviceContext::CreateEffect(bitmap guide decor blur)"
            );
            blur->SetInput(0, tintOutput.Get());
            // 半径是设备像素；效果输入是原始分辨率图片，sigma 随缩放折算，
            // 与 Painter 在设备空间模糊的结果一致。DrawImage 只有
            // targetPoint 重载，缩放通过临时世界变换完成。
            D2D1_MATRIX_3X2_F previousTransform = D2D1::Matrix3x2F::Identity();
            context->GetTransform(&previousTransform);
            const float scaleX = rectW / pixelW;
            const float scaleY = rectH / pixelH;
            const float padDevice = std::ceil(radius * 3.5f) + 2.0f;
            const float padNative = padDevice / std::max(scaleX, 0.0001f);
            const int passes = std::clamp(
                charStyle.glowConcentrationLevel, 0, 2
            ) + 1;
            context->SetTransform(
                D2D1::Matrix3x2F::Scale(scaleX, scaleY)
                    * D2D1::Matrix3x2F::Translation(
                        ch.bitmapRect.left - padDevice,
                        ch.bitmapRect.top - padDevice
                    )
                    * previousTransform
            );
            for (int index = 0; index < passes; ++index) {
                const float sigma = std::max(
                    0.0f,
                    static_cast<float>(
                        radius - index * radius / passes
                    ) / std::max(scaleX, 0.0001f)
                );
                checkHr(
                    blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION, sigma
                    ),
                    "ID2D1Effect::SetValue(bitmap guide decor sigma)",
                    device_
                );
                context->DrawImage(
                    blur,
                    D2D1::Point2F(0.0f, 0.0f),
                    D2D1::RectF(
                        -padNative,
                        -padNative,
                        pixelW + padNative,
                        pixelH + padNative
                    ),
                    D2D1_INTERPOLATION_MODE_LINEAR,
                    D2D1_COMPOSITE_MODE_SOURCE_OVER
                );
            }
            context->SetTransform(previousTransform);
        };
        auto drawBitmapGuidePart = [&](std::size_t charIndex, bool after) {
            restoreRealizationBaseTransform();
            if (charIndex >= line->chars.size()) {
                return;
            }
            const Impl::CachedChar &ch = line->chars[charIndex];
            if (!ch.bitmapGuide.has_value()) {
                return;
            }
            if (after && ch.bitmapGuide->afterPath.empty()) {
                return;
            }
            ID2D1Bitmap1 *bitmap = bitmapImageForGuide(*ch.bitmapGuide, after);
            if (bitmap == nullptr) {
                return;
            }
            const float opacity = globalOpacity * characterOpacityAt(charIndex);
            if (opacity <= 0.0f) {
                return;
            }
            // Wipe-capable avatars are a strict two-sided mask: the before
            // image stays only on the unsung side of the wipe edge (it must
            // not remain composited under a partly transparent after image).
            // The per-char phase path already clips both sides; this branch
            // matters for the legacy stack, whose before pass is unclipped.
            if (!after && !ch.bitmapGuide->afterPath.empty()) {
                if (mainWipeComplete) {
                    return;
                }
                if (hasAfterWipe) {
                    const D2D1_RECT_F beforeClip = style.vertical
                        ? (reverseVertical
                            ? D2D1::RectF(
                                line->bounds.left - geometryPad,
                                line->fillBounds.top - geometryPad,
                                line->bounds.right + geometryPad,
                                wipeEdge
                            )
                            : D2D1::RectF(
                                line->bounds.left - geometryPad, wipeEdge,
                                line->bounds.right + geometryPad,
                                line->fillBounds.bottom + geometryPad
                            ))
                        : (rtl
                            ? D2D1::RectF(
                                line->bounds.left - geometryPad, fullWipeClipTop,
                                wipeEdge, fullWipeClipBottom
                            )
                            : D2D1::RectF(
                                wipeEdge, fullWipeClipTop,
                                line->bounds.right + geometryPad, fullWipeClipBottom
                            ));
                    pushAxisAlignedClip(
                        beforeClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                    drawBitmapGuideDecor(ch, bitmap, after, opacity);
                    context->DrawBitmap(
                        bitmap,
                        ch.bitmapRect,
                        opacity,
                        D2D1_INTERPOLATION_MODE_LINEAR,
                        nullptr
                    );
                    context->PopAxisAlignedClip();
                    return;
                }
            }
            drawBitmapGuideDecor(ch, bitmap, after, opacity);
            context->DrawBitmap(
                bitmap,
                ch.bitmapRect,
                opacity,
                D2D1_INTERPOLATION_MODE_LINEAR,
                nullptr
            );
        };
        auto rubyWipeEdgeAt = [&](const Impl::CachedRuby &ruby) {
            float edge = style.vertical
                ? (reverseVertical ? ruby.bounds.bottom : ruby.bounds.top)
                : (rtl ? ruby.bounds.right : ruby.bounds.left);
            for (const Impl::CachedChar &ch : ruby.chars) {
                if (tMs < wipeStartMs(ch)) {
                    break;
                }
                edge = wipeCoordinateAt(ch);
                if (tMs <= wipeEndMs(ch)) {
                    break;
                }
            }
            return edge;
        };
        auto rubyWipeComplete = [&](const Impl::CachedRuby &ruby) {
            return !ruby.chars.empty()
                && std::all_of(
                    ruby.chars.begin(), ruby.chars.end(),
                    [&](const Impl::CachedChar &ch) { return tMs >= wipeEndMs(ch); }
                );
        };
        auto rubyWipePhaseAt = [&](const Impl::CachedRuby &ruby) {
            if (ruby.chars.empty()) {
                return N3WipePhase::Before;
            }
            const bool allBefore = std::all_of(
                ruby.chars.begin(), ruby.chars.end(),
                [&](const Impl::CachedChar &ch) { return tMs <= wipeStartMs(ch); }
            );
            if (allBefore) {
                return N3WipePhase::Before;
            }
            return rubyWipeComplete(ruby)
                ? N3WipePhase::After
                : N3WipePhase::Wiping;
        };
        auto rubyUnitWipeComplete = [&](const Impl::CachedRuby &ruby,
                                        std::size_t unitIndex) {
            return unitIndex < ruby.chars.size()
                && tMs >= wipeEndMs(ruby.chars[unitIndex]);
        };
        auto rubyUnitWipePhaseAt = [&](const Impl::CachedRuby &ruby,
                                       std::size_t unitIndex) {
            if (unitIndex >= ruby.chars.size()) {
                return N3WipePhase::Before;
            }
            const Impl::CachedChar &unit = ruby.chars[unitIndex];
            return tMs <= wipeStartMs(unit)
                ? N3WipePhase::Before
                : (tMs >= wipeEndMs(unit)
                    ? N3WipePhase::After
                    : N3WipePhase::Wiping);
        };
        auto rubyPhaseVisible = [&](const Impl::CachedRuby &ruby,
                                    float edge, bool after) {
            if (style.vertical) {
                return reverseVertical
                    ? (after ? edge < ruby.bounds.bottom : edge > ruby.bounds.top)
                    : (after ? edge > ruby.bounds.top : edge < ruby.bounds.bottom);
            }
            if (rtl) {
                return after ? edge < ruby.bounds.right : edge > ruby.bounds.left;
            }
            return after ? edge > ruby.bounds.left : edge < ruby.bounds.right;
        };
        auto rubyStyleFor = [&](int styleIndex) -> const TextStyle & {
            return styleIndex >= 0
                && styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(styleIndex)]
                : style;
        };

        // Glow sources are authored in line-local coordinates and written to
        // the scene-sized scratch after the line translation. Clamp requested
        // effect output to the scratch in the same local coordinate system.
        const D2D1_RECT_F glowCanvasRect = D2D1::RectF(
            -dx,
            -dy,
            static_cast<float>(scene.width) - dx,
            static_cast<float>(scene.height) - dy
        );
        const auto clampGlowRect = [&](const D2D1_RECT_F &rect) {
            return D2D1::RectF(
                std::max(rect.left, glowCanvasRect.left),
                std::max(rect.top, glowCanvasRect.top),
                std::min(rect.right, glowCanvasRect.right),
                std::min(rect.bottom, glowCanvasRect.bottom)
            );
        };
        const auto glowOutputRect = [&] (
            const D2D1_RECT_F &content,
            float sourceWidth,
            int radius
        ) {
            const float expansion = sourceWidth + 3.0f * radius + 16.0f;
            const D2D1_RECT_F expanded = expandedRect(content, expansion);
            return impl_->glowDirtyRectEnabled
                ? clampGlowRect(expanded)
                : expanded;
        };
        const auto glowClearBounds = [&] (
            const D2D1_RECT_F &sourceRect,
            int radius
        ) {
            const float expansion = 3.0f * radius + 16.0f;
            const D2D1_RECT_F expanded = expandedRect(sourceRect, expansion);
            if (!impl_->glowDirtyRectEnabled) {
                return expanded;
            }
            const D2D1_RECT_F clamped = clampGlowRect(expanded);
            // Keep the cropped target origin on the same device-pixel grid as
            // the full-scene scratch. Otherwise a fractional crop origin can
            // shift Direct2D antialias coverage even when the algebraic
            // source/destination rectangles cancel out.
            return D2D1::RectF(
                std::floor(clamped.left + dx) - dx,
                std::floor(clamped.top + dy) - dy,
                std::ceil(clamped.right + dx) - dx,
                std::ceil(clamped.bottom + dy) - dy
            );
        };

        const auto glowStart = Clock::now();
        struct MainGlowLayer {
            ID2D1Bitmap1 *source = nullptr;
            ID2D1Effect *blur = nullptr;
            std::vector<int> sigmas;
            D2D1_RECT_F sourceRect{};
            D2D1_RECT_F effectRect{};
        };
        std::vector<MainGlowLayer> mainGlowLayers;
        if (style.decorationKind == "glow"
            && !line->hasInlineStyles) {
            // 走字前/走字后发光半径不同时按状态拆成两个 source：每层用自己
            // 的轮廓笔宽与 sigma 阶梯（对应 Painter 经 karaoke_glow_states_differ
            // 的拆层）。半径相等则保持 N3 DrawOneLineDecorBlurMulti 的合并单源
            // ——N3 两色共用一个 DecorSize、一次模糊，此路径逐字节不变。
            const int beforeRadius = std::max(
                0, static_cast<int>(std::lround(style.glowBeforeRadius))
            );
            const int afterRadius = std::max(
                0, static_cast<int>(std::lround(style.glowAfterRadius))
            );
            const bool splitGlowStates = beforeRadius != afterRadius;
            for (int stateIndex = 0; stateIndex < (splitGlowStates ? 2 : 1);
                 ++stateIndex) {
                const bool stateAfter = splitGlowStates && stateIndex == 1;
                const bool *stateOnly = splitGlowStates ? &stateAfter : nullptr;
                const int radius = splitGlowStates
                    ? (stateAfter ? afterRadius : beforeRadius)
                    : std::max(
                        1,
                        static_cast<int>(std::lround(std::max(
                            style.glowBeforeRadius, style.glowAfterRadius
                        )))
                    );
                if (radius <= 0) {
                    // 该状态半径为 0：完全没有光晕（与 Painter radius==0 跳层一致）。
                    continue;
                }
                if (stateOnly != nullptr) {
                    const bool hasVisibleSource = std::any_of(
                        line->chars.begin(), line->chars.end(),
                        [&](const Impl::CachedChar &ch) {
                            const std::size_t charIndex = static_cast<std::size_t>(
                                &ch - line->chars.data()
                            );
                            if (!charUsesGroupedGlowAt(charIndex)
                                || charGeometryAt(charIndex) == nullptr) {
                                return false;
                            }
                            const N3WipePhase phase = wipePhaseAt(
                                line->chars, charIndex
                            );
                            return stateAfter
                                ? phase != N3WipePhase::Before
                                : phase != N3WipePhase::After;
                        }
                    );
                    if (!hasVisibleSource) {
                        continue;
                    }
                }
                MainGlowLayer layer;
                const float sourceWidth = std::max(0.0f, style.strokeWidth)
                    + (style.stroke2Width > 0.0f ? style.stroke2Width : 0.0f)
                    + static_cast<float>(radius);
                // Restrict the scratch clear and, at composite time, the blur
                // evaluation to the line's neighbourhood; Direct2D effects only
                // process the input needed for the requested output rectangle.
                const D2D1_RECT_F glowContent = unionRect(
                    line->bounds, line->fillBounds
                );
                layer.sourceRect = glowOutputRect(
                    glowContent, sourceWidth, radius
                );
                count(
                    frameDiagnostics.glowSourceAreaPx,
                    rectAreaPx(layer.sourceRect)
                );
                const D2D1_RECT_F glowClearRect = glowClearBounds(
                    layer.sourceRect, radius
                );
                layer.source = acquireGlowScratch(
                    glowClearRect.right - glowClearRect.left,
                    glowClearRect.bottom - glowClearRect.top
                );
                layer.blur = acquireGlowEffect();
                layer.effectRect = impl_->glowDirtyRectEnabled
                    ? D2D1::RectF(
                        layer.sourceRect.left - glowClearRect.left,
                        layer.sourceRect.top - glowClearRect.top,
                        layer.sourceRect.right - glowClearRect.left,
                        layer.sourceRect.bottom - glowClearRect.top
                    )
                    : D2D1::RectF(
                        layer.sourceRect.left + dx,
                        layer.sourceRect.top + dy,
                        layer.sourceRect.right + dx,
                        layer.sourceRect.bottom + dy
                    );
                context->SetTarget(layer.source);
                context->SetTransform(
                    impl_->glowDirtyRectEnabled
                        ? D2D1::Matrix3x2F::Translation(
                            -glowClearRect.left, -glowClearRect.top
                        )
                        : D2D1::Matrix3x2F::Translation(dx, dy)
                );
                context->BeginDraw();
                // The pooled bitmap can be larger than this frame's requested
                // crop. Clear the whole target before installing the local clip;
                // otherwise GaussianBlur may sample stale pixels just outside the
                // crop and make output depend on this worker's previous frame.
                context->Clear(D2D1::ColorF(0.0f, 0.0f));
                pushAxisAlignedClip(
                    glowClearRect, D2D1_ANTIALIAS_MODE_ALIASED
                );
                const auto drawGlowPart = [&](std::size_t index, bool after) {
                    if (!charUsesGroupedGlowAt(index)) {
                        return;
                    }
                    ID2D1Geometry *geometry = charGeometryAt(index);
                    if (geometry == nullptr) {
                        return;
                    }
                    ID2D1Brush *brush = after ? afterDecor.Get() : beforeDecor.Get();
                    brush->SetOpacity(
                        globalOpacity * characterOpacityAt(index)
                    );
                    context->DrawGeometry(geometry, brush, sourceWidth);
                };
                const auto pushGlowClip = [&](std::size_t index, bool after) {
                    float edge = delegatedWipeCoordinateAt(line->chars, index);
                    D2D1_RECT_F bounds = line->bounds;
                    if (useUtopiaTransition) {
                        const auto animated = utopiaCharWipe(index);
                        bounds = animated.first;
                        edge = animated.second;
                    }
                    const float pad = sourceWidth + 4.0f;
                    pushAxisAlignedClip(
                        directionalWipeClip(bounds, edge, pad, after),
                        D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                };
                const auto drawGlowPhase = [&](std::size_t index, N3WipePhase phase) {
                    if (phase != N3WipePhase::Wiping) {
                        const bool after = phase == N3WipePhase::After;
                        if (stateOnly != nullptr && *stateOnly != after) {
                            return;
                        }
                        drawGlowPart(index, after);
                        return;
                    }
                    if (stateOnly == nullptr) {
                        pushGlowClip(index, false);
                        drawGlowPart(index, false);
                        context->PopAxisAlignedClip();
                        pushGlowClip(index, true);
                        drawGlowPart(index, true);
                        context->PopAxisAlignedClip();
                        return;
                    }
                    pushGlowClip(index, *stateOnly);
                    drawGlowPart(index, *stateOnly);
                    context->PopAxisAlignedClip();
                };
                for (std::size_t reverse = line->chars.size(); reverse > 0; --reverse) {
                    const std::size_t index = reverse - 1;
                    if (wipePhaseAt(line->chars, index) == N3WipePhase::Before) {
                        drawGlowPhase(index, N3WipePhase::Before);
                    }
                }
                for (std::size_t index = 0; index < line->chars.size(); ++index) {
                    if (wipePhaseAt(line->chars, index) == N3WipePhase::After) {
                        drawGlowPhase(index, N3WipePhase::After);
                    }
                }
                for (std::size_t index = 0; index < line->chars.size(); ++index) {
                    if (wipePhaseAt(line->chars, index) == N3WipePhase::Wiping) {
                        drawGlowPhase(index, N3WipePhase::Wiping);
                    }
                }
                context->PopAxisAlignedClip();
                endDrawMeasured(
                    "ID2D1DeviceContext::EndDraw(glow source)",
                    frameDiagnostics.endDrawGlowSourceMs,
                    frameDiagnostics.endDrawGlowSourceCount
                );
                layer.blur->SetInput(0, layer.source);

                // N3 DrawOneLineDecorBlurMulti: N = BlurLevel + 1 and
                // sigma_i = R - floor(i * R / N). Equal radii keep the exact
                // combined source; split states ladder from their own radius.
                const int passes = std::clamp(style.glowConcentrationLevel, 0, 2) + 1;
                for (int index = 0; index < passes; ++index) {
                    layer.sigmas.push_back(radius - index * radius / passes);
                }
                mainGlowLayers.push_back(std::move(layer));
            }
        }

        struct RubyGlowLayer {
            ID2D1Bitmap1 *source = nullptr;
            ID2D1Effect *blur = nullptr;
            std::vector<int> sigmas;
            D2D1_MATRIX_3X2_F transform = D2D1::Matrix3x2F::Identity();
            bool hasTransform = false;
            D2D1_RECT_F sourceRect{};
            D2D1_RECT_F effectRect{};
        };
        std::vector<RubyGlowLayer> rubyGlowLayers;
        // Grouped layers (rubyOnly < 0) collect every unit whose animation is
        // identity this frame into one source per ruby style and wipe colour.
        // Units animating this frame keep the Painter blur-then-transform
        // semantics through dedicated per-ruby/per-unit layers.
        auto appendRubyGlowLayer = [&](int styleIndex, bool after,
                                       int rubyOnly, int unitOnly) {
            const TextStyle &rubyStyle = rubyStyleFor(styleIndex);
            const float requestedRadius = after
                ? rubyStyle.rubyGlowAfterRadius
                : rubyStyle.rubyGlowBeforeRadius;
            const int radius = std::max(
                0, static_cast<int>(std::lround(requestedRadius))
            );
            const bool hasVisibleSource = std::any_of(
                line->rubies.begin(), line->rubies.end(),
                [&](const Impl::CachedRuby &ruby) {
                    const int rubyIndex = static_cast<int>(&ruby - line->rubies.data());
                    const bool selected = (rubyOnly < 0 || rubyIndex == rubyOnly);
                    if (!selected || ruby.styleIndex != styleIndex) {
                        return false;
                    }
                    for (std::size_t unitIndex = 0;
                         unitIndex < ruby.geometries.size(); ++unitIndex) {
                        if ((unitOnly >= 0
                                && static_cast<int>(unitIndex) != unitOnly)
                            || (rubyOnly < 0
                                && !rubyUnitUsesGroupedGlowAt(ruby, unitIndex))
                            || rubyUnitOpacityAt(ruby, unitIndex) <= 0.0f) {
                            continue;
                        }
                        const N3WipePhase phase = useUtopiaTransition
                            ? rubyUnitWipePhaseAt(ruby, unitIndex)
                            : rubyWipePhaseAt(ruby);
                        if (after
                                ? phase != N3WipePhase::Before
                                : phase != N3WipePhase::After) {
                            return true;
                        }
                    }
                    return false;
                }
            );
            if (rubyStyle.rubyDecorationKind != "glow"
                || radius <= 0
                || !hasVisibleSource) {
                return;
            }
            RubyGlowLayer layer;
            if ((spinDirection != 0 || dripDirection != 0 || useUtopiaTransition)
                && rubyOnly >= 0) {
                const Impl::CachedRuby &ruby = line->rubies[
                    static_cast<std::size_t>(rubyOnly)
                ];
                const CharacterAnimationState animationState = unitOnly >= 0
                    ? rubyUnitAnimationAt(ruby, static_cast<std::size_t>(unitOnly))
                    : rubyUnitAnimationAt(ruby, 0);
                layer.transform = D2D1::Matrix3x2F::Translation(-dx, -dy)
                    * animationState.matrix
                    * D2D1::Matrix3x2F::Translation(dx, dy);
                layer.hasTransform = animationState.transformed;
            }
            const float sourceWidth = std::max(0.0f, rubyStyle.rubyStrokeWidth)
                + (rubyStyle.rubyStroke2Width > 0.0f
                    ? rubyStyle.rubyStroke2Width
                    : 0.0f)
                + static_cast<float>(radius);
            const float pad = sourceWidth * 0.5f + radius * 3.0f + 2.0f;
            bool hasContent = false;
            D2D1_RECT_F content{};
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                if ((rubyOnly >= 0 && static_cast<int>(rubyIndex) != rubyOnly)
                    || ruby.styleIndex != styleIndex) {
                    continue;
                }
                content = hasContent
                    ? unionRect(content, ruby.bounds)
                    : ruby.bounds;
                hasContent = true;
            }
            if (!hasContent) {
                return;
            }
            layer.sourceRect = glowOutputRect(
                content, sourceWidth, radius
            );
            count(
                frameDiagnostics.glowSourceAreaPx,
                rectAreaPx(layer.sourceRect)
            );
            const D2D1_RECT_F clearRect = glowClearBounds(
                layer.sourceRect, radius
            );
            layer.source = acquireGlowScratch(
                clearRect.right - clearRect.left,
                clearRect.bottom - clearRect.top
            );
            layer.blur = acquireGlowEffect();
            layer.effectRect = impl_->glowDirtyRectEnabled
                ? D2D1::RectF(
                    layer.sourceRect.left - clearRect.left,
                    layer.sourceRect.top - clearRect.top,
                    layer.sourceRect.right - clearRect.left,
                    layer.sourceRect.bottom - clearRect.top
                )
                : D2D1::RectF(
                    layer.sourceRect.left + dx,
                    layer.sourceRect.top + dy,
                    layer.sourceRect.right + dx,
                    layer.sourceRect.bottom + dy
                );
            context->SetTarget(layer.source);
            context->SetTransform(
                impl_->glowDirtyRectEnabled
                    ? D2D1::Matrix3x2F::Translation(
                        -clearRect.left, -clearRect.top
                    )
                    : D2D1::Matrix3x2F::Translation(dx, dy)
            );
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            pushAxisAlignedClip(
                clearRect, D2D1_ANTIALIAS_MODE_ALIASED
            );
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                if ((rubyOnly >= 0 && static_cast<int>(rubyIndex) != rubyOnly)
                    || ruby.styleIndex != styleIndex) {
                    continue;
                }
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                    after
                        ? rubyStyle.rubyAfterDecorPaint
                        : rubyStyle.rubyBeforeDecorPaint,
                    rubyPaintBounds(
                        after
                            ? rubyStyle.rubyAfterDecorPaint
                            : rubyStyle.rubyBeforeDecorPaint,
                        ruby.fillBounds,
                        ruby.horizontalFillBounds
                    ),
                    after ? rubyStyle.rubyAfterDecor : rubyStyle.rubyBeforeDecor
                );
                for (std::size_t geometryIndex = 0;
                     geometryIndex < ruby.geometries.size(); ++geometryIndex) {
                    if ((unitOnly >= 0
                            && static_cast<int>(geometryIndex) != unitOnly)
                        || (rubyOnly < 0
                            && !rubyUnitUsesGroupedGlowAt(
                                ruby, geometryIndex
                            ))) {
                        continue;
                    }
                    ID2D1Geometry *geometry = rubyOnly < 0
                        ? rubyGeometryAt(rubyIndex, geometryIndex)
                        : ruby.geometries[geometryIndex].Get();
                    if (geometry == nullptr) {
                        continue;
                    }
                    D2D1_RECT_F phaseBounds = ruby.bounds;
                    float edge = rubyWipeEdgeAt(ruby);
                    const N3WipePhase phase = useUtopiaTransition
                        ? rubyUnitWipePhaseAt(ruby, geometryIndex)
                        : rubyWipePhaseAt(ruby);
                    if ((phase == N3WipePhase::Before && after)
                        || (phase == N3WipePhase::After && !after)) {
                        continue;
                    }
                    if (useUtopiaTransition) {
                        const auto animated = utopiaRubyUnitWipe(
                            ruby, rubyIndex, geometryIndex, rubyStyle
                        );
                        phaseBounds = animated.first;
                        edge = animated.second;
                    }
                    const D2D1_RECT_F clip = directionalWipeClip(
                        phaseBounds, edge, pad, after
                    );
                    if (phase == N3WipePhase::Wiping) {
                        pushAxisAlignedClip(
                            clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                        );
                    }
                    brush->SetOpacity(
                        globalOpacity * rubyUnitOpacityAt(
                            ruby, geometryIndex
                        )
                    );
                    context->DrawGeometry(
                        geometry, brush.Get(), sourceWidth
                    );
                    if (phase == N3WipePhase::Wiping) {
                        context->PopAxisAlignedClip();
                    }
                }
            }
            context->PopAxisAlignedClip();
            endDrawMeasured(
                "ID2D1DeviceContext::EndDraw(ruby glow source)",
                frameDiagnostics.endDrawRubyGlowSourceMs,
                frameDiagnostics.endDrawRubyGlowSourceCount
            );
            layer.blur->SetInput(0, layer.source);
            const int passes = std::clamp(
                rubyStyle.rubyGlowConcentrationLevel, 0, 2
            ) + 1;
            for (int index = 0; index < passes; ++index) {
                layer.sigmas.push_back(radius - index * radius / passes);
            }
            rubyGlowLayers.push_back(std::move(layer));
        };
        std::vector<int> rubyStyleIndices;
        for (const Impl::CachedRuby &ruby : line->rubies) {
            if (std::find(
                    rubyStyleIndices.begin(), rubyStyleIndices.end(), ruby.styleIndex
                ) == rubyStyleIndices.end()) {
                rubyStyleIndices.push_back(ruby.styleIndex);
            }
        }
        for (int styleIndex : rubyStyleIndices) {
            appendRubyGlowLayer(styleIndex, false, -1, -1);
            appendRubyGlowLayer(styleIndex, true, -1, -1);
        }
        if (useUtopiaTransition || dripDirection != 0) {
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                for (std::size_t unitIndex = 0;
                     unitIndex < ruby.geometries.size(); ++unitIndex) {
                    if (!rubyUnitTransformed(ruby, unitIndex)
                        || rubyUnitUsesGroupedGlowAt(ruby, unitIndex)) {
                        continue;
                    }
                    appendRubyGlowLayer(
                        ruby.styleIndex, false,
                        static_cast<int>(rubyIndex), static_cast<int>(unitIndex)
                    );
                    appendRubyGlowLayer(
                        ruby.styleIndex, true,
                        static_cast<int>(rubyIndex), static_cast<int>(unitIndex)
                    );
                }
            }
        } else if (spinDirection != 0) {
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                if (rubyFadeOpacityAt(ruby) >= 1.0f
                    || rubyFadeOpacityAt(ruby) <= 0.0f) {
                    continue;
                }
                appendRubyGlowLayer(
                    ruby.styleIndex, false, static_cast<int>(rubyIndex), -1
                );
                appendRubyGlowLayer(
                    ruby.styleIndex, true, static_cast<int>(rubyIndex), -1
                );
            }
        }

        struct InlineGlowLayer {
            ID2D1Bitmap1 *source = nullptr;
            ID2D1Effect *blur = nullptr;
            std::vector<int> sigmas;
            D2D1_MATRIX_3X2_F transform = D2D1::Matrix3x2F::Identity();
            bool hasTransform = false;
            D2D1_RECT_F sourceRect{};
            D2D1_RECT_F effectRect{};
        };
        std::vector<InlineGlowLayer> inlineGlowLayers;
        // Grouped layers (charOnly < 0) collect every character whose
        // animation is identity this frame into one source per inline style
        // and wipe colour. Characters animating this frame keep the Painter
        // blur-then-transform semantics through per-character layers.
        auto appendInlineGlowLayer = [&](int styleIndex, bool after, int charOnly) {
            const TextStyle &charStyle = styleIndex >= 0
                && styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(styleIndex)]
                : style;
            const int radius = std::max(
                0,
                static_cast<int>(std::lround(
                    after ? charStyle.glowAfterRadius : charStyle.glowBeforeRadius
                ))
            );
            // Source visibility follows the per-character wipe phase, like the
            // body path. The retired reach test compared the line-wide
            // wipeEdge against ch.left/right, but a character's wipe-left
            // (ink minus primary edge / 2) lies left of line->bounds.left, so
            // the first character never counted as unreached and its
            // after-glow source leaked a blurred sliver through the whole
            // lead-in (utopia was clean because its branch already used the
            // phase check below).
            const bool hasVisibleSource = std::any_of(
                line->chars.begin(), line->chars.end(),
                [&](const Impl::CachedChar &ch) {
                    const std::size_t charIndex = static_cast<std::size_t>(
                        &ch - line->chars.data()
                    );
                    if ((charOnly >= 0
                            && static_cast<int>(charIndex) != charOnly)
                        || ch.styleIndex != styleIndex) {
                        return false;
                    }
                    if (charOnly >= 0) {
                        if (!ch.geometry) {
                            return false;
                        }
                        const N3WipePhase phase = wipePhaseAt(
                            line->chars, charIndex
                        );
                        return after
                            ? phase != N3WipePhase::Before
                            : phase != N3WipePhase::After;
                    }
                    if (!charUsesGroupedGlowAt(charIndex)
                        || charGeometryAt(charIndex) == nullptr) {
                        return false;
                    }
                    const N3WipePhase phase = wipePhaseAt(
                        line->chars, charIndex
                    );
                    return after
                        ? phase != N3WipePhase::Before
                        : phase != N3WipePhase::After;
                }
            );
            if (charStyle.decorationKind != "glow" || radius <= 0 || !hasVisibleSource) {
                return;
            }
            InlineGlowLayer layer;
            if ((spinDirection != 0 || dripDirection != 0 || useUtopiaTransition)
                && charOnly >= 0) {
                const CharacterAnimationState animationState = characterAnimationAt(
                    static_cast<std::size_t>(charOnly)
                );
                layer.transform = D2D1::Matrix3x2F::Translation(-dx, -dy)
                    * animationState.matrix
                    * D2D1::Matrix3x2F::Translation(dx, dy);
                layer.hasTransform = animationState.transformed;
            }
            Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                after ? charStyle.afterDecorPaint : charStyle.beforeDecorPaint,
                mainPaintBounds(
                    after ? charStyle.afterDecorPaint : charStyle.beforeDecorPaint,
                    styleIndex
                ),
                after ? charStyle.afterDecor : charStyle.beforeDecor
            );
            const float sourceWidth = std::max(charStyle.strokeWidth, 0.0f)
                + std::max(charStyle.stroke2Width, 0.0f)
                + static_cast<float>(radius);
            const float pad = sourceWidth * 0.5f + radius * 3.0f + 2.0f;
            bool hasContent = !impl_->glowDirtyRectEnabled;
            D2D1_RECT_F content = unionRect(line->bounds, line->fillBounds);
            if (impl_->glowDirtyRectEnabled) {
                for (std::size_t charIndex = 0;
                     charIndex < line->chars.size(); ++charIndex) {
                    const Impl::CachedChar &ch = line->chars[charIndex];
                    if ((charOnly >= 0
                            && static_cast<int>(charIndex) != charOnly)
                        || ch.styleIndex != styleIndex
                        || characterOpacityAt(charIndex) <= 0.0f) {
                        continue;
                    }
                    bool visible = false;
                    if (charOnly >= 0) {
                        if (ch.geometry != nullptr) {
                            const N3WipePhase phase = wipePhaseAt(
                                line->chars, charIndex
                            );
                            visible = after
                                ? phase != N3WipePhase::Before
                                : phase != N3WipePhase::After;
                        }
                    } else if (charUsesGroupedGlowAt(charIndex)
                               && charGeometryAt(charIndex) != nullptr) {
                        const N3WipePhase phase = wipePhaseAt(
                            line->chars, charIndex
                        );
                        visible = after
                            ? phase != N3WipePhase::Before
                            : phase != N3WipePhase::After;
                    }
                    if (!visible) {
                        continue;
                    }
                    const D2D1_RECT_F charBounds = D2D1::RectF(
                        ch.left, ch.top, ch.right, ch.bottom
                    );
                    content = hasContent
                        ? unionRect(content, charBounds)
                        : charBounds;
                    hasContent = true;
                }
            }
            if (!hasContent) {
                return;
            }
            layer.sourceRect = glowOutputRect(
                content, sourceWidth, radius
            );
            count(
                frameDiagnostics.glowSourceAreaPx,
                rectAreaPx(layer.sourceRect)
            );
            const D2D1_RECT_F clearRect = glowClearBounds(
                layer.sourceRect, radius
            );
            layer.source = acquireGlowScratch(
                clearRect.right - clearRect.left,
                clearRect.bottom - clearRect.top
            );
            layer.blur = acquireGlowEffect();
            layer.effectRect = impl_->glowDirtyRectEnabled
                ? D2D1::RectF(
                    layer.sourceRect.left - clearRect.left,
                    layer.sourceRect.top - clearRect.top,
                    layer.sourceRect.right - clearRect.left,
                    layer.sourceRect.bottom - clearRect.top
                )
                : D2D1::RectF(
                    layer.sourceRect.left + dx,
                    layer.sourceRect.top + dy,
                    layer.sourceRect.right + dx,
                    layer.sourceRect.bottom + dy
                );
            context->SetTarget(layer.source);
            context->SetTransform(
                impl_->glowDirtyRectEnabled
                    ? D2D1::Matrix3x2F::Translation(
                        -clearRect.left, -clearRect.top
                    )
                    : D2D1::Matrix3x2F::Translation(dx, dy)
            );
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            pushAxisAlignedClip(
                clearRect, D2D1_ANTIALIAS_MODE_ALIASED
            );
            for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
                const Impl::CachedChar &ch = line->chars[charIndex];
                if ((charOnly >= 0 && static_cast<int>(charIndex) != charOnly)
                    || ch.styleIndex != styleIndex) {
                    continue;
                }
                if (charOnly >= 0) {
                    // Per-character layer: draw the upright cached glyph and
                    // apply the animation matrix to the blurred result.
                    const N3WipePhase phase = wipePhaseAt(line->chars, charIndex);
                    if (ch.geometry == nullptr
                        || (phase == N3WipePhase::Before && after)
                        || (phase == N3WipePhase::After && !after)) {
                        continue;
                    }
                    brush->SetOpacity(
                        globalOpacity * characterOpacityAt(charIndex)
                    );
                    D2D1_RECT_F clip{};
                    bool needClip = false;
                    if (phase == N3WipePhase::Wiping) {
                        // Clip by this character's phase, not the line-wide
                        // front: before the first character starts that front
                        // rests at line->bounds.left (bare ink union), which
                        // sits right of the first glyph's wipe-left and glow
                        // stroke ring, so clipping a Before-phase glyph there
                        // cuts the left half of its before-glow.
                        if (useUtopiaTransition) {
                            const auto [animatedBounds, animatedEdge] =
                                utopiaCharWipe(charIndex);
                            clip = directionalWipeClip(
                                animatedBounds, animatedEdge, pad, after
                            );
                        } else {
                            clip = directionalWipeClip(
                                line->bounds,
                                delegatedWipeCoordinateAt(line->chars, charIndex),
                                pad,
                                after
                            );
                        }
                        needClip = true;
                    }
                    if (needClip) {
                        pushAxisAlignedClip(
                            clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                        );
                    }
                    context->DrawGeometry(
                        ch.geometry.Get(), brush.Get(), sourceWidth
                    );
                    if (needClip) {
                        context->PopAxisAlignedClip();
                    }
                    continue;
                }
                if (!charUsesGroupedGlowAt(charIndex)) {
                    continue;
                }
                ID2D1Geometry *geometry = charGeometryAt(charIndex);
                if (geometry == nullptr) {
                    continue;
                }
                const N3WipePhase phase = wipePhaseAt(line->chars, charIndex);
                if ((phase == N3WipePhase::Before && after)
                    || (phase == N3WipePhase::After && !after)) {
                    continue;
                }
                // Same phase-based rule as the per-character branch and the
                // grouped main path's pushGlowClip: only a Wiping glyph splits
                // its glow source at the (delegated) front; Before/After-phase
                // glyphs keep their full halo.
                const bool needClip = phase == N3WipePhase::Wiping;
                brush->SetOpacity(
                    globalOpacity * characterOpacityAt(charIndex)
                );
                if (needClip) {
                    D2D1_RECT_F clip{};
                    if (useUtopiaTransition) {
                        const auto [animatedBounds, animatedEdge] =
                            utopiaCharWipe(charIndex);
                        clip = directionalWipeClip(
                            animatedBounds, animatedEdge, pad, after
                        );
                    } else {
                        clip = directionalWipeClip(
                            line->bounds,
                            delegatedWipeCoordinateAt(line->chars, charIndex),
                            pad,
                            after
                        );
                    }
                    pushAxisAlignedClip(
                        clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                context->DrawGeometry(geometry, brush.Get(), sourceWidth);
                if (needClip) {
                    context->PopAxisAlignedClip();
                }
            }
            context->PopAxisAlignedClip();
            endDrawMeasured(
                "ID2D1DeviceContext::EndDraw(inline glow source)",
                frameDiagnostics.endDrawInlineGlowSourceMs,
                frameDiagnostics.endDrawInlineGlowSourceCount
            );
            layer.blur->SetInput(0, layer.source);
            const int passes = std::clamp(charStyle.glowConcentrationLevel, 0, 2) + 1;
            for (int index = 0; index < passes; ++index) {
                layer.sigmas.push_back(radius - index * radius / passes);
            }
            inlineGlowLayers.push_back(std::move(layer));
        };
        if (line->hasInlineStyles) {
            std::vector<int> styleIndices;
            for (const Impl::CachedChar &ch : line->chars) {
                if (std::find(styleIndices.begin(), styleIndices.end(), ch.styleIndex)
                    == styleIndices.end()) {
                    styleIndices.push_back(ch.styleIndex);
                }
            }
            for (int styleIndex : styleIndices) {
                appendInlineGlowLayer(styleIndex, false, -1);
                appendInlineGlowLayer(styleIndex, true, -1);
            }
        }
        if (spinDirection != 0 || dripDirection != 0 || useUtopiaTransition) {
            for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
                if (!charTransformedAt(charIndex)
                    || charUsesGroupedGlowAt(charIndex)) {
                    continue;
                }
                const int styleIndex = line->chars[charIndex].styleIndex;
                appendInlineGlowLayer(styleIndex, false, static_cast<int>(charIndex));
                appendInlineGlowLayer(styleIndex, true, static_cast<int>(charIndex));
            }
        }

        // Karaoke scan-line (front sweep) highlight. Legacy glow-layer plumbing
        // remains below for structural locality, but scanlineRadius is fixed to
        // zero: current rendering uses inner feather slices only.
        struct ScanlineGlowLayer {
            ID2D1Bitmap1 *source = nullptr;
            ID2D1Effect *blur = nullptr;
            std::vector<int> sigmas;
            D2D1_RECT_F sourceRect{};
            D2D1_RECT_F effectRect{};
        };
        const bool scanlineActive = line->scanlineEnabled
            && !noWipe
            && !mainWipeComplete
            && hasAfterWipe;
        // 底色发光保留 before/after 两侧各自的色相和饱和度，只提高 HSV
        // 的 V；单独颜色模式仍使用用户指定的统一颜色。
        const bool scanlineBrighten = style.scanlineMode == "brighten";
        const float scanlineAlpha = scanlineBrighten
            ? (style.scanlineBrightness > 0.0f ? 1.0f : 0.0f)
            : 1.0f;
        const RgbaColor scanlineColorValue = style.scanlineColor;
        const float scanlineBrightness = std::clamp(
            style.scanlineBrightness, 0.0f, 1.0f
        );
        const float scanlineHalfWidth = std::max(style.scanlineWidth, 1.0f) * 0.5f;
        const auto rectsOverlap = [](const D2D1_RECT_F &a, const D2D1_RECT_F &b) {
            return a.left < b.right && b.left < a.right
                && a.top < b.bottom && b.top < a.bottom;
        };
        const auto scanlineBandRect = [&](const D2D1_RECT_F &bounds, float edge) {
            return style.vertical
                ? D2D1::RectF(
                    bounds.left, edge - scanlineHalfWidth,
                    bounds.right, edge + scanlineHalfWidth
                )
                : D2D1::RectF(
                    edge - scanlineHalfWidth, fullWipeClipTop,
                    edge + scanlineHalfWidth, fullWipeClipBottom
                );
        };
        const auto scanlineFeatherSlices = [&](float edge) {
            std::vector<std::pair<D2D1_RECT_F, float>> slices;
            const float softness = std::min(
                std::max(style.scanlineGlowRadius, 0.0f), scanlineHalfWidth
            );
            const float core = scanlineHalfWidth - softness;
            const int count = std::clamp(
                static_cast<int>(std::lround(scanlineHalfWidth * 2.0f)), 1, 64
            );
            const float step = scanlineHalfWidth * 2.0f / static_cast<float>(count);
            slices.reserve(static_cast<std::size_t>(count));
            for (int index = 0; index < count; ++index) {
                const float offset = -scanlineHalfWidth
                    + static_cast<float>(index) * step;
                const float distance = std::abs(offset + step * 0.5f);
                float alpha = 1.0f;
                if (softness > 0.0f && distance > core) {
                    const float progress = std::clamp(
                        (scanlineHalfWidth - distance) / softness, 0.0f, 1.0f
                    );
                    alpha = progress * progress * (3.0f - 2.0f * progress);
                }
                if (alpha <= 0.0f) {
                    continue;
                }
                const D2D1_RECT_F rect = style.vertical
                    ? D2D1::RectF(
                        line->bounds.left, edge + offset,
                        line->bounds.right, edge + offset + step
                    )
                    : D2D1::RectF(
                        edge + offset, fullWipeClipTop,
                        edge + offset + step, fullWipeClipBottom
                    );
                slices.emplace_back(rect, alpha);
            }
            return slices;
        };
        // One band target per character: utopia follows the transformed wipe
        // edge of the wiping glyph, the plain wipe uses the shared line front.
        //  Returns nullopt when the char carries no band this frame: bitmap
        // guides are skipped like the body path, and only a character whose
        // own wipe window contains tMs (closed, so the hand-off arrival frame
        // keeps its band) carries it — a front resting inside a timing gap
        // must not park the highlight on the next (unsung) character's rim.
        // This matches the Painter's ``main_scanline_front`` and the per-unit
        // ruby fragment gate.
        const auto scanlineCharBand = [&](std::size_t charIndex)
            -> std::optional<std::pair<D2D1_RECT_F, float>> {
            const Impl::CachedChar &ch = line->chars[charIndex];
            if (ch.bitmapGuide.has_value()) {
                return std::nullopt;
            }
            const int start = wipeStartMs(ch);
            const int end = wipeEndMs(ch);
            if (!(start != end && start <= tMs && tMs <= end)) {
                return std::nullopt;
            }
            if (useUtopiaTransition) {
                const auto animated = utopiaCharWipe(charIndex);
                return std::make_pair(
                    scanlineBandRect(animated.first, animated.second),
                    animated.second
                );
            }
            return std::make_pair(
                scanlineBandRect(
                    D2D1::RectF(ch.left, ch.top, ch.right, ch.bottom), wipeEdge
                ),
                wipeEdge
            );
        };
        const auto scanlinePaintFor = [&](const PaintStyle &source) {
            return scanlineBrighten
                ? brightenPaintHsvValue(source, scanlineBrightness)
                : solidPaint(scanlineColorValue);
        };
        const auto scanlineColorFor = [&](const RgbaColor &source) {
            return scanlineBrighten
                ? brightenHsvValue(source, scanlineBrightness)
                : scanlineColorValue;
        };
        const auto pushScanlineStateClip = [&](float edge, bool after) {
            pushAxisAlignedClip(
                directionalWipeClip(line->bounds, edge, geometryPad, after),
                D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
            );
        };
        ScanlineGlowLayer scanlineMainGlow;
        ScanlineGlowLayer scanlineRubyGlow;
        // Scan-line softness is an opacity falloff strictly inside glyph
        // geometry.  Do not build the ordinary outward glow source: it leaks
        // into transparent counters and gaps between neighbouring glyphs.
        const int scanlineRadius = 0;
        auto prepareScanlineGlowLayer = [&](ScanlineGlowLayer &layer,
                                            const D2D1_RECT_F &content,
                                            float stroke, float stroke2,
                                            auto &&drawSource) {
            const float sourceWidth = std::max(stroke, 0.0f)
                + std::max(stroke2, 0.0f)
                + static_cast<float>(scanlineRadius);
            layer.sourceRect = glowOutputRect(content, sourceWidth, scanlineRadius);
            count(
                frameDiagnostics.glowSourceAreaPx,
                rectAreaPx(layer.sourceRect)
            );
            const D2D1_RECT_F clearRect = glowClearBounds(
                layer.sourceRect, scanlineRadius
            );
            layer.source = acquireGlowScratch(
                clearRect.right - clearRect.left,
                clearRect.bottom - clearRect.top
            );
            layer.blur = acquireGlowEffect();
            layer.effectRect = impl_->glowDirtyRectEnabled
                ? D2D1::RectF(
                    layer.sourceRect.left - clearRect.left,
                    layer.sourceRect.top - clearRect.top,
                    layer.sourceRect.right - clearRect.left,
                    layer.sourceRect.bottom - clearRect.top
                )
                : D2D1::RectF(
                    layer.sourceRect.left + dx,
                    layer.sourceRect.top + dy,
                    layer.sourceRect.right + dx,
                    layer.sourceRect.bottom + dy
                );
            context->SetTarget(layer.source);
            context->SetTransform(
                impl_->glowDirtyRectEnabled
                    ? D2D1::Matrix3x2F::Translation(
                        -clearRect.left, -clearRect.top
                    )
                    : D2D1::Matrix3x2F::Translation(dx, dy)
            );
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            pushAxisAlignedClip(clearRect, D2D1_ANTIALIAS_MODE_ALIASED);
            drawSource();
            context->PopAxisAlignedClip();
            endDrawMeasured(
                "ID2D1DeviceContext::EndDraw(scanline glow source)",
                frameDiagnostics.endDrawInlineGlowSourceMs,
                frameDiagnostics.endDrawInlineGlowSourceCount
            );
            layer.blur->SetInput(0, layer.source);
            layer.sigmas.push_back(scanlineRadius);
        };
        auto drawScanlineGlowLayer = [&](ScanlineGlowLayer &layer) {
            context->SetTransform(lineViewportTransform);
            const D2D1_RECT_F imageRect = D2D1::RectF(
                layer.sourceRect.left + dx, layer.sourceRect.top + dy,
                layer.sourceRect.right + dx, layer.sourceRect.bottom + dy
            );
            for (int sigma : layer.sigmas) {
                checkHr(
                    layer.blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(scanline StandardDeviation)",
                    device_
                );
                context->DrawImage(
                    layer.blur,
                    D2D1::Point2F(imageRect.left, imageRect.top),
                    layer.effectRect
                );
            }
        };
        if (scanlineActive && scanlineRadius > 0 && scanlineAlpha > 0.0f) {
            const float mainGlowStroke = std::max(style.strokeWidth, 0.0f)
                + std::max(style.stroke2Width, 0.0f)
                + static_cast<float>(scanlineRadius);
            const auto charContentRect = [&](std::size_t charIndex) {
                const Impl::CachedChar &ch = line->chars[charIndex];
                return expandedRect(
                    D2D1::RectF(ch.left, ch.top, ch.right, ch.bottom),
                    mainGlowStroke
                );
            };
            bool hasMainContent = false;
            D2D1_RECT_F mainContent{};
            for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
                const auto band = scanlineCharBand(charIndex);
                if (!band.has_value()) {
                    continue;
                }
                const D2D1_RECT_F content = charContentRect(charIndex);
                if (!rectsOverlap(band->first, content)) {
                    continue;
                }
                mainContent = hasMainContent
                    ? unionRect(mainContent, content)
                    : content;
                hasMainContent = true;
            }
            if (hasMainContent) {
                prepareScanlineGlowLayer(
                    scanlineMainGlow, mainContent,
                    style.strokeWidth, style.stroke2Width,
                    [&]() {
                        for (std::size_t charIndex = 0;
                             charIndex < line->chars.size(); ++charIndex) {
                            const auto band = scanlineCharBand(charIndex);
                            ID2D1Geometry *geometry = charGeometryAt(charIndex);
                            if (!band.has_value() || geometry == nullptr) {
                                continue;
                            }
                            const Impl::CachedChar &ch = line->chars[charIndex];
                            const TextStyle &charStyle = ch.styleIndex >= 0
                                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                                : style;
                            for (bool after : {false, true}) {
                                const PaintStyle sourcePaint = after
                                    ? charStyle.afterFillPaint
                                    : charStyle.beforeFillPaint;
                                const RgbaColor sourceColor = after
                                    ? charStyle.afterFill
                                    : charStyle.beforeFill;
                                const PaintStyle paint = scanlinePaintFor(sourcePaint);
                                const RgbaColor color = scanlineColorFor(sourceColor);
                                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                                    paint, mainPaintBounds(paint, ch.styleIndex), color
                                );
                                brush->SetOpacity(
                                    globalOpacity * characterOpacityAt(charIndex)
                                        * scanlineAlpha
                                );
                                pushAxisAlignedClip(
                                    band->first, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                                );
                                pushScanlineStateClip(band->second, after);
                                context->DrawGeometry(
                                    geometry, brush.Get(), mainGlowStroke
                                );
                                context->PopAxisAlignedClip();
                                context->PopAxisAlignedClip();
                            }
                        }
                    }
                );
            }
            bool hasRubyContent = false;
            D2D1_RECT_F rubyContent{};
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                if (useUtopiaTransition) {
                    for (std::size_t unitIndex = 0;
                         unitIndex < ruby.geometries.size(); ++unitIndex) {
                        if (rubyUnitWipePhaseAt(ruby, unitIndex)
                            != N3WipePhase::Wiping) {
                            continue;
                        }
                        const auto animated = utopiaRubyUnitWipe(
                            ruby, rubyIndex, unitIndex, rubyStyleFor(ruby.styleIndex)
                        );
                        rubyContent = hasRubyContent
                            ? unionRect(rubyContent, animated.first)
                            : animated.first;
                        hasRubyContent = true;
                    }
                } else if (rubyWipePhaseAt(ruby) == N3WipePhase::Wiping) {
                    rubyContent = hasRubyContent
                        ? unionRect(rubyContent, ruby.bounds)
                        : ruby.bounds;
                    hasRubyContent = true;
                }
            }
            if (hasRubyContent) {
                prepareScanlineGlowLayer(
                    scanlineRubyGlow, rubyContent,
                    style.rubyStrokeWidth, style.rubyStroke2Width,
                    [&]() {
                        for (std::size_t rubyIndex = 0;
                             rubyIndex < line->rubies.size(); ++rubyIndex) {
                            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                            const TextStyle &rubyStyle = rubyStyleFor(
                                ruby.styleIndex
                            );
                            const float rubyGlowStroke = std::max(
                                rubyStyle.rubyStrokeWidth, 0.0f
                            ) + std::max(rubyStyle.rubyStroke2Width, 0.0f)
                                + static_cast<float>(scanlineRadius);
                            for (std::size_t unitIndex = 0;
                                 unitIndex < ruby.geometries.size(); ++unitIndex) {
                                ID2D1Geometry *geometry = rubyGeometryAt(
                                    rubyIndex, unitIndex
                                );
                                D2D1_RECT_F band{};
                                if (geometry == nullptr) {
                                    continue;
                                }
                                if (useUtopiaTransition) {
                                    if (rubyUnitWipePhaseAt(ruby, unitIndex)
                                        != N3WipePhase::Wiping) {
                                        continue;
                                    }
                                    const auto animated = utopiaRubyUnitWipe(
                                        ruby, rubyIndex, unitIndex, rubyStyle
                                    );
                                    band = scanlineBandRect(
                                        animated.first, animated.second
                                    );
                                } else {
                                    if (rubyWipePhaseAt(ruby)
                                        != N3WipePhase::Wiping) {
                                        continue;
                                    }
                                    band = scanlineBandRect(
                                        ruby.bounds, rubyWipeEdgeAt(ruby)
                                    );
                                }
                                const float edge = style.vertical
                                    ? (band.top + band.bottom) * 0.5f
                                    : (band.left + band.right) * 0.5f;
                                for (bool after : {false, true}) {
                                    const PaintStyle sourcePaint = after
                                        ? rubyStyle.rubyAfterFillPaint
                                        : rubyStyle.rubyBeforeFillPaint;
                                    const RgbaColor sourceColor = after
                                        ? rubyStyle.rubyAfterFill
                                        : rubyStyle.rubyBeforeFill;
                                    const PaintStyle paint = scanlinePaintFor(sourcePaint);
                                    const RgbaColor color = scanlineColorFor(sourceColor);
                                    Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                                        paint, rubyPaintBounds(
                                            paint, ruby.fillBounds,
                                            ruby.horizontalFillBounds
                                        ), color
                                    );
                                    brush->SetOpacity(
                                        globalOpacity
                                            * rubyUnitOpacityAt(ruby, unitIndex)
                                            * scanlineAlpha
                                    );
                                    pushAxisAlignedClip(
                                        band, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                                    );
                                    pushScanlineStateClip(edge, after);
                                    context->DrawGeometry(
                                        geometry, brush.Get(), rubyGlowStroke
                                    );
                                    context->PopAxisAlignedClip();
                                    context->PopAxisAlignedClip();
                                }
                            }
                        }
                    }
                );
            }
        }
        frameDiagnostics.glowMs += elapsedMs(glowStart);

        // auto 档柱体发光源：每柱一枚（镜像 Painter 逐柱 paint_glow_path，
        // 覆盖/未覆盖柱各自的半径与配色）。柱动画不在源里，合成时套在
        // 模糊结果上（blur-then-transform）。源笔宽 = 描边(+二重)+半径，
        // padding 与 Painter glow_extent 同式（笔宽/2 + 3R + 2）。
        if (volumeAutoDecorated
            && signalState.visible
            && style.decorationKind == "glow"
            && signalState.opacity > 0.0f
            && style.volumeOpacity > 0.0f) {
            const float groupOpacityBase = std::clamp(
                style.volumeOpacity * signalState.opacity * lineAnimationOpacity,
                0.0f,
                1.0f
            );
            const float glowBase
                = volumeDecorStroke2Width > 0.0f
                ? volumeDecorStrokeWidth + volumeDecorStroke2Width
                : volumeDecorStrokeWidth;
            for (int index = 0; index < signalGeometry.count; ++index) {
                const bool covered = index <= signalState.activeIndex;
                const int radius = static_cast<int>(std::lround(
                    std::max(
                        0.0f,
                        (covered
                            ? style.glowAfterRadius
                            : style.glowBeforeRadius)
                            * volumeDecorScale
                    )
                ));
                if (radius <= 0) {
                    continue;
                }
                const BarAnimationState animation = barAnimationAt(
                    index, 0.0f, 0.0f
                );
                if (animation.opacity <= 0.0f) {
                    continue;
                }
                const float glowPen = std::max(
                    1.0f, glowBase + static_cast<float>(radius)
                );
                const float pad = std::ceil(
                    glowPen / 2.0f + static_cast<float>(radius) * 3.0f
                ) + 2.0f;
                const D2D1_RECT_F barRect = volumeBarRectAt(index);
                VolumeBarGlowLayer layer;
                layer.layerRect = D2D1::RectF(
                    barRect.left - pad,
                    barRect.top - pad,
                    barRect.right + pad,
                    barRect.bottom + pad
                );
                const float layerW = std::max(
                    layer.layerRect.right - layer.layerRect.left, 1.0f
                );
                const float layerH = std::max(
                    layer.layerRect.bottom - layer.layerRect.top, 1.0f
                );
                layer.source = acquireGlowScratch(layerW, layerH);
                layer.blur = acquireGlowEffect();
                const int passes
                    = std::clamp(style.glowConcentrationLevel, 0, 2) + 1;
                for (int pass = 0; pass < passes; ++pass) {
                    layer.sigmas.push_back(
                        radius - pass * radius / passes
                    );
                }
                context->SetTarget(layer.source);
                context->BeginDraw();
                context->Clear(D2D1::ColorF(0.0f, 0.0f));
                context->SetTransform(D2D1::Matrix3x2F::Translation(
                    -layer.layerRect.left, -layer.layerRect.top
                ));
                const PaintStyle &decorPaint = covered
                    ? style.afterDecorPaint
                    : style.beforeDecorPaint;
                const RgbaColor &decorColor = covered
                    ? style.afterDecor
                    : style.beforeDecor;
                D2D1_RECT_F groupRect = volumeBarRectAt(0);
                for (int other = 1; other < signalGeometry.count; ++other) {
                    const D2D1_RECT_F otherRect = volumeBarRectAt(other);
                    groupRect = D2D1::RectF(
                        std::min(groupRect.left, otherRect.left),
                        std::min(groupRect.top, otherRect.top),
                        std::max(groupRect.right, otherRect.right),
                        std::max(groupRect.bottom, otherRect.bottom)
                    );
                }
                Microsoft::WRL::ComPtr<ID2D1Brush> decorBrush = paintBrush(
                    decorPaint, groupRect, decorColor
                );
                decorBrush->SetOpacity(
                    groupOpacityBase * animation.opacity
                );
                context->DrawRoundedRectangle(
                    volumeBarRoundedRectAt(index),
                    decorBrush.Get(),
                    glowPen
                );
                checkHr(
                    context->EndDraw(),
                    "ID2D1DeviceContext::EndDraw(volume bar glow source)",
                    device_
                );
                layer.blur->SetInput(0, layer.source);
                layer.barIndex = index;
                volumeBarGlowLayers.push_back(std::move(layer));
            }
        }

        context->SetTarget(targetBitmap);
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        if (!renderedAnyLine) {
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
        }
        // Everything the line puts on the final target belongs inside the
        // opacity layer; Clear must stay outside it.
        if (lineOpacityLayer.prepared()) {
            count(frameDiagnostics.layerPush);
            lineOpacityLayer.push();
        }
        for (RubyGlowLayer &layer : rubyGlowLayers) {
            context->SetTransform(
                withViewport(
                    layer.hasTransform
                        ? layer.transform
                        : D2D1::Matrix3x2F::Identity()
                )
            );
            const D2D1_RECT_F imageRect = D2D1::RectF(
                layer.sourceRect.left + dx, layer.sourceRect.top + dy,
                layer.sourceRect.right + dx, layer.sourceRect.bottom + dy
            );
            for (int sigma : layer.sigmas) {
                checkHr(
                    layer.blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(ruby StandardDeviation)",
                    device_
                );
                context->DrawImage(
                    layer.blur,
                    D2D1::Point2F(imageRect.left, imageRect.top),
                    layer.effectRect
                );
            }
        }
        for (InlineGlowLayer &layer : inlineGlowLayers) {
            context->SetTransform(
                withViewport(
                    layer.hasTransform
                        ? layer.transform
                        : D2D1::Matrix3x2F::Identity()
                )
            );
            const D2D1_RECT_F imageRect = D2D1::RectF(
                layer.sourceRect.left + dx, layer.sourceRect.top + dy,
                layer.sourceRect.right + dx, layer.sourceRect.bottom + dy
            );
            for (int sigma : layer.sigmas) {
                checkHr(
                    layer.blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(inline StandardDeviation)",
                    device_
                );
                context->DrawImage(
                    layer.blur,
                    D2D1::Point2F(imageRect.left, imageRect.top),
                    layer.effectRect
                );
            }
        }
        for (MainGlowLayer &layer : mainGlowLayers) {
            const D2D1_RECT_F glowImageRect = D2D1::RectF(
                layer.sourceRect.left + dx, layer.sourceRect.top + dy,
                layer.sourceRect.right + dx, layer.sourceRect.bottom + dy
            );
            for (int sigma : layer.sigmas) {
                context->SetTransform(lineViewportTransform);
                checkHr(
                    layer.blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(StandardDeviation)",
                    device_
                );
                context->DrawImage(
                    layer.blur,
                    D2D1::Point2F(glowImageRect.left, glowImageRect.top),
                    layer.effectRect
                );
            }
        }
        realizationBaseTransform = withViewport(
            D2D1::Matrix3x2F::Translation(dx, dy)
        );
        context->SetTransform(realizationBaseTransform);
        sharedInstanceTransformActive = false;

        auto drawShadowSilhouette = [&](ID2D1Geometry *geometry,
                                        ID2D1Geometry *animatedOuterGeometry,
                                        ID2D1Brush *brush,
                                        float strokeWidth, float stroke2Width,
                                        bool transformed) {
            const float outerWidth = stroke2Width > 0.0f
                ? std::max(strokeWidth, 0.0f) + stroke2Width
                : std::max(strokeWidth, 0.0f);
            if (transformed && impl_->dynamicDirectStrokeEnabled) {
                if (outerWidth > 0.0f) {
                    context->DrawGeometry(geometry, brush, outerWidth);
                }
            } else if (transformed && animatedOuterGeometry != nullptr) {
                context->FillGeometry(animatedOuterGeometry, brush);
            } else if (outerWidth > 0.0f) {
                context->DrawGeometry(geometry, brush, outerWidth);
            }
            context->FillGeometry(geometry, brush);
        };
        auto drawLineShadowPhase = [&](bool after) {
            if (line->hasInlineStyles || hasCharacterTransition) {
                for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
                    const Impl::CachedChar &ch = line->chars[charIndex];
                    ID2D1Geometry *geometry = charGeometryAt(charIndex);
                    if (geometry == nullptr) {
                        continue;
                    }
                    const TextStyle &charStyle = ch.styleIndex >= 0
                        && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                        ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                        : style;
                    if (charStyle.decorationKind != "shadow") {
                        continue;
                    }
                    Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrushAt(
                        after ? charStyle.afterDecorPaint : charStyle.beforeDecorPaint,
                        mainPaintBounds(
                            after ? charStyle.afterDecorPaint
                                  : charStyle.beforeDecorPaint,
                            ch.styleIndex
                        ),
                        after ? charStyle.afterDecor : charStyle.beforeDecor,
                        charStyle.shadowOffsetX,
                        charStyle.shadowOffsetY
                    );
                    brush->SetOpacity(globalOpacity * characterOpacityAt(charIndex));
                    const CharacterAnimationState animationState =
                        characterAnimationAt(charIndex);
                    const D2D1_MATRIX_3X2_F charMatrix = animationState.matrix;
                    const float shadowX = animationState.transformed
                        ? charStyle.shadowOffsetX * charMatrix._11
                            + charStyle.shadowOffsetY * charMatrix._21
                        : charStyle.shadowOffsetX;
                    const float shadowY = animationState.transformed
                        ? charStyle.shadowOffsetX * charMatrix._12
                            + charStyle.shadowOffsetY * charMatrix._22
                        : charStyle.shadowOffsetY;
                    context->SetTransform(withViewport(
                        D2D1::Matrix3x2F::Translation(
                            dx + shadowX,
                            dy + shadowY
                        )
                    ));
                    bool pushedAfterClip = false;
                    const bool wipeComplete = useUtopiaTransition
                        ? charWipeComplete(charIndex)
                        : mainWipeComplete;
                    if (after && !wipeComplete) {
                        if (useUtopiaTransition) {
                            const auto [animatedBounds, animatedEdge]
                                = utopiaCharWipe(charIndex);
                            const float pad = std::max(
                                charStyle.strokeWidth + charStyle.stroke2Width,
                                2.0f
                            ) + 4.0f;
                            D2D1_RECT_F shiftedBounds = animatedBounds;
                            shiftedBounds.left -= shadowX;
                            shiftedBounds.right -= shadowX;
                            shiftedBounds.top -= shadowY;
                            shiftedBounds.bottom -= shadowY;
                            const float shiftedEdge = animatedEdge
                                - (style.vertical ? shadowY : shadowX);
                            pushAxisAlignedClip(
                                directionalWipeClip(
                                    shiftedBounds, shiftedEdge, pad, true
                                ),
                                D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                            );
                            pushedAfterClip = true;
                        } else {
                            pushAxisAlignedClip(
                                style.vertical
                                    ? D2D1::RectF(
                                        afterClip.left,
                                        afterClip.top - charStyle.shadowOffsetY,
                                        afterClip.right,
                                        afterClip.bottom - charStyle.shadowOffsetY
                                    )
                                    : D2D1::RectF(
                                        afterClip.left - charStyle.shadowOffsetX,
                                        afterClip.top,
                                        afterClip.right - charStyle.shadowOffsetX,
                                        afterClip.bottom
                                    ),
                                D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                            );
                            pushedAfterClip = true;
                        }
                    }
                    ID2D1Geometry *animatedOuter = charStyle.stroke2Width > 0.0f
                        ? stroke2GeometryAt(charIndex)
                        : strokeGeometryAt(charIndex);
                    drawShadowSilhouette(
                        geometry, animatedOuter, brush.Get(),
                        charStyle.strokeWidth, charStyle.stroke2Width,
                        charTransformedAt(charIndex)
                    );
                    if (pushedAfterClip) {
                        context->PopAxisAlignedClip();
                    }
                }
            } else if (style.decorationKind == "shadow") {
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrushAt(
                    after ? style.afterDecorPaint : style.beforeDecorPaint,
                    mainPaintBounds(
                        after ? style.afterDecorPaint : style.beforeDecorPaint,
                        -1
                    ),
                    after ? style.afterDecor : style.beforeDecor,
                    style.shadowOffsetX,
                    style.shadowOffsetY
                );
                context->SetTransform(withViewport(
                    D2D1::Matrix3x2F::Translation(
                        dx + style.shadowOffsetX,
                        dy + style.shadowOffsetY
                    )
                ));
                const bool pushedAfterClip = after && !mainWipeComplete;
                if (pushedAfterClip) {
                    pushAxisAlignedClip(
                        style.vertical
                            ? D2D1::RectF(
                                afterClip.left,
                                afterClip.top - style.shadowOffsetY,
                                afterClip.right,
                                afterClip.bottom - style.shadowOffsetY
                            )
                            : D2D1::RectF(
                                afterClip.left - style.shadowOffsetX,
                                afterClip.top,
                                afterClip.right - style.shadowOffsetX,
                                afterClip.bottom
                            ),
                        D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                for (const auto &geometry : line->geometries) {
                    drawShadowSilhouette(
                        geometry.Get(), nullptr, brush.Get(),
                        style.strokeWidth, style.stroke2Width, false
                    );
                }
                if (pushedAfterClip) {
                    context->PopAxisAlignedClip();
                }
            }
            context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(dx, dy)));
        };
        drawLineShadowPhase(false);
        if (hasAfterWipe) {
            drawLineShadowPhase(true);
        }

        for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            if (rubyStyle.rubyDecorationKind != "shadow") {
                continue;
            }
            const float edge = rubyWipeEdgeAt(ruby);
            const bool complete = rubyWipeComplete(ruby);
            auto drawRubyShadowPhase = [&](bool after) {
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrushAt(
                    after
                        ? rubyStyle.rubyAfterDecorPaint
                        : rubyStyle.rubyBeforeDecorPaint,
                    rubyPaintBounds(
                        after
                            ? rubyStyle.rubyAfterDecorPaint
                            : rubyStyle.rubyBeforeDecorPaint,
                        ruby.fillBounds,
                        ruby.horizontalFillBounds
                    ),
                    after ? rubyStyle.rubyAfterDecor : rubyStyle.rubyBeforeDecor,
                    rubyStyle.rubyShadowOffsetX,
                    rubyStyle.rubyShadowOffsetY
                );
                const bool pushedStaticClip = after
                    && !useUtopiaTransition
                    && !complete;
                if (pushedStaticClip) {
                    const float pad = std::max(
                        rubyStyle.rubyStrokeWidth + rubyStyle.rubyStroke2Width,
                        2.0f
                    ) + 4.0f;
                    pushAxisAlignedClip(
                        style.vertical
                            ? D2D1::RectF(
                                ruby.bounds.left - pad,
                                ruby.bounds.top - pad - rubyStyle.rubyShadowOffsetY,
                                ruby.bounds.right + pad,
                                edge - rubyStyle.rubyShadowOffsetY
                            )
                            : (rtl
                                ? D2D1::RectF(
                                    edge - rubyStyle.rubyShadowOffsetX,
                                    fullWipeClipTop,
                                    ruby.bounds.right + pad
                                        - rubyStyle.rubyShadowOffsetX,
                                    fullWipeClipBottom
                                )
                                : D2D1::RectF(
                                    ruby.bounds.left - pad
                                        - rubyStyle.rubyShadowOffsetX,
                                    fullWipeClipTop,
                                    edge - rubyStyle.rubyShadowOffsetX,
                                    fullWipeClipBottom
                                )),
                        D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                for (std::size_t geometryIndex = 0;
                     geometryIndex < ruby.geometries.size(); ++geometryIndex) {
                    ID2D1Geometry *geometry = rubyGeometryAt(
                        rubyIndex, geometryIndex
                    );
                    if (geometry == nullptr) {
                        continue;
                    }
                    const CharacterAnimationState animationState =
                        rubyUnitAnimationAt(ruby, geometryIndex);
                    brush->SetOpacity(globalOpacity * animationState.opacity);
                    const float shadowX = animationState.transformed
                        ? rubyStyle.rubyShadowOffsetX * animationState.matrix._11
                            + rubyStyle.rubyShadowOffsetY * animationState.matrix._21
                        : rubyStyle.rubyShadowOffsetX;
                    const float shadowY = animationState.transformed
                        ? rubyStyle.rubyShadowOffsetX * animationState.matrix._12
                            + rubyStyle.rubyShadowOffsetY * animationState.matrix._22
                        : rubyStyle.rubyShadowOffsetY;
                    context->SetTransform(withViewport(
                        D2D1::Matrix3x2F::Translation(
                            dx + shadowX, dy + shadowY
                        )
                    ));
                    bool pushedUtopiaClip = false;
                    if (after && useUtopiaTransition) {
                        // Same unit-phase gate as the ruby body: a
                        // not-yet-started unit skips its after shadow, a
                        // wiping unit clips at the shifted front.
                        const N3WipePhase phase = rubyUnitWipePhaseAt(
                            ruby, geometryIndex
                        );
                        if (phase == N3WipePhase::Before) {
                            continue;
                        }
                        if (phase == N3WipePhase::Wiping) {
                            const auto [animatedBounds, animatedEdge] =
                                utopiaRubyUnitWipe(
                                    ruby, rubyIndex, geometryIndex, rubyStyle
                                );
                            const float pad = std::max(
                                rubyStyle.rubyStrokeWidth
                                    + rubyStyle.rubyStroke2Width,
                                2.0f
                            ) + 4.0f;
                            D2D1_RECT_F shiftedBounds = animatedBounds;
                            shiftedBounds.left -= shadowX;
                            shiftedBounds.right -= shadowX;
                            shiftedBounds.top -= shadowY;
                            shiftedBounds.bottom -= shadowY;
                            const float shiftedEdge = animatedEdge
                                - (style.vertical ? shadowY : shadowX);
                            pushAxisAlignedClip(
                                directionalWipeClip(
                                    shiftedBounds, shiftedEdge, pad, true
                                ),
                                D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                            );
                            pushedUtopiaClip = true;
                        }
                    }
                    ID2D1Geometry *animatedOuter = rubyStyle.rubyStroke2Width > 0.0f
                        ? rubyStroke2GeometryAt(rubyIndex, geometryIndex)
                        : rubyStrokeGeometryAt(rubyIndex, geometryIndex);
                    drawShadowSilhouette(
                        geometry, animatedOuter, brush.Get(),
                        rubyStyle.rubyStrokeWidth,
                        rubyStyle.rubyStroke2Width,
                        rubyUnitTransformed(ruby, geometryIndex)
                    );
                    if (pushedUtopiaClip) {
                        context->PopAxisAlignedClip();
                    }
                }
                if (pushedStaticClip) {
                    context->PopAxisAlignedClip();
                }
            };
            drawRubyShadowPhase(false);
            if (rubyPhaseVisible(ruby, edge, true)) {
                drawRubyShadowPhase(true);
            }
        }
        context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(dx, dy)));

        // Route every line through the per-character N3 phase ordering. The
        // retired line-box "legacy stack" drew both colour states of the
        // whole line through one clip rect; its resting front could never
        // simultaneously clear the finished character's stroke2 ring and
        // spare the next character's own ring (the two painted extents can
        // touch or overlap), so each decoration needed another special case.
        // The phase path never paints a not-yet-started character's after
        // side at all, which removes the invariant conflict and matches the
        // Painter's per-glyph clip bands.
        {
        // All three N3 layers share this character classification and clip.
        const auto pushMainWipeClip = [&](std::size_t charIndex, bool after) {
            const Impl::CachedChar &ch = line->chars[charIndex];
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            float edge = delegatedWipeCoordinateAt(line->chars, charIndex);
            D2D1_RECT_F bounds = line->bounds;
            if (ch.bitmapGuide.has_value()) {
                bounds = ch.bitmapRect;
                const float imageHeight = std::max(
                    ch.bitmapRect.bottom - ch.bitmapRect.top, 0.0f
                );
                const float bottomOffset = std::abs(
                    ch.bitmapGuide->marginBottom
                        * std::max(scene.layoutReferenceScale, 0.01f)
                );
                // A bitmap shifted by at least its own height has visually
                // left the row, but it still belongs to this source line.
                // Keep the offset bitmap rect for clipping and reuse the
                // owning line's main wipe front instead of the collapsed
                // zero-width character interval.
                if (imageHeight > 0.0f && bottomOffset >= imageHeight) {
                    edge = wipeEdge;
                }
            } else if (useUtopiaTransition || charTransformedAt(charIndex)) {
                const auto animated = utopiaCharWipe(charIndex);
                bounds = animated.first;
                edge = animated.second;
            }
            const float pad = std::max(
                charStyle.strokeWidth + charStyle.stroke2Width, 2.0f
            ) + 4.0f;
            pushAxisAlignedClip(
                directionalWipeClip(bounds, edge, pad, after),
                D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
            );
        };
        const auto drawMainLayerPart = [&](std::size_t charIndex,
                                           bool after, int layer) {
            const Impl::CachedChar &ch = line->chars[charIndex];
            if (ch.bitmapGuide.has_value()) {
                if (layer == 2) {
                    drawBitmapGuidePart(charIndex, after);
                }
                return;
            }
            ID2D1Geometry *geometry = charGeometryAt(charIndex);
            if (geometry == nullptr) {
                return;
            }
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            const PaintStyle &paint = layer == 0
                ? (after ? charStyle.afterStroke2Paint : charStyle.beforeStroke2Paint)
                : (layer == 1
                    ? (after ? charStyle.afterStrokePaint : charStyle.beforeStrokePaint)
                    : (after ? charStyle.afterFillPaint : charStyle.beforeFillPaint));
            const RgbaColor &color = layer == 0
                ? (after ? charStyle.afterStroke2 : charStyle.beforeStroke2)
                : (layer == 1
                    ? (after ? charStyle.afterStroke : charStyle.beforeStroke)
                    : (after ? charStyle.afterFill : charStyle.beforeFill));
            // The common (non-role) Utopia path used to recreate one D2D
            // brush for every character and every visual layer.  A long line
            // therefore allocated dozens of COM brush objects per frame even
            // though all characters share the six line-level brushes above.
            // Reuse those brushes; role-specific brushes still retain their
            // own paint definition and are handled by the fallback below.
            Microsoft::WRL::ComPtr<ID2D1Brush> ownedBrush;
            ID2D1Brush *brush = nullptr;
            if (ch.styleIndex < 0) {
                brush = layer == 0
                    ? (after ? afterStroke2.Get() : beforeStroke2.Get())
                    : (layer == 1
                        ? (after ? afterStroke.Get() : beforeStroke.Get())
                        : (after ? afterFill.Get() : beforeFill.Get()));
            } else {
                ownedBrush = paintBrush(
                    paint, mainPaintBounds(paint, ch.styleIndex), color
                );
                brush = ownedBrush.Get();
            }
            brush->SetOpacity(globalOpacity * characterOpacityAt(charIndex));
            // Only glyphs whose matrix is non-identity need the pre-expanded
            // stroke geometry.  Treating the whole Utopia line as animated
            // makes every settled glyph fill a complex widened path on every
            // frame, which is dramatically slower than Direct2D's native
            // DrawGeometry stroke on long real-world lines.
            const bool animated = charTransformedAt(charIndex);
            const bool realizationEligible = !animated
                && std::max(charStyle.strokeWidth, 0.0f)
                    >= Impl::realizationStrokeThreshold;
            if (layer == 0) {
                if (charStyle.stroke2Width <= 0.0f) {
                    return;
                }
                ID2D1Geometry *animatedStroke2 = stroke2GeometryAt(charIndex);
                if (animated && impl_->dynamicDirectStrokeEnabled) {
                    drawCountedStroke(
                        geometry, brush,
                        std::max(0.0f, charStyle.strokeWidth)
                            + charStyle.stroke2Width,
                        true
                    );
                } else if (animated && animatedStroke2 != nullptr) {
                    fillCountedStroke(animatedStroke2, brush, true);
                } else {
                    strokeWithRealization(
                        ch.stroke2Realization.Get(), geometry, brush,
                        std::max(0.0f, charStyle.strokeWidth)
                            + charStyle.stroke2Width,
                        true,
                        ch.stroke2RealizationTransform,
                        realizationEligible
                    );
                }
                return;
            }
            if (layer == 1) {
                if (charStyle.strokeWidth <= 0.0f) {
                    return;
                }
                const bool protect = paintNeedsBodyProtection(
                    after ? charStyle.afterFillPaint : charStyle.beforeFillPaint
                );
                ID2D1Geometry *protectedGeometry = protectedGeometryAt(charIndex);
                ID2D1Geometry *animatedStroke = strokeGeometryAt(charIndex);
                if (animated && !protect
                    && impl_->dynamicDirectStrokeEnabled) {
                    drawCountedStroke(
                        geometry, brush, charStyle.strokeWidth, false
                    );
                } else if (animated && !protect && animatedStroke != nullptr) {
                    fillCountedStroke(animatedStroke, brush, false);
                } else if (protect && protectedGeometry != nullptr) {
                    if (animated) {
                        fillCountedStroke(protectedGeometry, brush, false);
                    } else {
                        fillStrokeWithRealization(
                            ch.protectedStrokeRealization.Get(),
                            protectedGeometry, brush, false,
                            ch.protectedStrokeRealizationTransform,
                            realizationEligible
                        );
                    }
                } else {
                    strokeWithRealization(
                        ch.strokeRealization.Get(), geometry, brush,
                        charStyle.strokeWidth, false,
                        ch.strokeRealizationTransform, realizationEligible
                    );
                }
                return;
            }
            fillWithRealization(
                ch.fillRealization.Get(), geometry, brush,
                ch.fillRealizationTransform, realizationEligible
            );
        };
        const auto drawMainLayer = [&](int layer) {
            const auto drawPhasePart = [&](std::size_t charIndex,
                                           N3WipePhase phase) {
                if (layer == 2 && bitmapGuideNoWipe(line->chars[charIndex])) {
                    drawMainLayerPart(charIndex, false, layer);
                    return;
                }
                if (phase != N3WipePhase::Wiping) {
                    drawMainLayerPart(
                        charIndex, phase == N3WipePhase::After, layer
                    );
                    return;
                }
                pushMainWipeClip(charIndex, false);
                drawMainLayerPart(charIndex, false, layer);
                context->PopAxisAlignedClip();
                pushMainWipeClip(charIndex, true);
                drawMainLayerPart(charIndex, true, layer);
                context->PopAxisAlignedClip();
            };
            for (std::size_t reverse = line->chars.size(); reverse > 0; --reverse) {
                const std::size_t index = reverse - 1;
                if (wipePhaseAt(line->chars, index) == N3WipePhase::Before) {
                    drawPhasePart(index, N3WipePhase::Before);
                }
            }
            for (std::size_t index = 0; index < line->chars.size(); ++index) {
                if (wipePhaseAt(line->chars, index) == N3WipePhase::After) {
                    drawPhasePart(index, N3WipePhase::After);
                }
            }
            for (std::size_t index = 0; index < line->chars.size(); ++index) {
                if (wipePhaseAt(line->chars, index) == N3WipePhase::Wiping) {
                    drawPhasePart(index, N3WipePhase::Wiping);
                }
            }
        };
        // N3 performs phase ordering independently for edge2, edge and body.
        drawMainLayer(0);
        drawMainLayer(1);
        drawMainLayer(2);
        }

        // Scan-line sweep on the main text. Feather slices are clipped to the
        // glyph geometry, so no rectangular halo reaches transparent gaps.
        if (scanlineActive && scanlineAlpha > 0.0f) {
            if (scanlineMainGlow.blur != nullptr) {
                drawScanlineGlowLayer(scanlineMainGlow);
            }
            context->SetTransform(realizationBaseTransform);
            sharedInstanceTransformActive = false;
            const float scanlineSolidPad = std::max(style.strokeWidth, 0.0f)
                + std::max(style.stroke2Width, 0.0f) + 4.0f;
            for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
                const auto band = scanlineCharBand(charIndex);
                ID2D1Geometry *geometry = charGeometryAt(charIndex);
                if (!band.has_value() || geometry == nullptr) {
                    continue;
                }
                const Impl::CachedChar &ch = line->chars[charIndex];
                if (!rectsOverlap(
                        band->first,
                        expandedRect(
                            D2D1::RectF(ch.left, ch.top, ch.right, ch.bottom),
                            scanlineSolidPad
                        )
                    )) {
                    continue;
                }
                const TextStyle &charStyle = ch.styleIndex >= 0
                    && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                    ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                    : style;
                const auto featherSlices = scanlineFeatherSlices(band->second);
                for (bool after : {false, true}) {
                    for (int layer = 0; layer < 3; ++layer) {
                        const PaintStyle &sourcePaint = layer == 0
                            ? (after ? charStyle.afterStroke2Paint : charStyle.beforeStroke2Paint)
                            : (layer == 1
                                ? (after ? charStyle.afterStrokePaint : charStyle.beforeStrokePaint)
                                : (after ? charStyle.afterFillPaint : charStyle.beforeFillPaint));
                        const RgbaColor &sourceColor = layer == 0
                            ? (after ? charStyle.afterStroke2 : charStyle.beforeStroke2)
                            : (layer == 1
                                ? (after ? charStyle.afterStroke : charStyle.beforeStroke)
                                : (after ? charStyle.afterFill : charStyle.beforeFill));
                        if ((layer == 0 && charStyle.stroke2Width <= 0.0f)
                            || (layer == 1 && charStyle.strokeWidth <= 0.0f)) {
                            continue;
                        }
                        const PaintStyle paint = scanlinePaintFor(sourcePaint);
                        const RgbaColor color = scanlineColorFor(sourceColor);
                        Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                            paint, mainPaintBounds(paint, ch.styleIndex), color
                        );
                        for (const auto &[sliceRect, sliceAlpha] : featherSlices) {
                            brush->SetOpacity(
                                globalOpacity * characterOpacityAt(charIndex)
                                    * scanlineAlpha * sliceAlpha
                            );
                            pushAxisAlignedClip(
                                sliceRect, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                            );
                            pushScanlineStateClip(band->second, after);
                            if (layer == 0) {
                                context->DrawGeometry(
                                    geometry, brush.Get(),
                                    std::max(charStyle.strokeWidth, 0.0f)
                                        + charStyle.stroke2Width
                                );
                            } else if (layer == 1) {
                                context->DrawGeometry(
                                    geometry, brush.Get(), charStyle.strokeWidth
                                );
                            } else {
                                context->FillGeometry(geometry, brush.Get());
                            }
                            context->PopAxisAlignedClip();
                            context->PopAxisAlignedClip();
                        }
                    }
                }
            }
        }
        auto drawRubyStack = [&](std::size_t rubyIndex, const Impl::CachedRuby &ruby, bool after) {
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            Microsoft::WRL::ComPtr<ID2D1Brush> fill = paintBrush(
                after ? rubyStyle.rubyAfterFillPaint : rubyStyle.rubyBeforeFillPaint,
                rubyPaintBounds(
                    after ? rubyStyle.rubyAfterFillPaint : rubyStyle.rubyBeforeFillPaint,
                    ruby.fillBounds,
                    ruby.horizontalFillBounds
                ),
                after ? rubyStyle.rubyAfterFill : rubyStyle.rubyBeforeFill
            );
            Microsoft::WRL::ComPtr<ID2D1Brush> stroke = paintBrush(
                after ? rubyStyle.rubyAfterStrokePaint : rubyStyle.rubyBeforeStrokePaint,
                rubyPaintBounds(
                    after ? rubyStyle.rubyAfterStrokePaint : rubyStyle.rubyBeforeStrokePaint,
                    ruby.fillBounds,
                    ruby.horizontalFillBounds
                ),
                after ? rubyStyle.rubyAfterStroke : rubyStyle.rubyBeforeStroke
            );
            Microsoft::WRL::ComPtr<ID2D1Brush> stroke2 = paintBrush(
                after
                    ? rubyStyle.rubyAfterStroke2Paint
                    : rubyStyle.rubyBeforeStroke2Paint,
                rubyPaintBounds(
                    after
                        ? rubyStyle.rubyAfterStroke2Paint
                        : rubyStyle.rubyBeforeStroke2Paint,
                    ruby.fillBounds,
                    ruby.horizontalFillBounds
                ),
                after ? rubyStyle.rubyAfterStroke2 : rubyStyle.rubyBeforeStroke2
            );
            for (std::size_t index = 0; index < ruby.geometries.size(); ++index) {
                ID2D1Geometry *geometry = rubyGeometryAt(rubyIndex, index);
                if (geometry == nullptr) {
                    continue;
                }
                const float rubyOpacity = globalOpacity
                    * rubyUnitOpacityAt(ruby, index);
                fill->SetOpacity(rubyOpacity);
                stroke->SetOpacity(rubyOpacity);
                stroke2->SetOpacity(rubyOpacity);
                bool pushedUtopiaClip = false;
                if (after && useUtopiaTransition) {
                    // Gate by unit wipe phase like the glow worker and the
                    // main-text body path. A not-yet-started unit must not
                    // paint its after side at all: its ratio-0 edge rests at
                    // the wipe-left (ink minus ruby primary edge / 2), which
                    // sits inside the unit's own stroke2 ring, so clipping
                    // there leaks a sliver of after colour on large stroked
                    // ruby the moment an earlier unit starts wiping.
                    const N3WipePhase phase = rubyUnitWipePhaseAt(
                        ruby, index
                    );
                    if (phase == N3WipePhase::Before) {
                        continue;
                    }
                    if (phase == N3WipePhase::Wiping) {
                        const auto [animatedBounds, animatedEdge] =
                            utopiaRubyUnitWipe(
                                ruby, rubyIndex, index, rubyStyle
                            );
                        const float pad = std::max(
                            rubyStyle.rubyStrokeWidth
                                + rubyStyle.rubyStroke2Width,
                            2.0f
                        ) + 4.0f;
                        pushAxisAlignedClip(
                            directionalWipeClip(
                                animatedBounds, animatedEdge, pad, true
                            ),
                            D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                        );
                        pushedUtopiaClip = true;
                    }
                }
                ID2D1Geometry *animatedStroke2 = rubyStroke2GeometryAt(
                    rubyIndex, index
                );
                const Impl::CachedChar *rubyChar = index < ruby.chars.size()
                    ? &ruby.chars[index]
                    : nullptr;
                const D2D1_MATRIX_3X2_F identityTransform =
                    D2D1::Matrix3x2F::Identity();
                const bool rubyTransformed = rubyUnitTransformed(ruby, index);
                const bool realizationEligible = !rubyTransformed
                    && std::max(rubyStyle.rubyStrokeWidth, 0.0f)
                        >= Impl::realizationStrokeThreshold;
                if (rubyStyle.rubyStroke2Width > 0.0f) {
                    if (rubyTransformed && impl_->dynamicDirectStrokeEnabled) {
                        drawCountedStroke(
                            geometry, stroke2.Get(),
                            std::max(0.0f, rubyStyle.rubyStrokeWidth)
                                + rubyStyle.rubyStroke2Width,
                            true
                        );
                    } else if (rubyTransformed && animatedStroke2 != nullptr) {
                        fillCountedStroke(
                            animatedStroke2, stroke2.Get(), true
                        );
                    } else {
                        strokeWithRealization(
                            rubyChar != nullptr
                                ? rubyChar->stroke2Realization.Get()
                                : nullptr,
                            geometry, stroke2.Get(),
                            std::max(0.0f, rubyStyle.rubyStrokeWidth)
                                + rubyStyle.rubyStroke2Width,
                            true,
                            rubyChar != nullptr
                                ? rubyChar->stroke2RealizationTransform
                                : identityTransform,
                            realizationEligible
                        );
                    }
                }
                if (rubyStyle.rubyStrokeWidth > 0.0f) {
                    const bool protect = paintNeedsBodyProtection(
                        after
                            ? rubyStyle.rubyAfterFillPaint
                            : rubyStyle.rubyBeforeFillPaint
                    );
                    ID2D1Geometry *protectedGeometry = rubyProtectedGeometryAt(
                        rubyIndex, index
                    );
                    ID2D1Geometry *animatedStroke = rubyStrokeGeometryAt(
                        rubyIndex, index
                    );
                    if (rubyTransformed && !protect
                        && impl_->dynamicDirectStrokeEnabled) {
                        drawCountedStroke(
                            geometry, stroke.Get(),
                            rubyStyle.rubyStrokeWidth, false
                        );
                    } else if (rubyTransformed && !protect
                        && animatedStroke != nullptr) {
                        fillCountedStroke(
                            animatedStroke, stroke.Get(), false
                        );
                    } else if (protect && protectedGeometry != nullptr) {
                        if (rubyTransformed) {
                            fillCountedStroke(
                                protectedGeometry, stroke.Get(), false
                            );
                        } else {
                            fillStrokeWithRealization(
                                rubyChar != nullptr
                                    ? rubyChar->protectedStrokeRealization.Get()
                                    : nullptr,
                                protectedGeometry, stroke.Get(), false,
                                rubyChar != nullptr
                                    ? rubyChar->protectedStrokeRealizationTransform
                                    : identityTransform,
                                realizationEligible
                            );
                        }
                    } else {
                        strokeWithRealization(
                            rubyChar != nullptr
                                ? rubyChar->strokeRealization.Get()
                                : nullptr,
                            geometry, stroke.Get(), rubyStyle.rubyStrokeWidth,
                            false,
                            rubyChar != nullptr
                                ? rubyChar->strokeRealizationTransform
                                : identityTransform,
                            realizationEligible
                        );
                    }
                }
                fillWithRealization(
                    rubyChar != nullptr ? rubyChar->fillRealization.Get() : nullptr,
                    geometry, fill.Get(),
                    rubyChar != nullptr
                        ? rubyChar->fillRealizationTransform
                        : identityTransform,
                    realizationEligible
                );
                if (pushedUtopiaClip) {
                    context->PopAxisAlignedClip();
                }
            }
        };
        for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            const float rubyWipeEdge = rubyWipeEdgeAt(ruby);
            const bool rubyComplete = rubyWipeComplete(ruby);
            const float rubyPad = std::max(
                rubyStyle.rubyStrokeWidth + rubyStyle.rubyStroke2Width, 2.0f
            ) + 4.0f;
            const D2D1_RECT_F rubyAfterClip = style.vertical
                ? (reverseVertical
                    ? D2D1::RectF(
                        ruby.bounds.left - rubyPad,
                        rubyWipeEdge,
                        ruby.bounds.right + rubyPad,
                        ruby.bounds.bottom + rubyPad
                    )
                    : D2D1::RectF(
                        ruby.bounds.left - rubyPad,
                        ruby.bounds.top - rubyPad,
                        ruby.bounds.right + rubyPad,
                        rubyWipeEdge
                    ))
                : (rtl
                    ? D2D1::RectF(
                        rubyWipeEdge,
                        fullWipeClipTop,
                        ruby.bounds.right + rubyPad,
                        fullWipeClipBottom
                    )
                    : D2D1::RectF(
                        ruby.bounds.left - rubyPad,
                        fullWipeClipTop,
                        rubyWipeEdge,
                        fullWipeClipBottom
                    ));
            drawRubyStack(rubyIndex, ruby, false);
            if (rubyPhaseVisible(ruby, rubyWipeEdge, true)) {
                if (!useUtopiaTransition && !rubyComplete) {
                    pushAxisAlignedClip(
                        rubyAfterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                drawRubyStack(rubyIndex, ruby, true);
                if (!useUtopiaTransition && !rubyComplete) {
                    context->PopAxisAlignedClip();
                }
            }
        }

        // Ruby uses the same inner-only feathering as the main text.
        if (scanlineActive && scanlineAlpha > 0.0f) {
            if (scanlineRubyGlow.blur != nullptr) {
                drawScanlineGlowLayer(scanlineRubyGlow);
            }
            context->SetTransform(realizationBaseTransform);
            sharedInstanceTransformActive = false;
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
                const float rubySolidPad = std::max(
                    rubyStyle.rubyStrokeWidth + rubyStyle.rubyStroke2Width, 2.0f
                ) + 4.0f;
                for (std::size_t unitIndex = 0;
                     unitIndex < ruby.geometries.size(); ++unitIndex) {
                    ID2D1Geometry *geometry = rubyGeometryAt(rubyIndex, unitIndex);
                    if (geometry == nullptr) {
                        continue;
                    }
                    D2D1_RECT_F band{};
                    if (useUtopiaTransition) {
                        if (rubyUnitWipePhaseAt(ruby, unitIndex)
                            != N3WipePhase::Wiping) {
                            continue;
                        }
                        const auto animated = utopiaRubyUnitWipe(
                            ruby, rubyIndex, unitIndex, rubyStyle
                        );
                        band = scanlineBandRect(animated.first, animated.second);
                    } else {
                        // Per-unit gate (closed window, like the main text):
                        // a group-level band keeps the highlight parked on
                        // the resting group front through intra-group gaps
                        // and paints not-yet-started units near it. The
                        // active unit's own front is the group front, so
                        // gating to in-window units loses nothing.
                        const Impl::CachedChar *unit =
                            unitIndex < ruby.chars.size()
                                ? &ruby.chars[unitIndex]
                                : nullptr;
                        const int unitStart = unit ? wipeStartMs(*unit) : 0;
                        const int unitEnd = unit ? wipeEndMs(*unit) : -1;
                        if (!(unitStart != unitEnd
                                && unitStart <= tMs && tMs <= unitEnd)) {
                            continue;
                        }
                        band = scanlineBandRect(ruby.bounds, rubyWipeEdgeAt(ruby));
                    }
                    if (!rectsOverlap(
                            band,
                            expandedRect(ruby.bounds, rubySolidPad)
                        )) {
                        continue;
                    }
                    const float edge = style.vertical
                        ? (band.top + band.bottom) * 0.5f
                        : (band.left + band.right) * 0.5f;
                    const auto featherSlices = scanlineFeatherSlices(edge);
                    for (bool after : {false, true}) {
                        for (int layer = 0; layer < 3; ++layer) {
                            const PaintStyle &sourcePaint = layer == 0
                                ? (after ? rubyStyle.rubyAfterStroke2Paint
                                         : rubyStyle.rubyBeforeStroke2Paint)
                                : (layer == 1
                                    ? (after ? rubyStyle.rubyAfterStrokePaint
                                             : rubyStyle.rubyBeforeStrokePaint)
                                    : (after ? rubyStyle.rubyAfterFillPaint
                                             : rubyStyle.rubyBeforeFillPaint));
                            const RgbaColor &sourceColor = layer == 0
                                ? (after ? rubyStyle.rubyAfterStroke2
                                         : rubyStyle.rubyBeforeStroke2)
                                : (layer == 1
                                    ? (after ? rubyStyle.rubyAfterStroke
                                             : rubyStyle.rubyBeforeStroke)
                                    : (after ? rubyStyle.rubyAfterFill
                                             : rubyStyle.rubyBeforeFill));
                            if ((layer == 0 && rubyStyle.rubyStroke2Width <= 0.0f)
                                || (layer == 1 && rubyStyle.rubyStrokeWidth <= 0.0f)) {
                                continue;
                            }
                            const PaintStyle paint = scanlinePaintFor(sourcePaint);
                            const RgbaColor color = scanlineColorFor(sourceColor);
                            Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                                paint, rubyPaintBounds(
                                    paint, ruby.fillBounds,
                                    ruby.horizontalFillBounds
                                ), color
                            );
                            for (const auto &[sliceRect, sliceAlpha] : featherSlices) {
                                brush->SetOpacity(
                                    globalOpacity * rubyUnitOpacityAt(ruby, unitIndex)
                                        * scanlineAlpha * sliceAlpha
                                );
                                pushAxisAlignedClip(
                                    sliceRect, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                                );
                                pushScanlineStateClip(edge, after);
                                if (layer == 0) {
                                    context->DrawGeometry(
                                        geometry, brush.Get(),
                                        std::max(rubyStyle.rubyStrokeWidth, 0.0f)
                                            + rubyStyle.rubyStroke2Width
                                    );
                                } else if (layer == 1) {
                                    context->DrawGeometry(
                                        geometry, brush.Get(), rubyStyle.rubyStrokeWidth
                                    );
                                } else {
                                    context->FillGeometry(geometry, brush.Get());
                                }
                                context->PopAxisAlignedClip();
                                context->PopAxisAlignedClip();
                            }
                        }
                    }
                }
            }
        }
        restoreRealizationBaseTransform();
        if (signalState.visible
            && (style.volumeEnabled ? style.volumeOpacity : style.litOpacity) > 0.0f
            && signalState.opacity > 0.0f) {
            context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(signalDx, dy)));
            const float signalGroupOpacity = std::clamp(
                (style.volumeEnabled ? style.volumeOpacity : style.litOpacity)
                    * signalState.opacity
                    // 行级 OpacityLayer 正常时该值为 1（透明度由图层
                    // 统一承载）；图层不可用的逐笔刷兜底路径里它是
                    // 入退场动画透明度，信号必须与正文同乘。
                    * lineAnimationOpacity,
                0.0f,
                1.0f
            );
            auto signalBrush = [&](const RgbaColor &color) {
                PaintStyle paint;
                paint.mode = "solid";
                paint.color = color;
                return paintBrush(paint, line->fillBounds, color);
            };
            auto normalFill = signalBrush(style.volumeFill);
            auto normalStroke = signalBrush(style.volumeStroke);
            auto overlayFill = signalBrush(style.volumeOverlayFill);
            auto overlayStroke = signalBrush(style.volumeOverlayStroke);
            const float groupX = style.volumeOffsetX - signalGeometry.groupWidth;

            // 的柱按同一曲线在其覆盖窗口内放大-缩回（fill 阶段折算与
            // Painter 相同：先扣闪烁段再按柱均分）。
            const bool barZoomPulse = volumeAutoDecorated
                && line->karaokeAnimation == "utopia"
                && line->zoomPulseEnabled;
            float barFillDuration = static_cast<float>(signalActiveDuration);
            float barFlashDuration = 0.0f;
            if (style.volumeFlashTimes > 0
                && style.volumeFlashDurationRatio > 0.0f) {
                barFillDuration = signalActiveDuration
                    / (style.volumeFlashTimes * style.volumeFlashDurationRatio
                        + 1.0f);
                barFlashDuration = std::max(
                    static_cast<float>(signalActiveDuration) - barFillDuration,
                    0.0f
                );
            }
            const int barElapsed = std::clamp(
                tMs - (signalEndMs - signalActiveDuration),
                0,
                std::max(signalActiveDuration - 1, 0)
            );
            const float barFillElapsed = std::max(
                static_cast<float>(barElapsed) - barFlashDuration, 0.0f
            );
            // auto 档发光合成：源已预烘焙，这里套柱动画矩阵（含整字放大）
            // 后逐层 DrawImage（blur-then-transform）。
            for (VolumeBarGlowLayer &layer : volumeBarGlowLayers) {
                const D2D1_RECT_F barRect = volumeBarRectAt(layer.barIndex);
                BarAnimationState barAnimation = barAnimationAt(
                    layer.barIndex,
                    (barRect.left + barRect.right) * 0.5f,
                    (barRect.top + barRect.bottom) * 0.5f
                );
                if (barAnimation.opacity <= 0.0f) {
                    continue;
                }
                float pulse = 1.0f;
                if (barZoomPulse && barFillDuration > 0.0f) {
                    const int barStart = static_cast<int>(
                        barFillDuration * layer.barIndex / signalGeometry.count
                    );
                    const int barEnd = static_cast<int>(
                        barFillDuration * (layer.barIndex + 1)
                            / signalGeometry.count
                    );
                    pulse = zoomPulseScale(
                        static_cast<int>(barFillElapsed),
                        barStart,
                        barEnd,
                        style.zoomPulseCurveLevel
                    );
                }
                if (pulse != 1.0f) {
                    barAnimation.matrix = barCenteredMatrix(
                        0.0f, 0.0f, 0.0f, pulse, pulse, 0.0f,
                        (barRect.left + barRect.right) * 0.5f,
                        (barRect.top + barRect.bottom) * 0.5f
                    ) * barAnimation.matrix;
                }
                const D2D1_MATRIX_3X2_F base =
                    D2D1::Matrix3x2F::Translation(signalDx, dy);
                context->SetTransform(
                    withViewport(barAnimation.matrix * base)
                );
                for (int sigma : layer.sigmas) {
                    checkHr(
                        layer.blur->SetValue(
                            D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                            static_cast<float>(sigma)
                        ),
                        "ID2D1Effect::SetValue(volume bar glow sigma)",
                        device_
                    );
                    context->DrawImage(
                        layer.blur,
                        D2D1::Point2F(
                            layer.layerRect.left, layer.layerRect.top
                        ),
                        D2D1::RectF(
                            0.0f,
                            0.0f,
                            layer.layerRect.right - layer.layerRect.left,
                            layer.layerRect.bottom - layer.layerRect.top
                        )
                    );
                }
                context->SetTransform(
                    withViewport(D2D1::Matrix3x2F::Translation(signalDx, dy))
                );
            }
            auto drawColumn = [&](int index, bool overlay) {
                const D2D1_RECT_F rectF = volumeBarRectAt(index);
                const D2D1_ROUNDED_RECT rect = volumeBarRoundedRectAt(index);
                const float centerX = (rectF.left + rectF.right) * 0.5f;
                const float centerY = (rectF.top + rectF.bottom) * 0.5f;
                BarAnimationState barAnimation = barAnimationAt(
                    index, centerX, centerY
                );
                if (barAnimation.opacity <= 0.0f) {
                    return;
                }
                float pulse = 1.0f;
                if (barZoomPulse && barFillDuration > 0.0f) {
                    const int barStart = static_cast<int>(
                        barFillDuration * index / signalGeometry.count
                    );
                    const int barEnd = static_cast<int>(
                        barFillDuration * (index + 1) / signalGeometry.count
                    );
                    pulse = zoomPulseScale(
                        static_cast<int>(barFillElapsed),
                        barStart,
                        barEnd,
                        style.zoomPulseCurveLevel
                    );
                }
                if (pulse != 1.0f) {
                    barAnimation.matrix
                        = barCenteredMatrix(
                            0.0f, 0.0f, 0.0f, pulse, pulse, 0.0f,
                            centerX, centerY
                        )
                        * barAnimation.matrix;
                }
                // 填充/描边/阴影都在同一柱动画变换内绘制（utopia 翻转/飞行
                // 时跟着柱体走）；绘制完恢复组级基准变换。
                const D2D1_MATRIX_3X2_F base =
                    D2D1::Matrix3x2F::Translation(signalDx, dy);
                const bool transformed = !barAnimation.matrix.IsIdentity();
                if (transformed) {
                    // 柱矩阵定义在柱局部坐标（rect 坐标），必须先于组平移
                    // 生效：v·barM·base·viewport（行向量，左到右依次应用）。
                    context->SetTransform(
                        withViewport(barAnimation.matrix * base)
                    );
                }
                const float barOpacity = signalGroupOpacity
                    * barAnimation.opacity;
                if (volumeAutoDecorated) {
                    const PaintStyle &fillPaint = overlay
                        ? style.afterFillPaint
                        : style.beforeFillPaint;
                    const PaintStyle &strokePaint = overlay
                        ? style.afterStrokePaint
                        : style.beforeStrokePaint;
                    const PaintStyle &stroke2Paint = overlay
                        ? style.afterStroke2Paint
                        : style.beforeStroke2Paint;
                    const PaintStyle &decorPaint = overlay
                        ? style.afterDecorPaint
                        : style.beforeDecorPaint;
                    const RgbaColor &fillColor = overlay
                        ? style.afterFill
                        : style.beforeFill;
                    const RgbaColor &strokeColor = overlay
                        ? style.afterStroke
                        : style.beforeStroke;
                    const RgbaColor &stroke2Color = overlay
                        ? style.afterStroke2
                        : style.beforeStroke2;
                    const RgbaColor &decorColor = overlay
                        ? style.afterDecor
                        : style.beforeDecor;
                    // 渐变/图片填充的画刷跨度 = 柱组外接框（与 Painter 的
                    // group_rect 同口径）。
                    D2D1_RECT_F groupRect = volumeBarRectAt(0);
                    for (int other = 1; other < signalGeometry.count; ++other) {
                        const D2D1_RECT_F otherRect = volumeBarRectAt(other);
                        groupRect = D2D1::RectF(
                            std::min(groupRect.left, otherRect.left),
                            std::min(groupRect.top, otherRect.top),
                            std::max(groupRect.right, otherRect.right),
                            std::max(groupRect.bottom, otherRect.bottom)
                        );
                    }
                    // 阴影装饰：偏移整影（外圈描边宽 + 填充），镜像
                    // drawShadowSilhouette。
                    if (style.decorationKind == "shadow"
                        && (style.shadowOffsetX != 0.0f
                            || style.shadowOffsetY != 0.0f)) {
                        const float shadowDx
                            = style.shadowOffsetX * volumeDecorScale;
                        const float shadowDy
                            = style.shadowOffsetY * volumeDecorScale;
                        if (shadowDx != 0.0f || shadowDy != 0.0f) {
                            const float shadowOuter
                                = volumeDecorStroke2Width > 0.0f
                                ? volumeDecorStrokeWidth
                                    + volumeDecorStroke2Width
                                : volumeDecorStrokeWidth;
                            Microsoft::WRL::ComPtr<ID2D1Brush> decorBrush
                                = paintBrush(decorPaint, groupRect, decorColor);
                            decorBrush->SetOpacity(barOpacity);
                            const D2D1_RECT_F shadowRect = D2D1::RectF(
                                rectF.left + shadowDx,
                                rectF.top + shadowDy,
                                rectF.right + shadowDx,
                                rectF.bottom + shadowDy
                            );
                            const float shadowRadius = std::max(
                                std::min(
                                    shadowRect.right - shadowRect.left,
                                    shadowRect.bottom - shadowRect.top
                                ) * 0.22f,
                                1.0f
                            );
                            const D2D1_ROUNDED_RECT shadowRounded
                                = D2D1::RoundedRect(
                                    shadowRect, shadowRadius, shadowRadius
                                );
                            if (shadowOuter > 0.0f) {
                                context->DrawRoundedRectangle(
                                    shadowRounded,
                                    decorBrush.Get(),
                                    shadowOuter
                                );
                            }
                            context->FillRoundedRectangle(
                                shadowRounded, decorBrush.Get()
                            );
                        }
                    }
                    if (volumeDecorStroke2Width > 0.0f
                        && stroke2Color.alpha > 0) {
                        Microsoft::WRL::ComPtr<ID2D1Brush> stroke2Brush
                            = paintBrush(stroke2Paint, groupRect, stroke2Color);
                        stroke2Brush->SetOpacity(barOpacity);
                        context->DrawRoundedRectangle(
                            rect,
                            stroke2Brush.Get(),
                            volumeDecorStrokeWidth + volumeDecorStroke2Width
                        );
                    }
                    if (volumeDecorStrokeWidth > 0.0f && strokeColor.alpha > 0) {
                        Microsoft::WRL::ComPtr<ID2D1Brush> strokeBrush
                            = paintBrush(strokePaint, groupRect, strokeColor);
                        strokeBrush->SetOpacity(barOpacity);
                        context->DrawRoundedRectangle(
                            rect, strokeBrush.Get(), volumeDecorStrokeWidth
                        );
                    }
                    Microsoft::WRL::ComPtr<ID2D1Brush> fillBrush = paintBrush(
                        fillPaint, groupRect, fillColor
                    );
                    fillBrush->SetOpacity(barOpacity);
                    context->FillRoundedRectangle(rect, fillBrush.Get());
                } else {
                    Microsoft::WRL::ComPtr<ID2D1Brush> fill =
                        overlay ? overlayFill : normalFill;
                    Microsoft::WRL::ComPtr<ID2D1Brush> stroke =
                        overlay ? overlayStroke : normalStroke;
                    fill->SetOpacity(barOpacity);
                    stroke->SetOpacity(barOpacity);
                    const RgbaColor &strokeColor = overlay
                        ? style.volumeOverlayStroke
                        : style.volumeStroke;
                    const float volumeStrokeWidth = style.volumeEnabled
                        ? style.volumeStrokeWidth
                        : style.litStrokeWidth;
                    context->FillRoundedRectangle(rect, fill.Get());
                    if (volumeStrokeWidth > 0.0f && strokeColor.alpha > 0) {
                        context->DrawRoundedRectangle(
                            rect, stroke.Get(), volumeStrokeWidth
                        );
                    }
                }
                if (transformed) {
                    context->SetTransform(withViewport(base));
                }
            };
            for (int index = signalState.activeIndex + 1;
                 index < signalGeometry.count;
                 ++index) {
                drawColumn(index, false);
            }
            for (int index = 0; index <= signalState.activeIndex; ++index) {
                drawColumn(index, true);
            }
        }
        if (shapeState.visible
            && shapeState.activeIndex >= 0
            && style.litOpacity > 0.0f) {
            context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(shapeDx, dy)));
            auto shapeBrush = [&](const RgbaColor &color, float opacity) {
                PaintStyle paint;
                paint.mode = "solid";
                paint.color = color;
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                    paint, line->fillBounds, color
                );
                brush->SetOpacity(std::clamp(opacity, 0.0f, 1.0f));
                return brush;
            };
            auto drawRawShape = [&](const D2D1_RECT_F &rect,
                                    const RgbaColor &fillColor,
                                    const RgbaColor &strokeColor,
                                    float strokeWidth,
                                    float opacity) {
                auto fill = shapeBrush(fillColor, opacity);
                auto stroke = shapeBrush(strokeColor, opacity);
                if (style.litStyle == "square") {
                    context->FillRectangle(rect, fill.Get());
                    if (strokeWidth > 0.0f && strokeColor.alpha > 0) {
                        context->DrawRectangle(rect, stroke.Get(), strokeWidth);
                    }
                } else if (style.litStyle == "rounded") {
                    const float radius = std::max(
                        (rect.right - rect.left) * 0.22f, 1.0f
                    );
                    const D2D1_ROUNDED_RECT rounded = D2D1::RoundedRect(
                        rect, radius, radius
                    );
                    context->FillRoundedRectangle(rounded, fill.Get());
                    if (strokeWidth > 0.0f && strokeColor.alpha > 0) {
                        context->DrawRoundedRectangle(
                            rounded, stroke.Get(), strokeWidth
                        );
                    }
                } else {
                    const D2D1_ELLIPSE ellipse = D2D1::Ellipse(
                        D2D1::Point2F(
                            (rect.left + rect.right) * 0.5f,
                            (rect.top + rect.bottom) * 0.5f
                        ),
                        (rect.right - rect.left) * 0.5f,
                        (rect.bottom - rect.top) * 0.5f
                    );
                    context->FillEllipse(ellipse, fill.Get());
                    if (strokeWidth > 0.0f && strokeColor.alpha > 0) {
                        context->DrawEllipse(ellipse, stroke.Get(), strokeWidth);
                    }
                }
            };
            // 形状灯「图片」模式：共用填充图缓存池（path+mtime+size 键控）。
            const bool litImageMode = style.litStyle == "image";
            ID2D1Bitmap1 *litImageBitmap = nullptr;
            if (litImageMode) {
                const auto imageFound = std::find_if(
                    impl_->images.begin(), impl_->images.end(),
                    [&](const Impl::CachedImage &image) {
                        return image.path == style.litImagePath
                            && image.modifiedMs == style.litImageModifiedMs
                            && image.size == style.litImageSize;
                    }
                );
                if (imageFound != impl_->images.end()) {
                    litImageBitmap = imageFound->bitmap.Get();
                }
            }
            for (int index = 0; index <= shapeState.activeIndex; ++index) {
                const bool active = index == shapeState.activeIndex;
                // lineAnimationOpacity 在逐笔刷兜底路径里承载行入退场动画
                // 透明度（图层可用时为 1），与音量柱 signalBrush 同口径。
                const float itemOpacity = style.litOpacity
                    * (active ? shapeState.activeOpacity : 1.0f)
                    * lineAnimationOpacity;
                const float itemX = style.litOffsetX
                    + static_cast<float>(index)
                        * (shapeGeometry.size * 1.5f + shapeGeometry.tracking)
                    + (active ? shapeState.dx : 0.0f);
                const float itemY = shapeGroupY + (active ? shapeState.dy : 0.0f);
                const D2D1_RECT_F rect = D2D1::RectF(
                    itemX,
                    itemY,
                    itemX + shapeGeometry.size,
                    itemY + shapeGeometry.size
                );
                if (litImageMode) {
                    // 图片 contain 进 size 方形槽位（Painter _draw_lit_image
                    // 同口径）；描边/柔化/阴影/边缘亮度为矢量形状专属。
                    // 缺图/解码失败回退圆形（drawRawShape 的 ellipse 分支）。
                    if (litImageBitmap != nullptr) {
                        const D2D1_SIZE_F dim = litImageBitmap->GetSize();
                        if (dim.width > 0.0f && dim.height > 0.0f) {
                            const float fit = std::min(
                                (rect.right - rect.left) / dim.width,
                                (rect.bottom - rect.top) / dim.height
                            );
                            const float drawW = dim.width * fit;
                            const float drawH = dim.height * fit;
                            const float centerX = (rect.left + rect.right) * 0.5f;
                            const float centerY = (rect.top + rect.bottom) * 0.5f;
                            context->DrawBitmap(
                                litImageBitmap,
                                D2D1::RectF(
                                    centerX - drawW * 0.5f,
                                    centerY - drawH * 0.5f,
                                    centerX + drawW * 0.5f,
                                    centerY + drawH * 0.5f
                                ),
                                std::clamp(itemOpacity, 0.0f, 1.0f)
                            );
                            continue;
                        }
                    }
                    drawRawShape(
                        rect,
                        style.litFill,
                        style.litStroke,
                        style.litStrokeWidth,
                        itemOpacity
                    );
                    continue;
                }
                if (style.litShadow) {
                    const float shadowOffset = std::max(
                        shapeGeometry.size * 0.08f, 1.0f
                    );
                    drawRawShape(
                        D2D1::RectF(
                            rect.left + shadowOffset,
                            rect.top + shadowOffset,
                            rect.right + shadowOffset,
                            rect.bottom + shadowOffset
                        ),
                        RgbaColor{0, 0, 0, 89},
                        RgbaColor{0, 0, 0, 0},
                        0.0f,
                        itemOpacity
                    );
                }
                if (style.litStrokeSoften > 0.0f
                    && style.litStrokeWidth > 0.0f) {
                    RgbaColor softStroke = style.litStroke;
                    softStroke.alpha = 71;
                    drawRawShape(
                        rect,
                        style.litFill,
                        softStroke,
                        style.litStrokeWidth + style.litStrokeSoften,
                        itemOpacity
                    );
                }
                drawRawShape(
                    rect,
                    style.litFill,
                    style.litStroke,
                    style.litStrokeWidth,
                    itemOpacity
                );
                if (active && style.litEdgeBrightness > 0.0f) {
                    const float inset = shapeGeometry.size * 0.18f;
                    const D2D1_ELLIPSE highlight = D2D1::Ellipse(
                        D2D1::Point2F(
                            rect.left + inset + shapeGeometry.size * 0.16f,
                            rect.top + inset + shapeGeometry.size * 0.16f
                        ),
                        shapeGeometry.size * 0.16f,
                        shapeGeometry.size * 0.16f
                    );
                    auto brush = shapeBrush(
                        RgbaColor{255, 255, 255, 255},
                        itemOpacity * std::min(
                            style.litEdgeBrightness * 0.55f, 1.0f
                        )
                    );
                    context->FillEllipse(highlight, brush.Get());
                }
            }
        }
        // The layer must close before EndDraw; a Direct2D layer cannot outlive
        // the draw it was pushed in.
        lineOpacityLayer.pop();
        endDrawMeasured(
            "ID2D1DeviceContext::EndDraw(frame layers)",
            frameDiagnostics.endDrawFrameLayersMs,
            frameDiagnostics.endDrawFrameLayersCount
        );
        renderedAnyLine = true;
        for (MainGlowLayer &layer : mainGlowLayers) {
            layer.blur->SetInput(0, nullptr);
        }
        for (RubyGlowLayer &layer : rubyGlowLayers) {
            layer.blur->SetInput(0, nullptr);
        }
        for (InlineGlowLayer &layer : inlineGlowLayers) {
            layer.blur->SetInput(0, nullptr);
        }
        // This line's composite is flushed; scratches can serve the next line.
        // Bursts (e.g. whole-line utopia outros) may allocate past the cap;
        // those extra entries are released here so steady state keeps at most
        // the cap's worth of scene-sized scratch memory resident.
        constexpr std::size_t kGlowPoolCap = 8;
        impl_->glowScratchInUse = 0;
        impl_->glowEffectInUse = 0;
        if (impl_->glowScratchPool.size() > kGlowPoolCap) {
            impl_->glowScratchPool.resize(kGlowPoolCap);
        }
        if (impl_->glowEffectPool.size() > kGlowPoolCap) {
            impl_->glowEffectPool.resize(kGlowPoolCap);
        }
        // Bitmap-guide decor effects bind fresh inputs on every draw, so
        // rewinding the counters is enough to reuse them on the next line.
        impl_->decorTintEffectInUse = 0;
        impl_->decorBlurEffectInUse = 0;
        impl_->decorCompositeEffectInUse = 0;
        if (impl_->decorTintEffectPool.size() > kGlowPoolCap) {
            impl_->decorTintEffectPool.resize(kGlowPoolCap);
        }
        if (impl_->decorBlurEffectPool.size() > kGlowPoolCap) {
            impl_->decorBlurEffectPool.resize(kGlowPoolCap);
        }
        if (impl_->decorCompositeEffectPool.size() > kGlowPoolCap) {
            impl_->decorCompositeEffectPool.resize(kGlowPoolCap);
        }
      }
    }

    if (!renderedAnyLine) {
        context->SetTarget(targetBitmap);
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        context->Clear(D2D1::ColorF(0.0f, 0.0f));
        endDrawMeasured(
            "ID2D1DeviceContext::EndDraw(empty frame)",
            frameDiagnostics.endDrawEmptyFrameMs,
            frameDiagnostics.endDrawEmptyFrameCount
        );
    }

    context->SetTarget(nullptr);
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    const double renderMs = elapsedMs(renderStart);

    if (!readback) {
        ProbeResult result;
        result.renderMs = renderMs;
        result.surface.width = scene.width;
        result.surface.height = scene.height;
        result.surface.stride = scene.width * 4;
        result.surface.pixelFormat = PixelFormat::Bgra8888Premultiplied;
        finalizeDiagnostics(result);
        return result;
    }

    const auto readbackStart = Clock::now();
    ID3D11Texture2D *stagingTexture = impl_->frameStagingTexture.Get();
    const int fixedCropTop = std::clamp(
        scene.exportCropTop, 0, std::max(scene.height - 1, 0)
    );
    const int fixedCropHeight = std::clamp(
        scene.exportCropHeight,
        0,
        std::max(scene.height - fixedCropTop, 0)
    );
    const bool fixedCrop = !compactBands
        && fixedCropHeight > 0
        && (fixedCropTop > 0 || fixedCropHeight < scene.height);
    std::vector<std::pair<int, int>> fixedBands;
    int fixedBandsHeight = 0;
    if (!compactBands) {
        fixedBands.reserve(scene.exportBands.size());
        for (const auto &[rawTop, rawHeight] : scene.exportBands) {
            const int top = std::clamp(rawTop, 0, std::max(scene.height - 1, 0));
            const int height = std::clamp(
                rawHeight, 0, std::max(scene.height - top, 0)
            );
            if (height > 0) {
                fixedBands.emplace_back(top, height);
                fixedBandsHeight += height;
            }
        }
    }
    std::vector<std::pair<int, int>> mergedIntervals;
    if (compactBands) {
        std::sort(readbackIntervals.begin(), readbackIntervals.end());
        for (const auto &interval : readbackIntervals) {
            if (mergedIntervals.empty()
                || interval.first > mergedIntervals.back().second + 2) {
                mergedIntervals.push_back(interval);
            } else {
                mergedIntervals.back().second = std::max(
                    mergedIntervals.back().second, interval.second
                );
            }
        }
    }
    if (compactBands && mergedIntervals.empty()) {
        ProbeResult result;
        result.renderMs = renderMs;
        result.readbackMs = elapsedMs(readbackStart);
        result.surface.width = scene.width;
        result.surface.height = scene.height;
        result.surface.stride = scene.width * 4;
        result.surface.pixelFormat = PixelFormat::Bgra8888Premultiplied;
        finalizeDiagnostics(result);
        return result;
    }
    if (compactBands) {
        int packedTop = 0;
        for (const auto &[top, bottom] : mergedIntervals) {
            D3D11_BOX sourceBox{};
            sourceBox.left = 0;
            sourceBox.right = static_cast<UINT>(scene.width);
            sourceBox.top = static_cast<UINT>(top);
            sourceBox.bottom = static_cast<UINT>(bottom);
            sourceBox.front = 0;
            sourceBox.back = 1;
            device_.d3dContext()->CopySubresourceRegion(
                stagingTexture,
                0,
                0,
                static_cast<UINT>(packedTop),
                0,
                targetTexture,
                0,
                &sourceBox
            );
            packedTop += bottom - top;
        }
    } else if (!fixedBands.empty()) {
        int packedTop = 0;
        for (const auto &[top, height] : fixedBands) {
            D3D11_BOX sourceBox{};
            sourceBox.left = 0;
            sourceBox.right = static_cast<UINT>(scene.width);
            sourceBox.top = static_cast<UINT>(top);
            sourceBox.bottom = static_cast<UINT>(top + height);
            sourceBox.front = 0;
            sourceBox.back = 1;
            device_.d3dContext()->CopySubresourceRegion(
                stagingTexture,
                0,
                0,
                static_cast<UINT>(packedTop),
                0,
                targetTexture,
                0,
                &sourceBox
            );
            packedTop += height;
        }
    } else if (fixedCrop) {
        D3D11_BOX sourceBox{};
        sourceBox.left = 0;
        sourceBox.right = static_cast<UINT>(scene.width);
        sourceBox.top = static_cast<UINT>(fixedCropTop);
        sourceBox.bottom = static_cast<UINT>(fixedCropTop + fixedCropHeight);
        sourceBox.front = 0;
        sourceBox.back = 1;
        device_.d3dContext()->CopySubresourceRegion(
            stagingTexture,
            0,
            0,
            0,
            0,
            targetTexture,
            0,
            &sourceBox
        );
    } else {
        device_.d3dContext()->CopyResource(stagingTexture, targetTexture);
    }
    D3D11_MAPPED_SUBRESOURCE mapped{};
    const auto gpuWaitStart = Clock::now();
    checkHr(
        device_.d3dContext()->Map(stagingTexture, 0, D3D11_MAP_READ, 0, &mapped),
        "ID3D11DeviceContext::Map(frame)",
        device_
    );
    frameDiagnostics.gpuWaitMs = elapsedMs(gpuWaitStart);

    ProbeResult result;
    result.renderMs = renderMs;
    result.surface.width = scene.width;
    result.surface.height = !fixedBands.empty()
        ? fixedBandsHeight
        : (fixedCrop ? fixedCropHeight : scene.height);
    result.surface.stride = scene.width * 4;
    result.surface.pixelFormat = PixelFormat::Bgra8888Premultiplied;
    int packedHeight = !fixedBands.empty()
        ? fixedBandsHeight
        : (fixedCrop ? fixedCropHeight : scene.height);
    if (compactBands) {
        packedHeight = 0;
        for (const auto &[top, bottom] : mergedIntervals) {
            result.surface.bands.push_back(RenderSurface::Band{
                top,
                bottom - top,
                packedHeight,
            });
            packedHeight += bottom - top;
        }
    }
    result.surface.bytes.resize(
        static_cast<std::size_t>(result.surface.stride) * packedHeight
    );
    const auto readbackCopyStart = Clock::now();
    for (int y = 0; y < packedHeight; ++y) {
        const auto *source = static_cast<const std::uint8_t *>(mapped.pData)
            + static_cast<std::size_t>(mapped.RowPitch) * y;
        auto *destination = result.surface.bytes.data()
            + static_cast<std::size_t>(result.surface.stride) * y;
        std::memcpy(destination, source, static_cast<std::size_t>(result.surface.stride));
    }
    frameDiagnostics.readbackCopyMs = elapsedMs(readbackCopyStart);
    device_.d3dContext()->Unmap(stagingTexture, 0);
    result.readbackMs = elapsedMs(readbackStart);
    finalizeDiagnostics(result);
    return result;
}

NativePreviewResult Direct2DGpuBackend::presentFrame(
    int tMs,
    const NativePreviewTarget &target
) {
    const auto rendered = renderFrameInternal(tMs, false, false);
    return previewSurface_.present(
        device_.d3dDevice(),
        device_.d3dContext(),
        impl_->frameTargetTexture.Get(),
        rendered.renderMs,
        target
    );
}

void Direct2DGpuBackend::closeNativePreview() {
    previewSurface_.close();
}

}  // namespace krok::subtitle::native
