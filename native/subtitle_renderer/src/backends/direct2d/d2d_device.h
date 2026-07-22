#pragma once

#include "../render_backend.h"

#include <d2d1_1.h>
#include <d3d11_4.h>
#include <dwrite.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

#include <memory>

namespace krok::subtitle::native {

struct D2DDeviceResources {
    BackendCaps caps;
    D3D_FEATURE_LEVEL featureLevel = D3D_FEATURE_LEVEL_11_0;
    Microsoft::WRL::ComPtr<IDXGIAdapter1> adapter;
    Microsoft::WRL::ComPtr<ID3D11Device> d3dDevice;
    Microsoft::WRL::ComPtr<ID3D11DeviceContext> d3dContext;
    Microsoft::WRL::ComPtr<ID2D1Factory1> d2dFactory;
    Microsoft::WRL::ComPtr<ID2D1Device> d2dDevice;
    Microsoft::WRL::ComPtr<IDWriteFactory> dwriteFactory;
};

class D2DDevice {
public:
    explicit D2DDevice(bool forceWarp);
    explicit D2DDevice(std::shared_ptr<D2DDeviceResources> sharedResources);

    const BackendCaps &capabilities() const noexcept { return resources_->caps; }
    ID3D11Device *d3dDevice() const noexcept { return resources_->d3dDevice.Get(); }
    ID3D11DeviceContext *d3dContext() const noexcept { return resources_->d3dContext.Get(); }
    ID2D1Factory1 *d2dFactory() const noexcept { return resources_->d2dFactory.Get(); }
    ID2D1Device *d2dDevice() const noexcept { return resources_->d2dDevice.Get(); }
    ID2D1DeviceContext *d2dContext() const noexcept { return d2dContext_.Get(); }
    IDWriteFactory *dwriteFactory() const noexcept { return resources_->dwriteFactory.Get(); }
    std::shared_ptr<D2DDeviceResources> sharedResources() const noexcept {
        return resources_;
    }
    void appendVideoMemoryDiagnostics(BackendDiagnostics *diagnostics) const noexcept;
    std::string deviceRemovedReason() const;

private:
    void createD3DDevice(bool forceWarp);
    void createD2DDevice();
    void populateAdapterCaps(bool forceWarp);

    std::shared_ptr<D2DDeviceResources> resources_;
    Microsoft::WRL::ComPtr<ID2D1DeviceContext> d2dContext_;
};

}  // namespace krok::subtitle::native
