#include "d2d_paint_resources.h"

#include <d2d1helper.h>
#include <propidl.h>
#include <wincodec.h>

#include <algorithm>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <vector>

namespace krok::subtitle::native::direct2d {
namespace {

std::string hresultText(
    const char *operation,
    HRESULT value,
    const std::string &deviceReason = {}
) {
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

D2D1_COLOR_F d2dColor(const RgbaColor &color) {
    return D2D1::ColorF(
        static_cast<float>(color.red) / 255.0f,
        static_cast<float>(color.green) / 255.0f,
        static_cast<float>(color.blue) / 255.0f,
        static_cast<float>(color.alpha) / 255.0f
    );
}

}  // namespace

Microsoft::WRL::ComPtr<ID2D1Brush> createPaintBrush(
    ID2D1DeviceContext *context,
    const PaintStyle &paint,
    const D2D1_RECT_F &rect,
    const RgbaColor &fallback,
    const D2DDevice &device,
    ID2D1Bitmap1 *image,
    float canvasDx,
    float canvasDy,
    std::uint64_t *brushCreated
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
        if (brushCreated != nullptr) {
            ++*brushCreated;
        }
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
        if (brushCreated != nullptr) {
            ++*brushCreated;
        }
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
    if (brushCreated != nullptr) {
        ++*brushCreated;
    }
    return brush;
}

D2D1_RECT_F rubyPaintBounds(
    const PaintStyle &paint,
    const D2D1_RECT_F &localBounds,
    const D2D1_RECT_F &horizontalBounds
) {
    return paint.mode == "gradient_horizontal" ? horizontalBounds : localBounds;
}

void updatePaintBrush(
    ID2D1Brush *brush,
    const PaintStyle &paint,
    const D2D1_RECT_F &rect,
    float canvasDx,
    float canvasDy
) {
    if (brush == nullptr) {
        return;
    }
    if (paint.mode == "image") {
        Microsoft::WRL::ComPtr<ID2D1BitmapBrush1> bitmapBrush;
        if (SUCCEEDED(brush->QueryInterface(IID_PPV_ARGS(
                bitmapBrush.ReleaseAndGetAddressOf())))) {
            const float scale = std::clamp(paint.imageScale, 0.01f, 10.0f);
            bitmapBrush->SetTransform(
                D2D1::Matrix3x2F::Scale(scale, scale)
                    * D2D1::Matrix3x2F::Translation(-canvasDx, -canvasDy)
            );
        }
        return;
    }
    const bool gradient = paint.mode == "gradient_horizontal"
        || paint.mode == "gradient_vertical"
        || paint.mode == "split_vertical";
    if (!gradient || paint.stops.empty()) {
        return;
    }
    Microsoft::WRL::ComPtr<ID2D1LinearGradientBrush> linear;
    if (FAILED(brush->QueryInterface(IID_PPV_ARGS(
            linear.ReleaseAndGetAddressOf())))) {
        return;
    }
    const bool horizontal = paint.mode == "gradient_horizontal";
    linear->SetStartPoint(horizontal
        ? D2D1::Point2F(rect.left, (rect.top + rect.bottom) * 0.5f)
        : D2D1::Point2F((rect.left + rect.right) * 0.5f, rect.top));
    linear->SetEndPoint(horizontal
        ? D2D1::Point2F(rect.right, (rect.top + rect.bottom) * 0.5f)
        : D2D1::Point2F((rect.left + rect.right) * 0.5f, rect.bottom));
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

namespace {

// /grctlext/Delay 以 1/100 秒计；0 视为非法并钳到 10ms——该规则必须与
// Python 侧 metrics.GUIDE_ANIM_MIN_FRAME_MS 完全一致，否则两条后端会在
// 相同 tMs 上选到不同帧。
constexpr int kAnimatedFrameMinMs = 10;

int animatedFrameDelayMs(IWICMetadataQueryReader *reader) {
    if (reader == nullptr) {
        return kAnimatedFrameMinMs;
    }
    PROPVARIANT value;
    PropVariantInit(&value);
    if (FAILED(reader->GetMetadataByName(L"/grctlext/Delay", &value))) {
        return kAnimatedFrameMinMs;
    }
    // GIF 元数据走 16 位（VT_UI2），别只认 VT_UI4。
    UINT centiseconds = 0;
    if (value.vt == VT_UI4) {
        centiseconds = value.ulVal;
    } else if (value.vt == VT_UI2) {
        centiseconds = static_cast<UINT>(value.uiVal);
    }
    PropVariantClear(&value);
    return std::max(static_cast<int>(centiseconds) * 10, kAnimatedFrameMinMs);
}

int gifDisposalMethod(IWICMetadataQueryReader *reader) {
    if (reader == nullptr) {
        return 0;
    }
    PROPVARIANT value;
    PropVariantInit(&value);
    if (FAILED(reader->GetMetadataByName(L"/grctlext/Disposal", &value))
        || value.vt != VT_UI1) {
        PropVariantClear(&value);
        return 0;
    }
    const BYTE raw = value.bVal;
    PropVariantClear(&value);
    return static_cast<int>(raw & 0x07);
}

int frameOffset(IWICMetadataQueryReader *reader, const wchar_t *name) {
    if (reader == nullptr) {
        return 0;
    }
    PROPVARIANT value;
    PropVariantInit(&value);
    if (FAILED(reader->GetMetadataByName(name, &value))) {
        return 0;
    }
    int result = 0;
    if (value.vt == VT_I2) {
        result = value.iVal;
    } else if (value.vt == VT_I4) {
        result = value.lVal;
    } else if (value.vt == VT_UI2) {
        result = static_cast<int>(value.uiVal);
    } else if (value.vt == VT_UI4) {
        result = static_cast<int>(value.ulVal);
    }
    PropVariantClear(&value);
    return std::max(result, 0);
}

void blendPremultipliedRow(
    std::uint8_t *destination,
    const std::uint8_t *source,
    std::size_t pixels
) {
    for (std::size_t index = 0; index < pixels; ++index) {
        const std::size_t offset = index * 4;
        const std::uint8_t alpha = source[offset + 3];
        if (alpha == 255) {
            destination[offset] = source[offset];
            destination[offset + 1] = source[offset + 1];
            destination[offset + 2] = source[offset + 2];
            destination[offset + 3] = 255;
        } else if (alpha != 0) {
            for (int channel = 0; channel < 4; ++channel) {
                const int src = source[offset + channel];
                const int dst = destination[offset + channel];
                // src、dst 均为预乘 PBGRA：src-over = src + dst * (1 - sa)。
                destination[offset + channel] = static_cast<std::uint8_t>(
                    src + dst * (255 - alpha) / 255
                );
            }
        }
    }
}

int logicalScreenExtent(
    IWICMetadataQueryReader *reader,
    const wchar_t *name
) {
    if (reader == nullptr) {
        return 0;
    }
    PROPVARIANT value;
    PropVariantInit(&value);
    if (FAILED(reader->GetMetadataByName(name, &value))) {
        return 0;
    }
    int result = 0;
    if (value.vt == VT_UI2) {
        result = static_cast<int>(value.uiVal);
    } else if (value.vt == VT_UI4) {
        result = static_cast<int>(value.ulVal);
    }
    PropVariantClear(&value);
    return result;
}

}  // namespace

AnimatedBitmapFrames loadWicAnimatedBitmaps(
    ID2D1DeviceContext *context,
    const std::wstring &path,
    int maxFrames
) {
    AnimatedBitmapFrames result;
    if (path.empty() || maxFrames <= 0) {
        return result;
    }
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(initialized) && initialized != RPC_E_CHANGED_MODE) {
        return result;
    }
    Microsoft::WRL::ComPtr<IWICImagingFactory> factory;
    if (FAILED(CoCreateInstance(
            CLSID_WICImagingFactory,
            nullptr,
            CLSCTX_INPROC_SERVER,
            IID_PPV_ARGS(factory.ReleaseAndGetAddressOf())))) {
        return result;
    }
    Microsoft::WRL::ComPtr<IWICBitmapDecoder> decoder;
    if (FAILED(factory->CreateDecoderFromFilename(
            path.c_str(),
            nullptr,
            GENERIC_READ,
            WICDecodeMetadataCacheOnLoad,
            decoder.ReleaseAndGetAddressOf()))) {
        return result;
    }
    GUID container = {};
    if (FAILED(decoder->GetContainerFormat(&container))
        || container != GUID_ContainerFormatGif) {
        // 仅 GIF 走动图路径；其余格式（含多页 TIFF）保持静态首帧。
        return result;
    }
    UINT frameCount = 0;
    if (FAILED(decoder->GetFrameCount(&frameCount)) || frameCount <= 1) {
        return result;
    }
    // 逻辑画布尺寸在 /logscrdesc；读不到时（理论不应发生）回退第 0 帧尺寸。
    UINT canvasWidth = 0;
    UINT canvasHeight = 0;
    {
        Microsoft::WRL::ComPtr<IWICBitmapFrameDecode> firstFrame;
        Microsoft::WRL::ComPtr<IWICMetadataQueryReader> firstReader;
        if (SUCCEEDED(decoder->GetFrame(0, firstFrame.ReleaseAndGetAddressOf()))
            && SUCCEEDED(
                firstFrame->GetMetadataQueryReader(firstReader.ReleaseAndGetAddressOf()))) {
            canvasWidth = static_cast<UINT>(
                logicalScreenExtent(firstReader.Get(), L"/logscrdesc/Width"));
            canvasHeight = static_cast<UINT>(
                logicalScreenExtent(firstReader.Get(), L"/logscrdesc/Height"));
        }
        if (canvasWidth == 0 || canvasHeight == 0) {
            UINT frameWidth = 0;
            UINT frameHeight = 0;
            if (FAILED(firstFrame->GetSize(&frameWidth, &frameHeight))) {
                return result;
            }
            canvasWidth = frameWidth;
            canvasHeight = frameHeight;
        }
    }
    if (canvasWidth == 0 || canvasHeight == 0) {
        return result;
    }
    const std::size_t canvasStride = static_cast<std::size_t>(canvasWidth) * 4;
    const std::size_t canvasPixels =
        canvasStride * static_cast<std::size_t>(canvasHeight);
    // GIF 帧是增量矩形 + dispose 语义：按规范在 CPU 侧逐帧合成全尺寸
    // 画布（背景透明，Qt 的 gif handler 同口径），再各自上传为 D2D 位图。
    std::vector<std::uint8_t> canvas(canvasPixels, 0);
    std::vector<std::uint8_t> snapshot;
    int previousDisposal = 0;
    RECT previousRect{0, 0, 0, 0};
    const UINT limitedFrames = std::min<UINT>(frameCount, static_cast<UINT>(maxFrames));
    const D2D1_BITMAP_PROPERTIES1 properties = D2D1::BitmapProperties1(
        D2D1_BITMAP_OPTIONS_NONE,
        D2D1::PixelFormat(
            DXGI_FORMAT_B8G8R8A8_UNORM,
            D2D1_ALPHA_MODE_PREMULTIPLIED
        ),
        96.0f,
        96.0f
    );
    for (UINT frameIndex = 0; frameIndex < limitedFrames; ++frameIndex) {
        Microsoft::WRL::ComPtr<IWICBitmapFrameDecode> frame;
        if (FAILED(decoder->GetFrame(frameIndex, frame.ReleaseAndGetAddressOf()))) {
            break;
        }
        Microsoft::WRL::ComPtr<IWICMetadataQueryReader> reader;
        frame->GetMetadataQueryReader(reader.ReleaseAndGetAddressOf());
        // 先应用上一帧的 dispose，再叠加本帧。
        if (previousDisposal == 2) {
            const int left = std::max<LONG>(previousRect.left, 0);
            const int top = std::max<LONG>(previousRect.top, 0);
            const int right = std::min<LONG>(previousRect.right, static_cast<LONG>(canvasWidth));
            const int bottom = std::min<LONG>(previousRect.bottom, static_cast<LONG>(canvasHeight));
            for (LONG y = top; y < bottom; ++y) {
                std::fill_n(
                    canvas.begin()
                        + static_cast<std::ptrdiff_t>(y) * static_cast<std::ptrdiff_t>(canvasStride)
                        + static_cast<std::ptrdiff_t>(left) * 4,
                    static_cast<std::size_t>(std::max(right - left, 0)) * 4,
                    0
                );
            }
        } else if (previousDisposal == 3 && snapshot.size() == canvasPixels) {
            canvas = snapshot;
        }
        Microsoft::WRL::ComPtr<IWICFormatConverter> converter;
        if (FAILED(factory->CreateFormatConverter(
                converter.ReleaseAndGetAddressOf()))
            || FAILED(converter->Initialize(
                frame.Get(),
                GUID_WICPixelFormat32bppPBGRA,
                WICBitmapDitherTypeNone,
                nullptr,
                0.0,
                WICBitmapPaletteTypeMedianCut))) {
            break;
        }
        UINT frameWidth = 0;
        UINT frameHeight = 0;
        if (FAILED(converter->GetSize(&frameWidth, &frameHeight))
            || frameWidth == 0
            || frameHeight == 0) {
            break;
        }
        const int offsetX = std::min(
            frameOffset(reader.Get(), L"/imgdesc/Left"),
            static_cast<int>(canvasWidth)
        );
        const int offsetY = std::min(
            frameOffset(reader.Get(), L"/imgdesc/Top"),
            static_cast<int>(canvasHeight)
        );
        const std::size_t frameStride = static_cast<std::size_t>(frameWidth) * 4;
        std::vector<std::uint8_t> framePixels(frameStride * frameHeight);
        if (FAILED(converter->CopyPixels(
                nullptr,
                static_cast<UINT>(frameStride),
                static_cast<UINT>(framePixels.size()),
                framePixels.data()))) {
            break;
        }
        const int disposal = gifDisposalMethod(reader.Get());
        if (disposal == 3) {
            snapshot = canvas;
        }
        for (UINT row = 0; row < frameHeight; ++row) {
            const int canvasY = static_cast<int>(row) + offsetY;
            if (canvasY < 0 || canvasY >= static_cast<int>(canvasHeight)) {
                continue;
            }
            const int canvasX = offsetX;
            const std::size_t copyPixels = std::min<std::size_t>(
                frameWidth,
                static_cast<std::size_t>(static_cast<int>(canvasWidth) - canvasX)
            );
            if (copyPixels <= 0) {
                continue;
            }
            std::uint8_t *destination = canvas.data()
                + static_cast<std::size_t>(canvasY) * canvasStride
                + static_cast<std::size_t>(canvasX) * 4;
            const std::uint8_t *source = framePixels.data()
                + static_cast<std::size_t>(row) * frameStride;
            blendPremultipliedRow(destination, source, copyPixels);
        }
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
        if (FAILED(context->CreateBitmap(
                D2D1::SizeU(canvasWidth, canvasHeight),
                canvas.data(),
                static_cast<UINT>(canvasStride),
                &properties,
                bitmap.ReleaseAndGetAddressOf()))) {
            break;
        }
        result.bitmaps.push_back(std::move(bitmap));
        result.delaysMs.push_back(animatedFrameDelayMs(reader.Get()));
        previousDisposal = disposal;
        previousRect = RECT{
            offsetX,
            offsetY,
            offsetX + static_cast<LONG>(frameWidth),
            offsetY + static_cast<LONG>(frameHeight),
        };
    }
    if (result.bitmaps.size() <= 1) {
        result.bitmaps.clear();
        result.delaysMs.clear();
    }
    return result;
}

}  // namespace krok::subtitle::native::direct2d
