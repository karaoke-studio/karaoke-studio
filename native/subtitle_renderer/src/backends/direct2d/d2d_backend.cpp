#include "d2d_backend.h"
#include "d2d_backend_internal.h"
#include "d2d_runtime_support.h"

#include <d2d1_2.h>
#include <d2d1helper.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <mutex>
#include <thread>

namespace krok::subtitle::native {
namespace {

using Clock = direct2d::RuntimeClock;
using direct2d::checkHr;
using direct2d::elapsedMs;
using direct2d::environmentFlagEnabled;
using direct2d::unpremultiply;

}  // namespace

Direct2DGpuBackend::Direct2DGpuBackend(bool forceWarp)
    : device_(forceWarp), impl_(std::make_unique<Impl>()) {
    if (forceWarp && !environmentFlagEnabled("KROK_GPU_REALIZATION_WARP", false)) {
        impl_->realizationEnabled = false;
    }
    impl_->realizationActive = impl_->realizationEnabled;
    impl_->diagnostics.countersEnabled = impl_->countersEnabled;
    impl_->diagnostics.resourceCacheEnabled = impl_->resourceCacheEnabled;
    impl_->diagnostics.brushCacheCapacity = Impl::brushCapacity;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
    impl_->diagnostics.realizationCapacity = Impl::defaultRealizationCapacity;
    impl_->diagnostics.glowDirtyRectEnabled = impl_->glowDirtyRectEnabled;
    if (impl_->realizationEnabled) {
        device_.d2dContext()->QueryInterface(IID_PPV_ARGS(
            impl_->realizationContext.ReleaseAndGetAddressOf()
        ));
    }
    impl_->diagnostics.realizationSupported =
        impl_->realizationContext != nullptr;
}

Direct2DGpuBackend::Direct2DGpuBackend(
    bool forceWarp,
    std::shared_ptr<D2DDeviceResources> sharedDeviceResources
)
    : device_(std::move(sharedDeviceResources)), impl_(std::make_unique<Impl>()) {
    if (forceWarp && !environmentFlagEnabled("KROK_GPU_REALIZATION_WARP", false)) {
        impl_->realizationEnabled = false;
    }
    impl_->realizationActive = impl_->realizationEnabled;
    impl_->diagnostics.countersEnabled = impl_->countersEnabled;
    impl_->diagnostics.resourceCacheEnabled = impl_->resourceCacheEnabled;
    impl_->diagnostics.brushCacheCapacity = Impl::brushCapacity;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
    impl_->diagnostics.realizationCapacity = Impl::defaultRealizationCapacity;
    impl_->diagnostics.glowDirtyRectEnabled = impl_->glowDirtyRectEnabled;
    if (impl_->realizationEnabled) {
        device_.d2dContext()->QueryInterface(IID_PPV_ARGS(
            impl_->realizationContext.ReleaseAndGetAddressOf()
        ));
    }
    impl_->diagnostics.realizationSupported =
        impl_->realizationContext != nullptr;
}

Direct2DGpuBackend::~Direct2DGpuBackend() {
    if (impl_->realizationControl) {
        impl_->realizationControl->stop.store(true, std::memory_order_release);
    }
    for (Impl::RetiredRealizationWorker &worker
         : impl_->retiredRealizationWorkers) {
        worker.control->stop.store(true, std::memory_order_release);
    }
    if (impl_->realizationThread.joinable()) {
        impl_->realizationThread.join();
    }
    for (Impl::RetiredRealizationWorker &worker
         : impl_->retiredRealizationWorkers) {
        if (worker.thread.joinable()) {
            worker.thread.join();
        }
    }
}

std::shared_ptr<D2DDeviceResources>
Direct2DGpuBackend::sharedDeviceResources() const noexcept {
    return device_.sharedResources();
}

void Direct2DGpuBackend::waitForRealizationPrewarm() {
    if (impl_->realizationThread.joinable()) {
        impl_->realizationThread.join();
    }
}

void Direct2DGpuBackend::cancelRealizationPrewarm() {
    if (impl_->realizationControl) {
        impl_->realizationControl->stop.store(true, std::memory_order_release);
    }
    for (Impl::RetiredRealizationWorker &worker
         : impl_->retiredRealizationWorkers) {
        worker.control->stop.store(true, std::memory_order_release);
    }
    if (impl_->realizationThread.joinable()) {
        impl_->realizationThread.join();
    }
    for (Impl::RetiredRealizationWorker &worker
         : impl_->retiredRealizationWorkers) {
        if (worker.thread.joinable()) {
            worker.thread.join();
        }
    }
    impl_->retiredRealizationWorkers.clear();
}

void Direct2DGpuBackend::adoptSharedGlyphResources(
    const Direct2DGpuBackend &source
) {
    if (device_.d2dDevice() != source.device_.d2dDevice()) {
        throw BackendError("shared glyph resources require one Direct2D device");
    }
    std::scoped_lock lock(impl_->realizationMutex, source.impl_->realizationMutex);
    RenderScene comparableScene = impl_->scene;
    comparableScene.realizationEnabled = source.impl_->scene.realizationEnabled;
    if (!impl_->configured || !source.impl_->configured
        || !(comparableScene == source.impl_->scene)) {
        throw BackendError("shared glyph resources require identical configured scenes");
    }
    impl_->scene.realizationEnabled = source.impl_->scene.realizationEnabled;
    if (!source.impl_->realizationPrewarmComplete.load(std::memory_order_acquire)) {
        throw BackendError("shared glyph resources are not fully prewarmed");
    }
    impl_->lines = source.impl_->lines;
    impl_->realizationActive = source.impl_->realizationActive
        && impl_->realizationContext != nullptr;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
    // COM geometry and realization objects are shared by AddRef through the
    // copied cache. Only the source worker owns/prepared the resource set, so
    // follower diagnostics must not multiply its count or preparation cost.
    impl_->realizationCount = 0;
    impl_->diagnostics.realizationCapacity = 0;
    impl_->diagnostics.realizationPrewarmTasks = 0;
    impl_->diagnostics.realizationPrewarmSkipped = 0;
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
    impl_->realizationPrewarmComplete.store(true, std::memory_order_release);
}

BackendCaps Direct2DGpuBackend::capabilities() const {
    return device_.capabilities();
}

BackendDiagnostics Direct2DGpuBackend::diagnostics() const {
    std::lock_guard<std::mutex> realizationLock(impl_->realizationMutex);
    BackendDiagnostics result = impl_->diagnostics;
    result.realizationPrewarmComplete = impl_->realizationPrewarmComplete.load(
        std::memory_order_acquire
    );
    result.brushCacheSize = impl_->brushes.size();
    result.estimatedCacheBytes += impl_->brushes.size()
        * sizeof(Impl::CachedBrush);
    result.realizationCount = impl_->realizationCount;
    result.estimatedCacheBytes += impl_->realizationCount * 512;
    for (const Impl::GlowScratch &scratch : impl_->glowScratchPool) {
        result.estimatedCacheBytes += static_cast<std::uint64_t>(scratch.width)
            * static_cast<std::uint64_t>(scratch.height) * 4;
    }
    device_.appendVideoMemoryDiagnostics(&result);
    return result;
}

ProbeResult Direct2DGpuBackend::renderProbe(const ProbeOptions &options) {
    if (options.width <= 0 || options.height <= 0 || options.width > 8192 || options.height > 8192) {
        throw BackendError("render probe dimensions must be within 1..8192");
    }

    D3D11_TEXTURE2D_DESC targetDesc{};
    targetDesc.Width = static_cast<UINT>(options.width);
    targetDesc.Height = static_cast<UINT>(options.height);
    targetDesc.MipLevels = 1;
    targetDesc.ArraySize = 1;
    targetDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    targetDesc.SampleDesc.Count = 1;
    targetDesc.Usage = D3D11_USAGE_DEFAULT;
    targetDesc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;

    Microsoft::WRL::ComPtr<ID3D11Texture2D> targetTexture;
    checkHr(
        device_.d3dDevice()->CreateTexture2D(&targetDesc, nullptr, targetTexture.ReleaseAndGetAddressOf()),
        "ID3D11Device::CreateTexture2D(target)",
        device_
    );
    Microsoft::WRL::ComPtr<IDXGISurface> targetSurface;
    checkHr(targetTexture.As(&targetSurface), "Query target IDXGISurface", device_);

    const D2D1_BITMAP_PROPERTIES1 bitmapProperties = D2D1::BitmapProperties1(
        D2D1_BITMAP_OPTIONS_TARGET,
        D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED),
        96.0f,
        96.0f
    );
    Microsoft::WRL::ComPtr<ID2D1Bitmap1> targetBitmap;
    checkHr(
        device_.d2dContext()->CreateBitmapFromDxgiSurface(
            targetSurface.Get(),
            &bitmapProperties,
            targetBitmap.ReleaseAndGetAddressOf()
        ),
        "ID2D1DeviceContext::CreateBitmapFromDxgiSurface",
        device_
    );

