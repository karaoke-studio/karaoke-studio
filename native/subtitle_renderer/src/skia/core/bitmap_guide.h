#pragma once

#include "include/core/SkImage.h"
#include "include/core/SkRect.h"
#include "static_glow.h"
#include "static_shadow.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

class SkCanvas;

namespace subtitle::skia {
struct BitmapGuideFrame {
    sk_sp<SkImage> image;
    std::int64_t endMs = 0;
};
using BitmapGuideAnimation = std::vector<BitmapGuideFrame>;

// bitmap_guide_schema=1: a line-prefix image guide as the existing GPU draws
// it. The before/after images are decoded at the platform boundary; the box
// is final physical geometry. Glow follows the GPU: the image alpha tinted
// with the state's decoration paint, blurred at the image's own resolution
// (sigma divided by the box scale) once per concentration pass, and drawn
// with linear sampling over the box before the image itself.
struct BitmapGuide {
    sk_sp<SkImage> before, after; // after is empty for a guide that never wipes
    SkRect box = SkRect::MakeEmpty();
    StaticGlow glow;
    bool decorate = true;
    float clipPad = 0; // Wipe clip reach beyond the box (stroke widths + 4).
    std::array<std::shared_ptr<const BitmapGuideAnimation>, 2> animations;
    int animationAnchorMs = 0; // Looping media time measured from the source's guide anchor.
    bool useLineFront = false;
    std::array<ShadowState, 2> shadow;
    GlowGradients glowGradients{};
};

class PreparedBitmapGuide {
public:
    PreparedBitmapGuide(const BitmapGuide&, std::size_t& remainingHaloBytes);
    bool wipes() const { return bool(after_); }
    const SkRect& box() const { return box_; }
    float clipPad() const { return clipPad_; }
    bool usesLineFront() const { return useLineFront_; }
    void draw(SkCanvas&, bool after, float opacity, int mediaTimeMs = 0) const;

private:
    struct Halo {
        sk_sp<SkImage> image; // Premultiplied tint blurred at native resolution, padded.
        float pad = 0;        // Native padding on each side.
    };
    std::vector<Halo> prepareHalo(const SkImage&, const GlowState&, const sk_sp<SkShader>& gradient,
                                  std::size_t& remainingBytes) const;
    sk_sp<SkImage> before_, after_;
    SkRect box_;
    float clipPad_;
    bool useLineFront_;
    std::array<std::vector<std::vector<Halo>>, 2> halos_;
    std::array<std::shared_ptr<const BitmapGuideAnimation>, 2> animations_;
    int animationAnchorMs_;
    std::array<ShadowState, 2> shadow_;
};
}
