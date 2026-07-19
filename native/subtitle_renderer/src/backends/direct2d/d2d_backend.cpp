#include "d2d_backend.h"

#include <d2d1helper.h>
#include <d2d1effects.h>
#include <dwrite.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <iomanip>
#include <sstream>
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

}  // namespace

struct Direct2DGpuBackend::Impl {
    struct CachedChar {
        int startMs = 0;
        int endMs = 0;
        float left = 0.0f;
        float right = 0.0f;
        float layoutLeft = 0.0f;
        float layoutRight = 0.0f;
    };

    struct CachedRuby {
        int startMs = 0;
        int endMs = 0;
        float baselineOffset = 0.0f;
        D2D1_RECT_F bounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
    };

    struct CachedLine {
        int startMs = 0;
        int endMs = 0;
        int lane = 0;
        float ascent = 0.0f;
        float descent = 0.0f;
        float boxAscent = 0.0f;
        D2D1_RECT_F bounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
        std::vector<CachedRuby> rubies;
    };

    RenderScene scene;
    std::vector<CachedLine> lines;
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
    return impl_->diagnostics;
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

    const TextStyle &style = scene.style;
    Microsoft::WRL::ComPtr<IDWriteFontCollection> fontCollection;
    checkHr(
        device_.dwriteFactory()->GetSystemFontCollection(
            fontCollection.ReleaseAndGetAddressOf(),
            FALSE
        ),
        "IDWriteFactory::GetSystemFontCollection",
        device_
    );
    auto resolveFace = [&](const std::wstring &family, int weight) {
        const std::wstring resolvedFamily = family.empty() ? L"Segoe UI" : family;
        auto face = createFontFace(fontCollection.Get(), resolvedFamily, weight, style.italic);
        if (!face && resolvedFamily != L"Segoe UI") {
            face = createFontFace(fontCollection.Get(), L"Segoe UI", weight, style.italic);
        }
        if (!face) {
            throw BackendError("DirectWrite could not resolve a usable font face");
        }
        return face;
    };
    const auto mainFace = resolveFace(style.fontFamily, style.fontWeight);
    const auto latinFace = resolveFace(
        style.latinFontFamily.value_or(style.fontFamily),
        style.latinFontWeight.value_or(style.fontWeight)
    );
    const auto rubyFace = resolveFace(
        style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily,
        style.rubyFontWeight
    );
    const auto rubyLatinFace = resolveFace(
        style.rubyLatinFontFamily.value_or(
            style.rubyFontFamily.empty() ? style.fontFamily : style.rubyFontFamily
        ),
        style.rubyLatinFontWeight.value_or(style.rubyFontWeight)
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
        Impl::CachedLine cached;
        cached.startMs = sourceLine.startMs;
        cached.endMs = sourceLine.endMs;
        cached.lane = style.dualLineLayout
            ? static_cast<int>(lineIndex % static_cast<std::size_t>(std::max(style.laneCount, 1)))
            : 0;
        cached.chars.reserve(sourceLine.chars.size());
        bool lineHasBounds = false;
        float cursor = 0.0f;

        for (std::size_t charIndex = 0; charIndex < sourceLine.chars.size(); ++charIndex) {
            const TextChar &sourceChar = sourceLine.chars[charIndex];
            const bool latin = isLatinText(sourceChar.text);
            const auto &requestedFace = latin ? latinFace : mainFace;
            const float fontSize = latin
                ? style.latinFontSize.value_or(style.fontSize)
                : style.fontSize;
            const int unit = std::max(static_cast<int>(fontSize), 1);
            const int edgeSize = std::max(static_cast<int>(style.strokeWidth), 0);

            DWRITE_FONT_METRICS fontMetrics{};
            requestedFace->GetMetrics(&fontMetrics);
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
            cached.boxAscent = std::max(
                cached.boxAscent,
                static_cast<float>(unit) * static_cast<float>(fontMetrics.ascent) / boxMetricTotal
                    + static_cast<float>(edgeSize) * 0.5f
            );

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
                if (!style.allowBiting) {
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
                    unit * std::clamp(style.spaceWidthPercent, 10, 100) / 100 + edgeSize
                );
            } else {
                layoutWidth = static_cast<float>(
                    unit * std::clamp(style.spaceWidthPercent, 10, 100) * 25 / 100 / 10
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
            const float wipePad = std::max(style.strokeWidth, 0.0f) * 0.5f;
            cached.chars.push_back(Impl::CachedChar{
                sourceChar.startMs,
                sourceChar.endMs,
                positionedHasBounds ? positionedCharBounds.left - wipePad : cursor,
                positionedHasBounds ? positionedCharBounds.right + wipePad : cursor + layoutWidth,
                cursor,
                cursor + layoutWidth,
            });
            cursor += layoutWidth;
            if (charIndex + 1 < sourceLine.chars.size()) {
                cursor += style.letterSpacing;
            }
        }

        for (const TextRuby &sourceRuby : sourceLine.rubies) {
            if (sourceRuby.units.empty()
                || sourceRuby.firstCharIndex < 0
                || sourceRuby.lastCharIndex < sourceRuby.firstCharIndex
                || sourceRuby.lastCharIndex >= static_cast<int>(cached.chars.size())) {
                continue;
            }
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
            const int rubyEdgeSize = std::max(static_cast<int>(style.rubyStrokeWidth), 0);

            for (const RubyUnit &sourceUnit : sourceRuby.units) {
                const bool latin = isLatinText(sourceUnit.text);
                const auto &requestedFace = latin ? rubyLatinFace : rubyFace;
                const float fontSize = latin
                    ? style.rubyLatinFontSize.value_or(style.rubyFontSize)
                    : style.rubyFontSize;
                const int unit = std::max(static_cast<int>(fontSize), 1);
                DWRITE_FONT_METRICS fontMetrics{};
                requestedFace->GetMetrics(&fontMetrics);
                const float boxMetricTotal = static_cast<float>(std::max(
                    static_cast<int>(fontMetrics.ascent) + static_cast<int>(fontMetrics.descent),
                    1
                ));
                rubyBoxDescent = std::max(
                    rubyBoxDescent,
                    static_cast<float>(unit) * static_cast<float>(fontMetrics.descent) / boxMetricTotal
                        + static_cast<float>(rubyEdgeSize) * 0.5f
                );

                std::vector<UINT16> glyphs = glyphIndices(requestedFace.Get(), sourceUnit.text);
                Microsoft::WRL::ComPtr<IDWriteFontFace> outlineFace = requestedFace;
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
                    std::vector<DWRITE_GLYPH_METRICS> metrics(glyphs.size());
                    checkHr(
                        requestedFace->GetDesignGlyphMetrics(
                            glyphs.data(),
                            static_cast<UINT32>(glyphs.size()),
                            metrics.data(),
                            FALSE
                        ),
                        "IDWriteFontFace::GetDesignGlyphMetrics(ruby character)",
                        device_
                    );
                    const int inkWidth = std::max(
                        static_cast<int>(glyph.bounds.right - glyph.bounds.left), 0
                    );
                    int leftBearing = metrics.front().leftSideBearing;
                    int rightBearing = metrics.front().rightSideBearing;
                    if (!style.allowBiting) {
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
                        unit * std::clamp(style.spaceWidthPercent, 10, 100) / 100
                            + rubyEdgeSize
                    );
                } else {
                    glyph.layoutWidth = static_cast<float>(
                        unit * std::clamp(style.spaceWidthPercent, 10, 100) * 25 / 100 / 10
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
            const bool centered = style.rubyAlignment == "center"
                || (style.rubyAlignment != "equal_space" && (
                    isAsciiAlnumText(sourceRuby.baseText)
                    || isAsciiAlnumText(sourceRuby.reading)
                ));
            float gap = style.rubyInterval;
            if (!centered && rubyGlyphs.size() > 1) {
                const float slots = targetWidth <= naturalWidth
                    ? static_cast<float>(rubyGlyphs.size() - 1)
                    : static_cast<float>(rubyGlyphs.size() + 1);
                gap = std::max(
                    (targetWidth - naturalWidth) / std::max(slots, 1.0f),
                    style.rubyInterval
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
            ruby.baselineOffset = -cached.boxAscent - style.rubyGap - rubyBoxDescent;
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
                }
                const float wipePad = std::max(style.rubyStrokeWidth, 0.0f) * 0.5f;
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
    impl_->diagnostics.estimatedCacheBytes = sizeof(Impl);
    for (const Impl::CachedLine &line : impl_->lines) {
        impl_->diagnostics.charCount += line.chars.size();
        impl_->diagnostics.geometryCount += line.geometries.size();
        impl_->diagnostics.estimatedCacheBytes += sizeof(Impl::CachedLine)
            + line.chars.capacity() * sizeof(Impl::CachedChar)
            + line.geometries.capacity() * sizeof(Microsoft::WRL::ComPtr<ID2D1Geometry>);
        impl_->diagnostics.rubyCount += line.rubies.size();
        for (const Impl::CachedRuby &ruby : line.rubies) {
            impl_->diagnostics.charCount += ruby.chars.size();
            impl_->diagnostics.geometryCount += ruby.geometries.size();
            impl_->diagnostics.estimatedCacheBytes += sizeof(Impl::CachedRuby)
                + ruby.chars.capacity() * sizeof(Impl::CachedChar)
                + ruby.geometries.capacity() * sizeof(Microsoft::WRL::ComPtr<ID2D1Geometry>);
        }
    }
    // Direct2D does not expose path allocation bytes. Keep a conservative
    // diagnostic estimate so cache growth/churn remains observable.
    impl_->diagnostics.estimatedCacheBytes += impl_->diagnostics.geometryCount * 256;
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
        const float visualPad = std::ceil(
            (std::max(style.strokeWidth, 0.0f) + std::max(style.stroke2Width, 0.0f)) * 0.5f
        );
        const float ascent = line->ascent > 0.0f ? line->ascent : -line->bounds.top;
        const float descent = line->descent > 0.0f ? line->descent : line->bounds.bottom;
        float topExtent = ascent;
        float combinedTop = line->bounds.top;
        float combinedBottom = line->bounds.bottom;
        for (const Impl::CachedRuby &ruby : line->rubies) {
            topExtent = std::max(topExtent, -ruby.bounds.top);
            combinedTop = std::min(combinedTop, ruby.bounds.top);
            combinedBottom = std::max(combinedBottom, ruby.bounds.bottom);
        }
        const int lanes = style.dualLineLayout ? std::max(style.laneCount, 1) : 1;
        const float mainHeight = topExtent + descent + visualPad * 2.0f;
        const float step = mainHeight + style.lineGap;
        float firstBaseline = static_cast<float>(scene.height) - style.bottomMargin
            - descent - visualPad - step * static_cast<float>(lanes - 1);
        if (style.verticalPosition == "top") {
            firstBaseline = style.bottomMargin + topExtent + visualPad;
        } else if (style.verticalPosition == "center") {
            const float totalHeight = mainHeight * static_cast<float>(lanes)
                + style.lineGap * static_cast<float>(lanes - 1);
            firstBaseline = (static_cast<float>(scene.height) - totalHeight) * 0.5f
                + topExtent + visualPad;
            if (lanes == 1) {
                // Single-line center alignment is defined by visible ink, just
                // like horizontal centering. This avoids font-leading drift.
                firstBaseline = (static_cast<float>(scene.height)
                    - (combinedBottom - combinedTop)) * 0.5f
                    - combinedTop;
            }
        }
        const float dy = firstBaseline + step * static_cast<float>(line->lane);
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> beforeFill;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> afterFill;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> beforeStroke;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> afterStroke;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> beforeStroke2;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> afterStroke2;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> beforeDecor;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> afterDecor;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> rubyBeforeFill;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> rubyAfterFill;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> rubyBeforeStroke;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> rubyAfterStroke;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> rubyBeforeStroke2;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> rubyAfterStroke2;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> rubyBeforeDecor;
        Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> rubyAfterDecor;
        checkHr(context->CreateSolidColorBrush(d2dColor(style.beforeFill), beforeFill.ReleaseAndGetAddressOf()), "Create before fill brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.afterFill), afterFill.ReleaseAndGetAddressOf()), "Create after fill brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.beforeStroke), beforeStroke.ReleaseAndGetAddressOf()), "Create before stroke brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.afterStroke), afterStroke.ReleaseAndGetAddressOf()), "Create after stroke brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.beforeStroke2), beforeStroke2.ReleaseAndGetAddressOf()), "Create before stroke2 brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.afterStroke2), afterStroke2.ReleaseAndGetAddressOf()), "Create after stroke2 brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.beforeDecor), beforeDecor.ReleaseAndGetAddressOf()), "Create before decor brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.afterDecor), afterDecor.ReleaseAndGetAddressOf()), "Create after decor brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.rubyBeforeFill), rubyBeforeFill.ReleaseAndGetAddressOf()), "Create ruby before fill brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.rubyAfterFill), rubyAfterFill.ReleaseAndGetAddressOf()), "Create ruby after fill brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.rubyBeforeStroke), rubyBeforeStroke.ReleaseAndGetAddressOf()), "Create ruby before stroke brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.rubyAfterStroke), rubyAfterStroke.ReleaseAndGetAddressOf()), "Create ruby after stroke brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.rubyBeforeStroke2), rubyBeforeStroke2.ReleaseAndGetAddressOf()), "Create ruby before stroke2 brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.rubyAfterStroke2), rubyAfterStroke2.ReleaseAndGetAddressOf()), "Create ruby after stroke2 brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.rubyBeforeDecor), rubyBeforeDecor.ReleaseAndGetAddressOf()), "Create ruby before decor brush", device_);
        checkHr(context->CreateSolidColorBrush(d2dColor(style.rubyAfterDecor), rubyAfterDecor.ReleaseAndGetAddressOf()), "Create ruby after decor brush", device_);

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

        struct RubyGlowLayer {
            Microsoft::WRL::ComPtr<ID2D1Bitmap1> source;
            Microsoft::WRL::ComPtr<ID2D1Effect> blur;
            std::vector<int> sigmas;
        };
        std::vector<RubyGlowLayer> rubyGlowLayers;
        auto appendRubyGlowLayer = [&](bool after, float requestedRadius, ID2D1Brush *brush) {
            const int radius = std::max(
                0, static_cast<int>(std::lround(requestedRadius))
            );
            if (style.rubyDecorationKind != "glow"
                || radius <= 0
                || line->rubies.empty()) {
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
            const float sourceWidth = std::max(0.0f, style.rubyStrokeWidth)
                + (style.rubyStroke2Width > 0.0f ? style.rubyStroke2Width : 0.0f)
                + static_cast<float>(radius);
            const float pad = sourceWidth * 0.5f + radius * 3.0f + 2.0f;
            context->SetTarget(layer.source.Get());
            context->SetTransform(D2D1::Matrix3x2F::Translation(dx, dy));
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            for (const Impl::CachedRuby &ruby : line->rubies) {
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
                    context->DrawGeometry(geometry.Get(), brush, sourceWidth);
                }
                context->PopAxisAlignedClip();
            }
            checkHr(
                context->EndDraw(),
                "ID2D1DeviceContext::EndDraw(ruby glow source)",
                device_
            );
            layer.blur->SetInput(0, layer.source.Get());
            const int passes = std::clamp(style.rubyGlowConcentrationLevel, 0, 2) + 1;
            for (int index = 0; index < passes; ++index) {
                layer.sigmas.push_back(radius - index * radius / passes);
            }
            rubyGlowLayers.push_back(std::move(layer));
        };
        appendRubyGlowLayer(false, style.rubyGlowBeforeRadius, rubyBeforeDecor.Get());
        appendRubyGlowLayer(true, style.rubyGlowAfterRadius, rubyAfterDecor.Get());

        context->SetTarget(targetBitmap);
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        context->Clear(D2D1::ColorF(0.0f, 0.0f));
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
        auto drawRubyStack = [&](const Impl::CachedRuby &ruby, ID2D1Brush *fill, ID2D1Brush *stroke, ID2D1Brush *stroke2) {
            if (style.rubyStroke2Width > 0.0f) {
                for (const auto &geometry : ruby.geometries) {
                    context->DrawGeometry(
                        geometry.Get(),
                        stroke2,
                        std::max(0.0f, style.rubyStrokeWidth) + style.rubyStroke2Width
                    );
                }
            }
            if (style.rubyStrokeWidth > 0.0f) {
                for (const auto &geometry : ruby.geometries) {
                    context->DrawGeometry(geometry.Get(), stroke, style.rubyStrokeWidth);
                }
            }
            for (const auto &geometry : ruby.geometries) {
                context->FillGeometry(geometry.Get(), fill);
            }
        };
        for (const Impl::CachedRuby &ruby : line->rubies) {
            const float rubyWipeEdge = rubyWipeEdgeAt(ruby);
            const float rubyPad = std::max(
                style.rubyStrokeWidth + style.rubyStroke2Width, 2.0f
            ) + 4.0f;
            const D2D1_RECT_F rubyAfterClip = D2D1::RectF(
                ruby.bounds.left - rubyPad,
                ruby.bounds.top - rubyPad,
                rubyWipeEdge,
                ruby.bounds.bottom + rubyPad
            );
            drawRubyStack(
                ruby,
                rubyBeforeFill.Get(),
                rubyBeforeStroke.Get(),
                rubyBeforeStroke2.Get()
            );
            if (rubyWipeEdge > ruby.bounds.left) {
                context->PushAxisAlignedClip(
                    rubyAfterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                );
                drawRubyStack(
                    ruby,
                    rubyAfterFill.Get(),
                    rubyAfterStroke.Get(),
                    rubyAfterStroke2.Get()
                );
                context->PopAxisAlignedClip();
            }
        }
        checkHr(context->EndDraw(), "ID2D1DeviceContext::EndDraw(frame layers)", device_);
        if (blur) {
            blur->SetInput(0, nullptr);
        }
        for (RubyGlowLayer &layer : rubyGlowLayers) {
            layer.blur->SetInput(0, nullptr);
        }
    }

    if (line == nullptr || line->geometries.empty()) {
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
    device_.d3dContext()->CopyResource(stagingTexture, targetTexture);
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
    result.surface.bytes.resize(static_cast<std::size_t>(result.surface.stride) * scene.height);
    for (int y = 0; y < scene.height; ++y) {
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
