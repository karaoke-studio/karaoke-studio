#pragma once

#include "include/core/SkImageFilter.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPath.h"
#include "include/core/SkShader.h"
#include "include/core/SkSurface.h"
#include "image_fill.h"
#include <array>
#include <cstdint>
#include <optional>
#include <vector>

class SkCanvas;

namespace subtitle::skia {
struct GlowState {
    int radius = 0;
    int concentration = 0;
    std::uint32_t color = 0;
    bool operator==(const GlowState&) const = default;
};

struct StaticGlow {
    GlowState before, after;
    float outlineWidth = 0;
    SkPaint::Join join = SkPaint::kRound_Join;
    bool operator==(const StaticGlow&) const = default;
};

using GlowGradients = std::array<sk_sp<SkShader>, 2>;
// Image paints draw the glow outline with the GPU's device-anchored bitmap brush.
using GlowImages = std::array<ImageFill, 2>;

class PreparedGlow {
public:
    struct Source {
        sk_sp<SkSurface> surface;
        SkIPoint origin;
        std::optional<SkRect> outputClip;
    };

    static std::vector<PreparedGlow> prepare(const StaticGlow&, bool mergeMatchingPhases = true);
    void prepareBytePasses();
    void appendCell(const SkPath&, const SkMatrix& peak = SkMatrix::I(), const GlowGradients& gradients = {},
                    const GlowImages& images = {});
    bool visible(bool after) const;
    SkRect sourceBounds() const;
    Source createSource(SkCanvas&, const SkRect& transitionBounds = SkRect::MakeEmpty(),
                        bool canonical = false) const;
    void drawCell(SkCanvas&, std::size_t index, bool after, std::optional<float> edge,
                  const SkPath* posed = nullptr, float opacity = 1.f, bool vertical = false, bool reverse = false,
                  bool sampleCoverage = false) const;
    void drawCellWithShader(SkCanvas&, std::size_t index, bool after, const sk_sp<SkShader>&, float opacity = 1.f) const;
    void composite(SkCanvas&, const Source&, bool bytePasses = false, bool cropRaster = false) const;
    void compositeCanonical(SkCanvas&, const Source&) const;
    void compositeTransformedCell(SkCanvas&, std::size_t index, bool after, std::optional<float> edge,
                                  const SkMatrix&, float opacity) const;

private:
    PreparedGlow(const StaticGlow&, const GlowState&, std::optional<bool> phase);
    SkPaint strokePaint() const;
    Source makeSource(SkCanvas&, const SkRect&, SkColorType format, bool raster = false) const;
    std::uint32_t before_, after_;
    int radius_;
    float strokeWidth_;
    SkPaint::Join join_;
    std::optional<bool> phase_;
    std::vector<sk_sp<SkImageFilter>> filters_;
    std::vector<sk_sp<SkImageFilter>> byteFilters_;
    struct Cell { SkPath path; GlowGradients gradients; GlowImages images; };
    std::vector<Cell> cells_;
    std::array<bool, 2> hasGradient_{};
    SkRect ink_ = SkRect::MakeEmpty();
    // Device transform of the line coordinates the current source is drawn in;
    // image brushes tile on the device grid like the destination fills.
    mutable SkMatrix imageDevice_ = SkMatrix::I();
    sk_sp<SkShader> cellShader(const Cell&, bool after) const;
};
}
