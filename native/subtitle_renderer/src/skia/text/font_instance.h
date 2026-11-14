#pragma once
#include "include/core/SkFont.h"
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace subtitle::skia::text {
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
    const SkFont& shapingFont() const { return font_; }
    float horizontalScale() const { return scaleX_; }
    const std::string& identity() const { return identity_; }
    float ascent() const;
    float descent() const;
    float leading() const;
private:
    SkFont font_;
    float scaleX_ = 1;
    std::string identity_;
};
}
