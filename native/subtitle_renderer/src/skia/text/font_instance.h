#pragma once
#include "include/core/SkFont.h"
#include "glyph_outline.h"
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>
struct hb_font_t;

namespace subtitle::skia::text {
// DirectWrite's synthetic oblique shear in y-down font coordinates.
inline constexpr float kSyntheticItalicSkewX = -87.f / 256.f;
// DirectWrite adds 2% of an em, rounded to a whole design unit.
inline constexpr int syntheticBoldDesignUnits(int em) { return (em + 25) / 50; }

struct Axis { std::uint32_t tag; float value; };
struct FontResource {
    std::vector<std::uint8_t> bytes;
    std::string sha256; // Computed from the actual bytes by the resource boundary.
    int faceIndex = 0;
    std::vector<Axis> axes;
};
struct FontSpec {
    std::shared_ptr<const FontResource> resource;
    float size = 48;
    float scaleX = 1;
    bool syntheticBold = false;
    bool syntheticItalic = false;
};
class FontInstance {
public:
    explicit FontInstance(const FontSpec& spec);
    ~FontInstance();
    GlyphGeometry glyphGeometry(SkGlyphID) const;
    const SkFont& shapingFont() const { return font_; }
    float horizontalScale() const { return scaleX_; }
    const std::string& identity() const { return identity_; }
    hb_font_t* shapingFace() const { return shapingFace_.get(); }
    float ascent() const;
    float descent() const;
    float leading() const;
    float capHeight() const;
private:
    SkFont font_;
    float scaleX_ = 1;
    std::string identity_;
    std::unique_ptr<GlyphOutlineSource> outlines_;
    std::shared_ptr<hb_font_t> shapingFace_;
    std::optional<float> lineAscent_, lineDescent_; // GPU line-box metrics (see constructor).
};
}
