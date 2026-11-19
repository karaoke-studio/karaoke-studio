#include "particle_ring.h"
#include "particle_outline.h"

#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPath.h"
#include "include/core/SkString.h"
#include "include/effects/SkRuntimeEffect.h"
#include <array>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
namespace {
struct RingEdges {
    std::array<SkV3, 32> planes{};
    int count = 0;
};

RingEdges ringEdges(float radius) {
    constexpr float kappa = .552284f;
    SkPathBuilder quarter;
    quarter.moveTo(radius, 0);
    particle_detail::flattenCubic(quarter, {radius, 0}, {radius, radius * kappa},
                                  {radius * kappa, radius}, {0, radius});
    const auto path = quarter.detach();
    SkPath::RawIter iter(path);
    SkPoint points[4];
    RingEdges result;
    iter.next(points);
    while (iter.next(points) == SkPath::kLine_Verb) {
        const SkPoint a = points[0], b = points[1];
        const float dx = b.x()-a.x(), dy = b.y()-a.y();
        const float length = std::hypot(dx, dy);
        if (length <= 0) continue;
        if (result.count == int(result.planes.size())) return {};
        const float nx = dy / length, ny = -dx / length;
        result.planes[result.count++] = {nx, ny, nx*a.x() + ny*a.y()};
    }
    return result;
}
}

std::optional<RingStroke> concentricRing(const SkPath& path) {
    SkPath::RawIter iter(path);
    SkPoint points[4];
    float radii[2];
    const auto matches = [](SkPoint actual, SkPoint expected) {
        return std::abs(actual.x() - expected.x()) < .01f && std::abs(actual.y() - expected.y()) < .01f;
    };
    for (int contour = 0; contour < 2; ++contour) {
        if (iter.next(points) != SkPath::kMove_Verb || points[0].x() <= 0 || std::abs(points[0].y()) >= .01f)
            return std::nullopt;
        const float radius = radii[contour] = points[0].x();
        const float y = contour == 0 ? radius : -radius;
        const SkPoint ends[]{{radius, 0}, {0, y}, {-radius, 0}, {0, -y}};
        for (int quarter = 0; quarter < 4; ++quarter) {
            if (iter.next(points) != SkPath::kCubic_Verb) return std::nullopt;
            const SkPoint a = ends[quarter], b = ends[(quarter + 1) % 4];
            constexpr float kappa = .552284f;
            if (!matches(points[1], {a.x() + b.x() * kappa, a.y() + b.y() * kappa})
                || !matches(points[2], {b.x() + a.x() * kappa, b.y() + a.y() * kappa})
                || !matches(points[3], b)) return std::nullopt;
        }
        if (iter.next(points) != SkPath::kClose_Verb) return std::nullopt;
    }
    if (iter.next(points) != SkPath::kDone_Verb || radii[0] <= radii[1]) return std::nullopt;
    return RingStroke{(radii[0] + radii[1]) * .5f, radii[0] - radii[1]};
}

void drawParticleRing(SkCanvas& canvas, SkPoint center, RingStroke ring, float scale, SkColor4f color) {
    // Integrate each edge across the pixel's square footprint using its
    // local tangent. Subtract the coverages so the two thin edges stay
    // coupled as the authored ring expands through subpixel radii.
    static const auto effect = [] {
        auto result = SkRuntimeEffect::MakeForShader(SkString(R"(
            uniform float2 center;
            uniform float2 radii;
            uniform half4 tint;
            uniform float3 outerPlanes[32];
            uniform float3 innerPlanes[32];
            uniform int outerCount;
            uniform int innerCount;

            float coverage(float distance, float major, float minor) {
                float x = clamp(distance + (major + minor) * 0.5, 0.0, major + minor);
                if (minor < 0.001) return clamp(x / major, 0.0, 1.0);
                float p = max(x - major, 0.0);
                float q = max(x - minor, 0.0);
                return clamp((x*x - p*p - q*q) / (2.0 * major * minor), 0.0, 1.0);
            }

            half4 main(float2 point) {
                float2 d = abs(point - center);
                float distance = length(d);
                float divisor = max(distance, 0.00001);
                float2 radial = d / divisor;
                float3 outside = float3(radial, radii.x - distance);
                float3 inside = float3(radial, radii.y - distance);
                if (outerCount > 0) outside.z = 1.0e7;
                if (innerCount > 0) inside.z = 1.0e7;
                for (int i = 0; i < 32; ++i) {
                    if (i >= outerCount && i >= innerCount) break;
                    if (i < outerCount) {
                        float3 plane = outerPlanes[i];
                        float gap = plane.z - dot(d, plane.xy);
                        if (gap < outside.z) outside = float3(plane.xy, gap);
                    }
                    if (i < innerCount) {
                        float3 plane = innerPlanes[i];
                        float gap = plane.z - dot(d, plane.xy);
                        if (gap < inside.z) inside = float3(plane.xy, gap);
                    }
                }
                float outer = coverage(outside.z, max(max(outside.x, outside.y), 0.00001), min(outside.x, outside.y));
                float inner = coverage(inside.z, max(max(inside.x, inside.y), 0.00001), min(inside.x, inside.y));
                return tint * half(max(outer - inner, 0.0));
            }
        )"));
        if (!result.effect) throw std::runtime_error(result.errorText.c_str());
        return result.effect;
    }();
    const float outer = (ring.radius + ring.width * .5f) * scale;
    const float inner = (ring.radius - ring.width * .5f) * scale;
    SkRuntimeShaderBuilder builder(effect);
    builder.uniform("center") = SkV2{center.x(), center.y()};
    builder.uniform("radii") = SkV2{outer, inner};
    const auto outerEdges = ringEdges(outer), innerEdges = ringEdges(inner);
    builder.uniform("outerCount") = outerEdges.count;
    builder.uniform("innerCount") = innerEdges.count;
    if (!builder.uniform("outerPlanes").set(outerEdges.planes.data(), int(outerEdges.planes.size()))
        || !builder.uniform("innerPlanes").set(innerEdges.planes.data(), int(innerEdges.planes.size())))
        throw std::runtime_error("Skia particle ring shader plane uniforms unavailable");
    builder.uniform("tint") = SkV4{color.fR * color.fA, color.fG * color.fA, color.fB * color.fA, color.fA};
    auto shader = builder.makeShader();
    if (!shader) throw std::runtime_error("Skia particle ring shader unavailable");
    SkPaint paint;
    paint.setShader(std::move(shader));
    const float extent = outer + 1.f;
    canvas.drawRect(SkRect::MakeLTRB(center.x()-extent, center.y()-extent,
                                    center.x()+extent, center.y()+extent), paint);
}
}
