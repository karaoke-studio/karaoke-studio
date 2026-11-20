#pragma once

#include "../../model/paint_types.h"
#include "include/core/SkRect.h"
#include <cstdint>
#include <optional>
#include <string>

class SkCanvas;
namespace subtitle::skia {
// scanline_shape_schema=1: the existing GPU's procedural flame column and
// rising bubbles at the line's scanline front, in the solid `color` mode.
// The flame raster comes from the shared effects math; geometry, colours and
// the display-window fade mirror the GPU exactly. Drawn after text and ruby.
struct ScanlineShape {
    enum class Kind : std::uint8_t { None, Flame, Bubble, Image };
    Kind kind = Kind::None;
    float bandWidth = 1;   // Physical scanline width.
    float fontPx = 1;      // Physical font size of the line style.
    krok::subtitle::native::RgbaColor outer, inner, core;
    int displayStart = 0, displayEnd = 0, lastCharEnd = 0;

    explicit operator bool() const { return kind != Kind::None; }
    static ScanlineShape make(const std::string& shape, krok::subtitle::native::RgbaColor base, float bandWidth,
                              float fontPx, int displayStart, int displayEnd, int lastCharEnd);
    // Shape opacity; it also scales the ordinary band while a shape is active.
    float fadeAt(int timeMs) const;
    void draw(SkCanvas&, int timeMs, float front, const SkRect& bodyInk, std::optional<float> rubyTop) const;
};
}
