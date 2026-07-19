#include "d2d_backend.h"

#include <d2d1helper.h>
#include <d2d1effects.h>
#include <dwrite.h>
#include <wincodec.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <cwctype>
#include <iomanip>
#include <sstream>
#include <limits>
#include <tuple>

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

bool isAsciiAlnumText(const std::wstring &text) {
    bool seen = false;
    for (wchar_t value : text) {
        if (value == L' ' || value == L'\t' || value == L'\r' || value == L'\n') {
            continue;
        }
        seen = true;
        if (!((value >= L'0' && value <= L'9')
            || (value >= L'A' && value <= L'Z')
            || (value >= L'a' && value <= L'z'))) {
            return false;
        }
    }
    return seen;
}

bool isWhitespaceText(const std::wstring &text) {
    return !text.empty() && std::all_of(text.begin(), text.end(), [](wchar_t value) {
        return std::iswspace(static_cast<wint_t>(value)) != 0;
    });
}

bool paintNeedsBodyProtection(const PaintStyle &paint) {
    if (paint.mode == "image") {
        return true;
    }
    if (paint.mode == "gradient_horizontal"
        || paint.mode == "gradient_vertical"
        || paint.mode == "split_vertical") {
        return std::any_of(paint.stops.begin(), paint.stops.end(), [](const PaintStop &stop) {
            return stop.color.alpha < 255;
        });
    }
    return paint.color.alpha < 255;
}

Microsoft::WRL::ComPtr<ID2D1Geometry> outsideStrokeGeometry(
    ID2D1Factory1 *factory,
    ID2D1Geometry *body,
    float width,
    const D2DDevice &device
) {
    if (body == nullptr || width <= 0.0f) {
        return {};
    }
    D2D1_STROKE_STYLE_PROPERTIES properties = D2D1::StrokeStyleProperties();
    properties.startCap = D2D1_CAP_STYLE_ROUND;
    properties.endCap = D2D1_CAP_STYLE_ROUND;
    properties.dashCap = D2D1_CAP_STYLE_ROUND;
    properties.lineJoin = D2D1_LINE_JOIN_ROUND;
    Microsoft::WRL::ComPtr<ID2D1StrokeStyle> strokeStyle;
    checkHr(
        factory->CreateStrokeStyle(
            properties, nullptr, 0, strokeStyle.ReleaseAndGetAddressOf()
        ),
        "Create protected body stroke style",
        device
    );
    Microsoft::WRL::ComPtr<ID2D1PathGeometry> widened;
    checkHr(
        factory->CreatePathGeometry(widened.ReleaseAndGetAddressOf()),
        "Create protected widened geometry",
        device
    );
    Microsoft::WRL::ComPtr<ID2D1GeometrySink> widenedSink;
    checkHr(widened->Open(widenedSink.ReleaseAndGetAddressOf()), "Open protected widened geometry", device);
    checkHr(body->Widen(width, strokeStyle.Get(), nullptr, widenedSink.Get()), "Widen protected body stroke", device);
    checkHr(widenedSink->Close(), "Close protected widened geometry", device);

    Microsoft::WRL::ComPtr<ID2D1PathGeometry> outside;
    checkHr(
        factory->CreatePathGeometry(outside.ReleaseAndGetAddressOf()),
        "Create protected outside geometry",
        device
    );
    Microsoft::WRL::ComPtr<ID2D1GeometrySink> outsideSink;
    checkHr(outside->Open(outsideSink.ReleaseAndGetAddressOf()), "Open protected outside geometry", device);
    checkHr(
        widened->CombineWithGeometry(
            body, D2D1_COMBINE_MODE_EXCLUDE, nullptr, outsideSink.Get()
        ),
        "Subtract protected glyph body",
        device
    );
    checkHr(outsideSink->Close(), "Close protected outside geometry", device);
    Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
    checkHr(outside.As(&geometry), "Query protected outside geometry", device);
    return geometry;
}

Microsoft::WRL::ComPtr<IDWriteFontFace> createFontFace(
    IDWriteFontCollection *collection,
    const std::wstring &familyName,
    int weight,
    bool italic
) {
    UINT32 familyIndex = 0;
    BOOL exists = FALSE;
    if (familyName.empty()
        || FAILED(collection->FindFamilyName(familyName.c_str(), &familyIndex, &exists))
        || !exists) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
    if (FAILED(collection->GetFontFamily(familyIndex, family.ReleaseAndGetAddressOf()))) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFont> font;
    if (FAILED(family->GetFirstMatchingFont(
            static_cast<DWRITE_FONT_WEIGHT>(std::clamp(weight, 1, 999)),
            DWRITE_FONT_STRETCH_NORMAL,
            italic ? DWRITE_FONT_STYLE_ITALIC : DWRITE_FONT_STYLE_NORMAL,
            font.ReleaseAndGetAddressOf()))) {
        return {};
    }
    Microsoft::WRL::ComPtr<IDWriteFontFace> face;
    if (FAILED(font->CreateFontFace(face.ReleaseAndGetAddressOf()))) {
        return {};
    }
    return face;
}

std::vector<UINT32> utf16CodeUnits(const std::wstring &text) {
    std::vector<UINT32> values;
    values.reserve(text.size());
    for (wchar_t value : text) {
        // N3 converts each UTF-16 System.Char independently instead of
        // decoding a Unicode scalar. wchar_t has the same 16-bit width here.
        values.push_back(static_cast<UINT32>(static_cast<std::uint16_t>(value)));
    }
    return values;
}

std::vector<UINT16> glyphIndices(IDWriteFontFace *face, const std::wstring &text) {
    const std::vector<UINT32> codeUnits = utf16CodeUnits(text);
    std::vector<UINT16> glyphs(codeUnits.size());
    if (!codeUnits.empty()
        && FAILED(face->GetGlyphIndices(
            codeUnits.data(),
            static_cast<UINT32>(codeUnits.size()),
            glyphs.data()))) {
        glyphs.clear();
    }
    return glyphs;
}

bool validGlyphIndices(const std::vector<UINT16> &glyphs) {
    // This intentionally mirrors N3: only the first glyph controls fallback.
    return !glyphs.empty() && glyphs.front() != 0;
}

Microsoft::WRL::ComPtr<IDWriteFontFace> findFallbackFontFace(
    IDWriteFontCollection *collection,
    const std::wstring &text,
    std::vector<Microsoft::WRL::ComPtr<IDWriteFontFace>> &successfulFaces,
    std::vector<UINT16> &glyphs
) {
    for (const auto &face : successfulFaces) {
        glyphs = glyphIndices(face.Get(), text);
        if (validGlyphIndices(glyphs)) {
            return face;
        }
    }

    auto tryFace = [&](Microsoft::WRL::ComPtr<IDWriteFontFace> face) {
        if (!face) {
            return Microsoft::WRL::ComPtr<IDWriteFontFace>{};
        }
        std::vector<UINT16> candidate = glyphIndices(face.Get(), text);
        if (!validGlyphIndices(candidate)) {
            return Microsoft::WRL::ComPtr<IDWriteFontFace>{};
        }
        glyphs = std::move(candidate);
        successfulFaces.push_back(face);
        return face;
    };

    // N3 gives this bold face priority before scanning the system collection.
    if (auto face = tryFace(createFontFace(
            collection, L"Microsoft JhengHei", DWRITE_FONT_WEIGHT_BOLD, false))) {
        return face;
    }
    const UINT32 familyCount = collection->GetFontFamilyCount();
    for (UINT32 index = 0; index < familyCount; ++index) {
        Microsoft::WRL::ComPtr<IDWriteFontFamily> family;
        if (FAILED(collection->GetFontFamily(index, family.ReleaseAndGetAddressOf()))) {
            continue;
        }
        Microsoft::WRL::ComPtr<IDWriteFont> font;
        if (FAILED(family->GetFirstMatchingFont(
                DWRITE_FONT_WEIGHT_BOLD,
                DWRITE_FONT_STRETCH_NORMAL,
                DWRITE_FONT_STYLE_NORMAL,
                font.ReleaseAndGetAddressOf()))) {
            continue;
        }
        Microsoft::WRL::ComPtr<IDWriteFontFace> candidate;
        if (FAILED(font->CreateFontFace(candidate.ReleaseAndGetAddressOf()))) {
            continue;
        }
        if (auto face = tryFace(std::move(candidate))) {
            return face;
        }
    }
    glyphs.clear();
    return {};
}