    const auto renderStart = Clock::now();
    ID2D1DeviceContext *context = device_.d2dContext();
    context->SetTarget(targetBitmap.Get());
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    context->BeginDraw();
    context->Clear(D2D1::ColorF(0.0f, 0.0f));

    Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> brush;
    const D2D1_COLOR_F color = D2D1::ColorF(
        static_cast<float>(options.red) / 255.0f,
        static_cast<float>(options.green) / 255.0f,
        static_cast<float>(options.blue) / 255.0f,
        static_cast<float>(options.alpha) / 255.0f
    );
    checkHr(context->CreateSolidColorBrush(color, brush.ReleaseAndGetAddressOf()), "CreateSolidColorBrush", device_);
    const float left = static_cast<float>(options.width) * 0.125f;
    const float top = static_cast<float>(options.height) * 0.25f;
    const float right = static_cast<float>(options.width) * 0.625f;
    const float bottom = static_cast<float>(options.height) * 0.75f;
    context->FillRectangle(D2D1::RectF(left, top, right, bottom), brush.Get());

    if (options.drawGlyph) {
        Microsoft::WRL::ComPtr<IDWriteTextFormat> textFormat;
        checkHr(
            device_.dwriteFactory()->CreateTextFormat(
                L"Segoe UI",
                nullptr,
                DWRITE_FONT_WEIGHT_BOLD,
                DWRITE_FONT_STYLE_NORMAL,
                DWRITE_FONT_STRETCH_NORMAL,
                std::max(12.0f, static_cast<float>(options.height) * 0.3f),
                L"en-us",
                textFormat.ReleaseAndGetAddressOf()
            ),
            "IDWriteFactory::CreateTextFormat",
            device_
        );
        context->DrawText(
            L"G",
            1,
            textFormat.Get(),
            D2D1::RectF(right, top, static_cast<float>(options.width), bottom),
            brush.Get(),
            D2D1_DRAW_TEXT_OPTIONS_NONE,
            DWRITE_MEASURING_MODE_NATURAL
        );
    }
    checkHr(context->EndDraw(), "ID2D1DeviceContext::EndDraw", device_);
    context->SetTarget(nullptr);
    const double renderMs = elapsedMs(renderStart);

