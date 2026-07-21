#pragma once

#include "../render_backend.h"

#include <d2d1_1.h>
#include <d3d11_4.h>
#include <dwrite.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

namespace krok::subtitle::native {

class D2DDevice {
public:
    explicit D2DDevice(bool forceWarp);

    const BackendCaps &capabilities() const noexcept { return caps_; }
    ID3D11Device *d3dDevice() const noexcept { return d3dDevice_.Get(); }
    ID3D11DeviceContext *d3dContext() const noexcept { return d3dContext_.Get(); }
    ID2D1Factory1 *d2dFactory() const noexcept { return d2dFactory_.Get(); }
    ID2D1Device *d2dDevice() const noexcept { return d2dDevice_.Get(); }
    ID2D1DeviceContext *d2dContext() const noexcept { return d2dContext_.Get(); }
    IDWriteFactory *dwriteFactory() const noexcept { return dwriteFactory_.Get(); }
    void appendVideoMemoryDiagnostics(BackendDiagnostics *diagnostics) const noexcept;
    std::string deviceRemovedReason() const;

private:
    void createD3DDevice(bool forceWarp);
    void createD2DDevice();
    void populateAdapterCaps(bool forceWarp);

    BackendCaps caps_;
    D3D_FEATURE_LEVEL featureLevel_ = D3D_FEATURE_LEVEL_11_0;
    Microsoft::WRL::ComPtr<IDXGIAdapter1> adapter_;
    Microsoft::WRL::ComPtr<ID3D11Device> d3dDevice_;
    Microsoft::WRL::ComPtr<ID3D11DeviceContext> d3dContext_;
    Microsoft::WRL::ComPtr<ID2D1Factory1> d2dFactory_;
    Microsoft::WRL::ComPtr<ID2D1Device> d2dDevice_;
    Microsoft::WRL::ComPtr<ID2D1DeviceContext> d2dContext_;
    Microsoft::WRL::ComPtr<IDWriteFactory> dwriteFactory_;
};

}  // namespace krok::subtitle::native