Microsoft::WRL::ComPtr<ID2D1Brush> createPaintBrush(
    ID2D1DeviceContext *context,
    const PaintStyle &paint,
    const D2D1_RECT_F &rect,
    const RgbaColor &fallback,
    const D2DDevice &device,
    ID2D1Bitmap1 *image = nullptr,
    float canvasDx = 0.0f,
    float canvasDy = 0.0f
) {
    if (paint.mode == "image" && image != nullptr) {
        Microsoft::WRL::ComPtr<ID2D1BitmapBrush1> bitmapBrush;
        const D2D1_BITMAP_BRUSH_PROPERTIES1 bitmapProperties =
            D2D1::BitmapBrushProperties1(
                D2D1_EXTEND_MODE_WRAP,
                D2D1_EXTEND_MODE_WRAP,
                D2D1_INTERPOLATION_MODE_LINEAR
            );
        const D2D1_BRUSH_PROPERTIES brushProperties = D2D1::BrushProperties();
        checkHr(
            context->CreateBitmapBrush(
                image,
                bitmapProperties,
                brushProperties,
                bitmapBrush.ReleaseAndGetAddressOf()
            ),
            "Create image fill bitmap brush",
            device
        );
        const float scale = std::clamp(paint.imageScale, 0.01f, 10.0f);
        bitmapBrush->SetTransform(
            D2D1::Matrix3x2F::Scale(scale, scale)
                * D2D1::Matrix3x2F::Translation(-canvasDx, -canvasDy)
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> brush;
        checkHr(bitmapBrush.As(&brush), "Query image fill bitmap brush", device);
        return brush;
    }
    const bool gradient = paint.mode == "gradient_horizontal"
        || paint.mode == "gradient_vertical"
        || paint.mode == "split_vertical";
    if (!gradient || paint.stops.empty()) {
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> solid;
        checkHr(
            context->CreateSolidColorBrush(
                d2dColor(paint.mode.empty() ? fallback : paint.color),
                solid.ReleaseAndGetAddressOf()
            ),
            "Create paint solid brush",
            device
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> brush;
        checkHr(solid.As(&brush), "Query paint solid brush", device);
        return brush;
    }

    std::vector<PaintStop> ordered = paint.stops;
    std::stable_sort(ordered.begin(), ordered.end(), [](const auto &left, const auto &right) {
        return left.position < right.position;
    });
    std::vector<D2D1_GRADIENT_STOP> stops;
    if (paint.mode == "split_vertical") {
        stops.reserve(ordered.size() * 2);
        stops.push_back(D2D1::GradientStop(
            std::clamp(ordered.front().position, 0.0f, 1.0f),
            d2dColor(ordered.front().color)
        ));
        for (std::size_t index = 1; index < ordered.size(); ++index) {
            const float position = std::clamp(ordered[index].position, 0.0f, 1.0f);
            stops.push_back(D2D1::GradientStop(
                position, d2dColor(ordered[index - 1].color)
            ));
            stops.push_back(D2D1::GradientStop(
                position, d2dColor(ordered[index].color)
            ));
        }
    } else {
        stops.reserve(ordered.size());
        for (const PaintStop &stop : ordered) {
            stops.push_back(D2D1::GradientStop(
                std::clamp(stop.position, 0.0f, 1.0f),
                d2dColor(stop.color)
            ));
        }
    }
    if (stops.size() == 1) {
        stops.push_back(D2D1::GradientStop(1.0f, stops.front().color));
    }
    Microsoft::WRL::ComPtr<ID2D1GradientStopCollection> collection;
    checkHr(
        context->CreateGradientStopCollection(
            stops.data(),
            static_cast<UINT32>(stops.size()),
            D2D1_GAMMA_2_2,
            paint.mode == "split_vertical"
                ? D2D1_EXTEND_MODE_WRAP
                : D2D1_EXTEND_MODE_CLAMP,
            collection.ReleaseAndGetAddressOf()
        ),
        "Create paint gradient stops",
        device
    );
    const bool horizontal = paint.mode == "gradient_horizontal";
    const D2D1_POINT_2F start = horizontal
        ? D2D1::Point2F(rect.left, (rect.top + rect.bottom) * 0.5f)
        : D2D1::Point2F((rect.left + rect.right) * 0.5f, rect.top);
    const D2D1_POINT_2F end = horizontal
        ? D2D1::Point2F(rect.right, (rect.top + rect.bottom) * 0.5f)
        : D2D1::Point2F((rect.left + rect.right) * 0.5f, rect.bottom);
    Microsoft::WRL::ComPtr<ID2D1LinearGradientBrush> linear;
    checkHr(
        context->CreateLinearGradientBrush(
            D2D1::LinearGradientBrushProperties(start, end),
            collection.Get(),
            linear.ReleaseAndGetAddressOf()
        ),
        "Create paint linear gradient brush",
        device
    );
    Microsoft::WRL::ComPtr<ID2D1Brush> brush;
    checkHr(linear.As(&brush), "Query paint gradient brush", device);
    return brush;
}

Microsoft::WRL::ComPtr<ID2D1Bitmap1> loadWicBitmap(
    ID2D1DeviceContext *context,
    const std::wstring &path
) {
    if (path.empty()) {
        return {};
    }
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(initialized) && initialized != RPC_E_CHANGED_MODE) {
        return {};
    }
    Microsoft::WRL::ComPtr<IWICImagingFactory> factory;
    if (FAILED(CoCreateInstance(
            CLSID_WICImagingFactory,
            nullptr,
            CLSCTX_INPROC_SERVER,
            IID_PPV_ARGS(factory.ReleaseAndGetAddressOf())))) {
        return {};
    }
    Microsoft::WRL::ComPtr<IWICBitmapDecoder> decoder;
    if (FAILED(factory->CreateDecoderFromFilename(
            path.c_str(),
            nullptr,
            GENERIC_READ,
            WICDecodeMetadataCacheOnLoad,
            decoder.ReleaseAndGetAddressOf()))) {
        return {};
    }
    Microsoft::WRL::ComPtr<IWICBitmapFrameDecode> frame;
    if (FAILED(decoder->GetFrame(0, frame.ReleaseAndGetAddressOf()))) {
        return {};
    }
    Microsoft::WRL::ComPtr<IWICFormatConverter> converter;
    if (FAILED(factory->CreateFormatConverter(converter.ReleaseAndGetAddressOf()))
        || FAILED(converter->Initialize(
            frame.Get(),
            GUID_WICPixelFormat32bppPBGRA,
            WICBitmapDitherTypeNone,
            nullptr,
            0.0,
            WICBitmapPaletteTypeMedianCut))) {
        return {};
    }
    Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
    UINT width = 0;
    UINT height = 0;
    if (FAILED(converter->GetSize(&width, &height)) || width == 0 || height == 0) {
        return {};
    }
    const UINT stride = width * 4;
    std::vector<std::uint8_t> pixels(static_cast<std::size_t>(stride) * height);
    if (FAILED(converter->CopyPixels(
            nullptr, stride, static_cast<UINT>(pixels.size()), pixels.data()))) {
        return {};
    }
    const D2D1_BITMAP_PROPERTIES1 properties = D2D1::BitmapProperties1(
        D2D1_BITMAP_OPTIONS_NONE,
        D2D1::PixelFormat(
            DXGI_FORMAT_B8G8R8A8_UNORM,
            D2D1_ALPHA_MODE_PREMULTIPLIED
        ),
        96.0f,
        96.0f
    );
    if (FAILED(context->CreateBitmap(
            D2D1::SizeU(width, height),
            pixels.data(),
            stride,
            &properties,
            bitmap.ReleaseAndGetAddressOf()))) {
        return {};
    }
    return bitmap;
}

}  // namespace

struct Direct2DGpuBackend::Impl {
    struct CachedImage {
        std::wstring path;
        std::uint64_t modifiedMs = 0;
        std::uint64_t size = 0;
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
    };
    struct CachedChar {
        int startMs = 0;
        int endMs = 0;
        float left = 0.0f;
        float right = 0.0f;
        float layoutLeft = 0.0f;
        float layoutRight = 0.0f;
        int styleIndex = -1;
        float boxAscent = 0.0f;
        Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> protectedStrokeGeometry;
    };

    struct CachedRuby {
        int startMs = 0;
        int endMs = 0;
        float baselineOffset = 0.0f;
        int styleIndex = -1;
        D2D1_RECT_F bounds{};
        D2D1_RECT_F fillBounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> protectedStrokeGeometries;
    };

    struct CachedLine {
        int startMs = 0;
        int endMs = 0;
        int sourceIndex = 0;
        int compositeOrder = 0;
        int lane = 0;
        bool staticOverlay = false;
        int fadeInMs = 0;
        int fadeOutMs = 0;
        std::vector<DisplayWindow> displayWindows;
        TextStyle style;
        float ascent = 0.0f;
        float descent = 0.0f;
        float boxAscent = 0.0f;
        bool hasRubyAnchor = false;
        float maxVisualPad = 0.0f;
        bool hasInlineStyles = false;
        D2D1_RECT_F bounds{};
        D2D1_RECT_F fillBounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
        std::vector<CachedRuby> rubies;
    };

    RenderScene scene;
    std::vector<CachedLine> lines;
    std::vector<CachedImage> images;
    BackendDiagnostics diagnostics;
    bool configured = false;
    int frameSurfaceWidth = 0;
    int frameSurfaceHeight = 0;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> frameTargetTexture;
    Microsoft::WRL::ComPtr<ID2D1Bitmap1> frameTargetBitmap;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> frameStagingTexture;
};

Direct2DGpuBackend::Direct2DGpuBackend(bool forceWarp)
    : device_(forceWarp), impl_(std::make_unique<Impl>()) {}

Direct2DGpuBackend::~Direct2DGpuBackend() = default;

BackendCaps Direct2DGpuBackend::capabilities() const {
    return device_.capabilities();
}

