#include "d2d_stroke_outline.h"
#include "d2d_runtime_support.h"

#include "clipper2/clipper.h"

#include <algorithm>
#include <cstdio>
#include <functional>
#include <vector>
#include <map>

namespace krok::subtitle::native::direct2d {

struct StrokeOutlineCache {
    Microsoft::WRL::ComPtr<ID2D1Geometry> source;
    Clipper2Lib::PathsD filled;
    std::map<float, Clipper2Lib::PathsD> expanded;
};

namespace {

// 平化收集 sink：把 path 流转成闭合点环（贝塞尔自适应细分）。
class FlattenSink final : public ID2D1GeometrySink {
public:
    std::vector<std::vector<D2D1_POINT_2F>> rings;

    STDMETHOD_(ULONG, AddRef)() override { return 2; }
    STDMETHOD_(ULONG, Release)() override { return 1; }
    STDMETHOD(QueryInterface)(REFIID riid, void **out) override {
        if (riid == __uuidof(IUnknown)
            || riid == __uuidof(ID2D1SimplifiedGeometrySink)
            || riid == __uuidof(ID2D1GeometrySink)) {
            *out = static_cast<ID2D1GeometrySink *>(this);
            return S_OK;
        }
        *out = nullptr;
        return E_NOINTERFACE;
    }
    D2D1_FILL_MODE fillMode = D2D1_FILL_MODE_WINDING;
    void STDMETHODCALLTYPE SetFillMode(D2D1_FILL_MODE value) override { fillMode = value; }
    void STDMETHODCALLTYPE SetSegmentFlags(D2D1_PATH_SEGMENT) override {}
    void STDMETHODCALLTYPE BeginFigure(
        D2D1_POINT_2F start, D2D1_FIGURE_BEGIN
    ) override {
        if (ring_.size() >= 3) rings.push_back(std::move(ring_));
        ring_.clear();
        ring_.push_back(start);
    }
    void STDMETHODCALLTYPE EndFigure(D2D1_FIGURE_END) override {
        if (ring_.size() >= 3) rings.push_back(std::move(ring_));
        ring_.clear();
    }
    void STDMETHODCALLTYPE AddLines(
        const D2D1_POINT_2F *points, UINT count
    ) override {
        ring_.insert(ring_.end(), points, points + count);
    }
    void STDMETHODCALLTYPE AddBeziers(
        const D2D1_BEZIER_SEGMENT *beziers, UINT count
    ) override {
        for (UINT i = 0; i < count; ++i) {
            flattenBezier(
                ring_.empty() ? D2D1_POINT_2F{0, 0} : ring_.back(),
                beziers[i].point1, beziers[i].point2, beziers[i].point3
            );
        }
    }
    void STDMETHODCALLTYPE AddLine(D2D1_POINT_2F point) override {
        ring_.push_back(point);
    }
    void STDMETHODCALLTYPE AddBezier(const D2D1_BEZIER_SEGMENT *bezier) override {
        AddBeziers(bezier, 1);
    }
    void STDMETHODCALLTYPE AddQuadraticBezier(
        const D2D1_QUADRATIC_BEZIER_SEGMENT *bezier
    ) override {
        const D2D1_POINT_2F p0 =
            ring_.empty() ? D2D1_POINT_2F{0, 0} : ring_.back();
        // 二次 → 三次提升。
        const D2D1_POINT_2F c1{
            p0.x + 2.0f / 3.0f * (bezier->point1.x - p0.x),
            p0.y + 2.0f / 3.0f * (bezier->point1.y - p0.y),
        };
        const D2D1_POINT_2F c2{
            bezier->point2.x + 2.0f / 3.0f * (bezier->point1.x - bezier->point2.x),
            bezier->point2.y + 2.0f / 3.0f * (bezier->point1.y - bezier->point2.y),
        };
        flattenBezier(p0, c1, c2, bezier->point2);
    }
    void STDMETHODCALLTYPE AddQuadraticBeziers(
        const D2D1_QUADRATIC_BEZIER_SEGMENT *beziers, UINT count
    ) override {
        for (UINT i = 0; i < count; ++i) {
            AddQuadraticBezier(&beziers[i]);
        }
    }
    void STDMETHODCALLTYPE AddArc(const D2D1_ARC_SEGMENT *) override {
        // 矢量字形轮廓不含弧（SVG 弧已在导入期转贝塞尔）。
    }
    STDMETHOD(Close)() override {
        if (ring_.size() >= 3) rings.push_back(std::move(ring_));
        ring_.clear();
        return S_OK;
    }

private:
    // 自适应细分：flatness 0.05（烘焙基准空间）。
    void flattenBezier(
        D2D1_POINT_2F p0, D2D1_POINT_2F c1, D2D1_POINT_2F c2, D2D1_POINT_2F p3
    ) {
        std::function<void(D2D1_POINT_2F, D2D1_POINT_2F, D2D1_POINT_2F,
                           D2D1_POINT_2F, int)>
            rec = [&](D2D1_POINT_2F a0, D2D1_POINT_2F b1, D2D1_POINT_2F b2,
                      D2D1_POINT_2F b3, int depth) {
                if (depth > 24) {
                    ring_.push_back(b3);
                    return;
                }
                const float dx = b3.x - a0.x, dy = b3.y - a0.y;
                const float d1 = std::abs((b1.x - b3.x) * dy - (b1.y - b3.y) * dx);
                const float d2 = std::abs((b2.x - b3.x) * dy - (b2.y - b3.y) * dx);
                if (d1 + d2
                    < 0.05f * std::sqrt(dx * dx + dy * dy + 1e-9f)) {
                    ring_.push_back(b3);
                    return;
                }
                const auto mid = [](D2D1_POINT_2F u, D2D1_POINT_2F v) {
                    return D2D1_POINT_2F{(u.x + v.x) * 0.5f, (u.y + v.y) * 0.5f};
                };
                const D2D1_POINT_2F ab = mid(a0, b1), bc = mid(b1, b2),
                                     cd = mid(b2, b3);
                const D2D1_POINT_2F abc = mid(ab, bc), bcd = mid(bc, cd);
                rec(a0, ab, abc, mid(abc, bcd), depth + 1);
                rec(mid(abc, bcd), bcd, cd, b3, depth + 1);
            };
        rec(p0, c1, c2, p3, 0);
    }

