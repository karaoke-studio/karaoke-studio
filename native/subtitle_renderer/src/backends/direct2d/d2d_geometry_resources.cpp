#include "d2d_geometry_resources.h"

#include <d2d1helper.h>

#include <algorithm>
#include <iomanip>
#include <sstream>

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

}  // namespace

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
            sink->AddBezier(D2D1::BezierSegment(point(0), point(2), point(4)));
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

}  // namespace krok::subtitle::native::direct2d
