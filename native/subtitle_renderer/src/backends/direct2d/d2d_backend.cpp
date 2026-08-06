#include "d2d_backend.h"

#include <d2d1_2.h>
#include <d2d1helper.h>
#include <d2d1effects.h>
#include <dwrite.h>
#include <wincodec.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <cwctype>
#include <iomanip>
#include <sstream>
#include <limits>
#include <mutex>
#include <tuple>
#include <thread>

namespace krok::subtitle::native {
namespace {

using Clock = std::chrono::steady_clock;

double elapsedMs(Clock::time_point start) {
    return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}

std::int64_t steadyNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        Clock::now().time_since_epoch()
    ).count();
}

bool environmentFlagEnabled(const char *name, bool defaultValue) {
    const char *value = std::getenv(name);
    if (value == nullptr || *value == '\0') {
        return defaultValue;
    }
    return std::strcmp(value, "0") != 0
        && std::strcmp(value, "false") != 0
        && std::strcmp(value, "False") != 0
        && std::strcmp(value, "FALSE") != 0;
}

std::uint64_t rectAreaPx(const D2D1_RECT_F &rect) {
    const double width = std::max(
        static_cast<double>(rect.right - rect.left), 0.0
    );
    const double height = std::max(
        static_cast<double>(rect.bottom - rect.top), 0.0
    );
    return static_cast<std::uint64_t>(std::ceil(width * height));
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

Microsoft::WRL::ComPtr<ID2D1PathGeometry> vectorGlyphGeometry(
    ID2D1Factory1 *factory,
    const VectorGlyph &glyph,
    float pixelSize,
    const D2DDevice &device
) {
    Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
    checkHr(
        factory->CreatePathGeometry(path.ReleaseAndGetAddressOf()),
        "ID2D1Factory::CreatePathGeometry(vector glyph)",
        device
    );
    Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
    checkHr(
        path->Open(sink.ReleaseAndGetAddressOf()),
        "ID2D1PathGeometry::Open(vector glyph)",
        device
    );
    sink->SetFillMode(D2D1_FILL_MODE_WINDING);
    sink->SetSegmentFlags(D2D1_PATH_SEGMENT_FORCE_ROUND_LINE_JOIN);
    const float scale = std::max(pixelSize, 1.0f)
        / std::max(glyph.unitsPerEm, 1.0f);
    bool figureOpen = false;
    for (const VectorPathCommand &command : glyph.commands) {
        const auto point = [&](std::size_t index) {
            return D2D1::Point2F(
                command.values[index] * scale,
                command.values[index + 1] * scale
            );
        };
        if (command.kind == 'M' && command.values.size() == 2) {
            if (figureOpen) {
                sink->EndFigure(D2D1_FIGURE_END_OPEN);
            }
            sink->BeginFigure(point(0), D2D1_FIGURE_BEGIN_FILLED);
            figureOpen = true;
        } else if (command.kind == 'L' && command.values.size() == 2
                   && figureOpen) {
            sink->AddLine(point(0));
        } else if (command.kind == 'C' && command.values.size() == 6
                   && figureOpen) {
            sink->AddBezier(D2D1::BezierSegment(
                point(0), point(2), point(4)
            ));
        } else if (command.kind == 'Q' && command.values.size() == 4
                   && figureOpen) {
            sink->AddQuadraticBezier(D2D1::QuadraticBezierSegment(
                point(0), point(2)
            ));
        } else if (command.kind == 'Z' && figureOpen) {
            sink->EndFigure(D2D1_FIGURE_END_CLOSED);
            figureOpen = false;
        }
    }
    if (figureOpen) {
        sink->EndFigure(D2D1_FIGURE_END_OPEN);
    }
    checkHr(sink->Close(), "ID2D1GeometrySink::Close(vector glyph)", device);
    return path;
}

struct VolumeSignalGeometry {
    int count = 1;
    float size = 1.0f;
    float columnWidth = 1.0f;
    float columnSpacing = 0.0f;
    float strokeExtent = 0.0f;
    float pitch = 1.0f;
    float groupWidth = 1.0f;
    float frontHeight = 1.0f;
    float heightDelta = 0.0f;
    float alignBaseShift = 0.0f;
    float alignDeltaShift = 0.0f;
};

VolumeSignalGeometry volumeSignalGeometry(const TextStyle &style) {
    VolumeSignalGeometry geometry;
    geometry.count = std::clamp(style.volumeColumnCount, 1, 16);
    geometry.size = std::max(style.volumeSize, 1.0f);
    geometry.columnWidth = std::max(style.volumeColumnWidth, 1.0f);
    geometry.columnSpacing = std::max(style.volumeColumnSpacing, 0.0f);
    geometry.strokeExtent = std::max(style.litStrokeWidth, 0.0f);
    // Painter's column spacing is measured between the stroked module cells;
    // the outline expands only the two outer group edges, not every pitch.
    geometry.pitch = geometry.columnWidth + geometry.columnSpacing;
    geometry.groupWidth = geometry.count * geometry.pitch
        - geometry.columnSpacing + geometry.strokeExtent * 2.0f;
    const float ratio = std::max(style.volumeRatio, 0.01f);
    float baseFactor = ratio;
    float depthFactor = 1.0f;
    if (ratio > 1.0f) {
        depthFactor = 1.0f / ratio;
        baseFactor = 1.0f;
    }
    geometry.frontHeight = baseFactor * geometry.size;
    geometry.heightDelta = geometry.count < 2
        ? 0.0f
        : ((depthFactor - baseFactor) * geometry.size)
            / static_cast<float>(geometry.count - 1);
    if (style.volumeAlign == 1) {
        geometry.alignBaseShift = (1.0f - baseFactor) * geometry.size * 0.5f;
        geometry.alignDeltaShift = -geometry.heightDelta * 0.5f;
    } else if (style.volumeAlign == 2) {
        geometry.alignBaseShift = (1.0f - baseFactor) * geometry.size;
        geometry.alignDeltaShift = -geometry.heightDelta;
    }
    return geometry;
}

float volumeFlashAlpha(int elapsed, int duration, const TextStyle &style) {
    if (duration <= 0 || elapsed < 0) {
        return 0.0f;
    }
    const int times = std::max(style.volumeFlashTimes, 0);
    if (times == 0) {
        return 1.0f;
    }
    const float perFlash = static_cast<float>(duration) / static_cast<float>(times);
    if (perFlash <= 0.0f) {
        return 1.0f;
    }
    float phase = std::fmod(static_cast<float>(elapsed) / perFlash, 1.0f) * 2.0f;
    if (phase > 1.0f) {
        phase = 2.0f - phase;
    }
    const float transition = std::clamp(
        static_cast<float>(style.volumeTransitionRatioPct) / 100.0f,
        0.0f,
        1.0f
    );
    if (transition <= 0.0f) {
        return 1.0f - ((phase * 2.0f - 1.0f) > 0.0f ? 1.0f : 0.0f);
    }
    const float fade = std::clamp(
        ((phase * 3.0f - 1.0f) * 0.67f) / transition,
        0.0f,
        1.0f
    );
    return 1.0f - fade;
}

struct VolumeSignalState {
    bool visible = false;
    int activeIndex = -1;
    float opacity = 0.0f;
};

struct ShapeSignalGeometry {
    int count = 1;
    float size = 1.0f;
    float tracking = 0.0f;
    float strokeExtent = 0.0f;
    float groupWidth = 1.0f;
};

ShapeSignalGeometry shapeSignalGeometry(const TextStyle &style) {
    ShapeSignalGeometry geometry;
    geometry.count = std::clamp(style.litNumber, 1, 8);
    geometry.size = std::max(style.litSize, 1.0f);
    geometry.tracking = std::max(style.litTracking, 0.0f);
    geometry.strokeExtent = std::max(style.litStrokeWidth, 0.0f)
        + std::max(style.litStrokeSoften, 0.0f);
    geometry.groupWidth = geometry.count * geometry.size
        + std::max(geometry.count - 1, 0)
            * (geometry.size * 0.5f + geometry.tracking);
    return geometry;
}

struct ShapeSignalState {
    bool visible = false;
    int activeIndex = -1;
    float activeOpacity = 1.0f;
    float dx = 0.0f;
    float dy = 0.0f;
};

ShapeSignalState shapeSignalState(
    int lineStartMs,
    const TextStyle &style,
    int tMs,
    int displayEndMs
) {
    ShapeSignalState state;
    if (style.vertical || !style.litEnabled || style.litStyle == "volume") {
        return state;
    }
    const int duration = std::max(style.signalsDurationMs, 0);
    const int activeDuration = std::max(
        duration - std::max(style.litWaitingTimeMs, 0), 0
    );
    if (activeDuration <= 0) {
        return state;
    }
    const int signalEnd = lineStartMs + style.litTimeOffsetMs;
    const int activeStart = signalEnd - activeDuration;
    if (tMs < activeStart || tMs >= displayEndMs) {
        return state;
    }
    state.visible = true;
    const int elapsed = std::max(tMs - activeStart, 0);
    const int count = std::clamp(style.litNumber, 1, 8);
    if (activeDuration <= 0 || count <= 1) {
        state.activeIndex = 0;
    } else if (elapsed >= activeDuration) {
        state.activeIndex = -1;
        return state;
    } else {
        const float raw = static_cast<float>((activeDuration - elapsed) * count)
            / static_cast<float>(activeDuration);
        state.activeIndex = std::clamp(static_cast<int>(raw), 0, count - 1);
        const float phase = std::clamp(
            raw - static_cast<float>(state.activeIndex), 0.0f, 1.0f
        );
        const float ratio = std::clamp(
            static_cast<float>(style.litTransitionRatioPct) / 100.0f,
            0.0f,
            1.0f
        );
        const float transitionPhase = 1.0f - phase;
        const float progress = ratio <= 0.0f
            ? 1.0f
            : std::clamp(
                (transitionPhase - (1.0f - ratio)) / ratio, 0.0f, 1.0f
            );
        if (style.litTransitionMode == "fade") {
            state.activeOpacity = 1.0f - progress;
        } else if (style.litTransitionMode == "slide") {
            state.activeOpacity = progress;
            const float distance = std::max(style.litTransitionDistance, 0.0f)
                * (1.0f - progress);
            constexpr float pi = 3.14159265358979323846f;
            const float radians = style.litTransitionAngleDeg * pi / 180.0f;
            state.dx = -std::cos(radians) * distance;
            state.dy = -std::sin(radians) * distance;
        }
    }
    return state;
}

VolumeSignalState volumeSignalState(
    int lineStartMs,
    const TextStyle &style,
    int tMs,
    int displayEndMs
) {
    VolumeSignalState state;
    if (style.vertical || !style.litEnabled || style.litStyle != "volume") {
        return state;
    }
    const int duration = std::max(style.signalsDurationMs, 0);
    const int activeDuration = std::max(
        duration - std::max(style.litWaitingTimeMs, 0), 0
    );
    if (activeDuration <= 0) {
        return state;
    }
    const int signalEnd = lineStartMs + style.litTimeOffsetMs;
    const int activeStart = signalEnd - activeDuration;
    if (tMs < activeStart || tMs >= displayEndMs) {
        return state;
    }
    const int elapsed = std::min(
        std::max(tMs - activeStart, 0),
        std::max(activeDuration - 1, 0)
    );
    const int count = std::clamp(style.volumeColumnCount, 1, 16);
    const int times = std::max(style.volumeFlashTimes, 0);
    const float flashRatio = std::max(style.volumeFlashDurationRatio, 0.0f);
    state.visible = true;
    state.opacity = 1.0f;
    int fillElapsed = elapsed;
    int fillDuration = activeDuration;
    if (times > 0 && flashRatio > 0.0f) {
        const float fillDurationFloat = static_cast<float>(activeDuration)
            / (static_cast<float>(times) * flashRatio + 1.0f);
        const float flashDuration = std::max(
            static_cast<float>(activeDuration) - fillDurationFloat, 0.0f
        );
        if (static_cast<float>(elapsed) < flashDuration) {
            state.activeIndex = -1;
            state.opacity = volumeFlashAlpha(
                elapsed,
                std::max(static_cast<int>(flashDuration), 1),
                style
            );
            return state;
        }
        fillElapsed = std::max(static_cast<int>(elapsed - flashDuration), 0);
        fillDuration = std::max(static_cast<int>(fillDurationFloat), 1);
    }
    const float raw = static_cast<float>(count * fillElapsed)
        / static_cast<float>(std::max(fillDuration, 1));
    state.activeIndex = std::clamp(static_cast<int>(raw), 0, count - 1);
    return state;
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

bool verticalRotates(const std::wstring &text) {
    static const std::wstring rotated =
        L"\u2190\u2192\u2010\u2011\u2012\u2013\u2014\u2015\u301c\uff5e"
        L"\u3008\u3009\u300a\u300b\u300c\u300d\u300e\u300f\u3010\u3011"
        L"\u3014\u3015\uff08\uff09\uff3b\uff3d\uff5b\uff5d\u30fc\uff70"
        L"<>()[]{}";
    return text.size() == 1 && rotated.find(text.front()) != std::wstring::npos;
}

std::pair<float, float> verticalGlyphOffset(
    const std::wstring &text, float cellWidth, float cellHeight
) {
    static const std::wstring corner = L"\u3001\u3002\uff0c\uff0e";
    static const std::wstring smallKana =
        L"\u3041\u3043\u3045\u3047\u3049\u3063\u3083\u3085\u3087\u308e"
        L"\u30a1\u30a3\u30a5\u30a7\u30a9\u30c3\u30e3\u30e5\u30e7\u30ee"
        L"\u30f5\u30f6";
    if (text.size() == 1 && corner.find(text.front()) != std::wstring::npos) {
        return {cellWidth * 0.28f, -cellHeight * 0.28f};
    }
    if (text.size() == 1 && smallKana.find(text.front()) != std::wstring::npos) {
        return {cellWidth * 0.10f, -cellHeight * 0.10f};
    }
    return {0.0f, 0.0f};
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

Microsoft::WRL::ComPtr<ID2D1Geometry> widenedStrokeGeometry(
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
        "Create animated stroke style",
        device
    );
    Microsoft::WRL::ComPtr<ID2D1PathGeometry> widened;
    checkHr(
        factory->CreatePathGeometry(widened.ReleaseAndGetAddressOf()),
        "Create animated widened geometry",
        device
    );
    Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
    checkHr(
        widened->Open(sink.ReleaseAndGetAddressOf()),
        "Open animated widened geometry",
        device
    );
    checkHr(
        body->Widen(width, strokeStyle.Get(), nullptr, sink.Get()),
        "Widen animated stroke",
        device
    );
    checkHr(sink->Close(), "Close animated widened geometry", device);
    Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
    checkHr(widened.As(&geometry), "Query animated widened geometry", device);
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
    float canvasDy = 0.0f,
    std::uint64_t *brushCreated = nullptr
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
        float top = 0.0f;
        float bottom = 0.0f;
        int styleIndex = -1;
        float boxAscent = 0.0f;
        float pivotX = 0.0f;
        float pivotY = 0.0f;
        Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> protectedStrokeGeometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> strokeGeometry;
        Microsoft::WRL::ComPtr<ID2D1Geometry> stroke2Geometry;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> fillRealization;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> protectedStrokeRealization;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> strokeRealization;
        Microsoft::WRL::ComPtr<ID2D1GeometryRealization> stroke2Realization;
        std::optional<BitmapGuide> bitmapGuide;
        D2D1_RECT_F bitmapRect{};
        std::vector<WipePoint> wipePoints;
    };

    struct CachedRuby {
        int startMs = 0;
        int endMs = 0;
        float baselineOffset = 0.0f;
        int styleIndex = -1;
        int transitionCharIndex = 0;
        int firstCharIndex = 0;
        int lastCharIndex = 0;
        float pivotX = 0.0f;
        float pivotY = 0.0f;
        D2D1_RECT_F bounds{};
        D2D1_RECT_F fillBounds{};
        D2D1_RECT_F horizontalFillBounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> protectedStrokeGeometries;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> strokeGeometries;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> stroke2Geometries;
    };

    struct CachedLine {
        int startMs = 0;
        int endMs = 0;
        int sourceIndex = 0;
        int sourceLineIndex = 0;
        int pageIndex = -1;
        int compositeOrder = 0;
        int lane = 0;
        bool staticOverlay = false;
        int fadeInMs = 0;
        int fadeOutMs = 0;
        std::string entryAnimation = "none";
        int entryDurationMs = 0;
        std::string exitAnimation = "none";
        int exitDurationMs = 0;
        std::string karaokeAnimation = "none";
        std::vector<DisplayWindow> displayWindows;
        std::vector<PlacementWindow> placementWindows;
        TextStyle style;
        float ascent = 0.0f;
        float descent = 0.0f;
        float boxAscent = 0.0f;
        bool hasRubyAnchor = false;
        float verticalRubyAllowance = 0.0f;
        float maxVisualPad = 0.0f;
        float legacyLaneHeight = 1.0f;
        float legacyLaneDescent = 0.0f;
        float n3DrawHeight = 1.0f;
        float n3Descent = 0.0f;
        // N3 char boxes accumulated over the line's own glyphs, independent of
        // the line style.  Static overlays (the title) size their box from
        // these so an inline role style fully governs the block; lyrics keep
        // the line-style box to hold the shared lane grid steady.
        float n3CharAscent = 0.0f;
        float n3CharDescent = 0.0f;
        bool hasN3CharBox = false;
        bool hasInlineStyles = false;
        bool hasInlineLaneGeometryOverride = false;
        std::optional<float> guideAnchorLeft;
        std::optional<float> guideAnchorRight;
        bool centerOverride = false;
        D2D1_RECT_F bounds{};
        D2D1_RECT_F fillBounds{};
        std::vector<CachedChar> chars;
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> geometries;
        std::vector<CachedRuby> rubies;
    };

    struct CachedBrush {
        PaintStyle paint;
        RgbaColor fallback;
        ID2D1Bitmap1 *imageIdentity = nullptr;
        D2D1_RECT_F rect{};
        float canvasDx = 0.0f;
        float canvasDy = 0.0f;
        Microsoft::WRL::ComPtr<ID2D1Brush> brush;
        std::uint64_t lastUse = 0;
    };

    enum class RealizationKind {
        Fill,
        ProtectedStroke,
        Stroke,
        Stroke2,
    };

    struct RealizationTask {
        std::size_t lineIndex = 0;
        int rubyIndex = -1;
        std::size_t charIndex = 0;
        RealizationKind kind = RealizationKind::Fill;
        Microsoft::WRL::ComPtr<ID2D1Geometry> geometry;
        float strokeWidth = 0.0f;
    };

    struct RealizationControl {
        std::atomic<bool> stop{false};
        std::atomic<bool> done{false};
        std::uint64_t generation = 0;
    };

    struct RetiredRealizationWorker {
        std::shared_ptr<RealizationControl> control;
        std::thread thread;
    };

    struct GlowScratch {
        Microsoft::WRL::ComPtr<ID2D1Bitmap1> bitmap;
        UINT32 width = 0;
        UINT32 height = 0;
    };

    RenderScene scene;
    std::vector<CachedLine> lines;
    std::vector<CachedImage> images;
    std::vector<CachedBrush> brushes;
    std::uint64_t brushUseSerial = 0;
    static constexpr std::size_t brushCapacity = 512;
    Microsoft::WRL::ComPtr<ID2D1DeviceContext1> realizationContext;
    std::uint64_t realizationCount = 0;
    std::uint64_t realizationGeneration = 0;
    static constexpr std::size_t defaultRealizationCapacity = 8192;
    static constexpr float realizationStrokeThreshold = 8.0f;
    std::shared_ptr<RealizationControl> realizationControl;
    std::thread realizationThread;
    std::vector<RetiredRealizationWorker> retiredRealizationWorkers;
    std::atomic<bool> realizationPrewarmComplete{true};
    std::atomic<bool> renderActive{false};
    std::atomic<std::int64_t> lastRenderCompletedMs{0};
    mutable std::mutex realizationMutex;
    BackendDiagnostics diagnostics;
    bool configured = false;
    int frameSurfaceWidth = 0;
    int frameSurfaceHeight = 0;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> frameTargetTexture;
    Microsoft::WRL::ComPtr<ID2D1Bitmap1> frameTargetBitmap;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> frameStagingTexture;
    // Persistent glow scratch targets and GaussianBlur effects. Dirty-rect
    // mode grows each scratch slot only to its largest requested region;
    // entries rewind per line after the composite is flushed.
    std::vector<GlowScratch> glowScratchPool;
    std::vector<Microsoft::WRL::ComPtr<ID2D1Effect>> glowEffectPool;
    std::size_t glowScratchInUse = 0;
    std::size_t glowEffectInUse = 0;
#if KROK_GPU_DIAGNOSTICS
    bool countersEnabled = environmentFlagEnabled("KROK_GPU_COUNTERS", true);
#else
    bool countersEnabled = false;
#endif
    bool resourceCacheEnabled = environmentFlagEnabled(
        "KROK_GPU_RESOURCE_CACHE", true
    );
    bool realizationEnabled = environmentFlagEnabled(
        "KROK_GPU_REALIZATION", true
    );
    bool realizationActive = false;
    bool glowDirtyRectEnabled = environmentFlagEnabled(
        "KROK_GPU_GLOW_DIRTY_RECT", true
    );
    // N3 transforms one base glyph geometry and applies dynamic edge widths
    // with DrawGeometry.  Keep an environment rollback while this path is
    // measured against the previous transform(pre-expanded stroke)+FillGeometry
    // implementation.
    bool dynamicDirectStrokeEnabled = environmentFlagEnabled(
        "KROK_GPU_DYNAMIC_DIRECT_STROKE", true
    );
};

Direct2DGpuBackend::Direct2DGpuBackend(bool forceWarp)
    : device_(forceWarp), impl_(std::make_unique<Impl>()) {
    if (forceWarp && !environmentFlagEnabled("KROK_GPU_REALIZATION_WARP", false)) {
        impl_->realizationEnabled = false;
    }
    impl_->realizationActive = impl_->realizationEnabled;
    impl_->diagnostics.countersEnabled = impl_->countersEnabled;
    impl_->diagnostics.resourceCacheEnabled = impl_->resourceCacheEnabled;
    impl_->diagnostics.brushCacheCapacity = Impl::brushCapacity;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
    impl_->diagnostics.realizationCapacity = Impl::defaultRealizationCapacity;
    impl_->diagnostics.glowDirtyRectEnabled = impl_->glowDirtyRectEnabled;
    if (impl_->realizationEnabled) {
        device_.d2dContext()->QueryInterface(IID_PPV_ARGS(
            impl_->realizationContext.ReleaseAndGetAddressOf()
        ));
    }
    impl_->diagnostics.realizationSupported =
        impl_->realizationContext != nullptr;
}

Direct2DGpuBackend::Direct2DGpuBackend(
    bool forceWarp,
    std::shared_ptr<D2DDeviceResources> sharedDeviceResources
)
    : device_(std::move(sharedDeviceResources)), impl_(std::make_unique<Impl>()) {
    if (forceWarp && !environmentFlagEnabled("KROK_GPU_REALIZATION_WARP", false)) {
        impl_->realizationEnabled = false;
    }
    impl_->realizationActive = impl_->realizationEnabled;
    impl_->diagnostics.countersEnabled = impl_->countersEnabled;
    impl_->diagnostics.resourceCacheEnabled = impl_->resourceCacheEnabled;
    impl_->diagnostics.brushCacheCapacity = Impl::brushCapacity;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
    impl_->diagnostics.realizationCapacity = Impl::defaultRealizationCapacity;
    impl_->diagnostics.glowDirtyRectEnabled = impl_->glowDirtyRectEnabled;
    if (impl_->realizationEnabled) {
        device_.d2dContext()->QueryInterface(IID_PPV_ARGS(
            impl_->realizationContext.ReleaseAndGetAddressOf()
        ));
    }
    impl_->diagnostics.realizationSupported =
        impl_->realizationContext != nullptr;
}

Direct2DGpuBackend::~Direct2DGpuBackend() {
    if (impl_->realizationControl) {
        impl_->realizationControl->stop.store(true, std::memory_order_release);
    }
    for (Impl::RetiredRealizationWorker &worker
         : impl_->retiredRealizationWorkers) {
        worker.control->stop.store(true, std::memory_order_release);
    }
    if (impl_->realizationThread.joinable()) {
        impl_->realizationThread.join();
    }
    for (Impl::RetiredRealizationWorker &worker
         : impl_->retiredRealizationWorkers) {
        if (worker.thread.joinable()) {
            worker.thread.join();
        }
    }
}

std::shared_ptr<D2DDeviceResources>
Direct2DGpuBackend::sharedDeviceResources() const noexcept {
    return device_.sharedResources();
}

void Direct2DGpuBackend::waitForRealizationPrewarm() {
    if (impl_->realizationThread.joinable()) {
        impl_->realizationThread.join();
    }
}

void Direct2DGpuBackend::adoptSharedGlyphResources(
    const Direct2DGpuBackend &source
) {
    if (device_.d2dDevice() != source.device_.d2dDevice()) {
        throw BackendError("shared glyph resources require one Direct2D device");
    }
    std::scoped_lock lock(impl_->realizationMutex, source.impl_->realizationMutex);
    RenderScene comparableScene = impl_->scene;
    comparableScene.realizationEnabled = source.impl_->scene.realizationEnabled;
    if (!impl_->configured || !source.impl_->configured
        || !(comparableScene == source.impl_->scene)) {
        throw BackendError("shared glyph resources require identical configured scenes");
    }
    impl_->scene.realizationEnabled = source.impl_->scene.realizationEnabled;
    if (!source.impl_->realizationPrewarmComplete.load(std::memory_order_acquire)) {
        throw BackendError("shared glyph resources are not fully prewarmed");
    }
    impl_->lines = source.impl_->lines;
    impl_->realizationActive = source.impl_->realizationActive
        && impl_->realizationContext != nullptr;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
    // COM geometry and realization objects are shared by AddRef through the
    // copied cache. Only the source worker owns/prepared the resource set, so
    // follower diagnostics must not multiply its count or preparation cost.
    impl_->realizationCount = 0;
    impl_->diagnostics.realizationCapacity = 0;
    impl_->diagnostics.realizationPrewarmTasks = 0;
    impl_->diagnostics.realizationPrewarmSkipped = 0;
    impl_->diagnostics.realizationPrewarmMs = 0.0;
    impl_->diagnostics.realizationPrewarmFillTasks = 0;
    impl_->diagnostics.realizationPrewarmStrokeTasks = 0;
    impl_->diagnostics.realizationPrewarmContextMs = 0.0;
    impl_->diagnostics.realizationPrewarmWaitMs = 0.0;
    impl_->diagnostics.realizationPrewarmFillCreateMs = 0.0;
    impl_->diagnostics.realizationPrewarmStrokeCreateMs = 0.0;
    impl_->diagnostics.realizationPrewarmPublishMs = 0.0;
    impl_->diagnostics.realizationPrewarmCreateP50Ms = 0.0;
    impl_->diagnostics.realizationPrewarmCreateP95Ms = 0.0;
    impl_->diagnostics.realizationPrewarmCreateMaxMs = 0.0;
    impl_->realizationPrewarmComplete.store(true, std::memory_order_release);
}

BackendCaps Direct2DGpuBackend::capabilities() const {
    return device_.capabilities();
}

BackendDiagnostics Direct2DGpuBackend::diagnostics() const {
    std::lock_guard<std::mutex> realizationLock(impl_->realizationMutex);
    BackendDiagnostics result = impl_->diagnostics;
    result.realizationPrewarmComplete = impl_->realizationPrewarmComplete.load(
        std::memory_order_acquire
    );
    result.brushCacheSize = impl_->brushes.size();
    result.estimatedCacheBytes += impl_->brushes.size()
        * sizeof(Impl::CachedBrush);
    result.realizationCount = impl_->realizationCount;
    result.estimatedCacheBytes += impl_->realizationCount * 512;
    for (const Impl::GlowScratch &scratch : impl_->glowScratchPool) {
        result.estimatedCacheBytes += static_cast<std::uint64_t>(scratch.width)
            * static_cast<std::uint64_t>(scratch.height) * 4;
    }
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
    if (impl_->realizationControl) {
        impl_->realizationControl->stop.store(true, std::memory_order_release);
    }
    if (impl_->realizationThread.joinable()) {
        if (impl_->realizationControl
            && impl_->realizationControl->done.load(std::memory_order_acquire)) {
            impl_->realizationThread.join();
        } else {
            impl_->retiredRealizationWorkers.push_back({
                impl_->realizationControl,
                std::move(impl_->realizationThread),
            });
        }
    }
    for (auto worker = impl_->retiredRealizationWorkers.begin();
         worker != impl_->retiredRealizationWorkers.end();) {
        if (!worker->control->done.load(std::memory_order_acquire)) {
            ++worker;
            continue;
        }
        if (worker->thread.joinable()) {
            worker->thread.join();
        }
        worker = impl_->retiredRealizationWorkers.erase(worker);
    }
    {
        std::lock_guard<std::mutex> realizationLock(impl_->realizationMutex);
        ++impl_->realizationGeneration;
        impl_->realizationCount = 0;
    }
    impl_->realizationControl.reset();
    impl_->realizationPrewarmComplete.store(true, std::memory_order_release);
    if (!impl_->brushes.empty()) {
        impl_->brushes.clear();
        impl_->brushUseSerial = 0;
        if (impl_->countersEnabled) {
            ++impl_->diagnostics.brushCacheInvalidations;
        }
    }
    if (impl_->frameSurfaceWidth != scene.width || impl_->frameSurfaceHeight != scene.height) {
        impl_->frameTargetBitmap.Reset();
        impl_->frameTargetTexture.Reset();
        impl_->frameStagingTexture.Reset();
        impl_->glowScratchPool.clear();
        impl_->glowEffectPool.clear();
        impl_->glowScratchInUse = 0;
        impl_->glowEffectInUse = 0;
        impl_->frameSurfaceWidth = scene.width;
        impl_->frameSurfaceHeight = scene.height;
    }
    impl_->scene = scene;
    const float layoutScale = std::max(scene.layoutReferenceScale, 0.01f);
    const bool scaledPreviewLayout = std::abs(layoutScale - 1.0f) > 0.000001f;
    const auto referenceInt = [&](float scaledValue, int minimum) {
        const int value = scaledPreviewLayout
            ? static_cast<int>(std::lround(scaledValue / layoutScale))
            : static_cast<int>(scaledValue);
        return std::max(value, minimum);
    };
    const auto scaleReferenceGeometry = [&](Microsoft::WRL::ComPtr<ID2D1PathGeometry> &path,
                                            const char *operation) {
        if (!scaledPreviewLayout || !path) {
            return;
        }
        const D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Scale(
            layoutScale, layoutScale
        );
        Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
        checkHr(
            device_.d2dFactory()->CreateTransformedGeometry(
                path.Get(), &matrix, transformed.ReleaseAndGetAddressOf()
            ),
            operation,
            device_
        );
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> scaledPath;
        checkHr(
            device_.d2dFactory()->CreatePathGeometry(scaledPath.ReleaseAndGetAddressOf()),
            "ID2D1Factory::CreatePathGeometry(scale preview outline)",
            device_
        );
        Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
        checkHr(
            scaledPath->Open(sink.ReleaseAndGetAddressOf()),
            "ID2D1PathGeometry::Open(scale preview outline)",
            device_
        );
        sink->SetFillMode(D2D1_FILL_MODE_WINDING);
        sink->SetSegmentFlags(D2D1_PATH_SEGMENT_FORCE_ROUND_LINE_JOIN);
        const HRESULT simplifyResult = transformed->Simplify(
            D2D1_GEOMETRY_SIMPLIFICATION_OPTION_CUBICS_AND_LINES,
            nullptr,
            sink.Get()
        );
        const HRESULT closeResult = sink->Close();
        checkHr(simplifyResult, operation, device_);
        checkHr(closeResult, "ID2D1GeometrySink::Close(scale preview outline)", device_);
        path = scaledPath;
    };
    impl_->realizationActive = impl_->realizationEnabled
        && scene.realizationEnabled
        && impl_->realizationContext;
    impl_->diagnostics.realizationEnabled = impl_->realizationActive;
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
    auto cacheBitmapImage = [&](const std::wstring &path,
                                std::uint64_t modifiedMs,
                                std::uint64_t size) {
        if (path.empty()) {
            return;
        }
        const bool cached = std::any_of(
            impl_->images.begin(), impl_->images.end(),
            [&](const Impl::CachedImage &image) {
                return image.path == path
                    && image.modifiedMs == modifiedMs
                    && image.size == size;
            }
        );
        if (!cached) {
            impl_->images.push_back(Impl::CachedImage{
                path,
                modifiedMs,
                size,
                loadWicBitmap(device_.d2dContext(), path),
            });
        }
    };
    cacheStyleImages(scene.style);
    for (const TextStyle &style : scene.lineStyles) {
        cacheStyleImages(style);
    }
    for (const TextStyle &style : scene.charStyles) {
        cacheStyleImages(style);
    }
    for (const TextLine &line : scene.lines) {
        for (const TextChar &ch : line.chars) {
            if (!ch.bitmapGuide.has_value()) {
                continue;
            }
            cacheBitmapImage(
                ch.bitmapGuide->beforePath,
                ch.bitmapGuide->beforeModifiedMs,
                ch.bitmapGuide->beforeSize
            );
            cacheBitmapImage(
                ch.bitmapGuide->afterPath,
                ch.bitmapGuide->afterModifiedMs,
                ch.bitmapGuide->afterSize
            );
        }
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
    auto imageForBitmapGuide = [&](const std::wstring &path,
                                   std::uint64_t modifiedMs,
                                   std::uint64_t size) -> ID2D1Bitmap1 * {
        const auto found = std::find_if(
            impl_->images.begin(), impl_->images.end(),
            [&](const Impl::CachedImage &image) {
                return image.path == path
                    && image.modifiedMs == modifiedMs
                    && image.size == size;
            }
        );
        return found == impl_->images.end() ? nullptr : found->bitmap.Get();
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
        cached.sourceLineIndex = sourceLine.sourceLineIndex;
        cached.pageIndex = sourceLine.pageIndex;
        cached.compositeOrder = sourceLine.compositeOrder;
        cached.guideAnchorLeft = sourceLine.guideAnchorLeft;
        cached.guideAnchorRight = sourceLine.guideAnchorRight;
        cached.centerOverride = sourceLine.centerOverride;
        cached.staticOverlay = sourceLine.staticOverlay;
        cached.fadeInMs = sourceLine.fadeInMs;
        cached.fadeOutMs = sourceLine.fadeOutMs;
        cached.entryAnimation = sourceLine.entryAnimation;
        cached.entryDurationMs = sourceLine.entryDurationMs;
        cached.exitAnimation = sourceLine.exitAnimation;
        cached.exitDurationMs = sourceLine.exitDurationMs;
        cached.karaokeAnimation = sourceLine.karaokeAnimation;
        cached.displayWindows = sourceLine.displayWindows;
        cached.placementWindows = sourceLine.placementWindows;
        DWRITE_FONT_METRICS laneMetrics{};
        mainFace->GetMetrics(&laneMetrics);
        const int laneFontSize = referenceInt(style.fontSize, 1);
        const float laneMetricUnits = static_cast<float>(std::max<UINT16>(
            laneMetrics.designUnitsPerEm, 1
        ));
        const float laneAscent = static_cast<float>(laneFontSize) * layoutScale
            * static_cast<float>(laneMetrics.ascent) / laneMetricUnits;
        const float laneDescent = static_cast<float>(laneFontSize) * layoutScale
            * static_cast<float>(laneMetrics.descent) / laneMetricUnits;
        const float laneVisualPad = std::ceil((
            std::max(style.strokeWidth / layoutScale, 0.0f)
            + std::max(style.stroke2Width / layoutScale, 0.0f)
        ) * 0.5f) * layoutScale;
        // Shared horizontal lanes use the line style's main font box. Inline
        // role/guide geometry may overflow visually, but must not change the
        // baseline step for only that line.
        cached.legacyLaneHeight = laneAscent + laneDescent + laneVisualPad * 2.0f;
        cached.legacyLaneDescent = laneDescent + laneVisualPad;
        if (style.layoutSemantics == "n3_1074") {
            const int fontSize = referenceInt(style.fontSize, 1);
            const int edgeSize = referenceInt(style.strokeWidth, 0);
            const int metricTotal = std::max(
                static_cast<int>(laneMetrics.ascent) + static_cast<int>(laneMetrics.descent), 1
            );
            cached.n3DrawHeight = static_cast<float>(fontSize + edgeSize) * layoutScale;
            cached.n3Descent = static_cast<float>(
                fontSize * static_cast<int>(laneMetrics.descent) / metricTotal
                + edgeSize / 2
            ) * layoutScale;
        }
        if (style.vertical && !sourceLine.rubies.empty()) {
            DWRITE_FONT_METRICS rubyMetrics{};
            rubyFace->GetMetrics(&rubyMetrics);
            const float rubyUnits = static_cast<float>(std::max<UINT16>(
                rubyMetrics.designUnitsPerEm, 1
            ));
            // QFontMetrics::height() uses the face's full ascent + descent,
            // rounded to device pixels.  This differs materially from the em
            // size for fonts such as Meiryo (28 px -> 42 px).
            cached.verticalRubyAllowance = std::max(
                std::round(
                    style.rubyFontSize * static_cast<float>(
                        rubyMetrics.ascent + rubyMetrics.descent
                    ) / rubyUnits
                ) + style.rubyGap,
                0.0f
            );
        }
        cached.lane = style.dualLineLayout
            ? sourceLine.lane % std::max(style.laneCount, 1)
            : 0;
        cached.chars.reserve(sourceLine.chars.size());
        bool lineHasBounds = false;
        float cursor = 0.0f;
        float firstSlotDescent = 0.0f;
        float firstSlotEdge = 0.0f;
        float firstSlotEdge2 = 0.0f;
        float maxDrawHeight = layoutScale;
        bool hasFirstSlot = false;

        for (std::size_t charIndex = 0; charIndex < sourceLine.chars.size(); ++charIndex) {
            const TextChar &sourceChar = sourceLine.chars[charIndex];
            const bool hasCharStyle = sourceChar.styleIndex >= 0
                && sourceChar.styleIndex < static_cast<int>(scene.charStyles.size());
            const TextStyle &charStyle = hasCharStyle
                ? scene.charStyles[static_cast<std::size_t>(sourceChar.styleIndex)]
                : style;
            cached.hasInlineStyles = cached.hasInlineStyles || hasCharStyle;
            cached.hasInlineLaneGeometryOverride =
                cached.hasInlineLaneGeometryOverride
                || (hasCharStyle && (
                    charStyle.fontFamily != style.fontFamily
                    || charStyle.latinFontFamily != style.latinFontFamily
                    || charStyle.fontSize != style.fontSize
                    || charStyle.latinFontSize != style.latinFontSize
                    || charStyle.fontWeight != style.fontWeight
                    || charStyle.latinFontWeight != style.latinFontWeight
                    || charStyle.italic != style.italic
                    || charStyle.strokeWidth != style.strokeWidth
                    || charStyle.stroke2Width != style.stroke2Width
                ));
            const bool vectorGlyph = sourceChar.vectorGlyph.has_value();
            const bool bitmapGuide = sourceChar.bitmapGuide.has_value();
            const bool latin = !vectorGlyph && !bitmapGuide && isLatinText(sourceChar.text);
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
            const int unit = referenceInt(fontSize, 1);
            const int edgeSize = referenceInt(charStyle.strokeWidth, 0);
            const int edge2Size = referenceInt(charStyle.stroke2Width, 0);
            cached.maxVisualPad = std::max(
                cached.maxVisualPad,
                std::ceil((
                    std::max(charStyle.strokeWidth / layoutScale, 0.0f)
                    + std::max(charStyle.stroke2Width / layoutScale, 0.0f)
                ) * 0.5f) * layoutScale
            );

            DWRITE_FONT_METRICS fontMetrics{};
            requestedFace->GetMetrics(&fontMetrics);
            if (!hasFirstSlot) {
                const int metricTotal = std::max(
                    static_cast<int>(fontMetrics.ascent)
                        + static_cast<int>(fontMetrics.descent),
                    1
                );
                firstSlotDescent = static_cast<float>(
                    unit * static_cast<int>(fontMetrics.descent) / metricTotal
                ) * layoutScale;
                firstSlotEdge = static_cast<float>(edgeSize) * layoutScale;
                firstSlotEdge2 = static_cast<float>(edge2Size) * layoutScale;
                hasFirstSlot = true;
            }
            maxDrawHeight = std::max(
                maxDrawHeight, static_cast<float>(unit + edgeSize) * layoutScale
            );
            // The product's lane boxes remain Painter-compatible. N3's exact
            // glyph bearings/outline are used inside those boxes, while the
            // face's em scale keeps mixed-font baselines close to QFontMetrics.
            const float verticalUnits = static_cast<float>(std::max<UINT16>(
                fontMetrics.designUnitsPerEm,
                1
            ));
            const float charAscent = static_cast<float>(unit) * layoutScale
                * static_cast<float>(fontMetrics.ascent) / verticalUnits;
            const float charDescent = static_cast<float>(unit) * layoutScale
                * static_cast<float>(fontMetrics.descent) / verticalUnits;
            cached.ascent = std::max(cached.ascent, charAscent);
            cached.descent = std::max(cached.descent, charDescent);
            const float boxMetricTotal = static_cast<float>(std::max(
                static_cast<int>(fontMetrics.ascent) + static_cast<int>(fontMetrics.descent),
                1
            ));
            const float charBoxAscent =
                static_cast<float>(unit) * layoutScale
                    * static_cast<float>(fontMetrics.ascent) / boxMetricTotal
                + static_cast<float>(edgeSize) * layoutScale * 0.5f;
            const float charBoxDescent =
                static_cast<float>(unit) * layoutScale
                    * static_cast<float>(fontMetrics.descent) / boxMetricTotal
                + static_cast<float>(edgeSize) * layoutScale * 0.5f;
            cached.n3CharAscent = std::max(cached.n3CharAscent, charBoxAscent);
            cached.n3CharDescent = std::max(cached.n3CharDescent, charBoxDescent);
            cached.hasN3CharBox = true;
            if (!isWhitespaceText(sourceChar.text) && charStyle.affectsRubyAnchor) {
                cached.boxAscent = std::max(cached.boxAscent, charBoxAscent);
                cached.hasRubyAnchor = true;
            }

            std::vector<UINT16> glyphs;
            Microsoft::WRL::ComPtr<IDWriteFontFace> outlineFace;
            Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
            if (vectorGlyph) {
                path = vectorGlyphGeometry(
                    device_.d2dFactory(), *sourceChar.vectorGlyph,
                    static_cast<float>(unit), device_
                );
            } else if (!bitmapGuide) {
                glyphs = glyphIndices(requestedFace.Get(), sourceChar.text);
                outlineFace = requestedFace;
                if (!validGlyphIndices(glyphs)) {
                    outlineFace = findFallbackFontFace(
                        fontCollection.Get(), sourceChar.text, fallbackFaces, glyphs
                    );
                }
            }
            if (!vectorGlyph && !bitmapGuide && outlineFace && !glyphs.empty()) {
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

            D2D1_RECT_F referenceCharBounds{};
            bool charHasBounds = path != nullptr;
            if (path) {
                checkHr(path->GetBounds(nullptr, &referenceCharBounds), "ID2D1Geometry::GetBounds(character)", device_);
                charHasBounds = std::isfinite(referenceCharBounds.left)
                    && std::isfinite(referenceCharBounds.top)
                    && std::isfinite(referenceCharBounds.right)
                    && std::isfinite(referenceCharBounds.bottom)
                    && referenceCharBounds.right > referenceCharBounds.left;
            }

            float layoutWidth = 0.0f;
            float pathOffset = 0.0f;
            D2D1_RECT_F bitmapRect{};
            bool bitmapHasBounds = false;
            if (bitmapGuide) {
                const BitmapGuide &guide = *sourceChar.bitmapGuide;
                ID2D1Bitmap1 *bitmap = imageForBitmapGuide(
                    guide.beforePath, guide.beforeModifiedMs, guide.beforeSize
                );
                if (bitmap == nullptr && !guide.afterPath.empty()) {
                    bitmap = imageForBitmapGuide(
                        guide.afterPath, guide.afterModifiedMs, guide.afterSize
                    );
                }
                float contentWidth = 1.0f * layoutScale;
                float contentHeight = std::max(static_cast<float>(unit), 1.0f) * layoutScale;
                if (bitmap != nullptr) {
                    const D2D1_SIZE_U pixelSize = bitmap->GetPixelSize();
                    const float imageWidth = std::max(static_cast<float>(pixelSize.width), 1.0f);
                    const float imageHeight = std::max(static_cast<float>(pixelSize.height), 1.0f);
                    if (guide.fixSize) {
                        contentWidth = imageWidth * layoutScale;
                        contentHeight = imageHeight * layoutScale;
                    } else {
                        contentHeight = std::max(
                            static_cast<float>(
                                std::max(
                                    static_cast<int>(unit * guide.zoomPercent) / 100,
                                    1
                                )
                            ),
                            1.0f
                        ) * layoutScale;
                        contentWidth = std::max(
                            contentHeight * imageWidth / imageHeight,
                            1.0f * layoutScale
                        );
                    }
                }
                const float marginLeft = guide.marginLeft * layoutScale;
                const float marginRight = guide.marginRight * layoutScale;
                const float marginBottom = guide.marginBottom * layoutScale;
                const int metricTotal = std::max(
                    static_cast<int>(fontMetrics.ascent)
                        + static_cast<int>(fontMetrics.descent),
                    1
                );
                const float anchorDescent = (
                    static_cast<float>(
                        unit * static_cast<int>(fontMetrics.descent) / metricTotal
                            + edgeSize / 2
                    ) * layoutScale
                );
                const float bitmapBottom = anchorDescent - marginBottom;
                layoutWidth = std::max(
                    contentWidth + marginLeft + marginRight,
                    1.0f * layoutScale
                );
                bitmapRect = D2D1::RectF(
                    cursor + marginLeft,
                    bitmapBottom - contentHeight,
                    cursor + marginLeft + contentWidth,
                    bitmapBottom
                );
                bitmapHasBounds = bitmapRect.right > bitmapRect.left
                    && bitmapRect.bottom > bitmapRect.top;
                if (bitmapHasBounds) {
                    extendBounds(cached.bounds, lineHasBounds, bitmapRect);
                }
            } else if (vectorGlyph) {
                layoutWidth = (static_cast<float>(unit)
                    * std::max(sourceChar.vectorGlyph->advanceWidth, 0.0f)
                    / std::max(sourceChar.vectorGlyph->unitsPerEm, 1.0f));
                layoutWidth = std::max(layoutWidth, 1.0f) * layoutScale;
            } else if (charHasBounds) {
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
                const int inkWidth = std::max(static_cast<int>(
                    referenceCharBounds.right - referenceCharBounds.left
                ), 0);
                int leftBearing = metrics.front().leftSideBearing;
                int rightBearing = metrics.front().rightSideBearing;
                if (!charStyle.allowBiting) {
                    leftBearing = std::max(leftBearing, 0);
                    rightBearing = std::max(rightBearing, 0);
                }
                const int advance = std::max(static_cast<int>(metrics.front().advanceWidth), 1);
                const int bodyWidth = inkWidth * (leftBearing + advance + rightBearing) / advance;
                layoutWidth = static_cast<float>(
                    std::max(bodyWidth, 0) + edgeSize
                ) * layoutScale;
                const int geometryLeft = inkWidth * leftBearing / advance;
                pathOffset = (-referenceCharBounds.left
                    + static_cast<float>(geometryLeft)
                    + static_cast<float>(edgeSize / 2)) * layoutScale;
            } else if (sourceChar.text == L" ") {
                layoutWidth = static_cast<float>(
                    unit * std::clamp(charStyle.spaceWidthPercent, 10, 100) / 100
                ) * layoutScale;
            } else {
                layoutWidth = static_cast<float>(
                    unit * std::clamp(charStyle.spaceWidthPercent, 10, 100) * 25 / 100 / 10
                    + edgeSize
                ) * layoutScale;
            }

            scaleReferenceGeometry(
                path, "ID2D1Factory::CreateTransformedGeometry(scale preview character)"
            );
            D2D1_RECT_F charBounds{};
            if (path && charHasBounds) {
                checkHr(
                    path->GetBounds(nullptr, &charBounds),
                    "ID2D1Geometry::GetBounds(scaled preview character)",
                    device_
                );
            }

            D2D1_RECT_F positionedCharBounds{};
            bool positionedHasBounds = false;
            if (bitmapHasBounds) {
                positionedCharBounds = bitmapRect;
                positionedHasBounds = true;
            } else if (path && charHasBounds) {
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
            const float wipePad = static_cast<float>(edgeSize / 2) * layoutScale;
            cached.chars.push_back(Impl::CachedChar{
                sourceChar.startMs,
                sourceChar.endMs,
                positionedHasBounds ? positionedCharBounds.left - wipePad : cursor,
                positionedHasBounds ? positionedCharBounds.right + wipePad : cursor + layoutWidth,
                cursor,
                cursor + layoutWidth,
                positionedHasBounds ? positionedCharBounds.top : -charAscent,
                positionedHasBounds ? positionedCharBounds.bottom : charDescent,
            });
            cached.chars.back().styleIndex = sourceChar.styleIndex;
            cached.chars.back().bitmapGuide = sourceChar.bitmapGuide;
            cached.chars.back().bitmapRect = bitmapRect;
            cached.chars.back().wipePoints = sourceChar.wipePoints;
            if (cached.chars.back().wipePoints.empty()) {
                cached.chars.back().wipePoints = {
                    WipePoint{sourceChar.startMs, 0.0f},
                    WipePoint{sourceChar.endMs, 1.0f},
                };
            }
            cached.chars.back().boxAscent = charBoxAscent;
            cached.chars.back().pivotX = cursor + layoutWidth * 0.5f;
            cached.chars.back().pivotY = (charDescent - charAscent) * 0.5f;
            if (positionedHasBounds && path) {
                cached.chars.back().geometry = cached.geometries.back();
                cached.chars.back().strokeGeometry = widenedStrokeGeometry(
                    device_.d2dFactory(),
                    cached.chars.back().geometry.Get(),
                    charStyle.strokeWidth,
                    device_
                );
                cached.chars.back().stroke2Geometry = widenedStrokeGeometry(
                    device_.d2dFactory(),
                    cached.chars.back().geometry.Get(),
                    charStyle.stroke2Width > 0.0f
                        ? std::max(charStyle.strokeWidth, 0.0f)
                            + charStyle.stroke2Width
                        : 0.0f,
                    device_
                );
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
            if (charIndex + 1 < sourceLine.chars.size()) {
                // N3's AlignOneLine never lets a sufficiently negative
                // LyricsInterval move the next character back past this one.
                cursor += std::max(layoutWidth + charStyle.letterSpacing, 0.0f);
            } else {
                cursor += layoutWidth;
            }
        }

        if (style.rightToLeft && !style.vertical && !cached.chars.empty()) {
            auto translateGeometry = [&](ID2D1Geometry *source, float offsetX,
                                         Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                                         const char *operation) {
                if (source == nullptr) {
                    target.Reset();
                    return;
                }
                const D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Translation(
                    offsetX, 0.0f
                );
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        source, &matrix, transformed.ReleaseAndGetAddressOf()
                    ),
                    operation,
                    device_
                );
                target = transformed;
            };
            cached.bounds = {};
            cached.geometries.clear();
            lineHasBounds = false;
            for (Impl::CachedChar &ch : cached.chars) {
                const float oldLayoutLeft = ch.layoutLeft;
                const float oldLayoutRight = ch.layoutRight;
                const float newLayoutLeft = cursor - oldLayoutRight;
                const float offsetX = newLayoutLeft - oldLayoutLeft;
                ch.left += offsetX;
                ch.right += offsetX;
                ch.layoutLeft = newLayoutLeft;
                ch.layoutRight = cursor - oldLayoutLeft;
                ch.pivotX += offsetX;
                if (ch.bitmapGuide.has_value()) {
                    ch.bitmapRect.left += offsetX;
                    ch.bitmapRect.right += offsetX;
                }
                translateGeometry(
                    ch.geometry.Get(), offsetX, ch.geometry,
                    "ID2D1Factory::CreateTransformedGeometry(RTL character)"
                );
                translateGeometry(
                    ch.protectedStrokeGeometry.Get(), offsetX,
                    ch.protectedStrokeGeometry,
                    "ID2D1Factory::CreateTransformedGeometry(RTL protected stroke)"
                );
                translateGeometry(
                    ch.strokeGeometry.Get(), offsetX, ch.strokeGeometry,
                    "ID2D1Factory::CreateTransformedGeometry(RTL stroke)"
                );
                translateGeometry(
                    ch.stroke2Geometry.Get(), offsetX, ch.stroke2Geometry,
                    "ID2D1Factory::CreateTransformedGeometry(RTL stroke2)"
                );
                if (ch.geometry) {
                    D2D1_RECT_F bounds{};
                    checkHr(
                        ch.geometry->GetBounds(nullptr, &bounds),
                        "ID2D1Geometry::GetBounds(RTL character)",
                        device_
                    );
                    extendBounds(cached.bounds, lineHasBounds, bounds);
                    cached.geometries.push_back(ch.geometry);
                } else if (ch.bitmapGuide.has_value()) {
                    extendBounds(cached.bounds, lineHasBounds, ch.bitmapRect);
                }
            }
        }

        if (style.vertical && !cached.chars.empty()) {
            DWRITE_FONT_METRICS verticalMetrics{};
            mainFace->GetMetrics(&verticalMetrics);
            const float designUnits = static_cast<float>(std::max<UINT16>(
                verticalMetrics.designUnitsPerEm, 1
            ));
            const float cellWidth = std::max(style.fontSize, 1.0f);
            const float cellHeight = std::max(
                style.fontSize
                    * static_cast<float>(verticalMetrics.ascent + verticalMetrics.descent)
                    / designUnits,
                1.0f
            );
            const float verticalAscent = style.fontSize
                * static_cast<float>(verticalMetrics.ascent) / designUnits;
            cached.geometries.clear();
            cached.bounds = {};
            lineHasBounds = false;
            auto transformVertical = [&](ID2D1Geometry *source,
                                         const D2D1_MATRIX_3X2_F &matrix,
                                         Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                                         const char *operation) {
                if (source == nullptr) {
                    target.Reset();
                    return;
                }
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        source, &matrix, transformed.ReleaseAndGetAddressOf()
                    ),
                    operation,
                    device_
                );
                target = transformed;
            };
            for (std::size_t index = 0; index < cached.chars.size(); ++index) {
                Impl::CachedChar &ch = cached.chars[index];
                const float cellTop = static_cast<float>(index) * cellHeight;
                // Painter advances the vertical wipe through every fixed cell,
                // including spaces and other glyphs with no outline geometry.
                ch.top = cellTop;
                ch.bottom = cellTop + cellHeight;
                if (ch.bitmapGuide.has_value()) {
                    const float bitmapWidth = std::max(
                        ch.bitmapRect.right - ch.bitmapRect.left, 1.0f
                    );
                    const float bitmapHeight = std::max(
                        ch.bitmapRect.bottom - ch.bitmapRect.top, 1.0f
                    );
                    ch.bitmapRect = D2D1::RectF(
                        -bitmapWidth * 0.5f,
                        cellTop + (cellHeight - bitmapHeight) * 0.5f,
                        bitmapWidth * 0.5f,
                        cellTop + (cellHeight + bitmapHeight) * 0.5f
                    );
                    ch.left = ch.bitmapRect.left;
                    ch.right = ch.bitmapRect.right;
                    ch.top = ch.bitmapRect.top;
                    ch.bottom = ch.bitmapRect.bottom;
                }
                const auto [offsetX, offsetY] = verticalGlyphOffset(
                    sourceLine.chars[index].text, cellWidth, cellHeight
                );
                const bool vectorGlyph = sourceLine.chars[index].vectorGlyph.has_value();
                D2D1_MATRIX_3X2_F matrix{};
                if (vectorGlyph && ch.geometry) {
                    D2D1_RECT_F vectorBounds{};
                    checkHr(
                        ch.geometry->GetBounds(nullptr, &vectorBounds),
                        "ID2D1Geometry::GetBounds(vertical vector glyph)",
                        device_
                    );
                    matrix = D2D1::Matrix3x2F::Translation(
                        -(vectorBounds.left + vectorBounds.right) * 0.5f,
                        cellTop + cellHeight * 0.5f
                            - (vectorBounds.top + vectorBounds.bottom) * 0.5f
                    );
                } else {
                    matrix = D2D1::Matrix3x2F::Translation(
                        -ch.pivotX + offsetX,
                        cellTop + verticalAscent + offsetY
                    );
                }
                if (!vectorGlyph && verticalRotates(sourceLine.chars[index].text)) {
                    matrix = matrix * D2D1::Matrix3x2F::Rotation(
                        90.0f, D2D1::Point2F(0.0f, cellTop + cellHeight * 0.5f)
                    );
                }
                transformVertical(
                    ch.geometry.Get(), matrix, ch.geometry,
                    "ID2D1Factory::CreateTransformedGeometry(vertical character)"
                );
                transformVertical(
                    ch.protectedStrokeGeometry.Get(), matrix,
                    ch.protectedStrokeGeometry,
                    "ID2D1Factory::CreateTransformedGeometry(vertical protected stroke)"
                );
                transformVertical(
                    ch.strokeGeometry.Get(), matrix, ch.strokeGeometry,
                    "ID2D1Factory::CreateTransformedGeometry(vertical stroke)"
                );
                transformVertical(
                    ch.stroke2Geometry.Get(), matrix, ch.stroke2Geometry,
                    "ID2D1Factory::CreateTransformedGeometry(vertical stroke2)"
                );
                if (ch.geometry) {
                    D2D1_RECT_F bounds{};
                    checkHr(
                        ch.geometry->GetBounds(nullptr, &bounds),
                        "ID2D1Geometry::GetBounds(vertical character)",
                        device_
                    );
                    const TextStyle &charStyle = ch.styleIndex >= 0
                        && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                        ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                        : style;
                    const float wipePad = static_cast<float>(
                        std::max(static_cast<int>(charStyle.strokeWidth), 0) / 2
                    );
                    ch.left = bounds.left - wipePad;
                    ch.right = bounds.right + wipePad;
                    extendBounds(cached.bounds, lineHasBounds, bounds);
                    cached.geometries.push_back(ch.geometry);
                } else if (ch.bitmapGuide.has_value()) {
                    extendBounds(cached.bounds, lineHasBounds, ch.bitmapRect);
                }
                ch.layoutLeft = -cellWidth * 0.5f;
                ch.layoutRight = cellWidth * 0.5f;
                ch.pivotX = 0.0f;
                ch.pivotY = cellTop + cellHeight * 0.5f;
            }
            cached.fillBounds = D2D1::RectF(
                -cellWidth * 0.5f,
                0.0f,
                cellWidth * 0.5f,
                cellHeight * static_cast<float>(cached.chars.size())
            );
        } else if (hasFirstSlot) {
            const float drawBottom = firstSlotDescent + std::floor(
                firstSlotEdge / layoutScale / 2.0f
            ) * layoutScale;
            const float inset = std::floor(
                (firstSlotEdge + firstSlotEdge2) / layoutScale / 2.0f
            ) * layoutScale;
            cached.fillBounds = D2D1::RectF(
                0.0f,
                drawBottom - maxDrawHeight + inset,
                std::max(cursor, 1.0f),
                std::max(drawBottom - inset, drawBottom - maxDrawHeight + inset + layoutScale)
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
            const bool rubyIsLatin = std::all_of(
                sourceRuby.units.begin(),
                sourceRuby.units.end(),
                [](const RubyUnit &unit) { return isLatinText(unit.text); }
            );
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
            const int rubyEdgeSize = referenceInt(rubyStyle.rubyStrokeWidth, 0);
            const int rubyAnchorEdgeSize = rubyEdgeSize;

            for (const RubyUnit &sourceUnit : sourceRuby.units) {
                const bool latin = isLatinText(sourceUnit.text);
                const auto &measureFace = latin
                    ? selectedRubyLatinFace
                    : selectedRubyFace;
                const auto &drawingFace = measureFace;
                const float measureFontSize = latin
                    ? rubyStyle.rubyLatinFontSize.value_or(rubyStyle.rubyFontSize)
                    : rubyStyle.rubyFontSize;
                const float drawingFontSize = measureFontSize;
                const int measureUnit = referenceInt(measureFontSize, 1);
                const int drawingUnit = referenceInt(drawingFontSize, 1);
                DWRITE_FONT_METRICS fontMetrics{};
                drawingFace->GetMetrics(&fontMetrics);
                const float boxMetricTotal = static_cast<float>(std::max(
                    static_cast<int>(fontMetrics.ascent) + static_cast<int>(fontMetrics.descent),
                    1
                ));
                rubyBoxDescent = std::max(
                    rubyBoxDescent,
                    (static_cast<float>(drawingUnit)
                        * static_cast<float>(fontMetrics.descent) / boxMetricTotal
                        + static_cast<float>(rubyAnchorEdgeSize) * 0.5f)
                        * layoutScale
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

                D2D1_RECT_F referenceRubyBounds{};
                bool hasBounds = path != nullptr;
                if (path) {
                    checkHr(
                        path->GetBounds(nullptr, &referenceRubyBounds),
                        "ID2D1Geometry::GetBounds(ruby character)",
                        device_
                    );
                    hasBounds = std::isfinite(referenceRubyBounds.left)
                        && std::isfinite(referenceRubyBounds.right)
                        && referenceRubyBounds.right > referenceRubyBounds.left;
                }
                RubyGlyph glyph;
                glyph.source = &sourceUnit;
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
                        static_cast<int>(
                            referenceRubyBounds.right - referenceRubyBounds.left
                        ), 0
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
                    ) * layoutScale;
                    const int geometryLeft = inkWidth * leftBearing / advance;
                    glyph.pathOffset = (-referenceRubyBounds.left
                        + static_cast<float>(geometryLeft)
                        + static_cast<float>(rubyEdgeSize / 2)) * layoutScale;
                } else if (sourceUnit.text == L" ") {
                    glyph.layoutWidth = static_cast<float>(
                        measureUnit * std::clamp(rubyStyle.spaceWidthPercent, 10, 100) / 100
                            + rubyEdgeSize
                    ) * layoutScale;
                } else {
                    glyph.layoutWidth = static_cast<float>(
                        measureUnit * std::clamp(rubyStyle.spaceWidthPercent, 10, 100) * 25 / 100 / 10
                            + rubyEdgeSize
                    ) * layoutScale;
                }
                scaleReferenceGeometry(
                    path, "ID2D1Factory::CreateTransformedGeometry(scale preview ruby)"
                );
                glyph.geometry = path;
                if (path && hasBounds) {
                    checkHr(
                        path->GetBounds(nullptr, &glyph.bounds),
                        "ID2D1Geometry::GetBounds(scaled preview ruby)",
                        device_
                    );
                }
                naturalWidth += glyph.layoutWidth;
                rubyGlyphs.push_back(std::move(glyph));
            }
            if (rubyGlyphs.empty()) {
                continue;
            }

            const float targetLeft = std::min(
                cached.chars[static_cast<std::size_t>(sourceRuby.firstCharIndex)].layoutLeft,
                cached.chars[static_cast<std::size_t>(sourceRuby.lastCharIndex)].layoutLeft
            );
            const float targetRight = std::max(
                cached.chars[static_cast<std::size_t>(sourceRuby.firstCharIndex)].layoutRight,
                cached.chars[static_cast<std::size_t>(sourceRuby.lastCharIndex)].layoutRight
            );
            const float targetWidth = std::max(
                targetRight - targetLeft, layoutScale
            );
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
                    + static_cast<float>(static_cast<int>(
                        (targetWidth - contentWidth) / layoutScale
                    ) / 2) * layoutScale;
            }
            std::vector<float> rubyOrigins(rubyGlyphs.size(), rubyCursor);
            float layoutCursor = rubyCursor;
            for (std::size_t visualIndex = 0;
                 visualIndex < rubyGlyphs.size(); ++visualIndex) {
                const std::size_t logicalIndex = style.rightToLeft
                    ? rubyGlyphs.size() - visualIndex - 1
                    : visualIndex;
                rubyOrigins[logicalIndex] = (centered || rubyGlyphs.size() == 1)
                    ? layoutCursor
                    : static_cast<float>(static_cast<int>(
                        layoutCursor / layoutScale
                    )) * layoutScale;
                layoutCursor += rubyGlyphs[logicalIndex].layoutWidth;
                if (visualIndex + 1 < rubyGlyphs.size()) {
                    layoutCursor += gap;
                }
            }

            Impl::CachedRuby ruby;
            ruby.startMs = sourceRuby.startMs;
            ruby.endMs = sourceRuby.endMs;
            ruby.styleIndex = sourceRuby.styleIndex;
            ruby.transitionCharIndex = sourceRuby.firstCharIndex;
            ruby.firstCharIndex = sourceRuby.firstCharIndex;
            ruby.lastCharIndex = sourceRuby.lastCharIndex;
            ruby.baselineOffset = -cached.boxAscent - style.rubyGap - rubyBoxDescent;
            DWRITE_FONT_METRICS rubyFillMetrics{};
            const auto &rubyFillFace = rubyIsLatin
                ? selectedRubyLatinFace
                : selectedRubyFace;
            rubyFillFace->GetMetrics(&rubyFillMetrics);
            const int rubyMetricTotal = std::max(
                static_cast<int>(rubyFillMetrics.ascent)
                    + static_cast<int>(rubyFillMetrics.descent),
                1
            );
            const int rubyFillSize = referenceInt(
                rubyIsLatin
                    ? rubyStyle.rubyLatinFontSize.value_or(rubyStyle.rubyFontSize)
                    : rubyStyle.rubyFontSize,
                1
            );
            const int rubyFillDescent = rubyFillSize
                * static_cast<int>(rubyFillMetrics.descent) / rubyMetricTotal;
            ruby.pivotX = rubyCursor + contentWidth * 0.5f;
            ruby.pivotY = ruby.baselineOffset
                + static_cast<float>(rubyFillDescent) * layoutScale
                - static_cast<float>(rubyFillSize) * layoutScale * 0.5f;
            const int rubyDrawEdge = referenceInt(rubyStyle.rubyStrokeWidth, 0);
            const int rubyDrawEdge2 = referenceInt(rubyStyle.rubyStroke2Width, 0);
            const float rubyDrawBottom = ruby.baselineOffset
                + static_cast<float>(rubyFillDescent + rubyDrawEdge / 2) * layoutScale;
            const float rubyInset = static_cast<float>(
                (rubyDrawEdge + rubyDrawEdge2) / 2
            ) * layoutScale;
            ruby.fillBounds = D2D1::RectF(
                targetLeft,
                rubyDrawBottom - static_cast<float>(rubyFillSize + rubyDrawEdge) * layoutScale
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
                const float origin = rubyOrigins[unitIndex];
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
                    ruby.strokeGeometries.push_back(widenedStrokeGeometry(
                        device_.d2dFactory(), positioned.Get(),
                        rubyStyle.rubyStrokeWidth, device_
                    ));
                    ruby.stroke2Geometries.push_back(widenedStrokeGeometry(
                        device_.d2dFactory(), positioned.Get(),
                        rubyStyle.rubyStroke2Width > 0.0f
                            ? std::max(rubyStyle.rubyStrokeWidth, 0.0f)
                                + rubyStyle.rubyStroke2Width
                            : 0.0f,
                        device_
                    ));
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
                const float wipePad = static_cast<float>(rubyEdgeSize / 2);
                ruby.chars.push_back(Impl::CachedChar{
                    glyph.source->startMs,
                    glyph.source->endMs,
                    positionedHasBounds ? positionedBounds.left - wipePad : origin,
                    positionedHasBounds
                        ? positionedBounds.right + wipePad
                        : origin + glyph.layoutWidth,
                    origin,
                    origin + glyph.layoutWidth,
                    positionedHasBounds ? positionedBounds.top : ruby.bounds.top,
                    positionedHasBounds ? positionedBounds.bottom : ruby.bounds.bottom,
                });
                ruby.chars.back().pivotX = origin + glyph.layoutWidth * 0.5f;
                ruby.chars.back().pivotY = ruby.pivotY;
                ruby.chars.back().wipePoints = {
                    WipePoint{glyph.source->startMs, 0.0f},
                    WipePoint{glyph.source->endMs, 1.0f},
                };
            }
            if (style.vertical && rubyHasBounds && !ruby.geometries.empty()) {
                const float mainCellWidth = std::max(style.fontSize, 1.0f);
                DWRITE_FONT_METRICS mainVerticalMetrics{};
                mainFace->GetMetrics(&mainVerticalMetrics);
                const float mainUnits = static_cast<float>(std::max<UINT16>(
                    mainVerticalMetrics.designUnitsPerEm, 1
                ));
                const float mainCellHeight = std::max(
                    style.fontSize * static_cast<float>(
                        mainVerticalMetrics.ascent + mainVerticalMetrics.descent
                    ) / mainUnits,
                    1.0f
                );
                DWRITE_FONT_METRICS rubyVerticalMetrics{};
                selectedRubyFace->GetMetrics(&rubyVerticalMetrics);
                const float rubyUnits = static_cast<float>(std::max<UINT16>(
                    rubyVerticalMetrics.designUnitsPerEm, 1
                ));
                const float rubyCellWidth = std::max(rubyStyle.rubyFontSize, 1.0f);
                const float rubyAscent = rubyStyle.rubyFontSize
                    * static_cast<float>(rubyVerticalMetrics.ascent) / rubyUnits;
                const float rubyX = mainCellWidth * 0.5f + style.rubyGap
                    + rubyCellWidth * 0.5f;
                const float baseTop = static_cast<float>(sourceRuby.firstCharIndex)
                    * mainCellHeight;
                const float spanHeight = static_cast<float>(
                    sourceRuby.lastCharIndex - sourceRuby.firstCharIndex + 1
                ) * mainCellHeight;
                ruby.bounds = {};
                rubyHasBounds = false;
                auto transformRubyVertical = [&](ID2D1Geometry *source,
                                                  const D2D1_MATRIX_3X2_F &matrix,
                                                  Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                                                  const char *operation) {
                    if (source == nullptr) {
                        target.Reset();
                        return;
                    }
                    Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                    checkHr(
                        device_.d2dFactory()->CreateTransformedGeometry(
                            source, &matrix, transformed.ReleaseAndGetAddressOf()
                        ),
                        operation,
                        device_
                    );
                    target = transformed;
                };
                const std::size_t count = sourceRuby.units.size();
                std::size_t geometryIndex = 0;
                for (std::size_t unitIndex = 0; unitIndex < count; ++unitIndex) {
                    const float slotTop = baseTop + spanHeight
                        * static_cast<float>(unitIndex) / static_cast<float>(count);
                    const float slotHeight = spanHeight / static_cast<float>(count);
                    const auto [offsetX, offsetY] = verticalGlyphOffset(
                        sourceRuby.units[unitIndex].text,
                        rubyCellWidth,
                        slotHeight
                    );
                    D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Translation(
                        -ruby.chars[unitIndex].pivotX + rubyX + offsetX,
                        slotTop + rubyAscent - ruby.baselineOffset + offsetY
                    );
                    if (verticalRotates(sourceRuby.units[unitIndex].text)) {
                        matrix = matrix * D2D1::Matrix3x2F::Rotation(
                            90.0f,
                            D2D1::Point2F(rubyX, slotTop + slotHeight * 0.5f)
                        );
                    }
                    ruby.chars[unitIndex].left = rubyX - rubyCellWidth * 0.5f;
                    ruby.chars[unitIndex].right = rubyX + rubyCellWidth * 0.5f;
                    ruby.chars[unitIndex].top = slotTop;
                    ruby.chars[unitIndex].bottom = slotTop + slotHeight;
                    if (!rubyGlyphs[unitIndex].geometry) {
                        continue;
                    }
                    transformRubyVertical(
                        ruby.geometries[geometryIndex].Get(), matrix,
                        ruby.geometries[geometryIndex],
                        "ID2D1Factory::CreateTransformedGeometry(vertical ruby)"
                    );
                    if (geometryIndex < ruby.protectedStrokeGeometries.size()) {
                        transformRubyVertical(
                            ruby.protectedStrokeGeometries[geometryIndex].Get(), matrix,
                            ruby.protectedStrokeGeometries[geometryIndex],
                            "ID2D1Factory::CreateTransformedGeometry(vertical ruby protected)"
                        );
                    }
                    if (geometryIndex < ruby.strokeGeometries.size()) {
                        transformRubyVertical(
                            ruby.strokeGeometries[geometryIndex].Get(), matrix,
                            ruby.strokeGeometries[geometryIndex],
                            "ID2D1Factory::CreateTransformedGeometry(vertical ruby stroke)"
                        );
                    }
                    if (geometryIndex < ruby.stroke2Geometries.size()) {
                        transformRubyVertical(
                            ruby.stroke2Geometries[geometryIndex].Get(), matrix,
                            ruby.stroke2Geometries[geometryIndex],
                            "ID2D1Factory::CreateTransformedGeometry(vertical ruby stroke2)"
                        );
                    }
                    D2D1_RECT_F bounds{};
                    checkHr(
                        ruby.geometries[geometryIndex]->GetBounds(nullptr, &bounds),
                        "ID2D1Geometry::GetBounds(vertical ruby)",
                        device_
                    );
                    extendBounds(ruby.bounds, rubyHasBounds, bounds);
                    ruby.chars[unitIndex].left = bounds.left;
                    ruby.chars[unitIndex].right = bounds.right;
                    ruby.chars[unitIndex].top = bounds.top;
                    ruby.chars[unitIndex].bottom = bounds.bottom;
                    ++geometryIndex;
                }
                ruby.fillBounds = D2D1::RectF(
                    rubyX - rubyCellWidth * 0.5f,
                    baseTop,
                    rubyX + rubyCellWidth * 0.5f,
                    baseTop + spanHeight
                );
                ruby.pivotX = rubyX;
                ruby.pivotY = baseTop + spanHeight * 0.5f;
            }
            if (rubyHasBounds && !ruby.geometries.empty()) {
                cached.rubies.push_back(std::move(ruby));
            }
        }
        if (!style.vertical && !style.rightToLeft && cached.rubies.size() > 1) {
            auto translateGeometryX = [&](Microsoft::WRL::ComPtr<ID2D1Geometry> &geometry,
                                          float offsetX,
                                          const char *operation) {
                if (!geometry || offsetX == 0.0f) {
                    return;
                }
                const D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Translation(
                    offsetX, 0.0f
                );
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        geometry.Get(), &matrix, transformed.ReleaseAndGetAddressOf()
                    ),
                    operation,
                    device_
                );
                geometry = transformed;
            };
            auto translateCharX = [&](Impl::CachedChar &ch, float offsetX) {
                ch.left += offsetX;
                ch.right += offsetX;
                ch.layoutLeft += offsetX;
                ch.layoutRight += offsetX;
                ch.pivotX += offsetX;
                translateGeometryX(
                    ch.geometry, offsetX,
                    "ID2D1Factory::CreateTransformedGeometry(ruby interference character)"
                );
                translateGeometryX(
                    ch.protectedStrokeGeometry, offsetX,
                    "ID2D1Factory::CreateTransformedGeometry(ruby interference protected stroke)"
                );
                translateGeometryX(
                    ch.strokeGeometry, offsetX,
                    "ID2D1Factory::CreateTransformedGeometry(ruby interference stroke)"
                );
                translateGeometryX(
                    ch.stroke2Geometry, offsetX,
                    "ID2D1Factory::CreateTransformedGeometry(ruby interference stroke2)"
                );
            };
            auto translateRubyX = [&](Impl::CachedRuby &ruby, float offsetX) {
                ruby.bounds.left += offsetX;
                ruby.bounds.right += offsetX;
                ruby.fillBounds.left += offsetX;
                ruby.fillBounds.right += offsetX;
                ruby.pivotX += offsetX;
                for (Impl::CachedChar &ch : ruby.chars) {
                    ch.left += offsetX;
                    ch.right += offsetX;
                    ch.layoutLeft += offsetX;
                    ch.layoutRight += offsetX;
                    ch.pivotX += offsetX;
                }
                for (auto &geometry : ruby.geometries) {
                    translateGeometryX(
                        geometry, offsetX,
                        "ID2D1Factory::CreateTransformedGeometry(ruby interference ruby)"
                    );
                }
                for (auto &geometry : ruby.protectedStrokeGeometries) {
                    translateGeometryX(
                        geometry, offsetX,
                        "ID2D1Factory::CreateTransformedGeometry(ruby interference ruby protected stroke)"
                    );
                }
                for (auto &geometry : ruby.strokeGeometries) {
                    translateGeometryX(
                        geometry, offsetX,
                        "ID2D1Factory::CreateTransformedGeometry(ruby interference ruby stroke)"
                    );
                }
                for (auto &geometry : ruby.stroke2Geometries) {
                    translateGeometryX(
                        geometry, offsetX,
                        "ID2D1Factory::CreateTransformedGeometry(ruby interference ruby stroke2)"
                    );
                }
            };

            for (std::size_t rubyIndex = 1; rubyIndex < cached.rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &previous = cached.rubies[rubyIndex - 1];
                Impl::CachedRuby &current = cached.rubies[rubyIndex];
                if (previous.chars.empty() || current.chars.empty()) {
                    continue;
                }
                const float deficit = previous.chars.back().layoutRight
                    + style.rubyInterval - current.chars.front().layoutLeft;
                if (deficit <= 0.0f) {
                    continue;
                }
                const float push = std::ceil(deficit);
                const std::size_t firstChar = static_cast<std::size_t>(std::clamp(
                    current.firstCharIndex,
                    0,
                    static_cast<int>(cached.chars.size())
                ));
                for (std::size_t charIndex = firstChar;
                     charIndex < cached.chars.size(); ++charIndex) {
                    translateCharX(cached.chars[charIndex], push);
                }
                for (std::size_t followingIndex = rubyIndex;
                     followingIndex < cached.rubies.size(); ++followingIndex) {
                    translateRubyX(cached.rubies[followingIndex], push);
                }
                cursor += push;
            }

            cached.geometries.clear();
            cached.bounds = {};
            lineHasBounds = false;
            for (const Impl::CachedChar &ch : cached.chars) {
                if (ch.geometry) {
                    D2D1_RECT_F bounds{};
                    checkHr(
                        ch.geometry->GetBounds(nullptr, &bounds),
                        "ID2D1Geometry::GetBounds(ruby interference character)",
                        device_
                    );
                    extendBounds(cached.bounds, lineHasBounds, bounds);
                    cached.geometries.push_back(ch.geometry);
                } else if (ch.bitmapGuide.has_value()) {
                    extendBounds(cached.bounds, lineHasBounds, ch.bitmapRect);
                }
            }
            cached.fillBounds.right = std::max(cursor, 1.0f);
        }
        const auto adjustWipeEnd = [](Impl::CachedChar &current,
                                      const Impl::CachedChar &following,
                                      bool rtl) {
            if (current.wipePoints.empty()) {
                return;
            }
            const float width = std::max(
                current.layoutRight - current.layoutLeft + 1.0f, 1.0f
            );
            if (!rtl && current.layoutRight >= following.layoutLeft) {
                current.wipePoints.back().position = std::clamp(
                    (following.layoutLeft - current.layoutLeft) / width,
                    0.0f, 1.0f
                );
            } else if (rtl && current.layoutLeft <= following.layoutRight) {
                current.wipePoints.back().position = std::clamp(
                    (current.layoutRight - following.layoutRight) / width,
                    0.0f, 1.0f
                );
            }
        };
        if (!style.vertical) {
            const bool rtl = style.rightToLeft;
            for (std::size_t index = 0; index + 1 < cached.chars.size(); ++index) {
                adjustWipeEnd(cached.chars[index], cached.chars[index + 1], rtl);
            }
            Impl::CachedChar *previousRubyChar = nullptr;
            for (Impl::CachedRuby &ruby : cached.rubies) {
                if (previousRubyChar != nullptr && !ruby.chars.empty()) {
                    adjustWipeEnd(*previousRubyChar, ruby.chars.front(), rtl);
                }
                for (std::size_t index = 0; index + 1 < ruby.chars.size(); ++index) {
                    adjustWipeEnd(ruby.chars[index], ruby.chars[index + 1], rtl);
                }
                if (!ruby.chars.empty()) {
                    previousRubyChar = &ruby.chars.back();
                }
            }
        }
        if (!cached.rubies.empty()) {
            D2D1_RECT_F sharedHorizontalBounds = cached.fillBounds;
            for (const Impl::CachedRuby &ruby : cached.rubies) {
                sharedHorizontalBounds.top = std::min(
                    sharedHorizontalBounds.top, ruby.fillBounds.top
                );
                sharedHorizontalBounds.bottom = std::max(
                    sharedHorizontalBounds.bottom, ruby.fillBounds.bottom
                );
            }
            sharedHorizontalBounds.right = std::max(
                sharedHorizontalBounds.right,
                sharedHorizontalBounds.left + 1.0f
            );
            sharedHorizontalBounds.bottom = std::max(
                sharedHorizontalBounds.bottom,
                sharedHorizontalBounds.top + 1.0f
            );
            for (Impl::CachedRuby &ruby : cached.rubies) {
                const TextStyle &rubyStyle = ruby.styleIndex >= 0
                    && ruby.styleIndex < static_cast<int>(scene.charStyles.size())
                    ? scene.charStyles[static_cast<std::size_t>(ruby.styleIndex)]
                    : style;
                ruby.horizontalFillBounds = rubyStyle.rubyHorizontalGradientWithMain
                    ? sharedHorizontalBounds
                    : ruby.fillBounds;
            }
        }
        if (!lineHasBounds) {
            cached.bounds = D2D1::RectF(0.0f, 0.0f, 0.0f, 0.0f);
        }
        impl_->lines.push_back(std::move(cached));
    }
    // Ruby drawing keeps geometry arrays for historical phase ordering. Mirror
    // their final post-layout/post-interference geometry into CachedChar so the
    // realization pack is indexed exactly like the main-character pack.
    for (Impl::CachedLine &line : impl_->lines) {
        for (Impl::CachedRuby &ruby : line.rubies) {
            for (std::size_t index = 0; index < ruby.chars.size(); ++index) {
                Impl::CachedChar &ch = ruby.chars[index];
                if (index < ruby.geometries.size()) {
                    ch.geometry = ruby.geometries[index];
                }
                if (index < ruby.protectedStrokeGeometries.size()) {
                    ch.protectedStrokeGeometry =
                        ruby.protectedStrokeGeometries[index];
                }
                if (index < ruby.strokeGeometries.size()) {
                    ch.strokeGeometry = ruby.strokeGeometries[index];
                }
                if (index < ruby.stroke2Geometries.size()) {
                    ch.stroke2Geometry = ruby.stroke2Geometries[index];
                }
            }
        }
    }
    impl_->diagnostics.realizationPrewarmSkipped = 0;
    impl_->diagnostics.realizationPrewarmTasks = 0;
    impl_->diagnostics.realizationPrewarmMs = 0.0;
    impl_->diagnostics.realizationPrewarmFillTasks = 0;
    impl_->diagnostics.realizationPrewarmStrokeTasks = 0;
    impl_->diagnostics.realizationPrewarmContextMs = 0.0;
    impl_->diagnostics.realizationPrewarmWaitMs = 0.0;
    impl_->diagnostics.realizationPrewarmFillCreateMs = 0.0;
    impl_->diagnostics.realizationPrewarmStrokeCreateMs = 0.0;
    impl_->diagnostics.realizationPrewarmPublishMs = 0.0;
    impl_->diagnostics.realizationPrewarmCreateP50Ms = 0.0;
    impl_->diagnostics.realizationPrewarmCreateP95Ms = 0.0;
    impl_->diagnostics.realizationPrewarmCreateMaxMs = 0.0;
    impl_->lastRenderCompletedMs.store(steadyNowMs(), std::memory_order_release);
    if (impl_->realizationActive) {
        std::vector<std::size_t> lineOrder(impl_->lines.size());
        for (std::size_t index = 0; index < lineOrder.size(); ++index) {
            lineOrder[index] = index;
        }
        const int prewarmTimeMs = impl_->scene.prewarmTimeMs;
        const auto distanceFromPrewarm = [&](const Impl::CachedLine &line) {
            int distance = std::min(
                std::abs(prewarmTimeMs - line.startMs),
                std::abs(prewarmTimeMs - line.endMs)
            );
            if (prewarmTimeMs >= line.startMs && prewarmTimeMs <= line.endMs) {
                distance = 0;
            }
            for (const DisplayWindow &window : line.displayWindows) {
                if (prewarmTimeMs >= window.startMs
                    && prewarmTimeMs <= window.endMs) {
                    return 0;
                }
                distance = std::min(
                    distance,
                    std::min(
                        std::abs(prewarmTimeMs - window.startMs),
                        std::abs(prewarmTimeMs - window.endMs)
                    )
                );
            }
            return distance;
        };
        std::stable_sort(
            lineOrder.begin(), lineOrder.end(),
            [&](std::size_t left, std::size_t right) {
                return distanceFromPrewarm(impl_->lines[left])
                    < distanceFromPrewarm(impl_->lines[right]);
            }
        );
        std::vector<Impl::RealizationTask> tasks;
        const std::size_t realizationCapacity = static_cast<std::size_t>(
            std::max<std::uint64_t>(
                impl_->scene.realizationCapacity,
                Impl::defaultRealizationCapacity
            )
        );
        impl_->diagnostics.realizationCapacity = realizationCapacity;
        tasks.reserve(realizationCapacity);
        std::uint64_t capacitySkipped = 0;
        const auto appendTask = [&] (
            std::size_t lineIndex,
            int rubyIndex,
            std::size_t charIndex,
            Impl::RealizationKind kind,
            ID2D1Geometry *geometry,
            float strokeWidth
        ) {
            const bool isStroke = kind == Impl::RealizationKind::Stroke
                || kind == Impl::RealizationKind::Stroke2;
            if (geometry == nullptr
                || (isStroke && strokeWidth <= 0.0f)) {
                return;
            }
            if (tasks.size() >= realizationCapacity) {
                ++capacitySkipped;
                return;
            }
            Impl::RealizationTask task;
            task.lineIndex = lineIndex;
            task.rubyIndex = rubyIndex;
            task.charIndex = charIndex;
            task.kind = kind;
            task.geometry = geometry;
            task.strokeWidth = strokeWidth;
            tasks.push_back(std::move(task));
        };
        const auto appendCharTasks = [&] (
            std::size_t lineIndex,
            int rubyIndex,
            std::size_t charIndex,
            const Impl::CachedChar &ch,
            float strokeWidth,
            float stroke2Width
        ) {
            const float mainWidth = std::max(strokeWidth, 0.0f);
            if (mainWidth < Impl::realizationStrokeThreshold) {
                return;
            }
            appendTask(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::Fill, ch.geometry.Get(), 0.0f
            );
            appendTask(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::ProtectedStroke,
                ch.protectedStrokeGeometry.Get(), 0.0f
            );
            appendTask(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::Stroke, ch.geometry.Get(), mainWidth
            );
            appendTask(
                lineIndex, rubyIndex, charIndex,
                Impl::RealizationKind::Stroke2, ch.geometry.Get(),
                stroke2Width > 0.0f ? mainWidth + stroke2Width : 0.0f
            );
        };
        for (std::size_t lineIndex : lineOrder) {
            const Impl::CachedLine &line = impl_->lines[lineIndex];
            for (std::size_t charIndex = 0;
                 charIndex < line.chars.size(); ++charIndex) {
                const Impl::CachedChar &ch = line.chars[charIndex];
                const TextStyle &charStyle = ch.styleIndex >= 0
                    && ch.styleIndex < static_cast<int>(impl_->scene.charStyles.size())
                    ? impl_->scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                    : line.style;
                appendCharTasks(
                    lineIndex, -1, charIndex, ch,
                    charStyle.strokeWidth, charStyle.stroke2Width
                );
            }
            for (std::size_t rubyIndex = 0;
                 rubyIndex < line.rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line.rubies[rubyIndex];
                const TextStyle &rubyStyle = ruby.styleIndex >= 0
                    && ruby.styleIndex < static_cast<int>(impl_->scene.charStyles.size())
                    ? impl_->scene.charStyles[static_cast<std::size_t>(ruby.styleIndex)]
                    : line.style;
                for (std::size_t charIndex = 0;
                     charIndex < ruby.chars.size(); ++charIndex) {
                    appendCharTasks(
                        lineIndex, static_cast<int>(rubyIndex), charIndex,
                        ruby.chars[charIndex],
                        rubyStyle.rubyStrokeWidth,
                        rubyStyle.rubyStroke2Width
                    );
                }
            }
        }
        impl_->diagnostics.realizationPrewarmSkipped = capacitySkipped;
        impl_->diagnostics.realizationPrewarmTasks = tasks.size();
        auto control = std::make_shared<Impl::RealizationControl>();
        control->generation = impl_->realizationGeneration;
        impl_->realizationControl = control;
        impl_->realizationPrewarmComplete.store(false, std::memory_order_release);
        impl_->realizationThread = std::thread([
            this,
            control,
            tasks = std::move(tasks)
        ]() mutable {
            // Keep individual background realization chunks short enough for
            // seek/style churn while staying inside the wide-stroke A/B gate.
            // Match N3's export precision. Export uses these cached realizations
            // directly, so a coarse tolerance becomes visible as faceted curves.
            constexpr float flatteningTolerance = 0.25f;
            const auto prewarmStart = Clock::now();
            auto sliceStart = prewarmStart;
            std::uint64_t failed = 0;
            std::uint64_t fillTasks = 0;
            std::uint64_t strokeTasks = 0;
            double contextMs = 0.0;
            double waitMs = 0.0;
            double fillCreateMs = 0.0;
            double strokeCreateMs = 0.0;
            double publishMs = 0.0;
            std::vector<double> createDurations;
            createDurations.reserve(tasks.size());
            const auto isCurrent = [&]() {
                return control->generation == impl_->realizationGeneration;
            };
            const auto finish = [&]() {
                std::lock_guard<std::mutex> lock(impl_->realizationMutex);
                if (isCurrent()) {
                    impl_->diagnostics.realizationPrewarmSkipped += failed;
                    impl_->diagnostics.realizationPrewarmMs = elapsedMs(prewarmStart);
                    impl_->diagnostics.realizationPrewarmFillTasks = fillTasks;
                    impl_->diagnostics.realizationPrewarmStrokeTasks = strokeTasks;
                    impl_->diagnostics.realizationPrewarmContextMs = contextMs;
                    impl_->diagnostics.realizationPrewarmWaitMs = waitMs;
                    impl_->diagnostics.realizationPrewarmFillCreateMs = fillCreateMs;
                    impl_->diagnostics.realizationPrewarmStrokeCreateMs = strokeCreateMs;
                    impl_->diagnostics.realizationPrewarmPublishMs = publishMs;
                    if (!createDurations.empty()) {
                        std::sort(createDurations.begin(), createDurations.end());
                        const auto percentile = [&](double value) {
                            const std::size_t index = static_cast<std::size_t>(
                                std::ceil(value * static_cast<double>(
                                    createDurations.size() - 1
                                ))
                            );
                            return createDurations[index];
                        };
                        impl_->diagnostics.realizationPrewarmCreateP50Ms =
                            percentile(0.50);
                        impl_->diagnostics.realizationPrewarmCreateP95Ms =
                            percentile(0.95);
                        impl_->diagnostics.realizationPrewarmCreateMaxMs =
                            createDurations.back();
                    }
                    impl_->realizationPrewarmComplete.store(
                        true, std::memory_order_release
                    );
                }
                control->done.store(true, std::memory_order_release);
            };
            Microsoft::WRL::ComPtr<ID2D1DeviceContext> workerBaseContext;
            Microsoft::WRL::ComPtr<ID2D1DeviceContext1> workerContext;
            const auto contextStart = Clock::now();
            HRESULT contextResult = device_.d2dDevice()->CreateDeviceContext(
                D2D1_DEVICE_CONTEXT_OPTIONS_ENABLE_MULTITHREADED_OPTIMIZATIONS,
                workerBaseContext.ReleaseAndGetAddressOf()
            );
            if (SUCCEEDED(contextResult)) {
                contextResult = workerBaseContext.As(&workerContext);
            }
            contextMs = elapsedMs(contextStart);
            if (FAILED(contextResult) || !workerContext) {
                ++failed;
                finish();
                return;
            }
            const auto shouldStop = [&]() {
                return control->stop.load(std::memory_order_acquire);
            };
            const auto waitForFrameGap = [&]() {
                while (!shouldStop()) {
                    const bool active = impl_->renderActive.load(
                        std::memory_order_acquire
                    );
                    const std::int64_t idleMs = steadyNowMs()
                        - impl_->lastRenderCompletedMs.load(
                            std::memory_order_acquire
                        );
                    // Continuous 60fps playback never has a 100ms idle
                    // window.  Waiting for that long left the real project
                    // permanently on DrawGeometry.  Start at most one task
                    // in each inter-frame gap after a short foreground grace
                    // period; publishing still waits on realizationMutex, so
                    // a completed resource cannot race the active frame.
                    if (!active && idleMs >= 2) {
                        return true;
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                }
                return false;
            };
            const auto publish = [&] (
                const Impl::RealizationTask &task,
                Microsoft::WRL::ComPtr<ID2D1GeometryRealization> created
            ) {
                std::lock_guard<std::mutex> lock(impl_->realizationMutex);
                if (shouldStop() || !isCurrent()
                    || task.lineIndex >= impl_->lines.size()) {
                    return false;
                }
                Impl::CachedLine &line = impl_->lines[task.lineIndex];
                Impl::CachedChar *targetChar = nullptr;
                if (task.rubyIndex < 0) {
                    if (task.charIndex < line.chars.size()) {
                        targetChar = &line.chars[task.charIndex];
                    }
                } else if (static_cast<std::size_t>(task.rubyIndex)
                           < line.rubies.size()) {
                    Impl::CachedRuby &ruby = line.rubies[
                        static_cast<std::size_t>(task.rubyIndex)
                    ];
                    if (task.charIndex < ruby.chars.size()) {
                        targetChar = &ruby.chars[task.charIndex];
                    }
                }
                if (targetChar == nullptr) {
                    return false;
                }
                switch (task.kind) {
                case Impl::RealizationKind::Fill:
                    targetChar->fillRealization = std::move(created);
                    break;
                case Impl::RealizationKind::ProtectedStroke:
                    targetChar->protectedStrokeRealization = std::move(created);
                    break;
                case Impl::RealizationKind::Stroke:
                    targetChar->strokeRealization = std::move(created);
                    break;
                case Impl::RealizationKind::Stroke2:
                    targetChar->stroke2Realization = std::move(created);
                    break;
                }
                ++impl_->realizationCount;
                return true;
            };
            const auto yieldSlice = [&]() {
                if (elapsedMs(sliceStart) >= 50.0) {
                    std::this_thread::yield();
                    sliceStart = Clock::now();
                }
            };
            for (const Impl::RealizationTask &task : tasks) {
                const auto waitStart = Clock::now();
                if (!waitForFrameGap()) {
                    break;
                }
                waitMs += elapsedMs(waitStart);
                Microsoft::WRL::ComPtr<ID2D1GeometryRealization> created;
                HRESULT result = E_FAIL;
                const bool stroked = task.kind == Impl::RealizationKind::Stroke
                    || task.kind == Impl::RealizationKind::Stroke2;
                const auto createStart = Clock::now();
                if (stroked) {
                    result = workerContext->CreateStrokedGeometryRealization(
                        task.geometry.Get(),
                        flatteningTolerance,
                        task.strokeWidth,
                        nullptr,
                        created.ReleaseAndGetAddressOf()
                    );
                } else {
                    result = workerContext->CreateFilledGeometryRealization(
                        task.geometry.Get(),
                        flatteningTolerance,
                        created.ReleaseAndGetAddressOf()
                    );
                }
                const double createMs = elapsedMs(createStart);
                createDurations.push_back(createMs);
                if (stroked) {
                    ++strokeTasks;
                    strokeCreateMs += createMs;
                } else {
                    ++fillTasks;
                    fillCreateMs += createMs;
                }
                if (SUCCEEDED(result)) {
                    const auto publishStart = Clock::now();
                    publish(task, std::move(created));
                    publishMs += elapsedMs(publishStart);
                } else {
                    ++failed;
                }
                yieldSlice();
            }
            finish();
        });
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
    if (impl_->countersEnabled) {
        impl_->diagnostics.geometryCreatedStable += impl_->diagnostics.geometryCount;
    }
    impl_->configured = true;
}

ProbeResult Direct2DGpuBackend::renderFrame(int tMs, bool compactBands) {
    return renderFrameInternal(tMs, compactBands, true);
}

ProbeResult Direct2DGpuBackend::renderFrameInternal(
    int tMs,
    bool compactBands,
    bool readback
) {
    if (!impl_->configured) {
        throw BackendError("GPU backend is not configured");
    }
    impl_->renderActive.store(true, std::memory_order_release);
    struct RenderActivityGuard {
        Impl *impl = nullptr;
        ~RenderActivityGuard() {
            impl->lastRenderCompletedMs.store(
                steadyNowMs(), std::memory_order_release
            );
            impl->renderActive.store(false, std::memory_order_release);
        }
    } renderActivityGuard{impl_.get()};
    // The prewarmer builds realizations on a second DeviceContext and only
    // publishes completed COM resources between frames. Holding this lock for
    // the frame keeps CachedChar realization slots race-free without blocking
    // the expensive creation work itself.
    std::lock_guard<std::mutex> realizationLock(impl_->realizationMutex);
    const RenderScene &scene = impl_->scene;
    const TextStyle &baseStyle = scene.style;
    ProbeResult::FrameDiagnostics frameDiagnostics;
    frameDiagnostics.countersEnabled = impl_->countersEnabled;
    const auto count = [&](std::uint64_t &counter, std::uint64_t amount = 1) {
        if (impl_->countersEnabled) {
            counter += amount;
        }
    };
    const auto finalizeDiagnostics = [&](ProbeResult &result) {
        result.frameDiagnostics = frameDiagnostics;
        if (!impl_->countersEnabled) {
            return;
        }
        BackendDiagnostics &total = impl_->diagnostics;
        ++total.framesRendered;
        total.brushCreated += frameDiagnostics.brushCreated;
        total.geometryCreatedStable += frameDiagnostics.geometryCreatedStable;
        total.geometryCreatedDynamic += frameDiagnostics.geometryCreatedDynamic;
        total.realizationHit += frameDiagnostics.realizationHit;
        total.realizationMiss += frameDiagnostics.realizationMiss;
        total.strokeDraw += frameDiagnostics.strokeDraw;
        total.stroke2Draw += frameDiagnostics.stroke2Draw;
        total.glowSourceAreaPx += frameDiagnostics.glowSourceAreaPx;
        total.layerPush += frameDiagnostics.layerPush;
        total.animationLayoutMs += frameDiagnostics.animationLayoutMs;
        total.geometryMs += frameDiagnostics.geometryMs;
        total.strokeMs += frameDiagnostics.strokeMs;
        total.glowMs += frameDiagnostics.glowMs;
        total.gpuWaitMs += frameDiagnostics.gpuWaitMs;
        total.readbackCopyMs += frameDiagnostics.readbackCopyMs;
    };

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
    if (!impl_->frameTargetTexture || !impl_->frameTargetBitmap) {
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
    }
    if (readback && !impl_->frameStagingTexture) {
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
    auto endDrawMeasured = [&](
        const char *operation,
        double &phaseMs,
        std::uint64_t &phaseCount
    ) {
        const auto started = Clock::now();
        const HRESULT result = context->EndDraw();
        const double durationMs = elapsedMs(started);
        frameDiagnostics.endDrawWaitMs += durationMs;
        phaseMs += durationMs;
        ++frameDiagnostics.endDrawCount;
        ++phaseCount;
        checkHr(result, operation, device_);
    };
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    context->SetTarget(nullptr);

    // Glow scratch bitmaps and GaussianBlur effects live on impl_ so steady
    // state playback allocates nothing; entries rewind per line once the
    // line's composite has been flushed to the frame target.
    impl_->glowScratchInUse = 0;
    impl_->glowEffectInUse = 0;
    auto acquireGlowScratch = [&](float requestedWidth,
                                  float requestedHeight) -> ID2D1Bitmap1 * {
        const UINT32 width = impl_->glowDirtyRectEnabled
            ? static_cast<UINT32>(std::max(
                std::ceil(static_cast<double>(requestedWidth)), 1.0
            ))
            : static_cast<UINT32>(scene.width);
        const UINT32 height = impl_->glowDirtyRectEnabled
            ? static_cast<UINT32>(std::max(
                std::ceil(static_cast<double>(requestedHeight)), 1.0
            ))
            : static_cast<UINT32>(scene.height);
        const std::size_t index = impl_->glowScratchInUse++;
        if (index >= impl_->glowScratchPool.size()) {
            impl_->glowScratchPool.emplace_back();
        }
        Impl::GlowScratch &scratch = impl_->glowScratchPool[index];
        if (scratch.bitmap
            && scratch.width >= width
            && scratch.height >= height) {
            return scratch.bitmap.Get();
        }
        scratch.width = std::max(scratch.width, width);
        scratch.height = std::max(scratch.height, height);
        scratch.bitmap.Reset();
        checkHr(
            context->CreateBitmap(
                D2D1::SizeU(
                    scratch.width,
                    scratch.height
                ),
                nullptr,
                0,
                &bitmapProperties,
                scratch.bitmap.ReleaseAndGetAddressOf()
            ),
            "ID2D1DeviceContext::CreateBitmap(glow scratch)",
            device_
        );
        return scratch.bitmap.Get();
    };
    auto acquireGlowEffect = [&]() -> ID2D1Effect * {
        if (impl_->glowEffectInUse < impl_->glowEffectPool.size()) {
            return impl_->glowEffectPool[impl_->glowEffectInUse++].Get();
        }
        Microsoft::WRL::ComPtr<ID2D1Effect> effect;
        checkHr(
            context->CreateEffect(
                CLSID_D2D1GaussianBlur, effect.ReleaseAndGetAddressOf()
            ),
            "ID2D1DeviceContext::CreateEffect(GaussianBlur)",
            device_
        );
        impl_->glowEffectPool.push_back(effect);
        ++impl_->glowEffectInUse;
        return impl_->glowEffectPool.back().Get();
    };

    const bool hasViewportTransform = scene.viewportScale != 1.0f
        || scene.viewportRotation != 0.0f
        || scene.viewportOffsetX != 0.0f
        || scene.viewportOffsetY != 0.0f;
    float viewportFractionX = 0.5f;
    float viewportFractionY = 0.5f;
    if (scene.viewportAlign.find("left") != std::string::npos) {
        viewportFractionX = 0.0f;
    } else if (scene.viewportAlign.find("right") != std::string::npos) {
        viewportFractionX = 1.0f;
    }
    if (scene.viewportAlign.find("top") != std::string::npos) {
        viewportFractionY = 0.0f;
    } else if (scene.viewportAlign.find("bottom") != std::string::npos) {
        viewportFractionY = 1.0f;
    }
    const D2D1_POINT_2F viewportPivot = D2D1::Point2F(
        static_cast<float>(scene.width) * viewportFractionX,
        static_cast<float>(scene.height) * viewportFractionY
    );
    const D2D1_MATRIX_3X2_F viewportTransform = hasViewportTransform
        ? D2D1::Matrix3x2F::Translation(-viewportPivot.x, -viewportPivot.y)
            * D2D1::Matrix3x2F::Scale(scene.viewportScale, scene.viewportScale)
            * D2D1::Matrix3x2F::Rotation(scene.viewportRotation)
            * D2D1::Matrix3x2F::Translation(
                viewportPivot.x + scene.viewportOffsetX,
                viewportPivot.y + scene.viewportOffsetY
            )
        : D2D1::Matrix3x2F::Identity();
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
            const int fadeInMs = window.fadeInMs >= 0
                ? window.fadeInMs
                : line.fadeInMs;
            const int fadeOutMs = window.fadeOutMs >= 0
                ? window.fadeOutMs
                : line.fadeOutMs;
            if (fadeInMs > 0 && tMs < window.startMs + fadeInMs) {
                opacity = std::min(
                    opacity,
                    static_cast<float>(tMs - window.startMs)
                        / static_cast<float>(fadeInMs)
                );
            }
            if (fadeOutMs > 0 && tMs > window.endMs - fadeOutMs) {
                opacity = std::min(
                    opacity,
                    static_cast<float>(window.endMs - tMs)
                        / static_cast<float>(fadeOutMs)
                );
            }
            best = std::max(best, std::clamp(opacity, 0.0f, 1.0f));
        }
        return best;
    };
    struct LineAnimationState {
        float opacity = 1.0f;
        float dx = 0.0f;
        float dy = 0.0f;
    };
    auto lineAnimationAt = [&](const Impl::CachedLine &line) {
        LineAnimationState state;
        if (line.staticOverlay || line.displayWindows.empty()) {
            return state;
        }
        const DisplayWindow &window = line.displayWindows.front();
        const auto progress = [](int elapsedMs, int durationMs) {
            if (durationMs <= 0) {
                return 1.0f;
            }
            return std::clamp(
                static_cast<float>(elapsedMs) / static_cast<float>(durationMs),
                0.0f,
                1.0f
            );
        };
        if (line.entryAnimation != "none" && line.entryDurationMs > 0) {
            const float linear = progress(tMs - window.startMs, line.entryDurationMs);
            const float eased = 1.0f - (1.0f - linear) * (1.0f - linear);
            if (line.entryAnimation == "fade") {
                state.opacity *= eased;
            } else if (line.entryAnimation == "slide_in") {
                state.opacity *= eased;
                const float direction = line.lane == 0 ? -1.0f : 1.0f;
                state.dx += direction * (1.0f - eased)
                    * std::max(line.style.fontSize * 0.9f, 36.0f);
            } else if (line.entryAnimation == "rise") {
                state.opacity *= eased;
                state.dy += (1.0f - eased)
                    * std::max(line.style.fontSize * 0.35f, 18.0f);
            }
        }
        if (line.exitAnimation != "none" && line.exitDurationMs > 0) {
            const float linear = progress(window.endMs - tMs, line.exitDurationMs);
            const float eased = linear * linear;
            if (line.exitAnimation == "fade") {
                state.opacity *= eased;
            } else if (line.exitAnimation == "slide_out") {
                state.opacity *= eased;
                const float direction = line.lane == 0 ? -1.0f : 1.0f;
                state.dx += direction * (1.0f - eased)
                    * std::max(line.style.fontSize * 0.9f, 36.0f);
            } else if (line.exitAnimation == "rise") {
                state.opacity *= eased;
                state.dy -= (1.0f - eased)
                    * std::max(line.style.fontSize * 0.35f, 18.0f);
            }
        }
        state.opacity = std::clamp(state.opacity, 0.0f, 1.0f);
        return state;
    };
    std::vector<const Impl::CachedLine *> activeLines;
    for (const Impl::CachedLine &candidate : impl_->lines) {
        const bool resolvedWindowVisible = !candidate.displayWindows.empty()
            && std::any_of(
                candidate.displayWindows.begin(), candidate.displayWindows.end(),
                [&](const DisplayWindow &window) {
                    return window.endMs > window.startMs
                        && tMs >= window.startMs
                        && tMs < window.endMs;
                }
            );
        const bool visible = candidate.staticOverlay
            ? overlayOpacityAt(candidate) > 0.0f
            : (!candidate.displayWindows.empty() ? resolvedWindowVisible : (
                tMs >= candidate.startMs - std::max(baseStyle.leadInMs, 0)
                && tMs < candidate.endMs + std::max(baseStyle.tailMs, 0)
            ));
        if (visible) {
            const bool sourceLineAlreadyActive = std::any_of(
                activeLines.begin(), activeLines.end(),
                [&](const Impl::CachedLine *line) {
                    return line->sourceIndex == candidate.sourceIndex
                        && line->sourceLineIndex == candidate.sourceLineIndex;
                }
            );
            if (!sourceLineAlreadyActive) {
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
    frameDiagnostics.animationLayoutMs += elapsedMs(renderStart);
    bool renderedAnyLine = false;
    std::vector<std::pair<int, int>> readbackIntervals;
    const auto pushAxisAlignedClip = [&] (
        const D2D1_RECT_F &rect,
        D2D1_ANTIALIAS_MODE antialiasMode
    ) {
        count(frameDiagnostics.layerPush);
        context->PushAxisAlignedClip(rect, antialiasMode);
    };
    const auto drawCountedStroke = [&] (
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        float width,
        bool secondStroke
    ) {
        const auto start = Clock::now();
        context->DrawGeometry(geometry, brush, width);
        frameDiagnostics.strokeMs += elapsedMs(start);
        count(
            secondStroke
                ? frameDiagnostics.stroke2Draw
                : frameDiagnostics.strokeDraw
        );
    };
    const auto fillCountedStroke = [&] (
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        bool secondStroke
    ) {
        const auto start = Clock::now();
        context->FillGeometry(geometry, brush);
        frameDiagnostics.strokeMs += elapsedMs(start);
        count(
            secondStroke
                ? frameDiagnostics.stroke2Draw
                : frameDiagnostics.strokeDraw
        );
    };
    const auto fillWithRealization = [&] (
        ID2D1GeometryRealization *realization,
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        bool eligible
    ) {
        if (eligible && impl_->realizationActive && realization != nullptr) {
            impl_->realizationContext->DrawGeometryRealization(realization, brush);
            count(frameDiagnostics.realizationHit);
            return;
        }
        if (impl_->realizationActive && eligible) {
            count(frameDiagnostics.realizationMiss);
        }
        context->FillGeometry(geometry, brush);
    };
    const auto strokeWithRealization = [&] (
        ID2D1GeometryRealization *realization,
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        float width,
        bool secondStroke,
        bool eligible
    ) {
        const auto start = Clock::now();
        if (eligible && impl_->realizationActive && realization != nullptr) {
            impl_->realizationContext->DrawGeometryRealization(realization, brush);
            count(frameDiagnostics.realizationHit);
        } else {
            if (impl_->realizationActive && eligible) {
                count(frameDiagnostics.realizationMiss);
            }
            context->DrawGeometry(geometry, brush, width);
        }
        frameDiagnostics.strokeMs += elapsedMs(start);
        count(
            secondStroke
                ? frameDiagnostics.stroke2Draw
                : frameDiagnostics.strokeDraw
        );
    };
    const auto fillStrokeWithRealization = [&] (
        ID2D1GeometryRealization *realization,
        ID2D1Geometry *geometry,
        ID2D1Brush *brush,
        bool secondStroke,
        bool eligible
    ) {
        const auto start = Clock::now();
        fillWithRealization(realization, geometry, brush, eligible);
        frameDiagnostics.strokeMs += elapsedMs(start);
        count(
            secondStroke
                ? frameDiagnostics.stroke2Draw
                : frameDiagnostics.strokeDraw
        );
    };
    for (const Impl::CachedLine *line : activeLines) {
      if (line != nullptr && (
        !line->geometries.empty()
        || std::any_of(
            line->chars.begin(), line->chars.end(),
            [](const Impl::CachedChar &ch) { return ch.bitmapGuide.has_value(); }
        )
      )) {
        const LineAnimationState animation = lineAnimationAt(*line);
        float placementOffsetX = 0.0f;
        float placementOffsetY = 0.0f;
        for (const PlacementWindow &window : line->placementWindows) {
            if (tMs >= window.startMs && tMs < window.endMs) {
                placementOffsetX = window.offsetX;
                placementOffsetY = window.offsetY;
                break;
            }
        }
        if (animation.opacity <= 0.0f) {
            continue;
        }
        const float globalOpacity = overlayOpacityAt(*line) * animation.opacity;
        const TextStyle &style = line->style;
        // Painter restores the viewport transform before drawing the title
        // overlay, so static title lines stay in screen coordinates.
        const D2D1_MATRIX_3X2_F lineViewportTransform = line->staticOverlay
            ? D2D1::Matrix3x2F::Identity()
            : viewportTransform;
        auto withViewport = [&](const D2D1_MATRIX_3X2_F &local) {
            return local * lineViewportTransform;
        };
        const bool hasCharacterTransition = line->entryAnimation == "char_fade"
            || line->exitAnimation == "char_fade"
            || line->entryAnimation == "char_drip"
            || line->exitAnimation == "char_drip"
            || line->entryAnimation == "spin_flip"
            || line->exitAnimation == "spin_flip"
            || line->entryAnimation == "utopia"
            || line->exitAnimation == "utopia"
            || line->karaokeAnimation == "utopia";
        const bool hasUtopiaTransition = line->entryAnimation == "utopia"
            || line->exitAnimation == "utopia"
            || line->karaokeAnimation == "utopia";
        std::string activeCharacterTransition;
        int activeCharacterDirection = 0;
        if (!line->displayWindows.empty()) {
            const DisplayWindow &window = line->displayWindows.front();
            if ((line->exitAnimation == "char_fade"
                    || line->exitAnimation == "char_drip"
                    || line->exitAnimation == "spin_flip")
                && line->exitDurationMs > 0
                && tMs >= std::max(line->endMs, window.endMs - 600)) {
                activeCharacterTransition = line->exitAnimation;
                activeCharacterDirection = 1;
            } else if ((line->entryAnimation == "char_fade"
                    || line->entryAnimation == "char_drip"
                    || line->entryAnimation == "spin_flip")
                && line->entryDurationMs > 0
                && tMs <= window.startMs + 600) {
                activeCharacterTransition = line->entryAnimation;
                activeCharacterDirection = -1;
            }
        }
        // Painter owns one per-character transition context at a time.  Utopia
        // remains the steady-state path, but an active char fade/spin on the
        // opposite side must temporarily take precedence.
        const bool useUtopiaTransition = hasUtopiaTransition
            && activeCharacterTransition.empty();
        auto charFadeOpacityAt = [&](std::size_t charIndex) {
            if (!hasCharacterTransition || line->displayWindows.empty()) {
                return 1.0f;
            }
            const int count = std::max(static_cast<int>(line->chars.size()), 1);
            const int index = std::clamp(
                static_cast<int>(charIndex), 0, count - 1
            );
            const int delayStep = count <= 1 ? 0 : 350 / (count - 1);
            const DisplayWindow &window = line->displayWindows.front();
            if ((line->exitAnimation == "char_fade"
                    || line->exitAnimation == "char_drip"
                    || line->exitAnimation == "spin_flip")
                && line->exitDurationMs > 0) {
                const int exitStart = std::max(line->endMs, window.endMs - 600);
                if (tMs >= exitStart) {
                    const int endMs = window.endMs
                        - delayStep * (count - index - 1);
                    return std::clamp(
                        static_cast<float>(endMs - tMs) / 250.0f,
                        0.0f,
                        1.0f
                    );
                }
            }
            if ((line->entryAnimation == "char_fade"
                    || line->entryAnimation == "char_drip"
                    || line->entryAnimation == "spin_flip")
                && line->entryDurationMs > 0
                && tMs <= window.startMs + 600) {
                const int startMs = window.startMs + delayStep * index;
                return std::clamp(
                    static_cast<float>(tMs - startMs) / 250.0f,
                    0.0f,
                    1.0f
                );
            }
            return 1.0f;
        };
        const int spinDirection = activeCharacterTransition == "spin_flip"
            ? activeCharacterDirection
            : 0;
        const int dripDirection = activeCharacterTransition == "char_drip"
            ? -activeCharacterDirection
            : 0;
        auto spinMatrix = [&](float opacity, float centerX, float centerY) {
            const float clamped = std::clamp(opacity, 0.0f, 1.0f);
            if (spinDirection == 0 || clamped >= 1.0f) {
                return D2D1::Matrix3x2F::Identity();
            }
            constexpr float pi = 3.14159265358979323846f;
            const float angle = std::min(
                (pi * 0.5f) * (1.0f - clamped),
                pi * 89.0f / 180.0f
            );
            const float skew = static_cast<float>(spinDirection) * std::tan(angle);
            // QTransform: translate(center), shear(0, skew), scale(opacity),
            // translate(-center). Direct2D uses the same row-vector matrix
            // layout, so write the resulting coefficients explicitly.
            return D2D1::Matrix3x2F(
                clamped,
                clamped * skew,
                0.0f,
                clamped,
                centerX * (1.0f - clamped),
                centerY * (1.0f - clamped) - clamped * skew * centerX
            );
        };
        auto dripMatrix = [&](float progress, float pivotX) {
            const float clamped = std::clamp(progress, 0.0f, 1.0f);
            if (dripDirection == 0 || clamped >= 1.0f) {
                return D2D1::Matrix3x2F::Identity();
            }
            constexpr float pi = 3.14159265358979323846f;
            const float angle = std::min(
                (pi * 0.5f) * (1.0f - clamped),
                pi * 89.0f / 180.0f
            );
            const float skew = static_cast<float>(dripDirection) * std::tan(angle);
            // N3 pivots CharDrip at (drawWidth, 0) for intro and
            // (drawWidth, -height) for outro.  A vertical shear depends only
            // on pivot X, so both reduce to the glyph's right edge here.
            return D2D1::Matrix3x2F(
                1.0f, skew, 0.0f, 1.0f, 0.0f, -skew * pivotX
            );
        };
        struct CharacterAnimationState {
            float opacity = 1.0f;
            D2D1_MATRIX_3X2_F matrix = D2D1::Matrix3x2F::Identity();
            bool transformed = false;
            bool utopiaExit = false;
        };
        auto utopiaFollowingDoneAt = [&](std::size_t charIndex) {
            const int count = static_cast<int>(line->chars.size());
            int index = std::clamp(static_cast<int>(charIndex), 0, count - 1);
            for (const Impl::CachedRuby &ruby : line->rubies) {
                if (ruby.lastCharIndex > ruby.firstCharIndex
                    && index >= ruby.firstCharIndex
                    && index <= ruby.lastCharIndex) {
                    index = std::clamp(ruby.lastCharIndex, 0, count - 1);
                    break;
                }
            }
            const int currentEnd = line->chars[static_cast<std::size_t>(index)].endMs;
            for (int next = index + 1; next < count; ++next) {
                const Impl::CachedChar &candidate = line->chars[
                    static_cast<std::size_t>(next)
                ];
                if (candidate.geometry != nullptr || candidate.bitmapGuide.has_value()) {
                    return currentEnd <= candidate.endMs
                        ? candidate.endMs
                        : currentEnd;
                }
            }
            return currentEnd + std::max(line->style.tailMs - 750, 0);
        };
        auto utopiaMatrix = [&](float dxValue, float dyValue, float rotation,
                                float scaleX, float scaleY,
                                float left, float baseline,
                                float centerX, float centerY) {
            // QTransform mutators pre-multiply in row-vector space. Reverse
            // the call order from _character_transform's scale-origin branch.
            return D2D1::Matrix3x2F::Translation(-centerX, -centerY)
                * D2D1::Matrix3x2F::Rotation(rotation)
                * D2D1::Matrix3x2F::Translation(
                    centerX - left, centerY - baseline
                )
                * D2D1::Matrix3x2F::Scale(scaleX, scaleY)
                * D2D1::Matrix3x2F::Translation(
                    left + dxValue, baseline + dyValue
                );
        };
        const auto wipeStartMs = [](const Impl::CachedChar &ch) {
            return ch.wipePoints.empty() ? ch.startMs : ch.wipePoints.front().timeMs;
        };
        const auto wipeEndMs = [](const Impl::CachedChar &ch) {
            return ch.wipePoints.empty() ? ch.endMs : ch.wipePoints.back().timeMs;
        };
        auto characterAnimationAt = [&](std::size_t charIndex) {
            CharacterAnimationState state;
            if (charIndex >= line->chars.size()) {
                state.opacity = 0.0f;
                return state;
            }
            const Impl::CachedChar &ch = line->chars[charIndex];
            if (!useUtopiaTransition) {
                const float progress = charFadeOpacityAt(charIndex);
                state.opacity = dripDirection != 0
                    ? (progress > 0.0f ? 1.0f : 0.0f)
                    : progress;
                if (dripDirection != 0) {
                    state.matrix = dripMatrix(
                        progress, ch.layoutRight
                    );
                    state.transformed = progress > 0.0f && progress < 1.0f;
                } else {
                    state.matrix = spinMatrix(progress, ch.pivotX, ch.pivotY);
                    state.transformed = spinDirection != 0 && progress < 1.0f;
                }
                return state;
            }
            if (line->displayWindows.empty()) {
                return state;
            }
            constexpr float pi = 3.14159265358979323846f;
            const DisplayWindow &window = line->displayWindows.front();
            float dxValue = 0.0f;
            float dyValue = 0.0f;
            float rotation = 0.0f;
            float scaleX = 1.0f;
            float scaleY = 1.0f;
            if (line->entryAnimation == "utopia"
                && tMs <= window.startMs + 700) {
                const int count = std::max(static_cast<int>(line->chars.size()), 1);
                const int delayStep = count <= 1 ? 0 : 200 / (count - 1);
                const int elapsed = tMs - window.startMs
                    - delayStep * static_cast<int>(charIndex);
                if (elapsed < 0) {
                    state.opacity = 0.0f;
                    scaleX = scaleY = 0.0f;
                } else {
                    state.opacity = std::min(
                        static_cast<float>(elapsed) / 400.0f, 1.0f
                    );
                    if (elapsed < 400) {
                        scaleX = scaleY = 1.3f
                            * static_cast<float>(elapsed) / 400.0f;
                    } else if (elapsed < 500) {
                        const float remaining = static_cast<float>(500 - elapsed);
                        scaleX = scaleY = 1.0f + 0.3f * remaining / 100.0f;
                    }
                }
            } else if (line->exitAnimation == "utopia"
                && tMs > utopiaFollowingDoneAt(charIndex)) {
                const float local = std::clamp(
                    static_cast<float>(tMs - utopiaFollowingDoneAt(charIndex))
                        / 750.0f,
                    0.0f,
                    1.0f
                );
                state.opacity = 1.0f - local;
                state.utopiaExit = true;
                const float shrink = 1.0f - local;
                const float amplitude = static_cast<float>(scene.height) / 15.0f;
                const float xTravel = local <= 0.5f
                    ? std::sin(pi * local) * amplitude
                    : amplitude + std::sin((local - 0.5f) * pi) * amplitude;
                const float yTravel = std::sin(pi * local * 0.5f) * amplitude;
                dxValue = -xTravel;
                dyValue = yTravel;
                rotation = -180.0f * local;
                scaleX = shrink * std::cos(pi * local);
                scaleY = shrink;
            } else if (line->karaokeAnimation == "utopia"
                && tMs > wipeStartMs(ch) && tMs < wipeEndMs(ch)
                && wipeStartMs(ch) != wipeEndMs(ch)) {
                const int overMs = std::min(
                    static_cast<int>((wipeEndMs(ch) - wipeStartMs(ch)) * 0.25f), 100
                );
                if (overMs > 0) {
                    const int peakMs = wipeStartMs(ch) + overMs;
                    const float progress = tMs <= peakMs
                        ? static_cast<float>(tMs - wipeStartMs(ch))
                            / static_cast<float>(overMs)
                        : static_cast<float>(wipeEndMs(ch) - tMs)
                            / static_cast<float>(std::max(wipeEndMs(ch) - peakMs, 1));
                    scaleX = scaleY = 1.0f
                        + 0.15f * std::clamp(progress, 0.0f, 1.0f);
                }
            }
            if (state.opacity <= 0.0f) {
                return state;
            }
            state.matrix = utopiaMatrix(
                dxValue, dyValue, rotation, scaleX, scaleY,
                ch.layoutLeft, 0.0f, ch.pivotX, ch.pivotY
            );
            state.transformed = dxValue != 0.0f || dyValue != 0.0f
                || rotation != 0.0f || scaleX != 1.0f || scaleY != 1.0f;
            return state;
        };
        auto characterOpacityAt = [&](std::size_t charIndex) {
            return characterAnimationAt(charIndex).opacity;
        };
        auto rubyUnitAnimationAt = [&](const Impl::CachedRuby &ruby,
                                       std::size_t unitIndex) {
            CharacterAnimationState state;
            if (!useUtopiaTransition) {
                const std::size_t transitionIndex = static_cast<std::size_t>(
                    std::max(ruby.transitionCharIndex, 0)
                );
                const float progress = charFadeOpacityAt(transitionIndex);
                state.opacity = dripDirection != 0
                    ? (progress > 0.0f ? 1.0f : 0.0f)
                    : progress;
                if (dripDirection != 0) {
                    const float pivotX = unitIndex < ruby.chars.size()
                        ? ruby.chars[unitIndex].layoutRight
                        : ruby.bounds.right;
                    state.matrix = dripMatrix(progress, pivotX);
                    state.transformed = progress > 0.0f && progress < 1.0f;
                } else {
                    // N3 spins a ruby run as one visual unit. Keep that
                    // established pivot while sharing the same animation
                    // classification as the main glyphs.
                    state.matrix = spinMatrix(progress, ruby.pivotX, ruby.pivotY);
                    state.transformed = spinDirection != 0 && progress < 1.0f;
                }
                return state;
            }
            if (unitIndex >= ruby.chars.size() || line->displayWindows.empty()) {
                state.opacity = characterOpacityAt(static_cast<std::size_t>(
                    std::max(ruby.transitionCharIndex, 0)
                ));
                return state;
            }
            constexpr float pi = 3.14159265358979323846f;
            const Impl::CachedChar &unit = ruby.chars[unitIndex];
            const DisplayWindow &window = line->displayWindows.front();
            float dxValue = 0.0f;
            float dyValue = 0.0f;
            float rotation = 0.0f;
            float scaleX = 1.0f;
            float scaleY = 1.0f;
            if (line->entryAnimation == "utopia"
                && tMs <= window.startMs + 700) {
                const int count = std::max(static_cast<int>(line->chars.size()), 1);
                const int delayStep = count <= 1 ? 0 : 200 / (count - 1);
                const int staggerIndex = std::clamp(
                    ruby.firstCharIndex, 0, count - 1
                );
                const int elapsed = tMs - window.startMs
                    - delayStep * staggerIndex;
                if (elapsed < 0) {
                    state.opacity = 0.0f;
                    scaleX = scaleY = 0.0f;
                } else {
                    state.opacity = std::min(
                        static_cast<float>(elapsed) / 400.0f, 1.0f
                    );
                    if (elapsed < 400) {
                        scaleX = scaleY = 1.3f
                            * static_cast<float>(elapsed) / 400.0f;
                    } else if (elapsed < 500) {
                        scaleX = scaleY = 1.0f
                            + 0.3f * static_cast<float>(500 - elapsed) / 100.0f;
                    }
                }
            } else if (line->exitAnimation == "utopia"
                && tMs > utopiaFollowingDoneAt(static_cast<std::size_t>(std::max(
                    ruby.lastCharIndex, 0
                )))) {
                const int doneMs = utopiaFollowingDoneAt(
                    static_cast<std::size_t>(std::max(ruby.lastCharIndex, 0))
                );
                const float local = std::clamp(
                    static_cast<float>(tMs - doneMs) / 750.0f, 0.0f, 1.0f
                );
                state.opacity = 1.0f - local;
                state.utopiaExit = true;
                const float shrink = 1.0f - local;
                const float amplitude = static_cast<float>(scene.height) / 15.0f;
                const float xTravel = local <= 0.5f
                    ? std::sin(pi * local) * amplitude
                    : amplitude + std::sin((local - 0.5f) * pi) * amplitude;
                dxValue = -xTravel;
                dyValue = std::sin(pi * local * 0.5f) * amplitude;
                rotation = -180.0f * local;
                scaleX = shrink * std::cos(pi * local);
                scaleY = shrink;
            } else if (line->karaokeAnimation == "utopia"
                && tMs > unit.startMs && tMs < unit.endMs
                && unit.startMs != unit.endMs) {
                const int overMs = std::min(
                    static_cast<int>((unit.endMs - unit.startMs) * 0.25f), 100
                );
                if (overMs > 0) {
                    const int peakMs = unit.startMs + overMs;
                    const float progress = tMs <= peakMs
                        ? static_cast<float>(tMs - unit.startMs)
                            / static_cast<float>(overMs)
                        : static_cast<float>(unit.endMs - tMs)
                            / static_cast<float>(std::max(unit.endMs - peakMs, 1));
                    scaleX = scaleY = 1.0f
                        + 0.15f * std::clamp(progress, 0.0f, 1.0f);
                }
            }
            if (state.opacity <= 0.0f) {
                return state;
            }
            state.matrix = utopiaMatrix(
                dxValue, dyValue, rotation, scaleX, scaleY,
                unit.layoutLeft, ruby.baselineOffset,
                unit.pivotX, unit.pivotY
            );
            state.transformed = dxValue != 0.0f || dyValue != 0.0f
                || rotation != 0.0f || scaleX != 1.0f || scaleY != 1.0f;
            return state;
        };
        auto rubyFadeOpacityAt = [&](const Impl::CachedRuby &ruby) {
            return characterOpacityAt(static_cast<std::size_t>(std::max(
                ruby.transitionCharIndex, 0
            )));
        };
        auto rubyUnitOpacityAt = [&](const Impl::CachedRuby &ruby,
                                     std::size_t unitIndex) {
            return rubyUnitAnimationAt(ruby, unitIndex).opacity;
        };
        if (hasCharacterTransition) {
            float maxOpacity = 0.0f;
            for (std::size_t index = 0; index < line->chars.size(); ++index) {
                maxOpacity = std::max(maxOpacity, characterOpacityAt(index));
            }
            if (maxOpacity <= 0.0f) {
                continue;
            }
        }
        const auto geometryStart = Clock::now();
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> frameCharGeometries(
            line->chars.size()
        );
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> frameProtectedGeometries(
            line->chars.size()
        );
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> frameStrokeGeometries(
            line->chars.size()
        );
        std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>> frameStroke2Geometries(
            line->chars.size()
        );
        for (std::size_t index = 0; index < line->chars.size(); ++index) {
            const Impl::CachedChar &ch = line->chars[index];
            const CharacterAnimationState charAnimation = characterAnimationAt(index);
            const float opacity = charAnimation.opacity;
            if (!ch.geometry || opacity <= 0.0f) {
                continue;
            }
            if (!charAnimation.transformed) {
                frameCharGeometries[index] = ch.geometry;
                frameProtectedGeometries[index] = ch.protectedStrokeGeometry;
                frameStrokeGeometries[index] = ch.strokeGeometry;
                frameStroke2Geometries[index] = ch.stroke2Geometry;
                continue;
            }
            const D2D1_MATRIX_3X2_F matrix = charAnimation.matrix;
            Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
            checkHr(
                device_.d2dFactory()->CreateTransformedGeometry(
                    ch.geometry.Get(), &matrix,
                    transformed.ReleaseAndGetAddressOf()
                ),
                "ID2D1Factory::CreateTransformedGeometry(spin character)",
                device_
            );
            count(frameDiagnostics.geometryCreatedDynamic);
            frameCharGeometries[index] = transformed;
            if (ch.protectedStrokeGeometry) {
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry>
                    transformedProtected;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        ch.protectedStrokeGeometry.Get(), &matrix,
                        transformedProtected.ReleaseAndGetAddressOf()
                    ),
                    "ID2D1Factory::CreateTransformedGeometry(spin protected stroke)",
                    device_
                );
                count(frameDiagnostics.geometryCreatedDynamic);
                frameProtectedGeometries[index] = transformedProtected;
            }
            auto transformStroke = [&](ID2D1Geometry *source,
                                       Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                                       const char *operation) {
                if (source == nullptr) {
                    return;
                }
                Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformedStroke;
                checkHr(
                    device_.d2dFactory()->CreateTransformedGeometry(
                        source, &matrix,
                        transformedStroke.ReleaseAndGetAddressOf()
                    ),
                    operation,
                    device_
                );
                count(frameDiagnostics.geometryCreatedDynamic);
                target = transformedStroke;
            };
            if (!impl_->dynamicDirectStrokeEnabled) {
                transformStroke(
                    ch.strokeGeometry.Get(), frameStrokeGeometries[index],
                    "ID2D1Factory::CreateTransformedGeometry(dynamic stroke)"
                );
                transformStroke(
                    ch.stroke2Geometry.Get(), frameStroke2Geometries[index],
                    "ID2D1Factory::CreateTransformedGeometry(dynamic stroke2)"
                );
            }
        }
        std::vector<std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>>>
            frameRubyGeometries(line->rubies.size());
        std::vector<std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>>>
            frameRubyProtectedGeometries(line->rubies.size());
        std::vector<std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>>>
            frameRubyStrokeGeometries(line->rubies.size());
        std::vector<std::vector<Microsoft::WRL::ComPtr<ID2D1Geometry>>>
            frameRubyStroke2Geometries(line->rubies.size());
        for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
            float maxRubyOpacity = 0.0f;
            for (std::size_t index = 0; index < ruby.geometries.size(); ++index) {
                maxRubyOpacity = std::max(
                    maxRubyOpacity,
                    rubyUnitAnimationAt(ruby, index).opacity
                );
            }
            if (maxRubyOpacity <= 0.0f) {
                continue;
            }
            frameRubyGeometries[rubyIndex].resize(ruby.geometries.size());
            frameRubyProtectedGeometries[rubyIndex].resize(
                ruby.protectedStrokeGeometries.size()
            );
            frameRubyStrokeGeometries[rubyIndex].resize(ruby.strokeGeometries.size());
            frameRubyStroke2Geometries[rubyIndex].resize(ruby.stroke2Geometries.size());
            for (std::size_t index = 0; index < ruby.geometries.size(); ++index) {
                const CharacterAnimationState rubyAnimation =
                    rubyUnitAnimationAt(ruby, index);
                if (rubyAnimation.opacity <= 0.0f) {
                    continue;
                }
                const D2D1_MATRIX_3X2_F matrix = rubyAnimation.matrix;
                if (!rubyAnimation.transformed) {
                    frameRubyGeometries[rubyIndex][index] = ruby.geometries[index];
                    if (index < ruby.protectedStrokeGeometries.size()) {
                        frameRubyProtectedGeometries[rubyIndex][index]
                            = ruby.protectedStrokeGeometries[index];
                    }
                    if (index < ruby.strokeGeometries.size()) {
                        frameRubyStrokeGeometries[rubyIndex][index]
                            = ruby.strokeGeometries[index];
                    }
                    if (index < ruby.stroke2Geometries.size()) {
                        frameRubyStroke2Geometries[rubyIndex][index]
                            = ruby.stroke2Geometries[index];
                    }
                } else {
                    Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformed;
                    checkHr(
                        device_.d2dFactory()->CreateTransformedGeometry(
                            ruby.geometries[index].Get(), &matrix,
                            transformed.ReleaseAndGetAddressOf()
                        ),
                        "ID2D1Factory::CreateTransformedGeometry(spin ruby)",
                        device_
                    );
                    count(frameDiagnostics.geometryCreatedDynamic);
                    frameRubyGeometries[rubyIndex][index] = transformed;
                    if (index < ruby.protectedStrokeGeometries.size()
                        && ruby.protectedStrokeGeometries[index]) {
                        Microsoft::WRL::ComPtr<ID2D1TransformedGeometry>
                            transformedProtected;
                        checkHr(
                            device_.d2dFactory()->CreateTransformedGeometry(
                                ruby.protectedStrokeGeometries[index].Get(), &matrix,
                                transformedProtected.ReleaseAndGetAddressOf()
                            ),
                            "ID2D1Factory::CreateTransformedGeometry(spin ruby protected stroke)",
                            device_
                        );
                        count(frameDiagnostics.geometryCreatedDynamic);
                        frameRubyProtectedGeometries[rubyIndex][index]
                            = transformedProtected;
                    }
                    auto transformRubyStroke = [&] (
                        ID2D1Geometry *source,
                        Microsoft::WRL::ComPtr<ID2D1Geometry> &target,
                        const char *operation
                    ) {
                        if (source == nullptr) {
                            return;
                        }
                        Microsoft::WRL::ComPtr<ID2D1TransformedGeometry> transformedStroke;
                        checkHr(
                            device_.d2dFactory()->CreateTransformedGeometry(
                                source, &matrix,
                                transformedStroke.ReleaseAndGetAddressOf()
                            ),
                            operation,
                            device_
                        );
                        count(frameDiagnostics.geometryCreatedDynamic);
                        target = transformedStroke;
                    };
                    if (!impl_->dynamicDirectStrokeEnabled
                        && index < ruby.strokeGeometries.size()) {
                        transformRubyStroke(
                            ruby.strokeGeometries[index].Get(),
                            frameRubyStrokeGeometries[rubyIndex][index],
                            "ID2D1Factory::CreateTransformedGeometry(spin ruby stroke)"
                        );
                    }
                    if (!impl_->dynamicDirectStrokeEnabled
                        && index < ruby.stroke2Geometries.size()) {
                        transformRubyStroke(
                            ruby.stroke2Geometries[index].Get(),
                            frameRubyStroke2Geometries[rubyIndex][index],
                            "ID2D1Factory::CreateTransformedGeometry(spin ruby stroke2)"
                        );
                    }
                }
            }
        }
        frameDiagnostics.geometryMs += elapsedMs(geometryStart);
        auto charGeometryAt = [&](std::size_t index) -> ID2D1Geometry * {
            return index < frameCharGeometries.size()
                ? frameCharGeometries[index].Get()
                : nullptr;
        };
        auto protectedGeometryAt = [&](std::size_t index) -> ID2D1Geometry * {
            return index < frameProtectedGeometries.size()
                ? frameProtectedGeometries[index].Get()
                : nullptr;
        };
        auto strokeGeometryAt = [&](std::size_t index) -> ID2D1Geometry * {
            return index < frameStrokeGeometries.size()
                ? frameStrokeGeometries[index].Get()
                : nullptr;
        };
        auto stroke2GeometryAt = [&](std::size_t index) -> ID2D1Geometry * {
            return index < frameStroke2Geometries.size()
                ? frameStroke2Geometries[index].Get()
                : nullptr;
        };
        auto rubyGeometryAt = [&](
            std::size_t rubyIndex, std::size_t geometryIndex
        ) -> ID2D1Geometry * {
            return rubyIndex < frameRubyGeometries.size()
                && geometryIndex < frameRubyGeometries[rubyIndex].size()
                ? frameRubyGeometries[rubyIndex][geometryIndex].Get()
                : nullptr;
        };
        auto rubyProtectedGeometryAt = [&](
            std::size_t rubyIndex, std::size_t geometryIndex
        ) -> ID2D1Geometry * {
            return rubyIndex < frameRubyProtectedGeometries.size()
                && geometryIndex < frameRubyProtectedGeometries[rubyIndex].size()
                ? frameRubyProtectedGeometries[rubyIndex][geometryIndex].Get()
                : nullptr;
        };
        auto rubyStrokeGeometryAt = [&] (
            std::size_t rubyIndex, std::size_t geometryIndex
        ) -> ID2D1Geometry * {
            return rubyIndex < frameRubyStrokeGeometries.size()
                && geometryIndex < frameRubyStrokeGeometries[rubyIndex].size()
                ? frameRubyStrokeGeometries[rubyIndex][geometryIndex].Get()
                : nullptr;
        };
        auto rubyStroke2GeometryAt = [&] (
            std::size_t rubyIndex, std::size_t geometryIndex
        ) -> ID2D1Geometry * {
            return rubyIndex < frameRubyStroke2Geometries.size()
                && geometryIndex < frameRubyStroke2Geometries[rubyIndex].size()
                ? frameRubyStroke2Geometries[rubyIndex][geometryIndex].Get()
                : nullptr;
        };
        // A character/ruby unit counts as "transformed" only on frames where
        // its animation matrix is non-identity.
        auto charTransformedAt = [&](std::size_t index) {
            return characterAnimationAt(index).transformed;
        };
        // N3's Utopia override substitutes the transformed geometry while
        // DrawOneLineDecorBlur is building the shared work bitmap, then blurs
        // that combined line once. Keep the dedicated blur-then-transform
        // layers for the other character animations, but never split Utopia
        // between two glow representations.
        auto charUsesGroupedGlowAt = [&](std::size_t index) {
            return useUtopiaTransition || !charTransformedAt(index);
        };
        auto rubyUnitTransformed = [&](const Impl::CachedRuby &ruby,
                                       std::size_t unitIndex) {
            return rubyUnitAnimationAt(ruby, unitIndex).transformed;
        };
        auto rubyUnitUsesGroupedGlowAt = [&](const Impl::CachedRuby &ruby,
                                             std::size_t unitIndex) {
            return useUtopiaTransition
                || !rubyUnitTransformed(ruby, unitIndex);
        };
        const auto expandedRect = [](const D2D1_RECT_F &rect, float amount) {
            return D2D1::RectF(
                rect.left - amount, rect.top - amount,
                rect.right + amount, rect.bottom + amount
            );
        };
        const auto unionRect = [](const D2D1_RECT_F &a, const D2D1_RECT_F &b) {
            return D2D1::RectF(
                std::min(a.left, b.left), std::min(a.top, b.top),
                std::max(a.right, b.right), std::max(a.bottom, b.bottom)
            );
        };
        int displayEndMs = line->endMs + std::max(style.tailMs, 0);
        for (const DisplayWindow &window : line->displayWindows) {
            if (tMs >= window.startMs && tMs < window.endMs) {
                displayEndMs = window.endMs;
                break;
            }
        }
        const VolumeSignalState signalState = volumeSignalState(
            line->startMs, style, tMs, displayEndMs
        );
        const VolumeSignalGeometry signalGeometry = volumeSignalGeometry(style);
        const ShapeSignalState shapeState = shapeSignalState(
            line->startMs, style, tMs, displayEndMs
        );
        const ShapeSignalGeometry shapeGeometry = shapeSignalGeometry(style);
        const int signalActiveDuration = std::max(
            style.signalsDurationMs - std::max(style.litWaitingTimeMs, 0), 0
        );
        const int signalEndMs = line->startMs + style.litTimeOffsetMs;
        const bool signalLayoutActive = style.litEnabled
            && !style.vertical
            && signalActiveDuration > 0
            && tMs >= signalEndMs - signalActiveDuration
            && tMs < displayEndMs;
        float lyricLeft = line->bounds.left;
        float lyricRight = line->bounds.right;
        const bool n3Layout = style.layoutSemantics == "n3_1074";
        if (n3Layout && !style.vertical) {
            lyricLeft = line->fillBounds.left;
            lyricRight = line->fillBounds.right;
            // N3 anchors the complete line box, including a reading that
            // overhangs its base-text target.  Painter folds the same ruby
            // layout boxes into _line_total_width even for centered rows.
            for (const Impl::CachedRuby &ruby : line->rubies) {
                for (const Impl::CachedChar &unit : ruby.chars) {
                    lyricLeft = std::min(lyricLeft, unit.layoutLeft);
                    lyricRight = std::max(lyricRight, unit.layoutRight);
                }
            }
        }
        const bool hasBitmapGuide = std::any_of(
            line->chars.begin(), line->chars.end(),
            [](const Impl::CachedChar &ch) { return ch.bitmapGuide.has_value(); }
        );
        if (line->guideAnchorLeft.has_value()
            && line->guideAnchorRight.has_value()
            && !style.vertical
            && !hasBitmapGuide) {
            lyricLeft = *line->guideAnchorLeft;
            lyricRight = *line->guideAnchorRight;
        }
        // The Painter no longer pads the horizontal line box with the stroke
        // extent under legacy semantics either -- both now anchor N3's logical
        // DrawLineLeft/Right, so mixed-role lines keep the glyph box as well.
        float unionLeft = lyricLeft;
        float unionRight = lyricRight;
        if (signalLayoutActive && style.litStyle == "volume") {
            // Painter aligns the offset-free union of the text and signal
            // module throughout the guide window, including flash-off frames.
            // The volume offset moves only the bars afterwards.
            unionLeft = std::min(unionLeft, -signalGeometry.groupWidth);
            unionRight = std::max(unionRight, 0.0f);
        } else if (signalLayoutActive) {
            unionLeft = std::min(unionLeft, 0.0f);
            unionRight = std::max(unionRight, shapeGeometry.groupWidth);
        }
        auto alignedDx = [&](float left, float right) {
            const float inkWidth = right - left;
            float value = (static_cast<float>(scene.width) - inkWidth) * 0.5f
                - left + style.centerOffsetX;
            if (style.alignment == "left") {
                value = style.horizontalMargin - left;
            } else if (style.alignment == "right") {
                value = static_cast<float>(scene.width) - style.horizontalMargin - right;
            }
            if (!style.vertical) {
                value += style.layoutOffsetX;
            }
            return value + animation.dx;
        };
        float dx = alignedDx(
            line->hasInlineStyles ? lyricLeft : unionLeft,
            line->hasInlineStyles ? lyricRight : unionRight
        ) + placementOffsetX;
        float signalDx = alignedDx(unionLeft, unionRight) + placementOffsetX;
        // N3 applies SmartHorizon after ordinary lane alignment.  Page ids
        // come from the same assign_lanes result used by the Painter oracle,
        // so invisible siblings still contribute to page-wide width maxima.
        // SmartHorizon is not part of the N3-only layout semantics: the layout
        // tab offers it for every project, so legacy styles take it as well.
        if (!style.vertical
            && style.dualLineLayout
            && style.smartHorizontal != "none"
            && style.alignment != "center"
            && line->pageIndex >= 0) {
            const auto layoutWidth = [](const Impl::CachedLine &candidate) {
                float left = candidate.fillBounds.left;
                float right = candidate.fillBounds.right;
                for (const Impl::CachedRuby &ruby : candidate.rubies) {
                    for (const Impl::CachedChar &unit : ruby.chars) {
                        left = std::min(left, unit.layoutLeft);
                        right = std::max(right, unit.layoutRight);
                    }
                }
                return std::max(right - left, 1.0f);
            };
            const auto firstCharFontSize = [&](const Impl::CachedLine &candidate) {
                if (!candidate.chars.empty()) {
                    const int styleIndex = candidate.chars.front().styleIndex;
                    if (styleIndex >= 0
                        && styleIndex < static_cast<int>(scene.charStyles.size())) {
                        return std::max(
                            scene.charStyles[static_cast<std::size_t>(styleIndex)].fontSize,
                            1.0f
                        );
                    }
                }
                return std::max(candidate.style.fontSize, 1.0f);
            };
            const float ownWidth = layoutWidth(*line);
            const float ownFontSize = firstCharFontSize(*line);
            float smartDx = 0.0f;
            if (style.smartHorizontal == "center_position") {
                const float threshold = std::floor(
                    static_cast<float>(scene.width) * 0.5f
                    + ownFontSize * 0.5f
                    - ownWidth
                );
                if (threshold > style.horizontalMargin) {
                    if (style.alignment == "right") {
                        const float currentLeft = static_cast<float>(scene.width)
                            - style.horizontalMargin - ownWidth;
                        smartDx = std::floor(
                            static_cast<float>(scene.width) * 0.5f
                            - ownFontSize * 0.5f
                        ) - currentLeft;
                    } else {
                        smartDx = threshold - style.horizontalMargin;
                    }
                }
            } else if (style.smartHorizontal == "equal_margins") {
                float maxLeft = 0.0f;
                float maxCenter = 0.0f;
                float maxRight = 0.0f;
                float pageHeadFontSize = style.fontSize;
                bool foundPageHead = false;
                for (const Impl::CachedLine &candidate : impl_->lines) {
                    if (candidate.sourceIndex != line->sourceIndex
                        || candidate.pageIndex != line->pageIndex) {
                        continue;
                    }
                    if (!foundPageHead) {
                        pageHeadFontSize = firstCharFontSize(candidate);
                        foundPageHead = true;
                    }
                    const float width = layoutWidth(candidate);
                    if (candidate.style.alignment == "right") {
                        maxRight = std::max(maxRight, width);
                    } else if (candidate.style.alignment == "center") {
                        maxCenter = std::max(maxCenter, width);
                    } else {
                        maxLeft = std::max(maxLeft, width);
                    }
                }
                if (maxLeft > 0.0f && maxRight > 0.0f) {
                    const float slack = static_cast<float>(scene.width)
                        - style.horizontalMargin * 2.0f
                        - maxLeft - maxCenter - maxRight
                        + pageHeadFontSize;
                    if (slack > 0.0f) {
                        const float halfSlack = std::floor(slack * 0.5f);
                        smartDx = style.alignment == "right"
                            ? -halfSlack
                            : halfSlack;
                    }
                }
            }
            dx += smartDx;
            signalDx += smartDx;
        }
        // The title is a standalone block with no lane grid to hold steady, so
        // its box comes from the glyphs it actually draws.  Sizing it from the
        // line style would let the base title scheme's font size move a title
        // that is entirely rendered with some other role scheme.  Mirrors
        // Painter's _layout_title_overlay.
        const bool ownCharBox = line->staticOverlay && line->hasN3CharBox;
        const float visualPad = n3Layout
            ? 0.0f
            : (line->hasInlineStyles
            ? line->maxVisualPad
            : std::ceil(
                (std::max(style.strokeWidth, 0.0f)
                    + std::max(style.stroke2Width, 0.0f)) * 0.5f
            ));
        // Lyric lanes are a page-level grid: Painter derives them from the style
        // alone (_fixed_line_geometry), never from what a line happens to
        // contain.  legacyLaneHeight/Descent carry exactly that style-level box,
        // so use it for every legacy lyric line, not only the ones carrying an
        // inline role scheme.  Deriving the grid from the line's own glyph
        // metrics let a single half-width space -- Latin text, therefore
        // measured with the Latin face -- move the whole upper row by the two
        // faces' ascent gap.  The title keeps its glyph-derived box per above.
        const float mainHeight = n3Layout
            ? (ownCharBox
                ? line->n3CharAscent + line->n3CharDescent
                : line->n3DrawHeight)
            : line->staticOverlay
                ? (line->ascent > 0.0f ? line->ascent : -line->bounds.top)
                    + (line->descent > 0.0f ? line->descent : line->bounds.bottom)
                    + visualPad * 2.0f
                : line->legacyLaneHeight;
        const float descent = n3Layout
            ? (ownCharBox ? line->n3CharDescent : line->n3Descent)
            : line->staticOverlay
                ? (line->descent > 0.0f ? line->descent : line->bounds.bottom)
                    + visualPad
                : line->legacyLaneDescent;
        const float ascent = mainHeight - descent;
        const int lanes = style.dualLineLayout ? std::max(style.laneCount, 1) : 1;
        const float rubyExtra = n3Layout || line->rubies.empty()
            ? 0.0f
            : std::max(
                style.rubyGap + style.rubyFontSize
                    + std::max(style.rubyStrokeWidth, 0.0f),
                0.0f
            );
        const float step = mainHeight + style.lineGap;
        float firstBaseline = static_cast<float>(scene.height) - style.bottomMargin
            - descent - step * static_cast<float>(lanes - 1);
        if (style.verticalPosition == "top") {
            firstBaseline = style.bottomMargin + rubyExtra + ascent;
        } else if (style.verticalPosition == "center") {
            const float totalHeight = mainHeight * static_cast<float>(lanes)
                + style.lineGap * static_cast<float>(lanes - 1);
            firstBaseline = (static_cast<float>(scene.height) - totalHeight) * 0.5f
                + ascent;
            if (lanes == 1 && !n3Layout && !line->hasInlineLaneGeometryOverride) {
                if (line->rubies.empty()) {
                    firstBaseline = (static_cast<float>(scene.height)
                        - (line->bounds.bottom - line->bounds.top)) * 0.5f
                        - line->bounds.top;
                } else {
                    const float blockHeight = mainHeight + rubyExtra;
                    firstBaseline = (static_cast<float>(scene.height) - blockHeight) * 0.5f
                        + rubyExtra + ascent;
                }
            }
        }
        if (style.verticalPosition == "center") {
            firstBaseline += style.centerOffsetY;
        }
        float dy = firstBaseline + step * static_cast<float>(line->lane)
            + animation.dy;
        if (style.vertical) {
            const float cellWidth = std::max(
                line->fillBounds.right - line->fillBounds.left, 1.0f
            );
            const float blockHeight = std::max(
                line->fillBounds.bottom - line->fillBounds.top, 1.0f
            );
            const float verticalRubyAllowance = line->verticalRubyAllowance;
            dx = static_cast<float>(scene.width) - style.bottomMargin
                - verticalRubyAllowance - cellWidth * 0.5f
                - static_cast<float>(line->lane)
                    * (cellWidth + verticalRubyAllowance + style.lineGap)
                + animation.dx;
            if (style.verticalPosition == "top") {
                dy = style.bottomMargin;
            } else if (style.verticalPosition == "center") {
                dy = std::max(
                    (static_cast<float>(scene.height) - blockHeight) * 0.5f,
                    0.0f
                );
            } else {
                dy = static_cast<float>(scene.height) - style.bottomMargin
                    - blockHeight;
            }
            dy += animation.dy;
        }
        if (!style.vertical) {
            dy += style.layoutOffsetY + placementOffsetY;
        }
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
        auto visualTransformPadding = [](const TextStyle &item, bool ruby) {
            const float stroke = ruby
                ? std::max(item.rubyStrokeWidth, 0.0f)
                    + std::max(item.rubyStroke2Width, 0.0f)
                : std::max(item.strokeWidth, 0.0f)
                    + std::max(item.stroke2Width, 0.0f);
            const std::string &decoration = ruby
                ? item.rubyDecorationKind
                : item.decorationKind;
            float padding = stroke * 0.5f + 3.0f;
            if (decoration == "glow") {
                const float glow = ruby
                    ? std::max(item.rubyGlowBeforeRadius, item.rubyGlowAfterRadius)
                    : std::max(item.glowBeforeRadius, item.glowAfterRadius);
                padding += std::max(glow, 0.0f) * 3.0f;
            } else if (decoration == "shadow") {
                const float shadowX = ruby
                    ? item.rubyShadowOffsetX
                    : item.shadowOffsetX;
                const float shadowY = ruby
                    ? item.rubyShadowOffsetY
                    : item.shadowOffsetY;
                padding += std::max(std::abs(shadowX), std::abs(shadowY));
            }
            return padding;
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
        // Compact readback bands must follow the complete transformed visual,
        // not the stable line box. N3 avoids this class of clipping by always
        // reading its full-frame target. Preserve our band optimization by
        // transforming a conservatively padded glyph rectangle instead. The
        // horizontal padding is important for CharDrip/SpinFlip: their shear
        // maps glow and shadow pixels from X into a much larger Y extent.
        auto extendAnimatedVisualBounds = [&] (
            ID2D1Geometry *baseGeometry,
            const CharacterAnimationState &animationState,
            float padding
        ) {
            if (baseGeometry == nullptr || !animationState.transformed) {
                return;
            }
            D2D1_RECT_F bounds{};
            checkHr(
                baseGeometry->GetBounds(nullptr, &bounds),
                "ID2D1Geometry::GetBounds(animated readback band)",
                device_
            );
            bounds.left -= padding;
            bounds.top -= padding;
            bounds.right += padding;
            bounds.bottom += padding;
            const D2D1_MATRIX_3X2_F &matrix = animationState.matrix;
            const auto transformedY = [&](float x, float y) {
                return x * matrix._12 + y * matrix._22 + matrix._32;
            };
            const float visualTop = std::min({
                transformedY(bounds.left, bounds.top),
                transformedY(bounds.right, bounds.top),
                transformedY(bounds.left, bounds.bottom),
                transformedY(bounds.right, bounds.bottom),
            });
            const float visualBottom = std::max({
                transformedY(bounds.left, bounds.top),
                transformedY(bounds.right, bounds.top),
                transformedY(bounds.left, bounds.bottom),
                transformedY(bounds.right, bounds.bottom),
            });
            contentTop = std::min(contentTop, visualTop);
            contentBottom = std::max(contentBottom, visualBottom);
        };
        for (std::size_t index = 0; index < line->chars.size(); ++index) {
            const Impl::CachedChar &ch = line->chars[index];
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            extendAnimatedVisualBounds(
                ch.geometry.Get(), characterAnimationAt(index),
                visualTransformPadding(charStyle, false)
            );
        }
        for (const Impl::CachedRuby &ruby : line->rubies) {
            const TextStyle &rubyStyle = ruby.styleIndex >= 0
                && ruby.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ruby.styleIndex)]
                : style;
            const float padding = visualTransformPadding(rubyStyle, true);
            for (std::size_t index = 0; index < ruby.chars.size(); ++index) {
                extendAnimatedVisualBounds(
                    ruby.chars[index].geometry.Get(),
                    rubyUnitAnimationAt(ruby, index), padding
                );
            }
        }
        for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
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
        const float signalTextMetric = (ascent - descent) * 0.5f;
        const float signalGroupY = style.volumeOffsetY
            - signalGeometry.strokeExtent
            - signalGeometry.size * 0.5f
            - signalTextMetric;
        if (signalState.visible) {
            for (int index = 0; index < signalGeometry.count; ++index) {
                const float top = signalGroupY + signalGeometry.strokeExtent
                    + signalGeometry.alignBaseShift
                    + static_cast<float>(index) * signalGeometry.alignDeltaShift;
                const float height = std::max(
                    signalGeometry.frontHeight
                        + static_cast<float>(index) * signalGeometry.heightDelta,
                    1.0f
                );
                contentTop = std::min(
                    contentTop, top - signalGeometry.strokeExtent - 2.0f
                );
                contentBottom = std::max(
                    contentBottom,
                    top + height + signalGeometry.strokeExtent + 2.0f
                );
            }
        }
        const float shapeGroupY = style.litOffsetY - ascent - shapeGeometry.size;
        if (shapeState.visible && shapeState.activeIndex >= 0) {
            for (int index = 0; index <= shapeState.activeIndex; ++index) {
                const bool active = index == shapeState.activeIndex;
                const float offsetY = active ? shapeState.dy : 0.0f;
                const float top = shapeGroupY + offsetY;
                const float shadowOffset = style.litShadow
                    ? std::max(shapeGeometry.size * 0.08f, 1.0f)
                    : 0.0f;
                contentTop = std::min(
                    contentTop,
                    top - shapeGeometry.strokeExtent - 2.0f
                );
                contentBottom = std::max(
                    contentBottom,
                    top + shapeGeometry.size + shadowOffset
                        + shapeGeometry.strokeExtent + 2.0f
                );
            }
        }
        int intervalTop = std::clamp(
            static_cast<int>(std::floor(dy + contentTop - topPad)),
            0,
            scene.height
        );
        int intervalBottom = std::clamp(
            static_cast<int>(std::ceil(dy + contentBottom + bottomPad)),
            0,
            scene.height
        );
        if (hasViewportTransform && !line->staticOverlay
            && intervalBottom > intervalTop) {
            auto transformedY = [&](float x, float y) {
                return x * lineViewportTransform._12
                    + y * lineViewportTransform._22
                    + lineViewportTransform._32;
            };
            const float transformedTop = std::min({
                transformedY(0.0f, static_cast<float>(intervalTop)),
                transformedY(static_cast<float>(scene.width), static_cast<float>(intervalTop)),
                transformedY(0.0f, static_cast<float>(intervalBottom)),
                transformedY(static_cast<float>(scene.width), static_cast<float>(intervalBottom)),
            });
            const float transformedBottom = std::max({
                transformedY(0.0f, static_cast<float>(intervalTop)),
                transformedY(static_cast<float>(scene.width), static_cast<float>(intervalTop)),
                transformedY(0.0f, static_cast<float>(intervalBottom)),
                transformedY(static_cast<float>(scene.width), static_cast<float>(intervalBottom)),
            });
            intervalTop = std::clamp(
                static_cast<int>(std::floor(transformedTop)) - 2,
                0,
                scene.height
            );
            intervalBottom = std::clamp(
                static_cast<int>(std::ceil(transformedBottom)) + 2,
                0,
                scene.height
            );
        }
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
        auto bitmapImageForGuide = [&](const BitmapGuide &guide, bool after) -> ID2D1Bitmap1 * {
            const std::wstring &path = after && !guide.afterPath.empty()
                ? guide.afterPath
                : guide.beforePath;
            const std::uint64_t modifiedMs = after && !guide.afterPath.empty()
                ? guide.afterModifiedMs
                : guide.beforeModifiedMs;
            const std::uint64_t size = after && !guide.afterPath.empty()
                ? guide.afterSize
                : guide.beforeSize;
            const auto found = std::find_if(
                impl_->images.begin(), impl_->images.end(),
                [&](const Impl::CachedImage &image) {
                    return image.path == path
                        && image.modifiedMs == modifiedMs
                        && image.size == size;
                }
            );
            return found == impl_->images.end() ? nullptr : found->bitmap.Get();
        };
        auto paintBrushAt = [&](const PaintStyle &paint, const D2D1_RECT_F &rect,
                                const RgbaColor &fallback,
                                float offsetX, float offsetY) {
            ID2D1Bitmap1 *image = imageForPaint(paint);
            const float canvasDx = dx + offsetX;
            const float canvasDy = dy + offsetY;
            const bool gradientPositionDependent = paint.mode == "gradient_horizontal"
                || paint.mode == "gradient_vertical"
                || paint.mode == "split_vertical";
            const auto samePosition = [&](const Impl::CachedBrush &entry) {
                if (gradientPositionDependent) {
                    return entry.rect.left == rect.left
                        && entry.rect.top == rect.top
                        && entry.rect.right == rect.right
                        && entry.rect.bottom == rect.bottom;
                }
                if (paint.mode == "image") {
                    return entry.canvasDx == canvasDx
                        && entry.canvasDy == canvasDy;
                }
                return true;
            };
            Microsoft::WRL::ComPtr<ID2D1Brush> brush;
            if (impl_->resourceCacheEnabled) {
                const auto found = std::find_if(
                    impl_->brushes.begin(), impl_->brushes.end(),
                    [&](const Impl::CachedBrush &entry) {
                        return entry.paint == paint
                            && entry.fallback == fallback
                            && entry.imageIdentity == image
                            && samePosition(entry);
                    }
                );
                if (found != impl_->brushes.end()) {
                    found->lastUse = ++impl_->brushUseSerial;
                    brush = found->brush;
                    if (impl_->countersEnabled) {
                        ++impl_->diagnostics.brushCacheHits;
                    }
                } else {
                    if (impl_->countersEnabled) {
                        ++impl_->diagnostics.brushCacheMisses;
                    }
                    brush = createPaintBrush(
                        context, paint, rect, fallback, device_, image,
                        canvasDx, canvasDy,
                        impl_->countersEnabled
                            ? &frameDiagnostics.brushCreated
                            : nullptr
                    );
                    if (impl_->brushes.size() >= Impl::brushCapacity) {
                        const auto oldest = std::min_element(
                            impl_->brushes.begin(), impl_->brushes.end(),
                            [](const Impl::CachedBrush &left,
                               const Impl::CachedBrush &right) {
                                return left.lastUse < right.lastUse;
                            }
                        );
                        impl_->brushes.erase(oldest);
                        if (impl_->countersEnabled) {
                            ++impl_->diagnostics.brushCacheEvictions;
                        }
                    }
                    impl_->brushes.push_back(Impl::CachedBrush{
                        paint,
                        fallback,
                        image,
                        rect,
                        canvasDx,
                        canvasDy,
                        brush,
                        ++impl_->brushUseSerial,
                    });
                }
            } else {
                brush = createPaintBrush(
                    context, paint, rect, fallback, device_, image,
                    canvasDx, canvasDy,
                    impl_->countersEnabled
                        ? &frameDiagnostics.brushCreated
                        : nullptr
                );
            }
            if (brush) {
                updatePaintBrush(
                    brush.Get(), paint, rect, canvasDx, canvasDy
                );
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

        const bool rtl = style.rightToLeft && !style.vertical;
        const auto wipePositionAt = [&](const Impl::CachedChar &ch) {
            if (ch.wipePoints.empty()) {
                const int duration = std::max(ch.endMs - ch.startMs, 1);
                return std::clamp(
                    static_cast<float>(tMs - ch.startMs) / static_cast<float>(duration),
                    0.0f, 1.0f
                );
            }
            if (tMs <= ch.wipePoints.front().timeMs) {
                return ch.wipePoints.front().position;
            }
            if (tMs >= ch.wipePoints.back().timeMs) {
                return ch.wipePoints.back().position;
            }
            for (std::size_t index = 1; index < ch.wipePoints.size(); ++index) {
                const WipePoint &previous = ch.wipePoints[index - 1];
                const WipePoint &following = ch.wipePoints[index];
                if (tMs >= following.timeMs) {
                    continue;
                }
                const int duration = following.timeMs - previous.timeMs;
                if (duration <= 0) {
                    return following.position;
                }
                const float local = std::clamp(
                    static_cast<float>(tMs - previous.timeMs)
                        / static_cast<float>(duration),
                    0.0f, 1.0f
                );
                return previous.position
                    + (following.position - previous.position) * local;
            }
            return ch.wipePoints.back().position;
        };
        const auto wipeCoordinateAt = [&](const Impl::CachedChar &ch) {
            const float position = wipePositionAt(ch);
            return style.vertical
                ? ch.top + (ch.bottom - ch.top) * position
                : (rtl
                    ? ch.right - (ch.right - ch.left) * position
                     : ch.left + (ch.right - ch.left) * position);
        };
        const auto unclampedWipePositionAt = [&](const Impl::CachedChar &ch) {
            if (ch.wipePoints.size() < 2) {
                const int duration = std::max(ch.endMs - ch.startMs, 1);
                return static_cast<float>(tMs - ch.startMs)
                    / static_cast<float>(duration);
            }
            if (tMs < ch.wipePoints.front().timeMs) {
                return ch.wipePoints.front().position;
            }
            std::size_t begin = 0;
            for (std::size_t index = ch.wipePoints.size() - 1; index > 0; --index) {
                if (ch.wipePoints[index - 1].timeMs <= tMs) {
                    begin = index - 1;
                    break;
                }
            }
            const WipePoint &previous = ch.wipePoints[begin];
            const WipePoint &following = ch.wipePoints[begin + 1];
            const int duration = following.timeMs - previous.timeMs;
            if (duration == 0) {
                return following.position;
            }
            return previous.position
                + (following.position - previous.position)
                    * static_cast<float>(tMs - previous.timeMs)
                    / static_cast<float>(duration);
        };
        const auto unclampedWipeCoordinateAt = [&](const Impl::CachedChar &ch) {
            const float position = unclampedWipePositionAt(ch);
            return style.vertical
                ? ch.top + (ch.bottom - ch.top) * position
                : (rtl
                    ? ch.right - (ch.right - ch.left) * position
                    : ch.left + (ch.right - ch.left) * position);
        };
        enum class N3WipePhase { Before, After, Wiping };
        const auto wipePhaseAt = [&](const std::vector<Impl::CachedChar> &chars,
                                     std::size_t index) {
            const Impl::CachedChar &ch = chars[index];
            const int start = wipeStartMs(ch);
            const int end = wipeEndMs(ch);
            bool wiping = start < tMs && tMs < end && start != end;
            if (!wiping && index + 1 < chars.size()) {
                const int followingEnd = wipeEndMs(chars[index + 1]);
                wiping = start < tMs && tMs < followingEnd
                    && start != followingEnd;
            }
            if (wiping) {
                return N3WipePhase::Wiping;
            }
            return tMs <= start ? N3WipePhase::Before : N3WipePhase::After;
        };
        const auto delegatedWipeCoordinateAt = [&](
            const std::vector<Impl::CachedChar> &chars, std::size_t index
        ) {
            const Impl::CachedChar &ch = chars[index];
            if (tMs > wipeEndMs(ch) && index + 1 < chars.size()
                && chars[index + 1].geometry != nullptr) {
                return wipeCoordinateAt(chars[index + 1]);
            }
            return tMs > wipeEndMs(ch)
                ? unclampedWipeCoordinateAt(ch)
                : wipeCoordinateAt(ch);
        };
        const auto charWipeComplete = [&](std::size_t charIndex) {
            return charIndex < line->chars.size()
                && wipePhaseAt(line->chars, charIndex) == N3WipePhase::After;
        };
        float wipeEdge = style.vertical
            ? line->fillBounds.top
            : (rtl ? line->bounds.right : line->bounds.left);
        for (const Impl::CachedChar &ch : line->chars) {
            if (tMs < wipeStartMs(ch)) {
                break;
            }
            wipeEdge = wipeCoordinateAt(ch);
            // At the exact hand-off frame N3 still uses this character's
            // AdjustWipeEnd endpoint. The following scanline takes over on
            // the next sample.
            if (tMs <= wipeEndMs(ch)) {
                break;
            }
        }
        // Painter releases the wipe once every timing segment is complete.
        // Keeping the final clip would leave before-colour pixels in outer
        // antialiasing, stroke2, shadow and glow extents.
        const bool mainWipeComplete = !line->chars.empty()
            && std::all_of(
                line->chars.begin(), line->chars.end(),
                [&](const Impl::CachedChar &ch) { return tMs >= wipeEndMs(ch); }
            );
        using UtopiaWipe = std::pair<D2D1_RECT_F, float>;
        std::vector<UtopiaWipe> utopiaCharWipeCache(line->chars.size());
        std::vector<bool> utopiaCharWipeReady(line->chars.size(), false);
        auto utopiaCharWipe = [&](std::size_t charIndex) {
            D2D1_RECT_F bounds{};
            if (charIndex >= line->chars.size()) {
                return UtopiaWipe{bounds, 0.0f};
            }
            if (utopiaCharWipeReady[charIndex]) {
                return utopiaCharWipeCache[charIndex];
            }
            std::size_t wipeIndex = charIndex;
            if (charIndex < line->chars.size()
                && tMs > wipeEndMs(line->chars[charIndex])
                && charIndex + 1 < line->chars.size()
                && line->chars[charIndex + 1].geometry != nullptr) {
                wipeIndex = charIndex + 1;
            }
            ID2D1Geometry *geometry = charGeometryAt(charIndex);
            if (geometry == nullptr) {
                utopiaCharWipeReady[charIndex] = true;
                utopiaCharWipeCache[charIndex] = UtopiaWipe{bounds, 0.0f};
                return utopiaCharWipeCache[charIndex];
            }
            checkHr(
                geometry->GetBounds(nullptr, &bounds),
                "ID2D1Geometry::GetBounds(utopia wipe)",
                device_
            );
            // The wipe edge delegates to the following character while that
            // character is still wiping, but the clip rect must keep covering
            // this character's own glyph; only the edge travels across the
            // delegated extent.
            D2D1_RECT_F wipeBounds = bounds;
            if (wipeIndex != charIndex) {
                ID2D1Geometry *wipeGeometry = charGeometryAt(wipeIndex);
                if (wipeGeometry == nullptr) {
                    wipeIndex = charIndex;
                } else {
                    checkHr(
                        wipeGeometry->GetBounds(nullptr, &wipeBounds),
                        "ID2D1Geometry::GetBounds(utopia wipe)",
                        device_
                    );
                }
            }
            const Impl::CachedChar &ch = line->chars[wipeIndex];
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            const float edgeHalf = static_cast<float>(
                std::max(static_cast<int>(charStyle.strokeWidth), 0) / 2
            );
            const float left = std::floor(wipeBounds.left) - edgeHalf;
            const float right = std::ceil(wipeBounds.right) + edgeHalf;
            float ratio = 0.0f;
            const CharacterAnimationState animationState = characterAnimationAt(wipeIndex);
            if (animationState.utopiaExit) {
                ratio = 1.0f;
            } else if (tMs > wipeStartMs(ch)) {
                ratio = tMs > wipeEndMs(ch)
                    ? unclampedWipePositionAt(ch)
                    : wipePositionAt(ch);
            }
            utopiaCharWipeReady[charIndex] = true;
            utopiaCharWipeCache[charIndex] = UtopiaWipe{
                bounds,
                left + std::max(right - left, 1.0f) * ratio
            };
            return utopiaCharWipeCache[charIndex];
        };
        std::vector<std::vector<UtopiaWipe>> utopiaRubyWipeCache;
        std::vector<std::vector<bool>> utopiaRubyWipeReady;
        utopiaRubyWipeCache.reserve(line->rubies.size());
        utopiaRubyWipeReady.reserve(line->rubies.size());
        for (const Impl::CachedRuby &ruby : line->rubies) {
            utopiaRubyWipeCache.emplace_back(ruby.chars.size());
            utopiaRubyWipeReady.emplace_back(ruby.chars.size(), false);
        }
        auto utopiaRubyUnitWipe = [&](const Impl::CachedRuby &ruby,
                                      std::size_t rubyIndex,
                                      std::size_t unitIndex,
                                      const TextStyle &rubyStyle) {
            D2D1_RECT_F bounds{};
            if (rubyIndex >= utopiaRubyWipeCache.size()
                || unitIndex >= utopiaRubyWipeCache[rubyIndex].size()) {
                return UtopiaWipe{bounds, 0.0f};
            }
            if (utopiaRubyWipeReady[rubyIndex][unitIndex]) {
                return utopiaRubyWipeCache[rubyIndex][unitIndex];
            }
            ID2D1Geometry *geometry = rubyGeometryAt(rubyIndex, unitIndex);
            if (geometry == nullptr || unitIndex >= ruby.chars.size()) {
                utopiaRubyWipeReady[rubyIndex][unitIndex] = true;
                utopiaRubyWipeCache[rubyIndex][unitIndex] = UtopiaWipe{bounds, 0.0f};
                return utopiaRubyWipeCache[rubyIndex][unitIndex];
            }
            checkHr(
                geometry->GetBounds(nullptr, &bounds),
                "ID2D1Geometry::GetBounds(utopia ruby wipe)",
                device_
            );
            const Impl::CachedChar &unit = ruby.chars[unitIndex];
            const float edgeHalf = static_cast<float>(
                std::max(static_cast<int>(rubyStyle.rubyStrokeWidth), 0) / 2
            );
            const float left = std::floor(bounds.left) - edgeHalf;
            const float right = std::ceil(bounds.right) + edgeHalf;
            float ratio = 0.0f;
            const CharacterAnimationState animationState = rubyUnitAnimationAt(
                ruby, unitIndex
            );
            if (animationState.utopiaExit || tMs >= wipeEndMs(unit)) {
                ratio = 1.0f;
            } else if (tMs > wipeStartMs(unit)) {
                ratio = wipePositionAt(unit);
            }
            utopiaRubyWipeReady[rubyIndex][unitIndex] = true;
            utopiaRubyWipeCache[rubyIndex][unitIndex] = UtopiaWipe{
                bounds,
                left + std::max(right - left, 1.0f) * ratio
            };
            return utopiaRubyWipeCache[rubyIndex][unitIndex];
        };

        const float geometryPad = std::max(style.strokeWidth + style.stroke2Width, 2.0f) + 4.0f;
        // N3 splits before/after colours with a full-frame vertical clip
        // (0..MovieInfo.Height). A line-local vertical clip creates a hard
        // horizontal seam when Utopia/CharDrip moves a glyph outside the
        // stable row. Keep a deliberately oversized local range so the target
        // surface, not the row box, is the only vertical boundary.
        const float fullWipeClipTop = -static_cast<float>(scene.height) * 2.0f;
        const float fullWipeClipBottom = static_cast<float>(scene.height) * 2.0f;
        const D2D1_RECT_F afterClip = style.vertical
            ? D2D1::RectF(
                line->bounds.left - geometryPad,
                line->fillBounds.top - geometryPad,
                line->bounds.right + geometryPad,
                wipeEdge
            )
            : (rtl
                ? D2D1::RectF(
                    wipeEdge,
                    fullWipeClipTop,
                    line->bounds.right + geometryPad,
                    fullWipeClipBottom
                )
                : D2D1::RectF(
                    line->bounds.left - geometryPad,
                    fullWipeClipTop,
                    wipeEdge,
                    fullWipeClipBottom
                ));
        const bool hasAfterWipe = style.vertical
            ? wipeEdge > line->fillBounds.top
            : (rtl ? wipeEdge < line->bounds.right : wipeEdge > line->bounds.left);
        auto bitmapGuideNoWipe = [&](const Impl::CachedChar &ch) {
            return ch.bitmapGuide.has_value() && ch.bitmapGuide->afterPath.empty();
        };
        auto drawBitmapGuidePart = [&](std::size_t charIndex, bool after) {
            if (charIndex >= line->chars.size()) {
                return;
            }
            const Impl::CachedChar &ch = line->chars[charIndex];
            if (!ch.bitmapGuide.has_value()) {
                return;
            }
            if (after && ch.bitmapGuide->afterPath.empty()) {
                return;
            }
            ID2D1Bitmap1 *bitmap = bitmapImageForGuide(*ch.bitmapGuide, after);
            if (bitmap == nullptr) {
                return;
            }
            const float opacity = globalOpacity * characterOpacityAt(charIndex);
            if (opacity <= 0.0f) {
                return;
            }
            context->DrawBitmap(
                bitmap,
                ch.bitmapRect,
                opacity,
                D2D1_INTERPOLATION_MODE_LINEAR,
                nullptr
            );
        };
        auto rubyWipeEdgeAt = [&](const Impl::CachedRuby &ruby) {
            float edge = style.vertical
                ? ruby.bounds.top
                : (rtl ? ruby.bounds.right : ruby.bounds.left);
            for (const Impl::CachedChar &ch : ruby.chars) {
                if (tMs < wipeStartMs(ch)) {
                    break;
                }
                edge = wipeCoordinateAt(ch);
                if (tMs <= wipeEndMs(ch)) {
                    break;
                }
            }
            return edge;
        };
        auto rubyWipeComplete = [&](const Impl::CachedRuby &ruby) {
            return !ruby.chars.empty()
                && std::all_of(
                    ruby.chars.begin(), ruby.chars.end(),
                    [&](const Impl::CachedChar &ch) { return tMs >= wipeEndMs(ch); }
                );
        };
        auto rubyWipePhaseAt = [&](const Impl::CachedRuby &ruby) {
            if (ruby.chars.empty()) {
                return N3WipePhase::Before;
            }
            const bool allBefore = std::all_of(
                ruby.chars.begin(), ruby.chars.end(),
                [&](const Impl::CachedChar &ch) { return tMs <= wipeStartMs(ch); }
            );
            if (allBefore) {
                return N3WipePhase::Before;
            }
            return rubyWipeComplete(ruby)
                ? N3WipePhase::After
                : N3WipePhase::Wiping;
        };
        auto rubyUnitWipeComplete = [&](const Impl::CachedRuby &ruby,
                                        std::size_t unitIndex) {
            return unitIndex < ruby.chars.size()
                && tMs >= wipeEndMs(ruby.chars[unitIndex]);
        };
        auto rubyUnitWipePhaseAt = [&](const Impl::CachedRuby &ruby,
                                       std::size_t unitIndex) {
            if (unitIndex >= ruby.chars.size()) {
                return N3WipePhase::Before;
            }
            const Impl::CachedChar &unit = ruby.chars[unitIndex];
            return tMs <= wipeStartMs(unit)
                ? N3WipePhase::Before
                : (tMs >= wipeEndMs(unit)
                    ? N3WipePhase::After
                    : N3WipePhase::Wiping);
        };
        auto rubyPhaseVisible = [&](const Impl::CachedRuby &ruby,
                                    float edge, bool after) {
            if (style.vertical) {
                return after ? edge > ruby.bounds.top : edge < ruby.bounds.bottom;
            }
            if (rtl) {
                return after ? edge < ruby.bounds.right : edge > ruby.bounds.left;
            }
            return after ? edge > ruby.bounds.left : edge < ruby.bounds.right;
        };
        auto rubyStyleFor = [&](int styleIndex) -> const TextStyle & {
            return styleIndex >= 0
                && styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(styleIndex)]
                : style;
        };

        // Glow sources are authored in line-local coordinates and written to
        // the scene-sized scratch after the line translation. Clamp requested
        // effect output to the scratch in the same local coordinate system.
        const D2D1_RECT_F glowCanvasRect = D2D1::RectF(
            -dx,
            -dy,
            static_cast<float>(scene.width) - dx,
            static_cast<float>(scene.height) - dy
        );
        const auto clampGlowRect = [&](const D2D1_RECT_F &rect) {
            return D2D1::RectF(
                std::max(rect.left, glowCanvasRect.left),
                std::max(rect.top, glowCanvasRect.top),
                std::min(rect.right, glowCanvasRect.right),
                std::min(rect.bottom, glowCanvasRect.bottom)
            );
        };
        const auto glowOutputRect = [&] (
            const D2D1_RECT_F &content,
            float sourceWidth,
            int radius
        ) {
            const float expansion = sourceWidth + 3.0f * radius + 16.0f;
            const D2D1_RECT_F expanded = expandedRect(content, expansion);
            return impl_->glowDirtyRectEnabled
                ? clampGlowRect(expanded)
                : expanded;
        };
        const auto glowClearBounds = [&] (
            const D2D1_RECT_F &sourceRect,
            int radius
        ) {
            const float expansion = 3.0f * radius + 16.0f;
            const D2D1_RECT_F expanded = expandedRect(sourceRect, expansion);
            if (!impl_->glowDirtyRectEnabled) {
                return expanded;
            }
            const D2D1_RECT_F clamped = clampGlowRect(expanded);
            // Keep the cropped target origin on the same device-pixel grid as
            // the full-scene scratch. Otherwise a fractional crop origin can
            // shift Direct2D antialias coverage even when the algebraic
            // source/destination rectangles cancel out.
            return D2D1::RectF(
                std::floor(clamped.left + dx) - dx,
                std::floor(clamped.top + dy) - dy,
                std::ceil(clamped.right + dx) - dx,
                std::ceil(clamped.bottom + dy) - dy
            );
        };

        const auto glowStart = Clock::now();
        ID2D1Bitmap1 *glowSource = nullptr;
        ID2D1Effect *blur = nullptr;
        std::vector<int> glowSigmas;
        D2D1_RECT_F glowSourceRect{};
        D2D1_RECT_F glowEffectRect{};
        if (style.decorationKind == "glow"
            && !line->hasInlineStyles) {
            const int radius = std::max(
                1,
                static_cast<int>(std::lround(std::max(style.glowBeforeRadius, style.glowAfterRadius)))
            );
            const float sourceWidth = std::max(0.0f, style.strokeWidth)
                + (style.stroke2Width > 0.0f ? style.stroke2Width : 0.0f)
                + static_cast<float>(radius);
            // Restrict the scratch clear and, at composite time, the blur
            // evaluation to the line's neighbourhood; Direct2D effects only
            // process the input needed for the requested output rectangle.
            const D2D1_RECT_F glowContent = unionRect(
                line->bounds, line->fillBounds
            );
            glowSourceRect = glowOutputRect(
                glowContent, sourceWidth, radius
            );
            count(
                frameDiagnostics.glowSourceAreaPx,
                rectAreaPx(glowSourceRect)
            );
            const D2D1_RECT_F glowClearRect = glowClearBounds(
                glowSourceRect, radius
            );
            glowSource = acquireGlowScratch(
                glowClearRect.right - glowClearRect.left,
                glowClearRect.bottom - glowClearRect.top
            );
            blur = acquireGlowEffect();
            glowEffectRect = impl_->glowDirtyRectEnabled
                ? D2D1::RectF(
                    glowSourceRect.left - glowClearRect.left,
                    glowSourceRect.top - glowClearRect.top,
                    glowSourceRect.right - glowClearRect.left,
                    glowSourceRect.bottom - glowClearRect.top
                )
                : D2D1::RectF(
                    glowSourceRect.left + dx,
                    glowSourceRect.top + dy,
                    glowSourceRect.right + dx,
                    glowSourceRect.bottom + dy
                );
            context->SetTarget(glowSource);
            context->SetTransform(
                impl_->glowDirtyRectEnabled
                    ? D2D1::Matrix3x2F::Translation(
                        -glowClearRect.left, -glowClearRect.top
                    )
                    : D2D1::Matrix3x2F::Translation(dx, dy)
            );
            context->BeginDraw();
            // The pooled bitmap can be larger than this frame's requested
            // crop. Clear the whole target before installing the local clip;
            // otherwise GaussianBlur may sample stale pixels just outside the
            // crop and make output depend on this worker's previous frame.
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            pushAxisAlignedClip(
                glowClearRect, D2D1_ANTIALIAS_MODE_ALIASED
            );
            const auto drawGlowPart = [&](std::size_t index, bool after) {
                if (!charUsesGroupedGlowAt(index)) {
                    return;
                }
                ID2D1Geometry *geometry = charGeometryAt(index);
                if (geometry == nullptr) {
                    return;
                }
                ID2D1Brush *brush = after ? afterDecor.Get() : beforeDecor.Get();
                brush->SetOpacity(
                    globalOpacity * characterOpacityAt(index)
                );
                context->DrawGeometry(geometry, brush, sourceWidth);
            };
            const auto pushGlowClip = [&](std::size_t index, bool after) {
                float edge = delegatedWipeCoordinateAt(line->chars, index);
                D2D1_RECT_F bounds = line->bounds;
                if (useUtopiaTransition) {
                    const auto animated = utopiaCharWipe(index);
                    bounds = animated.first;
                    edge = animated.second;
                }
                const float pad = sourceWidth + 4.0f;
                D2D1_RECT_F clip{};
                if (style.vertical) {
                    clip = after
                        ? D2D1::RectF(
                            bounds.left - pad, bounds.top - pad,
                            bounds.right + pad, edge
                        )
                        : D2D1::RectF(
                            bounds.left - pad, edge,
                            bounds.right + pad, bounds.bottom + pad
                        );
                } else if (rtl) {
                    clip = after
                        ? D2D1::RectF(
                            edge, bounds.top - pad,
                            bounds.right + pad, bounds.bottom + pad
                        )
                        : D2D1::RectF(
                            bounds.left - pad, bounds.top - pad,
                            edge, bounds.bottom + pad
                        );
                } else {
                    clip = after
                        ? D2D1::RectF(
                            bounds.left - pad, bounds.top - pad,
                            edge, bounds.bottom + pad
                        )
                        : D2D1::RectF(
                            edge, bounds.top - pad,
                            bounds.right + pad, bounds.bottom + pad
                        );
                }
                pushAxisAlignedClip(
                    clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                );
            };
            const auto drawGlowPhase = [&](std::size_t index, N3WipePhase phase) {
                if (phase != N3WipePhase::Wiping) {
                    drawGlowPart(index, phase == N3WipePhase::After);
                    return;
                }
                pushGlowClip(index, false);
                drawGlowPart(index, false);
                context->PopAxisAlignedClip();
                pushGlowClip(index, true);
                drawGlowPart(index, true);
                context->PopAxisAlignedClip();
            };
            for (std::size_t reverse = line->chars.size(); reverse > 0; --reverse) {
                const std::size_t index = reverse - 1;
                if (wipePhaseAt(line->chars, index) == N3WipePhase::Before) {
                    drawGlowPhase(index, N3WipePhase::Before);
                }
            }
            for (std::size_t index = 0; index < line->chars.size(); ++index) {
                if (wipePhaseAt(line->chars, index) == N3WipePhase::After) {
                    drawGlowPhase(index, N3WipePhase::After);
                }
            }
            for (std::size_t index = 0; index < line->chars.size(); ++index) {
                if (wipePhaseAt(line->chars, index) == N3WipePhase::Wiping) {
                    drawGlowPhase(index, N3WipePhase::Wiping);
                }
            }
            context->PopAxisAlignedClip();
            endDrawMeasured(
                "ID2D1DeviceContext::EndDraw(glow source)",
                frameDiagnostics.endDrawGlowSourceMs,
                frameDiagnostics.endDrawGlowSourceCount
            );
            blur->SetInput(0, glowSource);

            // N3 DrawOneLineDecorBlurMulti: N = BlurLevel + 1 and
            // sigma_i = R - floor(i * R / N). The common N3 path has one
            // DecorSize for both wipe colors, so use that exact combined source.
            const int passes = std::clamp(style.glowConcentrationLevel, 0, 2) + 1;
            for (int index = 0; index < passes; ++index) {
                glowSigmas.push_back(radius - index * radius / passes);
            }
        }

        struct RubyGlowLayer {
            ID2D1Bitmap1 *source = nullptr;
            ID2D1Effect *blur = nullptr;
            std::vector<int> sigmas;
            D2D1_MATRIX_3X2_F transform = D2D1::Matrix3x2F::Identity();
            bool hasTransform = false;
            D2D1_RECT_F sourceRect{};
            D2D1_RECT_F effectRect{};
        };
        std::vector<RubyGlowLayer> rubyGlowLayers;
        // Grouped layers (rubyOnly < 0) collect every unit whose animation is
        // identity this frame into one source per ruby style and wipe colour.
        // Units animating this frame keep the Painter blur-then-transform
        // semantics through dedicated per-ruby/per-unit layers.
        auto appendRubyGlowLayer = [&](int styleIndex, bool after,
                                       int rubyOnly, int unitOnly) {
            const TextStyle &rubyStyle = rubyStyleFor(styleIndex);
            const float requestedRadius = after
                ? rubyStyle.rubyGlowAfterRadius
                : rubyStyle.rubyGlowBeforeRadius;
            const int radius = std::max(
                0, static_cast<int>(std::lround(requestedRadius))
            );
            const bool hasVisibleSource = std::any_of(
                line->rubies.begin(), line->rubies.end(),
                [&](const Impl::CachedRuby &ruby) {
                    const int rubyIndex = static_cast<int>(&ruby - line->rubies.data());
                    const bool selected = (rubyOnly < 0 || rubyIndex == rubyOnly);
                    if (!selected || ruby.styleIndex != styleIndex) {
                        return false;
                    }
                    for (std::size_t unitIndex = 0;
                         unitIndex < ruby.geometries.size(); ++unitIndex) {
                        if ((unitOnly >= 0
                                && static_cast<int>(unitIndex) != unitOnly)
                            || (rubyOnly < 0
                                && !rubyUnitUsesGroupedGlowAt(ruby, unitIndex))
                            || rubyUnitOpacityAt(ruby, unitIndex) <= 0.0f) {
                            continue;
                        }
                        const N3WipePhase phase = useUtopiaTransition
                            ? rubyUnitWipePhaseAt(ruby, unitIndex)
                            : rubyWipePhaseAt(ruby);
                        if (after
                                ? phase != N3WipePhase::Before
                                : phase != N3WipePhase::After) {
                            return true;
                        }
                    }
                    return false;
                }
            );
            if (rubyStyle.rubyDecorationKind != "glow"
                || radius <= 0
                || !hasVisibleSource) {
                return;
            }
            RubyGlowLayer layer;
            if ((spinDirection != 0 || dripDirection != 0 || useUtopiaTransition)
                && rubyOnly >= 0) {
                const Impl::CachedRuby &ruby = line->rubies[
                    static_cast<std::size_t>(rubyOnly)
                ];
                const CharacterAnimationState animationState = unitOnly >= 0
                    ? rubyUnitAnimationAt(ruby, static_cast<std::size_t>(unitOnly))
                    : rubyUnitAnimationAt(ruby, 0);
                layer.transform = D2D1::Matrix3x2F::Translation(-dx, -dy)
                    * animationState.matrix
                    * D2D1::Matrix3x2F::Translation(dx, dy);
                layer.hasTransform = animationState.transformed;
            }
            const float sourceWidth = std::max(0.0f, rubyStyle.rubyStrokeWidth)
                + (rubyStyle.rubyStroke2Width > 0.0f
                    ? rubyStyle.rubyStroke2Width
                    : 0.0f)
                + static_cast<float>(radius);
            const float pad = sourceWidth * 0.5f + radius * 3.0f + 2.0f;
            bool hasContent = false;
            D2D1_RECT_F content{};
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                if ((rubyOnly >= 0 && static_cast<int>(rubyIndex) != rubyOnly)
                    || ruby.styleIndex != styleIndex) {
                    continue;
                }
                content = hasContent
                    ? unionRect(content, ruby.bounds)
                    : ruby.bounds;
                hasContent = true;
            }
            if (!hasContent) {
                return;
            }
            layer.sourceRect = glowOutputRect(
                content, sourceWidth, radius
            );
            count(
                frameDiagnostics.glowSourceAreaPx,
                rectAreaPx(layer.sourceRect)
            );
            const D2D1_RECT_F clearRect = glowClearBounds(
                layer.sourceRect, radius
            );
            layer.source = acquireGlowScratch(
                clearRect.right - clearRect.left,
                clearRect.bottom - clearRect.top
            );
            layer.blur = acquireGlowEffect();
            layer.effectRect = impl_->glowDirtyRectEnabled
                ? D2D1::RectF(
                    layer.sourceRect.left - clearRect.left,
                    layer.sourceRect.top - clearRect.top,
                    layer.sourceRect.right - clearRect.left,
                    layer.sourceRect.bottom - clearRect.top
                )
                : D2D1::RectF(
                    layer.sourceRect.left + dx,
                    layer.sourceRect.top + dy,
                    layer.sourceRect.right + dx,
                    layer.sourceRect.bottom + dy
                );
            context->SetTarget(layer.source);
            context->SetTransform(
                impl_->glowDirtyRectEnabled
                    ? D2D1::Matrix3x2F::Translation(
                        -clearRect.left, -clearRect.top
                    )
                    : D2D1::Matrix3x2F::Translation(dx, dy)
            );
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            pushAxisAlignedClip(
                clearRect, D2D1_ANTIALIAS_MODE_ALIASED
            );
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                if ((rubyOnly >= 0 && static_cast<int>(rubyIndex) != rubyOnly)
                    || ruby.styleIndex != styleIndex) {
                    continue;
                }
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                    after
                        ? rubyStyle.rubyAfterDecorPaint
                        : rubyStyle.rubyBeforeDecorPaint,
                    rubyPaintBounds(
                        after
                            ? rubyStyle.rubyAfterDecorPaint
                            : rubyStyle.rubyBeforeDecorPaint,
                        ruby.fillBounds,
                        ruby.horizontalFillBounds
                    ),
                    after ? rubyStyle.rubyAfterDecor : rubyStyle.rubyBeforeDecor
                );
                for (std::size_t geometryIndex = 0;
                     geometryIndex < ruby.geometries.size(); ++geometryIndex) {
                    if ((unitOnly >= 0
                            && static_cast<int>(geometryIndex) != unitOnly)
                        || (rubyOnly < 0
                            && !rubyUnitUsesGroupedGlowAt(
                                ruby, geometryIndex
                            ))) {
                        continue;
                    }
                    ID2D1Geometry *geometry = rubyOnly < 0
                        ? rubyGeometryAt(rubyIndex, geometryIndex)
                        : ruby.geometries[geometryIndex].Get();
                    if (geometry == nullptr) {
                        continue;
                    }
                    D2D1_RECT_F phaseBounds = ruby.bounds;
                    float edge = rubyWipeEdgeAt(ruby);
                    const N3WipePhase phase = useUtopiaTransition
                        ? rubyUnitWipePhaseAt(ruby, geometryIndex)
                        : rubyWipePhaseAt(ruby);
                    if ((phase == N3WipePhase::Before && after)
                        || (phase == N3WipePhase::After && !after)) {
                        continue;
                    }
                    if (useUtopiaTransition) {
                        const auto animated = utopiaRubyUnitWipe(
                            ruby, rubyIndex, geometryIndex, rubyStyle
                        );
                        phaseBounds = animated.first;
                        edge = animated.second;
                    }
                    const D2D1_RECT_F clip = style.vertical
                        ? (after
                            ? D2D1::RectF(
                                phaseBounds.left - pad,
                                phaseBounds.top - pad,
                                phaseBounds.right + pad, edge
                            )
                            : D2D1::RectF(
                                phaseBounds.left - pad, edge,
                                phaseBounds.right + pad,
                                phaseBounds.bottom + pad
                            ))
                        : (rtl
                            ? (after
                                ? D2D1::RectF(
                                    edge, phaseBounds.top - pad,
                                    phaseBounds.right + pad,
                                    phaseBounds.bottom + pad
                                )
                                : D2D1::RectF(
                                    phaseBounds.left - pad,
                                    phaseBounds.top - pad, edge,
                                    phaseBounds.bottom + pad
                                ))
                            : (after
                                ? D2D1::RectF(
                                    phaseBounds.left - pad,
                                    phaseBounds.top - pad, edge,
                                    phaseBounds.bottom + pad
                                )
                                : D2D1::RectF(
                                    edge, phaseBounds.top - pad,
                                    phaseBounds.right + pad,
                                    phaseBounds.bottom + pad
                                )));
                    if (phase == N3WipePhase::Wiping) {
                        pushAxisAlignedClip(
                            clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                        );
                    }
                    brush->SetOpacity(
                        globalOpacity * rubyUnitOpacityAt(
                            ruby, geometryIndex
                        )
                    );
                    context->DrawGeometry(
                        geometry, brush.Get(), sourceWidth
                    );
                    if (phase == N3WipePhase::Wiping) {
                        context->PopAxisAlignedClip();
                    }
                }
            }
            context->PopAxisAlignedClip();
            endDrawMeasured(
                "ID2D1DeviceContext::EndDraw(ruby glow source)",
                frameDiagnostics.endDrawRubyGlowSourceMs,
                frameDiagnostics.endDrawRubyGlowSourceCount
            );
            layer.blur->SetInput(0, layer.source);
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
            appendRubyGlowLayer(styleIndex, false, -1, -1);
            appendRubyGlowLayer(styleIndex, true, -1, -1);
        }
        if (useUtopiaTransition || dripDirection != 0) {
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                for (std::size_t unitIndex = 0;
                     unitIndex < ruby.geometries.size(); ++unitIndex) {
                    if (!rubyUnitTransformed(ruby, unitIndex)
                        || rubyUnitUsesGroupedGlowAt(ruby, unitIndex)) {
                        continue;
                    }
                    appendRubyGlowLayer(
                        ruby.styleIndex, false,
                        static_cast<int>(rubyIndex), static_cast<int>(unitIndex)
                    );
                    appendRubyGlowLayer(
                        ruby.styleIndex, true,
                        static_cast<int>(rubyIndex), static_cast<int>(unitIndex)
                    );
                }
            }
        } else if (spinDirection != 0) {
            for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
                const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
                if (rubyFadeOpacityAt(ruby) >= 1.0f
                    || rubyFadeOpacityAt(ruby) <= 0.0f) {
                    continue;
                }
                appendRubyGlowLayer(
                    ruby.styleIndex, false, static_cast<int>(rubyIndex), -1
                );
                appendRubyGlowLayer(
                    ruby.styleIndex, true, static_cast<int>(rubyIndex), -1
                );
            }
        }

        struct InlineGlowLayer {
            ID2D1Bitmap1 *source = nullptr;
            ID2D1Effect *blur = nullptr;
            std::vector<int> sigmas;
            D2D1_MATRIX_3X2_F transform = D2D1::Matrix3x2F::Identity();
            bool hasTransform = false;
            D2D1_RECT_F sourceRect{};
            D2D1_RECT_F effectRect{};
        };
        std::vector<InlineGlowLayer> inlineGlowLayers;
        // Grouped layers (charOnly < 0) collect every character whose
        // animation is identity this frame into one source per inline style
        // and wipe colour. Characters animating this frame keep the Painter
        // blur-then-transform semantics through per-character layers.
        auto appendInlineGlowLayer = [&](int styleIndex, bool after, int charOnly) {
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
                line->chars.begin(), line->chars.end(),
                [&](const Impl::CachedChar &ch) {
                    const std::size_t charIndex = static_cast<std::size_t>(
                        &ch - line->chars.data()
                    );
                    if ((charOnly >= 0
                            && static_cast<int>(charIndex) != charOnly)
                        || ch.styleIndex != styleIndex) {
                        return false;
                    }
                    if (charOnly >= 0) {
                        if (!ch.geometry) {
                            return false;
                        }
                        if (useUtopiaTransition) {
                            const N3WipePhase phase = wipePhaseAt(
                                line->chars, charIndex
                            );
                            return after
                                ? phase != N3WipePhase::Before
                                : phase != N3WipePhase::After;
                        }
                        return !(rtl
                            ? ((after && wipeEdge >= ch.right)
                                || (!after && wipeEdge <= ch.left))
                            : ((after && wipeEdge <= ch.left)
                                || (!after && wipeEdge >= ch.right)));
                    }
                    if (!charUsesGroupedGlowAt(charIndex)
                        || charGeometryAt(charIndex) == nullptr) {
                        return false;
                    }
                    if (useUtopiaTransition) {
                        const N3WipePhase phase = wipePhaseAt(
                            line->chars, charIndex
                        );
                        return after
                            ? phase != N3WipePhase::Before
                            : phase != N3WipePhase::After;
                    }
                    return !(rtl
                        ? ((after && wipeEdge >= ch.right)
                            || (!after && wipeEdge <= ch.left))
                        : ((after && wipeEdge <= ch.left)
                            || (!after && wipeEdge >= ch.right)));
                }
            );
            if (charStyle.decorationKind != "glow" || radius <= 0 || !hasVisibleSource) {
                return;
            }
            InlineGlowLayer layer;
            if ((spinDirection != 0 || dripDirection != 0 || useUtopiaTransition)
                && charOnly >= 0) {
                const CharacterAnimationState animationState = characterAnimationAt(
                    static_cast<std::size_t>(charOnly)
                );
                layer.transform = D2D1::Matrix3x2F::Translation(-dx, -dy)
                    * animationState.matrix
                    * D2D1::Matrix3x2F::Translation(dx, dy);
                layer.hasTransform = animationState.transformed;
            }
            Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                after ? charStyle.afterDecorPaint : charStyle.beforeDecorPaint,
                line->fillBounds,
                after ? charStyle.afterDecor : charStyle.beforeDecor
            );
            const float sourceWidth = std::max(charStyle.strokeWidth, 0.0f)
                + std::max(charStyle.stroke2Width, 0.0f)
                + static_cast<float>(radius);
            const float pad = sourceWidth * 0.5f + radius * 3.0f + 2.0f;
            bool hasContent = !impl_->glowDirtyRectEnabled;
            D2D1_RECT_F content = unionRect(line->bounds, line->fillBounds);
            if (impl_->glowDirtyRectEnabled) {
                for (std::size_t charIndex = 0;
                     charIndex < line->chars.size(); ++charIndex) {
                    const Impl::CachedChar &ch = line->chars[charIndex];
                    if ((charOnly >= 0
                            && static_cast<int>(charIndex) != charOnly)
                        || ch.styleIndex != styleIndex
                        || characterOpacityAt(charIndex) <= 0.0f) {
                        continue;
                    }
                    bool visible = false;
                    if (charOnly >= 0) {
                        if (ch.geometry != nullptr) {
                            if (useUtopiaTransition) {
                                const N3WipePhase phase = wipePhaseAt(
                                    line->chars, charIndex
                                );
                                visible = after
                                    ? phase != N3WipePhase::Before
                                    : phase != N3WipePhase::After;
                            } else {
                                visible = !(rtl
                                    ? ((after && wipeEdge >= ch.right)
                                        || (!after && wipeEdge <= ch.left))
                                    : ((after && wipeEdge <= ch.left)
                                        || (!after && wipeEdge >= ch.right)));
                            }
                        }
                    } else if (charUsesGroupedGlowAt(charIndex)
                               && charGeometryAt(charIndex) != nullptr) {
                        if (useUtopiaTransition) {
                            const N3WipePhase phase = wipePhaseAt(
                                line->chars, charIndex
                            );
                            visible = after
                                ? phase != N3WipePhase::Before
                                : phase != N3WipePhase::After;
                        } else {
                            visible = !(rtl
                                ? ((after && wipeEdge >= ch.right)
                                    || (!after && wipeEdge <= ch.left))
                                : ((after && wipeEdge <= ch.left)
                                    || (!after && wipeEdge >= ch.right)));
                        }
                    }
                    if (!visible) {
                        continue;
                    }
                    const D2D1_RECT_F charBounds = D2D1::RectF(
                        ch.left, ch.top, ch.right, ch.bottom
                    );
                    content = hasContent
                        ? unionRect(content, charBounds)
                        : charBounds;
                    hasContent = true;
                }
            }
            if (!hasContent) {
                return;
            }
            layer.sourceRect = glowOutputRect(
                content, sourceWidth, radius
            );
            count(
                frameDiagnostics.glowSourceAreaPx,
                rectAreaPx(layer.sourceRect)
            );
            const D2D1_RECT_F clearRect = glowClearBounds(
                layer.sourceRect, radius
            );
            layer.source = acquireGlowScratch(
                clearRect.right - clearRect.left,
                clearRect.bottom - clearRect.top
            );
            layer.blur = acquireGlowEffect();
            layer.effectRect = impl_->glowDirtyRectEnabled
                ? D2D1::RectF(
                    layer.sourceRect.left - clearRect.left,
                    layer.sourceRect.top - clearRect.top,
                    layer.sourceRect.right - clearRect.left,
                    layer.sourceRect.bottom - clearRect.top
                )
                : D2D1::RectF(
                    layer.sourceRect.left + dx,
                    layer.sourceRect.top + dy,
                    layer.sourceRect.right + dx,
                    layer.sourceRect.bottom + dy
                );
            context->SetTarget(layer.source);
            context->SetTransform(
                impl_->glowDirtyRectEnabled
                    ? D2D1::Matrix3x2F::Translation(
                        -clearRect.left, -clearRect.top
                    )
                    : D2D1::Matrix3x2F::Translation(dx, dy)
            );
            context->BeginDraw();
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
            pushAxisAlignedClip(
                clearRect, D2D1_ANTIALIAS_MODE_ALIASED
            );
            for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
                const Impl::CachedChar &ch = line->chars[charIndex];
                if ((charOnly >= 0 && static_cast<int>(charIndex) != charOnly)
                    || ch.styleIndex != styleIndex) {
                    continue;
                }
                if (charOnly >= 0) {
                    // Per-character layer: draw the upright cached glyph and
                    // apply the animation matrix to the blurred result.
                    N3WipePhase phase = N3WipePhase::Wiping;
                    if (useUtopiaTransition) {
                        phase = wipePhaseAt(line->chars, charIndex);
                    }
                    if (ch.geometry == nullptr
                        || (useUtopiaTransition
                            && ((phase == N3WipePhase::Before && after)
                                || (phase == N3WipePhase::After && !after)))
                        || (!useUtopiaTransition && (rtl
                            ? ((after && wipeEdge >= ch.right)
                                || (!after && wipeEdge <= ch.left))
                            : ((after && wipeEdge <= ch.left)
                                || (!after && wipeEdge >= ch.right))))) {
                        continue;
                    }
                    brush->SetOpacity(
                        globalOpacity * characterOpacityAt(charIndex)
                    );
                    D2D1_RECT_F clip{};
                    bool needClip = false;
                    if (useUtopiaTransition) {
                        if (phase == N3WipePhase::Wiping) {
                            const auto [animatedBounds, animatedEdge] =
                                utopiaCharWipe(charIndex);
                            clip = after
                                ? D2D1::RectF(
                                    animatedBounds.left - pad,
                                    fullWipeClipTop,
                                    animatedEdge,
                                    fullWipeClipBottom
                                )
                                : D2D1::RectF(
                                    animatedEdge,
                                    fullWipeClipTop,
                                    animatedBounds.right + pad,
                                    fullWipeClipBottom
                                );
                            needClip = true;
                        }
                    } else if (!mainWipeComplete) {
                        clip = rtl
                            ? (after
                                ? D2D1::RectF(
                                    wipeEdge, fullWipeClipTop,
                                    ch.right + pad, fullWipeClipBottom
                                )
                                : D2D1::RectF(
                                    ch.left - pad, fullWipeClipTop,
                                    wipeEdge, fullWipeClipBottom
                                ))
                            : (after
                                ? D2D1::RectF(
                                    ch.left - pad, fullWipeClipTop,
                                    wipeEdge, fullWipeClipBottom
                                )
                                : D2D1::RectF(
                                    wipeEdge, fullWipeClipTop,
                                    ch.right + pad, fullWipeClipBottom
                                ));
                        needClip = true;
                    }
                    if (needClip) {
                        pushAxisAlignedClip(
                            clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                        );
                    }
                    context->DrawGeometry(
                        ch.geometry.Get(), brush.Get(), sourceWidth
                    );
                    if (needClip) {
                        context->PopAxisAlignedClip();
                    }
                    continue;
                }
                if (!charUsesGroupedGlowAt(charIndex)) {
                    continue;
                }
                ID2D1Geometry *geometry = charGeometryAt(charIndex);
                if (geometry == nullptr) {
                    continue;
                }
                bool needClip = false;
                if (useUtopiaTransition) {
                    const N3WipePhase phase = wipePhaseAt(line->chars, charIndex);
                    if ((phase == N3WipePhase::Before && after)
                        || (phase == N3WipePhase::After && !after)) {
                        continue;
                    }
                    needClip = phase == N3WipePhase::Wiping;
                } else {
                    if (rtl
                        ? ((after && wipeEdge >= ch.right)
                            || (!after && wipeEdge <= ch.left))
                        : ((after && wipeEdge <= ch.left)
                            || (!after && wipeEdge >= ch.right))) {
                        continue;
                    }
                    needClip = !mainWipeComplete;
                }
                brush->SetOpacity(
                    globalOpacity * characterOpacityAt(charIndex)
                );
                if (needClip) {
                    D2D1_RECT_F clip{};
                    if (useUtopiaTransition) {
                        const auto [animatedBounds, animatedEdge] =
                            utopiaCharWipe(charIndex);
                        clip = after
                            ? D2D1::RectF(
                                animatedBounds.left - pad,
                                fullWipeClipTop,
                                animatedEdge,
                                fullWipeClipBottom
                            )
                            : D2D1::RectF(
                                animatedEdge,
                                fullWipeClipTop,
                                animatedBounds.right + pad,
                                fullWipeClipBottom
                            );
                    } else {
                        clip = rtl
                            ? (after
                                ? D2D1::RectF(
                                    wipeEdge, fullWipeClipTop,
                                    ch.right + pad, fullWipeClipBottom
                                )
                                : D2D1::RectF(
                                    ch.left - pad, fullWipeClipTop,
                                    wipeEdge, fullWipeClipBottom
                                ))
                            : (after
                                ? D2D1::RectF(
                                    ch.left - pad, fullWipeClipTop,
                                    wipeEdge, fullWipeClipBottom
                                )
                                : D2D1::RectF(
                                    wipeEdge, fullWipeClipTop,
                                    ch.right + pad, fullWipeClipBottom
                                ));
                    }
                    pushAxisAlignedClip(
                        clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                context->DrawGeometry(geometry, brush.Get(), sourceWidth);
                if (needClip) {
                    context->PopAxisAlignedClip();
                }
            }
            context->PopAxisAlignedClip();
            endDrawMeasured(
                "ID2D1DeviceContext::EndDraw(inline glow source)",
                frameDiagnostics.endDrawInlineGlowSourceMs,
                frameDiagnostics.endDrawInlineGlowSourceCount
            );
            layer.blur->SetInput(0, layer.source);
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
                appendInlineGlowLayer(styleIndex, false, -1);
                appendInlineGlowLayer(styleIndex, true, -1);
            }
        }
        if (spinDirection != 0 || dripDirection != 0 || useUtopiaTransition) {
            for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
                if (!charTransformedAt(charIndex)
                    || charUsesGroupedGlowAt(charIndex)) {
                    continue;
                }
                const int styleIndex = line->chars[charIndex].styleIndex;
                appendInlineGlowLayer(styleIndex, false, static_cast<int>(charIndex));
                appendInlineGlowLayer(styleIndex, true, static_cast<int>(charIndex));
            }
        }
        frameDiagnostics.glowMs += elapsedMs(glowStart);

        context->SetTarget(targetBitmap);
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        if (!renderedAnyLine) {
            context->Clear(D2D1::ColorF(0.0f, 0.0f));
        }
        for (RubyGlowLayer &layer : rubyGlowLayers) {
            context->SetTransform(
                withViewport(
                    layer.hasTransform
                        ? layer.transform
                        : D2D1::Matrix3x2F::Identity()
                )
            );
            const D2D1_RECT_F imageRect = D2D1::RectF(
                layer.sourceRect.left + dx, layer.sourceRect.top + dy,
                layer.sourceRect.right + dx, layer.sourceRect.bottom + dy
            );
            for (int sigma : layer.sigmas) {
                checkHr(
                    layer.blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(ruby StandardDeviation)",
                    device_
                );
                context->DrawImage(
                    layer.blur,
                    D2D1::Point2F(imageRect.left, imageRect.top),
                    layer.effectRect
                );
            }
        }
        for (InlineGlowLayer &layer : inlineGlowLayers) {
            context->SetTransform(
                withViewport(
                    layer.hasTransform
                        ? layer.transform
                        : D2D1::Matrix3x2F::Identity()
                )
            );
            const D2D1_RECT_F imageRect = D2D1::RectF(
                layer.sourceRect.left + dx, layer.sourceRect.top + dy,
                layer.sourceRect.right + dx, layer.sourceRect.bottom + dy
            );
            for (int sigma : layer.sigmas) {
                checkHr(
                    layer.blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(inline StandardDeviation)",
                    device_
                );
                context->DrawImage(
                    layer.blur,
                    D2D1::Point2F(imageRect.left, imageRect.top),
                    layer.effectRect
                );
            }
        }
        if (!glowSigmas.empty()) {
            const D2D1_RECT_F glowImageRect = D2D1::RectF(
                glowSourceRect.left + dx, glowSourceRect.top + dy,
                glowSourceRect.right + dx, glowSourceRect.bottom + dy
            );
            for (int sigma : glowSigmas) {
                context->SetTransform(lineViewportTransform);
                checkHr(
                    blur->SetValue(
                        D2D1_GAUSSIANBLUR_PROP_STANDARD_DEVIATION,
                        static_cast<float>(sigma)
                    ),
                    "ID2D1Effect::SetValue(StandardDeviation)",
                    device_
                );
                context->DrawImage(
                    blur,
                    D2D1::Point2F(glowImageRect.left, glowImageRect.top),
                    glowEffectRect
                );
            }
        }
        context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(dx, dy)));

        auto drawShadowSilhouette = [&](ID2D1Geometry *geometry,
                                        ID2D1Geometry *animatedOuterGeometry,
                                        ID2D1Brush *brush,
                                        float strokeWidth, float stroke2Width,
                                        bool transformed) {
            const float outerWidth = stroke2Width > 0.0f
                ? std::max(strokeWidth, 0.0f) + stroke2Width
                : std::max(strokeWidth, 0.0f);
            if (transformed && impl_->dynamicDirectStrokeEnabled) {
                if (outerWidth > 0.0f) {
                    context->DrawGeometry(geometry, brush, outerWidth);
                }
            } else if (transformed && animatedOuterGeometry != nullptr) {
                context->FillGeometry(animatedOuterGeometry, brush);
            } else if (outerWidth > 0.0f) {
                context->DrawGeometry(geometry, brush, outerWidth);
            }
            context->FillGeometry(geometry, brush);
        };
        auto drawLineShadowPhase = [&](bool after) {
            if (line->hasInlineStyles || hasCharacterTransition) {
                for (std::size_t charIndex = 0; charIndex < line->chars.size(); ++charIndex) {
                    const Impl::CachedChar &ch = line->chars[charIndex];
                    ID2D1Geometry *geometry = charGeometryAt(charIndex);
                    if (geometry == nullptr) {
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
                    brush->SetOpacity(globalOpacity * characterOpacityAt(charIndex));
                    const CharacterAnimationState animationState =
                        characterAnimationAt(charIndex);
                    const D2D1_MATRIX_3X2_F charMatrix = animationState.matrix;
                    const float shadowX = animationState.transformed
                        ? charStyle.shadowOffsetX * charMatrix._11
                            + charStyle.shadowOffsetY * charMatrix._21
                        : charStyle.shadowOffsetX;
                    const float shadowY = animationState.transformed
                        ? charStyle.shadowOffsetX * charMatrix._12
                            + charStyle.shadowOffsetY * charMatrix._22
                        : charStyle.shadowOffsetY;
                    context->SetTransform(withViewport(
                        D2D1::Matrix3x2F::Translation(
                            dx + shadowX,
                            dy + shadowY
                        )
                    ));
                    bool pushedAfterClip = false;
                    const bool wipeComplete = useUtopiaTransition
                        ? charWipeComplete(charIndex)
                        : mainWipeComplete;
                    if (after && !wipeComplete) {
                        if (useUtopiaTransition) {
                            const auto [animatedBounds, animatedEdge]
                                = utopiaCharWipe(charIndex);
                            if (animatedEdge <= animatedBounds.left) {
                                continue;
                            }
                            const float pad = std::max(
                                charStyle.strokeWidth + charStyle.stroke2Width,
                                2.0f
                            ) + 4.0f;
                            pushAxisAlignedClip(
                                D2D1::RectF(
                                    animatedBounds.left - pad - shadowX,
                                    fullWipeClipTop,
                                    animatedEdge - shadowX,
                                    fullWipeClipBottom
                                ),
                                D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                            );
                            pushedAfterClip = true;
                        } else {
                            pushAxisAlignedClip(
                                style.vertical
                                    ? D2D1::RectF(
                                        afterClip.left,
                                        afterClip.top - charStyle.shadowOffsetY,
                                        afterClip.right,
                                        afterClip.bottom - charStyle.shadowOffsetY
                                    )
                                    : D2D1::RectF(
                                        afterClip.left - charStyle.shadowOffsetX,
                                        afterClip.top,
                                        afterClip.right - charStyle.shadowOffsetX,
                                        afterClip.bottom
                                    ),
                                D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                            );
                            pushedAfterClip = true;
                        }
                    }
                    ID2D1Geometry *animatedOuter = charStyle.stroke2Width > 0.0f
                        ? stroke2GeometryAt(charIndex)
                        : strokeGeometryAt(charIndex);
                    drawShadowSilhouette(
                        geometry, animatedOuter, brush.Get(),
                        charStyle.strokeWidth, charStyle.stroke2Width,
                        charTransformedAt(charIndex)
                    );
                    if (pushedAfterClip) {
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
                context->SetTransform(withViewport(
                    D2D1::Matrix3x2F::Translation(
                        dx + style.shadowOffsetX,
                        dy + style.shadowOffsetY
                    )
                ));
                const bool pushedAfterClip = after && !mainWipeComplete;
                if (pushedAfterClip) {
                    pushAxisAlignedClip(
                        style.vertical
                            ? D2D1::RectF(
                                afterClip.left,
                                afterClip.top - style.shadowOffsetY,
                                afterClip.right,
                                afterClip.bottom - style.shadowOffsetY
                            )
                            : D2D1::RectF(
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
                        geometry.Get(), nullptr, brush.Get(),
                        style.strokeWidth, style.stroke2Width, false
                    );
                }
                if (pushedAfterClip) {
                    context->PopAxisAlignedClip();
                }
            }
            context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(dx, dy)));
        };
        drawLineShadowPhase(false);
        if (hasAfterWipe) {
            drawLineShadowPhase(true);
        }

        for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            if (rubyStyle.rubyDecorationKind != "shadow") {
                continue;
            }
            const float edge = rubyWipeEdgeAt(ruby);
            const bool complete = rubyWipeComplete(ruby);
            auto drawRubyShadowPhase = [&](bool after) {
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrushAt(
                    after
                        ? rubyStyle.rubyAfterDecorPaint
                        : rubyStyle.rubyBeforeDecorPaint,
                    rubyPaintBounds(
                        after
                            ? rubyStyle.rubyAfterDecorPaint
                            : rubyStyle.rubyBeforeDecorPaint,
                        ruby.fillBounds,
                        ruby.horizontalFillBounds
                    ),
                    after ? rubyStyle.rubyAfterDecor : rubyStyle.rubyBeforeDecor,
                    rubyStyle.rubyShadowOffsetX,
                    rubyStyle.rubyShadowOffsetY
                );
                const bool pushedStaticClip = after
                    && !useUtopiaTransition
                    && !complete;
                if (pushedStaticClip) {
                    const float pad = std::max(
                        rubyStyle.rubyStrokeWidth + rubyStyle.rubyStroke2Width,
                        2.0f
                    ) + 4.0f;
                    pushAxisAlignedClip(
                        style.vertical
                            ? D2D1::RectF(
                                ruby.bounds.left - pad,
                                ruby.bounds.top - pad - rubyStyle.rubyShadowOffsetY,
                                ruby.bounds.right + pad,
                                edge - rubyStyle.rubyShadowOffsetY
                            )
                            : (rtl
                                ? D2D1::RectF(
                                    edge - rubyStyle.rubyShadowOffsetX,
                                    fullWipeClipTop,
                                    ruby.bounds.right + pad
                                        - rubyStyle.rubyShadowOffsetX,
                                    fullWipeClipBottom
                                )
                                : D2D1::RectF(
                                    ruby.bounds.left - pad
                                        - rubyStyle.rubyShadowOffsetX,
                                    fullWipeClipTop,
                                    edge - rubyStyle.rubyShadowOffsetX,
                                    fullWipeClipBottom
                                )),
                        D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                for (std::size_t geometryIndex = 0;
                     geometryIndex < ruby.geometries.size(); ++geometryIndex) {
                    ID2D1Geometry *geometry = rubyGeometryAt(
                        rubyIndex, geometryIndex
                    );
                    if (geometry == nullptr) {
                        continue;
                    }
                    const CharacterAnimationState animationState =
                        rubyUnitAnimationAt(ruby, geometryIndex);
                    brush->SetOpacity(globalOpacity * animationState.opacity);
                    const float shadowX = animationState.transformed
                        ? rubyStyle.rubyShadowOffsetX * animationState.matrix._11
                            + rubyStyle.rubyShadowOffsetY * animationState.matrix._21
                        : rubyStyle.rubyShadowOffsetX;
                    const float shadowY = animationState.transformed
                        ? rubyStyle.rubyShadowOffsetX * animationState.matrix._12
                            + rubyStyle.rubyShadowOffsetY * animationState.matrix._22
                        : rubyStyle.rubyShadowOffsetY;
                    context->SetTransform(withViewport(
                        D2D1::Matrix3x2F::Translation(
                            dx + shadowX, dy + shadowY
                        )
                    ));
                    bool pushedUtopiaClip = false;
                    const bool unitComplete = useUtopiaTransition
                        ? rubyUnitWipeComplete(ruby, geometryIndex)
                        : complete;
                    if (after && useUtopiaTransition && !unitComplete) {
                        const auto [animatedBounds, animatedEdge] =
                            utopiaRubyUnitWipe(
                                ruby, rubyIndex, geometryIndex, rubyStyle
                            );
                        if (animatedEdge <= animatedBounds.left) {
                            continue;
                        }
                        const float pad = std::max(
                            rubyStyle.rubyStrokeWidth
                                + rubyStyle.rubyStroke2Width,
                            2.0f
                        ) + 4.0f;
                        pushAxisAlignedClip(
                            D2D1::RectF(
                                animatedBounds.left - pad - shadowX,
                                fullWipeClipTop,
                                animatedEdge - shadowX,
                                fullWipeClipBottom
                            ),
                            D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                        );
                        pushedUtopiaClip = true;
                    }
                    ID2D1Geometry *animatedOuter = rubyStyle.rubyStroke2Width > 0.0f
                        ? rubyStroke2GeometryAt(rubyIndex, geometryIndex)
                        : rubyStrokeGeometryAt(rubyIndex, geometryIndex);
                    drawShadowSilhouette(
                        geometry, animatedOuter, brush.Get(),
                        rubyStyle.rubyStrokeWidth,
                        rubyStyle.rubyStroke2Width,
                        rubyUnitTransformed(ruby, geometryIndex)
                    );
                    if (pushedUtopiaClip) {
                        context->PopAxisAlignedClip();
                    }
                }
                if (pushedStaticClip) {
                    context->PopAxisAlignedClip();
                }
            };
            drawRubyShadowPhase(false);
            if (rubyPhaseVisible(ruby, edge, true)) {
                drawRubyShadowPhase(true);
            }
        }
        context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(dx, dy)));

        const bool hasVisualOverlap = !style.vertical && !rtl && std::adjacent_find(
            line->chars.begin(), line->chars.end(),
            [&](const Impl::CachedChar &current,
                const Impl::CachedChar &following) {
                return current.right >= following.left;
            }
        ) != line->chars.end();
        const bool useN3PhaseOrdering = hasVisualOverlap
            || line->hasInlineStyles || hasCharacterTransition;
        if (!useN3PhaseOrdering) {
            const auto drawLegacyStack = [&](bool after, ID2D1Brush *fill,
                                             ID2D1Brush *stroke,
                                             ID2D1Brush *stroke2) {
                const bool realizationEligible =
                    std::max(style.strokeWidth, 0.0f)
                    >= Impl::realizationStrokeThreshold;
                if (style.stroke2Width > 0.0f) {
                    for (const Impl::CachedChar &ch : line->chars) {
                        if (!ch.geometry) {
                            continue;
                        }
                        strokeWithRealization(
                            ch.stroke2Realization.Get(), ch.geometry.Get(), stroke2,
                            std::max(0.0f, style.strokeWidth)
                                + style.stroke2Width,
                            true,
                            realizationEligible
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
                            fillStrokeWithRealization(
                                ch.protectedStrokeRealization.Get(),
                                ch.protectedStrokeGeometry.Get(), stroke, false,
                                realizationEligible
                            );
                        } else {
                            strokeWithRealization(
                                ch.strokeRealization.Get(), ch.geometry.Get(), stroke,
                                style.strokeWidth, false, realizationEligible
                            );
                        }
                    }
                }
                for (const Impl::CachedChar &ch : line->chars) {
                    if (ch.geometry) {
                        fillWithRealization(
                            ch.fillRealization.Get(), ch.geometry.Get(), fill,
                            realizationEligible
                        );
                    }
                }
                for (std::size_t index = 0; index < line->chars.size(); ++index) {
                    drawBitmapGuidePart(index, after);
                }
            };
            drawLegacyStack(
                false, beforeFill.Get(), beforeStroke.Get(), beforeStroke2.Get()
            );
            if (hasAfterWipe) {
                if (!mainWipeComplete) {
                    pushAxisAlignedClip(
                        afterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                drawLegacyStack(
                    true, afterFill.Get(), afterStroke.Get(), afterStroke2.Get()
                );
                if (!mainWipeComplete) {
                    context->PopAxisAlignedClip();
                }
            }
        } else {
        // All three N3 layers share this character classification and clip.
        const auto pushMainWipeClip = [&](std::size_t charIndex, bool after) {
            const Impl::CachedChar &ch = line->chars[charIndex];
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            float edge = delegatedWipeCoordinateAt(line->chars, charIndex);
            D2D1_RECT_F bounds = line->bounds;
            if (ch.bitmapGuide.has_value()) {
                bounds = ch.bitmapRect;
            } else if (useUtopiaTransition || charTransformedAt(charIndex)) {
                const auto animated = utopiaCharWipe(charIndex);
                bounds = animated.first;
                edge = animated.second;
            }
            const float pad = std::max(
                charStyle.strokeWidth + charStyle.stroke2Width, 2.0f
            ) + 4.0f;
            D2D1_RECT_F clip{};
            if (style.vertical) {
                clip = after
                    ? D2D1::RectF(
                        bounds.left - pad, bounds.top - pad,
                        bounds.right + pad, edge
                    )
                    : D2D1::RectF(
                        bounds.left - pad, edge,
                        bounds.right + pad, bounds.bottom + pad
                    );
            } else if (rtl) {
                clip = after
                    ? D2D1::RectF(
                        edge, fullWipeClipTop,
                        bounds.right + pad, fullWipeClipBottom
                    )
                    : D2D1::RectF(
                        bounds.left - pad, fullWipeClipTop,
                        edge, fullWipeClipBottom
                    );
            } else {
                clip = after
                    ? D2D1::RectF(
                        bounds.left - pad, fullWipeClipTop,
                        edge, fullWipeClipBottom
                    )
                    : D2D1::RectF(
                        edge, fullWipeClipTop,
                        bounds.right + pad, fullWipeClipBottom
                    );
            }
            pushAxisAlignedClip(
                clip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
            );
        };
        const auto drawMainLayerPart = [&](std::size_t charIndex,
                                           bool after, int layer) {
            const Impl::CachedChar &ch = line->chars[charIndex];
            if (ch.bitmapGuide.has_value()) {
                if (layer == 2) {
                    drawBitmapGuidePart(charIndex, after);
                }
                return;
            }
            ID2D1Geometry *geometry = charGeometryAt(charIndex);
            if (geometry == nullptr) {
                return;
            }
            const TextStyle &charStyle = ch.styleIndex >= 0
                && ch.styleIndex < static_cast<int>(scene.charStyles.size())
                ? scene.charStyles[static_cast<std::size_t>(ch.styleIndex)]
                : style;
            const PaintStyle &paint = layer == 0
                ? (after ? charStyle.afterStroke2Paint : charStyle.beforeStroke2Paint)
                : (layer == 1
                    ? (after ? charStyle.afterStrokePaint : charStyle.beforeStrokePaint)
                    : (after ? charStyle.afterFillPaint : charStyle.beforeFillPaint));
            const RgbaColor &color = layer == 0
                ? (after ? charStyle.afterStroke2 : charStyle.beforeStroke2)
                : (layer == 1
                    ? (after ? charStyle.afterStroke : charStyle.beforeStroke)
                    : (after ? charStyle.afterFill : charStyle.beforeFill));
            // The common (non-role) Utopia path used to recreate one D2D
            // brush for every character and every visual layer.  A long line
            // therefore allocated dozens of COM brush objects per frame even
            // though all characters share the six line-level brushes above.
            // Reuse those brushes; role-specific brushes still retain their
            // own paint definition and are handled by the fallback below.
            Microsoft::WRL::ComPtr<ID2D1Brush> ownedBrush;
            ID2D1Brush *brush = nullptr;
            if (ch.styleIndex < 0) {
                brush = layer == 0
                    ? (after ? afterStroke2.Get() : beforeStroke2.Get())
                    : (layer == 1
                        ? (after ? afterStroke.Get() : beforeStroke.Get())
                        : (after ? afterFill.Get() : beforeFill.Get()));
            } else {
                ownedBrush = paintBrush(paint, line->fillBounds, color);
                brush = ownedBrush.Get();
            }
            brush->SetOpacity(globalOpacity * characterOpacityAt(charIndex));
            // Only glyphs whose matrix is non-identity need the pre-expanded
            // stroke geometry.  Treating the whole Utopia line as animated
            // makes every settled glyph fill a complex widened path on every
            // frame, which is dramatically slower than Direct2D's native
            // DrawGeometry stroke on long real-world lines.
            const bool animated = charTransformedAt(charIndex);
            const bool realizationEligible = !animated
                && std::max(charStyle.strokeWidth, 0.0f)
                    >= Impl::realizationStrokeThreshold;
            if (layer == 0) {
                if (charStyle.stroke2Width <= 0.0f) {
                    return;
                }
                ID2D1Geometry *animatedStroke2 = stroke2GeometryAt(charIndex);
                if (animated && impl_->dynamicDirectStrokeEnabled) {
                    drawCountedStroke(
                        geometry, brush,
                        std::max(0.0f, charStyle.strokeWidth)
                            + charStyle.stroke2Width,
                        true
                    );
                } else if (animated && animatedStroke2 != nullptr) {
                    fillCountedStroke(animatedStroke2, brush, true);
                } else {
                    strokeWithRealization(
                        ch.stroke2Realization.Get(), geometry, brush,
                        std::max(0.0f, charStyle.strokeWidth)
                            + charStyle.stroke2Width,
                        true,
                        realizationEligible
                    );
                }
                return;
            }
            if (layer == 1) {
                if (charStyle.strokeWidth <= 0.0f) {
                    return;
                }
                const bool protect = paintNeedsBodyProtection(
                    after ? charStyle.afterFillPaint : charStyle.beforeFillPaint
                );
                ID2D1Geometry *protectedGeometry = protectedGeometryAt(charIndex);
                ID2D1Geometry *animatedStroke = strokeGeometryAt(charIndex);
                if (animated && !protect
                    && impl_->dynamicDirectStrokeEnabled) {
                    drawCountedStroke(
                        geometry, brush, charStyle.strokeWidth, false
                    );
                } else if (animated && !protect && animatedStroke != nullptr) {
                    fillCountedStroke(animatedStroke, brush, false);
                } else if (protect && protectedGeometry != nullptr) {
                    if (animated) {
                        fillCountedStroke(protectedGeometry, brush, false);
                    } else {
                        fillStrokeWithRealization(
                            ch.protectedStrokeRealization.Get(),
                            protectedGeometry, brush, false,
                            realizationEligible
                        );
                    }
                } else {
                    strokeWithRealization(
                        ch.strokeRealization.Get(), geometry, brush,
                        charStyle.strokeWidth, false, realizationEligible
                    );
                }
                return;
            }
            fillWithRealization(
                ch.fillRealization.Get(), geometry, brush, realizationEligible
            );
        };
        const auto drawMainLayer = [&](int layer) {
            const auto drawPhasePart = [&](std::size_t charIndex,
                                           N3WipePhase phase) {
                if (layer == 2 && bitmapGuideNoWipe(line->chars[charIndex])) {
                    drawMainLayerPart(charIndex, false, layer);
                    return;
                }
                if (phase != N3WipePhase::Wiping) {
                    drawMainLayerPart(
                        charIndex, phase == N3WipePhase::After, layer
                    );
                    return;
                }
                pushMainWipeClip(charIndex, false);
                drawMainLayerPart(charIndex, false, layer);
                context->PopAxisAlignedClip();
                pushMainWipeClip(charIndex, true);
                drawMainLayerPart(charIndex, true, layer);
                context->PopAxisAlignedClip();
            };
            for (std::size_t reverse = line->chars.size(); reverse > 0; --reverse) {
                const std::size_t index = reverse - 1;
                if (wipePhaseAt(line->chars, index) == N3WipePhase::Before) {
                    drawPhasePart(index, N3WipePhase::Before);
                }
            }
            for (std::size_t index = 0; index < line->chars.size(); ++index) {
                if (wipePhaseAt(line->chars, index) == N3WipePhase::After) {
                    drawPhasePart(index, N3WipePhase::After);
                }
            }
            for (std::size_t index = 0; index < line->chars.size(); ++index) {
                if (wipePhaseAt(line->chars, index) == N3WipePhase::Wiping) {
                    drawPhasePart(index, N3WipePhase::Wiping);
                }
            }
        };
        // N3 performs phase ordering independently for edge2, edge and body.
        drawMainLayer(0);
        drawMainLayer(1);
        drawMainLayer(2);
        }
        auto drawRubyStack = [&](std::size_t rubyIndex, const Impl::CachedRuby &ruby, bool after) {
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            Microsoft::WRL::ComPtr<ID2D1Brush> fill = paintBrush(
                after ? rubyStyle.rubyAfterFillPaint : rubyStyle.rubyBeforeFillPaint,
                rubyPaintBounds(
                    after ? rubyStyle.rubyAfterFillPaint : rubyStyle.rubyBeforeFillPaint,
                    ruby.fillBounds,
                    ruby.horizontalFillBounds
                ),
                after ? rubyStyle.rubyAfterFill : rubyStyle.rubyBeforeFill
            );
            Microsoft::WRL::ComPtr<ID2D1Brush> stroke = paintBrush(
                after ? rubyStyle.rubyAfterStrokePaint : rubyStyle.rubyBeforeStrokePaint,
                rubyPaintBounds(
                    after ? rubyStyle.rubyAfterStrokePaint : rubyStyle.rubyBeforeStrokePaint,
                    ruby.fillBounds,
                    ruby.horizontalFillBounds
                ),
                after ? rubyStyle.rubyAfterStroke : rubyStyle.rubyBeforeStroke
            );
            Microsoft::WRL::ComPtr<ID2D1Brush> stroke2 = paintBrush(
                after
                    ? rubyStyle.rubyAfterStroke2Paint
                    : rubyStyle.rubyBeforeStroke2Paint,
                rubyPaintBounds(
                    after
                        ? rubyStyle.rubyAfterStroke2Paint
                        : rubyStyle.rubyBeforeStroke2Paint,
                    ruby.fillBounds,
                    ruby.horizontalFillBounds
                ),
                after ? rubyStyle.rubyAfterStroke2 : rubyStyle.rubyBeforeStroke2
            );
            for (std::size_t index = 0; index < ruby.geometries.size(); ++index) {
                ID2D1Geometry *geometry = rubyGeometryAt(rubyIndex, index);
                if (geometry == nullptr) {
                    continue;
                }
                const float rubyOpacity = globalOpacity
                    * rubyUnitOpacityAt(ruby, index);
                fill->SetOpacity(rubyOpacity);
                stroke->SetOpacity(rubyOpacity);
                stroke2->SetOpacity(rubyOpacity);
                bool pushedUtopiaClip = false;
                if (after
                    && useUtopiaTransition
                    && !rubyUnitWipeComplete(ruby, index)) {
                    const auto [animatedBounds, animatedEdge] = utopiaRubyUnitWipe(
                        ruby, rubyIndex, index, rubyStyle
                    );
                    if (animatedEdge <= animatedBounds.left) {
                        continue;
                    }
                    const float pad = std::max(
                        rubyStyle.rubyStrokeWidth + rubyStyle.rubyStroke2Width,
                        2.0f
                    ) + 4.0f;
                    pushAxisAlignedClip(
                        D2D1::RectF(
                            animatedBounds.left - pad,
                            fullWipeClipTop,
                            animatedEdge,
                            fullWipeClipBottom
                        ),
                        D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                    pushedUtopiaClip = true;
                }
                ID2D1Geometry *animatedStroke2 = rubyStroke2GeometryAt(
                    rubyIndex, index
                );
                const Impl::CachedChar *rubyChar = index < ruby.chars.size()
                    ? &ruby.chars[index]
                    : nullptr;
                const bool rubyTransformed = rubyUnitTransformed(ruby, index);
                const bool realizationEligible = !rubyTransformed
                    && std::max(rubyStyle.rubyStrokeWidth, 0.0f)
                        >= Impl::realizationStrokeThreshold;
                if (rubyStyle.rubyStroke2Width > 0.0f) {
                    if (rubyTransformed && impl_->dynamicDirectStrokeEnabled) {
                        drawCountedStroke(
                            geometry, stroke2.Get(),
                            std::max(0.0f, rubyStyle.rubyStrokeWidth)
                                + rubyStyle.rubyStroke2Width,
                            true
                        );
                    } else if (rubyTransformed && animatedStroke2 != nullptr) {
                        fillCountedStroke(
                            animatedStroke2, stroke2.Get(), true
                        );
                    } else {
                        strokeWithRealization(
                            rubyChar != nullptr
                                ? rubyChar->stroke2Realization.Get()
                                : nullptr,
                            geometry, stroke2.Get(),
                            std::max(0.0f, rubyStyle.rubyStrokeWidth)
                                + rubyStyle.rubyStroke2Width,
                            true,
                            realizationEligible
                        );
                    }
                }
                if (rubyStyle.rubyStrokeWidth > 0.0f) {
                    const bool protect = paintNeedsBodyProtection(
                        after
                            ? rubyStyle.rubyAfterFillPaint
                            : rubyStyle.rubyBeforeFillPaint
                    );
                    ID2D1Geometry *protectedGeometry = rubyProtectedGeometryAt(
                        rubyIndex, index
                    );
                    ID2D1Geometry *animatedStroke = rubyStrokeGeometryAt(
                        rubyIndex, index
                    );
                    if (rubyTransformed && !protect
                        && impl_->dynamicDirectStrokeEnabled) {
                        drawCountedStroke(
                            geometry, stroke.Get(),
                            rubyStyle.rubyStrokeWidth, false
                        );
                    } else if (rubyTransformed && !protect
                        && animatedStroke != nullptr) {
                        fillCountedStroke(
                            animatedStroke, stroke.Get(), false
                        );
                    } else if (protect && protectedGeometry != nullptr) {
                        if (rubyTransformed) {
                            fillCountedStroke(
                                protectedGeometry, stroke.Get(), false
                            );
                        } else {
                            fillStrokeWithRealization(
                                rubyChar != nullptr
                                    ? rubyChar->protectedStrokeRealization.Get()
                                    : nullptr,
                                protectedGeometry, stroke.Get(), false,
                                realizationEligible
                            );
                        }
                    } else {
                        strokeWithRealization(
                            rubyChar != nullptr
                                ? rubyChar->strokeRealization.Get()
                                : nullptr,
                            geometry, stroke.Get(), rubyStyle.rubyStrokeWidth,
                            false,
                            realizationEligible
                        );
                    }
                }
                fillWithRealization(
                    rubyChar != nullptr ? rubyChar->fillRealization.Get() : nullptr,
                    geometry, fill.Get(), realizationEligible
                );
                if (pushedUtopiaClip) {
                    context->PopAxisAlignedClip();
                }
            }
        };
        for (std::size_t rubyIndex = 0; rubyIndex < line->rubies.size(); ++rubyIndex) {
            const Impl::CachedRuby &ruby = line->rubies[rubyIndex];
            const TextStyle &rubyStyle = rubyStyleFor(ruby.styleIndex);
            const float rubyWipeEdge = rubyWipeEdgeAt(ruby);
            const bool rubyComplete = rubyWipeComplete(ruby);
            const float rubyPad = std::max(
                rubyStyle.rubyStrokeWidth + rubyStyle.rubyStroke2Width, 2.0f
            ) + 4.0f;
            const D2D1_RECT_F rubyAfterClip = style.vertical
                ? D2D1::RectF(
                    ruby.bounds.left - rubyPad,
                    ruby.bounds.top - rubyPad,
                    ruby.bounds.right + rubyPad,
                    rubyWipeEdge
                )
                : (rtl
                    ? D2D1::RectF(
                        rubyWipeEdge,
                        fullWipeClipTop,
                        ruby.bounds.right + rubyPad,
                        fullWipeClipBottom
                    )
                    : D2D1::RectF(
                        ruby.bounds.left - rubyPad,
                        fullWipeClipTop,
                        rubyWipeEdge,
                        fullWipeClipBottom
                    ));
            drawRubyStack(rubyIndex, ruby, false);
            if (rubyPhaseVisible(ruby, rubyWipeEdge, true)) {
                if (!useUtopiaTransition && !rubyComplete) {
                    pushAxisAlignedClip(
                        rubyAfterClip, D2D1_ANTIALIAS_MODE_PER_PRIMITIVE
                    );
                }
                drawRubyStack(rubyIndex, ruby, true);
                if (!useUtopiaTransition && !rubyComplete) {
                    context->PopAxisAlignedClip();
                }
            }
        }
        if (signalState.visible
            && style.litOpacity > 0.0f
            && signalState.opacity > 0.0f) {
            context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(signalDx, dy)));
            auto signalBrush = [&](const RgbaColor &color) {
                PaintStyle paint;
                paint.mode = "solid";
                paint.color = color;
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                    paint, line->fillBounds, color
                );
                brush->SetOpacity(
                    std::clamp(style.litOpacity * signalState.opacity, 0.0f, 1.0f)
                );
                return brush;
            };
            auto normalFill = signalBrush(style.volumeFill);
            auto normalStroke = signalBrush(style.volumeStroke);
            auto overlayFill = signalBrush(style.volumeOverlayFill);
            auto overlayStroke = signalBrush(style.volumeOverlayStroke);
            const float groupX = style.volumeOffsetX - signalGeometry.groupWidth;
            auto drawColumn = [&](int index, bool overlay) {
                const float left = groupX + signalGeometry.strokeExtent
                    + static_cast<float>(index) * signalGeometry.pitch;
                const float top = signalGroupY + signalGeometry.strokeExtent
                    + signalGeometry.alignBaseShift
                    + static_cast<float>(index) * signalGeometry.alignDeltaShift;
                const float height = std::max(
                    signalGeometry.frontHeight
                        + static_cast<float>(index) * signalGeometry.heightDelta,
                    1.0f
                );
                const D2D1_ROUNDED_RECT rect = D2D1::RoundedRect(
                    D2D1::RectF(
                        left,
                        top,
                        left + signalGeometry.columnWidth,
                        top + height
                    ),
                    std::max(
                        std::min(signalGeometry.columnWidth, height) * 0.22f,
                        1.0f
                    ),
                    std::max(
                        std::min(signalGeometry.columnWidth, height) * 0.22f,
                        1.0f
                    )
                );
                ID2D1Brush *fill = overlay ? overlayFill.Get() : normalFill.Get();
                ID2D1Brush *stroke = overlay ? overlayStroke.Get() : normalStroke.Get();
                context->FillRoundedRectangle(rect, fill);
                const RgbaColor &strokeColor = overlay
                    ? style.volumeOverlayStroke
                    : style.volumeStroke;
                if (style.litStrokeWidth > 0.0f && strokeColor.alpha > 0) {
                    context->DrawRoundedRectangle(
                        rect, stroke, style.litStrokeWidth
                    );
                }
            };
            for (int index = signalState.activeIndex + 1;
                 index < signalGeometry.count;
                 ++index) {
                drawColumn(index, false);
            }
            for (int index = 0; index <= signalState.activeIndex; ++index) {
                drawColumn(index, true);
            }
        }
        if (shapeState.visible
            && shapeState.activeIndex >= 0
            && style.litOpacity > 0.0f) {
            context->SetTransform(withViewport(D2D1::Matrix3x2F::Translation(signalDx, dy)));
            auto shapeBrush = [&](const RgbaColor &color, float opacity) {
                PaintStyle paint;
                paint.mode = "solid";
                paint.color = color;
                Microsoft::WRL::ComPtr<ID2D1Brush> brush = paintBrush(
                    paint, line->fillBounds, color
                );
                brush->SetOpacity(std::clamp(opacity, 0.0f, 1.0f));
                return brush;
            };
            auto drawRawShape = [&](const D2D1_RECT_F &rect,
                                    const RgbaColor &fillColor,
                                    const RgbaColor &strokeColor,
                                    float strokeWidth,
                                    float opacity) {
                auto fill = shapeBrush(fillColor, opacity);
                auto stroke = shapeBrush(strokeColor, opacity);
                if (style.litStyle == "square") {
                    context->FillRectangle(rect, fill.Get());
                    if (strokeWidth > 0.0f && strokeColor.alpha > 0) {
                        context->DrawRectangle(rect, stroke.Get(), strokeWidth);
                    }
                } else if (style.litStyle == "rounded") {
                    const float radius = std::max(
                        (rect.right - rect.left) * 0.22f, 1.0f
                    );
                    const D2D1_ROUNDED_RECT rounded = D2D1::RoundedRect(
                        rect, radius, radius
                    );
                    context->FillRoundedRectangle(rounded, fill.Get());
                    if (strokeWidth > 0.0f && strokeColor.alpha > 0) {
                        context->DrawRoundedRectangle(
                            rounded, stroke.Get(), strokeWidth
                        );
                    }
                } else {
                    const D2D1_ELLIPSE ellipse = D2D1::Ellipse(
                        D2D1::Point2F(
                            (rect.left + rect.right) * 0.5f,
                            (rect.top + rect.bottom) * 0.5f
                        ),
                        (rect.right - rect.left) * 0.5f,
                        (rect.bottom - rect.top) * 0.5f
                    );
                    context->FillEllipse(ellipse, fill.Get());
                    if (strokeWidth > 0.0f && strokeColor.alpha > 0) {
                        context->DrawEllipse(ellipse, stroke.Get(), strokeWidth);
                    }
                }
            };
            for (int index = 0; index <= shapeState.activeIndex; ++index) {
                const bool active = index == shapeState.activeIndex;
                const float itemOpacity = style.litOpacity
                    * (active ? shapeState.activeOpacity : 1.0f);
                const float itemX = style.litOffsetX
                    + static_cast<float>(index)
                        * (shapeGeometry.size * 1.5f + shapeGeometry.tracking)
                    + (active ? shapeState.dx : 0.0f);
                const float itemY = shapeGroupY + (active ? shapeState.dy : 0.0f);
                const D2D1_RECT_F rect = D2D1::RectF(
                    itemX,
                    itemY,
                    itemX + shapeGeometry.size,
                    itemY + shapeGeometry.size
                );
                if (style.litShadow) {
                    const float shadowOffset = std::max(
                        shapeGeometry.size * 0.08f, 1.0f
                    );
                    drawRawShape(
                        D2D1::RectF(
                            rect.left + shadowOffset,
                            rect.top + shadowOffset,
                            rect.right + shadowOffset,
                            rect.bottom + shadowOffset
                        ),
                        RgbaColor{0, 0, 0, 89},
                        RgbaColor{0, 0, 0, 0},
                        0.0f,
                        itemOpacity
                    );
                }
                if (style.litStrokeSoften > 0.0f
                    && style.litStrokeWidth > 0.0f) {
                    RgbaColor softStroke = style.litStroke;
                    softStroke.alpha = 71;
                    drawRawShape(
                        rect,
                        style.litFill,
                        softStroke,
                        style.litStrokeWidth + style.litStrokeSoften,
                        itemOpacity
                    );
                }
                drawRawShape(
                    rect,
                    style.litFill,
                    style.litStroke,
                    style.litStrokeWidth,
                    itemOpacity
                );
                if (active && style.litEdgeBrightness > 0.0f) {
                    const float inset = shapeGeometry.size * 0.18f;
                    const D2D1_ELLIPSE highlight = D2D1::Ellipse(
                        D2D1::Point2F(
                            rect.left + inset + shapeGeometry.size * 0.16f,
                            rect.top + inset + shapeGeometry.size * 0.16f
                        ),
                        shapeGeometry.size * 0.16f,
                        shapeGeometry.size * 0.16f
                    );
                    auto brush = shapeBrush(
                        RgbaColor{255, 255, 255, 255},
                        itemOpacity * std::min(
                            style.litEdgeBrightness * 0.55f, 1.0f
                        )
                    );
                    context->FillEllipse(highlight, brush.Get());
                }
            }
        }
        endDrawMeasured(
            "ID2D1DeviceContext::EndDraw(frame layers)",
            frameDiagnostics.endDrawFrameLayersMs,
            frameDiagnostics.endDrawFrameLayersCount
        );
        renderedAnyLine = true;
        if (blur != nullptr) {
            blur->SetInput(0, nullptr);
        }
        for (RubyGlowLayer &layer : rubyGlowLayers) {
            layer.blur->SetInput(0, nullptr);
        }
        for (InlineGlowLayer &layer : inlineGlowLayers) {
            layer.blur->SetInput(0, nullptr);
        }
        // This line's composite is flushed; scratches can serve the next line.
        // Bursts (e.g. whole-line utopia outros) may allocate past the cap;
        // those extra entries are released here so steady state keeps at most
        // the cap's worth of scene-sized scratch memory resident.
        constexpr std::size_t kGlowPoolCap = 8;
        impl_->glowScratchInUse = 0;
        impl_->glowEffectInUse = 0;
        if (impl_->glowScratchPool.size() > kGlowPoolCap) {
            impl_->glowScratchPool.resize(kGlowPoolCap);
        }
        if (impl_->glowEffectPool.size() > kGlowPoolCap) {
            impl_->glowEffectPool.resize(kGlowPoolCap);
        }
      }
    }

    if (!renderedAnyLine) {
        context->SetTarget(targetBitmap);
        context->SetTransform(D2D1::Matrix3x2F::Identity());
        context->BeginDraw();
        context->Clear(D2D1::ColorF(0.0f, 0.0f));
        endDrawMeasured(
            "ID2D1DeviceContext::EndDraw(empty frame)",
            frameDiagnostics.endDrawEmptyFrameMs,
            frameDiagnostics.endDrawEmptyFrameCount
        );
    }

    context->SetTarget(nullptr);
    context->SetTransform(D2D1::Matrix3x2F::Identity());
    const double renderMs = elapsedMs(renderStart);

    if (!readback) {
        ProbeResult result;
        result.renderMs = renderMs;
        result.surface.width = scene.width;
        result.surface.height = scene.height;
        result.surface.stride = scene.width * 4;
        result.surface.pixelFormat = PixelFormat::Bgra8888Premultiplied;
        finalizeDiagnostics(result);
        return result;
    }

    const auto readbackStart = Clock::now();
    ID3D11Texture2D *stagingTexture = impl_->frameStagingTexture.Get();
    const int fixedCropTop = std::clamp(
        scene.exportCropTop, 0, std::max(scene.height - 1, 0)
    );
    const int fixedCropHeight = std::clamp(
        scene.exportCropHeight,
        0,
        std::max(scene.height - fixedCropTop, 0)
    );
    const bool fixedCrop = !compactBands
        && fixedCropHeight > 0
        && (fixedCropTop > 0 || fixedCropHeight < scene.height);
    std::vector<std::pair<int, int>> fixedBands;
    int fixedBandsHeight = 0;
    if (!compactBands) {
        fixedBands.reserve(scene.exportBands.size());
        for (const auto &[rawTop, rawHeight] : scene.exportBands) {
            const int top = std::clamp(rawTop, 0, std::max(scene.height - 1, 0));
            const int height = std::clamp(
                rawHeight, 0, std::max(scene.height - top, 0)
            );
            if (height > 0) {
                fixedBands.emplace_back(top, height);
                fixedBandsHeight += height;
            }
        }
    }
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
        finalizeDiagnostics(result);
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
    } else if (!fixedBands.empty()) {
        int packedTop = 0;
        for (const auto &[top, height] : fixedBands) {
            D3D11_BOX sourceBox{};
            sourceBox.left = 0;
            sourceBox.right = static_cast<UINT>(scene.width);
            sourceBox.top = static_cast<UINT>(top);
            sourceBox.bottom = static_cast<UINT>(top + height);
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
            packedTop += height;
        }
    } else if (fixedCrop) {
        D3D11_BOX sourceBox{};
        sourceBox.left = 0;
        sourceBox.right = static_cast<UINT>(scene.width);
        sourceBox.top = static_cast<UINT>(fixedCropTop);
        sourceBox.bottom = static_cast<UINT>(fixedCropTop + fixedCropHeight);
        sourceBox.front = 0;
        sourceBox.back = 1;
        device_.d3dContext()->CopySubresourceRegion(
            stagingTexture,
            0,
            0,
            0,
            0,
            targetTexture,
            0,
            &sourceBox
        );
    } else {
        device_.d3dContext()->CopyResource(stagingTexture, targetTexture);
    }
    D3D11_MAPPED_SUBRESOURCE mapped{};
    const auto gpuWaitStart = Clock::now();
    checkHr(
        device_.d3dContext()->Map(stagingTexture, 0, D3D11_MAP_READ, 0, &mapped),
        "ID3D11DeviceContext::Map(frame)",
        device_
    );
    frameDiagnostics.gpuWaitMs = elapsedMs(gpuWaitStart);

    ProbeResult result;
    result.renderMs = renderMs;
    result.surface.width = scene.width;
    result.surface.height = !fixedBands.empty()
        ? fixedBandsHeight
        : (fixedCrop ? fixedCropHeight : scene.height);
    result.surface.stride = scene.width * 4;
    result.surface.pixelFormat = PixelFormat::Bgra8888Premultiplied;
    int packedHeight = !fixedBands.empty()
        ? fixedBandsHeight
        : (fixedCrop ? fixedCropHeight : scene.height);
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
    const auto readbackCopyStart = Clock::now();
    for (int y = 0; y < packedHeight; ++y) {
        const auto *source = static_cast<const std::uint8_t *>(mapped.pData)
            + static_cast<std::size_t>(mapped.RowPitch) * y;
        auto *destination = result.surface.bytes.data()
            + static_cast<std::size_t>(result.surface.stride) * y;
        std::memcpy(destination, source, static_cast<std::size_t>(result.surface.stride));
    }
    frameDiagnostics.readbackCopyMs = elapsedMs(readbackCopyStart);
    device_.d3dContext()->Unmap(stagingTexture, 0);
    result.readbackMs = elapsedMs(readbackStart);
    finalizeDiagnostics(result);
    return result;
}

NativePreviewResult Direct2DGpuBackend::presentFrame(
    int tMs,
    const NativePreviewTarget &target
) {
    const auto rendered = renderFrameInternal(tMs, false, false);
    return previewSurface_.present(
        device_.d3dDevice(),
        device_.d3dContext(),
        impl_->frameTargetTexture.Get(),
        rendered.renderMs,
        target
    );
}

void Direct2DGpuBackend::closeNativePreview() {
    previewSurface_.close();
}

}  // namespace krok::subtitle::native
