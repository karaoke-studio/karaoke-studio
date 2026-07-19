#include "native_preview_surface.h"

#include <chrono>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace krok::subtitle::native {
namespace {

constexpr wchar_t kWindowClassName[] = L"KrokSubtitleNativePreview";

std::string hresultText(const char *operation, HRESULT value) {
    std::ostringstream stream;
    stream << operation << " failed (HRESULT=0x" << std::uppercase << std::hex
           << static_cast<unsigned long>(value) << ")";
    return stream.str();
}

void checkHr(HRESULT value, const char *operation) {
    if (FAILED(value)) {
        throw BackendError(hresultText(operation, value));
    }
}

LRESULT CALLBACK previewWindowProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    if (message == WM_NCHITTEST) {
        return HTTRANSPARENT;
    }
    if (message == WM_ERASEBKGND) {
        return 1;
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

ATOM ensureWindowClass() {
    static const ATOM atom = [] {
        WNDCLASSEXW windowClass{};
        windowClass.cbSize = sizeof(windowClass);
        windowClass.lpfnWndProc = previewWindowProc;
        windowClass.hInstance = GetModuleHandleW(nullptr);
        windowClass.lpszClassName = kWindowClassName;
        windowClass.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        const ATOM registered = RegisterClassExW(&windowClass);
        if (registered == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
            throw BackendError("RegisterClassExW(native preview) failed");
        }
        return registered;
    }();
    return atom;
}

void pumpWindowMessages() noexcept {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
}

}  // namespace

NativePreviewSurface::~NativePreviewSurface() {
    close();
}

void NativePreviewSurface::ensureWindow(const NativePreviewTarget &target) {
    auto *parent = reinterpret_cast<HWND>(target.parentWindow);
    if (parent == nullptr || !IsWindow(parent)) {
        throw BackendError("native preview parent HWND is invalid");
    }
    if (target.width <= 0 || target.height <= 0) {
        throw BackendError("native preview dimensions must be positive");
    }
    if (window_ != nullptr && parentWindow_ != parent) {
        close();
    }
    if (window_ == nullptr) {
        ensureWindowClass();
        window_ = CreateWindowExW(
            WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_NOREDIRECTIONBITMAP,
            kWindowClassName,
            L"",
            WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
            target.x,
            target.y,
            target.width,
            target.height,
            parent,
            nullptr,
            GetModuleHandleW(nullptr),
            nullptr
        );
        if (window_ == nullptr) {
            throw BackendError("CreateWindowExW(native preview) failed");
        }
        parentWindow_ = parent;
    }
    if (!SetWindowPos(
            window_, HWND_TOP, target.x, target.y, target.width, target.height,
            SWP_NOACTIVATE | SWP_SHOWWINDOW)) {
        throw BackendError("SetWindowPos(native preview) failed");
    }
    pumpWindowMessages();
}

void NativePreviewSurface::ensureSwapChain(ID3D11Device *device, int width, int height) {
    if (swapChain_ != nullptr) {
        if (width_ != width || height_ != height) {
            resizeSwapChain(width, height);
        }
        return;
    }
    Microsoft::WRL::ComPtr<IDXGIDevice> dxgiDevice;
    checkHr(device->QueryInterface(IID_PPV_ARGS(dxgiDevice.ReleaseAndGetAddressOf())), "Query IDXGIDevice(native preview)");
    Microsoft::WRL::ComPtr<IDXGIAdapter> adapter;
    checkHr(dxgiDevice->GetAdapter(adapter.ReleaseAndGetAddressOf()), "IDXGIDevice::GetAdapter(native preview)");
    Microsoft::WRL::ComPtr<IDXGIFactory2> factory;
    checkHr(adapter->GetParent(IID_PPV_ARGS(factory.ReleaseAndGetAddressOf())), "IDXGIAdapter::GetParent(native preview)");

    DXGI_SWAP_CHAIN_DESC1 description{};
    description.Width = static_cast<UINT>(width);
    description.Height = static_cast<UINT>(height);
    description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    description.Stereo = FALSE;
    description.SampleDesc.Count = 1;
    description.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    description.BufferCount = 2;
    description.Scaling = DXGI_SCALING_STRETCH;
    description.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
    description.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED;
    checkHr(
        factory->CreateSwapChainForComposition(
            device,
            &description,
            nullptr,
            swapChain_.ReleaseAndGetAddressOf()
        ),
        "IDXGIFactory2::CreateSwapChainForComposition"
    );

    checkHr(
        DCompositionCreateDevice(
            dxgiDevice.Get(),
            __uuidof(IDCompositionDevice),
            reinterpret_cast<void **>(compositionDevice_.ReleaseAndGetAddressOf())
        ),
        "DCompositionCreateDevice"
    );
    checkHr(
        compositionDevice_->CreateTargetForHwnd(
            window_, TRUE, compositionTarget_.ReleaseAndGetAddressOf()
        ),
        "IDCompositionDevice::CreateTargetForHwnd"
    );
    checkHr(
        compositionDevice_->CreateVisual(compositionVisual_.ReleaseAndGetAddressOf()),
        "IDCompositionDevice::CreateVisual"
    );
    checkHr(compositionVisual_->SetContent(swapChain_.Get()), "IDCompositionVisual::SetContent");
    checkHr(compositionTarget_->SetRoot(compositionVisual_.Get()), "IDCompositionTarget::SetRoot");
    checkHr(compositionDevice_->Commit(), "IDCompositionDevice::Commit");
    width_ = width;
    height_ = height;
}

void NativePreviewSurface::resizeSwapChain(int width, int height) {
    checkHr(
        swapChain_->ResizeBuffers(
            2,
            static_cast<UINT>(width),
            static_cast<UINT>(height),
            DXGI_FORMAT_B8G8R8A8_UNORM,
            0
        ),
        "IDXGISwapChain1::ResizeBuffers(native preview)"
    );
    width_ = width;
    height_ = height;
}

NativePreviewResult NativePreviewSurface::present(
    ID3D11Device *device,
    ID3D11DeviceContext *context,
    ID3D11Texture2D *source,
    double renderMs,
    const NativePreviewTarget &target
) {
    if (device == nullptr || context == nullptr || source == nullptr) {
        throw BackendError("native preview received an empty D3D resource");
    }
    D3D11_TEXTURE2D_DESC sourceDescription{};
    source->GetDesc(&sourceDescription);
    if (sourceDescription.Width != static_cast<UINT>(target.width)
        || sourceDescription.Height != static_cast<UINT>(target.height)) {
        throw BackendError("native preview target must match the configured GPU texture size");
    }
    ensureWindow(target);
    ensureSwapChain(device, target.width, target.height);

    const auto presentStart = std::chrono::steady_clock::now();
    Microsoft::WRL::ComPtr<ID3D11Texture2D> backBuffer;
    checkHr(
        swapChain_->GetBuffer(0, IID_PPV_ARGS(backBuffer.ReleaseAndGetAddressOf())),
        "IDXGISwapChain1::GetBuffer(native preview)"
    );
    context->CopyResource(backBuffer.Get(), source);
    checkHr(swapChain_->Present(0, 0), "IDXGISwapChain1::Present(native preview)");
    pumpWindowMessages();
    const auto presentEnd = std::chrono::steady_clock::now();

    NativePreviewResult result;
    result.renderMs = renderMs;
    result.presentMs = std::chrono::duration<double, std::milli>(
        presentEnd - presentStart
    ).count();
    result.childWindow = reinterpret_cast<std::uintptr_t>(window_);
    return result;
}

void NativePreviewSurface::close() noexcept {
    if (compositionVisual_ != nullptr) {
        compositionVisual_->SetContent(nullptr);
    }
    if (compositionTarget_ != nullptr) {
        compositionTarget_->SetRoot(nullptr);
    }
    if (compositionDevice_ != nullptr) {
        compositionDevice_->Commit();
    }
    compositionVisual_.Reset();
    compositionTarget_.Reset();
    compositionDevice_.Reset();
    swapChain_.Reset();
    width_ = 0;
    height_ = 0;
    if (window_ != nullptr) {
        pumpWindowMessages();
        DestroyWindow(window_);
        window_ = nullptr;
    }
    parentWindow_ = nullptr;
}

}  // namespace krok::subtitle::native
