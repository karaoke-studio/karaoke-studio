#pragma once
#include "include/core/SkPath.h"
#include "character_fade.h"
#include "character_geometry.h"
#include "character_transform.h"
#include "line_animation.h"
#include "noise_transition.h"
#include "scanline_band.h"
#include "sing_particles.h"
#include "solid_outline.h"
#include "subtitle_gradient.h"
#include "image_fill.h"
#include "static_shadow.h"
#include "static_glow.h"
#include "singing_scale.h"
#include "utopia_transition.h"
#include "../../model/wipe_point.h"
#include <cstdint>
#include <optional>
#include <vector>

namespace subtitle::skia {
// Final physical geometry. No font lookup, shaping, layout or timing allocation
// is allowed in PreparedScene's resolved-input path or in a frame draw.
struct ResolvedCell {
    SkPath path;
    float left = 0, right = 0;
    int startMs = 0, endMs = 0;
    std::uint32_t before = 0, after = 0;
    std::vector<krok::subtitle::native::WipePoint> wipePoints;
    bool adjustedWipeEnd = false; // Configuration-only validation marker; never changes time.
    float top = 0, bottom = 0; // Fixed vertical cell box; wipe axis on vertical lines only.
    std::optional<SolidOutline> outline;
    std::optional<SolidOutline> outerOutline;
    CharacterPivot pivot;
    float strokeFlashWidth = 0;
};
struct ResolvedLine {
    std::vector<ResolvedCell> cells;
    int startMs = 0, endMs = 0;
    struct Translation { int startMs, endMs; float x, y; };
    std::vector<Translation> translations;
    // One continuous wipe front clips every path in the group. Unit clocks
    // are final values; neither grouping nor timing is resolved while drawing.
    std::vector<std::vector<ResolvedCell>> rubyGroups;
    bool noWipe = false; // Whole-cell color switches at the final clock endpoint.
    bool wipeRightToLeft = false; // Physical wipe direction, never authored clock ownership or text shaping.
    // vertical_solid_schema=1: the front runs along y over each cell's top/bottom;
    // wipeRightToLeft then means bottom-to-top (the existing GPU's reverse vertical).
    bool vertical = false;
    LineAnimation animation;
    sk_sp<SkShader> beforeGradient, afterGradient;
    sk_sp<SkShader> beforeOutlineGradient, afterOutlineGradient, beforeOuterGradient, afterOuterGradient;
    LineImageFills images; // Device-anchored static image paints; take precedence over gradients.
    StaticShadow shadow;
    StaticGlow glow;
    NoiseTransition noise;
    CharacterFade characterFade;
    CharacterGeometry characterGeometry;
    SingingScale singingScale;
    UtopiaTransition utopiaTransition;
    bool transformedWipe = false;
    ScanlineBand scanline;
    std::vector<SingParticleBurst> particles;
    ParticleVisibility particleVisibility;
};
struct ResolvedSubtitleInput {
    int width = 0, height = 0;
    std::vector<ResolvedLine> lines;
};
}
