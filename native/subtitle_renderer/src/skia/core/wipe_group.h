#pragma once
#include "resolved_scene.h"
#include "include/core/SkRect.h"

class SkCanvas;
namespace subtitle::skia {
class PreparedWipeGroup {
public:
    explicit PreparedWipeGroup(const ResolvedRubyGroup&, bool noWipe = false, bool vertical = false,
                               std::optional<std::size_t> glowOffset = std::nullopt);
    std::optional<std::size_t> fadeOwner() const { return fadeOwner_; }
    bool spins() const { return spinPivot_.has_value(); }
    bool lower() const { return lower_; }
    SkMatrix spinMatrix(const CharacterTransformState& owner) const;
    void draw(SkCanvas&, int timeMs, const SingingScale&, float originX, float opacity, float viewportHeight,
              const SkMatrix& pose = SkMatrix::I(), SkPoint ownerPivot = {}) const;
    void drawShadow(SkCanvas&, int timeMs, const SingingScale&, float originX) const;
    void drawGlowSource(SkCanvas&, const PreparedGlow&, int timeMs, bool after,
                        const SingingScale&, float originX, float opacity) const;
    void compositeSpinGlow(SkCanvas&, const PreparedGlow&, int timeMs, bool after, float opacity, const SkMatrix&) const;
    void drawScanline(SkCanvas&, const ScanlineBand&, int timeMs, const SingingScale&, float originX) const;
private:
    void drawSinging(SkCanvas&, int timeMs, const SingingScale&, float originX, bool afterVisible,
                     float opacity, float viewportHeight) const;
    float frontAt(int timeMs, float startEdge) const;
    std::vector<ResolvedCell> cells_;
    StaticShadow shadow_;
    std::optional<std::size_t> glowOffset_;
    std::optional<std::size_t> fadeOwner_;
    SkRect bounds_;
    bool antialiasClip_ = false;
    bool noWipe_ = false;
    bool vertical_ = false;
    std::optional<SkPoint> spinPivot_;
    bool affine_ = false;
    bool lower_ = false;
};
}
