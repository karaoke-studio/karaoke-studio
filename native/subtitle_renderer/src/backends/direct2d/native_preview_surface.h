#pragma once

#include "../render_backend.h"

#include <d3d11.h>
#include <dcomp.h>
#include <dxgi1_3.h>
#include <windows.h>
#include <wrl/client.h>

namespace krok::subtitle::native {

// pump_native_preview 命令的底层入口：只派发 DComp 子窗口自己的消息
// （见 NativePreviewSurface::pumpMessages），暂停/空闲时由 Python worker
// 心跳驱动。经 Direct2DGpuBackend::pumpNativePreviewMessages 调用。

class NativePreviewSurface {
public:
    NativePreviewSurface() = default;
    ~NativePreviewSurface();

    NativePreviewResult present(
        ID3D11Device *device,
        ID3D11DeviceContext *context,
        ID3D11Texture2D *source,
        double renderMs,
        const NativePreviewTarget &target
    );
    void pumpMessages() noexcept;
    void close() noexcept;

private:
    void ensureWindow(const NativePreviewTarget &target);
    void ensureSwapChain(ID3D11Device *device, int width, int height);
    void resizeSwapChain(int width, int height);

    HWND window_ = nullptr;
    HWND parentWindow_ = nullptr;
    int width_ = 0;
    int height_ = 0;
    // 最近一次实际下发的子窗口矩形（客户区物理像素）。present 每帧都会调
    // ensureWindow——几何未变时跳过 SetWindowPos，避免按呈现节拍做窗口管
    // 理操作（低端 DWM 上与视频呈现层互相搅动 = 频闪，2026-10）。-1 表示
    // 尚未放置（窗口刚建 / close 后重建）。
    int placedX_ = -1;
    int placedY_ = -1;
    int placedWidth_ = -1;
    int placedHeight_ = -1;
    Microsoft::WRL::ComPtr<IDXGISwapChain1> swapChain_;
    Microsoft::WRL::ComPtr<IDCompositionDevice> compositionDevice_;
    Microsoft::WRL::ComPtr<IDCompositionTarget> compositionTarget_;
    Microsoft::WRL::ComPtr<IDCompositionVisual> compositionVisual_;
};

}  // namespace krok::subtitle::native
