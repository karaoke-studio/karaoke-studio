#include "gpu/gpu_session.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkPaint.h"
#include "include/effects/SkRuntimeEffect.h"
#include "include/gpu/GpuTypes.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include "include/gpu/ganesh/SkSurfaceGanesh.h"
#include <stdexcept>
#include <thread>

namespace subtitle::skia {
GpuSession::GpuSession() = default;
GpuSession::~GpuSession() = default;
sk_sp<SkSurface> GpuSession::makeSurface(int width, int height) {
    auto surface = SkSurfaces::RenderTarget(context_.get(), skgpu::Budgeted::kYes,
        SkImageInfo::Make(width, height, kRGBA_8888_SkColorType, kPremul_SkAlphaType,
                          SkColorSpace::MakeSRGB()), 0, kTopLeft_GrSurfaceOrigin, nullptr);
    if (!surface) throw GpuDeviceError("Ganesh render target creation failed");
    return surface;
}
void GpuSession::finish(SkSurface& surface) {
    context_->flushAndSubmit(&surface, GrSyncCpu::kYes);
    checkDevice();
}

void GpuSession::checkDevice() const {
    if (context_->abandoned()) throw GpuDeviceError("Ganesh GPU device lost");
}

void GpuSession::readStraightAsync(SkSurface& source, SkSurface& target, AsyncStraightRead& read) {
    // The same expression Ganesh applies before a synchronous straight RGBA
    // readPixels of an 8-bit premultiplied target, so both readbacks agree.
    static const auto unpremul = [] {
        auto effect = SkRuntimeEffect::MakeForColorFilter(SkString(
            "half4 main(half4 halfColor) {"
                "float4 color = float4(halfColor);"
                "color = floor(color * 255 + 0.5) / 255;"
                "color.rgb = color.a <= 0 ? half3(0) : floor(color.rgb / color.a * 255 + 0.5) / 255;"
                "return color;"
            "}"));
        if (!effect.effect) throw std::runtime_error("Skia 反预乘回读滤镜无法编译");
        return effect.effect->makeColorFilter(nullptr);
    }();
    read = {};
    {
        auto image = source.makeImageSnapshot();
        if (!image) throw GpuDeviceError("Skia GPU 帧快照失败");
        SkPaint paint;
        paint.setBlendMode(SkBlendMode::kSrc);
        paint.setColorFilter(unpremul);
        // The straight values are stored unchanged in a premultiplied target
        // and transferred without another conversion.
        target.getCanvas()->drawImage(image, 0, 0, SkSamplingOptions(), &paint);
    }
    const auto info = target.imageInfo();
    target.asyncRescaleAndReadPixels(info, info.bounds(), SkSurface::RescaleGamma::kSrc,
        SkSurface::RescaleMode::kNearest,
        [](SkSurface::ReadPixelsContext context, std::unique_ptr<const SkSurface::AsyncReadResult> result) {
            auto& pending = *static_cast<AsyncStraightRead*>(context);
            pending.result = std::move(result);
            pending.done = true;
        }, &read);
    context_->submit(GrSyncCpu::kNo);
    checkDevice();
}

void GpuSession::wait(AsyncStraightRead& read) {
    while (!read.done) {
        context_->checkAsyncWorkCompletion();
        if (read.done) break;
        checkDevice();
        std::this_thread::yield();
    }
    checkDevice();
}

void GpuSession::drain() {
    context_->submit(GrSyncCpu::kYes);
    context_->checkAsyncWorkCompletion();
}
}
