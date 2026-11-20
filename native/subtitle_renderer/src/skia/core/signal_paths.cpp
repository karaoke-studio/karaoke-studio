#include "signal_paths.h"
#include "include/core/SkPathBuilder.h"
#include "clipper2/clipper.h"
#include <array>
#include <cmath>
#include <numbers>
#include <stdexcept>

namespace subtitle::skia {
namespace {
SkPath star(float size) {
    std::array<SkPoint, 10> points;
    const float outer = size * .5f;
    constexpr auto pi = std::numbers::pi_v<float>;
    for (std::size_t index = 0; index < points.size(); ++index) {
        const float radius = index % 2 == 0 ? outer : outer * .45f;
        const float angle = -pi * .5f + float(index) * pi / 5.f;
        points[index] = {outer + radius * std::cos(angle), outer + radius * std::sin(angle)};
    }
    return SkPath::Polygon(points, true, SkPathFillType::kWinding);
}

using Clipper2Lib::PathD;
using Clipper2Lib::PathsD;
using Clipper2Lib::PointD;

PathD head(double x, double y, double rx, double ry) {
    PathD out;
    constexpr int segments = 96;
    for (int index = 0; index < segments; ++index) {
        const double angle = 2 * std::numbers::pi * index / segments;
        out.emplace_back(x + rx * std::cos(angle), y + ry * std::sin(angle));
    }
    return out;
}

PathD stem(double x, double right, double y, double bottom) {
    return {{x, y}, {right, y}, {right, bottom}, {x, bottom}};
}

void sampleCurve(PathD& out, PointD b, PointD c, PointD d) {
    const auto a = out.back();
    constexpr int segments = 32;
    for (int i = 1; i <= segments; ++i) {
        const double t = double(i) / segments;
        const double u = 1 - t;
        out.emplace_back(a.x*u*u*u + 3*b.x*u*u*t + 3*c.x*u*t*t + d.x*t*t*t,
                         a.y*u*u*u + 3*b.y*u*u*t + 3*c.y*u*t*t + d.y*t*t*t);
    }
}

PathD flag(double y) {
    PathD out{{.532, y}};
    sampleCurve(out, {.747, y + .06}, {.827, y + .20}, {.707, y + .36});
    out.emplace_back(.632, y + .285);
    sampleCurve(out, {.722, y + .18}, {.647, y + .09}, {.532, y + .055});
    return out;
}

SkPath unite(PathsD parts, float size) {
    // Union before stroking so touching pieces never expose internal seams.
    const auto contours = Clipper2Lib::Union(parts, Clipper2Lib::FillRule::NonZero, 6);
    if (contours.empty()) throw std::runtime_error("Skia 音符灯轮廓合并失败");
    SkPathBuilder out;
    for (const auto& contour : contours) {
        out.moveTo(float(contour.front().x * size), float(contour.front().y * size));
        for (std::size_t index = 1; index < contour.size(); ++index)
            out.lineTo(float(contour[index].x * size), float(contour[index].y * size));
        out.close();
    }
    return out.detach();
}

SkPath note(bool doubleFlag, float size) {
    PathsD parts{head(.34, .78, .20, .12), stem(.468, .532, .10, .80), flag(.10)};
    if (doubleFlag) parts.push_back(flag(.26));
    return unite(std::move(parts), size);
}

SkPath notePair(float size) {
    return unite({head(.24, .74, .17, .11), stem(.342, .402, .16, .76),
                  head(.62, .60, .17, .11), stem(.722, .782, .04, .62),
                  {{.342, .08}, {.782, .02}, {.782, .12}, {.342, .18}}}, size);
}
}

std::optional<SignalPath> signalPath(std::string_view shape, float size) {
    if (shape == "star") return SignalPath{star(size), {size * .5f, size * .36f}, size * .10f};
    if (shape == "note8" || shape == "note16")
        return SignalPath{note(shape == "note16", size), {size * .29f, size * .745f}, size * .06f};
    if (shape == "notepair")
        return SignalPath{notePair(size), {size * .20f, size * .705f}, size * .055f};
    return std::nullopt;
}
}
