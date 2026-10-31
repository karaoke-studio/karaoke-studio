#include "d2d_geometry_resources.h"
#include "d2d_stroke_outline.h"

#include "../../diagnostics/long_call_watchdog.h"

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

namespace {

Microsoft::WRL::ComPtr<ID2D1PathGeometry> openPath(
    ID2D1Factory1 *factory,
    const char *operation,
    const D2DDevice &device
) {
    Microsoft::WRL::ComPtr<ID2D1PathGeometry> path;
    checkHr(
        factory->CreatePathGeometry(path.ReleaseAndGetAddressOf()),
        operation,
        device
    );
    return path;
}

}  // namespace

Microsoft::WRL::ComPtr<ID2D1PathGeometry> lampShapeGeometry(
    ID2D1Factory1 *factory,
    const std::string &litStyle,
    float size,
    const D2DDevice &device
) {
    // 边长比例口径镜像 Painter 的 _lit_star_note_path：五角星（外接半径
    // 0.5×边长、内切 0.45×外接、顶点朝上）/ 八分音符（符头椭圆 + 符干 +
    // 符旗曲线）。音符三个部件先各自成几何，再布尔并集成单一轮廓——
    // 描边只画外形，不在符干与符头的接缝处画内线（与 QPainter
    // united() 同口径）。
    const float side = std::max(size, 1.0f);
    constexpr float pi = 3.14159265358979323846f;

    if (litStyle == "star") {
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> star = openPath(
            factory, "ID2D1Factory::CreatePathGeometry(lamp star)", device
        );
        Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
        checkHr(
            star->Open(sink.ReleaseAndGetAddressOf()),
            "ID2D1PathGeometry::Open(lamp star)",
            device
        );
        sink->SetFillMode(D2D1_FILL_MODE_WINDING);
        const float outer = side * 0.5f;
        const float inner = outer * 0.45f;
        bool first = true;
        for (int step = 0; step < 10; ++step) {
            const float radius = (step % 2 == 0) ? outer : inner;
            const float angle =
                -pi / 2.0f + static_cast<float>(step) * pi / 5.0f;
            const D2D1_POINT_2F point = D2D1::Point2F(
                side * 0.5f + radius * std::cos(angle),
                side * 0.5f + radius * std::sin(angle)
            );
            if (first) {
                sink->BeginFigure(point, D2D1_FIGURE_BEGIN_FILLED);
                first = false;
            } else {
                sink->AddLine(point);
            }
        }
        sink->EndFigure(D2D1_FIGURE_END_CLOSED);
        checkHr(
            sink->Close(), "ID2D1GeometrySink::Close(lamp star)", device
        );
        return star;
    }

    const auto unify =
        [&](Microsoft::WRL::ComPtr<ID2D1Geometry> a,
            Microsoft::WRL::ComPtr<ID2D1Geometry> b,
            const char *operation) -> Microsoft::WRL::ComPtr<ID2D1PathGeometry> {
            Microsoft::WRL::ComPtr<ID2D1PathGeometry> merged = openPath(
                factory, "ID2D1Factory::CreatePathGeometry(lamp note union)", device
            );
            Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
            checkHr(
                merged->Open(sink.ReleaseAndGetAddressOf()),
                "ID2D1PathGeometry::Open(lamp note union)",
                device
            );
            checkHr(
                a->CombineWithGeometry(
                    b.Get(),
                    D2D1_COMBINE_MODE_UNION,
                    nullptr,
                    D2D1_DEFAULT_FLATTENING_TOLERANCE,
                    sink.Get()
                ),
                operation,
                device
            );
            checkHr(
                sink->Close(), "ID2D1GeometrySink::Close(lamp note union)", device
            );
            return merged;
        };
    const auto ellipsePart = [&](float cx, float cy, float rx, float ry) {
        Microsoft::WRL::ComPtr<ID2D1EllipseGeometry> ellipse;
        checkHr(
            factory->CreateEllipseGeometry(
                D2D1::Ellipse(
                    D2D1::Point2F(side * cx, side * cy),
                    side * rx,
                    side * ry
                ),
                ellipse.ReleaseAndGetAddressOf()
            ),
            "ID2D1Factory::CreateEllipseGeometry(lamp note head)",
            device
        );
        return Microsoft::WRL::ComPtr<ID2D1Geometry>(ellipse);
    };
    const auto rectPart = [&](float x, float y, float w, float h) {
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> part = openPath(
            factory, "ID2D1Factory::CreatePathGeometry(lamp note stem)", device
        );
        Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
        checkHr(
            part->Open(sink.ReleaseAndGetAddressOf()),
            "ID2D1PathGeometry::Open(lamp note stem)",
            device
        );
        sink->SetFillMode(D2D1_FILL_MODE_WINDING);
        sink->BeginFigure(
            D2D1::Point2F(side * x, side * y), D2D1_FIGURE_BEGIN_FILLED
        );
        sink->AddLine(D2D1::Point2F(side * (x + w), side * y));
        sink->AddLine(D2D1::Point2F(side * (x + w), side * (y + h)));
        sink->AddLine(D2D1::Point2F(side * x, side * (y + h)));
        sink->EndFigure(D2D1_FIGURE_END_CLOSED);
        checkHr(
            sink->Close(), "ID2D1GeometrySink::Close(lamp note stem)", device
        );
        return Microsoft::WRL::ComPtr<ID2D1Geometry>(part);
    };
    const auto flagPart = [&](float y0) {
        // 符旗：附着在符干顶部、向右下弯的三角旗面（十六分音符的第二面
        // 旗 = 同形下移 0.16×边长，镜像 Painter 的 _flag(y0)）。旗面左缘
        // 与符干右缘（0.532×边长）重合。
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> part = openPath(
            factory, "ID2D1Factory::CreatePathGeometry(lamp note flag)", device
        );
        Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
        checkHr(
            part->Open(sink.ReleaseAndGetAddressOf()),
            "ID2D1PathGeometry::Open(lamp note flag)",
            device
        );
        sink->SetFillMode(D2D1_FILL_MODE_WINDING);
        sink->BeginFigure(
            D2D1::Point2F(side * 0.532f, side * y0),
            D2D1_FIGURE_BEGIN_FILLED
        );
        sink->AddBezier(D2D1::BezierSegment(
            D2D1::Point2F(side * 0.747f, side * (y0 + 0.06f)),
            D2D1::Point2F(side * 0.827f, side * (y0 + 0.20f)),
            D2D1::Point2F(side * 0.707f, side * (y0 + 0.36f))
        ));
        sink->AddLine(D2D1::Point2F(side * 0.632f, side * (y0 + 0.285f)));
        sink->AddBezier(D2D1::BezierSegment(
            D2D1::Point2F(side * 0.722f, side * (y0 + 0.18f)),
            D2D1::Point2F(side * 0.647f, side * (y0 + 0.09f)),
            D2D1::Point2F(side * 0.532f, side * (y0 + 0.055f))
        ));
        sink->EndFigure(D2D1_FIGURE_END_CLOSED);
        checkHr(
            sink->Close(), "ID2D1GeometrySink::Close(lamp note flag)", device
        );
        return Microsoft::WRL::ComPtr<ID2D1Geometry>(part);
    };

    if (litStyle == "note8" || litStyle == "note16") {
        // 单音符：符头 + 符干 + 符旗（十六分两面），布尔并集单一轮廓。
        // 符干右缘收在符头右极点（0.54×边长）之内、下端沉过符头中心线
        // （镜像 Painter：竖直符干与水平椭圆只在极点相切，右下角矩形直角
        // 会刺出符头轮廓）。
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> note =
            unify(ellipsePart(0.34f, 0.78f, 0.20f, 0.12f),
                  rectPart(0.468f, 0.10f, 0.064f, 0.70f),
                  "ID2D1Geometry::CombineWithGeometry(lamp note head+stem)");
        note = unify(note, flagPart(0.10f),
                     "ID2D1Geometry::CombineWithGeometry(lamp note +flag)");
        if (litStyle == "note16") {
            note = unify(note, flagPart(0.26f),
                         "ID2D1Geometry::CombineWithGeometry(lamp note +flag2)");
        }
        return note;
    }

    // 组合（notepair）：左符头低、右符头高，双符干接顶部斜横梁。符干
    // 同样右缘收进各自符头右极点内、下端沉过符头中心线（镜像 Painter）。
    Microsoft::WRL::ComPtr<ID2D1PathGeometry> pair =
        unify(ellipsePart(0.24f, 0.74f, 0.17f, 0.11f),
              rectPart(0.342f, 0.16f, 0.06f, 0.60f),
              "ID2D1Geometry::CombineWithGeometry(lamp pair head+stem)");
    pair = unify(pair, ellipsePart(0.62f, 0.60f, 0.17f, 0.11f),
                 "ID2D1Geometry::CombineWithGeometry(lamp pair +head2)");
    pair = unify(pair, rectPart(0.722f, 0.04f, 0.06f, 0.58f),
                 "ID2D1Geometry::CombineWithGeometry(lamp pair +stem2)");
    {
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> beam = openPath(
            factory, "ID2D1Factory::CreatePathGeometry(lamp pair beam)", device
        );
        Microsoft::WRL::ComPtr<ID2D1GeometrySink> sink;
        checkHr(
            beam->Open(sink.ReleaseAndGetAddressOf()),
            "ID2D1PathGeometry::Open(lamp pair beam)",
            device
        );
        sink->SetFillMode(D2D1_FILL_MODE_WINDING);
        sink->BeginFigure(
            D2D1::Point2F(side * 0.342f, side * 0.08f),
            D2D1_FIGURE_BEGIN_FILLED
        );
        sink->AddLine(D2D1::Point2F(side * 0.782f, side * 0.02f));
        sink->AddLine(D2D1::Point2F(side * 0.782f, side * 0.12f));
        sink->AddLine(D2D1::Point2F(side * 0.342f, side * 0.18f));
        sink->EndFigure(D2D1_FIGURE_END_CLOSED);
        checkHr(
            sink->Close(), "ID2D1GeometrySink::Close(lamp pair beam)", device
        );
        pair = unify(pair, beam,
                     "ID2D1Geometry::CombineWithGeometry(lamp pair +beam)");
    }
    return pair;
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
    const D2DDevice &device,
    std::shared_ptr<StrokeOutlineCache> *cache
) {
    if (body == nullptr || width <= 0.0f) {
        return {};
    }
    // Normalize filled topology before offsetting. Direct2D Widen on dense
    // outlines can take minutes; the polygon path also preserves overlapping
    // winding contours and real counters instead of treating them as XOR holes.
    if (auto outside = strokeOutlineGeometry(device, body, width, true, 0.0f, cache)) {
        Microsoft::WRL::ComPtr<ID2D1Geometry> result;
        checkHr(outside.As(&result), "Query polygon outside stroke", device);
        return result;
    }
    // Widen+Combine 是单体不可中断调用，密集路径上单次可达数十秒且常跑
    // 在主线程 configure 里（此时逐段/空闲两层心跳都被它阻塞）——登记
    // 预算由喂狗线程代喂（见 diagnostics::LongCallScope）。
    krok::subtitle::native::diagnostics::LongCallScope longCall(
        "d2d-protected", 240.0
    );
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
    // D2D's 0.25 DIP default makes Widen + EXCLUDE dominate configure time
    // for complex Japanese outlines. Half-pixel flattening keeps the outside
    // mask within the renderer's antialiasing fringe while substantially
    // reducing the number of curve segments fed into the boolean operation.
    constexpr float protectionFlatteningTolerance = 0.5f;
    checkHr(
        body->Widen(
            width, strokeStyle.Get(), nullptr,
            protectionFlatteningTolerance, widenedSink.Get()
        ),
        "Widen protected body stroke",
        device
    );
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
            body, D2D1_COMBINE_MODE_EXCLUDE, nullptr,
            protectionFlatteningTolerance, outsideSink.Get()
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
    // 同 outsideStrokeGeometry：单体 Widen 调用按预算登记代喂。
    krok::subtitle::native::diagnostics::LongCallScope longCall(
        "d2d-widen", 240.0
    );
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
