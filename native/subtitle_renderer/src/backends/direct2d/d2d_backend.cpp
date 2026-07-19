#include "d2d_backend.h"

#include <d2d1helper.h>
#include <d2d1effects.h>
#include <dwrite_1.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <atomic>
#include <limits>

namespace krok::subtitle::native {
namespace {

using Clock = std::chrono::steady_clock;

double elapsedMs(Clock::time_point start) {
    return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}

std::string hresultText(const char *operation, HRESULT value, const std::string &deviceReason = {}) {
    std::ostringstream stream;
    stream << operation << " failed (HRESULT=0x" << std::uppercase << std::hex
           << static_cast<unsigned long>(value) << ")";
    if (!deviceReason.empty()) {
        stream << "; " << deviceReason;
    }
    return stream.str();
}

void checkHr(HRESULT value, const char *operation, const D2DDevice &device) {
    if (FAILED(value)) {
        throw BackendError(hresultText(operation, value, device.deviceRemovedReason()));
    }
}

std::uint8_t unpremultiply(std::uint8_t value, std::uint8_t alpha) {
    if (alpha == 0) {
        return 0;
    }
    return static_cast<std::uint8_t>(std::min(255u, (static_cast<unsigned>(value) * 255u + alpha / 2u) / alpha));
}

D2D1_COLOR_F d2dColor(const RgbaColor &color) {
    return D2D1::ColorF(
        static_cast<float>(color.red) / 255.0f,
        static_cast<float>(color.green) / 255.0f,
        static_cast<float>(color.blue) / 255.0f,
        static_cast<float>(color.alpha) / 255.0f
    );
}

bool isLatinText(const std::wstring &text) {
    if (text.empty()) {
        return false;
    }
    return std::all_of(text.begin(), text.end(), [](wchar_t value) {
        return value >= 0x20 && value <= 0x7e;
    });
}

class GeometryRenderer final : public IDWriteTextRenderer {
public:
    explicit GeometryRenderer(ID2D1Factory1 *factory)
        : factory_(factory) {}

    const std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> &geometries() const noexcept {
        return geometries_;
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **object) override {
        if (object == nullptr) {
            return E_POINTER;
        }
        *object = nullptr;
        if (iid == __uuidof(IUnknown) || iid == __uuidof(IDWritePixelSnapping)
            || iid == __uuidof(IDWriteTextRenderer)) {
            *object = static_cast<IDWriteTextRenderer *>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }

    ULONG STDMETHODCALLTYPE AddRef() override {
        return ++refCount_;
    }

    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG value = --refCount_;
        if (value == 0) {
            delete this;
        }
        return value;
    }

