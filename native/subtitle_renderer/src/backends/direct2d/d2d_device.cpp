#include "d2d_device.h"

#include <d2d1helper.h>

#include <array>
#include <iomanip>
#include <sstream>
#include <windows.h>

namespace krok::subtitle::native {
namespace {

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

std::string utf8FromWide(const wchar_t *value) {
    if (value == nullptr || *value == L'\0') {
        return {};
    }
    const int count = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
    if (count <= 1) {
        return {};
    }
    std::string result(static_cast<std::size_t>(count), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value, -1, result.data(), count, nullptr, nullptr);
    result.pop_back();
    return result;
}

std::string featureLevelName(D3D_FEATURE_LEVEL level) {
    switch (level) {
    case D3D_FEATURE_LEVEL_11_1:
        return "11_1";
    case D3D_FEATURE_LEVEL_11_0:
        return "11_0";
    case D3D_FEATURE_LEVEL_10_1:
        return "10_1";
    case D3D_FEATURE_LEVEL_10_0:
        return "10_0";
    default:
        return "unknown";
    }
}

}  // namespace

D2DDevice::D2DDevice(bool forceWarp) {
    createD3DDevice(forceWarp);
    populateAdapterCaps(forceWarp);
    createD2DDevice();
}

void D2DDevice::createD3DDevice(bool forceWarp) {
    constexpr std::array<D3D_FEATURE_LEVEL, 2> levels{
        D3D_FEATURE_LEVEL_11_1,
        D3D_FEATURE_LEVEL_11_0,
    };
    const UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
    HRESULT result = E_FAIL;

    if (forceWarp) {
        result = D3D11CreateDevice(
            nullptr,
            D3D_DRIVER_TYPE_WARP,
            nullptr,
            flags,
            levels.data(),
            static_cast<UINT>(levels.size()),
            D3D11_SDK_VERSION,
            d3dDevice_.ReleaseAndGetAddressOf(),
            &featureLevel_,
            d3dContext_.ReleaseAndGetAddressOf()
        );
        checkHr(result, "D3D11CreateDevice(WARP)");
        Microsoft::WRL::ComPtr<ID3D11Multithread> multithread;
        if (SUCCEEDED(d3dContext_.As(&multithread))) {
            multithread->SetMultithreadProtected(TRUE);
        }
        return;
    }

    Microsoft::WRL::ComPtr<IDXGIFactory6> factory;
    checkHr(CreateDXGIFactory2(0, IID_PPV_ARGS(factory.ReleaseAndGetAddressOf())), "CreateDXGIFactory2");
    for (UINT index = 0;; ++index) {
        Microsoft::WRL::ComPtr<IDXGIAdapter1> candidate;
        result = factory->EnumAdapterByGpuPreference(
            index,
            DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE,
            IID_PPV_ARGS(candidate.ReleaseAndGetAddressOf())
        );
        if (result == DXGI_ERROR_NOT_FOUND) {
            break;
        }
        checkHr(result, "EnumAdapterByGpuPreference");
        DXGI_ADAPTER_DESC1 desc{};
        checkHr(candidate->GetDesc1(&desc), "IDXGIAdapter1::GetDesc1");
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0) {
            continue;
        }
        result = D3D11CreateDevice(
            candidate.Get(),
            D3D_DRIVER_TYPE_UNKNOWN,
            nullptr,
            flags,
            levels.data(),
            static_cast<UINT>(levels.size()),
            D3D11_SDK_VERSION,
            d3dDevice_.ReleaseAndGetAddressOf(),
            &featureLevel_,
            d3dContext_.ReleaseAndGetAddressOf()
        );
        if (SUCCEEDED(result)) {
            adapter_ = candidate;
            break;
        }
    }
    if (d3dDevice_ == nullptr) {
        throw BackendError(hresultText("D3D11CreateDevice(hardware)", result));
    }

    Microsoft::WRL::ComPtr<ID3D11Multithread> multithread;
    if (SUCCEEDED(d3dContext_.As(&multithread))) {
        multithread->SetMultithreadProtected(TRUE);
    }
}

void D2DDevice::populateAdapterCaps(bool forceWarp) {
    if (adapter_ == nullptr) {
        Microsoft::WRL::ComPtr<IDXGIDevice> dxgiDevice;
        checkHr(d3dDevice_.As(&dxgiDevice), "Query IDXGIDevice");
        Microsoft::WRL::ComPtr<IDXGIAdapter> baseAdapter;
        checkHr(dxgiDevice->GetAdapter(baseAdapter.ReleaseAndGetAddressOf()), "IDXGIDevice::GetAdapter");
        checkHr(baseAdapter.As(&adapter_), "Query IDXGIAdapter1");
    }
    DXGI_ADAPTER_DESC1 desc{};
    checkHr(adapter_->GetDesc1(&desc), "IDXGIAdapter1::GetDesc1");
    caps_.backend = "direct2d";
    caps_.adapterName = utf8FromWide(desc.Description);
    caps_.featureLevel = featureLevelName(featureLevel_);
    caps_.adapterVendorId = desc.VendorId;
    caps_.adapterDeviceId = desc.DeviceId;
    caps_.dedicatedVideoMemory = static_cast<std::uint64_t>(desc.DedicatedVideoMemory);
    caps_.warp = forceWarp || (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0;
    caps_.hardware = !caps_.warp;
    caps_.supportsTransparentSurface = true;
    caps_.supportsStagingReadback = true;
    caps_.supportsGlyphs = true;
}

void D2DDevice::createD2DDevice() {
    D2D1_FACTORY_OPTIONS options{};
    checkHr(
        D2D1CreateFactory(
            D2D1_FACTORY_TYPE_MULTI_THREADED,
            __uuidof(ID2D1Factory1),
            &options,
            reinterpret_cast<void **>(d2dFactory_.ReleaseAndGetAddressOf())
        ),
        "D2D1CreateFactory"
    );
    Microsoft::WRL::ComPtr<IDXGIDevice> dxgiDevice;
    checkHr(d3dDevice_.As(&dxgiDevice), "Query IDXGIDevice for Direct2D");
    checkHr(d2dFactory_->CreateDevice(dxgiDevice.Get(), d2dDevice_.ReleaseAndGetAddressOf()), "ID2D1Factory1::CreateDevice");
    checkHr(
        d2dDevice_->CreateDeviceContext(
            D2D1_DEVICE_CONTEXT_OPTIONS_ENABLE_MULTITHREADED_OPTIMIZATIONS,
            d2dContext_.ReleaseAndGetAddressOf()
        ),
        "ID2D1Device::CreateDeviceContext"
    );
    checkHr(
        DWriteCreateFactory(
            DWRITE_FACTORY_TYPE_SHARED,
            __uuidof(IDWriteFactory),
            reinterpret_cast<IUnknown **>(dwriteFactory_.ReleaseAndGetAddressOf())
        ),
        "DWriteCreateFactory"
    );
}

std::string D2DDevice::deviceRemovedReason() const {
    const HRESULT reason = d3dDevice_->GetDeviceRemovedReason();
    if (reason == S_OK) {
        return {};
    }
    return hresultText("D3D11 device removed", reason);
}

}  // namespace krok::subtitle::native
