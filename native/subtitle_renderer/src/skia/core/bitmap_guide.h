#pragma once

#include "include/core/SkImage.h"
#include "include/core/SkRect.h"
#include "static_glow.h"
#include "static_shadow.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <list>
#include <memory>
#include <mutex>
#include <unordered_map>
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
// with the state's decoration paint, blurred once per concentration pass and
// drawn with linear sampling over the box before the image itself. Halos are
// built lazily into a scene-wide bounded LRU cache (sharing solid halos across
// lines and downscaling oversized source images to device resolution first).
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
    // The GPU wipes whenever an after path is set, even if its file is missing.
    bool wipes = false;
};

struct BitmapGuideHalo {
    sk_sp<SkImage> image; // Premultiplied tint blurred at working resolution, padded.
    float pad = 0;        // Working-resolution padding on each side.
    int workW = 0;        // Working-resolution image width (native when upscaled, box-capped when downscaled).
    int workH = 0;        // Working-resolution image height.
};
using BitmapGuideHaloPasses = std::vector<BitmapGuideHalo>;

class BitmapHaloCache {
public:
    struct Key {
        std::uint32_t imageId = 0;
        std::uintptr_t gradientOwner = 0;
        bool afterPhase = false;
        int workW = 0;
        int workH = 0;
        std::int64_t scaleXQ64 = 0;
        std::int64_t scaleYQ64 = 0;
        int radius = 0;
        int concentration = 0;
        std::uint32_t tint = 0;
        std::int64_t strengthQ1000 = 1000;
        bool operator==(const Key&) const = default;
    };

    explicit BitmapHaloCache(std::size_t maxBytes = std::size_t(256) << 20);
    std::size_t maxBytes() const { return maxBytes_; }
    std::size_t usedBytes() const;
    std::size_t entryCount() const;
    std::shared_ptr<const BitmapGuideHaloPasses> find(const Key& key);
    std::shared_ptr<const BitmapGuideHaloPasses> insert(
        const Key& key, std::size_t bytes, std::shared_ptr<const BitmapGuideHaloPasses> passes);

private:
    struct KeyHash {
        std::size_t operator()(const Key& key) const noexcept;
    };
    struct Entry {
        Key key;
        std::size_t bytes = 0;
        std::shared_ptr<const BitmapGuideHaloPasses> passes;
    };
    std::size_t maxBytes_;
    std::size_t usedBytes_ = 0;
    std::list<Entry> lru_;
    std::unordered_map<Key, std::list<Entry>::iterator, KeyHash> index_;
    mutable std::mutex mutex_;
};

class PreparedBitmapGuide {
public:
    PreparedBitmapGuide(const BitmapGuide&, std::shared_ptr<BitmapHaloCache> haloCache);
    bool wipes() const { return wipes_; }
    const SkRect& box() const { return box_; }
    float clipPad() const { return clipPad_; }
    bool usesLineFront() const { return useLineFront_; }
    void draw(SkCanvas&, bool after, float opacity, int mediaTimeMs = 0) const;

private:
    struct HaloGeometry {
        int workW = 0;
        int workH = 0;
        float workScaleX = 1.f;
        float workScaleY = 1.f;
        bool downscale = false;
        float pad = 0.f;
        int margin = 0;
        int width = 0;
        int height = 0;
        int count = 0;
        std::size_t bytes = 0;
    };
    HaloGeometry haloGeometry(const SkImage&, const GlowState&) const;
    std::shared_ptr<const BitmapGuideHaloPasses> resolveHalo(const SkImage&, bool after) const;
    BitmapGuideHaloPasses buildHalo(const SkImage&, const GlowState&, const sk_sp<SkShader>& gradient,
                                    const HaloGeometry&) const;
    sk_sp<SkImage> before_, after_;
    bool wipes_;
    bool decorate_;
    SkRect box_;
    float clipPad_;
    bool useLineFront_;
    StaticGlow glow_;
    GlowGradients glowGradients_;
    std::shared_ptr<BitmapHaloCache> haloCache_;
    std::array<std::shared_ptr<const BitmapGuideAnimation>, 2> animations_;
    int animationAnchorMs_;
    std::array<ShadowState, 2> shadow_;
};
}
