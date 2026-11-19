#pragma once

#include "include/core/SkImageFilter.h"
#include "include/core/SkPath.h"
#include "include/core/SkSurface.h"
#include <cstdint>
#include <optional>
#include <vector>

class SkCanvas;

namespace subtitle::skia {
struct GlowState {
    int radius = 0;
    int concentration = 0;
    std::uint32_t color = 0;
};

struct StaticGlow {
    GlowState before, after;
    float outlineWidth = 0;
};

class PreparedGlow {
public:
    struct Source {
        sk_sp<SkSurface> surface;
        SkIPoint origin;
    };

    static std::vector<PreparedGlow> prepare(const StaticGlow&, bool mergeMatchingPhases = true);
    void appendCell(const SkPath&);
    bool visible(bool after) const;
    Source createSource(SkCanvas&) const;
    void drawCell(SkCanvas&, std::size_t index, bool after, std::optional<float> edge) const;
    void composite(SkCanvas&, const Source&) const;

private:
    PreparedGlow(const StaticGlow&, const GlowState&, std::optional<bool> phase);
    std::uint32_t before_, after_;
    int radius_;
    float strokeWidth_;
    std::optional<bool> phase_;
    std::vector<sk_sp<SkImageFilter>> filters_;
    std::vector<SkPath> cells_;
    SkRect ink_ = SkRect::MakeEmpty();
};
}
