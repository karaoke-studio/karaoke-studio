#pragma once
#include "resolved_scene.h"
#include "include/core/SkRect.h"

class SkCanvas;
namespace subtitle::skia {
class PreparedWipeGroup {
public:
    explicit PreparedWipeGroup(const std::vector<ResolvedCell>&, bool noWipe = false, bool vertical = false);
    void draw(SkCanvas&, int timeMs, const SingingScale&, float originX) const;
private:
    void drawSinging(SkCanvas&, int timeMs, const SingingScale&, float originX, bool afterVisible) const;
    std::vector<ResolvedCell> cells_;
    SkRect bounds_;
    bool antialiasClip_ = false;
    bool noWipe_ = false;
    bool vertical_ = false;
};
}