    HRESULT STDMETHODCALLTYPE IsPixelSnappingDisabled(void *, BOOL *disabled) override {
        if (disabled == nullptr) {
            return E_POINTER;
        }
        *disabled = FALSE;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetCurrentTransform(void *, DWRITE_MATRIX *transform) override {
        if (transform == nullptr) {
            return E_POINTER;
        }
        *transform = DWRITE_MATRIX{1.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f};
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetPixelsPerDip(void *, FLOAT *pixelsPerDip) override {
        if (pixelsPerDip == nullptr) {
            return E_POINTER;
        }
        *pixelsPerDip = 1.0f;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE DrawGlyphRun(
        void *,
        FLOAT baselineOriginX,
        FLOAT baselineOriginY,
        DWRITE_MEASURING_MODE,
        const DWRITE_GLYPH_RUN *glyphRun,
        const DWRITE_GLYPH_RUN_DESCRIPTION *,
        IUnknown *
    ) override {
        if (glyphRun == nullptr || glyphRun->fontFace == nullptr || glyphRun->glyphCount == 0) {
            return S_OK;
        }
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
        HRESULT result = factory_->CreatePathGeometry(path.ReleaseAndGetAddressOf());
        if (FAILED(result)) {
            return result;
        }
        Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
        result = path->Open(sink.ReleaseAndGetAddressOf());
        if (FAILED(result)) {
            return result;
        }
        sink->SetFillMode(D2D1_FILL_MODE_WINDING);
        result = glyphRun->fontFace->GetGlyphRunOutline(
            glyphRun->fontEmSize,
            glyphRun->glyphIndices,
            glyphRun->glyphAdvances,
            glyphRun->glyphOffsets,
            glyphRun->glyphCount,
            glyphRun->isSideways,
            (glyphRun->bidiLevel & 1u) != 0,
            sink.Get()
        );
        const HRESULT closeResult = sink->Close();
        if (FAILED(result)) {
            return result;
        }
        if (FAILED(closeResult)) {
            return closeResult;
        }
        const D2D1_MATRIX_3X2_F translation = D2D1::Matrix3x2F::Translation(
            baselineOriginX,
            baselineOriginY
        );
        Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
        result = factory_->CreateTransformedGeometry(
            path.Get(),
            &translation,
            transformed.ReleaseAndGetAddressOf()
        );
        if (SUCCEEDED(result)) {
            geometries_.push_back(transformed);
        }
        return result;
    }

    HRESULT STDMETHODCALLTYPE DrawUnderline(
        void *, FLOAT, FLOAT, const DWRITE_UNDERLINE *, IUnknown *
    ) override {
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE DrawStrikethrough(
        void *, FLOAT, FLOAT, const DWRITE_STRIKETHROUGH *, IUnknown *
    ) override {
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE DrawInlineObject(
        void *, FLOAT, FLOAT, IDWriteInlineObject *, BOOL, BOOL, IUnknown *
    ) override {
        return S_OK;
    }

private:
    std::atomic<ULONG> refCount_{1};
    Microsoft::WRL::ComPtr<ID2D1Factory1> factory_;
    std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries_;
};

}  // namespace

struct Direct2DGpuBackend::Impl {
    struct CachedChar {
        int startMs = 0;
        int endMs = 0;
        float left = 0.0f;
        float right = 0.0f;
    };

    struct CachedLine {
        int startMs = 0;
        int endMs = 0;
        D2D1_RECT_F bounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
    };

    RenderScene scene;
    std::vector<CachedLine> lines;
    bool configured = false;
};

Direct2DGpuBackend::Direct2DGpuBackend(bool forceWarp)
    : device_(forceWarp), impl_(std::make_unique<Impl>()) {}

Direct2DGpuBackend::~Direct2DGpuBackend() = default;

BackendCaps Direct2DGpuBackend::capabilities() const {
    return device_.capabilities();
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

void Direct2DGpuBackend::configure(const RenderScene &scene) {
    if (scene.width <= 0 || scene.height <= 0 || scene.width > 8192 || scene.height > 8192) {
        throw BackendError("GPU scene dimensions must be within 1..8192");
    }
    impl_->scene = scene;
    impl_->lines.clear();
    impl_->lines.reserve(scene.lines.size());

    const TextStyle &style = scene.style;
    const std::wstring fontFamily = style.fontFamily.empty() ? L"Segoe UI" : style.fontFamily;
    Microsoft::WRL::ComPtr<IDWriteTextFormat> format;
    checkHr(
        device_.dwriteFactory()->CreateTextFormat(
            fontFamily.c_str(),
            nullptr,
            static_cast<DWRITE_FONT_WEIGHT>(std::clamp(style.fontWeight, 1, 999)),
            style.italic ? DWRITE_FONT_STYLE_ITALIC : DWRITE_FONT_STYLE_NORMAL,
            DWRITE_FONT_STRETCH_NORMAL,
            std::max(style.fontSize, 1.0f),
            L"ja-jp",
            format.ReleaseAndGetAddressOf()
        ),
        "IDWriteFactory::CreateTextFormat(scene)",
        device_
    );
    checkHr(format->SetWordWrapping(DWRITE_WORD_WRAPPING_NO_WRAP), "IDWriteTextFormat::SetWordWrapping", device_);

    for (const TextLine &sourceLine : scene.lines) {
        std::wstring text;
        std::vector<DWRITE_TEXT_RANGE> ranges;
        ranges.reserve(sourceLine.chars.size());
        for (const TextChar &ch : sourceLine.chars) {
            const UINT32 start = static_cast<UINT32>(text.size());
            text += ch.text;
            ranges.push_back(DWRITE_TEXT_RANGE{start, static_cast<UINT32>(ch.text.size())});
        }
        if (text.empty()) {
            continue;
        }

        Microsoft::WRL::ComPtr<IDWriteTextLayout> layout;
        checkHr(
            device_.dwriteFactory()->CreateTextLayout(
                text.c_str(),
                static_cast<UINT32>(text.size()),
                format.Get(),
                std::max(4096.0f, static_cast<float>(scene.width) * 4.0f),
                std::max(1024.0f, static_cast<float>(scene.height) * 2.0f),
                layout.ReleaseAndGetAddressOf()
            ),
            "IDWriteFactory::CreateTextLayout",
            device_
        );
        for (std::size_t index = 0; index < sourceLine.chars.size(); ++index) {
            if (!isLatinText(sourceLine.chars[index].text)) {
                continue;
            }
            const DWRITE_TEXT_RANGE range = ranges[index];
            if (style.latinFontFamily.has_value() && !style.latinFontFamily->empty()) {
                checkHr(
                    layout->SetFontFamilyName(style.latinFontFamily->c_str(), range),
                    "IDWriteTextLayout::SetFontFamilyName(latin)",
                    device_
                );
            }
            if (style.latinFontSize.has_value()) {
                checkHr(
                    layout->SetFontSize(std::max(*style.latinFontSize, 1.0f), range),
                    "IDWriteTextLayout::SetFontSize(latin)",
                    device_
                );
            }
            if (style.latinFontWeight.has_value()) {
                checkHr(
                    layout->SetFontWeight(
                        static_cast<DWRITE_FONT_WEIGHT>(std::clamp(*style.latinFontWeight, 1, 999)),
                        range
                    ),
                    "IDWriteTextLayout::SetFontWeight(latin)",
                    device_
                );
            }
        }
        if (std::abs(style.letterSpacing) > 0.001f) {
            Microsoft::WRL::ComPtr<IDWriteTextLayout1> layout1;
            checkHr(layout.As(&layout1), "Query IDWriteTextLayout1", device_);
            checkHr(
                layout1->SetCharacterSpacing(
                    0.0f,
                    style.letterSpacing,
                    0.0f,
                    DWRITE_TEXT_RANGE{0, static_cast<UINT32>(text.size())}
                ),
                "IDWriteTextLayout1::SetCharacterSpacing",
                device_
            );
        }

        auto *renderer = new GeometryRenderer(device_.d2dFactory());
        const HRESULT drawResult = layout->Draw(nullptr, renderer, 0.0f, 0.0f);
        if (FAILED(drawResult)) {
            renderer->Release();
            checkHr(drawResult, "IDWriteTextLayout::Draw(geometry)", device_);
        }

        Impl::CachedLine cached;
        cached.startMs = sourceLine.startMs;
        cached.endMs = sourceLine.endMs;
        cached.geometries = renderer->geometries();
        renderer->Release();

        bool hasBounds = false;
        for (const auto &geometry : cached.geometries) {
            D2D1_RECT_F bounds{};
            checkHr(geometry->GetBounds(nullptr, &bounds), "ID2D1Geometry::GetBounds", device_);
            if (!hasBounds) {
                cached.bounds = bounds;
                hasBounds = true;
            } else {
                cached.bounds.left = std::min(cached.bounds.left, bounds.left);
                cached.bounds.top = std::min(cached.bounds.top, bounds.top);
                cached.bounds.right = std::max(cached.bounds.right, bounds.right);
                cached.bounds.bottom = std::max(cached.bounds.bottom, bounds.bottom);
            }
        }
        if (!hasBounds) {
            cached.bounds = D2D1::RectF(0.0f, 0.0f, 0.0f, 0.0f);
        }

        cached.chars.reserve(sourceLine.chars.size());
        for (std::size_t index = 0; index < sourceLine.chars.size(); ++index) {
            const DWRITE_TEXT_RANGE range = ranges[index];
            FLOAT leadingX = 0.0f;
            FLOAT leadingY = 0.0f;
            FLOAT trailingX = 0.0f;
            FLOAT trailingY = 0.0f;
            DWRITE_HIT_TEST_METRICS leadingMetrics{};
            DWRITE_HIT_TEST_METRICS trailingMetrics{};
            if (range.length > 0) {
                checkHr(
                    layout->HitTestTextPosition(
                        range.startPosition,
                        FALSE,
                        &leadingX,
                        &leadingY,
                        &leadingMetrics
                    ),
                    "IDWriteTextLayout::HitTestTextPosition(leading)",
                    device_
                );
                checkHr(
                    layout->HitTestTextPosition(
                        range.startPosition + range.length - 1,
                        TRUE,
                        &trailingX,
                        &trailingY,
                        &trailingMetrics
                    ),
                    "IDWriteTextLayout::HitTestTextPosition(trailing)",
                    device_
                );
            }
            cached.chars.push_back(Impl::CachedChar{
                sourceLine.chars[index].startMs,
                sourceLine.chars[index].endMs,
                std::min(leadingX, trailingX),
                std::max(leadingX, trailingX),
            });
        }
        impl_->lines.push_back(std::move(cached));
    }
    impl_->configured = true;
}

ProbeResult Direct2DGpuBackend::renderFrame(int tMs) {
    if (!impl_->configured) {
        throw BackendError("GPU backend is not configured");
    }
    const RenderScene &scene = impl_->scene;
    const TextStyle &style = scene.style;

    D3D11_TEXTURE2D_DESC targetDesc{};
    targetDesc.Width = static_cast<UINT>(scene.width);
    targetDesc.Height = static_cast<UINT>(scene.height);
    targetDesc.MipLevels = 1;
    targetDesc.ArraySize = 1;
    targetDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    targetDesc.SampleDesc.Count = 1;
    targetDesc.Usage = D3D11_USAGE_DEFAULT;
    targetDesc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;

    Microsoft::WRL::ComPtr<ID3D11Texture2D> targetTexture;
    checkHr(
        device_.d3dDevice()->CreateTexture2D(&targetDesc, nullptr, targetTexture.ReleaseAndGetAddressOf()),
        "ID3D11Device::CreateTexture2D(frame target)",
        device_
    );
    Microsoft::WRL::ComPtr<IDXGISurface> targetSurface;
    checkHr(targetTexture.As(&targetSurface), "Query frame target IDXGISurface", device_);
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
        "ID2D1DeviceContext::CreateBitmapFromDxgiSurface(frame)",
        device_
    );

    const auto renderStart = Clock::now();
    ID2D1DeviceContext *context = device_.d2dContext();
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    context->SetTarget(nullptr);

    const Impl::CachedLine *line = nullptr;
    for (const Impl::CachedLine &candidate : impl_->lines) {
        if (tMs >= candidate.startMs - std::max(style.leadInMs, 0)
            && tMs <= candidate.endMs + std::max(style.tailMs, 0)) {
            line = &candidate;
            break;
        }
    }
    if (line != nullptr && !line->geometries.empty()) {
        const float inkWidth = line->bounds.right - line->bounds.left;
        float dx = (static_cast<float>(scene.width) - inkWidth) * 0.5f - line->bounds.left;
        if (style.alignment == "left") {
            dx = style.horizontalMargin - line->bounds.left;
        } else if (style.alignment == "right") {
            dx = static_cast<float>(scene.width) - style.horizontalMargin - line->bounds.right;
        }
        float dy = static_cast<float>(scene.height) - style.bottomMargin - line->bounds.bottom;
        if (style.verticalPosition == "top") {
            dy = style.bottomMargin - line->bounds.top;
        } else if (style.verticalPosition == "center") {
            dy = (static_cast<float>(scene.height) - (line->bounds.bottom - line->bounds.top)) * 0.5f
                - line->bounds.top;
        }
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> beforeFill;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> afterFill;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> beforeStroke;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> afterStroke;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> beforeStroke2;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> afterStroke2;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> beforeDecor;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> afterDecor;
        checkHr(context->CreateSolidColorBrush(d2dColor(style.beforeFill), beforeFill.ReleaseAndGetAddressOf()), "Create before fill brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.afterFill), afterFill.ReleaseAndGetAddressOf()), "Create after fill brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.beforeStroke), beforeStroke.ReleaseAndGetAddressOf()), "Create before stroke brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.afterStroke), afterStroke.ReleaseAndGetAddressOf()), "Create after stroke brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.beforeStroke2), beforeStroke2.ReleaseAndGetAddressOf()), "Create before stroke2 brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.afterStroke2), afterStroke2.ReleaseAndGetAddressOf()), "Create after stroke2 brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.beforeDecor), beforeDecor.ReleaseAndGetAddressOf()), "Create before decor brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.afterDecor), afterDecor.ReleaseAndGetAddressOf()), "Create after decor brush", device_);

