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

D2DDevice::D2DDevice(bool forceWarp)
    : resources_(std::make_shared<D2DDeviceResources>()) {
    createD3DDevice(forceWarp);
    populateAdapterCaps(forceWarp);
    createD2DDevice();
}

D2DDevice::D2DDevice(std::shared_ptr<D2DDeviceResources> sharedResources)
    : resources_(std::move(sharedResources)) {
    if (!resources_ || !resources_->d2dDevice || !resources_->d3dDevice) {
        throw BackendError("shared Direct2D device resources are not initialized");
    }
    checkHr(
        resources_->d2dDevice->CreateDeviceContext(
            D2D1_DEVICE_CONTEXT_OPTIONS_ENABLE_MULTITHREADED_OPTIMIZATIONS,
            d2dContext_.ReleaseAndGetAddressOf()
        ),
        "ID2D1Device::CreateDeviceContext(shared worker)"
    );
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
            resources_->d3dDevice.ReleaseAndGetAddressOf(),
            &resources_->featureLevel,
            resources_->d3dContext.ReleaseAndGetAddressOf()
        );
        checkHr(result, "D3D11CreateDevice(WARP)");
        Microsoft::WRL::ComPtr<ID3D11Multithread> multithread;
        if (SUCCEEDED(resources_->d3dContext.As(&multithread))) {
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
            resources_->d3dDevice.ReleaseAndGetAddressOf(),
            &resources_->featureLevel,
            resources_->d3dContext.ReleaseAndGetAddressOf()
        );
        if (SUCCEEDED(result)) {
            resources_->adapter = candidate;
            break;
        }
    }
    if (resources_->d3dDevice == nullptr) {
        throw BackendError(hresultText("D3D11CreateDevice(hardware)", result));
    }

    Microsoft::WRL::ComPtr<ID3D11Multithread> multithread;
    if (SUCCEEDED(resources_->d3dContext.As(&multithread))) {
        multithread->SetMultithreadProtected(TRUE);
    }
}

void D2DDevice::populateAdapterCaps(bool forceWarp) {
    if (resources_->adapter == nullptr) {
        Microsoft::WRL::ComPtr<IDXGIDevice> dxgiDevice;
        checkHr(resources_->d3dDevice.As(&dxgiDevice), "Query IDXGIDevice");
        Microsoft::WRL::ComPtr<IDXGIAdapter> baseAdapter;
        checkHr(dxgiDevice->GetAdapter(baseAdapter.ReleaseAndGetAddressOf()), "IDXGIDevice::GetAdapter");
        checkHr(baseAdapter.As(&resources_->adapter), "Query IDXGIAdapter1");
    }
    DXGI_ADAPTER_DESC1 desc{};
    checkHr(resources_->adapter->GetDesc1(&desc), "IDXGIAdapter1::GetDesc1");
    resources_->caps.backend = "direct2d";
    resources_->caps.adapterName = utf8FromWide(desc.Description);
    resources_->caps.featureLevel = featureLevelName(resources_->featureLevel);
    resources_->caps.adapterVendorId = desc.VendorId;
    resources_->caps.adapterDeviceId = desc.DeviceId;
    resources_->caps.dedicatedVideoMemory = static_cast<std::uint64_t>(desc.DedicatedVideoMemory);
    resources_->caps.warp = forceWarp || (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0;
    resources_->caps.hardware = !resources_->caps.warp;
    resources_->caps.supportsTransparentSurface = true;
    resources_->caps.supportsStagingReadback = true;
    resources_->caps.supportsGlyphs = true;
    resources_->caps.supportsNativePreview = true;
}

void D2DDevice::createD2DDevice() {
    D2D1_FACTORY_OPTIONS options{};
    checkHr(
        D2D1CreateFactory(
            D2D1_FACTORY_TYPE_MULTI_THREADED,
            __uuidof(ID2D1Factory1),
            &options,
            reinterpret_cast<void **>(resources_->d2dFactory.ReleaseAndGetAddressOf())
        ),
        "D2D1CreateFactory"
    );
    Microsoft::WRL::ComPtr<IDXGIDevice> dxgiDevice;
    checkHr(resources_->d3dDevice.As(&dxgiDevice), "Query IDXGIDevice for Direct2D");
    checkHr(resources_->d2dFactory->CreateDevice(
        dxgiDevice.Get(), resources_->d2dDevice.ReleaseAndGetAddressOf()
    ), "ID2D1Factory1::CreateDevice");
    checkHr(
        resources_->d2dDevice->CreateDeviceContext(
            D2D1_DEVICE_CONTEXT_OPTIONS_ENABLE_MULTITHREADED_OPTIMIZATIONS,
            d2dContext_.ReleaseAndGetAddressOf()
        ),
        "ID2D1Device::CreateDeviceContext"
    );
    checkHr(
        DWriteCreateFactory(
            DWRITE_FACTORY_TYPE_SHARED,
            __uuidof(IDWriteFactory),
            reinterpret_cast<IUnknown **>(resources_->dwriteFactory.ReleaseAndGetAddressOf())
        ),
        "DWriteCreateFactory"
    );
}

void D2DDevice::appendVideoMemoryDiagnostics(
    BackendDiagnostics *diagnostics
) const noexcept {
    if (diagnostics == nullptr || resources_->adapter == nullptr) {
        return;
    }
    Microsoft::WRL::ComPtr<IDXGIAdapter3> adapter3;
    if (FAILED(resources_->adapter.As(&adapter3))) {
        return;
    }
    DXGI_QUERY_VIDEO_MEMORY_INFO local{};
    DXGI_QUERY_VIDEO_MEMORY_INFO nonLocal{};
    if (FAILED(adapter3->QueryVideoMemoryInfo(
            0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &local
        ))
        || FAILED(adapter3->QueryVideoMemoryInfo(
            0, DXGI_MEMORY_SEGMENT_GROUP_NON_LOCAL, &nonLocal
        ))) {
        return;
    }
    diagnostics->videoMemoryInfoAvailable = true;
    diagnostics->localVideoMemoryUsageBytes = local.CurrentUsage;
    diagnostics->localVideoMemoryBudgetBytes = local.Budget;
    diagnostics->nonLocalVideoMemoryUsageBytes = nonLocal.CurrentUsage;
    diagnostics->nonLocalVideoMemoryBudgetBytes = nonLocal.Budget;
}

std::string D2DDevice::deviceRemovedReason() const {
    const HRESULT reason = resources_->d3dDevice->GetDeviceRemovedReason();
    if (reason == S_OK) {
        return {};
    }
    return hresultText("D3D11 device removed", reason);
}

}  // namespace krok::subtitle::native