BackendDiagnostics Direct2DGpuBackend::diagnostics() const {
    BackendDiagnostics result = impl_->diagnostics;
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

void Direct2DGpuBackend::configure(const RenderScene &scene) {
    if (scene.width <= 0 || scene.height <= 0 || scene.width > 8192 || scene.height > 8192) {
        throw BackendError("GPU scene dimensions must be within 1..8192");
    }
    if (impl_->configured && impl_->scene == scene) {
        ++impl_->diagnostics.cacheHits;
        return;
    }
    ++impl_->diagnostics.cacheMisses;
    if (impl_->frameSurfaceWidth != scene.width || impl_->frameSurfaceHeight != scene.height) {
        impl_->frameTargetBitmap.Reset();
        impl_->frameTargetTexture.Reset();
        impl_->frameStagingTexture.Reset();
        impl_->frameSurfaceWidth = scene.width;
        impl_->frameSurfaceHeight = scene.height;
    }
    impl_->scene = scene;
    impl_->lines.clear();
    impl_->lines.reserve(scene.lines.size());
    impl_->images.clear();
    auto cacheStyleImages = [&](const TextStyle &style) {
        const PaintStyle *paints[] = {
            &style.beforeFillPaint, &style.afterFillPaint,
            &style.beforeStrokePaint, &style.afterStrokePaint,
            &style.beforeStroke2Paint, &style.afterStroke2Paint,
            &style.beforeDecorPaint, &style.afterDecorPaint,
            &style.rubyBeforeFillPaint, &style.rubyAfterFillPaint,
            &style.rubyBeforeStrokePaint, &style.rubyAfterStrokePaint,
            &style.rubyBeforeStroke2Paint, &style.rubyAfterStroke2Paint,
            &style.rubyBeforeDecorPaint, &style.rubyAfterDecorPaint,
        };
        for (const PaintStyle *paint : paints) {
            if (paint->mode != "image" || paint->imagePath.empty()) {
                continue;
            }
            const bool cached = std::any_of(
                impl_->images.begin(), impl_->images.end(),
                [&](const Impl::CachedImage &image) {
                    return image.path == paint->imagePath
                        && image.modifiedMs == paint->imageModifiedMs
                        && image.size == paint->imageSize;
                }
            );
            if (!cached) {
                impl_->images.push_back(Impl::CachedImage{
                    paint->imagePath,
                    paint->imageModifiedMs,
                    paint->imageSize,
                    loadWicBitmap(device_.d2dContext(), paint->imagePath),
                });
            }
        }
    };
    cacheStyleImages(scene.style);
    for (const TextStyle &style : scene.lineStyles) {
        cacheStyleImages(style);
    }
    for (const TextStyle &style : scene.charStyles) {
        cacheStyleImages(style);
    }

    Microsoft::WRL::ComPtr<IDWriteFontCollection> fontCollection;
    checkHr(
        device_.dwriteFactory()->GetSystemFontCollection(
            fontCollection.ReleaseAndGetAddressOf(),
            FALSE
        ),
        "IDWriteFactory::GetSystemFontCollection",
        device_
    );
    std::vector<Microsoft::WRL::ComPtr<IDWriteFontFace>> fallbackFaces;

    auto extendBounds = [](D2D1_RECT_F &target, bool &hasBounds, const D2D1_RECT_F &value) {
        if (!hasBounds) {
            target = value;
            hasBounds = true;
            return;
        }
        target.left = std::min(target.left, value.left);
        target.top = std::min(target.top, value.top);
        target.right = std::max(target.right, value.right);
        target.bottom = std::max(target.bottom, value.bottom);
    };

    for (std::size_t lineIndex = 0; lineIndex < scene.lines.size(); ++lineIndex) {
        const TextLine &sourceLine = scene.lines[lineIndex];
        const TextStyle &style = lineIndex < scene.lineStyles.size()
            ? scene.lineStyles[lineIndex]
            : scene.style;
        auto resolveFace = [&](const std::wstring &family, int weight, bool italic) {
            const std::wstring resolvedFamily = family.empty() ? L"Segoe UI" : family;
            auto face = createFontFace(
                fontCollection.Get(), resolvedFamily, weight, italic
            );
            if (!face && resolvedFamily != L"Segoe UI") {
                face = createFontFace(
                    fontCollection.Get(), L"Segoe UI", weight, italic
                );
            }
            if (!face) {
                throw BackendError("DirectWrite could not resolve a usable font face");
            }
            return face;
        };
        const auto mainFace = resolveFace(style.fontFamily, style.fontWeight, style.italic);
        const auto latinFace = resolveFace(
            style.latinFontFamily.value_or(style.fontFamily),
            style.latinFontWeight.value_or(style.fontWeight),
            style.italic
        );
        const auto rubyFace = resolveFace(
            style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily,
            style.rubyFontWeight,
            style.italic
        );
        const auto rubyLatinFace = resolveFace(
            style.rubyLatinFontFamily.value_or(
                style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily
            ),
            style.rubyLatinFontWeight.value_or(style.rubyFontWeight),
            style.italic
        );
        Impl::CachedLine cached;
        cached.style = style;
        cached.startMs = sourceLine.startMs;
        cached.endMs = sourceLine.endMs;
        cached.sourceIndex = sourceLine.sourceIndex;
        cached.compositeOrder = sourceLine.compositeOrder;
        cached.staticOverlay = sourceLine.staticOverlay;
        cached.fadeInMs = sourceLine.fadeInMs;
        cached.fadeOutMs = sourceLine.fadeOutMs;
        cached.displayWindows = sourceLine.displayWindows;
        cached.lane = style.dualLineLayout
            ? sourceLine.lane % std::max(style.laneCount, 1)
            : 0;
        cached.chars.reserve(sourceLine.chars.size());
        bool lineHasBounds = false;
        float cursor = 0.0f;
        int firstSlotDescent = 0;
        int firstSlotEdge = 0;
        int firstSlotEdge2 = 0;
        int maxDrawHeight = 1;
        bool hasFirstSlot = false;

        for (std::size_t charIndex = 0; charIndex < sourceLine.chars.size(); ++charIndex) {
            const TextChar &sourceChar = sourceLine.chars[charIndex];
            const bool hasCharStyle = sourceChar.styleIndex >= 0
                && sourceChar.styleIndex < static_cast<int>(scene.charStyles.size());
            const TextStyle &charStyle = hasCharStyle
                ? scene.charStyles[static_cast<std::size_t>(sourceChar.styleIndex)]
                : style;
            cached.hasInlineStyles = cached.hasInlineStyles || hasCharStyle;
            const bool latin = isLatinText(sourceChar.text);
            Microsoft::WRL::ComPtr<IDWriteFontFace> requestedFace = latin
                ? latinFace
                : mainFace;
            if (hasCharStyle) {
                requestedFace = resolveFace(
                    latin
                        ? charStyle.latinFontFamily.value_or(charStyle.fontFamily)
                        : charStyle.fontFamily,
                    latin
                        ? charStyle.latinFontWeight.value_or(charStyle.fontWeight)
                        : charStyle.fontWeight,
                    charStyle.italic
                );
            }
            const float fontSize = latin
                ? charStyle.latinFontSize.value_or(charStyle.fontSize)
                : charStyle.fontSize;
            const int unit = std::max(static_cast<int>(fontSize), 1);
            const int edgeSize = std::max(static_cast<int>(charStyle.strokeWidth), 0);
            cached.maxVisualPad = std::max(
                cached.maxVisualPad,
                std::ceil((
                    std::max(charStyle.strokeWidth, 0.0f)
                    + std::max(charStyle.stroke2Width, 0.0f)
                ) * 0.5f)
            );

            DWRITE_FONT_METRICS fontMetrics{};
            requestedFace->GetMetrics(&fontMetrics);
            if (!hasFirstSlot) {
                const int metricTotal = std::max(
                    static_cast<int>(fontMetrics.ascent)
                        + static_cast<int>(fontMetrics.descent),
                    1
                );
                firstSlotDescent = unit * static_cast<int>(fontMetrics.descent)
                    / metricTotal;
                firstSlotEdge = edgeSize;
                firstSlotEdge2 = std::max(
                    static_cast<int>(charStyle.stroke2Width), 0
                );
                hasFirstSlot = true;
            }
            maxDrawHeight = std::max(maxDrawHeight, unit + edgeSize);
            // The product's lane boxes remain Painter-compatible. N3's exact
            // glyph bearings/outline are used inside those boxes, while the
            // face's em scale keeps mixed-font baselines close to QFontMetrics.
            const float verticalUnits = static_cast<float>(std::max<UINT16>(
                fontMetrics.designUnitsPerEm,
                1
            ));
            cached.ascent = std::max(
                cached.ascent,
                static_cast<float>(unit) * static_cast<float>(fontMetrics.ascent) / verticalUnits
            );
            cached.descent = std::max(
                cached.descent,
                static_cast<float>(unit) * static_cast<float>(fontMetrics.descent) / verticalUnits
            );
            const float boxMetricTotal = static_cast<float>(std::max(
                static_cast<int>(fontMetrics.ascent) + static_cast<int>(fontMetrics.descent),
                1
            ));
            const float charBoxAscent =
                static_cast<float>(unit) * static_cast<float>(fontMetrics.ascent) / boxMetricTotal
                + static_cast<float>(edgeSize) * 0.5f;
            if (!isWhitespaceText(sourceChar.text) && charStyle.affectsRubyAnchor) {
                cached.boxAscent = std::max(cached.boxAscent, charBoxAscent);
                cached.hasRubyAnchor = true;
            }

            std::vector<UINT16> glyphs = glyphIndices(requestedFace.Get(), sourceChar.text);
            Microsoft::WRL::ComPtr<IDWriteFontFace> outlineFace = requestedFace;
            if (!validGlyphIndices(glyphs)) {
                outlineFace = findFallbackFontFace(
                    fontCollection.Get(), sourceChar.text, fallbackFaces, glyphs
                );
            }

            Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
            if (outlineFace && !glyphs.empty()) {
                checkHr(
                    device_.d2dFactory()->CreatePathGeometry(path.ReleaseAndGetAddressOf()),
                    "ID2D1Factory::CreatePathGeometry(character)",
                    device_
                );
                Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
                checkHr(path->Open(sink.ReleaseAndGetAddressOf()), "ID2D1PathGeometry::Open(character)", device_);
                sink->SetFillMode(D2D1_FILL_MODE_WINDING);
                sink->SetSegmentFlags(D2D1_PATH_SEGMENT_FORCE_ROUND_LINE_JOIN);
                const HRESULT outlineResult = outlineFace->GetGlyphRunOutline(
                    static_cast<float>(unit),
                    glyphs.data(),
                    nullptr,
                    nullptr,
                    static_cast<UINT32>(glyphs.size()),
                    FALSE,
                    FALSE,
                    sink.Get()
                );
                const HRESULT closeResult = sink->Close();
                checkHr(outlineResult, "IDWriteFontFace::GetGlyphRunOutline", device_);
                checkHr(closeResult, "ID2D1GeometrySink::Close(character)", device_);
            }

            D2D1_RECT_F charBounds{};
            bool charHasBounds = path != nullptr;
            if (path) {
                checkHr(path->GetBounds(nullptr, &charBounds), "ID2D1Geometry::GetBounds(character)", device_);
                charHasBounds = std::isfinite(charBounds.left)
                    && std::isfinite(charBounds.top)
                    && std::isfinite(charBounds.right)
                    && std::isfinite(charBounds.bottom)
                    && charBounds.right > charBounds.left;
            }

            float layoutWidth = 0.0f;
            float pathOffset = 0.0f;
            if (charHasBounds) {
                std::vector<DWRITE_GLYPH_METRICS> metrics(glyphs.size());
                // N3 deliberately asks the originally requested face for
                // metrics even when the outline came from a fallback face.
                checkHr(
                    requestedFace->GetDesignGlyphMetrics(
                        glyphs.data(),
                        static_cast<UINT32>(glyphs.size()),
                        metrics.data(),
                        FALSE
                    ),
                    "IDWriteFontFace::GetDesignGlyphMetrics(character)",
                    device_
                );
                const int inkWidth = std::max(static_cast<int>(charBounds.right - charBounds.left), 0);
                int leftBearing = metrics.front().leftSideBearing;
                int rightBearing = metrics.front().rightSideBearing;
                if (!charStyle.allowBiting) {
                    leftBearing = std::max(leftBearing, 0);
                    rightBearing = std::max(rightBearing, 0);
                }
                const int advance = std::max(static_cast<int>(metrics.front().advanceWidth), 1);
                const int bodyWidth = inkWidth * (leftBearing + advance + rightBearing) / advance;
                layoutWidth = static_cast<float>(std::max(bodyWidth, 0) + edgeSize);
                const int geometryLeft = inkWidth * leftBearing / advance;
                pathOffset = -charBounds.left
                    + static_cast<float>(geometryLeft)
                    + static_cast<float>(edgeSize) * 0.5f;
            } else if (sourceChar.text == L" ") {
                layoutWidth = static_cast<float>(
                    unit * std::clamp(charStyle.spaceWidthPercent, 10, 100) / 100 + edgeSize
                );
            } else {
                layoutWidth = static_cast<float>(
                    unit * std::clamp(charStyle.spaceWidthPercent, 10, 100) * 25 / 100 / 10
                    + edgeSize
                );
            }

            D2D1_RECT_F positionedCharBounds{};
            bool positionedHasBounds = false;
            if (path && charHasBounds) {
                const D2D1_MATRIX_3X2_F position = D2D1::Matrix3x2F::Translation(
                    cursor + pathOffset,
                    0.0f
                );
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> positioned;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        path.Get(),
                        &position,
                        positioned.ReleaseAndGetAddressOf()
                    ),
                    "ID2D1Factory::CreateTransformedGeometry(position character)",
                    device_
                );
                D2D1_RECT_F bounds{};
                checkHr(positioned->GetBounds(nullptr, &bounds), "ID2D1Geometry::GetBounds(positioned character)", device_);
                extendBounds(positionedCharBounds, positionedHasBounds, bounds);
                extendBounds(cached.bounds, lineHasBounds, bounds);
                cached.geometries.push_back(positioned);
            }
            const float wipePad = std::max(charStyle.strokeWidth, 0.0f) * 0.5f;
            cached.chars.push_back(Impl::CachedChar{
                sourceChar.startMs,
                sourceChar.endMs,
                positionedHasBounds ? positionedCharBounds.left - wipePad : cursor,
                positionedHasBounds ? positionedCharBounds.right + wipePad : cursor + layoutWidth,
                cursor,
                cursor + layoutWidth,
            });
            cached.chars.back().styleIndex = sourceChar.styleIndex;
            cached.chars.back().boxAscent = charBoxAscent;
            if (positionedHasBounds) {
                cached.chars.back().geometry = cached.geometries.back();
                if (charStyle.strokeWidth > 0.0f
                    && (paintNeedsBodyProtection(charStyle.beforeFillPaint)
                        || paintNeedsBodyProtection(charStyle.afterFillPaint))) {
                    cached.chars.back().protectedStrokeGeometry = outsideStrokeGeometry(
                        device_.d2dFactory(),
                        cached.chars.back().geometry.Get(),
                        charStyle.strokeWidth,
                        device_
                    );
                }
            }
            cursor += layoutWidth;
            if (charIndex + 1 < sourceLine.chars.size()) {
                cursor += charStyle.letterSpacing;
            }
        }

        if (hasFirstSlot) {
            const float drawBottom = static_cast<float>(
                firstSlotDescent + firstSlotEdge / 2
            );
            const float inset = static_cast<float>(
                (firstSlotEdge + firstSlotEdge2) / 2
            );
            cached.fillBounds = D2D1::RectF(
                0.0f,
                drawBottom - static_cast<float>(maxDrawHeight) + inset,
                std::max(cursor, 1.0f),
                std::max(drawBottom - inset, drawBottom - static_cast<float>(maxDrawHeight) + inset + 1.0f)
            );
        }

        if (!cached.hasRubyAnchor) {
            for (const TextRuby &ruby : sourceLine.rubies) {
                const int first = std::max(ruby.firstCharIndex, 0);
                const int last = std::min(
                    ruby.lastCharIndex,
                    static_cast<int>(cached.chars.size()) - 1
                );
                for (int index = first; index <= last; ++index) {
                    cached.boxAscent = std::max(
                        cached.boxAscent,
                        cached.chars[static_cast<std::size_t>(index)].boxAscent
                    );
                }
            }
        }

        for (const TextRuby &sourceRuby : sourceLine.rubies) {
            if (sourceRuby.units.empty()
                || sourceRuby.firstCharIndex < 0
                || sourceRuby.lastCharIndex < sourceRuby.firstCharIndex
                || sourceRuby.lastCharIndex >= static_cast<int>(cached.chars.size())) {
                continue;
            }
            const bool hasRubyStyle = sourceRuby.styleIndex >= 0
                && sourceRuby.styleIndex < static_cast<int>(scene.charStyles.size());
            const TextStyle &rubyStyle = hasRubyStyle
                ? scene.charStyles[static_cast<std::size_t>(sourceRuby.styleIndex)]
                : style;
            const auto selectedRubyFace = hasRubyStyle
                ? resolveFace(
                    rubyStyle.rubyFontFamily.empty()
                        ? rubyStyle.fontFamily
                        : rubyStyle.rubyFontFamily,
                    rubyStyle.rubyFontWeight,
                    rubyStyle.italic
                )
                : rubyFace;
            const auto selectedRubyLatinFace = hasRubyStyle
                ? resolveFace(
                    rubyStyle.rubyLatinFontFamily.value_or(
                        rubyStyle.rubyFontFamily.empty()
                            ? rubyStyle.fontFamily
                            : rubyStyle.rubyFontFamily
                    ),
                    rubyStyle.rubyLatinFontWeight.value_or(rubyStyle.rubyFontWeight),
                    rubyStyle.italic
                )
                : rubyLatinFace;
            struct RubyGlyph {
                const RubyUnit *source = nullptr;
                Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
                D2D1_RECT_F bounds{};
                float layoutWidth = 0.0f;
                float pathOffset = 0.0f;
            };
            std::vector<RubyGlyph> rubyGlyphs;
            rubyGlyphs.reserve(sourceRuby.units.size());
            float naturalWidth = 0.0f;
            float rubyBoxDescent = 0.0f;
            const int rubyEdgeSize = std::max(
                static_cast<int>(rubyStyle.rubyStrokeWidth), 0
            );
            const int rubyAnchorEdgeSize = std::max(
                static_cast<int>(style.rubyStrokeWidth), 0
            );

            for (const RubyUnit &sourceUnit : sourceRuby.units) {
                const bool latin = isLatinText(sourceUnit.text);
                const auto &measureFace = latin
                    ? selectedRubyLatinFace
                    : selectedRubyFace;
                const auto &drawingFace = latin ? rubyLatinFace : rubyFace;
                const float measureFontSize = latin
                    ? rubyStyle.rubyLatinFontSize.value_or(rubyStyle.rubyFontSize)
                    : rubyStyle.rubyFontSize;
                const float drawingFontSize = latin
                    ? style.rubyLatinFontSize.value_or(style.rubyFontSize)
                    : style.rubyFontSize;
                const int measureUnit = std::max(static_cast<int>(measureFontSize), 1);
                const int drawingUnit = std::max(static_cast<int>(drawingFontSize), 1);
                DWRITE_FONT_METRICS fontMetrics{};
                drawingFace->GetMetrics(&fontMetrics);
                const float boxMetricTotal = static_cast<float>(std::max(
                    static_cast<int>(fontMetrics.ascent) + static_cast<int>(fontMetrics.descent),
                    1
                ));
                rubyBoxDescent = std::max(
                    rubyBoxDescent,
                    static_cast<float>(drawingUnit)
                        * static_cast<float>(fontMetrics.descent) / boxMetricTotal
                        + static_cast<float>(rubyAnchorEdgeSize) * 0.5f
                );

                std::vector<UINT16> glyphs = glyphIndices(drawingFace.Get(), sourceUnit.text);
                Microsoft::WRL::ComPtr<IDWriteFontFace> outlineFace = drawingFace;
                if (!validGlyphIndices(glyphs)) {
                    outlineFace = findFallbackFontFace(
                        fontCollection.Get(), sourceUnit.text, fallbackFaces, glyphs
                    );
                }
                Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
                if (outlineFace && !glyphs.empty()) {
                    checkHr(
                        device_.d2dFactory()->CreatePathGeometry(path.ReleaseAndGetAddressOf()),
                        "ID2D1Factory::CreatePathGeometry(ruby character)",
                        device_
                    );
                    Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
                    checkHr(
                        path->Open(sink.ReleaseAndGetAddressOf()),
                        "ID2D1PathGeometry::Open(ruby character)",
                        device_
                    );
                    sink->SetFillMode(D2D1_FILL_MODE_WINDING);
                    sink->SetSegmentFlags(D2D1_PATH_SEGMENT_FORCE_ROUND_LINE_JOIN);
                    const HRESULT outlineResult = outlineFace->GetGlyphRunOutline(
                        static_cast<float>(drawingUnit),
                        glyphs.data(),
                        nullptr,
                        nullptr,
                        static_cast<UINT32>(glyphs.size()),
                        FALSE,
                        FALSE,
                        sink.Get()
                    );
                    const HRESULT closeResult = sink->Close();
                    checkHr(outlineResult, "IDWriteFontFace::GetGlyphRunOutline(ruby)", device_);
                    checkHr(closeResult, "ID2D1GeometrySink::Close(ruby character)", device_);
                }

                RubyGlyph glyph;
                glyph.source = &sourceUnit;
                glyph.geometry = path;
                bool hasBounds = path != nullptr;
                if (path) {
                    checkHr(
                        path->GetBounds(nullptr, &glyph.bounds),
                        "ID2D1Geometry::GetBounds(ruby character)",
                        device_
                    );
                    hasBounds = std::isfinite(glyph.bounds.left)
                        && std::isfinite(glyph.bounds.right)
                        && glyph.bounds.right > glyph.bounds.left;
                }
                if (hasBounds) {
                    std::vector<UINT16> measureGlyphs = glyphIndices(
                        measureFace.Get(), sourceUnit.text
                    );
                    Microsoft::WRL::ComPtr<IDWriteFontFace> metricFace = measureFace;
                    if (!validGlyphIndices(measureGlyphs)) {
                        metricFace = findFallbackFontFace(
                            fontCollection.Get(), sourceUnit.text, fallbackFaces, measureGlyphs
                        );
                    }
                    std::vector<DWRITE_GLYPH_METRICS> metrics(measureGlyphs.size());
                    checkHr(
                        metricFace->GetDesignGlyphMetrics(
                            measureGlyphs.data(),
                            static_cast<UINT32>(measureGlyphs.size()),
                            metrics.data(),
                            FALSE
                        ),
                        "IDWriteFontFace::GetDesignGlyphMetrics(ruby character)",
                        device_
                    );
                    const int drawingInkWidth = std::max(
                        static_cast<int>(glyph.bounds.right - glyph.bounds.left), 0
                    );
                    const int inkWidth = drawingUnit > 0
                        ? drawingInkWidth * measureUnit / drawingUnit
                        : drawingInkWidth;
                    int leftBearing = metrics.front().leftSideBearing;
                    int rightBearing = metrics.front().rightSideBearing;
                    if (!rubyStyle.allowBiting) {
                        leftBearing = std::max(leftBearing, 0);
                        rightBearing = std::max(rightBearing, 0);
                    }
                    const int advance = std::max(static_cast<int>(metrics.front().advanceWidth), 1);
                    const int bodyWidth = inkWidth * (leftBearing + advance + rightBearing) / advance;
                    glyph.layoutWidth = static_cast<float>(
                        std::max(bodyWidth, 0) + rubyEdgeSize
                    );
                    const int geometryLeft = inkWidth * leftBearing / advance;
                    glyph.pathOffset = -glyph.bounds.left
                        + static_cast<float>(geometryLeft)
                        + static_cast<float>(rubyEdgeSize) * 0.5f;
                } else if (sourceUnit.text == L" ") {
                    glyph.layoutWidth = static_cast<float>(
                        measureUnit * std::clamp(rubyStyle.spaceWidthPercent, 10, 100) / 100
                            + rubyEdgeSize
                    );
                } else {
                    glyph.layoutWidth = static_cast<float>(
                        measureUnit * std::clamp(rubyStyle.spaceWidthPercent, 10, 100) * 25 / 100 / 10
                            + rubyEdgeSize
                    );
                }
                naturalWidth += glyph.layoutWidth;
                rubyGlyphs.push_back(std::move(glyph));
            }
            if (rubyGlyphs.empty()) {
                continue;
            }

            const float targetLeft = cached.chars[
                static_cast<std::size_t>(sourceRuby.firstCharIndex)
            ].layoutLeft;
            const float targetRight = cached.chars[
                static_cast<std::size_t>(sourceRuby.lastCharIndex)
            ].layoutRight;
            const float targetWidth = std::max(targetRight - targetLeft, 1.0f);
            const bool centered = rubyStyle.rubyAlignment == "center"
                || (rubyStyle.rubyAlignment != "equal_space" && (
                    isAsciiAlnumText(sourceRuby.baseText)
                    || isAsciiAlnumText(sourceRuby.reading)
                ));
            float gap = rubyStyle.rubyInterval;
            if (!centered && rubyGlyphs.size() > 1) {
                const float slots = targetWidth <= naturalWidth
                    ? static_cast<float>(rubyGlyphs.size() - 1)
                    : static_cast<float>(rubyGlyphs.size() + 1);
                gap = std::max(
                    (targetWidth - naturalWidth) / std::max(slots, 1.0f),
                    rubyStyle.rubyInterval
                );
            }
            const float contentWidth = naturalWidth
                + gap * static_cast<float>(rubyGlyphs.size() - 1);
            float rubyCursor = targetLeft + (targetWidth - contentWidth) * 0.5f;
            if (centered || rubyGlyphs.size() == 1) {
                rubyCursor = targetLeft
                    + static_cast<float>(static_cast<int>(targetWidth - contentWidth) / 2);
            }

            Impl::CachedRuby ruby;
            ruby.startMs = sourceRuby.startMs;
            ruby.endMs = sourceRuby.endMs;
            ruby.styleIndex = sourceRuby.styleIndex;
            // Painter anchors every ruby run with the line-level ruby font and
            // gap; target-role styling changes paint/measurement, not baseline.
            ruby.baselineOffset = -cached.boxAscent - style.rubyGap - rubyBoxDescent;
            DWRITE_FONT_METRICS rubyFillMetrics{};
            rubyFace->GetMetrics(&rubyFillMetrics);
            const int rubyMetricTotal = std::max(
                static_cast<int>(rubyFillMetrics.ascent)
                    + static_cast<int>(rubyFillMetrics.descent),
                1
            );
            const int rubyFillSize = std::max(
                static_cast<int>(rubyStyle.rubyFontSize), 1
            );
            const int rubyFillDescent = rubyFillSize
                * static_cast<int>(rubyFillMetrics.descent) / rubyMetricTotal;
            const int rubyDrawEdge = std::max(
                static_cast<int>(rubyStyle.rubyStrokeWidth), 0
            );
            const int rubyDrawEdge2 = std::max(
                static_cast<int>(rubyStyle.rubyStroke2Width), 0
            );
            const float rubyDrawBottom = ruby.baselineOffset
                + static_cast<float>(rubyFillDescent + rubyDrawEdge / 2);
            const float rubyInset = static_cast<float>(
                (rubyDrawEdge + rubyDrawEdge2) / 2
            );
            ruby.fillBounds = D2D1::RectF(
                targetLeft,
                rubyDrawBottom - static_cast<float>(rubyFillSize + rubyDrawEdge)
                    + rubyInset,
                targetRight,
                std::max(
                    rubyDrawBottom - rubyInset,
                    rubyDrawBottom - static_cast<float>(rubyFillSize + rubyDrawEdge)
                        + rubyInset + 1.0f
                )
            );
            bool rubyHasBounds = false;
            for (std::size_t unitIndex = 0; unitIndex < rubyGlyphs.size(); ++unitIndex) {
                RubyGlyph &glyph = rubyGlyphs[unitIndex];
                const float origin = (centered || rubyGlyphs.size() == 1)
                    ? rubyCursor
                    : static_cast<float>(static_cast<int>(rubyCursor));
                D2D1_RECT_F positionedBounds{};
                bool positionedHasBounds = false;
                if (glyph.geometry) {
                    const D2D1_MATRIX_3X2_F position = D2D1::Matrix3x2F::Translation(
                        origin + glyph.pathOffset,
                        ruby.baselineOffset
                    );
                    Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> positioned;
                    checkHr(
                        device_.d2dFactory()->CreateTransformedGeometry(
                            glyph.geometry.Get(),
                            &position,
                            positioned.ReleaseAndGetAddressOf()
                        ),
                        "ID2D1Factory::CreateTransformedGeometry(position ruby character)",
                        device_
                    );
                    checkHr(
                        positioned->GetBounds(nullptr, &positionedBounds),
                        "ID2D1Geometry::GetBounds(positioned ruby character)",
                        device_
                    );
                    positionedHasBounds = positionedBounds.right > positionedBounds.left;
                    if (positionedHasBounds) {
                        extendBounds(ruby.bounds, rubyHasBounds, positionedBounds);
                    }
                    ruby.geometries.push_back(positioned);
                    if (rubyStyle.rubyStrokeWidth > 0.0f
                        && (paintNeedsBodyProtection(rubyStyle.rubyBeforeFillPaint)
                            || paintNeedsBodyProtection(rubyStyle.rubyAfterFillPaint))) {
                        ruby.protectedStrokeGeometries.push_back(
                            outsideStrokeGeometry(
                                device_.d2dFactory(),
                                positioned.Get(),
                                rubyStyle.rubyStrokeWidth,
                                device_
                            )
                        );
                    } else {
                        ruby.protectedStrokeGeometries.push_back({});
                    }
                }
                const float wipePad = std::max(rubyStyle.rubyStrokeWidth, 0.0f) * 0.5f;
                ruby.chars.push_back(Impl::CachedChar{
                    glyph.source->startMs,
                    glyph.source->endMs,
                    positionedHasBounds ? positionedBounds.left - wipePad : origin,
                    positionedHasBounds
                        ? positionedBounds.right + wipePad
                        : origin + glyph.layoutWidth,
                    origin,
                    origin + glyph.layoutWidth,
                });
                rubyCursor += glyph.layoutWidth;
                if (unitIndex + 1 < rubyGlyphs.size()) {
                    rubyCursor += gap;
                }
            }
            if (rubyHasBounds && !ruby.geometries.empty()) {
                cached.rubies.push_back(std::move(ruby));
            }
        }
        if (!lineHasBounds) {
            cached.bounds = D2D1::RectF(0.0f, 0.0f, 0.0f, 0.0f);
        }
        impl_->lines.push_back(std::move(cached));
    }
    impl_->diagnostics.lineCount = impl_->lines.size();
    impl_->diagnostics.charCount = 0;
    impl_->diagnostics.geometryCount = 0;
    impl_->diagnostics.rubyCount = 0;
    impl_->diagnostics.styleCount = 1
        + scene.lineStyles.size()
        + scene.charStyles.size();
    impl_->diagnostics.estimatedCacheBytes = sizeof(Impl)
        + scene.lineStyles.capacity() * sizeof(TextStyle)
        + scene.charStyles.capacity() * sizeof(TextStyle);
    for (const Impl::CachedImage &image : impl_->images) {
        impl_->diagnostics.estimatedCacheBytes += sizeof(Impl::CachedImage)
            + image.path.capacity() * sizeof(wchar_t);
        if (image.bitmap) {
            const D2D1_SIZE_U size = image.bitmap->GetPixelSize();
            impl_->diagnostics.estimatedCacheBytes += static_cast<std::uint64_t>(
                size.width
            ) * static_cast<std::uint64_t>(size.height) * 4;
        }
    }
    for (const Impl::CachedLine &line : impl_->lines) {
        impl_->diagnostics.charCount += line.chars.size();
        impl_->diagnostics.geometryCount += line.geometries.size();
        impl_->diagnostics.geometryCount += static_cast<std::uint64_t>(std::count_if(
            line.chars.begin(), line.chars.end(), [](const Impl::CachedChar &ch) {
                return ch.protectedStrokeGeometry != nullptr;
            }
        ));
        impl_->diagnostics.estimatedCacheBytes += sizeof(Impl::CachedLine)
            + line.chars.capacity() * sizeof(Impl::CachedChar)
            + line.geometries.capacity() * sizeof(Microsoft::WRL::ComPtr<ID2D1Geometry>);
        impl_->diagnostics.rubyCount += line.rubies.size();
        for (const Impl::CachedRuby &ruby : line.rubies) {
            impl_->diagnostics.charCount += ruby.chars.size();
            impl_->diagnostics.geometryCount += ruby.geometries.size();
            impl_->diagnostics.geometryCount += static_cast<std::uint64_t>(std::count_if(
                ruby.protectedStrokeGeometries.begin(),
                ruby.protectedStrokeGeometries.end(),
                [](const auto &geometry) { return geometry != nullptr; }
            ));
            impl_->diagnostics.estimatedCacheBytes += sizeof(Impl::CachedRuby)
                + ruby.chars.capacity() * sizeof(Impl::CachedChar)
                + ruby.geometries.capacity() * sizeof(Microsoft::WRL::ComPtr<ID2D1Geometry>)
                + ruby.protectedStrokeGeometries.capacity()
                    * sizeof(Microsoft::WRL::ComPtr<ID2D1Geometry>);
        }
    }
    // Direct2D does not expose path allocation bytes. Keep a conservative
    // diagnostic estimate so cache growth/churn remains observable.
    impl_->diagnostics.estimatedCacheBytes += impl_->diagnostics.geometryCount * 256;
    impl_->configured = true;
}