    const auto readbackStart = Clock::now();
    D3D11_TEXTURE2D_DESC stagingDesc = targetDesc;
    stagingDesc.Usage = D3D11_USAGE_STAGING;
    stagingDesc.BindFlags = 0;
    stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> stagingTexture;
    checkHr(
        device_.d3dDevice()->CreateTexture2D(&stagingDesc, nullptr, stagingTexture.ReleaseAndGetAddressOf()),
        "ID3D11Device::CreateTexture2D(staging)",
        device_
    );
    device_.d3dContext()->CopyResource(stagingTexture.Get(), targetTexture.Get());

    D3D11_MAPPED_SUBRESOURCE mapped{};
    checkHr(
        device_.d3dContext()->Map(stagingTexture.Get(), 0, D3D11_MAP_READ, 0, &mapped),
        "ID3D11DeviceContext::Map",
        device_
    );

    ProbeResult result;
    result.renderMs = renderMs;
    result.surface.width = options.width;
    result.surface.height = options.height;
    result.surface.stride = options.width * 4;
    result.surface.pixelFormat = PixelFormat::Rgba8888Straight;
    result.surface.bytes.resize(static_cast<std::size_t>(result.surface.stride) * options.height);
    for (int y = 0; y < options.height; ++y) {
        const auto *source = static_cast<const std::uint8_t *>(mapped.pData)
            + static_cast<std::size_t>(mapped.RowPitch) * y;
        auto *destination = result.surface.bytes.data()
            + static_cast<std::size_t>(result.surface.stride) * y;
        for (int x = 0; x < options.width; ++x) {
            const std::uint8_t blue = source[x * 4 + 0];
            const std::uint8_t green = source[x * 4 + 1];
            const std::uint8_t red = source[x * 4 + 2];
            const std::uint8_t alpha = source[x * 4 + 3];
            destination[x * 4 + 0] = unpremultiply(red, alpha);
            destination[x * 4 + 1] = unpremultiply(green, alpha);
            destination[x * 4 + 2] = unpremultiply(blue, alpha);
            destination[x * 4 + 3] = alpha;
        }
    }
    device_.d3dContext()->Unmap(stagingTexture.Get(), 0);
    result.readbackMs = elapsedMs(readbackStart);
    return result;
}

}  // namespace krok::subtitle::native