    std::vector<D2D1_POINT_2F> ring_;
};

}  // namespace

Microsoft::WRL::ComPtr<ID2D1PathGeometry> strokeOutlineGeometry(
    const D2DDevice &device,
    ID2D1Geometry *source,
    float strokeWidth,
    bool outsideOnly,
    float innerStrokeWidth,
    std::shared_ptr<StrokeOutlineCache> *cache
) {
    if (source == nullptr || strokeWidth <= 0.0f) {
        return {};
    }
    auto prepared = cache ? *cache : nullptr;
    if (!prepared || prepared->source.Get() != source) {
        prepared = std::make_shared<StrokeOutlineCache>();
        prepared->source = source;
        Microsoft::WRL::ComPtr<ID2D1PathGeometry> sourcePath;
        if (FAILED(source->QueryInterface(
                IID_PPV_ARGS(sourcePath.ReleaseAndGetAddressOf())))) {
            return {};
        }
        FlattenSink sink;
        if (FAILED(sourcePath->Stream(&sink)) || sink.rings.empty()) {
            return {};
        }
        Clipper2Lib::PathsD subjects;
        for (const auto &ring : sink.rings) {
            if (ring.size() < 3) continue;
            Clipper2Lib::PathD path;
            path.reserve(ring.size());
            for (const auto &pt : ring) path.emplace_back(pt.x, pt.y);
            subjects.push_back(std::move(path));
        }
        if (subjects.empty()) {
            return {};
        }
        // precision=4（×10^4 取整）：precision=2 会把描摹轮廓 0.01~0.05px 的
        // 间距打碎成数千碎片环（2026-10 校验期实测）。
        // Direct2D's winding fill permits disjoint solid contours with opposite
        // orientations (e.g. mirrored wings). Polygon offsetting instead expects
        // consistent outer/hole orientations and otherwise shrinks one solid as
        // a hole. Resolve the filled topology first: NonZero union normalizes the
        // outer contours while retaining actual nested holes. Reversing every
        // negative contour would incorrectly fill those holes.
        prepared->filled = Clipper2Lib::Union(
            subjects, sink.fillMode == D2D1_FILL_MODE_ALTERNATE
                ? Clipper2Lib::FillRule::EvenOdd : Clipper2Lib::FillRule::NonZero, 4
        );
        if (cache) *cache = prepared;
    }
    const auto &filled = prepared->filled;
    const auto expanded = [&](float width) -> const Clipper2Lib::PathsD & {
        auto entry = prepared->expanded.find(width);
        if (entry == prepared->expanded.end()) {
            entry = prepared->expanded.emplace(width, Clipper2Lib::InflatePaths(
                filled, width * 0.5, Clipper2Lib::JoinType::Round,
                Clipper2Lib::EndType::Polygon, 2.0, 4, 0.02)).first;
        }
        return entry->second;
    };
    Clipper2Lib::PathsD inflated = expanded(strokeWidth);
    if (outsideOnly) {
        const auto &inner = innerStrokeWidth > 0.0f
            ? expanded(innerStrokeWidth) : filled;
        inflated = Clipper2Lib::Difference(
            inflated, inner, Clipper2Lib::FillRule::NonZero, 4);

    }
    if (inflated.empty()) {
        return {};
    }
    Microsoft::WRL::ComPtr<ID2D1PathGeometry> outline;
    if (FAILED(device.d2dFactory()->CreatePathGeometry(
            outline.ReleaseAndGetAddressOf()))) {
        return {};
    }
    Microsoft::WRL::ComPtr<ID2D1GeometrySink> out;
    if (FAILED(outline->Open(out.ReleaseAndGetAddressOf()))) {
        return {};
    }
    out->SetFillMode(D2D1_FILL_MODE_WINDING);
    size_t writtenFigures = 0;
    for (const auto &path : inflated) {
        if (path.size() < 3) continue;
        out->BeginFigure(
            D2D1::Point2F(
                static_cast<float>(path[0].x), static_cast<float>(path[0].y)),
            D2D1_FIGURE_BEGIN_FILLED
        );
        for (size_t i = 1; i < path.size(); ++i) {
            out->AddLine(D2D1::Point2F(
                static_cast<float>(path[i].x), static_cast<float>(path[i].y)
            ));
        }
        out->EndFigure(D2D1_FIGURE_END_CLOSED);
        ++writtenFigures;
    }
    if (FAILED(out->Close()) || writtenFigures == 0) {
        return {};
    }
    return outline;
}

}  // namespace krok::subtitle::native::direct2d