ProbeResult Direct2DGpuBackend::renderFrame(int tMs, bool compactBands) {
    if (!impl_->configured) {
        throw BackendError("GPU backend is not configured");
    }
    const RenderScene &scene = impl_->scene;
    const TextStyle &baseStyle = scene.style;

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
    if (!impl_->frameTargetTexture || !impl_->frameTargetBitmap || !impl_->frameStagingTexture) {
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
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    context->SetTarget(nullptr);

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
            if (line.fadeInMs > 0 && tMs < window.startMs + line.fadeInMs) {
                opacity = std::min(
                    opacity,
                    static_cast<float>(tMs - window.startMs)
                        / static_cast<float>(line.fadeInMs)
                );
            }
            if (line.fadeOutMs > 0 && tMs > window.endMs - line.fadeOutMs) {
                opacity = std::min(
                    opacity,
                    static_cast<float>(window.endMs - tMs)
                        / static_cast<float>(line.fadeOutMs)
                );
            }
            best = std::max(best, std::clamp(opacity, 0.0f, 1.0f));
        }
        return best;
    };
    std::vector<const Impl::CachedLine *> activeLines;
    for (const Impl::CachedLine &candidate : impl_->lines) {
        const bool resolvedWindowVisible = !candidate.displayWindows.empty()
            && std::any_of(
                candidate.displayWindows.begin(), candidate.displayWindows.end(),
                [&](const DisplayWindow &window) {
                    return window.endMs > window.startMs
                        && tMs >= window.startMs
                        && tMs <= window.endMs;
                }
            );
        const bool visible = candidate.staticOverlay
            ? overlayOpacityAt(candidate) > 0.0f
            : (!candidate.displayWindows.empty() ? resolvedWindowVisible : (
                tMs >= candidate.startMs - std::max(baseStyle.leadInMs, 0)
                && tMs <= candidate.endMs + std::max(baseStyle.tailMs, 0)
            ));
        if (visible) {
            const bool laneAlreadyActive = std::any_of(
                activeLines.begin(), activeLines.end(),
                [&](const Impl::CachedLine *line) {
                    return line->sourceIndex == candidate.sourceIndex
                        && line->lane == candidate.lane;
                }
            );
            if (!laneAlreadyActive) {
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
    bool renderedAnyLine = false;
    std::vector<std::pair<int, int>> readbackIntervals;
    for (const Impl::CachedLine *line : activeLines) {
      if (line != nullptr && !line->geometries.empty()) {
        const float globalOpacity = overlayOpacityAt(*line);
        const TextStyle &style = line->style;
        const float inkWidth = line->bounds.right - line->bounds.left;
        float dx = (static_cast<float>(scene.width) - inkWidth) * 0.5f - line->bounds.left;
        dx += style.centerOffsetX;
        if (style.alignment == "left") {
            dx = style.horizontalMargin - line->bounds.left;
        } else if (style.alignment == "right") {
            dx = static_cast<float>(scene.width) - style.horizontalMargin - line->bounds.right;
        }
        const float visualPad = line->hasInlineStyles
            ? line->maxVisualPad
            : std::ceil(
                (std::max(style.strokeWidth, 0.0f)
                    + std::max(style.stroke2Width, 0.0f)) * 0.5f
            );
        const float ascent = line->ascent > 0.0f ? line->ascent : -line->bounds.top;
        const float descent = line->descent > 0.0f ? line->descent : line->bounds.bottom;
        const int lanes = style.dualLineLayout ? std::max(style.laneCount, 1) : 1;
        const float rubyExtra = line->rubies.empty()
            ? 0.0f
            : std::max(
                style.rubyGap + style.rubyFontSize
                    + std::max(style.rubyStrokeWidth, 0.0f),
                0.0f
            );
        const float mainHeight = ascent + descent + visualPad * 2.0f;
        const float step = mainHeight + style.lineGap;
        float firstBaseline = static_cast<float>(scene.height) - style.bottomMargin
            - descent - visualPad - step * static_cast<float>(lanes - 1);
        if (style.verticalPosition == "top") {
            firstBaseline = style.bottomMargin + rubyExtra + ascent + visualPad;
        } else if (style.verticalPosition == "center") {
            const float totalHeight = mainHeight * static_cast<float>(lanes)
                + style.lineGap * static_cast<float>(lanes - 1);
            firstBaseline = (static_cast<float>(scene.height) - totalHeight) * 0.5f
                + ascent + visualPad;
            if (lanes == 1) {
                if (line->rubies.empty()) {
                    firstBaseline = (static_cast<float>(scene.height)
                        - (line->bounds.bottom - line->bounds.top)) * 0.5f
                        - line->bounds.top;
                } else {
                    const float blockHeight = mainHeight + rubyExtra;
                    firstBaseline = (static_cast<float>(scene.height) - blockHeight) * 0.5f
                        + rubyExtra + visualPad + ascent;
                }
            }
        }
        if (style.verticalPosition == "center") {
            firstBaseline += style.centerOffsetY;
        }
        const float dy = firstBaseline + step * static_cast<float>(line->lane);
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
        for (const Impl::CachedRuby &ruby : line->rubies) {
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
        const int intervalTop = std::clamp(
            static_cast<int>(std::floor(dy + contentTop - topPad)),
            0,
            scene.height
        );
        const int intervalBottom = std::clamp(
            static_cast<int>(std::ceil(dy + contentBottom + bottomPad)),
            0,
            scene.height
        );
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
        auto paintBrushAt = [&](const PaintStyle &paint, const D2D1_RECT_F &rect,
                                const RgbaColor &fallback,
                                float offsetX, float offsetY) {
            auto brush = createPaintBrush(
                context, paint, rect, fallback, device_,
                imageForPaint(paint), dx + offsetX, dy + offsetY
            );
            if (brush) {
                brush->SetOpacity(globalOpacity);
            }
            return brush;
        };
        auto paintBrush = [&](const PaintStyle &paint, const D2D1_RECT_F &rect,
                              const RgbaColor &fallback) {
            return paintBrushAt(paint, rect, fallback, 0.0f, 0.0f);
        };
        Microsoft::WRL::ComPtr<ID2D1Brush> beforeFill = paintBrush(
            style.beforeFillPaint, line->fillBounds, style.beforeFill
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> afterFill = paintBrush(
            style.afterFillPaint, line->fillBounds, style.afterFill
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> beforeStroke = paintBrush(
            style.beforeStrokePaint, line->fillBounds, style.beforeStroke
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> afterStroke = paintBrush(
            style.afterStrokePaint, line->fillBounds, style.afterStroke
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> beforeStroke2 = paintBrush(
            style.beforeStroke2Paint, line->fillBounds, style.beforeStroke2
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> afterStroke2 = paintBrush(
            style.afterStroke2Paint, line->fillBounds, style.afterStroke2
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> beforeDecor = paintBrush(
            style.beforeDecorPaint, line->fillBounds, style.beforeDecor
        );
        Microsoft::WRL::ComPtr<ID2D1Brush> afterDecor = paintBrush(
            style.afterDecorPaint, line->fillBounds, style.afterDecor
        );

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
        auto rubyWipeEdgeAt = [&](const Impl::CachedRuby &ruby) {
            float edge = ruby.bounds.left;
            for (const Impl::CachedChar &ch : ruby.chars) {
                if (tMs >= ch.endMs) {
                    edge = std::max(edge, ch.right);
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
                edge = ch.left + (ch.right - ch.left) * ratio;
                break;
            }
            return edge;
        };
        auto rubyStyleFor = [&](int styleIndex) -> const TextStyle & {
            return styleIndex >= 0
                && styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(styleIndex)]
                : style;
        };

        Microsoft::WRL::ComPtr<ID2D1Bitmap1> glowSource;
        Microsoft::WRL::ComPtr<ID2D1Effect> blur;
        std::vector<int> glowSigmas;
        if (style.decorationKind == "glow" && !line->hasInlineStyles) {
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

        struct RubyGlowLayer {
            Microsoft::WRL::ComPtr<ID2D1Bitmap1> source;
            Microsoft::WRL::ComPtr<ID2D1Effect> blur;
            std::vector<int> sigmas;
        };
        std::vector<RubyGlowLayer> rubyGlowLayers;
        auto appendRubyGlowLayer = [&](int styleIndex, bool after) {
            const TextStyle &rubyStyle = rubyStyleFor(styleIndex);
            const float requestedRadius = after
                ? rubyStyle.rubyGlowAfterRadius
                : rubyStyle.rubyGlowBeforeRadius;
            const int radius = std::max(
                0, static_cast<int>(std::lround(requestedRadius))
            );
            const bool hasVisibleSource = std::any_of(
                line->rubies.begin(),
                line->rubies.end(),
                [&](const Impl::CachedRuby &ruby) {
                    const float edge = rubyWipeEdgeAt(ruby);
                    return ruby.styleIndex == styleIndex
                        && !((after && edge <= ruby.bounds.left)
                            || (!after && edge >= ruby.bounds.right));
                }
            );
            if (rubyStyle.rubyDecorationKind != "glow"
                || radius <= 0
                || !hasVisibleSource) {
                return;
            }
            RubyGlowLayer layer;
            checkHr(
                context->CreateBitmap(
                    D2D1::SizeU(static_cast<UINT32>(scene.width), static_cast<UINT32>(scene.height)),
                    nullptr,
                    0,
                    &bitmapProperties,
                    layer.source.ReleaseAndGetAddressOf()
                ),
                "ID2D1DeviceContext::CreateBitmap(ruby glow source)",
                device_
            );
            checkHr(
                context->CreateEffect(CLSID_D2D1GaussianBlur, layer.blur.ReleaseAndGetAddressOf()),
                "ID2D1DeviceContext::CreateEffect(ruby GaussianBlur)",
                device_
            );
            const float sourceWidth = std::max(0.0f, rubyStyle.rubyStrokeWidth)
                + (rubyStyle.rubyStroke2Width > 0.0f
                    ? rubyStyle.rubyStroke2Width
                    : 0.0f)
                + static_cast<float>(radius);
            const float pad = sourceWidth * 0.5f + radius * 3.0f + 2.0f;
            context->SetTarget(layer.source.Get());
            context->SetTransform(D2D1::Matrix3x2F::Translation(dx, dy));
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            for (const Impl::CachedRuby &ruby : line->rubies) {
                if (ruby.styleIndex != styleIndex) {
                    continue;
                }
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                    after
                        ? rubyStyle.rubyAfterDecorPaint
                        : rubyStyle.rubyBeforeDecorPaint,
                    ruby.fillBounds,
                    after ? rubyStyle.rubyAfterDecor : rubyStyle.rubyBeforeDecor
                );
                const float edge = rubyWipeEdgeAt(ruby);
                if ((after && edge <= ruby.bounds.left)
                    || (!after && edge >= ruby.bounds.right)) {
                    continue;
                }
                const D2D1_RECT_F clip = after
                    ? D2D1::RectF(
                        ruby.bounds.left - pad,
                        ruby.bounds.top - pad,
                        edge,
                        ruby.bounds.bottom + pad
                    )
                    : D2D1::RectF(
                        edge,
                        ruby.bounds.top - pad,
                        ruby.bounds.right + pad,
                        ruby.bounds.bottom + pad
                    );
                context->PushAxisAlignedClip(clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
                for (const auto &geometry : ruby.geometries) {
                    context->DrawGeometry(geometry.Get(), brush.Get(), sourceWidth);
                }
                context->PopAxisAlignedClip();
            }
            checkHr(
                context->EndDraw(),
                "ID2D1DeviceContext::EndDraw(ruby glow source)",
                device_
            );
            layer.blur->SetInput(0, layer.source.Get());
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
            appendRubyGlowLayer(styleIndex, false);
            appendRubyGlowLayer(styleIndex, true);
        }

        struct InlineGlowLayer {
            Microsoft::WRL::ComPtr<ID2D1Bitmap1> source;
            Microsoft::WRL::ComPtr<ID2D1Effect> blur;
            std::vector<int> sigmas;
        };
        std::vector<InlineGlowLayer> inlineGlowLayers;
        auto appendInlineGlowLayer = [&](int styleIndex, bool after) {
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
            const bool hasVisibleSource = std::any_of(
                line->chars.begin(),
                line->chars.end(),
                [&](const Impl::CachedChar &ch) {
                    return ch.styleIndex == styleIndex
                        && ch.geometry
                        && !((after && wipeEdge <= ch.left)
                            || (!after && wipeEdge >= ch.right));
                }
            );
            if (charStyle.decorationKind != "glow" || radius <= 0 || !hasVisibleSource) {
                return;
            }
            InlineGlowLayer layer;
            checkHr(
                context->CreateBitmap(
                    D2D1::SizeU(static_cast<UINT32>(scene.width), static_cast<UINT32>(scene.height)),
                    nullptr,
                    0,
                    &bitmapProperties,
                    layer.source.ReleaseAndGetAddressOf()
                ),
                "ID2D1DeviceContext::CreateBitmap(inline glow source)",
                device_
            );
            checkHr(
                context->CreateEffect(CLSID_D2D1GaussianBlur, layer.blur.ReleaseAndGetAddressOf()),
                "ID2D1DeviceContext::CreateEffect(inline GaussianBlur)",
                device_
            );
            Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                after ? charStyle.afterDecorPaint : charStyle.beforeDecorPaint,
                line->fillBounds,
                after ? charStyle.afterDecor : charStyle.beforeDecor
            );
            const float sourceWidth = std::max(charStyle.strokeWidth, 0.0f)
                + std::max(charStyle.stroke2Width, 0.0f)
                + static_cast<float>(radius);
            const float pad = sourceWidth * 0.5f + radius * 3.0f + 2.0f;
            context->SetTarget(layer.source.Get());
            context->SetTransform(D2D1::Matrix3x2F::Translation(dx, dy));
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            for (const Impl::CachedChar &ch : line->chars) {
                if (ch.styleIndex != styleIndex || !ch.geometry
                    || (after && wipeEdge <= ch.left)
                    || (!after && wipeEdge >= ch.right)) {
                    continue;
                }
                const D2D1_RECT_F clip = after
                    ? D2D1::RectF(
                        ch.left - pad, line->bounds.top - pad,
                        wipeEdge, line->bounds.bottom + pad
                    )
                    : D2D1::RectF(
                        wipeEdge, line->bounds.top - pad,
                        ch.right + pad, line->bounds.bottom + pad
                    );
                context->PushAxisAlignedClip(clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
                context->DrawGeometry(ch.geometry.Get(), brush.Get(), sourceWidth);
                context->PopAxisAlignedClip();
            }
            checkHr(
                context->EndDraw(),
                "ID2D1DeviceContext::EndDraw(inline glow source)",
                device_
            );
            layer.blur->SetInput(0, layer.source.Get());
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
                appendInlineGlowLayer(styleIndex, false);
                appendInlineGlowLayer(styleIndex, true);
            }
        }

        context->SetTarget(targetBitmap);
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        if (!renderedAnyLine) {
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
        }
        for (RubyGlowLayer &layer : rubyGlowLayers) {
            for (int sigma : layer.sigmas) {
                checkHr(
                    layer.blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(ruby StandardDeviation)",
                    device_
                );
                context->DrawImage(layer.blur.Get());
            }
        }
        for (InlineGlowLayer &layer : inlineGlowLayers) {
            for (int sigma : layer.sigmas) {
                checkHr(
                    layer.blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(inline StandardDeviation)",
                    device_
                );
                context->DrawImage(layer.blur.Get());
            }
        }
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

        auto drawShadowSilhouette = [&](ID2D1Geometry *geometry, ID2D1Brush *brush,
                                        float strokeWidth, float stroke2Width) {
            const float outerWidth = stroke2Width > 0.0f
                ? std::max(strokeWidth, 0.0f) + stroke2Width
                : std::max(strokeWidth, 0.0f);
            if (outerWidth > 0.0f) {
                context->DrawGeometry(geometry, brush, outerWidth);
            }
            context->FillGeometry(geometry, brush);
        };
        auto drawLineShadowPhase = [&](bool after) {
            if (line->hasInlineStyles) {
                for (const Impl::CachedChar &ch : line->chars) {
                    if (!ch.geometry) {
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
                        line->fillBounds,
                        after ? charStyle.afterDecor : charStyle.beforeDecor,
                        charStyle.shadowOffsetX,
                        charStyle.shadowOffsetY
                    );
                    context->SetTransform(D2D1::Matrix3x2F::Translation(
                        dx + charStyle.shadowOffsetX,
                        dy + charStyle.shadowOffsetY
                    ));
                    if (after) {
                        context->PushAxisAlignedClip(
                            D2D1::RectF(
                                afterClip.left - charStyle.shadowOffsetX,
                                afterClip.top,
                                afterClip.right - charStyle.shadowOffsetX,
                                afterClip.bottom
                            ),
                            D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                        );
                    }
                    drawShadowSilhouette(
                        ch.geometry.Get(), brush.Get(),
                        charStyle.strokeWidth, charStyle.stroke2Width
                    );
                    if (after) {
                        context->PopAxisAlignedClip();
                    }
                }
            } else if (style.decorationKind == "shadow") {
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrushAt(
                    after ? style.afterDecorPaint : style.beforeDecorPaint,
                    line->fillBounds,
                    after ? style.afterDecor : style.beforeDecor,
                    style.shadowOffsetX,
                    style.shadowOffsetY
                );
                context->SetTransform(D2D1::Matrix3x2F::Translation(
                    dx + style.shadowOffsetX,
                    dy + style.shadowOffsetY
                ));
                if (after) {
                    context->PushAxisAlignedClip(
                        D2D1::RectF(
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
                        geometry.Get(), brush.Get(),
                        style.strokeWidth, style.stroke2Width
                    );
                }
                if (after) {
                    context->PopAxisAlignedClip();
                }
            }
            context->SetTransform(D2D1::Matrix3x2F::Translation(dx, dy));
        };
        drawLineShadowPhase(false);
        if (wipeEdge > line->bounds.left) {
            drawLineShadowPhase(true);
        }

        for (const Impl::CachedRuby &ruby : line->rubies) {
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            if (rubyStyle.rubyDecorationKind != "shadow") {
                continue;
            }
            const float edge = rubyWipeEdgeAt(ruby);
            auto drawRubyShadowPhase = [&](bool after) {
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrushAt(
                    after
                        ? rubyStyle.rubyAfterDecorPaint
                        : rubyStyle.rubyBeforeDecorPaint,
                    ruby.fillBounds,
                    after ? rubyStyle.rubyAfterDecor : rubyStyle.rubyBeforeDecor,
                    rubyStyle.rubyShadowOffsetX,
                    rubyStyle.rubyShadowOffsetY
                );
                context->SetTransform(D2D1::Matrix3x2F::Translation(
                    dx + rubyStyle.rubyShadowOffsetX,
                    dy + rubyStyle.rubyShadowOffsetY
                ));
                if (after) {
                    const float pad = std::max(
                        rubyStyle.rubyStrokeWidth + rubyStyle.rubyStroke2Width,
                        2.0f
                    ) + 4.0f;
                    context->PushAxisAlignedClip(
                        D2D1::RectF(
                            ruby.bounds.left - pad - rubyStyle.rubyShadowOffsetX,
                            ruby.bounds.top - pad,
                            edge - rubyStyle.rubyShadowOffsetX,
                            ruby.bounds.bottom + pad
                        ),
                        D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                for (const auto &geometry : ruby.geometries) {
                    drawShadowSilhouette(
                        geometry.Get(), brush.Get(),
                        rubyStyle.rubyStrokeWidth,
                        rubyStyle.rubyStroke2Width
                    );
                }
                if (after) {
                    context->PopAxisAlignedClip();
                }
            };
            drawRubyShadowPhase(false);
            if (edge > ruby.bounds.left) {
                drawRubyShadowPhase(true);
            }
        }
        context->SetTransform(D2D1::Matrix3x2F::Translation(dx, dy));

        auto drawStack = [&](bool after, ID2D1Brush *fill, ID2D1Brush *stroke, ID2D1Brush *stroke2) {
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
                const bool protect = paintNeedsBodyProtection(
                    after ? style.afterFillPaint : style.beforeFillPaint
                );
                for (const Impl::CachedChar &ch : line->chars) {
                    if (!ch.geometry) {
                        continue;
                    }
                    if (protect && ch.protectedStrokeGeometry) {
                        context->FillGeometry(ch.protectedStrokeGeometry.Get(), stroke);
                    } else {
                        context->DrawGeometry(ch.geometry.Get(), stroke, style.strokeWidth);
                    }
                }
            }
            for (const auto &geometry : line->geometries) {
                context->FillGeometry(geometry.Get(), fill);
            }
        };
        auto drawInlineStack = [&](bool after) {
            for (const Impl::CachedChar &ch : line->chars) {
                if (!ch.geometry) {
                    continue;
                }
                const TextStyle &charStyle = ch.styleIndex >= 0
                    && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                    ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                    : style;
                const RgbaColor &fillColor = after
                    ? charStyle.afterFill
                    : charStyle.beforeFill;
                const RgbaColor &strokeColor = after
                    ? charStyle.afterStroke
                    : charStyle.beforeStroke;
                const RgbaColor &stroke2Color = after
                    ? charStyle.afterStroke2
                    : charStyle.beforeStroke2;
                Microsoft::WRL::ComPtr<ID2D1Brush> fillBrush = paintBrush(
                    after ? charStyle.afterFillPaint : charStyle.beforeFillPaint,
                    line->fillBounds,
                    fillColor
                );
                Microsoft::WRL::ComPtr<ID2D1Brush> strokeBrush = paintBrush(
                    after ? charStyle.afterStrokePaint : charStyle.beforeStrokePaint,
                    line->fillBounds,
                    strokeColor
                );
                Microsoft::WRL::ComPtr<ID2D1Brush> stroke2Brush = paintBrush(
                    after ? charStyle.afterStroke2Paint : charStyle.beforeStroke2Paint,
                    line->fillBounds,
                    stroke2Color
                );
                if (charStyle.stroke2Width > 0.0f) {
                    context->DrawGeometry(
                        ch.geometry.Get(),
                        stroke2Brush.Get(),
                        std::max(0.0f, charStyle.strokeWidth) + charStyle.stroke2Width
                    );
                }
                if (charStyle.strokeWidth > 0.0f) {
                    const bool protect = paintNeedsBodyProtection(
                        after ? charStyle.afterFillPaint : charStyle.beforeFillPaint
                    );
                    if (protect && ch.protectedStrokeGeometry) {
                        context->FillGeometry(
                            ch.protectedStrokeGeometry.Get(), strokeBrush.Get()
                        );
                    } else {
                        context->DrawGeometry(
                            ch.geometry.Get(), strokeBrush.Get(), charStyle.strokeWidth
                        );
                    }
                }
                context->FillGeometry(ch.geometry.Get(), fillBrush.Get());
            }
        };
        if (line->hasInlineStyles) {
            drawInlineStack(false);
            if (wipeEdge > line->bounds.left) {
                context->PushAxisAlignedClip(
                    afterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                );
                drawInlineStack(true);
                context->PopAxisAlignedClip();
            }
        } else {
            drawStack(false, beforeFill.Get(), beforeStroke.Get(), beforeStroke2.Get());
            if (wipeEdge > line->bounds.left) {
                context->PushAxisAlignedClip(afterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
                drawStack(true, afterFill.Get(), afterStroke.Get(), afterStroke2.Get());
                context->PopAxisAlignedClip();
            }
        }
        auto drawRubyStack = [&](const Impl::CachedRuby &ruby, bool after) {
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            Microsoft::WRL::ComPtr<ID2D1Brush> fill = paintBrush(
                after ? rubyStyle.rubyAfterFillPaint : rubyStyle.rubyBeforeFillPaint,
                ruby.fillBounds,
                after ? rubyStyle.rubyAfterFill : rubyStyle.rubyBeforeFill
            );
            Microsoft::WRL::ComPtr<ID2D1Brush> stroke = paintBrush(
                after ? rubyStyle.rubyAfterStrokePaint : rubyStyle.rubyBeforeStrokePaint,
                ruby.fillBounds,
                after ? rubyStyle.rubyAfterStroke : rubyStyle.rubyBeforeStroke
            );
            Microsoft::WRL::ComPtr<ID2D1Brush> stroke2 = paintBrush(
                after
                    ? rubyStyle.rubyAfterStroke2Paint
                    : rubyStyle.rubyBeforeStroke2Paint,
                ruby.fillBounds,
                after ? rubyStyle.rubyAfterStroke2 : rubyStyle.rubyBeforeStroke2
            );
            if (rubyStyle.rubyStroke2Width > 0.0f) {
                for (const auto &geometry : ruby.geometries) {
                    context->DrawGeometry(
                        geometry.Get(),
                        stroke2.Get(),
                        std::max(0.0f, rubyStyle.rubyStrokeWidth)
                            + rubyStyle.rubyStroke2Width
                    );
                }
            }
            if (rubyStyle.rubyStrokeWidth > 0.0f) {
                const bool protect = paintNeedsBodyProtection(
                    after
                        ? rubyStyle.rubyAfterFillPaint
                        : rubyStyle.rubyBeforeFillPaint
                );
                for (std::size_t index = 0; index < ruby.geometries.size(); ++index) {
                    const auto &protectedGeometry = index < ruby.protectedStrokeGeometries.size()
                        ? ruby.protectedStrokeGeometries[index]
                        : Microsoft::WRL::ComPtr<ID2D1Geometry>{};
                    if (protect && protectedGeometry) {
                        context->FillGeometry(protectedGeometry.Get(), stroke.Get());
                    } else {
                        context->DrawGeometry(
                            ruby.geometries[index].Get(),
                            stroke.Get(),
                            rubyStyle.rubyStrokeWidth
                        );
                    }
                }
            }
            for (const auto &geometry : ruby.geometries) {
                context->FillGeometry(geometry.Get(), fill.Get());
            }
        };
        for (const Impl::CachedRuby &ruby : line->rubies) {
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            const float rubyWipeEdge = rubyWipeEdgeAt(ruby);
            const float rubyPad = std::max(
                rubyStyle.rubyStrokeWidth + rubyStyle.rubyStroke2Width, 2.0f
            ) + 4.0f;
            const D2D1_RECT_F rubyAfterClip = D2D1::RectF(
                ruby.bounds.left - rubyPad,
                ruby.bounds.top - rubyPad,
                rubyWipeEdge,
                ruby.bounds.bottom + rubyPad
            );
            drawRubyStack(ruby, false);
            if (rubyWipeEdge > ruby.bounds.left) {
                context->PushAxisAlignedClip(
                    rubyAfterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                );
                drawRubyStack(ruby, true);
                context->PopAxisAlignedClip();
            }
        }
        checkHr(context->EndDraw(), "ID2D1DeviceContext::EndDraw(frame layers)", device_);
        renderedAnyLine = true;
        if (blur) {
            blur->SetInput(0, nullptr);
        }
        for (RubyGlowLayer &layer : rubyGlowLayers) {
            layer.blur->SetInput(0, nullptr);
        }
        for (InlineGlowLayer &layer : inlineGlowLayers) {
            layer.blur->SetInput(0, nullptr);
        }
      }
    }

    if (!renderedAnyLine) {
        context->SetTarget(targetBitmap);
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        context->Clear(D2D1::ColorF(0.0f, 0.0f));
        checkHr(context->EndDraw(), "ID2D1DeviceContext::EndDraw(empty frame)", device_);
    }

    context->SetTarget(nullptr);
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    const double renderMs = elapsedMs(renderStart);

    const auto readbackStart = Clock::now();
    ID3D11Texture2D *stagingTexture = impl_->frameStagingTexture.Get();
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
    } else {
        device_.d3dContext()->CopyResource(stagingTexture, targetTexture);
    }
    D3D11_MAPPED_SUBRESOURCE mapped{};
    checkHr(
        device_.d3dContext()->Map(stagingTexture, 0, D3D11_MAP_READ, 0, &mapped),
        "ID3D11DeviceContext::Map(frame)",
        device_
    );

    ProbeResult result;
    result.renderMs = renderMs;
    result.surface.width = scene.width;
    result.surface.height = scene.height;
    result.surface.stride = scene.width * 4;
    result.surface.pixelFormat = PixelFormat::Bgra8888Premultiplied;
    int packedHeight = scene.height;
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
    for (int y = 0; y < packedHeight; ++y) {
        const auto *source = static_cast<const std::uint8_t *>(mapped.pData)
            + static_cast<std::size_t>(mapped.RowPitch) * y;
        auto *destination = result.surface.bytes.data()
            + static_cast<std::size_t>(result.surface.stride) * y;
        std::memcpy(destination, source, static_cast<std::size_t>(result.surface.stride));
    }
    device_.d3dContext()->Unmap(stagingTexture, 0);
    result.readbackMs = elapsedMs(readbackStart);
    return result;
}

}  // namespace krok::subtitle::native
