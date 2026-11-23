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
    std::optional<std::size_t> utopiaExitOwner() const { return utopiaExitOwner_; }
    const SkRect& inkBounds() const { return bounds_; }
    bool spins() const { return spinPivot_.has_value(); }
    bool drips() const { return drip_; }
    bool lower() const { return lower_; }
    SkMatrix spinMatrix(const CharacterTransformState& owner) const;
    void draw(SkCanvas&, int timeMs, const SingingScale&, float originX, float opacity, float viewportHeight,
              const SkMatrix& pose = SkMatrix::I(), SkPoint ownerPivot = {},
              std::optional<float> entryScale = {}) const;
    void drawShadow(SkCanvas&, int timeMs, const SingingScale&, float originX) const;
    void drawUtopiaExit(SkCanvas&, int timeMs, int followingDone, const UtopiaTransition&,
                        float originX, float viewportHeight) const;
    void drawGlowSource(SkCanvas&, const PreparedGlow&, int timeMs, bool after,
                        const SingingScale&, float originX, float opacity,
                        const UtopiaTransition* exit = nullptr, int followingDone = 0,
                        std::optional<float> entryScale = {}) const;
    void compositeTransformedGlow(SkCanvas&, const PreparedGlow&, int timeMs, bool after, float opacity, const SkMatrix&) const;
    void drawScanline(SkCanvas&, const ScanlineBand&, int timeMs, const SingingScale&, float originX) const;
private:
    void drawSinging(SkCanvas&, int timeMs, const SingingScale&, float originX, bool afterVisible,
                     float opacity, float viewportHeight, const UtopiaTransition* exit = nullptr,
                     int followingDone = 0, std::optional<float> entryScale = {}) const;
    float wipeStart() const;
    float frontAt(int timeMs, float startEdge) const;
    sk_sp<SkShader> fillShader(const SkCanvas&, bool after) const;
    std::vector<ResolvedCell> cells_;
    StaticShadow shadow_;
    std::optional<std::size_t> glowOffset_;
    std::optional<std::size_t> fadeOwner_;
    std::optional<std::size_t> utopiaExitOwner_;
    SkRect bounds_;
    bool noWipe_ = false;
    bool vertical_ = false;
    bool rightToLeft_ = false;
    std::optional<SkPoint> spinPivot_;
    bool affine_ = false;
    bool lower_ = false;
    bool drip_ = false;
    std::array<sk_sp<SkShader>, 2> fillGradients_;
    std::array<ImageFill, 2> fillImages_;
    std::array<sk_sp<SkShader>, 2> outlineGradients_, outerOutlineGradients_;
    ScanlineGradients scanlineGradients_;
};
}