        float wipeEdge = line->bounds.left;
        for (const Impl::CachedChar &ch : line->chars) {
            if (tMs >= ch.endMs) {
                wipeEdge = std::max(wipeEdge, ch.right);
                continue;
            }
            if (tMs <= ch.startMs) {
                break;
            }
            const int duration = std::max(ch.endMs - ch.startMs, 1);
            const float ratio = std::clamp(
                static_cast<float>(tMs - ch.startMs) / static_cast<float>(duration),
                0.0f,
                1.0f
            );
            wipeEdge = ch.left + (ch.right - ch.left) * ratio;
            break;
        }

        const float geometryPad = std::max(style.strokeWidth + style.stroke2Width, 2.0f) + 4.0f;
        const D2D1_RECT_F afterClip = D2D1::RectF(
            line->bounds.left - geometryPad,
            line->bounds.top - geometryPad,
            wipeEdge,
            line->bounds.bottom + geometryPad
        );

        Microsoft::WRL::ComPtr<ID2D1Bitmap1> glowSource;
        Microsoft::WRL::ComPtr<ID2D1Effect> blur;
        std::vector<int> glowSigmas;
        if (style.decorationKind == "glow") {
            checkHr(
                context->CreateBitmap(
                    D2D1::SizeU(static_cast<UINT32>(scene.width), static_cast<UINT32>(scene.height)),
                    nullptr,
                    0,
                    &bitmapProperties,
                    glowSource.ReleaseAndGetAddressOf()
                ),
                "ID2D1DeviceContext::CreateBitmap(glow source)",
                device_
            );
            checkHr(
                context->CreateEffect(CLSID_D2D1GaussianBlur, blur.ReleaseAndGetAddressOf()),
                "ID2D1DeviceContext::CreateEffect(GaussianBlur)",
                device_
            );

            const int radius = std::max(
                1,
                static_cast<int>(std::lround(std::max(style.glowBeforeRadius, style.glowAfterRadius)))
            );
            const float sourceWidth = std::max(0.0f, style.strokeWidth)
                + (style.stroke2Width > 0.0f ? style.stroke2Width : 0.0f)
                + static_cast<float>(radius);
            context->SetTarget(glowSource.Get());
            context->SetTransform(D2D1::Matrix3x2F::Translation(dx, dy));
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            for (const auto &geometry : line->geometries) {
                context->DrawGeometry(geometry.Get(), beforeDecor.Get(), sourceWidth);
            }
            if (wipeEdge > line->bounds.left) {
                context->PushAxisAlignedClip(afterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
                for (const auto &geometry : line->geometries) {
                    context->DrawGeometry(geometry.Get(), afterDecor.Get(), sourceWidth);
                }
                context->PopAxisAlignedClip();
            }
            checkHr(context->EndDraw(), "ID2D1DeviceContext::EndDraw(glow source)", device_);
            blur->SetInput(0, glowSource.Get());

            // N3 DrawOneLineDecorBlurMulti: N = BlurLevel + 1 and
            // sigma_i = R - floor(i * R / N). The common N3 path has one
            // DecorSize for both wipe colors, so use that exact combined source.
            const int passes = std::clamp(style.glowConcentrationLevel, 0, 2) + 1;
            for (int index = 0; index < passes; ++index) {
                glowSigmas.push_back(radius - index * radius / passes);
            }
        }

        context->SetTarget(targetBitmap.Get());
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        context->Clear(D2D1::ColorF(0.0f, 0.0f));
        for (int sigma : glowSigmas) {
            checkHr(
                blur->SetValue(
                    D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                    static_cast<float>(sigma)
                ),
                "ID2D1Effect::SetValue(StandardDeviation)",
                device_
            );
            context->DrawImage(blur.Get());
        }
        context->SetTransform(D2D1::Matrix3x2F::Translation(dx, dy));

        auto drawStack = [&](ID2D1Brush *fill, ID2D1Brush *stroke, ID2D1Brush *stroke2) {
            if (style.stroke2Width > 0.0f) {
                for (const auto &geometry : line->geometries) {
                    context->DrawGeometry(
                        geometry.Get(),
                        stroke2,
                        std::max(0.0f, style.strokeWidth) + style.stroke2Width
                    );
                }
            }
            if (style.strokeWidth > 0.0f) {
                for (const auto &geometry : line->geometries) {
                    context->DrawGeometry(geometry.Get(), stroke, style.strokeWidth);
                }
            }
            for (const auto &geometry : line->geometries) {
                context->FillGeometry(geometry.Get(), fill);
            }
        };
        drawStack(beforeFill.Get(), beforeStroke.Get(), beforeStroke2.Get());
        if (wipeEdge > line->bounds.left) {
            context->PushAxisAlignedClip(afterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
            drawStack(afterFill.Get(), afterStroke.Get(), afterStroke2.Get());
            context->PopAxisAlignedClip();
        }
        checkHr(context->EndDraw(), "ID2D1DeviceContext::EndDraw(frame layers)", device_);
        if (blur) {
            blur->SetInput(0, nullptr);
        }
    }

    if (line == nullptr || line->geometries.empty()) {
        context->SetTarget(targetBitmap.Get());
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        context->Clear(D2D1::ColorF(0.0f, 0.0f));
        checkHr(context->EndDraw(), "ID2D1DeviceContext::EndDraw(empty frame)", device_);
    }

    context->SetTarget(nullptr);
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    const double renderMs = elapsedMs(renderStart);

    const auto readbackStart = Clock::now();
    D3D11_TEXTURE2D_DESC stagingDesc = targetDesc;
    stagingDesc.Usage = D3D11_USAGE_STAGING;
    stagingDesc.BindFlags = 0;
    stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> stagingTexture;
    checkHr(
        device_.d3dDevice()->CreateTexture2D(&stagingDesc, nullptr, stagingTexture.ReleaseAndGetAddressOf()),
        "ID3D11Device::CreateTexture2D(frame staging)",
        device_
    );
    device_.d3dContext()->CopyResource(stagingTexture.Get(), targetTexture.Get());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    checkHr(
        device_.d3dContext()->Map(stagingTexture.Get(), 0, D3D11_MAP_READ, 0, &mapped),
        "ID3D11DeviceContext::Map(frame)",
        device_
    );

    ProbeResult result;
    result.renderMs = renderMs;
    result.surface.width = scene.width;
    result.surface.height = scene.height;
    result.surface.stride = scene.width * 4;
    result.surface.pixelFormat = PixelFormat::Rgba8888Straight;
    result.surface.bytes.resize(static_cast<std::size_t>(result.surface.stride) * scene.height);
    for (int y = 0; y < scene.height; ++y) {
        const auto *source = static_cast<const std::uint8_t *>(mapped.pData)
            + static_cast<std::size_t>(mapped.RowPitch) * y;
        auto *destination = result.surface.bytes.data()
            + static_cast<std::size_t>(result.surface.stride) * y;
        for (int x = 0; x < scene.width; ++x) {
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
