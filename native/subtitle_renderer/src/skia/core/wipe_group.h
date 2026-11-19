#pragma once
#include "resolved_scene.h"
#include "include/core/SkRect.h"

class SkCanvas;
namespace subtitle::skia {
class PreparedWipeGroup {
public:
    explicit PreparedWipeGroup(const ResolvedRubyGroup&, bool noWipe = false, bool vertical = false,
                               std::optional<std::size_t> glowOffset = std::nullopt);
    void draw(SkCanvas&, int timeMs, const SingingScale&, float originX) const;
    void drawShadow(SkCanvas&, int timeMs, const SingingScale&, float originX) const;
    void drawGlowSource(SkCanvas&, const PreparedGlow&, int timeMs, bool after) const;
private:
    void drawSinging(SkCanvas&, int timeMs, const SingingScale&, float originX, bool afterVisible) const;
    float frontAt(int timeMs, float startEdge) const;
    std::vector<ResolvedCell> cells_;
    StaticShadow shadow_;
    std::optional<std::size_t> glowOffset_;
    SkRect bounds_;
    bool antialiasClip_ = false;
    bool noWipe_ = false;
    bool vertical_ = false;
};
}
